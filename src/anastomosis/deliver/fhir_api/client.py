"""A minimal FHIR R4 REST client over stdlib ``urllib`` — no new dependencies.

The API delivery route (PLAN item 13a) is the modern counterpart to the
browser route: when a destination speaks FHIR R4, a chart's notes are filed as
``DocumentReference`` resources over HTTPS instead of driven through a web UI.
This module is the transport floor — JSON in, JSON out — and nothing more; the
:mod:`anastomosis.deliver.fhir_api.destination` layer builds the resources and
the patient-safety logic on top.

Three properties shape the design, all of them carried over from the browser
route's threat model:

* **Loopback is the only exception to HTTPS.** :class:`FhirEndpoint` refuses a
  plaintext ``http`` base URL unless its host is the local loopback — the exact
  rule and rationale as :mod:`anastomosis.deliver.browser.cdp`: a FHIR base URL
  carries a bearer token and patient identifiers in its requests, so cleartext
  off-loopback would expose them to the network. Rejection is a hard
  ``ValueError``, never a warning.
* **The bearer token never surfaces.** It is held on the endpoint but masked in
  :meth:`FhirEndpoint.__repr__`, so it cannot leak into a log line, an
  exception's ``repr``, or a traceback frame. It is sent only in the
  ``Authorization`` header of a live request.
* **Error messages carry status codes and resource TYPE names only.** A FHIR
  ``OperationOutcome`` body may echo the patient identifier from a failed
  search, and a request URL embeds identifiers in its query string, so neither
  the body nor the URL is ever folded into a raised message — only the numeric
  status and the resource type are, both of which are safe to log.

HTTP-status routing maps onto the existing delivery error taxonomy
(:mod:`anastomosis.deliver.browser.errors`) so the upload engine's retry/abort
machinery drives an API destination unchanged: 401/403/404 and other 4xx are
:class:`PermanentDeliveryError`; 408/429/5xx and any transport-level failure
(timeout, connection refused) are :class:`TransientDeliveryError`.

The single :func:`urllib.request.urlopen` call site is audited: the request URL
is built only from the endpoint's validated base URL plus caller paths, and the
scheme is fixed at construction (the ``S310`` concern), so the ``noqa`` there is
justified. Tests never monkeypatch ``urllib``; the constructor accepts an
``opener`` seam so an in-process fake transport can be injected.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from anastomosis.deliver.browser.errors import (
    PermanentDeliveryError,
    TransientDeliveryError,
)

__all__ = ["FHIR_JSON", "FhirClient", "FhirEndpoint", "FhirResponse", "Opener"]

# The FHIR R4 JSON media type used for both Accept and Content-Type.
FHIR_JSON = "application/fhir+json"

# Statuses the destination should retry (the engine routes these to RETRY_WAIT).
_TRANSIENT_STATUSES = frozenset({408, 429})

# The only hosts that are the local loopback — mirrors the cdp.py rule exactly
# (urlsplit lowercases and unbrackets the host, so the bare forms are compared).
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


@dataclass(frozen=True)
class FhirResponse:
    """The transport-level result of one request: status + parsed body.

    ``location`` is the ``Location`` header verbatim (a server-assigned
    resource URL on a 201 Created); ``body`` is the parsed JSON object, or
    ``None`` when the response carried no body. Neither field is logged.
    """

    status: int
    body: dict[str, Any] | None
    location: str | None = None


# A transport seam: takes (method, url, headers, body bytes) and returns a
# FhirResponse, OR raises urllib.error.HTTPError / URLError exactly as urllib
# does. Tests inject an in-process fake; production uses _UrllibOpener.
Opener = Callable[[str, str, Mapping[str, str], bytes | None], FhirResponse]


@dataclass(frozen=True)
class FhirEndpoint:
    """A validated FHIR R4 base URL, with an optional masked bearer token.

    ``base_url`` must use ``https``, OR ``http`` only when its host is the local
    loopback (the cdp.py rule: a base URL carries a token and patient
    identifiers, so cleartext off-loopback is refused). The trailing slash is
    normalized away so path joins are unambiguous. ``bearer_token`` is held for
    the ``Authorization`` header but never appears in ``repr`` — see the custom
    ``__repr__`` below.
    """

    base_url: str
    bearer_token: str | None = None
    timeout_s: float = 30.0

    def __post_init__(self) -> None:
        parts = urllib.parse.urlsplit(self.base_url)
        if parts.scheme == "https":
            pass
        elif parts.scheme == "http":
            host = parts.hostname  # urlsplit lowercases and unbrackets the host.
            if host is None or host not in _LOOPBACK_HOSTS:
                raise ValueError(
                    "FHIR base_url may use http only for a loopback host "
                    "(127.0.0.1, ::1, or localhost): a base URL carries a bearer "
                    "token and patient identifiers, so cleartext off-loopback is "
                    f"refused; got host {host!r}"
                )
        else:
            raise ValueError(
                f"FHIR base_url scheme must be https (or http for loopback); got {parts.scheme!r}"
            )
        # Normalize the trailing slash away so path joins are unambiguous.
        normalized = self.base_url.rstrip("/")
        if normalized != self.base_url:
            object.__setattr__(self, "base_url", normalized)

    def __repr__(self) -> str:
        # The token must never surface — not in a log line, a traceback frame,
        # or a debugger. Report only whether one is set.
        token = "***" if self.bearer_token else None
        return (
            f"FhirEndpoint(base_url={self.base_url!r}, "
            f"bearer_token={token!r}, timeout_s={self.timeout_s!r})"
        )


class FhirClient:
    """A minimal FHIR R4 REST client: JSON ``get`` and ``post`` over urllib.

    The ``opener`` seam defaults to the audited urllib transport; tests inject
    an in-process fake so no monkeypatching of ``urllib`` is needed. All
    HTTP-error and transport failures are routed to the delivery error taxonomy
    here, so callers see only :class:`PermanentDeliveryError` /
    :class:`TransientDeliveryError`, never a raw urllib exception.
    """

    def __init__(self, endpoint: FhirEndpoint, *, opener: Opener | None = None) -> None:
        self._endpoint = endpoint
        self._opener: Opener = opener if opener is not None else _UrllibOpener(endpoint.timeout_s)

    @property
    def base_url(self) -> str:
        """The validated, slash-normalized base URL (no token, safe to read)."""
        return self._endpoint.base_url

    def get(self, path: str, params: Mapping[str, str] | None = None) -> dict[str, Any]:
        """GET ``path`` (relative to the base URL) and return the parsed JSON body.

        ``params`` are URL-encoded into the query string. A missing/empty body
        on a 2xx is reported as a :class:`PermanentDeliveryError` naming the
        resource type only — a successful GET that returns nothing is a
        malformed server, not a retryable hiccup.
        """
        url = self._build_url(path, params)
        response = self._request("GET", url, path, body=None)
        if response.body is None:
            raise PermanentDeliveryError(f"empty body from GET {_resource_type(path)}")
        return response.body

    def post(self, path: str, resource: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
        """POST ``resource`` to ``path``; return ``(parsed body or None, created id)``.

        The created id is taken from the ``Location`` header when present (the
        FHIR-conformant create response) and otherwise from the body's ``id``.
        ``None`` for the id means the server returned neither — the caller
        decides whether that is fatal for its operation.
        """
        url = self._build_url(path, None)
        payload = json.dumps(resource).encode("utf-8")
        response = self._request("POST", url, path, body=payload)
        created_id = _id_from_location(response.location)
        if created_id is None and response.body is not None:
            raw = response.body.get("id")
            created_id = raw if isinstance(raw, str) else None
        return response.body, created_id

    # --- internals ---

    def _build_url(self, path: str, params: Mapping[str, str] | None) -> str:
        url = f"{self._endpoint.base_url}/{path.lstrip('/')}"
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"
        return url

    def _headers(self, *, with_content_type: bool) -> dict[str, str]:
        headers = {"Accept": FHIR_JSON}
        if with_content_type:
            headers["Content-Type"] = FHIR_JSON
        if self._endpoint.bearer_token:
            headers["Authorization"] = f"Bearer {self._endpoint.bearer_token}"
        return headers

    def _request(self, method: str, url: str, path: str, *, body: bytes | None) -> FhirResponse:
        """Run one request through the opener, routing every failure by status.

        ``path`` is the caller's relative path (``Patient``,
        ``DocumentReference/123``); the resource type is derived from it, never
        from the full URL, so the endpoint's own base path segments cannot be
        mistaken for the resource type.

        PHI rule: a raised message names the HTTP status and the resource TYPE
        only — never the response body (an OperationOutcome may echo a patient
        identifier) and never the URL (its query string carries identifiers).
        """
        headers = self._headers(with_content_type=body is not None)
        resource = _resource_type(path)
        try:
            response = self._opener(method, url, headers, body)
        except urllib.error.HTTPError as exc:
            raise _route_http_status(int(exc.code), resource) from None
        except urllib.error.URLError:
            # Connection refused, DNS failure, timeout — all retryable transport
            # faults. The reason may name a host, so it is not folded in.
            raise TransientDeliveryError(f"transport failure reaching {resource}") from None
        if response.status >= 400:
            raise _route_http_status(response.status, resource) from None
        return response


def _route_http_status(
    status: int, resource: str
) -> PermanentDeliveryError | TransientDeliveryError:
    """Map an HTTP status to a delivery error (message: status + resource type only).

    401/403/404 and any other 4xx -> permanent; 408/429 and any 5xx ->
    transient (the engine retries those). The 401/403/404 split from other 4xx
    is explicit because they are the common "auth/missing" terminal cases.
    """
    if status in _TRANSIENT_STATUSES or 500 <= status < 600:
        return TransientDeliveryError(f"HTTP {status} from {resource}")
    return PermanentDeliveryError(f"HTTP {status} from {resource}")


def _resource_type(path: str) -> str:
    """The FHIR resource type from a relative path, for PHI-safe error messages.

    Returns the first alphabetic path segment (``Patient``,
    ``DocumentReference``, ``metadata``…). The query string is dropped before
    inspection so no identifier rides along. Falls back to ``"resource"`` when
    nothing recognizable is present.
    """
    cleaned = urllib.parse.urlsplit(path).path
    for segment in cleaned.split("/"):
        if segment and segment[:1].isalpha():
            return segment
    return "resource"


def _id_from_location(location: str | None) -> str | None:
    """Parse the resource id from a FHIR ``Location`` header.

    A create returns e.g. ``[base]/Patient/123/_history/1``; the id is the
    segment after the resource type. ``_history`` and version suffixes are
    ignored. Returns ``None`` when no id can be read.
    """
    if not location:
        return None
    segments = [s for s in urllib.parse.urlsplit(location).path.split("/") if s]
    if "_history" in segments:
        segments = segments[: segments.index("_history")]
    # The id is the last segment, with the resource type immediately before it.
    if len(segments) >= 2:
        return segments[-1]
    return None


class _UrllibOpener:
    """The production transport: one audited :func:`urllib.request.urlopen` call.

    Holds the timeout so the seam signature stays ``(method, url, headers,
    body)``. The scheme is fixed by :class:`FhirEndpoint` validation, so the
    ``S310`` concern (an attacker-chosen ``file://`` scheme) cannot arise; the
    ``noqa`` at the call site is justified on that basis.
    """

    def __init__(self, timeout_s: float) -> None:
        self._timeout_s = timeout_s

    def __call__(
        self, method: str, url: str, headers: Mapping[str, str], body: bytes | None
    ) -> FhirResponse:
        # S310: the scheme is fixed to http(s) by FhirEndpoint validation at
        # construction, and ``url`` is the validated base_url + a caller path —
        # never an attacker-chosen file:// or custom scheme. This is the single
        # audited request site, so the suppression is justified.
        request = urllib.request.Request(  # noqa: S310
            url, data=body, headers=dict(headers), method=method
        )
        with urllib.request.urlopen(request, timeout=self._timeout_s) as resp:  # noqa: S310
            raw = resp.read()
            location = resp.headers.get("Location")
            status = int(resp.status)
        parsed = json.loads(raw) if raw else None
        body_obj = parsed if isinstance(parsed, dict) else None
        return FhirResponse(status=status, body=body_obj, location=location)
