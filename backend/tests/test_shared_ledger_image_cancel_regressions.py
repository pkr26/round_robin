from dataclasses import asdict
from decimal import Decimal

import pytest

from backend.tests.test_image_provider import image_configuration as image_configuration  # noqa: PLC0414
from backend.tests.test_image_worker import FakeImageAdapter, image_factory as image_factory, seed_image  # noqa: PLC0414
from round_robin.database.models import BudgetOperation
from round_robin.database.visual_models import DesignOperation, DesignSession, DesignVersion
from round_robin.visual.iterations import VisualState
from round_robin.worker.image_tasks import execute_image_operation, reconcile_image_operations


@pytest.mark.parametrize('cost', ['0.75', None])
def test_corrupt_metadata_and_cancel_during_paid_send_keep_cost_and_cancel(image_factory, image_configuration, tmp_path, cost):
    design_id, operation_id = seed_image(image_factory, image_configuration)
    def cancel_and_corrupt(packet, authorization):
        with image_factory.begin() as session:
            design = session.get(DesignSession, design_id)
            state = VisualState(**design.state)
            state.cancel()
            design.status, design.execution_fence, design.state = 'cancelled', state.fence, asdict(state)
            session.get(BudgetOperation, operation_id).metadata_json = []
    class Provider(FakeImageAdapter):
        def submit(self, packet, authorization, *, idempotency_key):
            return {**super().submit(packet, authorization, idempotency_key=idempotency_key), 'actual_usd': cost}
    provider = Provider('openai', image_configuration, image_factory, callback=cancel_and_corrupt)
    result = execute_image_operation(operation_id, session_factory=image_factory, provider_registry={'openai': provider}, artifact_directory=tmp_path)
    assert result['status'] == 'ledger_integrity_unavailable'
    with image_factory() as session:
        design, ledger = session.get(DesignSession, design_id), session.get(BudgetOperation, operation_id)
        assert design.status == 'cancelled' and design.state['status'] == 'cancelled'
        assert ledger.metadata_json == [] and Decimal(ledger.reserved_usd) == Decimal('0.40')
        assert ledger.settled_usd == cost and ledger.status == ('settled' if cost else 'unknown')
        assert session.get(DesignOperation, operation_id).payload['status'] == 'fenced'
        assert session.query(DesignVersion).filter_by(session_id=design_id).count() == 0
    execute_image_operation(operation_id, session_factory=image_factory, provider_registry={'openai': provider}, artifact_directory=tmp_path)
    reconcile_image_operations(session_factory=image_factory)
    assert provider.calls == 1
