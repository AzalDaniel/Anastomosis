"""FhirClient + FhirEndpoint tests: status routing, id parsing, token masking.

The transport is an in-process fake opener injected at construction — urllib is
never monkeypatched. Synthetic data only: ``example.com`` hosts, ``feedface-``
ids, a never-real bearer token string.

PHI discipline is probed directly: a raised message and the endpoint repr must
never carry the token, the request URL, or a query string.

The redirect-refusal tests at the bottom are the one exception to the fake
transport: they drive the PRODUCTION urllib opener against real loopback
``http.server`` instances, because the property under test (a bearer token
never reaches a second origin) is only meaningful against real urllib.
"""

from __future__ import annotations

import contextlib
import http.server
import io
import json
import logging
import threading
import urllib.error
from collections.abc import Iterator, Mapping

import pytest

from anastomosis.deliver.browser.errors import (
    PermanentDeliveryError,
    TransientDeliveryError,
)
from anastomosis.deliver.fhir_api.client import (
    FHIR_JSON,
    FhirClient,
    FhirEndpoint,
    FhirResponse,
    RedirectRefusedError,
)

TOKEN = "feedface-token-never-real-0000"  # synthetic test token, never real


# --- fake transport -----------------------------------------------------------


class _RecordingOpener:
    """Records the last request and returns a scripted FhirResponse."""

    def __init__(self, response: FhirResponse) -> None:
        self._response = response
        self.calls: list[tuple[str, str, dict[str, str], bytes | None]] = []

    def __call__(
        self, method: str, url: str, headers: Mapping[str, str], body: bytes | None
    ) -> FhirResponse:
        self.calls.append((method, url, dict(headers), body))
        return self._response


class _RaisingOpener:
    """Raises a scripted exception, standing in for a urllib transport failure."""

    def __init__(self, exc: BaseException) -> None:
        self._exc = exc

    def __call__(
        self, method: str, url: str, headers: Mapping[str, str], body: bytes | None
    ) -> FhirResponse:
        raise self._exc


def _http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        url="http://127.0.0.1/fhir/Patient", code=code, msg="x", hdrs=None, fp=io.BytesIO(b"")
    )


def _client(opener: object, *, token: str | None = None) -> FhirClient:
    endpoint = FhirEndpoint("https://fhir.example.com/r4", bearer_token=token)
    return FhirClient(endpoint, opener=opener)  # type: ignore[arg-type]


# --- endpoint validation: scheme + loopback rule ------------------------------


def test_https_base_url_accepted() -> None:
    assert FhirEndpoint("https://fhir.example.com/r4").base_url == "https://fhir.example.com/r4"


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "[::1]"])
def test_loopback_http_accepted(host: str) -> None:
    endpoint = FhirEndpoint(f"http://{host}:8080/fhir")
    assert endpoint.base_url == f"http://{host}:8080/fhir"


@pytest.mark.parametrize("url", ["http://fhir.example.com/r4", "http://10.0.0.5:8080/fhir"])
def test_non_loopback_http_rejected(url: str) -> None:
    with pytest.raises(ValueError, match="loopback"):
        FhirEndpoint(url)


def test_non_http_scheme_rejected() -> None:
    with pytest.raises(ValueError, match="scheme"):
        FhirEndpoint("ftp://fhir.example.com/r4")


def test_trailing_slash_normalized() -> None:
    assert FhirEndpoint("https://fhir.example.com/r4/").base_url == "https://fhir.example.com/r4"


# --- token masking ------------------------------------------------------------


def test_token_absent_from_repr_and_str() -> None:
    endpoint = FhirEndpoint("https://fhir.example.com/r4", bearer_token=TOKEN)
    assert TOKEN not in repr(endpoint)
    assert TOKEN not in str(endpoint)
    assert "***" in repr(endpoint)


def test_token_absent_from_exception_and_log(caplog: pytest.LogCaptureFixture) -> None:
    client = _client(_RaisingOpener(_http_error(401)), token=TOKEN)
    with caplog.at_level(logging.DEBUG), pytest.raises(PermanentDeliveryError) as exc:
        client.get("Patient", {"identifier": "urn:anastomosis:id:mrn|abc"})
    assert TOKEN not in str(exc.value)
    assert TOKEN not in caplog.text


