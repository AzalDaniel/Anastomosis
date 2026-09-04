"""Tests for the per-patient bundle deliverer (Responder persona)."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

import anastomosis.sources.pf_tebra  # noqa: F401 — registers the source adapter
from anastomosis.core.model import PatientRecord
from anastomosis.deliver.bundle import BundleDeliverer
from anastomosis.deliver.render_index import RenderEntry, RenderIndex
from anastomosis.qa import CheckResult, DocumentQA, QAReport, Verdict
from anastomosis.sources import get_source

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "pf_tebra_v9"


@pytest.fixture
def records() -> list[PatientRecord]:
    return list(get_source("pf-tebra").load(FIXTURE))


def _fake_pdfs(records: list[PatientRecord], pdfs_dir: Path) -> list[Path]:
    """One ``%PDF-1.7 fake`` file per encounter, using the engine's name shape,
    plus a render-index sidecar so the bundle deliverer can attribute each
    PDF to its owning patient by ``patient_id`` — explicit attribution, not
    filename-prefix guessing."""
    pdfs_dir.mkdir(parents=True, exist_ok=True)
    out: list[Path] = []
    entries: list[RenderEntry] = []
    for record in records:
        family = re.sub(r"[^A-Za-z0-9_-]+", "_", (record.patient.family_name or "").strip()).strip(
            "_"
        )
        given = re.sub(r"[^A-Za-z0-9_-]+", "_", (record.patient.given_name or "").strip()).strip(
            "_"
        )
        if not (family and given):
            continue
        prefix = f"{family}_{given}_"
        seen: set[str] = set()
        for encounter in record.encounters:
            dos = (
                encounter.date_of_service.strftime("%m-%d-%Y")
                if encounter.date_of_service
                else "undated"
            )
            note_type = re.sub(r"[^A-Za-z0-9_-]+", "_", (encounter.note_type or "note")).strip("_")
            name = f"{prefix}{dos}_{note_type}.pdf"
            if name in seen:
                suffix = encounter.id.replace("-", "")[:8]
                name = f"{prefix}{dos}_{note_type}-{suffix}.pdf"
            seen.add(name)
            path = pdfs_dir / name
            path.write_bytes(b"%PDF-1.7 fake\n")
            out.append(path)
            entries.append(
                RenderEntry(
                    pdf=name,
                    patient_id=record.patient.id,
                    encounter_id=encounter.id,
                )
            )
    RenderIndex.from_entries(entries).write(pdfs_dir)
    return out


def test_bundle_per_patient_layout(tmp_path: Path, records: list[PatientRecord]) -> None:
    pdfs_dir = tmp_path / "charts"
    _fake_pdfs(records, pdfs_dir)
    out = tmp_path / "bundles"

    deliverer = BundleDeliverer(generator="anastomosis test")
    results = deliverer.deliver_records(records, pdfs_dir, out)
    assert len(results) == len(records)

    subdirs = sorted(p.name for p in out.iterdir() if p.is_dir())
    expected = sorted(record.patient.id for record in records)
    assert subdirs == expected

    for record in records:
        patient_dir = out / record.patient.id
        assert (patient_dir / "bundle.json").is_file()
        assert (patient_dir / "README.txt").is_file()
        pdfs_subdir = patient_dir / "pdfs"
        if pdfs_subdir.exists():
            for pdf in pdfs_subdir.glob("*.pdf"):
                # PDFs in this patient's slot must be the ones the engine
                # actually wrote for this patient. Filename prefix matches
                # patient name today because the synthetic fixture has no
                # same-name patients; the source of truth is the index.
                expected_prefix = pdf.name.split("_", 2)[:2]
                assert expected_prefix[0] == (record.patient.family_name or "")
                assert expected_prefix[1] == (record.patient.given_name or "")


def test_bundle_same_name_patients_never_cross_attribute(tmp_path: Path) -> None:
    """The cross-leak failure mode for the bundle deliverer:
    two distinct patients sharing both ``family_name`` and ``given_name`` must
    each receive only their own PDFs. A deliverer that bucketed by the
    ``{family}_{given}_`` prefix would silently mix them.
    """
    from datetime import date

    from anastomosis.core.model import Encounter, Patient, PatientRecord

    def _make(pid: str, dos: date, enc_id: str) -> PatientRecord:
        return PatientRecord(
            patient=Patient(
                id=pid, family_name="Smith", given_name="John", birth_date=date(1980, 1, 1)
            ),
            encounters=[
                Encounter(id=enc_id, patient_id=pid, date_of_service=dos, note_type="SOAP")
            ],
        )

    rec_a = _make("aaaa-0000-0000-0000-000000000001", date(2023, 5, 10), "encA-0000-0000")
    rec_b = _make("bbbb-0000-0000-0000-000000000002", date(2024, 3, 15), "encB-0000-0000")

    pdfs_dir = tmp_path / "charts"
    pdfs_dir.mkdir()
    pdf_a = pdfs_dir / "Smith_John_05-10-2023_SOAP.pdf"
    pdf_b = pdfs_dir / "Smith_John_03-15-2024_SOAP.pdf"
    pdf_a.write_bytes(b"%PDF-1.7 patient A\n")
    pdf_b.write_bytes(b"%PDF-1.7 patient B\n")
    RenderIndex.from_entries(
        [
            RenderEntry(pdf=pdf_a.name, patient_id=rec_a.patient.id, encounter_id="encA-0000-0000"),
            RenderEntry(pdf=pdf_b.name, patient_id=rec_b.patient.id, encounter_id="encB-0000-0000"),
        ]
    ).write(pdfs_dir)

    results = BundleDeliverer().deliver_records([rec_a, rec_b], pdfs_dir, tmp_path / "bundles")
    by_pid = {r.patient_id: r for r in results}
    assert [p.name for p in by_pid[rec_a.patient.id].pdf_paths] == [pdf_a.name]
    assert [p.name for p in by_pid[rec_b.patient.id].pdf_paths] == [pdf_b.name]


def test_bundle_missing_index_skips_pdfs_loudly(
    tmp_path: Path, records: list[PatientRecord], caplog: pytest.LogCaptureFixture
) -> None:
    """When ``pdfs_dir`` has PDFs but no ``render_index.json``, the bundle
    deliverer refuses to guess: every record gets zero PDFs and a WARNING
    naming the missing-index condition is logged."""
    import logging

    pdfs_dir = tmp_path / "charts"
    pdfs_dir.mkdir()
    (pdfs_dir / "Smith_John_05-10-2023_SOAP.pdf").write_bytes(b"%PDF-1.7 unindexed\n")

    with caplog.at_level(logging.WARNING, logger="anastomosis.deliver.bundle.bundle"):
        results = BundleDeliverer().deliver_records(records, pdfs_dir, tmp_path / "bundles")
    for r in results:
        assert r.pdf_paths == []
    warnings = [rec.message for rec in caplog.records if "no render index" in rec.message]
    assert warnings
    # The warning stands alone — the pdfs dir path (under the output tree) is gone.
    assert all(str(pdfs_dir) not in msg for msg in warnings)


def test_bundle_qa_slice_isolates_each_patient(
    tmp_path: Path, records: list[PatientRecord]
) -> None:
    """A QA report covering every patient is sliced per-patient so each
    bundle's ``qa_report.json`` mentions only that patient's encounters."""
    out = tmp_path / "bundles"

    # Build a fake QA report with one entry per encounter across all records.
    docs: list[DocumentQA] = []
    for record in records:
        for encounter in record.encounters:
            docs.append(
                DocumentQA(
                    path=tmp_path / f"{encounter.id}.pdf",
                    encounter_id=encounter.id,
                    results=[CheckResult(check="synthetic", verdict=Verdict.PASS, findings=[])],
                )
            )
    qa_report = QAReport(documents=docs)

    deliverer = BundleDeliverer()
    for record in records:
        deliverer.deliver(record, None, out, qa_report=qa_report)

    seen_ids: set[str] = set()
    for record in records:
        slice_path = out / record.patient.id / "qa_report.json"
        assert slice_path.is_file()
        payload = json.loads(slice_path.read_text(encoding="utf-8"))
        slice_encs = {doc["encounter_id"] for doc in payload["documents"]}
        expected_encs = {encounter.id for encounter in record.encounters}
        assert slice_encs == expected_encs, (
            f"qa slice for {record.patient.id} carried {slice_encs - expected_encs} "
            f"and was missing {expected_encs - slice_encs}"
        )
        assert payload["patient_id"] == record.patient.id
        # Cross-patient leakage check: each encounter id appears in exactly
        # one slice across all bundles.
        assert seen_ids.isdisjoint(slice_encs), (
            f"encounter ids {seen_ids & slice_encs} appeared in more than one slice"
        )
        seen_ids.update(slice_encs)


def test_bundle_qa_slice_carries_the_record_summarys_verdict(
    tmp_path: Path, records: list[PatientRecord]
) -> None:
    """The whole-patient page's verdict rides in the bundle that holds the page.

    A record summary grades a chart rather than a visit, so its row carries the
    PATIENT id where an encounter row carries an encounter id — the same
    stand-in the upload manifest and the export's encounter check use. Slicing
    on the record's encounter ids alone dropped exactly that row: an ordinary
    export put three rows in ``charts/qa_report.json`` and two in the bundle,
    and a patient whose whole chart is a scan got a well-formed report with no
    rows at all beside the only page it carries. An empty report reads as
    "nothing to say" rather than "the verdict for this page is missing" (#399).

    The two patients here are deliberately in one report: a row keyed on ONE
    patient's id must not reach the OTHER patient's bundle, which is the
    isolation the sibling test above holds for encounter rows.
    """
    out = tmp_path / "bundles"
    docs: list[DocumentQA] = [
        DocumentQA(
            path=tmp_path / f"{record.patient.id}_summary.pdf",
            encounter_id=record.patient.id,
            results=[CheckResult(check="synthetic", verdict=Verdict.PASS, findings=[])],
        )
        for record in records
    ]
    qa_report = QAReport(documents=docs)

    deliverer = BundleDeliverer()
    for record in records:
        deliverer.deliver(record, None, out, qa_report=qa_report)

    for record in records:
        payload = json.loads(
            (out / record.patient.id / "qa_report.json").read_text(encoding="utf-8")
        )
        keys = {doc["encounter_id"] for doc in payload["documents"]}
        assert keys == {record.patient.id}, (
            f"the summary row for {record.patient.id} did not reach its own bundle, "
            f"or another patient's did: {keys}"
        )


def test_bundle_no_qa_report_means_no_qa_file(tmp_path: Path, records: list[PatientRecord]) -> None:
    out = tmp_path / "bundles"
    deliverer = BundleDeliverer()
    deliverer.deliver(records[0], None, out)
    assert not (out / records[0].patient.id / "qa_report.json").exists()


def test_bundle_long_patient_id_stays_writable(
    tmp_path: Path, records: list[PatientRecord]
) -> None:
    """A source id longer than the filesystem allows still delivers: the
    directory name is cut (with its hash tag) instead of raising OSError."""
    long_id = "feedface-0000-0000-0000-0000000000aa" + "z" * 300
    patient = records[0].patient.model_copy(update={"id": long_id})
    record = records[0].model_copy(update={"patient": patient})

    out = tmp_path / "bundles"
    result = BundleDeliverer().deliver(record, None, out)
    assert result.out_dir.is_dir()
    assert result.bundle_path.is_file()
    assert len(result.patient_id) < len(long_id)


def test_bundle_handles_missing_pdfs(tmp_path: Path, records: list[PatientRecord]) -> None:
    out = tmp_path / "bundles"
    result = BundleDeliverer().deliver(records[0], None, out)
    assert result.pdf_paths == []
    assert result.bundle_path.is_file()
    assert result.readme_path is not None and result.readme_path.is_file()


def test_bundle_budgets_the_copied_chart_name(tmp_path: Path) -> None:
    """A renderer-length chart name must be DELIVERED, not warned about.

    Unbudgeted, ``pdfs/<617-char name>.pdf`` under a bundle directory blows the
    Windows path budget; the copy fails, the deliverer logs "pdf copy failed"
    and continues, and the operator hands over a bundle with a chart missing.
    """
    from datetime import date

    from anastomosis.core.model import Encounter, Patient, PatientRecord
    from anastomosis.core.textutil import MAX_PATH_CHARS

    pid = "feedface-0000-0000-0000-0000000000aa"
    record = PatientRecord(
        patient=Patient(id=pid, family_name="Fixture", given_name="Ada"),
        encounters=[
            Encounter(
                id="feedface-e000-0000-0000-0000000000aa",
                patient_id=pid,
                date_of_service=date(2023, 5, 10),
            )
        ],
    )

    pdfs_dir = tmp_path / "charts"
    pdfs_dir.mkdir()
    chart = pdfs_dir / f"Fixture_Ada_05-10-2023_{'S' * 200}.pdf"
    chart.write_bytes(b"%PDF-1.7 fake\n")

    result = BundleDeliverer().deliver(record, [chart], tmp_path / "bundles")

    assert len(result.pdf_paths) == 1
    delivered = result.pdf_paths[0]
    assert delivered.is_file()
    assert delivered.suffix == ".pdf"
    assert len(str(delivered)) <= MAX_PATH_CHARS
    assert delivered.read_bytes() == chart.read_bytes()


def test_bundle_refuses_two_patient_ids_that_sanitize_alike(tmp_path: Path) -> None:
    """``MRN 1234`` and ``MRN/1234`` both sanitize to ``MRN_1234``; the bundle
    writers are exist_ok/overwrite, so a silent collision would deliver ONE
    directory holding the second patient's record over the first."""
    from anastomosis.core.model import Patient, PatientRecord
    from anastomosis.deliver._shared import DeliveredNameCollision

    records = [
        PatientRecord(patient=Patient(id="MRN 1234", family_name="Fixture", given_name="Ada")),
        PatientRecord(patient=Patient(id="MRN/1234", family_name="Sample", given_name="Boris")),
    ]

    with pytest.raises(DeliveredNameCollision, match="patient directory"):
        BundleDeliverer().deliver_records(records, None, tmp_path / "bundles")


def test_bundle_standalone_deliver_still_works_without_a_ledger(
    tmp_path: Path, records: list[PatientRecord]
) -> None:
    """``deliver`` is a public single-record entry point: called without the
    per-run ledger it must behave exactly as before (one record cannot collide
    with itself), so a caller outside ``deliver_records`` is never broken."""
    out = tmp_path / "bundles"
    first = BundleDeliverer().deliver(records[0], None, out)
    again = BundleDeliverer().deliver(records[0], None, out)
    assert first.patient_id == again.patient_id
    assert again.bundle_path.is_file()


# --- the source's own documents ride along (#110) ----------------------------


def _charts_with_attachments(tmp_path: Path, records: list[PatientRecord]) -> Path:
    """A charts directory holding the attachments a run would have carried."""
    from anastomosis.pipeline import ATTACHMENTS_DIRNAME

    charts = tmp_path / "charts"
    landing = charts / ATTACHMENTS_DIRNAME
    landing.mkdir(parents=True)
    for record in records:
        for doc in record.documents:
            if doc.path:
                (landing / Path(doc.path).name).write_bytes((FIXTURE / doc.path).read_bytes())
    return charts


def test_a_bundle_carries_the_documents_its_charts_reference(tmp_path: Path) -> None:
    """A record request answered without them cites scans the bundle lacks."""
    records = list(get_source("pf-tebra").load(FIXTURE))
    charts = _charts_with_attachments(tmp_path, records)
    expected = {
        record.patient.id: sorted(Path(d.path).name for d in record.documents if d.path)
        for record in records
    }
    assert any(expected.values()), "the fixture no longer exercises this path"

    results = BundleDeliverer().deliver_records(records, charts, tmp_path / "bundles")

    by_patient = {r.patient_id: r for r in results}
    for patient_id, names in expected.items():
        result = by_patient[patient_id]
        assert sorted(p.name for p in result.attachment_paths) == names, patient_id
        for path in result.attachment_paths:
            assert path.is_file()
            assert path.parent.parent.name == patient_id, "delivered into another patient's bundle"


def test_a_bundle_without_the_carried_attachments_still_delivers(tmp_path: Path) -> None:
    """Conservation belongs to the run, so this reports rather than refuses.

    `pipeline._carry_attachments` knows the export and stops a run whose
    attachments did not all arrive. A charts directory with none means it was
    assembled without that step — the bundle says so and delivers the charts,
    rather than putting a precondition on a public entry point.
    """
    records = list(get_source("pf-tebra").load(FIXTURE))
    charts = tmp_path / "charts"
    charts.mkdir()

    results = BundleDeliverer().deliver_records(records, charts, tmp_path / "bundles")

    assert results, "the bundles were still written"
    assert all(r.attachment_paths == [] for r in results)


def test_two_documents_both_land_without_overwriting_each_other(tmp_path: Path) -> None:
    """Every document a record names gets its own slot in the bundle.

    The names cannot collide outright — `_attachments_for` reads them from one
    directory, and a directory cannot hold two files with one name. What CAN
    collide is the DELIVERED name, because it is budgeted to fit the bundle's
    path depth and two long names can be cut to the same thing. That case
    raises through the shared ledger rather than filing one scan over another;
    this pins the ordinary case, where both simply land.
    """
    landing = tmp_path / "attachments"
    landing.mkdir()
    (landing / "referral.pdf").write_bytes(b"%PDF-1.4 one\n")
    (landing / "labs.pdf").write_bytes(b"%PDF-1.4 two\n")
    patient_dir = tmp_path / "bundle"
    patient_dir.mkdir()

    copied, landed = BundleDeliverer()._copy_attachments(
        [
            ("feedface-doc0-0000-0000-000000000001", landing / "referral.pdf"),
            ("feedface-doc0-0000-0000-000000000002", landing / "labs.pdf"),
        ],
        patient_dir,
    )

    assert sorted(p.name for p in copied) == ["labs.pdf", "referral.pdf"]
    assert {p.read_bytes() for p in copied} == {b"%PDF-1.4 one\n", b"%PDF-1.4 two\n"}
    assert set(landed) == {
        "feedface-doc0-0000-0000-000000000001",
        "feedface-doc0-0000-0000-000000000002",
    }
    assert landed["feedface-doc0-0000-0000-000000000001"].url == "attachments/referral.pdf"
    assert landed["feedface-doc0-0000-0000-000000000002"].url == "attachments/labs.pdf"


# --- #382: the bundle's own FHIR rendition names the files beside it ---------

_DATA_ABSENT_REASON_EXT = "http://hl7.org/fhir/StructureDefinition/data-absent-reason"


def test_the_two_artifact_fixture_resolves_through_the_real_cli(tmp_path: Path) -> None:
    """Driven through the real CLI, the two-artifact fixture (one embedded
    B64 2-page PDF, one ``<reference>``d 1-page PDF, one patient, no
    encounters): both attachments land on disk (#372/#380), and now each
    DocumentReference's ``url`` resolves to one of them from ``bundle.json``'s
    own directory, with ``size`` and ``hash`` matching what is actually there.
    RED on main: both Attachments carried contentType only, url/size/hash all
    ``None``.
    """
    import base64
    import hashlib

    from test_ccda_delivered_documents import _embedded_and_referenced_export
    from typer.testing import CliRunner

    from anastomosis.cli import app

    export = tmp_path / "export"
    charts = tmp_path / "charts"
    bundles = tmp_path / "bundles"
    _embedded_and_referenced_export(export)

    result = CliRunner().invoke(
        app,
        [
            "pipeline",
            "run",
            str(export),
            "--source",
            "ccda",
            "--out",
            str(charts),
            "--no-qa",
            "--bundle",
            str(bundles),
        ],
    )
    assert result.exit_code == 0, result.output

    (patient_dir,) = [p for p in bundles.iterdir() if p.is_dir()]
    bundle = json.loads((patient_dir / "bundle.json").read_text())
    docrefs = [
        e["resource"]
        for e in bundle["entry"]
        if e["resource"]["resourceType"] == "DocumentReference" and "context" not in e["resource"]
    ]
    assert len(docrefs) == 2, "one DocumentReference per carried artifact"

    on_disk = {p.name for p in (patient_dir / "attachments").glob("*")}
    assert len(on_disk) == 2, "both the embedded and the referenced artifact are on disk"

    for docref in docrefs:
        attachment = docref["content"][0]["attachment"]
        assert "data" not in attachment, "the bytes are not doubled inline"
        url = attachment["url"]
        assert url.startswith("attachments/") and "\\" not in url
        assert not url.startswith(("/", "file://"))
        name = url.removeprefix("attachments/")
        assert name in on_disk, f"{url} does not resolve from bundle.json's own directory"
        data = (patient_dir / "attachments" / name).read_bytes()
        assert attachment["size"] == len(data)
        assert attachment["hash"] == base64.b64encode(hashlib.sha256(data).digest()).decode("ascii")


def test_two_artifacts_naming_one_carried_file_share_one_copy(tmp_path: Path) -> None:
    """Two artifacts naming one carried file are one file on disk and two
    working references to it — no duplicate entry, no second copy."""
    from anastomosis.core.model import DocumentArtifact, Patient

    charts = tmp_path / "charts"
    landing = charts / "attachments"
    landing.mkdir(parents=True)
    (landing / "shared.pdf").write_bytes(b"%PDF-1.4 shared\n")

    pid = "feedface-0000-4000-8000-000000000382"
    ids = ("feedface-doc0-0000-0000-000000000001", "feedface-doc0-0000-0000-000000000002")
    record = PatientRecord(
        patient=Patient(id=pid, given_name="Two", family_name="Refs"),
        documents=[
            DocumentArtifact(id=ids[0], patient_id=pid, path="shared.pdf", title="Referral"),
            DocumentArtifact(id=ids[1], patient_id=pid, path="shared.pdf", title="Referral (2)"),
        ],
    )

    (result,) = BundleDeliverer().deliver_records([record], charts, tmp_path / "bundles")

    on_disk = list(result.out_dir.glob("attachments/*"))
    assert [p.name for p in on_disk] == ["shared.pdf"], "one file on disk, not a second copy"
    bundle = json.loads(result.bundle_path.read_text())
    docrefs = {
        e["resource"]["id"]: e["resource"]
        for e in bundle["entry"]
        if e["resource"]["resourceType"] == "DocumentReference" and e["resource"]["id"] in ids
    }
    assert set(docrefs) == set(ids)
    attachments = [d["content"][0]["attachment"] for d in docrefs.values()]
    assert {a["url"] for a in attachments} == {"attachments/shared.pdf"}, (
        "both references resolve to the one file"
    )
    assert len({a["hash"] for a in attachments}) == 1, "one file, measured once"


def test_two_artifacts_naming_one_file_reuse_one_measurement_object(tmp_path: Path) -> None:
    """Reuse, not just agreement: two independently-computed digests over the
    same bytes are numerically equal anyway (hashing is deterministic), so
    that alone would not prove the second artifact skipped its own re-hash.
    Identity does: both ids get the exact object the first measured.
    """
    landing = tmp_path / "attachments"
    landing.mkdir()
    (landing / "shared.pdf").write_bytes(b"%PDF-1.4 shared\n")
    patient_dir = tmp_path / "bundle"
    patient_dir.mkdir()

    _, landed = BundleDeliverer()._copy_attachments(
        [
            ("feedface-doc0-0000-0000-000000000001", landing / "shared.pdf"),
            ("feedface-doc0-0000-0000-000000000002", landing / "shared.pdf"),
        ],
        patient_dir,
    )

    first = landed["feedface-doc0-0000-0000-000000000001"]
    second = landed["feedface-doc0-0000-0000-000000000002"]
    assert first is second, "the second artifact reused the first's measurement"


def test_a_document_whose_file_did_not_land_says_so_plainly(tmp_path: Path) -> None:
    """Entered for real: the charts directory carries only ONE of the two
    documents this record names. The bundle still delivers — conservation of
    the FULL export belongs to the run
    (see ``test_a_bundle_without_the_carried_attachments_still_delivers``) —
    but the DocumentReference for the file that did not land says so
    explicitly, in the field a FHIR consumer already knows to check for
    absent data, rather than shipping silent nulls indistinguishable from
    "nobody checked".
    """
    from anastomosis.core.model import DocumentArtifact, Patient

    charts = tmp_path / "charts"
    landing = charts / "attachments"
    landing.mkdir(parents=True)
    (landing / "present.pdf").write_bytes(b"%PDF-1.4 present\n")
    # "absent.pdf" is named by the record but never lands here.

    pid = "feedface-0000-4000-8000-000000000382"
    present_id = "feedface-doc0-0000-0000-000000000001"
    absent_id = "feedface-doc0-0000-0000-000000000002"
    record = PatientRecord(
        patient=Patient(id=pid, given_name="One", family_name="Missing"),
        documents=[
            DocumentArtifact(id=present_id, patient_id=pid, path="present.pdf"),
            DocumentArtifact(id=absent_id, patient_id=pid, path="absent.pdf"),
        ],
    )

    (result,) = BundleDeliverer().deliver_records([record], charts, tmp_path / "bundles")

    assert result.bundle_path.is_file(), "the bundle still delivers"
    bundle = json.loads(result.bundle_path.read_text())
    docrefs = {
        e["resource"]["id"]: e["resource"]
        for e in bundle["entry"]
        if e["resource"]["resourceType"] == "DocumentReference"
        and e["resource"]["id"] in (present_id, absent_id)
    }

    present_attachment = docrefs[present_id]["content"][0]["attachment"]
    assert present_attachment["url"] == "attachments/present.pdf"

    absent_attachment = docrefs[absent_id]["content"][0]["attachment"]
    assert "url" not in absent_attachment
    assert "size" not in absent_attachment
    assert "hash" not in absent_attachment
    assert absent_attachment["extension"] == [
        {"url": _DATA_ABSENT_REASON_EXT, "valueCode": "error"}
    ]


def test_two_deliveries_of_one_record_produce_byte_identical_bundle_json(tmp_path: Path) -> None:
    """One record object, delivered twice (necessary and not sufficient — see
    ``deliver.ccda_export``'s own ``test_two_builds_are_byte_identical``):
    pins that threading measured attachments through does not itself add
    dict-ordering nondeterminism to the ``sort_keys=True, indent=2`` JSON.
    """
    from anastomosis.core.model import DocumentArtifact, Patient

    charts = tmp_path / "charts"
    landing = charts / "attachments"
    landing.mkdir(parents=True)
    (landing / "a.pdf").write_bytes(b"%PDF-1.4 a\n")
    (landing / "b.pdf").write_bytes(b"%PDF-1.4 b\n")

    pid = "feedface-0000-4000-8000-000000000382"
    record = PatientRecord(
        patient=Patient(id=pid, given_name="Det", family_name="Erministic"),
        documents=[
            DocumentArtifact(
                id="feedface-doc0-0000-0000-000000000001", patient_id=pid, path="a.pdf"
            ),
            DocumentArtifact(
                id="feedface-doc0-0000-0000-000000000002", patient_id=pid, path="b.pdf"
            ),
        ],
    )

    deliverer = BundleDeliverer()
    (first,) = deliverer.deliver_records([record], charts, tmp_path / "one")
    (second,) = deliverer.deliver_records([record], charts, tmp_path / "two")

    assert first.bundle_path.read_bytes() == second.bundle_path.read_bytes()
