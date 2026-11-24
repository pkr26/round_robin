"""Image and actual-PNG vision contracts through injected HTTP transports."""
from dataclasses import asdict
from datetime import timedelta
import base64
from io import BytesIO
import json

import httpx
from PIL import Image
import pytest

from round_robin.database.models import utcnow
from round_robin.providers.base import ProviderFailure
from round_robin.providers.images import HTTPImageProvider, ImageAuthorization, ImageFundingLocked, load_image_configuration
from round_robin.visual.assets import validate_raster
from round_robin.visual.reviews import PixelView, independent_review_packet, issue_review_receipt
from round_robin.visual.schemas import ImageCapabilities, VisualBrief


def pixels(color="green"):
    output = BytesIO()
    Image.new("RGB", (64, 64), color).save(output, format="PNG")
    return validate_raster(output.getvalue()).canonical_bytes


def synthetic_image_configuration():
    providers = ("openai", "anthropic", "google")
    return {"version": "synthetic-image-config-v1", "funded": True, "expires_at": (utcnow() + timedelta(hours=1)).isoformat(), "generator_provider": "openai", "generator_model": "synthetic-image-model", "models": {provider: f"synthetic-{provider}-vision" for provider in providers}, "capabilities": {provider: {"provider": provider, "endpoint_version": "synthetic-endpoint-v1", "understands_images": True, "generates_images": provider == "openai", "edits_images": provider == "openai", "supported_media": ["image/png"]} for provider in providers}, "critic_providers": ["anthropic", "google"], "vision_prices": {provider: "0.10" for provider in providers}, "generation_price_usd": "0.40", "edit_price_usd": "0.40", "maximum_usd": "2", "supported_sizes": ["64x64"], "quality": "low", "anthropic_api_version": "2026-10-21", "reasoning_exposure_bounded": True}


@pytest.fixture
def image_configuration(tmp_path, monkeypatch):
    path = tmp_path / "image-config.json"
    path.write_text(json.dumps(synthetic_image_configuration()))
    monkeypatch.setenv("ROUND_ROBIN_PAID_ACCESS", "1")
    monkeypatch.setenv("ROUND_ROBIN_IMAGE_CONFIG_FILE", str(path))
    return load_image_configuration()


def authorization(kind="generation"):
    return ImageAuthorization("operation_one", "owner_one", "session_one", "0.40" if kind != "vision" else "0.10", 0, 1, "step_one")


def generation_packet(kind="generation"):
    return {"kind": kind, "owner_id": "owner_one", "session_id": "session_one", "step_id": "step_one", "brief": VisualBrief(purpose="Synthetic bitmap fixture", audience="Test reviewer", width=64, height=64).model_dump(mode="json"), "timeout_seconds": 10, **({"images": [pixels("blue")], "edit_intent": ["Preserve required text; change the background."]} if kind == "edit" else {})}


@pytest.mark.parametrize("kind", ["generation", "edit"])
def test_configured_openai_image_operation_sends_exactly_one_request_and_validates_pixels(image_configuration, kind):
    requests = []
    def send(request):
        requests.append(request)
        body = json.loads(request.content)
        assert body["model"] == "synthetic-image-model" and body["n"] == 1 and body["output_format"] == "png"
        assert "synthetic-key-secret" not in str(request.url) and "synthetic-key-secret" not in request.content.decode()
        if kind == "edit":
            assert body["images"][0]["image_url"] == "data:image/png;base64," + base64.b64encode(pixels("blue")).decode()
        return httpx.Response(200, headers={"x-request-id": "image_request_123"}, json={"data": [{"b64_json": base64.b64encode(pixels()).decode()}], "usage": {"input_tokens": 3, "output_tokens": 7}})
    adapter = HTTPImageProvider("openai", image_configuration, transport=httpx.MockTransport(send), api_key="synthetic-key-secret")
    result = adapter.submit(generation_packet(kind), authorization(kind), idempotency_key=None)
    assert len(requests) == 1 and result["image_bytes"] == pixels()
    assert result["actual_usd"] is None and result["provider_request_id"] == "image_request_123"


@pytest.mark.parametrize("provider", ["openai", "anthropic", "google"])
def test_all_three_vision_adapters_send_actual_same_png_and_server_bind_findings(image_configuration, provider):
    raw_pixels = pixels()
    raster = validate_raster(raw_pixels)
    capabilities = ImageCapabilities.model_validate(image_configuration["capabilities"][provider])
    views = [PixelView(raw_pixels, raster.canonical_hash)]
    receipt = issue_review_receipt(capabilities, "version_one", raster.canonical_hash, views)
    packet = {**independent_review_packet(VisualBrief(purpose="Synthetic pixel review", audience="Test"), receipt, views), "owner_id": "owner_one", "session_id": "session_one", "step_id": "step_one", "timeout_seconds": 10}
    received = []
    content = {"findings": [], "limitations": ["Synthetic fixture; no actual quality judgment."]}
    def send(request):
        body = json.loads(request.content)
        received.append(body)
        encoded = base64.b64encode(raw_pixels).decode()
        assert encoded in request.content.decode()
        assert "peer_findings" not in request.content.decode()
        text = json.dumps(content)
        if provider == "openai":
            response = {"status": "completed", "output": [{"type": "message", "content": [{"type": "output_text", "text": text}]}]}
        elif provider == "anthropic":
            response = {"stop_reason": "end_turn", "content": [{"type": "text", "text": text}]}
        else:
            response = {"candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": text}]}}]}
        return httpx.Response(200, json=response)
    result = HTTPImageProvider(provider, image_configuration, transport=httpx.MockTransport(send), api_key="synthetic-key").review(packet, authorization("vision"))
    critique = result["critique"]
    assert len(received) == 1
    assert (critique["provider"], critique["version_id"], critique["receipt_id"], critique["canonical_hash"]) == (provider, "version_one", receipt.id, raster.canonical_hash)


def test_image_funding_configuration_and_owner_scope_fail_before_transport(image_configuration):
    sent = []
    adapter = HTTPImageProvider("openai", image_configuration, transport=httpx.MockTransport(lambda request: sent.append(request)), api_key="synthetic")
    packet = generation_packet()
    packet["owner_id"] = "owner_other"
    with pytest.raises(ImageFundingLocked, match="owner/session/step"):
        adapter.submit(packet, authorization(), idempotency_key=None)
    assert sent == []


def test_image_refusal_and_url_only_result_never_regenerate_or_fetch(image_configuration):
    sent = []
    def refusal(request):
        sent.append(request)
        return httpx.Response(400, json={"error": {"code": "content_policy_violation", "message": "private-sentinel"}})
    adapter = HTTPImageProvider("openai", image_configuration, transport=httpx.MockTransport(refusal), api_key="synthetic")
    with pytest.raises(ProviderFailure, match="refusal") as error:
        adapter.submit(generation_packet(), authorization(), idempotency_key=None)
    assert not error.value.retryable and len(sent) == 1
    adapter.transport = httpx.MockTransport(lambda request: httpx.Response(200, json={"data": [{"url": "https://unapproved.invalid/private-token"}]}))
    with pytest.raises(ProviderFailure, match="invalid_image_result"):
        adapter.submit(generation_packet(), authorization(), idempotency_key=None)
    with pytest.raises(ImageFundingLocked, match="no recoverable"):
        adapter.lookup("invented_async_job", authorization())
