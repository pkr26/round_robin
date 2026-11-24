"""Real adapter requests use the configured provider's own synthetic credential."""

import json

import httpx
import pytest
from round_robin.providers.images import HTTPImageProvider, ImageFundingLocked
from round_robin.visual.assets import validate_raster
from round_robin.visual.reviews import (
    PixelView,
    independent_review_packet,
    issue_review_receipt,
)
from round_robin.visual.schemas import ImageCapabilities, VisualBrief

from backend.tests.test_image_provider import authorization, generation_packet, pixels
from backend.tests.test_image_provider import image_configuration as image_configuration  # noqa: PLC0414


@pytest.mark.parametrize("provider,key_name,header", [
    ("openai", "OPENAI_API_KEY", "authorization"),
    ("anthropic", "ANTHROPIC_API_KEY", "x-api-key"),
    ("google", "GEMINI_API_KEY", "x-goog-api-key"),
])
def test_provider_uses_own_environment_credential_and_configured_model(image_configuration, monkeypatch, provider, key_name, header):
    credentials = {"OPENAI_API_KEY": "synthetic-openai-key", "ANTHROPIC_API_KEY": "synthetic-anthropic-key", "GEMINI_API_KEY": "synthetic-google-key"}
    for name, value in credentials.items():
        monkeypatch.setenv(name, value)
    raster = validate_raster(pixels())
    views = [PixelView(raster.canonical_bytes, raster.canonical_hash)]
    capabilities = ImageCapabilities.model_validate(image_configuration["capabilities"][provider])
    receipt = issue_review_receipt(capabilities, "version_one", raster.canonical_hash, views)
    packet = {**independent_review_packet(VisualBrief(purpose="Synthetic review", audience="Test"), receipt, views), "owner_id": "owner_one", "session_id": "session_one", "step_id": "step_one"}
    seen = []
    content = json.dumps({"findings": [], "limitations": ["Synthetic review"]})
    def send(request):
        seen.append(request)
        if provider == "openai":
            response = {"status": "completed", "output": [{"type": "message", "content": [{"type": "output_text", "text": content}]}]}
        elif provider == "anthropic":
            response = {"stop_reason": "end_turn", "content": [{"type": "text", "text": content}]}
        else:
            response = {"candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": content}]}}]}
        return httpx.Response(200, json=response)
    adapter = HTTPImageProvider(provider, image_configuration, transport=httpx.MockTransport(send))
    adapter.review(packet, authorization("vision"))
    assert len(seen) == 1
    expected = credentials[key_name]
    assert seen[0].headers[header] == (f"Bearer {expected}" if provider == "openai" else expected)
    assert all(value not in str(seen[0].url) for value in credentials.values())
    for name, value in credentials.items():
        if name != key_name:
            assert value not in str(seen[0].headers) and value not in seen[0].content.decode()
    if provider == "google":
        assert image_configuration["models"][provider] in seen[0].url.path
    else:
        assert json.loads(seen[0].content)["model"] == image_configuration["models"][provider]


def test_missing_generator_key_fails_before_any_transport(image_configuration, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    sent = []
    adapter = HTTPImageProvider("openai", image_configuration, transport=httpx.MockTransport(lambda request: sent.append(request)))
    with pytest.raises(ImageFundingLocked):
        adapter.submit(generation_packet(), authorization())
    assert sent == []


def test_explicit_synthetic_key_overrides_environment(image_configuration, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-environment-key")
    adapter = HTTPImageProvider("openai", image_configuration, api_key="synthetic-explicit-key")
    assert adapter.api_key == "synthetic-explicit-key"