def test_token_sent_only_in_authorization_header() -> None:
    opener = _RecordingOpener(FhirResponse(status=200, body={"resourceType": "Bundle"}))
    client = _client(opener, token=TOKEN)
    client.get("Patient")
    _method, _url, headers, _body = opener.calls[-1]
    assert headers["Authorization"] == f"Bearer {TOKEN}"
    assert headers["Accept"] == FHIR_JSON


def test_requests_bypass_server_search_caches() -> None:
    # HAPI reuses cached results for identical search URLs (~60s); a stale
    # empty search right after a create cascades into duplicate patients.
    # Every request must carry Cache-Control: no-cache (read-after-write).
    opener = _RecordingOpener(FhirResponse(status=200, body={"resourceType": "Bundle"}))
    client = _client(opener)
    client.get("Patient", params={"identifier": "sys|val"})
    _method, _url, headers, _body = opener.calls[-1]
    assert headers["Cache-Control"] == "no-cache"


# --- HTTP status -> error routing matrix --------------------------------------


@pytest.mark.parametrize("code", [401, 403, 404, 400, 405, 409, 422])
def test_permanent_statuses_raise_permanent(code: int) -> None:
    client = _client(_RaisingOpener(_http_error(code)))
    with pytest.raises(PermanentDeliveryError) as exc:
        client.get("Patient")
    assert str(code) in str(exc.value)


@pytest.mark.parametrize("code", [408, 429, 500, 502, 503, 504])
def test_transient_statuses_raise_transient(code: int) -> None:
    client = _client(_RaisingOpener(_http_error(code)))
    with pytest.raises(TransientDeliveryError) as exc:
        client.get("Patient")
    assert str(code) in str(exc.value)


def test_urlerror_routes_transient() -> None:
    client = _client(_RaisingOpener(urllib.error.URLError("connection refused")))
    with pytest.raises(TransientDeliveryError):
        client.get("Patient")


def test_response_status_4xx_without_httperror_still_routes() -> None:
    # An opener that returns a >=400 status object (rather than raising) is
    # still routed by the client's own status check.
    client = _client(_RecordingOpener(FhirResponse(status=404, body=None)))
    with pytest.raises(PermanentDeliveryError):
        client.get("Patient")


# --- 201 + Location id parsing ------------------------------------------------


def test_post_parses_id_from_location_header() -> None:
    opener = _RecordingOpener(
        FhirResponse(
            status=201,
            body=None,
            location="https://fhir.example.com/r4/Patient/123/_history/1",
        )
    )
    client = _client(opener)
    body, created_id = client.post("Patient", {"resourceType": "Patient"})
    assert created_id == "123"
    assert body is None


def test_post_falls_back_to_body_id_without_location() -> None:
    opener = _RecordingOpener(
        FhirResponse(status=201, body={"resourceType": "Patient", "id": "abc-9"})
    )
    client = _client(opener)
    _body, created_id = client.post("Patient", {"resourceType": "Patient"})
    assert created_id == "abc-9"


def test_post_sends_content_type_and_payload() -> None:
    opener = _RecordingOpener(FhirResponse(status=201, body={"id": "x"}))
    client = _client(opener)
    client.post("DocumentReference", {"resourceType": "DocumentReference"})
    method, _url, headers, body = opener.calls[-1]
    assert method == "POST"
    assert headers["Content-Type"] == FHIR_JSON
    assert body is not None and b"DocumentReference" in body


# --- no URL / query text in raised messages -----------------------------------


def test_no_url_or_query_text_in_raised_message() -> None:
    client = _client(_RaisingOpener(_http_error(403)))
    with pytest.raises(PermanentDeliveryError) as exc:
        client.get("Patient", {"identifier": "urn:anastomosis:id:mrn|secret-mrn-value"})
    message = str(exc.value)
    # The query string (carrying a patient identifier) never rides the message.
    assert "secret-mrn-value" not in message
    assert "identifier" not in message
    assert "fhir.example.com" not in message
    assert "?" not in message
    # Only the status code and the resource type are present.
    assert "403" in message and "Patient" in message


