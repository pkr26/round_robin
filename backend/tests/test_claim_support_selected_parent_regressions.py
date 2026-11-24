"""Selected parent corrupt JSON is fenced before reuse or child creation."""
import hashlib
import json
from copy import deepcopy

import pytest
from fastapi.testclient import TestClient
from round_robin.database.claim_models import ClaimVersion
from round_robin.database.models import EvidenceRecord, PreflightRecord, Run
from round_robin.database.services import canonical_hash
from sqlalchemy import select, text

from backend.tests.test_exhaustive_evidence_reuse import parent as parent  # noqa: PLC0414
from backend.tests.test_exhaustive_evidence_reuse import preflight, start
from backend.tests.test_operations import prepared as prepared  # noqa: PLC0414


def corrupt_evidence(app, evidence_id):
    with app.state.session_factory.kw['bind'].begin() as connection:
        connection.exec_driver_sql('UPDATE evidence SET content = ? WHERE id = ?', ('{"sentinel": "PRIVATE_PARENT', evidence_id))


def test_selected_parent_invalid_json_is_sanitized_at_preflight(parent):
    app, _, _, _, selections, *_ = parent
    corrupt_evidence(app, selections[0]['id'])
    with TestClient(app, raise_server_exceptions=False) as client:
        response = preflight(client, parent, selections[:1])
    assert response.status_code == 409, response.text
    assert response.json()['error']['code'] == 'reuse_scope_invalid'
    assert 'PRIVATE_PARENT' not in response.text


def test_selected_parent_invalid_json_is_rechecked_before_start(parent):
    app, _, _, _, selections, *_ = parent
    with TestClient(app, raise_server_exceptions=False) as client:
        quote = preflight(client, parent, selections[:1])
        assert quote.status_code == 200, quote.text
        corrupt_evidence(app, selections[0]['id'])
        response, _ = start(client, parent, quote.json())
    assert response.status_code == 409, response.text
    assert response.json()['error']['code'] == 'reuse_scope_invalid'
    assert 'PRIVATE_PARENT' not in response.text
    with app.state.session_factory() as session:
        assert session.get(PreflightRecord, quote.json()['id']).consumed_by_run_id is None
        assert len(list(session.scalars(select(Run)))) == 1


def test_injected_damage_after_selection_recheck_is_sanitized_and_rolls_back(parent, monkeypatch):
    app, _, _, _, selections, *_ = parent
    from round_robin.api import preflight as preflight_module
    original = preflight_module.import_parent_items

    def damage_between_check_and_import(session, run, manifest):
        source = session.get(EvidenceRecord, selections[0]['id'])
        session.execute(text('UPDATE evidence SET content = :bad WHERE id = :id'),
                        {'bad': '{"sentinel": "PRIVATE_PARENT', 'id': source.id})
        session.expire(source)
        return original(session, run, manifest)

    monkeypatch.setattr(preflight_module, 'import_parent_items', damage_between_check_and_import)
    with TestClient(app, raise_server_exceptions=False) as client:
        quote = preflight(client, parent, selections[:1])
        assert quote.status_code == 200, quote.text
        response, _ = start(client, parent, quote.json())
    with app.state.session_factory() as session:
        assert session.get(PreflightRecord, quote.json()['id']).consumed_by_run_id is None
        assert len(list(session.scalars(select(Run)))) == 1
    assert response.status_code == 409, response.text
    assert 'PRIVATE_PARENT' not in response.text


@pytest.mark.parametrize('update_hash', [False, True])
def test_injected_claim_change_between_recheck_and_import_cannot_be_copied(parent, monkeypatch, update_hash):
    app, _, _, _, selections, *_ = parent
    from round_robin.api import preflight as preflight_module
    original = preflight_module.import_parent_items

    def change_between_check_and_import(session, run, manifest):
        source = session.get(ClaimVersion, selections[1]['id'])
        changed = deepcopy(source.content)
        changed['proposition'] = 'SYNTHETIC_CHANGED_CLAIM'
        session.execute(text('UPDATE claim_versions SET content = :content, sha256 = :sha WHERE id = :id'),
                        {'content': json.dumps(changed), 'sha': canonical_hash(changed) if update_hash else source.sha256, 'id': source.id})
        session.expire(source)
        return original(session, run, manifest)

    monkeypatch.setattr(preflight_module, 'import_parent_items', change_between_check_and_import)
    with TestClient(app, raise_server_exceptions=False) as client:
        quote = preflight(client, parent, selections)
        assert quote.status_code == 200, quote.text
        response, _ = start(client, parent, quote.json())
    if response.status_code == 202:
        with app.state.session_factory() as session:
            child = response.json()['id']
            claims = list(session.scalars(select(ClaimVersion).where(ClaimVersion.run_id == child)))
            assert claims and claims[0].content['proposition'] == 'SYNTHETIC_CHANGED_CLAIM'
    assert response.status_code == 409, response.text
    with app.state.session_factory() as session:
        assert session.get(PreflightRecord, quote.json()['id']).consumed_by_run_id is None
        assert len(list(session.scalars(select(Run)))) == 1


