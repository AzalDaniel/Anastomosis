"""C-CDA R2.1 / CCD XML → canonical PatientRecord.

The lossless rule, applied to a CDA document: every section the adapter knows
how to take apart becomes discrete canonical models, and **every section** —
structurally parsed or not — has its title and normalized narrative captured
into ``patient.extensions["ccda:section:<loinc>"]`` and its ``<entry>``
elements kept verbatim under ``patient.extensions["ccda:entries:<loinc>"]``, so
nothing on the chart is ever silently dropped (a known section whose entries
the parser cannot take apart would otherwise yield nothing at all, and its
prose is under no obligation to say what those entries say). A document
repeating a section code — split Problems (Active)/(Resolved) is ordinary
C-CDA — keeps each occurrence at its own key (``…:<loinc>#2``, ``#3``, … in
document order), so a second section can never overwrite the first. Document-level metadata rides
``patient.extensions`` too.

One section is captured differently: a 51899-3 section carrying this repo's own
export stamp is the loss ledger ``deliver/ccda_export`` wrote, and its entries
land discretely under ``patient.extensions["ccda:prior_loss_narrative"]`` so a
re-export can carry them forward deduplicated. Captured as an ordinary narrative
blob instead, generation N of an export → ingest → export loop swallowed
generation N-1's whole ledger as a single line and the document grew without
bound. An UNSTAMPED 51899-3 section is a third party's and keeps round-tripping
as ordinary foreign narrative.

The header is read as well as the body. A chart says who wrote it, who signed
it, who holds it and which visit it belongs to in ``author``, ``custodian``,
``legalAuthenticator`` and ``componentOf`` — and none of that was being read at
all, so 2,103 audited documents parsed without error and produced not one
practitioner and not one facility between them. Each participation keeps the
role the document gave it (``extensions["ccda:participation"]``): a legal
authenticator is not an informant, and a human author is not the
``assignedAuthoringDevice`` that generated the summary. A note that arrives with
no author has lost the answer to "who wrote this" while still looking complete.

Parsing is defensive by design: a missing optional element maps to ``None``, a
``nullFlavor`` on an element means "absent", but a file that is not a
``ClinicalDocument`` at all raises :exc:`ValueError` — a loud failure, never a
silent skip (the source-adapter contract). A role element naming nobody stays
nobody: CDA requires the wrapper even when it is empty, and a practitioner
invented from one would put a clinician on the chart that no document claims.

Element names here are limited to the verified C-CDA R2.1 reference; nothing
is invented. See ``tests/fixtures/ccda/README.md`` for the provenance ledger.
"""

from __future__ import annotations

import base64
import hashlib
import mimetypes
import re
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from datetime import date
from functools import cache
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from lxml import etree

from anastomosis.core.ccda_codes import (
    EXT_PRIOR_LOSS_NARRATIVE,
    EXT_SECTION_ENTRIES,
    LOINC_ALLERGIES,
    LOINC_ENCOUNTERS,
    LOINC_EXTENSIONS,
    LOINC_IMMUNIZATIONS,
    LOINC_MEDICATIONS,
    LOINC_NOTES,
    LOINC_PROBLEMS,
    LOINC_RESULTS,
    LOINC_SOCIAL,
    LOINC_VITALS,
    LOSS_NARRATIVE_GENERATION_ROOT,
    LOSS_NARRATIVE_TEMPLATE_ROOT,
    LOSS_NARRATIVE_TITLE,
    OID_ICD10,
    OID_RXNORM,
    OID_SNOMED,
    OID_SSN,
    SDTC,
    SECTION_CODE_UNKNOWN,
    TPL_SEVERITY,
    V3,
    XSI,
    first_rooted_id,
    organizer_component_source_id,
)
from anastomosis.core.hashutil import hash_and_size
from anastomosis.core.model import (
    EXT_INLINE_CONTENT,
    AllergyCategory,
    AllergyIntolerance,
    Condition,
    ContactKind,
    ContactPoint,
    DocumentArtifact,
    Encounter,
    Facility,
    Identifier,
    IdentifierKind,
    Immunization,
    MedicationStatement,
    NoteSection,
    Observation,
    ObservationCategory,
    Patient,
    PatientRecord,
    Practitioner,
    Provenance,
    SectionKind,
)
from anastomosis.core.model.patient import Address
from anastomosis.core.textutil import format_phone
from anastomosis.core.timeutil import is_zero_sentinel, parse_date, parse_dt
from anastomosis.sources.base import SourceDataError

__all__ = [
    "MAX_ARTIFACT_BYTES",
    "SOURCE",
    "UnstructuredBodyMissingError",
    "UnstructuredBodyTooLargeError",
    "parse_document",
]

SOURCE = "ccda"

# --- namespaces / OIDs (verified C-CDA R2.1 reference) -----------------------

NS = {"v3": V3, "sdtc": SDTC, "xsi": XSI}


# Allergy substance-class SNOMED codes → canonical category.
_ALLERGY_CATEGORY = {
    "416098002": AllergyCategory.DRUG,
    "414285001": AllergyCategory.FOOD,
    "426232007": AllergyCategory.ENVIRONMENT,
}

# C-CDA telecom @use → canonical phone kind.
_PHONE_USE = {
    "HP": ContactKind.PHONE_HOME,
    "HV": ContactKind.PHONE_HOME,
    "MC": ContactKind.PHONE_MOBILE,
    "WP": ContactKind.PHONE_WORK,
}

# administrativeGenderCode @code → display (when @displayName is absent).
_SEX_BY_CODE = {"F": "Female", "M": "Male", "UN": None}

# A GUID-shaped string: the synthetic-fixture prefix OR the canonical
# 8-4-4-4-12 hex form a real EHR would emit. Either is trusted as an
# already-stable id; everything else gets a deterministic uuid5.
_GUID_RE = re.compile(
    r"^(?:feedface-|00000000-)[0-9a-fA-F-]+$|"
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$",
    re.IGNORECASE,
)
_WS_RE = re.compile(r"\s+")

# --- small element helpers ---------------------------------------------------

_Element = etree._Element


def _q(tag: str) -> str:
    """A Clark-notation qualified name in the default v3 namespace."""
    return f"{{{V3}}}{tag}"


def _find(node: _Element | None, path: str) -> _Element | None:
    return None if node is None else node.find(path, NS)


def _findall(node: _Element | None, path: str) -> list[_Element]:
    return [] if node is None else node.findall(path, NS)


def _attr(node: _Element | None, name: str) -> str | None:
    """An attribute value, treating ``nullFlavor`` on the element as absent."""
    if node is None or node.get("nullFlavor") is not None:
        return None
    value = node.get(name)
    if value is None:
        return None
    text = value.strip()
    return text or None


def _val_attr(node: _Element | None, path: str, name: str) -> str | None:
    """``@name`` of the single child at ``path`` (nullFlavor-aware)."""
    return _attr(_find(node, path), name)


def _ts(node: _Element | None, path: str) -> Any:
    """``@value`` of the element at ``path``, parsed as an aware datetime.

    A run-of-zeros ``@value`` (:func:`~anastomosis.core.timeutil.
    is_zero_sentinel`) reads as absent HERE, before ``parse_dt`` ever sees
    it — not inside ``parse_dt`` itself. ``parse_dt`` is shared with every
    row-based adapter (``sources/_rowutil.clean_dt``, read by pf_tebra and
    oracle_ehi, and the learned adapter's own ``parse_datetime`` transform
    verb): a bare "0" in a TSV cell is a value that states something, and
    reading it as absent there would go unaccounted, in none of those
    adapters' ledgers. This vendor's C-CDA-specific spelling for "no date"
    is read only by this C-CDA-specific caller.
    """
    raw = _val_attr(node, path, "value")
    return None if is_zero_sentinel(raw) else parse_dt(raw)


def _ts_date(node: _Element | None, path: str) -> Any:
    """``@value`` of the element at ``path``, parsed as a calendar date.

    Same zero-sentinel guard as :func:`_ts`, for the same reason: `parse_date`
    calls `parse_dt` under the hood and would raise on a zero run exactly as
    loudly.
    """
    raw = _val_attr(node, path, "value")
    return None if is_zero_sentinel(raw) else parse_date(raw)


def _text_content(node: _Element | None) -> str | None:
    """Normalized visible text of an element subtree (whitespace collapsed)."""
    if node is None:
        return None
    parts = [t if isinstance(t, str) else t.decode() for t in node.itertext()]
    text = _WS_RE.sub(" ", "".join(parts)).strip()
    return text or None


def _collapse(raw: str | bytes | None) -> str | None:
    """Whitespace-collapsed text, or None when there is nothing to keep."""
    if raw is None:
        return None
    text = _WS_RE.sub(" ", raw if isinstance(raw, str) else raw.decode()).strip()
    return text or None


def _prov(source_file: str, source_id: str | None) -> Provenance:
    return Provenance(source_system=SOURCE, source_file=source_file, source_id=source_id)


# --- demographics ------------------------------------------------------------


def _identifiers(patient_role: _Element) -> list[Identifier]:
    out: list[Identifier] = []
    for id_node in _findall(patient_role, "v3:id"):
        root = _attr(id_node, "root")
        extension = _attr(id_node, "extension")
        if root == OID_SSN and extension:
            out.append(Identifier(kind=IdentifierKind.SSN, value=extension))
        elif extension:
            out.append(Identifier(kind=IdentifierKind.SOURCE_GUID, value=extension, system=root))
        elif root:
            out.append(Identifier(kind=IdentifierKind.SOURCE_GUID, value=root))
    return out


def _patient_id(patient_role: _Element, source_file: str) -> str:
    """Stable canonical patient id, mirroring :func:`_encounter_id` (re-parsing
    the same document must yield the same id — nothing downstream may see it
    change). A clean source GUID is used verbatim; any other source
    identifier is hashed into a deterministic uuid5; absent any id, the file
    name is.
    """
    for ident in _identifiers(patient_role):
        if ident.kind == IdentifierKind.SOURCE_GUID:
            if _GUID_RE.match(ident.value):
                return ident.value
            return str(uuid5(NAMESPACE_URL, f"anastomosis:ccda:patient:{ident.value}"))
    return str(uuid5(NAMESPACE_URL, f"anastomosis:ccda:{source_file}:patient"))


def _telecom(patient_role: _Element) -> list[ContactPoint]:
    out: list[ContactPoint] = []
    for node in _findall(patient_role, "v3:telecom"):
        raw = _attr(node, "value")
        if raw is None:
            continue
        if raw.startswith("mailto:"):
            out.append(ContactPoint(kind=ContactKind.EMAIL, value=raw.removeprefix("mailto:")))
        elif raw.startswith("tel:"):
            phone = format_phone(raw.removeprefix("tel:"))
            if phone:
                kind = _PHONE_USE.get(_attr(node, "use") or "", ContactKind.PHONE_OTHER)
                out.append(ContactPoint(kind=kind, value=phone))
    return out


def _addresses(node_with_addresses: _Element) -> list[Address]:
    """Every ``<addr>`` on an element — a patientRole, or any role that has one."""
    out: list[Address] = []
    for node in _findall(node_with_addresses, "v3:addr"):
        address = Address(
            line1=_text_content(_find(node, "v3:streetAddressLine")),
            city=_text_content(_find(node, "v3:city")),
            state=_text_content(_find(node, "v3:state")),
            postal_code=_text_content(_find(node, "v3:postalCode")),
        )
        if any(address.model_dump().values()):
            out.append(address)
    return out


def _race(patient: _Element) -> list[str]:
    out: list[str] = []
    for tag in (_q("raceCode"), f"{{{SDTC}}}raceCode"):
        for node in patient.findall(tag):
            display = _attr(node, "displayName")
            if display and display not in out:
                out.append(display)
    return out


def _ethnicity(patient: _Element) -> list[str]:
    out: list[str] = []
    for node in _findall(patient, "v3:ethnicGroupCode"):
        display = _attr(node, "displayName")
        if display and display not in out:
            out.append(display)
    return out


