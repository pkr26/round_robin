from types import SimpleNamespace

from round_robin.research import fetcher, search
from round_robin.research.fetcher import FetchResponse, Target
from round_robin.research.search import BraveSearch


def test_default_search_timeout_includes_dns_and_transport(monkeypatch):
    ticks = iter((100.0, 100.25))
    monkeypatch.setattr(search.time, "monotonic", lambda: next(ticks))
    seen = {}

    def validate(url, resolver, *, resolution_timeout):
        seen["dns_timeout"] = resolution_timeout
        return SimpleNamespace(url=url)

    def get(target, **kwargs):
        seen["request_timeout"] = kwargs["timeout"]
        return FetchResponse(200, {}, b'{"web":{"results":[]}}')

    monkeypatch.setattr(search, "validate_target", validate)
    assert (
        BraveSearch(transport=SimpleNamespace(get=get), api_key="synthetic").search(
            "approved"
        )
        == []
    )
    assert seen == {"dns_timeout": 5, "request_timeout": 4.75}


def test_search_real_transport_sends_only_json_accept_header(monkeypatch):
    requests = []

    class Socket:
        def settimeout(self, timeout):
            pass

        def close(self):
            pass

    class Connection:
        def __init__(self, host, port, timeout):
            self.sock = None

        def request(self, method, path, *, headers):
            requests.append(headers)

        def getresponse(self):
            return SimpleNamespace(
                status=200,
                getheaders=list,
                read=lambda n: b'{"web":{"results":[]}}',
            )

        def close(self):
            pass

    monkeypatch.setattr(fetcher.http.client, "HTTPConnection", Connection)
    monkeypatch.setattr(fetcher.socket, "create_connection", lambda *a, **k: Socket())
    monkeypatch.setattr(
        fetcher.ssl,
        "create_default_context",
        lambda: SimpleNamespace(wrap_socket=lambda sock, server_hostname: sock),
    )
    monkeypatch.setattr(
        search,
        "validate_target",
        lambda url, resolver, **kwargs: Target(
            url, "api.search.brave.com", "93.184.216.34", "/query"
        ),
    )
    assert (
        BraveSearch(
            transport=fetcher.SocketPublicTransport(), api_key="synthetic"
        ).search("approved")
        == []
    )
    assert len(requests) == 1
    assert [
        value.lower() for key, value in requests[0].items() if key.lower() == "accept"
    ] == ["application/json"]
