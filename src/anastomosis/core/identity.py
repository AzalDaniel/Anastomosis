"""Boundary-anchored identity matching — the ONE wrong-match defense.

Raw substring matching is a proven false-PASS factory for patient identity: a
missing heart rate of ``"98"`` hides inside a DOB ``"…1980"``, ``"4"`` inside
``"Room 4B"``, a short name inside a longer one (``"Ann Li"`` inside
``"Joann Liang"`` or ``"Mary-Ann Li-Wong"``), an unpadded date inside a
different date (``"1/2/1990"`` inside ``"11/2/1990"``). Every place this
toolkit asks "is this patient's name / DOB / value actually on the page" MUST
anchor the match on word/number boundaries, or a wrong patient can pass.

This module is that single home. It was extracted from the QA integrity check
(:mod:`anastomosis.qa.checks`), whose ``_present`` had the correct lookaround
shape, so the QA check, the L2/L3/L6 delivery verifier
(:mod:`anastomosis.deliver.verify.levels`), and the browser destination pack
(:mod:`anastomosis.destinations.browserpack`) all match through ONE predicate
family and cannot drift into a substring-loose variant again.

Two boundary definitions, deliberately distinct:

* **Value boundaries** (:func:`token_present`) — a quantity or date must not
  sit inside a word-character run, and not on either side of a ``.`` that
  joins it to more word characters: ``"98"`` must not match inside ``"98.6"``
  or ``"1980"``. A trailing bare period (``"1/2/1990."`` at sentence end) is
  NOT an embedding — the value stands alone there.
* **Name boundaries** (:func:`name_fragment_present`) — names additionally
  treat the intra-name joiners (the whole Unicode hyphen/dash family and all
  three apostrophes — see ``_NAME_HYPHENS`` / ``_NAME_APOSTROPHES``) as word
  characters on the lookbehind side, so ``"Ann"`` does not stand alone in
  ``"Mary-Ann"`` (any hyphen codepoint), nor ``"Brien"`` in ``"O'Brien"`` —
  the punctuated-compound form of the short-name-inside-a-longer-name
  collision. The trailing side is narrower: a hyphen rejects only when it
  joins into a word character, a possessive (``"Ann Li's Chart"``) matches,
  and truncation (``"Ann Li..."``) rejects — see ``_NAME_TRAILING``.

Unseparated scripts (Han, kana, Hangul) write a full name flush — family and
given concatenated with no space — so the record's separate name parts can
never each "stand alone" the way spaced scripts do. For parts that are wholly
ideographic, :func:`name_parts_present` therefore also accepts the JOINED
name (both part orders) as one boundary-anchored fragment: ``"李" + "明"``
matches ``"姓名: 李明"``. The boundaries themselves never loosen — ``"李明"``
embedded in ``"李明华"`` (a longer name: a different patient) or flush inside
running prose (``"患者李明的记录"``) still refuses, because an adjacent
ideograph is indistinguishable from more-of-the-name. That residual refusal
is the safe direction: the patient reads as not found and the run stops
loudly; a chart is never filed on a boundary-free guess.

PHI rule: these functions receive patient-derived values (names, DOBs) to do
their job but never log — a caller logs the boolean/count, never the value.
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
    """Lowercase and collapse every whitespace run to a single space.

    The one text-normalization the identity matchers share (PDF text extraction
    yields irregular whitespace; case varies by pack). Kept here so the delivery
    verifier's fuzzy windowing and these boundary matchers cannot disagree about
    what "the same text" means.
    """
    return " ".join(text.split()).lower()


# Truncation on the trailing side is NOT a boundary: a server-clipped cell
# ("Ann Li..." — Ann Lithgow? Ann Li-Wong?) has an UNKNOWN identity, so an
# ASCII two-dot run or the ellipsis character rejects exactly like a joined
# word character. A single bare period (sentence end) still matches.
_VALUE_TRAILING = r"(?!\w|\.\w|\.\.|\u2026)"


def token_present(needle: str, haystack: str) -> bool:
    """Boundary-anchored, case-sensitive presence: ``needle`` must stand alone.

    The lookarounds reject a match embedded in adjacent word characters or
    joined through a ``.`` to more word characters: ``(?<![\\w.])`` before, and
    after the needle neither a word character nor a ``.`` that continues into
    one — so ``"98"`` does not match inside ``"98.6"`` or ``"1980"``, and
    ``"1/2/1990"`` does not match inside ``"11/2/1990"`` or ``"1/2/1990.pdf"``.
    A trailing *bare* period (sentence end: ``"seen Ann Li."``,
    ``"DOB: 1/2/1990."``) is not an embedding and matches — a cosmetic period
    must not read as a different identity — but a two-dot run or an ellipsis
    is TRUNCATION (``"DOB 1/2/1990..."``): the full value is unknown, so it
    rejects. An empty needle matches nothing (fail closed). Callers that need
    case-insensitivity normalize their inputs first (see
    :func:`name_fragment_present`, :func:`date_token_present`).
    """
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
    """One contiguous name fragment stands alone in ``haystack``.

    Case-insensitive and whitespace-normalized on both sides, so a multi-word
    fragment (a compound family name: ``"De La Cruz"``) must appear as that
    contiguous phrase. Boundaries are the name-joiner-aware classes
    (``_NAME_BOUNDARY`` behind, ``_NAME_TRAILING`` ahead): ``"Ann Li"``
    matches in neither ``"Joann Liang"`` nor ``"Mary-Ann Li-Wong"`` (any
    hyphen codepoint) nor ``"Ann-Marie Li"``, while the possessive
    (``"Ann Li's Chart"``), a punctuation hyphen (``"Ann Li- DOB"``), and a
    bare sentence period (``"Patient: Ann Li."``) all still match, and a
    truncated cell (``"Ann Li..."``) rejects. Empty fragments match nothing.
    """
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
    """The flush-concatenated forms an unseparated-script name renders as.

    Both part orders (family-given and given-family), because the record does
    not say which order the destination renders. Only the two declared orders —
    never every permutation, which would loosen the contiguity guarantee for
    three-part names. Empty when any part carries a non-ideographic character.
    """
    stripped = [part.strip() for part in parts]
    if len(stripped) < 2 or not all(_UNSEPARATED_SCRIPT.match(part) for part in stripped):
        return []
    joined = "".join(stripped)
    reverse = "".join(reversed(stripped))
    return [joined] if joined == reverse else [joined, reverse]


def name_parts_present(parts: Iterable[str], haystack: str) -> bool:
    """Every declared name part appears in ``haystack`` as a contiguous fragment.

    ``parts`` are the record's own name fields (family name, given name), each
    matched as ONE phrase via :func:`name_fragment_present` — a multi-word
    family name must appear contiguously, never re-split and satisfied word-by-
    word across the page (which would let a reordered compound surname pass).
    Parts themselves are order-independent (``"Li, Ann"`` matches family
    ``"Li"`` + given ``"Ann"``). No parts at all is a fail-closed ``False`` —
    an identity check must not pass on the absence of a name.

    Wholly-ideographic parts get one extra chance: the flush-joined name in
    either part order, matched as ONE fragment under the same boundaries
    (see :func:`_joined_name_candidates`) — ``["李", "明"]`` matches
    ``"姓名: 李明"``, where neither part can stand alone because the other is
    its immediate neighbour. ``"李明"`` inside ``"李明华"`` or flush inside
    prose still refuses: an adjacent ideograph may be more of the name.
    """
    cleaned = [part for part in parts if part and part.strip()]
    if not cleaned:
        return False
    if all(name_fragment_present(part, haystack) for part in cleaned):
        return True
    return any(
        name_fragment_present(candidate, haystack) for candidate in _joined_name_candidates(cleaned)
    )


def name_present(expected_name: str, haystack: str) -> bool:
    """Every whitespace-separated word of ``expected_name`` stands alone.

    The single-string convenience over :func:`name_parts_present`: the name is
    split on whitespace and each word must be a standalone fragment, so
    ``"Ann Li"`` does NOT match ``"Joann Liang"`` (embedded), ``"Mary-Ann
    Li-Wong"`` (joined through hyphens), or ``"O'Brien"``-style apostrophe
    compounds. Callers that know the record's field structure should prefer
    :func:`name_parts_present`, which keeps multi-word fields contiguous.
    """
    return name_parts_present(expected_name.split(), haystack)


def date_token_spans(rendering: str, haystack: str) -> list[tuple[int, int]]:
    """WHERE a rendered date stands alone in ``haystack``, not merely whether.

    Same normalization and same boundaries as :func:`date_token_present` — it
    is the counting form of that predicate, kept beside it so the two can never
    disagree about what "the date is here" means. Spans index the NORMALIZED
    haystack, so they are useful for counting and de-duplicating occurrences
    across spellings, not for slicing the caller's original text.

    A caller that needs "is it here at all" wants the predicate; a caller that
    needs "is it here MORE times than the layout admits to" wants this. That
    second question is what lets a pack stamp the render day on purpose without
    the staleness check going blind to a template that stamps it by accident.
    """
    needle = normalize(rendering)
    if not needle:
        return []
    pattern = rf"(?<![\w.]){re.escape(needle)}{_VALUE_TRAILING}"
    return [match.span() for match in re.finditer(pattern, normalize(haystack))]


def date_token_present(rendering: str, haystack: str) -> bool:
    """A single rendered date stands alone in ``haystack`` (boundary-anchored).

    Case-insensitive and whitespace-normalized (month-name spellings carry
    letters and spaces; numeric spellings do not). Digit-boundary anchored via
    :func:`token_present`, so ``"1/2/1990"`` does not match inside
    ``"11/2/1990"`` — the unpadded-DOB-inside-a-different-date collision — but
    a sentence-final ``"1/2/1990."`` does match. Callers enumerate the accepted
    spellings (see :func:`anastomosis.core.timeutil.all_date_spellings`) and
    require at least one present.
    """
    return token_present(normalize(rendering), normalize(haystack))
