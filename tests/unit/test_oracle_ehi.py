"""Tests for the Oracle Health (Cerner Millennium) EHI adapter.

Each test asserts one fact the brief (docs/vendor_refs/ORACLE_EHI_SCHEMA.md)
documents or one trap baked into tests/fixtures/oracle_ehi_v500/README.md.
"""

from __future__ import annotations

import logging
from datetime import date
from pathlib import Path

import pytest

import anastomosis.sources.pf_tebra  # noqa: F401 — registered for the cross-detect test
from anastomosis.core.model import (
    AllergyCategory,
    IdentifierKind,
    ObservationCategory,
    PatientRecord,
    SectionKind,
)
from anastomosis.sources import detect_source, get_source
from anastomosis.sources.oracle_ehi.loader import (  # importing also registers the adapter
    iter_insert_rows,
    parse_column_names,
    read_export,
)
from anastomosis.sources.oracle_ehi.mapper import decode_ce_blob

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
ORACLE = FIXTURES / "oracle_ehi_v500"
PF = FIXTURES / "pf_tebra_v9"

P1 = "900000001"
P2 = "900000002"
E1 = "900100001"
E2 = "900100002"
E3 = "900100003"


# --- INSERT-statement reader --------------------------------------------------

_COLS = ["a", "b", "c"]


def test_insert_reader_multi_row() -> None:
    sql = "INSERT INTO `T` VALUES (1,2,3),(4,5,6),(7,8,9);"
    rows = iter_insert_rows(sql, "T", _COLS, "T.sql")
    assert rows == [
        {"a": "1", "b": "2", "c": "3"},
        {"a": "4", "b": "5", "c": "6"},
        {"a": "7", "b": "8", "c": "9"},
    ]


def test_insert_reader_null_and_numbers() -> None:
    sql = "INSERT INTO `T` VALUES (1,NULL,3.5);"
    assert iter_insert_rows(sql, "T", _COLS, "T.sql") == [{"a": "1", "b": None, "c": "3.5"}]


def test_insert_reader_quoted_escapes() -> None:
    # Backslash-n escape, doubled-quote escape, and a quoted comma/paren that
    # must NOT be read as tuple structure.
    sql = r"INSERT INTO `T` VALUES ('line1\nline2','it''s, (ok)','plain');"
    rows = iter_insert_rows(sql, "T", _COLS, "T.sql")
    assert rows == [{"a": "line1\nline2", "b": "it's, (ok)", "c": "plain"}]


def test_insert_reader_honors_statement_column_list() -> None:
    # Explicit (reordered) column list wins over the DDL default order.
    sql = "INSERT INTO `T` (`c`, `a`, `b`) VALUES ('cc','aa','bb');"
    assert iter_insert_rows(sql, "T", _COLS, "T.sql") == [{"a": "aa", "b": "bb", "c": "cc"}]


def test_insert_reader_ignores_other_tables_and_ddl() -> None:
    sql = (
        "CREATE TABLE `OTHER` (`x` INT);\n"
        "INSERT INTO `OTHER` VALUES (99);\n"
        "INSERT INTO `T` VALUES (1,2,3);"
    )
    assert iter_insert_rows(sql, "T", _COLS, "T.sql") == [{"a": "1", "b": "2", "c": "3"}]


def test_insert_reader_arity_mismatch_is_loud() -> None:
    sql = "INSERT INTO `T` VALUES (1,2);"  # 2 values, 3 columns
    with pytest.raises(ValueError, match=r"T\.sql.*2 values.*3 columns"):
        iter_insert_rows(sql, "T", _COLS, "T.sql")


def test_insert_reader_unterminated_string_is_loud() -> None:
    sql = "INSERT INTO `T` VALUES ('unterminated"
    with pytest.raises(ValueError, match=r"malformed INSERT in T\.sql"):
        iter_insert_rows(sql, "T", _COLS, "T.sql")


def test_malformed_value_does_not_leak_content() -> None:
    sql = "INSERT INTO `T` VALUES ('secret patient name"
    with pytest.raises(ValueError) as excinfo:
        iter_insert_rows(sql, "T", _COLS, "T.sql")
    assert "secret patient name" not in str(excinfo.value)


