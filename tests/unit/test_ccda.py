"""Tests for the C-CDA / CCD adapter against the synthetic fixture.

Each test asserts one section's mapping (or one trap) documented in
tests/fixtures/ccda/README.md.
"""

import ast
import inspect
import re
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

import anastomosis.sources.ccda
import anastomosis.sources.pf_tebra  # noqa: F401 — for the cross-adapter detect test
from anastomosis.core.fhir import from_bundle, to_bundle
from anastomosis.core.model import (
    AllergyCategory,
    IdentifierKind,
    ObservationCategory,
    Patient,
    PatientRecord,
)
from anastomosis.sources import get_source

CCDA_FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "ccda"
_BIRTHTIME_RE = re.compile(r'<birthTime value="[^"]*"/>')
PF_FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "pf_tebra_v9"
CCDA_EDGE_CASES = Path(__file__).resolve().parents[1] / "fixtures" / "ccda_edge_cases"


@pytest.fixture(scope="module")
def record() -> PatientRecord:
    adapter = get_source("ccda")
    assert adapter.detect(CCDA_FIXTURE)
    loaded = list(adapter.load(CCDA_FIXTURE))
    assert len(loaded) == 1
    return loaded[0]


@pytest.fixture(scope="module")
def zero_sentinel_record() -> PatientRecord:
    # Parsed directly, not via adapter.load(directory): ccda_edge_cases holds
    # several fixtures side by side, and loading the whole directory would
    # glob every one of them into a single multi-document load (the same
    # reason the individual-edge-case tests above parse a single file).
    from anastomosis.sources.ccda.parser import parse_document

    return parse_document(CCDA_EDGE_CASES / "feedface_ccd_zero_date_sentinel.xml")


# --- detection ---------------------------------------------------------------


def test_detect_true_on_ccda_dir() -> None:
    assert get_source("ccda").detect(CCDA_FIXTURE)


def test_detect_false_on_empty_and_pf_dirs(tmp_path: Path) -> None:
    assert not get_source("ccda").detect(tmp_path)
    assert not get_source("ccda").detect(PF_FIXTURE)


def test_pf_adapter_does_not_claim_ccda_dir() -> None:
    assert not get_source("pf-tebra").detect(CCDA_FIXTURE)


def test_load_keeps_one_ledger_per_document_and_resets(tmp_path: Path) -> None:
    """#315: the adapter ledgers every document as it parses — one set of books
    per file, on the same reset-per-load contract as ``quarantine`` — so the
    pipeline can settle them into the run's report without a third walk."""
    import shutil

    adapter = get_source("ccda")
    list(adapter.load(CCDA_FIXTURE))
    assert len(adapter.ledgers) == 1
    assert adapter.ledgers[0].constructs_offered > 0
    # Two copies of the fixture: the next load replaces the books, never appends.
    for name in ("a.xml", "b.xml"):
        shutil.copy(CCDA_FIXTURE / "feedface_ccd.xml", tmp_path / name)
    list(adapter.load(tmp_path))
    assert len(adapter.ledgers) == 2


def test_patient_id_is_deterministic_across_loads() -> None:
    """The C-CDA patient id is derived from the source (uuid5), not a fresh
    uuid4, so re-parsing the same document yields the same canonical id — like
    the encounter ids already did, and like the PF/Tebra adapter's source-guid
    id. Child records reference that same id, and it is a valid UUID."""
    import uuid

    adapter = get_source("ccda")
    first = next(iter(adapter.load(CCDA_FIXTURE)))
    second = next(iter(adapter.load(CCDA_FIXTURE)))
    assert first.patient.id == second.patient.id  # deterministic, not random
    uuid.UUID(first.patient.id)  # this fixture's id is a uuid5 — a valid UUID
    assert "901-65-4329" not in first.patient.id  # the SSN never becomes the id (PHI-safe)
    assert first.conditions and all(c.patient_id == first.patient.id for c in first.conditions)


def test_utf16_encoded_cda_is_detected_and_loaded(tmp_path: Path) -> None:
    """A UTF-16-encoded C-CDA (some Windows EHRs export UTF-16) is detected and
    loaded rather than silently skipped (zero records, no error). The
    encoding-aware sniff hands it to lxml, which parses UTF-16 natively, and the
    canonical record matches the UTF-8 form byte-for-byte (provenance aside)."""
    text = (CCDA_FIXTURE / "feedface_ccd.xml").read_text(encoding="utf-8")
    u16_dir = tmp_path / "u16"
    u16_dir.mkdir()
    (u16_dir / "feedface_ccd.xml").write_bytes(text.encode("utf-16"))  # adds a BOM

    adapter = get_source("ccda")
    assert adapter.detect(u16_dir)  # the old ASCII byte-sniff missed UTF-16
    loaded = list(adapter.load(u16_dir))
    assert len(loaded) == 1

    utf8 = next(iter(adapter.load(CCDA_FIXTURE)))
    u16 = loaded[0]
    assert u16.patient.birth_date == date(1979, 4, 6)
    # Compare clinical demographics; `id` is a per-load synthetic handle (not
    # source-derived in this adapter) and `provenance` carries the file path.
    drop = {"provenance", "id"}
    assert u16.patient.model_dump(mode="json", exclude=drop) == utf8.patient.model_dump(
        mode="json", exclude=drop
    )
    # The stable source identity is carried and matches across the two encodings.
    assert {(i.kind, i.value) for i in u16.patient.identifiers} == {
        (i.kind, i.value) for i in utf8.patient.identifiers
    }
    assert (len(u16.conditions), len(u16.observations), len(u16.encounters)) == (
        len(utf8.conditions),
        len(utf8.observations),
        len(utf8.encounters),
    )


# --- extension matching (#384) ------------------------------------------------


def test_a_ccd_extension_loads_beside_an_xml_export(tmp_path: Path) -> None:
    """The walk matched ``*.xml`` only, so a document Kareo/Tebra write as
    ``<name>.ccd`` was never opened, never counted, and never mentioned — a
    whole patient's chart silently absent from a run that exited 0 and
    reported success. An export holding one of each extension must load both.
    """
    import shutil

    fixture = CCDA_FIXTURE / "feedface_ccd.xml"
    shutil.copy(fixture, tmp_path / "summary.xml")
    shutil.copy(fixture, tmp_path / "ccd.ccd")

    loaded = list(get_source("ccda").load(tmp_path))
    assert len(loaded) == 2


