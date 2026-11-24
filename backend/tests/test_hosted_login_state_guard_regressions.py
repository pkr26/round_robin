"""Opaque login state must be valid before callback consumption."""

from datetime import UTC, datetime, timedelta

import pytest
from round_robin.database.models import PolicyBucket
from round_robin.security.hosted import OpaqueLoginState
from sqlalchemy import select

pytest_plugins = ("test_hosted",)

NOW = datetime(2026, 1, 1, tzinfo=UTC)


@pytest.mark.parametrize("damage", ["revoked", "missing_expiry", "invalid_expiry"])
def test_callback_discards_invalid_state_and_preserves_other_binding(hosted, monkeypatch, damage):
    monkeypatch.setattr("round_robin.security.hosted.utcnow", lambda: NOW)
    valid = {"kind": "login", "expires_at": (NOW + timedelta(minutes=1)).isoformat(), "state": {"nonce": "synthetic"}}
    damaged = dict(valid)
    if damage == "revoked":
        damaged["revoked"] = True
    elif damage == "missing_expiry":
        damaged.pop("expires_at")
    else:
        damaged["expires_at"] = "not-an-iso-date"
    with hosted.state.session_factory.begin() as session:
        for key, data in (("login:damaged", damaged), ("login:other", valid)):
            session.add(PolicyBucket(id=key, key=key, limit_usd="0", spending_locked=True, metadata_json=data))

    middleware = OpaqueLoginState(None, hosted.state.session_factory)
    assert middleware._reserve("login:damaged", "generation", "/auth/callback") == ({}, False)
    with hosted.state.session_factory() as session:
        assert session.scalar(select(PolicyBucket).where(PolicyBucket.key == "login:damaged")) is None
        other = session.scalar(select(PolicyBucket).where(PolicyBucket.key == "login:other"))
        assert other.metadata_json == valid

    assert middleware._reserve("login:other", "generation2", "/auth/callback") == (valid["state"], False)
    assert middleware._reserve("login:other", "generation3", "/auth/callback") == ({}, False)
