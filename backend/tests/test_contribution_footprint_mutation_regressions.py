"""Provider contributions preserve the downstream review envelope."""
import json
from dataclasses import replace
from decimal import Decimal

import httpx
import pytest
from round_robin.providers.base import ProviderFailure, ProviderOutput
from round_robin.providers.http import HTTPProvider

from backend.tests.test_providers import configuration, output, packet, response_for


def example(kind):
    if kind == "dissent":
        return {"dissent": ["first", "second"]}, "proposal", 1958
    if kind == "multibyte":
        return {"dissent": ["first", "second"]}, "proposal", 979
    if kind == "critical":
        return {"critical_defects": ["first", "second"], "audit_passed": False}, "audit", 1957
    if kind == "objections":
        return {"objections": [{"alias": f"new_objection_{i}", "concern": "Check the source.", "severity": "material", "reference_ids": []} for i in (1, 2)]}, "proposal", 1645
    if kind == "claims":
        return {"claims": [{"alias": f"new_claim_{i}", "proposition": "A scoped statement.", "kind": "fact", "evidence_status": "not_checked", "reference_ids": ["evidence_owned"]} for i in (1, 2)]}, "proposal", 1513
    return {"objection_responses": [{"objection_id": f"objection_{i}", "disposition": "answered", "response": "Addressed.", "reference_ids": []} for i in (1, 2)]}, "synthesis", 1777


@pytest.mark.parametrize("kind", ["dissent", "multibyte", "critical", "objections", "claims", "responses"])
@pytest.mark.parametrize("over", [False, True])
def test_provider_preserves_full_contribution_at_boundary_and_rejects_overflow(kind, over):
    fields, stage, length = example(kind)
    summary = ("é" if kind == "multibyte" else "x") * length + ("x" if over else "")
    content = {**output(), **fields, "summary": summary}
    normalized = ProviderOutput.model_validate(content).model_dump(mode="json")
    assert len(json.dumps(normalized, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()) == 2240 + int(over)
    request = packet(stage)
    if kind == "responses":
        request = replace(request, open_objections=tuple({"id": f"objection_{i}", "severity": "material", "status": "open", "concern": "Check.", "events": []} for i in (1, 2)))
    sent = []

    def respond(req):
        sent.append(req)
        return httpx.Response(200, headers={"x-request-id": "synthetic_request"}, json=response_for("openai", content))

    adapter = HTTPProvider("openai", configuration("openai"), transport=httpx.MockTransport(respond), api_key="synthetic")
    if over:
        with pytest.raises(ProviderFailure, match="schema_invalid") as error:
            adapter.generate(request)
        assert error.value.retryable and error.value.diagnostics[0]["code"] == "contribution_context_envelope"
        assert error.value.cost_usd == Decimal("0.0007") and error.value.provider_request_id == "synthetic_request"
    else:
        result = adapter.generate(request)
        assert result.output.summary == summary
        assert result.cost_usd == Decimal("0.0007") and result.provider_request_id == "synthetic_request"
        field = "dissent" if kind == "multibyte" else "critical_defects" if kind == "critical" else "objection_responses" if kind == "responses" else kind
        assert len(getattr(result.output, field)) == 2
    assert len(sent) == 1