@pytest.mark.parametrize("name", ["patient.CCD", "patient.Ccda"])
def test_uppercase_and_mixed_case_extensions_load(tmp_path: Path, name: str) -> None:
    """Matched on ``Path.suffix.lower()`` -- a capitalised extension is not
    a second silent miss on top of #384; proven on a case-sensitive
    (POSIX) filesystem."""
    import shutil

    shutil.copy(CCDA_FIXTURE / "feedface_ccd.xml", tmp_path / name)

    loaded = list(get_source("ccda").load(tmp_path))
    assert len(loaded) == 1


def test_detect_true_on_a_directory_holding_only_ccd_documents(tmp_path: Path) -> None:
    """Before the extension set widened, ``detect`` globbed ``*.xml`` too, so
    an export holding only ``.ccd`` documents — Kareo/Tebra's own spelling —
    was invisible to auto-detection: the pipeline would never even offer this
    adapter as the match."""
    import shutil

    shutil.copy(CCDA_FIXTURE / "feedface_ccd.xml", tmp_path / "summary.ccd")

    assert get_source("ccda").detect(tmp_path)


_REFERENCING_UNSTRUCTURED_CCD = """<?xml version="1.0" encoding="UTF-8"?>
<ClinicalDocument xmlns="urn:hl7-org:v3">
  <realmCode code="US"/>
  <id root="feedface-docu-0000-0000-000000000384"/>
  <code code="34133-9" displayName="Summarization of Episode Note"
        codeSystem="2.16.840.1.113883.6.1"/>
  <title>Scanned Referral</title>
  <effectiveTime value="20230510150000-0500"/>
  <recordTarget>
    <patientRole>
      <id root="feedface-pati-0000-0000-000000000384"/>
      <patient>
        <name><given>Ada</given><family>Fixture</family></name>
        <birthTime value="19850314"/>
      </patient>
    </patientRole>
  </recordTarget>
  <component><nonXMLBody>
    <text mediaType="application/pdf"><reference value="referral.pdf"/></text>
  </nonXMLBody></component>
</ClinicalDocument>
"""


def test_a_non_cda_file_beside_the_export_is_not_counted(tmp_path: Path) -> None:
    """A non-CDA file legitimately beside an export (e.g. a referenced
    attachment) is neither counted as skipped nor blamed for #384's
    finding -- it matches no accepted extension and does not sniff as
    CDA, and the document referencing it still loads."""
    (tmp_path / "referral.xml").write_text(_REFERENCING_UNSTRUCTURED_CCD, encoding="utf-8")
    (tmp_path / "referral.pdf").write_bytes(b"not a CDA document, unaccepted extension\n")

    adapter = get_source("ccda")
    loaded = list(adapter.load(tmp_path))
    assert len(loaded) == 1
    assert adapter.skipped_files == 0
    assert loaded[0].documents, "the referenced artifact must still be carried"


# --- #384 round two: detect() never raises (finding 1 / finding 7) -----------


def test_detect_never_raises_on_a_file_path(tmp_path: Path) -> None:
    """``detect`` must never raise: a file path (not a directory) reads as
    "not mine", never a crash that would abort ``detect_source``'s loop
    over every other adapter."""
    a_file = tmp_path / "export.xml"
    a_file.write_text("not a directory\n", encoding="utf-8")
    assert get_source("ccda").detect(a_file) is False


def test_detect_never_raises_on_a_missing_path(tmp_path: Path) -> None:
    assert get_source("ccda").detect(tmp_path / "does-not-exist") is False


