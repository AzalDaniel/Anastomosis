"""FHIR export/ingest tests: the round-trip IS the lossless guarantee.

Every fixture record must survive canonical → Bundle → canonical with
nothing changed (provenance excluded: it's local lineage, not exported).
"""

import base64
import copy
import json
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

import anastomosis.sources.pf_tebra  # noqa: F401 — registers the adapter
from anastomosis.core.fhir import DeliveredAttachment, from_bundle, to_bundle
from anastomosis.core.fhir.export import FhirExportError, _prune
from anastomosis.core.fhir.fields import TABLES
from anastomosis.core.model import (
    Address,
    AllergyCategory,
    DocumentArtifact,
    Guarantor,
    Patient,
    PatientContact,
    PatientRecord,
    PrescriptionTransaction,
    SectionKind,
)
from anastomosis.sources import get_source
from anastomosis.sources.ccda.parser import parse_document

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
    """Every shape the adversarial QA review proved lossy must
    round-trip: real values colliding with FHIR required-field
    placeholders ("Unknown" reactions/diagnoses/payers), None values
    that must NOT come back as placeholders, sparse name/address slots,
    empty strings, and record-level extensions."""
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
    # NOT `rebuilt.id == record.id`. The record's own id is a uuid4 minted at
    # parse time that no adapter sets and no source states, so carrying it into
    # the bundle only made two runs over one export differ (#405). The
    # extensions beside it ARE the source's, and those must survive.
    assert rebuilt.extensions == record.extensions
    assert rebuilt.patient.model_dump(mode="json", exclude={"provenance"}) == (
        record.patient.model_dump(mode="json", exclude={"provenance"})
    )
    for field in _LIST_FIELDS:
        assert _dumps(getattr(rebuilt, field)) == _dumps(getattr(record, field)), field

    pytest.importorskip("fhir.resources", reason="schema validation needs the fhir extra")
    from fhir.resources.R4B.bundle import Bundle

    Bundle.model_validate(bundle)


def test_the_records_own_runtime_id_does_not_reach_the_bundle(tmp_path: Path) -> None:
    """Contract: `PatientRecord.id` (a uuid4 minted at parse time) must
    not ride the delivered bundle. Two genuinely separate loads, since
    a reused record would mint no second id and this would pass while
    still carrying it.

    Does NOT make `bundle.json` byte-stable: every clinical object
    still takes its own uuid4 as its FHIR resource id/`fullUrl` — the
    same defect one layer down, open on #405."""
    from anastomosis.core.fhir import to_bundle
    from anastomosis.sources import get_source

    fixture = Path(__file__).resolve().parents[1] / "fixtures" / "ccda"
    first, second = (next(iter(get_source("ccda").load(fixture))) for _ in range(2))
    assert first.id != second.id, "two independent loads mint different record ids"

    extras = _record_extras(to_bundle(first))
    assert "id" not in extras, "the record's runtime id must not ride in the bundle"
    assert extras["extensions"] == first.extensions, "the source's own extensions still do"
    assert json.dumps(to_bundle(first)).count(first.id) == 0, (
        "the record's runtime id must not appear anywhere in the delivered bundle"
    )
    assert _record_extras(to_bundle(second)) == extras, (
        "and what does ride is identical across two loads of one export"
    )


def _record_extras(bundle: dict) -> dict:
    """The `__record__` extra out of a bundle's Patient resource."""
    from anastomosis.core.fhir.export import EXTRAS_NS

    patient = next(
        e["resource"] for e in bundle["entry"] if e["resource"]["resourceType"] == "Patient"
    )
    blob = next(x["valueString"] for x in patient["extension"] if x["url"] == EXTRAS_NS)
    return json.loads(blob)["__record__"]


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
    """A `DocumentArtifact` with no id must be refused by name, not
    surfaced as an unhandled `KeyError` from `to_bundle`'s own
    comprehension — after other charts have already been written and
    the output lock taken."""
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
    """A ``DocumentArtifact`` with a ``path`` but no ``attachments``
    entry is one the deliverer tried to carry and could not: the
    Attachment says so with FHIR's data-absent-reason extension rather
    than shipping url/size/hash all silently ``None`` (#382), which
    would be indistinguishable from "nobody checked"."""
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


