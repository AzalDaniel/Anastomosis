"""Live FHIR-server integration test (PLAN item 13a).

Runs the real :class:`FhirApiDestination` over a real :class:`FhirClient`
(stdlib urllib transport) against a live FHIR R4 server — a HAPI service
container in CI. It is gated two ways so it never disturbs the normal lanes:

* the ``fhir_integration`` marker, which the default test runs exclude; and
* a ``skipif`` on ``ANAST_FHIR_BASE_URL`` being unset, so even a direct
  ``pytest -m fhir_integration`` is a no-op without a server.

Synthetic data only: a ``feedface-`` GUID identifier, "Synthia Testpatient",
DOB 1990-01-02, and a tiny in-memory PDF. No PHI, no real server.

``tests/`` is not a package (``tests/unit`` has no ``__init__.py``), so this
file mirrors that — no ``__init__.py`` is added.
"""

from __future__ import annotations

import hashlib
import os
import uuid
from datetime import date
from pathlib import Path

import pytest

from anastomosis.core.model import Patient
from anastomosis.core.model.patient import Identifier, IdentifierKind
from anastomosis.deliver.browser.engine import UploadEngine
from anastomosis.deliver.browser.manifest import build_manifest
from anastomosis.deliver.browser.states import UploadState
from anastomosis.deliver.browser.tracking import TrackingDB
from anastomosis.deliver.fhir_api import FhirApiDestination, FhirClient, FhirEndpoint
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
