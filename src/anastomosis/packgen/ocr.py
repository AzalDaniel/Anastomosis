"""The offline OCR observation worker — Tesseract CLI, pinned and airgapped.

Fifty-three sample PDFs, 802 pages, zero natively extractable words: the one
real sample set this product has been shown is pixels. :mod:`.extract` refuses
such a page loudly (:class:`~anastomosis.packgen.extract.OcrRequiredError`) and
that refusal stays — it is now conditional on there being no engine to ask.
This module is the engine adapter that makes the other half possible.

What it produces is an OBSERVATION, never a reading. The distinction is carried
in the data (:data:`OCR_OBSERVATION` rides every token and every span derived
from one), not in this docstring: OCR geometry may suggest lines, columns,
bands and page breaks; OCR text may never fill a clinical field. A high-risk
value needs an independent structured source or a human. ``docs/audits/
learned-source/OCR_DECISION.md`` is the decision record this implements.

Offline is enforced, not promised. The subprocess is handed an EXPLICIT
environment built from nothing — no inherited ``*_PROXY``, no inherited
``TESSDATA_PREFIX`` unless the operator named one — the invocation passes only
local file paths, and :class:`OcrConfig` carries ``allow_network`` which may
only ever be ``False`` (a ``True`` is refused at construction). Tesseract is
also told ``OMP_THREAD_LIMIT=1`` and given a finite timeout and a finite pixel
cap, per the decision record's resource profile.

Absence of the binary is a REFUSAL, never a crash and never a silent skip:
:func:`discover_worker` returns ``None`` and the caller says what to install.

PHI rule: token text is a protected observation. Nothing in this module logs,
and the manifest / warnings it returns carry versions, hashes, counts and
integers only — never a token, never an input path. Tesseract's own hOCR
embeds the image filename it was given, which is why the caller renders to a
fixed, non-descriptive temporary name and why the raw hOCR is parsed and
dropped rather than retained.
"""

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
    """The engine ran and did not produce a usable observation.

    A timeout, a non-zero exit, a missing output file, or TSV and hOCR
    disagreeing about how many words were read. Every one of those is a review
    item, never a partial success — the message carries counts and exit codes
    only, never recognized text and never an input path.
    """


# --------------------------------------------------------------------------- #
# Configuration and the pinned invocation
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class OcrConfig:
    """Every knob of the invocation, fixed up front and hashed.

    Defaults are the decision record's resource profile: 216 DPI (3 x 72-point
    PDF units), a 25-megapixel page cap, one page per process with
    ``OMP_THREAD_LIMIT=1``, and a 60-second page deadline. ``allow_network`` is
    part of the schema so the manifest can state it, and may only be ``False``.

    ``min_confidence`` SELECTS layout candidates; it never deletes evidence.
    Tokens below it are still returned, flagged, and counted (see
    :attr:`OcrPageResult.below_confidence`).
    """

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
        """The DPI to render at, reduced deterministically to fit the cap.

        The rule is recorded rather than implicit: scale so that
        ``width_px * height_px <= max_pixels``, floor at 1 DPI, and never
        exceed the configured DPI. A caller that gets back a smaller number
        knows the page was downsampled and by how much.
        """
        area_in2 = max((width_pt / _PT_PER_IN) * (height_pt / _PT_PER_IN), 1e-9)
        cap_dpi = int((self.max_pixels / area_in2) ** 0.5)
        return max(1, min(self.dpi, cap_dpi))


@dataclass(frozen=True)
class PageImage:
    """The one raster a recognition ran against, and how to get back to points.

    ``clip_pt`` is the region's box in the PDF page's own coordinate frame;
    ``dpi_x``/``dpi_y`` and that box are the whole transform, so a pixel box
    maps back to page points with no hidden state. ``bytes_sha256`` pins the
    exact pixels the observation describes.
    """

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

    ``text`` is a protected observation: it may be shown to a reviewer beside
    the image, and it may never be logged or promoted into a clinical field.
    ``confidence_kind`` names the scale so two engines' numbers are never
    compared as if they shared a calibration; ``confidence`` is nullable
    because an engine without a documented score must report ``None`` rather
    than a fabricated one.
    """

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

    ``tokens`` holds EVERY token the engine returned, low-scoring ones
    included; ``below_confidence`` counts how many fall under the configured
    selection threshold, so a filtered candidate list always has a stated
    reason and a number. ``warnings`` are PHI-free strings.
    """

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
    """The tokens a layout learner may use, by the ONE selection predicate.

    Selection, never deletion: the caller keeps the full token list and records
    how many fell below. An engine that reports no score at all is kept — a
    missing score is not a low one, and fabricating a number to compare it
    against is exactly what the decision record forbids.
    """
    return [
        token
        for token in tokens
        if token.confidence is None or token.confidence >= config.min_confidence
    ]


