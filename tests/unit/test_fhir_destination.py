"""FhirApiDestination tests against an in-memory FHIR server (opener seam).

The destination is driven through a REAL :class:`FhirClient` whose transport is
an in-process fake server (an ``Opener``) — urllib is never touched, and the
client's own JSON/id/status handling is exercised on the way. The fake server
implements just enough FHIR R4 search/read/create semantics for the resolver,
banner, scanner, driver, and the L5/L6 readers.

The driver's DocumentReference is validated against the real
``fhir.resources`` R4 model (the ``fhir`` extra is installed in this env) so a
malformed resource fails loudly. The end-to-end test drives a real
:class:`UploadEngine` + :class:`LayeredVerifier` and asserts COMPLETED with
L5/L6 passing, then a duplicate second run.

Synthetic data only: ``feedface-`` ids, "Synthia Testpatient", DOB 1990-01-02.
"""

from __future__ import annotations

import base64
import hashlib
import json
import urllib.parse
from collections.abc import Mapping
from dataclasses import replace
from datetime import date
from pathlib import Path

import pytest

from anastomosis.core.model import Encounter, Patient
from anastomosis.deliver.browser.engine import UploadEngine
from anastomosis.deliver.browser.errors import PermanentDeliveryError
from anastomosis.deliver.browser.manifest import build_manifest
from anastomosis.deliver.browser.states import UploadState
from anastomosis.deliver.browser.tracking import TrackingDB
from anastomosis.deliver.fhir_api.client import FhirClient, FhirEndpoint, FhirResponse
from anastomosis.deliver.fhir_api.destination import FhirApiDestination
from anastomosis.deliver.verify import LayeredVerifier, LevelStatus
from anastomosis.destinations.base import DestinationPatient, UploadItem
from anastomosis.reconstruct.engine import RenderedDoc

pytest.importorskip("fitz", reason="end-to-end verify path needs PyMuPDF (render extra)")
import fitz

PAT = "feedface-0000-0000-0000-0000000000aa"
ENC = "feedface-e000-0000-0000-0000000000aa"
DOB = date(1990, 1, 2)
DOS = date(2023, 5, 10)
NAME = "Synthia Testpatient"
BASE = "https://fhir.example.com/r4"

_FILLER = [f"Clinical note body line {i} for archival padding." for i in range(20)]
GOOD_LINES = [NAME, "DOB 01/02/1990", "Date of service: May 10, 2023", *_FILLER]


# --- in-memory FHIR server (the transport seam) -------------------------------