def _patient(clinical_doc: _Element, source_file: str, doc_meta: dict[str, Any]) -> Patient:
    patient_role = _find(clinical_doc, "v3:recordTarget/v3:patientRole")
    if patient_role is None:
        raise ValueError("C-CDA recordTarget/patientRole is missing")
    patient = _find(patient_role, "v3:patient")
    if patient is None:
        raise ValueError("C-CDA patientRole/patient is missing")

    name = _find(patient, "v3:name")
    givens = [g for n in _findall(name, "v3:given") if (g := _text_content(n))]
    gender = _find(patient, "v3:administrativeGenderCode")
    sex = _attr(gender, "displayName")
    if sex is None and gender is not None:
        # nullFlavor="OTH" carries the source spelling in originalText — that is
        # where an unmapped gender string lives, so read it before the code.
        sex = _text_content(_find(gender, "v3:originalText")) or _SEX_BY_CODE.get(
            _attr(gender, "code") or ""
        )

    source_id = next(
        (i.value for i in _identifiers(patient_role) if i.kind == IdentifierKind.SSN), None
    )
    return Patient(
        id=_patient_id(patient_role, source_file),
        given_name=givens[0] if givens else None,
        middle_name=" ".join(givens[1:]) or None if len(givens) > 1 else None,
        family_name=_text_content(_find(name, "v3:family")),
        suffix=_text_content(_find(name, "v3:suffix")),
        birth_date=_ts_date(patient, "v3:birthTime"),
        sex=sex,
        marital_status=_val_attr(patient, "v3:maritalStatusCode", "displayName"),
        race=_race(patient),
        ethnicity=_ethnicity(patient),
        language=_val_attr(patient, "v3:languageCommunication/v3:languageCode", "code"),
        identifiers=_identifiers(patient_role),
        telecom=_telecom(patient_role),
        addresses=_addresses(patient_role),
        extensions=doc_meta,
        provenance=_prov(source_file, source_id),
    )


# --- section dispatch --------------------------------------------------------


def _section_code(section: _Element) -> str | None:
    return _val_attr(section, "v3:code", "code")


def _sections(clinical_doc: _Element) -> list[_Element]:
    return _findall(clinical_doc, "v3:component/v3:structuredBody/v3:component/v3:section")


def _entries(section: _Element) -> list[_Element]:
    return _findall(section, "v3:entry")


def entry_verbatim(entry: _Element) -> str:
    """One ``<entry>``, exactly as the document spells it.

    The shared vocabulary between what :func:`_capture_entries` stores and what
    the ledger later asks the record for — the same function on both sides, so
    the mirror cannot drift. lxml serialises a parsed element deterministically,
    and both sides parse the same file, so the strings compare byte-for-byte.
    ``with_tail=False`` because the whitespace after ``</entry>`` belongs to the
    section, not the entry.
    """
    return etree.tostring(entry, encoding="unicode", with_tail=False)


def free_key(extensions: dict[str, Any], key: str) -> str:
    """``key``, or its first free ``#2``, ``#3``, … variant, in document order.

    Documents legitimately repeat a section code (Problems (Active) and Problems
    (Resolved) are both 11450-4) and may carry several code-less sections — one
    stored section must never silently replace another.

    Public because the pipeline's fold (one patient's several source records
    into one chart) parks a clashing extension the same way. Two spellings of
    "keep both" would put a key somewhere the reader of the other one never
    looks, so there is one.
    """
    if key not in extensions:
        return key
    occurrence = 2
    while f"{key}#{occurrence}" in extensions:
        occurrence += 1
    return f"{key}#{occurrence}"


def _capture_narrative(record: PatientRecord, section: _Element, loinc: str | None) -> None:
    """Preserve one section's title and narrative under ``ccda:section:<loinc>``.

    Runs for EVERY section, structurally parsed or not: a structural parser
    skips an entry whose shape it does not support, and the narrative is then
    one of the two copies of what that section said — the other being its
    entries, kept verbatim beside this by :func:`_capture_entries`, since prose
    about a section is not a copy of the entries beneath it. A section with
    neither a title nor narrative text has no prose to keep and adds no key
    (sentinel discipline — absent stays absent). Mutating the model's
    extensions dict in place persists it on the patient (it is the validated
    dict object, not a fresh copy).

    The first occurrence of a repeated section code keeps the bare key, so a
    document with one section per code reads exactly as it always has.
    """
    title = _text_content(_find(section, "v3:title"))
    text = _text_content(_find(section, "v3:text"))
    extensions = record.patient.extensions
    if title is None and text is None:
        return
    key = free_key(extensions, f"ccda:section:{loinc or SECTION_CODE_UNKNOWN}")
    extensions[key] = {"title": title, "text": text}


def _capture_entries(root: _Element) -> dict[_Element, list[str]]:
    """Every section's entries, exactly as the document spells them.

    Taken in one pass over the untouched tree, before anything in this module
    rewrites it, and keyed by the section element so the section walk can
    collect its own without re-serialising a tree that has since changed.

    The shape issue #314 named: a section with ``<entry>`` children and no
    ``<text>`` reaches neither book, because the structural parser takes what
    it can and there is no narrative for the rest to fall back to. So what the
    document said is kept instead, in document order, parsed or not.

    A parsed entry's copy is redundant with its canonical object, and that
    redundancy is accepted on purpose — deciding per entry whether the parser
    consumed it would make this capture depend on the parser's reach, and the
    point of preservation is that it must not.

    EVERY section, whatever it renders. The capture used to stop at sections
    rendering no text, on the reading that prose about a section stands in for
    the entries beneath it. It does not: a C-CDA narrative is under no
    obligation to state what its entries state, and the corpus disproves it in
    its own documents — a Plan of Treatment reading "Continue lisinopril and
    recheck blood pressure in three months" carries an entry stating the coded
    value "No current problems". So the same entry was preserved or dropped by
    nothing but whether its section happened to carry prose. What made that
    limit hold for so long was the export side: the builder narrated each
    parked key into the 51899-3 loss section, which a re-ingest parked and the
    next export narrated again, so capturing every section grew the ledger
    without bound. The builder now DELIVERS these bytes as ``<entry>`` elements
    in the section carrying their code instead of narrating them, which is what
    lets this capture be complete.
    """
    captured: dict[_Element, list[str]] = {}
    for section in _sections(root):
        if entries := _entries(section):
            captured[section] = [entry_verbatim(entry) for entry in entries]
    return captured


def _store_entries(
    extensions: dict[str, Any],
    captured: dict[_Element, list[str]],
    section: _Element,
    loinc: str | None,
) -> None:
    """Park one section's captured entries under ``ccda:entries:<loinc>``.

    The stored shape is read by two other places — the ingest ledger's entry
    pool and, since the export delivers these bytes as entries rather than
    narrating them, ``deliver/ccda_export``. A section with no code of its own
    parks under :data:`SECTION_CODE_UNKNOWN`, the one bucket all three name.
    """
    entries = captured.get(section)
    if not entries:
        return
    key = free_key(extensions, f"{EXT_SECTION_ENTRIES}:{loinc or SECTION_CODE_UNKNOWN}")
    extensions[key] = entries


def _is_own_loss_narrative(section: _Element, loinc: str | None) -> bool:
    """Whether ``section`` is a loss ledger THIS repo's C-CDA exporter wrote.

    Matched on the section code plus the exporter's own stamp — never on the
    code alone: 51899-3 is a public LOINC any vendor may use, and a third
    party's section must keep round-tripping as ordinary foreign narrative.
    """
    if loinc != LOINC_EXTENSIONS:
        return False
    if any(
        _attr(node, "root") == LOSS_NARRATIVE_TEMPLATE_ROOT
        for node in _findall(section, "v3:templateId")
    ):
        return True
    return _text_content(_find(section, "v3:title")) == LOSS_NARRATIVE_TITLE


def _loss_generation(section: _Element) -> int | None:
    """The export generation stamped on a loss ledger, or ``None`` when absent
    or unreadable (sentinel discipline — the counter is provenance, not clinical
    content, so an unreadable one restarts the count rather than raising)."""
    for node in _findall(section, "v3:id"):
        if _attr(node, "root") != LOSS_NARRATIVE_GENERATION_ROOT:
            continue
        raw = _attr(node, "extension")
        if raw is None:
            continue
        try:
            return int(raw)
        except ValueError:
            # `str.isdigit()` was the guard here, and it is true for characters
            # int() refuses — a superscript "²" among them — so a crafted stamp
            # raised out of parse_document and aborted the WHOLE ingest instead
            # of this one counter. Asking int() directly is the same question
            # without the gap between the two.
            return None
    return None


def _narrative_entries(text_node: _Element | None) -> list[str]:
    """Every discrete piece of a section's narrative, in document order.

    One entry per child element — a ``<paragraph>`` as our exporter writes them,
    but equally a ``<table>`` or a ``<list>`` some other system rendered — plus
    any loose text between them.

    Paragraphs alone used to be collected, with the whole ``<text>`` kept only
    when there were NONE. A section holding one paragraph and one table
    therefore lost the table: it reached neither the carried-forward ledger nor
    the ordinary foreign-narrative key, and did not survive re-export. This is
    the section where fields with no structured slot are parked so nothing is
    dropped, so dropping part of it defeated the mechanism at the one point it
    exists to hold.
    """
    if text_node is None:
        return []
    entries: list[str] = []
    if lead := _collapse(text_node.text):
        entries.append(lead)
    for child in text_node:
        # A comment or processing instruction is not an element: lxml gives it a
        # callable tag, and itertext() raises ValueError on one. Without this a
        # comment inside <text> aborts the whole parse.
        if callable(child.tag):
            continue
        if body := _text_content(child):
            entries.append(body)
        if tail := _collapse(child.tail):
            entries.append(tail)
    return entries


def _capture_loss_narrative(record: PatientRecord, section: _Element) -> None:
    """Preserve OUR OWN loss ledger under ``ccda:prior_loss_narrative``.

    Captured as discrete entries (one per ``<paragraph>``, as the exporter wrote
    them) rather than as one ``ccda:section:51899-3`` narrative blob, so the next
    export can carry the entries forward deduplicated instead of swallowing the
    whole block as a single ever-growing line. A ledger whose narrative is not
    per-paragraph (hand-edited, or a shape from before this contract) is kept
    whole as one entry — unreadable structure must not cost the content.

    A document merged from two exports can carry two stamped ledgers; their
    entries CONCATENATE into the one key (highest generation wins) so the
    exporter dedupes a single carry-forward ledger and neither one is
    overwritten.
    """
    entries = _narrative_entries(_find(section, "v3:text"))
    if not entries:
        return
    merge_loss_narrative(record.patient.extensions, _loss_generation(section), entries)


def is_loss_ledger(value: Any) -> bool:
    """Whether ``value`` has the ``{generation, entries}`` shape this module
    writes under :data:`EXT_PRIOR_LOSS_NARRATIVE`.

    Public because two callers need to tell a real carried-forward ledger from
    an ordinary dict that happens to sit under the same key — a hand-made FHIR
    bundle may park any JSON at all there — before folding into it: this
    module's own :func:`merge_loss_narrative`, checking what it is about to
    fold INTO, and the pipeline's fold, checking what it is about to fold IN.
    """
    return (
        isinstance(value, dict)
        and isinstance(value.get("entries"), list)
        and isinstance(value.get("generation"), int | None)
    )