# --------------------------------------------------------------------------- #
# Engine discovery
# --------------------------------------------------------------------------- #


def find_tesseract() -> Path | None:
    """The Tesseract executable to use, or ``None`` when there is none.

    An operator-named :data:`TESSERACT_EXE_ENV` wins and is NOT searched for on
    PATH — naming a path that does not exist is an operator error worth
    surfacing as "no engine" rather than silently falling back to whatever the
    PATH happens to hold. Otherwise ``shutil.which`` resolves it, and the
    result is made absolute so the invocation never depends on the child's
    working directory.
    """
    named = os.environ.get(TESSERACT_EXE_ENV)
    if named:
        candidate = Path(named)
        return candidate.resolve() if candidate.is_file() else None
    found = shutil.which("tesseract")
    return Path(found).resolve() if found else None


def discover_worker(config: OcrConfig | None = None) -> TesseractWorker | None:
    """The OCR worker for this machine, or ``None`` when no engine is present.

    ``None`` is the whole point: the caller turns it into a refusal that names
    what to install (:data:`INSTALL_HINT`), rather than crashing or — far
    worse — quietly producing a pack with the raster pages missing.
    """
    exe = find_tesseract()
    if exe is None:
        return None
    try:
        return TesseractWorker(exe, config or OcrConfig())
    except OcrUnavailableError:
        return None


def _explicit_env(config: OcrConfig, tessdata: str | None) -> dict[str, str]:
    """The child's ENTIRE environment, built from nothing.

    Nothing is inherited: no ``*_PROXY``, no ``TESSDATA_PREFIX`` the operator
    did not name, no locale that could re-order output. ``OMP_THREAD_LIMIT=1``
    is the decision record's concurrency control. On Windows ``SystemRoot`` is
    required for the process to start at all, so it is the one passthrough.
    """
    env = {"OMP_THREAD_LIMIT": str(config.thread_count), "LC_ALL": "C"}
    if tessdata:
        env["TESSDATA_PREFIX"] = tessdata
    for passthrough in ("SystemRoot", "windir"):
        value = os.environ.get(passthrough)
        if value:
            env[passthrough] = value
    return env


class TesseractWorker:
    """A pinned, offline Tesseract CLI invocation, one page image at a time.

    Constructed with an absolute executable path; probes ``--version`` once so
    a broken or non-Tesseract binary refuses immediately rather than at the
    first page. The version string it read is part of every result's manifest.
    """

    def __init__(self, exe: Path, config: OcrConfig | None = None) -> None:
        self.exe = exe
        self.config = config or OcrConfig()
        self.tessdata = os.environ.get(TESSDATA_ENV) or None
        self.engine_version = self._probe_version()

    @property
    def tessdata_source(self) -> str:
        """Where language data came from — an operator pin, or the default.

        The decision record wants a hash-pinned, immutable tessdata directory.
        This records WHICH case applied so a pack never implies a pin it does
        not have; ``engine-default`` means the engine's own compiled-in path.
        """
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
        """Recognize one page image and return its observation.

        The image is written under a FIXED, non-descriptive filename in a
        private temporary directory: Tesseract's hOCR embeds the input path in
        the page title, so the name must carry nothing sample-derived. Both TSV
        and hOCR are requested in one pass (the decision record's default), and
        the two are cross-checked — a disagreement about how many words were
        read is an :class:`OcrEngineError`, not a quietly-preferred stream.
        """
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

    Blank-text rows carry no observation (Tesseract emits them for empty
    detections with ``conf`` -1); they are dropped here and the hOCR side drops
    the same ones, which is what makes the two counts comparable.
    """
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
    """One ``x_size`` (pixels) per non-blank word, in the same order as the TSV.

    hOCR is requested alongside the TSV because the TSV has no size evidence at
    all: ``x_size`` is Tesseract's own estimate of the line's text height, and
    it is the only thing in either stream from which a type scale can be built.

    Only ``<span>`` elements carry it, and Tesseract emits exactly two kinds of
    them — a line-ish container (``ocr_line``, ``ocr_header``, ``ocr_caption``,
    ``ocr_textfloat``) and ``ocrx_word`` — so a word takes the ``x_size`` of
    the most recent container. Blank words are skipped on both sides, which is
    what makes the TSV and hOCR counts comparable.
    """
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
    """One TSV row plus its hOCR line height as a provenance-carrying token.

    ``size_pt`` is a HEIGHT in points, not a font size: Tesseract recovers no
    face, weight or color, and the decision record is explicit that a scanned
    page's original rendering system is not recoverable. It is enough to rank a
    heading above body text, which is all a layout learner asks of it.
    """
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
