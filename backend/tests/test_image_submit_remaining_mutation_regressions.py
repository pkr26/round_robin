"""Additional generator defaults and post-preflight safety rechecks."""

import base64

import pytest
from round_robin.providers.images import ImageFundingLocked

from backend.tests.test_image_provider import authorization, generation_packet, pixels
from backend.tests.test_image_provider import image_configuration as image_configuration  # noqa: PLC0414
from backend.tests.test_image_submit_mutation_regressions import adapter_with_result


@pytest.mark.parametrize("kind", ["generation", "edit"])
def test_capabilities_rechecked_after_preflight(image_configuration, monkeypatch, kind):
    adapter, calls = adapter_with_result(image_configuration, monkeypatch)
    preflight = adapter.preflight
    def change_capability(packet, authority):
        preflight(packet, authority)
        key = "generates_images" if kind == "generation" else "edits_images"
        adapter.capabilities = adapter.capabilities.model_copy(update={key: False})
    monkeypatch.setattr(adapter, "preflight", change_capability)
    with pytest.raises(ImageFundingLocked):
        adapter.submit(generation_packet(kind), authorization(kind))
    assert calls == []


def test_generator_role_rechecked_after_preflight(image_configuration, monkeypatch):
    adapter, calls = adapter_with_result(image_configuration, monkeypatch)
    preflight = adapter.preflight
    def change_role(packet, authority):
        preflight(packet, authority)
        adapter.configuration = {**adapter.configuration, "generator_provider": "anthropic"}
    monkeypatch.setattr(adapter, "preflight", change_role)
    with pytest.raises(ImageFundingLocked):
        adapter.submit(generation_packet(), authorization())
    assert calls == []


def test_edit_parent_count_rechecked_after_preflight(image_configuration, monkeypatch):
    adapter, calls = adapter_with_result(image_configuration, monkeypatch)
    packet = generation_packet("edit")
    packet["images"] = [pixels()] * 4
    adapter.submit(packet, authorization("edit"))
    assert len(calls[0][2]["images"]) == 4
    calls.clear()
    preflight = adapter.preflight
    def add_parent(packet, authority):
        preflight(packet, authority)
        packet["images"].append(pixels())
    monkeypatch.setattr(adapter, "preflight", add_parent)
    with pytest.raises(ImageFundingLocked):
        adapter.submit(packet, authorization("edit"))
    assert calls == []


def test_missing_edit_parent_after_preflight_is_a_controlled_rejection(image_configuration, monkeypatch):
    adapter, calls = adapter_with_result(image_configuration, monkeypatch)
    preflight = adapter.preflight
    def remove_parent(packet, authority):
        preflight(packet, authority)
        del packet["images"]
    monkeypatch.setattr(adapter, "preflight", remove_parent)
    with pytest.raises(ImageFundingLocked):
        adapter.submit(generation_packet("edit"), authorization("edit"))
    assert calls == []


def test_absent_optional_values_keep_empty_defaults(image_configuration, monkeypatch):
    raw = {"data": [{"b64_json": base64.b64encode(pixels()).decode()}]}
    adapter, calls = adapter_with_result(image_configuration, monkeypatch, raw)
    packet = generation_packet()
    packet.pop("timeout_seconds")
    packet["brief"].pop("locked_requirements")
    result = adapter.submit(packet, authorization())
    import json
    prompt = json.loads(calls[0][2]["prompt"])
    assert prompt["targeted_changes"] == []
    assert prompt["preserve"] == []
    assert calls[0][3] == 600
    assert result["usage"] == {}


def test_valid_unicode_brief_not_inflated_past_prompt_limit(image_configuration, monkeypatch):
    adapter, calls = adapter_with_result(image_configuration, monkeypatch)
    packet = generation_packet()
    packet["brief"]["purpose"] = "é" * 3000
    adapter.submit(packet, authorization())
    assert len(calls) == 1
    assert len(calls[0][2]["prompt"]) < 16000
