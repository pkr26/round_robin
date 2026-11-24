"""Ledger arithmetic cannot inherit rounding from a caller's Decimal context."""
from decimal import Decimal, Inexact, localcontext

import pytest
from sqlalchemy import select

from backend.tests.test_orchestration import factory, seed
from backend.tests.test_providers import configuration, packet
from round_robin.api.preflight import run_configuration
from round_robin.configuration import Settings
from round_robin.database.models import BudgetOperation, Run, Step, utcnow
from round_robin.database.services import lock_policies
from round_robin.orchestration.admission import lock_work_run
from round_robin.orchestration.budget import BudgetDenied, authorize_operation, operations_exposure
from round_robin.orchestration.controller import advance_run
from round_robin.providers.http import HTTPProvider


@pytest.mark.parametrize("precision", [6, 28, 128])
def test_aggregate_exposure_preserves_supported_fractional_token_costs_at_large_caps(precision):
    operations = [BudgetOperation(id="whole_charge", status="settled", settled_usd="1000000000000", authorized_at=utcnow(), metadata_json={}), BudgetOperation(id="fractional_charge", status="unknown", reserved_usd="0.000000000000000001", metadata_json={})]
    with localcontext() as ambient:
        ambient.prec = precision
        ambient.traps[Inexact] = True
        exposure = operations_exposure(operations)
    assert exposure == Decimal("1000000000000.000000000000000001")


@pytest.mark.parametrize("precision", [6, 28, 128])
def test_new_fractional_authorization_cannot_round_back_under_an_exact_currency_cap(factory, monkeypatch, precision):
    monkeypatch.setattr("round_robin.orchestration.configuration.verify_frozen_configuration", lambda _: None)
    config = run_configuration(Settings(), "simulated")
    config.update(mode="live", funded=True)
    config["finish_policy"]["reserved_usd"] = "0"
    for key in ("per_run_usd", "owner_daily_usd", "owner_monthly_usd", "deployment_daily_usd", "deployment_monthly_usd"):
        config["limits"][key] = "1000000000000"
    run_id = seed(factory, config=config)
    with factory.begin() as session:
        for policy in lock_policies(session, "owner_test"):
            policy.spending_locked = False
            policy.limit_usd = "1000000000000"
            policy.metadata_json = {"daily_usd": "1000000000000", "monthly_usd": "1000000000000"}
        run = lock_work_run(session, run_id)
        advance_run(session, run)
        session.add(BudgetOperation(id="full_cap", run_id=run_id, owner_id=run.owner_id, status="settled", kind="test", reserved_usd="1000000000000", settled_usd="1000000000000", authorized_at=utcnow(), metadata_json={}))
        session.flush()
        with localcontext() as ambient:
            ambient.prec = precision
            ambient.traps[Inexact] = False
            with pytest.raises(BudgetDenied, match="run_budget_exhausted"):
                authorize_operation(session, run, "excess_fraction", "public_search", "0.000000000000000001", model_call=False)
        assert session.get(BudgetOperation, "excess_fraction") is None


def test_finish_hold_conversion_preserves_every_fraction_under_a_hostile_decimal_context(factory, monkeypatch):
    monkeypatch.setattr("round_robin.orchestration.configuration.verify_frozen_configuration", lambda _: None)
    config = run_configuration(Settings(), "simulated")
    config.update(mode="live", funded=True)
    run_id = seed(factory, config=config)
    with factory.begin() as session:
        for policy in lock_policies(session, "owner_test"):
            policy.spending_locked = False
            policy.limit_usd = "10"
            policy.metadata_json = {"daily_usd": "10", "monthly_usd": "50"}
        run = lock_work_run(session, run_id)
        advance_run(session, run)
        step = session.scalar(select(Step).where(Step.run_id == run_id))
        step.stage, step.role_slot_id = "synthesis", "synthesis"
        with localcontext() as ambient:
            ambient.prec = 6
            ambient.traps[Inexact] = True
            authorize_operation(session, run, "fractional_finish", "model", "0.000000000000000001", step=step, finish_role="synthesis")
        hold = session.get(BudgetOperation, f"finish_{run_id}")
        assert Decimal(hold.reserved_usd) == Decimal("0.399999999999999999")
        assert operations_exposure(list(session.scalars(select(BudgetOperation)))) == Decimal("0.40")


def test_provider_conservative_bound_is_independent_of_ambient_decimal_rounding():
    config = configuration("openai")
    config["pricing"]["openai"].update(input_usd_per_million="1.234567891234", output_usd_per_million="9.876543219876")
    adapter = HTTPProvider("openai", config, api_key="synthetic")
    expected = adapter.conservative_bound(packet())
    with localcontext() as ambient:
        ambient.prec = 6
        ambient.traps[Inexact] = True
        assert adapter.conservative_bound(packet()) == expected


def test_report_accounting_preserves_exact_settled_costs_under_hostile_decimal_context(factory):
    from round_robin.database.models import ReportVersion
    from round_robin.orchestration.controller import finalize_run
    config = run_configuration(Settings(), "simulated")
    config.update(mode="live", funded=True)
    run_id = seed(factory, config=config)
    with factory.begin() as session:
        run = session.get(Run, run_id)
        for identity, value in (("large_settled", "1000000000000"), ("tiny_settled", "0.000000000000000001")):
            session.add(BudgetOperation(id=identity, run_id=run_id, owner_id=run.owner_id, kind="model", status="settled", reserved_usd=value, settled_usd=value, authorized_at=utcnow(), metadata_json={"model_call": True}))
        session.flush()
        with localcontext() as ambient:
            ambient.prec = 6
            ambient.traps[Inexact] = True
            finalize_run(session, run, reason="test_no_review")
    with factory() as session:
        usage = session.scalar(select(ReportVersion).where(ReportVersion.run_id == run_id)).content["usage"]
        assert Decimal(usage["actual_cost"]) == Decimal("1000000000000.000000000000000001")
        assert usage["actual_cost"] == usage["conservative_exposure_usd"]
