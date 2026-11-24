"""Immutable edit feedback and server-owned regression completeness.

Receiving pixels, recording every recheck, and resolving defects are separate
facts. Missing model rows remain pending rather than erasing a prior issue.
"""
from copy import deepcopy
import hashlib
import json

from .schemas import Finding, VisualBrief


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def regression_targets(brief: dict, findings: list[dict]) -> list[dict]:
    if len(findings) > 40 or len({row["id"] for row in findings}) != len(findings):
        raise ValueError("Prior findings must be bounded and uniquely identified")
    targets = []
    for value in findings:
        finding = Finding.model_validate(value)
        if finding.severity in {"material", "critical"} and not (finding.previous_finding_id and finding.disposition == "addressed"):
            targets.append({"id": finding.id, "kind": "prior_finding", "description": finding.description, "severity": finding.severity})
    for index, requirement in enumerate(VisualBrief.model_validate(brief).locked_requirements):
        targets.append({"id": "requirement_" + digest([index, requirement])[:32], "kind": "locked_requirement", "description": requirement, "severity": "critical"})
    return targets


def build_edit_bundle(*, brief, parent_version_id, parent_hash, findings, dispositions, intent):
    VisualBrief.model_validate(brief)
    if not isinstance(intent, str) or not intent.strip() or len(intent) > 2000:
        raise ValueError("An edit requires a concrete bounded intent")
    if len(findings) > 40 or len({row["id"] for row in findings}) != len(findings):
        raise ValueError("Prior findings must be bounded and unique")
    known = {row["id"]: Finding.model_validate(row).model_dump(mode="json") for row in findings}
    if len(dispositions) > 20 or not set(dispositions) <= known.keys() or any(value not in {"address", "decline"} for value in dispositions.values()):
        raise ValueError("Every edit decision must resolve to an exact parent finding")
    bundle = {"schema_version": "visual-edit-1", "parent_version_id": parent_version_id,
              "parent_hash": parent_hash, "brief_hash": digest(brief), "intent": intent,
              "selected_findings": [{"finding": known[identity], "decision": dispositions[identity]} for identity in sorted(dispositions)],
              "preserve_requirements": list(brief.get("locked_requirements", [])),
              "regression_targets": regression_targets(brief, findings)}
    return bundle, digest(bundle)


def verify_edit_bundle(bundle, expected_hash, *, brief, parent_version_id, parent_hash):
    if not isinstance(bundle, dict) or digest(bundle) != expected_hash or bundle.get("schema_version") != "visual-edit-1":
        raise ValueError("Saved edit bundle hash or version changed")
    if (bundle.get("parent_version_id"), bundle.get("parent_hash"), bundle.get("brief_hash")) != (parent_version_id, parent_hash, digest(brief)):
        raise ValueError("Edit bundle changed its immutable parent or brief")
    return deepcopy(bundle)


def regression_summary(targets, critiques, eligible_providers, *, comparison_critiques=None, unverified_critiques=()):
    providers = set(eligible_providers)
    voters = critiques if comparison_critiques is None else comparison_critiques
    rows, blockers = [], []
    for target in targets:
        latest = {critique.provider: {"provider": critique.provider, "result": check.result, "reason": check.reason}
                  for critique in voters if critique.provider in providers
                  for check in critique.regression_checks if check.target_id == target["id"]}
        checks = list(latest.values())
        values = [check["result"] for check in checks]
        complete = len({check["provider"] for check in checks}) >= 2
        if not complete:
            status = "pending"
        elif "regressed" in values:
            status = "regressed"
        elif "persistent" in values:
            status = "persistent"
        elif all(value == "resolved" for value in values):
            status = "resolved"
        elif all(value == "declined" for value in values):
            status = "declined"
        else:
            status = "uncertain"
        rows.append({**target, "status": status, "checks": checks})
        if status != "resolved":
            blockers.append(target["id"])
    resolved_targets = {row["id"] for row in rows if row["kind"] == "prior_finding"
                        and row["severity"] in {"material", "critical"} and row["status"] == "resolved"}
    for critique in critiques:
        if critique.provider in providers:
            blockers.extend(finding.id for finding in critique.findings
                            if finding.severity in {"material", "critical"}
                            and not (finding.disposition == "addressed" and finding.previous_finding_id in resolved_targets))
    for critique in unverified_critiques:
        blockers.extend(finding.id for finding in critique.findings if finding.severity in {"material", "critical"})
    return {"complete": len(providers) >= 2 and all(row["status"] != "pending" for row in rows),
            "pending_target_ids": [row["id"] for row in rows if row["status"] == "pending"],
            "unresolved_blockers": sorted(set(blockers)), "dispositions": rows}
