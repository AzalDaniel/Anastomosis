"""Live-browser destination attach (the Playwright seam).

Single owner of the CDP-attach flow: takes a loopback CDP URL and a loaded
:class:`~anastomosis.destinations.loader.LoadedBrowserPack`, connects over
CDP (the loopback gate was validated by the caller), drives the operator's
existing context+page through :class:`PlaywrightPageAdapter`, and wraps it
into a :class:`BrowserPackDestination`. Both the CLI's ``anast upload`` and
the GUI's upload-console depend on this; the function lives here — not in
``cli.py`` — so the GUI never has to import a CLI-private helper.

Tests monkeypatch :func:`attach_destination` whole so the upload flow drives
a :class:`FakeDestination` with no browser; the import boundary is enforced
by ``tests/unit/test_import_boundaries.py``.
"""

from __future__ import annotations

__all__ = ["attach_destination"]


def attach_destination(cdp_url: str, loaded: object) -> object:
    """Connect over CDP and return a live :class:`BrowserPackDestination`.

    The operator has their EHR open in a Chromium they launched and logged
    into; we attach to that context, drive its first page, and hand back a
    destination whose ``close()`` releases ONLY our owned resources — the
    operator's browser stays running. CDP is loopback-only, already
    validated by the caller; this function does not re-check it.

    Imports are lazy (``deliver-browser`` is an optional extra), so the
    plain CLI/GUI loads without Playwright installed; tests that need a
    fake destination monkeypatch this function whole.
    """
    from anastomosis.deliver.browser.cdp import CdpEndpoint, connect_over_cdp
    from anastomosis.destinations.browserpack import (
        BrowserPackDestination,
        PlaywrightPageAdapter,
    )
    from anastomosis.destinations.loader import LoadedBrowserPack

    assert isinstance(loaded, LoadedBrowserPack)
    playwright, browser = connect_over_cdp(CdpEndpoint(cdp_url))
    # The operator has their EHR open; drive its existing context/page. We
    # NEVER close that context/page; on close() we release ONLY our owned
    # resources — disconnect the CDP session (browser.close() does not
    # close a connect_over_cdp browser) and stop the driver subprocess (in
    # that order, per Playwright).
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
