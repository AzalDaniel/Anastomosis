"""Tests for core.timeutil — including the DST oracle.

The oracle below is a clean re-implementation of the predecessor's hand-rolled
US daylight-saving rule (post-2007 Energy Policy Act: DST starts the second
Sunday of March at 2:00 local standard time and ends the first Sunday of
November at 2:00 local daylight time). zoneinfo must agree with it at every
instant we sweep — that proves the port changed the mechanism (IANA database
instead of hand-rolled math) without changing a single rendered timestamp.
"""

from datetime import UTC, date, datetime, timedelta, timezone

import pytest

from anastomosis.core.timeutil import (
    age_at,
    age_display,
    is_zero_sentinel,
    iso_date,
    iso_datetime,
    parse_date,
    parse_dt,
    to_local,
)

# --- parsing ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("3/14/2019 1:59:26 PM", datetime(2019, 3, 14, 13, 59, 26, tzinfo=UTC)),
        ("03/14/2019 13:59:26", datetime(2019, 3, 14, 13, 59, 26, tzinfo=UTC)),
        ("3/14/2019 13:59", datetime(2019, 3, 14, 13, 59, tzinfo=UTC)),
        ("3/14/2019", datetime(2019, 3, 14, tzinfo=UTC)),
        ("2019-03-14 13:59:26", datetime(2019, 3, 14, 13, 59, 26, tzinfo=UTC)),
        ("2019-03-14T13:59:26.500", datetime(2019, 3, 14, 13, 59, 26, 500000, tzinfo=UTC)),
        ("2019-03-14", datetime(2019, 3, 14, tzinfo=UTC)),
        ("20190314135926", datetime(2019, 3, 14, 13, 59, 26, tzinfo=UTC)),
        ("20190314", datetime(2019, 3, 14, tzinfo=UTC)),
        # C-CDA TS carrying its own UTC offset (the %z form) keeps it verbatim.
        (
            "20230510140000-0500",
            datetime(2023, 5, 10, 14, 0, 0, tzinfo=timezone(timedelta(hours=-5))),
        ),
    ],
)
def test_parse_dt_formats(raw: str, expected: datetime) -> None:
    assert parse_dt(raw) == expected


def test_parse_dt_ccda_ts_offset_is_aware() -> None:
    # The C-CDA TS "20230510140000-0500" must round to a -5h aware datetime;
    # the bare-date C-CDA TS "20230510" must still parse as a calendar date.
    parsed = parse_dt("20230510140000-0500")
    assert parsed is not None
    assert parsed.utcoffset() == timedelta(hours=-5)
    assert parse_date("20230510") == date(2023, 5, 10)


def test_parse_dt_keeps_explicit_offsets() -> None:
    parsed = parse_dt("2019-03-14T13:59:26-05:00")
    assert parsed is not None
    assert parsed.utcoffset() == timedelta(hours=-5)
    zulu = parse_dt("2019-03-14T13:59:26Z")
    assert zulu == datetime(2019, 3, 14, 13, 59, 26, tzinfo=UTC)


@pytest.mark.parametrize("raw", [None, "", "   ", "1/1/0001", "1/1/0001 12:00:00 AM", "0001-01-01"])
def test_parse_dt_sentinels_and_blanks(raw: str | None) -> None:
    assert parse_dt(raw) is None


def test_parse_dt_rejects_unknown_formats_loudly() -> None:
    with pytest.raises(ValueError, match="unrecognized"):
        parse_dt("the 14th of March")


@pytest.mark.parametrize("raw", ["0", "00", "000", "0000", "000000", "00000000", "0" * 12])
def test_a_run_of_zeros_is_the_source_saying_nothing(raw: str) -> None:
    """#385: `is_zero_sentinel` names a run of zeros (any length) as a
    value that names no year. Shared PREDICATE only: `parse_dt`/
    `parse_date` never consult it (see the sibling test below), since
    only the C-CDA parser's own readers know a "0" cell claims a C-CDA
    sentinel rather than a literal TSV value."""
    assert is_zero_sentinel(raw)


@pytest.mark.parametrize("raw", ["0", "00", "000", "0000", "000000", "00000000", "0" * 12])
def test_parse_dt_still_raises_on_a_zero_run(raw: str) -> None:
    """#385 round two: `parse_dt`/`parse_date` must keep raising on a
    zero run — row-based adapters (pf_tebra, oracle_ehi, the learned
    adapter) share this function, so swallowing "0" here would treat a
    genuine TSV value as absent in three unrelated adapters. The
    C-CDA-specific reading lives in the C-CDA-specific caller."""
    with pytest.raises(ValueError, match="unrecognized"):
        parse_dt(raw)
    with pytest.raises(ValueError, match="unrecognized"):
        parse_date(raw)


