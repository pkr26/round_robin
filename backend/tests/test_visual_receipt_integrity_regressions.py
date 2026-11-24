import pytest
from round_robin.visual.reviews import PixelReceipt
from round_robin.visual.schemas import RegressionCheck


def test_persisted_supplement_with_unmatched_receipt_does_not_resolve(saved_version):
    from copy import deepcopy

    from round_robin.database.visual_models import DesignSession, DesignVersion
    from round_robin.visual.assets import validate_raster
    from round_robin.visual.schemas import Finding
    from round_robin.visual.service import (
        commit_review,
        commit_version,
        version_feedback_findings,
        version_view,
    )

    from backend.tests.test_visual import png
    from backend.tests.test_visual_review_commit_mutation_regressions import (
        new_critique,
        new_receipt,
    )

    factory, design_id, version_id, directory = saved_version
    with factory.begin() as session:
        design, parent = session.get(DesignSession, design_id), session.get(DesignVersion, version_id)
        receipt = new_receipt(parent, "google")
        finding = Finding(id="finding_prior", description="Prior title missing", category="observed_defect", severity="critical")
        initial = commit_review(session, design, parent, new_critique(parent, receipt, findings=[finding]), receipt,
                                expected_fence=design.execution_fence)
        identity = initial.critique["findings"][0]["id"]
        child = commit_version(session, design, validate_raster(png()), directory / "visual", expected_fence=design.execution_fence,
                               parent_version_id=parent.id, source_kind="native_diagram")
        a = new_receipt(child, "anthropic")
        commit_review(session, design, child, new_critique(child, a), a, expected_fence=design.execution_fence)
        b = new_receipt(child, "google")
        check = RegressionCheck(target_id=identity, result="resolved", reason="synthetic")
        commit_review(session, design, child, new_critique(child, b, checks=[check]), b, expected_fence=design.execution_fence)
        a2 = new_receipt(child, "anthropic")
        supplement = commit_review(session, design, child, new_critique(child, a2, checks=[check]), a2,
                                   expected_fence=design.execution_fence)
        damaged = deepcopy(supplement.payload)
        damaged["critique"]["receipt_id"] = "receipt_missing"
        supplement.payload = damaged
        session.flush()
        view = version_view(session, child)
        assert view["regression_review"]["dispositions"][0]["status"] == "pending"
        assert identity in view["regression_review"]["unresolved_blockers"]
        assert identity in {item["id"] for item in version_feedback_findings(session, design, child)}


from backend.tests.test_exhaustive_visual_service import saved_version as saved_version  # noqa: PLC0414
from backend.tests.test_image_provider import image_configuration as image_configuration  # noqa: PLC0414
from backend.tests.test_image_worker import image_factory as image_factory  # noqa: PLC0414


def test_child_only_receipts_do_not_verify_parent_change(saved_version):
    from round_robin.database.visual_models import DesignSession, DesignVersion
    from round_robin.visual.assets import validate_raster
    from round_robin.visual.schemas import Finding
    from round_robin.visual.service import commit_review, commit_version, version_view

    from backend.tests.test_visual import png
    from backend.tests.test_visual_review_commit_mutation_regressions import (
        new_critique,
        new_receipt,
    )

    factory, design_id, version_id, directory = saved_version
    with factory.begin() as session:
        design, parent = session.get(DesignSession, design_id), session.get(DesignVersion, version_id)
        previous = new_receipt(parent, "google")
        finding = Finding(id="finding_prior", description="Prior title missing", category="observed_defect", severity="critical")
        row = commit_review(session, design, parent, new_critique(parent, previous, findings=[finding]), previous,
                            expected_fence=design.execution_fence)
        identity = row.critique["findings"][0]["id"]
        child = commit_version(session, design, validate_raster(png()), directory / "visual", expected_fence=design.execution_fence,
                               parent_version_id=parent.id, source_kind="native_diagram")
        for provider in ("anthropic", "google"):
            delivery = new_receipt(child, provider)
            assert len(delivery.views) == 1
            check = RegressionCheck(target_id=identity, result="resolved", reason="synthetic")
            commit_review(session, design, child, new_critique(child, delivery, checks=[check]), delivery,
                          expected_fence=design.execution_fence)
        session.flush()
        view = version_view(session, child)
        assert identity in view["regression_review"]["unresolved_blockers"]