class _FakeFhirServer:
    """A tiny in-memory FHIR R4 server implementing the verbs the pusher uses.

    Stores Patients and DocumentReferences; serves identifier/demographic
    Patient search, read-by-id, DocumentReference subject search and read, and
    create (assigning sequential ids, returning a Location header).
    """

    def __init__(self) -> None:
        self.patients: dict[str, dict[str, object]] = {}
        self.docs: dict[str, dict[str, object]] = {}
        self._seq = 0

    def add_patient(self, resource: dict[str, object]) -> str:
        pid = str(resource.get("id") or self._next_id())
        resource = {**resource, "id": pid}
        self.patients[pid] = resource
        return pid

    def _next_id(self) -> str:
        self._seq += 1
        return f"srv-{self._seq}"

    def __call__(
        self, method: str, url: str, headers: Mapping[str, str], body: bytes | None
    ) -> FhirResponse:
        parts = urllib.parse.urlsplit(url)
        # The path after the base (/r4/...). Strip the known base path prefix.
        path = parts.path
        base_path = urllib.parse.urlsplit(BASE).path
        if path.startswith(base_path):
            path = path[len(base_path) :]
        path = path.strip("/")
        params = dict(urllib.parse.parse_qsl(parts.query))
        segments = path.split("/")

        if method == "GET":
            return self._get(segments, params)
        if method == "POST":
            return self._post(segments, body)
        raise AssertionError(f"unexpected method {method}")

    def _get(self, segments: list[str], params: Mapping[str, str]) -> FhirResponse:
        head = segments[0]
        if head == "metadata":
            return FhirResponse(status=200, body={"resourceType": "CapabilityStatement"})
        if head == "Patient" and len(segments) == 1:
            return FhirResponse(status=200, body=self._search_patients(params))
        if head == "Patient" and len(segments) == 2:
            resource = self.patients.get(segments[1])
            if resource is None:
                return FhirResponse(status=404, body=None)
            return FhirResponse(status=200, body=resource)
        if head == "DocumentReference" and len(segments) == 1:
            return FhirResponse(status=200, body=self._search_docs(params))
        if head == "DocumentReference" and len(segments) == 2:
            resource = self.docs.get(segments[1])
            if resource is None:
                return FhirResponse(status=404, body=None)
            return FhirResponse(status=200, body=resource)
        return FhirResponse(status=404, body=None)

    def _post(self, segments: list[str], body: bytes | None) -> FhirResponse:
        resource = json.loads(body) if body else {}
        if segments[0] == "Patient":
            pid = self.add_patient(resource)
            return FhirResponse(status=201, body=None, location=f"{BASE}/Patient/{pid}/_history/1")
        if segments[0] == "DocumentReference":
            did = self._next_id()
            stored = {**resource, "id": did}
            self.docs[did] = stored
            return FhirResponse(
                status=201, body=stored, location=f"{BASE}/DocumentReference/{did}/_history/1"
            )
        raise AssertionError(f"unexpected create {segments}")

    def _search_patients(self, params: Mapping[str, str]) -> dict[str, object]:
        matches: list[dict[str, object]] = []
        ident = params.get("identifier")
        for resource in self.patients.values():
            if ident is not None:
                if _has_identifier(resource, ident):
                    matches.append(resource)
                continue
            if _demographics_match(resource, params):
                matches.append(resource)
        return _bundle(matches)

    def _search_docs(self, params: Mapping[str, str]) -> dict[str, object]:
        subject = params.get("subject")
        matches = [d for d in self.docs.values() if d.get("subject") == {"reference": subject}]
        return _bundle(matches)


def _bundle(resources: list[dict[str, object]]) -> dict[str, object]:
    return {
        "resourceType": "Bundle",
        "type": "searchset",
        "entry": [{"resource": r} for r in resources],
    }


def _has_identifier(resource: Mapping[str, object], token: str) -> bool:
    system, _, value = token.partition("|")
    idents = resource.get("identifier")
    if not isinstance(idents, list):
        return False
    return any(
        isinstance(i, Mapping) and i.get("system") == system and i.get("value") == value
        for i in idents
    )


def _demographics_match(resource: Mapping[str, object], params: Mapping[str, str]) -> bool:
    if "birthdate" in params and resource.get("birthDate") != params["birthdate"]:
        return False
    names = resource.get("name")
    name = names[0] if isinstance(names, list) and names else {}
    if "family" in params and name.get("family") != params["family"]:
        return False
    if "given" in params and params["given"] not in (name.get("given") or []):
        return False
    return True


# --- builders -----------------------------------------------------------------


def _patient() -> Patient:
    return Patient(id=PAT, given_name="Synthia", family_name="Testpatient", birth_date=DOB)


def _patient_resource(pid: str, *, family: str = "Testpatient", dob: str = "1990-01-02") -> dict:
    return {
        "resourceType": "Patient",
        "id": pid,
        "name": [{"given": ["Synthia"], "family": family}],
        "birthDate": dob,
    }


def _client(server: _FakeFhirServer) -> FhirClient:
    return FhirClient(FhirEndpoint(BASE), opener=server)


def _make_pdf(path: Path, lines: list[str]) -> Path:
    doc = fitz.open()
    page = doc.new_page(width=612, height=792)
    page.insert_textbox(fitz.Rect(36, 36, 576, 756), "\n".join(lines))
    doc.save(str(path))
    doc.close()
    return path


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


# --- resolver -----------------------------------------------------------------


