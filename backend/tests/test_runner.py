"""Offline assertions only; these do not pass the Phase 0B paid study gate."""

from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from experiments.journal import Journal, JournalError
from experiments.runner import OfflineRunner, Policy, PolicyError


CREDENTIALS = ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GOOGLE_API_KEY", "GEMINI_API_KEY", "AZURE_OPENAI_API_KEY")
ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def no_credentials(monkeypatch):
    for name in CREDENTIALS:
        monkeypatch.delenv(name, raising=False)


class Clock:
    def __init__(self):
        self.now = datetime(2026, 1, 31, 23, 59, tzinfo=timezone.utc)

    def __call__(self):
        return self.now


def ready(journal, policy=None, clock=None):
    runner = OfflineRunner(journal, policy=policy, clock=clock)
    runner.open_batch("batch")
    runner.open_run("batch", "run")
    return runner


def authorize(runner, attempt="a1", step="proposal", **kwargs):
    return runner.authorize(run_id="run", step_id=step, attempt_id=attempt, bound_usd=kwargs.pop("bound_usd", "0.10"), **kwargs)


def test_journal_lock_is_exclusive_between_processes(tmp_path):
    path = tmp_path / "authority"
    with Journal(path):
        code = "from experiments.journal import Journal, JournalBusy\nimport sys\ntry:\n with Journal(sys.argv[1]): pass\nexcept JournalBusy:\n sys.exit(17)\n"
        result = subprocess.run([sys.executable, "-c", code, str(path)], cwd=ROOT, capture_output=True, text=True)
        assert result.returncode == 17, result.stderr
    with Journal(path):
        pass


def test_process_crash_retains_authorization_and_never_resends(tmp_path):
    path = tmp_path / "authority"
    code = "from experiments.journal import Journal\nfrom experiments.runner import OfflineRunner\nimport os,sys\nwith Journal(sys.argv[1]) as j:\n r=OfflineRunner(j)\n r.open_batch('batch')\n r.open_run('batch','run')\n r.authorize(run_id='run',step_id='proposal',attempt_id='possibly-sent',bound_usd='0.25')\n os._exit(19)\n"
    result = subprocess.run([sys.executable, "-c", code, str(path)], cwd=ROOT, capture_output=True, text=True)
    assert result.returncode == 19, result.stderr
    with Journal(path) as journal:
        runner = OfflineRunner(journal)
        assert runner.summary()["unsettled_attempt_ids"] == ["possibly-sent"]
        assert Decimal(runner.summary()["deployment_daily_exposure_usd"]) == Decimal("0.65")
        with pytest.raises(PolicyError, match="already_authorized"):
            authorize(runner, "possibly-sent", bound_usd="0.25")
        assert runner.summary()["authorized_simulated_attempts"] == 1


def test_failed_durability_acknowledgment_prevents_adapter_send(tmp_path, monkeypatch):
    from experiments.adapters import SimulatedAdapter

    path = tmp_path / "authority"
    adapter = SimulatedAdapter()
    with Journal(path) as journal:
        runner = ready(journal)

        def failed_sync(_fd):
            raise OSError("injected durability failure")

        with monkeypatch.context() as patch:
            patch.setattr("experiments.journal.os.fsync", failed_sync)
            with pytest.raises(OSError):
                authorize(runner)
                adapter.generate({})  # reachable only after durable authorization
        assert adapter.call_count == 0
    with pytest.raises(JournalError, match="mismatch"):
        with Journal(path):
            pass


@pytest.mark.parametrize("damage", ["partial_line", "whole_line", "empty", "corrupt", "missing_events", "missing_manifest", "missing_head"])
def test_corrupt_truncated_or_missing_journal_fails_closed(tmp_path, damage):
    path = tmp_path / "authority"
    with Journal(path) as journal:
        ready(journal)
    events = path / "events.jsonl"
    original = events.read_bytes()
    if damage == "partial_line":
        events.write_bytes(original[:-3])
    elif damage == "whole_line":
        events.write_bytes(b"\n".join(original.split(b"\n")[:-2]) + b"\n")
    elif damage == "empty":
        events.write_bytes(b"")
    elif damage == "corrupt":
        events.write_bytes(original.replace(b"local-operator", b"other-operator"))
    else:
        {"missing_events": events, "missing_manifest": path / "manifest.json", "missing_head": path / "head.json"}[damage].unlink()
    with pytest.raises(JournalError):
        with Journal(path):
            pass


def test_same_batch_cli_replay_executes_zero_additional_work(tmp_path):
    cmd = [sys.executable, "-m", "experiments.runner", "--journal-dir", str(tmp_path / "authority"), "--batch-id", "sample", "--simulate"]
    first = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
    second = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
    assert first.returncode == second.returncode == 0, (first.stderr, second.stderr)
    a, b = json.loads(first.stdout), json.loads(second.stdout)
    assert a["authorized_simulated_attempts"] == b["authorized_simulated_attempts"] == 5
    assert b["replayed_without_execution"] is True
    assert b["live_provider_calls"] == 0 and b["phase_0_gate_passed"] is False


