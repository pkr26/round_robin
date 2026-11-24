"""Provider DNS resolution respects request deadlines and worker boundaries."""

import socket
import threading
import time

import httpcore
import pytest
from round_robin.providers import transport


def test_dns_capacity_wait_uses_the_remaining_request_deadline(monkeypatch):
    timeouts = []

    class BusyCapacity:
        def acquire(self, *, timeout):
            timeouts.append(timeout)
            return False

    monkeypatch.setattr(transport, "_DNS_CAPACITY", BusyCapacity())
    with pytest.raises(httpcore.ConnectTimeout, match="capacity"):
        transport._addresses("provider.invalid", 443, time.monotonic() + 10)
    assert len(timeouts) == 1
    assert timeouts[0] is not None and 0 < timeouts[0] <= 10


def test_successful_dns_preserves_host_port_protocol_and_unique_address_order(monkeypatch):
    calls = []

    def resolve(host, port, *, type):
        calls.append((host, port, type))
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.0.2.1", port)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.0.2.1", port)),
            (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("2001:db8::1", port, 0, 0)),
        ]

    monkeypatch.setattr(transport.socket, "getaddrinfo", resolve)
    addresses = transport._addresses("provider.invalid", 8443, time.monotonic() + 10)

    assert calls == [("provider.invalid", 8443, socket.SOCK_STREAM)]
    assert addresses == ["192.0.2.1", "2001:db8::1"]


def test_dns_worker_is_daemon_so_stalled_resolution_cannot_hold_process_open(monkeypatch):
    original_thread = threading.Thread
    workers = []

    def record_thread(*args, **kwargs):
        worker = original_thread(*args, **kwargs)
        workers.append(worker)
        return worker

    monkeypatch.setattr(transport.threading, "Thread", record_thread)
    monkeypatch.setattr(transport.socket, "getaddrinfo", lambda *args, **kwargs: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.0.2.1", 443)),
    ])
    assert transport._addresses("provider.invalid", 443, time.monotonic() + 10) == ["192.0.2.1"]
    assert len(workers) == 1
    assert workers[0].daemon is True


def test_stream_honors_shorter_io_timeout_and_preserves_tls_result():
    deadline = time.monotonic() + 120
    calls = []
    tls_stream = object()
    info_result = object()

    class Stream:
        def read(self, size, timeout=None):
            calls.append(("read", size, timeout))
            return b"response"

        def start_tls(self, context, hostname, timeout=None):
            calls.append(("tls", context, hostname, timeout))
            return tls_stream

        def get_extra_info(self, info):
            calls.append(("info", info))
            return info_result

    bounded = transport._DeadlineStream(Stream(), deadline)
    assert bounded.read(7, timeout=1.25) == b"response"
    wrapped = bounded.start_tls("tls-context", "provider.invalid", timeout=2.5)
    assert wrapped.stream is tls_stream
    assert wrapped.deadline == deadline
    assert bounded.get_extra_info("peer-certificate") is info_result
    assert calls == [
        ("read", 7, 1.25),
        ("tls", "tls-context", "provider.invalid", 2.5),
        ("info", "peer-certificate"),
    ]


def test_tcp_backend_forwards_destination_timeout_and_socket_options(monkeypatch):
    deadline = time.monotonic() + 120
    resolved = []
    connected = []
    raw_stream = object()
    options = [(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)]

    def resolve(host, port, request_deadline):
        resolved.append((host, port, request_deadline))
        return ["192.0.2.1"]

    class Backend:
        def connect_tcp(self, address, port, *, timeout=None, local_address=None, socket_options=None):
            connected.append((address, port, timeout, local_address, socket_options))
            return raw_stream

    monkeypatch.setattr(transport, "_addresses", resolve)
    backend = transport._DeadlineBackend(deadline)
    assert callable(getattr(backend.backend, "connect_tcp", None))
    backend.backend = Backend()
    wrapped = backend.connect_tcp("provider.invalid", 8443, timeout=1.25, local_address="127.0.0.1", socket_options=options)

    assert resolved == [("provider.invalid", 8443, deadline)]
    assert connected == [("192.0.2.1", 8443, 1.25, "127.0.0.1", options)]
    assert wrapped.stream is raw_stream
    assert wrapped.deadline == deadline


def test_provider_connection_pool_enforces_no_retries_and_deadline():
    deadline = time.monotonic() + 120
    tls_context = object()
    adapter = transport.DeadlineHTTPTransport(deadline, ssl_context=tls_context)
    try:
        pool = adapter.pool
        assert pool._ssl_context is tls_context
        assert pool._max_connections == 1
        assert pool._max_keepalive_connections == 0
        assert pool._retries == 0
        assert isinstance(pool._network_backend, transport._DeadlineBackend)
        assert pool._network_backend.deadline == deadline
    finally:
        adapter.close()


def test_provider_transport_preserves_request_and_response_data():
    import httpx

    observed = []

    class ResponseStream:
        def __iter__(self):
            yield b"response-bytes"

        def close(self):
            pass

    class Pool:
        def handle_request(self, request):
            observed.append(request)
            return httpcore.Response(
                207,
                headers=[(b"x-result", b"ok")],
                content=ResponseStream(),
                extensions={"http_version": b"HTTP/1.1"},
            )

        def close(self):
            pass

    adapter = transport.DeadlineHTTPTransport(time.monotonic() + 120)
    adapter.pool.close()
    adapter.pool = Pool()
    request = httpx.Request(
        "POST", "https://provider.invalid:8443/v1/resource?model=synthetic",
        headers={"x-request-token": "sentinel"}, content=b"request-bytes",
        extensions={"synthetic": "extension"},
    )
    response = adapter.handle_request(request)
    assert len(observed) == 1
    captured = observed[0]
    assert captured.method == b"POST"
    assert captured.url.scheme == b"https"
    assert captured.url.host == b"provider.invalid"
    assert captured.url.port == 8443
    assert captured.url.target == b"/v1/resource?model=synthetic"
    assert (b"x-request-token", b"sentinel") in captured.headers
    assert b"".join(captured.stream) == b"request-bytes"
    assert captured.extensions["synthetic"] == "extension"
    assert response.status_code == 207
    assert response.headers["x-result"] == "ok"
    assert response.read() == b"response-bytes"
    assert response.extensions["http_version"] == b"HTTP/1.1"
    adapter.close()
