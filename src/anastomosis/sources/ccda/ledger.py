"""What the XML offered, and what the record kept — the reading a document
"parsed successfully" cannot itself say.

Walks the document independently of the parser and gives every construct
exactly one disposition (rule 57), credited only by evidence a canonical
object actually carries, never by section-code dispatch (rule 58). An
id-less construct (:data:`ID_LESS_CONSTRUCTS`) is credited by content
match instead. :func:`assert_emittable` enforces the report's PHI
boundary: counts, element names, LOINC codes and template OIDs only.
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

from anastomosis.core.ccda_codes import (
    EXT_PRIOR_LOSS_NARRATIVE,
    EXT_SECTION_ENTRIES,
    SECTION_CODE_UNKNOWN,
)
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
    "skipped_files_clause",
]

#: The seam these books are kept at, named as a crossing (the vocabulary
#: ``Conservation`` messages read in).
STAGE = "ccda xml -> canonical"

_Element = etree._Element


class Disposition(StrEnum):
    """What became of one construct the document offered.

    The four are exhaustive by construction: overlap or a gap could report a
    balanced ledger over a chart that lost half of itself.
    """

    STRUCTURALLY_PARSED = "structurally_parsed"
    NARRATIVE_PRESERVED = "narrative_preserved"
    UNSUPPORTED = "unsupported"
    SOURCE_EMPTY = "source_empty"


# --- the emission vocabulary -------------------------------------------------

# A deliberate whitelist: anything not matching LOINC/OID becomes NONSTANDARD,
# so a patient's name can never pass as a code (same control as
# tools/ccda_shape_report.py's own report).
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
#: Scope is per-element: ``author``/``performer``/``informant`` count
#: everywhere (a note's author is a lost provider, not merely absent);
#: ``participant`` counts HEADER ONLY — a nested one in an allergy entry is
#: the allergen substance, already parsed elsewhere.
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

#: ``nonXMLBody`` is a whole Unstructured Document's clinical content (a
#: scan, a fax) — invisible to the section walk, so it needs its own construct.
BODY_PATHS: Mapping[str, tuple[str, ...]] = {
    "nonXMLBody": ("v3:component/v3:nonXMLBody",),
}


#: Every bare word a report may contain. A new key is added here, in the
#: open, never left implicit in a dict nobody re-reads.
REPORT_WORDS: frozenset[str] = frozenset(
    {
        "version",
        "documents",
        "constructs_offered",
        "entries_offered",
        "skipped_files",
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
    """One construct's books. ``instances`` is keyed by disposition, never a
    single verdict, since a group need not be uniform. A key present at
    zero means the ledger looked and found none; a missing key means it
    never looked.
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
    """Many documents' books, merged construct by construct. ``present_in``
    counts DOCUMENTS offering a construct at all — the number separating "no
    advance directives" from "this adapter drops them". ``skipped_files``
    merges with no row: files the directory walk saw as CDA but never opened
    (#384), carried here so an under-reported corpus still shows.
    """

    documents: int
    rows: tuple[LedgerRow, ...]
    present_in: Mapping[str, int]
    constructs_offered: int
    entries_offered: int
    skipped_files: int = 0

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
            "skipped_files": self.skipped_files,
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
    """How one id-less construct class states itself, on both sides of the
    seam. ``stated``/``recorded`` are hand-spelled, not derived from the
    parser, so drift differs rather than silently mirrors (uncredited is
    the safe direction). ``applies`` tests the construct node: id-lessness
    is a role CDA played, not a participation's name.
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
    """One ``<name>`` broken into the parts the document actually split it
    into. The un-split fallback matches the parser's own. Every ``<given>``
    is stated, not just the first — two given names stated is two facts, not
    one.
    """
    given = [("given", _text_content(node)) for node in _findall(name, "v3:given")]
    family = _text_content(_find(name, "v3:family"))
    residue = [(part, _text_content(_find(name, f"v3:{part}"))) for part in ("prefix", "suffix")]
    if family is None and not any(value is not None for _, value in given):
        return [("name", _text_content(name)), *residue]
    return [*given, ("family", family), *residue]


def _person_stated(node: _Element) -> frozenset[_Fact]:
    """What a participation states about the person who took part. Reads
    the whole subtree, since CDA spells the role differently per
    participation; address is deliberately not read to avoid re-spelling
    the parser's own normalization here.
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


