from copy import deepcopy

import pytest

from backend.tests.test_image_provider import image_configuration as image_configuration  # noqa: PLC0414
from backend.tests.test_image_worker import FakeImageAdapter, image_factory as image_factory, seed_image  # noqa: PLC0414
from round_robin.database.models import BudgetOperation
from round_robin.database.visual_models import DesignOperation, DesignSession
from round_robin.worker.image_tasks import execute_image_operation, reconcile_image_operations


@pytest.mark.parametrize("damage", ["owner", "run", "kind", "metadata"])
def test_reconciler_never_releases_misbound_or_malformed_canonical_pool(image_factory, image_configuration, damage):
    design_id, _ = seed_image(image_factory, image_configuration)
    identity = f"visualfinish_{design_id}"
    with image_factory.begin() as session:
        design = session.get(DesignSession, design_id)
        design.status = "cancelled"
        session.add(BudgetOperation(id=identity, run_id="other_design" if damage == "run" else design_id,
            owner_id="foreign_owner" if damage == "owner" else design.owner_id,
            kind="visual_edit" if damage == "kind" else "visual_finish_hold", status="held", reserved_usd="0.42",
            metadata_json=[] if damage == "metadata" else {"target_tag": "synthetic", "roles_remaining": []}))
    with image_factory() as session:
        row = session.get(BudgetOperation, identity)
        original = (row.owner_id, row.run_id, row.kind, row.status, row.reserved_usd, deepcopy(row.metadata_json))
    reconcile_image_operations(session_factory=image_factory)
    with image_factory() as session:
        row = session.get(BudgetOperation, identity)
        assert (row.owner_id, row.run_id, row.kind, row.status, row.reserved_usd, row.metadata_json) == original


@pytest.mark.parametrize("damage", ["owner", "run", "kind", "metadata", "status", "released", "noncritic", "underfunded", "empty_positive", "surplus"])
def test_generation_fences_bad_pool_before_provider_or_new_ledger(image_factory, image_configuration, tmp_path, damage):
    design_id, operation_id = seed_image(image_factory, image_configuration)
    identity = f"visualfinish_{design_id}"
    with image_factory.begin() as session:
        design = session.get(DesignSession, design_id)
        roles = ["openai", "google"] if damage == "noncritic" else ["anthropic", "google"]
        session.add(BudgetOperation(id=identity, run_id="other_design" if damage == "run" else design_id,
            owner_id="foreign_owner" if damage == "owner" else design.owner_id,
            kind="visual_edit" if damage == "kind" else "visual_finish_hold", status="unknown" if damage == "status" else "released" if damage == "released" else "held", reserved_usd="0" if damage == "released" else "0.01" if damage == "underfunded" else "0.42",
            metadata_json=[] if damage == "metadata" else {} if damage == "empty_positive" else {"target_tag": "generation:version_1", "roles_remaining": roles}))
    class PreflightAdapter(FakeImageAdapter):
        preflights = 0
        def preflight(self, packet, authorization):
            self.preflights += 1
    adapter = PreflightAdapter("openai", image_configuration, image_factory)
    with image_factory() as session:
        pool = session.get(BudgetOperation, identity)
        original = (pool.owner_id, pool.run_id, pool.kind, pool.status, pool.reserved_usd, deepcopy(pool.metadata_json))
    result = execute_image_operation(operation_id, session_factory=image_factory, provider_registry={"openai": adapter}, artifact_directory=tmp_path)
    assert result["status"] == "blocked" and adapter.calls == 0 and adapter.preflights == 0
    with image_factory() as session:
        pool = session.get(BudgetOperation, identity)
        assert (pool.owner_id, pool.run_id, pool.kind, pool.status, pool.reserved_usd, pool.metadata_json) == original
        assert session.get(BudgetOperation, operation_id) is None
        assert session.get(DesignOperation, operation_id).payload["status"] == "blocked"


def test_finish_pool_backing_is_exact_with_hostile_decimal_context(image_factory, image_configuration):
    from decimal import Inexact, Rounded, getcontext, localcontext

    from round_robin.visual.iterations import VisualLimit
    from round_robin.worker.image_tasks import _checked_finish_pool

    design_id, _ = seed_image(image_factory, image_configuration)
    with image_factory.begin() as session:
        design = session.get(DesignSession, design_id)
        config = deepcopy(design.configuration)
        config['vision_prices']['anthropic'] = '0.100001'
        config['vision_prices']['google'] = '0.200002'
        design.configuration = config
        session.add(BudgetOperation(id=f'visualfinish_{design_id}', run_id=design_id, owner_id=design.owner_id,
            kind='visual_finish_hold', status='held', reserved_usd='0.300003',
            metadata_json={'target_tag': 'generation:version_1', 'roles_remaining': ['anthropic', 'google']}))
    with image_factory() as session, localcontext() as context:
        context.prec = 1
        context.traps[Inexact] = True
        context.traps[Rounded] = True
        design = session.get(DesignSession, design_id)
        pool = _checked_finish_pool(session, design)
        assert pool.reserved_usd == '0.300003'
        assert getcontext().prec == 1 and getcontext().traps[Inexact] and getcontext().traps[Rounded]
        pool.reserved_usd = '0.300002'
        session.flush()
        with pytest.raises(VisualLimit, match='underfunded'):
            _checked_finish_pool(session, design)


@pytest.mark.parametrize('damage', [
    'duplicate_role', 'wrong_role', 'duplicate_critics', 'wrong_price_key',
    'negative', 'zero', 'nan', 'infinity', 'exponent', 'too_precise', 'too_large', 'number',
])
def test_finish_pool_rejects_invalid_role_and_price_without_mutation(image_factory, image_configuration, damage):
    from round_robin.visual.iterations import VisualLimit
    from round_robin.worker.image_tasks import _checked_finish_pool

    design_id, _ = seed_image(image_factory, image_configuration)
    with image_factory.begin() as session:
        design = session.get(DesignSession, design_id)
        config = deepcopy(design.configuration)
        roles = ['anthropic', 'google']
        if damage == 'duplicate_role':
            roles = ['anthropic', 'anthropic']
        elif damage == 'wrong_role':
            roles = ['anthropic', 'openai']
        elif damage == 'duplicate_critics':
            config['critic_providers'] = ['anthropic', 'anthropic']
        elif damage == 'wrong_price_key':
            del config['vision_prices']['google']
        else:
            values = {'negative': '-0.10', 'zero': '0', 'nan': 'NaN', 'infinity': 'Infinity',
                      'exponent': '1e-1', 'too_precise': '0.1000001', 'too_large': '1000000000001', 'number': 0.1}
            config['vision_prices']['google'] = values[damage]
        design.configuration = config
        session.add(BudgetOperation(id=f'visualfinish_{design_id}', run_id=design_id, owner_id=design.owner_id,
            kind='visual_finish_hold', status='held', reserved_usd='0.20',
            metadata_json={'target_tag': 'generation:version_1', 'roles_remaining': roles}))
    with image_factory() as session:
        design = session.get(DesignSession, design_id)
        pool = session.get(BudgetOperation, f'visualfinish_{design_id}')
        saved = (pool.status, pool.reserved_usd, deepcopy(pool.metadata_json))
        with pytest.raises(VisualLimit):
            _checked_finish_pool(session, design)
        assert (pool.status, pool.reserved_usd, pool.metadata_json) == saved