@pytest.mark.parametrize("other_vote, expected_status", [("resolved", "resolved"), ("uncertain", "uncertain")])
def test_genuine_comparison_quorum_and_future_obligation_preserve_raw_records(saved_version, other_vote, expected_status):
    from copy import deepcopy

    from round_robin.database.visual_models import DesignSession, DesignVersion
    from round_robin.visual.assets import validate_raster
    from round_robin.visual.feedback import regression_targets
    from round_robin.visual.reviews import PixelView, issue_review_receipt
    from round_robin.visual.schemas import Finding, ImageCapabilities
    from round_robin.visual.service import (
        commit_review,
        commit_version,
        version_feedback_findings,
        version_view,
    )

    from backend.tests.test_visual import png
    from backend.tests.test_visual_review_commit_mutation_regressions import (
        new_critique,
        new_receipt,
    )

    factory, design_id, version_id, directory = saved_version
    with factory.begin() as session:
        design, parent = session.get(DesignSession, design_id), session.get(DesignVersion, version_id)
        previous = new_receipt(parent, "google")
        finding = Finding(id="finding_prior", description="Prior title missing", category="observed_defect", severity="critical")
        parent_row = commit_review(session, design, parent, new_critique(parent, previous, findings=[finding]), previous,
                                   expected_fence=design.execution_fence)
        parent_identity = parent_row.critique["findings"][0]["id"]
        raster = validate_raster(png())
        child = commit_version(session, design, raster, directory / "visual", expected_fence=design.execution_fence,
                               parent_version_id=parent.id, source_kind="native_diagram")
        rows = []
        for provider in ("anthropic", "google"):
            capabilities = ImageCapabilities(provider=provider, endpoint_version="synthetic", understands_images=True, supported_media=["image/png"])
            views = [PixelView(raster.canonical_bytes, child.canonical_hash),
                     PixelView(raster.canonical_bytes, parent.canonical_hash, f"comparison_parent_version_{parent.id}")]
            receipt = issue_review_receipt(capabilities, child.id, child.canonical_hash, views)
            vote = "resolved" if provider == "anthropic" else other_vote
            check = RegressionCheck(target_id=parent_identity, result=vote, reason="synthetic")
            findings = [Finding(id="finding_updated", description="Prior title missing", category="observed_defect", severity="critical",
                                previous_finding_id=parent_identity, disposition="addressed")] if provider == "anthropic" else []
            rows.append(commit_review(session, design, child, new_critique(child, receipt, findings=findings, checks=[check]), receipt,
                                      expected_fence=design.execution_fence))
        session.flush()
        before = [deepcopy(row.critique) for row in rows]
        child_identity = rows[0].critique["findings"][0]["id"]
        view = version_view(session, child)
        assert view["regression_review"]["dispositions"][0]["status"] == expected_status
        derived = version_feedback_findings(session, design, child)
        next_ids = {item["id"] for item in regression_targets(design.frozen_brief, derived)}
        if expected_status == "resolved":
            assert child_identity not in view["regression_review"]["unresolved_blockers"]
            assert child_identity not in next_ids and parent_identity not in next_ids
        else:
            assert parent_identity in view["regression_review"]["unresolved_blockers"]
            assert child_identity in view["regression_review"]["unresolved_blockers"]
            assert child_identity in next_ids and parent_identity in next_ids
        assert [row.critique for row in rows] == before
        assert rows[0].critique["findings"][0]["disposition"] == "addressed"


def test_duplicate_receipt_id_cannot_transfer_parent_comparison_eligibility(saved_version):
    from dataclasses import replace

    from round_robin.database.visual_models import DesignSession, DesignVersion
    from round_robin.visual.assets import validate_raster
    from round_robin.visual.reviews import PixelView, issue_review_receipt
    from round_robin.visual.schemas import Finding, ImageCapabilities
    from round_robin.visual.service import commit_review, commit_version, version_view

    from backend.tests.test_visual import png
    from backend.tests.test_visual_review_commit_mutation_regressions import (
        new_critique,
        new_receipt,
    )
    factory, design_id, version_id, directory = saved_version
    with factory.begin() as session:
        design, parent = session.get(DesignSession, design_id), session.get(DesignVersion, version_id)
        prior = new_receipt(parent, "google")
        finding = Finding(id="finding_prior", description="Prior title missing", category="observed_defect", severity="critical")
        row = commit_review(session, design, parent, new_critique(parent, prior, findings=[finding]), prior, expected_fence=design.execution_fence)
        identity = row.critique["findings"][0]["id"]
        raster = validate_raster(png())
        child = commit_version(session, design, raster, directory / "visual", expected_fence=design.execution_fence,
                               parent_version_id=parent.id, source_kind="native_diagram")
        def pair(provider):
            caps = ImageCapabilities(provider=provider, endpoint_version="synthetic", understands_images=True, supported_media=["image/png"])
            return issue_review_receipt(caps, child.id, child.canonical_hash,
                [PixelView(raster.canonical_bytes, child.canonical_hash), PixelView(raster.canonical_bytes, parent.canonical_hash, f"comparison_parent_version_{parent.id}")])
        first = pair("anthropic")
        commit_review(session, design, child, new_critique(child, first), first, expected_fence=design.execution_fence)
        second = pair("google")
        check = RegressionCheck(target_id=identity, result="resolved", reason="synthetic")
        commit_review(session, design, child, new_critique(child, second, checks=[check]), second, expected_fence=design.execution_fence)
        child_only = replace(new_receipt(child, "anthropic"), id=first.id)
        commit_review(session, design, child, new_critique(child, child_only, checks=[check]), child_only, expected_fence=design.execution_fence)
        session.flush()
        assert identity in version_view(session, child)["regression_review"]["unresolved_blockers"]


