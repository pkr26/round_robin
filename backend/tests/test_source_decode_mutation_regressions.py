"""Malformed stored JSON must not leak internal data or turn reads into 500s."""

import pytest
from sqlalchemy import text

from backend.tests.test_artifacts_api import artifacts as artifacts  # noqa: PLC0414


@pytest.mark.parametrize("suffix", ["", "/passage_offer_a"])
@pytest.mark.parametrize("raw", ['{"secret":"PRIVATE_SOURCE_SENTINEL",', '[' * 1200 + '0' + ']' * 1200, '[' * 10000 + '0' + ']' * 10000])
def test_malformed_stored_source_returns_sanitized_unavailable(artifacts, suffix, raw):
    app, client, _, run_id, _ = artifacts
    with app.state.session_factory.begin() as session:
        session.execute(text("UPDATE evidence SET content = :value WHERE id = :identity"), {"value": raw, "identity": "passage_offer_a"})
    original = client._transport.raise_server_exceptions
    client._transport.raise_server_exceptions = False
    try:
        response = client.get(f"/api/runs/{run_id}/sources{suffix}")
    finally:
        client._transport.raise_server_exceptions = original
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "saved_result_unavailable"
    assert "PRIVATE_SOURCE_SENTINEL" not in response.text
    with app.state.session_factory() as session:
        assert session.scalar(text("SELECT content FROM evidence WHERE id = :identity"), {"identity": "passage_offer_a"}) == raw


@pytest.mark.parametrize("identity,expected", [("passage_offer_a", 503), ("passage_offer_b", 200)])
def test_corrupt_lookahead_is_unavailable_but_later_page_does_not_block(artifacts, identity, expected):
    app, client, _, run_id, _ = artifacts
    with app.state.session_factory.begin() as session:
        session.execute(text("UPDATE evidence SET content = :value WHERE id = :identity"), {"value": '{"secret":"PRIVATE_SOURCE_SENTINEL",', "identity": identity})
    original = client._transport.raise_server_exceptions
    client._transport.raise_server_exceptions = False
    try:
        page = client.get(f"/api/runs/{run_id}/sources", params={"limit": 1})
        first = client.get(f"/api/runs/{run_id}/sources/passage_goal")
        damaged = client.get(f"/api/runs/{run_id}/sources/{identity}")
    finally:
        client._transport.raise_server_exceptions = original
    assert page.status_code == expected
    if expected == 503:
        assert page.json()["error"]["code"] == "saved_result_unavailable"
    else:
        assert [item["id"] for item in page.json()["items"]] == ["passage_goal"]
        assert page.json()["next_cursor"] == "passage_goal"
    assert first.status_code == 200
    assert damaged.status_code == 503
    assert "PRIVATE_SOURCE_SENTINEL" not in page.text + damaged.text
