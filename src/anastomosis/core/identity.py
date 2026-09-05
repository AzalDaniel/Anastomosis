"""Boundary-anchored identity matching, the one wrong-match defense (6);
the sole implementation, so the QA check, the delivery verifier, and the
browser destination pack cannot drift into a substring-loose variant.

PHI (2): these functions receive patient-derived values to do their job
but never log — a caller logs the boolean/count, never the value.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

__all__ = [
    "date_token_present",
    "date_token_spans",
    "name_fragment_present",
    "name_parts_present",
    "name_present",
    "normalize",
    "token_present",
]


def normalize(text: str) -> str:
    """Lowercase and collapse whitespace runs: the one text-normalization
    the identity matchers share, so the delivery verifier's fuzzy windowing
    and these boundary matchers cannot disagree about "the same text"."""
    return " ".join(text.split()).lower()


# Truncation on the trailing side is NOT a boundary: a server-clipped cell
# ("Ann Li..." — Ann Lithgow? Ann Li-Wong?) has an UNKNOWN identity, so an
# ASCII two-dot run or the ellipsis character rejects exactly like a joined
# word character. A single bare period (sentence end) still matches.
_VALUE_TRAILING = r"(?!\w|\.\w|\.\.|\u2026)"


def token_present(needle: str, haystack: str) -> bool:
    """Boundary-anchored, case-sensitive presence (6): ``needle`` must stand
    alone, not joined through word characters or a ``.`` (``"98"`` not
    inside ``"98.6"``/``"1980"``). A trailing bare period matches; a
    two-dot run or ellipsis (truncation) rejects. Empty needle fails
    closed; callers needing case-insensitivity normalize first."""
    if not needle:
        return False
    return re.search(rf"(?<![\w.]){re.escape(needle)}{_VALUE_TRAILING}", haystack) is not None


# Characters that join fragments into ONE name: a match must not cross them.
# ``"Ann"`` is embedded in ``"Mary-Ann"`` exactly as it is in ``"Joann"`` —
# and PDF text extraction and EHR DOM text render the hyphen as any of the
# Unicode hyphen/dash family, so the whole family is listed: U+2010 hyphen,
# U+2011 non-breaking hyphen, U+2012 figure dash, U+2013 en dash, U+2014 em
# dash, U+2015 horizontal bar, U+2212 minus, U+00AD soft hyphen. Apostrophes
# likewise: ASCII ' plus the curly U+2018/U+2019 pair. (The re module
# resolves the escapes inside the pattern.)
_NAME_HYPHENS = r"\-\u2010\u2011\u2012\u2013\u2014\u2015\u2212\u00ad"
_NAME_APOSTROPHES = r"'\u2018\u2019"
_NAME_BOUNDARY = rf"[\w.{_NAME_HYPHENS}{_NAME_APOSTROPHES}]"

# The trailing side is narrower than the lookbehind, deliberately:
# * a hyphen/dash rejects only when it JOINS into a word character
#   ("Li-Wong" embeds; "Ann Li- DOB" is punctuation and matches);
# * an apostrophe never rejects on the trailing side — the possessive
#   ("Ann Li's Chart") is the same patient, and the O'Brien-style embedding
#   is caught by the LOOKBEHIND on the next fragment;
# * truncation (.. / ellipsis) rejects as in the value rule.
_NAME_TRAILING = rf"(?!\w|\.\w|\.\.|\u2026|[{_NAME_HYPHENS}]\w)"


def name_fragment_present(fragment: str, haystack: str) -> bool:
    """One contiguous name fragment stands alone in ``haystack`` (6):
    case-insensitive, whitespace-normalized, a multi-word fragment matched
    as one phrase. ``"Ann Li"`` does not match in ``"Joann Liang"`` or
    ``"Mary-Ann Li-Wong"`` (any hyphen codepoint), but a possessive or a
    bare sentence period still matches. Empty fragments match nothing."""
    cleaned = normalize(fragment)
    if not cleaned:
        return False
    hay = normalize(haystack)
    pattern = rf"(?<!{_NAME_BOUNDARY}){re.escape(cleaned)}{_NAME_TRAILING}"
    return re.search(pattern, hay) is not None


# Scripts whose names render with no separator between family and given parts:
# Han (URO + extension A + compatibility + supplementary planes), the two kana
# blocks, and Hangul syllables. A part must consist WHOLLY of these for the
# joined-name fallback below to apply — one Latin character makes it a spaced-
# script name that must satisfy the part-wise rule.
_UNSEPARATED_SCRIPT = re.compile(
    r"^["
    r"\u3040-\u30ff"  # hiragana + katakana
    r"\u3400-\u4dbf"  # CJK unified ideographs extension A
    r"\u4e00-\u9fff"  # CJK unified ideographs
    r"\uf900-\ufaff"  # CJK compatibility ideographs
    r"\uac00-\ud7a3"  # Hangul syllables
    r"\U00020000-\U0002ffff"  # CJK unified ideographs extensions B and beyond
    r"]+$"
)


def _joined_name_candidates(parts: list[str]) -> list[str]:
    """The flush-concatenated forms an unseparated-script name renders as:
    both part orders (the record does not say which the destination uses),
    never every permutation of 3+ parts, which would loosen contiguity.
    Empty when any part carries a non-ideographic character."""
    stripped = [part.strip() for part in parts]
    if len(stripped) < 2 or not all(_UNSEPARATED_SCRIPT.match(part) for part in stripped):
        return []
    joined = "".join(stripped)
    reverse = "".join(reversed(stripped))
    return [joined] if joined == reverse else [joined, reverse]


def name_parts_present(parts: Iterable[str], haystack: str) -> bool:
    """Every declared name part appears in ``haystack`` as its own
    contiguous fragment (6), never re-split word-by-word. No parts fails
    closed. Wholly-ideographic parts get one extra chance: the flush-joined
    name in either part order — ``["李", "明"]`` matches ``"姓名: 李明"`` —
    but an adjacent ideograph (``"李明华"``) still refuses."""
    cleaned = [part for part in parts if part and part.strip()]
    if not cleaned:
        return False
    if all(name_fragment_present(part, haystack) for part in cleaned):
        return True
    return any(
        name_fragment_present(candidate, haystack) for candidate in _joined_name_candidates(cleaned)
    )


def name_present(expected_name: str, haystack: str) -> bool:
    """The single-string convenience over :func:`name_parts_present`: each
    whitespace-split word of ``expected_name`` must stand alone. Callers
    that know the record's field structure should prefer
    :func:`name_parts_present`, which keeps multi-word fields contiguous."""
    return name_parts_present(expected_name.split(), haystack)


def date_token_spans(rendering: str, haystack: str) -> list[tuple[int, int]]:
    """WHERE a rendered date stands alone, not merely whether — the
    counting form of :func:`date_token_present`, same boundaries, so the
    two can never disagree. Spans index the NORMALIZED haystack, useful
    for de-duplicating occurrences, never for slicing the caller's
    original text."""
    needle = normalize(rendering)
    if not needle:
        return []
    pattern = rf"(?<![\w.]){re.escape(needle)}{_VALUE_TRAILING}"
    return [match.span() for match in re.finditer(pattern, normalize(haystack))]


def date_token_present(rendering: str, haystack: str) -> bool:
    """A single rendered date stands alone in ``haystack`` (6), case-
    insensitive and whitespace-normalized, via :func:`token_present`:
    ``"1/2/1990"`` does not match inside ``"11/2/1990"``. Callers enumerate
    accepted spellings (:func:`anastomosis.core.timeutil.all_date_spellings`)
    and require at least one present."""
    return token_present(normalize(rendering), normalize(haystack))
