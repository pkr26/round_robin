"""Public fetching preserves its pinned destination and bounded I/O contract."""

import pytest
from round_robin.research import fetcher


@pytest.mark.parametrize("body,content_length,rejected", [(b"abcd", "4", False), (b"abcde", None, True)])
def test_transport_keeps_deadlines_byte_bounds_and_ipv6_host(monkeypatch, body, content_length, rejected):
    observed = {}

    class RawSocket:
        def settimeout(self, value):
            observed["handshake_timeout"] = value

        def close(self):
            observed["raw_closed"] = True

    raw = RawSocket()

    def connect(address, timeout=None):
        observed["address"] = address
        observed["connect_timeout"] = timeout
        return raw

    class TLSContext:
        def wrap_socket(self, sock, *, server_hostname):
            assert sock is raw
            observed["sni"] = server_hostname
            return sock

    class Response:
        status = 200

        def getheaders(self):
            headers = [("Content-Type", "text/plain")]
            if content_length is not None:
                headers.append(("Content-Length", content_length))
            return headers

        def read(self, size=None):
            observed["read_size"] = size
            return body if size is None else body[:size]

    class Connection:
        def __init__(self, hostname, port=None, *, timeout=None):
            self.sock = None

        def request(self, method, path, *, headers):
            observed["request"] = (method, path, headers)

        def getresponse(self):
            return Response()

        def close(self):
            observed["closed"] = True

    monkeypatch.setattr(fetcher.socket, "create_connection", connect)
    monkeypatch.setattr(fetcher.ssl, "create_default_context", TLSContext)
    monkeypatch.setattr(fetcher.http.client, "HTTPConnection", Connection)
    monkeypatch.setattr(fetcher, "DeadlineSocket", lambda sock, deadline: sock)
    target = fetcher.Target(
        url="https://[2001:4860:4860::8888]/facts",
        hostname="2001:4860:4860::8888",
        address="93.184.216.34",
        request_target="/facts?scope=public",
    )

    if rejected:
        with pytest.raises(fetcher.FetchRejected):
            fetcher.SocketPublicTransport().get(target, timeout=0.2, byte_limit=4)
    else:
        response = fetcher.SocketPublicTransport().get(target, timeout=0.2, byte_limit=4)
        assert response.body == b"abcd"
        assert response.headers["content-type"] == "text/plain"

    assert observed["address"] == ("93.184.216.34", 443)
    assert observed["connect_timeout"] == 0.2
    assert 0 < observed["handshake_timeout"] <= 0.2
    assert observed["sni"] == "2001:4860:4860::8888"
    method, path, headers = observed["request"]
    assert (method, path) == ("GET", "/facts?scope=public")
    assert headers["Host"] == "[2001:4860:4860::8888]"
    normalized_headers = {name.lower(): value for name, value in headers.items()}
    assert normalized_headers["user-agent"].lower().startswith("roundrobin/")
    assert {value.strip().lower() for value in normalized_headers["accept"].split(",")} == {
        "text/plain", "text/html", "application/xhtml+xml",
    }
    assert normalized_headers["accept-encoding"].lower() == "identity"
    assert observed["read_size"] == 5
    assert observed["closed"] is True
