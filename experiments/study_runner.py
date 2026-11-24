"""Opt-in, preregistered real-provider Phase 0B study. No implicit spending.

This command is separate from the credential-free simulator. It requires a
frozen typed manifest, inspected discovery/baseline records, an expiring explicit
approval, pinned provider configuration and a locked operator spending policy.
The same exclusive, fsynced budget journal is reused across every funded batch.
"""

from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import random
import re
import stat
import sys
import threading
import time
from copy import deepcopy
from types import SimpleNamespace
from typing import Literal, Self
from zoneinfo import ZoneInfo

# `experiments` is a repository CLI while the application uses a src layout.
_backend_source = str(Path(__file__).resolve().parents[1] / "backend" / "src")
if _backend_source not in sys.path:
    sys.path.insert(0, _backend_source)

from pydantic import AwareDatetime, Field, model_validator

from experiments.evaluation import STRATA, TrialOutput, TrialSpec, WORKFLOWS, build_blinded_packets, fingerprint, save_blinded_packets
from experiments.cases import substantive_fingerprint, reject_development_overlap
from experiments.journal import Journal, JournalError, canonical
from experiments.runner import JournalBudget, Policy, PolicyError, _exact_money, money
from round_robin.database.models import utcnow
from round_robin.providers.base import OUTPUT_TOKENS, PROVIDERS, ProviderFailure, ProviderRequest, ProviderResult
from round_robin.providers.http import HTTPProvider, request_contract_hash
from round_robin.orchestration.protocol import correction_required, policy_descriptor, recovery_fits, next_stage
from round_robin.research.calculator import lifecycle_total
from round_robin.schemas.briefs import DraftBrief
from round_robin.schemas.common import Contract, Hash, Id, Money, ShortText
from round_robin.schemas.provider_configuration import ProviderConfiguration


class StudyBlocked(RuntimeError):
    pass


class CheckedSnapshot(Contract):
    id: Id
    text: str = Field(min_length=1, max_length=2000)
    sha256: Hash
    source_title: ShortText
    locator: ShortText
    origin: Literal["fictional", "user_supplied", "retrieved"]
    checked_at: AwareDatetime
    checked_by: ShortText
    check_basis: ShortText

    @model_validator(mode="after")
    def exact_bytes(self) -> Self:
        if hashlib.sha256(self.text.encode()).hexdigest() != self.sha256:
            raise ValueError("Snapshot hash must bind the exact inspected passage")
        return self


class StudyCase(Contract):
    task_id: str = Field(pattern=r"^[A-Z][A-Z0-9_-]{2,32}$")
    version: int = Field(ge=1, strict=True)
    stratum: Literal["budget_lifecycle", "deadline_feasibility", "missing_stale_quote", "preference_status_quo", "misleading_shared_evidence"]
    brief: DraftBrief
    acceptable_outcomes: list[ShortText] = Field(min_length=1, max_length=20)
    critical_dependencies: list[ShortText] = Field(min_length=1, max_length=20)
    checked_snapshots: list[CheckedSnapshot] = Field(min_length=1, max_length=24)
    cluster_id: ShortText | None = None

    @model_validator(mode="after")
    def confirmed_scope(self) -> Self:
        if not 2 <= sum(option.role == "candidate" for option in self.brief.options) <= 4 or self.brief.horizon_months is None or not self.brief.hard_requirements:
            raise ValueError("Frozen study inputs need confirmed alternatives, horizon and hard requirements")
        references = {snapshot.id for snapshot in self.checked_snapshots}
        if len(references) != len(self.checked_snapshots) or any(component.source_ref not in references for option in self.brief.options for component in option.cost_components):
            raise ValueError("Every quote fact must resolve within its frozen task snapshots")
        return self


class FrozenStudyManifest(Contract):
    dataset_id: str = Field(min_length=3, max_length=128)
    version: int = Field(ge=1, strict=True)
    status: Literal["frozen_human_reviewed"]
    frozen_at: AwareDatetime
    frozen_by: ShortText
    label: ShortText
    tasks: list[StudyCase] = Field(min_length=16, max_length=16)
    repeated_task_ids: list[str] = Field(min_length=5, max_length=5)
    double_review_task_ids: list[str] = Field(min_length=8, max_length=16)
    primary_usefulness_required: Literal[13] = 13
    primary_denominator: Literal[16] = 16
    development_case_fingerprints: list[Hash] = Field(default_factory=list, max_length=1000)

    @model_validator(mode="after")
    def exact_design(self) -> Self:
        identities = {task.task_id for task in self.tasks}
        if len(identities) != 16 or Counter(task.stratum for task in self.tasks) != Counter(STRATA):
            raise ValueError("Freeze exactly sixteen distinct tasks in the preregistered strata")
        substantive = []
        for task in self.tasks:
            substantive.append(substantive_fingerprint(task.brief.model_dump(mode="json"),
                [{"id": snapshot.id, "text": snapshot.text, "sha256": snapshot.sha256, "source_title": snapshot.source_title, "locator": snapshot.locator, "origin": snapshot.origin} for snapshot in task.checked_snapshots]))
            if any(snapshot.checked_at > self.frozen_at for snapshot in task.checked_snapshots):
                raise ValueError("Snapshots must be inspected before the manifest is frozen")
        if len(set(substantive)) != 16:
            raise ValueError("Aliased or renamed copies cannot inflate sixteen independent study cases")
        reject_development_overlap(substantive, self.development_case_fingerprints)
        for values in (self.repeated_task_ids, self.double_review_task_ids):
            if len(set(values)) != len(values) or not set(values) <= identities:
                raise ValueError("Repeat/reviewer allocation must be unique and preregistered")
        return self


class EvidenceFile(Contract):
    path: Path
    sha256: Hash


