"""Tests for core.clock — the SOURCE_DATE_EPOCH seam every stamping site shares."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from anastomosis.core import clock

_EPOCH = 1700000000
_EPOCH_DATETIME = datetime.fromtimestamp(_EPOCH, tz=UTC)
_EPOCH_DATE = datetime.fromtimestamp(_EPOCH).date()  # host-local, matches clock.today()


def test_now_pinned_to_source_date_epoch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SOURCE_DATE_EPOCH", str(_EPOCH))
    first = clock.now()
    second = clock.now()
    assert first == second == _EPOCH_DATETIME


def test_today_pinned_to_source_date_epoch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SOURCE_DATE_EPOCH", str(_EPOCH))
    assert clock.today() == _EPOCH_DATE


def test_now_is_real_time_without_the_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SOURCE_DATE_EPOCH", raising=False)
    before = datetime.now(UTC)
    got = clock.now()
    after = datetime.now(UTC)
    assert before <= got <= after
    assert after - before < timedelta(seconds=1)


def test_today_is_the_real_host_day_without_the_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SOURCE_DATE_EPOCH", raising=False)
    assert clock.today() == date.today()


def test_now_is_timezone_aware(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SOURCE_DATE_EPOCH", str(_EPOCH))
    assert clock.now().tzinfo is not None
