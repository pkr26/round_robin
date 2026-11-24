"""Each visual operation must be covered by its own configured price bound."""

from dataclasses import replace

import pytest
from round_robin.providers import images
from round_robin.providers.images import (
    HTTPImageProvider,
    ImageAuthorization,
    ImageFundingLocked,
)


@pytest.mark.parametrize(
    ("kind", "required", "below"),
    [("vision", "0.20", "0.19"), ("generation", "0.40", "0.39"), ("edit", "0.70", "0.69")],
)
def test_visual_operation_uses_its_own_frozen_funding_bound(monkeypatch, kind, required, below):
    # Avoid filesystem, credentials, and network; exercise the authorization
    # gate used by preflight with deliberately distinct conservative prices.
    monkeypatch.setattr(images, "verify_image_configuration", lambda _configuration: None)
    adapter = object.__new__(HTTPImageProvider)
    adapter.provider = "openai"
    adapter.api_key = "synthetic"
    adapter.configuration = {"vision_prices": {"openai": "0.20"}, "generation_price_usd": "0.40", "edit_price_usd": "0.70"}
    packet = {"owner_id": "owner_one", "session_id": "session_one", "step_id": "step_one"}
    authorization = ImageAuthorization("operation_one", "owner_one", "session_one", required, 0, 1, "step_one")

    adapter._gate(packet, authorization, kind)
    with pytest.raises(ImageFundingLocked):
        adapter._gate(packet, replace(authorization, bound_usd=below), kind)