def test_resolver_demographic_single_match() -> None:
    server = _FakeFhirServer()
    pid = server.add_patient(_patient_resource("srv-1"))
    dest = FhirApiDestination(_client(server))
    resolved = dest.resolve(_patient())
    assert resolved is not None
    assert resolved.destination_patient_id == pid
    assert resolved.matched_on == ("family_name", "given_name", "birth_date")


def test_resolver_identifier_match_uses_export_systems() -> None:
    from anastomosis.core.fhir import export
    from anastomosis.core.model.patient import Identifier, IdentifierKind

    server = _FakeFhirServer()
    mrn_system = export.IDENTIFIER_SYSTEMS["mrn"]
    resource = _patient_resource("srv-9")
    resource["identifier"] = [{"system": mrn_system, "value": "MRN-123"}]
    server.add_patient(resource)
    patient = Patient(
        id=PAT,
        family_name="Testpatient",
        birth_date=DOB,
        identifiers=[Identifier(kind=IdentifierKind.MRN, value="MRN-123")],
    )
    dest = FhirApiDestination(_client(server))
    resolved = dest.resolve(patient)
    assert resolved is not None and resolved.matched_on == ("identifier",)
    assert resolved.destination_patient_id == "srv-9"


def test_resolver_multiple_matches_raises() -> None:
    server = _FakeFhirServer()
    server.add_patient(_patient_resource("srv-1"))
    server.add_patient(_patient_resource("srv-2"))
    dest = FhirApiDestination(_client(server))
    with pytest.raises(PermanentDeliveryError, match="refusing to guess"):
        dest.resolve(_patient())


def test_resolver_zero_match_returns_none() -> None:
    server = _FakeFhirServer()
    dest = FhirApiDestination(_client(server))
    assert dest.resolve(_patient()) is None


def test_resolver_zero_with_create_posts_patient_via_export_builder() -> None:
    server = _FakeFhirServer()
    dest = FhirApiDestination(_client(server), create_missing_patients=True)
    resolved = dest.resolve(_patient())
    assert resolved is not None and resolved.matched_on == ("created",)
    # The server now holds a Patient built by the export code (name + DOB).
    created = server.patients[resolved.destination_patient_id]
    assert created["birthDate"] == "1990-01-02"
    assert created["name"] == [{"given": ["Synthia"], "family": "Testpatient"}]
    # The export resource id was dropped before POST (server assigned its own).
    assert created["id"] != PAT


# --- banner -------------------------------------------------------------------


def test_banner_passes_on_family_and_dob() -> None:
    server = _FakeFhirServer()
    server.add_patient(_patient_resource("srv-1"))
    dest = FhirApiDestination(_client(server))
    assert dest.current_patient_matches(_patient()) is True


def test_banner_fails_on_family_mismatch() -> None:
    server = _FakeFhirServer()
    server.add_patient(_patient_resource("srv-1", family="Different"))
    dest = FhirApiDestination(_client(server))
    # The demographic search won't even find a "Testpatient" family; banner
    # resolves to None and fails closed.
    assert dest.current_patient_matches(_patient()) is False


def test_banner_fails_closed_on_missing_dob() -> None:
    server = _FakeFhirServer()
    # Server record has no birthDate -> cannot confirm -> fail closed.
    from anastomosis.core.fhir import export
    from anastomosis.core.model.patient import Identifier, IdentifierKind

    resource = _patient_resource("srv-1")
    del resource["birthDate"]
    # identifier must be on the resource BEFORE add_patient (the server
    # stores a copy) so resolve() finds a real, DOB-less match and the
    # banner's _birthdate_matches branch is the one under test.
    resource["identifier"] = [{"system": export.IDENTIFIER_SYSTEMS["mrn"], "value": "M1"}]
    server.add_patient(resource)
    dest = FhirApiDestination(_client(server))
    patient = Patient(
        id=PAT,
        family_name="Testpatient",
        birth_date=DOB,
        identifiers=[Identifier(kind=IdentifierKind.MRN, value="M1")],
    )
    assert dest.current_patient_matches(patient) is False


