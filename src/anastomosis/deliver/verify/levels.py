"""The L0-L6 verification levels: one small class per check.

Each level is independent and side-effect-free with a
``run(...) -> LevelResult`` method; :mod:`.composite` stacks them. Pre
(L0-L4) proves the local file is intact and this patient's chart, and
that the destination's open chart is the right patient (L4); post (L5-L6)
cross-checks the destination's metadata and round-trips the bytes back.
PHI: ``LevelResult.detail`` carries level/field names, counts and ratios
only, never a patient value (49). PyMuPDF imports lazily.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date
from difflib import SequenceMatcher
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any

from anastomosis.core.hashutil import hash_and_size
from anastomosis.core.identity import date_token_present, name_fragment_present
from anastomosis.core.identity import normalize as _normalize
from anastomosis.core.model import Encounter, Patient
from anastomosis.core.timeutil import all_date_spellings
from anastomosis.deliver.browser.errors import WrongPatientError
from anastomosis.deliver.verify.types import VerifyPolicy
from anastomosis.destinations.base import (
    DestinationPatient,
    DocumentReader,
    MetadataReader,
    UploadItem,
)
from anastomosis.reconstruct.packs import LoadedPack

if TYPE_CHECKING:
    from anastomosis.destinations.base import BannerCheck

__all__ = [
    "L0FileIntegrity",
    "L1PageAndSize",
    "L2IdentityText",
    "L3HeaderFields",
    "L4Banner",
    "L5Metadata",
    "L6RoundTrip",
    "LevelResult",
    "LevelStatus",
    "PdfSnapshot",
    "date_renderings",
    "fuzzy_contains",
]

# A sub-KiB "PDF" is a rendering failure, not a one-page chart: a Chromium
# print of even an empty note is several KiB.
_MIN_PDF_BYTES = 1024

# The identity-match threshold (item 11): 0.88 tolerates rendering noise but
# fails structural changes (added name ~0.79-0.83, "Last, First" ~0.53)
# loudly rather than let a similar-but-wrong name slip through.
_NAME_RATIO = 0.88

# How far past the needle's length each window reaches, so a middle name
# or suffix does not sink the ratio; load-bearing for the ratios above.
_WINDOW_SLACK = 8


class LevelStatus(StrEnum):
    """The outcome of one level — mirrors the QA :class:`Verdict` house style."""

    PASS = "pass"  # noqa: S105 — status label, not a password
    FAIL = "fail"
    SKIP = "skip"


@dataclass(frozen=True)
class LevelResult:
    """The PHI-safe outcome of one verification level: ``detail`` carries
    only level names, counts, ratios and field names, surfaced in
    reports and possibly logged (49).
    """

    level: str  # "L0".."L6"
    status: LevelStatus
    detail: str


# --- shared matching helpers (the boundary-anchored / fuzzy lessons) ---


def fuzzy_contains(needle: str, haystack: str, *, ratio: float = _NAME_RATIO) -> float:
    """Best fuzzy match ratio of ``needle`` anywhere in ``haystack``: a
    token-aligned sliding window (a whole-string ratio drowns a short
    name in a long page), case/whitespace-normalized. Returns the ratio;
    the caller compares against a threshold.
    """
    n = _normalize(needle)
    if not n:
        return 0.0
    hay = _normalize(haystack)
    if not hay:
        return 0.0
    # Boundary-anchored fast path (2), not raw substring: "Ann Li" must not
    # score 1.0 inside "Joann Liang" or "Mary-Ann Li-Wong".
    if name_fragment_present(n, hay):
        return 1.0
    matcher = SequenceMatcher(autojunk=False)
    matcher.set_seq2(n)
    window_len = len(n) + _WINDOW_SLACK
    best = 0.0
    # Token-boundary window starts (past the needle's length, so a middle
    # name/suffix doesn't sink the ratio); linear, not quadratic, in page length.
    start = 0
    for token in hay.split(" "):
        matcher.set_seq1(hay[start : start + window_len])
        best = max(best, matcher.ratio())
        if best >= 1.0:
            break
        start += len(token) + 1  # +1 for the single separating space
    return best


def date_renderings(value: date) -> set[str]:
    """Every chart spelling ``value`` might render as (L2/L3 verify);
    delegates to ``core.timeutil`` so this and QA's ``DataIntegrityCheck``
    cannot drift apart.
    """
    return all_date_spellings(value)


def _date_present(value: date, text: str) -> bool:
    """Whether any candidate rendering of ``value`` appears in ``text``,
    boundary-anchored per spelling (6) so an unpadded DOB does not match
    inside a longer date run.
    """
    return any(date_token_present(s, text) for s in date_renderings(value))


# --- the lazy PyMuPDF gate ---


def _import_pymupdf() -> Any:
    """Import PyMuPDF lazily, raising ``RuntimeError`` naming the
    ``render`` extra if missing, so this module loads without it (75).
    """
    try:
        import pymupdf
    except ImportError as exc:  # pragma: no cover - environment-dependent
        raise RuntimeError(
            "PDF verification needs the render extra: pip install 'anastomosis[render]'"
        ) from exc
    return pymupdf


def _first_page_text(doc: Any) -> str:
    """Page-1 text of an open PyMuPDF document, or "" if it has no pages."""
    for page in doc:
        return str(page.get_text())
    return ""


class PdfSnapshot:
    """One item's parsed PDF facts (page count + page-1 text), shared
    across levels within one phase. Lazy: nothing is parsed until a
    level asks, so L1's sub-KiB floor still rejects without opening the
    file. A level given no snapshot builds its own."""

    __slots__ = ("_page_count", "_page_one_text", "_parsed", "path")

    def __init__(self, path: Path) -> None:
        self.path = path
        self._parsed = False
        self._page_count = 0
        self._page_one_text = ""

    def _parse(self) -> None:
        if self._parsed:
            return
        with _import_pymupdf().open(self.path) as doc:
            # Order matters: read text while the doc is open, so page_count
            # and text describe ONE read of one file.
            page_count = int(doc.page_count)
            text = _first_page_text(doc)
        self._page_count = page_count
        self._page_one_text = text
        self._parsed = True

    @property
    def page_count(self) -> int:
        self._parse()
        return self._page_count

    @property
    def page_one_text(self) -> str:
        self._parse()
        return self._page_one_text


def _snapshot_for(item: UploadItem, snapshot: PdfSnapshot | None) -> PdfSnapshot:
    """The caller's shared snapshot, or a fresh one for a direct level call."""
    if snapshot is not None:
        return snapshot
    return PdfSnapshot(item.file_path)


