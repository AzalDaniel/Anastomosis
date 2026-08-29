"""A document row finds the file it names, or admits it could not.

A `patient-documents` row is a pointer: it carries a document's name, type and
size, and a storage id naming the file that IS the document. Before this the
pointer was mapped and the file was never looked for — five attachments in a
real export, among them a 7-page clinical PDF, reached the output as nothing at
all, with no trace anywhere that they had existed.

These cover both halves of the fix: the file is found wherever the export put
it, and a row whose file is genuinely missing says so instead of shipping an
artifact that claims a document it has not got.
"""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

import pytest

import anastomosis.sources.pf_tebra  # noqa: F401 — registers the adapter
from anastomosis.core.model import PatientRecord
from anastomosis.sources import get_source
from anastomosis.sources.pf_tebra.loader import find_attachments

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "pf_tebra_v9"
BLOB = "binary-content/feedface-d0c0-0000-0000-000000000001.pdf"


@pytest.fixture
def export(tmp_path: Path) -> Path:
    """A writable copy of the fixture, so a test can take its attachment away."""
    root = tmp_path / "export"
    shutil.copytree(FIXTURE, root)
    return root


def _documents(root: Path) -> list:
    return [doc for record in get_source("pf-tebra").load(root) for doc in record.documents]


def test_a_document_row_resolves_to_the_file_it_names(export: Path) -> None:
    """The whole point: the row's storage id finds the blob, with real facts."""
    (document,) = _documents(export)

    assert document.path == BLOB
    assert document.mime_type == "application/pdf", "the row's extension decides the media type"
    assert document.page_count == 2, "a scanned record's length is not a guess"
    expected = hashlib.sha256((export / BLOB).read_bytes()).hexdigest()
    assert document.sha256 == expected


def test_an_extensionless_blob_still_reports_its_pages(export: Path) -> None:
    """The shape a real export actually has.

    Attachments are written into ``binary-content/`` under the storage GUID with
    no extension — all 9,955 of them in the export this was measured against —
    and the type is stated in the row's ``OriginalFileExtension`` instead. The
    finder already copes (it indexes by stem) and so does the media type (it
    reads that column first), but the page count keyed on the stored file's
    suffix, so it matched nothing and every attachment in a real export came
    back with no page count — including the PDFs, which are most of them.

    Renaming the fixture blob is the whole test: same bytes, same row, the one
    difference being the thing a real export does differently.
    """
    blob = export / BLOB
    blob.rename(blob.with_suffix(""))

    (document,) = _documents(export)

    assert document.path == BLOB.removesuffix(".pdf"), "found by stem, extension or not"
    assert document.mime_type == "application/pdf", "the ROW says what it is"
    assert document.page_count == 2, "and a real export's attachments still count their pages"


def test_a_blob_the_row_does_not_call_a_pdf_reports_no_pages(export: Path) -> None:
    """The other half: the declared type is believed when it says 'not a PDF'.

    Real exports carry HL7 v2 messages, JPEGs, legacy Office documents and video
    in the same extensionless ``binary-content/`` folder. None of those has a
    page count, and reaching for one would only produce a warning per file.
    """
    blob = export / BLOB
    blob.rename(blob.with_suffix(""))
    table = export / "patient-documents.tsv"
    rows = table.read_text(encoding="utf-8").replace("\t.pdf\t", "\t.jpg\t")
    table.write_text(rows, encoding="utf-8")

    (document,) = _documents(export)

    assert document.mime_type == "image/jpeg"
    assert document.page_count is None


def test_the_recorded_path_stays_inside_the_export(export: Path) -> None:
    """A chart travels to another EHR. The operator's directory does not go too.

    The render index already records chart filenames rather than absolute paths;
    an attachment is the same. `binary-content/<id>.pdf` locates the file
    relative to the export the operator chose, and says nothing about the
    machine that read it.
    """
    (document,) = _documents(export)

    assert document.path is not None
    assert not Path(document.path).is_absolute()
    assert str(export) not in document.path
    assert str(export.parent) not in document.path


def test_a_row_whose_file_is_missing_keeps_its_metadata_and_claims_nothing_else(
    export: Path,
) -> None:
    """An export can arrive without its blobs. That is a smaller loss than a lie.

    The row's own columns still ride through to the preserved-fields narrative,
    so nothing about the document disappears — but the artifact claims no path,
    no digest and no page count, because it has none of them.
    """
    (export / BLOB).unlink()

    (document,) = _documents(export)

    assert document.path is None
    assert document.sha256 is None
    assert document.page_count is None
    assert document.title, "the row's own columns still describe the document"
    assert document.extensions, "and its surplus columns still ride to the narrative"


def test_two_files_with_one_name_resolve_to_neither(export: Path) -> None:
    """Ambiguity is never guessed past.

    Two candidate files for one document id means the export cannot say which
    belongs in this patient's chart. Attaching the wrong scan to the wrong
    record is the failure this tool exists to prevent, so it attaches neither.
    """
    duplicate = export / "elsewhere" / Path(BLOB).name
    duplicate.parent.mkdir()
    shutil.copy(export / BLOB, duplicate)

    (document,) = _documents(export)

    assert document.path is None, "one of two candidates was picked"


def test_the_index_skips_the_export_s_own_tables(export: Path) -> None:
    """The TSVs are the export, not attachments in it."""
    attachments = find_attachments(export)

    assert "patient-documents" not in attachments.by_id
    assert "patient-demographics" not in attachments.by_id
    assert Path(BLOB).stem in attachments.by_id


def test_a_file_the_export_does_not_reference_is_indexed_but_unused(export: Path) -> None:
    """Indexing is by stem, so a stray file is found and simply never asked for."""
    (export / "notes.txt").write_text("synthetic", encoding="utf-8")

    attachments = find_attachments(export)
    (document,) = _documents(export)

    assert "notes" in attachments.by_id
    assert document.path == BLOB, "the stray file did not disturb the real one"


def test_the_attachment_survives_the_fhir_round_trip() -> None:
    """path, digest and page count are facts about the document, so they travel."""
    from anastomosis.core.fhir import from_bundle, to_bundle

    for record in get_source("pf-tebra").load(FIXTURE):
        if not record.documents:
            continue
        rebuilt: PatientRecord = from_bundle(to_bundle(record))
        before = [d.model_dump(mode="json", exclude={"provenance"}) for d in record.documents]
        after = [d.model_dump(mode="json", exclude={"provenance"}) for d in rebuilt.documents]
        assert after == before
