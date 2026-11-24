"""Exercise real socket entry points and session-start credential rejection."""

import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile

import pytest


ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def default_network_profile(monkeypatch):
    monkeypatch.delenv("ROUND_ROBIN_TEST_ALLOW_LOOPBACK", raising=False)


@pytest.mark.parametrize("host", ["example.com", "api.openai.com", "127.0.0.1", "::1", "localhost"])
def test_dns_and_connection_helpers_are_denied_by_default(host):
    with pytest.raises(RuntimeError, match="offline test network boundary"):
        socket.getaddrinfo(host, 443)
    with pytest.raises(RuntimeError, match="offline test network boundary"):
        socket.create_connection((host, 443), timeout=0.01)


def test_raw_tcp_and_udp_entrypoints_are_denied_by_default():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as stream:
        with pytest.raises(RuntimeError, match="offline test network boundary"):
            stream.connect(("1.1.1.1", 443))
        with pytest.raises(RuntimeError, match="offline test network boundary"):
            stream.connect_ex(("127.0.0.1", 443))
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as datagram:
        with pytest.raises(RuntimeError, match="offline test network boundary"):
            datagram.sendto(b"synthetic", ("1.1.1.1", 53))


def test_explicit_loopback_opt_in_allows_only_disposable_local_service(monkeypatch):
    monkeypatch.setenv("ROUND_ROBIN_TEST_ALLOW_LOOPBACK", "1")
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        listener.settimeout(1)
        with socket.create_connection(listener.getsockname(), timeout=1) as client:
            accepted, _ = listener.accept()
            with accepted:
                client.sendall(b"local-fixture")
                assert accepted.recv(32) == b"local-fixture"
    assert socket.getaddrinfo("localhost", 80)
    for host in ("example.com", "127.0.0.1.example.com", "169.254.169.254", "192.168.0.1"):
        with pytest.raises(RuntimeError, match="offline test network boundary"):
            socket.getaddrinfo(host, 443)


def test_production_credentials_fail_before_collection_without_printing_values():
    sentinel = "synthetic-secret-that-must-never-be-printed"
    env = {**os.environ, "OPENAI_API_KEY": sentinel}
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "backend/tests/test_offline_boundary.py"],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=15,
    )
    combined = result.stdout + result.stderr
    assert result.returncode != 0
    assert "refuses production provider credentials" in combined
    assert sentinel not in combined


def test_boundary_is_active_during_test_module_collection():
    with tempfile.TemporaryDirectory(prefix="offline-boundary-", dir=ROOT / "backend" / "tests") as directory:
        path = Path(directory) / "test_import_network.py"
        path.write_text("import socket\nsocket.getaddrinfo('external.invalid',443)\n", encoding="utf-8")
        result = subprocess.run(
            [sys.executable, "-m", "pytest", "--collect-only", "-q", str(path)],
            cwd=ROOT, capture_output=True, text=True, timeout=15,
        )
        assert result.returncode != 0
        assert "offline test network boundary denied destination" in result.stdout + result.stderr
