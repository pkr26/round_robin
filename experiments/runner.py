"""Bounded offline Phase 0 runner. Live providers are deliberately unavailable.

The journal directory is the aggregate authority and must be reused across batches.
This single-process simulator is not proof of PostgreSQL/worker concurrency, real
provider pricing, or protection against an operator deleting/relocating history.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Context, Decimal, InvalidOperation, localcontext
from functools import wraps
import hashlib
import json
from pathlib import Path
import re
import sys
from typing import Any, Callable

from experiments.adapters import ContractError, SimulatedAdapter, prepare_packet, require_offline_environment
from experiments.journal import Journal, JournalError


class PolicyError(RuntimeError):
    pass


def _exact_money(function):
    """Keep journal accounting independent of caller precision and traps."""
    @wraps(function)
    def calculate(*args, **kwargs):
        with localcontext(Context(prec=128)):
            return function(*args, **kwargs)
    return calculate


def money(value: Any) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise PolicyError("invalid monetary amount") from exc
    if not result.is_finite() or result < 0:
        raise PolicyError("monetary amounts must be finite and nonnegative")
    return result


def identity(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value):
        raise PolicyError("invalid noncontent identity")
    return value


@dataclass(frozen=True)
class Policy:
    batch_requests: int = 24
    batch_usd: str = "2.00"
    run_requests: int = 24
    run_usd: str = "2.00"
    owner_daily_usd: str = "10.00"
    owner_monthly_usd: str = "50.00"
    deployment_daily_usd: str = "10.00"
    deployment_monthly_usd: str = "50.00"
    active_seconds: int = 600
    finish_role_usd: str = "0.20"
    finish_role_seconds: int = 20

    def __post_init__(self) -> None:
        for key, value in asdict(self).items():
            if key.endswith("usd"):
                money(value)
            elif type(value) is not int or value <= 0:
                raise PolicyError("invalid count/time policy")
        if self.run_requests > 24 or self.run_requests < 2 or self.active_seconds > 600:
            raise PolicyError("policy exceeds text-run bounds or lacks finish slots")


class JournalBudget:
    def __init__(self, journal: Journal, policy: Policy | None = None, clock: Callable[[], datetime] | None = None):
        self.journal = journal
        self.policy = policy or Policy()
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        if not journal.events:
            journal.append({"type": "policy", "policy": asdict(self.policy), "at": self._now()})
        elif journal.events[0].get("type") != "policy" or journal.events[0].get("policy") != asdict(self.policy):
            raise PolicyError("journal policy mismatch; limits cannot reset on restart")
        self._state()

    def _now(self) -> str:
        now = self.clock()
        if now.tzinfo is None:
            raise PolicyError("clock must be timezone-aware")
        value = now.astimezone(timezone.utc).isoformat()
        if self.journal.events and value < self.journal.events[-1]["at"]:
            raise PolicyError("clock_regression: no new authorization")
        return value

    def _append(self, kind: str, **fields: Any) -> None:
        self.journal.append({"type": kind, **fields, "at": self._now()})

    @_exact_money
    def _state(self) -> dict[str, Any]:
        state: dict[str, Any] = {"batches": {}, "runs": {}, "attempts": {}, "locked": False, "adjustments": {}}
        try:
            for event in self.journal.events:
                kind = event["type"]
                if kind == "policy":
                    continue
                if kind == "batch_opened":
                    if event["batch_id"] in state["batches"]:
                        raise ValueError("duplicate batch")
                    state["batches"][event["batch_id"]] = event
                elif kind == "run_opened":
                    if event["run_id"] in state["runs"]:
                        raise ValueError("duplicate run")
                    state["runs"][event["run_id"]] = {**event, "holds": {"synthesis": money(self.policy.finish_role_usd), "audit": money(self.policy.finish_role_usd)}, "final_roles": set(), "terminal": False, "correction_count": 0}
                elif kind == "authorized":
                    if event["attempt_id"] in state["attempts"]:
                        raise ValueError("duplicate authorization")
                    run = state["runs"][event["run_id"]]
                    role = event["finish_role"]
                    if role:
                        run["holds"][role] -= money(event["bound_usd"])
                        if run["holds"][role] < 0 or role in run["final_roles"]:
                            raise ValueError("invalid finish transfer")
                        run["final_roles"].add(role)
                    state["attempts"][event["attempt_id"]] = {**event, "settlement": None, "booking": Decimal("0")}
                elif kind == "settled":
                    attempt = state["attempts"][event["attempt_id"]]
                    if attempt["settlement"] is not None:
                        raise ValueError("duplicate settlement")
                    attempt["settlement"] = event
                elif kind == "reconciled":
                    if event["adjustment_id"] in state["adjustments"]:
                        raise ValueError("duplicate adjustment")
                    state["adjustments"][event["adjustment_id"]] = event
                    state["attempts"][event["attempt_id"]]["settlement"] = event
                elif kind == "booked":
                    attempt = state["attempts"][event["attempt_id"]]
                    attempt["booking"] = max(attempt["booking"], money(event["amount_usd"]))
                elif kind == "spending_lock":
                    state["locked"] = event["enabled"]
                elif kind == "discussion_recorded":
                    run = state["runs"][event["run_id"]]
                    if run["terminal"] or not isinstance(event["issues"], list):
                        raise ValueError("invalid discussion journal state")
                    run["discussion_issues"] = event["issues"]
                elif kind == "correction_reserved":
                    run = state["runs"][event["run_id"]]
                    if run["terminal"] or run["correction_count"] or run["final_roles"] != {"synthesis", "audit"}:
                        raise ValueError("invalid correction transition")
                    run["correction_count"] = 1
                    run["final_roles"] = set()
                    for role in ("synthesis", "audit"):
                        run["holds"][role] += money(self.policy.finish_role_usd)
                elif kind == "run_closed":
                    run = state["runs"][event["run_id"]]
                    run["terminal"] = True
                    run["holds"] = {"synthesis": Decimal("0"), "audit": Decimal("0")}
                else:
                    raise ValueError("unknown event")
        except (KeyError, ValueError, TypeError, InvalidOperation) as exc:
            raise JournalError("invalid journal event history") from exc
        return state

    def open_batch(self, batch_id: str, *, metadata: dict | None = None) -> None:
        identity(batch_id)
        existing = self._state()["batches"].get(batch_id)
        if existing and existing.get("metadata") != metadata:
            raise PolicyError("batch authorization binding changed")
        if not existing:
            self._append("batch_opened", batch_id=batch_id, metadata=metadata)

    @_exact_money
    def open_run(self, batch_id: str, run_id: str, owner_id: str = "local-operator") -> None:
        identity(batch_id), identity(run_id), identity(owner_id)
        state = self._state()
        if batch_id not in state["batches"]:
            raise PolicyError("unknown batch")
        if run_id in state["runs"]:
            run = state["runs"][run_id]
            if run["batch_id"] != batch_id or run["owner_id"] != owner_id:
                raise PolicyError("run identity conflict")
            return
        if state["locked"]:
            raise PolicyError("spending_locked")
        batch_attempts = sum(a["batch_id"] == batch_id for a in state["attempts"].values())
        protected_calls = sum(2 - len(r["final_roles"]) for r in state["runs"].values() if r["batch_id"] == batch_id and not r["terminal"])
        if batch_attempts + protected_calls + 2 > self.policy.batch_requests:
            raise PolicyError("batch_finish_call_reserve")
        at = self._now()
        reserve = money(self.policy.finish_role_usd) * 2
        self._check_money(state, batch_id, owner_id, run_id, reserve, at)
        self._append("run_opened", batch_id=batch_id, run_id=run_id, owner_id=owner_id)

    @_exact_money
    def _exposure(self, state: dict[str, Any], at: str, *, scope: str, value: str | None = None, period: str | None = None) -> Decimal:
        total = Decimal("0")
        for run in state["runs"].values():
            if scope != "deployment" and run.get(scope) != value:
                continue
            # Unused finish holds carry into active windows until explicit closing.
            total += sum(run["holds"].values(), Decimal("0"))
            for attempt in state["attempts"].values():
                if attempt["run_id"] != run["run_id"]:
                    continue
                settled = attempt["settlement"]
                if settled and settled["trusted_final"]:
                    if period == "daily" and attempt["at"][:10] != at[:10]:
                        continue
                    if period == "monthly" and attempt["at"][:7] != at[:7]:
                        continue
                    total += money(settled["reported_usd"])
                else:
                    reported = money(settled["reported_usd"]) if settled and settled["reported_usd"] is not None else Decimal("0")
                    total += max(money(attempt["bound_usd"]), reported, attempt["booking"])
        return total

    @_exact_money
    def _check_money(self, state: dict[str, Any], batch_id: str, owner_id: str, run_id: str, extra: Decimal, at: str) -> None:
        checks = [
            ("batch_id", batch_id, None, self.policy.batch_usd),
            ("run_id", run_id, None, self.policy.run_usd),
            ("owner_id", owner_id, "daily", self.policy.owner_daily_usd),
            ("owner_id", owner_id, "monthly", self.policy.owner_monthly_usd),
            ("deployment", None, "daily", self.policy.deployment_daily_usd),
            ("deployment", None, "monthly", self.policy.deployment_monthly_usd),
        ]
        for scope, value, period, limit in checks:
            if self._exposure(state, at, scope=scope, value=value, period=period) + extra > money(limit):
                raise PolicyError(f"currency_limit: {scope} {period or 'total'}")

    @_exact_money
    def authorize(self, *, run_id: str, step_id: str, attempt_id: str, bound_usd: str | None, timeout_seconds: int = 30, finish_role: str | None = None, metadata: dict | None = None) -> str:
        """Durably reserve; an existing identity NEVER grants permission to resend."""
        identity(run_id), identity(step_id), identity(attempt_id)
        if bound_usd is None:
            raise PolicyError("unknown_price: conservative bound required")
        bound = money(bound_usd)
        if type(timeout_seconds) is not int or timeout_seconds <= 0:
            raise PolicyError("invalid timeout")
        if finish_role not in {None, "synthesis", "audit"}:
            raise PolicyError("invalid final role")
        state = self._state()
        if attempt_id in state["attempts"]:
            raise PolicyError("attempt_already_authorized: reconcile identity; do not resend")
        if run_id not in state["runs"]:
            raise PolicyError("unknown run")
        run = state["runs"][run_id]
        if state["locked"] or run["terminal"]:
            raise PolicyError("spending_locked_or_terminal")
        attempts = list(state["attempts"].values())
        if sum(a["batch_id"] == run["batch_id"] for a in attempts) >= self.policy.batch_requests:
            raise PolicyError("batch_request_limit")
        run_attempts = [a for a in attempts if a["run_id"] == run_id]
        if sum(a["step_id"] == step_id for a in run_attempts) >= 2:
            raise PolicyError("step_call_limit")
        if len(run_attempts) >= self.policy.run_requests:
            raise PolicyError("run_request_limit")
        protected_roles = {"synthesis", "audit"} - run["final_roles"]
        at = self._now()
        elapsed = (datetime.fromisoformat(at) - datetime.fromisoformat(run["at"])).total_seconds()
        if elapsed < 0:
            raise PolicyError("clock_regression")
        if finish_role:
            if finish_role not in protected_roles or bound > run["holds"][finish_role]:
                raise PolicyError("finish_role_already_used_or_underfunded")
            # Conversion replaces the designated hold rather than adding exposure.
            extra = Decimal("0")
            remaining_roles = len(protected_roles) - 1
        else:
            extra = bound
            remaining_roles = len(protected_roles)
        if len(run_attempts) + 1 + remaining_roles > self.policy.run_requests:
            raise PolicyError("finish_call_reserve")
        # Batch slots, just like money, cannot be consumed by optional work.
        remaining_batch_roles = sum(2 - len(r["final_roles"]) for r in state["runs"].values() if r["batch_id"] == run["batch_id"] and not r["terminal"])
        if finish_role:
            remaining_batch_roles -= 1
        if sum(a["batch_id"] == run["batch_id"] for a in attempts) + 1 + remaining_batch_roles > self.policy.batch_requests:
            raise PolicyError("batch_finish_call_reserve")
        if elapsed + timeout_seconds + remaining_roles * self.policy.finish_role_seconds > self.policy.active_seconds:
            raise PolicyError("finish_time_reserve")
        self._check_money(state, run["batch_id"], run["owner_id"], run_id, extra, at)
        self._append("authorized", batch_id=run["batch_id"], run_id=run_id, step_id=step_id, attempt_id=attempt_id, bound_usd=str(bound), timeout_seconds=timeout_seconds, finish_role=finish_role, metadata=metadata)
        return attempt_id

    def settle(self, attempt_id: str, *, reported_usd: str | None, trusted_final: bool, outcome: str, metadata: dict | None = None) -> None:
        if type(trusted_final) is not bool:
            raise PolicyError("invalid usage certainty")
        if outcome not in {"succeeded", "failed", "refused", "unknown"}:
            raise PolicyError("invalid outcome")
        amount = str(money(reported_usd)) if reported_usd is not None else None
        if trusted_final and amount is None:
            raise PolicyError("final usage requires evidence amount")
        attempt = self._state()["attempts"].get(attempt_id)
        if not attempt:
            raise PolicyError("unknown attempt")
        values = {"attempt_id": attempt_id, "reported_usd": amount, "trusted_final": bool(trusted_final), "outcome": outcome}
        if attempt["settlement"]:
            if all(attempt["settlement"].get(k) == v for k, v in values.items()):
                return
            raise PolicyError("settlement conflict; use explicit evidence reconciliation")
        self._append("settled", **values, metadata=metadata)

    @_exact_money
    def reserve_correction(self, run_id: str) -> None:
        """Reserve one complete correction/audit inside original call/time/money caps."""
        state = self._state()
        run = state["runs"].get(run_id)
        if not run or run["terminal"] or state["locked"] or run["correction_count"] or run["final_roles"] != {"synthesis", "audit"}:
            raise PolicyError("correction_not_available")
        attempts = list(state["attempts"].values())
        own = [item for item in attempts if item["run_id"] == run_id]
        if any(sum(item["step_id"] in {stage, stage + "_correction"} for item in own) >= 2 for stage in ("synthesis", "audit")):
            raise PolicyError("correction_logical_call_limit")
        protected = sum(2 - len(item["final_roles"]) for item in state["runs"].values() if item["batch_id"] == run["batch_id"] and not item["terminal"])
        if len(own) + 2 > self.policy.run_requests or sum(item["batch_id"] == run["batch_id"] for item in attempts) + protected + 2 > self.policy.batch_requests:
            raise PolicyError("correction_call_reserve")
        at = self._now()
        elapsed = (datetime.fromisoformat(at) - datetime.fromisoformat(run["at"])).total_seconds()
        if elapsed + max(190, 2 * self.policy.finish_role_seconds) + 25 >= self.policy.active_seconds:
            raise PolicyError("correction_time_reserve")
        self._check_money(state, run["batch_id"], run["owner_id"], run_id, 2 * money(self.policy.finish_role_usd), at)
        self._append("correction_reserved", run_id=run_id)

    def reconcile(self, attempt_id: str, adjustment_id: str, *, final_usd: str) -> None:
        identity(adjustment_id)
        state = self._state()
        if attempt_id not in state["attempts"]:
            raise PolicyError("unknown attempt")
        values = {"attempt_id": attempt_id, "adjustment_id": adjustment_id, "reported_usd": str(money(final_usd)), "trusted_final": True, "outcome": "unknown"}
        if adjustment_id in state["adjustments"]:
            if all(state["adjustments"][adjustment_id].get(k) == v for k, v in values.items()):
                return
            raise PolicyError("adjustment identity conflict")
        self._append("reconciled", **values)

    def book_conservative(self, attempt_id: str, amount_usd: str) -> None:
        if attempt_id not in self._state()["attempts"]:
            raise PolicyError("unknown attempt")
        self._append("booked", attempt_id=attempt_id, amount_usd=str(money(amount_usd)))

    def set_spending_lock(self, enabled: bool) -> None:
        if type(enabled) is not bool:
            raise PolicyError("invalid spending lock")
        self._append("spending_lock", enabled=enabled)

    def close_run(self, run_id: str, *, metadata: dict | None = None) -> None:
        run = self._state()["runs"].get(run_id)
        if not run:
            raise PolicyError("unknown run")
        if not run["terminal"]:
            self._append("run_closed", run_id=run_id, metadata=metadata)

    def summary(self) -> dict[str, Any]:
        state = self._state()
        at = self._now()
        return {
            "mode": "simulation_only", "simulated": True, "live_provider_calls": 0,
            "journal_events": len(self.journal.events), "authorized_simulated_attempts": len(state["attempts"]),
            "unsettled_attempt_ids": [a["attempt_id"] for a in state["attempts"].values() if not a["settlement"] or not a["settlement"]["trusted_final"]],
            "deployment_daily_exposure_usd": str(self._exposure(state, at, scope="deployment", period="daily")),
            "deployment_monthly_exposure_usd": str(self._exposure(state, at, scope="deployment", period="monthly")),
            "spending_locked": state["locked"], "phase_0_gate_passed": False,
        }


class OfflineRunner(JournalBudget):
    """The default runner never gains live access from credentials or a flag."""

    def __init__(self, *args, **kwargs):
        require_offline_environment()
        super().__init__(*args, **kwargs)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Offline simulated runner; no paid providers or measured evaluation results.")
    parser.add_argument("--journal-dir", type=Path, required=True, help="reuse this aggregate journal across all batches")
    parser.add_argument("--batch-id", required=True)
    parser.add_argument("--simulate", action="store_true", help="explicit simulation label (simulation is the only mode)")
    args = parser.parse_args(argv)
    try:
        require_offline_environment()
        identity(args.batch_id)
        with Journal(args.journal_dir) as journal:
            runner = OfflineRunner(journal)
            runner.open_batch(args.batch_id)
            run_id = "run-" + hashlib.sha256(args.batch_id.encode()).hexdigest()[:24]
            state = runner._state()
            already_exists = run_id in state["runs"]
            if not already_exists:
                runner.open_run(args.batch_id, run_id)
                adapter = SimulatedAdapter()
                for role in ("proposal-openai", "proposal-anthropic", "proposal-google", "synthesis", "audit"):
                    packet = prepare_packet({"role": "proposal" if role.startswith("proposal") else role, "allowed_ids": ["fictional-evidence-1"]})
                    attempt_id = f"{run_id}-{role}-1"
                    runner.authorize(run_id=run_id, step_id=role, attempt_id=attempt_id, bound_usd="0.10", finish_role=role if role in {"synthesis", "audit"} else None)
                    result = adapter.generate(packet)
                    runner.settle(attempt_id, reported_usd=str(result.reported_cost) if result.reported_cost is not None else None, trusted_final=result.reported_cost is not None, outcome="succeeded" if result.content else "failed")
                runner.close_run(run_id)
            print(json.dumps({**runner.summary(), "batch_id": args.batch_id, "run_id": run_id, "replayed_without_execution": already_exists}, sort_keys=True))
        return 0
    except (JournalError, PolicyError, ContractError, OSError):
        # Do not stringify external errors, paths or content in public diagnostics.
        print(json.dumps({"mode": "simulation_only", "error": "offline_runner_blocked", "message": "Journal, environment or policy validation failed; inspect local configuration without resetting history."}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