def test_detect_never_raises_on_a_directory_it_cannot_list(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same tolerance as a file or a missing path, for the third way
    ``Path.iterdir()`` can fail: a permission this account lacks."""

    def _forbidden(self: Path) -> list[Path]:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(Path, "iterdir", _forbidden)
    assert get_source("ccda").detect(tmp_path) is False


def test_a_subdirectory_sharing_a_document_extension_does_not_abort_the_load(
    tmp_path: Path,
) -> None:
    """A subdirectory or broken symlink sharing a document extension
    (``*.ccd``) is excluded by ``is_file()`` before any content read is
    attempted, never aborting the walk (#384 round two)."""
    import shutil

    shutil.copy(CCDA_FIXTURE / "feedface_ccd.xml", tmp_path / "summary.xml")
    (tmp_path / "not_a_document.ccd").mkdir()

    adapter = get_source("ccda")
    loaded = list(adapter.load(tmp_path))
    assert len(loaded) == 1
    assert adapter.skipped_files == 0


# --- #384 round two: recognition by first start tag, not a byte window -------


def _padded_export(tmp_path: Path) -> Path:
    """A copy of the reference fixture with a >4 KB comment before its root —
    longer than the old byte-window sniff ever read."""
    text = (CCDA_FIXTURE / "feedface_ccd.xml").read_text(encoding="utf-8")
    comment = f"<!-- {'padding ' * 700} -->\n"
    assert len(comment.encode("utf-8")) > 4096, "the padding must outgrow the old window"
    idx = text.index("<ClinicalDocument")
    padded_dir = tmp_path / "padded"
    padded_dir.mkdir()
    (padded_dir / "summary.xml").write_text(text[:idx] + comment + text[idx:], encoding="utf-8")
    return padded_dir


def test_a_document_with_a_long_leading_comment_is_still_read(tmp_path: Path) -> None:
    """A real CCD carrying a vendor comment before its root parsed cleanly
    through ``parse_document`` directly and was STILL silently absent from
    every ``load`` — the old 4 KB peek never reached the markers it was
    looking for (#384 round two, finding 4). Recognition now decides by the
    document's FIRST START TAG, which a comment of any length cannot move."""
    padded_dir = _padded_export(tmp_path)
    adapter = get_source("ccda")
    loaded = list(adapter.load(padded_dir))
    assert len(loaded) == 1
    assert adapter.skipped_files == 0


def test_an_ordinary_non_cda_xml_is_neither_read_nor_counted(tmp_path: Path) -> None:
    """An accepted-extension file that is genuinely not CDA must stay
    uncounted either way — a count cannot tell "not CDA" apart from "CDA
    behind a long comment", so the fix must not paper over the difference by
    counting everything with the right extension."""
    (tmp_path / "notes.xml").write_text("<root><a>hi</a></root>", encoding="utf-8")
    adapter = get_source("ccda")
    assert list(adapter.load(tmp_path)) == []
    assert adapter.skipped_files == 0


def test_a_truncated_document_is_neither_read_nor_counted_and_does_not_raise(
    tmp_path: Path,
) -> None:
    """Garbage that cannot even produce a first start tag reads as "not CDA",
    the same tolerance ``detect`` needs, extended to the real ``load``."""
    (tmp_path / "broken.xml").write_bytes(b"not even well-formed xml")
    adapter = get_source("ccda")
    assert list(adapter.load(tmp_path)) == []
    assert adapter.skipped_files == 0


def test_a_clinical_document_in_the_wrong_namespace_is_not_read(tmp_path: Path) -> None:
    """The root element must be ``ClinicalDocument`` in CDA's OWN namespace —
    a document that borrows the tag name in some other namespace is not CDA
    just because a substring search would have matched it."""
    (tmp_path / "wrong_ns.xml").write_text(
        '<ClinicalDocument xmlns="urn:other:ns"><realm/></ClinicalDocument>',
        encoding="utf-8",
    )
    adapter = get_source("ccda")
    assert list(adapter.load(tmp_path)) == []
    assert adapter.skipped_files == 0


# --- #384 round two: document order is filename order, proven (finding 3) ---


def test_document_order_is_filename_order_not_creation_order(tmp_path: Path) -> None:
    """Mutant-proof: document order is filename order, never creation order
    (#384 round two) -- the refusal's position is the operator's only
    PHI-safe handle on which document to repair."""
    from anastomosis.pipeline import PipelineError, load_records

    good = (CCDA_FIXTURE / "feedface_ccd.xml").read_text(encoding="utf-8")
    for name, index in (("c_chart.xml", 3), ("b_chart.xml", 2), ("a_chart.xml", 1)):
        body = good.replace(
            "feedface-0000-0000-0000-000000000001",
            f"feedface-0000-0000-0000-00000000000{index}",
        )
        if name == "a_chart.xml":
            body = _BIRTHTIME_RE.sub('<birthTime value="not-a-date"/>', body)
        (tmp_path / name).write_text(body, encoding="utf-8")

    with pytest.raises(PipelineError) as caught:
        load_records(get_source("ccda"), tmp_path)
    message = str(caught.value)
    assert "document 1 of 3" in message
    assert "c_chart" not in message and "a_chart" not in message  # position, never a filename


# --- #384 round two, nit: skipped_files resets on every load, not just some --


def test_skipped_files_resets_on_a_load_that_finds_none(tmp_path: Path) -> None:
    """Reset beside ``ledgers``, before ``_scan`` runs, not only by ``_scan``'s
    own return value: a process-singleton adapter must not keep reporting a
    PREVIOUS run's unrelated count on a run that found none of its own."""
    import shutil

    first = tmp_path / "first"
    first.mkdir()
    shutil.copy(CCDA_FIXTURE / "feedface_ccd.xml", first / "summary.xml")
    shutil.copy(CCDA_FIXTURE / "feedface_ccd.xml", first / "extra.txt")

    adapter = get_source("ccda")
    list(adapter.load(first))
    assert adapter.skipped_files == 1

    second = tmp_path / "second"
    second.mkdir()
    shutil.copy(CCDA_FIXTURE / "feedface_ccd.xml", second / "summary.xml")
    list(adapter.load(second))
    assert adapter.skipped_files == 0


# --- demographics ------------------------------------------------------------


def test_demographics(record: PatientRecord) -> None:
    p = record.patient
    assert p.display_name == "Cora Specimen"
    assert p.birth_date == date(1979, 4, 6)
    assert p.sex == "Female"
    assert p.race == ["Asian"]
    assert p.ethnicity == ["Not Hispanic or Latino"]
    assert p.language == "en"
    assert p.identifier(IdentifierKind.SSN) == "901-65-4329"
    phones = {t.kind.value: t.value for t in p.telecom}
    assert phones["phone_home"] == "(206) 555-0177"
    assert phones["email"] == "cora.specimen@example.com"
    assert p.addresses[0].line1 == "456 Sample Way"
    assert p.addresses[0].postal_code == "98102"


# --- problems ----------------------------------------------------------------


def test_conditions(record: PatientRecord) -> None:
    htn = next(c for c in record.conditions if c.snomed == "38341003")
    assert htn.icd10 == "I10"
    assert htn.active is True
    assert htn.display == "Hypertensive disorder"
    migraine = next(c for c in record.conditions if c.snomed == "37796009")
    assert migraine.active is False  # has effectiveTime/high
    assert migraine.stopped == date(2020, 9, 1)
    assert migraine.icd10 is None


# --- allergies ---------------------------------------------------------------


def test_allergies(record: PatientRecord) -> None:
    by_substance = {a.substance: a for a in record.allergies}
    penicillin = by_substance["Penicillin G"]
    assert penicillin.category == AllergyCategory.DRUG
    assert penicillin.reactions == ["Hives"]
    assert penicillin.severity == "Moderate"
    assert penicillin.extensions["ccda:allergen_code"] == "7980"
    peanut = by_substance["Peanut"]
    assert peanut.category == AllergyCategory.FOOD
    assert peanut.reactions == ["Anaphylaxis"]
    assert peanut.severity == "Severe"
    assert peanut.extensions["ccda:allergen_code"] == "256349002"


# --- medications -------------------------------------------------------------


def test_medications(record: PatientRecord) -> None:
    by_rxnorm = {m.rxnorm: m for m in record.medications}
    lisinopril = by_rxnorm["314076"]
    assert lisinopril.active is True
    assert lisinopril.start == date(2023, 1, 1)
    assert lisinopril.stop is None  # high nullFlavor="UNK"
    assert lisinopril.extensions["ccda:route"] == "Oral"
    amoxicillin = by_rxnorm["308182"]
    assert amoxicillin.active is False  # statusCode completed
    assert amoxicillin.stop == date(2022, 3, 14)


# --- immunizations -----------------------------------------------------------


def test_immunizations(record: PatientRecord) -> None:
    flu = next(i for i in record.immunizations if "Influenza" in (i.vaccine or ""))
    assert flu.administered_on == date(2022, 10, 3)
    assert flu.lot_number == "FLU2022A"
    assert flu.comment is None
    refused = next(i for i in record.immunizations if i.comment == "Refused")
    assert refused.extensions["ccda:negationInd"] == "true"


# --- zero-sentinel timestamps (#385) ------------------------------------------
#
# A vendor's TS @value of nothing but zeros ("0", "00000000", ...) reads as
# absent rather than raising out of parse_dt and aborting the whole document;
# the loss is credited on the record — see core.timeutil.is_zero_sentinel and
# sources.ccda.parser._record_zero_sentinels.


def test_a_medication_whose_start_is_a_zero_sentinel_is_kept_without_one(
    zero_sentinel_record: PatientRecord,
) -> None:
    """A medication whose only stated start is an all-zero TS (#385) must
    survive with no start (high is already nullFlavor="UNK", already None),
    and the loss must be named on the record rather than vanishing."""
    [medication] = zero_sentinel_record.medications
    assert medication.start is None
    assert medication.stop is None
    assert zero_sentinel_record.patient.extensions["ccda:timestamp_named_no_instant"] == {
        "effectiveTime/low": 1
    }


def test_a_numeric_zero_is_not_a_date_sentinel(zero_sentinel_record: PatientRecord) -> None:
    """Red on main (the fixture would not even parse). The same document's
    30954-2 result carries a real PQ @value="0" that never routes through
    parse_dt — it must keep reading as the number zero, and the sentinel
    count above must not grow because of it: the count stays 1, naming only
    the medication's TS, never this observation's PQ."""
    [observation] = [o for o in zero_sentinel_record.observations if o.code == "11555-0"]
    assert observation.value == "0"
    assert zero_sentinel_record.patient.extensions["ccda:timestamp_named_no_instant"] == {
        "effectiveTime/low": 1
    }


def test_a_nullflavor_timestamp_is_absent_not_a_sentinel(record: PatientRecord) -> None:
    """Not a #385 acceptance test -- a nullFlavor TS reads as absent on its
    own, and ``is_zero_sentinel`` must not change that."""
    assert "ccda:timestamp_named_no_instant" not in record.patient.extensions


def test_every_timestamp_path_the_parser_reads_is_named_in_TS_PATHS() -> None:
    """Anti-drift: ``TS_PATHS`` (hand-kept) must name every timestamp path
    the parser actually reads, verified by walking the parser's own AST
    rather than trusted from memory -- so a call reading an unlisted
    path, or a listed path nothing reads, fails here instead of drifting
    silently."""
    from anastomosis.sources.ccda import parser as ccda_parser

    tree = ast.parse(inspect.getsource(ccda_parser))

    def _is_find_of(node: ast.AST, path: str) -> bool:
        return (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "_find"
            and len(node.args) == 2
            and isinstance(node.args[1], ast.Constant)
            and node.args[1].value == path
        )

    effective_time_vars = {
        node.targets[0].id
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name)
        and _is_find_of(node.value, "v3:effectiveTime")
    }

    read_paths: set[str] = set()
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in {"_ts", "_ts_date"}
            and len(node.args) == 2
            and isinstance(node.args[1], ast.Constant)
            and isinstance(node.args[1].value, str)
        ):
            continue
        stripped = "/".join(seg.removeprefix("v3:") for seg in node.args[1].value.split("/"))
        first = node.args[0]
        if isinstance(first, ast.Name) and first.id in effective_time_vars:
            stripped = f"effectiveTime/{stripped}"
        read_paths.add(stripped)

    assert read_paths == set(ccda_parser.TS_PATHS)


