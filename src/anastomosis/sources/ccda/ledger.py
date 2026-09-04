"""What the XML offered, and what the record kept.

An audit put 2,103 real C-CDA documents through this adapter. Every one of them
parsed. No error was raised, no document was skipped, and the run reported
success — while eleven canonical collections came back empty across all 2,103:
not one practitioner, not one facility, not one coverage, not one document, and
not one of the 12,277 encounters carried a note section or a chief complaint.

"It parsed" had said nothing about whether the chart survived, and nothing could
say otherwise, because nothing anywhere counted what the XML OFFERED. Only the
survivors were ever counted, and a count of survivors is the one number that
looks identical whether the loss was zero or total. This module is the other
count.

It walks the document independently of :mod:`~anastomosis.sources.ccda.parser`
— deliberately, so it can see constructs the parser's own traversal never
reaches (a subsection nested one component deeper is invisible to a depth-one
XPath, and would otherwise be invisible here too) — and gives every clinically
meaningful construct exactly one disposition:

* ``structurally_parsed`` — it became a typed canonical object;
* ``narrative_preserved`` — its words are in ``patient.extensions``, but no
  typed object carries them, so nothing downstream can index, chart, reconcile
  or migrate them as data;
* ``unsupported`` — the adapter has no slot for it. Named, counted, and not
  quietly folded into the success column;
* ``source_empty`` — the document offered none. A different fact from losing
  them, and recorded as one, because a report that cannot tell "there were no
  allergies" from "the allergies did not survive" is not evidence of anything.

**Evidence, never assumption.** A construct is credited as structurally parsed
only when some canonical object's ``provenance.source_id`` names an ``<id
root>`` that construct carries. Not because its section code is in a dispatch
table, and not because the matching collection is non-empty — either would let
this instrument certify a fix that never ran. The cost is a known, one-sided
bias: a construct whose id root is shared with another construct in the same
document (a root shared by two constructs cannot say which of them an object
came from) can never be credited, and is counted in ``unlinkable`` so the
reading stays honest about its own blind spot. The bias runs toward reporting
loss that is not there, which is the direction a loss detector should err.

**A second form of evidence, only where the first is impossible.** Some
constructs cannot carry an id at all: CDA R2 gives ``Device`` and
``RelatedEntity`` no ``<id>``, so an authoring device and an informant who is a
relative were reported as lost in every document that had one, permanently and
by schema rather than by anything the adapter did. For those classes — declared
by name in :data:`ID_LESS_CONSTRUCTS`, never inferred — a parse is credited when
the record holds an object STATING what the XML states for that construct: the
same by-content argument :func:`_narrative_pool` makes, because reconstructing
the parser's mapping rules here would make the instrument a mirror, and a mirror
that drifts reports the drift as loss. The bias is the same one: content is
matched exactly, a construct that states nothing is not matched at all, and N
constructs against M matching objects credit ``min`` by multiset intersection —
each object answers for one construct and then is spent, so two identical
informants and one object credit one parse and leave the other in
``unlinkable``. The id rule is untouched everywhere else, shared-root refusal
included.

The books balance or the ledger refuses: every construct the document offers
ends in exactly one disposition, and so does every ``<entry>``, checked through
:class:`~anastomosis.core.conservation.Conservation` — the same primitive the
render, upload and delivery seams are held to, for the same reason.

PHI: counts, element names, LOINC codes and template OIDs. Nothing else. Ids
are compared and never emitted, narrative text is compared and never emitted,
the name, telephone number and relationship an id-less construct states are
compared and never emitted, and a code or template root the vocabulary does not
recognise is reported as ``nonstandard`` rather than passed through — a
document's ``@code`` is under its author's control, and a name is not a name
because we assumed it was a code. :func:`assert_emittable` enforces that at the boundary rather than
trusting it, exactly as ``tools/ccda_shape_report.py`` does with the report it
sends home.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Callable, Iterable, Iterator, Mapping
from copy import deepcopy
from dataclasses import dataclass, field
from enum import StrEnum
from functools import lru_cache
from typing import TYPE_CHECKING, Any, Generic, TypeVar

from lxml import etree

from anastomosis.core.ccda_codes import EXT_PRIOR_LOSS_NARRATIVE
from anastomosis.core.conservation import Conservation
from anastomosis.core.model import Practitioner
from anastomosis.core.model.base import AnastBase

# The parser's own helpers, imported rather than re-spelled. Each encodes
# knowledge this ledger must not hold a second copy of: `_PARSER` is the
# hardened XML posture every third-party document is read under, `_section_code`
# and `_is_own_loss_narrative` are the parser's own answers about a section,
# `_sections` is the walk that decides which sections the record can hold
# anything of at all, `_inline_narrative_references` is the one step that
# rewrites a tree and is applied here only to a copy, and `_text_content`,
# `_attr`, `_narrative_entries` and `entry_verbatim` are the exact
# normalizations whose output the parser STORED —
# a ledger that collapsed whitespace even slightly differently would compare its
# own spelling of a value against the parser's and conclude that every section,
# and every actor, had been dropped.
from .parser import (
    _PARSER,
    EXT_SECTION_ENTRIES,
    _attr,
    _find,
    _findall,
    _inline_narrative_references,
    _is_own_loss_narrative,
    _narrative_entries,
    _q,
    _section_code,
    _text_content,
    entry_verbatim,
    parse_document,
)
from .parser import (
    _sections as _parser_sections,
)

if TYPE_CHECKING:
    from pathlib import Path

    from anastomosis.core.model import PatientRecord

__all__ = [
    "ABSENT",
    "BODY_PATHS",
    "ID_LESS_CONSTRUCTS",
    "NONSTANDARD",
    "PARTICIPATION_PATHS",
    "REPORT_WORDS",
    "STAGE",
    "CorpusLedger",
    "Disposition",
    "DocumentLedger",
    "LedgerRow",
    "aggregate",
    "assert_emittable",
    "document_ledger",
    "physician_reading",
]

#: The seam these books are kept at, named as a crossing (the vocabulary
#: ``Conservation`` messages read in).
STAGE = "ccda xml -> canonical"

_Element = etree._Element


class Disposition(StrEnum):
    """What became of one construct the document offered.

    Exactly one applies to each, and the four together are exhaustive by
    construction — an instrument whose classes overlap or leave a gap can
    report a balanced ledger over a chart that lost half of itself.
    """

    STRUCTURALLY_PARSED = "structurally_parsed"
    NARRATIVE_PRESERVED = "narrative_preserved"
    UNSUPPORTED = "unsupported"
    SOURCE_EMPTY = "source_empty"


# --- the emission vocabulary -------------------------------------------------

# Deliberately a whitelist, and deliberately narrow. Every string this ledger
# can emit is either a word it chose itself or a document value that matched one
# of these; anything else becomes NONSTANDARD. A patient's name is not a LOINC
# code and not an OID, so the vocabulary itself is the control — the same
# argument tools/ccda_shape_report.py makes about its own report.
_LOINC_RE = re.compile(r"^[0-9]{1,6}-[0-9]$")
_OID_RE = re.compile(r"^[0-9]+(?:\.[0-9]+)+$")

#: A code or template root the vocabulary does not recognise. The COUNT still
#: travels (a corpus full of these is a finding); the value does not.
NONSTANDARD = "nonstandard"
#: A construct the document left without a code at all.
ABSENT = "none"

#: Construct-name prefixes. The kind is ours, the name is vetted, so a construct
#: name cannot carry a value out even if the vetting above were wrong.
_SECTION_KIND = "section"
_PARTICIPATION_KIND = "participation"
_BODY_KIND = "body"

_KINDS = frozenset({_SECTION_KIND, _PARTICIPATION_KIND, _BODY_KIND})

#: The participation elements, each with the ElementPath(s) it is counted at.
#:
#: Scope is per-element and argued, not uniform. ``author``, ``performer`` and
#: ``informant`` are counted wherever they appear — the clinician who signed a
#: note is as much a named provider as the one in the header, and the header
#: scope alone would have reported the note's author as absent rather than as
#: lost. ``participant`` is counted in the HEADER ONLY, because a nested
#: ``<participant>`` inside an allergy entry is the allergen substance, which
#: this adapter does parse; counting it here would report a fact that survived
#: as a fact that vanished. The rest occur only in the header in CDA R2.
PARTICIPATION_PATHS: Mapping[str, tuple[str, ...]] = {
    "author": (".//v3:author",),
    "assignedAuthoringDevice": (".//v3:assignedAuthoringDevice",),
    "dataEnterer": ("v3:dataEnterer",),
    "informant": (".//v3:informant",),
    "custodian": ("v3:custodian",),
    "informationRecipient": ("v3:informationRecipient",),
    "legalAuthenticator": ("v3:legalAuthenticator",),
    "authenticator": ("v3:authenticator",),
    "participant": ("v3:participant",),
    "serviceEvent": ("v3:documentationOf/v3:serviceEvent",),
    "performer": (".//v3:performer",),
    "encompassingEncounter": ("v3:componentOf/v3:encompassingEncounter",),
}

#: The document body forms. ``nonXMLBody`` is the whole clinical content of an
#: Unstructured Document — a scanned referral, a faxed discharge summary — and
#: it is a construct precisely because the adapter's section walk cannot see it:
#: such a document parses cleanly and yields a patient with no chart at all.
BODY_PATHS: Mapping[str, tuple[str, ...]] = {
    "nonXMLBody": ("v3:component/v3:nonXMLBody",),
}


#: Every bare word a report may contain: the keys it is built from, the four
#: disposition names, and the two labels a refused value collapses to. A new key
#: has to be added here, in the open, rather than riding out inside a dict nobody
#: re-read — the same bargain ``tools/ccda_shape_report.py`` strikes with its own
#: report.
REPORT_WORDS: frozenset[str] = frozenset(
    {
        "version",
        "documents",
        "constructs_offered",
        "entries_offered",
        "constructs",
        "construct",
        "templates",
        "present_in_documents",
        "offered",
        "instances",
        "entries",
        "unlinkable",
        ABSENT,
        NONSTANDARD,
        *(disposition.value for disposition in Disposition),
    }
)


def _vocabulary(value: str | None, pattern: re.Pattern[str]) -> str:
    """``value`` if the vocabulary admits it, else a label that says which way
    it failed. Absent and unrecognised are different findings."""
    if value is None:
        return ABSENT
    return value if pattern.match(value) else NONSTANDARD


def _construct(kind: str, name: str) -> str:
    return f"{kind}:{name}"


# --- rows --------------------------------------------------------------------


@dataclass(frozen=True)
class LedgerRow:
    """One construct's books: how many the document offered, and where they went.

    ``instances`` is keyed by disposition rather than carrying a single verdict,
    because a construct group is not uniform — three of a document's four
    sections may be parsed and the fourth dropped, and a row that had to pick
    one word for that would have to pick the flattering one or the alarming one.

    A key present with a count of ZERO is a deliberate statement: the ledger
    looked for this construct and the document had none. A key simply missing
    would read as "the ledger did not look", and this report exists to be
    believed about absences.
    """

    construct: str
    templates: tuple[str, ...] = ()
    instances: Mapping[Disposition, int] = field(default_factory=dict)
    entries: Mapping[Disposition, int] = field(default_factory=dict)
    #: Instances no evidence could reach: they share an id root with another
    #: construct, or they carry no id and state nothing the record states back.
    #: The ledger's own blind spot, counted rather than described.
    unlinkable: int = 0

    @property
    def offered(self) -> int:
        return sum(self.instances.values())

    @property
    def entries_offered(self) -> int:
        return sum(self.entries.values())

    def counted(self, disposition: Disposition) -> int:
        return self.instances.get(disposition, 0)


def _merge(
    left: Mapping[Disposition, int], right: Mapping[Disposition, int]
) -> dict[Disposition, int]:
    merged: dict[Disposition, int] = dict(left)
    for disposition, count in right.items():
        merged[disposition] = merged.get(disposition, 0) + count
    return merged


# --- ledgers -----------------------------------------------------------------


@dataclass(frozen=True)
class DocumentLedger:
    """One document's books, already balanced (see :meth:`check`)."""

    rows: tuple[LedgerRow, ...]
    #: Counted in its own pass over the document, BEFORE anything was
    #: classified, so a construct the row builder never produced a row for is
    #: what the conservation goes short by.
    constructs_offered: int
    entries_offered: int

    def conservation(self) -> Conservation:
        return _conservation("construct", self.constructs_offered, self.rows, _instances)

    def entry_conservation(self) -> Conservation:
        return _conservation("entry", self.entries_offered, self.rows, _entries)

    def check(self) -> None:
        self.conservation().check()
        self.entry_conservation().check()


