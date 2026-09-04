"""FHIR export/ingest tests: the round-trip IS the lossless guarantee.

Every fixture record must survive canonical → Bundle → canonical with
nothing changed (provenance excluded: it's local lineage, not exported).
"""

import base64
import copy
import json
from pathlib import Path

import pytest

import anastomosis.sources.pf_tebra  # noqa: F401 — registers the adapter
from anastomosis.core.fhir import DeliveredAttachment, from_bundle, to_bundle
from anastomosis.core.fhir.export import FhirExportError, _prune
from anastomosis.core.model import DocumentArtifact, Patient, PatientRecord, SectionKind
from anastomosis.sources import get_source

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "pf_tebra_v9"

_LIST_FIELDS = (
    "encounters",
    "observations",
    "conditions",
    "allergies",
    "medications",
    "prescriptions",
    "immunizations",
    "family_history",
    "past_medical_history",
    "advance_directives",
    "goals",
    "coverages",
    "documents",
    "practitioners",
    "facilities",
)


@pytest.fixture(scope="module")
def records() -> list[PatientRecord]:
    return list(get_source("pf-tebra").load(FIXTURE))


def _dumps(models: list) -> list[dict]:
    return [m.model_dump(mode="json", exclude={"provenance"}) for m in models]


def test_round_trip_is_lossless(records: list[PatientRecord]) -> None:
    for record in records:
        rebuilt = from_bundle(to_bundle(record))
        assert rebuilt.patient.model_dump(mode="json", exclude={"provenance"}) == (
            record.patient.model_dump(mode="json", exclude={"provenance"})
        ), f"patient mismatch for {record.patient.id}"
        for field in _LIST_FIELDS:
            assert _dumps(getattr(rebuilt, field)) == _dumps(getattr(record, field)), (
                f"{field} mismatch for {record.patient.id}"
            )


def test_uuid_ids_use_standard_urn_uuid_scheme(records: list[PatientRecord]) -> None:
    """A UUID resource id is emitted as the FHIR-standard, server-resolvable
    ``urn:uuid:`` in fullUrl and references; a non-UUID id keeps
    ``urn:anastomosis:`` so the id still round-trips. Both recover on ingest."""
    from anastomosis.core.fhir.export import _ref, _urn
    from anastomosis.core.fhir.ingest import _unref

    assert _urn("feedface-0000-0000-0000-000000000001").startswith("urn:uuid:")
    assert _urn("patient-1") == "urn:anastomosis:patient-1"  # non-UUID fallback
    # parseable-but-non-canonical (braced) → fallback, so urn:uuid stays valid
    braced = "{feedface-0000-0000-0000-000000000001}"
    assert _urn(braced) == f"urn:anastomosis:{braced}"
    for rid in ("feedface-0000-0000-0000-000000000001", "patient-1", braced):
        assert _unref(_ref(rid)) == rid  # every scheme recovers the id verbatim

    # PF/Tebra ids are UUIDs, so the live bundle uses the standard scheme, and
    # every reference still resolves to a fullUrl under it.
    bundle = to_bundle(records[0])
    full_urls = {e["fullUrl"] for e in bundle["entry"]}
    patient_full = next(
        e["fullUrl"] for e in bundle["entry"] if e["resource"]["resourceType"] == "Patient"
    )
    assert patient_full.startswith("urn:uuid:")
    encounters = [
        e["resource"] for e in bundle["entry"] if e["resource"]["resourceType"] == "Encounter"
    ]
    assert encounters and all(e["subject"]["reference"] in full_urls for e in encounters)


def test_bundle_is_standard_shaped(records: list[PatientRecord]) -> None:
    bundle = to_bundle(records[0])  # Ada Fixture
    assert bundle["resourceType"] == "Bundle" and bundle["type"] == "collection"
    by_type: dict[str, list[dict]] = {}
    for entry in bundle["entry"]:
        by_type.setdefault(entry["resource"]["resourceType"], []).append(entry["resource"])

    patient = by_type["Patient"][0]
    assert patient["birthDate"] == "1985-03-14"
    assert patient["gender"] == "female"
    assert {"system": "http://hl7.org/fhir/sid/us-ssn", "value": "900-12-3456"} in patient[
        "identifier"
    ]

    systolic = next(
        o
        for o in by_type["Observation"]
        if o["code"].get("coding", [{}])[0].get("code") == "8480-6"
    )
    assert systolic["valueQuantity"]["value"] == 118.0
    assert systolic["category"][0]["coding"][0]["code"] == "vital-signs"

    htn = next(c for c in by_type["Condition"] if c["code"]["text"] == "Essential hypertension")
    systems = {c["system"]: c["code"] for c in htn["code"]["coding"]}
    assert systems["http://hl7.org/fhir/sid/icd-10-cm"] == "I10"
    assert systems["http://www.snomed.info/sct"] == "59621000"

    penicillin = by_type["AllergyIntolerance"][0]
    assert penicillin["category"] == ["medication"]  # drug → FHIR's 'medication'
    assert penicillin["reaction"][0]["severity"] == "severe"

    # References resolve within the bundle.
    full_urls = {entry["fullUrl"] for entry in bundle["entry"]}
    encounter = by_type["Encounter"][0]
    assert encounter["subject"]["reference"] in full_urls