def test_injected_consistent_evidence_change_after_recheck_is_not_copied(parent, monkeypatch):
    app, _, _, _, selections, *_ = parent
    from round_robin.api import preflight as preflight_module
    original = preflight_module.import_parent_items

    def change_between_check_and_import(session, run, manifest):
        source = session.get(EvidenceRecord, selections[0]['id'])
        changed = deepcopy(source.content)
        changed['text'] = 'SYNTHETIC_CHANGED_EVIDENCE'
        changed['sha256'] = hashlib.sha256(changed['text'].encode()).hexdigest()
        session.execute(text('UPDATE evidence SET content = :content, sha256 = :sha WHERE id = :id'),
                        {'content': json.dumps(changed), 'sha': changed['sha256'], 'id': source.id})
        session.expire(source)
        return original(session, run, manifest)

    monkeypatch.setattr(preflight_module, 'import_parent_items', change_between_check_and_import)
    with TestClient(app, raise_server_exceptions=False) as client:
        quote = preflight(client, parent, selections[:1])
        assert quote.status_code == 200, quote.text
        response, _ = start(client, parent, quote.json())
    assert response.status_code == 409, response.text
    assert response.json()['error']['code'] == 'reuse_scope_invalid'
    with app.state.session_factory() as session:
        assert session.get(PreflightRecord, quote.json()['id']).consumed_by_run_id is None
        assert len(list(session.scalars(select(Run)))) == 1


@pytest.mark.parametrize('kind', ['evidence', 'claim'])
def test_unflushed_parent_change_is_not_silently_discarded_before_import(parent, monkeypatch, kind):
    app, _, _, _, selections, *_ = parent
    from round_robin.api import preflight as preflight_module
    original = preflight_module.import_parent_items
    selected = selections[0] if kind == 'evidence' else selections[1]

    def dirty_before_import(session, run, manifest):
        source = session.get(EvidenceRecord if kind == 'evidence' else ClaimVersion, selected['id'])
        changed = deepcopy(source.content)
        if kind == 'evidence':
            changed['text'] = 'SYNTHETIC_PENDING_EVIDENCE'
            changed['sha256'] = hashlib.sha256(changed['text'].encode()).hexdigest()
            source.sha256 = changed['sha256']
        else:
            changed['proposition'] = 'SYNTHETIC_PENDING_CLAIM'
            source.sha256 = canonical_hash(changed)
        source.content = changed
        # No explicit flush: the import boundary must not discard dirty state
        # and proceed as though the old preflight content still existed.
        return original(session, run, manifest)

    monkeypatch.setattr(preflight_module, 'import_parent_items', dirty_before_import)
    with TestClient(app, raise_server_exceptions=False) as client:
        quote = preflight(client, parent, selections)
        assert quote.status_code == 200, quote.text
        response, _ = start(client, parent, quote.json())
    assert response.status_code == 409, response.text
    assert response.json()['error']['code'] == 'reuse_scope_invalid'
    with app.state.session_factory() as session:
        assert session.get(PreflightRecord, quote.json()['id']).consumed_by_run_id is None
        assert len(list(session.scalars(select(Run)))) == 1


@pytest.mark.parametrize('damage', ['missing_id', 'invalid_kind', 'boolean_version'])
def test_malformed_import_manifest_rolls_back_without_exposing_content(parent, monkeypatch, damage):
    app, _, _, _, selections, *_ = parent
    from round_robin.api import preflight as preflight_module
    original = preflight_module.import_parent_items

    def malformed_before_import(session, run, manifest):
        if damage == 'missing_id':
            damaged = [{key: value for key, value in item.items() if key != 'id'} for item in manifest]
        elif damage == 'invalid_kind':
            damaged = [{**item, 'kind': 'unknown'} for item in manifest]
        else:
            damaged = [{**item, 'version': True} for item in manifest]
        return original(session, run, damaged)

    monkeypatch.setattr(preflight_module, 'import_parent_items', malformed_before_import)
    with TestClient(app, raise_server_exceptions=False) as client:
        quote = preflight(client, parent, selections[:1])
        assert quote.status_code == 200, quote.text
        response, _ = start(client, parent, quote.json())
    assert response.status_code == 409, response.text
    assert response.json()['error']['code'] == 'reuse_scope_invalid'
    with app.state.session_factory() as session:
        assert session.get(PreflightRecord, quote.json()['id']).consumed_by_run_id is None
        assert len(list(session.scalars(select(Run)))) == 1
