"""Live FHIR-server integration tests (PLAN item 13a).

Runs the real :class:`FhirApiDestination` over a real :class:`FhirClient`
(stdlib urllib transport) against a live FHIR R4 server — a HAPI service
container in CI — at two altitudes:

* the ENGINE level, driving :class:`UploadEngine` directly (push, search,
  banner, read-back, duplicate scan);
* the COMMAND level, driving :func:`run_upload_command` exactly as ``anast
  upload --fhir`` does — real manifest on disk, real output lock, the L0-L6
  ladder on, the ledger and run report written into the 0700 output dir.

It is gated two ways so it never disturbs the normal lanes:

* the ``fhir_integration`` marker, which the default test runs exclude; and
* a ``skipif`` on ``ANAST_FHIR_BASE_URL`` being unset, so even a direct
  ``pytest -m fhir_integration`` is a no-op without a server.

Synthetic data only: ``feedface-`` GUID identifiers, "Synthia Testpatient",
DOB 1990-01-02, and locally-built PDFs (a tiny hand-written one for the engine
test, a PyMuPDF-rendered one the L0-L6 ladder can actually read for the command
test). No PHI, no real server.

``tests/`` is not a package (``tests/unit`` has no ``__init__.py``), so this
file mirrors that — no ``__init__.py`` is added.
"""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from datetime import date
from pathlib import Path
from types import ModuleType

import pytest

from anastomosis.core.model import Patient, PatientRecord
from anastomosis.core.model.patient import Identifier, IdentifierKind
from anastomosis.core.upload_command import LEDGER_NAME, UploadCommand, run_upload_command
from anastomosis.deliver.browser.engine import UploadEngine
from anastomosis.deliver.browser.manifest import build_manifest
from anastomosis.deliver.browser.persist import write_upload_manifest
from anastomosis.deliver.browser.states import UploadState
from anastomosis.deliver.browser.tracking import TrackingDB
from anastomosis.deliver.fhir_api.attach import attach_fhir_destination
from anastomosis.deliver.fhir_api.client import FhirClient, FhirEndpoint
from anastomosis.deliver.fhir_api.destination import FhirApiDestination
from anastomosis.destinations.base import UploadItem
from anastomosis.reconstruct.engine import RenderedDoc

_BASE_URL = os.environ.get("ANAST_FHIR_BASE_URL")

pytestmark = [
    pytest.mark.fhir_integration,
    pytest.mark.skipif(_BASE_URL is None, reason="ANAST_FHIR_BASE_URL unset"),
]

DOB = date(1990, 1, 2)
# A unique synthetic identifier per run so repeated CI runs do not collide on
# the shared server (a feedface- GUID — never a real identifier).
_RUN_GUID = f"feedface-{uuid.uuid4().hex[:12]}"
ENC = f"feedface-e000-{uuid.uuid4().hex[:12]}"
PAT = f"feedface-p000-{uuid.uuid4().hex[:12]}"

# The command-level test gets its OWN synthetic patient/encounter so the two
# tests never resolve to each other's records (identifier search must match
# exactly one) and can run in either order.
_CMD_GUID = f"feedface-{uuid.uuid4().hex[:12]}"
CMD_ENC = f"feedface-e001-{uuid.uuid4().hex[:12]}"
CMD_PAT = f"feedface-p001-{uuid.uuid4().hex[:12]}"

_TINY_PDF = (
    b"%PDF-1.4\n"
    b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
    b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
    b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 200 200]>>endobj\n"
    b"trailer<</Root 1 0 R>>\n%%EOF\n"
)


def _patient() -> Patient:
    return Patient(
        id=PAT,
        given_name="Synthia",
        family_name="Testpatient",
        birth_date=DOB,
        identifiers=[Identifier(kind=IdentifierKind.SOURCE_GUID, value=_RUN_GUID)],
    )


def _item(path: Path) -> UploadItem:
    data = path.read_bytes()
    return UploadItem(
        item_key=f"{ENC}:{hashlib.sha256(data).hexdigest()[:12]}",
        encounter_id=ENC,
        patient_id=PAT,
        file_path=path,
        sha256=hashlib.sha256(data).hexdigest(),
        size_bytes=len(data),
    )


def _destination() -> FhirApiDestination:
    assert _BASE_URL is not None  # guarded by the module skipif
    client = FhirClient(FhirEndpoint(_BASE_URL))
    return FhirApiDestination(client, create_missing_patients=True)


