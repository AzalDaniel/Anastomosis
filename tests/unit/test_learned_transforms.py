"""The closed transform verb table for learned mappings.

Every verb is single-input and returns ``None`` for an empty/sentinel cell;
parsing/arity is validated up front so a bad spec fails at load, never per-row.
"""

from __future__ import annotations

from datetime import date, datetime

import pytest

from anastomosis.sources.learned.transforms import (
    TransformError,
    apply_transform,
    parse_transform,
)


def test_strip_is_default_and_cleans_sentinels() -> None:
    strip = parse_transform("strip")
    assert strip("  hi  ") == "hi"
    assert strip(r"\N") is None  # MySQL null escape
    assert strip("NULL") is None
    assert strip("") is None
    assert strip(None) is None


def test_identity_preserves_verbatim() -> None:
    assert parse_transform("identity")("  spaced  ") == "  spaced  "
    assert parse_transform("identity")(None) is None


def test_case_transforms() -> None:
    assert parse_transform("upper")("abc") == "ABC"
    assert parse_transform("lower")("ABC") == "abc"
    assert parse_transform("upper")(None) is None


def test_parse_date_multi_format_and_explicit() -> None:
    assert parse_transform("parse_date")("2019-03-14") == date(2019, 3, 14)
    assert parse_transform("parse_date")("3/14/2019") == date(2019, 3, 14)
    assert parse_transform("parse_date:%m/%d/%Y")("3/14/2019") == date(2019, 3, 14)
    assert parse_transform("parse_date")(None) is None


def test_parse_datetime_is_tz_aware() -> None:
    result = parse_transform("parse_datetime")("2019-03-14T13:59:26Z")
    assert isinstance(result, datetime)
    assert result.tzinfo is not None


def test_phone_and_numeric() -> None:
    assert parse_transform("phone")("5555550100") == "(555) 555-0100"
    assert parse_transform("numeric")("-1") is None  # the not-set sentinel
    assert parse_transform("numeric")("42") == "42"


def test_split_takes_an_index_and_tolerates_overrun() -> None:
    take_last = parse_transform("split:,:1")
    assert take_last("Doe,John") == "John"
    assert parse_transform("split:,:0")("Doe,John") == "Doe"
    assert parse_transform("split:,:5")("Doe,John") is None  # index past the end


def test_const_ignores_input() -> None:
    always = parse_transform("const:Active")
    assert always("anything") == "Active"
    assert always(None) == "Active"


def test_unknown_verb_and_bad_arity_raise_at_parse() -> None:
    with pytest.raises(TransformError):
        parse_transform("teleport")
    with pytest.raises(TransformError):
        parse_transform("split")  # needs 2 args
    with pytest.raises(TransformError):
        parse_transform("split:,")  # only 1 arg
    with pytest.raises(TransformError):
        parse_transform("strip:nope")  # takes no args
    with pytest.raises(TransformError):
        parse_transform("split:,:notanint")  # non-integer index


def test_parse_datetime_explicit_format_with_colons() -> None:
    # A time format contains colons; arity-aware arg splitting must keep it whole.
    result = parse_transform("parse_datetime:%Y-%m-%d %H:%M")("2019-03-14 13:59")
    assert isinstance(result, datetime)
    assert (result.year, result.hour, result.minute) == (2019, 13, 59)


def test_apply_transform_convenience() -> None:
    assert apply_transform("upper", "x") == "X"