#: Construct classes CDA itself leaves without an ``<id>``, listed by name
#: (never inferred): "can never have an id" is a property of the standard,
#: not of one document.
#:
#: * ``assignedAuthoringDevice`` — CDA R2's ``Device`` has no ``id`` in any
#:   document.
#: * ``informant`` — plays ``assignedEntity`` (has ``id``, keeps the id
#:   rule) or ``RelatedEntity`` (CDA R2 gives it none); only the second is
#:   admitted.
ID_LESS_CONSTRUCTS: Mapping[str, _Content] = {
    "assignedAuthoringDevice": _Content(_always, _device_stated, _device_recorded),
    "informant": _Content(_plays("relatedEntity"), _person_stated, _person_recorded),
}


# --- the places a record keeps evidence in -----------------------------------
# Two shapes of place: free to ask again (:class:`_Facts`) and spent by
# asking (:class:`_KeyedPool`) — declared in the type, not a method body, so
# a caller cannot get the distinction wrong (#329).

_Key = TypeVar("_Key")


@dataclass(frozen=True)
class _Facts:
    """A place that answers as often as it is asked, and is never spent.

    Frozen and holding a ``frozenset``: nothing here removes or adds a
    member, so "asking costs nothing" is readable from the type itself.
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

    ``take`` is the only way in or out — no look-without-taking, so a
    caller cannot credit the same stored thing twice.
    """

    def __init__(self, counts: Counter[_Key]) -> None:
        self._counts = counts

    def take(self, key: _Key) -> bool:
        if self._counts[key] < 1:
            return False
        self._counts[key] -= 1
        return True

    def take_all(self, keys: Iterable[_Key]) -> bool:
        """Claim every one of ``keys``, or claim none of them — a partial
        claim would spend some and leave the next asker short.
        """
        needed = Counter(keys)
        if any(self._counts[key] < count for key, count in needed.items()):
            return False
        self._counts -= needed
        return True