# --- scanner round-trip -------------------------------------------------------


def test_scanner_reads_titles_driver_wrote(tmp_path: Path) -> None:
    server = _FakeFhirServer()
    pid = server.add_patient(_patient_resource("srv-1"))
    dest = FhirApiDestination(_client(server))
    # An explicit fingerprint DISTINCT from the file name: proves the driver
    # writes item.fingerprint (not the filename) as the attachment title.
    base = _item(_make_pdf(tmp_path / "note.pdf", GOOD_LINES))
    item = replace(base, fingerprint="enc-0042 office visit 2026-01-05")
    dest_patient = DestinationPatient(destination_patient_id=pid)
    # Driver writes the fingerprint as content[0].attachment.title…
    dest.upload(item, dest_patient)
    # …and the scanner reads exactly that back.
    assert dest.existing_fingerprints(dest_patient) == {"enc-0042 office visit 2026-01-05"}


# --- driver: validate against the real fhir.resources DocumentReference -------


def test_driver_document_reference_validates_against_fhir_resources(tmp_path: Path) -> None:
    fhir_docref = pytest.importorskip("fhir.resources.documentreference")
    server = _FakeFhirServer()
    dest = FhirApiDestination(_client(server))
    item = _item(_make_pdf(tmp_path / "note.pdf", GOOD_LINES))
    resource = dest._document_reference(item, DestinationPatient(destination_patient_id="srv-1"))
    model = fhir_docref.DocumentReference.model_validate(resource)
    assert model.status == "current"
    assert model.content[0].attachment.contentType == "application/pdf"
    assert model.content[0].attachment.size == item.size_bytes
    assert model.content[0].attachment.title == item.fingerprint
    # hash is deliberately omitted (R4 SHA-1 vs our sha256 ledger standard).
    assert model.content[0].attachment.hash is None


def test_driver_receipt_echoes_size(tmp_path: Path) -> None:
    server = _FakeFhirServer()
    pid = server.add_patient(_patient_resource("srv-1"))
    dest = FhirApiDestination(_client(server))
    item = _item(_make_pdf(tmp_path / "note.pdf", GOOD_LINES))
    receipt = dest.upload(item, DestinationPatient(destination_patient_id=pid))
    assert receipt.destination_doc_id is not None
    assert receipt.echoed_size_bytes == item.size_bytes


# --- MetadataReader / DocumentReader ------------------------------------------


def test_metadata_reader_reports_size(tmp_path: Path) -> None:
    server = _FakeFhirServer()
    pid = server.add_patient(_patient_resource("srv-1"))
    dest = FhirApiDestination(_client(server))
    item = _item(_make_pdf(tmp_path / "note.pdf", GOOD_LINES))
    dp = DestinationPatient(destination_patient_id=pid)
    receipt = dest.upload(item, dp)
    assert receipt.destination_doc_id is not None
    meta = dest.read_metadata(dp, receipt.destination_doc_id)
    assert meta == {"size_bytes": item.size_bytes}


def test_document_reader_inline_data_round_trips(tmp_path: Path) -> None:
    server = _FakeFhirServer()
    pid = server.add_patient(_patient_resource("srv-1"))
    dest = FhirApiDestination(_client(server))
    item = _item(_make_pdf(tmp_path / "note.pdf", GOOD_LINES))
    dp = DestinationPatient(destination_patient_id=pid)
    receipt = dest.upload(item, dp)
    assert receipt.destination_doc_id is not None
    assert dest.read_back(dp, receipt.destination_doc_id) == item.file_path.read_bytes()


