"""Independent audit of offline append-only accounting and adapter controls."""
from decimal import Decimal
import json

import pytest

from experiments.journal import Journal, JournalError
from experiments.runner import OfflineRunner, PolicyError


@pytest.mark.parametrize("target", ["manifest", "head", "sequence"])
def test_journal_integrity_rejects_boolean_or_float_substitutes_for_integer_fields(tmp_path, target):
    path = tmp_path / "journal"
    with Journal(path) as journal:
        journal.append({"type": "audit_marker"})
    if target == "manifest":
        source = path / "manifest.json"
        body = json.loads(source.read_bytes())
        body["schema_version"] = True
        source.write_text(json.dumps(body) + "\n")
    elif target == "head":
        source = path / "head.json"
        body = json.loads(source.read_bytes())
        body["sequence"] = 1.0
        source.write_text(json.dumps(body) + "\n")
    else:
        source = path / "events.jsonl"
        body = json.loads(source.read_bytes())
        body["sequence"] = True
        source.write_text(json.dumps(body) + "\n")
    with pytest.raises(JournalError):
        with Journal(path):
            pass


@pytest.mark.parametrize("certainty", ["false", "true", 0, 1, None, [], {}])
def test_untyped_final_usage_certainty_cannot_release_unknown_exposure(tmp_path, certainty):
    with Journal(tmp_path / "journal") as journal:
        runner = OfflineRunner(journal)
        runner.open_batch("batch")
        runner.open_run("batch", "run")
        runner.authorize(run_id="run", step_id="proposal", attempt_id="attempt", bound_usd="0.20")
        before = runner.summary()
        with pytest.raises(PolicyError):
            runner.settle("attempt", reported_usd="0", trusted_final=certainty, outcome="unknown")
        assert runner.summary() == before
        assert Decimal(runner.summary()["deployment_daily_exposure_usd"]) == Decimal("0.60")


@pytest.mark.parametrize("precision", [1, 6, 28])
@pytest.mark.parametrize("trap_inexact", [False, True])
def test_journal_financial_transfers_and_caps_ignore_caller_decimal_context(tmp_path, precision, trap_inexact):
    from decimal import Inexact, localcontext
    from experiments.runner import Policy
    policy = Policy(batch_usd="0.15", run_usd="0.15", finish_role_usd="0.05", owner_daily_usd="0.15", owner_monthly_usd="0.15", deployment_daily_usd="0.15", deployment_monthly_usd="0.15")
    with Journal(tmp_path / "fractional-authority") as journal:
        runner = OfflineRunner(journal, policy)
        runner.open_batch("batch")
        runner.open_run("batch", "run")
        runner.authorize(run_id="run", step_id="proposal", attempt_id="proposal", bound_usd="0.049")
        with localcontext() as ambient:
            ambient.prec, ambient.traps[Inexact] = precision, trap_inexact
            runner.authorize(run_id="run", step_id="synthesis", attempt_id="synthesis", bound_usd="0.031", finish_role="synthesis")
            assert runner._state()["runs"]["run"]["holds"]["synthesis"] == Decimal("0.019")
            assert Decimal(runner.summary()["deployment_daily_exposure_usd"]) == Decimal("0.149")
            before = len(journal.events)
            with pytest.raises(PolicyError, match="currency_limit"):
                runner.authorize(run_id="run", step_id="optional", attempt_id="excess", bound_usd="0.01")
            assert len(journal.events) == before