@pytest.mark.parametrize("corruption", ["wrong_hash", "zero_size", "missing_size", "too_many"])
def test_wrong_first_view_cannot_cast_regression_vote(saved_version, corruption):
    from copy import deepcopy

    from round_robin.database.visual_models import DesignSession, DesignVersion
    from round_robin.visual.assets import validate_raster
    from round_robin.visual.reviews import PixelView, issue_review_receipt
    from round_robin.visual.schemas import Finding, ImageCapabilities
    from round_robin.visual.service import commit_review, commit_version, version_view

    from backend.tests.test_visual import png
    from backend.tests.test_visual_review_commit_mutation_regressions import (
        new_critique,
        new_receipt,
    )
    factory, design_id, version_id, directory = saved_version
    with factory.begin() as session:
        design, parent = session.get(DesignSession, design_id), session.get(DesignVersion, version_id)
        prior = new_receipt(parent, "google")
        finding = Finding(id="finding_prior", description="Prior title missing", category="observed_defect", severity="critical")
        row = commit_review(session, design, parent, new_critique(parent, prior, findings=[finding]), prior, expected_fence=design.execution_fence)
        identity = row.critique["findings"][0]["id"]
        raster = validate_raster(png())
        child = commit_version(session, design, raster, directory / "visual", expected_fence=design.execution_fence,
                               parent_version_id=parent.id, source_kind="native_diagram")
        rows = []
        for provider in ("anthropic", "google"):
            caps = ImageCapabilities(provider=provider, endpoint_version="synthetic", understands_images=True, supported_media=["image/png"])
            receipt = issue_review_receipt(caps, child.id, child.canonical_hash,
                [PixelView(raster.canonical_bytes, child.canonical_hash), PixelView(raster.canonical_bytes, parent.canonical_hash, f"comparison_parent_version_{parent.id}")])
            check = RegressionCheck(target_id=identity, result="resolved", reason="synthetic")
            rows.append(commit_review(session, design, child, new_critique(child, receipt, checks=[check]), receipt, expected_fence=design.execution_fence))
        damaged = deepcopy(rows[0].receipt)
        if corruption == "wrong_hash":
            damaged["views"][0]["sha256"] = "b" * 64
        elif corruption == "zero_size":
            damaged["views"][0]["byte_size"] = 0
        elif corruption == "missing_size":
            damaged["views"][0].pop("byte_size")
        else:
            damaged["views"] = list(damaged["views"]) + [damaged["views"][0]] * 3
        rows[0].receipt = damaged
        session.flush()
        assert identity in version_view(session, child)["regression_review"]["unresolved_blockers"]


