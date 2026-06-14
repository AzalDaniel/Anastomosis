"""The verifier seam the upload engine calls before and after each upload.

This module is the *seam only*. The real verification ladder — the L0-L6
checks, of which L4 (the banner readback) is the wrong-patient defense — is a
separate plan item (PLAN item 11) and lands in its own PR. The engine depends
on this small protocol so it can be wired and tested today against a verifier
that always passes, and have the L0-L6 implementation slotted in later
without touching the engine.

Contract for an implementer of :class:`Verifier`:

* return ``None`` to pass a check;
* raise :class:`PermanentDeliveryError` (or a subclass) to fail it — the
  engine routes that to the step-appropriate terminal state and does not
  retry;
* raise :class:`TransientDeliveryError` for a condition worth retrying (a
  page still settling, a readback not yet available).

PHI rule: a verifier reads patient-derived values to do its job but MUST NOT
put them in an exception message; the engine logs the exception *type* only.
"""

from __future__ import annotations

from typing import Protocol

from anastomosis.core.model import Patient
from anastomosis.destinations.base import UploadItem, UploadReceipt

__all__ = ["NullVerifier", "Verifier"]


class Verifier(Protocol):
    """Pre- and post-upload verification gates around one upload."""

    def verify_pre(self, item: UploadItem, patient: Patient) -> None:
        """Check before any bytes are sent. Raise to fail; return to pass."""
        ...

    def verify_post(self, item: UploadItem, receipt: UploadReceipt) -> None:
        """Check after the upload returns. Raise to fail; return to pass."""
        ...


class NullVerifier:
    """A verifier that passes everything.

    Placeholder until the L0-L6 verification ladder (PLAN item 11) is ported;
    it lets the engine be wired and tested now and have real checks slotted in
    later without an engine change.
    """

    def verify_pre(self, item: UploadItem, patient: Patient) -> None:
        return None

    def verify_post(self, item: UploadItem, receipt: UploadReceipt) -> None:
        return None
