"""A FHIR R4 ``DocumentReference`` pusher implementing the Destination protocol.

:class:`FhirApiDestination` files a chart's PDFs as ``DocumentReference``
resources, plus the optional :class:`MetadataReader`/:class:`DocumentReader`
capabilities that give the verifier L5/L6 for free. The resolver searches by
the export identifier systems first, else demographics; one match resolves,
multiple is a hard error, zero returns ``None`` (or creates a ``Patient``).
The payload preflight is RULES.md 43; the same-origin URL check is RULES.md 42.
"""

from __future__ import annotations

import base64
import logging
from collections.abc import Mapping

from anastomosis.core.clock import now as _clock_now
from anastomosis.core.fhir import export
from anastomosis.core.logutil import exc_tag
from anastomosis.core.model import Identifier, IdentifierKind, Patient, PatientRecord
from anastomosis.deliver.browser.errors import PermanentDeliveryError
from anastomosis.destinations.base import (
    DestinationPatient,
    Session,
    UploadItem,
    UploadReceipt,
)

from .client import FhirClient

__all__ = ["FhirApiDestination", "PayloadTooLarge"]

logger = logging.getLogger(__name__)

# The generic default document type: LOINC 34109-9 "Note". A migration that
# knows its note LOINC (Progress 11506-3, H&P 34117-2, …) overrides it.
_DEFAULT_DOC_LOINC = "34109-9"
_DEFAULT_DOC_DISPLAY = "Note"
_LOINC_SYSTEM = export.LOINC
_PDF_MIME = "application/pdf"

_MIB = 1024 * 1024
# Measured: a 32 MiB PDF peaks ~139 MiB inline (bytes + base64 + request);
# 50 MiB stays far above any chart this toolkit renders while keeping that
# multiple bounded. A destination that accepts more sets ``max_payload_bytes``.
_MAX_PAYLOAD_BYTES = 50 * _MIB


class PayloadTooLarge(PermanentDeliveryError):
    """An item exceeds the inline-payload bound (RULES.md 43): permanent, and
    the message never names the filename.
    """


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


#: Which of a patient's identifiers to search the destination by, best first.
#: The search takes ONE identifier, and :meth:`FhirClient.get` puts it in the
#: query string, which reaches the destination's access log and any proxy
#: between — so the SSN is the last resort, not a coin toss. The source GUID
#: leads because a re-run into a destination this tool wrote to matches its
#: own recorded identity.
_SEARCH_IDENTIFIER_ORDER: tuple[IdentifierKind, ...] = (
    IdentifierKind.SOURCE_GUID,
    IdentifierKind.MRN,
    IdentifierKind.PRN,
    IdentifierKind.OTHER,
)

#: Reached only with ``--search-by-ssn``: an SSN in a URL query string reaches
#: the destination's access log and every proxy between (#232). Off, an
#: SSN-only patient is simply not found — a visible, recoverable duplicate
#: with ``--create-patients``, or a skipped, reported item without it.
_SSN_SEARCH_KIND = IdentifierKind.SSN


def _has_usable_ssn(patient: Patient) -> bool:
    """Does this patient carry a nonblank SSN — distinct from a blank
    identifier the source never gave?
    """
    return any(i.kind is _SSN_SEARCH_KIND and i.value for i in patient.identifiers)


def _search_identifier(patient: Patient, *, allow_ssn: bool = False) -> Identifier | None:
    """The identifier to search by, or ``None`` to fall back: best kind
    first, source order within a kind, SSN only with ``allow_ssn`` and
    only last.
    """
    order = (*_SEARCH_IDENTIFIER_ORDER, _SSN_SEARCH_KIND) if allow_ssn else _SEARCH_IDENTIFIER_ORDER
    for kind in order:
        for ident in patient.identifiers:
            if ident.kind is kind and ident.value:
                return ident
    return None