def test_insert_reader_quoted_cell_containing_insert_shape() -> None:
    # FINDING 3 regression: a quoted cell whose text is a literal
    # "INSERT INTO ... VALUES (...)" must NOT be re-scanned as a statement head.
    # Single-pass lexing consumes the string literal whole, so the statement
    # parses to ONE row with the embedded SQL text intact.
    sql = "INSERT INTO `T` VALUES ('INSERT INTO `T` VALUES (1,2,3)','x','y');"
    rows = iter_insert_rows(sql, "T", _COLS, "f.sql")
    assert rows == [{"a": "INSERT INTO `T` VALUES (1,2,3)", "b": "x", "c": "y"}]


def test_insert_reader_tolerates_apostrophe_in_comment() -> None:
    # A stray apostrophe in an inter-statement comment is filler, not a string
    # start — the scanner must not choke on it (real strings live only in
    # value tuples, which are consumed whole).
    sql = "-- the statement's column order\nINSERT INTO `T` VALUES (1,2,3);"
    assert iter_insert_rows(sql, "T", _COLS, "f.sql") == [{"a": "1", "b": "2", "c": "3"}]


def test_parse_column_names_skips_constraints() -> None:
    ddl = (
        "CREATE TABLE `T` (\n"
        "  `a` DOUBLE NOT NULL,\n"
        "  `b` VARCHAR(40),\n"
        "  PRIMARY KEY (`a`),\n"
        "  KEY `idx_b` (`b`)\n"
        ") ENGINE=InnoDB;"
    )
    assert parse_column_names(ddl, "T") == ["a", "b"]


# --- detection ----------------------------------------------------------------


def test_detect_positive() -> None:
    assert get_source("oracle-ehi").detect(ORACLE)
    assert detect_source(ORACLE) is get_source("oracle-ehi")


def test_detect_rejects_pf_fixture() -> None:
    assert not get_source("oracle-ehi").detect(PF)


def test_pf_does_not_detect_oracle_fixture() -> None:
    assert not get_source("pf-tebra").detect(ORACLE)


def test_detect_rejects_empty_dir(tmp_path: Path) -> None:
    assert not get_source("oracle-ehi").detect(tmp_path)


def test_data_file_without_schema_is_loud(tmp_path: Path) -> None:
    # A present data dump with no CREATE TABLE to key it = silent loss risk.
    (tmp_path / "v500" / "activity").mkdir(parents=True)
    (tmp_path / "v500" / "schema").mkdir()
    (tmp_path / "v500" / "activity" / "PERSON.sql").write_text(
        "INSERT INTO `PERSON` VALUES (900000001,'X');", encoding="latin-1"
    )
    with pytest.raises(ValueError, match="no CREATE TABLE"):
        read_export(tmp_path)


# --- full ingest --------------------------------------------------------------


@pytest.fixture(scope="module")
def records() -> dict[str, PatientRecord]:
    adapter = get_source("oracle-ehi")
    loaded = {r.patient.id: r for r in adapter.load(ORACLE)}
    assert len(loaded) == 2
    return loaded


def test_patient_identity_and_codes(records: dict[str, PatientRecord]) -> None:
    alpha = records[P1].patient
    assert alpha.family_name == "Testpatient, Alpha Q"
    assert alpha.birth_date == date(1985, 3, 14)
    assert alpha.sex == "Female"  # SEX_CD 362 resolved through CODE_VALUE
    assert alpha.identifier(IdentifierKind.SOURCE_GUID) == P1


def test_person_alias_preserved_losslessly(records: dict[str, PatientRecord]) -> None:
    # The brief does not let us assert "this is the MRN"; alias values survive
    # as OTHER identifiers tagged with their source column, never invented.
    alpha = records[P1].patient
    alias_values = {i.value for i in alpha.identifiers if i.kind is IdentifierKind.OTHER}
    assert "MRN900000001" in alias_values
    assert "FIN-ALPHA-001" in alias_values
    assert "oracle_ehi:PERSON_ALIAS" in alpha.extensions  # full rows also kept