class _MatchedPool:
    """A place whose members are claimed by a predicate, one claim each.

    The rule, not the object, decides which facts count; one object is one
    parse, so N id-less constructs against M matching objects credit
    ``min``.
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
    """Everything the parsed record can PROVE about a construct, built once
    per document. Each field's type says whether asking it costs anything
    (see :class:`_Facts`).
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
    #: Entries stored from our own exported loss ledgers, one claim each.
    #: Counted, not merely present, since the parser concatenates every
    #: stamped ledger into one key.
    own_loss_entries: _KeyedPool[str]
    #: A parked PAYLOAD, one claim per stored item — spent, not asked as a
    #: fact, since a key with an empty payload (``ccda:serviceEvent: []``)
    #: is not the same as one that kept something.
    parked_items: _KeyedPool[str]
    #: Where a canonical object is claimed, one claim per object.
    objects: _MatchedPool
    #: A text-less section's verbatim entries, one claim per stored copy —
    #: the sixth place (#314), an instance of an existing type.
    entries: _KeyedPool[tuple[str, str]]
    #: Source ids the record's DOCUMENT artifacts name. Kept apart from
    #: ``source_ids``: answering from the whole record would credit an
    #: unstructured body to an id every record's own provenance already carries.
    document_source_ids: _Facts

    def links(self, node: _Element) -> bool | None:
        """Whether every clinical statement in ``node`` reached the record.
        ``None`` means no id here could ever answer — never "no". Requires
        ALL calibrated statements (rule 58): one surviving sibling must not
        vouch for a dropped one.
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
        """Contract: (statements that must link, whether one could not be
        asked). A statement whose ids are all shared with something else is
        a blind spot, not a drop — it must never let a sibling's success
        answer for it.
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
        """Whether the record holds an object stating what this construct
        states — the second form of evidence, for :data:`ID_LESS_CONSTRUCTS`
        classes only, after the first came back ``None``. A construct
        stating nothing also answers ``None``: absence is not evidence.
        """
        rule = ID_LESS_CONSTRUCTS.get(name)
        if rule is None or not rule.applies(node):
            return None
        facts = rule.stated(node)
        if not facts:
            return None
        # A miss here is None (this ledger's own blind spot), never False —
        # unlike :meth:`kept_narrative`, where a miss IS a no because the
        # narrative pool is what the record actually kept.
        return True if self.objects.take(lambda obj: rule.recorded(obj) == facts) else None

    def kept_narrative(self, title: str | None, text: str | None) -> bool:
        """Whether the record kept this construct's own narrative, spending it."""
        return self.narrative.take((title, text))

    def entry_kept(self, code: str, entry: _Element) -> bool:
        """Whether the record preserved this entry verbatim, spending it: a
        byte-exact match via the parser's own serialisation, asked of the
        record rather than reconstructed, so a parser change need not touch
        this file.
        """
        return self.entries.take((code, entry_verbatim(entry)))

    def carried_as_document(self, identifier: str | None) -> bool | None:
        """Whether a document artifact in the record came from ``identifier``.
        ``None`` means the same as in :meth:`links`: no id to ask by.
        ``NonXMLBody`` carries no ``<id>`` at all, so it is attributed by
        the DOCUMENT's own id instead.
        """
        if identifier is None:
            return None
        return self.document_source_ids.holds(identifier)

    def parked_under(self, name: str) -> bool:
        """Whether a payload parked under this construct answers for it,
        spending it — a construct preserved WITHOUT a typed object. Reads
        the PAYLOAD, not the key: an empty list under ``ccda:serviceEvent``
        is not preservation. One stored item answers one offered construct.
        """
        return self.parked_items.take(name)

    def own_loss_kept(self, entries: list[str]) -> bool:
        """Whether THIS stamped ledger's own entries are among the stored
        ones — all of them, and spent. Which sections may claim at all is
        settled by the caller, :func:`_narrative_kept`.
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
    ``ccda:prior_loss_narrative`` is ONE key across however many stamped
    51899-3 sections were walked (concatenated into one deduplicated
    appendix), so it answers for the construct class, not one construct.
    """
    stored = record.patient.extensions.get(EXT_PRIOR_LOSS_NARRATIVE)
    if not isinstance(stored, dict):
        return Counter()
    entries = stored.get("entries")
    if not isinstance(entries, list):
        return Counter()
    # A list of strings is the rest of that key's contract; an unhashable
    # element would otherwise abort the whole reading via `Counter`.
    return Counter(entry for entry in entries if isinstance(entry, str))


def _parked_pool(record: PatientRecord) -> Counter[str]:
    """How many facts the record parked under each ``ccda:`` namespace,
    keyed by its first segment (both ``:`` and ``#`` separators cut, since
    both are shapes the parser's key-writing can produce). PHI: values are
    counted, never read; an empty payload counts as none.
    """
    pool: Counter[str] = Counter()
    for key, value in record.patient.extensions.items():
        namespace, _, remainder = key.partition(":")
        if namespace != "ccda" or not remainder:
            continue
        name = remainder.partition(":")[0].partition("#")[0]
        pool[name] += len(value) if isinstance(value, list) else bool(value)
    return pool


