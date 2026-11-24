import hashlib
import json
from copy import deepcopy

import pytest
from round_robin.database.visual_models import (
    DesignOperation,
    DesignSession,
    DesignVersion,
)
from round_robin.visual.assets import validate_raster
from round_robin.visual.iterations import VisualLimit
from round_robin.visual.schemas import Finding
from round_robin.visual.service import commit_review, commit_version, queue_version_reviews
from sqlalchemy import select

from backend.tests.test_exhaustive_visual_service import saved_version as saved_version  # noqa: PLC0414
from backend.tests.test_image_provider import image_configuration as image_configuration  # noqa: PLC0414
from backend.tests.test_image_worker import image_factory as image_factory  # noqa: PLC0414
from backend.tests.test_visual import png
from backend.tests.test_visual_review_commit_mutation_regressions import new_critique, new_receipt


@pytest.mark.parametrize('status', ['queued', 'claimed', 'authorized', 'running'])
def test_existing_critic_work_is_not_duplicated_but_other_critic_is_scheduled(saved_version, status):
    factory, design_id, version_id, _ = saved_version
    with factory.begin() as session:
        design = session.get(DesignSession, design_id)
        version = session.get(DesignVersion, version_id)
        existing = DesignOperation(id='existing_critic', session_id=design.id, provider='anthropic',
                                   payload={'kind': 'vision', 'step_id': f'vision:{version.id}:anthropic', 'status': status})
        session.add(existing)
        session.flush()
        rows = queue_version_reviews(session, design, version)
        assert [row.provider for row in rows] == ['google']
        session.flush()
        assert len(list(session.scalars(select(DesignOperation).where(DesignOperation.id == 'existing_critic')))) == 1


def test_incomplete_recovery_cannot_queue_a_second_supplement(saved_version):
    factory, design_id, parent_id, directory = saved_version
    with factory.begin() as session:
        design = session.get(DesignSession, design_id)
        parent = session.get(DesignVersion, parent_id)
        finding = Finding(id='finding_prior', description='Missing title', category='observed_defect', severity='critical')
        delivery = new_receipt(parent)
        commit_review(session, design, parent, new_critique(parent, delivery, findings=[finding]), delivery, expected_fence=design.execution_fence)
        session.flush()
        child = commit_version(session, design, validate_raster(png()), directory / 'visual', expected_fence=design.execution_fence,
                               parent_version_id=parent.id, source_kind='native_diagram')
        first_delivery = new_receipt(child)
        commit_review(session, design, child, new_critique(child, first_delivery), first_delivery, expected_fence=design.execution_fence)
        session.flush()
        second_delivery = new_receipt(child)
        supplement = commit_review(session, design, child, new_critique(child, second_delivery), second_delivery, expected_fence=design.execution_fence)
        assert supplement.payload['kind'] == 'review_supplement'
        session.flush()
        with pytest.raises(VisualLimit, match='recovery review'):
            queue_version_reviews(session, design, child)
        assert not any(isinstance(row, DesignOperation) and row.payload.get('kind') == 'vision' for row in session.new)


def test_queued_review_binds_current_version_scope_and_canonical_request_hash(saved_version):
    factory, design_id, version_id, _ = saved_version
    with factory.begin() as session:
        design = session.get(DesignSession, design_id)
        version = session.get(DesignVersion, version_id)
        config = deepcopy(design.configuration)
        config['operator_configuration_hash'] = 'a' * 64
        design.configuration = config
        design.frozen_brief = dict(reversed(list(design.frozen_brief.items())))
        rows = queue_version_reviews(session, design, version)
        assert {row.provider for row in rows} == {'anthropic', 'google'}
        for row in rows:
            payload = row.payload
            request = {'kind': 'vision', 'version_id': version.id, 'input_hash': version.canonical_hash,
                       'brief_hash': hashlib.sha256(json.dumps(design.frozen_brief, sort_keys=True).encode()).hexdigest(),
                       'configuration_hash': config['operator_configuration_hash'], 'provider': row.provider}
            assert all(payload[key] == value for key, value in request.items())
            assert payload['request_hash'] == hashlib.sha256(json.dumps(request, sort_keys=True).encode()).hexdigest()
            assert payload['step_id'] == f'vision:{version.id}:{row.provider}'
            assert payload['target_version_id'] == version.id and payload['parent_version_id'] is None
            assert payload['status'] == 'queued' and payload['dispatch_status'] == 'pending'
            assert payload['owner_id'] == design.owner_id and payload['session_id'] == design.id
            assert payload['fence'] == design.execution_fence and payload['epoch'] == design.deployment_epoch
            assert payload['bound_usd'] == config['vision_prices'][row.provider]
            assert payload['review_pool_tag'] == version.id
            assert payload['initial_admission_expires_at'] == design.preflight['initial_admission_expires_at']
            assert payload['queue_accepted_at']


def test_optional_absent_preflight_does_not_invent_admission_expiry(saved_version):
    factory, design_id, version_id, _ = saved_version
    with factory.begin() as session:
        design = session.get(DesignSession, design_id)
        version = session.get(DesignVersion, version_id)
        design.preflight = None
        rows = queue_version_reviews(session, design, version)
        assert len(rows) == 2
        assert all(row.payload['initial_admission_expires_at'] is None for row in rows)


@pytest.mark.parametrize('missing', ['critic_providers', 'capabilities', 'critic_capability'])
def test_missing_frozen_critic_configuration_fails_closed(saved_version, missing):
    factory, design_id, version_id, _ = saved_version
    with factory.begin() as session:
        design = session.get(DesignSession, design_id)
        version = session.get(DesignVersion, version_id)
        config = deepcopy(design.configuration)
        if missing == 'critic_capability':
            config['capabilities'].pop('anthropic')
        else:
            config.pop(missing)
        design.configuration = config
        with pytest.raises(VisualLimit):
            queue_version_reviews(session, design, version)