# --- vitals + results --------------------------------------------------------


def test_vitals(record: PatientRecord) -> None:
    vitals = [o for o in record.observations if o.category == ObservationCategory.VITAL_SIGNS]
    assert len(vitals) >= 4
    by_code = {o.code: o for o in vitals}
    assert by_code["8480-6"].value == "122"  # systolic
    assert by_code["8462-4"].value == "78"  # diastolic
    assert by_code["8480-6"].unit == "mm[Hg]"
    effective = by_code["8480-6"].effective_at
    assert effective is not None
    assert effective.utcoffset() == timedelta(hours=-5)
    assert effective.astimezone(UTC).hour == 19  # 14:00 -0500 → 19:00 UTC


def test_results(record: PatientRecord) -> None:
    labs = [o for o in record.observations if o.category == ObservationCategory.LABORATORY]
    glucose = next(o for o in labs if o.code == "2345-7")
    assert glucose.value == "92"
    assert glucose.unit == "mg/dL"
    creatinine = next(o for o in labs if o.code == "2160-0")
    assert creatinine.value == "0.9"


# --- measurements reach the visit they were taken at -------------------------


def test_a_measurement_is_charted_at_the_visit_it_was_taken_at(record: PatientRecord) -> None:
    """Both SOAP packs group observations by ``encounter.id``; this adapter
    alone left it unset, so every vital/lab rendered nowhere. The
    same-day Notes entry must not count as a second candidate, or the
    day reads ambiguous and nothing links."""
    office = next(
        e for e in record.encounters if (e.encounter_type or "").startswith("Office outpatient")
    )
    charted = record.observations_for(office.id)
    assert [o.code for o in charted] == [
        "8480-6",
        "8462-4",
        "8867-4",
        "29463-7",
        "8302-2",
        "2345-7",
        "2160-0",
    ]
    assert next(o for o in charted if o.code == "8480-6").value == "122"

    measurements = [
        o
        for o in record.observations
        if o.category in (ObservationCategory.VITAL_SIGNS, ObservationCategory.LABORATORY)
    ]
    assert measurements and all(o.encounter_id == office.id for o in measurements)

    # Smoking status is a standing fact about the patient, not a reading taken
    # at an appointment, so it stays record-level where the packs read it from.
    smoking = next(o for o in record.observations if o.display == "Tobacco use")
    assert smoking.encounter_id is None

    note_only = next(e for e in record.encounters if e.encounter_type is None)
    assert record.observations_for(note_only.id) == []


