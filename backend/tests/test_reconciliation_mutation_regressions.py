"""Financial reconciliation must not release uncertainty without bound evidence."""

import hashlib
import json
from decimal import Decimal

import pytest
from round_robin.database import services
from round_robin.database.models import Attempt, BudgetOperation, Step
from round_robin.operations import recovery
from round_robin.operations.recovery import OperationError, reconcile
from round_robin.orchestration.budget import operations_exposure

pytest_plugins = ("test_operations",)


def _set_identity(app, *, kind="public_search", provider="brave", request="request-synthetic"):
    with app.state.session_factory.begin() as session:
        operation = session.get(BudgetOperation, "operation_unknown")
        operation.kind = kind
        operation.metadata_json = {"provider": provider, "provider_request_id": request}


def _receipt(tmp_path, **changes):
    receipt = {
        "operation_id": "operation_unknown",
        "final": True,
        "provider": "brave",
        "provider_request_id": "request-synthetic",
        "usage_evidence": {"requests": 1},
        "reported_usd": "0.10",
    }
    receipt.update(changes)
    path = tmp_path / "receipt.json"
    path.write_text(json.dumps(receipt))
    return path


def _assert_unresolved(app):
    with app.state.session_factory() as session:
        operation = session.get(BudgetOperation, "operation_unknown")
        assert operation.status == "unknown"
        assert operation.settled_usd is None
        assert "provider_receipt_sha256" not in operation.metadata_json
        assert operations_exposure([operation], period="daily") >= Decimal("0.40")


@pytest.mark.parametrize("changes", [
    {"operation_id": "operation_foreign"},
    {"final": False},
    {"final": 1},
    {"provider_request_id": "other-request"},
    {"provider": "other-provider"},
    {"usage_evidence": None},
])
def test_receipt_must_match_every_binding_and_be_final(prepared, tmp_path, changes):
    app, *_ = prepared
    _set_identity(app)
    with pytest.raises(OperationError):
        reconcile(app.state.session_factory, "operation_unknown", provider_receipt=_receipt(tmp_path, **changes))
    _assert_unresolved(app)


@pytest.mark.parametrize("missing", ["provider", "request"])
def test_missing_recorded_identity_cannot_be_supplied_by_receipt(prepared, tmp_path, missing):
    app, *_ = prepared
    identity = {missing: None}
    _set_identity(app, **identity)
    receipt_field = "provider" if missing == "provider" else "provider_request_id"
    with pytest.raises(OperationError):
        reconcile(app.state.session_factory, "operation_unknown", provider_receipt=_receipt(tmp_path, **{receipt_field: None}))
    _assert_unresolved(app)


def test_model_receipt_requires_attempt_even_if_operation_metadata_matches(prepared, tmp_path):
    app, *_ = prepared
    _set_identity(app, kind="model")
    with pytest.raises(OperationError):
        reconcile(app.state.session_factory, "operation_unknown", provider_receipt=_receipt(tmp_path))
    _assert_unresolved(app)


def test_final_receipt_is_recorded_with_its_exact_digest(prepared, tmp_path):
    app, *_ = prepared
    _set_identity(app)
    path = _receipt(tmp_path)
    result = reconcile(app.state.session_factory, "operation_unknown", provider_receipt=path)
    assert result == {"operation_id": "operation_unknown", "status": "settled", "uncertainty_released": True}
    with app.state.session_factory() as session:
        operation = session.get(BudgetOperation, "operation_unknown")
        assert Decimal(operation.settled_usd) == Decimal("0.10")
        assert operation.metadata_json["provider_receipt_sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
        assert operation.metadata_json["provider_request_id"] == "request-synthetic"


def test_model_receipt_records_attempt_request_identity(prepared, tmp_path):
    app, _, run, _, _ = prepared
    with app.state.session_factory.begin() as session:
        session.add(Step(id="step_reconciliation", run_id=run["id"], stage="synthesis", role_slot_id="synthesis", provider="brave", status="failed"))
        session.flush()
        session.add(Attempt(id="operation_unknown", run_id=run["id"], step_id="step_reconciliation", provider="brave", status="unknown", provider_request_id="request-synthetic", reserved_usd="0.40", usage_certainty="unknown"))
    reconcile(app.state.session_factory, "operation_unknown", provider_receipt=_receipt(tmp_path))
    with app.state.session_factory() as session:
        operation = session.get(BudgetOperation, "operation_unknown")
        assert operation.status == "settled"
        assert operation.metadata_json["provider_request_id"] == "request-synthetic"


def test_small_operator_booking_preserves_reserved_exposure(prepared):
    app, *_ = prepared
    result = reconcile(app.state.session_factory, "operation_unknown", conservative_usd="0.01")
    assert result["status"] == "unknown"
    assert result["uncertainty_released"] is False
    with app.state.session_factory() as session:
        operation = session.get(BudgetOperation, "operation_unknown")
        assert Decimal(operation.metadata_json["conservative_booking_usd"]) >= Decimal("0.40")
        assert operation.metadata_json["operator_booking"] is True
        assert operations_exposure([operation], period="daily") >= Decimal("0.40")


def test_booking_observes_settlement_committed_before_lock(prepared, monkeypatch):
    app, _, run, _, _ = prepared
    original = recovery.lock_policies
    original_locked_run = services.locked_run
    calls = []

    def settle_before_lock(session, owner_id):
        calls.append(("policy", owner_id))
        with app.state.session_factory.begin() as other:
            operation = other.get(BudgetOperation, "operation_unknown")
            operation.status = "settled"
            operation.settled_usd = "0.20"
        return original(session, owner_id)

    def lock_run(session, run_id):
        calls.append(("run", run_id))
        return original_locked_run(session, run_id)

    monkeypatch.setattr(recovery, "lock_policies", settle_before_lock)
    monkeypatch.setattr(services, "locked_run", lock_run)
    result = reconcile(app.state.session_factory, "operation_unknown", conservative_usd="0.50")
    assert calls == [("policy", "owner_local"), ("run", run["id"])]
    assert result["status"] == "settled"
    assert result["uncertainty_released"] is True
    with app.state.session_factory() as session:
        operation = session.get(BudgetOperation, "operation_unknown")
        assert operation.status == "settled"
        assert Decimal(operation.settled_usd) == Decimal("0.20")