def test_note_documentreference_carries_readable_html(records: list[PatientRecord]) -> None:
    bundle = to_bundle(records[0])
    docrefs = [
        e["resource"]
        for e in bundle["entry"]
        if e["resource"]["resourceType"] == "DocumentReference"
    ]
    html = base64.b64decode(docrefs[0]["content"][0]["attachment"]["data"]).decode()
    assert 'data-kind="subjective"' in html
    assert "Reports good medication adherence" in html
    assert docrefs[0]["docStatus"] == "final"  # signed note


def test_html_fallback_when_json_rendition_absent(records: list[PatientRecord]) -> None:
    # A bundle from a foreign system may carry only the HTML rendition;
    # section structure must still come back via the data-kind wrappers.
    bundle = copy.deepcopy(to_bundle(records[0]))
    for entry in bundle["entry"]:
        resource = entry["resource"]
        if resource["resourceType"] == "DocumentReference" and "context" in resource:
            resource["content"] = [
                c for c in resource["content"] if c["attachment"]["contentType"] == "text/html"
            ]
    rebuilt = from_bundle(bundle)
    original = records[0].encounters[0]
    parsed = next(e for e in rebuilt.encounters if e.id == original.id)
    assert [s.kind for s in parsed.sections] == [s.kind for s in original.sections]
    subjective = parsed.section(SectionKind.SUBJECTIVE)
    assert subjective is not None and subjective.text == (
        original.section(SectionKind.SUBJECTIVE).text  # type: ignore[union-attr]
    )
    assert parsed.addenda == original.addenda if original.addenda else True


def test_bundle_validates_against_fhir_r4_schema(records: list[PatientRecord]) -> None:
    pytest.importorskip("fhir.resources", reason="schema validation needs the fhir extra")
    from fhir.resources.R4B.bundle import Bundle

    for record in records:
        Bundle.model_validate(to_bundle(record))


def test_ingest_requires_a_patient() -> None:
    with pytest.raises(ValueError, match="no Patient"):
        from_bundle({"resourceType": "Bundle", "type": "collection", "entry": []})


def test_round_trip_edge_cases_from_qa_review() -> None:
    """Every shape the adversarial QA review proved lossy must round-trip.

    Notably: real values that collide with FHIR required-field placeholders
    ("Unknown" reactions/diagnoses/payers), None values that must NOT come
    back as placeholders, sparse name/address slots, empty strings, and
    record-level extensions.
    """
    from datetime import date

    from anastomosis.core.model import (
        Address,
        AllergyIntolerance,
        Coverage,
        Encounter,
        FamilyMemberHistory,
        Immunization,
        MedicationStatement,
        Observation,
        Patient,
    )

    pid = "feedface-0000-0000-0000-0000000000ee"
    record = PatientRecord(
        extensions={"pf_tebra:RecordLevel": "survives"},
        patient=Patient(
            id=pid,
            # Two names for list positions FHIR fills from the front. Without
            # the extensions export writes for these, "Q" comes back as the
            # GIVEN name and "Apt 4" as line1 — each value in a neighbouring
            # field, on the patient's own identity.
            middle_name="Q",
            addresses=[Address(line2="Apt 4")],
        ),
        encounters=[
            Encounter(
                id="feedface-e000-0000-0000-0000000000ee",
                patient_id=pid,
                date_of_service=date(2023, 1, 1),
            )
        ],
        observations=[
            Observation(patient_id=pid, display="Empty-string value", value=""),
            Observation(patient_id=pid, code="8480-6", value="120/80", unit="mmHg"),
            Observation(patient_id=pid, code="8867-4", value="NaN"),
        ],
        allergies=[
            AllergyIntolerance(
                patient_id=pid,
                substance="Probe",
                reactions=["Unknown"],  # a REAL charted value, not a placeholder
                severity="Life-threatening",  # not a FHIR severity code
            )
        ],
        medications=[MedicationStatement(patient_id=pid, generic_name="metformin")],
        immunizations=[Immunization(patient_id=pid)],  # vaccine=None
        family_history=[FamilyMemberHistory(patient_id=pid, diagnosis="Unknown", relation=None)],
        coverages=[
            Coverage(patient_id=pid, payer="Unknown", order_of_benefits=0),
            Coverage(patient_id=pid),  # payer=None must NOT come back "Unknown"
        ],
    )
    bundle = to_bundle(record)
    json.dumps(bundle)  # NaN guard: bundle must stay JSON-serializable
    rebuilt = from_bundle(bundle)
    assert rebuilt.id == record.id
    assert rebuilt.extensions == record.extensions
    assert rebuilt.patient.model_dump(mode="json", exclude={"provenance"}) == (
        record.patient.model_dump(mode="json", exclude={"provenance"})
    )
    for field in _LIST_FIELDS:
        assert _dumps(getattr(rebuilt, field)) == _dumps(getattr(record, field)), field

    pytest.importorskip("fhir.resources", reason="schema validation needs the fhir extra")
    from fhir.resources.R4B.bundle import Bundle

    Bundle.model_validate(bundle)