# Two office visits on one calendar day — ordinary for a patient seen twice, and
# the case where the document's timestamps stop being evidence about which visit
# a reading belongs to.
_TWO_VISITS_ONE_DAY_CCD = """<?xml version="1.0" encoding="UTF-8"?>
<ClinicalDocument xmlns="urn:hl7-org:v3" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
  <id root="feedface-0000-0000-0000-00000000cda3"/>
  <title>Two-visits-one-day CCD</title>
  <recordTarget><patientRole>
    <id root="feedface-0000-0000-0000-000000000001"/>
    <patient><name><given>Ada</given><family>Fixture</family></name></patient>
  </patientRole></recordTarget>
  <component><structuredBody>
    <component><section>
      <code code="8716-3" codeSystem="2.16.840.1.113883.6.1"/>
      <title>Vital Signs</title>
      <text>BP 122/78 mm[Hg] (2023-05-10)</text>
      <entry><organizer classCode="CLUSTER" moodCode="EVN">
        <id root="feedface-0000-0000-0000-0000000000a1"/>
        <effectiveTime value="20230510140000-0500"/>
        <component><observation classCode="OBS" moodCode="EVN">
          <code code="8480-6" displayName="Systolic blood pressure"
                codeSystem="2.16.840.1.113883.6.1"/>
          <effectiveTime value="20230510140000-0500"/>
          <value xsi:type="PQ" value="122" unit="mm[Hg]"/>
        </observation></component>
      </organizer></entry>
    </section></component>
    <component><section>
      <code code="46240-8" codeSystem="2.16.840.1.113883.6.1"/>
      <title>Encounters</title>
      <text>Two office visits (2023-05-10)</text>
      <entry><encounter classCode="ENC" moodCode="EVN">
        <id root="feedface-0000-0000-0000-0000000000e1"/>
        <code code="99213" displayName="Office outpatient visit 15 minutes"
              codeSystem="2.16.840.1.113883.6.12"/>
        <effectiveTime value="20230510"/>
      </encounter></entry>
      <entry><encounter classCode="ENC" moodCode="EVN">
        <id root="feedface-0000-0000-0000-0000000000e2"/>
        <code code="99214" displayName="Office outpatient visit 25 minutes"
              codeSystem="2.16.840.1.113883.6.12"/>
        <effectiveTime value="20230510"/>
      </encounter></entry>
    </section></component>
  </structuredBody></component>
</ClinicalDocument>
"""


def test_two_visits_on_one_day_leave_the_measurement_record_level(tmp_path: Path) -> None:
    """The restraint half: two visits on one calendar day is not evidence
    for either, so the measurement stays record-level rather than being
    charted to whichever encounter the parser saw first."""
    from anastomosis.sources.ccda.parser import parse_document

    doc = tmp_path / "two_visits.xml"
    doc.write_text(_TWO_VISITS_ONE_DAY_CCD, encoding="utf-8")
    parsed = parse_document(doc)

    assert len(parsed.encounters) == 2
    assert {e.date_of_service for e in parsed.encounters} == {date(2023, 5, 10)}
    systolic = next(o for o in parsed.observations if o.code == "8480-6")
    assert systolic.value == "122"  # still parsed, in full
    assert systolic.encounter_id is None
    assert all(parsed.observations_for(e.id) == [] for e in parsed.encounters)


def test_the_link_is_only_made_on_evidence_the_document_gives() -> None:
    """The linker declines to guess past the document's own evidence: a
    future ``entryRelationship`` follow would win over a same-day guess,
    and an undated measurement has nothing to match on."""
    from anastomosis.core.model import Encounter, Observation
    from anastomosis.sources.ccda.parser import _link_measurements_to_encounters

    pid = "feedface-0000-0000-0000-000000000001"
    stated = Observation(
        patient_id=pid,
        encounter_id="feedface-0000-0000-0000-0000000000e9",
        category=ObservationCategory.VITAL_SIGNS,
        code="8480-6",
        value="122",
        effective_at=datetime(2023, 5, 10, 14, 0, tzinfo=UTC),
    )
    undated = Observation(
        patient_id=pid,
        category=ObservationCategory.VITAL_SIGNS,
        code="8462-4",
        value="78",
    )
    record = PatientRecord(
        patient=Patient(id=pid),
        encounters=[
            Encounter(
                id="feedface-0000-0000-0000-0000000000e1",
                patient_id=pid,
                date_of_service=date(2023, 5, 10),
                encounter_type="Office outpatient visit 15 minutes",
            )
        ],
        observations=[stated, undated],
    )
    _link_measurements_to_encounters(record)

    assert stated.encounter_id == "feedface-0000-0000-0000-0000000000e9"
    assert undated.encounter_id is None


# --- social history ----------------------------------------------------------


def test_smoking_observation(record: PatientRecord) -> None:
    social = [o for o in record.observations if o.category == ObservationCategory.SOCIAL_HISTORY]
    smoking = next(o for o in social if o.display == "Tobacco use")
    assert smoking.value == "Never smoker"


# --- encounters + notes ------------------------------------------------------


def test_encounters_include_office_visit_and_note(record: PatientRecord) -> None:
    assert len(record.encounters) >= 2
    office = next(
        e for e in record.encounters if (e.encounter_type or "").startswith("Office outpatient")
    )
    assert office.date_of_service == date(2023, 5, 10)
    note_encounter = next(
        e for e in record.encounters if any(s.kind.value == "narrative" for s in e.sections)
    )
    assert note_encounter.date_of_service == date(2023, 5, 10)
    narrative = note_encounter.sections[0]
    assert narrative.html is None
    assert narrative.text is not None
    assert "routine blood pressure follow-up" in narrative.text


# --- losslessness ------------------------------------------------------------


# A Problems section (LOINC 11450-4 — structurally parsed) whose single entry
# is NOT the act/entryRelationship/observation shape `_conditions` requires, so
# the structural parser yields nothing and the narrative is the only record of
# what the section said.
_UNSUPPORTED_PROBLEMS_CCD = """<?xml version="1.0" encoding="UTF-8"?>
<ClinicalDocument xmlns="urn:hl7-org:v3">
  <id root="feedface-0000-0000-0000-00000000cda2"/>
  <title>Unsupported-entry CCD</title>
  <recordTarget><patientRole>
    <id root="feedface-0000-0000-0000-000000000001"/>
    <patient><name><given>Ada</given><family>Fixture</family></name></patient>
  </patientRole></recordTarget>
  <component><structuredBody>
    <component><section>
      <code code="11450-4" codeSystem="2.16.840.1.113883.6.1"/>
      <title>Problems</title>
      <text>SENTINEL-UNSUPPORTED-PROBLEM (active, onset 2021-02-15)</text>
      <entry><observation classCode="OBS" moodCode="EVN">
        <value code="38341003" codeSystem="2.16.840.1.113883.6.96"/>
      </observation></entry>
    </section></component>
  </structuredBody></component>
</ClinicalDocument>
"""


def test_known_section_narrative_survives_unsupported_entries(tmp_path: Path) -> None:
    """A structurally-parsed section whose entries the parser cannot take apart
    still keeps its narrative: the <text> of EVERY section is captured, not only
    an unknown section's, so an unsupported entry shape cannot erase the chart."""
    from anastomosis.sources.ccda.parser import parse_document

    doc = tmp_path / "unsupported_problems.xml"
    doc.write_text(_UNSUPPORTED_PROBLEMS_CCD, encoding="utf-8")
    parsed = parse_document(doc)
    assert parsed.conditions == []  # the structural parser is unchanged: it still skips
    section = parsed.patient.extensions["ccda:section:11450-4"]
    assert section["title"] == "Problems"
    assert "SENTINEL-UNSUPPORTED-PROBLEM" in section["text"]