def _pages_and_text_of_bytes(pymupdf: Any, data: bytes) -> tuple[int, str]:
    """(page_count, page-1 text) of an in-memory PDF for L6's read-back;
    takes the already-imported module so the caller gates the render
    extra before parsing.
    """
    with pymupdf.open(stream=data, filetype="pdf") as doc:
        return int(doc.page_count), _first_page_text(doc)


# --- L0: file integrity (pre, no PyMuPDF) ---


class L0FileIntegrity:
    """The file exists and its bytes still match the manifest: an
    independent re-read (never reuses the engine's preflight digest),
    sharing only the hasher (:func:`hash_and_size`) so the sites cannot
    disagree on chunking. Stdlib only — works without the render extra.
    """

    level = "L0"

    def run(self, item: UploadItem) -> LevelResult:
        path = item.file_path
        if not path.exists():
            return LevelResult(self.level, LevelStatus.FAIL, "file missing")
        try:
            digest, size = hash_and_size(path)
        except OSError:
            return LevelResult(self.level, LevelStatus.FAIL, "file unreadable")
        if size != item.size_bytes:
            return LevelResult(self.level, LevelStatus.FAIL, "size_bytes mismatch")
        if digest != item.sha256:
            return LevelResult(self.level, LevelStatus.FAIL, "sha256 mismatch")
        return LevelResult(self.level, LevelStatus.PASS, "sha256 and size match")


# --- L1: page count + size sanity (pre) ---


class L1PageAndSize:
    """The PDF opens, has >= 1 page, is above the sub-KiB floor, and (when the
    caller declares one) has exactly the expected page count."""

    level = "L1"

    def run(
        self,
        item: UploadItem,
        *,
        expected_pages: int | None = None,
        snapshot: PdfSnapshot | None = None,
        policy: VerifyPolicy = VerifyPolicy.RENDERED_CHART,
    ) -> LevelResult:
        # The sub-KiB floor is about a Chromium PRINT, not a PDF: a source
        # document is whatever the scanner wrote, and L0 already re-hashed it.
        if policy is VerifyPolicy.RENDERED_CHART and item.size_bytes <= _MIN_PDF_BYTES:
            # Below the floor the file never gets opened — the size alone
            # condemns it, so the snapshot stays unparsed.
            return LevelResult(
                self.level, LevelStatus.FAIL, f"size_bytes below {_MIN_PDF_BYTES}-byte floor"
            )
        pages = _snapshot_for(item, snapshot).page_count
        if pages < 1:
            return LevelResult(self.level, LevelStatus.FAIL, "page_count below 1")
        if expected_pages is not None and pages != expected_pages:
            return LevelResult(
                self.level,
                LevelStatus.FAIL,
                f"page_count {pages} != expected {expected_pages}",
            )
        return LevelResult(self.level, LevelStatus.PASS, f"page_count={pages}, size ok")