#: Narrative elements that GROUP every cell rather than being one. An ID on
#: any of these cites the whole arrangement, so excluding ``<text>`` alone
#: is not enough. ``<tr>`` is deliberately NOT here: one row is one
#: statement, the same granularity as an ``<item>`` or ``<td>``.
_NARRATIVE_CONTAINERS = frozenset({"table", "thead", "tbody", "tfoot", "list", "caption"})


def _is_narrative_cell(node: _Element, text: _Element) -> bool:
    """Whether this node inside ``text`` is one citable cell: strictly
    inside, carrying an ID, and rendering words — a ``<content>`` holding
    only ``renderMultiMedia`` reaches the record as nothing. PHI: text is
    checked for presence only, never stored or emitted.
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
    Asked upward, once per cell — asking downward costs the square of them
    (a 4,800-row table took 14 seconds).
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
    """Every cell's name mapped to the innermost cells it stands over —
    two addresses over one word (a row's name, a nested cell's name) spend
    the same claim. A wrapping cell keeps no claim outside the wrapped one.
    """
    enclosing = _enclosing_cells(named)
    cover: dict[str, set[str]] = {}
    for leaf in _leaf_cells(enclosing):
        path = list(_cell_names_from(leaf, enclosing))
        for name in path:
            cover.setdefault(name, set()).update(path[:1])
    return {name: frozenset(cells) for name, cells in cover.items()}


class _Anchors:
    """One section's narrative cells, addressable by any name written over
    them — a place spent like the pools above, but several names may lead
    to one claim, since C-CDA lets a document name a table at every level.
    """

    def __init__(self, covers: Mapping[str, frozenset[str]]) -> None:
        self._covers = covers
        self._cells: _KeyedPool[str] = _KeyedPool(
            Counter({cell for cells in covers.values() for cell in cells})
        )

    def demand(self, cited: list[str]) -> int:
        """How many cells this citation asks for; zero if it names one we
        lack. Counts the ADDRESS map, never the claims, so a section can
        settle its entries in any order.
        """
        if any(name not in self._covers for name in cited):
            return 0
        return len({cell for name in cited for cell in self._covers[name]})

    def take(self, cited: list[str]) -> bool:
        """Claim the cells ``cited`` addresses, all of them or none. A name
        this section's narrative does not define fails the whole claim
        rather than being quietly dropped.
        """
        if not cited or any(name not in self._covers for name in cited):
            return False
        return self._cells.take_all({cell for name in cited for cell in self._covers[name]})


def _section_anchors(section: _Element) -> dict[str, frozenset[str]]:
    """The narrative cells INSIDE this section's ``<text>``, by every name.
    Containment is decided by the tree, not string matching, so a cell
    reading "No" cannot answer for a citation elsewhere. PHI: only names
    are kept; text is checked for presence, never stored.
    """
    text = _find(section, "v3:text")
    if text is None:
        return {}
    return _cell_cover([node for node in text.iter() if _is_narrative_cell(node, text)])


def _cited_anchors(entry: _Element) -> list[str]:
    """The narrative cell names this entry writes, in document order.
    Repeats are left in; collapsing happens at the claim in
    :meth:`_Anchors.take`. Only a ``#``-prefixed value is a citation, per
    :func:`~.parser._inline_narrative_references`; stripped the same way
    that function strips before resolving.
    """
    return [
        value[1:]
        for reference in entry.iter(_q("reference"))
        if (value := (reference.get("value") or "").strip()).startswith("#")
    ]


def _entry_pool(record: PatientRecord) -> Counter[tuple[str, str]]:
    """Every verbatim entry the record stored, read from
    ``ccda:entries:<code>`` as the parser wrote it. Keyed by section code
    and bytes together, so two identical entries in different sections
    cannot claim each other's copy.
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
    """Every source id the record's provenance points back at. PHI:
    compared and never emitted — one member is the patient's own source
    identifier.
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
    """What KIND of statement this is: element name plus its template
    roots, sorted, RAW (never through :func:`_vocabulary`) — merging two
    unrelated kinds is the false-alarm direction :meth:`_Evidence.links`
    must avoid. PHI: compared, never emitted, like ``source_ids``.
    """
    templates = sorted(
        root for child in statement if child.tag == _q("templateId") and (root := child.get("root"))
    )
    return " ".join([str(etree.QName(statement).localname), *templates])


def _linked_kinds(root: _Element, linkable: set[str], source_ids: set[str]) -> set[str]:
    """The statement kinds this document is SHOWN to link, from its own
    record — calibration, not a table, so a new mapping moves this set
    automatically. Read from the whole document so one section can vouch
    for the same kind elsewhere.
    """
    return {
        _statement_kind(statement)
        for statement in _clinical_statements(root)
        if _own_id_roots(statement) & linkable & source_ids
    }


def _linkable_roots(root: _Element) -> set[str]:
    """Id roots that occur exactly once in the document. A shared root
    cannot say which construct an object came from (ordinary C-CDA: one
    org OID stamps the author, custodian and every entry beneath them).
    """
    counts = Counter(value for node in root.iter(_q("id")) if (value := node.get("root")))
    return {value for value, count in counts.items() if count == 1}


def _narrative_pool(record: PatientRecord) -> Counter[tuple[str | None, str | None]]:
    """The (title, text) pairs the parser actually stored, as a multiset.
    Matched by CONTENT, never by reconstructing the parser's ``#2``/``#3``
    suffix rule — a mirror that drifts would report the drift as loss.
    """
    pool: Counter[tuple[str | None, str | None]] = Counter()
    for key, value in record.patient.extensions.items():
        if key.startswith("ccda:section:") and isinstance(value, dict):
            pool[(value.get("title"), value.get("text"))] += 1
    return pool


# --- sections ----------------------------------------------------------------


def _sections(root: _Element) -> list[_Element]:
    """EVERY ``<section>`` in the document, at any depth — not the
    parser's own depth-one XPath, so a nested subsection is still visible
    here.
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
    """Whether this is a section :func:`~.parser._capture_entries` parks
    for. Asked because stored copies are keyed by section CODE, which is
    not unique (Problems Active/Resolved share 11450-4); asks the
    parser's own walk rather than restating it, for the reason
    :func:`_walked_index` gives.
    """
    if _is_own_loss_narrative(section, _section_code(section)):
        return False
    return _walked_index(section) is not None