# Two sections sharing one LOINC (split Problems (Active)/(Resolved) is ordinary
# C-CDA) plus two sections with no code at all — four narratives that must land
# on four distinct keys.
_REPEATED_SECTIONS_CCD = """<?xml version="1.0" encoding="UTF-8"?>
<ClinicalDocument xmlns="urn:hl7-org:v3">
  <id root="feedface-0000-0000-0000-00000000cda9"/>
  <title>Repeated-section CCD</title>
  <recordTarget><patientRole>
    <id root="feedface-0000-0000-0000-000000000001"/>
    <patient><name><given>Ada</given><family>Fixture</family></name></patient>
  </patientRole></recordTarget>
  <component><structuredBody>
    <component><section>
      <code code="11450-4" codeSystem="2.16.840.1.113883.6.1"/>
      <title>Problems (Active)</title>
      <text>SENTINEL-FIRST-PROBLEM-NARRATIVE</text>
    </section></component>
    <component><section>
      <code code="11450-4" codeSystem="2.16.840.1.113883.6.1"/>
      <title>Problems (Resolved)</title>
      <text>SENTINEL-SECOND-PROBLEM-NARRATIVE</text>
    </section></component>
    <component><section>
      <title>No-code section A</title>
      <text>SENTINEL-NOCODE-A</text>
    </section></component>
    <component><section>
      <title>No-code section B</title>
      <text>SENTINEL-NOCODE-B</text>
    </section></component>
  </structuredBody></component>
</ClinicalDocument>
"""


def test_repeated_section_code_keeps_every_narrative(tmp_path: Path) -> None:
    """A second section sharing a LOINC must not overwrite the first: each
    occurrence gets its own key, suffixed in document order, so neither the
    Active nor the Resolved Problems narrative vanishes. Two code-less sections
    are disambiguated the same way."""
    from anastomosis.sources.ccda.parser import parse_document

    doc = tmp_path / "repeated_sections.xml"
    doc.write_text(_REPEATED_SECTIONS_CCD, encoding="utf-8")
    ext = parse_document(doc).patient.extensions
    assert ext["ccda:section:11450-4"] == {
        "title": "Problems (Active)",
        "text": "SENTINEL-FIRST-PROBLEM-NARRATIVE",
    }
    assert ext["ccda:section:11450-4#2"] == {
        "title": "Problems (Resolved)",
        "text": "SENTINEL-SECOND-PROBLEM-NARRATIVE",
    }
    assert ext["ccda:section:unknown"]["text"] == "SENTINEL-NOCODE-A"
    assert ext["ccda:section:unknown#2"]["text"] == "SENTINEL-NOCODE-B"


def test_single_occurrence_sections_keep_the_bare_key(record: PatientRecord) -> None:
    """The suffix appears only on a REPEAT: a document with one section per code
    reads exactly as before (no ``#1``)."""
    keys = [k for k in record.patient.extensions if k.startswith("ccda:section:")]
    assert keys and not any("#" in key for key in keys)


def test_parsed_section_keeps_entries_and_narrative(record: PatientRecord) -> None:
    """Narrative capture is additive: the fixture's Problems section still maps
    to typed conditions AND now keeps the section text it renders from."""
    assert {c.snomed for c in record.conditions} >= {"38341003", "37796009"}
    problems = record.patient.extensions["ccda:section:11450-4"]
    assert problems["title"] == "Problems"
    assert "Essential hypertension" in problems["text"]


def test_unparsed_section_and_document_metadata_survive(record: PatientRecord) -> None:
    ext = record.patient.extensions
    plan = ext["ccda:section:18776-5"]
    assert plan["title"] == "Plan of Treatment"
    assert "recheck blood pressure in three months" in plan["text"]
    assert ext["ccda:documentId"] == "feedface-0000-0000-0000-00000000cda1"
    assert "ccda:title" in ext


# --- idempotency -------------------------------------------------------------


def test_encounter_ids_are_deterministic_across_reparses() -> None:
    # The engine's idempotent-skip relies on encounter.id being stable across
    # re-runs (same-day collision suffixing keys off encounter.id[:8]). A
    # plain uuid4 fallback would re-render every encounter on every pass.
    from anastomosis.sources.ccda.parser import parse_document

    first = parse_document(CCDA_FIXTURE / "feedface_ccd.xml")
    second = parse_document(CCDA_FIXTURE / "feedface_ccd.xml")
    assert [e.id for e in first.encounters] == [e.id for e in second.encounters]


_TWO_ENCOUNTER_CCD = """<?xml version="1.0" encoding="UTF-8"?>
<ClinicalDocument xmlns="urn:hl7-org:v3">
  <id root="feedface-0000-0000-0000-000000000401"/>
  <code code="34133-9" displayName="Summarization of Episode Note"
        codeSystem="2.16.840.1.113883.6.1"/>
  <effectiveTime value="20230510150000-0500"/>
  <recordTarget>
    <patientRole>
      <id root="feedface-0000-0000-0000-000000000402"/>
      <patient>
        <name><given>Test</given><family>Patient</family></name>
        <administrativeGenderCode code="F" displayName="Female"
                                  codeSystem="2.16.840.1.113883.5.1"/>
        <birthTime value="19800101"/>
      </patient>
    </patientRole>
  </recordTarget>
  <component><structuredBody>
    <component><section>
      <code code="46240-8" codeSystem="2.16.840.1.113883.6.1"/>
      <title>Encounters</title>
      <text><paragraph>Office visit</paragraph></text>
      <entry><encounter classCode="ENC" moodCode="EVN">
        <id root="1.2.3" extension="X:4"/>
        <code code="99213" displayName="Office visit"/>
        <effectiveTime><low value="20230510"/></effectiveTime>
      </encounter></entry>
      <entry><encounter classCode="ENC" moodCode="EVN">
        <id root="1.2.3:X" extension="4"/>
        <code code="99214" displayName="Follow up"/>
        <effectiveTime><low value="20230712"/></effectiveTime>
      </encounter></entry>
    </section></component>
  </structuredBody></component>
</ClinicalDocument>
"""


