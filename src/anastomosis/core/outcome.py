"""Whether the last command did its work, or stopped because someone said no.

An exit code cannot carry this distinction. A command that ends because the
operator answered "no" to its own confirmation has not *failed* — nothing went
wrong, so it exits 0 — and from outside, that is indistinguishable from the
work having been done. The guided session reads exit 0 and says "Filing has
finished."

Only the command knows which happened, so the command says so: it calls
:func:`declined` on the way out, and whoever ran it reads that with
:func:`take_declined`. The read is destructive, because an outcome describes
one run and a stale one would frame the next.

Nothing here is required for a command to work. A command that never calls
`declined` simply has no outcome to report, which is what every non-interactive
path already looks like.
"""

from __future__ import annotations

__all__ = ["declined", "take_declined"]

_declined: str | None = None


def declined(what_did_not_happen: str) -> None:
    """Record that this command stopped because the operator said no.

    ``what_did_not_happen`` is a plain sentence naming the thing that was not
    done — "No charts were filed." — because the caller reporting on this run
    has no other way to know.
    """
    global _declined
    _declined = what_did_not_happen


def take_declined() -> str | None:
    """The refusal from the last command, if it declined, and forget it."""
    global _declined
    outcome, _declined = _declined, None
    return outcome
