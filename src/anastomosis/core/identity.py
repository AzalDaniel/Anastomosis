"""Boundary-anchored identity matching — the ONE wrong-match defense.

Raw substring matching is a proven false-PASS factory for patient identity: a
missing heart rate of ``"98"`` hides inside a DOB ``"…1980"``, ``"4"`` inside
``"Room 4B"``, a short name inside a longer one (``"Ann Li"`` inside
``"Joann Liang"``), an unpadded date inside a different date (``"1/2/1990"``
inside ``"11/2/1990"``). Every place this toolkit asks "is this patient's name
/ DOB / value actually on the page" MUST anchor the match on word/number
boundaries, or a wrong patient can pass.

This module is that single home. It was extracted from the QA integrity check
(:mod:`anastomosis.qa.checks`), whose ``_present`` already had the correct
lookaround shape, so the QA check, the L2/L3/L6 delivery verifier
(:mod:`anastomosis.deliver.verify.levels`), and the browser destination pack
(:mod:`anastomosis.destinations.browserpack`) all match through ONE predicate
and cannot drift into a substring-loose variant again.

Three helpers, one primitive:

* :func:`token_present` — the primitive: case-sensitive, boundary-anchored
  presence of a single needle. This is the QA integrity check's historical
  ``_present`` byte-for-byte, so promoting it here keeps that check's behavior
  unchanged.
* :func:`name_present` — every whitespace-separated part of an expected name
  must appear as a standalone token (case-insensitive). A short name embedded in
  a longer one therefore fails.
* :func:`date_token_present` — a single rendered date must appear as a
  standalone token (case-insensitive, whitespace-normalized), so an unpadded
  date cannot match inside a longer digit run.

PHI rule: these functions receive patient-derived values (names, DOBs) to do
their job but never log — a caller logs the boolean/count, never the value.
"""

from __future__ import annotations

import re

__all__ = ["date_token_present", "name_present", "normalize", "token_present"]


def normalize(text: str) -> str:
    """Lowercase and collapse every whitespace run to a single space.

    The one text-normalization the identity matchers share (PDF text extraction
    yields irregular whitespace; case varies by pack). Kept here so the delivery
    verifier's fuzzy windowing and these boundary matchers cannot disagree about
    what "the same text" means.
    """
    return " ".join(text.split()).lower()


def token_present(needle: str, haystack: str) -> bool:
    """Boundary-anchored, case-sensitive presence: ``needle`` must stand alone.

    The lookarounds reject a match embedded in adjacent word characters or a
    number run — ``(?<![\\w.])`` before and ``(?![\\w.])`` after — so a name
    inside a longer name, or an unpadded date inside a different date, does NOT
    match. This is the exact predicate the QA ``DataIntegrityCheck`` relies on;
    callers that need case-insensitivity normalize their inputs first (see
    :func:`name_present`, :func:`date_token_present`).
    """
    return re.search(rf"(?<![\w.]){re.escape(needle)}(?![\w.])", haystack) is not None


def name_present(expected_name: str, haystack: str) -> bool:
    """Every part of ``expected_name`` appears in ``haystack`` as a whole token.

    Case-insensitive, whitespace-tolerant. ``expected_name`` is split on
    whitespace and each part must be :func:`token_present` in the haystack, so
    ``"Ann Li"`` does NOT match ``"Joann Liang"`` (neither ``"Ann"`` nor
    ``"Li"`` stands alone there). An empty name matches nothing (returns
    ``False``) — the identity check must not pass on the absence of a name.
    """
    parts = [part for part in expected_name.split() if part]
    if not parts:
        return False
    hay = haystack.lower()
    return all(token_present(part.lower(), hay) for part in parts)


def date_token_present(rendering: str, haystack: str) -> bool:
    """A single rendered date stands alone in ``haystack`` (boundary-anchored).

    Case-insensitive and whitespace-normalized (month-name spellings carry
    letters and spaces; numeric spellings do not). Digit-boundary anchored via
    :func:`token_present`, so ``"1/2/1990"`` does not match inside
    ``"11/2/1990"`` — the unpadded-DOB-inside-a-different-date collision.
    Callers enumerate the accepted spellings (see
    :func:`anastomosis.core.timeutil.all_date_spellings`) and require at least
    one present.
    """
    return token_present(normalize(rendering), normalize(haystack))
