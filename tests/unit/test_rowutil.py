"""Tests for sources._rowutil — the row-cell helpers pf_tebra and oracle_ehi
share (and, through the same `core.timeutil.parse_dt`/`parse_date` the
learned adapter's `parse_date`/`parse_datetime` transform verbs reach too).

`clean_dt`/`clean_date` are a thin wrap over `parse_dt`/`parse_date`, with no
ledger or extensions dict of their own to credit a silently-absorbed value on
— so a value these three adapters' date columns could state has nowhere
honest to land except a loud failure. #385 first read a C-CDA vendor's
all-zero TS `@value` ("0") as absent inside `parse_dt` itself, which is this
module's OWN reader too: `clean_dt({"c": "0"}, "c")` silently returned `None`
for a TSV cell of literal `"0"` on that fix, in all three adapters, with
nothing anywhere recording it. The zero-sentinel reading now lives in the
C-CDA parser's own `_ts`/`_ts_date` (see `test_ccda.py`), never here —
`clean_dt`/`clean_date` must keep raising on a value this module has never
seen, exactly as they did before #385 touched anything.
"""

import pytest

from anastomosis.sources._rowutil import clean_date, clean_dt


@pytest.mark.parametrize("raw", ["0", "00000000"])
def test_clean_dt_raises_on_a_zero_run_it_cannot_read(raw: str) -> None:
    """#385 round two: a TSV cell holding a literal zero-run is a value that
    states something, not a vendor's C-CDA "no date" spelling — pf_tebra and
    oracle_ehi read every date column through this function, and neither has
    anywhere to record a silently-absorbed value the way the C-CDA parser
    can. Red on the interim fix in a2858a4 (returned `None`); green both
    before that commit and after this one.
    """
    with pytest.raises(ValueError, match="unrecognized"):
        clean_dt({"c": raw}, "c")


@pytest.mark.parametrize("raw", ["0", "00000000"])
def test_clean_date_raises_on_a_zero_run_it_cannot_read(raw: str) -> None:
    """Same guard, the date-only reader. `clean_date` calls `parse_date`,
    which calls `parse_dt` underneath, so it raises for the identical reason.
    """
    with pytest.raises(ValueError, match="unrecognized"):
        clean_date({"c": raw}, "c")


@pytest.mark.parametrize("raw", ["0.0", "2023-13-45"])
def test_clean_dt_still_raises_on_ordinary_unrecognized_cells(raw: str) -> None:
    """Unchanged by #385 either way: a cell that was never a zero-run sentinel
    candidate always raised, and still does."""
    with pytest.raises(ValueError, match="unrecognized"):
        clean_dt({"c": raw}, "c")


def test_clean_dt_still_reads_the_sql_year_one_sentinel_as_absent() -> None:
    """The ORIGINAL sentinel this module exists to absorb — Practice Fusion /
    Tebra's `1/1/0001 12:00:00 AM` — must still read as `None`, not raise:
    #385 narrowed nothing about this reading, only added (and then withdrew)
    a second one."""
    assert clean_dt({"c": "1/1/0001 12:00:00 AM"}, "c") is None
    assert clean_date({"c": "1/1/0001"}, "c") is None


def test_clean_dt_and_clean_date_pass_through_blanks_and_missing_columns() -> None:
    """A column absent from the row, or a cell holding only whitespace, reads
    as `None` — never a raise. Both reach the same `clean_cell` sentinel
    handling `test_textutil.py` already covers; this is the fact `_rowutil`'s
    own callers depend on."""
    assert clean_dt({}, "c") is None
    assert clean_dt({"c": "   "}, "c") is None
    assert clean_date({}, "c") is None
