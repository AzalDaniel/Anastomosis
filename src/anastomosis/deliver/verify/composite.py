"""The layered verifier: stack L0-L6 behind the engine's Verifier seam.

:class:`LayeredVerifier` implements the engine's
:class:`~anastomosis.deliver.browser.verify.Verifier` protocol without touching
the engine — it is a pure plug-in. ``verify_pre`` runs L0-L4 in order;
``verify_post`` runs L5-L6. The engine routes a ``verify_pre`` failure to
``PRE_VERIFY_FAILED`` and a ``verify_post`` failure to ``POST_VERIFY_FAILED``,
and a :class:`WrongPatientError` to the run-level abort — so this class maps
its outcomes onto exactly those signals:

* first failing pre-level  -> raise :class:`PermanentDeliveryError`;
* first failing post-level -> raise :class:`PermanentDeliveryError`;
* an L4 banner wrong-patient -> let :class:`WrongPatientError` propagate
  (run-level abort, not one item's failure).

The L2 DOB hard-fail is a *document*-level wrong-chart catch and returns a
``fail`` :class:`LevelResult` (routed to ``PRE_VERIFY_FAILED``, one item) — not
a run abort. Only L4, the live-banner readback, aborts the whole run, because
only it proves the *destination's open chart* is the wrong patient.

Every raised message carries only level + field NAMES — never a patient value
(the engine logs ``exc_tag`` type names, but a defensive message must be safe
on its own). All levels run even after a failure (so a report can show the
whole table) *except* a wrong-patient, which aborts immediately for safety.

Stateful read-back resolution (documented carefully because it is subtle):
the engine's ``verify_post(item, receipt)`` does not carry the
:class:`Patient` or the :class:`DestinationPatient`, but L5/L6 need the
destination patient to read a document back. The Verifier protocol is the
engine seam and must NOT change (changing it breaks the pre/post pairing). So
when a destination is set, ``verify_pre`` resolves the destination patient once
(``destination.resolver.resolve(patient)``) and remembers it keyed by
``item_key``; ``verify_post`` looks it up. In standalone mode (no destination)
the map is empty and L5/L6 skip. The state lives on the *instance* and is per
patient-item, not shared across workers: the parallel runner builds one
verifier per worker via ``verifier_factory``, so no two workers share this map.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping

from anastomosis.core.logutil import exc_tag
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
)

__all__ = ["ALL_LEVELS", "LayeredVerifier"]

logger = logging.getLogger(__name__)

# Every level id the stack knows, in run order. The default ``levels`` set.
ALL_LEVELS: frozenset[str] = frozenset({"L0", "L1", "L2", "L3", "L4", "L5", "L6"})


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
    ) -> None:
        # item.encounter_id -> Encounter, for L3 "dos".
        self._records = dict(records) if records else {}
        self._pack = pack
        self._destination = destination
        self._expected_pages = dict(expected_pages) if expected_pages else {}
        self._levels = levels if levels is not None else ALL_LEVELS
        # item_key -> DestinationPatient, captured in verify_pre so verify_post
        # (which the engine calls without a patient) can resolve a read-back.
        self._resolved: dict[str, DestinationPatient] = {}
        # item_key -> canonical Patient, captured in verify_pre for L6's
        # identity re-assertion (the Verifier protocol's verify_post does not
        # carry the patient).
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

    def verify_pre(self, item: UploadItem, patient: Patient) -> None:
        """Run L0-L4; raise on the first failure (after collecting all results).

        A :class:`WrongPatientError` from L4's live-banner readback propagates
        immediately — patient safety aborts the whole run rather than running
        the rest of the table. Any *failing* level (including L2's DOB hard-fail)
        is recorded, the rest still run, then the first failure is raised as a
        PRE_VERIFY error (one item to ``PRE_VERIFY_FAILED``).
        """
        # Capture the destination patient once (feeds L5/L6 read-back later).
        self._capture_dest_patient(item, patient)
        encounter = self._records.get(item.encounter_id)

        steps: tuple[tuple[str, Callable[[], LevelResult]], ...] = (
            ("L0", lambda: self._l0.run(item)),
            (
                "L1",
                lambda: self._l1.run(item, expected_pages=self._expected_pages.get(item.item_key)),
            ),
            ("L2", lambda: self._l2.run(item, patient)),
            (
                "L3",
                lambda: self._l3.run(item, patient, pack=self._pack, encounter=encounter),
            ),
            ("L4", lambda: self._l4.run(patient, banner=self._banner())),
        )
        # An L4 banner wrong-patient escapes the loop as a WrongPatientError —
        # the engine's abort handler owns it; the partial table recorded so far
        # stays on the instance for a report to inspect.
        first_failure = self._run_steps(item.item_key, steps)
        if first_failure is not None:
            raise _PreVerifyError(f"{first_failure.level}: {first_failure.detail}")

    def verify_post(self, item: UploadItem, receipt: UploadReceipt) -> None:
        """Run L5-L6; raise on the first failure (after collecting all results).

        Uses the :class:`DestinationPatient` captured in :meth:`verify_pre` and
        the receipt's ``destination_doc_id``. In standalone mode (no
        destination, nothing captured) L5/L6 skip.
        """
        dest_patient = self._resolved.get(item.item_key)
        doc_id = receipt.destination_doc_id
        # The canonical patient captured in verify_pre: L6's reprocessed tier
        # re-asserts IDENTITY against the read-back (a whole-page similarity
        # ratio false-passes a swapped chart — see L6RoundTrip's docstring).
        patient = self._patients.get(item.item_key)

        steps: tuple[tuple[str, Callable[[], LevelResult]], ...] = (
            (
                "L5",
                lambda: self._l5.run(item, dest_patient, doc_id, reader=self._metadata_reader()),
            ),
            (
                "L6",
                lambda: self._l6.run(
                    item, dest_patient, doc_id, reader=self._document_reader(), patient=patient
                ),
            ),
        )
        first_failure = self._run_steps(item.item_key, steps, append=True)
        if first_failure is not None:
            raise _PostVerifyError(f"{first_failure.level}: {first_failure.detail}")

    # --- report accessors ---

    def results_for(self, item_key: str) -> list[LevelResult]:
        """The collected level table for ``item_key`` (empty if never verified)."""
        return list(self._results.get(item_key, []))

    # --- helpers ---

    def _run_steps(
        self,
        item_key: str,
        steps: tuple[tuple[str, Callable[[], LevelResult]], ...],
        *,
        append: bool = False,
    ) -> LevelResult | None:
        """Run the in-scope steps, recording every result; return first failure.

        Every step runs even after a failure (so the recorded table is
        complete). A step that *raises* (L4's :class:`WrongPatientError`)
        records the results gathered so far before the exception escapes — the
        partial table stays inspectable — then the exception propagates to the
        engine's abort handler.
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

    def _capture_dest_patient(self, item: UploadItem, patient: Patient) -> None:
        """Resolve and remember the destination patient (for post read-back).

        Mirrors the engine's own resolve so L5/L6 can read a document back
        without the Verifier protocol carrying the patient into ``verify_post``.
        A ``None`` resolution (patient not found) just leaves L5/L6 to skip.
        The canonical patient is remembered unconditionally — L6's identity
        re-assertion needs it even when destination resolution fails.
        """
        self._patients[item.item_key] = patient
        if self._destination is None:
            return
        try:
            resolved = self._destination.resolver.resolve(patient)
        except Exception as exc:  # a resolver hiccup must not crash verification
            logger.warning("verifier resolve failed for item %s (%s)", item.item_key, exc_tag(exc))
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
