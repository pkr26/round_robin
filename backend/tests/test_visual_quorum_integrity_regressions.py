"""Resolution is derived from eligible quorum, without rewriting saved critiques."""

from copy import deepcopy

import pytest
from round_robin.database.visual_models import (
    DesignOperation,
    DesignReview,
    DesignSession,
    DesignVersion,
)
from round_robin.visual.assets import validate_raster
from round_robin.visual.iterations import VisualLimit
from round_robin.visual.reviews import PixelView, issue_review_receipt
from round_robin.visual.schemas import Finding, ImageCapabilities, RegressionCheck
from round_robin.visual.service import (
    commit_review,
    commit_version,
    complete_version_critics,
    queue_version_reviews,
    version_feedback_findings,
    version_regression_targets,
    version_view,
)
from sqlalchemy import select

from backend.tests.test_exhaustive_visual_service import saved_version as saved_version  # noqa: PLC0414
from backend.tests.test_image_provider import image_configuration as image_configuration  # noqa: PLC0414
from backend.tests.test_image_worker import image_factory as image_factory  # noqa: PLC0414
from backend.tests.test_visual import png
from backend.tests.test_visual_review_commit_mutation_regressions import (
    new_critique,
    new_receipt,
)


def child_receipt(version, parent, provider, *, include_parent=True):
    raster = validate_raster(png())
    views = [PixelView(raster.canonical_bytes, version.canonical_hash)]
    if include_parent:
        views.append(PixelView(raster.canonical_bytes, parent.canonical_hash, f"comparison_parent_version_{parent.id}"))
    capabilities = ImageCapabilities(provider=provider, endpoint_version="synthetic", understands_images=True, supported_media=["image/png"])
    return issue_review_receipt(capabilities, version.id, version.canonical_hash, views)


@pytest.mark.parametrize("scenario", ["new", "minor", "unknown", "single", "ineligible", "resolved", "child_only", "wrong_hash", "wrong_transform", "missing_receipt"])
def test_new_and_linked_critical_findings_require_exact_quorum(saved_version, scenario):
    factory, design_id, version_id, directory = saved_version
    with factory.begin() as session:
        design = session.get(DesignSession, design_id)
        parent = session.get(DesignVersion, version_id)
        prior = Finding(id="finding_prior", description="Earlier observed issue", category="observed_defect",
                        severity="minor" if scenario in {"minor", "unknown"} else "critical")
        receipt = new_receipt(parent, "google")
        saved_prior = commit_review(session, design, parent, new_critique(parent, receipt, findings=[prior]), receipt,
                                    expected_fence=design.execution_fence)
        prior_id = saved_prior.critique["findings"][0]["id"]
        child = commit_version(session, design, validate_raster(png()), directory / "visual",
                               expected_fence=design.execution_fence, parent_version_id=parent.id, source_kind="native_diagram")
        if scenario == "ineligible":
            child.generator_provider = "anthropic"
        targets = version_regression_targets(session, design, child)
        target_ids = [target["id"] for target in targets]
        if scenario in {"minor", "unknown"}:
            assert prior_id not in target_ids
        else:
            assert prior_id in target_ids
        linked_id = None if scenario == "new" else prior_id
        observation = Finding(id="finding_child", description="Current critical defect", category="observed_defect",
                              severity="critical", disposition="addressed", previous_finding_id=linked_id)
        saved_child = None
        observation_provider = "google" if scenario == "ineligible" else "anthropic"
        for provider in ("anthropic", "google"):
            receipt = child_receipt(child, parent, provider, include_parent=scenario != "child_only")
            checks = [RegressionCheck(target_id=target_id, result="resolved", reason="Inspected the relevant pixels")
                      for target_id in target_ids] if scenario != "single" or provider == "anthropic" else []
            critique = new_critique(child, receipt, findings=[observation] if provider == observation_provider else [], checks=checks)
            saved = commit_review(session, design, child, critique, receipt, expected_fence=design.execution_fence)
            if provider == observation_provider:
                saved_child = saved
        session.flush()
        child_id = saved_child.critique["findings"][0]["id"]
        if scenario == "unknown":
            altered = deepcopy(saved_child.critique)
            altered["findings"][0]["previous_finding_id"] = "finding_missing"
            saved_child.critique = altered
            session.flush()
        if scenario == "missing_receipt":
            altered = deepcopy(saved_child.critique)
            altered["receipt_id"] = "receipt_missing"
            saved_child.critique = altered
            session.flush()
        if scenario in {"wrong_hash", "wrong_transform"}:
            for row in session.scalars(select(DesignReview).where(DesignReview.version_id == child.id)):
                altered = deepcopy(row.receipt)
                if scenario == "wrong_hash":
                    altered["views"][1]["sha256"] = "f" * 64
                else:
                    altered["views"][1]["transform"] = "comparison_parent_version_other"
                row.receipt = altered
            session.flush()
        before = deepcopy(saved_child.critique)
        child_view = version_view(session, child)
        current = child_view["regression_review"]
        derived = version_feedback_findings(session, design, child)
        child_entry = next(item for item in derived if item["id"] == child_id)
        has_quorum = scenario == "resolved"
        assert (child_id in current["unresolved_blockers"]) is not has_quorum
        assert child_entry["disposition"] == ("addressed" if has_quorum else "pending")
        if scenario in {"single", "ineligible", "child_only", "wrong_hash", "wrong_transform", "missing_receipt"}:
            assert prior_id in current["pending_target_ids"]
        if scenario in {"child_only", "wrong_hash", "wrong_transform"}:
            assert any("parent view" in item for item in child_view["coverage"]["limitations"])
        if scenario == "missing_receipt":
            assert any("matching evidence" in item for item in child_view["coverage"]["limitations"])
        grandchild = commit_version(session, design, validate_raster(png()), directory / "visual",
                                    expected_fence=design.execution_fence, parent_version_id=child.id, source_kind="native_diagram")
        next_targets = {row["id"] for row in version_regression_targets(session, design, grandchild)}
        assert (child_id in next_targets) is not has_quorum
        session.flush()
        assert session.scalar(select(DesignReview).where(DesignReview.id == saved_child.id)).critique == before


