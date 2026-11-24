import json
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
import round_robin.orchestration.configuration as configuration_module
from round_robin.api.preflight import run_configuration
from round_robin.configuration import Settings
from round_robin.database.models import utcnow
from round_robin.orchestration.budget import BudgetDenied
from round_robin.orchestration.configuration import verify_frozen_configuration
from round_robin.providers import images

from backend.tests.test_provider_configuration import synthetic_configuration


@pytest.fixture
def config(tmp_path):
    path = tmp_path / "synthetic.json"
    value = synthetic_configuration()
    path.write_text(json.dumps(value))
    settings = replace(Settings(), paid_access_enabled=True, provider_config_file=path)
    frozen = run_configuration(settings, "live")
    return path, value, settings, SimpleNamespace(configuration=frozen, deployment_epoch=settings.deployment_epoch)


def test_valid_frozen_config_and_simulated_legacy_are_allowed(config):
    path, _, settings, run = config
    verify_frozen_configuration(run, operator_settings=settings)
    path.unlink()
    verify_frozen_configuration(SimpleNamespace(configuration={"mode": "simulated"}), operator_settings=settings)
    verify_frozen_configuration(SimpleNamespace(configuration={}), operator_settings=settings)


@pytest.mark.parametrize("change,code", [
    ("funding", "operator_funding_disabled"),
    ("missing_setting", "operator_funding_disabled"),
    ("missing_file", "frozen_configuration_unavailable"),
    ("malformed_file", "frozen_configuration_unavailable"),
    ("run_expired", "frozen_configuration_expired"),
    ("operator_expired", "frozen_configuration_changed"),
    ("operator_changed", "frozen_configuration_changed"),
    ("epoch_changed", "execution_epoch_changed"),
    ("contract_changed", "frozen_request_contract_changed"),
    ("missing_hash", "frozen_configuration_unavailable"),
])
def test_frozen_config_guard_rejects_without_replacing_authority(config, change, code):
    path, value, settings, run = config
    run.configuration = deepcopy(run.configuration)
    if change == "funding": settings = replace(settings, paid_access_enabled=False)
    elif change == "missing_setting": settings = replace(settings, provider_config_file=None)
    elif change == "missing_file": path.unlink()
    elif change == "malformed_file": path.write_text("{broken")
    elif change == "run_expired": run.configuration["expires_at"] = (utcnow() - timedelta(seconds=1)).isoformat()
    elif change == "operator_expired":
        value["expires_at"] = (utcnow() - timedelta(seconds=1)).isoformat(); path.write_text(json.dumps(value))
    elif change == "operator_changed":
        value["models"]["openai"] = "changed_synthetic_model"; path.write_text(json.dumps(value))
    elif change == "epoch_changed": run.deployment_epoch += 1
    elif change == "contract_changed": run.configuration["request_contract_hash"] = "0" * 64
    elif change == "missing_hash": run.configuration.pop("operator_configuration_hash")
    with pytest.raises(BudgetDenied) as failure:
        verify_frozen_configuration(run, operator_settings=settings)
    assert str(failure.value) == code


def test_visual_configuration_delegates_and_sanitizes_failure(monkeypatch):
    run = SimpleNamespace(configuration={"visual_configuration": True})
    settings = object()
    calls = []
    def reject(value, settings):
        calls.append((value, settings))
        raise images.ImageFundingLocked("synthetic private sentinel")
    monkeypatch.setattr(images, "verify_image_configuration", reject)
    with pytest.raises(BudgetDenied) as failure:
        verify_frozen_configuration(run, operator_settings=settings)
    assert calls == [(run.configuration, settings)]
    assert str(failure.value) == "visual_configuration_changed_or_unfunded"


def test_run_expires_at_exact_boundary(config, monkeypatch):
    _, _, settings, run = config
    expiry = datetime.fromisoformat(run.configuration["expires_at"])
    monkeypatch.setattr(configuration_module, "utcnow", lambda: expiry)
    with pytest.raises(BudgetDenied) as failure:
        verify_frozen_configuration(run, operator_settings=settings)
    assert str(failure.value) == "frozen_configuration_expired"


def test_operator_expiry_independently_checked_at_boundary(config, monkeypatch):
    _, _, settings, run = config
    expiry = datetime.fromisoformat(run.configuration["expires_at"])
    # Exercise the independent operator guard with an inconsistent frozen expiry.
    run.configuration["expires_at"] = (expiry + timedelta(days=1)).isoformat()
    monkeypatch.setattr(configuration_module, "utcnow", lambda: expiry)
    with pytest.raises(BudgetDenied) as failure:
        verify_frozen_configuration(run, operator_settings=settings)
    assert str(failure.value) == "frozen_configuration_changed"
