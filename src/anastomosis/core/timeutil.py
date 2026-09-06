"""The one date parser for vendor text (67): sentinel dates return ``None``,
mixed formats (ISO, US slash-dates, C-CDA ``TS`` blobs) are recognized, and
an unrecognized non-empty value raises rather than vanishing. Naive input
is taken as UTC (67); :func:`to_local` converts via :mod:`zoneinfo`, the
IANA database, never hand-rolled DST math."""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, timedelta, timezone, tzinfo
from zoneinfo import ZoneInfo

__all__ = [
    "age_at",
    "age_display",
    "all_date_spellings",
    "is_zero_sentinel",
    "iso_date",
    "iso_datetime",
    "parse_date",
    "parse_dt",
    "to_local",
]

# Formats beyond ISO 8601 seen in real exports, most common first. C-CDA TS
# blobs are NOT here — see _parse_hl7_ts for why strptime cannot read them.
_FORMATS = (
    "%m/%d/%Y %I:%M:%S %p",  # 3/14/2019 1:59:26 PM   (PF/Tebra TSV timestamps)
    "%m/%d/%Y %H:%M:%S",  # 03/14/2019 13:59:26
    "%m/%d/%Y %H:%M",  # 3/14/2019 13:59
    "%m/%d/%Y",  # 3/14/2019              (DOB-style)
)

# An HL7 v3 / C-CDA TS is a run of digits whose LENGTH is its precision
# ("2023" through "20230510150405"), optionally with fractional seconds and
# an offset. strptime cannot be trusted with these: its %m/%d/%H/%M/%S each
# match one OR TWO digits, so a 10-digit hour-precision value silently
# re-segments into the wrong date instead of failing. Reading fields by
# position makes a length this does not handle raise instead of guess.
_HL7_TS = re.compile(
    r"(?P<year>\d{4})(?P<month>\d{2})?(?P<day>\d{2})?"
    r"(?P<hour>\d{2})?(?P<minute>\d{2})?(?P<second>\d{2})?"
    r"(?:\.\d+)?"  # fractional seconds: legal, and below our resolution
    r"(?P<tz>Z|[+-]\d{2}:?\d{2})?"
)


def _hl7_offset(raw: str | None) -> tzinfo | None:
    if raw is None:
        return None
    if raw == "Z":
        return UTC
    hours, minutes = int(raw[1:3]), int(raw[-2:])
    delta = timedelta(hours=hours, minutes=minutes)
    return timezone(-delta if raw[0] == "-" else delta)


def _parse_hl7_ts(text: str) -> datetime | None:
    """Read a C-CDA TS by field position. ``None`` if it is not one."""
    match = _HL7_TS.fullmatch(text)
    if match is None:
        return None
    # Precision truncates from the right, and the pattern enforces that on its
    # own: the fields are greedy and in order, so a later one cannot be filled
    # unless every coarser one already is. No separate ordering check needed.
    parts = {name: match.group(name) for name in ("month", "day", "hour", "minute", "second")}
    try:
        return datetime(
            year=int(match.group("year")),
            month=int(parts["month"] or 1),
            day=int(parts["day"] or 1),
            hour=int(parts["hour"] or 0),
            minute=int(parts["minute"] or 0),
            second=int(parts["second"] or 0),
            tzinfo=_hl7_offset(match.group("tz")),
        )
    except ValueError:
        # Shaped like a TS but out of range (month 15, day 32). Fall through so
        # the remaining formats get a look and the caller sees parse_dt's error.
        return None


def _parse_raw(text: str) -> datetime | None:
    if (parsed := _parse_hl7_ts(text)) is not None:
        return parsed
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


def is_zero_sentinel(value: str | None) -> bool:
    """Contract: true for a value naming no year at all, one notch past the
    year-1 sentinel :func:`parse_dt` already absorbs (67). NOT consulted by
    :func:`parse_dt` itself (a bare "0" in a row-based adapter must still
    raise); only the C-CDA ``_ts`` readers call this directly.
    ``2023-13-45`` still raises: it names a year, just an unreadable one."""
    if value is None:
        return False
    text = value.strip()
    return bool(text) and set(text) == {"0"}


def parse_dt(value: str | None, *, assume: tzinfo = UTC) -> datetime | None:
    """Parse a source timestamp into an aware :class:`datetime` (67). Naive
    inputs get ``assume`` attached (default UTC); an offset carried in the
    input is kept. ``None`` for empty values and year-1 sentinels; raises
    :exc:`ValueError` for an unrecognized non-empty value."""
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


def _iso_day(value: object) -> str:
    """An ISO value's calendar-date part, a partial one widened to its first day."""
    parts = str(value).split("T", 1)[0].split("-")
    if len(parts) == 1 and parts[0].isdigit():
        return f"{parts[0]}-01-01"
    if len(parts) == 2:
        return f"{parts[0]}-{parts[1]}-01"
    return "-".join(parts)


def iso_date(value: object, *, pad_partial: bool = False) -> date | None:
    """Contract: an ISO 8601 date as a :class:`date`; ``None`` for an empty
    value; :exc:`ValueError` for anything unreadable. ``pad_partial`` accepts
    the wider shapes a FHIR ``date``/``dateTime`` may take — a bare year, a
    year and month, a time of day — and widens each to a calendar date."""
    if not value:
        return None
    return date.fromisoformat(_iso_day(value) if pad_partial else str(value))


def iso_datetime(value: object, *, pad_partial: bool = False) -> datetime | None:
    """Contract: :func:`iso_date`'s rules for an ISO 8601 timestamp; an offset
    the value carries is kept. ``pad_partial`` widens a value naming no time of
    day, a partial date included, to midnight instead of failing."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        if not pad_partial:
            raise
        return datetime.fromisoformat(_iso_day(value))


def all_date_spellings(value: date) -> set[str]:
    """Every chart spelling a pack might render ``value`` as, shared by the
    L2/L3 delivery verifier and the QA integrity check so they can never
    diverge on which spelling counts as present. Unpadded ``%-m``/``%-d``
    forms are built by hand: those strftime codes are glibc-only, absent
    on Windows."""
    return {
        # numeric, padded and unpadded, slash and dash
        f"{value.month:02d}/{value.day:02d}/{value.year}",
        f"{value.month}/{value.day}/{value.year}",
        f"{value.month:02d}-{value.day:02d}-{value.year}",
        f"{value.month}-{value.day}-{value.year}",
        # month-name spellings (full and abbreviated), padded and unpadded day
        f"{value.strftime('%B')} {value.day:02d}, {value.year}",
        f"{value.strftime('%B')} {value.day}, {value.year}",
        f"{value.strftime('%b')} {value.day:02d}, {value.year}",
        f"{value.strftime('%b')} {value.day}, {value.year}",
    }


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
