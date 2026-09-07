"""The layered verifier: stack L0-L6 behind the engine's Verifier seam.

:class:`LayeredVerifier` implements
:class:`~anastomosis.deliver.browser.verify.Verifier` as a pure plug-in.
``verify_pre`` runs L0-L4, ``verify_post`` runs L5-L6; the first failing
level raises :class:`PermanentDeliveryError`, but only L4's live banner
readback raises :class:`WrongPatientError` (a run abort, not one item) —
L2's DOB hard-fail is a document-level ``fail``, not an abort (48). Every
raised message carries level + field names only, never a patient value (49).
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping

from anastomosis.core.logutil import exc_tag, safe_log_id
from anastomosis.core.model import Encounter, Patient
from anastomosis.deliver.browser.errors import PermanentDeliveryError
from anastomosis.destinations.base import (
    BannerCheck,
    Destination,
    DestinationPatient,
    DocumentReader,
    MetadataReader,
    UploadItem,
    UploadReceipt,
)
from anastomosis.reconstruct.packs import LoadedPack

from .levels import (
    L0FileIntegrity,
    L1PageAndSize,
    L2IdentityText,
    L3HeaderFields,
    L4Banner,
    L5Metadata,
    L6RoundTrip,
    LevelResult,
    LevelStatus,
    PdfSnapshot,
)

# Re-exported here (the public typedef lives in .types so the
# report writer can import it without traversing this module's heavier
# imports and hitting a circular import: verify.composite -> browser.errors
# -> browser/__init__ -> browser.reports -> verify.composite, which only
# surfaces in a fresh interpreter).
from .types import LevelCoverage, VerifyPolicy

__all__ = ["ALL_LEVELS", "LayeredVerifier", "LevelCoverage", "VerifyPolicy"]


logger = logging.getLogger(__name__)

# Every level id the stack knows, in run order. The default ``levels`` set.
ALL_LEVELS: frozenset[str] = frozenset({"L0", "L1", "L2", "L3", "L4", "L5", "L6"})


#: What a scan cannot support (L2/L3 need rendered text) and the
#: PHI-safe reason recorded when each is skipped (46).
_POLICY_SKIPS: dict[VerifyPolicy, dict[str, str]] = {
    VerifyPolicy.SOURCE_PAGED: {
        "L2": "source document: no rendered page text to match a name against",
        "L3": "source document: no pack rendered it, so it declares no header fields",
    },
    VerifyPolicy.SOURCE_OPAQUE: {
        "L1": "source document: its declared media type is not one this toolkit pages",
        "L2": "source document: no rendered page text to match a name against",
        "L3": "source document: no pack rendered it, so it declares no header fields",
        "L5": "source document: no local page count to compare the destination's against",
    },
}


def _skip_step(level: str, reason: str) -> Callable[[], LevelResult]:
    """A step recording why this level cannot run — so the run report
    names a real skip instead of a quietly narrower ladder.
    """
    return lambda: LevelResult(level, LevelStatus.SKIP, reason)


def _for_policy(
    steps: tuple[tuple[str, Callable[[], LevelResult]], ...], policy: VerifyPolicy
) -> tuple[tuple[str, Callable[[], LevelResult]], ...]:
    """The same steps, with the ones this item's bytes cannot support skipped."""
    skips = _POLICY_SKIPS.get(policy)
    if skips is None:
        return steps
    return tuple(
        (level, _skip_step(level, skips[level]) if level in skips else step)
        for level, step in steps
    )


class _PreVerifyError(PermanentDeliveryError):
    """A pre-upload level failed — routes to PRE_VERIFY_FAILED. PHI-safe message."""


class _PostVerifyError(PermanentDeliveryError):
    """A post-upload level failed — routes to POST_VERIFY_FAILED. PHI-safe message."""


