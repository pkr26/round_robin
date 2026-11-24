from copy import deepcopy

import pytest

from backend.tests.test_image_provider import image_configuration as image_configuration  # noqa: PLC0414
from backend.tests.test_image_worker import FakeImageAdapter, image_factory as image_factory, seed_image  # noqa: PLC0414
from round_robin.database.models import BudgetOperation
from round_robin.database.visual_models import DesignOperation, DesignSession
from round_robin.worker.image_tasks import execute_image_operation


@pytest.mark.parametrize('owner', ['owner_image', 'foreign_owner'])
def test_unreadable_active_sibling_fences_before_provider_preflight_and_preserves_hold(image_factory, image_configuration, tmp_path, owner):
    design_id, operation_id = seed_image(image_factory, image_configuration)
    with image_factory.begin() as session:
        design = session.get(DesignSession, design_id)
        initial_state = deepcopy(design.state)
        session.add(BudgetOperation(id='hold_malformed', run_id=design_id, owner_id=owner,
            kind='visual_finish_hold', status='held', reserved_usd='0.40', metadata_json=[]))
    class Provider(FakeImageAdapter):
        preflights = 0
        def preflight(self, packet, authorization):
            self.preflights += 1
    adapter = Provider('openai', image_configuration, image_factory)
    result = execute_image_operation(operation_id, session_factory=image_factory,
                                    provider_registry={'openai': adapter}, artifact_directory=tmp_path)
    assert result['status'] == 'blocked' and adapter.calls == adapter.preflights == 0
    with image_factory() as session:
        assert session.get(DesignOperation, operation_id).payload['status'] == 'blocked'
        assert session.get(DesignSession, design_id).state == initial_state
        assert session.get(BudgetOperation, operation_id) is None
        assert session.get(BudgetOperation, f'visualfinish_{design_id}') is None
        row = session.get(BudgetOperation, 'hold_malformed')
        assert row.metadata_json == [] and row.reserved_usd == '0.40' and row.status == 'held'
