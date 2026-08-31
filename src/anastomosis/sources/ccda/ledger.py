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
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from lxml import etree

from anastomosis.core.ccda_codes import EXT_PRIOR_LOSS_NARRATIVE
from anastomosis.core.conservation import Conservation
from anastomosis.core.model import Practitioner
from anastomosis.core.model.base import AnastBase

# The parser's own helpers, imported rather than re-spelled. Four of them encode
# knowledge this ledger must not hold a second copy of: `_PARSER` is the
# hardened XML posture every third-party document is read under, `_section_code`
# and `_is_own_loss_narrative` are the parser's own answers about a section, and
# `_text_content` and `_attr` are the exact normalizations whose output the
# parser STORED — a ledger that collapsed whitespace even slightly differently
# would compare its own spelling of a value against the parser's and conclude
# that every section, and every actor, had been dropped.
from .parser import (
    _PARSER,
    _attr,
    _find,
    _findall,
    _is_own_loss_narrative,
    _q,
    _section_code,
    _text_content,
    parse_document,
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


# --- what the record can prove -----------------------------------------------


@dataclass
class _Evidence:
    """Everything the parsed record can PROVE about a construct.

    Built once per document. ``narrative`` and ``objects`` are consumed as they
    match: two sections spelled identically are two obligations, as are two
    identical informants, and a pool that answered yes twice for one stored
    thing would credit a construct that was dropped.
    """

    source_ids: frozenset[str]
    linkable_roots: frozenset[str]
    narrative: Counter[tuple[str | None, str | None]]
    extension_keys: frozenset[str]
    #: Every canonical object the record holds, spent as it answers. One object
    #: is one parse: an object that has already stood for a construct cannot
    #: stand for a second, so N id-less constructs against M matching objects
    #: credit ``min`` and two identical informants against one object credit one.
    objects: list[AnastBase | None]

    def links(self, node: _Element) -> bool | None:
        """Whether some canonical object came from this construct.

        ``None`` is not "no": it means the construct carries no id this ledger
        could have linked by, so the question was never asked. Kept distinct
        because collapsing it into "no" would report the ledger's blind spot as
        the adapter's loss.
        """
        roots = {root for root in _id_roots(node) if root in self.linkable_roots}
        if not roots:
            return None
        return bool(roots & self.source_ids)

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
        return self._spend(rule, facts) if facts else None

    def _spend(self, rule: _Content, facts: frozenset[_Fact]) -> bool | None:
        """Take the first object stating exactly ``facts`` out of circulation."""
        for index, obj in enumerate(self.objects):
            if obj is not None and rule.recorded(obj) == facts:
                self.objects[index] = None
                return True
        return None

    def kept_narrative(self, title: str | None, text: str | None) -> bool:
        pair = (title, text)
        if self.narrative[pair] < 1:
            return False
        self.narrative[pair] -= 1
        return True

    def parked_under(self, name: str) -> bool:
        """Whether anything in ``extensions`` is namespaced to this construct.

        The adapter parks what it cannot map under a ``ccda:`` key, so this is
        how a construct that is preserved WITHOUT a typed object is recognised —
        and it reads the record rather than a table of what the parser is
        believed to do, so a future fix moves this number without touching this
        file.
        """
        prefix = f"ccda:{name}"
        return any(key == prefix or key.startswith(f"{prefix}:") for key in self.extension_keys)


def _evidence(root: _Element, record: PatientRecord) -> _Evidence:
    objects: list[AnastBase | None] = list(_provenanced(record))
    return _Evidence(
        source_ids=frozenset(_record_source_ids(record)),
        linkable_roots=frozenset(_linkable_roots(root)),
        narrative=_narrative_pool(record),
        extension_keys=frozenset(record.patient.extensions),
        objects=objects,
    )


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
    return {
        obj.provenance.source_id
        for obj in _provenanced(record)
        if obj.provenance is not None and obj.provenance.source_id is not None
    }


def _id_roots(node: _Element) -> set[str]:
    return {root for id_node in node.iter(_q("id")) if (root := id_node.get("root"))}


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


def _entry_dispositions(
    entries: list[_Element], evidence: _Evidence, narrative_kept: bool
) -> tuple[dict[Disposition, int], int]:
    counts: Counter[Disposition] = Counter()
    unlinkable = 0
    for entry in entries:
        linked = evidence.links(entry)
        unlinkable += linked is None
        counts[_entry_disposition(entry, linked, narrative_kept)] += 1
    return dict(counts), unlinkable


def _narrative_kept(
    section: _Element, evidence: _Evidence, pair: tuple[str | None, str | None]
) -> tuple[bool, bool]:
    """``(anything of this section was kept, its TEXT was kept)``.

    Two answers rather than one, because they answer for different things. A
    section whose ``<title>`` reached the record kept something of itself. Its
    ENTRIES did not: a section with entries and no ``<text>`` — an ordinary
    export shape, and one this corpus generates on purpose — stores
    ``{"title": ..., "text": None}``, and the word "Payers" is not a recovery
    of the coverage that was in the entries. Counting those entries as
    narrative-preserved on the strength of a stored title is precisely the
    flattering arithmetic this ledger exists to refuse.

    Our own exported loss ledger is the one section stored somewhere else —
    entry by entry under ``ccda:prior_loss_narrative``, so a re-export cannot
    nest generation N-1 inside generation N — so it is asked about at its own
    address rather than reported as dropped.
    """
    if _is_own_loss_narrative(section, _section_code(section)):
        kept = EXT_PRIOR_LOSS_NARRATIVE in evidence.extension_keys
        return kept, kept
    if pair == (None, None):
        return False, False
    kept = evidence.kept_narrative(*pair)
    return kept, kept and pair[1] is not None


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
    if narrative_kept:
        return Disposition.NARRATIVE_PRESERVED
    return Disposition.UNSUPPORTED


def _section_row(section: _Element, evidence: _Evidence) -> LedgerRow:
    entries = _findall(section, "v3:entry")
    pair = (
        _text_content(_find(section, "v3:title")),
        _text_content(_find(section, "v3:text")),
    )
    kept, text_kept = _narrative_kept(section, evidence, pair)
    entry_counts, unlinkable = _entry_dispositions(entries, evidence, text_kept)
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
            *_named_rows(root, _BODY_KIND, BODY_PATHS, evidence),
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
