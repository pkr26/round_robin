import pytest
from round_robin.database.models import Meeting, utcnow
from round_robin.database.visual_models import (
    DesignOperation,
    DesignReview,
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
    version_regression_targets,
)
from sqlalchemy import select

from backend.tests.test_exhaustive_visual_service import saved_version as saved_version  # noqa: PLC0414
from backend.tests.test_image_provider import image_configuration as image_configuration  # noqa: PLC0414
from backend.tests.test_image_worker import image_factory as image_factory  # noqa: PLC0414
from backend.tests.test_visual import png


def new_receipt(version, provider='anthropic'):
    pixels = validate_raster(png()).canonical_bytes
    capability = ImageCapabilities(provider=provider, endpoint_version='synthetic', understands_images=True, supported_media=['image/png'])
    return issue_review_receipt(capability, version.id, version.canonical_hash, [PixelView(pixels, version.canonical_hash)])


def new_critique(version, delivery, *, findings=(), checks=()):
    return Critique(provider=delivery.provider, version_id=version.id, canonical_hash=version.canonical_hash,
                    receipt_id=delivery.id, findings=list(findings), regression_checks=list(checks))


def new_finding(alias, prior=None):
    return Finding(id=alias, description='Synthetic visual finding', category='observed_defect', severity='critical', previous_finding_id=prior)


def check(identity):
    return RegressionCheck(target_id=identity, result='resolved', reason='Visible in supplied pixels')


@pytest.mark.parametrize('change', ['missing_meeting', 'deleted_meeting', 'foreign_version', 'foreign_provider'])
def test_review_commit_fences_deleted_or_out_of_scope_content(saved_version, change):
    factory, design_id, version_id, _ = saved_version
    with factory.begin() as session:
        design = session.get(DesignSession, design_id)
        version = session.get(DesignVersion, version_id)
        delivery = new_receipt(version)
        critique = new_critique(version, delivery, findings=[new_finding('finding_alias')])
        meeting = session.get(Meeting, design.meeting_id)
        if change == 'missing_meeting':
            session.delete(meeting)
            session.flush()
        elif change == 'deleted_meeting':
            meeting.deleted_at = utcnow()
        elif change == 'foreign_version':
            version.session_id = 'foreign_design'
        else:
            design.recipients = ['openai', 'google']
        with pytest.raises(VisualLimit):
            commit_review(session, design, version, critique, delivery, expected_fence=design.execution_fence)
        session.flush()
        assert session.scalar(select(DesignReview).where(DesignReview.version_id == version.id)) is None


def make_child_with_two_prior_findings(session, design, parent, directory):
    delivery = new_receipt(parent)
    prior = new_critique(parent, delivery, findings=[new_finding('finding_first'), new_finding('finding_second')])
    commit_review(session, design, parent, prior, delivery, expected_fence=design.execution_fence)
    child = commit_version(session, design, validate_raster(png()), directory / 'visual', expected_fence=design.execution_fence,
                           parent_version_id=parent.id, source_kind='native_diagram')
    targets = version_regression_targets(session, design, child)
    assert len(targets) == 2
    return child, [target['id'] for target in targets]


def test_foreign_prior_finding_cannot_be_saved_on_child(saved_version):
    factory, design_id, parent_id, directory = saved_version
    with factory.begin() as session:
        design = session.get(DesignSession, design_id)
        parent = session.get(DesignVersion, parent_id)
        child, _ = make_child_with_two_prior_findings(session, design, parent, directory)
        delivery = new_receipt(child)
        critique = new_critique(child, delivery, findings=[new_finding('finding_new', prior='finding_foreign')])
        with pytest.raises(ValueError):
            commit_review(session, design, child, critique, delivery, expected_fence=design.execution_fence)
        session.flush()
        assert session.scalar(select(DesignReview).where(DesignReview.version_id == child.id)) is None


def test_server_finding_ids_and_original_review_advance_revision_once(saved_version):
    factory, design_id, version_id, _ = saved_version
    with factory.begin() as session:
        design = session.get(DesignSession, design_id)
        version = session.get(DesignVersion, version_id)
        delivery = new_receipt(version)
        initial_revision = design.revision
        row = commit_review(session, design, version, new_critique(version, delivery, findings=[new_finding('finding_alias')]),
                            delivery, expected_fence=design.execution_fence)
        session.flush()
        assert design.revision == initial_revision + 1
        first_id = row.critique['findings'][0]['id']
        assert first_id != 'finding_alias'
        second_delivery = new_receipt(version, 'google')
        second = commit_review(session, design, version, new_critique(version, second_delivery, findings=[new_finding('finding_alias')]),
                               second_delivery, expected_fence=design.execution_fence)
        session.flush()
        assert second.critique['findings'][0]['id'] not in {'finding_alias', first_id}


def test_completed_review_cannot_gain_a_duplicate_supplement(saved_version):
    factory, design_id, parent_id, directory = saved_version
    with factory.begin() as session:
        design = session.get(DesignSession, design_id)
        parent = session.get(DesignVersion, parent_id)
        child, target_ids = make_child_with_two_prior_findings(session, design, parent, directory)
        pixels = validate_raster(png()).canonical_bytes
        capability = ImageCapabilities(provider='anthropic', endpoint_version='synthetic', understands_images=True, supported_media=['image/png'])
        delivery = issue_review_receipt(capability, child.id, child.canonical_hash, [PixelView(pixels, child.canonical_hash), PixelView(pixels, parent.canonical_hash, f'comparison_parent_version_{parent.id}')])
        critique = new_critique(child, delivery, checks=[check(identity) for identity in target_ids])
        commit_review(session, design, child, critique, delivery, expected_fence=design.execution_fence)
        session.flush()
        with pytest.raises(VisualLimit):
            commit_review(session, design, child, critique, delivery, expected_fence=design.execution_fence)
        session.flush()
        assert not [row for row in session.scalars(select(DesignOperation).where(DesignOperation.session_id == design.id)) if row.payload.get('kind') == 'review_supplement']


def test_incomplete_review_allows_one_supplement_without_unbounded_retry(saved_version):
    factory, design_id, parent_id, directory = saved_version
    with factory.begin() as session:
        design = session.get(DesignSession, design_id)
        child, target_ids = make_child_with_two_prior_findings(session, design, session.get(DesignVersion, parent_id), directory)
        delivery = new_receipt(child)
        incomplete = new_critique(child, delivery, checks=[check(target_ids[0])])
        original = commit_review(session, design, child, incomplete, delivery, expected_fence=design.execution_fence)
        session.flush()
        revision = design.revision
        supplement = commit_review(session, design, child, incomplete, delivery, expected_fence=design.execution_fence)
        session.flush()
        assert design.revision == revision + 1
        assert supplement.payload['kind'] == 'review_supplement' and supplement.payload['status'] == 'completed'
        assert supplement.payload['source_review_id'] == original.id and supplement.payload['version_id'] == child.id
        assert supplement.id != original.id
        with pytest.raises(VisualLimit):
            commit_review(session, design, child, new_critique(child, delivery, checks=[check(target_ids[1])]),
                          delivery, expected_fence=design.execution_fence)