@pytest.mark.parametrize("raw", ["0.0", "-0", "0000-00-00", "2023-13-45", "20230510T"])
def test_a_near_miss_is_not_a_zero_sentinel_and_still_raises(raw: str) -> None:
    """Narrowed, not loosened: each of these holds a character that is not
    `0` (a dot, a minus sign, a dash, a real-looking year that gets the rest
    wrong), so `is_zero_sentinel` must not match it and `parse_dt` must keep
    raising — silently accepting a near-miss as "no date" would hide a vendor
    quirk the loud-failure contract exists to surface."""
    assert not is_zero_sentinel(raw)
    with pytest.raises(ValueError, match="unrecognized"):
        parse_dt(raw)


def test_parse_date_is_calendar_faithful() -> None:
    # A date-only field never shifts across timezones.
    assert parse_date("3/14/2019") == date(2019, 3, 14)
    assert parse_date("1/1/0001") is None


# --- to_local: known answers around real transitions -----------------------


def test_to_local_naive_means_utc() -> None:
    local = to_local(datetime(2019, 1, 15, 17, 0), "America/New_York")
    assert (local.hour, local.minute) == (12, 0)  # EST = UTC-5
    summer = to_local(datetime(2019, 7, 15, 17, 0), "America/New_York")
    assert summer.hour == 13  # EDT = UTC-4


def test_to_local_spring_forward_skips_an_hour() -> None:
    # 2019-03-10: 06:59 UTC is 1:59 EST; one UTC minute later is 3:00 EDT.
    before = to_local(datetime(2019, 3, 10, 6, 59, tzinfo=UTC), "America/New_York")
    after = to_local(datetime(2019, 3, 10, 7, 0, tzinfo=UTC), "America/New_York")
    assert (before.hour, before.minute) == (1, 59)
    assert (after.hour, after.minute) == (3, 0)


def test_to_local_fall_back_repeats_an_hour() -> None:
    # 2019-11-03: 05:30 UTC is 1:30 EDT; 06:30 UTC is 1:30 EST again.
    first = to_local(datetime(2019, 11, 3, 5, 30, tzinfo=UTC), "America/New_York")
    second = to_local(datetime(2019, 11, 3, 6, 30, tzinfo=UTC), "America/New_York")
    assert (first.hour, first.minute) == (second.hour, second.minute) == (1, 30)
    assert first.utcoffset() == timedelta(hours=-4)
    assert second.utcoffset() == timedelta(hours=-5)


# --- the DST oracle ---------------------------------------------------------


def _nth_sunday(year: int, month: int, n: int) -> date:
    first = date(year, month, 1)
    first_sunday = first + timedelta(days=(6 - first.weekday()) % 7)
    return first_sunday + timedelta(weeks=n - 1)


def _oracle_offset(utc_dt: datetime, std_offset: int) -> int:
    """The predecessor's hand-rolled US DST rule, re-typed as a test oracle."""
    year = utc_dt.year
    # DST starts at 2:00 local *standard* time on the second Sunday of March...
    spring = datetime(year, 3, _nth_sunday(year, 3, 2).day, 2 - std_offset, 0, tzinfo=UTC)
    # ...and ends at 2:00 local *daylight* time on the first Sunday of November.
    fall = datetime(year, 11, _nth_sunday(year, 11, 1).day, 2 - (std_offset + 1), 0, tzinfo=UTC)
    return std_offset + 1 if spring <= utc_dt < fall else std_offset


@pytest.mark.parametrize(
    ("tz", "std_offset"),
    [("America/New_York", -5), ("America/Chicago", -6), ("America/Los_Angeles", -8)],
)
def test_zoneinfo_agrees_with_hand_rolled_dst_oracle(tz: str, std_offset: int) -> None:
    instants: list[datetime] = []
    for year in range(2007, 2031):
        # Dense sweep crossing both transition instants: 15-minute steps, ±6h.
        spring = datetime(year, 3, _nth_sunday(year, 3, 2).day, 2 - std_offset, 0, tzinfo=UTC)
        fall = datetime(year, 11, _nth_sunday(year, 11, 1).day, 2 - (std_offset + 1), 0, tzinfo=UTC)
        for transition in (spring, fall):
            instants += [transition + timedelta(minutes=15 * k) for k in range(-24, 25)]
        # Plus a mid-month spot check across the whole year.
        instants += [datetime(year, m, 15, 12, 0, tzinfo=UTC) for m in range(1, 13)]
    assert len(instants) > 2500
    for utc_dt in instants:
        expected = timedelta(hours=_oracle_offset(utc_dt, std_offset))
        assert to_local(utc_dt, tz).utcoffset() == expected, f"{tz} disagrees at {utc_dt}"


# --- ages -------------------------------------------------------------------


def test_age_at_birthday_boundary() -> None:
    dob = date(1990, 3, 14)
    assert age_at(dob, date(2024, 3, 13)) == 33
    assert age_at(dob, date(2024, 3, 14)) == 34


