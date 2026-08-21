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
  treat the intra-name joiners ``-``, ``'``, and the curly apostrophe
  (U+2019) as word characters, so ``"Ann"`` does not stand alone in
  ``"Mary-Ann"``, nor ``"Brien"`` in ``"O'Brien"`` — the punctuated-compound
  form of the short-name-inside-a-longer-name collision.

Known conservative limitation: scripts written without separators (CJK and
other ideographic text) offer no boundary to anchor on, so an expected name
part rendered flush against other ideographs (``"李"`` against ``"李明"``)
does NOT match. The failure direction is safe — the patient reads as not
found / mismatched and the run stops loudly; a chart is never filed on a
boundary-free guess. Tracked in ``docs/PLAN.md`` (Open work).

PHI rule: these functions receive patient-derived values (names, DOBs) to do
their job but never log — a caller logs the boolean/count, never the value.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

__all__ = [
    "date_token_present",
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


def token_present(needle: str, haystack: str) -> bool:
    """Boundary-anchored, case-sensitive presence: ``needle`` must stand alone.

    The lookarounds reject a match embedded in adjacent word characters or
    joined through a ``.`` to more word characters: ``(?<![\\w.])`` before, and
    after the needle neither a word character nor a ``.`` that continues into
    one — so ``"98"`` does not match inside ``"98.6"`` or ``"1980"``, and
    ``"1/2/1990"`` does not match inside ``"11/2/1990"`` or ``"1/2/1990.pdf"``.
    A trailing *bare* period (sentence end: ``"seen Ann Li."``,
    ``"DOB: 1/2/1990."``) is not an embedding and matches — a cosmetic period
    must not read as a different identity. An empty needle matches nothing
    (fail closed). Callers that need case-insensitivity normalize their inputs
    first (see :func:`name_fragment_present`, :func:`date_token_present`).
    """
    if not needle:
        return False
    return re.search(rf"(?<![\w.]){re.escape(needle)}(?!\w|\.\w)", haystack) is not None


# Characters that join fragments into ONE name: a match must not cross them.
# ``"Ann"`` is embedded in ``"Mary-Ann"`` exactly as it is in ``"Joann"``.
# The last entry is the curly apostrophe (U+2019) many EHR UIs render for
# a typed ' (the re module resolves the escape inside the pattern).
_NAME_BOUNDARY = r"[\w.\-'\u2019]"


def name_fragment_present(fragment: str, haystack: str) -> bool:
    """One contiguous name fragment stands alone in ``haystack``.

    Case-insensitive and whitespace-normalized on both sides, so a multi-word
    fragment (a compound family name: ``"De La Cruz"``) must appear as that
    contiguous phrase. Boundaries are the name-joiner-aware class: adjacent
    word characters AND the intra-name joiners (``-``, ``'``, the curly
    apostrophe) reject the match, so ``"Ann Li"`` matches in neither ``"Joann Liang"`` nor
    ``"Mary-Ann Li-Wong"`` nor ``"Ann-Marie Li"``. A trailing bare period
    still matches (``"Patient: Ann Li."``). Empty fragments match nothing.
    """
    cleaned = normalize(fragment)
    if not cleaned:
        return False
    hay = normalize(haystack)
    pattern = rf"(?<!{_NAME_BOUNDARY}){re.escape(cleaned)}(?![\w\-'\u2019]|\.\w)"
    return re.search(pattern, hay) is not None


def name_parts_present(parts: Iterable[str], haystack: str) -> bool:
    """Every declared name part appears in ``haystack`` as a contiguous fragment.

    ``parts`` are the record's own name fields (family name, given name), each
    matched as ONE phrase via :func:`name_fragment_present` — a multi-word
    family name must appear contiguously, never re-split and satisfied word-by-
    word across the page (which would let a reordered compound surname pass).
    Parts themselves are order-independent (``"Li, Ann"`` matches family
    ``"Li"`` + given ``"Ann"``). No parts at all is a fail-closed ``False`` —
    an identity check must not pass on the absence of a name.
    """
    cleaned = [part for part in parts if part and part.strip()]
    if not cleaned:
        return False
    return all(name_fragment_present(part, haystack) for part in cleaned)


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
