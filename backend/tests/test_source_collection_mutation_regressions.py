"""Public-source retrieval preserves bounded transport and evidence semantics."""

import hashlib

import pytest
from round_robin.research import fetcher
from round_robin.research.fetcher import (
    FetchAllowance,
    FetchRejected,
    FetchResponse,
    FetchUnavailable,
)


class RecordingTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, target, **kwargs):
        self.calls.append((target, kwargs))
        return self.responses.pop(0)


def allowance(url="https://example.com/start", *, byte_limit=1048576):
    return FetchAllowance(registered_urls={"candidate": url}, byte_limit=byte_limit)


def public(_):
    return ["8.8.8.8"]


@pytest.mark.parametrize("timeout", [0, -1, 30.01])
def test_invalid_retrieval_timeout_is_rejected_without_consuming_allowance(timeout):
    budget = allowance()
    transport = RecordingTransport([])
    with pytest.raises(FetchRejected):
        fetcher.collect_source("candidate", budget, transport=transport, resolver=public, timeout=timeout)
    assert budget.pages == 0 and transport.calls == []


def test_maximum_timeout_and_exact_byte_allowance_are_accepted(monkeypatch):
    seen = []
    original = fetcher.validate_target

    def validate(url, resolver, *, resolution_timeout=5):
        seen.append(resolution_timeout)
        return original(url, resolver, resolution_timeout=resolution_timeout)

    monkeypatch.setattr(fetcher, "validate_target", validate)
    budget = allowance(byte_limit=8)
    transport = RecordingTransport([FetchResponse(200, {"content-type": "text/plain"}, b"12345678")])
    source = fetcher.collect_source("candidate", budget, transport=transport, resolver=public, timeout=30)
    assert source.text == "12345678" and budget.pages == 1
    assert len(seen) == 1 and 0 < seen[0] <= 30
    assert transport.calls[0][1]["byte_limit"] == 8
    assert 0 < transport.calls[0][1]["timeout"] <= 30


@pytest.mark.parametrize("status", [301, 303, 308])
def test_supported_redirects_are_revalidated_and_followed(status):
    resolved = []
    budget = allowance()
    transport = RecordingTransport([
        FetchResponse(status, {"location": "/final"}, b""),
        FetchResponse(200, {"content-type": "text/plain"}, b"public"),
    ])
    source = fetcher.collect_source("candidate", budget, transport=transport, resolver=lambda host: resolved.append(host) or ["8.8.8.8"])
    assert [target.url for target, _ in transport.calls] == ["https://example.com/start", "https://example.com/final"]
    assert resolved == ["example.com", "example.com"]
    assert source.url == "https://example.com/final" and source.text == "public"
    assert budget.pages == 1


def test_non_redirect_status_does_not_follow_location():
    transport = RecordingTransport([FetchResponse(304, {"location": "/not-a-redirect"}, b"")])
    with pytest.raises(FetchUnavailable):
        fetcher.collect_source("candidate", allowance(), transport=transport, resolver=public)
    assert len(transport.calls) == 1


def test_media_parameters_plaintext_and_source_hash_are_preserved():
    body = b"  <script>literal</script>\r\n\r\n next line  \r\n"
    transport = RecordingTransport([FetchResponse(200, {"content-type": "Text/Plain; charset=utf-8; extra=ok"}, body)])
    source = fetcher.collect_source("candidate", allowance(), transport=transport, resolver=public)
    assert source.text == "<script>literal</script>\nnext line"
    assert source.content_hash == hashlib.sha256(source.text.encode()).hexdigest()
    assert source.origin_group == "origin_" + hashlib.sha256(b"example.com").hexdigest()
    assert source.status == "not_checked"


def test_xhtml_is_extracted_without_active_content():
    transport = RecordingTransport([FetchResponse(200, {"content-type": "application/xhtml+xml; charset=utf-8"}, b"<html><p>First</p><script>private</script><p>Second</p></html>")])
    source = fetcher.collect_source("candidate", allowance(), transport=transport, resolver=public)
    assert source.text == "First\nSecond"
    assert source.content_hash == hashlib.sha256(b"First\nSecond").hexdigest()


def test_missing_media_type_is_rejected_without_uncontrolled_exception():
    transport = RecordingTransport([FetchResponse(200, {}, b"data")])
    with pytest.raises(FetchRejected):
        fetcher.collect_source("candidate", allowance(), transport=transport, resolver=public)


def test_deadline_expires_before_dns_and_at_dns_completion(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(fetcher.time, "monotonic", lambda: clock[0])
    transport = RecordingTransport([])
    real_validate = fetcher.validate_target

    def expired_before_dns(*args, **kwargs):
        pytest.fail("Expired requests must not resolve DNS")

    clock[0] = 100.0
    ticks = iter((100.0, 110.0))
    monkeypatch.setattr(fetcher.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(fetcher, "validate_target", expired_before_dns)
    with pytest.raises(FetchUnavailable):
        fetcher.collect_source("candidate", allowance(), transport=transport, resolver=public, timeout=10)

    clock[0] = 100.0
    monkeypatch.setattr(fetcher.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(fetcher, "validate_target", real_validate)

    def consume_deadline(host):
        clock[0] = 110.0
        return ["8.8.8.8"]

    with pytest.raises(FetchUnavailable):
        fetcher.collect_source("candidate", allowance(), transport=transport, resolver=consume_deadline, timeout=10)
    assert transport.calls == []


def test_dns_validation_receives_the_short_remaining_source_deadline(monkeypatch):
    original = fetcher.validate_target
    timeouts = []

    def validate(url, resolver, *, resolution_timeout=5):
        timeouts.append(resolution_timeout)
        return original(url, resolver, resolution_timeout=resolution_timeout)

    monkeypatch.setattr(fetcher, "validate_target", validate)
    transport = RecordingTransport([FetchResponse(200, {"content-type": "text/plain"}, b"public")])
    assert fetcher.collect_source("candidate", allowance(), transport=transport, resolver=public, timeout=0.5).text == "public"
    assert len(timeouts) == 1 and 0 < timeouts[0] <= 0.5
