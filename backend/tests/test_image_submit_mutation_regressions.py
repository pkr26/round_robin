"""Generator request, immutable edit, and result validation contracts."""

import base64
import hashlib
import json

import pytest
from round_robin.providers.base import ProviderFailure
from round_robin.providers.images import HTTPImageProvider, ImageFundingLocked
from round_robin.visual.feedback import build_edit_bundle

from backend.tests.test_image_provider import authorization, generation_packet, pixels
from backend.tests.test_image_provider import image_configuration as image_configuration  # noqa: PLC0414


def adapter_with_result(configuration, monkeypatch, raw=None):
    adapter = HTTPImageProvider("openai", configuration, api_key="synthetic-only")
    calls = []
    def send(url, headers, payload, *, timeout, maximum_bytes):
        calls.append((url, headers, payload, timeout, maximum_bytes))
        return (raw if raw is not None else {"data": [{"b64_json": base64.b64encode(pixels()).decode()}], "usage": {"input_tokens": 3}}, "request_one")
    monkeypatch.setattr(adapter, "_http", send)
    return adapter, calls


def test_generation_request_keeps_brief_preservation_funding_and_limits(image_configuration, monkeypatch):
    adapter, calls = adapter_with_result(image_configuration, monkeypatch)
    packet = generation_packet()
    packet["brief"]["locked_requirements"] = ["Keep the title", "Preserve the brand"]
    packet["timeout_seconds"] = 17
    result = adapter.submit(packet, authorization())
    url, headers, payload, timeout, maximum = calls[0]
    prompt = json.loads(payload["prompt"])
    assert url == "https://api.openai.com/v1/images/generations"
    assert headers["Authorization"] == "Bearer synthetic-only"
    assert payload["quality"] == image_configuration["quality"]
    assert prompt["brief"] == packet["brief"]
    assert prompt["preserve"] == packet["brief"]["locked_requirements"]
    assert timeout == 17 and maximum == 90 * 1024 * 1024
    assert result["usage"] == {"input_tokens": 3}
    assert result["image_bytes"] == pixels()


def test_timeout_is_capped_at_ten_minutes(image_configuration, monkeypatch):
    adapter, calls = adapter_with_result(image_configuration, monkeypatch)
    packet = generation_packet()
    packet["timeout_seconds"] = 1000
    adapter.submit(packet, authorization())
    assert calls[0][3] == 600


def test_edit_bundle_is_checked_before_send_and_hash_reaches_request(image_configuration, monkeypatch):
    adapter, calls = adapter_with_result(image_configuration, monkeypatch)
    packet = generation_packet("edit")
    bundle, digest = build_edit_bundle(brief=packet["brief"], parent_version_id="version_parent", parent_hash=hashlib.sha256(packet["images"][0]).hexdigest(), findings=[], dispositions={}, intent="Change the background")
    packet.update(edit_bundle=bundle, edit_bundle_hash=digest)
    adapter.submit(packet, authorization("edit"))
    prompt = json.loads(calls[0][2]["prompt"])
    assert prompt["approved_edit_bundle"] == bundle
    assert prompt["edit_bundle_hash"] == digest
    packet["edit_bundle"] = {**bundle, "intent": "Changed after authorization"}
    calls.clear()
    with pytest.raises(ValueError):
        adapter.submit(packet, authorization("edit"))
    assert calls == []


@pytest.mark.parametrize("extra", [0, 1])
def test_prompt_limit_is_inclusive_and_prevents_send(image_configuration, monkeypatch, extra):
    adapter, calls = adapter_with_result(image_configuration, monkeypatch)
    packet = generation_packet()
    empty = json.dumps({"brief": packet["brief"], "targeted_changes": "", "preserve": packet["brief"]["locked_requirements"]}, ensure_ascii=False)
    packet["edit_intent"] = "x" * (16000 + extra - len(empty))
    if extra:
        with pytest.raises(ImageFundingLocked):
            adapter.submit(packet, authorization())
        assert calls == []
    else:
        adapter.submit(packet, authorization())
        assert len(calls[0][2]["prompt"]) == 16000


@pytest.mark.parametrize("mode", ["multiple", "bad_base64"])
def test_malformed_generator_result_cannot_be_accepted(image_configuration, monkeypatch, mode):
    encoded = base64.b64encode(pixels()).decode()
    data = [{"b64_json": encoded}, {"b64_json": encoded}] if mode == "multiple" else [{"b64_json": encoded + "!"}]
    adapter, calls = adapter_with_result(image_configuration, monkeypatch, {"data": data})
    with pytest.raises(ProviderFailure, match="invalid_image_result"):
        adapter.submit(generation_packet(), authorization())
    assert len(calls) == 1
