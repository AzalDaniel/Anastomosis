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

from anastomosis.core.model import Encounter, Identifier, IdentifierKind, Patient
from anastomosis.deliver.browser.engine import UploadEngine
from anastomosis.deliver.browser.errors import PermanentDeliveryError
from anastomosis.deliver.browser.manifest import build_manifest
from anastomosis.deliver.browser.states import UploadState
from anastomosis.deliver.browser.tracking import TrackingDB
from anastomosis.deliver.fhir_api.client import FhirClient, FhirEndpoint, FhirResponse
from anastomosis.deliver.fhir_api.destination import FhirApiDestination, PayloadTooLarge
from anastomosis.deliver.verify import LayeredVerifier, LevelStatus
from anastomosis.destinations.base import DestinationPatient, UploadItem
from anastomosis.reconstruct.engine import RenderedDoc

pytest.importorskip("pymupdf", reason="end-to-end verify path needs PyMuPDF (render extra)")
import pymupdf

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

    ``page_size`` makes the DocumentReference search page the way a real server
    does — a window plus a ``Bundle.link[next]``. None means one page, which is
    what every test here assumed until a client that only read the first page
    turned out to be filing duplicates.
    """

    def __init__(self, *, page_size: int | None = None) -> None:
        self.patients: dict[str, dict[str, object]] = {}
        self.docs: dict[str, dict[str, object]] = {}
        self.page_size = page_size
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
        if self.page_size is None:
            return _bundle(matches)
        # Page like a real server: hand back a window and advertise the rest as
        # Bundle.link[next], which is the ONLY way a client learns there is more.
        offset = int(params.get("_offset", "0"))
        window = matches[offset : offset + self.page_size]
        next_offset = offset + self.page_size
        next_url = None
        if next_offset < len(matches):
            next_url = f"{BASE}/DocumentReference?subject={subject}&_offset={next_offset}"
        return _bundle(window, next_url=next_url)


def _bundle(
    resources: list[dict[str, object]], *, next_url: str | None = None
) -> dict[str, object]:
    bundle: dict[str, object] = {
        "resourceType": "Bundle",
        "type": "searchset",
        "entry": [{"resource": r} for r in resources],
    }
    if next_url is not None:
        bundle["link"] = [{"relation": "next", "url": next_url}]
    return bundle


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
    doc = pymupdf.open()
    page = doc.new_page(width=612, height=792)
    page.insert_textbox(pymupdf.Rect(36, 36, 576, 756), "\n".join(lines))
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


def test_banner_never_creates_patients() -> None:
    # A verification step must be side-effect free: even with
    # create_missing_patients=True, an unresolvable banner check fails
    # closed WITHOUT creating the record it was supposed to verify.
    server = _FakeFhirServer()
    dest = FhirApiDestination(_client(server), create_missing_patients=True)
    assert dest.current_patient_matches(_patient()) is False
    assert server.patients == {}, "banner check created a patient"


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


# --- driver: the inline-payload bound -----------------------------------------


def test_driver_refuses_oversized_item_without_reading_the_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bound is enforced BEFORE the read: a DocumentReference inlines the
    PDF, so reading first is exactly the memory spike being prevented."""
    server = _FakeFhirServer()
    pid = server.add_patient(_patient_resource("srv-1"))
    item = _item(_make_pdf(tmp_path / "note.pdf", GOOD_LINES))
    dest = FhirApiDestination(_client(server), max_payload_bytes=item.size_bytes - 1)

    def _explode(self: Path) -> bytes:
        raise AssertionError("an over-limit file must never be read")

    monkeypatch.setattr(Path, "read_bytes", _explode)

    with pytest.raises(PayloadTooLarge) as excinfo:
        dest.upload(item, DestinationPatient(destination_patient_id=pid))
    assert item.item_key in str(excinfo.value)
    assert not server.docs, "nothing may be filed for a refused item"


