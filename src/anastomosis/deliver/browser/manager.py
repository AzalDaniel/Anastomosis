"""Session lifecycle management around one destination (M2 item 10).

A real browser session leaks slowly and dies unpredictably — the same two
hazards the reconstruction engine handles for Chromium renderers (see
:mod:`anastomosis.reconstruct.engine`'s renderer recycling and crash
relaunch). :class:`ManagedDestination` is that operational layer for the
upload side: it decorates any :class:`Destination` and wraps only its
:class:`UploadDriver` so the engine, the ledger, and every other collaborator
see an ordinary destination, while the wrapped ``upload`` quietly handles
session health.

Three behaviors, mirrored from the renderer lifecycle:

* **Crash relaunch (before the upload).** If the session is found dead at the
  start of an upload, it is closed (tolerantly) and reopened once — exactly
  one relaunch per call — before the real upload runs. A vendor session that
  timed out between items costs a relaunch, not a failed chart.
* **Recycling (after a successful upload).** Browser sessions leak memory and
  accumulate state over a long batch, so after ``recycle_every`` successful
  uploads the session is closed and reopened and the counter reset — the same
  reasoning as renderer recycling, preferring a cheap periodic relaunch over
  debugging a slow leak.
* **Dead-session cleanup (after a failed upload).** If the inner upload raises
  and the session is dead afterwards, the session is closed and the original
  exception is re-raised *unchanged* — the engine's transient/permanent
  routing decides the item's fate from the exception type, so converting types
  here would corrupt that decision.

Single-threaded by contract: a :class:`ManagedDestination` owns one session
and one upload counter with no locking, so each parallel worker must construct
its OWN :class:`ManagedDestination` (the parallel runner does exactly this via
its destination factory). Sharing one across threads would race the counter
and the session lifecycle.

PHI rule: this layer logs only ``exc_tag`` type names and counts — never a
patient value, a path, or a receipt extra.
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

    Holds no patient state; it reads the inner destination's session and
    driver on every call so it always sees the live collaborators. Not
    thread-safe by design — see the module docstring.
    """

    def __init__(self, inner: Destination, *, recycle_every: int) -> None:
        self._inner = inner
        self._recycle_every = recycle_every
        self._uploads_since_launch = 0

    def upload(self, item: UploadItem, patient: DestinationPatient) -> UploadReceipt:
        """Upload via the inner driver, managing the session around the call.

        Order matches the docstring: (a) crash-relaunch a dead session before
        the upload, exactly once; (b) run the inner upload; (c) on success,
        count it and recycle the session when the threshold is reached; (d) on
        failure with a dead session, close it and re-raise the error AS-IS.
        """
        # (a) Crash relaunch: a session found dead at the start of a call is
        # reopened once before the upload runs.
        if not self._inner.session.is_alive():
            logger.info("session not alive; relaunching before upload")
            self._relaunch()

        try:
            # (b) The real upload.
            receipt = self._inner.driver.upload(item, patient)
        except BaseException:
            # (d) On failure, if the session died, close it and re-raise the
            # ORIGINAL exception unchanged — the engine routes on its type.
            if not self._inner.session.is_alive():
                logger.warning("session dead after upload failure; closing")
                self._close_quietly()
            raise

        # (c) Success: count it and recycle on the threshold.
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
        """Close the session, swallowing (but logging) a close error.

        A dead session may raise on close; that must not mask the upload's own
        outcome, so the close error is logged by type and discarded — exactly
        how the renderer lifecycle tolerates a failing ``close``.
        """
        try:
            self._inner.session.close()
        except Exception as exc:
            logger.warning("session close failed (%s)", exc_tag(exc))


class ManagedDestination:
    """A :class:`Destination` wrapper that manages its inner session lifecycle.

    Delegates :attr:`name`, :attr:`resolver`, :attr:`banner`, and
    :attr:`scanner` straight to ``inner``; exposes the inner session as its own
    :attr:`session`; and wraps :attr:`driver` so each upload manages session
    health (crash relaunch, periodic recycling, dead-session cleanup).

    Single-threaded by contract — one session and one counter, unlocked. Each
    parallel worker gets its OWN :class:`ManagedDestination`.
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
