import pytest

from backend.tests.test_orchestration import factory as factory, seed  # noqa: PLC0414
from round_robin.database.claim_models import ClaimVersion
from round_robin.database.models import EvidenceRecord, Run, utcnow
from round_robin.database.services import canonical_hash
from round_robin.research.reuse import ClaimRejected, StoredClaim, claim_packet
from backend.tests.test_claim_support_roots_regressions import add_source


def test_claim_packet_refreshes_previously_loaded_evidence_identity(factory):
    run_id = seed(factory)
    with factory.begin() as session:
        run = session.get(Run, run_id)
        add_source(session, run, 'evidence_source', 'supported')
        raw = {'id': 'claim_existing', 'version': 1, 'predecessor_id': None, 'proposition': 'A supported synthetic claim.',
               'kind': 'fact', 'evidence_status': 'supported', 'reference_ids': ['evidence_source'],
               'time_sensitive': True, 'verified_at': utcnow().isoformat()}
        content = StoredClaim.model_validate(raw).model_dump(mode='json')
        session.add(ClaimVersion(id=content['id'], version=1, run_id=run_id, meeting_id=run.meeting_id, owner_id=run.owner_id,
                                 step_id=None, content=content, sha256=canonical_hash(content)))
    with factory() as reader:
        run = reader.get(Run, run_id)
        cached = reader.get(EvidenceRecord, 'evidence_source')
        assert cached.sha256 != '0' * 64
        with factory.begin() as writer:
            writer.get(EvidenceRecord, 'evidence_source').sha256 = '0' * 64
        with pytest.raises(ClaimRejected):
            claim_packet(reader, run)
