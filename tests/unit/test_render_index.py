# AI-assisted: written with Claude agents under the author's direction and review; see DESIGN.md.
"""Tests for the PDF→patient render index that the engine writes alongside
charts (PR-O — the patient-safety hardening that replaces the old
``{family}_{given}_`` filename-prefix guessing with explicit ``patient_id``
attribution)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from anastomosis.deliver.render_index import (
    INDEX_FILENAME,
    RenderEntry,
    RenderIndex,
)


def _entries() -> list[RenderEntry]:
    return [
        RenderEntry(
            pdf="Smith_John_05-10-2023_SOAP.pdf",
            patient_id="aaaa-0000-0000-0000-000000000001",
            encounter_id="encA-0000-0000",
        ),
        RenderEntry(
            pdf="Smith_John_03-15-2024_SOAP.pdf",
            patient_id="bbbb-0000-0000-0000-000000000002",
            encounter_id="encB-0000-0000",
        ),
        RenderEntry(
            pdf="Smith_John_07-04-2024_progress.pdf",
            patient_id="aaaa-0000-0000-0000-000000000001",
            encounter_id="encA2-0000-0000",
        ),
    ]


def test_render_index_round_trips_through_disk(tmp_path: Path) -> None:
    src = RenderIndex.from_entries(_entries())
    path = src.write(tmp_path)
    assert path == tmp_path / INDEX_FILENAME
    assert path.is_file()

    loaded = RenderIndex.load(tmp_path)
    assert loaded is not None
    # Entry-by-entry equality (set-equivalence is what matters; the dataclass
    # sorts by ``pdf`` at construction).
    assert {(e.pdf, e.patient_id, e.encounter_id) for e in loaded.entries} == {
        (e.pdf, e.patient_id, e.encounter_id) for e in src.entries
    }


def test_render_index_for_patient_groups_by_id(tmp_path: Path) -> None:
    index = RenderIndex.from_entries(_entries())
    a = "aaaa-0000-0000-0000-000000000001"
    b = "bbbb-0000-0000-0000-000000000002"
    # Patient A has two PDFs, patient B has one. Each patient sees ONLY
    # their own — the same-name cross-leak that motivates this module.
    assert set(index.for_patient(a)) == {
        "Smith_John_05-10-2023_SOAP.pdf",
        "Smith_John_07-04-2024_progress.pdf",
    }
    assert set(index.for_patient(b)) == {"Smith_John_03-15-2024_SOAP.pdf"}
    assert index.for_patient("cccc-not-indexed") == ()


def test_render_index_lookup_by_pdf_name(tmp_path: Path) -> None:
    index = RenderIndex.from_entries(_entries())
    entry = index.lookup("Smith_John_05-10-2023_SOAP.pdf")
    assert entry is not None
    assert entry.patient_id == "aaaa-0000-0000-0000-000000000001"
    assert entry.encounter_id == "encA-0000-0000"
    assert index.lookup("missing.pdf") is None


def test_render_index_unattributed_for_orphan_names(tmp_path: Path) -> None:
    index = RenderIndex.from_entries(_entries())
    orphans = index.unattributed(
        ["Smith_John_05-10-2023_SOAP.pdf", "stray.pdf", "another_stray.pdf"]
    )
    assert orphans == ("another_stray.pdf", "stray.pdf")


def test_render_index_load_returns_none_for_missing_file(tmp_path: Path) -> None:
    assert RenderIndex.load(tmp_path) is None  # no file written
    assert RenderIndex.load(tmp_path / "does-not-exist") is None  # no dir
    assert RenderIndex.load(None) is None


def test_render_index_load_returns_none_for_malformed_json(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    import logging

    (tmp_path / INDEX_FILENAME).write_text("{not valid json", encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="anastomosis.deliver.render_index"):
        assert RenderIndex.load(tmp_path) is None
    unreadable = [rec.message for rec in caplog.records if "unreadable" in rec.message]
    assert unreadable, "a corrupted index must be logged loudly, never silent"
    # Named by basename only — never the path under the output tree.
    assert all(INDEX_FILENAME in msg and str(tmp_path) not in msg for msg in unreadable)


def test_render_index_load_rejects_schema_mismatch(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    import logging

    (tmp_path / INDEX_FILENAME).write_text(
        json.dumps({"version": 99, "entries": []}), encoding="utf-8"
    )
    with caplog.at_level(logging.WARNING, logger="anastomosis.deliver.render_index"):
        assert RenderIndex.load(tmp_path) is None
    mismatch = [rec.message for rec in caplog.records if "schema mismatch" in rec.message]
    assert mismatch
    assert all(INDEX_FILENAME in msg and str(tmp_path) not in msg for msg in mismatch)


def test_render_index_load_skips_malformed_entries(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    import logging

    (tmp_path / INDEX_FILENAME).write_text(
        json.dumps(
            {
                "version": 1,
                "entries": [
                    {"pdf": "ok.pdf", "patient_id": "pid", "encounter_id": "eid"},
                    {"pdf": "bad.pdf"},  # missing required keys
                    "not-a-dict",
                ],
            }
        ),
        encoding="utf-8",
    )
    with caplog.at_level(logging.WARNING, logger="anastomosis.deliver.render_index"):
        index = RenderIndex.load(tmp_path)
    assert index is not None
    assert [e.pdf for e in index.entries] == ["ok.pdf"]
    # Each malformed row emits a warning so corruption is never silent.
    malformed = [rec.message for rec in caplog.records if "malformed entry" in rec.message]
    assert len(malformed) >= 2
    assert all(INDEX_FILENAME in msg and str(tmp_path) not in msg for msg in malformed)


def test_render_index_write_is_atomic_and_deterministic(tmp_path: Path) -> None:
    """Two writes of the same entries produce byte-identical JSON."""
    index = RenderIndex.from_entries(_entries())
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    index.write(a)
    index.write(b)
    assert (a / INDEX_FILENAME).read_bytes() == (b / INDEX_FILENAME).read_bytes()


def test_missing_render_index_never_attributes_pdf_by_filename(tmp_path: Path) -> None:
    """The explicit filename-attribution negative: two patients with the SAME
    display name, one prefix-matching PDF on disk, and NO sidecar — neither the
    archive nor the bundle deliverer may attach the PDF to either patient.
    The archive routes it to ``unattributed/``; the bundle delivers both
    patients with zero PDFs. Filename-prefix guessing must never resurface
    as a fallback path.
    """
    from datetime import date

    from anastomosis.core.model import Patient, PatientRecord
    from anastomosis.deliver.archive import ArchiveDeliverer
    from anastomosis.deliver.bundle import BundleDeliverer

    def _record(pid: str, dob: date) -> PatientRecord:
        return PatientRecord(
            patient=Patient(id=pid, family_name="Smith", given_name="John", birth_date=dob),
            encounters=[],
        )

    rec_a = _record("aaaa-0000-0000-0000-00000000000a", date(1980, 1, 1))
    rec_b = _record("bbbb-0000-0000-0000-00000000000b", date(1995, 6, 15))

    pdfs_dir = tmp_path / "charts"
    pdfs_dir.mkdir()
    orphan = "Smith_John_05-10-2023_SOAP.pdf"
    (pdfs_dir / orphan).write_bytes(b"%PDF-1.7 whose is this\n")
    assert RenderIndex.load(pdfs_dir) is None  # genuinely no sidecar

    # Archive: the PDF lands in unattributed/, and in NEITHER patient's slot.
    archive_out = tmp_path / "archive"
    archive_result = ArchiveDeliverer().deliver([rec_a, rec_b], pdfs_dir, archive_out)
    assert archive_result.pdf_count == 0
    for rec in (rec_a, rec_b):
        slot = archive_out / "patients" / rec.patient.id / "pdfs"
        assert not slot.exists() or not list(slot.glob("*.pdf")), (
            f"{orphan} was guessed onto {rec.patient.id} without an index"
        )
    assert [p.name for p in (archive_out / "unattributed").glob("*.pdf")] == [orphan]

    # Bundle: both patients deliver with zero PDFs (no unattributed slot,
    # never a guess).
    bundle_results = BundleDeliverer().deliver_records(
        [rec_a, rec_b], pdfs_dir, tmp_path / "bundles"
    )
    for result in bundle_results:
        assert result.pdf_paths == [], (
            f"bundle guessed {orphan} onto {result.patient_id} without an index"
        )


def test_engine_writes_render_index_at_end_of_run(tmp_path: Path) -> None:
    """The reconstruction engine must persist a render_index.json next to
    the rendered PDFs so the archive/bundle deliverers can attribute by
    patient_id (not by filename guessing).
    """
    import anastomosis.sources.pf_tebra  # noqa: F401 — registers adapter
    from anastomosis.reconstruct import discover_packs
    from anastomosis.reconstruct.engine import ReconstructionEngine
    from anastomosis.sources import get_source

    fixture = Path(__file__).resolve().parents[1] / "fixtures" / "pf_tebra_v9"
    records = list(get_source("pf-tebra").load(fixture))
    pack_status = discover_packs()["generic_soap"]
    assert pack_status.pack is not None

    log: list[tuple[str, Path]] = []

    class _FakeRenderer:
        def render(self, html: str, pdf_path: Path) -> None:
            pdf_path.write_bytes(b"%PDF-1.7 fake")
            log.append((html, pdf_path))

        def close(self) -> None: ...

    engine = ReconstructionEngine(pack_status.pack, _FakeRenderer)
    out = tmp_path / "charts"
    result = engine.run(records, out)
    assert result.rendered or result.documents

    loaded = RenderIndex.load(out)
    assert loaded is not None, "engine must write render_index.json"
    # The index covers every rendered (or skipped) doc with the patient_id
    # and encounter_id the engine saw — no downstream guessing required.
    indexed = {(e.pdf, e.patient_id, e.encounter_id) for e in loaded.entries}
    expected = {(doc.path.name, doc.patient_id, doc.encounter_id) for doc in result.documents}
    assert indexed == expected, f"index drift: {indexed} != {expected}"