def test_non_json_serializable_extension_value_survives_export() -> None:
    """A non-JSON-serializable value in extensions (e.g. a datetime a future
    adapter might stash) must not crash bundle export and lose the whole record.
    _exts now serializes the extensions blob with default=str (the per-field
    serializer already had this guard; the blob did not)."""
    from datetime import UTC, datetime

    from anastomosis.core.model import Patient

    pid = "feedface-0000-0000-0000-0000000000df"
    stamp = datetime(2023, 6, 1, 12, 30, tzinfo=UTC)
    record = PatientRecord(patient=Patient(id=pid, extensions={"src:recorded": stamp}))

    bundle = to_bundle(record)  # must not raise TypeError
    json.dumps(bundle)  # the bundle stays JSON-serializable

    ext_strings = [
        x.get("valueString", "")
        for entry in bundle["entry"]
        for x in entry["resource"].get("extension", [])
    ]
    assert any(str(stamp) in s for s in ext_strings)  # datetime survived, stringified


# --- a resource with no id is refused, not indexed (#110) --------------------


def test_a_document_with_no_id_is_refused_by_name(records: list[PatientRecord]) -> None:
    """The shape a real Practice Fusion export hit: document rows with no id.

    The mapper built `DocumentArtifact`s that were empty shells, `_prune` tidied
    the empty `id` away, and `to_bundle` then indexed `r["id"]` inside a
    comprehension — so an unhandled `KeyError` surfaced three frames up, after
    fourteen charts were already written and the output lock taken. The data
    problem was real; the way it arrived was not usable by anyone.
    """
    record = records[0].model_copy(deep=True)
    record.documents.append(
        DocumentArtifact(id="", patient_id=record.patient.id, mime_type="application/octet-stream")
    )

    with pytest.raises(FhirExportError) as caught:
        to_bundle(record)

    message = str(caught.value)
    assert "1 DocumentReference" in message, message
    assert "no id" in message
    # The diagnosis names types and counts. Nothing off the record rides along:
    # not the patient's id, not a document title, not a path.
    assert record.patient.id not in message
    for document in record.documents:
        assert not document.title or document.title not in message
        assert not document.path or document.path not in message


def test_the_refusal_counts_every_missing_id_not_just_the_first() -> None:
    """One run, one diagnosis: how many, and of what."""
    patient = Patient(id="feedface-0000-4000-8000-000000000001")
    record = PatientRecord(
        id="feedface-0000-4000-8000-0000000000ff",
        patient=patient,
        documents=[DocumentArtifact(id="", patient_id=patient.id) for _ in range(3)],
    )

    with pytest.raises(FhirExportError) as caught:
        to_bundle(record)

    assert "3 of 4 resources" in str(caught.value), caught.value


def test_pruning_keeps_the_fields_a_resource_cannot_be_addressed_without() -> None:
    """`_prune` tidies empty OPTIONAL fields; the structural ones survive it.

    Without this the emptiness disappears before anything can report it.
    """
    pruned = _prune({"resourceType": "DocumentReference", "id": "", "title": "", "status": None})

    assert pruned == {"resourceType": "DocumentReference", "id": ""}


def test_a_record_whose_documents_all_carry_ids_still_exports(
    records: list[PatientRecord],
) -> None:
    """The guard refuses missing ids and nothing else."""
    for record in records:
        entries = to_bundle(record)["entry"]
        assert entries and all(entry["fullUrl"] for entry in entries)


# --- #382: the Attachment names where its bytes actually landed --------------


def _artifact_docref(bundle: dict, artifact_id: str) -> dict:
    (resource,) = [
        e["resource"]
        for e in bundle["entry"]
        if e["resource"]["resourceType"] == "DocumentReference"
        and e["resource"]["id"] == artifact_id
    ]
    return resource