def test_encounter_identity_pair_quotes_each_half_so_a_colon_cannot_cross_it(
    tmp_path: Path,
) -> None:
    """``("1.2.3", "X:4")`` and ``("1.2.3:X", "4")`` must not hash to the
    same encounter id -- pinned against literal uuid5 strings, not a
    second call to the function under test (#378's own precedent)."""
    from anastomosis.sources.ccda.parser import parse_document

    doc = tmp_path / "two_encounters.xml"
    doc.write_text(_TWO_ENCOUNTER_CCD, encoding="utf-8")

    record = parse_document(doc)

    ids = sorted(e.id for e in record.encounters)
    assert ids == sorted(
        ["20c70a5e-f37d-5241-86d1-39c1ae15d794", "5582544a-2141-5900-9cdf-b68f2a8ec44f"]
    )


def test_an_oid_root_and_extension_encounter_id_is_stable_across_reparses(
    tmp_path: Path,
) -> None:
    """#393's idempotent-skip invariant on the (root, extension) recipe
    specifically -- the shared-fixture test above only exercises
    GUID-root encounters."""
    from anastomosis.sources.ccda.parser import parse_document

    doc = tmp_path / "two_encounters.xml"
    doc.write_text(_TWO_ENCOUNTER_CCD, encoding="utf-8")

    first = parse_document(doc)
    second = parse_document(doc)

    assert [e.id for e in first.encounters] == [e.id for e in second.encounters]


def test_an_encounter_whose_first_id_is_nullflavor_still_reads_its_second_rooted_id(
    tmp_path: Path,
) -> None:
    """The encounter walk reads its id through ``first_rooted_id``, not raw
    first-child lookup: a nullFlavor id ahead of a real rooted one must
    not read as id-less (#378, on the encounter branch #393 names)."""
    from anastomosis.sources.ccda.parser import parse_document

    doc = tmp_path / "nullflavor_first.xml"
    doc.write_text(
        _TWO_ENCOUNTER_CCD.replace(
            '<id root="1.2.3" extension="X:4"/>',
            '<id nullFlavor="NI"/><id root="feedface-e000-0000-0000-00000000009c"/>',
        ),
        encoding="utf-8",
    )

    record = parse_document(doc)

    by_type = {e.encounter_type: e.id for e in record.encounters}
    assert by_type["Office visit"] == "feedface-e000-0000-0000-00000000009c"


def test_a_component_with_no_id_takes_a_provenance_derived_from_its_organizer(
    tmp_path: Path,
) -> None:
    # A component <observation> under a Results organizer whose own <id> is
    # nullFlavor="NI" is still the organizer's statement, not one with no
    # provenance at all (#378). The id has to be document-intrinsic — no
    # source_file in the recipe, unlike _encounter_id's fallback — because it
    # has to survive an export/re-ingest round trip that lands under a
    # different name; proven here by reparsing the identical bytes under a
    # file name that shares nothing with the fixture's own.
    #
    # `expected` is the LITERAL uuid5 string, not a call to
    # organizer_component_source_id: computing it via the function under test
    # let three mutations of the recipe itself survive the suite (the
    # namespace word "organizer" -> "organiser", the extension dropped from
    # the name, `{index}` pinned to 0) — the test and the code always agreed
    # because they were the same code. A recipe change is a decision that
    # rewrites every already-migrated chart's provenance and belongs behind
    # a literal someone has to notice changed, not a helper call that moves
    # with it silently.
    from anastomosis.sources.ccda.parser import parse_document

    fixture = (
        Path(__file__).resolve().parents[1]
        / "fixtures"
        / "ccda_edge_cases"
        / "feedface_ccd_idless_result_component.xml"
    )
    expected = "7029466a-7630-5f95-9072-85ca63f186dc"

    parsed = parse_document(fixture)
    [observation] = parsed.observations
    assert observation.provenance is not None
    assert observation.provenance.source_id == expected

    renamed = tmp_path / "a_file_name_sharing_nothing_with_the_fixture.xml"
    renamed.write_bytes(fixture.read_bytes())
    reparsed = parse_document(renamed)
    [reobservation] = reparsed.observations
    assert reobservation.provenance is not None
    assert reobservation.provenance.source_id == expected


# --- cross-adapter FHIR round trip -------------------------------------------


def _dumps(models: list) -> list[dict]:
    return [m.model_dump(mode="json", exclude={"provenance"}) for m in models]


def test_fhir_round_trip_is_lossless(record: PatientRecord) -> None:
    rebuilt = from_bundle(to_bundle(record))
    assert rebuilt.patient.model_dump(mode="json", exclude={"provenance"}) == (
        record.patient.model_dump(mode="json", exclude={"provenance"})
    )
    assert _dumps(rebuilt.conditions) == _dumps(record.conditions)
    assert _dumps(rebuilt.allergies) == _dumps(record.allergies)


_VALUE_TYPES_CCD = """<?xml version="1.0"?>
<ClinicalDocument xmlns="urn:hl7-org:v3" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
  <recordTarget><patientRole><id root="feedface-0000-0000-0000-000000000001"/>
    <patient><name><given>Ada</given><family>Fixture</family></name>
    <administrativeGenderCode code="F" displayName="Female" codeSystem="2.16.840.1.113883.5.1"/>
    <birthTime value="19850314"/></patient></patientRole></recordTarget>
  <component><structuredBody><component><section>
    <code code="30954-2" codeSystem="2.16.840.1.113883.6.1"/><title>Results</title>
    <entry><organizer classCode="BATTERY" moodCode="EVN"><statusCode code="completed"/>
      <component><observation classCode="OBS" moodCode="EVN">
        <code code="2345-7" displayName="Glucose" codeSystem="2.16.840.1.113883.6.1"/>
        <effectiveTime value="20230510"/>
        <value xsi:type="PQ" value="99" unit="mg/dL"/></observation></component>
      <component><observation classCode="OBS" moodCode="EVN">
        <code code="5811-5" displayName="Specific gravity" codeSystem="2.16.840.1.113883.6.1"/>
        <effectiveTime value="20230510"/>
        <value xsi:type="ST">1.015</value></observation></component>
      <component><observation classCode="OBS" moodCode="EVN">
        <code code="664-3" displayName="Poikilocytosis" codeSystem="2.16.840.1.113883.6.1"/>
        <effectiveTime value="20230510"/>
        <value xsi:type="CD" code="260385009" displayName="Negative"/></observation></component>
      <component><observation classCode="OBS" moodCode="EVN">
        <code code="6690-2" displayName="WBC" codeSystem="2.16.840.1.113883.6.1"/>
        <effectiveTime value="20230510"/>
        <value xsi:type="IVL_PQ"><low value="4.0" unit="10*3/uL"/>
        <high value="11.0" unit="10*3/uL"/></value></observation></component>
    </organizer></entry></section></component></structuredBody></component>
</ClinicalDocument>
"""