def test_driver_accepts_an_item_within_the_bound(tmp_path: Path) -> None:
    server = _FakeFhirServer()
    pid = server.add_patient(_patient_resource("srv-1"))
    item = _item(_make_pdf(tmp_path / "note.pdf", GOOD_LINES))
    # Exactly at the bound passes (the refusal is strictly "over"), and the
    # default constructor — 50 MiB — passes a normal chart untouched.
    at_bound = FhirApiDestination(_client(server), max_payload_bytes=item.size_bytes)
    assert at_bound.upload(item, DestinationPatient(destination_patient_id=pid)).destination_doc_id
    default = FhirApiDestination(_client(server))
    assert default.upload(item, DestinationPatient(destination_patient_id=pid)).destination_doc_id


def test_driver_catches_an_oversized_file_a_stale_manifest_understates(tmp_path: Path) -> None:
    """The preflight weighs the FILE, not the manifest.

    ``size_bytes`` comes from the upload manifest and can be stale; the bytes
    on disk are the ones read, base64-encoded, and serialized, so they are what
    set the memory peak. A manifest that understates the file must not wave it
    past the bound.
    """
    server = _FakeFhirServer()
    pid = server.add_patient(_patient_resource("srv-1"))
    honest = _item(_make_pdf(tmp_path / "note.pdf", GOOD_LINES))
    lying = replace(honest, size_bytes=1)
    dest = FhirApiDestination(_client(server), max_payload_bytes=honest.size_bytes - 1)
    with pytest.raises(PayloadTooLarge):
        dest.upload(lying, DestinationPatient(destination_patient_id=pid))


def test_driver_delivers_a_small_file_a_stale_manifest_overstates(tmp_path: Path) -> None:
    """The other direction of the same rule: a manifest that lies LARGE about a
    small file must NOT refuse it.

    Nothing oversized is ever read here — the file on disk is well inside the
    bound — so refusing on the manifest's word alone would strand a deliverable
    chart on a number that describes nothing. The measurement is the stat.
    """
    server = _FakeFhirServer()
    pid = server.add_patient(_patient_resource("srv-1"))
    honest = _item(_make_pdf(tmp_path / "note.pdf", GOOD_LINES))
    inflated = replace(honest, size_bytes=honest.size_bytes * 100)
    dest = FhirApiDestination(_client(server), max_payload_bytes=honest.size_bytes)

    receipt = dest.upload(inflated, DestinationPatient(destination_patient_id=pid))

    assert receipt.destination_doc_id
    assert len(server.docs) == 1


def test_payload_too_large_reports_the_measured_size(tmp_path: Path) -> None:
    """The message must quote the size that was actually weighed (the stat)."""
    server = _FakeFhirServer()
    honest = _item(_make_pdf(tmp_path / "note.pdf", GOOD_LINES))
    lying = replace(honest, size_bytes=1)
    on_disk_mib = honest.file_path.stat().st_size / (1024 * 1024)
    dest = FhirApiDestination(_client(server), max_payload_bytes=honest.size_bytes - 1)

    with pytest.raises(PayloadTooLarge) as excinfo:
        dest.upload(lying, DestinationPatient(destination_patient_id="srv-1"))

    assert f"is {on_disk_mib:.1f} MiB" in str(excinfo.value)


def test_payload_too_large_message_carries_no_filename_or_patient_value(tmp_path: Path) -> None:
    # A chart filename embeds the patient name and the date of service, so the
    # message names the opaque item_key and the sizes only.
    server = _FakeFhirServer()
    chart = tmp_path / "Testpatient_Synthia_05-10-2023_SOAP.pdf"
    item = _item(_make_pdf(chart, GOOD_LINES))
    dest = FhirApiDestination(_client(server), max_payload_bytes=item.size_bytes - 1)
    with pytest.raises(PayloadTooLarge) as excinfo:
        dest.upload(item, DestinationPatient(destination_patient_id="srv-1"))
    message = str(excinfo.value)
    for forbidden in (chart.name, str(chart), "Synthia", "Testpatient", "1990", "05-10-2023"):
        assert forbidden not in message, f"PHI leak in PayloadTooLarge: {forbidden!r}"
    assert "MiB" in message  # the actionable part: the limit, in human units


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


# --- ID-005: verification must be side-effect-free (never a second POST) ------


