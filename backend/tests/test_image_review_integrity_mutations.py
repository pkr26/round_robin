"""Critic responses retain bounds, uncertainty, receipt IDs and limitations."""

import hashlib
import json

import httpx
import pytest
from round_robin.providers.base import ProviderFailure
from round_robin.providers.images import HTTPImageProvider, ImageFundingLocked
from round_robin.visual.reviews import (
    PixelView,
    independent_review_packet,
    issue_review_receipt,
)
from round_robin.visual.schemas import ImageCapabilities, VisualBrief

from backend.tests.test_image_provider import authorization, pixels

pytest_plugins = ("backend.tests.test_image_provider",)


def review_packet(configuration):
    child, parent = pixels("green"), pixels("blue")
    child_hash, parent_hash = hashlib.sha256(child).hexdigest(), hashlib.sha256(parent).hexdigest()
    views = [PixelView(child, child_hash), PixelView(parent, parent_hash, "comparison_parent_version_parent_one")]
    capabilities = ImageCapabilities.model_validate(configuration["capabilities"]["openai"])
    receipt = issue_review_receipt(capabilities, "version_one", child_hash, views)
    prior = {"parent_version_id": "parent_one", "parent_hash": parent_hash, "findings": [], "edit_intent": "Synthetic edit"}
    packet = independent_review_packet(VisualBrief(purpose="Synthetic review", audience="Tester"), receipt, views, prior_context=prior)
    return {**packet, "owner_id": "owner_one", "session_id": "session_one", "step_id": "step_one"}, receipt


def openai_response(text, *, usage=None):
    return {"status": "completed", "output": [{"type": "message", "content": [{"type": "output_text", "text": text}]}], "usage": usage or {}}


def test_image_review_preserves_reported_limitations_regression_and_usage(image_configuration):
    packet, receipt = review_packet(image_configuration)
    content = {"findings": [], "limitations": ["Comparison is uncertain"],
               "regression_checks": [{"target_id": "finding_one", "result": "uncertain", "reason": "Parent detail is unclear"}]}
    usage = {"input_tokens": 14, "output_tokens": 5}
    response = openai_response(json.dumps(content), usage=usage)
    adapter = HTTPImageProvider("openai", image_configuration, transport=httpx.MockTransport(
        lambda request: httpx.Response(200, headers={"x-request-id": "request_one"}, json=response)), api_key="synthetic")
    result = adapter.review(packet, authorization("vision"))
    assert result["status"] == "completed" and result["provider_request_id"] == "request_one"
    assert result["actual_usd"] is None and result["usage"] == usage
    assert result["critique"]["receipt_id"] == receipt.id
    assert result["critique"]["limitations"] == content["limitations"]
    assert result["critique"]["regression_checks"] == content["regression_checks"]


def test_image_review_rejects_unsupported_png_before_transport(image_configuration):
    packet, _ = review_packet(image_configuration)
    sent = []

    def respond(request):
        sent.append(request)
        return httpx.Response(200, json=openai_response(json.dumps({"findings": [], "limitations": []})))

    adapter = HTTPImageProvider("openai", image_configuration, transport=httpx.MockTransport(respond), api_key="synthetic")
    adapter.capabilities = adapter.capabilities.model_copy(update={"supported_media": ["image/jpeg"]})
    with pytest.raises(ImageFundingLocked):
        adapter.review(packet, authorization("vision"))
    assert sent == []


def test_image_review_rechecks_parent_context_after_preflight(image_configuration, monkeypatch):
    packet, _ = review_packet(image_configuration)
    sent = []

    def respond(request):
        sent.append(request)
        return httpx.Response(200, json=openai_response(json.dumps({"findings": [], "limitations": []})))

    adapter = HTTPImageProvider("openai", image_configuration, transport=httpx.MockTransport(respond), api_key="synthetic")
    original_preflight = adapter.preflight

    def preflight_then_change_parent(request, authorized):
        original_preflight(request, authorized)
        # The request is a shallow copy; simulate the caller changing the
        # shared parent context after the initial check but before sending.
        request["shared_prior_context"]["parent_hash"] = "0" * 64

    monkeypatch.setattr(adapter, "preflight", preflight_then_change_parent)
    with pytest.raises(ValueError):
        adapter.review(packet, authorization("vision"))
    assert sent == []


def test_invalid_critic_schema_keeps_request_id_and_retry_classification(image_configuration):
    packet, _ = review_packet(image_configuration)
    response = openai_response('{"findings":"synthetic-private-invalid","limitations":[]}')
    adapter = HTTPImageProvider("openai", image_configuration, transport=httpx.MockTransport(
        lambda request: httpx.Response(200, headers={"x-request-id": "request_one"}, json=response)), api_key="synthetic")
    with pytest.raises(ProviderFailure) as error:
        adapter.review(packet, authorization("vision"))
    assert error.value.code == "schema_invalid" and error.value.retryable is True
    assert error.value.provider_request_id == "request_one"
    assert "synthetic-private-invalid" not in str(error.value)


@pytest.mark.parametrize(("requested", "expected"), [(10, 10), (100, 90), (None, 90)])
def test_image_review_uses_bounded_timeout_and_response_size(image_configuration, monkeypatch, requested, expected):
    packet, _ = review_packet(image_configuration)
    if requested is not None:
        packet["timeout_seconds"] = requested
    adapter = HTTPImageProvider("openai", image_configuration, api_key="synthetic")
    observed = []

    def fake_http(url, headers, payload, *, timeout, maximum_bytes):
        observed.append((timeout, maximum_bytes))
        return openai_response(json.dumps({"findings": [], "limitations": []})), "request_one"

    monkeypatch.setattr(adapter, "_http", fake_http)
    result = adapter.review(packet, authorization("vision"))
    assert result["status"] == "completed"
    assert observed == [(expected, 2 * 1024 * 1024)]
