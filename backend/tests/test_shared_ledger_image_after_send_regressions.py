from decimal import Decimal

import pytest

from backend.tests.test_image_provider import image_configuration as image_configuration  # noqa: PLC0414
from backend.tests.test_image_worker import FakeImageAdapter, image_factory as image_factory, seed_image  # noqa: PLC0414
from round_robin.database.models import BudgetOperation
from round_robin.database.visual_models import DesignOperation, DesignSession, DesignVersion
from round_robin.worker.image_tasks import execute_image_operation, reconcile_image_operations


@pytest.mark.parametrize('cost', ['0.75', None])
def test_corrupt_post_send_metadata_retains_raw_ledger_and_fences_publication(image_factory, image_configuration, tmp_path, cost):
    design_id, operation_id = seed_image(image_factory, image_configuration)
    def corrupt(packet, authorization):
        with image_factory.begin() as session:
            session.get(BudgetOperation, authorization.operation_id).metadata_json = []
    class Provider(FakeImageAdapter):
        def submit(self, packet, authorization, *, idempotency_key):
            result = super().submit(packet, authorization, idempotency_key=idempotency_key)
            return {**result, 'actual_usd': cost, 'provider_request_id': 'synthetic-request'}
    provider = Provider('openai', image_configuration, image_factory, callback=corrupt)
    result = execute_image_operation(operation_id, session_factory=image_factory, provider_registry={'openai': provider}, artifact_directory=tmp_path)
    assert result['status'] == 'ledger_integrity_unavailable' and provider.calls == 1
    with image_factory() as session:
        ledger = session.get(BudgetOperation, operation_id)
        design = session.get(DesignSession, design_id)
        operation = session.get(DesignOperation, operation_id)
        assert ledger.metadata_json == [] and Decimal(ledger.reserved_usd) == Decimal('0.40')
        assert ledger.status == ('settled' if cost is not None else 'unknown')
        assert ledger.settled_usd == cost
        assert design.status == 'quarantined' and operation.payload['status'] == 'fenced'
        assert operation.payload['error_code'] == 'ledger_integrity_failed'
        assert operation.payload['provider_request_id'] == 'synthetic-request'
        assert design.state['operations'][operation_id]['status'] == ('settled' if cost is not None else 'unknown')
        assert not session.query(DesignVersion).filter_by(session_id=design_id).count()
    assert execute_image_operation(operation_id, session_factory=image_factory, provider_registry={'openai': provider}, artifact_directory=tmp_path)['status'] == 'already_authorized_or_settled'
    reconcile_image_operations(session_factory=image_factory)
    assert provider.calls == 1
    with image_factory() as session:
        ledger = session.get(BudgetOperation, operation_id)
        assert ledger.metadata_json == [] and ledger.settled_usd == cost


def test_misbound_post_send_ledger_does_not_settle_unproven_identity(image_factory, image_configuration, tmp_path):
    design_id, operation_id = seed_image(image_factory, image_configuration)
    def corrupt(packet, authorization):
        with image_factory.begin() as session:
            ledger = session.get(BudgetOperation, authorization.operation_id)
            ledger.metadata_json, ledger.owner_id = [], 'foreign_owner'
    class Provider(FakeImageAdapter):
        def submit(self, packet, authorization, *, idempotency_key):
            return {**super().submit(packet, authorization, idempotency_key=idempotency_key), 'actual_usd': '0.75'}
    provider = Provider('openai', image_configuration, image_factory, callback=corrupt)
    result = execute_image_operation(operation_id, session_factory=image_factory, provider_registry={'openai': provider}, artifact_directory=tmp_path)
    assert result['status'] == 'persistence_unconfirmed' and provider.calls == 1
    with image_factory() as session:
        ledger = session.get(BudgetOperation, operation_id)
        assert ledger.owner_id == 'foreign_owner' and ledger.metadata_json == []
        assert ledger.status == 'reserved' and ledger.settled_usd is None
        assert session.get(DesignOperation, operation_id).payload['status'] == 'authorized'
