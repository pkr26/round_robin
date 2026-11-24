from sqlalchemy import event

from backend.tests.test_evidence_regressions import claim
from backend.tests.test_orchestration import factory as factory, seed  # noqa: PLC0414
from round_robin.database.claim_models import ClaimVersion
from round_robin.database.models import Run, Step, utcnow
from round_robin.database.services import canonical_hash
from round_robin.research.reuse import StoredClaim, persist_claims


def test_large_allowed_packet_binds_only_bounded_existing_claim_ids(factory):
    run_id = seed(factory)
    with factory.begin() as session:
        run = session.get(Run, run_id)
        for identity in ('claim_prior', 'claim_unrelated'):
            content = StoredClaim.model_validate({'id': identity, 'version': 1, 'proposition': 'Synthetic assumption',
                'kind': 'assumption', 'evidence_status': 'not_checked', 'time_sensitive': True,
                'verified_at': utcnow().isoformat()}).model_dump(mode='json')
            session.add(ClaimVersion(id=identity, version=1, run_id=run.id, meeting_id=run.meeting_id,
                owner_id=run.owner_id, step_id=None, content=content, sha256=canonical_hash(content)))
        session.add(Step(id='step_current', run_id=run.id, stage='proposal', role_slot_id='proposal_1', provider='openai', status='succeeded'))
    # Unreferenced corrupt content must not be hydrated as a side effect of filtering IDs.
    with factory.kw['bind'].begin() as connection:
        connection.exec_driver_sql('UPDATE claim_versions SET content = ? WHERE id = ?', ('{invalid unrelated', 'claim_unrelated'))
    allowed = {f'evidence_{index:06d}' for index in range(66000)} | {'claim_prior'}
    parameter_counts = []
    engine = factory.kw['bind']
    def observe(connection, cursor, statement, parameters, context, executemany):
        if 'FROM claim_versions' in statement:
            parameter_counts.append(len(parameters))
    event.listen(engine, 'before_cursor_execute', observe)
    try:
        with factory.begin() as session:
            run, step = session.get(Run, run_id), session.get(Step, 'step_current')
            result = persist_claims(session, run, step, {'claims': [claim(refs=['claim_prior'])]}, allowed)
            assert result['claims'][0]['evidence_status'] == 'fictional'
    finally:
        event.remove(engine, 'before_cursor_execute', observe)
    assert parameter_counts and max(parameter_counts) <= 201
