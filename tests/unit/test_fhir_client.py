"""FhirClient + FhirEndpoint tests: status routing, id parsing, token masking.

The transport is an in-process fake opener injected at construction — urllib is
never monkeypatched. Synthetic data only: ``example.com`` hosts, ``feedface-``
ids, a never-real bearer token string.

PHI discipline is probed directly: a raised message and the endpoint repr must
never carry the token, the request URL, or a query string.
"""

from __future__ import annotations

import io
import logging
import urllib.error
from collections.abc import Mapping

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