class StudyApproval(Contract):
    schema_version: Literal["1.0"] = "1.0"
    approval_id: str = Field(pattern=r"^[a-zA-Z0-9_-]{3,96}$")
    authorized_by: ShortText
    authorized_at: AwareDatetime
    expires_at: AwareDatetime
    batch_id: str = Field(pattern=r"^[a-zA-Z0-9_-]{3,96}$")
    owner_id: str = Field(pattern=r"^[a-zA-Z0-9_-]{3,96}$")
    deployment_id: str = Field(pattern=r"^[a-zA-Z0-9_-]{3,96}$")
    journal_directory: Path
    spending_policy_file: Path
    manifest: EvidenceFile
    provider_configuration: EvidenceFile
    discovery_record: EvidenceFile
    baseline_selection_record: EvidenceFile
    reviewer_plan_record: EvidenceFile
    baseline_provider: Literal["openai", "anthropic", "google"]
    synthesizer: Literal["openai", "anthropic", "google"]
    auditor: Literal["openai", "anthropic", "google"]
    execution_order_seed: ShortText
    blinding_seed: ShortText
    batch_requests: int = Field(ge=1, le=1872, strict=True)
    batch_usd: Money
    trial_usd: Money
    finish_role_usd: Money
    owner_daily_usd: Money
    owner_monthly_usd: Money
    deployment_daily_usd: Money
    deployment_monthly_usd: Money
    timezone: str = "America/Phoenix"
    fixed_evidence_only: Literal[True]
    all_three_provider_recipients_approved: Literal[True]
    other_paid_execution_disabled: Literal[True]

    @model_validator(mode="after")
    @_exact_money
    def funding_bounds(self) -> Self:
        if self.synthesizer == self.auditor or money(self.batch_usd) <= 0 or money(self.batch_usd) > 156 or not 0 < money(self.trial_usd) <= 2 or money(self.finish_role_usd) * 2 > money(self.trial_usd):
            raise ValueError("Explicit study funding must fit the original trial/batch envelope and independent audit")
        if any(money(getattr(self, key)) <= 0 for key in ("owner_daily_usd", "owner_monthly_usd", "deployment_daily_usd", "deployment_monthly_usd")):
            raise ValueError("Explicit positive aggregate ceilings are required")
        if self.expires_at <= self.authorized_at:
            raise ValueError("Approval expiry must follow its timestamp")
        ZoneInfo(self.timezone)
        return self


def _read_private_json(path: Path, *, maximum=4 * 1024 * 1024):
    if path.is_symlink() or not path.is_file() or path.stat().st_size > maximum:
        raise StudyBlocked("missing_or_oversized_authorization_artifact")
    with path.open("rb") as stream:
        raw = stream.read(maximum + 1)
    if len(raw) > maximum:
        raise StudyBlocked("authorization_artifact_grew_beyond_bound")
    return json.loads(raw)


def _checked_file(record: EvidenceFile):
    path = record.path.expanduser()
    maximum = 4 * 1024 * 1024
    if path.is_symlink() or not path.is_file() or path.stat().st_size > maximum:
        raise StudyBlocked("missing_linked_or_oversized_preregistered_artifact")
    with path.open("rb") as stream:
        raw = stream.read(maximum + 1)
    if len(raw) > maximum or hashlib.sha256(raw).hexdigest() != record.sha256:
        raise StudyBlocked("preregistered_artifact_hash_mismatch")
    return json.loads(raw)


def private_write(path: Path, value) -> str:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    data = canonical(value) + b"\n"
    if path.is_symlink():
        raise StudyBlocked("study_artifact_symlink_rejected")
    if path.exists():
        if path.read_bytes() != data:
            raise StudyBlocked("immutable_study_artifact_conflict")
        return hashlib.sha256(data).hexdigest()
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    parent = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(parent)
    finally:
        os.close(parent)
    return hashlib.sha256(data).hexdigest()


@_exact_money
def load_study_approval(path: Path):
    approval = StudyApproval.model_validate(_read_private_json(path, maximum=128 * 1024))
    if approval.expires_at <= utcnow() or approval.authorized_at > utcnow():
        raise StudyBlocked("study_approval_expired_or_future")
    manifest = FrozenStudyManifest.model_validate(_checked_file(approval.manifest))
    provider = ProviderConfiguration.model_validate(_checked_file(approval.provider_configuration))
    if manifest.frozen_at > approval.authorized_at or provider.expires_at <= utcnow():
        raise StudyBlocked("manifest_not_frozen_before_approval_or_provider_config_expired")
    discovery = _checked_file(approval.discovery_record)
    if not isinstance(discovery, dict):
        raise StudyBlocked("five_user_discovery_and_comprehension_records_required")
    interviews = discovery.get("interviews", [])
    if not isinstance(interviews, list) or any(not isinstance(row, dict) or any(not isinstance(row.get(key), str) or not row[key].strip() for key in ("participant_id", "consent_reference", "recent_decision")) for row in interviews):
        raise StudyBlocked("five_user_discovery_and_comprehension_records_required")
    if discovery.get("status") != "completed_actual_participants" or discovery.get("fictional") is not False or len(interviews) != 5 or len({row.get("participant_id") for row in interviews}) != 5 or any(not row.get("participant_id") or not row.get("consent_reference") or not row.get("recent_decision") or row.get("target_audience") is not True for row in interviews) or sum(row.get("completed_unaided") is True and row.get("located_recommendation_or_no_choice") is True and row.get("located_caveat") is True and row.get("located_exact_support") is True and row.get("located_next_action") is True for row in interviews) < 4:
        raise StudyBlocked("five_user_discovery_and_comprehension_records_required")
    baseline = _checked_file(approval.baseline_selection_record)
    if not isinstance(baseline, dict):
        raise StudyBlocked("strong_baseline_development_selection_required")
    development = baseline.get("development_task_ids", [])
    hashes = baseline.get("evidence_output_hashes", [])
    if not isinstance(development, list) or any(not isinstance(value, str) or not value for value in development) or not isinstance(hashes, list) or any(not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{64}", value) for value in hashes):
        raise StudyBlocked("strong_baseline_development_selection_required")
    if baseline.get("status") != "selected_on_separate_development_cases" or baseline.get("selected_provider") != approval.baseline_provider or baseline.get("self_check_enabled") is not True or len(set(development)) < 3 or set(development) & {task.task_id for task in manifest.tasks} or not baseline.get("selection_reason") or len(baseline.get("evidence_output_hashes", [])) < 3 or any(not re.fullmatch(r"[a-f0-9]{64}", value) for value in baseline.get("evidence_output_hashes", [])):
        raise StudyBlocked("strong_baseline_development_selection_required")
    review = _checked_file(approval.reviewer_plan_record)
    if not isinstance(review, dict) or any(not isinstance(review.get(key, []), list) or any(not isinstance(value, str) or not value for value in review.get(key, [])) for key in ("reviewer_ids", "double_review_task_ids")):
        raise StudyBlocked("independent_human_review_plan_required")
    if review.get("status") != "preregistered" or len(set(review.get("reviewer_ids", []))) < 2 or set(review.get("double_review_task_ids", [])) != set(manifest.double_review_task_ids) or review.get("all_suspected_critical_defects_double_reviewed") is not True:
        raise StudyBlocked("independent_human_review_plan_required")
    # Whole-request input-byte bound includes all allowed peer packets and schema
    # overhead; a smaller declared finish reserve cannot authorize optional work.
    conservative_finish = max((Decimal("160000") * money(price.input_usd_per_million) + Decimal(price.billable_output_bound_tokens) * money(price.output_usd_per_million)) / Decimal(1_000_000) for price in provider.pricing.values())
    if money(approval.finish_role_usd) < conservative_finish:
        raise StudyBlocked("finish_reserve_below_conservative_configured_bound")
    return approval, manifest, provider


@contextmanager
def spending_policy(approval, *, exclusive=False):
    path = approval.spending_policy_file.expanduser().absolute()
    if path.is_symlink():
        raise StudyBlocked("spending_policy_symlink_rejected")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_path = path.with_suffix(path.suffix + ".lock")
    if lock_path.is_symlink():
        raise StudyBlocked("spending_policy_lock_symlink_rejected")
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0) | os.O_NONBLOCK, 0o600)
    if not stat.S_ISREG(os.fstat(descriptor).st_mode):
        os.close(descriptor)
        raise StudyBlocked("spending_policy_lock_requires_regular_file")
    os.fchmod(descriptor, 0o600)
    with os.fdopen(descriptor, "a+b") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        try:
            yield path
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def set_spending_lock(approval, enabled):
    if type(enabled) is not bool:
        raise StudyBlocked("spending_lock_requires_explicit_boolean")
    with spending_policy(approval, exclusive=True) as path:
        values = {"owner_id": approval.owner_id, "deployment_id": approval.deployment_id, "journal_directory": str(approval.journal_directory.expanduser().resolve()), "locked": bool(enabled), **{key: getattr(approval, key) for key in ("batch_usd", "owner_daily_usd", "owner_monthly_usd", "deployment_daily_usd", "deployment_monthly_usd")}, "batch_requests": approval.batch_requests}
        if path.exists():
            previous = _read_private_json(path, maximum=128 * 1024)
            if any(previous.get(key) != values[key] for key in ("owner_id", "deployment_id", "journal_directory")):
                raise StudyBlocked("spending_authority_identity_changed")
            for key in ("batch_usd", "owner_daily_usd", "owner_monthly_usd", "deployment_daily_usd", "deployment_monthly_usd"):
                values[key] = str(min(money(previous[key]), money(values[key])))
            values["batch_requests"] = min(previous["batch_requests"], values["batch_requests"])
        temporary = path.with_suffix(path.suffix + ".pending")
        if temporary.exists():
            raise StudyBlocked("interrupted_policy_update_requires_reconciliation")
        private_write(temporary, values)
        os.replace(temporary, path)
        parent = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(parent)
        finally:
            os.close(parent)


