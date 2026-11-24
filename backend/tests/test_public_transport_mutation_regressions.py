"""No-network checks for pinned public transport, deadlines and response bounds."""
import socket
from types import SimpleNamespace

import pytest
from round_robin.research import fetcher
from round_robin.research.fetcher import (
    FetchRejected,
    FetchUnavailable,
    SocketPublicTransport,
    Target,
)


@pytest.fixture
def fake_transport(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Network access forbidden in public transport probe")
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    observed = SimpleNamespace(connections=[], sockets=[], wraps=[], reads=[], requests=[], closed=[])
    observed.response = SimpleNamespace(status=206, headers=[("Content-Type", "text/plain"), ("X-Source", "synthetic")], body=b"ABCD")
    observed.tls_error = None
    class RawSocket:
        def __init__(self):
            self.timeouts = []
            self.closed = False
        def settimeout(self, timeout):
            self.timeouts.append(timeout)
        def close(self):
            self.closed = True
        def sendall(self, value):
            pass
    def connect(address=None, timeout=None, **kwargs):
        raw = RawSocket()
        observed.sockets.append((address, timeout, raw))
        return raw
    class TLSContext:
        def wrap_socket(self, raw=None, server_hostname=None, **kwargs):
            observed.wraps.append((raw, server_hostname))
            if observed.tls_error:
                raise observed.tls_error
            return raw
    class Response:
        @property
        def status(self): return observed.response.status
        def getheaders(self): return observed.response.headers
        def read(self, size=None):
            observed.reads.append(size)
            return observed.response.body if size is None else observed.response.body[:size]
    class Connection:
        def __init__(self, host=None, port=None, timeout=None, **kwargs):
            self.host, self.port, self.timeout, self.sock = host, port, timeout, None
            observed.connections.append(self)
        def request(self, method=None, url=None, **kwargs):
            observed.requests.append((method, url, kwargs.get("headers"), self.sock))
            self.sock.sendall(b"synthetic request")
        def getresponse(self): return Response()
        def close(self):
            observed.closed.append(self)
            if self.sock is not None: self.sock.close()
    observed.clock = iter([100.0, 101.0, 102.0])
    monkeypatch.setattr(fetcher.time, "monotonic", lambda: next(observed.clock))
    monkeypatch.setattr(fetcher.socket, "create_connection", connect)
    monkeypatch.setattr(fetcher.ssl, "create_default_context", TLSContext)
    monkeypatch.setattr(fetcher.http.client, "HTTPConnection", Connection)
    return observed


@pytest.mark.parametrize("hostname,address,expected_host", [
    ("public.example", "203.0.113.10", "public.example"),
    ("2606:4700:4700::1111", "2606:4700:4700::1111", "[2606:4700:4700::1111]"),
])
def test_pinned_address_sni_host_deadline_and_bounded_request(fake_transport, hostname, address, expected_host):
    target = Target(url=f"https://{hostname}/facts", hostname=hostname, address=address, request_target="/facts?scope=public")
    response = SocketPublicTransport().get(target, timeout=5.0, byte_limit=4,
                                           private_headers={"X-Synthetic": "scoped", "Host": "untrusted.example"})
    observed = fake_transport
    assert len(observed.connections) == len(observed.sockets) == len(observed.wraps) == len(observed.requests) == 1
    connection = observed.connections[0]
    raw = observed.sockets[0][2]
    assert observed.sockets[0][:2] == ((address, 443), 5.0)
    assert raw.timeouts == [4.0, 3.0]
    assert observed.wraps == [(raw, hostname)]
    method, path, headers, wrapper = observed.requests[0]
    assert (method, path) == ("GET", "/facts?scope=public")
    assert headers["Host"] == expected_host and headers["X-Synthetic"] == "scoped"
    assert headers.get("Accept") == "text/plain,text/html,application/xhtml+xml"
    assert headers.get("Accept-Encoding") == "identity"
    assert isinstance(wrapper, fetcher.DeadlineSocket) and wrapper.sock is raw and wrapper.deadline == 105.0
    assert observed.reads == [5] and observed.closed == [connection] and raw.closed
    assert response.status == 206 and response.headers == {"content-type": "text/plain", "x-source": "synthetic"} and response.body == b"ABCD"


@pytest.mark.parametrize("length,body,accepted", [(4, b"ABCD", True), (5, b"ABCDE", False), (None, b"ABCDE", False)])
def test_response_limit_accepts_exact_boundary_and_rejects_excess(fake_transport, length, body, accepted):
    fake_transport.response.headers = [] if length is None else [("Content-Length", str(length))]
    fake_transport.response.body = body
    target = Target(url="https://public.example/", hostname="public.example", address="203.0.113.10", request_target="/")
    if accepted:
        assert SocketPublicTransport().get(target, timeout=5, byte_limit=4).body == body
        assert fake_transport.reads == [5]
    else:
        with pytest.raises(FetchRejected):
            SocketPublicTransport().get(target, timeout=5, byte_limit=4)
        assert fake_transport.reads == ([] if length == 5 else [5])
    assert fake_transport.sockets[0][2].closed


def test_tls_failure_closes_raw_socket_without_request_or_sensitive_details(fake_transport):
    fake_transport.tls_error = OSError("PRIVATE_TLS_SENTINEL")
    target = Target(url="https://public.example/", hostname="public.example", address="203.0.113.10", request_target="/")
    with pytest.raises(FetchUnavailable) as caught:
        SocketPublicTransport().get(target, timeout=5, byte_limit=4)
    assert "PRIVATE_TLS_SENTINEL" not in str(caught.value)
    assert fake_transport.sockets[0][2].closed and not fake_transport.requests
    assert len(fake_transport.closed) == 1


def test_handshake_and_write_use_remaining_subsecond_deadline(fake_transport):
    fake_transport.clock = iter([100.0, 100.2, 100.3])
    target = Target(url="https://public.example/", hostname="public.example", address="203.0.113.10", request_target="/")
    assert SocketPublicTransport().get(target, timeout=0.5, byte_limit=4).body == b"ABCD"
    assert fake_transport.sockets[0][2].timeouts == pytest.approx([0.3, 0.2])
