"""Image transport must keep private requests isolated and responses bounded."""

import time

import httpx
import pytest
from round_robin.providers import images
from round_robin.providers.base import ProviderFailure
from round_robin.providers.images import HTTPImageProvider


def adapter(handler):
    instance = object.__new__(HTTPImageProvider)
    instance.transport = httpx.MockTransport(handler)
    return instance


def test_image_http_disables_proxy_inheritance_redirects_and_uses_timeout(monkeypatch):
    client = httpx.Client
    options = []

    def capture_client(*args, **kwargs):
        options.append(kwargs.copy())
        return client(*args, **kwargs)

    monkeypatch.setattr(images.httpx, "Client", capture_client)
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.invalid:9999")
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={"ok": True})

    result, request_id = adapter(respond)._http(
        "https://provider.invalid/images", {"Authorization": "Bearer synthetic-secret"},
        {"pixels": "synthetic-private-pixels"}, timeout=8, maximum_bytes=100,
    )
    assert result == {"ok": True} and request_id is None
    assert len(options) == len(requests) == 1
    assert not options[0].get("trust_env", True)
    assert not options[0].get("follow_redirects", False)
    assert options[0]["timeout"] == 8
    assert requests[0].method == "POST"
    assert requests[0].headers["authorization"] == "Bearer synthetic-secret"
    assert requests[0].headers["accept-encoding"].lower() == "identity"


def test_image_http_default_transport_gets_absolute_deadline_without_opening_a_socket(monkeypatch):
    deadlines = []

    def simulated_transport(deadline):
        deadlines.append(deadline)
        return httpx.MockTransport(lambda request: httpx.Response(200, json={"ok": True}))

    monkeypatch.setattr(images, "DeadlineHTTPTransport", simulated_transport)
    instance = object.__new__(HTTPImageProvider)
    instance.transport = None
    before = time.monotonic()
    assert instance._http("https://provider.invalid", {}, {}, timeout=8, maximum_bytes=100)[0] == {"ok": True}
    after = time.monotonic()
    assert len(deadlines) == 1 and before + 8 <= deadlines[0] <= after + 8


@pytest.mark.parametrize("status", [300, 301, 307, 308])
def test_image_http_never_forwards_private_body_on_redirect(status):
    requests = []

    def respond(request):
        requests.append(request)
        if len(requests) > 1:
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(status, headers={"location": "https://elsewhere.invalid/collect"})

    with pytest.raises(ProviderFailure) as error:
        adapter(respond)._http("https://provider.invalid/images", {"Authorization": "Bearer synthetic-secret"},
                               {"pixels": "synthetic-private-pixels"}, timeout=1, maximum_bytes=100)
    assert error.value.code == "provider_rejected_request"
    assert len(requests) == 1


@pytest.mark.parametrize(
    ("status", "expected", "retryable"),
    [(401, "authentication", False), (403, "authentication", False), (429, "rate_limit", True),
     (500, "transient", True), (503, "transient", True)],
)
def test_image_http_failure_classification_and_safe_request_id(status, expected, retryable):
    response = httpx.Response(status, headers={"request-id": "MixedCase_Request-42"}, content=b"private response")
    with pytest.raises(ProviderFailure) as error:
        adapter(lambda request: response)._http("https://provider.invalid", {}, {}, timeout=1, maximum_bytes=100)
    assert (error.value.code, error.value.retryable, error.value.provider_request_id) == (expected, retryable, "MixedCase_Request-42")
    assert "private response" not in str(error.value)


@pytest.mark.parametrize("status", [400, 422])
@pytest.mark.parametrize("code", ["content_policy_violation", "moderation_blocked", "safety", "refusal"])
def test_image_http_does_not_retry_provider_safety_refusals(status, code):
    response = httpx.Response(status, json={"error": {"code": code, "message": "private response"}})
    with pytest.raises(ProviderFailure) as error:
        adapter(lambda request: response)._http("https://provider.invalid", {}, {}, timeout=1, maximum_bytes=100)
    assert error.value.code == "refusal" and not error.value.retryable
    assert "private response" not in str(error.value)


class Chunked(httpx.SyncByteStream):
    def __iter__(self):
        yield b'{"ok":'
        yield b'true}'


def test_image_http_enforces_cumulative_response_bytes_at_exact_limit():
    def respond(request):
        return httpx.Response(200, stream=Chunked())

    assert adapter(respond)._http("https://provider.invalid", {}, {}, timeout=1, maximum_bytes=11)[0] == {"ok": True}
    with pytest.raises(ProviderFailure) as error:
        adapter(respond)._http("https://provider.invalid", {}, {}, timeout=1, maximum_bytes=10)
    assert error.value.code == "response_limit"


class UnreadableCompressedBody(httpx.SyncByteStream):
    def __iter__(self):
        raise AssertionError("Compressed bytes must be rejected before the response body is read")
        yield b""


def test_image_http_rejects_compression_before_reading_body_and_retains_safe_id():
    response = httpx.Response(200, headers={"content-encoding": "gzip", "x-request-id": "Safe_ID-42"}, stream=UnreadableCompressedBody())
    with pytest.raises(ProviderFailure) as error:
        adapter(lambda request: response)._http("https://provider.invalid", {}, {}, timeout=1, maximum_bytes=100)
    assert error.value.code == "unsupported_response_encoding"
    assert error.value.provider_request_id == "Safe_ID-42"


@pytest.mark.parametrize(("request_id", "expected"), [("Safe_ID-42", "Safe_ID-42"), ("untrusted private token", None)])
def test_image_http_malformed_response_does_not_disclose_body_or_unsafe_request_id(request_id, expected):
    response = httpx.Response(200, headers={"x-request-id": request_id}, content=b"private malformed response")
    with pytest.raises(ProviderFailure) as error:
        adapter(lambda request: response)._http("https://provider.invalid", {}, {}, timeout=1, maximum_bytes=100)
    assert error.value.code == "image_transport_or_response_unknown"
    assert error.value.provider_request_id == expected
    assert "private" not in str(error.value)
