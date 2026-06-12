"""A FHIR R4 ``DocumentReference`` pusher implementing the Destination protocol.

:class:`FhirApiDestination` is the API counterpart to a browser destination
pack: it files a reconstructed chart's PDFs into a FHIR R4 server as
``DocumentReference`` resources, and it implements the same
:class:`~anastomosis.destinations.base.Destination` protocol the upload engine
drives — plus the optional :class:`MetadataReader` and :class:`DocumentReader`
capabilities, so the :class:`~anastomosis.deliver.verify.LayeredVerifier` gets
its L5 (metadata cross-check) and L6 (round-trip read-back) for free.

Identifier-system reuse (load-bearing for the patient match): the resolver
searches ``Patient?identifier={system}|{value}`` using the **exact** identifier
systems :mod:`anastomosis.core.fhir.export` writes
(:data:`~anastomosis.core.fhir.export.IDENTIFIER_SYSTEMS`), so a chart exported
by this toolkit and re-homed through this destination round-trips on the same
system URIs. When the canonical patient has no identifier at all, the resolver
falls back to a demographic search (``family`` + ``given`` + ``birthdate``).
Exactly one match resolves; multiple matches are a hard
:class:`PermanentDeliveryError` (filing against a guessed patient is the
wrong-patient failure the whole subsystem exists to prevent); zero matches
return ``None`` — or, with ``create_missing_patients``, POST a new ``Patient``
built by the existing export code and use the created id.

Patient resource construction reuses ``export._patient`` verbatim (the lossless
extensions tail and identifier systems come along), with the resource ``id``
dropped before the POST so the server assigns its own.

PHI rule: every log line and every raised message carries counts, opaque ids,
HTTP statuses, and ``exc_tag`` type names only — never an identifier value, a
name, a DOB, a token, or a URL.
"""

from __future__ import annotations

import base64
import logging
from collections.abc import Mapping
from datetime import UTC, datetime

from anastomosis.core.fhir import export
from anastomosis.core.logutil import exc_tag
from anastomosis.core.model import Patient, PatientRecord
from anastomosis.deliver.browser.errors import PermanentDeliveryError
from anastomosis.destinations.base import (
    DestinationPatient,
    Session,
    UploadItem,
    UploadReceipt,
)

from .client import FhirClient

__all__ = ["FhirApiDestination"]

logger = logging.getLogger(__name__)

# The generic default document type: LOINC 34109-9 "Note". A migration that
# knows its note LOINC (Progress 11506-3, H&P 34117-2, …) overrides it.
_DEFAULT_DOC_LOINC = "34109-9"
_DEFAULT_DOC_DISPLAY = "Note"
_LOINC_SYSTEM = export.LOINC
_PDF_MIME = "application/pdf"


class _NoopSession:
    """A no-op session whose liveness is one cached ``GET {base}/metadata``."""

    def __init__(self, client: FhirClient) -> None:
        self._client = client
        self._alive: bool | None = None

    def open(self) -> None:
        return None

    def close(self) -> None:
        return None

    def is_alive(self) -> bool:
        # Cache the capability probe: the engine may ask repeatedly, and a FHIR
        # server's CapabilityStatement does not change mid-run.
        if self._alive is None:
            try:
                self._client.get("metadata")
                self._alive = True
            except Exception as exc:  # a dead/unreachable server is simply not alive
                logger.warning("FHIR metadata probe failed (%s)", exc_tag(exc))
                self._alive = False
        return self._alive