@dataclass(frozen=True)
class CorpusLedger:
    """Many documents' books, merged construct by construct.

    ``present_in`` counts the DOCUMENTS that offered a construct at all, which
    is the number that separates "this corpus has no advance directives" from
    "this adapter drops advance directives". Without it a zero in the parsed
    column is unreadable.
    """

    documents: int
    rows: tuple[LedgerRow, ...]
    present_in: Mapping[str, int]
    constructs_offered: int
    entries_offered: int

    def conservation(self) -> Conservation:
        return _conservation("construct", self.constructs_offered, self.rows, _instances)

    def entry_conservation(self) -> Conservation:
        return _conservation("entry", self.entries_offered, self.rows, _entries)

    def check(self) -> None:
        self.conservation().check()
        self.entry_conservation().check()

    def as_report(self) -> dict[str, Any]:
        """The corpus reading as plain data, safe to write out or paste into an
        issue. :func:`assert_emittable` is applied here, not left to the
        caller — a report is exactly the thing that travels."""
        report: dict[str, Any] = {
            "version": 1,
            "documents": self.documents,
            "constructs_offered": self.constructs_offered,
            "entries_offered": self.entries_offered,
            "constructs": [_row_report(row, self.present_in) for row in self.rows],
        }
        assert_emittable(report)
        return report


def _instances(row: LedgerRow) -> Mapping[Disposition, int]:
    return row.instances


def _entries(row: LedgerRow) -> Mapping[Disposition, int]:
    return row.entries


def _conservation(
    unit: str,
    offered: int,
    rows: Iterable[LedgerRow],
    column: Callable[[LedgerRow], Mapping[Disposition, int]],
) -> Conservation:
    """The books for one unit: what the document held against where the rows put it."""
    dispositions: Counter[str] = Counter({d.value: 0 for d in Disposition})
    for row in rows:
        for disposition, count in column(row).items():
            dispositions[disposition.value] += count
    return Conservation(stage=STAGE, unit=unit, offered=offered, dispositions=dict(dispositions))


def _row_report(row: LedgerRow, present_in: Mapping[str, int]) -> dict[str, Any]:
    return {
        "construct": row.construct,
        "templates": list(row.templates),
        "present_in_documents": present_in.get(row.construct, 0),
        "offered": row.offered,
        "instances": {d.value: n for d, n in sorted(row.instances.items())},
        "entries": {d.value: n for d, n in sorted(row.entries.items())},
        "unlinkable": row.unlinkable,
    }


# --- what a construct CDA gives no id can still state -------------------------

#: One stated fact: the label this ledger reads it under, and the value.
_Fact = tuple[str, str]


@dataclass(frozen=True)
class _Content:
    """How one id-less construct class states itself, on both sides of the seam.

    ``stated`` reads the XML and ``recorded`` reads a canonical object; a parse
    is credited when the two produce the SAME facts. Both halves are spelled
    here rather than derived from the parser, for the reason ``_narrative_pool``
    gives: reconstructing the parser's mapping rules would make this instrument
    a mirror, and a mirror that drifts reports the drift as loss. Where a
    spelling IS shared with the parser it is shared knowingly — if the two ever
    disagree the facts differ, differing means uncredited, and uncredited is the
    direction this ledger is allowed to be wrong in.

    ``applies`` is asked of the construct node, because id-lessness is a
    property of the role CDA played, not of the participation's name: an
    ``informant`` playing an ``assignedEntity`` carries an id and keeps the id
    rule, shared-root refusal included.
    """

    applies: Callable[[_Element], bool]
    stated: Callable[[_Element], frozenset[_Fact]]
    recorded: Callable[[AnastBase], frozenset[_Fact]]


def _facts(*pairs: tuple[str, str | None]) -> frozenset[_Fact]:
    """The pairs something was actually stated for. Nothing stated is no fact,
    which is how a construct that states nothing ends up unmatchable."""
    return frozenset((label, value) for label, value in pairs if value is not None)


def _stated_text(value: object) -> str | None:
    """A value the record states, or ``None`` when it is not text at all.

    ``extensions`` is typed ``Any``; a dict or a list arriving where a name was
    expected must read as "stated nothing" rather than compare by repr.
    """
    return value if isinstance(value, str) else None


def _stated_texts(value: object) -> list[str]:
    return [item for item in value if isinstance(item, str)] if isinstance(value, list) else []


#: The fields an authoring device is made of. CDA R2's ``Device`` has exactly
#: these two names on it and no free text besides.
_DEVICE_FIELDS = ("softwareName", "manufacturerModelName")


def _device_stated(node: _Element) -> frozenset[_Fact]:
    return _facts(*((name, _text_content(_find(node, f"v3:{name}"))) for name in _DEVICE_FIELDS))


def _device_recorded(obj: AnastBase) -> frozenset[_Fact]:
    return _facts(
        *((name, _stated_text(obj.extensions.get(f"ccda:{name}"))) for name in _DEVICE_FIELDS)
    )


def _name_facts(name: _Element) -> list[tuple[str, str | None]]:
    """One ``<name>`` broken into the parts the document actually split it into.

    The un-split fallback is the parser's own and is here for the same reason
    the parser has it: a name written as element text still says who this is.
    Every ``<given>`` is stated, not the first one — a document that spelled two
    given names stated two, and a record holding one of them is not holding what
    the document said.
    """
    given = [("given", _text_content(node)) for node in _findall(name, "v3:given")]
    family = _text_content(_find(name, "v3:family"))
    residue = [(part, _text_content(_find(name, f"v3:{part}"))) for part in ("prefix", "suffix")]
    if family is None and not any(value is not None for _, value in given):
        return [("name", _text_content(name)), *residue]
    return [*given, ("family", family), *residue]


def _person_stated(node: _Element) -> frozenset[_Fact]:
    """What a participation states about the person who took part.

    Read over the whole participation subtree rather than at a fixed path: CDA
    spells the role and the person element differently under every
    participation, and a second copy of that table would be one more thing to
    drift. An address is deliberately not read — normalizing one is the
    parser's business and re-spelling it here is precisely the mirror this
    avoids — so two actors alike but for their address state the same facts and
    compete for one object, which can only lower the credited count.
    """
    facts: list[tuple[str, str | None]] = []
    for name in node.iter(_q("name")):
        facts += _name_facts(name)
    facts += [("telecom", _attr(tel, "value")) for tel in node.iter(_q("telecom"))]
    # displayName before code: the document's own spelling of what its code
    # means is the one the record keeps, so it is the one compared.
    facts += [
        ("code", _attr(code, "displayName") or _attr(code, "code"))
        for code in node.iter(_q("code"))
    ]
    return _facts(*facts)