def merge_loss_narrative(
    extensions: dict[str, Any], generation: int | None, entries: list[str]
) -> None:
    """Fold one stamped loss ledger into whatever ``extensions`` already holds.

    Entries CONCATENATE in the order they were read and the highest generation
    wins, so neither ledger is overwritten. That keeps one document's own
    round trip — export, re-ingest, export again — down to a single
    carry-forward appendix, because the exporter dedupes prior against current
    (``_carried_forward``) before it writes the next generation. It does NOT
    dedupe across the several source records one patient's chart can now be
    merged from (:mod:`anastomosis.pipeline`'s fold): two documents that both
    already carry an identical stamped entry merge into a ledger that states it
    twice, on purpose — an entry string does not carry enough to tell "the same
    fact stamped twice" from "two distinct objects that genuinely share a
    value", so the accepted direction is to risk saying a true thing twice
    rather than ever say it zero times. An absent generation on either side
    does not reset the counter for the other.

    The accumulator is checked with :func:`is_loss_ledger`, not merely
    ``isinstance(prior, dict)``: a chart merged from several source records can
    already hold a non-ledger dict at this key from one of them (an ordinary
    extensions clash the pipeline's fold has not yet resolved), and folding
    into it as though it were a ledger would raise on the shape it does not
    have instead of leaving it for the pipeline's own clashing-key rule.

    Public because two callers fold ledgers into one key: this module, walking
    the several stamped sections of one document, and the pipeline's fold,
    merging the several source records of one patient. The rule is the same on
    both sides, so it is written once.
    """
    # Annotated (rather than left to inference): `dict[str, Any].get` without a
    # default types as `Any | None`, and `is_loss_ledger` is an ordinary bool
    # (not a TypeGuard), so mypy cannot narrow the `| None` away on its own —
    # only the isinstance this function no longer needs at runtime did that.
    prior: Any = extensions.get(EXT_PRIOR_LOSS_NARRATIVE)
    if is_loss_ledger(prior):
        prior_generation = prior["generation"]
        extensions[EXT_PRIOR_LOSS_NARRATIVE] = {
            "generation": (
                generation if prior_generation is None else max(prior_generation, generation or 0)
            ),
            "entries": [*prior["entries"], *entries],
        }
        return
    extensions[EXT_PRIOR_LOSS_NARRATIVE] = {"generation": generation, "entries": entries}


# --- problems ----------------------------------------------------------------


def _conditions(section: _Element, patient_id: str, source_file: str) -> list[Condition]:
    out: list[Condition] = []
    for entry in _entries(section):
        act = _find(entry, "v3:act")
        if act is None:
            continue
        active = _val_attr(act, "v3:statusCode", "code") == "active"
        obs = _find(act, "v3:entryRelationship/v3:observation")
        if obs is None:
            continue
        value = _find(obs, "v3:value")
        snomed = icd10 = None
        if value is not None and _attr(value, "codeSystem") == OID_SNOMED:
            snomed = _attr(value, "code")
        translation = _find(value, "v3:translation")
        if translation is not None and _attr(translation, "codeSystem") == OID_ICD10:
            icd10 = _attr(translation, "code")
        display = _attr(value, "displayName") or _text_content(_find(value, "v3:originalText"))
        out.append(
            Condition(
                patient_id=patient_id,
                snomed=snomed,
                icd10=icd10,
                display=display,
                onset=_ts_date(obs, "v3:effectiveTime/v3:low"),
                stopped=_ts_date(obs, "v3:effectiveTime/v3:high"),
                active=active,
                provenance=_prov(source_file, _val_attr(act, "v3:id", "root")),
            )
        )
    return out


# --- allergies ---------------------------------------------------------------


def _allergies(section: _Element, patient_id: str, source_file: str) -> list[AllergyIntolerance]:
    out: list[AllergyIntolerance] = []
    for entry in _entries(section):
        obs = _find(entry, "v3:act/v3:entryRelationship/v3:observation")
        if obs is None:
            continue
        value_code = _val_attr(obs, "v3:value", "code")
        category = _ALLERGY_CATEGORY.get(value_code or "", AllergyCategory.OTHER)

        allergen = _find(obs, "v3:participant/v3:participantRole/v3:playingEntity/v3:code")
        substance = _attr(allergen, "displayName")
        extensions: dict[str, Any] = {}
        if (allergen_code := _attr(allergen, "code")) is not None:
            extensions["ccda:allergen_code"] = allergen_code

        reactions: list[str] = []
        severity: str | None = None
        for rel in _findall(obs, "v3:entryRelationship"):
            inner = _find(rel, "v3:observation")
            if inner is None:
                continue
            template = _val_attr(inner, "v3:templateId", "root")
            display = _val_attr(inner, "v3:value", "displayName")
            if rel.get("typeCode") == "MFST" and display:
                reactions.append(display)
            elif template == TPL_SEVERITY:
                severity = display

        out.append(
            AllergyIntolerance(
                patient_id=patient_id,
                substance=substance,
                category=category,
                reactions=reactions,
                severity=severity,
                onset=_ts_date(_find(entry, "v3:act"), "v3:effectiveTime/v3:low"),
                extensions=extensions,
                provenance=_prov(source_file, _val_attr(obs, "v3:id", "root")),
            )
        )
    return out


# --- medications -------------------------------------------------------------


def _medications(section: _Element, patient_id: str, source_file: str) -> list[MedicationStatement]:
    out: list[MedicationStatement] = []
    for entry in _entries(section):
        admin = _find(entry, "v3:substanceAdministration")
        if admin is None:
            continue
        material = _find(
            admin, "v3:consumable/v3:manufacturedProduct/v3:manufacturedMaterial/v3:code"
        )
        period = _find(admin, "v3:effectiveTime")
        extensions: dict[str, Any] = {}
        if (dose := _val_attr(admin, "v3:doseQuantity", "value")) is not None:
            unit = _val_attr(admin, "v3:doseQuantity", "unit")
            extensions["ccda:dose"] = f"{dose} {unit}" if unit else dose
        if (route := _val_attr(admin, "v3:routeCode", "displayName")) is not None:
            extensions["ccda:route"] = route

        is_rxnorm = _attr(material, "codeSystem") == OID_RXNORM
        out.append(
            MedicationStatement(
                patient_id=patient_id,
                display_name=_attr(material, "displayName"),
                rxnorm=_attr(material, "code") if is_rxnorm else None,
                start=_ts_date(period, "v3:low"),
                stop=_ts_date(period, "v3:high"),  # nullFlavor=UNK → None
                active=_val_attr(admin, "v3:statusCode", "code") == "active",
                extensions=extensions,
                provenance=_prov(source_file, _val_attr(admin, "v3:id", "root")),
            )
        )
    return out


# --- immunizations -----------------------------------------------------------


def _immunizations(section: _Element, patient_id: str, source_file: str) -> list[Immunization]:
    out: list[Immunization] = []
    for entry in _entries(section):
        admin = _find(entry, "v3:substanceAdministration")
        if admin is None:
            continue
        material = _find(admin, "v3:consumable/v3:manufacturedProduct/v3:manufacturedMaterial")
        code = _find(material, "v3:code")
        refused = admin.get("negationInd") == "true"
        extensions: dict[str, Any] = {}
        if refused:
            extensions["ccda:negationInd"] = "true"

        out.append(
            Immunization(
                patient_id=patient_id,
                vaccine=_attr(code, "displayName"),
                administered_on=_ts_date(admin, "v3:effectiveTime"),
                lot_number=_text_content(_find(material, "v3:lotNumberText")),
                comment="Refused" if refused else None,
                extensions=extensions,
                provenance=_prov(source_file, _val_attr(admin, "v3:id", "root")),
            )
        )
    return out


# --- vitals + results --------------------------------------------------------


def _observation_value(value: _Element | None) -> tuple[str | None, str | None]:
    """``(value, unit)`` from any C-CDA result form, not just ``xsi:type="PQ"``.

    Reading only the PQ shape (``@value``/``@unit``) left every qualitative
    result empty while still producing an Observation carrying its LOINC code —
    a finalized result that says nothing, which a receiver reads as "no result"
    rather than as the Negative or Trace the document actually recorded.
    """
    if value is None:
        return None, None
    if (quantity := _attr(value, "value")) is not None:  # PQ, INT, REAL
        return quantity, _attr(value, "unit")
    if (coded := _attr(value, "displayName")) is not None:  # CD
        return coded, None
    if (interval := _interval_value(value)) is not None:  # IVL_PQ
        return interval
    # ST / ED and anything else that carries its result as element text.
    return _text_content(value), None


def _interval_value(value: _Element) -> tuple[str | None, str | None] | None:
    """``(reading, unit)`` for an IVL_PQ, or ``None`` when it is not one.

    A range with both ends reads ``low-high``; one open end reads as the end it
    has, because "under 5" is still a result and dropping the number to avoid
    picking a spelling for the missing half would lose it. ``None`` means the
    element carried no bound at all, which is the caller's cue to keep looking
    rather than to record an empty range.
    """
    low, high = _find(value, "v3:low"), _find(value, "v3:high")
    lo, hi = (_attr(end, "value") for end in (low, high))
    if not (lo or hi):
        return None
    reading = f"{lo}-{hi}" if lo and hi else (lo or hi)
    return reading, _attr(low, "unit") or _attr(high, "unit")


def _measurements(
    section: _Element,
    patient_id: str,
    category: ObservationCategory,
    organizer_path: str,
    source_file: str,
) -> list[Observation]:
    out: list[Observation] = []
    for entry in _entries(section):
        organizer = _find(entry, organizer_path)
        if organizer is None:
            continue
        organizer_id = first_rooted_id(organizer)
        components = _findall(organizer, "v3:component/v3:observation")
        for index, component in enumerate(components):
            code = _find(component, "v3:code")
            reading, unit = _observation_value(_find(component, "v3:value"))
            # A component with no id of its own is still the organizer's
            # statement, not a statement with no provenance at all — see
            # organizer_component_source_id. ``first_rooted_id`` reads every
            # ``<id>`` child in document order, not only the first, so a
            # component whose first ``<id>`` is nullFlavor and whose second
            # is rooted is read as owning that second id — the same reading
            # the builder's stated-id walk already gives it.
            component_id = first_rooted_id(component)
            source_id = component_id[0] if component_id is not None else None
            if source_id is None and organizer_id is not None:
                root, extension = organizer_id
                source_id = organizer_component_source_id(root, extension, index)
            out.append(
                Observation(
                    patient_id=patient_id,
                    category=category,
                    code=_attr(code, "code"),
                    display=_attr(code, "displayName"),
                    value=reading,
                    unit=unit,
                    effective_at=_ts(component, "v3:effectiveTime")
                    or _ts(organizer, "v3:effectiveTime"),
                    provenance=_prov(source_file, source_id),
                )
            )
    return out


# --- social history ----------------------------------------------------------


def _social_history(section: _Element, patient_id: str, source_file: str) -> list[Observation]:
    out: list[Observation] = []
    for entry in _entries(section):
        obs = _find(entry, "v3:observation")
        if obs is None or _val_attr(obs, "v3:code", "code") != "72166-2":
            continue
        out.append(
            Observation(
                patient_id=patient_id,
                category=ObservationCategory.SOCIAL_HISTORY,
                display="Tobacco use",
                value=_val_attr(obs, "v3:value", "displayName"),
                effective_at=_ts(obs, "v3:effectiveTime"),
                provenance=_prov(source_file, _val_attr(obs, "v3:id", "root")),
            )
        )
    return out


# --- encounters + notes ------------------------------------------------------


def _encounter_id(root: str | None, source_file: str, index: int) -> str:
    """Stable encounter id.

    Prefers the source's id-root when it looks like a real GUID (the
    synthetic-fixture shape, or any 8-4-4-4-12 hex pattern a vendor would
    emit). Otherwise derives a deterministic UUID from the file name and
    the encounter's positional index in the document — so re-parsing the
    same CCD yields the same encounter ids, which is what the engine's
    idempotent-skip invariant rides on.
    """
    if root and _GUID_RE.match(root):
        return root
    return str(uuid5(NAMESPACE_URL, f"anastomosis:ccda:{source_file}:encounter:{index}"))