def test_push_search_banner_readback_and_duplicate(tmp_path: Path) -> None:
    path = tmp_path / "note.pdf"
    path.write_bytes(_TINY_PDF)
    item = _item(path)
    docs = [RenderedDoc(path=path, encounter_id=ENC, patient_id=PAT)]
    items = build_manifest(docs)
    patient = _patient()

    dest = _destination()
    assert dest.session.is_alive() is True

    # Create-on-miss resolves (and creates) the patient by its feedface GUID.
    resolved = dest.resolve(patient)
    assert resolved is not None

    # Banner: the freshly created chart matches on family + DOB.
    assert dest.current_patient_matches(patient) is True

    # First run: PENDING -> ... -> COMPLETED.
    tracking = TrackingDB(tmp_path / "ledger.sqlite")
    run_id = tracking.begin_run(dest.name)
    engine = UploadEngine(dest, tracking)
    result = engine.run(items, {PAT: patient}, run_id)
    assert result.counts == {UploadState.COMPLETED.value: 1}

    # The scanner sees the filed document by its fingerprint title.
    fingerprints = dest.scanner.existing_fingerprints(resolved)
    assert item.fingerprint in fingerprints

    # Read the stored bytes back: HAPI keeps the inline base64 verbatim, so the
    # round-trip is byte-identical. The engine persisted the created doc id.
    doc_id = (
        tracking._conn()
        .execute("SELECT destination_doc_id FROM items WHERE item_key = ?", (item.item_key,))
        .fetchone()["destination_doc_id"]
    )
    assert doc_id is not None
    assert dest.read_back(resolved, doc_id) == _TINY_PDF
    assert dest.read_metadata(resolved, doc_id).get("size_bytes") == item.size_bytes

    # Second run on a fresh ledger: the duplicate scan finds the filed document.
    tracking2 = TrackingDB(tmp_path / "ledger2.sqlite")
    run_id2 = tracking2.begin_run(dest.name)
    engine2 = UploadEngine(dest, tracking2)
    result2 = engine2.run(items, {PAT: patient}, run_id2)
    assert result2.counts == {UploadState.DUPLICATE_AT_DESTINATION.value: 1}


# --- the command level: exactly what `anast upload --fhir` drives ------------


def _cmd_patient() -> Patient:
    return Patient(
        id=CMD_PAT,
        given_name="Synthia",
        family_name="Testpatient",
        birth_date=DOB,
        identifiers=[Identifier(kind=IdentifierKind.SOURCE_GUID, value=_CMD_GUID)],
    )


def _verifiable_pdf(pymupdf: ModuleType, path: Path) -> Path:
    """A real one-page PDF whose page 1 carries the patient's name and DOB.

    The L0-L6 ladder reads the file for real: L1 rejects a sub-KiB "PDF" and L2
    hard-fails unless page 1 shows the DOB and fuzzy-matches the name, so the
    tiny hand-written PDF the engine-level test uses cannot serve here. Filler
    lines pad the page past the 1 KiB floor — the same shape
    ``tests/unit/test_verify_composite.py`` builds.
    """
    lines = [
        "Synthia Testpatient",
        "DOB 01/02/1990",
        *[f"Clinical note body line {i} for archival padding." for i in range(20)],
    ]
    doc = pymupdf.open()
    page = doc.new_page(width=612, height=792)
    page.insert_textbox(pymupdf.Rect(36, 36, 576, 756), "\n".join(lines))
    doc.save(str(path))
    doc.close()
    return path


def test_run_upload_command_files_and_verifies_end_to_end(tmp_path: Path) -> None:
    """The whole operator path: a manifest on disk -> ``run_upload_command`` with
    the real :class:`FhirApiDestination` -> a clean result, a COMPLETED ledger,
    and a run report proving the ladder reached L5/L6.

    This is the CLI's exact drive (``anast upload --fhir`` differs only in
    parsing argv and printing the summary), so it pins the API route end to end:
    the output lock, the lock-then-read manifest, the L0-L6 verifier wired to a
    packless destination, the ledger, and the report.
    """
    assert _BASE_URL is not None  # guarded by the module skipif
    pymupdf = pytest.importorskip("pymupdf", reason="the L0-L6 ladder needs the render extra")

    out_dir = tmp_path / "out"
    out_dir.mkdir()
    path = _verifiable_pdf(pymupdf, out_dir / "note.pdf")
    patient = _cmd_patient()
    write_upload_manifest(
        [RenderedDoc(path=path, encounter_id=CMD_ENC, patient_id=CMD_PAT)],
        [PatientRecord(id=CMD_PAT, patient=patient)],
        out_dir,
    )

    result = run_upload_command(
        UploadCommand(out_dir=out_dir, verify=True),
        lambda: attach_fhir_destination(_BASE_URL, create_missing_patients=True),
    )

    # A clean landing: no abort, every item COMPLETED.
    assert result.aborted_reason is None
    assert result.is_clean is True
    assert result.exit_code == 0
    assert result.counts == {UploadState.COMPLETED.value: 1}

    # The ledger on disk agrees (the resumable record, not just the return value).
    ledger = TrackingDB(out_dir / LEDGER_NAME)
    try:
        assert dict(ledger.counts()) == {UploadState.COMPLETED.value: 1}
    finally:
        ledger.close()

    # The run report landed inside the 0700 output dir and records the ladder's
    # ACTUAL coverage: L3 skips (no browser pack on this route) while L4 (banner
    # re-read), L5 (metadata) and L6 (round-trip read-back) all ran and passed —
    # the API route's selling point over the browser one.
    assert result.report_path.parent == out_dir
    report = json.loads(result.report_path.read_text(encoding="utf-8"))
    assert report["aborted_reason"] is None
    coverage = report["verification_coverage"]
    assert coverage["L3"]["skip_count"] == 1
    assert coverage["L3"]["skip_reasons"] == ["no pack provided"]
    for level in ("L0", "L1", "L2", "L4", "L5", "L6"):
        assert coverage[level]["pass_count"] == 1, (level, coverage[level])
        assert coverage[level]["fail_count"] == 0
