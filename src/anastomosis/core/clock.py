"""The one wall-clock read every stamping site shares.

Every ``generated_at``/``ingested_at``/render-day stamp reads through here,
never through :func:`datetime.datetime.now` directly. With
``SOURCE_DATE_EPOCH`` set (https://reproducible-builds.org/specs/source-date-epoch/),
both functions return that pinned instant instead of the real clock, so two
runs stamp byte-identically; unset, nothing changes.

Not for a lock timeout, a rate limiter, or a retry backoff — those measure
elapsed time and stay on :func:`time.monotonic`.
"""

from __future__ import annotations

import os
from datetime import UTC, date, datetime

__all__ = ["now", "today"]


def now() -> datetime:
    """The current UTC instant, or ``SOURCE_DATE_EPOCH`` when that is set."""
    epoch = os.environ.get("SOURCE_DATE_EPOCH")
    if epoch is not None:
        return datetime.fromtimestamp(int(epoch), tz=UTC)
    return datetime.now(UTC)


def today() -> date:
    """The host's calendar day, or ``SOURCE_DATE_EPOCH``'s day — naive and
    host-local like :func:`datetime.date.today`, which this replaces."""
    epoch = os.environ.get("SOURCE_DATE_EPOCH")
    if epoch is not None:
        return datetime.fromtimestamp(int(epoch)).date()  # noqa: DTZ006 - host-local by design
    return date.today()  # noqa: DTZ011 - host-local by design; see module docstring