def _encounters(section: _Element, patient_id: str, actors: _Actors) -> list[Encounter]:
    source_file = actors.source_file
    out: list[Encounter] = []
    for index, entry in enumerate(_entries(section)):
        enc = _find(entry, "v3:encounter")
        if enc is None:
            continue
        code = _find(enc, "v3:code")
        # displayName first — a genuinely coded encounter from another system
        # names itself there. A nullFlavor code has no displayName to read (_attr
        # treats the whole element as absent), and its type is in originalText.
        original_text = _text_content(_find(code, "v3:originalText"))
        encounter_type = _attr(code, "displayName") or original_text
        out.append(
            Encounter(
                id=_encounter_id(_val_attr(enc, "v3:id", "root"), source_file, index),
                patient_id=patient_id,
                date_of_service=_ts_date(enc, "v3:effectiveTime")
                or _ts_date(enc, "v3:effectiveTime/v3:low"),
                encounter_type=encounter_type,
                provider_id=_entry_performer(enc, actors),
                provenance=_prov(source_file, _val_attr(enc, "v3:id", "root")),
            )
        )
    return out


def _entry_performer(enc: _Element, actors: _Actors) -> str | None:
    """Who delivered the care this entry records.

    An encounter that names no performer cannot say which clinician the visit
    should be attributed to, which is half of what a migrated chart is FOR.
    """
    performer = _find(enc, "v3:performer")
    entity = _find(performer, _ASSIGNED_ENTITY.path)
    if performer is None or entity is None:
        return None
    return actors.add_person(performer, "performer", entity, _ASSIGNED_ENTITY)


def _note_encounters(section: _Element, patient_id: str, actors: _Actors) -> list[Encounter]:
    source_file = actors.source_file
    out: list[Encounter] = []
    for index, entry in enumerate(_entries(section)):
        act = _find(entry, "v3:act")
        if act is None:
            continue
        text = _text_content(_find(act, "v3:text"))
        # The note's own author, already read out of the document-wide author
        # pass: this is the answer to "who wrote this note", and it is the one a
        # reader of the note needs rather than the header's.
        author = _find(act, "v3:author")
        out.append(
            Encounter(
                id=_encounter_id(_val_attr(act, "v3:id", "root"), f"{source_file}:note", index),
                patient_id=patient_id,
                date_of_service=_ts_date(act, "v3:author/v3:time"),
                note_type=_val_attr(act, "v3:code", "displayName"),
                provider_id=None if author is None else actors.authors.get(author),
                sections=[NoteSection(kind=SectionKind.NARRATIVE, text=text, html=None)],
                provenance=_prov(source_file, _val_attr(act, "v3:id", "root")),
            )
        )
    return out


_ENCOUNTER_LIST_FIELDS = ("sections", "addenda", "diagnosis_ids")
_ENCOUNTER_IDENTITY_FIELDS = ("id", "patient_id", "provenance")


def _folds_together(seen: Encounter, incoming: Encounter) -> bool:
    """Whether two encounters under one id are halves of one visit.

    They are only if nothing they BOTH state disagrees. Two entries describing
    genuinely different visits — different dates, different types — happen in
    ordinary C-CDA and must stay two objects, so the archive refuses rather than
    writing one page over the other. Folding those would invent a hybrid visit
    that happened on neither day, which is the misfiling this project exists to
    prevent (see tests/unit/test_duplicate_encounter_ids.py).
    """
    for name in type(seen).model_fields:
        if name in _ENCOUNTER_IDENTITY_FIELDS or name in _ENCOUNTER_LIST_FIELDS:
            continue
        if name == "extensions":
            continue
        mine, theirs = getattr(seen, name), getattr(incoming, name)
        if mine is not None and theirs is not None and mine != theirs:
            return False
    return True


def fold_encounters_sharing_an_id(encounters: list[Encounter]) -> list[Encounter]:
    """One ``<id root>`` is one visit when the halves agree.

    A C-CDA may describe the same encounter twice: once as an entry in the
    46240-8 Encounters section and again as the Note Activity documenting it in
    34109-9. Both legitimately carry the same ``<id root>``, and this parser
    appended an Encounter for each, so a record round-tripped through our own
    exporter came back with every visit doubled and two objects sharing one id.
    Downstream that is fatal rather than untidy: ArchiveDeliverer refuses the
    patient with DeliveredNameCollision, blaming a source that did nothing wrong.

    Complementary halves fold — first non-None wins per scalar, lists
    concatenate, order preserved. Contradictory ones do not: they stay separate
    so the collision still surfaces.

    Public because the same two halves also arrive in two DOCUMENTS: an export
    holding one patient's visit summary and its note names that visit twice
    across two files, and the pipeline's fold unions their encounters. The rule
    is a property of the canonical Encounter, not of one traversal, so both
    callers run this one — but the reach across documents is only as wide as
    :func:`_encounter_id` makes it: that function honours the source's
    ``<id root>`` verbatim only when it looks like a GUID, and otherwise derives
    an id from the source FILE name and position, so a vendor OID root (the
    shape most vendors emit) still gets one id PER DOCUMENT and never folds
    here, across two documents, no matter how it agrees with itself.
    """
    folded: dict[str, Encounter] = {}
    order: list[str] = []
    kept_apart: list[Encounter] = []
    for encounter in encounters:
        seen = folded.get(encounter.id)
        if seen is None:
            folded[encounter.id] = encounter
            order.append(encounter.id)
            continue
        if not _folds_together(seen, encounter):
            kept_apart.append(encounter)
            continue
        folded[encounter.id] = seen.model_copy(update=_folded_fields(seen, encounter))
    return [folded[key] for key in order] + kept_apart


def _folded_fields(seen: Encounter, incoming: Encounter) -> dict[str, object]:
    """The fields to copy from ``incoming`` onto ``seen``, by kind.

    Three rules, and which one applies is a property of the field rather than
    of the visit: lists concatenate in the order the halves were read,
    extensions merge with the later half winning a shared key, and every other
    scalar is filled in only where the first half left a gap. Identity fields
    are excluded — the halves already agree on those, which is what let them
    fold at all.
    """
    update: dict[str, object] = {}
    for name in _ENCOUNTER_LIST_FIELDS:
        update[name] = [*(getattr(seen, name) or []), *(getattr(incoming, name) or [])]
    if extensions := incoming.extensions:
        update["extensions"] = {**(seen.extensions or {}), **extensions}
    for name in _folded_scalar_fields(type(incoming)):
        value = getattr(incoming, name)
        if getattr(seen, name) is None and value is not None:
            update[name] = value
    return update


@cache
def _folded_scalar_fields(model: type[Encounter]) -> tuple[str, ...]:
    """Every field folded by the fill-a-gap rule: not identity, list or extensions.

    Derived from the model rather than listed here, so a field added to
    Encounter is folded by default instead of being silently skipped by a
    hand-written tuple nobody remembered to extend.
    """
    handled = {*_ENCOUNTER_IDENTITY_FIELDS, *_ENCOUNTER_LIST_FIELDS, "extensions"}
    return tuple(name for name in model.model_fields if name not in handled)


# --- participations: who wrote it, who did it, and where ---------------------

# The NPI arc. An id under this root names the provider nationally and has its
# own slot on Practitioner; every other id on a role is that role's own
# identifier.
OID_NPI = "2.16.840.1.113883.4.6"


@dataclass(frozen=True)
class _Role:
    """One CDA role element, and where it keeps the person and the organization.

    CDA spells this differently under every participation — an
    ``assignedEntity`` plays an ``assignedPerson`` and represents an
    organization, an ``associatedEntity`` plays an ``associatedPerson`` and is
    scoped by one — so the spellings are written out here rather than derived
    from the participation's name. Deriving them would be a guess, and a guess
    at this seam reads a document's spouse as its author.

    ``person`` is a tuple because a document may play a person under another
    role's spelling; the element actually found is recorded on the practitioner,
    so the record says what the document said rather than what was expected.
    """

    path: str
    person: tuple[str, ...]
    organization: str | None


_ASSIGNED_ENTITY = _Role("v3:assignedEntity", ("v3:assignedPerson",), "v3:representedOrganization")
_RELATED_ENTITY = _Role("v3:relatedEntity", ("v3:relatedPerson",), None)
#: ``informationRecipient`` is the conforming spelling and is tried first;
#: ``assignedPerson`` is VENDOR TOLERANCE, kept deliberately. No C-CDA R2.1
#: document may play a person under that name here — an ``intendedRecipient``
#: plays an ``informationRecipient`` — but exporters that reuse their
#: ``assignedEntity`` writer for this one participation do emit it, and the
#: posture of this adapter is to read what exists and refuse only what it would
#: lose. Reading it costs one path and loses nothing: ``_person_element``
#: returns the element name the document actually used and it lands on the
#: practitioner as ``ccda:entity``, so a record built from the non-standard form
#: still says which form it was built from. Removing the path would turn a
#: recipient this adapter can name into a document that parsed clean and carried
#: nobody, which is the failure this whole module exists to close (#312).
#: This is tolerance on the READ side only: the corpus generator emits the
#: conforming shape (#327), so the ledger's numbers are evidence about documents
#: that could exist, and the tolerant path is exercised by a fixture that says
#: in its own name that it is vendor divergence.
_INTENDED_RECIPIENT = _Role(
    "v3:intendedRecipient",
    ("v3:informationRecipient", "v3:assignedPerson"),
    "v3:receivedOrganization",
)
_ASSOCIATED_ENTITY = _Role(
    "v3:associatedEntity", ("v3:associatedPerson",), "v3:scopingOrganization"
)
_ASSIGNED_AUTHOR = _Role("v3:assignedAuthor", ("v3:assignedPerson",), "v3:representedOrganization")

#: The participations besides ``author``: the ElementPath each is found at, and
#: the role element(s) CDA allows beneath it. One row per participation NAME, and
#: that name is what lands on the canonical object: a legalAuthenticator is not
#: an authenticator and neither is an informant, so a record that folded them
#: together could no longer say who signed the chart and who merely supplied the
#: history.
#:
#: Scope is per-row and argued, not uniform — the same argument the ingest
#: ledger's own table makes, so the two agree about what they are counting.
#: ``informant`` is read wherever it appears, because a clinical statement may
#: name who supplied it and the header's informant does not answer for that one.
#: Everything else is a DIRECT child of ``ClinicalDocument``: a ``<participant>``
#: nested inside an allergy entry is the allergen substance and is parsed as one
#: already, and a walk that reached it would make a practitioner out of a drug.
_PARTICIPATIONS: tuple[tuple[str, str, tuple[_Role, ...]], ...] = (
    ("dataEnterer", "v3:dataEnterer", (_ASSIGNED_ENTITY,)),
    ("informant", ".//v3:informant", (_ASSIGNED_ENTITY, _RELATED_ENTITY)),
    ("informationRecipient", "v3:informationRecipient", (_INTENDED_RECIPIENT,)),
    ("legalAuthenticator", "v3:legalAuthenticator", (_ASSIGNED_ENTITY,)),
    ("authenticator", "v3:authenticator", (_ASSIGNED_ENTITY,)),
    ("participant", "v3:participant", (_ASSOCIATED_ENTITY,)),
)

#: Marks the Encounter that came from ``componentOf/encompassingEncounter``.
#: Read back by :func:`_visit_candidates`, which has to tell the document's own
#: frame from an entry inside it.
EXT_ENCOMPASSING = "ccda:componentOf"


