"""The critic wire contract remains compatible without weakening local checks."""
import json

import httpx
import pytest
from pydantic import ValidationError
from round_robin.providers.base import ProviderFailure
from round_robin.providers.http import HTTPProvider
from round_robin.providers.images import CriticContent, HTTPImageProvider
from round_robin.schemas.contributions import ClaimSupportBasis

from backend.tests.test_exhaustive_visual_provider import vision_packet
from backend.tests.test_image_provider import authorization
from backend.tests.test_image_provider import image_configuration as image_configuration  # noqa: PLC0414
from backend.tests.test_providers import configuration, output, packet, response_for


def test_anthropic_critic_wire_schema_and_local_geometry_validation(image_configuration):
    requests = []

    def respond(request):
        requests.append(json.loads(request.content))
        invalid = {
            "findings": [{
                "id": "finding_one", "description": "Synthetic issue", "category": "observed_defect",
                "severity": "minor", "region": {"x": 0, "y": 0, "width": 0, "height": 0.5},
                "view_index": 0, "previous_finding_id": None, "locked_requirement": None,
                "disposition": "pending",
            }],
            "limitations": [], "regression_checks": [],
        }
        return httpx.Response(200, json={"stop_reason": "end_turn", "content": [{"type": "text", "text": json.dumps(invalid)}]})

    adapter = HTTPImageProvider("anthropic", image_configuration, api_key="synthetic", transport=httpx.MockTransport(respond))
    with pytest.raises(ProviderFailure, match="schema_invalid"):
        adapter.review(vision_packet(adapter), authorization("vision"))
    assert len(requests) == 1
    schema = requests[0]["output_config"]["format"]["schema"]
    finding = schema["$defs"]["Finding"]
    assert set(finding["required"]) == set(finding["properties"])
    assert finding["additionalProperties"] is False
    assert "default" not in finding["properties"]["view_index"]
    assert "default" not in finding["properties"]["region"]
    assert "default" not in finding["properties"]["disposition"]
    width = schema["$defs"]["Region"]["properties"]["width"]
    assert "exclusiveMinimum" not in width and "maximum" not in width
    assert "exclusiveMinimum=0" in width["description"] and "maximum=1" in width["description"]
    local_width = CriticContent.model_json_schema(mode="serialization")["$defs"]["Region"]["properties"]["width"]
    assert local_width["exclusiveMinimum"] == 0 and local_width["maximum"] == 1


def test_anthropic_text_wire_requires_nonempty_claim_basis():
    requests = []

    def respond(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json=response_for("anthropic", output()))

    adapter = HTTPProvider("anthropic", configuration("anthropic"), transport=httpx.MockTransport(respond), api_key="synthetic")
    adapter.generate(packet())
    assert len(requests) == 1
    schema = requests[0]["output_config"]["format"]["schema"]
    references = schema["$defs"]["ClaimSupportBasis"]["properties"]["reference_ids"]
    assert references.get("minItems") == 1
    assert ClaimSupportBasis.model_validate({"reference_ids": ["evidence_one"]}).reference_ids == ["evidence_one"]
    with pytest.raises(ValidationError):
        ClaimSupportBasis.model_validate({"reference_ids": []})
