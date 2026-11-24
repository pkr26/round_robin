import pytest
from round_robin.database.visual_models import (
    DesignReview,
    DesignSession,
    DesignVersion,
)
from round_robin.visual.assets import validate_raster
from round_robin.visual.reviews import PixelView, issue_review_receipt
from round_robin.visual.schemas import Critique, Finding, ImageCapabilities
from round_robin.visual.service import commit_review
from sqlalchemy import select

from backend.tests.test_exhaustive_visual_service import saved_version as saved_version  # noqa: PLC0414
from backend.tests.test_image_provider import image_configuration as image_configuration  # noqa: PLC0414
from backend.tests.test_image_worker import image_factory as image_factory  # noqa: PLC0414
from backend.tests.test_visual import png


def test_review_cannot_cite_first_undelivered_view(saved_version):
    factory, design_id, version_id, _ = saved_version
    with factory.begin() as session:
        design = session.get(DesignSession, design_id)
        version = session.get(DesignVersion, version_id)
        pixels = validate_raster(png()).canonical_bytes
        capabilities = ImageCapabilities(provider='anthropic', endpoint_version='synthetic', understands_images=True, supported_media=['image/png'])
        delivered = issue_review_receipt(capabilities, version.id, version.canonical_hash, [PixelView(pixels, version.canonical_hash)])
        finding = Finding(id='finding_test', description='Synthetic observation', category='observed_defect', severity='material', view_index=1)
        critique = Critique(provider='anthropic', version_id=version.id, canonical_hash=version.canonical_hash, receipt_id=delivered.id, findings=[finding])
        with pytest.raises(ValueError):
            commit_review(session, design, version, critique, delivered, expected_fence=design.execution_fence)
        session.flush()
        assert session.scalar(select(DesignReview).where(DesignReview.version_id == version.id)) is None
        critique.findings[0].view_index = 0
        saved = commit_review(session, design, version, critique, delivered, expected_fence=design.execution_fence)
        session.flush()
        assert saved.critique['findings'][0]['view_index'] == 0
