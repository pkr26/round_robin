"""A newly reported severe defect cannot resolve itself by a status label."""

from round_robin.database.visual_models import DesignSession, DesignVersion
from round_robin.visual.schemas import Finding
from round_robin.visual.service import commit_review, version_view

from backend.tests.test_exhaustive_visual_service import saved_version as saved_version  # noqa: PLC0414
from backend.tests.test_image_provider import image_configuration as image_configuration  # noqa: PLC0414
from backend.tests.test_image_worker import image_factory as image_factory  # noqa: PLC0414
from backend.tests.test_visual_review_commit_mutation_regressions import (
    new_critique,
    new_receipt,
)


def test_new_addressed_critical_finding_remains_a_blocker(saved_version):
    factory, design_id, version_id, _ = saved_version
    with factory.begin() as session:
        design, version = session.get(DesignSession, design_id), session.get(DesignVersion, version_id)
        first = new_receipt(version, "anthropic")
        defect = Finding(id="finding_new", description="The mandatory title is absent", severity="critical",
                         category="observed_defect", disposition="addressed")
        saved = commit_review(session, design, version, new_critique(version, first, findings=[defect]), first,
                              expected_fence=design.execution_fence)
        second = new_receipt(version, "google")
        commit_review(session, design, version, new_critique(version, second), second, expected_fence=design.execution_fence)
        session.flush()
        identity = saved.critique["findings"][0]["id"]
        view = version_view(session, version)
        assert set(view["coverage"]["eligible_critic_providers"]) == {"anthropic", "google"}
        assert identity in view["regression_review"]["unresolved_blockers"]