@dataclass
class _Actors:
    """The people and places one document names, gathered as they are read.

    Facilities are keyed and practitioners are not, and the asymmetry is the
    point. One organization is routinely named by several participations — in
    most exports the author's practice IS the custodian — and two Facility
    objects under one id is the collision ``ArchiveDeliverer`` refuses a whole
    patient for. One person is equally often named twice, but as two different
    answers: the clinician who wrote the note and the one who legally signed it
    are separate facts even when they are the same human, so each participation
    keeps its own Practitioner carrying its own role.

    ``authors`` maps each ``<author>`` element to the practitioner it produced,
    so a Note Activity can point at the author already read out of it instead of
    a second pass creating that person twice.
    """

    source_file: str
    practitioners: list[Practitioner] = field(default_factory=list)
    facilities: dict[str, Facility] = field(default_factory=dict)
    authors: dict[_Element, str] = field(default_factory=dict)

    def add_person(
        self, participation: _Element, participation_name: str, entity: _Element, role: _Role
    ) -> str | None:
        """Record the person a participation names; return the practitioner id.

        ``None`` when the role element names nobody. CDA requires the wrapper
        even when it is empty — this repo's own exporter writes a bare
        ``<assignedAuthor/>`` to satisfy the schema — and a Practitioner built
        from one would be an actor no document ever named.
        """
        if not _names_an_actor(entity, role):
            return None
        practitioner = _person_practitioner(
            participation,
            participation_name,
            entity,
            role,
            self.source_file,
            len(self.practitioners),
        )
        self._link_organization(practitioner, entity, role)
        self.practitioners.append(practitioner)
        return practitioner.id

    def add_device(self, author: _Element, entity: _Element) -> str:
        """Record the SYSTEM an author names; return the practitioner id."""
        practitioner = _device_practitioner(
            author, entity, self.source_file, len(self.practitioners)
        )
        self._link_organization(practitioner, entity, _ASSIGNED_AUTHOR)
        self.practitioners.append(practitioner)
        return practitioner.id

    def add_facility(self, nodes: Sequence[_Element]) -> str | None:
        """Record an organization; return the facility id it was filed under.

        ``None`` when the elements describe no place at all, for the same reason
        an empty role names nobody.
        """
        if not _describes_a_place(nodes):
            return None
        facility = _facility(nodes, self.source_file, len(self.facilities))
        seen = self.facilities.get(facility.id)
        self.facilities[facility.id] = facility if seen is None else _fill_gaps(seen, facility)
        return facility.id

    def _link_organization(self, practitioner: Practitioner, entity: _Element, role: _Role) -> None:
        """Register the organization a role is scoped by, and remember the link.

        The link rides `extensions` as the ORGANIZATION'S OWN id root rather
        than the canonical facility id: it is a fact the document stated, and it
        stays resolvable through the facility's provenance without inventing a
        field the model does not have.
        """
        if role.organization is None:
            return
        organization = _find(entity, role.organization)
        if organization is None:
            return
        self.add_facility((organization,))
        identifier = _find(organization, "v3:id")
        key = f"ccda:{role.organization.removeprefix('v3:')}"
        if (root := _attr(identifier, "root")) is not None:
            practitioner.extensions[key] = root
        # CDA II identity is the pair: extension is unique only inside root.
        # Keep the established root-valued key for compatibility and retain the
        # other half explicitly so a downstream resolver cannot join the actor
        # to a different organization under the same assigning authority.
        if (extension := _attr(identifier, "extension")) is not None:
            practitioner.extensions[f"{key}Extension"] = extension


#: What a role element can state about who took part. An informant is routinely
#: a coded relationship and nothing else ("spouse"), and a header participant an
#: id and a phone number, so a person element is not the only evidence there is.
_ACTOR_EVIDENCE = ("v3:id", "v3:code", "v3:telecom", "v3:addr", "v3:assignedAuthoringDevice")


def _names_an_actor(entity: _Element, role: _Role) -> bool:
    """Whether a role element states anything at all about who took part.

    Only a bare wrapper is nobody. CDA requires the wrapper even when it is
    empty — this repo's own exporter writes a lone ``<assignedAuthor/>`` to
    satisfy the schema — and a Practitioner built from one would be an actor no
    document ever named.
    """
    if _person_element(entity, role)[0] is not None:
        return True
    return any(_find(entity, path) is not None for path in _ACTOR_EVIDENCE)


def _describes_a_place(nodes: Sequence[_Element]) -> bool:
    """Whether organization elements state anything about a place."""
    return any(
        _first(nodes, path) is not None for path in ("v3:id", "v3:name", "v3:addr", "v3:telecom")
    )


def _participant_id(source_file: str, participation: str, index: int) -> str:
    """Stable practitioner id, derived rather than taken from the source root.

    One ``<id root>`` legitimately names two participations — the clinician who
    wrote the note and then signed it carries the same ``assignedEntity`` id
    twice — and those are two answers the record has to keep apart, so using the
    root verbatim would put two objects under one id. ``provenance.source_id``
    still names the root the document carried, which is what makes the parse
    attributable back to the element it came from.
    """
    return str(uuid5(NAMESPACE_URL, f"anastomosis:ccda:{source_file}:{participation}:{index}"))


def _facility_id(root: str | None, extension: str | None, source_file: str, index: int) -> str:
    """Stable facility id, mirroring :func:`_patient_id`.

    Keyed on the organization's complete CDA II. ``extension`` is a unique
    identifier only within ``root``; using the root alone merges different
    facilities that share an assigning authority. A root-only UUID can stay
    verbatim, while any compound identifier is deterministically namespaced.
    An organization the document left unidentified falls back to its position,
    which is the most a document that named nothing supports.
    """
    if root and extension is None and _GUID_RE.match(root):
        return root
    if root:
        compound = f"{len(root)}:{root}:{extension or ''}"
        return str(uuid5(NAMESPACE_URL, f"anastomosis:ccda:organization:{compound}"))
    return str(uuid5(NAMESPACE_URL, f"anastomosis:ccda:{source_file}:organization:{index}"))


def _first(nodes: Sequence[_Element], path: str) -> _Element | None:
    """The first match for ``path`` across ``nodes``, in the order given."""
    for node in nodes:
        found = _find(node, path)
        if found is not None:
            return found
    return None


def _every(nodes: Sequence[_Element], path: str) -> list[_Element]:
    """Every match for ``path`` across ``nodes``, in the order given."""
    return [found for node in nodes for found in _findall(node, path)]


def _name_parts(name: _Element | None) -> tuple[str | None, str | None, str | None]:
    """``(given, family, display)`` from a CDA ``<name>``.

    ``display`` is filled only when the document did NOT split the name: a name
    written as element text still says who this is, and dropping it for having
    no parts would lose the answer the whole record is here to carry.
    """
    given = next((g for node in _findall(name, "v3:given") if (g := _text_content(node))), None)
    family = _text_content(_find(name, "v3:family"))
    if given is not None or family is not None:
        return given, family, None
    return None, None, _text_content(name)


def _name_residue(name: _Element | None) -> dict[str, Any]:
    """Name parts Practitioner has no field for.

    A credential is deliberately NOT derived from a suffix: "MD" is one and
    "Jr." is not, and guessing which would print a credential the document never
    claimed.
    """
    out: dict[str, Any] = {}
    for path, key in (("v3:prefix", "ccda:prefix"), ("v3:suffix", "ccda:suffix")):
        if (value := _text_content(_find(name, path))) is not None:
            out[key] = value
    return out


def _person_element(entity: _Element, role: _Role) -> tuple[_Element | None, str | None]:
    """The person a role plays, and the element name the document used for it."""
    for path in role.person:
        found = _find(entity, path)
        if found is not None:
            return found, path.removeprefix("v3:")
    return None, None


def _npi(entity: _Element) -> str | None:
    return next(
        (
            extension
            for node in _findall(entity, "v3:id")
            if _attr(node, "root") == OID_NPI and (extension := _attr(node, "extension"))
        ),
        None,
    )


def _role_source_id(entity: _Element) -> str | None:
    """The id root a role is identified BY, or ``None`` when it carries none.

    The NPI arc is a code system, not an instance identifier: every provider in
    the country shares that root, so crediting a parse to it would attribute one
    document's object by a value that says nothing about this document. A role
    with only an NPI gets no source id — recorded as unattributable rather than
    attributed to the wrong thing.
    """
    return next(
        (
            root
            for node in _findall(entity, "v3:id")
            if (root := _attr(node, "root")) and root != OID_NPI
        ),
        None,
    )


def _residual_ids(entity: _Element, consumed: str | None) -> list[dict[str, str]]:
    """Identifiers the mapping did not consume, so a second one is not dropped."""
    out: list[dict[str, str]] = []
    for node in _findall(entity, "v3:id"):
        root, extension = _attr(node, "root"), _attr(node, "extension")
        if root == OID_NPI:
            continue
        # ``source_id`` consumes only a root. When an II also has an extension,
        # that extension remains clinical provenance and must survive here;
        # otherwise every provider under one assigning authority becomes
        # indistinguishable after ingest.
        if root is not None and root == consumed and extension is None:
            continue
        if pair := {
            key: value for key, value in (("root", root), ("extension", extension)) if value
        }:
            out.append(pair)
    return out


def _contact_residue(node: _Element) -> dict[str, Any]:
    """A role's own address and telecom, which Practitioner has no slot for.

    Kept rather than dropped: the phone number on a note's author is how a
    receiving practice reaches the clinician who wrote it.
    """
    out: dict[str, Any] = {}
    if values := [value for n in _findall(node, "v3:telecom") if (value := _attr(n, "value"))]:
        out["ccda:telecom"] = values
    if addresses := [address.model_dump(exclude_none=True) for address in _addresses(node)]:
        out["ccda:addr"] = addresses
    return out


def _role_extensions(
    participation: _Element,
    participation_name: str,
    entity: _Element,
    role: _Role,
    entity_name: str | None,
) -> dict[str, Any]:
    """Everything a participation states that no Practitioner field holds.

    Two of these are load-bearing rather than incidental. ``ccda:participation``
    is the document's own word for what this actor did, and it is the difference
    between "who signed this chart" and "who told us about it".
    ``ccda:role`` is the CDA ROLE CLASS, and CDA draws a line with it that this
    record has to keep: an ``assignedEntity`` is somebody in a healthcare-
    provider role, a ``relatedEntity`` or ``associatedEntity`` is somebody in a
    personal relationship with the patient. It is recorded separately from
    ``ccda:entity`` — which names the person or device element played — because a
    role routinely names an actor with no person element at all (an emergency
    contact given as an id and a phone number), and a downstream reader that had
    to fall back on the participation's NAME would be guessing.
    """
    out: dict[str, Any] = {
        "ccda:participation": participation_name,
        "ccda:role": role.path.removeprefix("v3:"),
    }
    if entity_name is not None:
        out["ccda:entity"] = entity_name
    for node, attribute, key in (
        (participation, "typeCode", "ccda:typeCode"),
        (entity, "classCode", "ccda:classCode"),
    ):
        if (value := node.get(attribute)) is not None:
            out[key] = value
    for path, attribute, key in (
        ("v3:time", "value", "ccda:time"),
        ("v3:signatureCode", "code", "ccda:signatureCode"),
        ("v3:functionCode", "displayName", "ccda:functionCode"),
    ):
        if (value := _val_attr(participation, path, attribute)) is not None:
            out[key] = value
    code = _find(entity, "v3:code")
    if (display := _attr(code, "displayName") or _attr(code, "code")) is not None:
        out["ccda:code"] = display
    return out


def _person_practitioner(
    participation: _Element,
    participation_name: str,
    entity: _Element,
    role: _Role,
    source_file: str,
    index: int,
) -> Practitioner:
    """The human a participation names, carrying the role it named them in."""
    person, entity_name = _person_element(entity, role)
    name = _find(person, "v3:name")
    given, family, display = _name_parts(name)
    source_id = _role_source_id(entity)
    extensions = _role_extensions(participation, participation_name, entity, role, entity_name)
    extensions |= _name_residue(name)
    extensions |= _contact_residue(entity)
    if residual := _residual_ids(entity, source_id):
        extensions["ccda:id"] = residual
    return Practitioner(
        id=_participant_id(source_file, participation_name, index),
        given_name=given,
        family_name=family,
        display_name=display,
        npi=_npi(entity),
        extensions=extensions,
        provenance=_prov(source_file, source_id),
    )


