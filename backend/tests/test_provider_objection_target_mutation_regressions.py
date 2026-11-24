"""Provider responses cannot attach objections to unapproved references."""
import httpx
import pytest
from round_robin.providers.base import ProviderFailure
from round_robin.providers.http import HTTPProvider

from backend.tests.test_providers import configuration, output, packet, response_for


@pytest.mark.parametrize("provider", ["openai", "anthropic", "google"])
@pytest.mark.parametrize("target", [None, "evidence_owned", "foreign_reference"])
def test_approved_or_absent_objection_targets_pass_and_foreign_targets_fail(provider, target):
    sent = []
    content = {**output(), "objections": [{"alias": "new_objection_1", "concern": "Check the cited basis.",
                                           "severity": "material", "target_reference_id": target, "reference_ids": []}]}

    def respond(request):
        sent.append(request)
        return httpx.Response(200, json=response_for(provider, content))

    adapter = HTTPProvider(provider, configuration(provider), transport=httpx.MockTransport(respond), api_key="synthetic")
    if target == "foreign_reference":
        with pytest.raises(ProviderFailure, match="invalid_reference") as failure:
            adapter.generate(packet())
        assert failure.value.retryable
    else:
        result = adapter.generate(packet())
        assert result.output.objections[0].target_reference_id == target
    assert len(sent) == 1
