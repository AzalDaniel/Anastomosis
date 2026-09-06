"""Oracle Health (Cerner Millennium) V500 join graph → canonical records.

Every table mapping declares the columns it consumes; every other valued
column lands in ``extensions`` under an ``oracle_ehi:`` namespace (rule 63).
Grounded in ``docs/vendor_refs/ORACLE_EHI_SCHEMA.md``, cited by section at
each mapping; where the brief says "could not determine" (§8), this raises
or routes to extensions rather than inventing vendor semantics (rule 64).
``CLINICAL_EVENT`` is the spine for vitals, problems, allergies and
documents alike (§3.2); classification keys on documented structural
columns and ``EVENT_TITLE_TEXT``, never on an undocumented ``EVENT_CD``
meaning."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from typing import Any

from anastomosis.core.codes import VITALS
from anastomosis.core.logutil import exc_tag, safe_log_id
from anastomosis.core.model import (
    AllergyCategory,
    AllergyIntolerance,
    Condition,
    DocumentArtifact,
    Encounter,
    Identifier,
    IdentifierKind,
    NoteSection,
    Observation,
    ObservationCategory,
    Patient,
    PatientRecord,
    Provenance,
    SectionKind,
)
from anastomosis.core.textutil import clean_cell, html_to_text
from anastomosis.sources._rowutil import clean_date, clean_dt, clean_str, group_by, residual

from .loader import Export, Row

__all__ = ["decode_ce_blob", "map_export"]

logger = logging.getLogger(__name__)

SOURCE = "oracle_ehi"

# Code set that resolves blob STORAGE_CD meanings (§3.2, §4.2).
_STORAGE_CODE_SET = "25"
# CDF_MEANING values that classify a remote blob handle (§4.2).
_OTG = "OTG"  # handle is a Document Imaging document id
_DICOM_SIUID = "DICOM_SIUID"  # handle is a DICOM study UID

# Row-cell helpers shared with pf_tebra/mapper.py (see sources/_rowutil.py).
_s = clean_str
_dt = clean_dt
_d = clean_date
_by = group_by


def _ext(row: Row, mapped: frozenset[str]) -> dict[str, Any]:
    """Everything the mapping didn't consume — the lossless catch-all."""
    return residual(row, mapped, SOURCE)


def _prov(table: str, source_id: str | None) -> Provenance:
    return Provenance(source_system=SOURCE, source_file=f"{table}.sql", source_id=source_id)


def _is_current(row: Row) -> bool:
    """A versioned activity row is current when ``VALID_UNTIL_DT_TM`` is
    open (§3.2). The brief doesn't document Cerner's literal open-value
    sentinel, so "absent / not a real instant" is the only grounded test
    (:func:`parse_dt` already returns ``None`` for that)."""
    return _dt(row, "VALID_UNTIL_DT_TM") is None


# --- code resolution (§3.2 dms_code_sets2.html) -------------------------------


class _CodeBook:
    """Resolves ``*_CD`` numeric keys against ``CODE_VALUE`` (§3.2), keyed
    by ``CODE_VALUE``. ``DISPLAY`` is the human label; ``CODE_SET`` +
    ``CDF_MEANING`` drive storage-location logic (§4.2). Unknown keys
    resolve to ``None`` — never a guessed string."""

    def __init__(self, rows: list[Row]) -> None:
        self._by_key: dict[str, Row] = {}
        for row in rows:
            key = _s(row, "CODE_VALUE")
            if key is not None:
                self._by_key[key] = row

    def display(self, code: str | None) -> str | None:
        row = self._by_key.get(code or "")
        return _s(row, "DISPLAY") if row else None

    def cdf_meaning(self, code: str | None) -> str | None:
        row = self._by_key.get(code or "")
        return _s(row, "CDF_MEANING") if row else None

    def code_set(self, code: str | None) -> str | None:
        row = self._by_key.get(code or "")
        return _s(row, "CODE_SET") if row else None


# --- patients (§3.2 dms_person3.html) -----------------------------------------

_PERSON_MAPPED = frozenset(
    {
        "PERSON_ID",
        "NAME_FULL_FORMATTED",
        "BIRTH_DT_TM",
        "SEX_CD",
        "DECEASED_DT_TM",
    }
)
# PERSON_ALIAS columns are not enumerated in the brief (§3.2 covers only
# PERSON/ENCOUNTER); per rule 64 the adapter surfaces every alias value as
# an OTHER identifier plus the raw row, rather than guessing which is the MRN.
_PERSON_ALIAS_JOIN = "PERSON_ID"


