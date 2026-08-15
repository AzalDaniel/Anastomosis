"""ManagedDestination tests: recycling, crash relaunch, and pass-through.

Synthetic data only — ``feedface-`` GUIDs, neutral file names. The inner
destination is an instrumented in-memory double built here (NOT a change to
``fake.py``): an instrumented session records open/close calls and exposes a
toggleable ``is_alive`` so the manager's session lifecycle is observable.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from anastomosis.deliver.browser.errors import PermanentDeliveryError, TransientDeliveryError
from anastomosis.deliver.browser.manager import ManagedDestination
from anastomosis.destinations.base import (
    DestinationPatient,
    UploadItem,
    UploadReceipt,
)

PAT_DEST = "dest-a"


class _InstrumentedSession:
    """A session that counts open/close and reports a controllable liveness."""

    def __init__(self) -> None:
        self.open_calls = 0
        self.close_calls = 0
        self._alive = True
        self.close_raises = False

    def set_alive(self, alive: bool) -> None:
        self._alive = alive

    def open(self) -> None:
        self.open_calls += 1
        self._alive = True

    def close(self) -> None:
        self.close_calls += 1
        if self.close_raises:
            raise RuntimeError("session close blew up")
        self._alive = False

    def is_alive(self) -> bool:
        return self._alive


class _InstrumentedDestination:
    """Minimal Destination double with an instrumented session and scripted upload."""

    def __init__(
        self,
        *,
        fail_on: set[str] | None = None,
        permanent_on: set[str] | None = None,
        die_after_failure_keys: set[str] | None = None,
    ) -> None:
        self.sess = _InstrumentedSession()
        # item_key -> raise TransientDeliveryError on upload.
        self._fail_on = set(fail_on or set())
        self._permanent_on = set(permanent_on or set())
        # On a failed upload of these keys, the session goes dead afterwards.
        self._die_after_failure_keys = set(die_after_failure_keys or set())
        self.upload_calls: list[str] = []

    @property
    def name(self) -> str:
        return "instrumented"

    @property
    def session(self) -> _InstrumentedSession:
        return self.sess

    @property
    def resolver(self) -> _InstrumentedDestination:
        return self

    @property
    def banner(self) -> _InstrumentedDestination:
        return self

    @property
    def scanner(self) -> _InstrumentedDestination:
        return self

    @property
    def driver(self) -> _InstrumentedDestination:
        return self

    def upload(self, item: UploadItem, patient: DestinationPatient) -> UploadReceipt:
        self.upload_calls.append(item.item_key)
        if item.item_key in self._permanent_on:
            if item.item_key in self._die_after_failure_keys:
                self.sess.set_alive(False)
            raise PermanentDeliveryError
        if item.item_key in self._fail_on:
            if item.item_key in self._die_after_failure_keys:
                self.sess.set_alive(False)
            raise TransientDeliveryError
        return UploadReceipt(destination_doc_id=f"doc-{item.item_key}")


def _item(key: str) -> UploadItem:
    return UploadItem(
        item_key=key,
        encounter_id=f"enc-{key}",
        patient_id="feedface-0000-0000-0000-00000000000a",
        file_path=Path(f"/dev/null/{key}.pdf"),
        sha256="0" * 64,
        size_bytes=10,
        fingerprint=f"{key}.pdf",
    )


def _dest_patient() -> DestinationPatient:
    return DestinationPatient(destination_patient_id=PAT_DEST, matched_on=("id",))


# --- delegation ---


def test_delegates_collaborators_to_inner() -> None:
    inner = _InstrumentedDestination()
    managed = ManagedDestination(inner)
    assert managed.name == "instrumented"
    assert managed.session is inner.session
    assert managed.resolver is inner
    assert managed.banner is inner
    assert managed.scanner is inner
    # The driver is wrapped, NOT the inner driver.
    assert managed.driver is not inner


# --- recycling after N uploads ---


def test_recycles_session_after_recycle_every_uploads() -> None:
    inner = _InstrumentedDestination()
    managed = ManagedDestination(inner, recycle_every=3)
    patient = _dest_patient()

    for i in range(3):
        managed.driver.upload(_item(f"k{i}"), patient)

    # After exactly recycle_every successful uploads: one close + one reopen.
    assert inner.sess.close_calls == 1
    assert inner.sess.open_calls == 1

    # Counter reset: the next two uploads do not trigger another recycle.
    managed.driver.upload(_item("k3"), patient)
    managed.driver.upload(_item("k4"), patient)
    assert inner.sess.close_calls == 1
    assert inner.sess.open_calls == 1

    # The sixth upload (third since the recycle) triggers the next recycle.
    managed.driver.upload(_item("k5"), patient)
    assert inner.sess.close_calls == 2
    assert inner.sess.open_calls == 2


# --- crash relaunch before upload ---


def test_dead_session_relaunched_exactly_once_before_upload() -> None:
    inner = _InstrumentedDestination()
    managed = ManagedDestination(inner, recycle_every=100)
    inner.sess.set_alive(False)  # session is dead at the start of the call.

    receipt = managed.driver.upload(_item("k0"), _dest_patient())

    # Exactly one close + one reopen before the upload; the upload then ran.
    assert inner.sess.close_calls == 1
    assert inner.sess.open_calls == 1
    assert inner.upload_calls == ["k0"]
    assert receipt.destination_doc_id == "doc-k0"


def test_live_session_is_not_relaunched_before_upload() -> None:
    inner = _InstrumentedDestination()
    managed = ManagedDestination(inner, recycle_every=100)

    managed.driver.upload(_item("k0"), _dest_patient())

    assert inner.sess.open_calls == 0
    assert inner.sess.close_calls == 0


# --- inner exception passes through unchanged + dead session closed ---


def test_inner_exception_passes_through_unchanged_and_closes_dead_session() -> None:
    inner = _InstrumentedDestination(fail_on={"k0"}, die_after_failure_keys={"k0"})
    managed = ManagedDestination(inner)

    with pytest.raises(TransientDeliveryError) as caught:
        managed.driver.upload(_item("k0"), _dest_patient())

    # Type identity preserved — the manager must NOT convert exception types.
    assert type(caught.value) is TransientDeliveryError
    # The dead session was closed (cleanup), but never reopened on the failure path.
    assert inner.sess.close_calls == 1
    assert inner.sess.open_calls == 0


def test_permanent_exception_passes_through_unchanged() -> None:
    inner = _InstrumentedDestination(permanent_on={"k0"})
    managed = ManagedDestination(inner)

    with pytest.raises(PermanentDeliveryError) as caught:
        managed.driver.upload(_item("k0"), _dest_patient())
    assert type(caught.value) is PermanentDeliveryError


def test_failure_with_live_session_does_not_close() -> None:
    # The upload raises but the session stays alive: nothing to clean up.
    inner = _InstrumentedDestination(fail_on={"k0"})
    managed = ManagedDestination(inner)

    with pytest.raises(TransientDeliveryError):
        managed.driver.upload(_item("k0"), _dest_patient())
    assert inner.sess.close_calls == 0
    assert inner.sess.open_calls == 0


def test_failed_upload_does_not_count_toward_recycle() -> None:
    # A failure must not advance the recycle counter.
    inner = _InstrumentedDestination(fail_on={"k0"})
    managed = ManagedDestination(inner, recycle_every=1)

    with pytest.raises(TransientDeliveryError):
        managed.driver.upload(_item("k0"), _dest_patient())
    # recycle_every=1 but the upload failed: no recycle happened.
    assert inner.sess.close_calls == 0
    assert inner.sess.open_calls == 0
    # A subsequent SUCCESS does recycle (now one successful upload reached 1).
    managed.driver.upload(_item("k1"), _dest_patient())
    assert inner.sess.close_calls == 1
    assert inner.sess.open_calls == 1


# --- close error tolerated during relaunch ---


def test_relaunch_tolerates_close_error() -> None:
    inner = _InstrumentedDestination()
    inner.sess.set_alive(False)
    inner.sess.close_raises = True  # the dead session errors on close.
    managed = ManagedDestination(inner, recycle_every=100)

    # The close error is swallowed; the relaunch still reopens and uploads.
    receipt = managed.driver.upload(_item("k0"), _dest_patient())
    assert receipt.destination_doc_id == "doc-k0"
    assert inner.sess.close_calls == 1
    assert inner.sess.open_calls == 1


# --- run bracketing passthrough ---


def test_open_close_passthrough() -> None:
    inner = _InstrumentedDestination()
    managed = ManagedDestination(inner)
    managed.open()
    managed.close()
    assert inner.sess.open_calls == 1
    assert inner.sess.close_calls == 1