def test_a_bare_export_carries_no_attachment_claim() -> None:
    """``to_bundle(record)`` with no ``attachments`` — every existing caller in
    this suite, and any export with no deliverer in the loop — makes no
    assertion either way: no url/size/hash, and no "missing" marker either,
    because nothing here has a filesystem to check against.
    """
    pid = "feedface-0000-4000-8000-000000000382"
    record = PatientRecord(
        patient=Patient(id=pid),
        documents=[
            DocumentArtifact(
                id="feedface-doc0-0000-0000-000000000001",
                patient_id=pid,
                path="scan.pdf",
                mime_type="application/pdf",
            )
        ],
    )

    bundle = to_bundle(record)

    attachment = _artifact_docref(bundle, "feedface-doc0-0000-0000-000000000001")["content"][0][
        "attachment"
    ]
    assert "url" not in attachment
    assert "size" not in attachment
    assert "hash" not in attachment
    assert "extension" not in attachment


def test_a_delivered_attachment_resolves_with_a_relative_forward_slash_url() -> None:
    """The Attachment carries what the deliverer measured, base64 not hex."""
    import hashlib

    pid = "feedface-0000-4000-8000-000000000382"
    doc_id = "feedface-doc0-0000-0000-000000000001"
    record = PatientRecord(
        patient=Patient(id=pid),
        documents=[
            DocumentArtifact(
                id=doc_id, patient_id=pid, path="scan.pdf", mime_type="application/pdf"
            )
        ],
    )
    content = b"%PDF-1.4 synthetic\n"
    attachments = {
        doc_id: DeliveredAttachment(
            url="attachments/scan.pdf",
            size=len(content),
            sha256=hashlib.sha256(content).hexdigest(),
        )
    }

    bundle = to_bundle(record, attachments)

    attachment = _artifact_docref(bundle, doc_id)["content"][0]["attachment"]
    assert attachment["url"] == "attachments/scan.pdf"
    assert "\\" not in attachment["url"]
    assert not attachment["url"].startswith(("/", "file://"))
    assert attachment["size"] == len(content)
    assert attachment["hash"] == base64.b64encode(hashlib.sha256(content).digest()).decode("ascii")
    # R4's Attachment.hash is base64Binary, not the hex spelling this toolkit's
    # other digests carry — the schema is the check that would catch a slip.
    assert attachment["hash"] != hashlib.sha256(content).hexdigest()

    pytest.importorskip("fhir.resources", reason="schema validation needs the fhir extra")
    from fhir.resources.R4B.bundle import Bundle

    Bundle.model_validate(bundle)


def test_a_named_document_the_delivery_did_not_carry_says_so_plainly() -> None:
    """A ``DocumentArtifact`` whose ``path`` is set but has no entry in
    ``attachments`` is one the deliverer tried to carry and could not — the
    Attachment says so with FHIR's own data-absent-reason extension rather
    than shipping url/size/hash all silently ``None``, the shape #382 found:
    a DocumentReference asserting a document exists with nothing to find it
    with, indistinguishable from "nobody checked".
    """
    pid = "feedface-0000-4000-8000-000000000382"
    doc_id = "feedface-doc0-0000-0000-000000000001"
    record = PatientRecord(
        patient=Patient(id=pid),
        documents=[
            DocumentArtifact(
                id=doc_id, patient_id=pid, path="scan.pdf", mime_type="application/pdf"
            )
        ],
    )

    bundle = to_bundle(record, {})  # the deliverer carried nothing

    attachment = _artifact_docref(bundle, doc_id)["content"][0]["attachment"]
    assert "url" not in attachment
    assert "size" not in attachment
    assert "hash" not in attachment
    assert attachment["extension"] == [
        {"url": "http://hl7.org/fhir/StructureDefinition/data-absent-reason", "valueCode": "error"}
    ]

    pytest.importorskip("fhir.resources", reason="schema validation needs the fhir extra")
    from fhir.resources.R4B.bundle import Bundle

    Bundle.model_validate(bundle)


def test_a_document_with_no_path_makes_no_claim_even_with_attachments_given() -> None:
    """A document the SOURCE never resolved (no ``path`` at all — an
    unfetched remote blob) is not "missing from this delivery"; it was never
    going to have a file. An empty ``attachments`` mapping must not turn that
    into a false "not carried" marker.
    """
    pid = "feedface-0000-4000-8000-000000000382"
    doc_id = "feedface-doc0-0000-0000-000000000001"
    record = PatientRecord(
        patient=Patient(id=pid),
        documents=[DocumentArtifact(id=doc_id, patient_id=pid, mime_type="application/pdf")],
    )

    bundle = to_bundle(record, {})

    attachment = _artifact_docref(bundle, doc_id)["content"][0]["attachment"]
    assert "extension" not in attachment
    assert "url" not in attachment