def _alias_identifiers(rows: list[Row]) -> tuple[list[Identifier], list[dict[str, Any]]]:
    """PERSON_ALIAS rows → OTHER identifiers + their lossless payloads.
    Without a documented MRN column, every value is kept as an
    :class:`IdentifierKind.OTHER` identifier naming its source column, and
    the whole row also rides to the patient's extensions."""
    identifiers: list[Identifier] = []
    payloads: list[dict[str, Any]] = []
    for row in rows:
        payloads.append(_ext(row, frozenset({_PERSON_ALIAS_JOIN})))
        for col, value in row.items():
            cleaned = clean_cell(value)
            if col is None or col == _PERSON_ALIAS_JOIN or cleaned is None:
                continue
            # Numeric-id columns and free text alike are kept; the kind is
            # OTHER because the brief does not let us assert "this is the MRN".
            identifiers.append(
                Identifier(kind=IdentifierKind.OTHER, value=cleaned, system=f"{SOURCE}:{col}")
            )
    return identifiers, payloads


def _map_patient(row: Row, aliases: list[Row], codes: _CodeBook) -> Patient:
    person_id = _s(row, "PERSON_ID")
    assert person_id is not None  # loader keys rows; join column required

    identifiers = [Identifier(kind=IdentifierKind.SOURCE_GUID, value=person_id, system=SOURCE)]
    alias_ids, alias_payloads = _alias_identifiers(aliases)
    identifiers.extend(alias_ids)

    extensions = _ext(row, _PERSON_MAPPED)
    if alias_payloads:
        extensions[f"{SOURCE}:PERSON_ALIAS"] = alias_payloads
    deceased = _dt(row, "DECEASED_DT_TM")
    if deceased is not None:
        extensions[f"{SOURCE}:DECEASED_DT_TM"] = deceased.isoformat()

    return Patient(
        id=person_id,
        # NAME_FULL_FORMATTED is the only formatted-name column the brief
        # documents (§3.2); structured given/family columns are not cited.
        given_name=None,
        family_name=_s(row, "NAME_FULL_FORMATTED"),
        birth_date=_d(row, "BIRTH_DT_TM"),
        sex=codes.display(_s(row, "SEX_CD")),
        status="Deceased" if deceased is not None else None,
        identifiers=identifiers,
        extensions=extensions,
        provenance=_prov("PERSON", person_id),
    )


# --- encounters (§3.2 dms_encounter17.html) -----------------------------------

_ENCOUNTER_MAPPED = frozenset(
    {
        "ENCNTR_ID",
        "PERSON_ID",
        "ENCNTR_TYPE_CD",
        "REG_DT_TM",
        "DISCH_DT_TM",
        "REASON_FOR_VISIT",
    }
)


def _map_encounter(row: Row, codes: _CodeBook) -> Encounter:
    encntr_id = _s(row, "ENCNTR_ID")
    person_id = _s(row, "PERSON_ID")
    assert encntr_id is not None and person_id is not None

    reg = _dt(row, "REG_DT_TM")
    extensions = _ext(row, _ENCOUNTER_MAPPED)
    if disch := _dt(row, "DISCH_DT_TM"):
        extensions[f"{SOURCE}:DISCH_DT_TM"] = disch.isoformat()

    return Encounter(
        id=encntr_id,
        patient_id=person_id,
        # REG_DT_TM is the registration instant; the calendar date is the
        # date of service (DateField semantics — no tz shift).
        date_of_service=reg.date() if reg is not None else None,
        chief_complaint=_s(row, "REASON_FOR_VISIT"),  # §3.2 free-text visit reason
        encounter_type=codes.display(_s(row, "ENCNTR_TYPE_CD")),
        extensions=extensions,
        provenance=_prov("ENCOUNTER", encntr_id),
    )


# --- the notes pathway (§4) ---------------------------------------------------

_CE_MAPPED = frozenset(
    {
        "EVENT_ID",
        "PERSON_ID",
        "ENCNTR_ID",
        "EVENT_CD",
        "EVENT_CLASS_CD",
        "EVENT_TITLE_TEXT",
        "RESULT_STATUS_CD",
        "EVENT_END_DT_TM",
        "SERIES_REF_NBR",
        "VALID_UNTIL_DT_TM",
    }
)


