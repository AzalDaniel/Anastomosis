"""Session lifecycle management around one destination (M2 item 10).

:class:`ManagedDestination` decorates a :class:`Destination`, wrapping only
its :class:`UploadDriver` so collaborators see an ordinary destination while
session health is handled: crash relaunch before a dead session's upload,
periodic recycling after ``recycle_every`` successes, and dead-session
cleanup that re-raises the original exception unchanged after a failure.
Single-threaded by contract (50). PHI: logs only ``exc_tag`` and counts (2).
"""

from __future__ import annotations

import logging

from anastomosis.core.logutil import exc_tag
from anastomosis.destinations.base import (
    BannerCheck,
    Destination,
    DestinationPatient,
    ExistingDocsScanner,
    PatientResolver,
    Session,
    UploadDriver,
    UploadItem,
    UploadReceipt,
)

__all__ = ["ManagedDestination"]

logger = logging.getLogger(__name__)


class _ManagedDriver:
    """The upload driver wrapper that owns session health for one destination.

    Reads the inner destination's session/driver on every call; not
    thread-safe by design (50).
    """

    def __init__(self, inner: Destination, *, recycle_every: int) -> None:
        self._inner = inner
        self._recycle_every = recycle_every
        self._uploads_since_launch = 0

    def upload(self, item: UploadItem, patient: DestinationPatient) -> UploadReceipt:
        """Upload via the inner driver, managing session health around it
        (crash relaunch, recycling, dead-session cleanup — see module doc).
        """
        # Crash relaunch: a session found dead at the start of a call is
        # reopened once before the upload runs.
        if not self._inner.session.is_alive():
            logger.info("session not alive; relaunching before upload")
            self._relaunch()

        try:
            receipt = self._inner.driver.upload(item, patient)
        except BaseException:
            # On failure, if the session died, close it and re-raise the
            # ORIGINAL exception unchanged — the engine routes on its type.
            if not self._inner.session.is_alive():
                logger.warning("session dead after upload failure; closing")
                self._close_quietly()
            raise

        self._uploads_since_launch += 1
        if self._uploads_since_launch >= self._recycle_every:
            logger.info(
                "recycling session after %d successful upload(s)",
                self._uploads_since_launch,
            )
            self._close_quietly()
            self._inner.session.open()
            self._uploads_since_launch = 0
        return receipt

    # --- session helpers ---

    def _relaunch(self) -> None:
        """Close (tolerantly) and reopen the session — the crash-relaunch step."""
        self._close_quietly()
        self._inner.session.open()
        self._uploads_since_launch = 0

    def _close_quietly(self) -> None:
        """Close the session, logging (not raising) a close error so it
        cannot mask the upload's own outcome.
        """
        try:
            self._inner.session.close()
        except Exception as exc:
            logger.warning("session close failed (%s)", exc_tag(exc))


class ManagedDestination:
    """A :class:`Destination` wrapper that manages its inner session
    lifecycle: delegates everything but wraps :attr:`driver` for crash
    relaunch, recycling and dead-session cleanup. Single-threaded by
    contract (50).
    """

    def __init__(self, inner: Destination, *, recycle_every: int = 100) -> None:
        self._inner = inner
        self._driver = _ManagedDriver(inner, recycle_every=recycle_every)

    # --- Destination protocol (delegation) ---

    @property
    def name(self) -> str:
        return self._inner.name

    @property
    def session(self) -> Session:
        return self._inner.session

    @property
    def resolver(self) -> PatientResolver:
        return self._inner.resolver

    @property
    def banner(self) -> BannerCheck:
        return self._inner.banner

    @property
    def scanner(self) -> ExistingDocsScanner:
        return self._inner.scanner

    @property
    def driver(self) -> UploadDriver:
        return self._driver

    # --- run bracketing (so the batch layer can open/close a run) ---

    def open(self) -> None:
        """Open the inner session (passthrough so a batch can bracket a run)."""
        self._inner.session.open()

    def close(self) -> None:
        """Close the inner session (passthrough so a batch can bracket a run)."""
        self._inner.session.close()
