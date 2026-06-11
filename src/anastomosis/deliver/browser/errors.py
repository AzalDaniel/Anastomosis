"""Delivery error taxonomy for the browser upload engine.

The engine's retry, abort, and terminal-state decisions are driven by which
of these an operation raises:

* :class:`TransientDeliveryError` — retryable (a flaky selector, a slow
  page, a dropped session); the state machine routes it to ``RETRY_WAIT``.
* :class:`PermanentDeliveryError` — not retryable; the item fails for good.
* :class:`WrongPatientError` — a patient-safety event; the engine aborts
  the *entire run*, not just the item.
* :class:`IllegalTransitionError` — a programming error in the state
  machine; loud by design (an illegal transition must never pass silently).

PHI rule: the *message* on any of these exceptions MUST NOT contain a
patient-derived value (name, DOB, address, identifier). Callers log
``exc_tag(exc)`` — the exception *type* name — never ``str(exc)``; the
ledger persists ``last_error_type``/``error_type`` as type names only.
"""

from __future__ import annotations

__all__ = [
    "DeliveryError",
    "IllegalTransitionError",
    "PermanentDeliveryError",
    "TransientDeliveryError",
    "WrongPatientError",
]


class DeliveryError(Exception):
    """Base class for every browser-delivery failure."""


class TransientDeliveryError(DeliveryError):
    """A retryable failure — the engine waits and tries the item again."""


class PermanentDeliveryError(DeliveryError):
    """A non-retryable failure — the item is done, unsuccessfully."""


class WrongPatientError(PermanentDeliveryError):
    """A patient-safety event: the destination chart is the wrong patient.

    The engine aborts the entire run when this is raised — never just the
    one item. Filing into the wrong chart is the failure this subsystem
    exists to prevent, so it stops everything.
    """


class IllegalTransitionError(DeliveryError):
    """An illegal upload-state transition was attempted (a logic error)."""