def test_encounter_joins_and_type(records: dict[str, PatientRecord]) -> None:
    alpha = records[P1]
    assert {e.id for e in alpha.encounters} == {E1, E2}
    e1 = next(e for e in alpha.encounters if e.id == E1)
    assert e1.patient_id == P1
    assert e1.date_of_service == date(2024, 2, 5)
    assert e1.encounter_type == "Wellness Visit"  # ENCNTR_TYPE_CD via CODE_VALUE
    assert e1.chief_complaint == "Annual wellness visit"
    # ENCOUNTER_02 used a reordered explicit column list — joins must still work.
    e3 = records[P2].encounters[0]
    assert e3.id == E3 and e3.chief_complaint == "Chest pain evaluation"


def test_local_note_text_reaches_sections_and_document(
    records: dict[str, PatientRecord],
) -> None:
    e1 = next(e for e in records[P1].encounters if e.id == E1)
    narrative = e1.section(SectionKind.NARRATIVE)
    assert narrative is not None
    # Multi-blob document concatenated; '' escape decoded.
    assert "Patient reports feeling well" in (narrative.text or "")
    assert "Patient's questions addressed" in (narrative.text or "")
    # A DocumentArtifact carries the event title (§4.1 local document).
    local_docs = [d for d in records[P1].documents if d.mime_type == "text/plain"]
    assert any(d.title == "Progress Note" for d in local_docs)


def test_remote_blob_is_reference_not_fetched(records: dict[str, PatientRecord]) -> None:
    remote = next(d for d in records[P2].documents if d.extensions.get("oracle_ehi:storage_class"))
    assert remote.path is None  # never fetched (§4.2)
    assert remote.extensions["oracle_ehi:storage_class"] == "DICOM_SIUID"
    assert remote.extensions["oracle_ehi:BLOB_HANDLE"].startswith("1.2.840.10008")
    assert remote.title == "DICOM study (remote)"


def test_two_vitals_on_one_encounter_become_two_observations(
    records: dict[str, PatientRecord],
) -> None:
    # FINDING 1 regression (the collision probe's exact scenario): two measured
    # events on encounter E1 (Systolic BP 128, Body weight 150) map to TWO
    # distinct Observations, each keyed by its EVENT_ID, with BOTH values and
    # their unit codes preserved. The old shared-encounter-extensions fold made
    # the second value overwrite the first ("128" vanished).
    obs = {o.id: o for o in records[P1].observations}
    bp = obs["900300002"]
    weight = obs["900300003"]
    assert bp.value == "128" and weight.value == "150"  # both survive
    assert bp.encounter_id == E1 and weight.encounter_id == E1
    # Title matches a known vital → VITAL_SIGNS + the standard LOINC.
    assert bp.category is ObservationCategory.VITAL_SIGNS
    assert bp.code == "8480-6" and bp.display == "Systolic blood pressure"
    # Body weight's canonical LOINC is the predecessor's 3141-9 (dual-map port);
    # 29463-7 remains an accepted alias on the same VitalCode.
    assert weight.code == "3141-9"
    # The numeric unit code rides through (no CODE_VALUE label exists for it).
    assert bp.unit == "9001" and weight.unit == "9002"


def test_problem_event_becomes_condition(records: dict[str, PatientRecord]) -> None:
    # FINDING 1: a problem-shaped event (title prefix "Problem:", §3.2) maps to
    # a Condition keyed by EVENT_ID; the coded RESULT_VAL (I10) is preserved
    # losslessly (no ICD-10/SNOMED column is documented, so it is not typed).
    conditions = {c.id: c for c in records[P1].conditions}
    hypertension = conditions["900300004"]
    assert hypertension.display == "Problem: Essential hypertension"
    assert hypertension.extensions["oracle_ehi:RESULT_VAL"] == "I10"


