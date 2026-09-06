"""Unit-suite network guard: the toolkit is local-first and
PHI-stays-on-the-box, so this autouse fixture restricts every unit
test to loopback only — a non-loopback connection fails loudly here
instead of silently leaving the machine.

Loopback and AF_UNIX are allowed on purpose: asyncio's event-loop
self-pipe uses one of those, never egress. Lifted on teardown so it
never leaks into ``tests/integration`` (live HAPI) or ``tests/e2e``.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
import pytest_socket

_LOOPBACK = ["127.0.0.1", "::1", "localhost"]


@pytest.fixture(autouse=True)
def _block_external_network() -> Iterator[None]:
    pytest_socket.socket_allow_hosts(_LOOPBACK, allow_unix_socket=True)
    try:
        yield
    finally:
        pytest_socket.enable_socket()


@pytest.fixture(autouse=True)
def _source_registry_is_not_shared_between_tests() -> Iterator[None]:
    """Snapshot the source-adapter registry and put it back after each
    test: teaching a format registers into process-global state, so one
    test's saved format would otherwise change what a later test may
    save. Built-ins load BEFORE the snapshot, since they arrive lazily
    on first use and set a module flag."""
    from anastomosis.sources import available_sources, base

    available_sources()
    saved = dict(base._REGISTRY)
    try:
        yield
    finally:
        base._REGISTRY.clear()
        base._REGISTRY.update(saved)


@pytest.fixture(autouse=True)
def _user_home_is_not_the_developers(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Point ``~/.anastomosis`` at a per-test temporary home: trusted
    hashes, learned layouts/mappings and migration profiles all hang
    off ``Path.home()``, so without this a developer's own taught
    layout would join the packs a test asserts about."""
    home = tmp_path_factory.mktemp("anastomosis-home")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))  # Path.home() on Windows
    assert Path.home() == home
