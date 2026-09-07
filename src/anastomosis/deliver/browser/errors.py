"""Delivery error taxonomy for the browser upload engine.

Which of these an operation raises drives the engine's retry, abort, and
terminal-state decisions; each class below says which. PHI: never
``str(exc)`` in a message or log, ``exc_tag(exc)`` only (2).
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
    """The destination chart is the wrong patient; aborts the entire run,
    never just the item (48)."""


class IllegalTransitionError(DeliveryError):
    """An illegal upload-state transition was attempted (a logic error)."""
