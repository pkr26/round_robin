"""Public search preserves request, response and deadline boundaries."""

import json
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
from round_robin.research import search as search_module
from round_robin.research.fetcher import FetchRejected, FetchResponse, FetchUnavailable
from round_robin.research.search import BraveSearch


class RecordingTransport:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def get(self, target, **kwargs):
        self.calls.append((target, kwargs))
        return self.response


def fake_validation(monkeypatch):
    calls = []

    def validate(url, resolver, *, resolution_timeout=5):
        calls.append((url, resolver, resolution_timeout))
        return SimpleNamespace(url=url)

    monkeypatch.setattr(search_module, "validate_target", validate)
    return calls


def test_search_sends_only_approved_query_and_bounded_private_request(monkeypatch):
    validation = fake_validation(monkeypatch)
    transport = RecordingTransport(FetchResponse(200, {}, b'{"web":{"results":[]}}'))
    resolver = lambda host: ["93.184.216.34"]
    search = BraveSearch(transport=transport, resolver=resolver, api_key="SYNTHETIC_KEY")

    assert search.search("approved public terms", timeout=2) == []
    assert len(validation) == 1
    url, used_resolver, dns_timeout = validation[0]
    parts = urlsplit(url)
    assert (parts.scheme, parts.netloc, parts.path) == ("https", "api.search.brave.com", "/res/v1/web/search")
    assert parse_qs(parts.query) == {"q": ["approved public terms"], "count": ["8"], "text_decorations": ["false"]}
    assert used_resolver is resolver and dns_timeout == 2
    target, kwargs = transport.calls[0]
    assert target.url == url and "SYNTHETIC_KEY" not in url
    assert 0 < kwargs["timeout"] <= 2
    assert kwargs["byte_limit"] == 262144
    headers = {key.lower(): value for key, value in kwargs["private_headers"].items()}
    assert headers["accept"].lower() == "application/json"
    assert headers["x-subscription-token"] == "SYNTHETIC_KEY"


@pytest.mark.parametrize("query", ["x" * 600, " ".join(["x"] * 75)])
def test_search_accepts_query_limits_inclusively(monkeypatch, query):
    fake_validation(monkeypatch)
    transport = RecordingTransport(FetchResponse(200, {}, b'{"web":{"results":[]}}'))
    assert BraveSearch(transport=transport, api_key="synthetic").search(query) == []
    assert len(transport.calls) == 1


def test_search_fails_closed_without_credentials_and_uses_explicit_or_environment_key(monkeypatch):
    fake_validation(monkeypatch)
    transport = RecordingTransport(FetchResponse(200, {}, b'{"web":{"results":[]}}'))
    monkeypatch.delenv("BRAVE_SEARCH_API_KEY", raising=False)
    with pytest.raises(FetchRejected):
        BraveSearch(transport=transport).search("approved")
    assert transport.calls == []
    monkeypatch.setenv("BRAVE_SEARCH_API_KEY", "SYNTHETIC_ENV_KEY")
    assert BraveSearch(transport=transport).search("approved") == []
    assert transport.calls[-1][1]["private_headers"]["X-Subscription-Token"] == "SYNTHETIC_ENV_KEY"
    with pytest.raises(FetchRejected):
        BraveSearch(transport=transport, api_key="").search("approved")
    assert len(transport.calls) == 1


def test_search_expires_at_deadline_but_uses_positive_remaining_time(monkeypatch):
    fake_validation(monkeypatch)
    clock = [100.0]
    monkeypatch.setattr(search_module.time, "monotonic", lambda: clock[0])
    transport = RecordingTransport(FetchResponse(200, {}, b'{"web":{"results":[]}}'))
    search = BraveSearch(transport=transport, api_key="synthetic")
    with pytest.raises(FetchUnavailable):
        search.search("approved", timeout=0)
    assert transport.calls == []

    ticks = iter((100.0, 104.75))
    monkeypatch.setattr(search_module.time, "monotonic", lambda: next(ticks))
    assert search.search("approved", timeout=5) == []
    assert transport.calls[0][1]["timeout"] == 0.25


def test_search_response_status_and_byte_limits_are_enforced(monkeypatch):
    fake_validation(monkeypatch)
    body = b'{"web":{"results":[]}}'
    exact = body + b" " * (262144 - len(body))
    transport = RecordingTransport(FetchResponse(200, {}, exact))
    search = BraveSearch(transport=transport, api_key="synthetic")
    assert search.search("approved") == []
    for response in (FetchResponse(503, {}, body), FetchResponse(200, {}, exact + b" ")):
        transport.response = response
        with pytest.raises(FetchUnavailable):
            search.search("approved")


def test_search_results_are_typed_limited_and_preserve_public_content(monkeypatch):
    fake_validation(monkeypatch)
    entries = [{"title": "T" * 501, "url": "https://example.com/" + "p" * 2049}, {"url": "https://example.com/no-title"}]
    entries.extend({"title": str(i), "url": f"https://example.com/{i}"} for i in range(7))
    transport = RecordingTransport(FetchResponse(200, {}, json.dumps({"web": {"results": entries}}).encode()))
    search = BraveSearch(transport=transport, api_key="synthetic")
    hits = search.search("approved")
    assert len(hits) == 8
    assert hits[0].title == "T" * 500 and hits[0].url == entries[0]["url"][:2048]
    assert hits[1].title == "" and hits[1].url == entries[1]["url"]
    for payload in ({}, {"web": {}}, {"web": {"results": []}}):
        transport.response = FetchResponse(200, {}, json.dumps(payload).encode())
        assert search.search("approved") == []
    transport.response = FetchResponse(200, {}, b'{"web":{"results":[{"url":["not-a-url"],"title":"Title"}]}}')
    with pytest.raises(FetchUnavailable):
        search.search("approved")