def _person_recorded(obj: AnastBase) -> frozenset[_Fact]:
    """What a canonical actor states about itself, in the same vocabulary.

    Only a :class:`~anastomosis.core.model.Practitioner` can answer: the record
    carries every actor as one, whatever role the document named them in.
    """
    if not isinstance(obj, Practitioner):
        return frozenset()
    stated: list[tuple[str, str | None]] = [
        ("given", obj.given_name),
        ("family", obj.family_name),
        ("name", obj.display_name),
        *(
            (part, _stated_text(obj.extensions.get(f"ccda:{part}")))
            for part in ("prefix", "suffix", "code")
        ),
        *(("telecom", value) for value in _stated_texts(obj.extensions.get("ccda:telecom"))),
    ]
    return _facts(*stated)


def _plays(role: str) -> Callable[[_Element], bool]:
    """A construct that plays ``role`` — the CDA element that carries no id."""
    return lambda node: _find(node, f"v3:{role}") is not None


def _always(node: _Element) -> bool:
    """A construct whose CDA class has no id in any of its forms."""
    return True


#: The construct classes CDA itself leaves without an ``<id>``, listed by name
#: rather than inferred, because "this one had no id in this document" is a
#: property of one document and "this one can never have an id" is a property of
#: the standard — and only the second may widen the evidence rule.
#:
#: * ``assignedAuthoringDevice`` — CDA R2's ``Device`` class has ``classCode``,
#:   ``determinerCode``, ``code``, ``manufacturerModelName`` and
#:   ``softwareName``. There is no ``id`` on it to link by, in any document.
#: * ``informant`` — an informant plays either an ``assignedEntity``, which
#:   carries ``id``, or a ``RelatedEntity``, which CDA R2 gives no ``id`` at
#:   all. Only the second is admitted here; the first keeps the id rule.
ID_LESS_CONSTRUCTS: Mapping[str, _Content] = {
    "assignedAuthoringDevice": _Content(_always, _device_stated, _device_recorded),
    "informant": _Content(_plays("relatedEntity"), _person_stated, _person_recorded),
}


# --- the places a record keeps evidence in -----------------------------------
#
# Every evidence question this module asks is the same shape: a construct
# offers a HANDLE, and a PLACE in the record is asked whether it holds
# something that handle can be shown to have produced. What separates the
# questions is not the asking, it is whether asking COSTS anything.
#
# That distinction used to live inside two method bodies and was declared in
# neither signature, which is what #329 was filed about. It is now the type of
# the place. A ``_Facts`` has no method that mutates it and is frozen besides,
# so an id can answer for any number of constructs and no future edit can
# quietly make it answer once. A pool answers only by being spent, because its
# members are obligations: two sections spelled identically are two
# obligations, as are two identical informants, and a pool that answered yes
# twice for one stored thing would credit a construct that was dropped.
#
# The two pools stay two types rather than one. A narrative is claimed by an
# exact key; an object is claimed by a predicate the CALLER supplies, because
# which facts count is a property of the asking rule and not of the object.
# Forcing both through one interface would mean precomputing every object's
# facts under a rule not yet known, and would leave a name that means two
# things.

_Key = TypeVar("_Key")


@dataclass(frozen=True)
class _Facts:
    """A place that answers as often as it is asked, and is never spent.

    Frozen, and holding a ``frozenset``, on purpose: there is no method here
    that removes a member and no way to add one. "Asking this does not cost
    anything" is therefore readable from the type, which is the whole point of
    the exercise, rather than from remembering that nobody wrote a decrement.
    """

    held: frozenset[str]

    def holds(self, value: str) -> bool:
        return value in self.held

    def any_of(self, values: Iterable[str]) -> bool:
        return bool(self.held & frozenset(values))

    def namespaced(self, prefix: str) -> bool:
        """Whether anything here is ``prefix`` itself or a key under it."""
        return any(key == prefix or key.startswith(f"{prefix}:") for key in self.held)


class _KeyedPool(Generic[_Key]):
    """A place whose members are claimed by an exact key, one claim each.

    ``take`` is the only way in or out. There is deliberately no way to look
    without taking: a caller that could ask without spending could write a
    predicate that credits the same stored thing twice, which is the arithmetic
    this ledger exists to refuse.
    """

    def __init__(self, counts: Counter[_Key]) -> None:
        self._counts = counts

    def take(self, key: _Key) -> bool:
        if self._counts[key] < 1:
            return False
        self._counts[key] -= 1
        return True

    def take_all(self, keys: Iterable[_Key]) -> bool:
        """Claim every one of ``keys``, or claim none of them.

        A caller needing several claims to answer one question must not spend
        half of them and then fail: the ones it spent would be gone from the
        next asker, which is a loss this ledger invented rather than found.
        """
        needed = Counter(keys)
        if any(self._counts[key] < count for key, count in needed.items()):
            return False
        self._counts -= needed
        return True


class _MatchedPool:
    """A place whose members are claimed by a predicate, one claim each.

    Separate from :class:`_KeyedPool` because the question is not "is this key
    present" but "does any object here record exactly what the asking rule says
    this construct states" — and the rule, not the object, decides which facts
    count. One object is one parse: an object that has already stood for a
    construct cannot stand for a second, so N id-less constructs against M
    matching objects credit ``min``.
    """

    def __init__(self, items: Iterable[AnastBase]) -> None:
        self._items: list[AnastBase | None] = list(items)

    def take(self, matches: Callable[[AnastBase], bool]) -> bool:
        for index, item in enumerate(self._items):
            if item is not None and matches(item):
                self._items[index] = None
                return True
        return False


# --- what the record can prove -----------------------------------------------


