"""Tests for the shared boundary-anchored identity predicate.

The wrong-match defense lives in ONE place (:mod:`anastomosis.core.identity`)
and is reused by the QA integrity check, the L2/L3/L6 delivery verifier, and the
browser destination pack. These tests pin the boundary-anchored behavior the
whole cluster depends on: a short name embedded in a longer one, an unpadded
date embedded in a longer date, a "Last, First" reorder, and a hyphen/space
swap must all be rejected. Synthetic values only.
"""

from __future__ import annotations

import pytest

from anastomosis.core.identity import date_token_present, name_present, token_present

# --- token_present: the case-sensitive boundary primitive (the QA _present) ---


@pytest.mark.parametrize(
    ("needle", "haystack", "present"),
    [
        ("Ann Li", "The patient Ann Li was seen", True),  # stands alone
        ("Ann Li", "Joann Liang was seen", False),  # embedded in a longer name
        ("72", "Heart rate 72 bpm", True),
        ("72", "ID 9872X", False),  # embedded in a longer number
        ("72", "seen in year 1972", False),  # hides inside a longer year
        ("1/2/1990", "seen 1/2/1990 today", True),
        ("1/2/1990", "seen 11/2/1990 today", False),  # unpadded date inside a longer date
        ("Synthia Probe", "MarySynthia Probeworth", False),  # both ends embedded
    ],
)
def test_token_present_is_boundary_anchored(needle: str, haystack: str, present: bool) -> None:
    assert token_present(needle, haystack) is present


def test_token_present_is_case_sensitive() -> None:
    # The QA integrity check matches case-sensitively; this primitive preserves that.
    assert token_present("Synthia", "Synthia Testpatient") is True
    assert token_present("synthia", "Synthia Testpatient") is False


# --- name_present: every part a whole token, case-insensitive ---


@pytest.mark.parametrize(
    ("expected", "haystack", "present"),
    [
        ("Ann Li", "Ann Li reports well", True),
        ("Ann Li", "ANN LI reports well", True),  # case-insensitive
        ("Ann Li", "Joann Liang reports well", False),  # short parts embedded in longer
        ("Testpatient Synthia", "Testpatient, Synthia  MRN 555001", True),  # Last, First row
        ("Synthia Testpatient", "Cynthia Testpatient", False),  # sound-alike given fails a part
        ("Mary-Jane Doe", "Mary Jane Doe", False),  # hyphen part not present as a space token
    ],
)
def test_name_present_requires_each_part_as_whole_token(
    expected: str, haystack: str, present: bool
) -> None:
    assert name_present(expected, haystack) is present


def test_name_present_empty_name_matches_nothing() -> None:
    # An identity check must never pass on the absence of a name.
    assert name_present("", "any page text") is False
    assert name_present("   ", "any page text") is False


# --- date_token_present: a rendered date, digit-boundary anchored ---


@pytest.mark.parametrize(
    ("rendering", "haystack", "present"),
    [
        ("1/2/1990", "DOB 1/2/1990 on file", True),
        ("1/2/1990", "DOB 11/2/1990 on file", False),  # the unpadded-in-longer collision
        ("01/02/1990", "DOB 01/02/1990 on file", True),
        ("01/02/1990", "DOB 101/02/1990 on file", False),
        ("January 2, 1990", "born january 2, 1990 here", True),  # case + whitespace tolerant
        ("January 2, 1990", "born  January   2, 1990", True),  # collapsed whitespace
    ],
)
def test_date_token_present_is_boundary_anchored(
    rendering: str, haystack: str, present: bool
) -> None:
    assert date_token_present(rendering, haystack) is present
