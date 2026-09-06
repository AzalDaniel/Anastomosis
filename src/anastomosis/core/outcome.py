"""Whether the last command did its work, or stopped because the operator
said no — a distinction no exit code carries, since a decline exits 0
just like success does. :func:`declined` records it; :func:`take_declined`
reads it destructively, since a stale outcome would frame the next run. A
command that never calls :func:`declined` has none to report.
"""

from __future__ import annotations

__all__ = ["declined", "take_declined"]

_declined: str | None = None


def declined(what_did_not_happen: str) -> None:
    """Record that this command stopped because the operator said no.
    ``what_did_not_happen`` is a plain sentence naming the thing that was
    not done — "No charts were filed." — for a caller with no other way
    to know."""
    global _declined
    _declined = what_did_not_happen


def take_declined() -> str | None:
    """The refusal from the last command, if it declined, and forget it."""
    global _declined
    outcome, _declined = _declined, None
    return outcome