@dataclass(frozen=True)
class _Evidence:
    """Everything the parsed record can PROVE about a construct.

    Built once per document. The five places below are the whole vocabulary,
    and each one's type says whether asking it costs anything — see the note
    above :class:`_Facts`.
    """

    #: Where an id is asked. A fact: one id answers for as many constructs as
    #: carry it, because an id is true rather than owed.
    source_ids: _Facts
    #: Which roots are worth asking by at all. Also a fact, and asked of the
    #: construct rather than of the record.
    linkable_roots: _Facts
    #: Which KINDS of clinical statement this document has been seen to link,
    #: as template OIDs (or an element name where a statement declares none).
    #: A fact, and the calibration :meth:`links` needs — see it for why.
    linked_kinds: _Facts
    #: Where a stored narrative is claimed, one claim per stored pair.
    narrative: _KeyedPool[tuple[str | None, str | None]]
    #: Where a namespace is asked. A fact, because two questions are asked of
    #: it that cost nothing: whether the prior-loss narrative is held, and
    #: whether a key exists at all.
    extension_keys: _Facts
    #: The entries the record stored from OUR OWN exported loss ledgers, one
    #: claim each. Counted, not merely present: the parser concatenates every
    #: stamped ledger it walks into one key, so the key's existence says a
    #: ledger arrived and says nothing about WHICH.
    own_loss_entries: _KeyedPool[str]
    #: Where a parked PAYLOAD is claimed, one claim per stored item. Counted
    #: and spent rather than asked as a fact, because a key is not a payload:
    #: `ccda:serviceEvent` holding `[]` is the adapter having written the
    #: namespace and kept nothing, and two offered events against one stored
    #: item are one preserved and one lost.
    parked_items: _KeyedPool[str]
    #: Where a canonical object is claimed, one claim per object.
    objects: _MatchedPool
    #: Where a text-less section's verbatim entries are claimed, one claim per
    #: stored copy — the sixth place, added for #314, and an instance of an
    #: existing type rather than a new kind of question.
    entries: _KeyedPool[tuple[str, str]]
    #: The source ids the record's DOCUMENT artifacts name. Kept apart from
    #: ``source_ids`` because the body constructs are asked a narrower question
    #: than ``links`` asks (see :meth:`carried_as_document`), and answering it
    #: out of the whole record would credit an unstructured body to the
    #: document id every record's own provenance already carries.
    document_source_ids: _Facts

    def links(self, node: _Element) -> bool | None:
        """Whether every clinical statement in this construct reached the record.

        ``None`` is not "no": it means the construct carries no id this ledger
        could have linked by, so the question was never asked. Kept distinct
        because collapsing it into "no" would report the ledger's blind spot as
        the adapter's loss.

        The question used to be "did ANY id under here reach the record", and
        one id is a poor answer for a wrapper holding several statements: an
        ``<organizer>`` of two results kept its verdict when one of the two was
        dropped, because the survivor's id still matched. So the statements are
        asked one at a time and the answer is ALL of them — but only of the
        statements this document has shown the adapter linking, which is the
        difference between a missing sibling and a shape the mapping folds in
        on purpose. A Problem Concern Act is linked and its nested Problem
        Observation never is; requiring the observation would report every
        conforming problem as half lost. :attr:`linked_kinds` is that
        calibration, read from this document's own successes.

        With no calibrated statement to ask about, the old any-of answer stands
        unchanged — so this can only ever turn a yes into a no or into a blind
        spot, never a no into a yes, and a construct whose every statement was
        dropped still reads as the loss it always did.
        """
        roots = {root for root in _id_roots(node) if self.linkable_roots.holds(root)}
        if not roots:
            return None
        obligations, uncheckable = self._obligations(node)
        if uncheckable:
            return None
        if not obligations:
            return self.source_ids.any_of(roots)
        return all(
            self.source_ids.any_of(self._own_linkable(statement)) for statement in obligations
        )

    def _obligations(self, node: _Element) -> tuple[list[_Element], bool]:
        """``(the statements that must link, whether one could not be asked)``.

        A statement of a kind this document links, carrying ids that are all
        shared with something else, is one this reading cannot follow — and
        dropping it from the obligation set would let its sibling's success
        answer for it, which is the whole habit being broken here. It is a
        blind spot instead: the caller reports the construct as impossible to
        check rather than as parsed, so a lost statement behind a shared root
        can still be counted as loss and never as a clean bill of health.
        """
        obligations: list[_Element] = []
        uncheckable = False
        for statement in _clinical_statements(node):
            if not self.linked_kinds.holds(_statement_kind(statement)):
                continue
            if self._own_linkable(statement):
                obligations.append(statement)
            elif _own_id_roots(statement):
                uncheckable = True
        return obligations, uncheckable

    def _own_linkable(self, statement: _Element) -> set[str]:
        """A statement's OWN linkable id roots — its ``<id>`` children, not its
        descendants', so a nested performer's id cannot answer for the act."""
        return {root for root in _own_id_roots(statement) if self.linkable_roots.holds(root)}

    def states(self, name: str, node: _Element) -> bool | None:
        """Whether the record holds an object stating what this construct states.

        The second form of evidence, asked only after the first came back
        ``None``, and only of the classes :data:`ID_LESS_CONSTRUCTS` names.
        ``None`` again means the question could not be answered rather than
        answered no — a construct that states nothing at all is one of those,
        because absence of evidence is not evidence — so the construct stays
        counted in the ledger's blind spot exactly as it was before.
        """
        rule = ID_LESS_CONSTRUCTS.get(name)
        if rule is None or not rule.applies(node):
            return None
        facts = rule.stated(node)
        if not facts:
            return None
        # A miss here is None, not False, and the asymmetry with
        # :meth:`kept_narrative` below is deliberate. Content matching mirrors
        # what the parser chose to record, so failing to find a match says the
        # mirror did not line up — this ledger's own blind spot rather than a
        # loss it can attribute to the adapter. A narrative miss IS a no,
        # because the narrative pool is what the record actually kept.
        return True if self.objects.take(lambda obj: rule.recorded(obj) == facts) else None

    def kept_narrative(self, title: str | None, text: str | None) -> bool:
        """Whether the record kept this construct's own narrative, spending it."""
        return self.narrative.take((title, text))

    def entry_kept(self, code: str, entry: _Element) -> bool:
        """Whether the record preserved this exact entry verbatim, spending it.

        The handle is the entry's own serialisation, through the same function
        the parser stored it with — so the question is byte-exact, and a miss
        is a real no: either the record holds these bytes or it does not.

        Asked of every unlinked entry now, not only of one whose section kept
        no narrative. The parser still parks only where a section RENDERS no
        text, so the answer is no wherever it did not park — and asking anyway
        is the point: this side reads what the record HOLDS rather than
        reconstructing the rule by which it was written.
        """
        return self.entries.take((code, entry_verbatim(entry)))

    def carried_as_document(self, identifier: str | None) -> bool | None:
        """Whether a document artifact in the record came from ``identifier``.

        ``None`` has the same meaning it has in :meth:`links` — the question
        could not be asked — and for the same reason: no id to ask it by.

        A separate question from ``links`` because CDA gives ``NonXMLBody`` no
        ``<id>`` element at all, so there is nothing inside the construct to
        compare and every unstructured body would sit in ``unlinkable`` forever,
        whatever the adapter learned to do with it. The DOCUMENT's own id is
        what such a body is attributable by — an Unstructured Document IS its
        artifact — and the evidence stays the record's word rather than a
        table's: with no artifact carrying that id, the row reads unsupported.
        """
        if identifier is None:
            return None
        return self.document_source_ids.holds(identifier)

    def parked_under(self, name: str) -> bool:
        """Whether a payload parked under this construct answers for it, spending it.

        The adapter parks what it cannot map under a ``ccda:`` key, so this is
        how a construct preserved WITHOUT a typed object is recognised — and it
        reads the record rather than a table of what the parser is believed to
        do, so a future fix moves this number without touching this file.

        What is read is the PAYLOAD, not the key. The key was the whole test
        once, and a key is the cheapest thing in the record to be right about:
        an adapter that wrote ``ccda:serviceEvent`` and then stored an empty
        list under it scored the same as one that stored the event's facts, so
        a regression that cleared the payload was reported as preservation. One
        stored item answers for one offered construct and is then spent, so a
        document offering two events with one item stored reads as one
        preserved and one lost rather than as two preserved.
        """
        return self.parked_items.take(name)

    def own_loss_kept(self, entries: list[str]) -> bool:
        """Whether THIS stamped ledger's own entries are among the stored ones.

        All of them, and spent — a claim, not a look. Which sections may make
        the claim at all is the CALLER's question and is settled in
        :func:`_narrative_kept`, because the store is filled from the parser's
        walk and a section off that walk has no standing to ask.
        """
        return bool(entries) and self.own_loss_entries.take_all(entries)


def _evidence(root: _Element, record: PatientRecord) -> _Evidence:
    source_ids = _record_source_ids(record)
    linkable = _linkable_roots(root)
    return _Evidence(
        source_ids=_Facts(frozenset(source_ids)),
        linkable_roots=_Facts(frozenset(linkable)),
        linked_kinds=_Facts(frozenset(_linked_kinds(root, linkable, source_ids))),
        narrative=_KeyedPool(_narrative_pool(record)),
        extension_keys=_Facts(frozenset(record.patient.extensions)),
        own_loss_entries=_KeyedPool(_own_loss_pool(record)),
        parked_items=_KeyedPool(_parked_pool(record)),
        objects=_MatchedPool(_provenanced(record)),
        entries=_KeyedPool(_entry_pool(record)),
        document_source_ids=_Facts(frozenset(_source_ids(record.documents))),
    )


def _own_loss_pool(record: PatientRecord) -> Counter[str]:
    """The entries the record kept from this exporter's own loss ledgers.

    ``ccda:prior_loss_narrative`` is ONE key however many stamped 51899-3
    sections the parser walked — it concatenates their entries so a re-export
    carries a single deduplicated appendix — so the key answers for the
    construct class and not for any one construct. Counted here for the same
    reason every other place in this module is counted: two ledgers offered
    and one stored is one preserved and one lost, and asking whether the key
    exists reads it as two.
    """
    stored = record.patient.extensions.get(EXT_PRIOR_LOSS_NARRATIVE)
    if not isinstance(stored, dict):
        return Counter()
    entries = stored.get("entries")
    if not isinstance(entries, list):
        return Counter()
    # A list OF STRINGS is the rest of that key's contract, and the narrowing
    # finishes here rather than half way: a record this module did not build is
    # still an input, and an unhashable element would raise out of `Counter`
    # and abort the whole reading instead of leaving one section uncredited.
    return Counter(entry for entry in entries if isinstance(entry, str))


def _parked_pool(record: PatientRecord) -> Counter[str]:
    """How many facts the record parked under each ``ccda:`` namespace.

    Keyed by the namespace's first segment. Both separators are cut because
    both are shapes the parser's own key-writing can produce — ``:`` for a key
    it deepens, ``#`` for the number ``free_key`` appends to a repeat — and
    neither is reached by any participation key it writes today: this side
    reads the shape rather than the current caller list, so a parked
    participation that later repeats is bucketed without an edit here.

    PHI: the values are COUNTED and never read. A list is as many facts as it
    has members, a mapping or a scalar is one when it holds anything, and
    anything empty is none — which is the whole point, since an empty payload
    is a namespace with nothing in it.
    """
    pool: Counter[str] = Counter()
    for key, value in record.patient.extensions.items():
        namespace, _, remainder = key.partition(":")
        if namespace != "ccda" or not remainder:
            continue
        name = remainder.partition(":")[0].partition("#")[0]
        pool[name] += len(value) if isinstance(value, list) else bool(value)
    return pool


#: Narrative elements that GROUP every cell rather than being one, plus the
#: caption that labels the group. An ID on any of these names the whole
#: arrangement, and an entry citing it is citing the section's prose by another
#: route — the credit this ledger stopped giving — so excluding the ``<text>``
#: element by identity is not enough on its own.
#:
#: A ``<tr>`` is deliberately NOT here: one row is one statement, the same
#: granularity as the ``<item>`` of a list or the ``<td>`` inside it, and
#: excluding it reported loss for narrative the record demonstrably holds.
_NARRATIVE_CONTAINERS = frozenset({"table", "thead", "tbody", "tfoot", "list", "caption"})


def _is_narrative_cell(node: _Element, text: _Element) -> bool:
    """Whether this node inside ``text`` is one citable cell of narrative.

    Strictly inside, and not one of the things that merely hold cells: an entry
    citing the ``<text>`` element, or the ``<table>`` filling it, is citing the
    section's whole prose, which is the credit this ledger stopped giving.

    It must carry a name, because an unnamed cell cannot be cited, and it must
    render words. A ``<content>`` holding only a ``renderMultiMedia`` reaches
    the record as nothing at all, so crediting an entry to it would be
    crediting it to something the record does not have. A comment is not a
    cell either, and lxml gives comments a callable tag rather than a string.

    PHI: the text is read to ask whether there is any, and is neither stored
    nor emitted.
    """
    return (
        node is not text
        and not callable(node.tag)
        and etree.QName(node).localname not in _NARRATIVE_CONTAINERS
        and bool(node.get("ID"))
        and _text_content(node) is not None
    )


def _enclosing_cells(named: list[_Element]) -> dict[_Element, _Element | None]:
    """Each cell mapped to the nearest cell that encloses it, or ``None``.

    Asked upward, once per cell, because asking downward — "does any other
    cell lie inside this one" — is a question about every pair and costs the
    square of them: a results table of 4,800 rows spent 14 seconds answering
    it. The walk up is bounded by how deeply narrative nests, which is small.
    """
    inside = set(named)
    return {
        node: next((cell for cell in node.iterancestors() if cell in inside), None)
        for node in named
    }


