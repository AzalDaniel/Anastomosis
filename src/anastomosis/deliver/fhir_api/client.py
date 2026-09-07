"""A minimal FHIR R4 REST client over stdlib ``urllib`` — no new dependencies.

The transport floor for PLAN item 13a; :mod:`.destination` builds the
resources on top. Loopback-only http, masked token, PHI-safe errors
(RULES.md 41); status routes to the delivery taxonomy, every redirect
refused including same-origin (RULES.md 42) — :class:`_UrllibOpener`'s own
opener never re-offers ``Authorization`` to a server-named URL.

Tests inject an ``opener`` seam; ``urllib`` itself is never monkeypatched.
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
    """A redirect was refused (RULES.md 42): permanent, since retrying cannot
    fix a misconfigured base URL. The message names the numeric status only —
    never the server-controlled target, which may carry identifiers.
    """


@dataclass(frozen=True)
class FhirResponse:
    """One request's result: status, parsed JSON ``body`` (or ``None``), and
    ``location`` — the ``Location`` header verbatim. Neither field is logged.
    """

    status: int
    body: dict[str, Any] | None
    location: str | None = None


# (method, url, headers, body) -> FhirResponse, or raises HTTPError/URLError
# exactly as urllib does. Tests inject a fake; production uses _UrllibOpener.
Opener = Callable[[str, str, Mapping[str, str], bytes | None], FhirResponse]


@dataclass(frozen=True)
class FhirEndpoint:
    """A validated FHIR base URL with an optional masked bearer token
    (RULES.md 41). ``base_url`` loses its trailing slash so path joins stay
    unambiguous.
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
    """FHIR R4 ``get``/``post`` over urllib, routed to the delivery error
    taxonomy (RULES.md 42): callers see only :class:`PermanentDeliveryError` /
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
        """GET ``path``; return the parsed JSON body. Raises
        :class:`PermanentDeliveryError` (resource type only) on an empty
        2xx body — a malformed server, not a retryable hiccup.
        """
        url = self._build_url(path, params)
        response = self._request("GET", url, path, body=None)
        if response.body is None:
            raise PermanentDeliveryError(f"empty body from GET {_resource_type(path)}")
        return response.body

    def post(self, path: str, resource: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
        """POST ``resource`` to ``path``; return ``(body or None, created id)``.
        The id comes from the ``Location`` header, else the body's ``id``,
        else ``None`` — the caller decides whether a missing id is fatal.
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
        # no-cache: HAPI caches identical search URLs ~1min, and a stale empty
        # search right after a Patient create would file under a duplicate.
        headers = {"Accept": FHIR_JSON, "Cache-Control": "no-cache"}
        if with_content_type:
            headers["Content-Type"] = FHIR_JSON
        if self._endpoint.bearer_token:
            headers["Authorization"] = f"Bearer {self._endpoint.bearer_token}"
        return headers

    def _request(self, method: str, url: str, path: str, *, body: bytes | None) -> FhirResponse:
        """Run one request; failures route to the RULES.md 42 taxonomy. The
        resource type comes from ``path``, not the full URL, so the
        endpoint's own base segments are never mistaken for it.
        """
        headers = self._headers(with_content_type=body is not None)
        resource = _resource_type(path)
        try:
            response = self._opener(method, url, headers, body)
        except RedirectRefusedError:
            # Must reach the caller as itself: routing through
            # _route_http_status would relabel it retryable.
            raise
        except urllib.error.HTTPError as exc:
            raise _route_http_status(int(exc.code), resource) from None
        except urllib.error.URLError as exc:
            # urllib wraps handler-raised exceptions in URLError; unwrap first.
            if isinstance(exc.reason, RedirectRefusedError):
                raise exc.reason from None
            # Retryable transport fault; the reason may name a host.
            raise TransientDeliveryError(f"transport failure reaching {resource}") from None
        if response.status >= 400:
            raise _route_http_status(response.status, resource) from None
        return response


def _route_http_status(
    status: int, resource: str
) -> PermanentDeliveryError | TransientDeliveryError:
    """HTTP status -> delivery error (RULES.md 42); message names status and
    resource type only.
    """
    if status in _TRANSIENT_STATUSES or 500 <= status < 600:
        return TransientDeliveryError(f"HTTP {status} from {resource}")
    return PermanentDeliveryError(f"HTTP {status} from {resource}")


def _resource_type(path: str) -> str:
    """The first alphabetic path segment (``Patient``, ``DocumentReference``),
    query string dropped so no identifier rides along; ``"resource"`` if none.
    """
    cleaned = urllib.parse.urlsplit(path).path
    for segment in cleaned.split("/"):
        if segment and segment[:1].isalpha():
            return segment
    return "resource"


def _id_from_location(location: str | None) -> str | None:
    """The id from a ``Location`` header (``.../Patient/123/_history/1`` ->
    ``"123"``); ``_history``/version suffixes ignored, ``None`` if unparseable.
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
    """Refuses every redirect (RULES.md 42): ``redirect_request`` fires before
    urllib copies headers onto the new request, so ``Authorization`` is never
    sent to a server-named URL.
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
        # newurl/msg are server-controlled and may carry identifiers; only
        # the numeric status is named.
        raise RedirectRefusedError(
            f"HTTP {code} redirect refused: an authorized request must not "
            "follow a redirect, because the Authorization header would be "
            "re-sent to a destination chosen by the server. Configure the FHIR "
            "base URL to point at the final destination."
        )


class _UrllibOpener:
    """The production transport: one audited urllib request, redirects
    refused. Built once, with :class:`_RefuseRedirectHandler` the only
    redirect handler in the opener's chain.
    """

    def __init__(self, timeout_s: float) -> None:
        self._timeout_s = timeout_s
        # An HTTPRedirectHandler instance makes build_opener skip its own
        # default, so every 30x lands in _RefuseRedirectHandler instead.
        self._opener = urllib.request.build_opener(_RefuseRedirectHandler())

    def __call__(
        self, method: str, url: str, headers: Mapping[str, str], body: bytes | None
    ) -> FhirResponse:
        # S310: scheme is fixed by FhirEndpoint validation; url is base_url
        # plus a caller path, never attacker-chosen. Single audited site.
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