def test_unverified_addressed_finding_survives_even_if_other_votes_resolve_prior(saved_version):
    from dataclasses import asdict
    factory, design_id, version_id, directory = saved_version
    with factory.begin() as session:
        design, parent = session.get(DesignSession, design_id), session.get(DesignVersion, version_id)
        receipt = new_receipt(parent, "google")
        finding = Finding(id="finding_prior", description="Earlier defect", category="observed_defect", severity="critical")
        saved = commit_review(session, design, parent, new_critique(parent, receipt, findings=[finding]), receipt, expected_fence=design.execution_fence)
        prior_id = saved.critique["findings"][0]["id"]
        child = commit_version(session, design, validate_raster(png()), directory / "visual", expected_fence=design.execution_fence,
                               parent_version_id=parent.id, source_kind="native_diagram")
        check = RegressionCheck(target_id=prior_id, result="resolved", reason="Compared the parent and child")
        for provider in ("anthropic", "google"):
            receipt = child_receipt(child, parent, provider)
            commit_review(session, design, child, new_critique(child, receipt, checks=[check]), receipt, expected_fence=design.execution_fence)
        receipt = child_receipt(child, parent, "anthropic")
        unverified_finding = Finding(id="finding_unverified", description="New critical observation", category="observed_defect",
                                     severity="critical", disposition="addressed", previous_finding_id=prior_id)
        damaged = new_critique(child, receipt, findings=[unverified_finding]).model_dump(mode="json")
        damaged["receipt_id"] = "receipt_missing"
        supplement = DesignOperation(id="supplement_corrupt", session_id=design.id, provider="anthropic",
            payload={"kind": "review_supplement", "status": "completed", "version_id": child.id,
                     "receipt": asdict(receipt), "critique": damaged})
        session.add(supplement)
        session.flush()
        original = deepcopy(supplement.payload)
        view = version_view(session, child)
        assert view["regression_review"]["dispositions"][0]["status"] == "resolved"
        assert "finding_unverified" in view["regression_review"]["unresolved_blockers"]
        carried = version_feedback_findings(session, design, child)
        assert next(item for item in carried if item["id"] == "finding_unverified")["disposition"] == "pending"
        grandchild = commit_version(session, design, validate_raster(png()), directory / "visual",
                                    expected_fence=design.execution_fence, parent_version_id=child.id, source_kind="native_diagram")
        assert "finding_unverified" in {target["id"] for target in version_regression_targets(session, design, grandchild)}
        assert supplement.payload == original