def _leaf_cells(enclosing: Mapping[_Element, _Element | None]) -> list[_Element]:
    """The cells that enclose no other cell: one word belongs to one of these."""
    wrappers = set(enclosing.values())
    return [node for node in enclosing if node not in wrappers]


def _cell_names_from(
    leaf: _Element, enclosing: Mapping[_Element, _Element | None]
) -> Iterator[str]:
    """This cell's name, then the name of every cell that encloses it."""
    walk: _Element | None = leaf
    while walk is not None:
        name = walk.get("ID")
        if name:
            yield name
        walk = enclosing[walk]


def _cell_cover(named: list[_Element]) -> dict[str, frozenset[str]]:
    """Every cell's name mapped to the innermost cells it stands over.

    One word is one statement, and it can be addressed by more than one name.
    A table writes the row's name on the row and the cell's name on the cell
    inside it, and an entry may reach that word by either — a procedure's
    ``<originalText>`` names the row while its ``<text>`` names the content.
    So the names are addresses and the innermost cells are the claims: two
    addresses over one word spend the one claim, and the second entry to ask
    gets nothing.

    Dropping the outer name instead was tried, and it reported loss for a word
    the record demonstrably holds — a single entry citing its own row, the
    ordinary arrangement, came back unsupported.

    A cell that wraps another keeps no claim of its own. Words of its own
    outside the wrapped cell are not separately claimable, which is the
    conservative reading: overlapping text is one statement, not two.
    """
    enclosing = _enclosing_cells(named)
    cover: dict[str, set[str]] = {}
    for leaf in _leaf_cells(enclosing):
        path = list(_cell_names_from(leaf, enclosing))
        for name in path:
            cover.setdefault(name, set()).update(path[:1])
    return {name: frozenset(cells) for name, cells in cover.items()}


class _Anchors:
    """One section's narrative cells, addressable by any name written over them.

    A place, like the pools above, and spent the same way: :meth:`take` is the
    only way in, and a yes costs a claim. What it adds is that several names
    may lead to one claim, because C-CDA lets a document write a name at every
    level of its table and an entry cite whichever it likes.
    """

    def __init__(self, covers: Mapping[str, frozenset[str]]) -> None:
        self._covers = covers
        self._cells: _KeyedPool[str] = _KeyedPool(
            Counter({cell for cells in covers.values() for cell in cells})
        )

    def demand(self, cited: list[str]) -> int:
        """How many cells this citation asks for; zero if it names one we lack.

        A count over the ADDRESS map, never over the claims: it says how much
        an entry is asking for, not whether the asking will succeed, and the
        claims stay reachable only through :meth:`take`. It exists so a
        section can settle its entries in an order it chooses rather than the
        one the document happened to list them in.
        """
        if any(name not in self._covers for name in cited):
            return 0
        return len({cell for name in cited for cell in self._covers[name]})

    def take(self, cited: list[str]) -> bool:
        """Claim the cells ``cited`` addresses, all of them or none.

        A name this section's narrative does not define is a citation the
        document cannot back, and it fails the whole claim rather than being
        quietly dropped — the entry named something that is not there.
        """
        if not cited or any(name not in self._covers for name in cited):
            return False
        return self._cells.take_all({cell for name in cited for cell in self._covers[name]})


def _section_anchors(section: _Element) -> dict[str, frozenset[str]]:
    """The narrative cells INSIDE this section's ``<text>``, by every name.

    A cell in another section is not here — containment is decided by the tree
    rather than by whether one string happens to occur inside another, so a
    cell reading "No" cannot answer for an entry in a section whose prose
    contains the word. Nor is the ``<text>`` element itself, or a ``<table>``
    or ``<caption>``: those name the whole arrangement, and an entry citing
    one is citing the section's prose by another route.

    PHI: only the cells' NAMES are kept. The text is read to ask whether there
    is any, and is neither stored nor emitted.
    """
    text = _find(section, "v3:text")
    if text is None:
        return {}
    return _cell_cover([node for node in text.iter() if _is_narrative_cell(node, text)])


def _cited_anchors(entry: _Element) -> list[str]:
    """The narrative cell names this entry writes, in document order.

    Repeats are left in. An entry routinely names one cell twice — a
    procedure's ``<originalText>`` and its ``<text>`` both point at the row
    that describes it — and that is one citation of one cell; the collapsing
    happens where the claim is made, in :meth:`_Anchors.take`, which resolves
    every address to the cells behind it and asks for the set. Doing it twice
    would put the invariant in two places and pin it in neither.

    Only a ``#``-prefixed value is a citation, because that is the only kind
    :func:`~.parser._inline_narrative_references` resolves — anything else
    reaches the record carrying nothing. ``strip`` because that function
    strips before it resolves, and two sides of one mirror that disagree
    about whitespace report the disagreement as loss.
    """
    return [
        value[1:]
        for reference in entry.iter(_q("reference"))
        if (value := (reference.get("value") or "").strip()).startswith("#")
    ]


def _entry_pool(record: PatientRecord) -> Counter[tuple[str, str]]:
    """Every verbatim entry the record stored, by the section that stored it.

    Read from what the parser actually wrote under ``ccda:entries:<code>`` —
    the stored shape, never a reconstruction of the storing rule — and counted,
    because two identical entries in two text-less sections are two stored
    copies and must answer for exactly two constructs.

    Keyed by the section code as well as the bytes. Two byte-identical entries
    in different sections are ordinary (an empty coded entry repeats), and with
    the bytes alone the section that parked nothing could claim the copy parked
    for the other, making the reading depend on document order. The parser
    writes the code into the key, so this side reads it rather than guessing.
    """
    pool: Counter[tuple[str, str]] = Counter()
    prefix = f"{EXT_SECTION_ENTRIES}:"
    for key, value in record.patient.extensions.items():
        if not key.startswith(prefix) or not isinstance(value, list):
            continue
        code = key[len(prefix) :].partition("#")[0]
        pool.update((code, item) for item in value if isinstance(item, str))
    return pool


def _provenanced(record: PatientRecord) -> Iterator[AnastBase]:
    """Every canonical object in the record that can name where it came from."""
    yield record
    yield record.patient
    for name in type(record).model_fields:
        value = getattr(record, name)
        if isinstance(value, list):
            yield from (item for item in value if isinstance(item, AnastBase))


def _record_source_ids(record: PatientRecord) -> set[str]:
    """Every source id the record's provenance points back at.

    PHI: this set is COMPARED and never emitted. One of its members is the
    patient's own source identifier, which is why it may not be logged, named
    in a message, or written into a report — and why nothing here does.
    """
    return _source_ids(_provenanced(record))


def _source_ids(objects: Iterable[AnastBase]) -> set[str]:
    """Every source id ``objects`` point back at. PHI: compared, never emitted."""
    return {
        obj.provenance.source_id
        for obj in objects
        if obj.provenance is not None and obj.provenance.source_id is not None
    }


def _id_roots(node: _Element) -> set[str]:
    return {root for id_node in node.iter(_q("id")) if (root := id_node.get("root"))}


def _own_id_roots(node: _Element) -> set[str]:
    """The roots of ``node``'s own ``<id>`` CHILDREN, ignoring its descendants'."""
    return {root for child in node if child.tag == _q("id") and (root := child.get("root"))}


#: The CDA acts a clinical statement can be. Everything the R2.1 entry content
#: models allow in an ``<entry>`` or nested under one; the ledger asks each of
#: them separately rather than asking their enclosing wrapper once.
_STATEMENT_TAGS = frozenset(
    _q(tag)
    for tag in (
        "act",
        "encounter",
        "observation",
        "observationMedia",
        "organizer",
        "procedure",
        "regionOfInterest",
        "substanceAdministration",
        "supply",
    )
)


def _clinical_statements(node: _Element) -> list[_Element]:
    """Every clinical statement at or under ``node``, at any depth."""
    return [element for element in node.iter() if element.tag in _STATEMENT_TAGS]


def _statement_kind(statement: _Element) -> str:
    """What KIND of statement this is, in the document's own vocabulary.

    Its element name and the template roots it declares, sorted — a Problem
    Concern Act and the Problem Observation inside it are two kinds, which is
    exactly the distinction :meth:`_Evidence.links` needs and the one an
    element name alone is too coarse to make (a reaction and an allergy are
    both ``observation``). A statement declaring no template is identified by
    its element name, which is all the document offered.

    The roots go in RAW, not through :func:`_vocabulary`, and the element name
    is carried even when templates exist. Both are the same guard against the
    same mistake: this string is an identity, and every kind it wrongly merges
    becomes an obligation somebody else's success calibrated, which is the
    false-alarm direction. Sanitising collapsed every non-OID root to one label,
    so two unrelated vendor templates read as one kind; dropping the element
    name let an ``<act>`` and an ``<observation>`` sharing a template do the
    same.

    PHI: compared, never emitted — like ``source_ids`` beside it. Template
    roots and element names are structural vocabulary either way, and no id,
    value, or narrative reaches this string.
    """
    templates = sorted(
        root for child in statement if child.tag == _q("templateId") and (root := child.get("root"))
    )
    return " ".join([str(etree.QName(statement).localname), *templates])


