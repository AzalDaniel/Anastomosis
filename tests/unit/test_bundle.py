"""Tests for the per-patient bundle deliverer (Responder persona)."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

import anastomosis.sources.pf_tebra  # noqa: F401 — registers the source adapter
from anastomosis.core.model import PatientRecord
from anastomosis.deliver.bundle import BundleDeliverer
from anastomosis.qa import CheckResult, DocumentQA, QAReport, Verdict
from anastomosis.sources import get_source

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "pf_tebra_v9"


@pytest.fixture
def records() -> list[PatientRecord]:
    return list(get_source("pf-tebra").load(FIXTURE))


def _fake_pdfs(records: list[PatientRecord], pdfs_dir: Path) -> list[Path]:
    """One ``%PDF-1.7 fake`` file per encounter, using the engine's name shape."""
    pdfs_dir.mkdir(parents=True, exist_ok=True)
    out: list[Path] = []
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
    return out


def test_bundle_per_patient_layout(tmp_path: Path, records: list[PatientRecord]) -> None:
    pdfs_dir = tmp_path / "charts"
    pdfs = _fake_pdfs(records, pdfs_dir)
    out = tmp_path / "bundles"

    deliverer = BundleDeliverer(generator="anastomosis test")
    for record in records:
        deliverer.deliver(record, pdfs, out)

    # One subdir per patient, each with the expected files.
    subdirs = sorted(p.name for p in out.iterdir() if p.is_dir())
    expected = sorted(record.patient.id for record in records)
    # Subdirs are safe-id versions of patient ids; the synthetic fixture uses
    # plain ASCII GUIDs so the round-trip is identity.
    assert subdirs == expected

    for record in records:
        patient_dir = out / record.patient.id
        assert (patient_dir / "bundle.json").is_file()
        assert (patient_dir / "README.txt").is_file()
        pdfs_subdir = patient_dir / "pdfs"
        if pdfs_subdir.exists():
            for pdf in pdfs_subdir.glob("*.pdf"):
                # PDFs in this patient's slot must be named after this patient.
                expected_prefix = pdf.name.split("_", 2)[:2]
                assert expected_prefix[0] == (record.patient.family_name or "")
                assert expected_prefix[1] == (record.patient.given_name or "")


def test_bundle_pdfs_never_cross_patient(tmp_path: Path, records: list[PatientRecord]) -> None:
    """A PDF for patient A must never appear inside patient B's pdfs/ dir."""
    pdfs_dir = tmp_path / "charts"
    pdfs = _fake_pdfs(records, pdfs_dir)
    out = tmp_path / "bundles"
    deliverer = BundleDeliverer()
    for record in records:
        deliverer.deliver(record, pdfs, out)

    # Build the truth table: filename → owning patient's family_given prefix.
    truth: dict[str, str] = {}
    for record in records:
        if not (record.patient.family_name and record.patient.given_name):
            continue
        family = re.sub(r"[^A-Za-z0-9_-]+", "_", record.patient.family_name.strip()).strip("_")
        given = re.sub(r"[^A-Za-z0-9_-]+", "_", record.patient.given_name.strip()).strip("_")
        truth[record.patient.id] = f"{family}_{given}_"

    for record_id, expected_prefix in truth.items():
        pdfs_subdir = out / record_id / "pdfs"
        if not pdfs_subdir.exists():
            continue
        for pdf in pdfs_subdir.glob("*.pdf"):
            assert pdf.name.startswith(expected_prefix), (
                f"PDF {pdf.name} leaked into {record_id}/pdfs/ (expected prefix {expected_prefix})"
            )


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
