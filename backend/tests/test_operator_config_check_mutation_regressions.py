"""Operator configuration checks report the actual validated deployment."""

import json
from dataclasses import replace

import pytest
from fastapi import HTTPException
from round_robin.operations.recovery import OperationError, schema_head
from round_robin.ops import config_check
from sqlalchemy import text

from backend.tests.test_deep_operator import operator as operator  # noqa: PLC0414
from backend.tests.test_provider_configuration import synthetic_configuration


@pytest.mark.parametrize("profile", ["development", "invited_hosted"])
def test_check_reports_validated_profile_schema_and_no_provider_calls(operator, profile):
    app, settings = operator
    if profile == "invited_hosted":
        settings = replace(settings, development=False, hosted_enabled=True,
            public_origin="https://app.example", oidc_issuer="https://identity.example", oidc_client_id="synthetic-client",
            oidc_client_secret="synthetic-secret", invite_allowlist=(("https://identity.example", "user_test"),),
            hosted_quotas_configured=True, private_storage_configured=True, retention_configured=True,
            command_hash_key="synthetic-command-hash-key-32-characters")
    result = config_check(settings, app.state.engine)
    assert result == {"configuration": "valid", "schema_revision": schema_head(), "profile": profile,
                      "paid_access_enabled": False, "provider_requests": 0}


def test_funded_check_validates_actual_config_and_reports_paid_state(operator, tmp_path, monkeypatch):
    app, settings = operator
    path = tmp_path / "synthetic-funding.json"
    path.write_text(json.dumps(synthetic_configuration()))
    monkeypatch.delenv("ROUND_ROBIN_IMAGE_CONFIG_FILE", raising=False)
    paid = replace(settings, paid_access_enabled=True, provider_config_file=path)
    result = config_check(paid, app.state.engine)
    assert result["paid_access_enabled"] is True
    assert result["schema_revision"] == schema_head() and result["provider_requests"] == 0
    path.unlink()
    with pytest.raises(HTTPException) as failure:
        config_check(paid, app.state.engine)
    assert failure.value.detail["code"] == "provider_configuration_invalid"


def test_schema_mismatch_cannot_be_reported_as_valid(operator):
    app, settings = operator
    with app.state.engine.begin() as connection:
        connection.execute(text("UPDATE alembic_version SET version_num='wrong_revision'"))
    with pytest.raises(OperationError):
        config_check(settings, app.state.engine)
