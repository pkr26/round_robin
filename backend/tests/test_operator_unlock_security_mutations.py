"""A synthetic operator unlock must respect recovery fences and funded caps."""

import json
from datetime import datetime
from types import SimpleNamespace

import pytest
from round_robin import ops
from round_robin.database.models import Base, PolicyBucket
from round_robin.database.services import lock_policies
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

CAPS = {"deployment_daily_usd": "3", "deployment_monthly_usd": "30", "owner_daily_usd": "4", "owner_monthly_usd": "20"}


@pytest.fixture
def cli(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'operator-unlock.sqlite'}"
    engine = create_engine(url)
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    settings = SimpleNamespace(database_url=url, artifact_directory=tmp_path / "artifacts",
                               durability_control_directory=tmp_path / "actual-control", deployment_epoch=7)

    def configure(actual_url):
        if actual_url != url:
            raise AssertionError("Only the synthetic SQLite database is permitted")
        return engine, factory

    monkeypatch.setattr(ops, "Settings", lambda: settings)
    monkeypatch.setattr(ops, "configure_database", configure)
    monkeypatch.setattr(ops, "funding_configuration", lambda actual_settings: {"versions": ["text_v1", "image_v2"], "caps": CAPS})
    monkeypatch.setenv("ROUND_ROBIN_DURABILITY_CONTROL_DIRECTORY", str(tmp_path / "different-control"))
    yield settings, factory
    engine.dispose()


def buckets(factory, owner):
    with factory() as session:
        return {row.key: row for row in session.scalars(select(PolicyBucket).where(PolicyBucket.key.in_(["deployment", f"owner:{owner}"])))}


@pytest.mark.parametrize("quarantine", ["restore_quarantine", "durability_compromised"])
def test_operator_unlock_never_clears_a_recovery_fence(cli, capsys, quarantine):
    _, factory = cli
    with factory.begin() as session:
        deployment, owner = lock_policies(session, "owner_synthetic")
        deployment.metadata_json = {quarantine: True}
        assert deployment.spending_locked and owner.spending_locked
    assert ops.main(["spend-lock", "--unlock", "--owner", "owner_synthetic"]) == 1
    assert json.loads(capsys.readouterr().out)["status"] == "blocked"
    saved = buckets(factory, "owner_synthetic")
    assert saved["deployment"].spending_locked and saved["owner:owner_synthetic"].spending_locked
    assert saved["deployment"].metadata_json[quarantine] is True


@pytest.mark.parametrize("control_path", ["explicit", "derived_from_artifacts"])
def test_operator_unlock_uses_independent_durability_marker(cli, monkeypatch, capsys, tmp_path, control_path):
    from round_robin import configuration

    settings, factory = cli
    if control_path == "explicit":
        control = settings.durability_control_directory
    else:
        settings.durability_control_directory = None
        monkeypatch.delenv("ROUND_ROBIN_DURABILITY_CONTROL_DIRECTORY", raising=False)
        monkeypatch.setattr(configuration, "settings", SimpleNamespace(artifact_directory=tmp_path / "unrelated" / "artifacts"))
        control = settings.artifact_directory.parent / "durability-control"
    control.mkdir()
    marker = control / "failure_test.json"
    marker.write_text('{"unreadable": true}')
    calls = []
    monkeypatch.setattr(ops, "funding_configuration", lambda settings: calls.append("called"))
    assert ops.main(["spend-lock", "--unlock", "--owner", "owner_synthetic"]) == 1
    assert json.loads(capsys.readouterr().out)["status"] == "blocked"
    assert calls == []
    assert buckets(factory, "owner_synthetic") == {}


@pytest.mark.parametrize("owner", [None, "owner_synthetic"])
def test_operator_unlock_records_exact_caps_epoch_versions_and_owner(cli, capsys, owner):
    settings, factory = cli
    expected_owner = owner or "owner_local"
    with factory.begin() as session:
        deployment, owner_policy = lock_policies(session, expected_owner)
        deployment.version, owner_policy.version = 5, 8
        deployment.metadata_json, owner_policy.metadata_json = {"preserved": "deployment"}, {"preserved": "owner"}
    args = ["spend-lock", "--unlock"] + (["--owner", owner] if owner else [])
    assert ops.main(args) == 0
    assert json.loads(capsys.readouterr().out) == {"spending_locked": False, "owner_id": expected_owner,
                                                  "funding_configuration_versions": ["text_v1", "image_v2"], "provider_requests": 0}
    saved = buckets(factory, expected_owner)
    for prefix, key, version in [("deployment", "deployment", 6), ("owner", f"owner:{expected_owner}", 9)]:
        policy = saved[key]
        assert policy.spending_locked is False and policy.version == version
        assert policy.limit_usd == CAPS[f"{prefix}_daily_usd"]
        assert policy.metadata_json["daily_usd"] == CAPS[f"{prefix}_daily_usd"]
        assert policy.metadata_json["monthly_usd"] == CAPS[f"{prefix}_monthly_usd"]
        assert policy.metadata_json["deployment_epoch"] == settings.deployment_epoch
        assert policy.metadata_json["funding_configuration_versions"] == ["text_v1", "image_v2"]
        assert policy.metadata_json["preserved"] == prefix
        assert datetime.fromisoformat(policy.metadata_json["operator_authorized_at"]).tzinfo is not None