class StudyLedger(JournalBudget):
    def __init__(self, journal, approval, *, clock=None):
        self.approval, self.current_policy = approval, None
        policy = Policy(batch_requests=approval.batch_requests, batch_usd=approval.batch_usd, run_usd=approval.trial_usd, owner_daily_usd=approval.owner_daily_usd, owner_monthly_usd=approval.owner_monthly_usd, deployment_daily_usd=approval.deployment_daily_usd, deployment_monthly_usd=approval.deployment_monthly_usd, finish_role_usd=approval.finish_role_usd, finish_role_seconds=95)
        super().__init__(journal, policy, clock=clock)

    @contextmanager
    def gate(self):
        with spending_policy(self.approval) as path:
            policy = _read_private_json(path, maximum=128 * 1024)
            if policy.get("locked") is not False or policy.get("owner_id") != self.approval.owner_id or policy.get("deployment_id") != self.approval.deployment_id or policy.get("journal_directory") != str(self.journal.path):
                raise StudyBlocked("study_spending_locked_or_foreign_authority")
            if self.approval.expires_at <= utcnow():
                raise StudyBlocked("study_approval_expired")
            self.current_policy = policy
            try:
                yield
            finally:
                self.current_policy = None

    @_exact_money
    def _check_money(self, state, batch_id, owner_id, run_id, extra, at):
        super()._check_money(state, batch_id, owner_id, run_id, extra, at)
        if self.current_policy is None:
            raise StudyBlocked("spending_policy_lock_required")
        for key, scope, value, period in (("batch_usd", "batch_id", batch_id, None), ("owner_daily_usd", "owner_id", owner_id, "daily"), ("owner_monthly_usd", "owner_id", owner_id, "monthly"), ("deployment_daily_usd", "deployment", None, "daily"), ("deployment_monthly_usd", "deployment", None, "monthly")):
            if self._exposure(state, at, scope=scope, value=value, period=period) + extra > money(self.current_policy[key]):
                raise PolicyError("current_operator_currency_limit")

    def open_run(self, *args, **kwargs):
        with self.gate():
            state = self._state()
            used = sum(attempt["batch_id"] == self.approval.batch_id for attempt in state["attempts"].values())
            protected = sum(2 - len(run["final_roles"]) for run in state["runs"].values() if run["batch_id"] == self.approval.batch_id and not run["terminal"])
            if used + protected + 2 > self.current_policy["batch_requests"]:
                raise PolicyError("current_operator_request_limit")
            return super().open_run(*args, **kwargs)

    def authorize(self, *args, **kwargs):
        with self.gate():
            state = self._state()
            count = sum(attempt["batch_id"] == self.approval.batch_id for attempt in state["attempts"].values())
            protected = sum(2 - len(run["final_roles"]) for run in state["runs"].values() if run["batch_id"] == self.approval.batch_id and not run["terminal"])
            run = state["runs"].get(kwargs.get("run_id"))
            if run and kwargs.get("finish_role") in {"synthesis", "audit"} - run["final_roles"]:
                protected -= 1
            if count + 1 + protected > self.current_policy["batch_requests"]:
                raise PolicyError("current_operator_request_limit")
            return super().authorize(*args, **kwargs)

    def reserve_correction(self, run_id):
        with self.gate():
            state = self._state()
            run = state["runs"][run_id]
            protected = sum(2 - len(item["final_roles"]) for item in state["runs"].values() if item["batch_id"] == run["batch_id"] and not item["terminal"])
            used = sum(item["batch_id"] == run["batch_id"] for item in state["attempts"].values())
            if used + protected + 2 > self.current_policy["batch_requests"]:
                raise PolicyError("current_operator_request_limit")
            return super().reserve_correction(run_id)


