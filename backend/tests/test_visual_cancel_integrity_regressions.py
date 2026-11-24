"""A damaged saved critique cannot prevent a user from stopping work."""

from copy import deepcopy
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import select

from backend.tests.test_visual import png, upload_original, visual_app as visual_app  # noqa: PLC0414
from backend.tests.test_exhaustive_visual_service import saved_version as saved_version  # noqa: PLC0414
from backend.tests.test_image_provider import image_configuration as image_configuration  # noqa: PLC0414
from backend.tests.test_image_worker import image_factory as image_factory  # noqa: PLC0414
from backend.tests.test_visual_review_commit_mutation_regressions import new_critique, new_receipt
from round_robin.api import visual
from round_robin.database.models import BudgetOperation, Command, Meeting, PolicyBucket
from round_robin.database.visual_models import DesignOperation, DesignReview, DesignSession, DesignVersion
from round_robin.visual.iterations import VisualLimit
from round_robin.visual.assets import validate_raster
from round_robin.visual.schemas import DesignCommand, Finding
from round_robin.visual.service import commit_review, commit_version, version_feedback_findings, version_view
from round_robin.visual.views import DesignSessionView
from round_robin.worker import image_tasks


def test_cancel_commits_fence_with_redacted_unavailable_review_and_unknown_spend(visual_app, monkeypatch):
    app, client, _, design = visual_app
    saved = upload_original(client, design)
    design_id, version_id = design["id"], saved["versions"][0]["id"]
    with app.state.session_factory.begin() as session:
        current, version = session.get(DesignSession, design_id), session.get(DesignVersion, version_id)
        receipt = new_receipt(version, "anthropic")
        finding = Finding(id="finding_sensitive", description="Synthetic critical concern", severity="critical", category="observed_defect")
        row = commit_review(session, current, version, new_critique(version, receipt, findings=[finding]), receipt,
                            expected_fence=current.execution_fence)
        identity = row.critique["findings"][0]["id"]
        damaged = {"findings": deepcopy(row.critique["findings"])}
        row.critique = damaged
        revision, fence, owner = current.revision, current.execution_fence, current.owner_id
        session.add(BudgetOperation(id="hold_visual_cancel", run_id=design_id, owner_id=owner, kind="visual_finish_hold", status="held", reserved_usd="0.20", metadata_json={}))
        session.add(BudgetOperation(id="unknown_visual_cancel", run_id=design_id, owner_id=owner, kind="visual_edit", status="unknown", reserved_usd="0.42", metadata_json={}))
    command = {"command_id": str(uuid4()), "expected_revision": revision}
    result = client.post(f"/api/design-sessions/{design_id}/cancel", json=command)
    assert result.status_code == 200, result.text
    body = result.json()
    DesignSessionView.model_validate(body)
    assert body["status"] == "cancelled"
    version_view = body["versions"][0]
    assert version_view["id"] == version_id and version_view["canonical_hash"] == saved["versions"][0]["canonical_hash"]
    assert not version_view["coverage"]["complete"] and not version_view["regression_review"]["complete"]
    assert any("unavailable" in text for text in version_view["coverage"]["limitations"])
    assert identity in version_view["regression_review"]["unresolved_blockers"]
    assert version_view["reviews"] == []
    assert "Synthetic critical concern" not in result.text
    assert client.post(f"/api/design-sessions/{design_id}/cancel", json=command).json() == body
    assert client.get(f"/api/design-sessions/{design_id}").json()["status"] == "cancelled"
    with app.state.session_factory() as session:
        current = session.get(DesignSession, design_id)
        assert current.status == "cancelled" and current.execution_fence == fence + 1
        assert session.get(DesignReview, row.id).critique == damaged
        assert session.get(BudgetOperation, "hold_visual_cancel").status == "released"
        unknown = session.get(BudgetOperation, "unknown_visual_cancel")
        assert unknown.status == "unknown" and unknown.reserved_usd == "0.42"
        assert session.get(Command, (owner, command["command_id"])) is not None
        with pytest.raises(VisualLimit):
            version_feedback_findings(session, current, session.get(DesignVersion, version_id))
        policy = session.scalar(select(PolicyBucket).where(PolicyBucket.key == "deployment"))
        scope = (session.get(Meeting, current.meeting_id), policy)
        stale = SimpleNamespace(provider="anthropic", payload={"kind": "vision", "fence": fence, "epoch": current.deployment_epoch,
                                "owner_id": owner, "session_id": design_id})
        monkeypatch.setattr("round_robin.operations.durability.marker_pending", lambda *args, **kwargs: False)
        with pytest.raises(VisualLimit):
            image_tasks._authority(current, stale, scope, session=session)