def test_document_reader_same_origin_url_followed(tmp_path: Path) -> None:
    server = _FakeFhirServer()
    payload = b"%PDF-1.4 by-reference bytes"
    # A by-reference attachment whose Binary is served same-origin.
    server.docs["d1"] = {
        "resourceType": "DocumentReference",
        "id": "d1",
        "content": [{"attachment": {"contentType": "application/pdf", "url": f"{BASE}/Binary/b1"}}],
    }
    # The Binary read goes through GET Binary/b1 -> our server must answer it.
    binary_data = base64.b64encode(payload).decode("ascii")

    class _ServerWithBinary(_FakeFhirServer):
        def _get(self, segments: list[str], params: Mapping[str, str]) -> FhirResponse:
            if segments[0] == "Binary":
                return FhirResponse(
                    status=200, body={"resourceType": "Binary", "data": binary_data}
                )
            return super()._get(segments, params)

    server2 = _ServerWithBinary()
    server2.docs["d1"] = server.docs["d1"]
    dest = FhirApiDestination(_client(server2))
    out = dest.read_back(DestinationPatient(destination_patient_id="srv-1"), "d1")
    assert out == payload


def test_document_reader_cross_origin_url_refused() -> None:
    server = _FakeFhirServer()
    server.docs["d1"] = {
        "resourceType": "DocumentReference",
        "id": "d1",
        "content": [
            {"attachment": {"contentType": "application/pdf", "url": "https://evil.example.org/x"}}
        ],
    }
    dest = FhirApiDestination(_client(server))
    with pytest.raises(PermanentDeliveryError, match="cross-origin"):
        dest.read_back(DestinationPatient(destination_patient_id="srv-1"), "d1")


# --- end to end: engine + destination + LayeredVerifier -----------------------


def _encounter() -> Encounter:
    return Encounter(id=ENC, patient_id=PAT, date_of_service=DOS)


def test_end_to_end_completes_with_l5_l6_then_duplicate(tmp_path: Path) -> None:
    server = _FakeFhirServer()
    server.add_patient(_patient_resource("srv-1"))
    path = _make_pdf(tmp_path / "note.pdf", GOOD_LINES)
    docs = [RenderedDoc(path=path, encounter_id=ENC, patient_id=PAT)]
    items = build_manifest(docs)

    # First run: resolve -> upload -> L0-L6 -> COMPLETED.
    dest = FhirApiDestination(_client(server))
    tracking = TrackingDB(tmp_path / "ledger.sqlite")
    run_id = tracking.begin_run(dest.name)
    verifier = LayeredVerifier(records={ENC: _encounter()}, destination=dest)
    engine = UploadEngine(dest, tracking, verifier=verifier)
    result = engine.run(items, {PAT: _patient()}, run_id)

    assert result.counts == {UploadState.COMPLETED.value: 1}
    table = {r.level: r.status for r in verifier.results_for(items[0].item_key)}
    assert table["L4"] is LevelStatus.PASS
    assert table["L5"] is LevelStatus.PASS  # size cross-check
    assert table["L6"] is LevelStatus.PASS  # byte-identical read-back

    # Second run on a fresh ledger: the document is already filed at the
    # destination, so the duplicate scan catches it before any re-send.
    dest2 = FhirApiDestination(_client(server))
    tracking2 = TrackingDB(tmp_path / "ledger2.sqlite")
    run_id2 = tracking2.begin_run(dest2.name)
    engine2 = UploadEngine(dest2, tracking2)
    result2 = engine2.run(items, {PAT: _patient()}, run_id2)
    assert result2.counts == {UploadState.DUPLICATE_AT_DESTINATION.value: 1}


# --- PHI discipline across failing paths --------------------------------------


def test_no_phi_in_logs_on_failure(caplog: pytest.LogCaptureFixture) -> None:
    import logging

    server = _FakeFhirServer()
    server.add_patient(_patient_resource("srv-1"))
    server.add_patient(_patient_resource("srv-2"))
    dest = FhirApiDestination(_client(server))
    with caplog.at_level(logging.DEBUG), pytest.raises(PermanentDeliveryError) as exc:
        dest.resolve(_patient())
    blob = caplog.text + str(exc.value)
    for forbidden in ("Synthia", "Testpatient", "1990", "01/02", "srv-1", "srv-2"):
        assert forbidden not in blob, f"PHI/id leak: {forbidden!r}"