def _linked_kinds(root: _Element, linkable: set[str], source_ids: set[str]) -> set[str]:
    """The statement kinds this document is SHOWN to link, from its own record.

    Calibration rather than a table: a mapping that starts recording problem
    observations by their own id moves this set on the next run, with nothing
    here to edit. Read from the whole document so one section's success can
    speak for the same kind in another.
    """
    return {
        _statement_kind(statement)
        for statement in _clinical_statements(root)
        if _own_id_roots(statement) & linkable & source_ids
    }


def _linkable_roots(root: _Element) -> set[str]:
    """Id roots that occur exactly once in the document.

    A root two constructs share cannot say which of them an object came from —
    and sharing is ordinary C-CDA, where one organization OID is stamped on the
    author, the custodian and every entry beneath them. Crediting both would
    turn one parsed entry into a whole header that "survived".
    """
    counts = Counter(value for node in root.iter(_q("id")) if (value := node.get("root")))
    return {value for value, count in counts.items() if count == 1}


def _narrative_pool(record: PatientRecord) -> Counter[tuple[str | None, str | None]]:
    """The (title, text) pairs the parser actually stored, as a multiset.

    Matched by CONTENT rather than by reconstructing the parser's ``#2``/``#3``
    key suffixes: the suffix rule is the parser's business and would have to be
    mirrored here to stay right, and a mirror that drifts reports the drift as
    data loss.
    """
    pool: Counter[tuple[str | None, str | None]] = Counter()
    for key, value in record.patient.extensions.items():
        if key.startswith("ccda:section:") and isinstance(value, dict):
            pool[(value.get("title"), value.get("text"))] += 1
    return pool


# --- sections ----------------------------------------------------------------


def _sections(root: _Element) -> list[_Element]:
    """EVERY ``<section>`` in the document, at any depth.

    Not the parser's own depth-one XPath. A C-CDA may nest subsections one
    ``<component>`` deeper, and a ledger that inherited the parser's reach could
    never report a section the parser cannot see — which is the single thing it
    is here to do.
    """
    return list(root.iter(_q("section")))


def _template_roots(node: _Element) -> tuple[str, ...]:
    return tuple(
        _vocabulary(child.get("root"), _OID_RE) for child in _findall(node, "v3:templateId")
    )


def _has_element_child(node: _Element) -> bool:
    # A comment or processing instruction is not content: lxml gives it a
    # callable tag, and an <entry> holding only one is an empty entry.
    return any(not callable(child.tag) for child in node)


def _entry_disposition(entry: _Element, linked: bool | None, narrative_kept: bool) -> Disposition:
    if not _has_element_child(entry):
        return Disposition.SOURCE_EMPTY
    if linked:
        return Disposition.STRUCTURALLY_PARSED
    return Disposition.NARRATIVE_PRESERVED if narrative_kept else Disposition.UNSUPPORTED


def _parks_its_entries(section: _Element) -> bool:
    """Whether this is a section :func:`~.parser._capture_entries` parks for.

    Asked because the stored copies are keyed by section CODE, and a code is
    not unique: Problems (Active) and Problems (Resolved) are both 11450-4, and
    a nested subsection repeats its parent's. Without this, a section that
    parked nothing could claim the copy parked for its namesake, and which one
    got it depended on document order.

    It ASKS the parser's own walk rather than restating it. The first attempt
    restated it — "parent is a component under a structuredBody" — and the two
    diverged in both directions, which is the whole reason this file reads the
    record instead of a table of what the parser is believed to do. A document
    nesting a second ``<structuredBody>`` inside a section has children whose
    parent chain satisfies that test and which the parser's anchored path never
    reaches; they parked nothing and took the copy earned by the section that
    did. The loss-narrative section is the same mistake from the other side:
    the parser skips it deliberately, so it parks nothing either.
    """
    if _text_content(_find(section, "v3:text")) is not None:
        return False
    if _is_own_loss_narrative(section, _section_code(section)):
        return False
    return _walked_index(section) is not None


def _walked_index(section: _Element) -> int | None:
    """Where this section sits in the parser's own walk, or ``None`` if it does
    not sit there at all.

    The walk is an anchored path — ``component/structuredBody/component/
    section`` — so a section nested inside another one, or sitting straight
    under ``<structuredBody>`` without its ``<component>``, is not on it. The
    parser visits neither, which means nothing of either is anywhere in the
    record, and no store may be asked about them.

    A position rather than a yes, because the one caller that needs the yes
    also needs to find this same section in a second reading of the document
    (:func:`_hydrated_sections`), and matching by position is the only way
    that does not depend on element identity across two parses. It ASKS the
    walk rather than restating it, for the reason :func:`_parks_its_entries`
    gives.
    """
    for position, candidate in enumerate(_walk(section.getroottree().getroot())):
        if candidate is section:
            return position
    return None


@lru_cache(maxsize=1)
def _walk(root: _Element) -> list[_Element]:
    """The parser's section walk over this document, taken once.

    Three questions ask it for every section, and re-walking the tree for each
    made the ledger quadratic in section count — four times slower at three
    hundred sections, which a long Epic export reaches. One document is live
    per reading, so a single slot is the whole cache; the key is the root
    element, whose identity holds for as long as the entry does.
    """
    return list(_parser_sections(root))


def _entry_evidence(
    entries: list[_Element], evidence: _Evidence, code: str | None
) -> list[tuple[bool | None, bool]]:
    """Per entry, in document order: its link verdict, and whether a stored
    verbatim copy already answers for it.

    One pass over both, because both questions SPEND: asking either of them
    twice would take twice. The narrative is deliberately not asked here — it
    is settled across the whole section afterwards, in
    :func:`_narrative_credits`.
    """
    verdicts = []
    for entry in entries:
        linked = evidence.links(entry)
        copied = (
            not linked
            and _has_element_child(entry)
            and code is not None
            and evidence.entry_kept(code, entry)
        )
        verdicts.append((linked, copied))
    return verdicts


def _entries_asking_narrative(
    entries: list[_Element], verdicts: list[tuple[bool | None, bool]]
) -> list[_Element]:
    """The entries with nothing else to show, in document order.

    A parsed entry's evidence is its object, and spending a narrative cell for
    it would starve an unparsed sibling that has nothing else. An entry with a
    stored copy of its bytes is already answered for. An empty one is
    SOURCE_EMPTY, which is not a preservation.
    """
    return [
        entry
        for entry, (linked, copied) in zip(entries, verdicts, strict=True)
        if not linked and not copied and _has_element_child(entry)
    ]


def _settle(
    asking: list[_Element], covers: Mapping[str, frozenset[str]], widest_first: bool
) -> set[_Element]:
    """One deterministic pass: which of these entries these cells can honour.

    The order is decided by the CONTENT — how many cells an entry asks for,
    then the names it asks by — and never by the position the section happened
    to list it in. Two entries that tie on both are asking for the same thing,
    so whichever of them wins, the count is the same.
    """
    anchors = _Anchors(covers)
    order = sorted(
        ((entry, _cited_anchors(entry)) for entry in asking),
        key=lambda claim: (anchors.demand(claim[1]), sorted(claim[1])),
        reverse=widest_first,
    )
    return {entry for entry, cited in order if anchors.take(cited)}


def _narrative_credits(
    asking: list[_Element], covers: Mapping[str, frozenset[str]]
) -> set[_Element]:
    """Which of these entries the section's cells can honour, settled together.

    Served in the order the section lists them, an entry citing a whole row
    takes every cell under it and starves the entries that cite those cells by
    name — so the same three entries over the same two words read two
    preserved or one, decided by nothing but which came first. A reading that
    turns on that is the defect this module refuses everywhere else it counts.

    Both ends are tried, narrowest claim first and widest first, and the one
    that honours more entries is the reading. Neither alone is enough: serving
    the narrow claims first lets one entry citing a cell from each of two rows
    kill both row-citing entries; serving the wide ones first lets a row
    swallow cells its own entries had named.

    What this is NOT is a proof of the best possible assignment. Choosing the
    most entries a set of cells can honour is set packing, and this is a
    heuristic over it: measured against a brute-force maximum it never credits
    MORE than an honest assignment could — no preservation is ever invented —
    but on some arrangements it credits fewer, and reports loss an optimal
    assignment would not. That is the safe direction for this instrument, and
    it is stated here rather than implied to be exact.
    """
    if not covers:
        return set()
    narrow = _settle(asking, covers, widest_first=False)
    widest = _settle(asking, covers, widest_first=True)
    return narrow if len(narrow) >= len(widest) else widest


def _entry_dispositions(
    entries: list[_Element],
    evidence: _Evidence,
    code: str | None,
    covers: Mapping[str, frozenset[str]],
) -> tuple[dict[Disposition, int], int]:
    """Every entry's verdict, and how many the ledger could not reach at all.

    What an entry can show is either a verbatim copy of its bytes in the
    record, or the document's own ``<reference>`` into narrative the record
    kept. The section's prose used to answer instead — one boolean shared by
    every entry beneath it — and a ``<text>`` is under no obligation to state
    what its entries state, so generic prose counted an entry whose every fact
    was absent from the record as preserved.
    """
    verdicts = _entry_evidence(entries, evidence, code)
    narrated = _narrative_credits(_entries_asking_narrative(entries, verdicts), covers)
    counts = Counter(
        _entry_disposition(entry, linked, copied or entry in narrated)
        for entry, (linked, copied) in zip(entries, verdicts, strict=True)
    )
    return dict(counts), sum(linked is None for linked, _ in verdicts)


