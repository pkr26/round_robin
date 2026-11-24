"""Operator funding choices and policy gates retain their frozen audit scope."""

from dataclasses import replace
from datetime import datetime

import pytest
from round_robin.api import preflight
from round_robin.configuration import Settings
from round_robin.database.models import Base, PolicyBucket
from round_robin.operations.recovery import OperationError
from round_robin.ops import funding_configuration, set_policy_flag
from round_robin.providers import images
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

TEXT_CAPS = {"deployment_daily_usd": "3", "deployment_monthly_usd": "40", "owner_daily_usd": "8", "owner_monthly_usd": "10"}
IMAGE_CAPS = {"deployment_daily_usd": "6", "deployment_monthly_usd": "30", "owner_daily_usd": "4", "owner_monthly_usd": "20"}


@pytest.mark.parametrize("modalities", ["text", "image", "both"])
def test_operator_funding_preserves_exact_versions_and_minimum_caps(tmp_path, monkeypatch, modalities):
    settings = replace(Settings(), paid_access_enabled=True, provider_config_file=tmp_path / "text" if modalities != "image" else None)
    if modalities != "text":
        monkeypatch.setenv("ROUND_ROBIN_IMAGE_CONFIG_FILE", str(tmp_path / "image"))
    else:
        monkeypatch.delenv("ROUND_ROBIN_IMAGE_CONFIG_FILE", raising=False)
    calls = []

    def load_text(actual_settings, mode):
        if actual_settings is not settings or mode != "live":
            raise AssertionError("Text loader must receive the authorized settings and live mode")
        calls.append("text")
        return {"funded": True, "version": "text_v1", "aggregate_funding": TEXT_CAPS}

    def load_image(actual_settings):
        if actual_settings is not settings:
            raise AssertionError("Image loader must receive the authorized settings")
        calls.append("image")
        return {"version": "image_v2", **IMAGE_CAPS}

    monkeypatch.setattr(preflight, "run_configuration", load_text)
    monkeypatch.setattr(images, "load_image_configuration", load_image)
    result = funding_configuration(settings)
    expected_versions = {"text": ["text_v1"], "image": ["image_v2"], "both": ["text_v1", "image_v2"]}
    expected_caps = {"text": TEXT_CAPS, "image": IMAGE_CAPS,
                     "both": {"deployment_daily_usd": "3", "deployment_monthly_usd": "30", "owner_daily_usd": "4", "owner_monthly_usd": "10"}}
    assert result == {"versions": expected_versions[modalities], "caps": expected_caps[modalities]}
    assert calls == {"text": ["text"], "image": ["image"], "both": ["text", "image"]}[modalities]


@pytest.fixture
def policy_factory(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'operator-policy.sqlite'}")
    Base.metadata.create_all(engine)
    yield sessionmaker(engine, expire_on_commit=False)
    engine.dispose()


def deployment(session):
    return session.scalar(select(PolicyBucket).where(PolicyBucket.key == "deployment"))


def test_operator_lock_and_drain_actions_are_versioned_and_audited(policy_factory):
    for version, name, value in [(2, "spending_locked", True), (3, "draining", True), (4, "draining", False)]:
        result = set_policy_flag(policy_factory, name=name, value=value)
        assert result == {name: value, "provider_requests": 0}
        with policy_factory() as session:
            policy = deployment(session)
            assert policy.version == version and policy.spending_locked is True
            action = policy.metadata_json["operator_action"]
            assert action["name"] == name and action["value"] is value
            assert datetime.fromisoformat(action["at"]).tzinfo is not None
            if name == "draining":
                assert policy.metadata_json["draining"] is value


def test_emergency_lock_remains_available_during_restore_quarantine(policy_factory):
    set_policy_flag(policy_factory, name="spending_locked", value=True)
    with policy_factory.begin() as session:
        policy = deployment(session)
        policy.metadata_json = {**policy.metadata_json, "restore_quarantine": True}
    assert set_policy_flag(policy_factory, name="spending_locked", value=True)["spending_locked"] is True
    with pytest.raises(OperationError):
        set_policy_flag(policy_factory, name="spending_locked", value=False)
    with policy_factory() as session:
        assert deployment(session).spending_locked is True


def test_policy_helper_allows_cleared_quarantine(policy_factory):
    set_policy_flag(policy_factory, name="spending_locked", value=True)
    assert set_policy_flag(policy_factory, name="spending_locked", value=False)["spending_locked"] is False
    with policy_factory() as session:
        assert deployment(session).spending_locked is False