def test_step_and_run_call_limits_include_recovery_and_protect_finish(tmp_path):
    with Journal(tmp_path / "authority") as journal:
        runner = ready(journal)
        for index in range(2):
            attempt = authorize(runner, f"retry-{index}", bound_usd="0.01")
            runner.settle(attempt, reported_usd="0", trusted_final=True, outcome="failed")
        with pytest.raises(PolicyError, match="step_call_limit"):
            authorize(runner, "third")
        for index in range(20):
            attempt = authorize(runner, f"optional-{index}", f"step-{index}", bound_usd="0.01")
            runner.settle(attempt, reported_usd="0", trusted_final=True, outcome="succeeded")
        with pytest.raises(PolicyError, match="finish_call_reserve"):
            authorize(runner, "borrow-slot", "extra", bound_usd="0.01")
        authorize(runner, "final-1", "synthesis", finish_role="synthesis")
        authorize(runner, "final-2", "audit", finish_role="audit")
        with pytest.raises(PolicyError, match="request_limit"):
            authorize(runner, "call-25", "extra")
        assert runner.summary()["authorized_simulated_attempts"] == 24


def test_unknown_price_and_higher_reported_usage_block_spending(tmp_path):
    with Journal(tmp_path / "authority") as journal:
        runner = ready(journal)
        with pytest.raises(PolicyError, match="unknown_price"):
            authorize(runner, bound_usd=None)
        authorize(runner)
        runner.settle("a1", reported_usd="3", trusted_final=False, outcome="succeeded")
        assert Decimal(runner.summary()["deployment_daily_exposure_usd"]) == Decimal("3.40")
        with pytest.raises(PolicyError, match="currency_limit"):
            authorize(runner, "a2")


def test_finish_money_converts_once_and_optional_work_cannot_borrow(tmp_path):
    policy = replace(Policy(), run_usd="0.40")
    with Journal(tmp_path / "authority") as journal:
        runner = ready(journal, policy)
        assert Decimal(runner.summary()["deployment_daily_exposure_usd"]) == Decimal("0.40")
        with pytest.raises(PolicyError, match="currency_limit"):
            authorize(runner, bound_usd="0.01")
        authorize(runner, "syn", "synthesis", bound_usd="0.20", finish_role="synthesis")
        assert Decimal(runner.summary()["deployment_daily_exposure_usd"]) == Decimal("0.40")
        with pytest.raises(PolicyError, match="finish_role"):
            authorize(runner, "syn-again", "synthesis", bound_usd="0.20", finish_role="synthesis")
        authorize(runner, "aud", "audit", bound_usd="0.20", finish_role="audit")
        assert Decimal(runner.summary()["deployment_daily_exposure_usd"]) == Decimal("0.40")


def test_finish_time_is_protected_and_restart_does_not_renew_time(tmp_path):
    clock = Clock()
    path = tmp_path / "authority"
    with Journal(path) as journal:
        ready(journal, clock=clock)
    clock.now = datetime(2026, 2, 1, 0, 8, tzinfo=timezone.utc)  # 540 active seconds
    with Journal(path) as journal:
        runner = OfflineRunner(journal, clock=clock)
        with pytest.raises(PolicyError, match="finish_time_reserve"):
            authorize(runner, timeout_seconds=30)
        authorize(runner, "syn", "synthesis", timeout_seconds=30, finish_role="synthesis")


def test_unknown_exposure_survives_booking_and_day_month_rollover(tmp_path):
    clock = Clock()
    policy = replace(Policy(), owner_daily_usd="0.60", owner_monthly_usd="0.60", deployment_daily_usd="0.60", deployment_monthly_usd="0.60")
    path = tmp_path / "authority"
    with Journal(path) as journal:
        runner = ready(journal, policy, clock)
        authorize(runner, bound_usd="0.20")
        runner.settle("a1", reported_usd=None, trusted_final=False, outcome="unknown")
        runner.book_conservative("a1", "0.20")
        runner.close_run("run")
    clock.now = datetime(2026, 2, 1, tzinfo=timezone.utc)
    with Journal(path) as journal:
        runner = OfflineRunner(journal, policy, clock)
        runner.open_batch("second-batch")
        runner.open_run("second-batch", "second-run")
        assert Decimal(runner.summary()["deployment_daily_exposure_usd"]) == Decimal("0.60")
        with pytest.raises(PolicyError, match="currency_limit"):
            runner.authorize(run_id="second-run", step_id="proposal", attempt_id="new", bound_usd="0.01")
        runner.reconcile("a1", "provider-evidence-1", final_usd="0.10")
        runner.reconcile("a1", "provider-evidence-1", final_usd="0.10")
        # Settled charge stays in January; adjustment/replay does not count twice.
        assert Decimal(runner.summary()["deployment_daily_exposure_usd"]) == Decimal("0.40")


def test_kill_switch_and_policy_are_preserved_on_restart(tmp_path):
    path = tmp_path / "authority"
    with Journal(path) as journal:
        runner = ready(journal)
        runner.set_spending_lock(True)
    with Journal(path) as journal:
        runner = OfflineRunner(journal)
        with pytest.raises(PolicyError, match="locked"):
            authorize(runner)
        with pytest.raises(PolicyError, match="policy mismatch"):
            OfflineRunner(journal, replace(Policy(), run_usd="3"))


def test_clock_regression_refuses_authorization(tmp_path):
    clock = Clock()
    with Journal(tmp_path / "authority") as journal:
        runner = ready(journal, clock=clock)
        clock.now = datetime(2026, 1, 30, tzinfo=timezone.utc)
        with pytest.raises(PolicyError, match="clock_regression"):
            authorize(runner)