def _walked_index(section: _Element) -> int | None:
    """Where this section sits in the parser's own anchored walk
    (``component/structuredBody/component/section``), or ``None`` if it
    is not on it. A position, not a yes/no: :func:`_hydrated_sections`
    must find this same section in a second reading by position, since
    element identity does not survive across two parses.
    """
    for position, candidate in enumerate(_walk(section.getroottree().getroot())):
        if candidate is section:
            return position
    return None


@lru_cache(maxsize=1)
def _walk(root: _Element) -> list[_Element]:
    """The parser's section walk over this document, taken once —
    re-walking per question was quadratic in section count (4x slower at
    300 sections). One document is live per reading, so a single cache
    slot suffices.
    """
    return list(_parser_sections(root))


def _entry_evidence(
    entries: list[_Element], evidence: _Evidence, code: str | None
) -> list[tuple[bool | None, bool]]:
    """Per entry, in document order: its link verdict, and whether a
    stored verbatim copy already answers for it — one pass, since both
    questions SPEND. Narrative is settled separately, in
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
    """The entries with nothing else to show, in document order. A parsed
    entry's evidence is its object; one with a stored byte copy is
    already answered for; an empty one is SOURCE_EMPTY, not a
    preservation.
    """
    return [
        entry
        for entry, (linked, copied) in zip(entries, verdicts, strict=True)
        if not linked and not copied and _has_element_child(entry)
    ]


def _settle(
    asking: list[_Element], covers: Mapping[str, frozenset[str]], widest_first: bool
) -> set[_Element]:
    """One deterministic pass: which of these entries these cells can
    honour. Order is decided by CONTENT — demand, then names — never by
    document position, so a tie always resolves the same way.
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
    """Which of these entries the section's cells can honour: tried both
    narrowest-first and widest-first, keeping whichever honours more,
    since either order alone can starve entries the other would credit.
    Never over-credits relative to an optimal assignment (rule 57).
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
    """Every entry's verdict, and how many the ledger could not reach at
    all. An entry can show a verbatim byte copy, or the document's own
    ``<reference>`` into kept narrative — never the section's shared
    prose, since C-CDA makes no promise a section's text states what its
    entries state.
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
    """Whether anything of this section's OWN narrative reached the
    record — never graded against its entries. Our own exported loss
    ledger is asked at its OWN address (the WALKED twin's position),
    since an off-walk section contributed nothing to that store.
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
    """The walked sections, read as the PARSER reads them: on a hydrated
    COPY (rule 59), since the verbatim-entry mirror depends on the
    untouched tree. Matched by position, since element identity does not
    survive into a second tree; cached one document at a time.
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
    code = (_section_code(section) or SECTION_CODE_UNKNOWN) if _parks_its_entries(section) else None
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


