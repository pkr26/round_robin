"""An existing corrupted claim still fences immutable step overwrite."""
import pytest
from round_robin.database.claim_models import ClaimVersion
from round_robin.database.models import Run, Step, utcnow
from round_robin.database.services import canonical_hash
from round_robin.research.claims import ClaimRejected
from round_robin.research.reuse import StoredClaim, persist_claims

from backend.tests.test_orchestration import factory as factory  # noqa: PLC0414
from backend.tests.test_orchestration import seed


def test_existing_claim_prevents_overwrite_without_decoding_damaged_json(factory):
    run_id = seed(factory)
    with factory.begin() as session:
        run = session.get(Run, run_id)
        step = Step(id='step_existing', run_id=run_id, stage='proposal', role_slot_id='proposal_1', provider='openai', status='succeeded')
        session.add(step)
        content = StoredClaim.model_validate({'id': 'claim_existing', 'version': 1, 'proposition': 'Synthetic claim',
            'kind': 'assumption', 'evidence_status': 'not_checked', 'reference_ids': [],
            'time_sensitive': False, 'verified_at': utcnow()}).model_dump(mode='json')
        session.add(ClaimVersion(id=content['id'], run_id=run_id, meeting_id=run.meeting_id, owner_id=run.owner_id,
            version=1, step_id=step.id, content=content, sha256=canonical_hash(content)))
    with factory.kw['bind'].begin() as connection:
        connection.exec_driver_sql('UPDATE claim_versions SET content = ? WHERE id = ?', ('{"sentinel": "PRIVATE_CLAIM', 'claim_existing'))
    with factory.begin() as session:
        run = session.get(Run, run_id)
        step = session.get(Step, 'step_existing')
        with pytest.raises(ClaimRejected, match='cannot overwrite') as error:
            persist_claims(session, run, step, {'claims': [{'alias': 'new_claim_1'}]}, set())
        assert 'PRIVATE_CLAIM' not in str(error.value)