class _LaggingIndexServer(_FakeFhirServer):
    """A server whose SEARCH index lags a create: a just-POSTed Patient is not
    yet returned by search. Counts Patient POSTs so a duplicate create shows."""

    def __init__(self) -> None:
        super().__init__()
        self.patient_post_count = 0

    def _search_patients(self, params: Mapping[str, str]) -> dict[str, object]:
        return _bundle([])  # the lag: the created patient is never searchable yet

    def _post(self, segments: list[str], body: bytes | None) -> FhirResponse:
        if segments and segments[0] == "Patient":
            self.patient_post_count += 1
        return super()._post(segments, body)


def test_verifier_reuses_engine_identity_and_never_creates_a_second_patient(
    tmp_path: Path,
) -> None:
    """ID-005: with ``create_missing_patients`` and a lagging search index, the
    engine's resolve legitimately POSTs ONE Patient. The verifier must REUSE
    that identity (threaded in from the engine), never re-resolve through the
    create-capable resolver — which under the lag would POST a SECOND Patient
    (the exact side effect the destination's own docstring forbids). Total
    POST /Patient stays 1, and engine-id == verifier-id."""
    from anastomosis.deliver.browser.errors import WrongPatientError

    server = _LaggingIndexServer()
    dest = FhirApiDestination(_client(server), create_missing_patients=True)
    item = _item(_make_pdf(tmp_path / "note.pdf", GOOD_LINES))

    # The engine's own resolve — the ONE legitimate create.
    engine_dp = dest.resolve(_patient())
    assert engine_dp is not None
    assert server.patient_post_count == 1

    verifier = LayeredVerifier(records={ENC: _encounter()}, destination=dest)
    # Thread the engine's identity: verify_pre must NOT resolve again. L4's
    # banner still fails closed on the lagging (empty) search, so verify_pre
    # raises — but crucially WITHOUT a second POST.
    with pytest.raises(WrongPatientError):
        verifier.verify_pre(item, _patient(), engine_dp)

    assert server.patient_post_count == 1, "verifier POSTed a second Patient during verification"
    captured = verifier._resolved[item.item_key]
    assert captured.destination_patient_id == engine_dp.destination_patient_id


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


def test_the_duplicate_scan_reads_every_page_of_the_searchset() -> None:
    """A resumed run must not re-file a chart the destination already holds.

    A FHIR search returns a searchset the SERVER pages, advertising the
    continuation as `Bundle.link[relation="next"]`. The scan read one page and
    stopped, so a patient with more documents than the server's page size had
    fingerprints it could not see — and the invisible ones are the most
    recently filed, which is exactly where a crashed run's last upload sits.

    `destinations/base.py` states the contract this holds: "re-filing would
    double a patient's chart."
    """
    server = _FakeFhirServer(page_size=20)
    pid = server.add_patient(_patient_resource(PAT))
    subject = {"reference": f"Patient/{pid}"}
    for n in range(1, 22):  # 21 documents behind a 20-per-page server
        did = server._next_id()
        server.docs[did] = {
            "resourceType": "DocumentReference",
            "id": did,
            "subject": subject,
            "content": [{"attachment": {"title": f"fingerprint-{n:04d}"}}],
        }

    destination = FhirApiDestination(_client(server))
    found = destination.existing_fingerprints(
        DestinationPatient(destination_patient_id=pid, matched_on="identifier")
    )

    assert len(found) == 21, f"the scan stopped early and saw {len(found)} of 21"
    assert "fingerprint-0021" in found, (
        "the last-filed document is invisible to the duplicate scan — a resumed "
        "run would file it a second time"
    )