def test_allergy_event_becomes_allergy_with_reaction(
    records: dict[str, PatientRecord],
) -> None:
    # FINDING 1: the Penicillin allergy lands as an AllergyIntolerance with the
    # Hives reaction preserved (RESULT_VAL → reaction), keyed by EVENT_ID.
    allergies = {a.id: a for a in records[P2].allergies}
    penicillin = allergies["900300005"]
    assert penicillin.substance == "Penicillin"
    assert penicillin.reactions == ["Hives"]
    # No allergen-category code is documented, so the category stays OTHER.
    assert penicillin.category is AllergyCategory.OTHER


def test_discrete_event_columns_ride_to_own_extensions(
    records: dict[str, PatientRecord],
) -> None:
    # Losslessness: each discrete event's unconsumed columns land on its OWN
    # record's extensions, never a shared dict — so distinct events' same-named
    # audit/relation columns coexist instead of overwriting.
    obs = {o.id: o for o in records[P1].observations}
    assert obs["900300002"].extensions["oracle_ehi:EVENT_RELTN_CD"] == "8002"
    assert obs["900300002"].extensions["oracle_ehi:UPDT_CNT"] == "1"
    # The collision used to fold these into encounter.extensions; that path is
    # now empty of per-event result columns.
    e1 = next(e for e in records[P1].encounters if e.id == E1)
    assert "oracle_ehi:RESULT_VAL" not in e1.extensions
    assert "oracle_ehi:RESULT_UNITS_CD" not in e1.extensions


def test_superseded_event_is_filtered_but_preserved(
    records: dict[str, PatientRecord],
) -> None:
    e1 = next(e for e in records[P1].encounters if e.id == E1)
    # Exactly one current note section (the superseded draft did not render).
    assert len([s for s in e1.sections if s.kind is SectionKind.NARRATIVE]) == 1
    assert "Earlier draft" not in (e1.section(SectionKind.NARRATIVE).text or "")  # type: ignore[union-attr]
    # ...but it is not lost: it rides to extensions.
    superseded = e1.extensions["oracle_ehi:CLINICAL_EVENT_superseded"]
    assert isinstance(superseded, list) and len(superseded) == 1


def test_superseded_blob_body_survives_ingest(
    records: dict[str, PatientRecord],
) -> None:
    # FINDING 2 regression: the superseded event preserves not just its ROW but
    # its CE_BLOB body. The superseded probe proved "Earlier draft of the
    # progress note." vanished; it must now ride in the stashed payload.
    e1 = next(e for e in records[P1].encounters if e.id == E1)
    payload = e1.extensions["oracle_ehi:CLINICAL_EVENT_superseded"][0]
    assert "Earlier draft of the progress note." in payload["oracle_ehi:CE_BLOB_body"]


def test_sentinel_dates_become_none(records: dict[str, PatientRecord]) -> None:
    # The superseded event's EVENT_END_DT_TM is the 1/1/0001 SQL min-date
    # sentinel; it lands in the superseded payload as a year-1 string that the
    # mapper's parse path turns to None wherever it is read as a real instant.
    e1 = next(e for e in records[P1].encounters if e.id == E1)
    payload = e1.extensions["oracle_ehi:CLINICAL_EVENT_superseded"][0]
    # The raw cell is preserved verbatim (lossless), but no rendered datetime
    # field on the encounter carries a year-1 instant.
    assert payload["oracle_ehi:EVENT_END_DT_TM"].startswith("1/1/0001")
    assert e1.last_modified_at is None


def test_deceased_status(records: dict[str, PatientRecord]) -> None:
    assert records[P2].patient.status == "Deceased"
    assert "oracle_ehi:DECEASED_DT_TM" in records[P2].patient.extensions


def test_losslessness_probe_unmapped_columns(records: dict[str, PatientRecord]) -> None:
    # Probe column 1: PERSON audit column UPDT_CNT is not mapped → extensions.
    alpha = records[P1].patient
    assert alpha.extensions["oracle_ehi:UPDT_CNT"] == "3"
    assert "oracle_ehi:UPDT_DT_TM" in alpha.extensions
    # Probe column 2: ENCOUNTER's discharge instant rides to extensions.
    e3 = records[P2].encounters[0]
    assert "oracle_ehi:DISCH_DT_TM" in e3.extensions
    assert "oracle_ehi:UPDT_CNT" in e3.extensions