# --- discrete clinical events → their own canonical records (§3.2) ------------
#
# The fixture's RESULT_UNITS_CD keys (9001/9002) don't resolve in
# CODE_VALUE, so the raw unit code rides to extensions, not a resolved label.

# Columns every discrete-event record consumes; all others ride to that
# record's OWN ``extensions`` (the lossless catch-all, per-event — not shared).
_EVENT_MAPPED = frozenset(
    {
        "EVENT_ID",
        "PERSON_ID",
        "ENCNTR_ID",
        "EVENT_TITLE_TEXT",
        "RESULT_VAL",
        "RESULT_UNITS_CD",
        "RESULT_STATUS_CD",
        "EVENT_END_DT_TM",
        "VALID_FROM_DT_TM",
        "VALID_UNTIL_DT_TM",
    }
)

# Title-text prefixes classifying problem/allergy events — read off
# EVENT_TITLE_TEXT, the only documented free-text classifier (§3.2).
_PROBLEM_TITLE_PREFIXES = ("problem:", "problem ", "diagnosis:")
_ALLERGY_TITLE_PREFIXES = ("allergy:", "allergy ", "allergen:")

# Vital display labels (core.codes.VITALS), matched case-insensitively
# against EVENT_TITLE_TEXT: a match carries LOINC + VITAL_SIGNS; no match
# still measures (category LABORATORY) with the title as display.
_VITAL_BY_DISPLAY = {v.display.casefold(): v for v in VITALS.values()}


def _event_title(row: Row) -> str | None:
    return _s(row, "EVENT_TITLE_TEXT")


def _has_measured_result(row: Row) -> bool:
    """A vitals-/lab-shaped event: a non-sentinel ``RESULT_VAL`` plus units
    (§3.2) — the pairing the brief documents for a measured result,
    distinguishing a quantity (BP, weight) from a coded problem value."""
    return _s(row, "RESULT_VAL") is not None and _s(row, "RESULT_UNITS_CD") is not None


def decode_ce_blob(blob_contents: str | None, compression_cd: str | None) -> str | None:
    """Decode a locally stored ``CE_BLOB.BLOB_CONTENTS`` payload. No
    compression code: text, returned as-is (already latin1-decoded, §5.1).
    A compression code present: the algorithm is undocumented (§8), so this
    raises :exc:`NotImplementedError` citing §8 rather than guess. PHI-safe:
    the message names the schema gap only, never blob content."""
    text = clean_cell(blob_contents)
    if compression_cd is not None and clean_cell(compression_cd) is not None:
        raise NotImplementedError(
            "CE_BLOB.COMPRESSION_CD is set but the compression algorithm and its "
            "code-set values are undocumented in the EHI brief "
            "(ORACLE_EHI_SCHEMA.md §8). Decoding would require guessing; refusing. "
            "Supply the COMPRESSION_CD code set to implement this path."
        )
    return text


def _local_note_sections(
    event_id: str, ce_blobs: list[Row]
) -> tuple[list[NoteSection], dict[str, Any]]:
    """Assemble local document text for one event from its CE_BLOB rows
    (§4.1), concatenated in ``BLOB_SEQ_NUM`` order. A compressed blob trips
    :func:`decode_ce_blob`'s loud path; caught here, logged PHI-safely
    (event id + exception type), with the raw blob preserved in
    extensions."""
    ordered = sorted(ce_blobs, key=lambda r: _seq(r.get("BLOB_SEQ_NUM")))
    parts: list[str] = []
    undecoded: list[dict[str, Any]] = []
    for blob in ordered:
        try:
            text = decode_ce_blob(blob.get("BLOB_CONTENTS"), _s(blob, "COMPRESSION_CD"))
        except NotImplementedError as exc:
            logger.warning(
                "CE_BLOB for event %s not decoded (%s)", safe_log_id(event_id), exc_tag(exc)
            )
            # Losslessness: the UNDECODABLE payload rides along too — an empty
            # exclusion set here keeps every column, raw bytes included.
            undecoded.append(_ext(blob, frozenset()))
            continue
        if text:
            parts.append(text)
    sections: list[NoteSection] = []
    if parts:
        body = "\n".join(parts)
        sections.append(NoteSection(kind=SectionKind.NARRATIVE, html=body, text=html_to_text(body)))
    extensions: dict[str, Any] = {}
    if undecoded:
        extensions[f"{SOURCE}:CE_BLOB_undecoded"] = undecoded
    return sections, extensions