def test_malformed_saved_review_fails_closed_and_keeps_critical_context(saved_version):
    from copy import deepcopy

    from round_robin.database.visual_models import DesignSession, DesignVersion
    from round_robin.visual.iterations import VisualLimit
    from round_robin.visual.schemas import Finding
    from round_robin.visual.service import (
        commit_review,
        version_feedback_findings,
        version_view,
    )

    from backend.tests.test_visual_review_commit_mutation_regressions import (
        new_critique,
        new_receipt,
    )

    factory, design_id, version_id, _ = saved_version
    with factory.begin() as session:
        design, version = session.get(DesignSession, design_id), session.get(DesignVersion, version_id)
        first = new_receipt(version, "anthropic")
        defect = Finding(id="finding_alias", description="Critical missing title", category="observed_defect", severity="critical")
        row = commit_review(session, design, version, new_critique(version, first, findings=[defect]), first,
                            expected_fence=design.execution_fence)
        second = new_receipt(version, "google")
        commit_review(session, design, version, new_critique(version, second), second, expected_fence=design.execution_fence)
        damaged = deepcopy(row.critique)
        identity = damaged["findings"][0]["id"]
        damaged["limitations"] = 42
        row.critique = damaged
        session.flush()
        view = version_view(session, version)
        assert not view["coverage"]["complete"] and not view["regression_review"]["complete"]
        assert identity in view["regression_review"]["unresolved_blockers"]
        assert any("unavailable" in message for message in view["coverage"]["limitations"])
        try:
            context = version_feedback_findings(session, design, version)
        except (VisualLimit, ValueError):
            return
        assert identity in {item["id"] for item in context}
        assert row.critique == damaged


def test_actual_worker_packet_comparison_votes_clear_valid_prior(saved_version, monkeypatch):
    from types import SimpleNamespace

    from round_robin.database.visual_models import DesignSession, DesignVersion
    from round_robin.visual.assets import validate_raster
    from round_robin.visual.schemas import Finding, ImageCapabilities
    from round_robin.visual.service import commit_review, commit_version, version_view
    from round_robin.worker import image_tasks

    from backend.tests.test_visual import png
    from backend.tests.test_visual_review_commit_mutation_regressions import (
        new_critique,
        new_receipt,
    )
    factory, design_id, version_id, directory = saved_version
    monkeypatch.setattr(image_tasks, "_request_allowance", lambda *args: 90)
    with factory.begin() as session:
        design, parent = session.get(DesignSession, design_id), session.get(DesignVersion, version_id)
        prior = new_receipt(parent, "google")
        defect = Finding(id="finding_prior", description="Prior title missing", category="observed_defect", severity="critical")
        row = commit_review(session, design, parent, new_critique(parent, prior, findings=[defect]), prior, expected_fence=design.execution_fence)
        identity = row.critique["findings"][0]["id"]
        child = commit_version(session, design, validate_raster(png()), directory / "visual", expected_fence=design.execution_fence,
                               parent_version_id=parent.id, source_kind="native_diagram")
        for name in ("anthropic", "google"):
            caps = ImageCapabilities(provider=name, endpoint_version="synthetic", understands_images=True, supported_media=["image/png"])
            operation = SimpleNamespace(provider=name, payload={"kind": "vision", "step_id": "vision_test", "target_version_id": child.id, "input_hash": child.canonical_hash})
            packet = image_tasks._packet(session, design, operation, SimpleNamespace(capabilities=caps), directory)
            assert packet["receipt"]["views"][0]["sha256"] == child.canonical_hash
            assert packet["receipt"]["views"][1]["sha256"] == parent.canonical_hash
            assert packet["receipt"]["views"][1]["transform"] == f"comparison_parent_version_{parent.id}"
            receipt = PixelReceipt(**{**packet["receipt"], "views": tuple(packet["receipt"]["views"])})
            check = RegressionCheck(target_id=identity, result="resolved", reason="synthetic")
            commit_review(session, design, child, new_critique(child, receipt, checks=[check]), receipt, expected_fence=design.execution_fence)
        session.flush()
        assert version_view(session, child)["regression_review"]["dispositions"][0]["status"] == "resolved"


@pytest.mark.parametrize("mismatch", ["provider", "session"])
def test_saved_row_scope_mismatch_cannot_hide_severe_finding(saved_version, mismatch):
    from round_robin.database.visual_models import DesignSession, DesignVersion
    from round_robin.visual.schemas import Finding
    from round_robin.visual.service import (
        commit_review,
        version_feedback_findings,
        version_view,
    )

    from backend.tests.test_visual_review_commit_mutation_regressions import (
        new_critique,
        new_receipt,
    )
    factory, design_id, version_id, _ = saved_version
    with factory.begin() as session:
        design, version = session.get(DesignSession, design_id), session.get(DesignVersion, version_id)
        delivery = new_receipt(version, "anthropic")
        defect = Finding(id="finding_critical", description="Critical missing title", category="observed_defect", severity="critical", disposition="addressed")
        row = commit_review(session, design, version, new_critique(version, delivery, findings=[defect]), delivery, expected_fence=design.execution_fence)
        other = new_receipt(version, "google")
        commit_review(session, design, version, new_critique(version, other), other, expected_fence=design.execution_fence)
        identity = row.critique["findings"][0]["id"]
        if mismatch == "provider":
            row.provider = "openai"
        else:
            row.session_id = "design_other"
        session.flush()
        view = version_view(session, version)
        assert identity in view["regression_review"]["unresolved_blockers"]
        assert not view["coverage"]["complete"]
        assert identity in {finding["id"] for finding in version_feedback_findings(session, design, version)}


