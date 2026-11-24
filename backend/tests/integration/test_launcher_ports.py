"""Actual loopback socket lifecycle for local launcher/verifier admission."""
import os
import socket

import pytest

from scripts.dev import available
from scripts.verify import free_port


pytestmark = pytest.mark.skipif(os.environ.get("ROUND_ROBIN_TEST_ALLOW_LOOPBACK") != "1", reason="Socket lifecycle check requires the explicit disposable loopback profile")


def test_port_probes_reject_listeners_but_allow_their_closed_connections():
    with socket.socket() as listener, socket.socket() as client:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        listener.listen(1)
        assert available(port) is False
        assert free_port(port) is False
        client.connect(("127.0.0.1", port))
        connection, _ = listener.accept()
        connection.close()  # The server side initiates TIME_WAIT.
        assert client.recv(1) == b""
    assert available(port) is True
    assert free_port(port) is True
