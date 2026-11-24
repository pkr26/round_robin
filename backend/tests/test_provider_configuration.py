from dataclasses import replace
from datetime import timedelta
import json
from uuid import uuid4

from fastapi import HTTPException
from fastapi.testclient import TestClient
import pytest

from round_robin.app import create_app
from round_robin.api.preflight import run_configuration
from round_robin.configuration import Settings
from round_robin.database.models import utcnow
from round_robin.database.services import lock_policies
from round_robin.fixtures import sample_brief


def synthetic_configuration():
    return {"version": "synthetic-config-v1", "funded": True, "prompt_version": "synthetic-prompts-v1", "models": {provider: f"synthetic-{provider}-model" for provider in ("openai", "anthropic", "google")}, "pricing": {provider: {"version": "synthetic-pricing-v1", "input_usd_per_million": "1", "output_usd_per_million": "2", "billable_output_bound_tokens": 4096, "reasoning_exposure_bounded": True, "effective_date": utcnow().date().isoformat()} for provider in ("openai", "anthropic", "google")}, "capabilities": {provider: {"structured_text": True, "context_window_tokens": 32768, "verified_at": utcnow().isoformat(), "evidence_version": "synthetic-test-evidence-v1"} for provider in ("openai", "anthropic", "google")}, "limits": {"per_run_usd": "2", "owner_daily_usd": "10", "owner_monthly_usd": "50"}, "finish_policy": {"reserved_usd": "0.40"}, "aggregate_funding": {"deployment_daily_usd": "10", "deployment_monthly_usd": "50", "owner_daily_usd": "8", "owner_monthly_usd": "40"}, "anthropic_api_version": "2023-06-01", "expires_at": (utcnow() + timedelta(hours=1)).isoformat(), "fallbacks": {stage: ["openai", "anthropic", "google"] for stage in ("intake", "synthesis", "audit")}, "research": {"enabled": False}}


def test_provider_configuration_requires_both_explicit_file_and_funding_flag(tmp_path):
    path = tmp_path / "synthetic-provider-config.json"
    path.write_text(json.dumps(synthetic_configuration()))
    disabled = run_configuration(replace(Settings(), provider_config_file=path, paid_access_enabled=False), "live")
    assert disabled["funded"] is False
    enabled = run_configuration(replace(Settings(), provider_config_file=path, paid_access_enabled=True), "live")
    assert enabled["funded"] is True
    assert enabled["operator_configuration_hash"]
    assert enabled["limits"]["owner_daily_usd"] == "8"
    assert enabled["limits"]["owner_monthly_usd"] == "40"
    assert run_configuration(replace(Settings(), provider_config_file=path, paid_access_enabled=True), "simulated")["funded"] is False


@pytest.mark.parametrize("change", ["unknown_secret", "unbounded_reasoning", "missing_provider", "expired"])
def test_invalid_operator_configuration_fails_closed_without_network(tmp_path, change):
    config = synthetic_configuration()
    if change == "unknown_secret":
        config["api_key"] = "synthetic-must-not-be-accepted"
    elif change == "unbounded_reasoning":
        config["pricing"]["openai"]["reasoning_exposure_bounded"] = False
    elif change == "missing_provider":
        del config["capabilities"]["google"]
    else:
        config["expires_at"] = (utcnow() - timedelta(seconds=1)).isoformat()
    path = tmp_path / "synthetic-provider-config.json"
    path.write_text(json.dumps(config))
    with pytest.raises(HTTPException) as error:
        run_configuration(replace(Settings(), provider_config_file=path, paid_access_enabled=True), "live")
    assert error.value.status_code == 422
    assert error.value.detail["code"] == "provider_configuration_invalid"


def test_funded_preflight_still_requires_policy_unlock_and_freezes_configuration(tmp_path):
    path = tmp_path / "synthetic-provider-config.json"
    config = synthetic_configuration()
    path.write_text(json.dumps(config))
    settings = replace(Settings(), provider_config_file=path, paid_access_enabled=True, artifact_directory=tmp_path / "artifacts")
    application = create_app(f"sqlite:///{tmp_path/'funding.db'}", settings=settings, initialize=True)
    with TestClient(application) as client:
        meeting = client.post("/api/meetings", json={"command_id": str(uuid4()), "brief": sample_brief().model_dump(mode="json")}).json()
        route = f"/api/meetings/{meeting['id']}/preflight"
        quote = client.post(route, json={"command_id": str(uuid4()), "expected_draft_revision": 1, "mode": "live"}).json()
        assert quote["blockers"]
        with application.state.session_factory.begin() as session:
            for policy in lock_policies(session, "owner_local"):
                policy.spending_locked = False
                policy.limit_usd = "10"
                policy.metadata_json = {"daily_usd": "10", "monthly_usd": "50"}
        quote = client.post(route, json={"command_id": str(uuid4()), "expected_draft_revision": 1, "mode": "live"}).json()
        assert quote["blockers"] == []
        config["models"]["openai"] = "synthetic-reconfigured-model"
        path.write_text(json.dumps(config))
        result = client.post(f"/api/meetings/{meeting['id']}/runs", json={"command_id": str(uuid4()), "preflight_id": quote["id"], "expected_draft_revision": 1})
        assert result.status_code == 409
        assert result.json()["error"]["code"] == "preflight_stale"
