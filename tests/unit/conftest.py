"""Unit-suite network guard.

The toolkit is local-first and PHI-stays-on-the-box; nothing in the unit suite
should ever reach an *external* network. This autouse fixture (scoped to
``tests/unit`` by this conftest's location) restricts every unit test to
loopback only — a connection to any non-loopback host fails loudly here instead
of silently leaving the machine.

Loopback (``127.0.0.1``/``::1``/``localhost``) and AF_UNIX are allowed on
purpose, not as a leak: asyncio's event-loop self-pipe — exercised by the GUI
controller's async workers — uses an AF_UNIX socketpair on POSIX and a loopback
socket on Windows (the Proactor loop), and neither is egress. Blocking those
broke the suite cross-platform; allowing loopback while blocking outbound hosts
is the correct PHI boundary. The fixture lifts the restriction on teardown so it
never leaks into the separate ``tests/integration`` lane (live HAPI, gated by
``ANAST_FHIR_BASE_URL``) or the ``tests/e2e`` render lane.
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
    """Snapshot the source-adapter registry and put it back after each test.

    The registry is process-global product state, and teaching a format now
    registers into it — deliberately, so a taught source is selectable without
    a restart. In a running application that accumulation is the feature. In a
    test run it means one test's saved format changes what a later test is
    allowed to save, and the failure surfaces far from its cause: two files
    here passed alone and failed in the suite before this existed.

    The built-ins are loaded BEFORE the snapshot on purpose. They arrive
    lazily on first use and set a module flag; snapshotting an empty registry
    and restoring it afterwards would delete them while the flag still claimed
    they were loaded, leaving every later test with no adapters at all.
    """
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
    """Point ``~/.anastomosis`` at a per-test temporary home.

    Everything the toolkit remembers between runs — trusted pack hashes, learned
    layouts, learned source mappings, migration profiles — hangs off
    ``Path.home()``. Discovery reads that directory on every ``discover_packs``
    call, so without this a developer's own taught layout would join the packs a
    test asserts about, and a test that teaches one would write into their real
    home and leave it there.

    Same reasoning as the registry fixture above: real product state, isolated
    per test rather than shared across the run.
    """
    home = tmp_path_factory.mktemp("anastomosis-home")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))  # Path.home() on Windows
    assert Path.home() == home
