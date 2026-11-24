"""Visual preflight rejects invalid paid packets before a reservation is made."""

import hashlib

import pytest
from round_robin.providers.images import HTTPImageProvider, ImageFundingLocked
from round_robin.visual.reviews import (
    PixelView,
    independent_review_packet,
    issue_review_receipt,
)
from round_robin.visual.schemas import ImageCapabilities, VisualBrief

from backend.tests.test_image_provider import authorization, generation_packet, pixels
from backend.tests.test_image_review_integrity_mutations import review_packet

pytest_plugins = ("backend.tests.test_image_provider",)


def adapter(configuration):
    return HTTPImageProvider("openai", configuration, api_key="synthetic")


def test_image_preflight_rejects_edit_when_only_generation_is_supported(image_configuration):
    provider = adapter(image_configuration)
    provider.capabilities = provider.capabilities.model_copy(update={"generates_images": True, "edits_images": False})
    with pytest.raises(ImageFundingLocked):
        provider.preflight(generation_packet("edit"), authorization("edit"))


@pytest.mark.parametrize("count", [0, 4, 5])
def test_edit_preflight_enforces_one_to_four_approved_images(image_configuration, count):
    packet = generation_packet("edit")
    packet["images"] = [pixels("blue")] * count
    if count == 4:
        adapter(image_configuration).preflight(packet, authorization("edit"))
    else:
        with pytest.raises(ImageFundingLocked):
            adapter(image_configuration).preflight(packet, authorization("edit"))


def test_edit_preflight_allows_exact_pixel_bound_and_rejects_one_byte_less(image_configuration):
    packet = generation_packet("edit")
    image_size = len(packet["images"][0])
    provider = adapter(image_configuration)
    provider.capabilities = provider.capabilities.model_copy(update={"maximum_input_bytes": image_size})
    provider.preflight(packet, authorization("edit"))
    provider.capabilities = provider.capabilities.model_copy(update={"maximum_input_bytes": image_size - 1})
    with pytest.raises(ImageFundingLocked):
        provider.preflight(packet, authorization("edit"))


def vision_packet(configuration, count):
    image = pixels("green")
    digest = hashlib.sha256(image).hexdigest()
    views = [PixelView(image, digest) for _ in range(min(count, 4))]
    capability = ImageCapabilities.model_validate(configuration["capabilities"]["openai"])
    receipt = issue_review_receipt(capability, "version_one", digest, views)
    result = independent_review_packet(VisualBrief(purpose="Synthetic review", audience="Tester"), receipt, views)
    result.update(kind="vision", owner_id="owner_one", session_id="session_one", step_id="step_one")
    if count == 5:
        result["images"].append(image)
        result["receipt"]["views"] = [*result["receipt"]["views"], dict(result["receipt"]["views"][0])]
    return result


@pytest.mark.parametrize("count", [4, 5])
def test_vision_preflight_enforces_four_view_limit(image_configuration, count):
    packet = vision_packet(image_configuration, count)
    if count == 4:
        adapter(image_configuration).preflight(packet, authorization("vision"))
    else:
        with pytest.raises(ImageFundingLocked):
            adapter(image_configuration).preflight(packet, authorization("vision"))


def test_vision_preflight_rejects_non_list_and_exact_pixel_overflow(image_configuration):
    packet = vision_packet(image_configuration, 1)
    provider = adapter(image_configuration)
    original = packet["images"]
    packet["images"] = tuple(original)
    with pytest.raises(ImageFundingLocked):
        provider.preflight(packet, authorization("vision"))
    packet["images"] = original
    size = len(original[0])
    provider.capabilities = provider.capabilities.model_copy(update={"maximum_input_bytes": size})
    provider.preflight(packet, authorization("vision"))
    provider.capabilities = provider.capabilities.model_copy(update={"maximum_input_bytes": size - 1})
    with pytest.raises(ImageFundingLocked):
        provider.preflight(packet, authorization("vision"))


def test_vision_preflight_binds_prior_context_to_actual_parent_pixels(image_configuration):
    packet, _ = review_packet(image_configuration)
    packet["kind"] = "vision"
    provider = adapter(image_configuration)
    provider.preflight(packet, authorization("vision"))
    packet["shared_prior_context"]["parent_hash"] = "0" * 64
    with pytest.raises(ValueError):
        provider.preflight(packet, authorization("vision"))
