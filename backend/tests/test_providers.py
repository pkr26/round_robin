from decimal import Decimal
import json

import httpx
import pytest

from round_robin.providers.base import ProviderFailure, ProviderOutput, ProviderRequest
from round_robin.providers.http import HTTPProvider
from round_robin.providers.simulated import SimulatedProvider
from round_robin.fixtures import sample_brief


def packet(stage="proposal", peers=None):
    return ProviderRequest("run_test", "step_test", stage, stage, sample_brief().model_dump(mode="json"), [], peers or [], frozenset({"evidence_owned"}), 2048, 20)


def configuration(provider):
    return {"mode": "live", "funded": True, "models": {provider: "operator-configured-model"}, "anthropic_api_version": "2026-10-21", "capabilities": {provider: {"structured_text": True}}, "pricing": {provider: {"version": "synthetic-test-prices", "input_usd_per_million": "2", "output_usd_per_million": "10", "billable_output_bound_tokens": 4096, "reasoning_exposure_bounded": True}}}


def output():
    return ProviderOutput(summary="Supplied facts remain conditional.", recommendation_option_id="option_a", reference_ids=["evidence_owned"], questions=[], assumptions=[], constraints=[], audit_passed=None, critical_defects=[], dissent=[]).model_dump(mode="json")


def response_for(provider, content=None):
    text = json.dumps(content or output())
    if provider == "openai":
        return {"status": "completed", "output": [{"type": "message", "content": [{"type": "output_text", "text": text}]}], "usage": {"input_tokens": 100, "output_tokens": 50}}
    if provider == "anthropic":
        return {"stop_reason": "end_turn", "content": [{"type": "text", "text": text}], "usage": {"input_tokens": 100, "output_tokens": 50}}
    return {"candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": text}]}}], "usageMetadata": {"promptTokenCount": 100, "totalTokenCount": 150}}


@pytest.mark.parametrize("provider", ["openai", "anthropic", "google"])
def test_http_adapter_exactly_one_bounded_request_and_sanitized_secret_location(provider):
    received = []
    def send(request):
        received.append(request)
        assert request.method == "POST"
        assert "sentinel-secret" not in str(request.url)
        assert "sentinel-secret" not in request.content.decode()
        body = json.loads(request.content)
        assert body["model"] == "operator-configured-model" if provider != "google" else "generateContent" in request.url.path
        return httpx.Response(200, headers={"x-request-id": "provider-request-owned-123"}, json=response_for(provider))
    adapter = HTTPProvider(provider, configuration(provider), transport=httpx.MockTransport(send), api_key="sentinel-secret")
    assert adapter.conservative_bound(packet()) > Decimal("0")
    result = adapter.generate(packet())
    assert result.output.recommendation_option_id == "option_a"
    assert result.cost_usd == Decimal("0.0007")
    assert result.provider_request_id == "provider-request-owned-123"
    assert len(received) == 1


@pytest.mark.parametrize("status,code,retryable", [(401, "authentication", False), (402, "credit_exhausted", False), (404, "model_unavailable", False), (429, "rate_limit", True), (503, "transient", True)])
def test_http_errors_do_not_hide_automatic_retries(status, code, retryable):
    requests = []
    def send(request):
        requests.append(request)
        return httpx.Response(status, headers={"retry-after": "7"}, json={"error": {"message": "private-sentinel-error"}})
    adapter = HTTPProvider("openai", configuration("openai"), transport=httpx.MockTransport(send), api_key="synthetic")
    with pytest.raises(ProviderFailure) as error:
        adapter.generate(packet())
    assert error.value.code == code and error.value.retryable is retryable
    assert len(requests) == 1 and "sentinel" not in str(error.value)


def test_funding_and_unknown_price_block_transport_before_send():
    calls = []
    adapter = HTTPProvider("openai", {"mode": "live", "funded": False}, transport=httpx.MockTransport(lambda request: calls.append(request)), api_key="synthetic")
    with pytest.raises(ProviderFailure, match="funding_locked"):
        adapter.generate(packet())
    config = configuration("openai")
    config["pricing"]["openai"].pop("billable_output_bound_tokens")
    adapter.configuration = config
    adapter.model = config["models"]["openai"]
    adapter.pricing = config["pricing"]["openai"]
    with pytest.raises(ProviderFailure, match="pricing_unavailable"):
        adapter.generate(packet())
    assert calls == []


@pytest.mark.parametrize("provider", ["openai", "anthropic", "google"])
def test_explicit_refusals_never_become_schema_repairs(provider):
    raw = response_for(provider)
    if provider == "openai":
        raw["output"][0]["content"] = [{"type": "refusal", "refusal": "No"}]
    elif provider == "anthropic":
        raw["stop_reason"] = "refusal"
    else:
        raw["candidates"][0]["finishReason"] = "SAFETY"
    adapter = HTTPProvider(provider, configuration(provider), transport=httpx.MockTransport(lambda request: httpx.Response(200, json=raw)), api_key="synthetic")
    with pytest.raises(ProviderFailure, match="refusal") as error:
        adapter.generate(packet())
    assert not error.value.retryable


def test_missing_usage_preserves_valid_output_as_unknown_and_references_are_checked():
    raw = response_for("openai")
    raw.pop("usage")
    adapter = HTTPProvider("openai", configuration("openai"), transport=httpx.MockTransport(lambda request: httpx.Response(200, json=raw)), api_key="synthetic")
    assert adapter.generate(packet()).cost_usd is None
    bad = output()
    bad["reference_ids"] = ["foreign-run-evidence"]
    adapter.transport = httpx.MockTransport(lambda request: httpx.Response(200, json=response_for("openai", bad)))
    with pytest.raises(ProviderFailure, match="invalid_reference"):
        adapter.generate(packet())


def test_simulator_uses_current_arbitrary_brief_and_proposals_never_receive_peers():
    request = packet()
    request.brief["title"] = "A different supplier comparison"
    result = SimulatedProvider("google").generate(request)
    assert "A different supplier comparison" in result.output.summary and result.simulated
    with pytest.raises(ProviderFailure, match="independence_violation"):
        SimulatedProvider("google").generate(packet(peers=[{"summary": "not independent"}]))


def test_schema_valid_but_unscoped_content_is_not_an_authoritative_proposal():
    content = output()
    content.update(summary="An unrelated answer without option or evidence content.", recommendation_option_id=None, reference_ids=[], constraints=[])
    adapter = HTTPProvider("openai", configuration("openai"), transport=httpx.MockTransport(lambda request: httpx.Response(200, json=response_for("openai", content))), api_key="synthetic")
    with pytest.raises(ProviderFailure, match="substantive_content_unavailable") as error:
        adapter.generate(packet())
    assert error.value.retryable is False
