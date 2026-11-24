"""Opt-in real loopback TLS evidence for the production public-fetch transport.

These tests construct an internal Target directly and remap only its fixed port
443 to a disposable loopback listener. Public URL validation is never weakened.
No DNS or connection to example.com or any external destination is permitted.
"""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import socket
import ssl
import threading
import time

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
import pytest

from round_robin.research.fetcher import FetchUnavailable, SocketPublicTransport, Target

pytestmark = pytest.mark.skipif(os.environ.get("ROUND_ROBIN_TEST_ALLOW_LOOPBACK") != "1", reason="Real TLS transport checks require the explicit loopback-only test profile")


@pytest.fixture
def tls_files(tmp_path):
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "example.com")])
    now = datetime.now(timezone.utc)
    certificate = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
        .public_key(private_key.public_key()).serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1)).not_valid_after(now + timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName("example.com")]), critical=False)
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .sign(private_key, hashes.SHA256()))
    certificate_path, key_path = tmp_path / "fixture-cert.pem", tmp_path / "fixture-key.pem"
    certificate_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(private_key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    key_path.chmod(0o600)
    return certificate_path, key_path


@contextmanager
def tls_server(files, *, mode="close_body"):
    certificate_path, key_path = files
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certificate_path, key_path)
    observations = {"sni": [], "host": [], "requests": [], "peers": [], "connections": 0, "errors": []}
    context.set_servername_callback(lambda sock, hostname, ctx: observations["sni"].append(hostname))
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(4)
    listener.settimeout(.05)
    stop = threading.Event()
    body = b"An exact fictional response body over the pinned TLS connection.\n" * 300

    def serve():
        while not stop.is_set():
            try:
                raw, peer = listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            observations["connections"] += 1
            observations["peers"].append(peer[0])
            try:
                raw.settimeout(1)
                with context.wrap_socket(raw, server_side=True) as stream:
                    request = b""
                    while b"\r\n\r\n" not in request and len(request) < 8192:
                        chunk = stream.recv(1024)
                        if not chunk:
                            break
                        request += chunk
                    observations["requests"].append(request)
                    for header in request.split(b"\r\n"):
                        if header.lower().startswith(b"host:"):
                            observations["host"].append(header.partition(b":")[2].strip().decode("ascii"))
                    if mode == "slow_headers":
                        stream.sendall(b"HTTP/1.1 200 OK\r\n")
                        for byte in b"Content-Type: text/plain\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok":
                            if stop.wait(.035):
                                break
                            stream.sendall(bytes([byte]))
                    elif mode == "slow_body":
                        stream.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nContent-Length: 1000\r\nConnection: close\r\n\r\n")
                        for _ in range(1000):
                            if stop.wait(.035):
                                break
                            stream.sendall(b"x")
                    else:
                        stream.sendall(f"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode())
                        # Delay the body until HTTPConnection has processed the
                        # Connection: close header; buffering cannot hide an
                        # incorrectly closed underlying TLS socket.
                        stop.wait(.08)
                        stream.sendall(body)
            except (OSError, ssl.SSLError) as exception:
                observations["errors"].append(type(exception).__name__)
                raw.close()
    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        yield listener.getsockname()[1], observations, body
    finally:
        stop.set()
        listener.close()
        thread.join(timeout=2)
        assert not thread.is_alive(), "Disposable TLS server did not stop"


def pin_fixture_connection(monkeypatch, port, certificate_path, *, trust_fixture=True):
    real_create_connection = socket.create_connection
    requested_destinations = []
    def connect(address, *args, **kwargs):
        requested_destinations.append(address)
        assert address == ("127.0.0.1", 443), "The production transport must connect to its validated pinned IP"
        return real_create_connection(("127.0.0.1", port), *args, **kwargs)
    monkeypatch.setattr(socket, "create_connection", connect)
    if trust_fixture:
        def only_fixture_ca(*args, **kwargs):
            client = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            client.check_hostname = True
            client.verify_mode = ssl.CERT_REQUIRED
            client.load_verify_locations(cafile=certificate_path)
            return client
        monkeypatch.setattr(ssl, "create_default_context", only_fixture_ca)
    return requested_destinations


def internal_target(hostname="example.com"):
    return Target(url=f"https://{hostname}/facts", hostname=hostname, address="127.0.0.1", request_target="/facts?scope=public")


def test_real_tls_preserves_pinned_ip_sni_host_and_connection_close_body(monkeypatch, tls_files):
    with tls_server(tls_files) as (port, observed, expected):
        destinations = pin_fixture_connection(monkeypatch, port, tls_files[0])
        response = SocketPublicTransport().get(internal_target(), timeout=2, byte_limit=32768)
    assert response.status == 200
    assert response.body == expected
    assert observed["sni"] == ["example.com"]
    assert observed["host"] == ["example.com"]
    assert observed["peers"] == ["127.0.0.1"]
    assert destinations == [("127.0.0.1", 443)]
    assert observed["requests"][0].startswith(b"GET /facts?scope=public HTTP/1.1\r\n")


@pytest.mark.parametrize("mode", ["slow_headers", "slow_body"])
def test_real_tls_total_deadline_is_not_renewed_by_drip_traffic(monkeypatch, tls_files, mode):
    with tls_server(tls_files, mode=mode) as (port, observed, _):
        destinations = pin_fixture_connection(monkeypatch, port, tls_files[0])
        began = time.monotonic()
        with pytest.raises(FetchUnavailable):
            SocketPublicTransport().get(internal_target(), timeout=.25, byte_limit=32768)
        elapsed = time.monotonic() - began
    assert .15 <= elapsed < .9, f"A 250 ms total deadline took {elapsed:.3f} seconds"
    assert len(destinations) == 1
    assert observed["connections"] == 1


def test_real_tls_untrusted_certificate_has_no_insecure_fallback(monkeypatch, tls_files):
    with tls_server(tls_files) as (port, observed, _):
        destinations = pin_fixture_connection(monkeypatch, port, tls_files[0], trust_fixture=False)
        with pytest.raises(FetchUnavailable):
            SocketPublicTransport().get(internal_target(), timeout=1, byte_limit=32768)
    assert destinations == [("127.0.0.1", 443)]
    assert observed["requests"] == []


def test_real_tls_hostname_mismatch_has_no_insecure_fallback(monkeypatch, tls_files):
    with tls_server(tls_files) as (port, observed, _):
        destinations = pin_fixture_connection(monkeypatch, port, tls_files[0])
        with pytest.raises(FetchUnavailable):
            SocketPublicTransport().get(internal_target("wrong.example"), timeout=1, byte_limit=32768)
    assert destinations == [("127.0.0.1", 443)]
    assert observed["sni"] == ["wrong.example"]
    assert observed["requests"] == []