@pytest.mark.parametrize("scope", ["missing", "array", "foreign", "non_dict", "queued_supplement", "bad_status"])
def test_corrupt_supplement_scope_cannot_certify_review_or_prevent_cancellation(visual_app, scope):
    from dataclasses import asdict
    app, client, _, design = visual_app
    saved = upload_original(client, design)
    design_id, version_id = design["id"], saved["versions"][0]["id"]
    with app.state.session_factory.begin() as session:
        current, version = session.get(DesignSession, design_id), session.get(DesignVersion, version_id)
        for provider in ("anthropic", "google"):
            receipt = new_receipt(version, provider)
            commit_review(session, current, version, new_critique(version, receipt), receipt, expected_fence=current.execution_fence)
        receipt = new_receipt(version, "anthropic")
        severe = Finding(id="finding_unscoped", description="Unscoped critical concern", severity="critical", category="observed_defect")
        payload = {"kind": "review_supplement", "critique": new_critique(version, receipt, findings=[severe]).model_dump(mode="json"),
                   "receipt": asdict(receipt)}
        if scope == "array":
            payload["version_id"] = []
        elif scope == "foreign":
            payload["version_id"] = "version_not_in_session"
        elif scope == "non_dict":
            payload = None
        elif scope == "queued_supplement":
            payload["status"] = "queued"
        elif scope == "bad_status":
            payload["status"] = []
        session.add(DesignOperation(id="designop_corrupt", session_id=design_id, provider="anthropic", payload=payload))
        revision = current.revision
    with app.state.session_factory() as session:
        original = deepcopy(session.get(DesignOperation, "designop_corrupt").payload)
    polled = client.get(f"/api/design-sessions/{design_id}")
    assert polled.status_code == 200, polled.text
    before = polled.json()["versions"][0]
    assert not before["coverage"]["complete"] and not before["regression_review"]["complete"]
    assert any("unavailable" in message for message in before["coverage"]["limitations"])
    if scope != "non_dict":
        assert "finding_unscoped" in before["regression_review"]["unresolved_blockers"]
    with app.state.session_factory() as session:
        with pytest.raises(VisualLimit):
            version_feedback_findings(session, session.get(DesignSession, design_id), session.get(DesignVersion, version_id))
    command = {"command_id": str(uuid4()), "expected_revision": revision}
    stopped = client.post(f"/api/design-sessions/{design_id}/cancel", json=command)
    assert stopped.status_code == 200, stopped.text
    assert stopped.json()["status"] == "cancelled"
    assert not stopped.json()["versions"][0]["coverage"]["complete"]
    with app.state.session_factory() as session:
        assert session.get(DesignOperation, "designop_corrupt").payload == original


def test_scoped_corrupt_child_review_does_not_invalidate_unrelated_parent(saved_version, monkeypatch):
    from round_robin.api import visual
    factory, design_id, parent_id, directory = saved_version
    with factory.begin() as session:
        design, parent = session.get(DesignSession, design_id), session.get(DesignVersion, parent_id)
        for provider in ("anthropic", "google"):
            receipt = new_receipt(parent, provider)
            commit_review(session, design, parent, new_critique(parent, receipt), receipt, expected_fence=design.execution_fence)
        child = commit_version(session, design, validate_raster(png()), directory / "visual", expected_fence=design.execution_fence,
                               parent_version_id=parent.id, source_kind="native_diagram")
        receipt = new_receipt(child, "google")
        saved = commit_review(session, design, child, new_critique(child, receipt), receipt, expected_fence=design.execution_fence)
        saved.critique = {"findings": "malformed"}
        session.flush()
        assert version_view(session, parent)["coverage"]["complete"]
        assert not version_view(session, child)["coverage"]["complete"]
        version_feedback_findings(session, design, parent)
        with pytest.raises(VisualLimit):
            version_feedback_findings(session, design, child)
        revision, owner = design.revision, design.owner_id
        child_id = child.id
    monkeypatch.setattr(visual, "visual_enabled", lambda: None)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(settings=SimpleNamespace(command_hash_key="synthetic"))), state=SimpleNamespace())
    with factory() as session:
        result = visual.cancel_design(design_id, DesignCommand(command_id=uuid4(), expected_revision=revision), request, owner, session)
    parsed = DesignSessionView.model_validate(result)
    versions = {version.id: version for version in parsed.versions}
    assert versions[parent_id].coverage.complete
    assert not versions[child_id].coverage.complete and parsed.status == "cancelled"


