# AI-assisted: written with Claude agents under the author's direction and review; see DESIGN.md.
"""The unit suite must not be able to reach an external network host.

Proves the autouse guard in conftest.py is active: a connection to a
non-loopback host is refused, while loopback and asyncio (the GUI controller's
async workers) keep working — the self-pipe is an AF_UNIX socketpair on POSIX
and a loopback socket on Windows, neither of which is egress.
"""

from __future__ import annotations

import asyncio
import socket
import warnings

import pytest
import pytest_socket


def test_external_network_is_blocked() -> None:
    # 192.0.2.1 is TEST-NET-1 (RFC 5737), never routed. The guard refuses the
    # connect by host *before* any real attempt, so this never touches a wire.
    # pytest-socket warns as well as raises; silence the artifact warning.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        with pytest.raises(pytest_socket.SocketConnectBlockedError):
            socket.create_connection(("192.0.2.1", 80), timeout=0.2)


def test_loopback_and_asyncio_still_work() -> None:
    # A fresh asyncio loop (AF_UNIX self-pipe on POSIX, loopback on Windows) and
    # a real loopback round-trip both succeed under the guard.
    loop = asyncio.new_event_loop()
    try:
        assert loop.run_until_complete(asyncio.sleep(0)) is None
    finally:
        loop.close()

    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        client = socket.create_connection(("127.0.0.1", server.getsockname()[1]), timeout=0.2)
        client.close()
    finally:
        server.close()
