"""Live-browser destination attach (the Playwright seam).

Single owner of the CDP-attach flow: takes a loopback CDP URL and a loaded
:class:`~anastomosis.destinations.loader.LoadedBrowserPack` and returns a
:class:`BrowserPackDestination`. Lives here, not in ``cli.py``, so the GUI
needs no CLI-private import; tests monkeypatch this function whole for a
browserless :class:`FakeDestination` (`test_import_boundaries.py`).
"""

from __future__ import annotations

__all__ = ["attach_destination"]


def attach_destination(cdp_url: str, loaded: object) -> object:
    """Contract: attach to the operator's open, logged-in context/page over
    CDP and return a :class:`BrowserPackDestination` whose ``close()``
    releases only our owned resources, never that page. Does not itself
    validate the CDP endpoint (the caller already did).
    """
    from anastomosis.deliver.browser.cdp import CdpEndpoint, connect_over_cdp
    from anastomosis.destinations.browserpack import (
        BrowserPackDestination,
        PlaywrightPageAdapter,
    )
    from anastomosis.destinations.loader import LoadedBrowserPack

    assert isinstance(loaded, LoadedBrowserPack)
    playwright, browser = connect_over_cdp(CdpEndpoint(cdp_url))
    # browser.close() alone does not close a connect_over_cdp browser;
    # playwright.stop() (below, in that order) is what ends the subprocess.
    page = browser.contexts[0].pages[0]

    def _teardown() -> None:
        browser.close()
        playwright.stop()

    return BrowserPackDestination(
        loaded.require_selectors(),
        PlaywrightPageAdapter(page),
        loaded.config,
        teardown=_teardown,
    )