def test_the_duplicate_scan_refuses_a_cross_origin_next_link() -> None:
    """The `next` URL is chosen by the server, so it gets the redirect rule.

    The client refuses redirects because the Authorization header must never
    travel to a host the operator did not configure. A `next` link pointing
    off-origin is the same request wearing different clothes.
    """
    server = _FakeFhirServer()
    pid = server.add_patient(_patient_resource(PAT))
    subject = {"reference": f"Patient/{pid}"}
    did = server._next_id()
    server.docs[did] = {
        "resourceType": "DocumentReference",
        "id": did,
        "subject": subject,
        "content": [{"attachment": {"title": "fingerprint-0001"}}],
    }
    original = server._search_docs

    def _evil(params: Mapping[str, str]) -> dict[str, object]:
        bundle = original(params)
        bundle["link"] = [
            {"relation": "next", "url": "https://elsewhere.example/DocumentReference?p=2"}
        ]
        return bundle

    server._search_docs = _evil  # type: ignore[method-assign]

    destination = FhirApiDestination(_client(server))
    with pytest.raises(PermanentDeliveryError, match="cross-origin"):
        destination.existing_fingerprints(
            DestinationPatient(destination_patient_id=pid, matched_on="identifier")
        )


# --- which identifier the destination is searched by (#232) -------------------
#
# The search takes ONE identifier, and it used to be whichever mapped first —
# so the order the SOURCE DOCUMENT listed them in chose it. For the shipped
# C-CDA fixture that was the patient's SSN, and search params ride in the query
# string, where a request line is logged by the destination and by any proxy
# between. These hold the order as a decision.


def _searched_by(patient: Patient, *, allow_ssn: bool = False) -> str:
    """The system of the identifier the resolver would search by.

    Returns a parenthesised description instead when no identifier is used:
    the demographic fallback, or the refusal to search at all.
    """
    from anastomosis.deliver.fhir_api.destination import FhirApiDestination as D

    destination = D.__new__(D)
    destination._search_by_ssn = allow_ssn
    params, matched = D._search_params(destination, patient)
    if not params:
        return f"(no search: {','.join(matched)})"
    if "identifier" not in params:
        return f"(demographics: {','.join(matched)})"
    return params["identifier"].split("|", 1)[0]


def _patient_with(*kinds: IdentifierKind) -> Patient:
    return Patient(
        id="feedface-0000-0000-0000-0000000000aa",
        family_name="Testpatient",
        given_name="Synthia",
        identifiers=[Identifier(kind=kind, value=f"{kind.value}-value") for kind in kinds],
    )


def test_the_search_identifier_is_chosen_not_inherited_from_document_order() -> None:
    """The same two identifiers, either way round, choose the same one."""
    from anastomosis.core.fhir import export

    guid_first = _searched_by(_patient_with(IdentifierKind.SOURCE_GUID, IdentifierKind.SSN))
    ssn_first = _searched_by(_patient_with(IdentifierKind.SSN, IdentifierKind.SOURCE_GUID))
    assert guid_first == ssn_first == export.IDENTIFIER_SYSTEMS["source_guid"]


@pytest.mark.parametrize(
    ("carried", "expected"),
    [
        ((IdentifierKind.SSN, IdentifierKind.MRN), "mrn"),
        ((IdentifierKind.SSN, IdentifierKind.PRN), "prn"),
        ((IdentifierKind.SSN, IdentifierKind.OTHER), "other"),
        ((IdentifierKind.MRN, IdentifierKind.SOURCE_GUID), "source_guid"),
    ],
)
def test_anything_else_a_patient_carries_is_preferred_to_the_ssn(
    carried: tuple[IdentifierKind, ...], expected: str
) -> None:
    from anastomosis.core.fhir import export

    assert _searched_by(_patient_with(*carried)) == export.IDENTIFIER_SYSTEMS[expected]
    # And with the SSN allowed, the answer does not move: turning the option on
    # changes what happens for patients carrying nothing else, and nobody else.
    assert (
        _searched_by(_patient_with(*carried), allow_ssn=True)
        == (export.IDENTIFIER_SYSTEMS[expected])
    )


def test_an_ssn_only_patient_is_not_searched_for_by_default() -> None:
    """The question #232 left open, answered the conservative way.

    Search params ride in the query string, which the destination and every
    proxy between record in a request line — not a place this tool can clean up
    afterwards. So an SSN goes there when the operator asks for it and not
    because the patient had nothing else.
    """
    assert _searched_by(_patient_with(IdentifierKind.SSN)).startswith("(no search:")


