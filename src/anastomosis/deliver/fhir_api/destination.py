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

Size rule: a ``DocumentReference`` carries the PDF *inside* the JSON body, so
filing one costs several times the file in memory (read + base64 + serialized
request). :class:`FhirApiDestination` therefore refuses an oversized item
BEFORE reading it, raising :class:`PayloadTooLarge`; the bound is the
``max_payload_bytes`` constructor argument. Streaming the bytes as a ``Binary``
resource is the tracked longer-term path (docs/PLAN.md, "Open work").

PHI rule: every log line and every raised message carries counts, opaque ids,
HTTP statuses, and ``exc_tag`` type names only — never an identifier value, a
name, a DOB, a token, or a URL.
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
# Largest single document this route will inline. Measured: building the
# resource for a 32 MiB PDF peaks ~139 MiB (~4.3x — the bytes, their base64
# form, and the serialized request all live at once), so an unbounded item is
# an out-of-memory risk on the operator's own machine and a request most
# servers would reject anyway. 50 MiB is far above any chart this toolkit
# renders while keeping that multiple bounded; a destination that accepts more
# gets it via ``max_payload_bytes``.
_MAX_PAYLOAD_BYTES = 50 * _MIB


class PayloadTooLarge(PermanentDeliveryError):
    """One item's document exceeds the route's inline-payload bound.

    Permanent, not transient: retrying re-reads the same oversized file. The
    message names the opaque ``item_key`` and the limit — never a filename
    (chart filenames embed the patient name and date of service) and never a
    patient value.
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


#: Which of a patient's identifiers to look them up in the destination by, best
#: first. The search takes ONE identifier, so this decides which patient value
#: is sent — and :meth:`FhirClient.get` puts search params in the query string,
#: which reaches the destination's access log and any proxy between. That is a
#: different exposure surface from a request body, and not one this tool can
#: clean up afterwards, so the SSN is the last resort rather than a coin toss.
#:
#: Before this was a list, the search used the FIRST identifier that mapped to a
#: known system, so the order the SOURCE DOCUMENT happened to list them in chose
#: it. Measured across the shipped fixtures, that meant a source GUID for
#: pf-tebra and oracle-ehi, an MRN for fhir-r4 — and the patient's SSN for
#: c-cda, whose first ``v3:id`` carries the SSN OID.
#:
#: The source GUID leads because on a re-run into a destination this tool has
#: written to, it is the exact identity this tool recorded. The destination's
#: own record numbers come next.
_SEARCH_IDENTIFIER_ORDER: tuple[IdentifierKind, ...] = (
    IdentifierKind.SOURCE_GUID,
    IdentifierKind.MRN,
    IdentifierKind.PRN,
    IdentifierKind.OTHER,
)

#: The one kind that is NOT reached for unless an operator asks. It sat at the
#: end of the order above, so a patient carrying only an SSN had it put in a
#: URL query string — and that reaches the destination's access log and every
#: proxy between, which is not somewhere this tool can clean up afterwards.
#:
#: The question #232 left open was whether an SSN should ever be a search
#: parameter, and the honest reading of the trade is that it is the operator's
#: to make, not one to inherit by default. Off, a patient with nothing else
#: simply is not found: with --create-patients that is a duplicate chart, which
#: is visible and recoverable, and without it the item is skipped and reported.
#: On (``--search-by-ssn``), the SSN is the last resort it used to be, for a
#: destination that really does hold its patients under nothing else.
_SSN_SEARCH_KIND = IdentifierKind.SSN


def _has_usable_ssn(patient: Patient) -> bool:
    """Does this patient carry an SSN with something in it?

    The distinction that matters when nothing else was searchable: an SSN this
    run declines to send is an identity withheld, while a blank identifier is
    an identity the source never gave.
    """
    return any(i.kind is _SSN_SEARCH_KIND and i.value for i in patient.identifiers)


def _search_identifier(patient: Patient, *, allow_ssn: bool = False) -> Identifier | None:
    """The identifier to search the destination by, or None to fall back.

    Best kind first, and within a kind the one the source listed first — an
    adapter that emits two MRNs still gets the same answer every run. The SSN
    is reached only with ``allow_ssn``, and only after everything else, so
    turning it on changes the answer for exactly the patients who carry
    nothing better.
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
        """Find the destination's record for ``patient`` (never a guess).

        Searches by identifier first (the export identifier-system convention),
        falling back to a demographic search only when the patient carries no
        identifier at all. A patient carrying ONLY an SSN is not that patient:
        without ``search_by_ssn`` there is nothing this run will search on, and
        it resolves to nothing rather than to a name-and-DOB match. Exactly one
        match resolves; multiple is a hard error; zero returns ``None`` unless
        ``create_missing_patients`` is set, which POSTs a new Patient and
        returns its id.
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
            # Nothing to search ON. Issuing the GET anyway would ask the server
            # for every Patient it holds, and the multi-match refusal below
            # would then report "matched 4,000 records" — true, useless, and
            # hiding the actual reason. PHI-safe: matched_on names why, never a
            # patient value.
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
        # PHI-BY-DESIGN: ``matched_on`` is the NAME of the field(s) the search
        # used ("identifier" / "family_name" / "given_name" / "birth_date"),
        # never a patient value — the field-name-not-value logging discipline.
        # See SECURITY.md, "Code scanning & suppression policy (auditable)".
        # codeql[py/clear-text-logging-sensitive-data]
        logger.info("no destination patient matched on %s", matched_on)
        return None

    def _search_params(self, patient: Patient) -> tuple[dict[str, str], tuple[str, ...]]:
        """Build the Patient search params + the matched-on field names (PHI-safe).

        Identifier search uses ``{system}|{value}`` with the export systems, on
        the identifier :data:`_SEARCH_IDENTIFIER_ORDER` prefers rather than
        whichever one happens to be first (see that constant). The demographic
        fallback uses family/given/birthdate; a missing birth_date there is
        still searched (FHIR ANDs only the params present), and the matched_on
        names reflect exactly which params were sent.
        """
        chosen = _search_identifier(patient, allow_ssn=self._search_by_ssn)
        if chosen is not None:
            system = export.IDENTIFIER_SYSTEMS[chosen.kind.value]
            return {"identifier": f"{system}|{chosen.value}"}, ("identifier",)
        if not self._search_by_ssn and _has_usable_ssn(patient):
            # Carries an identity, just not one this run will put in a URL.
            # Demographics is NOT the answer here: that fallback exists for a
            # patient the source gave no identity at all, and letting a
            # withheld SSN reach it would trade a query-string exposure for a
            # name-and-DOB match on a stranger — the failure this subsystem
            # exists to prevent, and the worse of the two. A patient whose only
            # identifier entry is BLANK is a different case: the source gave
            # nothing, so the fallback below is right for them.
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

        EVERY page. A FHIR search returns a searchset the SERVER pages, and
        advertises the continuation as ``Bundle.link[relation="next"]``. Reading
        only the first page means a patient with more documents than the
        server's page size has fingerprints this scan cannot see — so a resumed
        run re-files a chart already in their record, which is the one thing
        this scan exists to prevent. A patient with 21 documents behind a
        20-per-page server had exactly one invisible fingerprint, and it was the
        most recently filed one: the likeliest to be the crashed run's.
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
                # Loudly, not quietly: a truncated scan is indistinguishable
                # from a clean one, and the consequence of the difference is a
                # doubled chart. Count only — never a patient id.
                raise PermanentDeliveryError(
                    f"the existing-document scan did not finish after {pages} pages; "
                    "refusing to file rather than risk duplicating this chart"
                )
            path, params = self._same_origin_path(next_url, "search next link")
        logger.info("scanned %d existing fingerprint(s) over %d page(s)", len(fingerprints), pages)
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
        # Refuse an oversized document BEFORE it is read: the read, its base64
        # form, and the serialized request all coexist, so the peak is several
        # times the file. Checking first is what keeps that bounded.
        self._check_payload_size(item)
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
        """Raise :class:`PayloadTooLarge` when ``item`` exceeds the bound.

        The bound is measured against the file's ACTUAL size on disk, and only
        that: the bytes this route is about to read, base64-encode, and
        serialize are the bytes that set the memory peak. ``item.size_bytes``
        comes from the upload manifest and is advisory — a stale manifest that
        UNDERSTATES the file must not wave it past (the stat catches that), and
        one that OVERSTATES a small file must not refuse a chart this route can
        perfectly well deliver. The message reports the measured size, so what
        the operator is told is what was actually weighed.

        Ordering is load-bearing: this runs BEFORE the read, which is the whole
        point of a preflight.
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

    def _same_origin_path(self, url: str, what: str) -> tuple[str, dict[str, str]]:
        """A server-supplied URL, checked and turned into (path, params).

        Same-origin rule: the URL must share the base URL's scheme, host and
        port. A relative path resolves against the base. This is the same
        reasoning that makes the client refuse redirects — the Authorization
        header must never travel to a host the operator did not configure — and
        it applies to every URL the SERVER chooses, which is both a
        by-reference attachment and a search's `next` page link.

        ``what`` names the URL in the refusal, so the operator is told which one
        pointed off-origin.
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


#: How many search pages the duplicate scan will walk before refusing. A server
#: whose `next` link never terminates would otherwise loop forever; at typical
#: page sizes this is tens of thousands of documents for one patient.
_MAX_SCAN_PAGES = 200


# --- module helpers (PHI-safe: shape readers only, never log values) ---------


def _next_page_url(bundle: Mapping[str, object]) -> str | None:
    """The searchset's continuation link, or None on the last page.

    Shape-tolerant like its siblings here: a server that answers with something
    other than a list of link objects gets treated as "no more pages", not a
    crash — but the caller's page cap is what stops a malformed `next` looping.
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
