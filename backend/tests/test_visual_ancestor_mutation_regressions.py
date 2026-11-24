import sys
from types import SimpleNamespace

import pytest
from round_robin.database.visual_models import (
    DesignOperation,
    DesignSession,
    DesignVersion,
)
from round_robin.visual.assets import validate_raster
from round_robin.visual.iterations import VisualLimit
from round_robin.visual.reviews import PixelView, issue_review_receipt
from round_robin.visual.schemas import (
    Critique,
    Finding,
    ImageCapabilities,
    RegressionCheck,
)
from round_robin.visual.service import (
    commit_review,
    commit_version,
    version_feedback_findings,
    version_regression_targets,
)
from sqlalchemy import event

from backend.tests.test_exhaustive_visual_service import saved_version as saved_version  # noqa: PLC0414
from backend.tests.test_image_provider import image_configuration as image_configuration  # noqa: PLC0414
from backend.tests.test_image_worker import image_factory as image_factory  # noqa: PLC0414
from backend.tests.test_visual import png


def receipt(version, provider, pixels):
    caps = ImageCapabilities(provider=provider, endpoint_version='synthetic', understands_images=True, supported_media=['image/png'])
    return issue_review_receipt(caps, version.id, version.canonical_hash, [PixelView(pixels, version.canonical_hash)])


def test_foreign_session_and_parent_cycle_are_rejected(saved_version):
    factory, design_id, version_id, _ = saved_version
    with factory.begin() as session:
        design = session.get(DesignSession, design_id)
        version = session.get(DesignVersion, version_id)
        with pytest.raises(VisualLimit):
            version_feedback_findings(session, SimpleNamespace(id='foreign_design'), version)
        version.parent_version_id = version.id
        session.flush()
        queries = []
        def count_query(*_args):
            queries.append(1)
        event.listen(session.bind, 'before_cursor_execute', count_query)
        old_limit = sys.getrecursionlimit()
        try:
            sys.setrecursionlimit(250)
            with pytest.raises(VisualLimit):
                version_feedback_findings(session, design, version)
        finally:
            sys.setrecursionlimit(old_limit)
            event.remove(session.bind, 'before_cursor_execute', count_query)
        assert len(queries) <= 6


def test_generator_review_cannot_discharge_prior_critic_obligation(saved_version):
    factory, design_id, version_id, directory = saved_version
    with factory.begin() as session:
        design = session.get(DesignSession, design_id)
        parent = session.get(DesignVersion, version_id)
        raster = validate_raster(png())
        delivery = receipt(parent, 'google', raster.canonical_bytes)
        prior = Finding(id='finding_prior', description='Title missing', category='observed_defect', severity='critical')
        committed = commit_review(session, design, parent, Critique(provider='google', version_id=parent.id,
                                  canonical_hash=parent.canonical_hash, receipt_id=delivery.id, findings=[prior]),
                                  delivery, expected_fence=design.execution_fence)
        parent_finding_id = committed.critique['findings'][0]['id']
        child = commit_version(session, design, raster, directory / 'visual', expected_fence=design.execution_fence,
                               parent_version_id=parent.id, source_kind='native_diagram')
        child.generator_provider = 'anthropic'  # Simulate saved generated-version provenance.
        targets = version_regression_targets(session, design, child)
        assert [target['id'] for target in targets] == [parent_finding_id]
        for provider in ('anthropic', 'google'):
            delivery = receipt(child, provider, raster.canonical_bytes)
            check = RegressionCheck(target_id=parent_finding_id, result='resolved', reason='Seen in current pixels')
            own = Finding(id='finding_child', description='Minor new mark', category='observed_defect', severity='minor')
            commit_review(session, design, child, Critique(provider=provider, version_id=child.id,
                          canonical_hash=child.canonical_hash, receipt_id=delivery.id, findings=[own], regression_checks=[check]),
                          delivery, expected_fence=design.execution_fence)
        session.flush()
        assert parent_finding_id in {row['id'] for row in version_feedback_findings(session, design, child)}


def test_forty_saved_findings_are_accepted_but_excess_fails_closed(saved_version):
    factory, design_id, version_id, _ = saved_version
    with factory.begin() as session:
        design = session.get(DesignSession, design_id)
        version = session.get(DesignVersion, version_id)
        pixels = validate_raster(png()).canonical_bytes
        for provider in ('anthropic', 'google'):
            delivery = receipt(version, provider, pixels)
            findings = [Finding(id=f'finding_{i}', description=f'Defect {i}', category='observed_defect', severity='minor') for i in range(20)]
            commit_review(session, design, version, Critique(provider=provider, version_id=version.id,
                          canonical_hash=version.canonical_hash, receipt_id=delivery.id, findings=findings),
                          delivery, expected_fence=design.execution_fence)
        session.flush()
        assert len(version_feedback_findings(session, design, version)) == 40
        extra = Finding(id='finding_extra', description='Another defect', category='observed_defect', severity='minor').model_dump(mode='json')
        session.add(DesignOperation(id='supplement_extra', session_id=design.id, provider='google',
                                    payload={'kind': 'review_supplement', 'version_id': version.id, 'critique': {'findings': [extra]}}))
        session.flush()
        with pytest.raises(VisualLimit):
            version_feedback_findings(session, design, version)