def test_every_ccda_value_type_keeps_its_result(tmp_path: Path) -> None:
    """Every ``xsi:type`` form must keep its value (#243), not only ``PQ`` --
    a still-created result with ``Observation.value`` None reads to a
    receiving EHR as "no result" rather than the finding recorded."""
    (tmp_path / "values.xml").write_text(_VALUE_TYPES_CCD, encoding="utf-8")

    (record,) = list(get_source("ccda").load(tmp_path))
    readings = {o.display: (o.value, o.unit) for o in record.observations}

    assert readings["Glucose"] == ("99", "mg/dL")  # PQ, the one that always worked
    assert readings["Specific gravity"] == ("1.015", None)  # ST — element text
    assert readings["Poikilocytosis"] == ("Negative", None)  # CD — the coded display
    assert readings["WBC"] == ("4.0-11.0", "10*3/uL")  # IVL_PQ — a range with its unit
    assert not [d for d, (v, _) in readings.items() if v is None], "a result lost its value"


def test_one_unreadable_document_is_refused_by_position_not_anonymously(tmp_path: Path) -> None:
    """A document the adapter cannot parse refuses the run, and must say
    WHICH one by position -- not by filename (patient-derived) or by
    bare exception type, which left bisecting 2,103 documents by hand as
    the only recourse (#243)."""
    from anastomosis.pipeline import PipelineError, load_records

    good = (CCDA_FIXTURE / "feedface_ccd.xml").read_text(encoding="utf-8")
    for index in range(1, 6):
        body = good.replace(
            "feedface-0000-0000-0000-000000000001",
            f"feedface-0000-0000-0000-00000000000{index}",
        )
        if index == 3:
            body = _BIRTHTIME_RE.sub('<birthTime value="not-a-date"/>', body)
        (tmp_path / f"patient_{index:02d}.xml").write_text(body, encoding="utf-8")

    with pytest.raises(PipelineError) as caught:
        load_records(get_source("ccda"), tmp_path)

    message = str(caught.value)
    assert "document 3 of 5" in message, "the refusal must name which document"
    assert "ValueError" in message
    # The file name is a patient value in a real C-CDA export, so it must not appear.
    assert "patient_03" not in message


_NARRATIVE_REFS_CCD = """<?xml version="1.0"?>
<ClinicalDocument xmlns="urn:hl7-org:v3" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
  <recordTarget><patientRole><id root="feedface-0000-0000-0000-000000000001"/>
    <patient><name><given>Ada</given><family>Fixture</family></name>
    <administrativeGenderCode code="F" displayName="Female" codeSystem="2.16.840.1.113883.5.1"/>
    <birthTime value="19850314"/></patient></patientRole></recordTarget>
  <component><structuredBody>
    <component><section>
      <code code="11450-4" codeSystem="2.16.840.1.113883.6.1"/><title>Problems</title>
      <text><list><item ID="prob1">Chronic obstructive pulmonary disease</item></list></text>
      <entry><act classCode="ACT" moodCode="EVN"><entryRelationship typeCode="SUBJ">
        <observation classCode="OBS" moodCode="EVN">
          <code code="55607006" codeSystem="2.16.840.1.113883.6.96"/>
          <effectiveTime><low value="20230510"/></effectiveTime>
          <value xsi:type="CD" code="13645005" codeSystem="2.16.840.1.113883.6.96">
            <originalText><reference value="#prob1"/></originalText>
          </value>
        </observation></entryRelationship></act></entry>
    </section></component>
    <component><section>
      <code code="34109-9" codeSystem="2.16.840.1.113883.6.1"/><title>Notes</title>
      <text><paragraph ID="n1">SUBJECTIVE: cough x3d. PLAN: fluids and rest.</paragraph></text>
      <entry><act classCode="ACT" moodCode="EVN">
        <id root="feedface-0000-4000-8000-00000000e001"/>
        <code code="34109-9" displayName="Note" codeSystem="2.16.840.1.113883.6.1"/>
        <text><reference value="#n1"/></text>
        <author><time value="20230510"/></author>
      </act></entry>
    </section></component>
  </structuredBody></component>
</ClinicalDocument>
"""


def test_a_narrative_reference_is_read_as_the_words_it_points_at(tmp_path: Path) -> None:
    """A ``<reference value="#id"/>`` carries no text of its own (#243): a
    problem or note whose body is only a reference must resolve to the
    words it points at, not arrive blank."""
    (tmp_path / "refs.xml").write_text(_NARRATIVE_REFS_CCD, encoding="utf-8")

    (record,) = list(get_source("ccda").load(tmp_path))

    (condition,) = record.conditions
    assert condition.display == "Chronic obstructive pulmonary disease", "the problem lost its name"

    (encounter,) = record.encounters
    (section,) = encounter.sections
    assert section.text == "SUBJECTIVE: cough x3d. PLAN: fluids and rest.", "the note body was lost"


def test_a_reference_naming_an_id_the_document_lacks_is_left_alone(tmp_path: Path) -> None:
    """Unresolvable is not the same as resolvable-to-nothing: a dangling
    reference must not invent text, and must not take the document down.
    """
    dangling = _NARRATIVE_REFS_CCD.replace('value="#prob1"', 'value="#nosuchid"')
    (tmp_path / "refs.xml").write_text(dangling, encoding="utf-8")

    (record,) = list(get_source("ccda").load(tmp_path))

    (condition,) = record.conditions
    assert condition.display is None, "a dangling reference must not fabricate a name"
    # The note half still resolves — one bad reference does not poison the rest.
    (encounter,) = record.encounters
    assert encounter.sections[0].text == "SUBJECTIVE: cough x3d. PLAN: fluids and rest."


def test_a_reference_carrying_its_own_fallback_text_keeps_it(tmp_path: Path) -> None:
    """Some writers put the words inline AND point at the narrative. The inline
    text is what the document already says, so resolving must not overwrite it.
    """
    inline = _NARRATIVE_REFS_CCD.replace(
        '<reference value="#prob1"/>', '<reference value="#prob1">Emphysema</reference>'
    )
    (tmp_path / "refs.xml").write_text(inline, encoding="utf-8")

    (record,) = list(get_source("ccda").load(tmp_path))

    (condition,) = record.conditions
    assert condition.display == "Emphysema", "the document's own words were overwritten"