def _seq(value: str | None) -> float:
    cleaned = clean_cell(value)
    try:
        return float(cleaned) if cleaned is not None else 0.0
    except ValueError:
        return 0.0


def _remote_document(
    row: Row, person_id: str, encntr_id: str | None, codes: _CodeBook
) -> DocumentArtifact:
    """A ``CE_BLOB_RESULT`` row → a DocumentArtifact *reference* (§4.2): the
    row carries a ``BLOB_HANDLE`` plus a ``STORAGE_CD`` whose
    ``CDF_MEANING`` (code set 25) says where it points (DICOM study UID, or
    a Document Imaging document id). The body is never fetched."""
    handle = _s(row, "BLOB_HANDLE")
    storage_cd = _s(row, "STORAGE_CD")
    # STORAGE_CD meanings are only authoritative within code set 25 (§4.2).
    meaning = (
        codes.cdf_meaning(storage_cd) if codes.code_set(storage_cd) == _STORAGE_CODE_SET else None
    )

    extensions = _ext(row, frozenset({"EVENT_ID", "BLOB_HANDLE", "STORAGE_CD", "FORMAT_CD"}))
    if handle is not None:
        extensions[f"{SOURCE}:BLOB_HANDLE"] = handle
    if meaning is not None:
        extensions[f"{SOURCE}:storage_class"] = meaning

    title = None
    if meaning == _DICOM_SIUID:
        title = "DICOM study (remote)"
    elif meaning == _OTG:
        title = "Document Imaging document (remote)"

    return DocumentArtifact(
        id=handle or _s(row, "EVENT_ID") or "",
        patient_id=person_id,
        encounter_id=encntr_id,
        path=None,  # remote: never fetched (§4.2)
        mime_type="application/octet-stream",
        title=title,
        extensions=extensions,
        provenance=_prov("CE_BLOB_RESULT", _s(row, "EVENT_ID")),
    )


def _local_document(
    event_row: Row, person_id: str, local_sections: list[NoteSection]
) -> DocumentArtifact | None:
    """A DocumentArtifact for a local-text clinical-event document (§4.1):
    its narrative lives on the encounter section, but an artifact carrying
    the title is still emitted so the document is discoverable."""
    if not local_sections:
        return None
    event_id = _s(event_row, "EVENT_ID")
    return DocumentArtifact(
        id=event_id or "",
        patient_id=person_id,
        encounter_id=_s(event_row, "ENCNTR_ID"),
        path=None,
        mime_type="text/plain",
        title=_s(event_row, "EVENT_TITLE_TEXT"),
        generated_at=_dt(event_row, "EVENT_END_DT_TM"),
        provenance=_prov("CLINICAL_EVENT", event_id),
    )


# --- discrete-event record builders -------------------------------------------


def _event_observation(event_row: Row, person_id: str, encntr_id: str | None) -> Observation:
    """A discrete (non-document) event → an :class:`Observation` (§3.2). A
    measured event matching a known vital gets that LOINC + VITAL_SIGNS;
    another measured event is LABORATORY; else OTHER with the title
    verbatim as ``display`` — never a guessed clinical name."""
    title = _event_title(event_row)
    vital = _VITAL_BY_DISPLAY.get((title or "").casefold())
    measured = _has_measured_result(event_row)
    if vital is not None:
        category = ObservationCategory.VITAL_SIGNS
    elif measured:
        category = ObservationCategory.LABORATORY
    else:
        category = ObservationCategory.OTHER
    return Observation(
        id=_s(event_row, "EVENT_ID") or "",
        patient_id=person_id,
        encounter_id=encntr_id,
        category=category,
        # LOINC only when the title IS a known vital; otherwise no code is
        # asserted (the EVENT_CD value list is undocumented, §3.2 — see header).
        code=vital.loinc if vital else None,
        display=title,
        value=_s(event_row, "RESULT_VAL"),
        # RESULT_UNITS_CD is numeric; the fixture's keys don't resolve in
        # CODE_VALUE, so the raw code is the honest unit (and rides to extensions).
        unit=_s(event_row, "RESULT_UNITS_CD"),
        effective_at=_dt(event_row, "EVENT_END_DT_TM"),  # §3.2 clinically relevant time
        recorded_at=_dt(event_row, "VALID_FROM_DT_TM"),
        extensions=_ext(event_row, _EVENT_MAPPED),
        provenance=_prov("CLINICAL_EVENT", _s(event_row, "EVENT_ID")),
    )