def test_an_ssn_only_patient_does_not_slide_into_a_demographic_match() -> None:
    """The failure mode that would make this fix worse than the bug.

    Demographics exists for a patient the source gave no identity at all.
    Letting a withheld SSN fall through to it would trade a query-string
    exposure for a name-and-DOB match on a stranger, which is the wrong-patient
    failure this whole subsystem exists to prevent.
    """
    patient = Patient(
        id="feedface-0000-0000-0000-0000000000ae",
        family_name="Testpatient",
        given_name="Synthia",
        birth_date=date(1980, 1, 2),
        identifiers=[Identifier(kind=IdentifierKind.SSN, value="900-00-0000")],
    )
    answer = _searched_by(patient)
    assert answer.startswith("(no search:")
    assert "demographics" not in answer


def test_the_operator_can_still_reach_an_ssn_only_patient() -> None:
    """A destination that really does hold its patients under nothing else is
    not locked out — it is opted into, once, by the person who knows that."""
    from anastomosis.core.fhir import export

    assert (
        _searched_by(_patient_with(IdentifierKind.SSN), allow_ssn=True)
        == (export.IDENTIFIER_SYSTEMS["ssn"])
    )


def test_two_identifiers_of_one_kind_keep_source_order() -> None:
    """A deterministic answer when the preference cannot separate them."""
    patient = Patient(
        id="feedface-0000-0000-0000-0000000000ab",
        identifiers=[
            Identifier(kind=IdentifierKind.MRN, value="first"),
            Identifier(kind=IdentifierKind.MRN, value="second"),
        ],
    )
    from anastomosis.deliver.fhir_api.destination import FhirApiDestination as D

    destination = D.__new__(D)
    destination._search_by_ssn = False
    params, _ = D._search_params(destination, patient)
    assert params["identifier"].endswith("|first")


def test_a_patient_with_no_identifier_still_falls_back_to_demographics() -> None:
    patient = Patient(
        id="feedface-0000-0000-0000-0000000000ac", family_name="Testpatient", given_name="Synthia"
    )
    assert _searched_by(patient).startswith("(demographics")


def test_an_identifier_with_no_value_does_not_become_an_empty_search() -> None:
    """A blank identifier must fall back, not search the destination for "".

    Found by a surviving mutation rather than by reading: the preference order
    covers every kind, so the only identifier the loop can skip is one with no
    value — and searching ``mrn|`` would ask the destination to match every
    patient whose MRN is blank.
    """
    patient = Patient(
        id="feedface-0000-0000-0000-0000000000ad",
        family_name="Testpatient",
        given_name="Synthia",
        identifiers=[Identifier(kind=IdentifierKind.MRN, value="")],
    )
    assert _searched_by(patient).startswith("(demographics")


@pytest.mark.parametrize("source", ["pf-tebra", "ccda", "fhir-r4", "oracle-ehi"])
def test_no_shipped_adapter_sends_an_ssn_when_the_patient_has_another_id(source: str) -> None:
    """The fixture sweep that found this, kept as the guard.

    C-CDA's first ``v3:id`` carries the SSN OID, so before the preference order
    every C-CDA patient was looked up in the destination by their SSN.
    """
    import anastomosis.sources.ccda
    import anastomosis.sources.fhir_r4
    import anastomosis.sources.oracle_ehi
    import anastomosis.sources.pf_tebra  # noqa: F401
    from anastomosis.core.fhir import export
    from anastomosis.sources import get_source

    fixtures = {
        "pf-tebra": "pf_tebra_v9",
        "ccda": "ccda",
        "fhir-r4": "fhir_r4",
        "oracle-ehi": "oracle_ehi_v500",
    }
    root = Path(__file__).resolve().parents[1] / "fixtures" / fixtures[source]
    records = list(get_source(source).load(root))
    assert records, f"{source} fixture loaded nothing"
    for record in records:
        kinds = {i.kind for i in record.patient.identifiers if i.value}
        if kinds - {IdentifierKind.SSN}:
            assert _searched_by(record.patient) != export.IDENTIFIER_SYSTEMS["ssn"], (
                f"{source} would send an SSN despite carrying {sorted(k.value for k in kinds)}"
            )
