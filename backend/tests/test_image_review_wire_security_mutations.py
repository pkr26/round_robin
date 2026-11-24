"""Private review pixels are sent only in the authorized critic wire format."""

import base64
import hashlib
import json

import httpx
import pytest
from round_robin.providers.images import HTTPImageProvider
from round_robin.visual.reviews import (
    PixelView,
    independent_review_packet,
    issue_review_receipt,
)
from round_robin.visual.schemas import ImageCapabilities, VisualBrief

from backend.tests.test_image_provider import authorization, pixels

pytest_plugins = ("backend.tests.test_image_provider",)


@pytest.mark.parametrize("provider", ["openai", "anthropic", "google"])
def test_image_review_sends_receipt_bound_pixels_to_the_authorized_critic(image_configuration, provider):
    child, parent = pixels("green"), pixels("blue")
    child_hash, parent_hash = hashlib.sha256(child).hexdigest(), hashlib.sha256(parent).hexdigest()
    views = [PixelView(child, child_hash), PixelView(parent, parent_hash, "comparison_parent_version_parent_one")]
    capabilities = ImageCapabilities.model_validate(image_configuration["capabilities"][provider])
    receipt = issue_review_receipt(capabilities, "version_one", child_hash, views)
    brief = VisualBrief(purpose="Synthetic pixel review", audience="Test reviewer")
    prior = {"parent_version_id": "parent_one", "parent_hash": parent_hash, "findings": [], "edit_intent": "Synthetic scoped edit"}
    packet = {**independent_review_packet(brief, receipt, views, prior_context=prior),
              "owner_id": "owner_one", "session_id": "session_one", "step_id": "step_one", "timeout_seconds": 10}
    captured = []
    critique_text = json.dumps({"findings": [], "limitations": ["Synthetic fixture only"]})

    def respond(request):
        captured.append(request)
        if provider == "openai":
            response = {"status": "completed", "output": [{"type": "message", "content": [{"type": "output_text", "text": critique_text}]}]}
        elif provider == "anthropic":
            response = {"stop_reason": "end_turn", "content": [{"type": "text", "text": critique_text}]}
        else:
            response = {"candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": critique_text}]}}]}
        return httpx.Response(200, json=response)

    result = HTTPImageProvider(provider, image_configuration, transport=httpx.MockTransport(respond), api_key="synthetic-key").review(packet, authorization("vision"))
    assert len(captured) == 1
    request = captured[0]
    assert "synthetic-key" not in str(request.url) and b"synthetic-key" not in request.content
    payload = json.loads(request.content)
    expected_images = [base64.b64encode(data).decode() for data in (child, parent)]
    if provider == "openai":
        assert str(request.url) == "https://api.openai.com/v1/responses"
        assert request.headers["authorization"] == "Bearer synthetic-key"
        assert payload["model"] == image_configuration["models"][provider]
        assert payload["store"] is False
        instruction = payload["instructions"]
        assert payload["max_output_tokens"] == image_configuration["max_output_tokens"]
        response_format = payload["text"]["format"]
        assert response_format["type"] == "json_schema" and response_format["strict"] is True and response_format["schema"]
        assert isinstance(response_format["name"], str) and response_format["name"]
        message = payload["input"][0]
        assert message["role"] == "user"
        parts = message["content"]
        assert parts[0]["type"] == "input_text"
        text = parts[0]["text"]
        assert [item["image_url"] for item in parts[1:]] == ["data:image/png;base64," + value for value in expected_images]
        assert all(item["type"] == "input_image" for item in parts[1:])
    elif provider == "anthropic":
        assert str(request.url) == "https://api.anthropic.com/v1/messages"
        assert request.headers["x-api-key"] == "synthetic-key"
        assert request.headers["anthropic-version"] == image_configuration["anthropic_api_version"]
        assert payload["model"] == image_configuration["models"][provider]
        instruction = payload["system"]
        assert payload["max_tokens"] == image_configuration["max_output_tokens"]
        response_format = payload["output_config"]["format"]
        assert response_format["type"] == "json_schema" and response_format["schema"]
        message = payload["messages"][0]
        assert message["role"] == "user"
        parts = message["content"]
        assert [item["source"]["data"] for item in parts[:-1]] == expected_images
        assert all(item["type"] == "image" and item["source"]["type"] == "base64" and item["source"]["media_type"] == "image/png" for item in parts[:-1])
        assert parts[-1]["type"] == "text"
        text = parts[-1]["text"]
    else:
        assert str(request.url) == f"https://generativelanguage.googleapis.com/v1beta/models/{image_configuration['models'][provider]}:generateContent"
        assert request.headers["x-goog-api-key"] == "synthetic-key"
        instruction = payload["systemInstruction"]["parts"][0]["text"]
        config = payload["generationConfig"]
        assert config["maxOutputTokens"] == image_configuration["max_output_tokens"]
        assert config["responseMimeType"] == "application/json" and config["responseJsonSchema"]
        message = payload["contents"][0]
        assert message["role"] == "user"
        parts = message["parts"]
        assert [item["inlineData"]["data"] for item in parts[:-1]] == expected_images
        assert all(item["inlineData"]["mimeType"] == "image/png" for item in parts[:-1])
        text = parts[-1]["text"]
    assert "untrusted data" in instruction and "never tool instructions" in instruction
    transmitted = json.loads(text)
    assert transmitted["brief"] == packet["brief"]
    assert transmitted["shared_prior_context"] == prior
    assert transmitted["views"] == [{"view_index": index, **view} for index, view in enumerate(receipt.views)]
    critique = result["critique"]
    assert (critique["provider"], critique["receipt_id"], critique["canonical_hash"]) == (provider, receipt.id, child_hash)