def test_get_empty_body_is_permanent() -> None:
    client = _client(_RecordingOpener(FhirResponse(status=200, body=None)))
    with pytest.raises(PermanentDeliveryError):
        client.get("Patient")


# --- redirect refusal, against the PRODUCTION urllib opener -------------------
#
# These are the only tests here that do NOT inject a fake transport: the
# property under test is a property of real urllib. urllib's default opener
# follows a 30x and re-attaches the original headers — the Authorization bearer
# among them — to a target the SERVER named, so the endpoint's validated origin
# stops bounding who sees the token. Two loopback http.server instances stand in
# for the configured endpoint (A) and the redirect target (B); B records every
# request it receives, so "the token did not move" is asserted as a fact about
# the second origin rather than inferred from an error message.
#
# An https -> http downgrade needs no test of its own: refusal happens before
# the handler looks at the target at all, so there is no scheme-, host-, or
# port-dependent branch a downgrade could slip through. Standing up TLS would
# exercise nothing the refuse-all path does not already cover.

# Synthetic, never-real bearer used for the live-loopback tests.
LOOPBACK_TOKEN = "test-token-not-real"

# What each recording server captures: (method, headers).
Received = list[tuple[str, dict[str, str]]]


@contextlib.contextmanager
def _running(handler_cls: type[http.server.BaseHTTPRequestHandler]) -> Iterator[str]:
    """Serve ``handler_cls`` on a fresh 127.0.0.1 port; yield its origin URL."""
    server = http.server.HTTPServer(("127.0.0.1", 0), handler_cls)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _recording_handler(received: Received) -> type[http.server.BaseHTTPRequestHandler]:
    """A handler that appends every request to ``received`` and answers 200."""

    class Handler(http.server.BaseHTTPRequestHandler):
        def _handle(self) -> None:
            received.append((self.command, dict(self.headers.items())))
            payload = json.dumps({"resourceType": "Bundle"}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", FHIR_JSON)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        do_GET = _handle
        do_POST = _handle

        def log_message(self, format: str, *args: object) -> None:
            pass  # keep pytest output clean

    return Handler


def _redirecting_handler(status: int, location: str) -> type[http.server.BaseHTTPRequestHandler]:
    """A handler that answers every request with ``status`` + ``Location``."""

    class Handler(http.server.BaseHTTPRequestHandler):
        def _handle(self) -> None:
            self.send_response(status)
            self.send_header("Location", location)
            self.send_header("Content-Length", "0")
            self.end_headers()

        do_GET = _handle
        do_POST = _handle

        def log_message(self, format: str, *args: object) -> None:
            pass

    return Handler


def _live_client(base_url: str) -> FhirClient:
    """A real FhirClient (production opener) pointed at a loopback origin."""
    # http is allowed here only because the host is loopback — the same
    # exception FhirEndpoint enforces, so no control is relaxed for the test.
    return FhirClient(FhirEndpoint(f"{base_url}/fhir", bearer_token=LOOPBACK_TOKEN, timeout_s=5.0))


def _assert_phi_safe_refusal(exc: RedirectRefusedError, status: int, target: str) -> None:
    """The refusal names the status only — never a URL, a host, or the token."""
    message = str(exc)
    assert str(status) in message
    assert LOOPBACK_TOKEN not in message
    assert "http://" not in message
    assert "127.0.0.1" not in message
    assert target not in message


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
@pytest.mark.parametrize("method", ["GET", "POST"])
def test_cross_origin_redirect_refused_and_target_never_contacted(status: int, method: str) -> None:
    # Server A (the configured endpoint) redirects to server B on another port.
    # The bearer must not follow: B must record ZERO requests.
    received: Received = []
    with _running(_recording_handler(received)) as target_origin:
        target = f"{target_origin}/fhir/Patient"
        with _running(_redirecting_handler(status, target)) as endpoint_origin:
            client = _live_client(endpoint_origin)
            with pytest.raises(RedirectRefusedError) as exc:
                if method == "GET":
                    client.get("Patient")
                else:
                    client.post("Patient", {"resourceType": "Patient"})
            _assert_phi_safe_refusal(exc.value, status, target)
    # The whole point: the second origin was never asked for anything, so it
    # never saw the Authorization header.
    assert received == []


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_refused_redirect_is_permanent_not_transient(status: int) -> None:
    # RedirectRefusedError must reach the caller as ITSELF, not re-routed into
    # the transport-failure branch: the engine would otherwise retry and
    # re-offer the token to the same redirect.
    received: Received = []
    with _running(_recording_handler(received)) as target_origin:
        with _running(_redirecting_handler(status, f"{target_origin}/fhir")) as endpoint:
            client = _live_client(endpoint)
            with pytest.raises(RedirectRefusedError) as exc:
                client.get("Patient")
    assert isinstance(exc.value, PermanentDeliveryError)
    assert not isinstance(exc.value, TransientDeliveryError)
    assert received == []


def test_same_origin_redirect_is_also_refused() -> None:
    """A same-origin redirect is refused too, by design.

    A validated FHIR base URL that redirects is a misconfiguration, and the
    only way to call one redirect "safe" is to parse the target — which is
    server-controlled text. Refusing every redirect is the loud, safe default;
    the operator's fix is to configure the final URL.
    """
    holder: list[str] = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # BaseHTTPRequestHandler's dispatch name
            self.send_response(302)
            self.send_header("Location", f"{holder[0]}/fhir/Patient")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, format: str, *args: object) -> None:
            pass

    with _running(Handler) as origin:
        holder.append(origin)  # Location points back at this same origin.
        client = _live_client(origin)
        with pytest.raises(RedirectRefusedError) as exc:
            client.get("Patient")
    assert "302" in str(exc.value)


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_redirect_without_a_location_header_is_a_clean_permanent_error(status: int) -> None:
    """A 3xx carrying NO Location must surface as a clean delivery error.

    urllib routes a 30x through the redirect handler only when there is a
    target to redirect TO; with no ``Location`` (a malformed server, or one
    stripping the header) the redirect handlers decline and urllib's default
    error path raises ``HTTPError``. That must land in the delivery taxonomy as
    a PERMANENT error naming the status and the resource type — not as a raw
    urllib exception escaping the client, and not as a transient the engine
    would retry forever against a server that cannot answer.
    """

    class Handler(http.server.BaseHTTPRequestHandler):
        def _handle(self) -> None:
            self.send_response(status)  # deliberately no Location header
            self.send_header("Content-Length", "0")
            self.end_headers()

        do_GET = _handle
        do_POST = _handle

        def log_message(self, format: str, *args: object) -> None:
            pass

    with _running(Handler) as origin:
        client = _live_client(origin)
        with pytest.raises(PermanentDeliveryError) as exc:
            client.get("Patient")

    error = exc.value
    assert not isinstance(error, TransientDeliveryError)
    assert not isinstance(error, RedirectRefusedError)
    # PHI + transport discipline: status + resource TYPE only.
    message = str(error)
    assert f"HTTP {status}" in message and "Patient" in message
    assert LOOPBACK_TOKEN not in message
    assert "127.0.0.1" not in message


def test_production_opener_still_works_without_a_redirect() -> None:
    # Control: refusing redirects must not break the ordinary path. A plain 200
    # through the production opener parses normally and carries the bearer.
    received: Received = []
    with _running(_recording_handler(received)) as origin:
        client = _live_client(origin)
        body = client.get("Patient", {"identifier": "sys|feedface-0001"})
    assert body == {"resourceType": "Bundle"}
    assert len(received) == 1
    method, headers = received[0]
    assert method == "GET"
    assert headers["Authorization"] == f"Bearer {LOOPBACK_TOKEN}"
    assert headers["Accept"] == FHIR_JSON
