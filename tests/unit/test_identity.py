"""Tests for the shared boundary-anchored identity predicate.

The wrong-match defense lives in ONE place (:mod:`anastomosis.core.identity`)
and is reused by the QA integrity check, the L2/L3/L6 delivery verifier, and the
browser destination pack. These tests pin the boundary-anchored behavior the
whole cluster depends on: a short name embedded in a longer one (space-joined
OR hyphen/apostrophe-joined), an unpadded date embedded in a longer date, a
"Last, First" reorder, and a hyphen/space swap must all be rejected — while a
cosmetic sentence period after a legitimate value must NOT read as a different
identity. Synthetic values only.
"""

from __future__ import annotations

import pytest

from anastomosis.core.identity import (
    date_token_present,
    name_fragment_present,
    name_parts_present,
    name_present,
    token_present,
)

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


@pytest.mark.parametrize(
    ("needle", "haystack", "present"),
    [
        # The '.' in the lookarounds is load-bearing: a bare integer must not
        # match inside a decimal (the historical "98" false-PASS inside "98.6").
        ("98", "Temp 98.6 F", False),
        ("120", "BP 120.5/80 today", False),
        ("98", "Temp was 98 F", True),
        # A trailing BARE period is a sentence end, not an embedding: a
        # cosmetic period after a legitimate value must not read as a
        # different identity (it aborted whole runs at the banner check).
        ("98", "Temp was 98.", True),
        ("1/2/1990", "DOB: 1/2/1990.", True),
        # ...but a '.' that JOINS into more word characters is an embedding
        # in both directions (filenames, dotted usernames, decimals).
        ("1/2/1990", "see 1/2/1990.pdf export", False),
        ("Li", "see Ann.Li.Smith for details", False),
        # The LOOKBEHIND '.' is load-bearing too: the fraction digit after a
        # decimal point must not read as a standalone value.
        ("6", "Temp 98.6 F", False),
        # A two-dot run or an ellipsis is TRUNCATION, not a sentence end: the
        # full value is unknown, so it must reject.
        ("1/2/1990", "DOB 1/2/1990... more", False),
        ("1/2/1990", "DOB 1/2/1990\u2026", False),
    ],
)
def test_token_present_period_boundary_is_asymmetric(
    needle: str, haystack: str, present: bool
) -> None:
    assert token_present(needle, haystack) is present


def test_token_present_empty_needle_fails_closed() -> None:
    # A shared primitive must not fail OPEN on a blank value.
    assert token_present("", "any, text.") is False
    assert date_token_present("", "DOB 01/02/1990, MRN 555001") is False


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


@pytest.mark.parametrize(
    ("expected", "haystack", "present"),
    [
        # Intra-name joiners (-, ', curly apostrophe) count as embedding: the
        # punctuated compound is the dominant real-world form of the
        # short-name-inside-a-longer-name collision.
        ("Ann Li", "Mary-Ann Li-Wong  01/02/1990", False),
        ("Ann Li", "Ann-Marie Li  01/02/1990", False),
        ("Brien Sam", "O'Brien, Sam 01/02/1990", False),
        ("Brien Sam", "O\u2019Brien, Sam 01/02/1990", False),  # curly apostrophe (U+2019)
        # The legitimate punctuated names themselves still match.
        ("O'Brien Sam", "O'Brien, Sam 01/02/1990", True),
        ("Ann-Marie Li", "Li, Ann-Marie  MRN 555002", True),
        # A cosmetic sentence period after the name must not reject it.
        ("Ann Li", "Patient: Ann Li.", True),
        ("Li Ann", "Patient: Ann Li.", True),  # parts are order-independent
        # PDF extraction and EHR DOMs render compound-name hyphens as ANY of
        # the Unicode hyphen/dash family — each must reject like ASCII '-'.
        ("Ann Li", "Mary\u2010Ann Li\u2010Wong  01/02/1990", False),  # U+2010 hyphen
        ("Ann Li", "Mary\u2011Ann Li\u2011Wong  01/02/1990", False),  # non-breaking hyphen
        ("Ann Li", "Mary\u2013Ann Li\u2013Wong  01/02/1990", False),  # en dash
        ("Ann Li", "Mary\u00adAnn Li\u00adWong  01/02/1990", False),  # soft hyphen
        ("Brien Sam", "O\u2018Brien, Sam 01/02/1990", False),  # left single quote
        # The possessive is the SAME patient; a punctuation hyphen (no word
        # character after it) is a separator, not a joiner. Neither may
        # false-reject — at the banner that aborts the whole run.
        ("Ann Li", "Ann Li's Chart  DOB 01/02/1990", True),
        ("Ann Li", "Ann Li\u2019s Chart  DOB 01/02/1990", True),
        ("Ann Li", "Ann Li- DOB 01/02/1990", True),
        # A truncated cell has an UNKNOWN identity — reject, never guess.
        ("Ann Li", "Ann Li... (truncated cell)", False),
        ("Ann Li", "Ann Li\u2026 (truncated cell)", False),
    ],
)
def test_name_present_treats_name_joiners_as_embedding(
    expected: str, haystack: str, present: bool
) -> None:
    assert name_present(expected, haystack) is present


# --- name_parts_present: each declared FIELD is one contiguous fragment ---


@pytest.mark.parametrize(
    ("parts", "haystack", "present"),
    [
        # A multi-word family name must appear contiguously — word-by-word
        # satisfaction across the row would let a reordered compound surname
        # (a DIFFERENT patient) pass.
        (["Dela Testfamily", "Testgiven"], "Testfamily, Testgiven Dela Other", False),
        (["Dela Testfamily", "Testgiven"], "Dela Testfamily, Testgiven  MRN 555003", True),
        (["Li", "Ann"], "Li, Ann  MRN 555004", True),  # Last, First across fields
        (["Li", "Ann"], "Joann Liang  MRN 555005", False),
        ([], "any row text", False),  # no declared parts: fail closed
        (["", "  "], "any row text", False),
    ],
)
def test_name_parts_present_requires_contiguous_fields(
    parts: list[str], haystack: str, present: bool
) -> None:
    assert name_parts_present(parts, haystack) is present


def test_name_fragment_present_normalizes_case_and_whitespace() -> None:
    assert name_fragment_present("De  La Cruz", "maria de la cruz gomez") is True
    assert name_fragment_present("De La Cruz", "maria cruz, de la") is False


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