def test_child_only_historical_votes_allow_two_view_recovery(saved_version):
    factory, design_id, version_id, directory = saved_version
    with factory.begin() as session:
        design, parent = session.get(DesignSession, design_id), session.get(DesignVersion, version_id)
        receipt = new_receipt(parent, "google")
        finding = Finding(id="finding_prior", description="Earlier defect", category="observed_defect", severity="critical")
        prior = commit_review(session, design, parent, new_critique(parent, receipt, findings=[finding]), receipt, expected_fence=design.execution_fence)
        identity = prior.critique["findings"][0]["id"]
        child = commit_version(session, design, validate_raster(png()), directory / "visual", expected_fence=design.execution_fence,
                               parent_version_id=parent.id, source_kind="native_diagram")
        check = RegressionCheck(target_id=identity, result="resolved", reason="Inspect parent and child")
        for provider in ("anthropic", "google"):
            receipt = new_receipt(child, provider)
            commit_review(session, design, child, new_critique(child, receipt, checks=[check]), receipt, expected_fence=design.execution_fence)
        session.flush()
        assert version_view(session, child)["coverage"]["complete"]
        assert identity in version_view(session, child)["regression_review"]["pending_target_ids"]
        assert complete_version_critics(session, design, child) == set()
        assert {item.provider for item in queue_version_reviews(session, design, child)} == {"anthropic", "google"}
        for provider in ("anthropic", "google"):
            receipt = child_receipt(child, parent, provider)
            commit_review(session, design, child, new_critique(child, receipt, checks=[check]), receipt, expected_fence=design.execution_fence)
        session.flush()
        assert identity not in version_view(session, child)["regression_review"]["unresolved_blockers"]
        assert complete_version_critics(session, design, child) == {"anthropic", "google"}


def test_malformed_saved_critique_fails_closed_without_rewriting_it(saved_version):
    factory, design_id, version_id, _ = saved_version
    with factory.begin() as session:
        design, version = session.get(DesignSession, design_id), session.get(DesignVersion, version_id)
        receipt = new_receipt(version, "google")
        saved = commit_review(session, design, version, new_critique(version, receipt), receipt, expected_fence=design.execution_fence)
        damaged = {**saved.critique, "findings": "not-a-list"}
        saved.critique = damaged
        session.flush()
        view = version_view(session, version)
        assert not view["coverage"]["complete"] and not view["regression_review"]["complete"]
        assert any("unavailable" in message for message in view["coverage"]["limitations"])
        with pytest.raises(VisualLimit, match="saved review is invalid"):
            version_feedback_findings(session, design, version)
        assert saved.critique == damaged


def test_old_queued_edit_with_changed_derived_findings_is_blocked_before_spend(saved_version, image_configuration, monkeypatch):
    from round_robin.database.models import BudgetOperation
    from round_robin.visual.feedback import build_edit_bundle
    from round_robin.worker import image_tasks

    from backend.tests.test_image_worker import FakeImageAdapter

    factory, design_id, version_id, directory = saved_version
    operation_id = "image_operation_one"
    with factory.begin() as session:
        design, parent = session.get(DesignSession, design_id), session.get(DesignVersion, version_id)
        receipt = new_receipt(parent, "google")
        finding = Finding(id="finding_old", description="Critical missing title", category="observed_defect",
                          severity="critical", disposition="addressed")
        review = commit_review(session, design, parent, new_critique(parent, receipt, findings=[finding]), receipt,
                               expected_fence=design.execution_fence)
        raw_finding = deepcopy(review.critique["findings"][0])
        choices = {raw_finding["id"]: "address"}
        old_bundle, old_hash = build_edit_bundle(brief=design.frozen_brief, parent_version_id=parent.id,
            parent_hash=parent.canonical_hash, findings=[raw_finding], dispositions=choices, intent="Restore the title")
        operation = session.get(DesignOperation, operation_id)
        operation.payload = {**operation.payload, "kind": "edit", "step_id": "edit:version_2", "parent_version_id": parent.id,
                             "input_hash": parent.canonical_hash, "edit_intent": "Restore the title", "finding_dispositions": choices,
                             "edit_bundle": old_bundle, "edit_bundle_hash": old_hash}
        original_state = deepcopy(design.state)
    calls = []
    original_packet = image_tasks._packet
    def observe_packet(*args, **kwargs):
        calls.append(True)
        return original_packet(*args, **kwargs)
    monkeypatch.setattr(image_tasks, "_packet", observe_packet)
    provider = FakeImageAdapter("openai", image_configuration, factory)
    result = image_tasks.execute_image_operation(operation_id, session_factory=factory,
                                                provider_registry={"openai": provider}, artifact_directory=directory)
    assert result == {"status": "blocked"} and calls == [True] and provider.calls == 0
    with factory() as session:
        operation = session.get(DesignOperation, operation_id)
        assert operation.payload["status"] == "blocked" and operation.payload["error_code"] == "VisualLimit"
        assert session.get(BudgetOperation, operation_id) is None
        assert session.get(DesignOperation, f"imagestorage_{operation_id}") is None
        assert session.get(DesignSession, design_id).state == original_state
        assert session.get(DesignReview, review.id).critique["findings"][0] == raw_finding