@pytest.mark.parametrize("damaged", [None, {}, {"kind": [], "status": "queued"},
                                      {"kind": "vision", "status": []},
                                      {"kind": "vision", "status": "queued", "dispatch_status": []}])
def test_corrupt_saved_operation_blocks_other_queued_work_before_send(saved_version, image_configuration, damaged):
    from backend.tests.test_image_worker import FakeImageAdapter
    factory, design_id, _, directory = saved_version
    with factory.begin() as session:
        design = session.get(DesignSession, design_id)
        session.add(DesignOperation(id="designop_corrupt", session_id=design_id, provider="anthropic", payload=damaged))
        session.add(BudgetOperation(id="designop_corrupt", run_id=design_id, owner_id=design.owner_id,
                                    kind="visual_vision", status="unknown", reserved_usd="0.42", metadata_json={}))
    provider = FakeImageAdapter("openai", image_configuration, factory)
    result = image_tasks.execute_image_operation("image_operation_one", session_factory=factory,
                                                provider_registry={"openai": provider}, artifact_directory=directory)
    assert result == {"status": "blocked"} and provider.calls == 0
    with factory() as session:
        assert session.get(BudgetOperation, "image_operation_one") is None
        assert session.get(DesignOperation, "designop_corrupt").payload == damaged
        ledger = session.get(BudgetOperation, "designop_corrupt")
        assert ledger.status == "unknown" and ledger.reserved_usd == "0.42"


def test_malformed_target_keeps_existing_authorization_uncertain(saved_version, image_configuration):
    from backend.tests.test_image_worker import FakeImageAdapter
    factory, design_id, _, directory = saved_version
    with factory.begin() as session:
        design = session.get(DesignSession, design_id)
        session.get(DesignOperation, "image_operation_one").payload = None
        session.add(BudgetOperation(id="image_operation_one", run_id=design_id, owner_id=design.owner_id,
                                    kind="visual_edit", status="unknown", reserved_usd="0.42", metadata_json={}))
    provider = FakeImageAdapter("openai", image_configuration, factory)
    result = image_tasks.execute_image_operation("image_operation_one", session_factory=factory,
                                                provider_registry={"openai": provider}, artifact_directory=directory)
    assert result == {"status": "authorization_unconfirmed"} and provider.calls == 0
    image_tasks.reconcile_image_operations(session_factory=factory)
    with factory() as session:
        assert session.get(DesignOperation, "image_operation_one").payload is None
        ledger = session.get(BudgetOperation, "image_operation_one")
        assert ledger.status == "unknown" and ledger.reserved_usd == "0.42"


def test_unscoped_saved_supplement_blocks_new_authorization(saved_version, image_configuration):
    from backend.tests.test_image_worker import FakeImageAdapter
    factory, design_id, _, directory = saved_version
    payload = {"kind": "review_supplement", "critique": {"findings": []}}
    with factory.begin() as session:
        session.add(DesignOperation(id="designop_unscoped", session_id=design_id, provider="anthropic", payload=payload))
    provider = FakeImageAdapter("openai", image_configuration, factory)
    result = image_tasks.execute_image_operation("image_operation_one", session_factory=factory,
                                                provider_registry={"openai": provider}, artifact_directory=directory)
    assert result == {"status": "blocked"} and provider.calls == 0
    with factory() as session:
        assert session.get(BudgetOperation, "image_operation_one") is None
        assert session.get(DesignOperation, "designop_unscoped").payload == payload


def test_valid_review_envelope_without_version_scope_fences_recovery(saved_version):
    from dataclasses import asdict
    from round_robin.database.visual_models import DesignOperation
    factory, design_id, version_id, _ = saved_version
    with factory.begin() as session:
        design, version = session.get(DesignSession, design_id), session.get(DesignVersion, version_id)
        receipt = new_receipt(version, "anthropic")
        critique = new_critique(version, receipt).model_dump(mode="json")
        session.add(DesignOperation(id="unscoped_valid_envelope", session_id=design.id, provider="anthropic", payload={"kind": "review_supplement", "status": "completed", "critique": critique, "receipt": asdict(receipt)}))
    with factory.begin() as session:
        with pytest.raises(VisualLimit):
            image_tasks._close_unserviceable_design(session, session.get(DesignSession, design_id))


