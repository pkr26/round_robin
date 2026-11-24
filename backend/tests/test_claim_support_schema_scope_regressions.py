"""Large but schema-valid saved claim references avoid SQL bind exhaustion."""
from round_robin.database.models import Run, utcnow
from round_robin.research.reuse import (
    StoredClaim,
    _claim_references,
    _reference_statuses,
)

from backend.tests.test_orchestration import factory as factory  # noqa: PLC0414
from backend.tests.test_orchestration import seed


def test_schema_bounded_claim_reference_union_does_not_exhaust_sql_bindings(factory):
    run_id = seed(factory)
    claims = []
    for i in range(84):
        refs = [f'evidence_synthetic_{i}_{j}' for j in range(400)]
        claim = StoredClaim.model_validate({'id': f'claim_{i}', 'version': 1, 'proposition': 'Unverified historical claim',
            'kind': 'fact', 'evidence_status': 'not_checked', 'reference_ids': refs[:40],
            'refuting_reference_ids': refs[40:80], 'support_bases': [{'reference_ids': refs[k:k + 40]} for k in range(80, 400, 40)],
            'time_sensitive': True, 'verified_at': utcnow()})
        claims.append(claim.model_dump(mode='json'))
    references = _claim_references(claims)
    assert len(references) == 33600
    with factory() as session:
        run = session.get(Run, run_id)
        assert _reference_statuses(session, run, references) == {}