def _device_practitioner(
    author: _Element, entity: _Element, source_file: str, index: int
) -> Practitioner:
    """The system that generated a document, kept apart from the people who write one.

    A human author and an authoring device are different answers to "who wrote
    this", and a record that cannot tell them apart is worse than one admitting
    it does not know: an automated summary attributed to a clinician is a
    statement nobody made. The device keeps ``ccda:entity`` naming the element
    CDA gave it, so the distinction survives on the object rather than in the
    reader's memory of where it came from.
    """
    device = _find(entity, "v3:assignedAuthoringDevice")
    model = _text_content(_find(device, "v3:manufacturerModelName"))
    software = _text_content(_find(device, "v3:softwareName"))
    source_id = _role_source_id(entity)
    extensions = _role_extensions(
        author, "author", entity, _ASSIGNED_AUTHOR, "assignedAuthoringDevice"
    )
    extensions |= _contact_residue(entity)
    for value, key in ((model, "ccda:manufacturerModelName"), (software, "ccda:softwareName")):
        if value is not None:
            extensions[key] = value
    if residual := _residual_ids(entity, source_id):
        extensions["ccda:id"] = residual
    return Practitioner(
        id=_participant_id(source_file, "author", index),
        display_name=software or model,
        extensions=extensions,
        provenance=_prov(source_file, source_id),
    )


def _facility_contacts(telecoms: list[_Element]) -> tuple[str | None, str | None, list[str]]:
    """``(phone, fax, everything else)`` from a set of ``<telecom>`` elements.

    First of each kind wins and the rest ride `extensions` rather than
    overwriting it, because a practice with two lines has two lines.
    """
    phone = fax = None
    residue: list[str] = []
    for node in telecoms:
        raw = _attr(node, "value")
        if raw is None:
            continue
        if raw.startswith("tel:") and phone is None:
            phone = format_phone(raw.removeprefix("tel:"))
        elif raw.startswith("fax:") and fax is None:
            fax = format_phone(raw.removeprefix("fax:"))
        else:
            residue.append(raw)
    return phone, fax, residue


def _facility(nodes: Sequence[_Element], source_file: str, index: int) -> Facility:
    """A practice location from the organization element(s) that describe it.

    Several elements rather than one because C-CDA splits a place across them:
    an ``encompassingEncounter``'s ``healthCareFacility`` carries the id, the
    ``location`` beneath it carries the name and address, and the
    ``serviceProviderOrganization`` beside it carries the organization's. Each
    field is taken from the first element that states it, in the order CDA
    nests them, so a document that fills either half is read the same way.
    """
    identifier = _first(nodes, "v3:id")
    root = _attr(identifier, "root")
    extension = _attr(identifier, "extension")
    address = _first(nodes, "v3:addr")
    lines = [
        line for node in _findall(address, "v3:streetAddressLine") if (line := _text_content(node))
    ]
    phone, fax, residue = _facility_contacts(_every(nodes, "v3:telecom"))
    extensions: dict[str, Any] = {}
    if extension is not None:
        extensions["ccda:id"] = [
            {key: value for key, value in (("root", root), ("extension", extension)) if value}
        ]
    if residue:
        extensions["ccda:telecom"] = residue
    if len(lines) > 2:
        extensions["ccda:streetAddressLine"] = lines[2:]
    return Facility(
        id=_facility_id(root, extension, source_file, index),
        name=_text_content(_first(nodes, "v3:name")),
        address_line1=lines[0] if lines else None,
        address_line2=lines[1] if len(lines) > 1 else None,
        city=_text_content(_find(address, "v3:city")),
        state=_text_content(_find(address, "v3:state")),
        postal_code=_text_content(_find(address, "v3:postalCode")),
        phone=phone,
        fax=fax,
        extensions=extensions,
        provenance=_prov(source_file, root),
    )


_FACILITY_SKIP = frozenset({"id", "provenance", "extensions"})


