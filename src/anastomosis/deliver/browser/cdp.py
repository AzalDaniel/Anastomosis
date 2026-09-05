"""CDP attach configuration: connect to a browser the user already drives.

The user logs into their own already-open browser; this module attaches to
that session over CDP. Loopback only, explicit port, else ``ValueError``
(52). :data:`SHARED_MACHINE_WARNING` is the CLI/GUI's pre-attach text
(`RULES_CANDIDATES.md`). No Playwright import at module load (75).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

__all__ = ["SHARED_MACHINE_WARNING", "CdpEndpoint", "connect_over_cdp"]

_ALLOWED_SCHEMES = frozenset({"http", "https", "ws", "wss"})

# ``::1`` may arrive bracketed (``[::1]``); urlsplit strips brackets, so the
# bare form below is what we compare against.
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})

SHARED_MACHINE_WARNING = (
    "Filing runs through a browser window on this computer, over a local "
    "diagnostic connection. On a computer other people can sign in to, anyone "
    "signed in could reach that connection and act in that browser — "
    "including the EHR you are signed in to there. Anastomosis never stores "
    "your EHR sign-in: you sign in yourself, in that browser, and the "
    "connection ends when the browser closes. Only run this on a computer "
    "you trust, and close the browser when the run is done."
)


@dataclass(frozen=True)
class CdpEndpoint:
    """A validated, loopback-only CDP attach target (52)."""

    url: str

    def __post_init__(self) -> None:
        parts = urlsplit(self.url)

        if parts.scheme not in _ALLOWED_SCHEMES:
            raise ValueError(
                f"CDP endpoint scheme must be one of {sorted(_ALLOWED_SCHEMES)}; "
                f"got {parts.scheme!r}"
            )

        host = parts.hostname  # urlsplit lowercases and unbrackets the host.
        if host is None or host not in _LOOPBACK_HOSTS:
            raise ValueError(
                "CDP endpoint host must be loopback (127.0.0.1, ::1, or localhost): "
                "the debug port grants full control of the browser and its logged-in "
                f"EHR session, so a non-loopback endpoint is refused; got host {host!r}"
            )

        # An explicit port is required: parts.port raises ValueError on a
        # malformed port and is None when no port was given.
        if parts.port is None:
            raise ValueError(
                "CDP endpoint must include an explicit port (e.g. http://127.0.0.1:9222); "
                "the debug port is never assumed"
            )


def connect_over_cdp(endpoint: CdpEndpoint) -> tuple[Any, Any]:  # pragma: no cover
    """Contract: returns ``(playwright, browser)``, both owned for teardown —
    ``browser.close()`` alone disconnects the CDP session, never the driver
    subprocess or the operator's browser; the caller must also call
    ``playwright.stop()``. Imports Playwright lazily (75).
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError(
            "playwright is required for browser delivery — install anastomosis[deliver-browser]"
        ) from exc
    playwright = sync_playwright().start()
    browser = playwright.chromium.connect_over_cdp(endpoint.url)
    return playwright, browser
