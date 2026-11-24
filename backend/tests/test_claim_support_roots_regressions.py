from datetime import timedelta

import pytest

from backend.tests.test_evidence_regressions import claim, complete_brief
from backend.tests.test_orchestration import factory as factory, seed  # noqa: PLC0414
from round_robin.database.models import EvidenceRecord, Run, Step, utcnow
from round_robin.fixtures import sample_bundle
from round_robin.orchestration.reporting import comparison_as_of, comparison_data
from round_robin.research.reuse import _reference_statuses, _support_reference_ids, claim_packet, persist_claims
from round_robin.schemas.views import StoredEvidence


def add_source(session, run, identity, status):
    original = sample_bundle().evidence[0].model_dump(mode='json')
    content = {key: value for key, value in original.items() if key in StoredEvidence.model_fields}
    content.update(id=identity, run_id=run.id, owner_id=run.owner_id, origin='user_supplied', verification_status=status)
    content = StoredEvidence.model_validate(content).model_dump(mode='json', exclude_unset=True)
    session.add(EvidenceRecord(id=identity, run_id=run.id, meeting_id=run.meeting_id, owner_id=run.owner_id, content=content, sha256=content['sha256']))
    session.flush()


def test_only_current_run_evidence_and_nonempty_calculations_are_roots(factory):
    brief = complete_brief()
    brief.options[1].cost_components[0].amount = None
    brief.options[1].cost_components[0].unknown_reason = 'Synthetic quote unavailable'
    run_id, foreign_id = seed(factory, brief=brief), seed(factory, suffix='foreign')
    with factory.begin() as session:
        run, foreign = session.get(Run, run_id), session.get(Run, foreign_id)
        add_source(session, run, 'evidence_current', 'user_supplied')
        add_source(session, foreign, 'evidence_foreign', 'supported')
        _, calculations = comparison_data(brief, as_of=comparison_as_of(run))
        known, unknown = calculations[0]['id'], calculations[1]['id']
        known_ids = {item['id'] for item in calculations if item['output'] is not None}
        assert calculations[0]['output'] is not None and calculations[1]['output'] is None
        assert _support_reference_ids(session, run) == {'evidence_current'} | known_ids
        assert _support_reference_ids(session, run, set()) == set()
        assert _support_reference_ids(session, run, {'evidence_current', 'evidence_foreign', known, unknown}) == {'evidence_current', known}
        assert _support_reference_ids(session, run, {known}) == {known}
        assert _reference_statuses(session, run) == {'evidence_current': 'user_supplied'}


@pytest.mark.parametrize('status,expected', [('user_supplied', 'supported'), ('supported', 'supported'), ('not_checked', 'not_checked'), ('mixed', 'not_checked'), ('contradicted', 'not_checked')])
def test_persisted_claim_cannot_upgrade_unchecked_or_adverse_source(factory, status, expected):
    run_id = seed(factory, brief=complete_brief())
    with factory.begin() as session:
        run = session.get(Run, run_id)
        run.configuration = {**run.configuration, 'mode': 'live'}
        add_source(session, run, 'evidence_source', status)
        step = Step(id='step_claim', run_id=run_id, stage='proposal', role_slot_id='proposal', provider='openai', status='succeeded')
        session.add(step)
        session.flush()
        saved = persist_claims(session, run, step, {'claims': [claim(refs=['evidence_source'])]}, {'evidence_source'})
        assert saved['claims'][0]['evidence_status'] == expected
        assert claim_packet(session, run)[0]['evidence_status'] == expected


def test_quote_expiry_changes_calculation_root_as_of_immutable_run_date(factory):
    before = utcnow().date()
    brief = complete_brief(valid_until=before)
    run_id = seed(factory, brief=brief)
    with factory.begin() as session:
        run = session.get(Run, run_id)
        run.created_at = utcnow() + timedelta(days=3)
        _, calculations = comparison_data(brief, as_of=comparison_as_of(run))
        assert calculations and all(item['output'] is None for item in calculations)
        assert _support_reference_ids(session, run) == set()


@pytest.mark.parametrize('resolved_complete', [False, True])
def test_roots_follow_resolved_brief_instead_of_historical_input(factory, resolved_complete):
    complete = complete_brief()
    incomplete = complete.model_copy(deep=True)
    incomplete.options[0].cost_components[0].amount = None
    incomplete.options[0].cost_components[0].unknown_reason = 'Synthetic quote missing'
    original, resolved = (incomplete, complete) if resolved_complete else (complete, incomplete)
    run_id = seed(factory, brief=original)
    with factory.begin() as session:
        run = session.get(Run, run_id)
        run.configuration = {**run.configuration, 'resolved_brief': resolved.model_dump(mode='json')}
        _, expected = comparison_data(resolved, as_of=comparison_as_of(run))
        assert (expected[0]['output'] is not None) is resolved_complete
        assert _support_reference_ids(session, run) == {item['id'] for item in expected if item['output'] is not None}