def _merge_facility_extensions(seen: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    """Merge lossless tails without choosing a winner for conflicting facts."""
    merged = dict(seen)
    for key, value in incoming.items():
        previous = merged.get(key)
        if previous is None:
            merged[key] = value
        elif previous == value:
            continue
        elif isinstance(previous, list) and isinstance(value, list):
            merged[key] = [*previous, *(item for item in value if item not in previous)]
        else:
            raise ValueError("C-CDA organization identifier has conflicting extension facts")
    return merged


def _mergeable(seen: Facility, incoming: Facility) -> Iterator[tuple[str, Any, Any]]:
    """Each field the merge may consider, with both namings' values beside it.

    Identity, provenance and the lossless tail are excluded: the first two are
    what established these are the same organization, and the third merges by
    its own rule rather than field by field.
    """
    for name in type(seen).model_fields:
        if name not in _FACILITY_SKIP:
            yield name, getattr(seen, name), getattr(incoming, name)


def _stated(seen: Facility, incoming: Facility) -> Iterator[tuple[str, Any, Any]]:
    """Fields both namings filled in — the only ones that can disagree."""
    return (
        (name, old, new)
        for name, old, new in _mergeable(seen, incoming)
        if old is not None and new is not None
    )


def _unstated(seen: Facility, incoming: Facility) -> Iterator[tuple[str, Any, Any]]:
    """Fields the first naming left empty and the second can fill."""
    return (
        (name, old, new)
        for name, old, new in _mergeable(seen, incoming)
        if old is None and new is not None
    )


def _fill_gaps(seen: Facility, incoming: Facility) -> Facility:
    """One organization named twice is one facility; the second naming fills gaps.

    Neither naming is more authoritative than the other — the author's header
    and the custodian's block describe the same practice — so nothing already
    stated is overwritten and only what was missing is filled. That is the same
    rule two halves of one encounter fold by, for the same reason: writing one
    over the other would silently pick a winner.
    """
    conflicts = [name for name, old, new in _stated(seen, incoming) if old != new]
    if conflicts:
        # Field names/counts only: neither organization value reaches the error.
        raise ValueError(
            "C-CDA organization identifier is reused with conflicting facility fields "
            f"({len(conflicts)} fields)"
        )
    update: dict[str, object] = {name: new for name, _old, new in _unstated(seen, incoming)}
    if incoming.extensions:
        update["extensions"] = _merge_facility_extensions(seen.extensions, incoming.extensions)
    return seen.model_copy(update=update) if update else seen


def _authors(root: _Element, actors: _Actors) -> None:
    """Every ``<author>`` in the document, header and note alike.

    Document-wide rather than header-only because the clinician who wrote a note
    is as much the answer to "who wrote this" as the one in the header — and for
    a reader of that note, it is the ONLY answer that helps. An
    ``assignedAuthoringDevice`` takes the other branch: it is a system, not a
    person, and the two must not arrive looking alike.
    """
    for author in root.iter(_q("author")):
        entity = _find(author, _ASSIGNED_AUTHOR.path)
        if entity is None:
            continue
        if _find(entity, "v3:assignedAuthoringDevice") is not None:
            actors.authors[author] = actors.add_device(author, entity)
            continue
        if (actor := actors.add_person(author, "author", entity, _ASSIGNED_AUTHOR)) is not None:
            actors.authors[author] = actor


def _named_participations(root: _Element, actors: _Actors) -> None:
    """The named actors besides the authors, each keeping the role it carried."""
    for name, path, roles in _PARTICIPATIONS:
        for participation in _findall(root, path):
            for role in roles:
                entity = _find(participation, role.path)
                if entity is not None:
                    actors.add_person(participation, name, entity, role)


def _custodians(root: _Element, actors: _Actors) -> None:
    """The organization holding the record.

    A custodian names no person at all — the element under it is an
    organization — so it becomes a facility and nothing pretends a human
    custodied the chart.
    """
    for custodian in _findall(root, "v3:custodian"):
        organization = _find(custodian, "v3:assignedCustodian/v3:representedCustodianOrganization")
        if organization is not None:
            actors.add_facility((organization,))


def _service_events(root: _Element, actors: _Actors, record: PatientRecord) -> None:
    """The episode of care the document is about, and who delivered it.

    The performer becomes a Practitioner — that is the answer to "who provided
    this care". The serviceEvent itself does NOT become an Encounter: its
    ``effectiveTime`` is the care-provision PERIOD, routinely years wide on a
    CCD, and charting its low bound as a date of service would invent a visit on
    a day nothing happened. So its own facts are preserved under
    ``patient.extensions`` instead, where they are recoverable without claiming
    to be a visit.
    """
    events: list[dict[str, Any]] = []
    for event in _findall(root, "v3:documentationOf/v3:serviceEvent"):
        for performer in _findall(event, "v3:performer"):
            entity = _find(performer, _ASSIGNED_ENTITY.path)
            if entity is not None:
                actors.add_person(performer, "performer", entity, _ASSIGNED_ENTITY)
        if facts := _service_event_facts(event):
            events.append(facts)
    if events:
        record.patient.extensions["ccda:serviceEvent"] = events


def _service_event_facts(event: _Element) -> dict[str, Any]:
    """What a service event states about itself, as plain data.

    Only the values the document actually carried: an absent key and a key
    holding ``None`` say different things, and this is the one copy of these
    facts the record keeps.
    """
    period = _find(event, "v3:effectiveTime")
    facts = {
        "id": _val_attr(event, "v3:id", "root"),
        "classCode": event.get("classCode"),
        "code": _val_attr(event, "v3:code", "code"),
        "display": _val_attr(event, "v3:code", "displayName"),
        "low": _attr(period, "value") or _val_attr(period, "v3:low", "value"),
        "high": _val_attr(period, "v3:high", "value"),
    }
    return {key: value for key, value in facts.items() if value is not None}


def _encompassing_encounters(root: _Element, patient_id: str, actors: _Actors) -> list[Encounter]:
    """The visit the document itself is about.

    A Progress Note routinely says which encounter it documents HERE and nowhere
    else — such a document has no Encounters section at all — so a reader that
    looks only at 46240-8 sees a note attached to no visit, which is the shape
    an unattributed chart arrives in.
    """
    source_file = actors.source_file
    out: list[Encounter] = []
    for index, enc in enumerate(_findall(root, "v3:componentOf/v3:encompassingEncounter")):
        code = _find(enc, "v3:code")
        root_id = _val_attr(enc, "v3:id", "root")
        _encounter_participants(enc, actors)
        out.append(
            Encounter(
                id=_encounter_id(root_id, f"{source_file}:encompassing", index),
                patient_id=patient_id,
                date_of_service=_ts_date(enc, "v3:effectiveTime")
                or _ts_date(enc, "v3:effectiveTime/v3:low"),
                encounter_type=_attr(code, "displayName")
                or _text_content(_find(code, "v3:originalText")),
                provider_id=_responsible_party(enc, actors),
                facility_id=_encounter_facility(enc, actors),
                extensions={EXT_ENCOMPASSING: "encompassingEncounter"},
                provenance=_prov(source_file, root_id),
            )
        )
    return out


def _responsible_party(enc: _Element, actors: _Actors) -> str | None:
    """The clinician the visit was under — the encounter's own provider."""
    responsible = _find(enc, "v3:responsibleParty")
    party = _find(responsible, _ASSIGNED_ENTITY.path)
    if responsible is None or party is None:
        return None
    return actors.add_person(responsible, "responsibleParty", party, _ASSIGNED_ENTITY)


def _encounter_participants(enc: _Element, actors: _Actors) -> None:
    """Everyone else the visit names — the attender, the admitter, the referrer.

    Encounter has one provider slot and these are not it, so they are recorded
    as the participations they are rather than competing for that field.
    """
    for participant in _findall(enc, "v3:encounterParticipant"):
        entity = _find(participant, _ASSIGNED_ENTITY.path)
        if entity is not None:
            actors.add_person(participant, "encounterParticipant", entity, _ASSIGNED_ENTITY)


def _encounter_facility(enc: _Element, actors: _Actors) -> str | None:
    """Where the visit happened, from the encounter's own location."""
    facility = _find(enc, "v3:location/v3:healthCareFacility")
    if facility is None:
        return None
    nodes = [facility]
    nodes += [
        node
        for path in ("v3:location", "v3:serviceProviderOrganization")
        if (node := _find(facility, path)) is not None
    ]
    return actors.add_facility(tuple(nodes))


def _participations(
    root: _Element, patient_id: str, actors: _Actors, record: PatientRecord
) -> None:
    """Everyone and everywhere the document names, into the record.

    The C-CDA header is where a chart says who wrote it, who signed it, who
    holds it and which visit it belongs to, and none of it was being read: 2,103
    audited documents parsed without error and produced not one practitioner and
    not one facility between them. A note that arrives with no author has lost
    the answer to "who wrote this" while still looking complete, which is the
    worst shape a surviving record can take.
    """
    _authors(root, actors)
    _named_participations(root, actors)
    _custodians(root, actors)
    _service_events(root, actors, record)
    record.encounters += _encompassing_encounters(root, patient_id, actors)


# --- the unstructured body ---------------------------------------------------

#: The largest artifact this adapter carries out of one document, decoded.
#:
#: An Unstructured Document's body is a whole scanned chart, and a base64 one is
#: resident twice on the way in — as the source's characters and as the bytes it
#: decodes to. So the ceiling is DECLARED here rather than discovered at
#: whatever size this machine happens to die at, which is a limit nobody can
#: read, reproduce, or raise. 32 MiB admits the scans real exports carry (a few
#: hundred colour pages) and refuses the pathological one loudly, because the
#: alternative — carrying the first 32 MiB of a clinical document — is a chart
#: that looks complete and is not.
MAX_ARTIFACT_BYTES = 32 * 1024 * 1024

#: What a body that declares no ``@mediaType`` is recorded as: "some bytes, no
#: idea what kind" — the same answer ``pf_tebra`` gives a row that states no
#: type. Never a guess at the real one: an artifact announced as a PDF it may
#: not be is worse than one that admits the document said nothing.
UNDECLARED_MEDIA_TYPE = "application/octet-stream"

#: Where the ``<nonXMLBody>``'s own declarations ride, so an attribute this
#: adapter has no field for is preserved rather than read and discarded.
EXT_NON_XML_BODY = "ccda:nonXMLBody"

#: What happened to this chart, in words, for whoever reads the record — the
#: export's loss narrative, the preserved-fields section, a physician opening
#: the bundle. Constant text: it describes the SHAPE of an Unstructured
#: Document, and nothing in it comes off the document it describes.
UNSTRUCTURED_BODY_NOTE = (
    "This document's entire clinical content is one embedded artifact — a scan, "
    "a fax, or another non-XML file — carried as an attachment beside the chart "
    "and named by this document's path. The source held no coded clinical data: "
    "no problems, medications, allergies, immunizations, results or notes were "
    "available to migrate as data, so the attachment is the chart."
)

_EMBEDDED = "embedded artifact"
_REFERENCED = "referenced artifact"


class UnstructuredBodyMissingError(SourceDataError):
    """An Unstructured Document's whole content is a file the export lacks.

    Nothing else is on that chart, so carrying the document anyway would hand
    the operator a patient with a name and no record — the silent total loss
    this adapter exists to make impossible. The run refuses instead.
    """


class UnstructuredBodyTooLargeError(SourceDataError):
    """An artifact over :data:`MAX_ARTIFACT_BYTES`.

    Refused whole rather than truncated: half a scanned discharge summary is
    not a smaller version of that summary, it is a document whose second half
    silently does not exist.
    """


def _within_ceiling(size: int, what: str) -> None:
    """Refuse an artifact this adapter will not carry, in bytes and limits only."""
    if size > MAX_ARTIFACT_BYTES:
        raise UnstructuredBodyTooLargeError(
            f"a C-CDA Unstructured Document's {what} is {size} bytes, over this "
            f"adapter's declared {MAX_ARTIFACT_BYTES}-byte ceiling; refusing to carry it "
            "rather than truncating a clinical document to fit. Nothing else is on that "
            "chart, so the run stops instead of delivering the patient without it."
        )


def _resolved_reference(reference: str, document: Path) -> Path:
    """The file a ``nonXMLBody`` reference names, beside the document itself.

    The value is a third party's word about the filesystem, so it is resolved
    and then checked back against the directory it was resolved in: a ``../`` in
    someone else's document must not make this adapter read a file the operator
    never pointed at.

    A value that is not a relative filename at all — an absolute path, a URL, a
    ``#`` fragment — resolves to nothing beside the document and is refused by
    the same check, which is the right answer for all three: this parser reads
    the export the operator pointed at and fetches nothing.

    PHI: the refusal names no filename. A C-CDA export names its files after the
    patient, so the reference value is a patient-derived string and the message
    says the SHAPE of what is missing instead.
    """
    directory = document.parent.resolve()
    resolved = (directory / reference).resolve()
    if not resolved.is_relative_to(directory) or not resolved.is_file():
        raise UnstructuredBodyMissingError(
            "a C-CDA Unstructured Document's entire content is a referenced file that is "
            "not beside it in the export, so this patient's chart did not resolve to "
            "anything. The run refuses rather than migrating a patient with correct "
            "demographics and nothing on their chart. The file is not named here because "
            "a C-CDA export names its files after the patient: look for the document whose "
            "<nonXMLBody><text><reference> points at a file the export does not hold."
        )
    return resolved


def _embedded_bytes(text: _Element) -> bytes:
    """The artifact a ``nonXMLBody`` carries inside itself.

    ``representation="B64"`` is what a scan arrives as; anything else means the
    element's own characters ARE the content (CDA's default for ED), which is
    how a plain-text body comes across. Base64 that does not decode raises —
    the adapter names the document by position and refuses the run — because
    the alternative is a patient whose chart quietly became zero bytes.

    The ceiling is checked against the encoded length BEFORE decoding: four
    base64 characters are three bytes, so the estimate is exact to within the
    padding, and a body that cannot be carried must not be materialized to find
    that out.
    """
    # Joined verbatim rather than through `_text_content`: that helper
    # collapses whitespace, which is right for narrative and destroys a
    # plain-text body's own line breaks.
    raw = "".join(t if isinstance(t, str) else t.decode() for t in text.itertext())
    if (text.get("representation") or "").upper() != "B64":
        plain = raw.encode("utf-8")
        _within_ceiling(len(plain), _EMBEDDED)
        return plain
    packed = "".join(raw.split())
    _within_ceiling(len(packed) // 4 * 3, _EMBEDDED)
    content = base64.b64decode(packed, validate=True)
    _within_ceiling(len(content), _EMBEDDED)
    return content


def _delivered_suffix(media_type: str | None) -> str:
    """A file extension for an embedded artifact's delivered name.

    Naming a file, not typing it: ``mime_type`` keeps whatever the document
    declared, verbatim, and this is only what the bytes are called on disk. A
    media type nothing maps to gets no suffix rather than a plausible one.
    """
    if not media_type:
        return ""
    return mimetypes.guess_extension(media_type.split(";")[0].strip()) or ""


def _non_xml_body_extensions(body: _Element, text: _Element) -> dict[str, Any]:
    """What the ``<nonXMLBody>`` declared that no ``DocumentArtifact`` field holds.

    ``@mediaType`` is the one attribute consumed into a field (``mime_type``)
    and so is not repeated here; a body that declared none is recorded as
    having declared none, because "the document said octet-stream" and "the
    document said nothing" are different facts about a chart.
    """
    declared: dict[str, Any] = {
        "note": UNSTRUCTURED_BODY_NOTE,
        "representation": text.get("representation"),
        "reference": _val_attr(text, "v3:reference", "value"),
        "languageCode": _val_attr(body, "v3:languageCode", "code"),
        "confidentialityCode": _val_attr(body, "v3:confidentialityCode", "code"),
        "classCode": body.get("classCode"),
    }
    kept = {key: value for key, value in declared.items() if value is not None}
    if text.get("mediaType") is None:
        kept["mediaType_declared"] = False
    return kept


def _artifact_content(
    text: _Element, document: Path, extensions: dict[str, Any], media_type: str | None, index: int
) -> tuple[str, str]:
    """``(delivered path, sha256)`` for the body, filling ``extensions`` as needed.

    A referenced body already exists as a file in the export, and keeps the name
    the export gave it — exactly like a ``pf_tebra`` attachment, so the pipeline
    copies it with no new machinery. An EMBEDDED body has no file anywhere, so
    its bytes travel on the artifact (:data:`EXT_INLINE_CONTENT`) and delivery
    writes them under a name derived from the artifact's own id, which is
    deterministic and carries nothing off the patient.
    """
    if (reference := _val_attr(text, "v3:reference", "value")) is not None:
        digest, size = hash_and_size(_resolved_reference(reference, document))
        _within_ceiling(size, _REFERENCED)
        return reference, digest
    content = _embedded_bytes(text)
    extensions[EXT_INLINE_CONTENT] = base64.b64encode(content).decode("ascii")
    return f"{_artifact_name(document, index)}{_delivered_suffix(media_type)}", _digest(content)


def _digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _artifact_name(document: Path, index: int) -> str:
    """The artifact's id, and so the stem of an embedded one's delivered filename.

    Derived the way every other id this parser mints is (:func:`_participant_id`,
    :func:`_facility_id`): a uuid5 over the source file, the construct and the
    position, so two runs over the same export deliver the same name and the
    name itself carries nothing readable off the patient. The position is in it
    for the same reason it is in a participant's: a document stating the
    construct twice is two artifacts, and one id would deliver the second over
    the first.
    """
    return str(uuid5(NAMESPACE_URL, f"anastomosis:ccda:{document.name}:nonXMLBody:{index}"))


def _unstructured_documents(
    root: _Element, document: Path, patient_id: str, doc_meta: dict[str, Any]
) -> list[DocumentArtifact]:
    """The chart of an Unstructured Document, as the artifact it is.

    A C-CDA Unstructured Document carries its whole clinical content as an
    embedded or referenced file under ``<nonXMLBody>`` instead of coded
    sections. The section walk cannot see it, so such a document parsed cleanly
    and produced a patient with an empty chart, and nothing in the run said so:
    1,024 of them in a 6,144-document ledger run, every one reported a success.

    Carried rather than refused. Refusing loses the patient outright; carrying
    preserves exactly what the source had. A record holding demographics plus
    one attached artifact is not a degraded chart — for a practice whose records
    were scanned, it is a faithful one.

    Returns a list, so the caller needs no branch and a document that states the
    construct more than once (CDA allows one body, an export is not obliged to
    obey) carries every one rather than the first.
    """
    return [
        artifact
        for index, body in enumerate(_findall(root, "v3:component/v3:nonXMLBody"))
        if (artifact := _artifact(body, index, document, patient_id, doc_meta)) is not None
    ]


def _artifact(
    body: _Element, index: int, document: Path, patient_id: str, doc_meta: dict[str, Any]
) -> DocumentArtifact | None:
    """One ``<nonXMLBody>`` as a :class:`DocumentArtifact`, or ``None``.

    ``None`` for a body stating no ``<text>`` at all, which is the honest answer
    for a construct that offered nothing — and the answer the ledger reads back
    as ``source_empty`` rather than as a loss.
    """
    text = _find(body, "v3:text")
    if text is None:
        return None
    media_type = text.get("mediaType")
    # Namespaced, so what the body declared stays attributable to the construct
    # it came off rather than becoming loose keys on an artifact.
    extensions: dict[str, Any] = {EXT_NON_XML_BODY: _non_xml_body_extensions(body, text)}
    path, digest = _artifact_content(text, document, extensions, media_type, index)
    return DocumentArtifact(
        id=_artifact_name(document, index),
        patient_id=patient_id,
        path=path,
        sha256=digest,
        # As DECLARED. A type this adapter normalized, corrected or guessed would
        # be this tool telling a receiving system what a scan is, which is a
        # claim only the document holding it can make.
        mime_type=media_type or UNDECLARED_MEDIA_TYPE,
        title=doc_meta.get("ccda:title"),
        extensions=extensions,
        # The document's own id: an unstructured body carries no <id> of its own
        # (CDA gives NonXMLBody none), and the document IS the artifact.
        provenance=_prov(document.name, doc_meta.get("ccda:documentId")),
    )


# --- top-level assembly ------------------------------------------------------


# Hardened XML parser: third-party clinical documents must never resolve
# external entities (XXE), fetch external DTDs over the network (SSRF), or
# expand into unbounded trees (billion-laughs / quadratic blowup). These
# flags are the OWASP-recommended posture for any XML ingest the
# application does not author itself.
_PARSER = etree.XMLParser(
    resolve_entities=False,
    no_network=True,
    load_dtd=False,
    huge_tree=False,
)


def _inline_narrative_references(root: _Element) -> None:
    """Give every ``<reference value="#id"/>`` the narrative text it points at.

    Linking a coded entry to its human-readable narrative by reference is THE
    standard C-CDA mechanism, and a ``<reference>`` element carries no text of
    its own. So a problem whose ``<originalText>`` is a reference came through
    unnamed, and a Note Activity whose ``<text>`` is a reference came through
    empty — while the words sat a few elements away in the same document.

    Resolved once per document by filling in each reference's own text, so every
    reader downstream (originalText, entry text, note bodies) sees the words
    without knowing references exist. A reference naming an id the document does
    not define is left alone rather than guessed at.
    """
    targets = {node.get("ID"): node for node in root.iter() if node.get("ID")}
    if not targets:
        return
    for reference in root.iter(_q("reference")):
        value = (reference.get("value") or "").strip()
        if not value.startswith("#"):
            continue
        # A reference carrying its own fallback text keeps it; an id the document
        # does not define resolves to None, which leaves the reference as it was.
        if not reference.text:
            reference.text = _text_content(targets.get(value[1:]))


# Categories a visit may claim. A measurement is taken AT a moment, so a visit
# on its date says something about where it belongs. Social history does not
# work that way — smoking status is a standing fact about the patient, not a
# reading from one afternoon — so it stays record-level, which is also where
# the packs read it from.
_VISIT_MEASUREMENTS = frozenset({ObservationCategory.VITAL_SIGNS, ObservationCategory.LABORATORY})


def _visit_candidates(record: PatientRecord) -> dict[date, list[Encounter]]:
    """The visits a measurement may be charted at, indexed by calendar day.

    Only an encounter carrying BOTH a date and a type is a candidate: a
    note-only encounter documents a visit, it is not one, and counting it would
    make every documented visit ambiguous with itself.

    A ``componentOf/encompassingEncounter`` is the document's FRAME rather than
    an entry inside it, so it supplies a candidate only for a day no entry
    claims. When a document states the visit both ways — in its header and again
    in the Encounters section — those are one visit stated twice, and admitting
    both would make the day ambiguous and strand the measurements taken on it.
    """
    entries: dict[date, list[Encounter]] = {}
    frames: dict[date, list[Encounter]] = {}
    for encounter in record.encounters:
        if encounter.date_of_service is None or encounter.encounter_type is None:
            continue
        index = frames if EXT_ENCOMPASSING in encounter.extensions else entries
        index.setdefault(encounter.date_of_service, []).append(encounter)
    for day, framed in frames.items():
        entries.setdefault(day, framed)
    return entries


def _link_measurements_to_encounters(record: PatientRecord) -> None:
    """Attach a measurement to the visit it was taken at, when exactly one visit
    claims that calendar day.

    C-CDA does not require a structural link from a Vital Signs or Results
    observation back to an Encounter activity, and the documents we see carry
    none — no ``componentOf/encompassingEncounter``, no ``entryRelationship``
    naming one — so the document's own timestamps are the only evidence there
    is. Every other source adapter fills ``encounter_id`` in; this one left it
    empty, so every observation on a chart grouped under the patient-level
    ``None`` key and both SOAP packs, which index strictly by encounter id,
    rendered no vitals at all. The QA check written to catch precisely that
    reads the same empty index, so it passed on an empty loop while the values
    were missing from the page — a silent loss with its own guard blinded by
    the same root cause.

    ONE encounter on the observation's date is evidence; two are not, and the
    measurement then stays record-level rather than being charted at a visit it
    may not belong to. Note-only encounters are not candidates: a Note Activity
    documents a visit, it is not one, and counting it would make every
    documented visit ambiguous with itself. An ``encounter_id`` a source already
    stated is never overwritten.

    Runs after the id fold, so a visit described twice — once in Encounters,
    again as the note documenting it — is one candidate here rather than two.
    """
    by_date = _visit_candidates(record)
    for observation in record.observations:
        if observation.encounter_id is not None or observation.effective_at is None:
            continue
        if observation.category not in _VISIT_MEASUREMENTS:
            continue
        # Both sides are the calendar date the document wrote: `parse_dt` keeps
        # the source's own offset and `parse_date` is that same instant's
        # `.date()`, so neither has been shifted across midnight on the way here.
        same_day = by_date.get(observation.effective_at.date(), [])
        if len(same_day) == 1:
            observation.encounter_id = same_day[0].id


# --- zero-sentinel timestamps -------------------------------------------

# Every TS shape a _ts/_ts_date call in this module reads, named the way
# _count_zero_sentinels walks it: a bare tag read straight off an entry
# (birthTime, effectiveTime, author/time), or a two-level parent-then-child
# for a TS read off an already-resolved parent — an IVL_TS's own low/high,
# where the medications reader resolves the effectiveTime node once and then
# reads its low and high children off that node, rather than re-stating the
# whole path each time. The shape read is still effectiveTime/low, regardless
# of which already-found node the call started from.
# test_every_timestamp_path_the_parser_reads_is_named_in_TS_PATHS is the
# anti-drift guard: a _ts/_ts_date call reading a path not named here (or a
# name here nothing reads any more) fails it, so the two cannot separate
# silently.
TS_PATHS = ("birthTime", "effectiveTime", "effectiveTime/low", "effectiveTime/high", "author/time")

EXT_TS_NO_INSTANT = "ccda:timestamp_named_no_instant"


def _count_zero_sentinels(root: _Element) -> dict[str, int]:
    """How many TS elements the document itself names as a run of zeros, by shape.

    Walked over the whole document independently of any `_ts`/`_ts_date` call
    site — this is what the DOCUMENT states, not what one field extractor
    happened to read, so the count does not depend on which caller's local
    variable reached the node first. A node carrying `nullFlavor` is skipped:
    that is an explicit "absent", not a zero run, and already reads as `None`
    with no help from `is_zero_sentinel`.
    """
    counts: dict[str, int] = {}
    for shape in TS_PATHS:
        xpath = ".//v3:" + "/v3:".join(shape.split("/"))
        n = sum(
            1
            for node in _findall(root, xpath)
            if node.get("nullFlavor") is None and is_zero_sentinel(node.get("value"))
        )
        if n:
            counts[shape] = n
    return counts


def _record_zero_sentinels(record: PatientRecord, root: _Element) -> None:
    """Credit a run-of-zeros TS the parser read as absent, on the record itself.

    `_ts`/`_ts_date` treat a "0" (of any length) as absent rather than raising
    `parse_dt`'s own ValueError, so the medication (or condition, encounter,
    ...) it belongs to survives with no start instead of aborting the whole
    document. That is a real loss — a start date the source named nothing
    usable for — and losslessness means it rides the record rather than
    vanishing at the parse boundary. Sets the key only when the count is
    non-empty, so an ordinary document carries no trace of a check that found
    nothing.
    """
    if counts := _count_zero_sentinels(root):
        record.patient.extensions[EXT_TS_NO_INSTANT] = counts


def parse_document(path: Path) -> PatientRecord:
    """Parse one C-CDA / CCD XML file into a :class:`PatientRecord`.

    Raises :exc:`ValueError` if the file is not a CDA ``ClinicalDocument`` —
    a loud failure, per the source-adapter contract.
    """
    tree = etree.parse(str(path), _PARSER)
    root = tree.getroot()
    if etree.QName(root).localname != "ClinicalDocument" or root.tag != _q("ClinicalDocument"):
        raise ValueError(f"{path.name}: not a C-CDA ClinicalDocument (root <{root.tag}>)")

    # Verbatim FIRST, hydration second. `_inline_narrative_references` fills a
    # <reference> element's own text in place, so an entry serialised after it
    # runs carries narrative this document does not spell at that position —
    # which made the "verbatim" copy a copy of the parser's tree rather than of
    # the file, and broke the byte-exact question the ledger asks of it. The
    # copy is taken from the document as parsed, and nothing else touches it.
    entries_by_section = _capture_entries(root)

    _inline_narrative_references(root)

    source_file = path.name
    doc_meta: dict[str, Any] = {}
    if (doc_id := _val_attr(root, "v3:id", "root")) is not None:
        doc_meta["ccda:documentId"] = doc_id
    if (effective := _val_attr(root, "v3:effectiveTime", "value")) is not None:
        doc_meta["ccda:effectiveTime"] = effective
    if (title := _text_content(_find(root, "v3:title"))) is not None:
        doc_meta["ccda:title"] = title

    patient = _patient(root, source_file, doc_meta)
    pid = patient.id
    record = PatientRecord(
        patient=patient, provenance=_prov(source_file, doc_meta.get("ccda:documentId"))
    )
    actors = _Actors(source_file=source_file)
    _participations(root, pid, actors, record)
    # Before the section walk, and unconditionally: an Unstructured Document has
    # no sections to walk, which is exactly why one used to leave here with a
    # patient and an empty chart.
    record.documents += _unstructured_documents(root, path, pid, doc_meta)

    for section in _sections(root):
        loinc = _section_code(section)
        if loinc == LOINC_PROBLEMS:
            record.conditions += _conditions(section, pid, source_file)
        elif loinc == LOINC_ALLERGIES:
            record.allergies += _allergies(section, pid, source_file)
        elif loinc == LOINC_MEDICATIONS:
            record.medications += _medications(section, pid, source_file)
        elif loinc == LOINC_IMMUNIZATIONS:
            record.immunizations += _immunizations(section, pid, source_file)
        elif loinc == LOINC_VITALS:
            record.observations += _measurements(
                section, pid, ObservationCategory.VITAL_SIGNS, "v3:organizer", source_file
            )
        elif loinc == LOINC_RESULTS:
            record.observations += _measurements(
                section, pid, ObservationCategory.LABORATORY, "v3:organizer", source_file
            )
        elif loinc == LOINC_SOCIAL:
            record.observations += _social_history(section, pid, source_file)
        elif loinc == LOINC_ENCOUNTERS:
            record.encounters += _encounters(section, pid, actors)
        elif loinc == LOINC_NOTES:
            record.encounters += _note_encounters(section, pid, actors)
        # Losslessness: the narrative and the entries are captured for every
        # section, not only the unparsed ones. The structural parsers above `continue` past an entry
        # whose shape they do not support, so a known section can yield nothing
        # while its <text> still holds the clinical statement; the duplication
        # for a fully-parsed section is the cheap side of that trade. Our own
        # loss ledger is the one exception — captured entry-by-entry so a repeat
        # export cannot nest it inside the next one.
        if _is_own_loss_narrative(section, loinc):
            _capture_loss_narrative(record, section)
        else:
            _capture_narrative(record, section, loinc)
            _store_entries(record.patient.extensions, entries_by_section, section, loinc)

    record.practitioners = actors.practitioners
    record.facilities = list(actors.facilities.values())
    _record_zero_sentinels(record, root)
    record.encounters = fold_encounters_sharing_an_id(record.encounters)
    _link_measurements_to_encounters(record)
    return record