class FhirApiDestination:
    """Push DocumentReferences into a FHIR R4 server (the aggregate Destination)."""

    def __init__(
        self,
        client: FhirClient,
        *,
        name: str = "fhir_api",
        create_missing_patients: bool = False,
        doc_type_loinc: str = _DEFAULT_DOC_LOINC,
        doc_type_display: str = _DEFAULT_DOC_DISPLAY,
    ) -> None:
        self._client = client
        self._name = name
        self._create_missing_patients = create_missing_patients
        self._doc_type_loinc = doc_type_loinc
        self._doc_type_display = doc_type_display
        self._session = _NoopSession(client)

    # --- Destination protocol ---

    @property
    def name(self) -> str:
        return self._name

    @property
    def session(self) -> Session:
        return self._session

    @property
    def resolver(self) -> FhirApiDestination:
        return self

    @property
    def banner(self) -> FhirApiDestination:
        return self

    @property
    def scanner(self) -> FhirApiDestination:
        return self

    @property
    def driver(self) -> FhirApiDestination:
        return self

    # --- PatientResolver ---

    def resolve(self, patient: Patient) -> DestinationPatient | None:
        """Find the destination's record for ``patient`` (never a guess).

        Searches by identifier first (the export identifier-system convention),
        falling back to a demographic search only when the patient carries no
        identifier. Exactly one match resolves; multiple is a hard error; zero
        returns ``None`` unless ``create_missing_patients`` is set, which POSTs
        a new Patient and returns its id.
        """
        found = self._find(patient)
        if found is not None:
            return found
        if self._create_missing_patients:
            return self._create_patient(patient)
        return None

    def _find(self, patient: Patient) -> DestinationPatient | None:
        """Search-only resolution: never creates (the banner check uses this).

        A verification step must be side-effect free — a banner re-resolve
        that could CREATE a patient would corrupt the very state it verifies.
        """
        params, matched_on = self._search_params(patient)
        bundle = self._client.get("Patient", params)
        ids = _entry_ids(bundle)
        if len(ids) > 1:
            # NEVER guess between charts — a wrong patient is the worst outcome.
            raise PermanentDeliveryError(
                f"Patient search matched {len(ids)} records on {matched_on}; refusing to guess"
            )
        if len(ids) == 1:
            return DestinationPatient(destination_patient_id=ids[0], matched_on=matched_on)
        logger.info("no destination patient matched on %s", matched_on)
        return None

    def _search_params(self, patient: Patient) -> tuple[dict[str, str], tuple[str, ...]]:
        """Build the Patient search params + the matched-on field names (PHI-safe).

        Identifier search uses ``{system}|{value}`` with the export systems.
        The demographic fallback uses family/given/birthdate; a missing
        birth_date there is still searched (FHIR ANDs only the params present),
        and the matched_on names reflect exactly which params were sent.
        """
        for ident in patient.identifiers:
            system = export.IDENTIFIER_SYSTEMS.get(ident.kind.value)
            if system and ident.value:
                return {"identifier": f"{system}|{ident.value}"}, ("identifier",)
        params: dict[str, str] = {}
        matched: list[str] = []
        if patient.family_name:
            params["family"] = patient.family_name
            matched.append("family_name")
        if patient.given_name:
            params["given"] = patient.given_name
            matched.append("given_name")
        if patient.birth_date:
            params["birthdate"] = patient.birth_date.isoformat()
            matched.append("birth_date")
        return params, tuple(matched)

    def _create_patient(self, patient: Patient) -> DestinationPatient:
        """POST a new Patient built by the export code; return the created id.

        Reuses ``export._patient`` so the resource carries the same identifier
        systems and lossless extensions tail a normal export would. The
        resource ``id`` is dropped so the server assigns its own.
        """
        resource = export._patient(patient, PatientRecord(patient=patient))
        resource.pop("id", None)
        _body, created_id = self._client.post("Patient", resource)
        if created_id is None:
            raise PermanentDeliveryError("Patient create returned no id")
        logger.info("created destination Patient")
        return DestinationPatient(destination_patient_id=created_id, matched_on=("created",))

    # --- BannerCheck ---

    def current_patient_matches(self, expected: Patient) -> bool:
        """API-mode wrong-patient defense: re-read the Patient and compare.

        Reads ``Patient/{id}`` (the id resolved for this item is not carried
        here, so the banner re-resolves ``expected`` to the same id the engine
        used) and checks family name (case-insensitive) AND birthDate. A
        missing birthDate on either side is a fail-closed ``False`` — the
        verification cannot be completed, so it is treated as a mismatch.

        Uses the search-only ``_find`` (never the creating ``resolve``): a
        verification step must not create the record it verifies.
        """
        resolved = self._find(expected)
        if resolved is None:
            return False
        try:
            resource = self._client.get(f"Patient/{resolved.destination_patient_id}")
        except Exception as exc:  # an unreadable banner cannot confirm — fail closed
            logger.warning("banner read failed (%s)", exc_tag(exc))
            return False
        return _family_matches(resource, expected) and _birthdate_matches(resource, expected)

    # --- ExistingDocsScanner ---

    def existing_fingerprints(self, patient: DestinationPatient) -> set[str]:
        """Fingerprints already filed for this patient (the duplicate defense).

        Lists ``DocumentReference?subject=Patient/{id}`` and reads each entry's
        ``content[0].attachment.title`` — the same field the driver writes — so
        a document filed on a prior (possibly crashed) run is found and skipped.
        """
        bundle = self._client.get(
            "DocumentReference",
            {"subject": f"Patient/{patient.destination_patient_id}"},
        )
        fingerprints: set[str] = set()
        for entry in bundle.get("entry", []) or []:
            title = _attachment_title(entry.get("resource", {}))
            if title:
                fingerprints.add(title)
        logger.info("scanned %d existing fingerprint(s)", len(fingerprints))
        return fingerprints

    # --- UploadDriver ---

    def upload(self, item: UploadItem, patient: DestinationPatient) -> UploadReceipt:
        """File ``item`` as a ``DocumentReference`` and return the receipt.

        Builds the resource (status current, LOINC type, subject, tz-aware UTC
        date, base64 PDF attachment carrying the fingerprint as its title and
        the size as ``attachment.size``), POSTs it, and reports the created id
        plus the echoed attachment size when the server returns a body.
        """
        resource = self._document_reference(item, patient)
        body, created_id = self._client.post("DocumentReference", resource)
        if created_id is None:
            raise PermanentDeliveryError("DocumentReference create returned no id")
        echoed = _echoed_size(body)
        logger.info("filed DocumentReference for patient (size echoed: %s)", echoed is not None)
        return UploadReceipt(destination_doc_id=created_id, echoed_size_bytes=echoed)

    def _document_reference(
        self, item: UploadItem, patient: DestinationPatient
    ) -> dict[str, object]:
        # attachment.hash is deliberately OMITTED: FHIR R4 defines it as the
        # SHA-1 of the data, but the upload ledger standardizes on sha256, so a
        # SHA-1 here would be a second, conflicting digest. The fingerprint
        # (sha256-derived) rides in the title instead; the round-trip read-back
        # (L6) re-hashes with sha256.
        data = base64.b64encode(item.file_path.read_bytes()).decode("ascii")
        return {
            "resourceType": "DocumentReference",
            "status": "current",
            "type": {
                "coding": [
                    {
                        "system": _LOINC_SYSTEM,
                        "code": self._doc_type_loinc,
                        "display": self._doc_type_display,
                    }
                ]
            },
            "subject": {"reference": f"Patient/{patient.destination_patient_id}"},
            "date": datetime.now(UTC).isoformat(),
            "content": [
                {
                    "attachment": {
                        "contentType": _PDF_MIME,
                        "data": data,
                        "title": item.fingerprint,
                        "size": item.size_bytes,
                    }
                }
            ],
        }

    # --- MetadataReader (optional capability -> L5) ---

    def read_metadata(
        self, patient: DestinationPatient, destination_doc_id: str
    ) -> Mapping[str, str | int]:
        """The destination's metadata for a filed document: size when present.

        Reads ``DocumentReference/{id}`` and reports ``attachment.size`` when
        the server stored it. Page count is not a FHIR attachment field, so it
        is simply omitted — L5 then checks only what is present.
        """
        resource = self._client.get(f"DocumentReference/{destination_doc_id}")
        attachment = _first_attachment(resource)
        meta: dict[str, str | int] = {}
        size = attachment.get("size")
        if isinstance(size, int):
            meta["size_bytes"] = size
        return meta

    # --- DocumentReader (optional capability -> L6) ---

    def read_back(self, patient: DestinationPatient, destination_doc_id: str) -> bytes:
        """Read the stored document's bytes back for the L6 round-trip.

        Prefers inline ``attachment.data`` (base64). When the attachment is
        stored by reference (``attachment.url``), the URL is followed ONLY when
        it is same-origin with the configured base URL — a cross-origin
        attachment URL is refused (it could redirect the read-back at an
        attacker-controlled host carrying the bearer token). A non-conforming
        attachment is a hard error rather than a silent empty read.
        """
        resource = self._client.get(f"DocumentReference/{destination_doc_id}")
        attachment = _first_attachment(resource)
        inline = attachment.get("data")
        if isinstance(inline, str):
            return base64.b64decode(inline)
        url = attachment.get("url")
        if isinstance(url, str) and url:
            return self._read_attachment_url(url)
        raise PermanentDeliveryError("DocumentReference attachment has neither data nor url")

    def _read_attachment_url(self, url: str) -> bytes:
        """Fetch a by-reference attachment, refusing a cross-origin URL.

        Same-origin rule: the attachment URL must share the base URL's scheme,
        host, and port. A relative path is resolved against the base. The fetch
        reuses the client's GET, which returns parsed JSON — a Binary resource
        carries the bytes in its base64 ``data`` field.
        """
        from urllib.parse import urlsplit

        base = urlsplit(self._client.base_url)
        target = urlsplit(url)
        if target.scheme or target.netloc:
            same_origin = (
                target.scheme == base.scheme
                and target.hostname == base.hostname
                and target.port == base.port
            )
            if not same_origin:
                raise PermanentDeliveryError("attachment url is cross-origin; refusing to follow")
            # The client re-joins onto the base URL, so hand it a path relative
            # to the base — strip the base path prefix from the absolute path.
            path = target.path
            if base.path and path.startswith(base.path):
                path = path[len(base.path) :]
        elif url.startswith("/"):
            # Scheme-less absolute path (same server by construction): still
            # strip the base path so the client doesn't double-prefix it.
            path = url
            if base.path and path.startswith(base.path):
                path = path[len(base.path) :]
        else:
            path = url  # already relative to the base
        resource = self._client.get(path)
        data = resource.get("data")
        if isinstance(data, str):
            return base64.b64decode(data)
        raise PermanentDeliveryError("by-reference attachment returned no data")


