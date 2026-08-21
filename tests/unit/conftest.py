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
