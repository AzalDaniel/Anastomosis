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
    PDF to its owning patient by ``patient_id`` (the PR-O regression fix)."""
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
    """The patient-safety regression Codex flagged for the bundle deliverer:
    two distinct patients sharing both ``family_name`` and ``given_name`` must
    each receive only their own PDFs. Pre-PR-O the deliverer bucketed by
    ``{family}_{given}_`` prefix and silently mixed them.
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
    assert any("no render index" in rec.message for rec in caplog.records)


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


def test_bundle_no_qa_report_means_no_qa_file(tmp_path: Path, records: list[PatientRecord]) -> None:
    out = tmp_path / "bundles"
    deliverer = BundleDeliverer()
    deliverer.deliver(records[0], None, out)
    assert not (out / records[0].patient.id / "qa_report.json").exists()


def test_bundle_handles_missing_pdfs(tmp_path: Path, records: list[PatientRecord]) -> None:
    out = tmp_path / "bundles"
    result = BundleDeliverer().deliver(records[0], None, out)
    assert result.pdf_paths == []
    assert result.bundle_path.is_file()
    assert result.readme_path is not None and result.readme_path.is_file()