# --- module helpers (PHI-safe: shape readers only, never log values) ---------


def _entry_ids(bundle: Mapping[str, object]) -> list[str]:
    """Resource ids from a FHIR searchset Bundle's entries."""
    ids: list[str] = []
    entries = bundle.get("entry")
    if not isinstance(entries, list):
        return ids
    for entry in entries:
        if not isinstance(entry, Mapping):
            continue
        resource = entry.get("resource")
        if isinstance(resource, Mapping):
            rid = resource.get("id")
            if isinstance(rid, str):
                ids.append(rid)
    return ids


def _attachment_title(resource: Mapping[str, object]) -> str | None:
    attachment = _first_attachment(resource)
    title = attachment.get("title")
    return title if isinstance(title, str) else None


def _first_attachment(resource: Mapping[str, object]) -> Mapping[str, object]:
    content = resource.get("content")
    if isinstance(content, list) and content and isinstance(content[0], Mapping):
        attachment = content[0].get("attachment")
        if isinstance(attachment, Mapping):
            return attachment
    return {}


def _echoed_size(body: Mapping[str, object] | None) -> int | None:
    if body is None:
        return None
    size = _first_attachment(body).get("size")
    return size if isinstance(size, int) else None


def _family_matches(resource: Mapping[str, object], expected: Patient) -> bool:
    if not expected.family_name:
        return False
    want = expected.family_name.casefold()
    names = resource.get("name")
    if not isinstance(names, list):
        return False
    for name in names:
        if isinstance(name, Mapping):
            family = name.get("family")
            if isinstance(family, str) and family.casefold() == want:
                return True
    return False


def _birthdate_matches(resource: Mapping[str, object], expected: Patient) -> bool:
    # Fail closed: a missing birthDate on either side cannot be confirmed equal.
    if expected.birth_date is None:
        return False
    birth_date = resource.get("birthDate")
    return isinstance(birth_date, str) and birth_date == expected.birth_date.isoformat()