def _event_condition(event_row: Row, person_id: str) -> Condition:
    """A problem-shaped event → a :class:`Condition` (title prefix, §3.2).
    ``RESULT_VAL`` (e.g. an ICD-10 string) is preserved as the display when
    no title remains; code typing isn't asserted (no coding column in the
    brief), so the raw value rides to extensions."""
    extensions = _ext(event_row, _EVENT_MAPPED)
    # RESULT_VAL is consumed by _EVENT_MAPPED but Condition has no value
    # field for it; preserve it explicitly so it is never dropped.
    if result_val := _s(event_row, "RESULT_VAL"):
        extensions[f"{SOURCE}:RESULT_VAL"] = result_val
    return Condition(
        id=_s(event_row, "EVENT_ID") or "",
        patient_id=person_id,
        display=_event_title(event_row) or _s(event_row, "RESULT_VAL"),
        recorded_at=_dt(event_row, "VALID_FROM_DT_TM"),
        active=True,
        extensions=extensions,
        provenance=_prov("CLINICAL_EVENT", _s(event_row, "EVENT_ID")),
    )


def _event_allergy(event_row: Row, person_id: str) -> AllergyIntolerance:
    """An allergy-shaped event → an :class:`AllergyIntolerance` (title
    prefix, §3.2). Substance is read from the title after its ``Allergy:``
    prefix; ``RESULT_VAL`` is preserved as the reaction. Category stays
    OTHER — the brief gives no allergen-category code."""
    title = _event_title(event_row) or ""
    substance = title
    lowered = title.casefold()
    for prefix in _ALLERGY_TITLE_PREFIXES:
        if lowered.startswith(prefix):
            substance = title[len(prefix) :].strip() or title
            break
    reaction = _s(event_row, "RESULT_VAL")
    return AllergyIntolerance(
        id=_s(event_row, "EVENT_ID") or "",
        patient_id=person_id,
        substance=substance or None,
        category=AllergyCategory.OTHER,  # no allergen-category code in the brief
        reactions=[reaction] if reaction is not None else [],
        extensions=_ext(event_row, _EVENT_MAPPED),
        provenance=_prov("CLINICAL_EVENT", _s(event_row, "EVENT_ID")),
    )


def _title_has_prefix(title: str | None, prefixes: tuple[str, ...]) -> bool:
    lowered = (title or "").casefold()
    return any(lowered.startswith(p) for p in prefixes)


# --- assembly -----------------------------------------------------------------