@pytest.mark.parametrize("missing", ["receipt", "critique"])
def test_malformed_supplement_is_fenced_without_losing_critical_findings(saved_version, missing):
    from round_robin.database.visual_models import (
        DesignOperation,
        DesignSession,
        DesignVersion,
    )
    from round_robin.visual.iterations import VisualLimit
    from round_robin.visual.schemas import Finding
    from round_robin.visual.service import commit_review, version_feedback_findings, version_view

    from backend.tests.test_visual_review_commit_mutation_regressions import (
        new_critique,
        new_receipt,
    )
    factory, design_id, version_id, _ = saved_version
    with factory.begin() as session:
        design, version = session.get(DesignSession, design_id), session.get(DesignVersion, version_id)
        for provider in ("anthropic", "google"):
            delivery = new_receipt(version, provider)
            commit_review(session, design, version, new_critique(version, delivery), delivery, expected_fence=design.execution_fence)
        supplement_receipt = new_receipt(version, "anthropic")
        defect = Finding(id="finding_supplement", description="Critical missing title", category="observed_defect", severity="critical")
        raw_critique = new_critique(version, supplement_receipt, findings=[defect]).model_dump(mode="json")
        payload = {"kind": "review_supplement", "version_id": version.id, "critique": raw_critique,
                   "receipt": {"id": supplement_receipt.id, "provider": supplement_receipt.provider, "version_id": version.id,
                               "canonical_hash": version.canonical_hash, "views": supplement_receipt.views, "endpoint_version": "synthetic"}}
        del payload[missing]
        session.add(DesignOperation(id="designsupplement_corrupt", session_id=design.id, provider="anthropic", payload=payload))
        session.flush()
        if missing == "critique":
            view = version_view(session, version)
            assert not view["coverage"]["complete"] and not view["regression_review"]["complete"]
            assert any("unavailable" in text for text in view["coverage"]["limitations"])
            with pytest.raises(VisualLimit, match="saved review"):
                version_feedback_findings(session, design, version)
        else:
            assert "finding_supplement" in version_view(session, version)["regression_review"]["unresolved_blockers"]


def test_queued_old_edit_bundle_with_rewritten_finding_fences_before_packet(saved_version, monkeypatch):
    from types import SimpleNamespace

    from round_robin.database.visual_models import DesignSession, DesignVersion
    from round_robin.visual.feedback import build_edit_bundle
    from round_robin.visual.iterations import VisualLimit
    from round_robin.visual.schemas import Finding
    from round_robin.visual.service import commit_review
    from round_robin.worker import image_tasks

    from backend.tests.test_visual_review_commit_mutation_regressions import (
        new_critique,
        new_receipt,
    )
    factory, design_id, version_id, directory = saved_version
    monkeypatch.setattr(image_tasks, "_request_allowance", lambda *args: 90)
    with factory.begin() as session:
        design, parent = session.get(DesignSession, design_id), session.get(DesignVersion, version_id)
        receipt = new_receipt(parent, "google")
        finding = Finding(id="finding_old", description="Critical missing title", category="observed_defect", severity="critical", disposition="addressed")
        row = commit_review(session, design, parent, new_critique(parent, receipt, findings=[finding]), receipt, expected_fence=design.execution_fence)
        raw = row.critique["findings"][0]
        old_bundle, old_hash = build_edit_bundle(brief=design.frozen_brief, parent_version_id=parent.id, parent_hash=parent.canonical_hash,
                                                  findings=[raw], dispositions={raw["id"]: "address"}, intent="restore title")
        operation = SimpleNamespace(provider="openai", payload={"kind": "edit", "step_id": "edit_test", "parent_version_id": parent.id,
            "input_hash": parent.canonical_hash, "edit_intent": "restore title", "finding_dispositions": {raw["id"]: "address"},
            "edit_bundle": old_bundle, "edit_bundle_hash": old_hash})
        with pytest.raises(VisualLimit, match="immutable parent findings"):
            image_tasks._packet(session, design, operation, SimpleNamespace(), directory)
        assert row.critique["findings"][0]["disposition"] == "addressed"