def _narrative_kept(
    section: _Element, evidence: _Evidence, pair: tuple[str | None, str | None]
) -> bool:
    """Whether anything of this section's OWN narrative reached the record.

    One answer, and it answers for the section rather than for anything under
    it. It used to return a second — "and its ``<text>`` survived" — which the
    entries were then graded against, on the assumption that a section's prose
    states what its entries state. C-CDA makes no such promise, so the entries
    are asked at their own address now (:func:`_entry_dispositions`) and this
    is the section's own line again.

    Our own exported loss ledger is the one section stored somewhere else —
    entry by entry under ``ccda:prior_loss_narrative``, so a re-export cannot
    nest generation N-1 inside generation N — so it is asked about at its own
    address rather than reported as dropped.

    That store is filled from the parser's section walk, which means its
    address is "the lines the WALKED ledgers put there". A stamped section off
    the walk contributed nothing to it, and its lines can still be in there
    because another ledger wrote the same ones: an export that dropped the same
    field twice says so twice. Asking the store about such a section is asking
    the wrong address, and it answered yes — a section entirely absent from the
    record read preserved, and the ledger that had really delivered the lines
    was left holding an empty pool and reported lost. Which of the two got the
    credit came down to document order.
    """
    if _is_own_loss_narrative(section, _section_code(section)):
        position = _walked_index(section)
        if position is None:
            return False
        twin = _hydrated_sections(section.getroottree().getroot())[position]
        return evidence.own_loss_kept(_narrative_entries(_find(twin, "v3:text")))
    if pair == (None, None):
        return False
    return evidence.kept_narrative(*pair)


@lru_cache(maxsize=1)
def _hydrated_sections(root: _Element) -> list[_Element]:
    """The walked sections, read as the PARSER reads them.

    The parser resolves ``<reference value="#id"/>`` in place before it captures
    anything, so a ledger line that points into the narrative is stored carrying
    the words it points at. This side re-reads the file and sees the pointer
    instead, matches nothing, and reports a ledger that arrived whole as lost.

    The fix has to be here rather than on the capture side. Capturing before
    hydration makes the two sides agree, but they agree on LESS: a line that is
    only a reference has no text of its own, so it is stored as nothing at all
    and the carried-forward appendix quietly loses it. That is a real deletion
    in the one mechanism this repo has for keeping what it cannot model, traded
    for a reporting fix — the wrong way round.

    So the document is hydrated on a COPY, and only for this question. The
    verbatim-entry mirror next door depends on the untouched tree
    (:func:`~.parser._capture_entries` is taken before hydration on purpose, and
    a hydrated copy of an ``<entry>`` matches nothing the parser stored), which
    is why this resolves a second reading rather than the shared one.

    The caller reads the result by position in the walk, because element
    identity does not survive into a second tree; the cache itself is keyed by
    the root, one document at a time.
    """
    twin = deepcopy(root)
    _inline_narrative_references(twin)
    return list(_parser_sections(twin))


def _section_disposition(
    entries: list[_Element],
    pair: tuple[str | None, str | None],
    narrative_kept: bool,
    entry_counts: Mapping[Disposition, int],
) -> Disposition:
    if not entries and pair == (None, None):
        return Disposition.SOURCE_EMPTY
    if entry_counts.get(Disposition.STRUCTURALLY_PARSED):
        return Disposition.STRUCTURALLY_PARSED
    if narrative_kept or entry_counts.get(Disposition.NARRATIVE_PRESERVED):
        # A section with no prose IS its entries: when those were preserved,
        # something of the section survived, by the same "any" convention the
        # parsed branch above already uses.
        return Disposition.NARRATIVE_PRESERVED
    return Disposition.UNSUPPORTED


def _section_row(section: _Element, evidence: _Evidence) -> LedgerRow:
    entries = _findall(section, "v3:entry")
    # Read off the hydrated twin, for the reason `_hydrated_sections` gives: the
    # parser stored this pair with its references resolved, and asking with the
    # pointer instead reported a section that arrived whole as lost. A section
    # off the walk has no twin and no stored narrative either, so the raw
    # reading is the right one for it.
    read = (
        section
        if (at := _walked_index(section)) is None
        else _hydrated_sections(section.getroottree().getroot())[at]
    )
    pair = (
        _text_content(_find(read, "v3:title")),
        _text_content(_find(read, "v3:text")),
    )
    kept = _narrative_kept(section, evidence, pair)
    # The cells inside the narrative the record demonstrably holds —
    # `kept_narrative` has just claimed this exact pair — so an entry citing one
    # is citing something that survived. Nothing when the narrative did not.
    # And the cells off the same twin, for the same reason: a cell whose only
    # content is a <reference> holds no words raw and holds them hydrated, and
    # a row reporting its narrative preserved while an entry citing that cell
    # reads lost is one reading contradicting itself.
    covers = _section_anchors(read) if kept else {}
    code = (_section_code(section) or "unknown") if _parks_its_entries(section) else None
    entry_counts, unlinkable = _entry_dispositions(entries, evidence, code, covers)
    disposition = _section_disposition(entries, pair, kept, entry_counts)
    return LedgerRow(
        construct=_construct(_SECTION_KIND, _vocabulary(_section_code(section), _LOINC_RE)),
        templates=_template_roots(section),
        instances={disposition: 1},
        entries=entry_counts,
        unlinkable=unlinkable,
    )


# --- participations ----------------------------------------------------------


def _participation_disposition(
    node: _Element, name: str, linked: bool | None, evidence: _Evidence
) -> Disposition:
    if linked:
        return Disposition.STRUCTURALLY_PARSED
    if evidence.parked_under(name):
        return Disposition.NARRATIVE_PRESERVED
    if not _has_element_child(node):
        return Disposition.SOURCE_EMPTY
    return Disposition.UNSUPPORTED


def _participation_row(
    root: _Element, kind: str, name: str, paths: tuple[str, ...], evidence: _Evidence
) -> LedgerRow:
    # Seeded at zero rather than left to appear on first increment: a construct
    # this document has none of must still say so, in the same shape as one it
    # has, or the report can only be read as a list of what was found.
    counts: Counter[Disposition] = Counter({Disposition.SOURCE_EMPTY: 0})
    unlinkable = 0
    for node in _nodes(root, paths):
        linked = evidence.links(node)
        if linked is None:
            linked = evidence.states(name, node)
        unlinkable += linked is None
        counts[_participation_disposition(node, name, linked, evidence)] += 1
    return LedgerRow(
        construct=_construct(kind, name),
        instances=dict(counts),
        unlinkable=unlinkable,
    )


# --- the body ----------------------------------------------------------------


def _document_identity(root: _Element, evidence: _Evidence) -> str | None:
    """The id root this document is identified BY, when it identifies only this.

    Filtered through ``linkable_roots`` for the reason that set exists: a root
    two constructs share cannot say which of them an object came from, and a
    document whose own id is stamped on half its entries would credit its body
    to any of them.
    """
    identifier = _find(root, "v3:id")
    if identifier is None:
        return None
    value = identifier.get("root")
    return value if value is not None and evidence.linkable_roots.holds(value) else None


def _body_row(
    root: _Element, kind: str, name: str, paths: tuple[str, ...], evidence: _Evidence
) -> LedgerRow:
    """One body form's books, asked at the document's id rather than its own.

    Everything else here is measured by :meth:`_Evidence.links`, which reads the
    ``<id root>`` values INSIDE a construct. ``nonXMLBody`` has none to read —
    CDA R2 gives ``NonXMLBody`` no ``id`` element — so that question is
    unanswerable for it by construction, and asking it anyway would report the
    ledger's own blind spot as the adapter's loss forever. The question that can
    be answered is :meth:`_Evidence.carried_as_document`, and it is answered out
    of the record exactly like every other row.
    """
    identity = _document_identity(root, evidence)
    counts: Counter[Disposition] = Counter({Disposition.SOURCE_EMPTY: 0})
    unlinkable = 0
    for node in _nodes(root, paths):
        carried = evidence.carried_as_document(identity)
        unlinkable += carried is None
        counts[_participation_disposition(node, name, carried, evidence)] += 1
    return LedgerRow(
        construct=_construct(kind, name),
        instances=dict(counts),
        unlinkable=unlinkable,
    )


def _body_rows(root: _Element, evidence: _Evidence) -> list[LedgerRow]:
    return [
        _body_row(root, _BODY_KIND, name, paths, evidence) for name, paths in BODY_PATHS.items()
    ]


def _nodes(root: _Element, paths: tuple[str, ...]) -> list[_Element]:
    return [node for path in paths for node in _findall(root, path)]


def _named_rows(
    root: _Element, kind: str, table: Mapping[str, tuple[str, ...]], evidence: _Evidence
) -> list[LedgerRow]:
    return [_participation_row(root, kind, name, paths, evidence) for name, paths in table.items()]


# --- assembly ----------------------------------------------------------------


def _offered(root: _Element) -> int:
    """The construct count, taken in its own pass before anything is classified.

    Deliberately not derived from the rows: an offered figure computed FROM the
    dispositions balances against them no matter what was dropped on the way,
    which is the shape of every clean report this project has had to unlearn.
    """
    named = sum(
        len(_nodes(root, paths))
        for table in (PARTICIPATION_PATHS, BODY_PATHS)
        for paths in table.values()
    )
    # Its own walk for sections, not `_sections`: sharing the enumerator with
    # the row builder would make the two agree by construction, including about
    # a section neither of them saw.
    return sum(1 for _ in root.iter(_q("section"))) + named


def _ledger(root: _Element, record: PatientRecord) -> DocumentLedger:
    evidence = _evidence(root, record)
    ledger = DocumentLedger(
        rows=(
            *(_section_row(section, evidence) for section in _sections(root)),
            *_named_rows(root, _PARTICIPATION_KIND, PARTICIPATION_PATHS, evidence),
            *_body_rows(root, evidence),
        ),
        constructs_offered=_offered(root),
        entries_offered=len(list(root.iter(_q("entry")))),
    )
    ledger.check()
    return ledger