# --- L2: identity fuzzy text + DOB hard-fail (pre) ---


class L2IdentityText:
    """The wrong-chart defense: name fuzzy-matches page 1 at >= 0.88, and
    the patient's DOB (if set) is a *hard* gate — a right name with a
    wrong or missing DOB fails regardless of the name ratio.
    """

    level = "L2"

    def run(
        self, item: UploadItem, patient: Patient, *, snapshot: PdfSnapshot | None = None
    ) -> LevelResult:
        text = _snapshot_for(item, snapshot).page_one_text
        name = patient.display_name
        if not name:
            return LevelResult(self.level, LevelStatus.SKIP, "patient has no display name")
        ratio = fuzzy_contains(name, text)
        # DOB hard-fail FIRST: a wrong/absent DOB fails even a perfect name.
        if patient.birth_date is not None and not _date_present(patient.birth_date, text):
            return LevelResult(
                self.level,
                LevelStatus.FAIL,
                f"birth_date not on page 1 (name ratio {ratio:.2f})",
            )
        if ratio < _NAME_RATIO:
            return LevelResult(
                self.level,
                LevelStatus.FAIL,
                f"patient_name ratio {ratio:.2f} < {_NAME_RATIO:.2f}",
            )
        return LevelResult(self.level, LevelStatus.PASS, f"patient_name ratio {ratio:.2f}")


# --- L3: pack header fields (pre) ---

# Field names this verifier knows. An entry outside this set is a LOUD
# fail — never a silent skip.
_SUPPORTED_HEADER_FIELDS = frozenset({"patient_name", "dob", "dos"})


class L3HeaderFields:
    """Verify the header fields a pack declares in
    ``verify_header_fields``: ``patient_name``, ``dob``, ``dos``. Empty
    list skips; an unsupported declared name fails loudly, naming it —
    never a silent skip.
    """

    level = "L3"

    def run(
        self,
        item: UploadItem,
        patient: Patient,
        *,
        pack: LoadedPack | None,
        encounter: Encounter | None,
        snapshot: PdfSnapshot | None = None,
    ) -> LevelResult:
        if pack is None:
            return LevelResult(self.level, LevelStatus.SKIP, "no pack provided")
        fields = pack.manifest.verify_header_fields
        if not fields:
            return LevelResult(self.level, LevelStatus.SKIP, "no header fields declared")
        text = _snapshot_for(item, snapshot).page_one_text
        failures: list[str] = []
        for field_name in fields:
            if field_name not in _SUPPORTED_HEADER_FIELDS:
                # Loud: name the unsupported field, fail immediately.
                return LevelResult(
                    self.level,
                    LevelStatus.FAIL,
                    f"unsupported header field {field_name!r}",
                )
            if not self._field_present(field_name, text, patient, encounter):
                failures.append(field_name)
        if failures:
            return LevelResult(
                self.level,
                LevelStatus.FAIL,
                f"header fields not found: {sorted(failures)}",
            )
        return LevelResult(
            self.level, LevelStatus.PASS, f"header fields verified: {sorted(fields)}"
        )

    def _field_present(
        self,
        field_name: str,
        text: str,
        patient: Patient,
        encounter: Encounter | None,
    ) -> bool:
        if field_name == "patient_name":
            name = patient.display_name
            return bool(name) and fuzzy_contains(name, text) >= _NAME_RATIO
        if field_name == "dob":
            return patient.birth_date is not None and _date_present(patient.birth_date, text)
        if field_name == "dos":  # pragma: no branch - the only remaining supported name
            dos = encounter.date_of_service if encounter is not None else None
            return dos is not None and _date_present(dos, text)
        return False  # pragma: no cover - unreachable; unsupported names short-circuit above


# --- L4: banner check (pre) ---


