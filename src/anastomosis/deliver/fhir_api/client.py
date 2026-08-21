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

The single request call site is audited: the request URL is built only from the
endpoint's validated base URL plus caller paths, and the scheme is fixed at
construction (the ``S310`` concern), so the ``noqa`` there is justified. That
audit only holds if the request stays at the audited URL, so
:class:`_UrllibOpener` builds its own opener with a redirect handler that
**refuses every redirect** rather than using urllib's default: urllib's default
opener follows a 30x and re-attaches the caller's headers — the
``Authorization`` bearer among them — to a target named by the server, which
may be an origin the operator never configured. A validated FHIR base URL has
no business redirecting; when one does, the refusal is loud
(:class:`RedirectRefusedError`) and the operator's fix is to configure the final
URL. Same-origin redirects are refused too — narrowing the rule to
cross-origin would mean trusting a parse of server-controlled text.

Tests never monkeypatch ``urllib``; the constructor accepts an ``opener`` seam
so an in-process fake transport can be injected.
"""

from __future__ import annotations

import http.client
import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import IO, Any, NoReturn

from anastomosis.deliver.browser.errors import (
    PermanentDeliveryError,
    TransientDeliveryError,
)

__all__ = [
    "FHIR_JSON",
    "FhirClient",
    "FhirEndpoint",
    "FhirResponse",
    "Opener",
    "RedirectRefusedError",
]

# The FHIR R4 JSON media type used for both Accept and Content-Type.
FHIR_JSON = "application/fhir+json"

# Statuses the destination should retry (the engine routes these to RETRY_WAIT).
_TRANSIENT_STATUSES = frozenset({408, 429})

# The only hosts that are the local loopback — mirrors the cdp.py rule exactly
# (urlsplit lowercases and unbrackets the host, so the bare forms are compared).
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


class RedirectRefusedError(PermanentDeliveryError):
    """The server answered with a redirect and the client refused to follow it.

    An authorized request must not follow a redirect. urllib's default opener
    re-attaches the original request's headers — including the
    ``Authorization`` bearer — to the redirect target, and that target is
    server-controlled text that may name an origin the operator never
    configured. :class:`FhirEndpoint` validates exactly one base URL, and that
    validation is worth nothing if a 30x can move the audience of a
    token-bearing request. So every redirect is refused, same-origin ones
    included: a validated base URL that redirects is a misconfiguration, and
    guessing which redirects are "safe" would mean trusting the very text the
    server controls.

    Permanent, not transient: retrying cannot fix it. The operator's fix is to
    configure the FHIR base URL to point at the final destination.

    PHI rule: the message names the numeric status ONLY. The redirect target is
    never folded in — it is server-controlled, and a FHIR URL's query string
    carries patient identifiers.
    """


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
        # Cache-Control: no-cache — HAPI (and other servers) reuse cached
        # results for identical search URLs for up to a minute. The resolver's
        # read-after-write semantics REQUIRE fresh reads: a stale empty search
        # right after a Patient create cascades into one duplicate patient per
        # resolve, with the chart filed under the last duplicate.
        headers = {"Accept": FHIR_JSON, "Cache-Control": "no-cache"}
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
        except RedirectRefusedError:
            # A refused redirect is already a PermanentDeliveryError carrying a
            # PHI-safe message, and it must reach the caller AS ITSELF: rerouting
            # it through _route_http_status would relabel a 307 as retryable and
            # the engine would re-offer the token to the same redirect.
            raise
        except urllib.error.HTTPError as exc:
            raise _route_http_status(int(exc.code), resource) from None
        except urllib.error.URLError as exc:
            # urllib re-raises some handler-raised exceptions wrapped in a
            # URLError (its ``reason``), so unwrap before the transient default —
            # a refused redirect must never be reported as a retryable hiccup.
            if isinstance(exc.reason, RedirectRefusedError):
                raise exc.reason from None
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


class _RefuseRedirectHandler(urllib.request.HTTPRedirectHandler):
    """A redirect handler that refuses every redirect instead of following one.

    urllib routes each of 301/302/303/307/308 through ``redirect_request``
    before it re-issues anything, so overriding that one method covers the
    whole family — and it fires BEFORE the base class copies the request
    headers onto the new request, so the ``Authorization`` bearer is never
    built into a request aimed at a server-named URL.

    Handing an *instance* of this subclass to
    :func:`urllib.request.build_opener` makes it drop its own default
    :class:`urllib.request.HTTPRedirectHandler`, so this is the only redirect
    handler in the chain. Every other default handler (proxy, http, https,
    error processing) is left exactly as ``urlopen`` would have it.
    """

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: IO[bytes],
        code: int,
        msg: str,
        headers: http.client.HTTPMessage,
        newurl: str,
    ) -> NoReturn:
        # PHI + tainted-input rule: ``newurl`` (the Location header) and ``msg``
        # are server-controlled and a FHIR URL's query string carries patient
        # identifiers, so neither is named. The numeric status is safe.
        raise RedirectRefusedError(
            f"HTTP {code} redirect refused: an authorized request must not "
            "follow a redirect, because the Authorization header would be "
            "re-sent to a destination chosen by the server. Configure the FHIR "
            "base URL to point at the final destination."
        )


class _UrllibOpener:
    """The production transport: one audited urllib request, redirects refused.

    Holds the timeout so the seam signature stays ``(method, url, headers,
    body)``. The scheme is fixed by :class:`FhirEndpoint` validation, so the
    ``S310`` concern (an attacker-chosen ``file://`` scheme) cannot arise; the
    ``noqa`` at the call site is justified on that basis. The opener is built
    once, in ``__init__``, with :class:`_RefuseRedirectHandler` so that audited
    URL is also the *only* URL the token is ever offered to.
    """

    def __init__(self, timeout_s: float) -> None:
        self._timeout_s = timeout_s
        # Built once and reused. Supplying an instance of an
        # HTTPRedirectHandler subclass makes build_opener skip its own default
        # redirect handler, so every 30x lands in _RefuseRedirectHandler and
        # raises RedirectRefusedError instead of re-sending the bearer token.
        self._opener = urllib.request.build_opener(_RefuseRedirectHandler())

    def __call__(
        self, method: str, url: str, headers: Mapping[str, str], body: bytes | None
    ) -> FhirResponse:
        # S310: the scheme is fixed to http(s) by FhirEndpoint validation at
        # construction, and ``url`` is the validated base_url + a caller path —
        # never an attacker-chosen file:// or custom scheme. This is the single
        # audited request site, so the suppression is justified. The opener
        # refuses redirects, so the request cannot be walked off that URL.
        request = urllib.request.Request(  # noqa: S310
            url, data=body, headers=dict(headers), method=method
        )
        with self._opener.open(request, timeout=self._timeout_s) as resp:
            raw = resp.read()
            location = resp.headers.get("Location")
            status = int(resp.status)
        parsed = json.loads(raw) if raw else None
        body_obj = parsed if isinstance(parsed, dict) else None
        return FhirResponse(status=status, body=body_obj, location=location)