class LayeredVerifier:
    """Stack the L0-L6 levels behind the engine's Verifier protocol."""

    def __init__(
        self,
        *,
        records: Mapping[str, Encounter] | None = None,
        pack: LoadedPack | None = None,
        destination: Destination | None = None,
        expected_pages: Mapping[str, int] | None = None,
        levels: frozenset[str] | None = None,
        verify_policies: Mapping[str, VerifyPolicy] | None = None,
    ) -> None:
        # item.encounter_id -> Encounter, for L3 "dos".
        self._records = dict(records) if records else {}
        self._pack = pack
        self._destination = destination
        self._expected_pages = dict(expected_pages) if expected_pages else {}
        self._levels = levels if levels is not None else ALL_LEVELS
        # item_key -> what kind of file this item is; empty (defaulting to
        # a rendered chart) for a pre-v4 manifest.
        self._policies = dict(verify_policies) if verify_policies else {}
        # item_key -> DestinationPatient, captured in verify_pre so verify_post
        # (which the engine calls without a patient) can resolve a read-back.
        self._resolved: dict[str, DestinationPatient] = {}
        # item_key -> canonical Patient, for L6's identity re-assertion
        # (verify_post's protocol signature carries no patient).
        self._patients: dict[str, Patient] = {}
        # item_key -> the level table from the most recent verify of that item,
        # for reports. ``last_results`` is the most recent overall.
        self._results: dict[str, list[LevelResult]] = {}
        self.last_results: list[LevelResult] = []

        self._l0 = L0FileIntegrity()
        self._l1 = L1PageAndSize()
        self._l2 = L2IdentityText()
        self._l3 = L3HeaderFields()
        self._l4 = L4Banner()
        self._l5 = L5Metadata()
        self._l6 = L6RoundTrip()

    # --- Verifier protocol ---

    def verify_pre(
        self, item: UploadItem, patient: Patient, dest_patient: DestinationPatient | None = None
    ) -> None:
        """Contract: runs L0-L4, recording every result; raises the first
        FAIL as a PRE_VERIFY error (48), except L4's banner mismatch,
        which propagates as :class:`WrongPatientError` (run abort).
        ``dest_patient``, when given, is reused verbatim for L5/L6.
        """
        # Capture the destination patient once (feeds L5/L6 read-back later).
        self._capture_dest_patient(item, patient, dest_patient)
        encounter = self._records.get(item.encounter_id)
        policy = self._policies.get(item.item_key, VerifyPolicy.RENDERED_CHART)
        # One parse for the whole pre phase (L1 wants page count, L2/L3 want
        # page-1 text); lazy, so L1's size floor need not open the file.
        snapshot = PdfSnapshot(item.file_path)

        steps: tuple[tuple[str, Callable[[], LevelResult]], ...] = (
            ("L0", lambda: self._l0.run(item)),
            (
                "L1",
                lambda: self._l1.run(
                    item,
                    expected_pages=self._expected_pages.get(item.item_key),
                    snapshot=snapshot,
                    policy=policy,
                ),
            ),
            ("L2", lambda: self._l2.run(item, patient, snapshot=snapshot)),
            (
                "L3",
                lambda: self._l3.run(
                    item, patient, pack=self._pack, encounter=encounter, snapshot=snapshot
                ),
            ),
            ("L4", lambda: self._l4.run(patient, banner=self._banner())),
        )
        steps = _for_policy(steps, policy)
        # An L4 wrong-patient escapes as WrongPatientError; the partial
        # table recorded so far stays on the instance for a report.
        first_failure = self._run_steps(item.item_key, steps)
        if first_failure is not None:
            raise _PreVerifyError(f"{first_failure.level}: {first_failure.detail}")

    def verify_post(self, item: UploadItem, receipt: UploadReceipt) -> None:
        """Contract: runs L5-L6 using the :class:`DestinationPatient`
        captured in :meth:`verify_pre`; raises the first FAIL as a
        POST_VERIFY error (48). Skips in standalone mode (nothing captured).
        """
        dest_patient = self._resolved.get(item.item_key)
        doc_id = receipt.destination_doc_id
        # L6's reprocessed tier re-asserts IDENTITY against the read-back
        # (a whole-page similarity ratio alone false-passes a swapped chart).
        patient = self._patients.get(item.item_key)
        # A FRESH snapshot: L5/L6 claim about the local file as it is NOW,
        # after bytes were sent, not the pre-phase's.
        snapshot = PdfSnapshot(item.file_path)

        steps: tuple[tuple[str, Callable[[], LevelResult]], ...] = (
            (
                "L5",
                lambda: self._l5.run(
                    item, dest_patient, doc_id, reader=self._metadata_reader(), snapshot=snapshot
                ),
            ),
            (
                "L6",
                lambda: self._l6.run(
                    item,
                    dest_patient,
                    doc_id,
                    reader=self._document_reader(),
                    patient=patient,
                    snapshot=snapshot,
                ),
            ),
        )
        steps = _for_policy(steps, self._policies.get(item.item_key, VerifyPolicy.RENDERED_CHART))
        first_failure = self._run_steps(item.item_key, steps, append=True)
        if first_failure is not None:
            raise _PostVerifyError(f"{first_failure.level}: {first_failure.detail}")

    # --- report accessors ---

    def results_for(self, item_key: str) -> list[LevelResult]:
        """The collected level table for ``item_key`` (empty if never verified)."""
        return list(self._results.get(item_key, []))

    def coverage_summary(self) -> dict[str, LevelCoverage]:
        """One :class:`LevelCoverage` row per level that ran: per-status
        counts plus deduplicated skip reasons, counts and reason strings
        only (49) — the actual L-coverage for the upload run report.
        """
        passes: dict[str, int] = {}
        fails: dict[str, int] = {}
        skips: dict[str, int] = {}
        reasons: dict[str, set[str]] = {}
        for results in self._results.values():
            for entry in results:
                level = entry.level
                if entry.status is LevelStatus.PASS:
                    passes[level] = passes.get(level, 0) + 1
                elif entry.status is LevelStatus.FAIL:
                    fails[level] = fails.get(level, 0) + 1
                else:
                    skips[level] = skips.get(level, 0) + 1
                    reasons.setdefault(level, set()).add(entry.detail)
        levels = sorted(passes.keys() | fails.keys() | skips.keys())
        return {
            level: {
                "pass_count": passes.get(level, 0),
                "fail_count": fails.get(level, 0),
                "skip_count": skips.get(level, 0),
                "skip_reasons": sorted(reasons.get(level, set())),
            }
            for level in levels
        }

    # --- helpers ---

    def _run_steps(
        self,
        item_key: str,
        steps: tuple[tuple[str, Callable[[], LevelResult]], ...],
        *,
        append: bool = False,
    ) -> LevelResult | None:
        """Run every in-scope step, recording all results; return the
        first FAIL. A raising step (L4's :class:`WrongPatientError`)
        still records results gathered so far before it propagates.
        """
        results: list[LevelResult] = []
        first_failure: LevelResult | None = None
        try:
            for level_id, step in steps:
                if level_id not in self._levels:
                    continue
                result = step()
                results.append(result)
                if result.status is LevelStatus.FAIL and first_failure is None:
                    first_failure = result
        finally:
            self._record(item_key, results, append=append)
        return first_failure

    def _capture_dest_patient(
        self, item: UploadItem, patient: Patient, dest_patient: DestinationPatient | None
    ) -> None:
        """Contract: remembers ``dest_patient`` verbatim (no re-resolve, so
        a create-capable destination cannot POST a duplicate) for
        verify_post's read-back; falls back to the destination's own
        resolver only in standalone mode. Never mutates state (49).
        """
        self._patients[item.item_key] = patient
        if dest_patient is not None:
            self._resolved[item.item_key] = dest_patient
            return
        if self._destination is None:
            return
        try:
            resolved = self._destination.resolver.resolve(patient)
        except Exception as exc:  # a resolver hiccup must not crash verification
            logger.warning(
                "verifier resolve failed for item %s (%s)", safe_log_id(item.item_key), exc_tag(exc)
            )
            return
        if resolved is not None:
            self._resolved[item.item_key] = resolved

    def _banner(self) -> BannerCheck | None:
        return self._destination.banner if self._destination is not None else None

    def _metadata_reader(self) -> MetadataReader | None:
        dest = self._destination
        if dest is not None and isinstance(dest, MetadataReader):
            return dest
        return None

    def _document_reader(self) -> DocumentReader | None:
        dest = self._destination
        if dest is not None and isinstance(dest, DocumentReader):
            return dest
        return None

    def _record(self, item_key: str, results: list[LevelResult], *, append: bool = False) -> None:
        if append and item_key in self._results:
            self._results[item_key].extend(results)
        else:
            self._results[item_key] = list(results)
        self.last_results = list(self._results[item_key])