def test_cancel_preserves_malformed_ancestor_and_marks_child_unavailable(saved_version, monkeypatch):
    from copy import deepcopy
    from backend.tests.test_visual import png
    from round_robin.visual.assets import validate_raster
    from round_robin.visual.service import commit_version
    from round_robin.visual.views import DesignSessionView
    factory, design_id, version_id, directory = saved_version
    with factory.begin() as session:
        design, parent = session.get(DesignSession, design_id), session.get(DesignVersion, version_id)
        receipt = new_receipt(parent, "anthropic")
        finding = Finding(id="finding_local", description="Critical missing title", category="observed_defect", severity="critical")
        row = commit_review(session, design, parent, new_critique(parent, receipt, findings=[finding]), receipt, expected_fence=design.execution_fence)
        identity = row.critique["findings"][0]["id"]
        child = commit_version(session, design, validate_raster(png()), directory / "visual", expected_fence=design.execution_fence,
                               parent_version_id=parent.id, source_kind="native_diagram")
        child_id, saved_id = child.id, row.id
        damaged = {"findings": deepcopy(row.critique["findings"])}
        row.critique = damaged
        revision, fence, owner = design.revision, design.execution_fence, design.owner_id
    monkeypatch.setattr(visual, "visual_enabled", lambda: None)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(settings=SimpleNamespace(command_hash_key="synthetic"))), state=SimpleNamespace())
    with factory() as session:
        result = visual.cancel_design(design_id, DesignCommand(command_id=uuid4(), expected_revision=revision), request, owner, session)
    parsed = DesignSessionView.model_validate(result)
    assert parsed.status == "cancelled" and parsed.state.fence == fence + 1
    assert {item.id for item in parsed.versions} == {version_id, child_id}
    assert all(not item.coverage.complete and not item.regression_review.complete for item in parsed.versions)
    assert all(identity in item.regression_review.unresolved_blockers for item in parsed.versions)
    assert all(any("unavailable" in line for line in item.coverage.limitations) for item in parsed.versions)
    with factory() as session:
        from round_robin.database.visual_models import DesignReview
        assert session.get(DesignReview, saved_id).critique == damaged


@pytest.mark.parametrize("bad_version", ["absent", None, "", 7, ["x"], {"id": "x"}, "version_foreign"])
def test_unscoped_supplement_marks_all_versions_unavailable_but_cancel_works(saved_version, monkeypatch, bad_version):
    from dataclasses import asdict
    from backend.tests.test_visual import png
    from round_robin.database.visual_models import DesignOperation
    from round_robin.visual.assets import validate_raster
    from round_robin.visual.service import commit_version, version_feedback_findings, version_view
    from round_robin.visual.views import DesignSessionView

    factory, design_id, version_id, directory = saved_version
    with factory.begin() as session:
        design, parent = session.get(DesignSession, design_id), session.get(DesignVersion, version_id)
        child = commit_version(session, design, validate_raster(png()), directory / "visual", expected_fence=design.execution_fence,
                               parent_version_id=parent.id, source_kind="native_diagram")
        receipt = new_receipt(parent, "anthropic")
        finding = Finding(id="finding_unscoped", description="Critical missing title", category="observed_defect", severity="critical")
        critique = new_critique(parent, receipt, findings=[finding]).model_dump(mode="json")
        payload = {"kind": "review_supplement", "critique": critique, "receipt": asdict(receipt)}
        if bad_version != "absent":
            payload["version_id"] = bad_version
        session.add(DesignOperation(id="designsupplement_unscoped", session_id=design.id, provider="anthropic", payload=payload))
        session.flush()
        for version in (parent, child):
            view = version_view(session, version)
            assert not view["coverage"]["complete"] and not view["regression_review"]["complete"]
            assert "finding_unscoped" in view["regression_review"]["unresolved_blockers"]
            with pytest.raises(VisualLimit):
                version_feedback_findings(session, design, version)
        revision, owner, fence = design.revision, design.owner_id, design.execution_fence
    monkeypatch.setattr(visual, "visual_enabled", lambda: None)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(settings=SimpleNamespace(command_hash_key="synthetic"))), state=SimpleNamespace())
    with factory() as session:
        result = visual.cancel_design(design_id, DesignCommand(command_id=uuid4(), expected_revision=revision), request, owner, session)
    parsed = DesignSessionView.model_validate(result)
    assert parsed.status == "cancelled" and parsed.state.fence == fence + 1
    assert len(parsed.versions) == 2 and all(not item.coverage.complete for item in parsed.versions)