class L4Banner:
    """Re-invoke the destination banner readback (defense in depth), so
    the ladder is also correct standalone. A mismatch raises
    :class:`WrongPatientError` rather than a ``fail`` result — patient
    safety aborts the whole run, never just one item's level (48).
    """

    level = "L4"

    def run(self, patient: Patient, *, banner: BannerCheck | None) -> LevelResult:
        if banner is None:
            return LevelResult(self.level, LevelStatus.SKIP, "no banner (standalone mode)")
        if not banner.current_patient_matches(patient):
            raise WrongPatientError
        return LevelResult(self.level, LevelStatus.PASS, "banner matches patient")


# --- L5: destination metadata (post) ---


class L5Metadata:
    """Cross-check the destination's reported size and page count via a
    :class:`MetadataReader`; skips if absent. A reported value that
    disagrees fails; an unreported one is not checked.
    """

    level = "L5"

    def run(
        self,
        item: UploadItem,
        dest_patient: DestinationPatient | None,
        destination_doc_id: str | None,
        *,
        reader: MetadataReader | None,
        snapshot: PdfSnapshot | None = None,
    ) -> LevelResult:
        if reader is None:
            return LevelResult(self.level, LevelStatus.SKIP, "destination has no MetadataReader")
        if dest_patient is None or destination_doc_id is None:
            return LevelResult(self.level, LevelStatus.SKIP, "no destination doc resolved")
        meta = reader.read_metadata(dest_patient, destination_doc_id)
        checked: list[str] = []
        reported_size = meta.get("size_bytes")
        if reported_size is not None:
            if int(reported_size) != item.size_bytes:
                return LevelResult(self.level, LevelStatus.FAIL, "reported size_bytes mismatch")
            checked.append("size_bytes")
        reported_pages = meta.get("page_count")
        if reported_pages is not None:
            if int(reported_pages) != _snapshot_for(item, snapshot).page_count:
                return LevelResult(self.level, LevelStatus.FAIL, "reported page_count mismatch")
            checked.append("page_count")
        if not checked:
            return LevelResult(
                self.level, LevelStatus.SKIP, "destination reported nothing to check"
            )
        return LevelResult(self.level, LevelStatus.PASS, f"metadata verified: {sorted(checked)}")


# --- L6: round-trip read-back (post) ---


class L6RoundTrip:
    """Read the uploaded bytes back via a :class:`DocumentReader` (skips
    if absent). Tier 1: byte-identical sha256, ``pass``. Tier 2: same
    page count and page-1 IDENTITY, not whole-page similarity — two
    patients' charts score ~0.99 on a whole-page ratio and would
    false-pass a swap. Anything else is a ``fail``."""

    level = "L6"

    def run(
        self,
        item: UploadItem,
        dest_patient: DestinationPatient | None,
        destination_doc_id: str | None,
        *,
        reader: DocumentReader | None,
        patient: Patient | None = None,
        snapshot: PdfSnapshot | None = None,
    ) -> LevelResult:
        if reader is None:
            return LevelResult(self.level, LevelStatus.SKIP, "destination has no DocumentReader")
        if dest_patient is None or destination_doc_id is None:
            return LevelResult(self.level, LevelStatus.SKIP, "no destination doc resolved")
        data = reader.read_back(dest_patient, destination_doc_id)
        if hashlib.sha256(data).hexdigest() == item.sha256:
            return LevelResult(self.level, LevelStatus.PASS, "byte-identical read-back")
        # Tier 2: gate the render extra first (its RuntimeError must
        # surface); an unparseable read-back is a clean L6 fail, not a crash.
        pymupdf = _import_pymupdf()
        try:
            back_pages, back_text = _pages_and_text_of_bytes(pymupdf, data)
        except Exception:  # any PyMuPDF parse failure is a corruption fail, not a crash
            return LevelResult(self.level, LevelStatus.FAIL, "read-back is not a valid PDF")
        if back_pages != _snapshot_for(item, snapshot).page_count:
            return LevelResult(self.level, LevelStatus.FAIL, "read-back page_count differs")
        if patient is None or not patient.display_name:
            return LevelResult(
                self.level,
                LevelStatus.FAIL,
                "reprocessed bytes but no patient context to re-assert identity",
            )
        ratio = fuzzy_contains(patient.display_name, back_text)
        if patient.birth_date is not None and not _date_present(patient.birth_date, back_text):
            return LevelResult(
                self.level,
                LevelStatus.FAIL,
                f"read-back lost the birth_date (name ratio {ratio:.2f})",
            )
        if ratio < _NAME_RATIO:
            return LevelResult(
                self.level,
                LevelStatus.FAIL,
                f"read-back patient_name ratio {ratio:.2f} < {_NAME_RATIO:.2f}",
            )
        return LevelResult(self.level, LevelStatus.PASS, "reprocessed")
