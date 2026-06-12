"""CDP attach config tests: loopback-only validation, warning, lazy import.

These tests MUST NOT require Playwright (the CI test lane does not install the
``deliver-browser`` extra). Validation and warning text are pure; the
missing-dependency error path is exercised by poisoning ``sys.modules`` so the
lazy import inside :func:`connect_over_cdp` raises ``ImportError``.
"""

from __future__ import annotations

import sys

import pytest

from anastomosis.deliver.browser.cdp import (
    SHARED_MACHINE_WARNING,
    CdpEndpoint,
    connect_over_cdp,
)

# --- accepted loopback endpoints ---


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:9222",
        "http://localhost:9222",
        "http://[::1]:9222",
        "https://127.0.0.1:9222",
        "ws://localhost:9222",
        "wss://[::1]:9222",
    ],
)
def test_accepts_loopback_with_explicit_port(url: str) -> None:
    endpoint = CdpEndpoint(url)
    assert endpoint.url == url


# --- rejected endpoints ---


@pytest.mark.parametrize(
    "url",
    [
        "http://0.0.0.0:9222",
        "http://192.168.1.5:9222",
        "http://evil.example:9222",
        "http://10.0.0.1:9222",
        "http://[2001:db8::1]:9222",
    ],
)
def test_rejects_non_loopback_host(url: str) -> None:
    with pytest.raises(ValueError, match="loopback"):
        CdpEndpoint(url)


def test_rejects_missing_port() -> None:
    with pytest.raises(ValueError, match="port"):
        CdpEndpoint("http://127.0.0.1")


def test_rejects_bad_scheme() -> None:
    with pytest.raises(ValueError, match="scheme"):
        CdpEndpoint("ftp://127.0.0.1:9222")


def test_rejects_no_scheme() -> None:
    # Without a scheme urlsplit puts everything in the path; scheme is empty.
    with pytest.raises(ValueError, match="scheme"):
        CdpEndpoint("127.0.0.1:9222")


# --- shared-machine warning text ---


def test_shared_machine_warning_mentions_multiuser_and_no_credentials() -> None:
    text = SHARED_MACHINE_WARNING.lower()
    assert "multi-user" in text
    # Never stores credentials — the central promise of the browser route.
    assert "credential" in text
    assert "never" in text


# --- lazy-import error path (no playwright installed) ---


def test_connect_over_cdp_raises_naming_extra_when_playwright_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Poison the import so the lazy `from playwright.sync_api import ...` fails,
    # regardless of whether playwright happens to be installed.
    monkeypatch.setitem(sys.modules, "playwright", None)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", None)

    endpoint = CdpEndpoint("http://127.0.0.1:9222")
    with pytest.raises(RuntimeError, match=r"anastomosis\[deliver-browser\]"):
        connect_over_cdp(endpoint)
