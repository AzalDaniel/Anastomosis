"""Time handling for clinical source data.

EHI exports are full of temporal traps this module exists to absorb:

* **Sentinel dates.** Practice Fusion / Tebra TSVs spell "no value" as
  ``1/1/0001 12:00:00 AM`` (the SQL min-date). A sentinel parsed as a real
  date puts nonsense on a chart, so parsing returns ``None`` for year-1 dates.
* **Mixed formats.** A single export mixes several timestamp spellings
  (ISO, US slash-dates with and without 12-hour clocks, C-CDA ``TS`` blobs).
  :func:`parse_dt` recognizes all of them; an *unrecognized* non-empty value
  raises instead of vanishing — silent data loss is never acceptable here.
* **The naive-UTC convention.** Source database timestamps arrive naive but
  mean UTC, while charts must show practice-local wall-clock time. To keep a
  naive datetime from ever traveling through the pipeline (ruff's DTZ rules
  enforce this), :func:`parse_dt` attaches a timezone at the parse boundary,
  and :func:`to_local` converts via :mod:`zoneinfo` — the IANA database, not
  hand-rolled DST math. The DST-oracle test in ``tests/unit/test_timeutil.py``
  sweeps US transitions across two decades to prove zoneinfo agrees with the
  predecessor's hand-rolled rule everywhere that rule was correct.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, tzinfo
from zoneinfo import ZoneInfo

__all__ = ["age_at", "age_display", "parse_date", "parse_dt", "to_local"]

# Formats beyond ISO 8601 seen in real exports, most common first.
_FORMATS = (
    "%m/%d/%Y %I:%M:%S %p",  # 3/14/2019 1:59:26 PM   (PF/Tebra TSV timestamps)
    "%m/%d/%Y %H:%M:%S",  # 03/14/2019 13:59:26
    "%m/%d/%Y %H:%M",  # 3/14/2019 13:59
    "%m/%d/%Y",  # 3/14/2019              (DOB-style)
    "%Y%m%d%H%M%S%z",  # 20190314135926-0500    (C-CDA TS with UTC offset)
    "%Y%m%d%H%M%S",  # 20190314135926         (C-CDA TS)
    "%Y%m%d",  # 20190314               (C-CDA date-only TS)
)


def _parse_raw(text: str) -> datetime | None:
    try:
        # Handles ISO dates, "YYYY-MM-DD HH:MM:SS[.ffffff]", offsets, and "Z".
        return datetime.fromisoformat(text)
    except ValueError:
        pass
    for fmt in _FORMATS:
        try:
            return datetime.strptime(text, fmt)  # noqa: DTZ007 — parse_dt attaches tz
        except ValueError:
            continue
    return None


def parse_dt(value: str | None, *, assume: tzinfo = UTC) -> datetime | None:
    """Parse a source timestamp into an aware :class:`datetime`.

    Naive inputs get ``assume`` attached (default UTC — the EHI database
    convention); inputs carrying their own offset keep it. Returns ``None``
    for empty values and year-1 sentinels; raises :exc:`ValueError` for a
    non-empty value in a format this module has never seen, so new source
    quirks surface in QA instead of disappearing.
    """
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None
    parsed = _parse_raw(text)
    if parsed is None:
        raise ValueError(f"unrecognized date/time format: {text!r}")
    if parsed.year == 1:  # SQL min-date sentinel, e.g. "1/1/0001 12:00:00 AM"
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=assume)
    return parsed


def parse_date(value: str | None) -> date | None:
    """Parse a source date string as written — no timezone conversion.

    For date-only fields (DOB, onset dates) the calendar date in the export
    is the truth; shifting it across timezones would corrupt it.
    """
    parsed = parse_dt(value)
    return None if parsed is None else parsed.date()


def to_local(dt: datetime, tz: str | tzinfo) -> datetime:
    """Convert ``dt`` to practice-local time. Naive input is taken as UTC."""
    zone = ZoneInfo(tz) if isinstance(tz, str) else tz
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(zone)


def age_at(dob: date, on: date) -> int:
    """Age in completed years on the given date."""
    return on.year - dob.year - ((on.month, on.day) < (dob.month, dob.day))


def age_display(dob: date, on: date) -> str:
    """Clinical age string: days under 1 month, months under 2 years, else years."""
    days = (on - dob).days
    if days < 0:
        raise ValueError("age_display: date of birth is after the as-of date")
    months = (on.year - dob.year) * 12 + on.month - dob.month - (on.day < dob.day)
    if months < 1:
        return f"{days} day" if days == 1 else f"{days} days"
    if months < 24:
        return f"{months} mo" if months == 1 else f"{months} mos"
    years = age_at(dob, on)
    return f"{years} yr" if years == 1 else f"{years} yrs"