def trial_plan(manifest, seed):
    digest = fingerprint(manifest.model_dump(mode="json"))
    primary, repeats = [], []
    for workflow in WORKFLOWS:
        for case in manifest.tasks:
            for index in range(3 if case.task_id in manifest.repeated_task_ids else 1):
                trial = TrialSpec(f"{case.task_id}.v{case.version}.{workflow}.r{index}", case.task_id, case.version, workflow, index, index == 0, digest)
                (primary if trial.primary else repeats).append(trial)
    rng = random.Random(seed)
    rng.shuffle(primary)
    rng.shuffle(repeats)
    return primary + repeats


class FundedStudyRunner:
    def __init__(self, journal, approval, manifest, provider_configuration, *, provider_registry=None, sleeper=time.sleep):
        if journal.mode != "explicit_live_study" or journal.path != approval.journal_directory.expanduser().resolve():
            raise StudyBlocked("study_journal_profile_or_authority_path_mismatch")
        self.journal, self.approval, self.manifest = journal, approval, manifest
        self.configuration = {**provider_configuration.model_dump(mode="json"), "mode": "live", "request_contract_hash": request_contract_hash(), "discussion_policy": policy_descriptor(), "study_workflow_policies": {arm: policy_descriptor(arm) for arm in WORKFLOWS}, "study_protocol_version": "fixed-evidence-claim-correction-v2"}
        self.manifest_hash = fingerprint(manifest.model_dump(mode="json"))
        self.configuration_hash = fingerprint(self.configuration)
        self.plan = trial_plan(manifest, approval.execution_order_seed)
        self._preregistered_trials = {trial.trial_id: trial for trial in self.plan}
        self.cases = {case.task_id: case for case in manifest.tasks}
        self.artifacts = journal.path / "studies" / self.manifest_hash
        self.mutex = threading.RLock()
        self._discussion_runs = {}
        self.sleeper = sleeper
        self.ledger = StudyLedger(journal, approval)
        self.ledger.open_batch(approval.batch_id, metadata={"approval_hash": fingerprint(approval.model_dump(mode="json")), "manifest_hash": self.manifest_hash, "configuration_hash": self.configuration_hash, "trial_plan_hash": fingerprint([asdict(trial) for trial in self.plan]), "owner_id": approval.owner_id, "deployment_id": approval.deployment_id})
        self.providers = provider_registry or {provider: HTTPProvider(provider, self.configuration) for provider in PROVIDERS}
        if provider_registry is None and any(not self.providers[provider].api_key for provider in PROVIDERS):
            raise StudyBlocked("configured_provider_credentials_required_before_admission")
        private_write(self.artifacts / "frozen-manifest.json", manifest.model_dump(mode="json"))
        private_write(self.artifacts / "provider-configuration.json", self.configuration)
        private_write(self.artifacts / "preregistered-trial-plan.json", [asdict(trial) for trial in self.plan])

    def _authority(self):
        if self.approval.expires_at <= utcnow():
            raise StudyBlocked("study_approval_expired")
        manifest = FrozenStudyManifest.model_validate(_checked_file(self.approval.manifest))
        if fingerprint(manifest.model_dump(mode="json")) != self.manifest_hash or fingerprint(self.manifest.model_dump(mode="json")) != self.manifest_hash:
            raise StudyBlocked("frozen_study_manifest_changed")
        current = ProviderConfiguration.model_validate(_checked_file(self.approval.provider_configuration))
        if current.expires_at <= utcnow() or fingerprint({**current.model_dump(mode="json"), "mode": "live", "request_contract_hash": request_contract_hash(), "discussion_policy": policy_descriptor(), "study_workflow_policies": {arm: policy_descriptor(arm) for arm in WORKFLOWS}, "study_protocol_version": "fixed-evidence-claim-correction-v2"}) != self.configuration_hash or fingerprint(self.configuration) != self.configuration_hash:
            raise StudyBlocked("frozen_provider_configuration_changed_or_expired")

    def _run_id(self, trial):
        # Batch IDs cannot create fresh copies of a previously admitted primary.
        return "trial_" + hashlib.sha256(f"{self.manifest_hash}:{trial.trial_id}".encode()).hexdigest()[:32]

    def _trial_directory(self, trial):
        return self.artifacts / self._run_id(trial)

    def _elapsed(self, run_id):
        run = self.ledger._state()["runs"][run_id]
        elapsed = (utcnow() - datetime.fromisoformat(run["at"])).total_seconds()
        if elapsed < 0:
            raise StudyBlocked("study_clock_regression")
        return elapsed

    def _verify_retained_artifacts(self, state, run_id):
        for attempt in state["attempts"].values():
            if attempt["run_id"] != run_id:
                continue
            links = [attempt.get("metadata") or {}]
            if attempt.get("settlement"):
                links.append(attempt["settlement"].get("metadata") or {})
            for metadata in links:
                for prefix in ("request", "response"):
                    relative = metadata.get(f"{prefix}_artifact")
                    if relative is None:
                        continue
                    path = self.journal.path / relative
                    if Path(relative).is_absolute() or path.is_symlink() or not path.resolve().is_relative_to(self.journal.path) or not path.is_file() or path.stat().st_size > 4 * 1024 * 1024 or hashlib.sha256(path.read_bytes()).hexdigest() != metadata.get(f"{prefix}_sha256"):
                        raise StudyBlocked("retained_provider_artifact_missing_or_corrupt")

    def _call(self, trial, step_id, stage, provider_name, peers, *, finish_role=None, fallbacks=(), discussion_snapshot=None):
        case, run_id = self.cases[trial.task_id], self._run_id(trial)
        with self.mutex:
            if run_id not in self._discussion_runs:
                persisted = self.ledger._state()["runs"][run_id]
                self._discussion_runs[run_id] = SimpleNamespace(id=run_id, owner_id=self.approval.owner_id,
                    input_snapshot=case.brief.model_dump(mode="json"),
                    configuration={"discussion_issues": deepcopy(persisted.get("discussion_issues", []))})
        snapshots = [snapshot.model_dump(mode="json") for snapshot in case.checked_snapshots]
        from round_robin.orchestration.reporting import comparison_data
        cost_as_of = self.manifest.frozen_at.astimezone(ZoneInfo(case.brief.timezone)).date()
        _, calculations = comparison_data(case.brief, as_of=cost_as_of)
        allowed = {snapshot.id for snapshot in case.checked_snapshots} | {calculation["id"] for calculation in calculations} | {peer["id"] for peer in peers}
        claims = [claim for peer in peers for claim in peer["output"].get("claims", []) if claim.get("id")]
        allowed.update(claim["id"] for claim in claims)
        attempts, failures = [], []
        repair_diagnostics = ()
        current_provider = provider_name
        for ordinal in range(2):
            attempt_id = "attempt_" + hashlib.sha256(f"{run_id}:{step_id}:{ordinal}".encode()).hexdigest()[:40]
            request = None
            try:
                with self.mutex:
                    self._authority()
                    state = self.ledger._state()
                    if stage in {"synthesis", "audit"} and sum(item["run_id"] == run_id and item["step_id"] in {stage, stage + "_correction"} for item in state["attempts"].values()) >= 2:
                        raise PolicyError("logical_finish_call_limit")
                    role = finish_role if finish_role not in state["runs"][run_id]["final_roles"] else None
                    remaining_roles = 2 - len(state["runs"][run_id]["final_roles"]) - int(role is not None)
                    timeout = min(90, math.floor(600 - self._elapsed(run_id) - remaining_roles * 95 - 25))
                    if timeout <= 0:
                        raise PolicyError("protected_finish_time_exhausted")
                    scoped_peers = [{"id": peer["id"], "provider": peer["provider"], "stage": peer["stage"], "output": peer["output"]} for peer in peers]
                    from round_robin.research.discussion import current_assessments, discussion_issues
                    current_run = self._discussion_runs[run_id]
                    context_run = SimpleNamespace(id=current_run.id, owner_id=current_run.owner_id, input_snapshot=current_run.input_snapshot,
                        configuration=deepcopy(discussion_snapshot if discussion_snapshot is not None else current_run.configuration))
                    issues = [] if stage == "proposal" else [item for item in discussion_issues(context_run) if item["severity"] in {"material", "critical"} and item["status"] in {"open", "answered_pending_review"}]
                    assessments = current_assessments(context_run, {snapshot["id"]: snapshot for snapshot in snapshots}, now=self.manifest.frozen_at)
                    request = ProviderRequest(run_id, step_id, stage, step_id, case.brief.model_dump(mode="json"), snapshots, scoped_peers, frozenset(allowed), OUTPUT_TOKENS[stage], timeout, bool(repair_diagnostics), tuple(calculations), tuple(claims), cost_as_of=cost_as_of.isoformat(),
                        repair_diagnostics=repair_diagnostics, open_objections=tuple(issues), assessments=tuple(assessments))
                    packet = request.packet()
                    adapter = self.providers[current_provider]
                    bound = adapter.conservative_bound(request)
                    request_path = self._trial_directory(trial) / f"{attempt_id}.request.json"
                    packet_hash = private_write(request_path, {"trial_id": trial.trial_id, "input_hash": fingerprint(case.model_dump(mode="json")), "configuration_hash": self.configuration_hash, "provider": current_provider, "packet": packet})
                    self.ledger.authorize(run_id=run_id, step_id=step_id, attempt_id=attempt_id, bound_usd=str(bound), timeout_seconds=timeout, finish_role=role, metadata={"request_artifact": str(request_path.relative_to(self.journal.path)), "request_sha256": packet_hash, "provider": current_provider, "configuration_hash": self.configuration_hash})
                    attempts.append(attempt_id)
                # Every send follows a confirmed durable journal authorization.
                result = adapter.generate(request)
                record = {"status": "succeeded", "provider": result.actual_provider, "model": result.actual_model, "provider_request_id": result.provider_request_id, "output": result.output.model_dump(mode="json"), "raw_text": result.raw_text or result.output.model_dump_json(), "usage": result.usage, "cost_usd": str(result.cost_usd) if result.cost_usd is not None else None, "active_seconds": self._elapsed(run_id), "simulated": result.simulated}
                if result.simulated:
                    raise StudyBlocked("a_simulated_adapter_cannot_supply_live_study_evidence")
                with self.mutex:
                    path = self._trial_directory(trial) / f"{attempt_id}.response.json"
                    digest = private_write(path, record)
                    self.ledger.settle(attempt_id, reported_usd=str(result.cost_usd) if result.cost_usd is not None else None, trusted_final=result.cost_usd is not None, outcome="succeeded", metadata={"response_artifact": str(path.relative_to(self.journal.path)), "response_sha256": digest, "provider_request_id": result.provider_request_id})
                from round_robin.research.claims import assign_claim_ids
                claim_index = iter(range(1, 21))
                scoped_output, _ = assign_claim_ids(record["output"], allowed_reference_ids=allowed,
                    id_factory=lambda: "claim_" + hashlib.sha256(f"{run_id}:{step_id}:{next(claim_index)}".encode()).hexdigest()[:40])
                from round_robin.research.discussion import reduce_discussion
                with self.mutex:
                    scoped_output, issue_values, issue_events = reduce_discussion(self._discussion_runs[run_id],
                        SimpleNamespace(id=step_id, run_id=run_id, provider=result.actual_provider, stage=stage), scoped_output, allowed)
                    issue_path = self._trial_directory(trial) / f"{attempt_id}.discussion.json"
                    private_write(issue_path, {"issues": issue_values, "events": issue_events})
                    self.ledger._append("discussion_recorded", run_id=run_id, issues=issue_values)
                    self._discussion_runs[run_id].configuration = {**self._discussion_runs[run_id].configuration,
                        "discussion_policy_version": "scoped_obligations_v1", "discussion_issues": issue_values}
                return {"id": step_id, "provider": result.actual_provider, "stage": stage, "output": scoped_output, "attempt_ids": attempts, "active_seconds": record["active_seconds"], "failures": failures}
            except ProviderFailure as failure:
                repair_diagnostics = tuple(getattr(failure, "diagnostics", ())) if failure.code in {"schema_invalid", "invalid_reference", "malformed_response"} else ()
                with self.mutex:
                    failure_record = {"status": "refused" if failure.code == "refusal" else "failed", "error_code": failure.code, "provider": current_provider, "provider_request_id": failure.provider_request_id, "raw_text": failure.raw_text, "may_have_sent": failure.may_have_sent, "active_seconds": self._elapsed(run_id)}
                    path = self._trial_directory(trial) / f"{attempt_id}.response.json"
                    digest = private_write(path, failure_record)
                    authorized = attempt_id in self.ledger._state()["attempts"]
                    if authorized:
                        known = failure.cost_usd is not None or not failure.may_have_sent
                        self.ledger.settle(attempt_id, reported_usd=str(failure.cost_usd) if failure.cost_usd is not None else "0" if not failure.may_have_sent else None, trusted_final=known, outcome="refused" if failure.code == "refusal" else "unknown" if failure.may_have_sent else "failed", metadata={"response_artifact": str(path.relative_to(self.journal.path)), "response_sha256": digest, "provider_request_id": failure.provider_request_id})
                    failures.append({"error_code": failure.code, "response_artifact": str(path.relative_to(self.journal.path)), "response_sha256": digest, "may_have_sent": failure.may_have_sent, "provider_request_id": failure.provider_request_id})
                if ordinal or failure.code == "refusal":
                    break
                unavailable = failure.code in {"authentication", "credit_exhausted", "model_unavailable", "provider_configuration_invalid"}
                replacement = next((provider for provider in fallbacks if provider != current_provider), None) if unavailable else None
                if replacement:
                    current_provider = replacement
                elif not failure.retryable:
                    break
                delay = failure.retry_after if failure.retry_after is not None else 2.5
                if not math.isfinite(delay) or delay < 0:
                    delay = 2.5
                if not recovery_fits(stage=stage, remaining_seconds=600-self._elapsed(run_id), wait_seconds=delay):
                    break
                self.sleeper(delay)
            except (PolicyError, StudyBlocked, ValueError) as failure:
                return {"id": step_id, "provider": current_provider, "stage": stage, "output": None, "attempt_ids": attempts, "failures": [{"error_code": type(failure).__name__}], "active_seconds": self._elapsed(run_id)}
        return {"id": step_id, "provider": current_provider, "stage": stage, "output": None, "attempt_ids": attempts, "failures": failures, "active_seconds": self._elapsed(run_id)}

    def _parallel(self, trial, stage, providers, peers):
        with self.mutex:
            snapshot = deepcopy(self._discussion_runs[self._run_id(trial)].configuration)
        with ThreadPoolExecutor(max_workers=3) as pool:
            futures = [pool.submit(self._call, trial, f"{stage}_{provider}", stage, provider, peers, discussion_snapshot=snapshot) for provider in providers]
            return [future.result() for future in futures]

    @_exact_money
    def execute_trial(self, trial):
        if self._preregistered_trials.get(trial.trial_id) != trial:
            raise StudyBlocked("trial_not_in_preregistered_plan")
        run_id = self._run_id(trial)
        output_path = self._trial_directory(trial) / "outcome.json"
        with self.mutex:
            state = self.ledger._state()
            existing = state["runs"].get(run_id)
            if output_path.exists():
                if not existing or not existing["terminal"]:
                    raise StudyBlocked("uncommitted_cached_outcome_requires_reconciliation")
                terminal = next((event for event in reversed(self.journal.events) if event["type"] == "run_closed" and event["run_id"] == run_id), None)
                if terminal is None or hashlib.sha256(output_path.read_bytes()).hexdigest() != (terminal.get("metadata") or {}).get("outcome_sha256"):
                    raise StudyBlocked("immutable_original_outcome_hash_mismatch")
                self._verify_retained_artifacts(state, run_id)
                outcome = _read_private_json(output_path)
                if outcome.get("trial_id") != trial.trial_id or outcome.get("configuration_hash") != self.configuration_hash or outcome.get("input_hash") != fingerprint(self.cases[trial.task_id].model_dump(mode="json")):
                    raise StudyBlocked("cached_trial_binding_mismatch")
                return outcome
            if existing and existing["terminal"]:
                raise StudyBlocked("closed_trial_output_missing_never_repeat")
            if not existing:
                self._authority()
                self.ledger.open_run(self.approval.batch_id, run_id, self.approval.owner_id)
        interrupted = existing is not None
        self._discussion_runs[run_id] = SimpleNamespace(id=run_id, owner_id=self.approval.owner_id,
            input_snapshot=self.cases[trial.task_id].brief.model_dump(mode="json"), configuration={"discussion_issues": deepcopy(existing.get("discussion_issues", [])) if existing else []})
        records = []
        if not interrupted:
            if trial.workflow == "single_provider":
                records.append(self._call(trial, "proposal_baseline", "proposal", self.approval.baseline_provider, []))
            else:
                records.extend(self._parallel(trial, "proposal", PROVIDERS, []))
            proposals = [record for record in records if record["stage"] == "proposal" and record["output"]]
            following = next_stage("proposals", [record["provider"] for record in proposals], profile=trial.workflow)
            enough = following != "publication"
            peers = proposals
            if following == "critique":
                critiques = self._parallel(trial, "critique", [record["provider"] for record in proposals], proposals)
                records.extend(critiques)
                evidence_review = self._call(trial, "evidence_review", "evidence_review", self.approval.synthesizer, [*proposals, *[record for record in critiques if record["output"]]])
                records.append(evidence_review)
                criticism = any(item["severity"] in {"material", "critical"} and item["status"] in {"open", "answered_pending_review"}
                                for item in self._discussion_runs[run_id].configuration.get("discussion_issues", []))
                if next_stage("evidence_review", profile=trial.workflow, material_findings=criticism) == "revision":
                    revision_peers = [record for record in [*proposals, *critiques, evidence_review] if record["output"]]
                    revisions = self._parallel(trial, "revision", [record["provider"] for record in proposals], revision_peers)
                    records.extend(revisions)
                    peers = [*proposals, *[record for record in critiques + [evidence_review] + revisions if record["output"]]]
                else:
                    peers = [record for record in [*proposals, *critiques, evidence_review] if record["output"]]
            if enough:
                synthesizer = self.approval.baseline_provider if trial.workflow == "single_provider" else self.approval.synthesizer
                synthesis = self._call(trial, "synthesis", "synthesis", synthesizer, peers, finish_role="synthesis", fallbacks=() if trial.workflow == "single_provider" else self.configuration["fallbacks"]["synthesis"])
                records.append(synthesis)
                if next_stage("synthesis", [synthesis["provider"]] if synthesis["output"] else [], profile=trial.workflow) == "audit":
                    auditor = self.approval.baseline_provider if trial.workflow == "single_provider" else next((provider for provider in [self.approval.auditor, *self.configuration["fallbacks"]["audit"]] if provider != synthesis["provider"]), None)
                    if auditor is None:
                        audit = {"id": "audit", "provider": None, "stage": "audit", "output": None, "attempt_ids": [], "failures": [{"error_code": "independent_auditor_unavailable"}], "active_seconds": self._elapsed(run_id)}
                    else:
                        audit = self._call(trial, "audit", "audit", auditor, [*peers, synthesis], finish_role="audit", fallbacks=() if trial.workflow == "single_provider" else [provider for provider in self.configuration["fallbacks"]["audit"] if provider != synthesis["provider"]])
                    records.append(audit)
                    if audit["output"] and correction_required(audit_passed=audit["output"].get("audit_passed"),
                            critical_defects=audit["output"].get("critical_defects", []), iteration=0,
                            open_objections=self._discussion_runs[run_id].configuration.get("discussion_issues", [])):
                        try:
                            with self.mutex:
                                self._authority()
                                self.ledger.reserve_correction(run_id)
                        except (PolicyError, StudyBlocked):
                            # The first failed result is retained if there is no
                            # room for the complete correction and second audit.
                            pass
                        else:
                            correction_peers = [record for record in [*peers, synthesis, audit] if record["output"]]
                            corrected = self._call(trial, "synthesis_correction", "synthesis", synthesizer, correction_peers,
                                finish_role="synthesis", fallbacks=() if trial.workflow == "single_provider" else self.configuration["fallbacks"]["synthesis"])
                            records.append(corrected)
                            if corrected["output"]:
                                next_auditor = self.approval.baseline_provider if trial.workflow == "single_provider" else next((provider for provider in [self.approval.auditor, *self.configuration["fallbacks"]["audit"]] if provider != corrected["provider"]), None)
                                if next_auditor:
                                    records.append(self._call(trial, "audit_correction", "audit", next_auditor,
                                        [*correction_peers, corrected], finish_role="audit",
                                        fallbacks=() if trial.workflow == "single_provider" else [provider for provider in self.configuration["fallbacks"]["audit"] if provider != corrected["provider"]]))
        successful = [record for record in records if record["output"]]
        synthesis = next((record for record in reversed(successful) if record["stage"] == "synthesis"), None)
        audit = next((record for record in reversed(successful) if record["stage"] == "audit"), None)
        missing = [record["id"] for record in records if record["output"] is None]
        if interrupted:
            missing.append("interrupted_execution_not_replayed")
        discussion_ledger = self._discussion_runs[run_id].configuration.get("discussion_issues", [])
        open_issues = [item for item in discussion_ledger if item["severity"] in {"material", "critical"} and item["status"] in {"open", "answered_pending_review"}]
        status = "completed" if synthesis and audit and audit["output"]["audit_passed"] is True and not audit["output"]["critical_defects"] and not missing and not open_issues else "limited" if successful else "failed"
        from round_robin.orchestration.reporting import comparison_data
        comparisons, calculations = comparison_data(self.cases[trial.task_id].brief, as_of=self.manifest.frozen_at.astimezone(ZoneInfo(self.cases[trial.task_id].brief.timezone)).date())
        public = {"recommendation": synthesis["output"] if synthesis else None, "comparison": comparisons, "calculations": calculations, "audit_findings": {"audit_passed": audit["output"]["audit_passed"], "summary": audit["output"]["summary"], "critical_defects": audit["output"]["critical_defects"], "dissent": audit["output"]["dissent"]} if audit else None, "limitations": ["The frozen fixed-evidence workflow does not independently establish every supplied real-world fact.", *(["One or more mandatory review stages are unavailable; inspect the retained result's limitations."] if missing else []), *(["The final audit did not pass; the recommendation requires review of the saved audit findings."] if audit and (audit["output"]["audit_passed"] is not True or audit["output"]["critical_defects"]) else [])], "next_action": "Inspect the decisive caveat, cited supporting passage and remaining hard requirements before choosing."}
        reference_map = {record["id"]: "ref_" + hashlib.sha256(f"{self.manifest_hash}:{trial.trial_id}:{record['id']}".encode()).hexdigest()[:24] for record in successful}
        def peer_references(value):
            if isinstance(value, dict):
                return set().union(*(peer_references(item) for item in value.values())) if value else set()
            if isinstance(value, list):
                return set().union(*(peer_references(item) for item in value)) if value else set()
            if isinstance(value, str):
                return {original for original in reference_map if re.search(r"(?<![A-Za-z0-9])" + re.escape(original) + r"(?![A-Za-z0-9])", value)}
            return set()
        cited_ids = peer_references(public)
        while True:
            expanded = cited_ids | set().union(*(peer_references(record["output"]) for record in successful if record["id"] in cited_ids))
            if expanded == cited_ids:
                break
            cited_ids = expanded
        cited = [record for record in successful if record["id"] in cited_ids]
        if cited:
            public["anonymous_contribution_sources"] = [{"id": reference_map[record["id"]], "scope": "Anonymous adviser assertion; its linked source passage still requires support checking.", "content": record["output"]} for record in cited]
        def opaque_references(value):
            if isinstance(value, dict):
                return {key: opaque_references(item) for key, item in value.items()}
            if isinstance(value, list):
                return [opaque_references(item) for item in value]
            if isinstance(value, str):
                for original, opaque in sorted(reference_map.items(), key=lambda item: len(item[0]), reverse=True):
                    value = re.sub(r"(?<![A-Za-z0-9])" + re.escape(original) + r"(?![A-Za-z0-9])", opaque, value)
                return value
            return value
        public = opaque_references(public)
        public["unresolved_material_obligations"] = [{"concern": item["concern"], "severity": item["severity"], "status": item["status"]} for item in open_issues]
        with self.mutex:
            state = self.ledger._state()
            attempts = [attempt for attempt in state["attempts"].values() if attempt["run_id"] == run_id]
            unknown = [attempt["attempt_id"] for attempt in attempts if not attempt["settlement"] or not attempt["settlement"]["trusted_final"]]
            outcome = {**asdict(trial), "run_id": run_id, "status": status, "admitted": True, "configuration_hash": self.configuration_hash, "input_hash": fingerprint(self.cases[trial.task_id].model_dump(mode="json")), "stage_records": records, "public_reference_map": reference_map, "attempt_ids": [attempt["attempt_id"] for attempt in attempts], "request_count": len(attempts), "unknown_attempt_ids": unknown, "active_seconds": self._elapsed(run_id), "first_useful_active_seconds": min((record["active_seconds"] for record in successful if record["stage"] == "proposal" and any(constraint["basis_refs"] for constraint in record["output"]["constraints"])), default=None), "mandatory_work_complete": status == "completed", "public_text": json.dumps(public, ensure_ascii=False), "simulated": False, "phase_passed": False}
            reported = sum((money(attempt["settlement"]["reported_usd"]) for attempt in attempts if attempt.get("settlement") and attempt["settlement"].get("reported_usd") is not None), Decimal(0))
            conservative = self.ledger._exposure(state, self.ledger._now(), scope="run_id", value=run_id)
            unused_holds = sum(state["runs"][run_id]["holds"].values(), Decimal(0))
            outcome.update(discussion_policy=self.configuration["study_workflow_policies"][trial.workflow],
                discussion_issues=discussion_ledger, open_material_objection_ids=[item["id"] for item in open_issues],
                reported_usd=str(reported), conservative_actual_exposure_usd=str(conservative-unused_holds),
                unknown_exposure_usd=str(max(Decimal(0), conservative-unused_holds-reported)))
            digest = private_write(output_path, outcome)
            self.ledger.close_run(run_id, metadata={"outcome_artifact": str(output_path.relative_to(self.journal.path)), "outcome_sha256": digest})
        return outcome

    def run(self):
        outputs, blocked = [], None
        for trial in self.plan:
            try:
                outputs.append(self.execute_trial(trial))
            except (PolicyError, StudyBlocked) as error:
                blocked = type(error).__name__
                break
        trial_outputs = [TrialOutput(row["trial_id"], "output_" + hashlib.sha256(row["trial_id"].encode()).hexdigest()[:32], row["public_text"], row["status"], row["configuration_hash"]) for row in outputs]
        if trial_outputs:
            packets, mapping = build_blinded_packets(self.manifest.model_dump(mode="json"), trial_outputs, seed=self.approval.blinding_seed)
            mapping["private_contribution_reference_maps"] = {row["trial_id"]: row["public_reference_map"] for row in outputs}
            mapping["approved_reviewer_ids"] = _checked_file(self.approval.reviewer_plan_record)["reviewer_ids"]
            folder = self.artifacts / ("review-bundle_" + fingerprint([asdict(output) for output in trial_outputs])[:24])
            if not folder.exists():
                save_blinded_packets(folder, packets, mapping)
            elif not (folder / "reviewer_packets.json").is_file() or not (folder / "private_identity_mapping.json").is_file() or json.loads((folder / "reviewer_packets.json").read_text()) != packets or json.loads((folder / "private_identity_mapping.json").read_text()) != mapping:
                raise StudyBlocked("review_bundle_incomplete_or_changed")
            private_write(folder / "private_trial_outputs.json", [asdict(output) for output in trial_outputs])
            measurement_fields = ("trial_id", "workflow", "dataset_fingerprint", "configuration_hash", "status", "admitted", "mandatory_work_complete", "active_seconds", "first_useful_active_seconds", "reported_usd", "unknown_exposure_usd", "conservative_actual_exposure_usd")
            private_write(folder / "private_trial_measurements.json", [{key: row.get(key) for key in measurement_fields} for row in outputs])
        state = self.ledger._state()
        attempts = [attempt for attempt in state["attempts"].values() if attempt["batch_id"] == self.approval.batch_id]
        summary = {"mode": "explicit_live_study", "simulated": False, "phase_passed": False, "manifest_hash": self.manifest_hash, "planned_trials": 78, "primary_tasks_per_workflow": 16, "repeat_denominator_contribution": 0, "retained_outputs": len(outputs), "not_executed_trial_ids": [trial.trial_id for trial in self.plan if trial.trial_id not in {row["trial_id"] for row in outputs}], "authorized_requests_in_batch": len(attempts), "smoke_requests": 0, "unknown_attempt_ids": [attempt["attempt_id"] for attempt in attempts if not attempt["settlement"] or not attempt["settlement"]["trusted_final"]], "deployment_daily_exposure_usd": str(self.ledger._exposure(state, self.ledger._now(), scope="deployment", period="daily")), "deployment_monthly_exposure_usd": str(self.ledger._exposure(state, self.ledger._now(), scope="deployment", period="monthly")), "blocked": blocked, "pending_gates": ["blinded human grades and adjudication", "user-valued benefit over strong baseline", "explicit protocol decision", "held-out release confirmation"]}
        private_write(self.artifacts / ("invocation_" + hashlib.sha256(canonical(summary)).hexdigest()[:24] + ".json"), summary)
        return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description="Explicitly approved funded Phase 0B study; never run in routine CI.")
    parser.add_argument("--approval-file", type=Path)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--set-spending-lock", choices=["locked", "unlocked"])
    parser.add_argument("--schemas", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.schemas:
            private_write(args.schemas, {"approval": StudyApproval.model_json_schema(), "manifest": FrozenStudyManifest.model_json_schema(), "provider_configuration": ProviderConfiguration.model_json_schema()})
            print(json.dumps({"schemas_saved": True, "provider_calls": 0}))
            return 0
        if args.approval_file is None:
            raise StudyBlocked("explicit_approval_file_required")
        if args.set_spending_lock == "locked":
            approval = StudyApproval.model_validate(_read_private_json(args.approval_file, maximum=128 * 1024))
            set_spending_lock(approval, True)
            print(json.dumps({"spending_locked": True, "provider_calls": 0}))
            return 0
        approval, manifest, configuration = load_study_approval(args.approval_file)
        if args.set_spending_lock:
            set_spending_lock(approval, args.set_spending_lock == "locked")
            print(json.dumps({"spending_locked": args.set_spending_lock == "locked", "provider_calls": 0}))
            return 0
        if not args.live:
            raise StudyBlocked("explicit_live_flag_required")
        with Journal(approval.journal_directory, mode="explicit_live_study") as journal:
            summary = FundedStudyRunner(journal, approval, manifest, configuration).run()
            print(json.dumps(summary, sort_keys=True))
        return 2 if summary["blocked"] else 0
    except (StudyBlocked, PolicyError, JournalError, OSError, ValueError, KeyError):
        # Human records, credentials, request bodies and paths never enter CLI errors.
        print(json.dumps({"error": "study_not_authorized_or_reconciliation_required", "provider_calls_implicitly_authorized": 0, "phase_passed": False}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
