"""The offline OCR observation worker — Tesseract CLI, pinned and airgapped.

Produces an OBSERVATION, never a reading (RULES.md 34;
``docs/audits/learned-source/OCR_DECISION.md`` is the decision record).
Offline is enforced: the subprocess gets an EXPLICIT environment built
from nothing (no inherited proxy or tessdata), :class:`OcrConfig`'s
``allow_network`` may only be ``False``, and the invocation is
resource-capped per the decision record's profile. Absence of the binary
is a refusal (:func:`discover_worker` returns ``None``), never a crash or
a silent skip. PHI: nothing here logs; the manifest carries only
versions, hashes and counts — never a token or an input path (hOCR
embeds the image filename it was given, so the caller renders to a
fixed, non-descriptive name)."""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from tempfile import TemporaryDirectory

__all__ = [
    "NATIVE_OR_SYNTHETIC",
    "NATIVE_TEXT",
    "OCR_OBSERVATION",
    "PROVENANCES",
    "TESSERACT_BACKEND_ID",
    "BBox",
    "OcrConfig",
    "OcrEngineError",
    "OcrPageResult",
    "OcrToken",
    "OcrUnavailableError",
    "PageImage",
    "TesseractWorker",
    "discover_worker",
    "find_tesseract",
]

# --------------------------------------------------------------------------- #
# Provenance vocabulary
# --------------------------------------------------------------------------- #

#: A real PDF text object: coordinates, font, weight and color are source
#: evidence. This is the default every span carries.
NATIVE_TEXT = "native_text"

#: A selectable text layer that sits on top of a scan. Extraction succeeded,
#: which is not the same as the text being right — a searchable PDF's layer may
#: itself be somebody else's OCR. Held as evidence, never as truth.
NATIVE_OR_SYNTHETIC = "native_or_synthetic"

#: Recognized from pixels by this worker. Layout evidence only.
OCR_OBSERVATION = "ocr_observation"

#: Every provenance a span or token may carry, for validation.
PROVENANCES = frozenset({NATIVE_TEXT, NATIVE_OR_SYNTHETIC, OCR_OBSERVATION})

#: The one backend this module implements. The decision record keeps
#: RapidOCR/Docling/PaddleOCR as separately-reviewed optional environments;
#: none of them is installed, imported or reachable from here.
TESSERACT_BACKEND_ID = "tesseract-cli"

#: Operator overrides. Both are deliberately explicit: the decision record says
#: not to depend on a mutable system tessdata directory, so an operator that
#: has pinned one names it, and the manifest records which case applied.
TESSERACT_EXE_ENV = "ANAST_OCR_TESSERACT"
TESSDATA_ENV = "ANAST_OCR_TESSDATA"

#: What to tell an operator whose machine has no engine. Actionable and
#: offline: the language data is a file to place, not a download to trigger.
INSTALL_HINT = (
    "no offline OCR engine found: install Tesseract 5 with the 'eng' language "
    f"data and put it on PATH, or set {TESSERACT_EXE_ENV} to the executable "
    f"(and {TESSDATA_ENV} to the tessdata directory). Nothing is downloaded."
)

_VERSION_RE = re.compile(r"^tesseract\s+(\S+)", re.IGNORECASE)

# TSV levels, per Tesseract's documented hierarchy. Level 5 rows are words.
_TSV_WORD_LEVEL = 5
_TSV_COLUMNS = 12

# hOCR: the line-level ``x_size`` is Tesseract's own estimate of the text
# height in pixels, and it is the ONLY size evidence the engine offers. It is
# not a font size — see :func:`_token`. Tesseract quotes ``title`` with single
# quotes on words and double quotes on lines, so both are accepted.
_HOCR_SPAN_RE = re.compile(
    r"<span class=['\"](?P<cls>[a-z_]+)['\"][^>]*"
    r"title=['\"](?P<title>[^'\"]*)['\"][^>]*>(?P<tail>[^<]*)"
)
_HOCR_X_SIZE_RE = re.compile(r"x_size\s+([0-9.]+)")

_PT_PER_IN = 72.0