class FhirApiDestination:
    """Push DocumentReferences into a FHIR R4 server (the aggregate Destination)."""

    def __init__(
        self,
        client: FhirClient,
        *,
        name: str = "fhir_api",
        create_missing_patients: bool = False,
        search_by_ssn: bool = False,
        doc_type_loinc: str = _DEFAULT_DOC_LOINC,
        doc_type_display: str = _DEFAULT_DOC_DISPLAY,
        max_payload_bytes: int = _MAX_PAYLOAD_BYTES,
    ) -> None:
        self._client = client
        self._name = name
        self._create_missing_patients = create_missing_patients
        self._search_by_ssn = search_by_ssn
        self._doc_type_loinc = doc_type_loinc
        self._doc_type_display = doc_type_display
        # The inline-payload bound, enforced BEFORE the file is read.
        self._max_payload_bytes = max_payload_bytes
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
        """Find the destination's record for ``patient``, never a guess:
        identifier first, demographics only when none exist; SSN-only
        resolves to nothing without ``search_by_ssn``. Zero matches creates
        a Patient when ``create_missing_patients`` is set.
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
        if not params:
            # Issuing the GET anyway would ask for every Patient the server
            # holds; matched_on names why, never a patient value.
            logger.info("no destination search is possible: %s", "; ".join(matched_on))
            return None
        bundle = self._client.get("Patient", params)
        ids = _entry_ids(bundle)
        if len(ids) > 1:
            # NEVER guess between charts — a wrong patient is the worst outcome.
            raise PermanentDeliveryError(
                f"Patient search matched {len(ids)} records on {matched_on}; refusing to guess"
            )
        if len(ids) == 1:
            return DestinationPatient(destination_patient_id=ids[0], matched_on=matched_on)
        # matched_on is a field NAME, never a value (SECURITY.md).
        # codeql[py/clear-text-logging-sensitive-data]
        logger.info("no destination patient matched on %s", matched_on)
        return None

    def _search_params(self, patient: Patient) -> tuple[dict[str, str], tuple[str, ...]]:
        """Patient search params + matched-on field names (PHI-safe):
        identifier via :data:`_SEARCH_IDENTIFIER_ORDER`, else family/given/
        birthdate (FHIR ANDs only the params present).
        """
        chosen = _search_identifier(patient, allow_ssn=self._search_by_ssn)
        if chosen is not None:
            system = export.IDENTIFIER_SYSTEMS[chosen.kind.value]
            return {"identifier": f"{system}|{chosen.value}"}, ("identifier",)
        if not self._search_by_ssn and _has_usable_ssn(patient):
            # Demographics is not the answer here: it exists for a patient the
            # source gave no identity at all, and falling back for a withheld
            # SSN would trade query-string exposure for a name/DOB guess.
            return {}, ("identifier withheld (SSN only; --search-by-ssn is off)",)
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
        if not params:
            return {}, ("no identifier, name or date of birth to search on",)
        return params, tuple(matched)

    def _create_patient(self, patient: Patient) -> DestinationPatient:
        """POST a Patient via ``export._patient`` (same identifier systems and
        extensions tail); the resource id is dropped so the server assigns one.
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
        """Re-read the Patient and compare family name (case-insensitive) and
        birthDate; a missing birthDate on either side fails closed. Uses the
        search-only ``_find``, never ``resolve``: verification must not
        create the record it verifies.
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
        """Fingerprints already filed for this patient (the duplicate defense):
        ``content[0].attachment.title`` across EVERY result page — a partial
        scan would miss fingerprints and let a resumed run re-file a chart.
        """
        fingerprints: set[str] = set()
        path: str | None = "DocumentReference"
        params: dict[str, str] | None = {"subject": f"Patient/{patient.destination_patient_id}"}
        pages = 0
        while path is not None:
            bundle = self._client.get(path, params)
            pages += 1
            for entry in bundle.get("entry", []) or []:
                title = _attachment_title(entry.get("resource", {}))
                if title:
                    fingerprints.add(title)
            next_url = _next_page_url(bundle)
            if next_url is None:
                break
            if pages >= _MAX_SCAN_PAGES:
                # A truncated scan is indistinguishable from a clean one, and
                # the difference is a doubled chart — refuse loudly instead.
                raise PermanentDeliveryError(
                    f"the existing-document scan did not finish after {pages} pages; "
                    "refusing to file rather than risk duplicating this chart"
                )
            path, params = self._same_origin_path(next_url, "search next link")
        logger.info("scanned %d existing fingerprint(s) over %d page(s)", len(fingerprints), pages)
        return fingerprints

    # --- UploadDriver ---

    def upload(self, item: UploadItem, patient: DestinationPatient) -> UploadReceipt:
        """File ``item`` as a ``DocumentReference``; return the receipt with
        the created id and the echoed attachment size, when the server
        returns one.
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
        self._check_payload_size(item)
        # attachment.hash is omitted: FHIR defines it as SHA-1, but the upload
        # ledger standardizes on sha256, so the fingerprint rides in the title
        # instead and L6 re-hashes with sha256.
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
            "date": _clock_now().isoformat(),
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

    def _check_payload_size(self, item: UploadItem) -> None:
        """Raises :class:`PayloadTooLarge` above the bound, measured on disk
        rather than the manifest's advisory size (RULES.md 43).
        """
        size = item.file_path.stat().st_size
        if size <= self._max_payload_bytes:
            return
        # PHI: the opaque item_key and the two sizes — never the filename (it
        # embeds the patient name and date of service) and never a value.
        raise PayloadTooLarge(
            f"item {item.item_key} is {size / _MIB:.1f} MiB; this route inlines the "
            f"document in the request body and refuses payloads over "
            f"{self._max_payload_bytes / _MIB:.0f} MiB. File it by the browser route, "
            f"or raise max_payload_bytes if the destination server accepts more."
        )

    # --- MetadataReader (optional capability -> L5) ---

    def read_metadata(
        self, patient: DestinationPatient, destination_doc_id: str
    ) -> Mapping[str, str | int]:
        """A filed document's ``attachment.size`` when the server stored it;
        page count is omitted (not a FHIR attachment field) and L5 checks
        only what is present.
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
        """The stored document's bytes for the L6 round-trip: inline
        ``attachment.data`` first, else a same-origin ``attachment.url``
        (RULES.md 42). Neither present is a hard error.
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

    def _same_origin_path(self, url: str, what: str) -> tuple[str, dict[str, str]]:
        """A server-supplied URL, checked same-origin and turned into
        ``(path, params)`` (RULES.md 42); ``what`` names it in the refusal.
        """
        from urllib.parse import parse_qsl, urlsplit

        base = urlsplit(self._client.base_url)
        target = urlsplit(url)
        if target.scheme or target.netloc:
            same_origin = (
                target.scheme == base.scheme
                and target.hostname == base.hostname
                and target.port == base.port
            )
            if not same_origin:
                raise PermanentDeliveryError(f"{what} is cross-origin; refusing to follow")
            # The client re-joins onto the base URL, so hand it a path relative
            # to the base — strip the base path prefix from the absolute path.
            path = target.path
            if base.path and path.startswith(base.path):
                path = path[len(base.path) :]
        elif url.startswith("/"):
            # Scheme-less absolute path (same server by construction): still
            # strip the base path so the client doesn't double-prefix it.
            path = target.path
            if base.path and path.startswith(base.path):
                path = path[len(base.path) :]
        else:
            path = target.path  # already relative to the base
        return path, dict(parse_qsl(target.query))

    def _read_attachment_url(self, url: str) -> bytes:
        """Fetch a by-reference attachment, refusing a cross-origin URL.

        The fetch reuses the client's GET, which returns parsed JSON — a Binary
        resource carries the bytes in its base64 ``data`` field.
        """
        path, params = self._same_origin_path(url, "attachment url")
        resource = self._client.get(path, params or None)
        data = resource.get("data")
        if isinstance(data, str):
            return base64.b64decode(data)
        raise PermanentDeliveryError("by-reference attachment returned no data")


#: Search pages the duplicate scan walks before refusing — a server whose
#: `next` link never terminates would otherwise loop forever.
_MAX_SCAN_PAGES = 200


# --- module helpers (PHI-safe: shape readers only, never log values) ---------


def _next_page_url(bundle: Mapping[str, object]) -> str | None:
    """The searchset's continuation link, or ``None`` on the last page —
    shape-tolerant; the caller's page cap is what stops a malformed `next`.
    """
    links = bundle.get("link")
    if not isinstance(links, list):
        return None
    for link in links:
        if isinstance(link, Mapping) and link.get("relation") == "next":
            url = link.get("url")
            if isinstance(url, str) and url:
                return url
    return None


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