def test_undecoded_compressed_blob_preserved(records: dict[str, PatientRecord]) -> None:
    e1 = next(e for e in records[P1].encounters if e.id == E1)
    undecoded = e1.extensions["oracle_ehi:CE_BLOB_undecoded"]
    assert isinstance(undecoded, list) and len(undecoded) == 1
    assert undecoded[0]["oracle_ehi:COMPRESSION_CD"] == "2099"
    # The undecodable PAYLOAD itself must survive (losslessness): a fix that
    # keeps only metadata while dropping the bytes fails here.
    assert any("COMPRESSED-BINARY-PLACEHOLDER" in str(v) for v in undecoded[0].values())


# --- blob decode contract -----------------------------------------------------


def test_decode_uncompressed_blob_returns_text() -> None:
    assert decode_ce_blob("<p>hello</p>", None) == "<p>hello</p>"
    assert decode_ce_blob(None, None) is None


def test_decode_compressed_blob_raises_with_citation() -> None:
    with pytest.raises(NotImplementedError, match=r"§8"):
        decode_ce_blob("anything", "2099")


def test_decode_error_message_is_phi_safe() -> None:
    with pytest.raises(NotImplementedError) as excinfo:
        decode_ce_blob("secret note body text", "2099")
    assert "secret note body text" not in str(excinfo.value)


# --- PHI-safe logging ---------------------------------------------------------


def test_compressed_blob_logs_type_not_content(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING, logger="anastomosis.sources.oracle_ehi.mapper")
    list(get_source("oracle-ehi").load(ORACLE))
    warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("NotImplementedError" in m for m in warnings)
    # The event id is a count/id (safe); blob content and the exception message
    # must never appear.
    blob = "\n".join(warnings)
    assert "COMPRESSED-BINARY-PLACEHOLDER" not in blob
    assert "compression algorithm" not in blob  # the exc message itself


# --- end to end: ingest -> reconstruct (FakeChromium) -> QA -------------------


def test_e2e_pipeline_renders_and_passes_qa(tmp_path: Path) -> None:
    fitz = pytest.importorskip("fitz", reason="e2e QA needs PyMuPDF (render extra)")
    from anastomosis.qa import Verdict, run_qa
    from anastomosis.reconstruct import discover_packs
    from anastomosis.reconstruct.engine import ReconstructionEngine

    class FakeRenderer:
        """Renders the HTML's visible text into a real one-page PDF so QA's
        text-extraction checks run against actual content (FakeChromium)."""

        def render(self, html: str, pdf_path: Path) -> None:
            text = _visible_text(html)
            doc = fitz.open()
            page = doc.new_page(width=612, height=792)
            page.insert_textbox(fitz.Rect(36, 36, 576, 756), text)
            doc.save(str(pdf_path))
            doc.close()

        def close(self) -> None:
            pass

    status = discover_packs()["generic_soap"]
    assert status.pack is not None, status.diagnosis
    records = list(get_source("oracle-ehi").load(ORACLE))

    engine = ReconstructionEngine(status.pack, FakeRenderer)  # type: ignore[arg-type]
    result = engine.run(records, tmp_path / "out")
    assert result.failed == []
    assert len(result.rendered) == len([e for r in records for e in r.encounters])

    by_encounter = {e.id: (e, r) for r in records for e in r.encounters}
    qa_inputs = [
        (doc.path, by_encounter[doc.encounter_id][0], by_encounter[doc.encounter_id][1])
        for doc in result.documents
    ]
    report = run_qa(qa_inputs)
    assert report.documents, "QA produced no document results"
    assert all(d.verdict is not Verdict.FAIL for d in report.documents), [
        f for d in report.documents for res in d.results for f in res.findings
    ]


def _visible_text(html: str) -> str:
    from anastomosis.core.textutil import html_to_text

    return html_to_text(html) or ""