#: ``(x0, y0, x1, y1)`` in one named coordinate frame — never a mix of two.
BBox = tuple[float, float, float, float]


class OcrUnavailableError(RuntimeError):
    """No offline OCR engine is installed, or the one named is unusable.

    A refusal, not a failure: the message is :data:`INSTALL_HINT`, which names
    exactly what to place on the machine and says nothing is fetched.
    """


class OcrEngineError(RuntimeError):
    """The engine ran and did not produce a usable observation: a timeout, a
    non-zero exit, a missing output file, or TSV/hOCR disagreeing on word
    count. Never a partial success; the message carries counts and exit
    codes only, never recognized text or an input path."""


# --------------------------------------------------------------------------- #
# Configuration and the pinned invocation
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class OcrConfig:
    """Every knob of the invocation, fixed up front and hashed. Defaults
    are the decision record's resource profile (216 DPI, 25-megapixel
    cap, 60s deadline). ``allow_network`` may only be ``False``.
    ``min_confidence`` SELECTS candidates; it never deletes evidence —
    tokens below it are still returned, flagged, and counted."""

    language: str = "eng"
    dpi: int = 216
    page_segmentation: str = "3"
    max_pixels: int = 25_000_000
    timeout_seconds: int = 60
    thread_count: int = 1
    min_confidence: float = 40.0
    allow_network: bool = False

    def __post_init__(self) -> None:
        if self.allow_network:
            raise ValueError("the OCR worker is offline-only: allow_network cannot be True")
        if self.dpi <= 0 or self.max_pixels <= 0 or self.timeout_seconds <= 0:
            raise ValueError("OCR dpi, max_pixels and timeout_seconds must be positive")
        if self.thread_count != 1:
            raise ValueError("the OCR worker runs one page per process: thread_count must be 1")

    @property
    def config_sha256(self) -> str:
        """A digest of the whole invocation, for the observation manifest."""
        canonical = "|".join(
            (
                TESSERACT_BACKEND_ID,
                self.language,
                str(self.dpi),
                self.page_segmentation,
                str(self.max_pixels),
                str(self.timeout_seconds),
                str(self.thread_count),
                f"{self.min_confidence:.3f}",
                "allow_network=False",
            )
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def dpi_for(self, width_pt: float, height_pt: float) -> int:
        """The DPI to render at, reduced deterministically to fit the cap:
        scale so ``width_px * height_px <= max_pixels``, floor at 1 DPI,
        never exceed the configured DPI. A smaller returned number means
        the page was downsampled, and by how much."""
        area_in2 = max((width_pt / _PT_PER_IN) * (height_pt / _PT_PER_IN), 1e-9)
        cap_dpi = int((self.max_pixels / area_in2) ** 0.5)
        return max(1, min(self.dpi, cap_dpi))


@dataclass(frozen=True)
class PageImage:
    """The one raster a recognition ran against, and how to get back to
    points. ``clip_pt`` plus ``dpi_x``/``dpi_y`` is the whole transform (no
    hidden state); ``bytes_sha256`` pins the exact pixels described."""

    page_index: int
    region_id: str
    bytes_sha256: str
    width_px: int
    height_px: int
    dpi_x: float
    dpi_y: float
    rotation_deg: float
    page_width_pt: float
    page_height_pt: float
    clip_pt: BBox

    def to_page_pt(self, bbox_px: BBox) -> BBox:
        """Map an image-pixel box (top-left origin) into page points."""
        x0, y0, x1, y1 = bbox_px
        ox, oy = self.clip_pt[0], self.clip_pt[1]
        sx, sy = _PT_PER_IN / self.dpi_x, _PT_PER_IN / self.dpi_y
        return (
            round(ox + x0 * sx, 1),
            round(oy + y0 * sy, 1),
            round(ox + x1 * sx, 1),
            round(oy + y1 * sy, 1),
        )


@dataclass(frozen=True)
class OcrToken:
    """One recognized word, with its geometry in both frames and its score.
    ``text`` is a protected observation: shown to a reviewer, never logged
    or promoted into a clinical field. ``confidence_kind`` keeps two
    engines' scores from being compared as if calibrated; ``confidence``
    is ``None`` rather than a fabricated score when an engine has none."""

    text: str
    bbox_px: BBox
    bbox_page_pt: BBox
    size_pt: float
    line_id: str
    block_id: str
    confidence: float | None
    confidence_kind: str
    page_index: int
    region_id: str
    source: str = OCR_OBSERVATION


@dataclass(frozen=True)
class OcrPageResult:
    """Everything one recognition produced, including what it did not keep.
    ``tokens`` holds EVERY token, low-scoring ones included;
    ``below_confidence`` counts how many fall under the selection
    threshold. ``warnings`` are PHI-free strings."""

    tokens: tuple[OcrToken, ...]
    page_image: PageImage
    engine_version: str
    config_sha256: str
    tessdata_source: str
    below_confidence: int
    warnings: tuple[str, ...]
    timings_ms: Mapping[str, int] = field(default_factory=dict)
    full_page_fallback: bool = False

    def selected(self, config: OcrConfig) -> tuple[OcrToken, ...]:
        """Tokens at or above the configured confidence — layout candidates."""
        return tuple(above_threshold(self.tokens, config))


def above_threshold(tokens: Sequence[OcrToken], config: OcrConfig) -> list[OcrToken]:
    """The tokens a layout learner may use, by the ONE selection
    predicate — selection, never deletion: the caller keeps the full list
    and records how many fell below. A missing score is not a low one, so
    an unscored token is kept rather than fabricating a number to compare
    it against."""
    return [
        token
        for token in tokens
        if token.confidence is None or token.confidence >= config.min_confidence
    ]


# --------------------------------------------------------------------------- #
# Engine discovery
# --------------------------------------------------------------------------- #


def find_tesseract() -> Path | None:
    """The Tesseract executable to use, or ``None``. An operator-named
    :data:`TESSERACT_EXE_ENV` wins and is NOT searched for on PATH — a
    nonexistent named path surfaces as "no engine", not a silent PATH
    fallback. Otherwise ``shutil.which`` resolves it, made absolute so the
    invocation never depends on the child's working directory."""
    named = os.environ.get(TESSERACT_EXE_ENV)
    if named:
        candidate = Path(named)
        return candidate.resolve() if candidate.is_file() else None
    found = shutil.which("tesseract")
    return Path(found).resolve() if found else None


def discover_worker(config: OcrConfig | None = None) -> TesseractWorker | None:
    """The OCR worker for this machine, or ``None`` — the caller turns
    that into a refusal naming what to install (:data:`INSTALL_HINT`),
    rather than crashing or quietly producing a pack with pages missing."""
    exe = find_tesseract()
    if exe is None:
        return None
    try:
        return TesseractWorker(exe, config or OcrConfig())
    except OcrUnavailableError:
        return None


def _explicit_env(config: OcrConfig, tessdata: str | None) -> dict[str, str]:
    """The child's ENTIRE environment, built from nothing: no ``*_PROXY``,
    no unnamed ``TESSDATA_PREFIX``, no locale that could re-order output.
    ``OMP_THREAD_LIMIT=1`` is the decision record's concurrency control;
    ``SystemRoot`` is passed through because Windows needs it to start."""
    env = {"OMP_THREAD_LIMIT": str(config.thread_count), "LC_ALL": "C"}
    if tessdata:
        env["TESSDATA_PREFIX"] = tessdata
    for passthrough in ("SystemRoot", "windir"):
        value = os.environ.get(passthrough)
        if value:
            env[passthrough] = value
    return env


class TesseractWorker:
    """A pinned, offline Tesseract CLI invocation, one page image at a
    time. Probes ``--version`` once so a broken binary refuses immediately
    rather than at the first page; the version string is part of every
    result's manifest."""

    def __init__(self, exe: Path, config: OcrConfig | None = None) -> None:
        self.exe = exe
        self.config = config or OcrConfig()
        self.tessdata = os.environ.get(TESSDATA_ENV) or None
        self.engine_version = self._probe_version()

    @property
    def tessdata_source(self) -> str:
        """Where language data came from — an operator pin, or the
        default. The decision record wants a hash-pinned tessdata
        directory; this records which case applied so a pack never
        implies a pin it does not have."""
        return "operator-pinned" if self.tessdata else "engine-default"

    def manifest(self) -> dict[str, object]:
        """The PHI-free provenance block a pack records about this engine."""
        return {
            "backend_id": TESSERACT_BACKEND_ID,
            "engine_version": self.engine_version,
            "language": self.config.language,
            "dpi": self.config.dpi,
            "page_segmentation": self.config.page_segmentation,
            "timeout_seconds": self.config.timeout_seconds,
            "thread_count": self.config.thread_count,
            "max_pixels": self.config.max_pixels,
            "min_confidence": self.config.min_confidence,
            "allow_network": False,
            "tessdata": self.tessdata_source,
            "config_sha256": self.config.config_sha256,
        }

    def _probe_version(self) -> str:
        try:
            completed = self._run([str(self.exe), "--version"], timeout=15)
        except (OSError, subprocess.SubprocessError) as exc:
            raise OcrUnavailableError(INSTALL_HINT) from exc
        if completed.returncode != 0:
            raise OcrUnavailableError(INSTALL_HINT)
        first = (completed.stdout or completed.stderr).splitlines()
        match = _VERSION_RE.match(first[0].strip()) if first else None
        if match is None:
            raise OcrUnavailableError(INSTALL_HINT)
        return f"tesseract {match.group(1)}"

    def _run(self, argv: list[str], *, timeout: int) -> subprocess.CompletedProcess[str]:
        """Run the pinned executable with an explicit, network-free env."""
        return subprocess.run(  # noqa: S603 - absolute exe, shell=False, fixed argv
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env=_explicit_env(self.config, self.tessdata),
        )

    def recognize(self, png_bytes: bytes, page_image: PageImage) -> OcrPageResult:
        """Recognizes one page image and returns its observation. The image
        is written under a FIXED, non-descriptive filename (Tesseract's
        hOCR embeds the input path in the page title). TSV and hOCR are
        both requested and cross-checked; a disagreement about word count
        is an :class:`OcrEngineError`, not a quietly-preferred stream."""
        pixels = page_image.width_px * page_image.height_px
        if pixels > self.config.max_pixels:
            raise OcrEngineError(
                f"page image {pixels} px exceeds the configured cap {self.config.max_pixels}"
            )
        started = time.monotonic()
        with TemporaryDirectory(prefix="anast-ocr-") as tmp:
            stem = Path(tmp) / "page"
            image_path = stem.with_suffix(".png")
            image_path.write_bytes(png_bytes)
            try:
                completed = self._run(
                    [
                        str(self.exe),
                        str(image_path),
                        str(stem),
                        "-l",
                        self.config.language,
                        "--psm",
                        self.config.page_segmentation,
                        "--dpi",
                        str(round(page_image.dpi_x)),
                        "tsv",
                        "hocr",
                    ],
                    timeout=self.config.timeout_seconds,
                )
            except subprocess.TimeoutExpired:
                # One of the four engine faults this class promises to raise
                # as its own, so a hung engine is never rewrapped upstream as
                # "sample unreadable".
                raise OcrEngineError(
                    f"OCR engine exceeded the {self.config.timeout_seconds}s page deadline"
                ) from None
            if completed.returncode != 0:
                raise OcrEngineError(f"OCR engine exited {completed.returncode}")
            tsv_path, hocr_path = stem.with_suffix(".tsv"), stem.with_suffix(".hocr")
            if not tsv_path.is_file() or not hocr_path.is_file():
                raise OcrEngineError("OCR engine produced no TSV/hOCR output")
            rows = _parse_tsv(tsv_path.read_text(encoding="utf-8", errors="replace"))
            sizes = _parse_hocr_line_sizes(hocr_path.read_text(encoding="utf-8", errors="replace"))
        elapsed_ms = int((time.monotonic() - started) * 1000)
        return self._assemble(rows, sizes, page_image, elapsed_ms)

    def _assemble(
        self,
        rows: list[_TsvRow],
        line_sizes: list[float],
        page_image: PageImage,
        elapsed_ms: int,
    ) -> OcrPageResult:
        """Join the TSV word stream to the hOCR line heights, or fail loudly."""
        if len(rows) != len(line_sizes):
            raise OcrEngineError(
                f"OCR streams disagree: TSV read {len(rows)} words, hOCR read {len(line_sizes)}"
            )
        tokens = tuple(
            _token(row, size_px, page_image) for row, size_px in zip(rows, line_sizes, strict=True)
        )
        below = sum(
            1
            for token in tokens
            if token.confidence is not None and token.confidence < self.config.min_confidence
        )
        warnings: list[str] = []
        if self.tessdata is None:
            warnings.append("tessdata is the engine default; no operator hash pin was supplied")
        if below:
            warnings.append(
                f"{below} token(s) below the {self.config.min_confidence:g} confidence "
                "threshold were retained as evidence but not selected as layout candidates"
            )
        return OcrPageResult(
            tokens=tokens,
            page_image=page_image,
            engine_version=self.engine_version,
            config_sha256=self.config.config_sha256,
            tessdata_source=self.tessdata_source,
            below_confidence=below,
            warnings=tuple(warnings),
            timings_ms={"total": elapsed_ms},
        )


# --------------------------------------------------------------------------- #
# Output parsing
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class _TsvRow:
    """One level-5 (word) row of Tesseract's TSV, already typed."""

    block: int
    line: int
    left: int
    top: int
    width: int
    height: int
    conf: float
    text: str


def _parse_tsv(raw: str) -> list[_TsvRow]:
    """Every non-blank word row of a Tesseract TSV, in engine order.
    Blank-text rows (``conf`` -1) carry no observation and are dropped
    here; the hOCR side drops the same ones, keeping the two counts
    comparable."""
    rows: list[_TsvRow] = []
    for line in raw.splitlines()[1:]:  # first line is the header
        fields = line.split("\t")
        if len(fields) < _TSV_COLUMNS or fields[0] != str(_TSV_WORD_LEVEL):
            continue
        text = fields[11].strip()
        if not text:
            continue
        rows.append(
            _TsvRow(
                block=int(fields[2]),
                line=int(fields[4]),
                left=int(fields[6]),
                top=int(fields[7]),
                width=int(fields[8]),
                height=int(fields[9]),
                conf=float(fields[10]),
                text=text,
            )
        )
    return rows


def _parse_hocr_line_sizes(raw: str) -> list[float]:
    """One ``x_size`` (pixels) per non-blank word, in TSV order — the TSV
    alone has no size evidence at all. Only ``<span>`` elements carry it, in
    two kinds (a line-ish container, or ``ocrx_word``), so a word takes the
    most recent container's ``x_size``. Blank words are skipped on both
    sides, keeping the two counts comparable."""
    sizes: list[float] = []
    current = 0.0
    for match in _HOCR_SPAN_RE.finditer(raw):
        if match.group("cls") == "ocrx_word":
            if match.group("tail").strip():
                sizes.append(current)
            continue
        size_match = _HOCR_X_SIZE_RE.search(match.group("title"))
        current = float(size_match.group(1)) if size_match else 0.0
    return sizes


def _token(row: _TsvRow, size_px: float, page_image: PageImage) -> OcrToken:
    """One TSV row plus its hOCR line height as a provenance-carrying
    token. ``size_pt`` is a HEIGHT in points, not a font size — Tesseract
    recovers no face, weight or color — but it is enough to rank a
    heading above body text, which is all a layout learner asks of it."""
    bbox_px: BBox = (
        float(row.left),
        float(row.top),
        float(row.left + row.width),
        float(row.top + row.height),
    )
    size_pt = round(size_px * _PT_PER_IN / page_image.dpi_y * 2) / 2
    return OcrToken(
        text=row.text,
        bbox_px=bbox_px,
        bbox_page_pt=page_image.to_page_pt(bbox_px),
        size_pt=size_pt,
        line_id=f"{page_image.region_id}:b{row.block}:l{row.line}",
        block_id=f"{page_image.region_id}:b{row.block}",
        confidence=round(row.conf, 2),
        confidence_kind="tesseract_conf",
        page_index=page_image.page_index,
        region_id=page_image.region_id,
    )
