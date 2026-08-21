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

PyMuPDF (``pymupdf``) is imported lazily inside the levels that read the PDF, so
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

from anastomosis.core.hashutil import hash_and_size
from anastomosis.core.identity import date_token_present, name_fragment_present
from anastomosis.core.identity import normalize as _normalize
from anastomosis.core.model import Encounter, Patient
from anastomosis.core.timeutil import all_date_spellings
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
    "PdfSnapshot",
    "date_renderings",
    "fuzzy_contains",
]

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

# How far past the needle's length each comparison window reaches, so a middle
# name or a credential suffix rendered next to the name does not sink the ratio.
# Load-bearing for the probe ratios documented above — widening it moves every
# ratio the threshold is calibrated against.
_WINDOW_SLACK = 8


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
    # Boundary-anchored fast path (NOT a raw ``n in hay`` substring): the whole
    # name must stand alone in the page. A raw substring returned 1.0 for a
    # short name buried in a longer one ("Ann Li" inside "Joann Liang") — the
    # wrong-chart false-PASS this predicate exists to reject. The NAME-boundary
    # predicate is required here (not the value one): intra-name joiners must
    # count as embedding, or "Ann Li" scores 1.0 inside "Mary-Ann Li-Wong". A
    # legitimate rendering variant (middle name, suffix, "Last, First") is not
    # a bounded whole-name match here and falls through to the fuzzy window.
    if name_fragment_present(n, hay):
        return 1.0
    matcher = SequenceMatcher(autojunk=False)
    matcher.set_seq2(n)
    window_len = len(n) + _WINDOW_SLACK
    best = 0.0
    # Anchor each window start on a token boundary so we never compare against a
    # window that bisects a word; take a little more than the needle's length so
    # a rendered middle name or suffix padding the name does not sink the ratio.
    #
    # ``_normalize`` collapsed every whitespace run to ONE space and stripped the
    # ends, so a token's start offset indexes straight into ``hay`` and the
    # window is a slice: ``hay[start : start + window_len]`` is character-for-
    # character the old ``" ".join(tokens[i:])[:window_len]``, without rebuilding
    # (and immediately discarding) the rest of the page at every token. Same
    # windows, same ratios, linear instead of quadratic in page length.
    start = 0
    for token in hay.split(" "):
        matcher.set_seq1(hay[start : start + window_len])
        best = max(best, matcher.ratio())
        if best >= 1.0:
            break
        start += len(token) + 1  # +1 for the single separating space
    return best


def date_renderings(value: date) -> set[str]:
    """Every chart spelling a pack might render ``value`` as (L2/L3 verify).

    Delegates to the single canonical enumerator in ``core.timeutil`` so this
    delivery verifier and the QA ``DataIntegrityCheck`` share one definition and
    cannot drift apart (the caller still requires *at least one* present).
    """
    return all_date_spellings(value)


def _date_present(value: date, text: str) -> bool:
    """Whether any candidate rendering of ``value`` appears in ``text``.

    Boundary-anchored per spelling (:func:`anastomosis.core.identity.date_token_present`),
    so an unpadded DOB does not match inside a longer date run ("1/2/1990" does
    not satisfy a page showing "11/2/1990") — the wrong-patient DOB collision
    this hard gate exists to catch.
    """
    return any(date_token_present(s, text) for s in date_renderings(value))


# --- the lazy PyMuPDF gate ---


def _import_pymupdf() -> Any:
    """Import PyMuPDF lazily, naming the extra if it is not installed.

    The levels that read a PDF import here so this module loads on a machine
    without the ``render`` extra (L0 needs no PDF); a missing install raises a
    ``RuntimeError`` naming ``anastomosis[render]``, matching the
    optional-dependency error style in :mod:`anastomosis.reconstruct.chromium`.
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
    """One item's parsed PDF facts — page count + page-1 text — read once.

    L1 wants the page count, L2/L3 want page-1 text, L5 wants the page count
    again and L6 wants it once more: five levels, one unchanging local file,
    and (before this) up to five ``pymupdf.open`` + text-extraction passes per
    item. This is the per-item twin of the QA runner's per-document snapshot
    cache (:func:`anastomosis.qa.checks.prime_snapshot_cache`): the levels read
    from it instead of each opening the file.

    **Lazy on purpose.** Nothing is parsed until a level asks, so the ladder's
    ordering semantics are untouched: L1 still rejects a sub-KiB file on its
    size alone without ever opening it, a missing render extra still raises
    from the level that needs PyMuPDF (never from the composite that built the
    snapshot), and a corrupt PDF still raises inside the first level that reads
    it.

    **Scoped to one phase.** :class:`~.composite.LayeredVerifier` builds one
    snapshot for its pre-upload levels and a second for its post-upload levels
    rather than carrying one across the upload: L5/L6 run *after* bytes were
    sent, and re-reading the local file there keeps their answer about the
    on-disk file honest (and keeps page text — PHI — out of memory for the
    duration of a run).

    A level given no snapshot builds its own, so calling a level directly
    (``L1PageAndSize().run(item)``) behaves exactly as it always has.
    """

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
            # Order matters: read the text while the document is open, and take
            # page_count from the same open so the two facts describe ONE read
            # of one file.
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
    """(page_count, page-1 text) of an in-memory PDF — for L6 read-back.

    Takes the already-imported ``pymupdf`` module so the caller can gate the
    render extra (and surface its RuntimeError) *before* the parse, then treat
    any parse failure here as a corruption fail rather than re-raising.
    """
    with pymupdf.open(stream=data, filetype="pdf") as doc:
        return int(doc.page_count), _first_page_text(doc)


# --- L0: file integrity (pre, no PyMuPDF) ---


class L0FileIntegrity:
    """The file exists and its bytes still match the manifest.

    Overlaps the engine's preflight by design: re-hashing here makes the stack
    self-contained when run *outside* the engine (a future ``anast verify``
    command, a standalone re-check), so the ladder never assumes the engine ran
    first. The engine having just hashed the same file is therefore NOT a reason
    to reuse its digest — an independent re-read of the bytes is the whole
    proposition of this level, and a cached digest would prove only that the
    manifest agrees with itself. What IS shared with preflight and the manifest
    is the *hasher* (:func:`anastomosis.core.hashutil.hash_and_size`), so the
    three sites cannot disagree about how a file is chunked.

    L0 uses only the stdlib — it works without the render extra.
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
    ) -> LevelResult:
        if item.size_bytes <= _MIN_PDF_BYTES:
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
    """The patient's name fuzzy-matches page 1; their DOB (if set) is present.

    The wrong-chart defense at the document level. The name must fuzzy-match at
    ratio >= 0.88, and — if the patient has a DOB — at least one rendering of
    that DOB must appear on page 1. The DOB check is a *hard* gate: a page that
    carries the right name but a different DOB (or no DOB) fails regardless of
    the name ratio, because a wrong-patient DOB on the page is exactly the
    catastrophe this level exists to catch.
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
        snapshot: PdfSnapshot | None = None,
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