def document_ledger(path: Path, record: PatientRecord | None = None) -> DocumentLedger:
    """Account for one C-CDA document against the record it produced.

    ``record`` is accepted so a caller that has already parsed the document does
    not parse it twice; omitted, the document is parsed here. Either way the
    XML is re-read under the parser's own hardened posture — a ledger is not a
    reason to relax an XXE defence.

    Raises :exc:`~anastomosis.core.conservation.ConservationError` if the books
    do not balance, and whatever :func:`~anastomosis.sources.ccda.parser.parse_document`
    raises for a file that is not a C-CDA at all. Both are loud on purpose: an
    instrument that shrugs is worse than no instrument, because the reading it
    prints will still be believed.
    """
    if record is None:
        record = parse_document(path)
    root = etree.parse(str(path), _PARSER).getroot()
    return _ledger(root, record)


def aggregate(ledgers: Iterable[DocumentLedger]) -> CorpusLedger:
    """Merge document ledgers into one corpus reading.

    Rows merge on construct AND template set, so the same section code declared
    under a vendor's own templateId stays a separate line — that pairing is the
    thing a corpus is read for, and averaging it away hides exactly the variant
    that broke.
    """
    merged: dict[tuple[str, tuple[str, ...]], LedgerRow] = {}
    present: Counter[str] = Counter()
    documents = constructs = entries = 0
    for ledger in ledgers:
        documents += 1
        constructs += ledger.constructs_offered
        entries += ledger.entries_offered
        for row in ledger.rows:
            key = (row.construct, row.templates)
            merged[key] = _merged_row(merged.get(key), row)
        present.update({row.construct for row in ledger.rows if row.offered})
    corpus = CorpusLedger(
        documents=documents,
        rows=tuple(merged[key] for key in sorted(merged)),
        present_in=dict(present),
        constructs_offered=constructs,
        entries_offered=entries,
    )
    corpus.check()
    return corpus


def _merged_row(seen: LedgerRow | None, incoming: LedgerRow) -> LedgerRow:
    if seen is None:
        return incoming
    return LedgerRow(
        construct=seen.construct,
        templates=seen.templates,
        instances=_merge(seen.instances, incoming.instances),
        entries=_merge(seen.entries, incoming.entries),
        unlinkable=seen.unlinkable + incoming.unlinkable,
    )


# --- the reading a physician gets --------------------------------------------

#: What each disposition is called at the end of a run. Number-agnostic on
#: purpose — "1 became data" and "43 empty in the source" both scan — because a
#: verb that had to agree would need a plural rule per phrase, and the first
#: mismatch to slip through would sit in the one report written to be read.
#:
#: ``UNSUPPORTED`` deliberately states an epistemic position, not a cause. An
#: unlinkable instance lands in this column too — on the reference fixture the
#: record CARRIES both authors while their shared id root forbids crediting
#: either — so a phrase like "dropped, no place here" would assert, in the one
#: sentence a physician reads, a loss the module's own docstring only claims as
#: an upper bound. "Not credited" is what the ledger actually knows, and the
#: closing blind-spot line says how much of it is "could not check".
_SAID: Mapping[Disposition, str] = {
    Disposition.STRUCTURALLY_PARSED: "became data",
    Disposition.NARRATIVE_PRESERVED: "kept as text only",
    Disposition.UNSUPPORTED: "not credited as data",
    Disposition.SOURCE_EMPTY: "empty in the source",
}


def _n(count: int, one: str, many: str) -> str:
    return f"{count} {one if count == 1 else many}"


def _account(counts: Mapping[Disposition, int], always: Iterable[Disposition]) -> str:
    """Every disposition's column, spelled out.

    ``always`` names the columns said even at zero — a zero is a statement here
    for the reason :class:`LedgerRow` gives — and any OTHER nonzero column is
    said too, so the sentence's numbers always add back up to the total it
    opened with. A column dropped for reading smoothly is how a clean report
    lies.
    """
    spoken = set(always)
    said = [d for d in Disposition if d in spoken or counts.get(d, 0)]
    return ", ".join(f"{counts.get(d, 0)} {_SAID[d]}" for d in said)


def _kind_instances(corpus: CorpusLedger, kind: str) -> Counter[Disposition]:
    prefix = f"{kind}:"
    counts: Counter[Disposition] = Counter()
    for row in corpus.rows:
        if row.construct.startswith(prefix):
            counts.update(row.instances)
    return counts


def _sections_lines(corpus: CorpusLedger) -> list[str]:
    counts = _kind_instances(corpus, _SECTION_KIND)
    offered = sum(counts.values())
    documents = _n(corpus.documents, "document", "documents")
    if not offered:
        return [f"Across {documents} the source offered no sections."]
    lines = [
        f"Across {documents} the source offered {_n(offered, 'section', 'sections')}: "
        f"{_account(counts, Disposition)}."
    ]
    entries: Counter[Disposition] = Counter()
    for row in corpus.rows:
        entries.update(row.entries)
    if sum(entries.values()):
        said = (Disposition.STRUCTURALLY_PARSED, Disposition.NARRATIVE_PRESERVED)
        lines.append(
            f"Those sections carried "
            f"{_n(sum(entries.values()), 'coded entry', 'coded entries')}: "
            f"{_account(entries, said)}."
        )
    else:
        lines.append("Those sections carried no coded entries.")
    return lines


def _participations_line(corpus: CorpusLedger) -> str:
    counts = _kind_instances(corpus, _PARTICIPATION_KIND)
    offered = sum(counts.values())
    if not offered:
        return "No author, informant, or other named participant appears in the source."
    said = (Disposition.STRUCTURALLY_PARSED, Disposition.UNSUPPORTED)
    return (
        f"People and devices around the chart — its authors, informants, performers — "
        f"were named {_n(offered, 'time', 'times')}: {_account(counts, said)}."
    )


def _body_lines(corpus: CorpusLedger) -> list[str]:
    counts = _kind_instances(corpus, _BODY_KIND)
    offered = sum(counts.values())
    if not offered:
        return []
    said = (Disposition.STRUCTURALLY_PARSED, Disposition.UNSUPPORTED)
    return [
        f"The whole chart travelled as a scanned or non-XML body "
        f"{_n(offered, 'time', 'times')}: {_account(counts, said)}."
    ]


def _unlinkable_line(corpus: CorpusLedger) -> str:
    """The blind spot's own line, said whichever way it went.

    An unlinkable construct already sits in a column above — it is never
    CREDITED, so it lands on the loss side — and this line says how much of
    that loss is really "could not check": the reading's one-sided bias,
    stated in the direction it runs.
    """
    unlinkable = sum(row.unlinkable for row in corpus.rows)
    if not unlinkable:
        return "Every construct the source offered could be checked one way or the other."
    return (
        f"{_n(unlinkable, 'construct', 'constructs')} impossible to check — no identifier "
        f"this reading can follow — never credited as data, so the loss above can only be "
        f"overstated, not understated."
    )


def physician_reading(corpus: CorpusLedger) -> tuple[str, ...]:
    """The corpus reading in the vocabulary of the chart, one sentence per line.

    This is the sentence a doctor can act on — "420 became data, 473 kept as
    text only" — where ``unsupported: 17390`` is not. Deliberately aggregate:
    no LOINC codes, no template OIDs, no per-section rows. The full account,
    construct by construct, is :meth:`CorpusLedger.as_report`, written beside
    the charts as ``loss_ledger.json`` for whoever needs the parser's
    vocabulary after all.

    The blind spot is published, not buried: ``unlinkable`` gets the closing
    line whichever way it went, because a reading that only mentions its own
    uncertainty when convenient is the kind of clean report this project keeps
    having to unlearn.

    PHI: every sentence is these templates' own words plus integers. Nothing a
    document stated — no name, no id, no code — can reach a line, because
    nothing here reads one.
    """
    return (
        *_sections_lines(corpus),
        _participations_line(corpus),
        *_body_lines(corpus),
        _unlinkable_line(corpus),
    )


# --- what may leave ----------------------------------------------------------


def _construct_name(value: str) -> bool:
    """Whether ``value`` is a construct name this module could have built.

    Closed on both halves: the kind is one of three words we chose, and the name
    is either a LOINC code, one of the participation element names, or one of the
    two labels. ``section:Cora`` is refused — an NCName-shaped hole in this
    check is exactly wide enough for a family name.
    """
    kind, _, name = value.partition(":")
    if kind not in _KINDS:
        return False
    known = name in PARTICIPATION_PATHS or name in BODY_PATHS or name in {ABSENT, NONSTANDARD}
    return known or bool(_LOINC_RE.match(name))


def _emittable(value: object) -> bool:
    """Whether ``value`` is structural enough to leave the operator's machine."""
    if isinstance(value, bool) or isinstance(value, int):
        return True
    if not isinstance(value, str):
        return False
    return value in REPORT_WORDS or _construct_name(value) or bool(_OID_RE.match(value))


def assert_emittable(report: object, where: str = "report") -> None:
    """Walk a finished report and refuse to hand over anything unvetted.

    The report is the artifact that travels — into an issue, a PR body, a file
    an operator mails back — so the check belongs at the point of handover
    rather than in the reviewer's memory of how the rows were built.
    """
    if isinstance(report, dict):
        for key, value in report.items():
            if not _emittable(key):
                raise ValueError(f"refusing to emit unsafe key at {where}")
            assert_emittable(value, f"{where}.{key}")
    elif isinstance(report, list):
        for index, value in enumerate(report):
            assert_emittable(value, f"{where}[{index}]")
    elif not _emittable(report):
        raise ValueError(f"refusing to emit unsafe value at {where}")