def test_a_bundle_is_byte_identical_across_two_independent_loads(tmp_path: Path) -> None:
    # #405, #412: a fact's id is the `<id>` its source stated, or its
    # position where the source stated none.
    source = Path(__file__).parent.parent / "fixtures" / "ccda"
    documents = sorted(source.glob("*.xml"))
    assert documents, "the C-CDA fixture corpus is the input this pins"

    def load() -> str:
        bundles = [to_bundle(parse_document(doc)) for doc in documents]
        return json.dumps(bundles, sort_keys=True, default=str)

    assert load() == load()


def test_every_clinical_resource_id_is_derived_rather_than_minted() -> None:
    """The stronger statement, per resource type rather than over the blob.

    A whole-bundle comparison passes if the bundle is empty; this fails unless
    each kind is actually present AND stable, so it cannot pass vacuously.
    """
    source = Path(__file__).parent.parent / "fixtures" / "ccda"
    documents = sorted(source.glob("*.xml"))

    def ids_by_type() -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for doc in documents:
            for entry in to_bundle(parse_document(doc)).get("entry", []):
                resource = entry.get("resource", {})
                out.setdefault(resource.get("resourceType", "?"), []).append(resource.get("id", ""))
        return out

    first, second = ids_by_type(), ids_by_type()
    assert first == second
    # The five kinds #405 measured as drifting. If a fixture stops carrying one,
    # this says so rather than quietly narrowing what the test proves.
    for kind in ("Observation", "Condition", "AllergyIntolerance", "MedicationStatement"):
        assert first.get(kind), f"no {kind} in the corpus — this test proves less than it claims"


# --- the field table is the inventory, and the round trip is its guard -------

#: Values the generic sample below cannot supply: a row whose write rule needs
#: a particular shape (an empty contentType, a line2-only address), and the
#: nested models with required fields of their own.
_SAMPLE_OVERRIDES: dict[str, object] = {
    "addresses": [Address(line2="Suite 400")],
    "contacts": [PatientContact(name="Next Of Kin", relationship="spouse")],
    "guarantor": Guarantor(name="Guarantor Name"),
    "transactions": [PrescriptionTransaction(kind="Sent")],
    "category": AllergyCategory.FOOD,
}

_PID = "feedface-0000-4000-8000-00000000f1d0"


def _sample(model_cls: type, name: str) -> object:
    """A distinct value for one canonical field, from the model's own
    annotation — distinct so a swapped pair of rows cannot pass."""
    if name in _SAMPLE_OVERRIDES:
        return _SAMPLE_OVERRIDES[name]
    annotation = model_cls.model_fields[name].annotation
    by_type: dict[object, object] = {
        str | None: f"{name} value",
        list[str]: [f"{name} value"],
        int | None: 7,
        date | None: date(2023, 4, 5),
        datetime | None: datetime(2023, 4, 5, 6, 7, 8, tzinfo=UTC),
    }
    assert annotation in by_type, f"no sample for {model_cls.__name__}.{name}: {annotation}"
    return by_type[annotation]


def _slot(model_cls: type) -> str:
    """The PatientRecord list that holds this model, read off the record."""
    for name, field in PatientRecord.model_fields.items():
        if field.annotation == list[model_cls]:  # type: ignore[valid-type]
            return name
    raise AssertionError(f"no PatientRecord list holds {model_cls.__name__}")


def _saturated() -> PatientRecord:
    """One instance of every entity in the table, every tail field set."""
    record = PatientRecord(patient=Patient(id=_PID))
    for index, (model_cls, table) in enumerate(TABLES):
        values = {f.name: _sample(model_cls, f.name) for f in table if f.read}
        if model_cls is Patient:
            record.patient = Patient(id=_PID, **values)
            continue
        if model_cls is DocumentArtifact:
            values["title"] = "Chart"  # else the Attachment prunes to nothing
        if "patient_id" in model_cls.model_fields:
            values["patient_id"] = _PID
        item = model_cls(id=f"feedface-0000-4000-8000-{index:012d}", **values)
        record = record.model_copy(update={_slot(model_cls): [item]})
    return record