def aggregate(ledgers: Iterable[DocumentLedger], skipped_files: int = 0) -> CorpusLedger:
    """Merge document ledgers into one corpus reading.

    Rows merge on construct AND template set, so the same section code declared
    under a vendor's own templateId stays a separate line — that pairing is the
    thing a corpus is read for, and averaging it away hides exactly the variant
    that broke.

    ``skipped_files`` rides straight onto the corpus rather than through any
    row: it counts files the adapter never opened, so no document ledger in
    ``ledgers`` can carry it. Defaulted to 0 so every existing caller — the
    corpus generator included — reads exactly as it did before this parameter
    existed.
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
        skipped_files=skipped_files,
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


def skipped_files_clause(count: int) -> str:
    """The PHI-safe clause naming files the sniff recognised as CDA content but
    whose extension named none of the three this adapter reads.

    Shared between two tellings of the same fact so they cannot drift apart
    under one another's edits: the physician reading (:func:`_skip_lines`,
    said when OTHER documents did load) and ``pipeline.load_records``'s
    ``empty_export`` refusal (said when NOTHING else loaded — #384 round two,
    finding 2). Never a filename, which a C-CDA export names after the
    patient; a count and the three fixed extension strings are the whole of
    what may leave.
    """
    return (
        f"{_n(count, 'file', 'files')} in the export read like a C-CDA document but carried "
        "no extension this adapter reads (.xml, .ccd, .ccda)"
    )


def _skip_lines(corpus: CorpusLedger) -> list[str]:
    """The one line this reading owes about files never opened at all.

    Every other line here accounts for what a document, once opened, offered
    and kept. A file this adapter's own sniff recognised as a CDA document but
    whose extension named none of the three it reads (.xml, .ccd, .ccda,
    matched without regard to case) was never opened — #384's defect was
    exactly this count going unreported, so an export with a document under
    the wrong extension read as a complete, successful run. Said first, ahead
    of the sections a physician might otherwise read as the whole account: a
    reader who has already learned what the opened documents' SECTIONS held
    has lost the chance to learn a whole sibling document existed and was not
    one of them.
    """
    if not corpus.skipped_files:
        return []
    return [f"{skipped_files_clause(corpus.skipped_files)} — skipped, not read."]


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
    having to unlearn. :func:`_skip_lines` is the same discipline applied one
    level up the stack, before the construct rows below it even start — a
    document never opened is a bigger loss than any row inside one that was.

    PHI: every sentence is these templates' own words plus integers. Nothing a
    document stated — no name, no id, no code — can reach a line, because
    nothing here reads one.
    """
    return (
        *_skip_lines(corpus),
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
