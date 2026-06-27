"""Property-based invariants (Hypothesis) for the highest data-loss-risk pure
functions: date parsing and the date-spelling enumerator. These complement the
example-based unit tests with machine-generated inputs across wide ranges.
"""

from __future__ import annotations

from datetime import date, datetime

from hypothesis import given
from hypothesis import strategies as st

from anastomosis.core.timeutil import all_date_spellings, parse_date, parse_dt
from anastomosis.deliver.verify.levels import date_renderings
from anastomosis.qa.checks import _date_spellings

# Clinical-plausible range, second precision (the export formats carry no sub-second).
_DATETIMES = st.datetimes(min_value=datetime(1900, 1, 1), max_value=datetime(2100, 12, 31)).map(
    lambda d: d.replace(microsecond=0)
)
_DATES = st.dates(min_value=date(1900, 1, 1), max_value=date(2100, 12, 31))


@given(_DATETIMES)
def test_parse_dt_never_returns_a_wrong_datetime(dt: datetime) -> None:
    """For every format the module documents, parse_dt round-trips to the SAME
    wall-clock instant — it never silently returns a different date/time."""
    for fmt in ("%m/%d/%Y %I:%M:%S %p", "%m/%d/%Y %H:%M:%S", "%Y%m%d%H%M%S"):
        parsed = parse_dt(dt.strftime(fmt))
        assert parsed is not None
        assert parsed.replace(tzinfo=None) == dt  # tz is attached at the boundary


@given(_DATES)
def test_parse_date_roundtrips_the_calendar_date(d: date) -> None:
    """A date-only field parses back to exactly the calendar date written — no
    timezone shift can move it."""
    assert parse_date(d.strftime("%m/%d/%Y")) == d


@given(st.text(max_size=40))
def test_parse_dt_is_none_or_datetime_or_raises_cleanly(s: str) -> None:
    """parse_dt over ARBITRARY text either returns None / a datetime, or raises a
    ValueError — it never returns a non-datetime and never raises anything else
    (silent corruption and surprise exception types are both excluded)."""
    try:
        result = parse_dt(s)
    except ValueError:
        return  # an unrecognized non-empty format surfaces loudly — allowed
    assert result is None or isinstance(result, datetime)


@given(_DATES)
def test_date_spellings_are_unified_and_year_bearing(d: date) -> None:
    """The QA integrity check and the L2/L3 verifier enumerate the IDENTICAL set
    (the unification holds for all dates), every spelling carries the 4-digit
    year, and the set is non-empty."""
    spellings = all_date_spellings(d)
    assert spellings
    assert all(str(d.year) in s for s in spellings)
    assert _date_spellings(d) == spellings == date_renderings(d)
