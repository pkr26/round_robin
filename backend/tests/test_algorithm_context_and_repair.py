"""Regression checks for pre-spend admission and actionable bounded repair."""
from dataclasses import replace
from uuid import uuid4
import hashlib
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from backend.tests.test_providers import configuration, output, packet, response_for
from round_robin.app import create_app
from round_robin.configuration import Settings
from round_robin.database.models import Upload
from round_robin.fixtures import sample_brief
from round_robin.providers.base import ProviderFailure, ProviderOutput, validate_output
from round_robin.providers.http import HTTPProvider


def test_utf8_selected_evidence_is_rejected_at_preflight_before_any_run(tmp_path):
    app = create_app(f"sqlite:///{tmp_path / 'context.db'}", settings=replace(Settings(), artifact_directory=tmp_path / 'artifacts'), initialize=True)
    with TestClient(app) as client:
        meeting = client.post('/api/meetings', json={'command_id': str(uuid4()), 'brief': sample_brief().model_dump(mode='json')}).json()
        text = '界' * 40000
        with app.state.session_factory.begin() as session:
            session.add(Upload(id='upload_unicode', meeting_id=meeting['id'], owner_id='owner_local', status='ready', filename='quote.txt', media_type='text/plain', canonical_text=text, sha256=hashlib.sha256(text.encode()).hexdigest(), byte_size=len(text.encode()), recipients=['openai','anthropic','google']))
        brief = {**meeting['draft'], 'selected_attachment_ids': ['upload_unicode']}
        assert client.patch(f"/api/meetings/{meeting['id']}/draft", json={'command_id': str(uuid4()), 'expected_revision': 1, 'brief': brief}).status_code == 200
        quote = client.post(f"/api/meetings/{meeting['id']}/preflight", json={'command_id': str(uuid4()), 'expected_draft_revision': 2}).json()
        assert any('UTF-8' in blocker for blocker in quote['blockers'])
        started = client.post(f"/api/meetings/{meeting['id']}/runs", json={'command_id':str(uuid4()), 'preflight_id':quote['id'], 'expected_draft_revision':2})
        assert started.status_code == 409


def test_repair_contains_sanitized_validation_path_without_private_input():
    body = response_for('openai', {**output(), 'summary': 'PRIVATE-INVALID-SUMMARY-' + 'x' * 4001})
    adapter = HTTPProvider('openai', configuration('openai'), transport=httpx.MockTransport(lambda _: httpx.Response(200, json=body)), api_key='synthetic')
    with pytest.raises(ProviderFailure) as error:
        adapter.generate(packet())
    diagnostics = error.value.diagnostics
    assert diagnostics and diagnostics[0]['path'] == 'summary'
    assert 'PRIVATE' not in json.dumps(diagnostics)
    request = replace(packet(), repair=True, repair_diagnostics=diagnostics)
    _, _, wire = adapter._payload(request)
    assert 'repair_diagnostics' in wire['input'][0]['content']
    assert 'Repair the previous invalid response' in wire['instructions']


def test_intake_changes_are_typed_scoped_and_never_applied_automatically():
    request = packet('intake')
    suggestion = {'field':'horizon_months','proposed_value_json':'24','reason':'Confirm a common horizon.','reference_ids':['evidence_owned']}
    result = ProviderOutput.model_validate({**output(), 'proposed_changes':[suggestion]})
    validate_output(result, request)
    assert request.brief['horizon_months'] != 24
    with pytest.raises(ProviderFailure, match='unexpected_brief_change'):
        validate_output(result, packet('proposal'))
    result.proposed_changes[0].proposed_value_json = '999999'
    with pytest.raises(ProviderFailure, match='schema_invalid'):
        validate_output(result, request)


def test_hidden_support_basis_references_are_scoped():
    claim = {'alias':'new_claim_1','proposition':'Supplier meets requirements','kind':'fact','evidence_status':'supported','reference_ids':[], 'support_bases':[{'reference_ids':['evidence_other_owner']}], 'refuting_reference_ids':[], 'support_scope':'Frozen requirement'}
    result = ProviderOutput.model_validate({**output(), 'claims':[claim]})
    with pytest.raises(ProviderFailure, match='invalid_reference'):
        validate_output(result, packet())


def test_oversized_valid_json_output_is_repaired_before_it_crowds_out_final_review():
    # Each individual field is legal; their combined downstream footprint is
    # the relevant resource. Nothing is silently truncated to make it fit.
    result = ProviderOutput.model_validate({**output(), 'summary': 'S' * 4000, 'dissent': ['A material qualification.']})
    with pytest.raises(ProviderFailure) as error:
        validate_output(result, packet())
    assert error.value.retryable
    assert error.value.diagnostics[0]['code'] == 'contribution_context_envelope'
    assert result.summary == 'S' * 4000 and result.dissent


def test_packet_keeps_full_claims_once_and_latest_objection_answer():
    claim = {'id':'claim_prior', 'proposition':'An attributed fact', 'reference_ids':['evidence_owned']}
    issue = {'id':'objection_prior','concern':'Check delivery','severity':'critical','events':[
        {'disposition':'opened','response':'Check delivery'},
        {'disposition':'answered','response':'A current schedule is cited.','reference_ids':['evidence_owned']}]}
    request = replace(packet('synthesis'), claims=(claim,), open_objections=(issue,), peer_outputs=[{'id':'step_prior','provider':'anthropic','stage':'proposal','output':{**output(),'claims':[claim]}}])
    body = request.packet()
    assert body['claims'] == [claim]
    assert 'claims' not in body['peer_outputs'][0]['output']
    assert body['open_objections'][0]['latest_answer']['response'] == 'A current schedule is cited.'
    assert 'events' not in body['open_objections'][0]
    assert issue['events'][0]['disposition'] == 'opened'  # saved history preserved


def test_a_preference_or_assumption_cannot_transmit_factual_support():
    from round_robin.research.claims import grounded_claim_aliases
    for kind in ('preference', 'assumption', 'recommendation'):
        prior = {'id':'claim_prior', 'kind':kind, 'evidence_status':'supported', 'reference_ids':['evidence_owned']}
        result = grounded_claim_aliases([{'alias':'new_claim_1','kind':'fact','evidence_status':'supported','reference_ids':['claim_prior']}],
            support_reference_ids={'evidence_owned'}, prior_claims=[prior], reference_statuses={'evidence_owned':'user_supplied'})
        assert result == set()
