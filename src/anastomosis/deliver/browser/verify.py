"""The verifier seam the upload engine calls before and after each upload.

The real L0-L6 ladder (L4 the wrong-patient banner readback) lives behind
this protocol in :mod:`anastomosis.deliver.verify`; ``--no-verify`` swaps in
:class:`NullVerifier` without touching engine code (verify is on by
default (47)). Contract: ``None`` passes, :class:`PermanentDeliveryError`
fails without retry, :class:`TransientDeliveryError` retries (48). PHI:
exception type only, never a patient-derived value in a message (2).
"""

from __future__ import annotations

from typing import Protocol

from anastomosis.core.model import Patient
from anastomosis.destinations.base import DestinationPatient, UploadItem, UploadReceipt

__all__ = ["NullVerifier", "Verifier"]


class Verifier(Protocol):
    """Pre- and post-upload verification gates around one upload."""

    def verify_pre(
        self, item: UploadItem, patient: Patient, dest_patient: DestinationPatient | None = None
    ) -> None:
        """Contract: raise to fail, return to pass. ``dest_patient``, when
        given, lets the verifier reuse the engine's already-resolved
        identity instead of re-resolving (a second resolve could POST a
        duplicate patient); optional so a standalone caller still works.
        """
        ...

    def verify_post(self, item: UploadItem, receipt: UploadReceipt) -> None:
        """Check after the upload returns. Raise to fail; return to pass."""
        ...


class NullVerifier:
    """Explicit-skip pass-through, used only when ``UploadCommand.verify``
    is ``False`` (verify is on by default (47)); the ``render`` extra the
    real ladder needs is never imported.
    """

    def verify_pre(
        self, item: UploadItem, patient: Patient, dest_patient: DestinationPatient | None = None
    ) -> None:
        return None

    def verify_post(self, item: UploadItem, receipt: UploadReceipt) -> None:
        return None
