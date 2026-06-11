"""The L0-L6 verification levels: one small class per check.

This is the verification ladder PLAN item 11 calls for — the layered defense
that proves a reconstructed chart landed in the right destination chart, intact
and identifiable. Each level is an independent, side-effect-free check with a
``run(...) -> LevelResult`` method; :mod:`.composite` stacks them behind the
engine's :class:`~anastomosis.deliver.browser.verify.Verifier` seam.

The levels split by phase:

* **pre** (L0-L4) run before any bytes are sent — they prove the local file is
  intact (L0/L1), that it is *this* patient's chart (L2/L3, the wrong-chart
  defense), and that the destination's open chart is the right patient (L4,
  the wrong-patient defense).
* **post** (L5-L6) run after the upload returns — they cross-check the
  destination's own metadata (L5) and round-trip the stored bytes back (L6).

PHI rule (load-bearing): a :class:`LevelResult.detail` carries level names,
counts, ratios, and field *names* — never a patient name, DOB, date, or path.
The detail strings are surfaced in reports and may be logged, so an honest
level cannot leak PHI. The same goes for any exception raised out of a level.

PyMuPDF (``fitz``) is imported lazily inside the levels that read the PDF, so
this module imports on a machine without the ``render`` extra; L0 (pure
file-integrity) works there too. A missing install raises a ``RuntimeError``
naming ``anastomosis[render]``, matching the optional-dependency error style in
:mod:`anastomosis.reconstruct.chromium`.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date
from difflib import SequenceMatcher
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any

from anastomosis.core.model import Encounter, Patient
from anastomosis.deliver.browser.errors import WrongPatientError
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
    "date_renderings",
    "fuzzy_contains",
]

# 1 MiB chunks: matches the engine/manifest hashers so an L0 re-hash reads the
# file exactly the way the manifest measured it.
_HASH_CHUNK_BYTES = 1024 * 1024

# A sub-KiB "PDF" is a rendering failure, not a one-page chart: a Chromium
# print of even an empty note is several KiB. Below this the file is corrupt or
# truncated, so L1 fails it rather than letting it ship.
_MIN_PDF_BYTES = 1024

# The identity-match threshold (PLAN item 11). 0.88 tolerates light rendering
# noise — case, whitespace, a trailing suffix — but NOT structural changes to
# the name: probes show an added/dropped middle name (~0.79-0.83), a
# hyphen<->space swap (~0.83), and "Last, First" reordering (~0.53) all land
# BELOW the threshold. That is the fail-safe direction: a legitimate alternate
# rendering fails loudly to PRE_VERIFY_FAILED for the operator to inspect,
# rather than a similar-but-wrong name slipping through. The DOB hard gate is
# the primary defense; the ratio is the secondary one.
_NAME_RATIO = 0.88


class LevelStatus(StrEnum):
    """The outcome of one level — mirrors the QA :class:`Verdict` house style."""

    PASS = "pass"  # noqa: S105 — status label, not a password
    FAIL = "fail"
    SKIP = "skip"


@dataclass(frozen=True)
class LevelResult:
    """The PHI-safe outcome of one verification level.

    ``detail`` carries only level names, counts, ratios, and field *names* —
    never a patient value, a date, or a path. It is surfaced in reports and may
    be logged.
    """

    level: str  # "L0".."L6"
    status: LevelStatus
    detail: str


# --- shared matching helpers (the boundary-anchored / fuzzy lessons) ---


def fuzzy_contains(needle: str, haystack: str, *, ratio: float = _NAME_RATIO) -> float:
    """Best fuzzy match ratio of ``needle`` anywhere in ``haystack``.

    A whole-string ``SequenceMatcher`` ratio is the wrong tool for "is this
    name *somewhere* in a page of text" — a short name drowns in a long page.
    Instead this slides a window the size of the needle across the haystack
    (token-aligned so the window starts on word boundaries) and returns the
    best window ratio. Case- and whitespace-normalized; stdlib only.

    Returns the best ratio found (so callers can compare against a threshold
    and report the ratio in a PHI-safe detail). ``ratio`` is unused here beyond
    documenting the intended comparison threshold; the caller does the compare.
    """
    n = _normalize(needle)
    if not n:
        return 0.0
    hay = _normalize(haystack)
    if not hay:
        return 0.0
    if n in hay:
        return 1.0
    matcher = SequenceMatcher(autojunk=False)
    matcher.set_seq2(n)
    tokens = hay.split(" ")
    window_len = len(n)
    best = 0.0
    # Anchor each window start on a token boundary so we never compare against a
    # window that bisects a word; take a little more than the needle's length so
    # a rendered middle name or suffix padding the name does not sink the ratio.
    for i in range(len(tokens)):
        window = " ".join(tokens[i:])[: window_len + 8]
        matcher.set_seq1(window)
        best = max(best, matcher.ratio())
        if best >= 1.0:
            break
    return best


def _normalize(text: str) -> str:
    """Lowercase and collapse all whitespace runs to single spaces."""
    return " ".join(text.split()).lower()


def date_renderings(value: date) -> set[str]:
    """Every chart spelling a pack might render ``value`` as.

    The pack does not declare a single canonical date format — different packs
    (and different fields within one pack) render dates as ``%m/%d/%Y``,
    ``%B %d, %Y``, or unpadded variants. Rather than guess one, this returns
    the full candidate set and the caller requires *at least one* present
    (the QA ``_date_spellings`` lesson, re-applied here).

    The unpadded ``%-m/%-d`` equivalents are built BY HAND from the integer
    date parts — ``%-d``/``%-m`` are glibc-only and absent on Windows, and
    ``ruff`` (DTZ/portability) and ``mypy --strict`` both run on Windows CI.
    """
    return {
        # numeric, padded and unpadded
        f"{value.month:02d}/{value.day:02d}/{value.year}",
        f"{value.month}/{value.day}/{value.year}",
        f"{value.month:02d}-{value.day:02d}-{value.year}",
        f"{value.month}-{value.day}-{value.year}",
        # month-name spellings (full and abbreviated), padded and unpadded day
        f"{value.strftime('%B')} {value.day:02d}, {value.year}",
        f"{value.strftime('%B')} {value.day}, {value.year}",
        f"{value.strftime('%b')} {value.day:02d}, {value.year}",
        f"{value.strftime('%b')} {value.day}, {value.year}",
    }


def _date_present(value: date, text: str) -> bool:
    """Whether any candidate rendering of ``value`` appears in ``text``."""
    haystack = _normalize(text)
    return any(_normalize(s) in haystack for s in date_renderings(value))


# --- the lazy PyMuPDF gate ---


def _import_fitz() -> Any:
    """Import PyMuPDF lazily, naming the extra if it is not installed.

    The levels that read a PDF import here so this module loads on a machine
    without the ``render`` extra (L0 needs no PDF); a missing install raises a
    ``RuntimeError`` naming ``anastomosis[render]``, matching the
    optional-dependency error style in :mod:`anastomosis.reconstruct.chromium`.
    """
    try:
        import fitz  # PyMuPDF
    except ImportError as exc:  # pragma: no cover - environment-dependent
        raise RuntimeError(
            "PDF verification needs the render extra: pip install 'anastomosis[render]'"
        ) from exc
    return fitz


def _first_page_text(doc: Any) -> str:
    """Page-1 text of an open PyMuPDF document, or "" if it has no pages."""
    for page in doc:
        return str(page.get_text())
    return ""


def _page_one_text(path: Path) -> str:
    with _import_fitz().open(path) as doc:
        return _first_page_text(doc)


def _page_count(path: Path) -> int:
    with _import_fitz().open(path) as doc:
        return int(doc.page_count)


def _pages_and_text_of_bytes(fitz: Any, data: bytes) -> tuple[int, str]:
    """(page_count, page-1 text) of an in-memory PDF — for L6 read-back.

    Takes the already-imported ``fitz`` module so the caller can gate the
    render extra (and surface its RuntimeError) *before* the parse, then treat
    any parse failure here as a corruption fail rather than re-raising.
    """
    with fitz.open(stream=data, filetype="pdf") as doc:
        return int(doc.page_count), _first_page_text(doc)


# --- L0: file integrity (pre, no PyMuPDF) ---


class L0FileIntegrity:
    """The file exists and its bytes still match the manifest.

    Overlaps the engine's preflight by design: re-hashing here makes the stack
    self-contained when run *outside* the engine (a future ``anast verify``
    command, a standalone re-check), so the ladder never assumes the engine ran
    first. L0 uses only the stdlib hashlib — it works without the render extra.
    """

    level = "L0"

    def run(self, item: UploadItem) -> LevelResult:
        path = item.file_path
        if not path.exists():
            return LevelResult(self.level, LevelStatus.FAIL, "file missing")
        digest = hashlib.sha256()
        size = 0
        try:
            with path.open("rb") as handle:
                while chunk := handle.read(_HASH_CHUNK_BYTES):
                    digest.update(chunk)
                    size += len(chunk)
        except OSError:
            return LevelResult(self.level, LevelStatus.FAIL, "file unreadable")
        if size != item.size_bytes:
            return LevelResult(self.level, LevelStatus.FAIL, "size_bytes mismatch")
        if digest.hexdigest() != item.sha256:
            return LevelResult(self.level, LevelStatus.FAIL, "sha256 mismatch")
        return LevelResult(self.level, LevelStatus.PASS, "sha256 and size match")


# --- L1: page count + size sanity (pre) ---


class L1PageAndSize:
    """The PDF opens, has >= 1 page, is above the sub-KiB floor, and (when the
    caller declares one) has exactly the expected page count."""

    level = "L1"

    def run(self, item: UploadItem, *, expected_pages: int | None = None) -> LevelResult:
        if item.size_bytes <= _MIN_PDF_BYTES:
            return LevelResult(
                self.level, LevelStatus.FAIL, f"size_bytes below {_MIN_PDF_BYTES}-byte floor"
            )
        pages = _page_count(item.file_path)
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
    """The patient's name fuzzy-matches page 1; their DOB (if set) is present.

    The wrong-chart defense at the document level. The name must fuzzy-match at
    ratio >= 0.88, and — if the patient has a DOB — at least one rendering of
    that DOB must appear on page 1. The DOB check is a *hard* gate: a page that
    carries the right name but a different DOB (or no DOB) fails regardless of
    the name ratio, because a wrong-patient DOB on the page is exactly the
    catastrophe this level exists to catch.
    """

    level = "L2"

    def run(self, item: UploadItem, patient: Patient) -> LevelResult:
        text = _page_one_text(item.file_path)
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

# Field names this verifier knows how to check. An entry in a pack's
# verify_header_fields outside this set is a LOUD fail (the spec): we must not
# silently skip a header field the operator declared.
_SUPPORTED_HEADER_FIELDS = frozenset({"patient_name", "dob", "dos"})


class L3HeaderFields:
    """Verify the header fields a pack declares in ``verify_header_fields``.

    Driven by the pack manifest, not hard-coded. Supported field names:

    * ``patient_name`` — family+given fuzzy-match >= 0.88 (the L2 matcher);
    * ``dob`` — a rendering of ``Patient.birth_date`` present on page 1;
    * ``dos`` — a rendering of the encounter's ``date_of_service`` present.

    An empty list skips (nothing declared). A declared name this verifier does
    not support fails loudly with the unsupported NAME in the detail — never a
    silent skip.
    """

    level = "L3"

    def run(
        self,
        item: UploadItem,
        patient: Patient,
        *,
        pack: LoadedPack | None,
        encounter: Encounter | None,
    ) -> LevelResult:
        if pack is None:
            return LevelResult(self.level, LevelStatus.SKIP, "no pack provided")
        fields = pack.manifest.verify_header_fields
        if not fields:
            return LevelResult(self.level, LevelStatus.SKIP, "no header fields declared")
        text = _page_one_text(item.file_path)
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
    """Re-invoke the destination banner readback (defense in depth).

    The ENGINE already gates every upload on ``destination.banner`` before
    calling the verifier — L4 re-checks it inside the stack so the ladder is
    *also* correct when run standalone (a future ``anast verify`` that drives
    the stack without the engine). When the verifier was built without a
    banner, L4 skips with an explicit detail.

    Asymmetry, by design: a banner mismatch raises :class:`WrongPatientError`
    rather than returning a ``fail`` :class:`LevelResult`. Patient safety
    propagates as the abort exception the engine already routes — a wrong
    patient stops the whole run, it is never just one item's failed level.
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
    """Cross-check the destination's reported size and page count.

    Requires a :class:`MetadataReader`; skips with an explicit detail when the
    destination does not implement one. Passes when the reported ``size_bytes``
    (if reported) equals the item's size and the reported ``page_count`` (if
    reported) equals the local PDF's. A reported value that disagrees fails.
    """

    level = "L5"

    def run(
        self,
        item: UploadItem,
        dest_patient: DestinationPatient | None,
        destination_doc_id: str | None,
        *,
        reader: MetadataReader | None,
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
            if int(reported_pages) != _page_count(item.file_path):
                return LevelResult(self.level, LevelStatus.FAIL, "reported page_count mismatch")
            checked.append("page_count")
        if not checked:
            return LevelResult(
                self.level, LevelStatus.SKIP, "destination reported nothing to check"
            )
        return LevelResult(self.level, LevelStatus.PASS, f"metadata verified: {sorted(checked)}")


# --- L6: round-trip read-back (post) ---


class L6RoundTrip:
    """Read the uploaded bytes back and prove they still carry the chart.

    Requires a :class:`DocumentReader`; skips when absent. Two tiers, because
    EHRs commonly re-process (re-compress, re-paginate, stamp) an upload, so
    byte-identity is the happy path but not the only acceptable outcome:

    1. **Byte-identity** — sha256 of the read-back equals the item's sha256:
       the strongest possible proof, ``pass``.
    2. **Reprocessed** — the bytes differ, but the read-back has the same page
       count AND its page-1 text re-asserts the patient's IDENTITY (the L2
       predicate: name fuzzy >= 0.88 and, when the patient has one, a DOB
       rendering present): the document survived re-processing intact,
       ``pass`` with detail ``"reprocessed"``.

       Identity, not page-vs-page similarity, on purpose: two different
       patients' charts share almost all of their boilerplate, so a
       whole-page ratio scores a SWAPPED chart ~0.99 and false-passes — the
       exact wrong-patient outcome this level exists to catch (found by an
       adversarial probe; the swapped-chart regression test pins it).
       Without the canonical patient in hand (standalone post-only use) the
       differing bytes cannot be identity-checked, so the tier FAILS rather
       than guessing — fail-safe.

    Anything else (different page count, identity no longer provable, or bytes
    that no longer parse as a PDF at all) is a ``fail`` — the destination
    mangled or swapped the document.
    """

    level = "L6"

    def run(
        self,
        item: UploadItem,
        dest_patient: DestinationPatient | None,
        destination_doc_id: str | None,
        *,
        reader: DocumentReader | None,
        patient: Patient | None = None,
    ) -> LevelResult:
        if reader is None:
            return LevelResult(self.level, LevelStatus.SKIP, "destination has no DocumentReader")
        if dest_patient is None or destination_doc_id is None:
            return LevelResult(self.level, LevelStatus.SKIP, "no destination doc resolved")
        data = reader.read_back(dest_patient, destination_doc_id)
        if hashlib.sha256(data).hexdigest() == item.sha256:
            return LevelResult(self.level, LevelStatus.PASS, "byte-identical read-back")
        # Tier 2: tolerate destination re-processing if the chart survived. Gate
        # the render extra first (its RuntimeError must surface), then parse —
        # a read-back that no longer parses as a PDF is a corruption FAIL, kept
        # here as a clean L6 fail rather than an exception the engine retries.
        fitz = _import_fitz()
        try:
            back_pages, back_text = _pages_and_text_of_bytes(fitz, data)
        except Exception:  # any PyMuPDF parse failure is a corruption fail, not a crash
            return LevelResult(self.level, LevelStatus.FAIL, "read-back is not a valid PDF")
        if back_pages != _page_count(item.file_path):
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