@pytest.mark.parametrize(
    ("dob", "on", "expected"),
    [
        (date(2024, 3, 1), date(2024, 3, 21), "20 days"),
        (date(2024, 3, 1), date(2024, 3, 2), "1 day"),
        (date(2024, 3, 1), date(2024, 4, 15), "1 mo"),
        (date(2023, 3, 1), date(2024, 2, 15), "11 mos"),
        (date(2022, 6, 1), date(2024, 4, 15), "22 mos"),
        (date(2022, 3, 1), date(2024, 3, 1), "2 yrs"),
        (date(1990, 3, 14), date(2024, 6, 1), "34 yrs"),
    ],
)
def test_age_display_clinical_buckets(dob: date, on: date, expected: str) -> None:
    assert age_display(dob, on) == expected


def test_age_display_rejects_future_dob() -> None:
    with pytest.raises(ValueError, match="after"):
        age_display(date(2030, 1, 1), date(2024, 1, 1))


def test_hour_precision_ts_is_read_by_position_not_re_segmented() -> None:
    """The defect this parser exists to prevent (#241): ``strptime``'s
    %m/%d/%H/%M/%S each match one OR TWO digits, so a 10-digit
    hour-precision TS would re-segment instead of failing —
    "2023051015" reading as 2023-05-01, nine days wrong with no
    exception."""
    assert parse_date("2023051015") == date(2023, 5, 10)
    assert parse_date("20230510") == date(2023, 5, 10)
    assert parse_dt("202305101504") == datetime(2023, 5, 10, 15, 4, tzinfo=UTC)


def test_legal_hl7_precisions_parse_instead_of_raising() -> None:
    """A TS's length IS its precision; year and year-month are legal HL7 v3."""
    assert parse_date("2023") == date(2023, 1, 1)
    assert parse_date("202305") == date(2023, 5, 1)
    assert parse_date("20230510150405.000") == date(2023, 5, 10)
    assert parse_dt("20230510150405.000-0500") == datetime(
        2023, 5, 10, 15, 4, 5, tzinfo=timezone(-timedelta(hours=5))
    )
    assert parse_dt("20230510150405Z") == datetime(2023, 5, 10, 15, 4, 5, tzinfo=UTC)


def test_a_digit_run_we_cannot_place_still_raises() -> None:
    """Narrowed, not loosened. A length the pattern does not cover must not be
    guessed at — that is the whole point of reading fields by position.
    """
    for bad in ("202305101", "2023051015060708", "20231510", "20230532"):
        with pytest.raises(ValueError, match=r"unrecognized|month|day"):
            parse_date(bad)


# --- the FHIR ISO converters -----------------------------------------------


@pytest.mark.parametrize("empty", [None, "", 0])
def test_an_empty_iso_value_is_no_date_at_all(empty: object) -> None:
    assert iso_date(empty) is None
    assert iso_datetime(empty) is None


def test_a_full_iso_value_reads_the_same_with_or_without_padding() -> None:
    assert iso_date("2019-03-14") == date(2019, 3, 14)
    assert iso_date("2019-03-14", pad_partial=True) == date(2019, 3, 14)
    assert iso_datetime("2019-03-14T13:59:26Z") == datetime(2019, 3, 14, 13, 59, 26, tzinfo=UTC)
    assert iso_datetime("2019-03-14T13:59:26+05:00") == datetime(
        2019, 3, 14, 13, 59, 26, tzinfo=timezone(timedelta(hours=5))
    )


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2019", date(2019, 1, 1)),
        ("2019-03", date(2019, 3, 1)),
        ("2019-03-14T13:59:26Z", date(2019, 3, 14)),
    ],
)
def test_padding_widens_the_shapes_a_fhir_date_may_take(raw: str, expected: date) -> None:
    assert iso_date(raw, pad_partial=True) == expected


@pytest.mark.parametrize("raw", ["2019", "2019-03", "2019-03-14T13:59:26Z"])
def test_without_padding_a_partial_date_raises_rather_than_guessing(raw: str) -> None:
    with pytest.raises(ValueError):
        iso_date(raw)


def test_padding_widens_a_dateless_timestamp_to_midnight() -> None:
    assert iso_datetime("2019", pad_partial=True) == datetime(2019, 1, 1)
    assert iso_datetime("2019-03-14") == datetime(2019, 3, 14)


@pytest.mark.parametrize("raw", ["nonsense", "2019-13-45", "14/03/2019"])
def test_an_unreadable_iso_value_raises_in_both_modes(raw: str) -> None:
    for pad in (False, True):
        with pytest.raises(ValueError):
            iso_date(raw, pad_partial=pad)
        with pytest.raises(ValueError):
            iso_datetime(raw, pad_partial=pad)
