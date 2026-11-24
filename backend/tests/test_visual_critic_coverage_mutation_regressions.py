from round_robin.database.visual_models import DesignSession, DesignVersion
from round_robin.visual.assets import validate_raster
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
    complete_version_critics,
    queue_version_reviews,
    version_regression_targets,
)

from backend.tests.test_exhaustive_visual_service import saved_version as saved_version  # noqa: PLC0414
from backend.tests.test_image_provider import image_configuration as image_configuration  # noqa: PLC0414
from backend.tests.test_image_worker import image_factory as image_factory  # noqa: PLC0414
from backend.tests.test_visual import png


def test_critic_completeness_combines_only_that_critics_saved_reviews(saved_version):
    factory, design_id, parent_id, directory = saved_version
    with factory.begin() as session:
        design = session.get(DesignSession, design_id)
        parent = session.get(DesignVersion, parent_id)
        raw = png()
        raster = validate_raster(raw)

        def receipt(version, provider):
            capabilities = ImageCapabilities(provider=provider, endpoint_version='synthetic', understands_images=True, supported_media=['image/png'])
            views = [PixelView(raster.canonical_bytes, version.canonical_hash)]
            if version.parent_version_id:
                views.append(PixelView(raster.canonical_bytes, parent.canonical_hash, f'comparison_parent_version_{parent.id}'))
            return issue_review_receipt(capabilities, version.id, version.canonical_hash, views)

        parent_receipt = receipt(parent, 'anthropic')
        findings = [Finding(id='finding_one', description='Missing title', category='observed_defect', severity='critical'),
                    Finding(id='finding_two', description='Unreadable label', category='observed_defect', severity='material')]
        commit_review(session, design, parent, Critique(provider='anthropic', version_id=parent.id, canonical_hash=parent.canonical_hash,
                      receipt_id=parent_receipt.id, findings=findings), parent_receipt, expected_fence=design.execution_fence)
        child = commit_version(session, design, raster, directory / 'visual', expected_fence=design.execution_fence,
                               parent_version_id=parent.id, source_kind='native_diagram')
        targets = version_regression_targets(session, design, child)
        assert len(targets) == 2

        def submit(provider, indexes):
            current_receipt = receipt(child, provider)
            checks = [RegressionCheck(target_id=targets[index]['id'], result='resolved', reason='Visible in supplied image') for index in indexes]
            review = Critique(provider=provider, version_id=child.id, canonical_hash=child.canonical_hash,
                              receipt_id=current_receipt.id, regression_checks=checks)
            return commit_review(session, design, child, review, current_receipt, expected_fence=design.execution_fence)

        submit('anthropic', [0])
        submit('google', [0, 1])
        session.flush()
        assert complete_version_critics(session, design, child) == {'google'}
        assert [row.provider for row in queue_version_reviews(session, design, child)] == ['anthropic']
        supplement = submit('anthropic', [1])
        session.flush()
        assert supplement.payload['kind'] == 'review_supplement'
        assert complete_version_critics(session, design, child) == {'anthropic', 'google'}
        assert queue_version_reviews(session, design, child) == []
        incomplete = {key: value for key, value in supplement.payload['critique'].items() if key != 'regression_checks'}
        supplement.payload = {**supplement.payload, 'critique': incomplete}
        session.flush()
        assert complete_version_critics(session, design, child) == {'google'}
