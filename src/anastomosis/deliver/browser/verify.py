"""The verifier seam the upload engine calls before and after each upload.

This module is the *seam* plus the default pass-through implementation. The real
verification ladder — the L0-L6 checks, of which L4 (the banner readback) is the
wrong-patient defense — IS implemented, in :mod:`anastomosis.deliver.verify`
(``composite.py`` + ``levels.py``), behind exactly this protocol. The engine
itself is untouched: it calls this small protocol, so the verifier can be the
default :class:`NullVerifier` (pass-through, used when verification is not
requested) or the real :class:`~anastomosis.deliver.verify.LayeredVerifier`,
which is wired OPT-IN through :class:`~anastomosis.core.upload_command.UploadCommand`'s
``verify`` flag (``anast upload --verify`` / the GUI's "Verify uploads"
checkbox).

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
    """A verifier that passes everything — the DEFAULT pass-through.

    Used when verification is not requested (``UploadCommand.verify`` is
    ``False``, the default). The real L0-L6 ladder is
    :class:`anastomosis.deliver.verify.LayeredVerifier`, wired opt-in via
    ``UploadCommand.verify`` / ``anast upload --verify``; with verify off the
    engine falls back to this, and the ``render`` extra the ladder needs is
    never imported. The engine's banner wrong-patient abort runs regardless of
    which verifier is in place.
    """

    def verify_pre(self, item: UploadItem, patient: Patient) -> None:
        return None

    def verify_post(self, item: UploadItem, receipt: UploadReceipt) -> None:
        return None
