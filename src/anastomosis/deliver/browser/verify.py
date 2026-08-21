"""The verifier seam the upload engine calls before and after each upload.

This module is the *seam* plus the default pass-through implementation. The real
verification ladder — the L0-L6 checks, of which L4 (the banner readback) is the
wrong-patient defense — IS implemented, in :mod:`anastomosis.deliver.verify`
(``composite.py`` + ``levels.py``), behind exactly this protocol. The engine
itself is untouched: it calls this small protocol, so the verifier can be the
default :class:`NullVerifier` (pass-through, used only when verification is
explicitly skipped) or the real :class:`~anastomosis.deliver.verify.LayeredVerifier`,
wired through :class:`~anastomosis.core.upload_command.UploadCommand`'s ``verify``
flag — which is ON by default (``anast upload`` / the GUI's pre-checked "Verify
uploads" box); ``--no-verify`` / unchecking selects the pass-through.

The engine's banner wrong-patient abort runs BEFORE the verifier and regardless
of which verifier is in place — it is the engine's own safety gate, not a
verifier level.

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
from anastomosis.destinations.base import DestinationPatient, UploadItem, UploadReceipt

__all__ = ["NullVerifier", "Verifier"]


class Verifier(Protocol):
    """Pre- and post-upload verification gates around one upload."""

    def verify_pre(
        self, item: UploadItem, patient: Patient, dest_patient: DestinationPatient | None = None
    ) -> None:
        """Check before any bytes are sent. Raise to fail; return to pass.

        ``dest_patient`` is the destination patient the engine ALREADY resolved
        for this item. Passing it in lets a verifier reuse that identity for its
        post-upload read-back instead of RE-resolving — a second resolve on a
        create-capable destination could POST a duplicate patient. It is
        optional so a standalone caller (no prior resolve) still works; such a
        caller's verifier resolves side-effect-free or skips read-back.
        """
        ...

    def verify_post(self, item: UploadItem, receipt: UploadReceipt) -> None:
        """Check after the upload returns. Raise to fail; return to pass."""
        ...


class NullVerifier:
    """A verifier that passes everything — the explicit-skip pass-through.

    Used only when verification is explicitly skipped (``UploadCommand.verify``
    is ``False`` — ``--no-verify`` / the unchecked GUI box); verification is ON
    by default. The real L0-L6 ladder is
    :class:`anastomosis.deliver.verify.LayeredVerifier`, wired via
    ``UploadCommand.verify``; with verify off the engine falls back to this and
    the ``render`` extra the ladder needs is never imported. The engine's banner
    wrong-patient abort runs regardless of which verifier is in place.
    """

    def verify_pre(
        self, item: UploadItem, patient: Patient, dest_patient: DestinationPatient | None = None
    ) -> None:
        return None

    def verify_post(self, item: UploadItem, receipt: UploadReceipt) -> None:
        return None