def test_every_field_in_the_table_survives_the_round_trip() -> None:
    """The central guard: for every row of the FHIR field table, a record
    carrying a value for it comes back carrying the same value. Driven by the
    table, so a row added later is covered without touching this test.
    """
    record = _saturated()
    rebuilt = from_bundle(to_bundle(record))

    for model_cls, table in TABLES:
        rows = [f for f in table if f.read]
        if not rows:
            continue
        before = [record.patient] if model_cls is Patient else getattr(record, _slot(model_cls))
        after = [rebuilt.patient] if model_cls is Patient else getattr(rebuilt, _slot(model_cls))
        assert len(after) == len(before), model_cls.__name__
        for original, returned in zip(before, after, strict=True):
            for row in rows:
                # Not vacuous: the sample above sets every row, so a value that
                # came back as None would mean the round trip dropped it.
                assert getattr(original, row.name) is not None, f"{model_cls.__name__}.{row.name}"
                assert getattr(returned, row.name) == getattr(original, row.name), (
                    f"{model_cls.__name__}.{row.name}"
                )


#: Every ``urn:anastomosis:field:`` key `to_bundle` writes, in emission order,
#: spelled out rather than derived from the table — a row renamed, dropped or
#: reordered moves both walkers at once, so only a literal catches it.
_TAIL_KEYS: dict[str, tuple[str, ...]] = {
    "AllergyIntolerance": ("category", "severity", "reactions"),
    "Condition": ("acuity",),
    "Coverage": (
        "payer",
        "order_of_benefits",
        "plan_name",
        "plan_type",
        "coverage_type",
        "group_number",
        "priority_label",
        "employer",
        "relationship_to_insured",
        "payment_type",
        "copay",
        "status_label",
    ),
    "DocumentReference": (
        "artifact",
        "path",
        "sha256",
        "page_count",
        "pack_name",
        "encounter_id",
        "generated_at",
    ),
    "Encounter": ("encounter_type", "signed_by_id", "signed_at", "last_modified_at"),
    "FamilyMemberHistory": ("relation", "diagnosis", "onset_date"),
    "Immunization": ("source", "vaccine"),
    "Location": (),
    "MedicationRequest": (
        "prefix",
        "status_label",
        "refills",
        "quantity",
        "medication_id",
        "display_date",
        "transactions",
    ),
    "MedicationStatement": (
        "generic_name",
        "brand_name",
        "strength",
        "route",
        "dose_form",
        "rxnorm",
        "display_name",
        "associated_dx",
        "last_modified_at",
        "prescription_ids",
    ),
    "Observation": ("value", "unit", "recorded_at", "display"),
    "Patient": (
        "sex",
        "gender_identity",
        "sexual_orientation",
        "race",
        "ethnicity",
        "mothers_maiden_name",
        "middle_name",
        "addresses",
        "contact_preference",
        "status",
        "notes",
        "contacts",
        "guarantor",
    ),
    "Practitioner": ("credential",),
}


def test_the_tail_carries_exactly_the_inventory_it_is_committed_to() -> None:
    """The pin the round trip cannot be: both walkers read one table, so a
    moved row keeps the round trip green while the delivered bundle changes.
    """
    from anastomosis.core.fhir.fields import FIELD_NS

    emitted: dict[str, list[str]] = {}
    for entry in to_bundle(_saturated())["entry"]:
        resource = entry["resource"]
        emitted.setdefault(resource["resourceType"], []).extend(
            x["url"].removeprefix(FIELD_NS)
            for x in resource.get("extension", [])
            if x["url"].startswith(FIELD_NS)
        )

    assert {k: tuple(v) for k, v in emitted.items()} == _TAIL_KEYS