def map_export(export: Export) -> Iterator[PatientRecord]:
    """Join the loaded V500 tables into one PatientRecord per PERSON."""
    codes = _CodeBook(export.get("CODE_VALUE", []))

    aliases_by_person = _by(export.get("PERSON_ALIAS", []), "PERSON_ID")
    encounters_by_person = _by(export.get("ENCOUNTER", []), "PERSON_ID")
    events_by_person = _by(export.get("CLINICAL_EVENT", []), "PERSON_ID")
    ce_blobs_by_event = _by(export.get("CE_BLOB", []), "EVENT_ID")
    blob_results_by_event = _by(export.get("CE_BLOB_RESULT", []), "EVENT_ID")

    for person_row in export.get("PERSON", []):
        person_id = _s(person_row, "PERSON_ID")
        if person_id is None:
            continue
        patient = _map_patient(person_row, aliases_by_person.get(person_id, []), codes)

        encounters = [_map_encounter(row, codes) for row in encounters_by_person.get(person_id, [])]
        encounters_by_id = {e.id: e for e in encounters}

        documents: list[DocumentArtifact] = []
        observations: list[Observation] = []
        conditions: list[Condition] = []
        allergies: list[AllergyIntolerance] = []
        for event_row in events_by_person.get(person_id, []):
            if not _is_current(event_row):
                # Closed version: preserve the row AND its CE_BLOB body so the
                # superseded text is never silently dropped, but don't render.
                _stash_superseded(event_row, encounters_by_id, ce_blobs_by_event)
                continue
            event_id = _s(event_row, "EVENT_ID")
            if event_id is None:
                continue
            encntr_id = _s(event_row, "ENCNTR_ID")
            target = encounters_by_id.get(encntr_id or "")

            local_blobs = ce_blobs_by_event.get(event_id, [])
            remote_blobs = blob_results_by_event.get(event_id, [])
            if local_blobs or remote_blobs:
                # A *document* event: its body is the encounter narrative and
                # a DocumentArtifact; it does not also become a discrete record.
                local_sections, blob_ext = _local_note_sections(event_id, local_blobs)
                if target is not None:
                    _attach_event(target, event_row, local_sections, blob_ext)
                artifact = _local_document(event_row, person_id, local_sections)
                if artifact is not None:
                    documents.append(artifact)
                for result_row in remote_blobs:
                    documents.append(_remote_document(result_row, person_id, encntr_id, codes))
                continue

            # A discrete event → its OWN canonical record, keyed by EVENT_ID, so
            # same-named columns (RESULT_VAL, RESULT_UNITS_CD, ...) never collide.
            title = _event_title(event_row)
            if _title_has_prefix(title, _PROBLEM_TITLE_PREFIXES):
                conditions.append(_event_condition(event_row, person_id))
            elif _title_has_prefix(title, _ALLERGY_TITLE_PREFIXES):
                allergies.append(_event_allergy(event_row, person_id))
            else:
                observations.append(_event_observation(event_row, person_id, encntr_id))

        yield PatientRecord(
            patient=patient,
            encounters=encounters,
            observations=observations,
            conditions=conditions,
            allergies=allergies,
            documents=documents,
            provenance=Provenance(source_system=SOURCE, source_id=person_id),
        )


def _attach_event(
    encounter: Encounter,
    event_row: Row,
    local_sections: list[NoteSection],
    blob_ext: dict[str, Any],
) -> None:
    """Fold a document event's note text + lossless payload onto its
    encounter. Only document events (CE_BLOB/CE_BLOB_RESULT rows) reach
    here; discrete events become their own records, so same-named
    ``RESULT_*`` columns of distinct events never collide in this dict."""
    encounter.sections = [*encounter.sections, *local_sections]
    extensions = dict(encounter.extensions)
    extensions.update(_ext(event_row, _CE_MAPPED))
    extensions.update(blob_ext)
    if title := _s(event_row, "EVENT_TITLE_TEXT"):
        encounter.note_type = encounter.note_type or title
    encounter.extensions = extensions


def _stash_superseded(
    event_row: Row,
    encounters_by_id: dict[str, Encounter],
    ce_blobs_by_event: dict[str, list[Row]],
) -> None:
    """Preserve a non-current clinical-event row, and its CE_BLOB body,
    losslessly. The row alone is not enough: a superseded document's blob
    (e.g. an earlier draft) lives in CE_BLOB, not on the event row, so it
    is decoded here (or kept raw) and attached to the stashed payload."""
    encntr_id = _s(event_row, "ENCNTR_ID")
    target = encounters_by_id.get(encntr_id or "")
    if target is None:
        return
    payload = _ext(event_row, frozenset())
    event_id = _s(event_row, "EVENT_ID")
    if event_id is not None:
        body, blob_ext = _superseded_blob_body(event_id, ce_blobs_by_event.get(event_id, []))
        if body is not None:
            payload[f"{SOURCE}:CE_BLOB_body"] = body
        payload.update(blob_ext)
    extensions = dict(target.extensions)
    superseded = extensions.setdefault(f"{SOURCE}:CLINICAL_EVENT_superseded", [])
    if isinstance(superseded, list):
        superseded.append(payload)
    target.extensions = extensions


def _superseded_blob_body(event_id: str, ce_blobs: list[Row]) -> tuple[str | None, dict[str, Any]]:
    """Decode a superseded event's CE_BLOB text (§4.1), reusing the
    local-note decode path so multi-blob order, escapes, and the
    compressed-blob loud-but-caught behaviour all match the current-version
    pathway. ``None`` body when nothing decodable."""
    sections, blob_ext = _local_note_sections(event_id, ce_blobs)
    body = "\n".join(s.html for s in sections if s.html) or None
    return body, blob_ext
