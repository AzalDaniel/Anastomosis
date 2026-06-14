"""CDP attach configuration: connect to a browser the user already drives.

The browser route's threat model is unusual and deliberate. Anastomosis NEVER
stores EHR credentials and never logs the user into the destination itself.
Instead the user opens their EHR in a Chromium they launched with a remote
debugging port, logs in by hand, and Anastomosis attaches to that already
authenticated session over the Chrome DevTools Protocol (CDP). The attachment
lives only as long as the browser the user controls.

That debug port is full remote control of the browser — and therefore of the
logged-in EHR session behind it. Two guards make the attach safe:

* **Loopback only.** :class:`CdpEndpoint` refuses any host that is not the
  local loopback. A debug port bound to a routable address would expose the
  user's authenticated EHR session to anyone on the network; rejecting it is a
  hard ``ValueError``, never a warning.
* **Shared-machine warning.** Even on loopback, any *local* user on a
  multi-user machine can reach the port. :data:`SHARED_MACHINE_WARNING` is the
  exact text the CLI and GUI must surface before attaching, so the user
  understands the boundary they are accepting.

No Playwright import lives at module load: this module is importable (and its
validation testable) on a machine without the ``deliver-browser`` extra.
:func:`connect_over_cdp` imports it lazily and is intentionally kept thin so
the testable surface (validation, warning text, the missing-dependency error)
needs no browser.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

__all__ = ["SHARED_MACHINE_WARNING", "CdpEndpoint", "connect_over_cdp"]

# Schemes a CDP/DevTools endpoint may legitimately use.
_ALLOWED_SCHEMES = frozenset({"http", "https", "ws", "wss"})

# The only hosts that are the local loopback. ``::1`` may arrive bracketed
# (``[::1]``) inside a URL authority; urlsplit strips the brackets for us, so
# the bare form is what we compare against.
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})

SHARED_MACHINE_WARNING = (
    "SECURITY: attaching to a browser debug port. On a multi-user machine, "
    "ANY local user can reach this port and drive the attached browser — "
    "including the EHR session you are logged into. Anastomosis NEVER stores "
    "your EHR credentials: you log in yourself in the browser you launched, "
    "and the attachment ends when that browser closes. Only attach on a "
    "machine you trust, and close the browser when the run is done."
)


@dataclass(frozen=True)
class CdpEndpoint:
    """A validated, loopback-only CDP attach target.

    ``url`` must use an http/https/ws/wss scheme, name a loopback host
    (``127.0.0.1``, ``::1`` with or without brackets, or ``localhost``), and
    carry an explicit port (the debug port is never assumed). Any other host
    is a :class:`ValueError` naming the loopback rule — exposing a debug port
    off-loopback would hand the network the user's logged-in EHR session.
    """

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


def connect_over_cdp(endpoint: CdpEndpoint) -> Any:  # pragma: no cover - needs playwright
    """Attach to the browser at ``endpoint`` over CDP and return the browser.

    Playwright is imported lazily here so the module (and its validation) work
    without the ``deliver-browser`` extra; a missing install raises a
    ``RuntimeError`` naming the extra, matching the optional-dependency error
    style used elsewhere (see :mod:`anastomosis.reconstruct.chromium`). Kept
    deliberately thin — all testable logic lives in :class:`CdpEndpoint` and
    :data:`SHARED_MACHINE_WARNING`, not here.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError(
            "playwright is required for browser delivery — install anastomosis[deliver-browser]"
        ) from exc
    playwright = sync_playwright().start()
    return playwright.chromium.connect_over_cdp(endpoint.url)
