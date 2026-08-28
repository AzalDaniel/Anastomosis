"""Attachments named by the records reach the run's output, or the run stops.

A `DocumentArtifact` with a `path` names a real file in the export — a scanned
referral, a lab report, the pages a chart is incomplete without. #175 taught the
source adapter to find those files; nothing carried them out of the export, so
they still reached the operator as nothing at all.

Why they are copied rather than read at delivery time: `deliver_outputs` states
its own contract — each deliverer reads out of `charts_dir` and is never handed
the export — and `anast archive` can be run later against a charts directory
alone, so a deliverer that needed the original export still sitting at the same
path would work inside the pipeline and fail for anyone delivering after.

The conservation check is the point. #110's failure was never a malformed
artifact: every check passed because each artifact was fine, and the loss was in
what never arrived. So a chart is not delivered without the documents it
references — the run refuses instead.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

import anastomosis.sources.pf_tebra  # noqa: F401 — registers the adapter
from anastomosis.core.model import DocumentArtifact, Patient, PatientRecord
from anastomosis.pipeline import (
    ATTACHMENTS_DIRNAME,
    PipelineError,
    _carry_attachments,
    run_pipeline,
)

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "pf_tebra_v9"
BLOB = "binary-content/feedface-d0c0-0000-0000-000000000001.pdf"


@pytest.fixture
def export(tmp_path: Path) -> Path:
    """A writable copy of the fixture, so a test can take its attachment away."""
    root = tmp_path / "export"
    shutil.copytree(FIXTURE, root)
    return root


def _run(export_dir: Path, out: Path):
    return run_pipeline(
        export_dir=export_dir,
        out=out,
        source="pf-tebra",
        pack="practice_fusion_soap",
        pack_dirs=None,
        force=False,
        section=None,
        qa=False,
    )


def _carried(out: Path) -> list[str]:
    directory = out / ATTACHMENTS_DIRNAME
    if not directory.is_dir():
        return []
    return sorted(p.name for p in directory.iterdir() if not p.name.startswith("_"))


def test_an_attachment_the_records_name_reaches_the_output(export: Path, tmp_path: Path) -> None:
    """The whole point: the file is where a deliverer will look for it."""
    out = tmp_path / "charts"

    result = _run(export, out)

    named = [doc for record in result.records for doc in record.documents if doc.path]
    assert named, "the fixture no longer exercises this path"
    assert _carried(out) == [Path(BLOB).name]
    # Byte-for-byte, not just present under the right name.
    assert (out / ATTACHMENTS_DIRNAME / Path(BLOB).name).read_bytes() == (
        export / BLOB
    ).read_bytes()


def test_the_record_still_locates_it_without_any_extra_index(export: Path, tmp_path: Path) -> None:
    """A deliverer needs no sidecar: the document's own path is the lookup key.

    That is deliberate — `render_index.json` exists for charts because a chart's
    filename carries no patient id, and reverse-inferring one cross-attributed
    two patients sharing a name. An attachment has no such gap.
    """
    out = tmp_path / "charts"

    result = _run(export, out)

    for record in result.records:
        for doc in record.documents:
            if doc.path:
                assert (out / ATTACHMENTS_DIRNAME / Path(doc.path).name).is_file()


def test_a_run_refuses_rather_than_deliver_a_chart_without_its_documents(
    tmp_path: Path,
) -> None:
    """The conservation check, driven at the carry step.

    Exercised directly rather than through a whole run: the source adapter only
    resolves a file it can SEE, so an export doctored to break the copy would
    simply not produce a document with a path, and the run would sail past with
    nothing to conserve. The case that matters is a record that names a file
    which is not there by the time it is carried — an export whose blob moved
    between reading the tables and copying the files.
    """
    record = PatientRecord(
        id="feedface-0000-4000-8000-0000000000ff",
        patient=Patient(id="feedface-0000-4000-8000-000000000001"),
        documents=[
            DocumentArtifact(
                id="feedface-d0c0-0000-0000-000000000001",
                patient_id="feedface-0000-4000-8000-000000000001",
                path="binary-content/gone.pdf",
                sha256="0" * 64,
            )
        ],
    )

    with pytest.raises(PipelineError) as caught:
        _carry_attachments([record], tmp_path / "export", tmp_path / "charts")

    assert caught.value.kind == "attachment_missing"
    assert "did not reach the output" in str(caught.value)
    assert "1 of 1" in str(caught.value)


def test_one_storage_id_naming_two_different_files_is_refused(tmp_path: Path) -> None:
    """The #121 shape, one layer down: two scans, one id, one slot.

    Copying the second over the first would file one patient's scan under
    another patient's document, silently. The delivery ledger already knows how
    to refuse that; the carry step claims through it.
    """
    export = tmp_path / "export"
    (export / "binary-content").mkdir(parents=True)
    (export / "binary-content" / "shared.pdf").write_bytes(b"%PDF-1.4 synthetic\n")
    shared_id = "feedface-d0c0-0000-0000-00000000dupe"

    def _doc(patient: str, digest: str) -> DocumentArtifact:
        return DocumentArtifact(
            id=shared_id,
            patient_id=patient,
            path="binary-content/shared.pdf",
            sha256=digest,
        )

    records = [
        PatientRecord(
            id=f"feedface-0000-4000-8000-00000000000{n}",
            patient=Patient(id=f"feedface-0000-4000-8000-00000000000{n}"),
            documents=[_doc(f"feedface-0000-4000-8000-00000000000{n}", digest)],
        )
        for n, digest in ((1, "a" * 64), (2, "b" * 64))
    ]

    with pytest.raises(PipelineError) as caught:
        _carry_attachments(records, export, tmp_path / "charts")

    assert caught.value.kind == "attachment_collision"
    assert shared_id not in str(caught.value), "the raw id must not reach the message"


def test_the_same_file_named_twice_is_carried_once(tmp_path: Path) -> None:
    """Two documents genuinely pointing at one file is not a collision."""
    export = tmp_path / "export"
    (export / "binary-content").mkdir(parents=True)
    (export / "binary-content" / "shared.pdf").write_bytes(b"%PDF-1.4 synthetic\n")
    out = tmp_path / "charts"

    def _record(n: int) -> PatientRecord:
        pid = f"feedface-0000-4000-8000-00000000000{n}"
        return PatientRecord(
            id=pid,
            patient=Patient(id=pid),
            documents=[
                DocumentArtifact(
                    id="feedface-d0c0-0000-0000-00000000same",
                    patient_id=pid,
                    path="binary-content/shared.pdf",
                    sha256="c" * 64,
                )
            ],
        )

    assert _carry_attachments([_record(1), _record(2)], export, out) == 1
    assert _carried(out) == ["shared.pdf"]


def test_an_export_with_no_attachments_runs_exactly_as_before(export: Path, tmp_path: Path) -> None:
    """The step costs nothing when there is nothing to carry."""
    (export / BLOB).unlink()
    (export / "patient-documents.tsv").write_text(
        (export / "patient-documents.tsv").read_text(encoding="utf-8").splitlines()[0] + "\n",
        encoding="utf-8",
    )
    out = tmp_path / "charts"

    result = _run(export, out)

    assert _carried(out) == []
    assert result.render_result.documents, "the charts still rendered"


def test_a_path_pointing_outside_the_export_is_refused(tmp_path: Path) -> None:
    """The path is the adapter's word, and a record can also arrive from a bundle.

    `from_bundle` rebuilds a record from JSON someone else may have written, so
    a `../..` in a hand-made bundle would otherwise copy a file from anywhere
    this process can read into an output directory that gets delivered onward.
    """
    export = tmp_path / "export"
    export.mkdir()
    outside = tmp_path / "secret.pdf"
    outside.write_bytes(b"%PDF-1.4 not the operator's to send\n")

    record = PatientRecord(
        id="feedface-0000-4000-8000-0000000000ff",
        patient=Patient(id="feedface-0000-4000-8000-000000000001"),
        documents=[
            DocumentArtifact(
                id="feedface-d0c0-0000-0000-000000000001",
                patient_id="feedface-0000-4000-8000-000000000001",
                path="../secret.pdf",
                sha256="0" * 64,
            )
        ],
    )

    with pytest.raises(PipelineError) as caught:
        _carry_attachments([record], export, tmp_path / "charts")

    assert caught.value.kind == "attachment_escape"
    assert not (tmp_path / "charts" / ATTACHMENTS_DIRNAME / "secret.pdf").exists()
