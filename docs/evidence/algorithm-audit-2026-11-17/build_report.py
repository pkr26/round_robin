"""Combine independently recorded algorithm audit artifacts without changing product code."""
from collections import Counter
import hashlib
import json
from pathlib import Path

BASE = Path(__file__).resolve().parent
ROOT = BASE.parents[2]
DOMAINS = ("root", "runtime", "evidence", "evaluation")
reviews = {domain: json.loads((BASE / domain / "review.json").read_text()) for domain in DOMAINS}
findings = []
inventory = []
for domain, review in reviews.items():
    for original in review["findings"]:
        item = {**original, "domain": domain, "review_artifact": f"{domain}/review.json"}
        item["priority"] = item.get("priority", item.get("severity", "P2"))
        kind = item.get("kind", "implementation_bug")
        if kind in {"process_capability_gap", "capability gap"} or "-G" in item["id"]:
            item["classification"] = "capability_gap"
        elif kind == "protocol_mismatch":
            item["classification"] = "protocol_mismatch"
        else:
            item["classification"] = "defect"
        findings.append(item)
    for original in review.get("inventory", review.get("algorithm_inventory", [])):
        item = {**original, "domain": domain}
        item["entry_id"] = f"ALG-{len(inventory) + 1:02d}"
        item["name"] = original.get("algorithm", original.get("name"))
        item["source_refs"] = original.get("source", original.get("source_refs_and_invariants",
            f"{original.get('file', '')}:{original.get('line', '')}"))
        item["invariant"] = original.get("main_invariant", original.get("invariant", original.get("source_refs_and_invariants", "")))
        item["status"] = original.get("status", original.get("assessment", "reviewed"))
        item["finding_ids"] = original.get("finding_ids", [original["finding_id"]] if original.get("finding_id") else [])
        inventory.append(item)
findings.sort(key=lambda item: (item["priority"], item["classification"], item["id"]))

source_paths = set()
for directory, extensions in (("backend/src", {".py"}), ("experiments", {".py"}), ("frontend/app", {".ts", ".tsx"}), ("packages/api-client/src", {".ts"})):
    source_paths.update(path for path in (ROOT / directory).rglob("*") if path.is_file() and path.suffix in extensions)
for name in ("pyproject.toml", "uv.lock", "package.json", "pnpm-lock.yaml", "frontend/package.json"):
    source_paths.add(ROOT / name)
manifest = [{"path": str(path.relative_to(ROOT)), "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "bytes": path.stat().st_size} for path in sorted(source_paths)]
source_body = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
summary = {
    "audit_date": "2026-11-17", "timezone": "America/Phoenix", "inventory_entries": len(inventory),
    "inventory_by_domain": dict(Counter(item["domain"] for item in inventory)),
    "findings": len(findings), "classifications": dict(Counter(item["classification"] for item in findings)),
    "priorities": dict(Counter(item["priority"] for item in findings)),
    "backend_passed": 1971, "backend_skipped": 43, "frontend_passed": 247,
    "source_manifest_sha256": hashlib.sha256(source_body).hexdigest(),
    "product_changes": False, "paid_calls": 0, "accuracy_established": False,
    "scope_note": "Inventory entries are related algorithm/process responsibilities, not 80 unique mathematical algorithms or a claim that every source line was formally verified.",
}
for name, value in (("summary.json", summary), ("findings.json", findings), ("algorithm-inventory.json", inventory), ("source-manifest.json", manifest)):
    (BASE / name).write_text(json.dumps(value, indent=2) + "\n")

inventory_text = ["ROUND ROBIN — ALGORITHM AND PROCESS INVENTORY", "Audit date: November 17, 2026 (America/Phoenix)", "",
    "All mapped discussion responsibilities are listed, including experimental and gated visual paths. 'No concrete defect observed' describes the review result; it is not a correctness proof or an empirical model-quality result. Domain entries sometimes inspect different boundaries of the same module. Sources and invariant details are retained in algorithm-inventory.json and each domain review.", ""]
for item in inventory:
    inventory_text.extend([f"{item['entry_id']} [{item['domain']}] {item['name']}", f"Status: {item['status']}", f"Source: {item['source_refs']}"])
    if item["invariant"] and item["invariant"] != item["source_refs"]:
        inventory_text.append(f"Invariant/assessment: {item['invariant']}")
    if item.get("complexity_or_bounds"):
        inventory_text.append(f"Complexity/bounds: {item['complexity_or_bounds']}")
    if item["finding_ids"]:
        inventory_text.append("Findings: " + ", ".join(item["finding_ids"]))
    inventory_text.append("")
(BASE / "ALGORITHM_INVENTORY.txt").write_text("\n".join(inventory_text))

report = """ROUND ROBIN — DISCUSSION ALGORITHM AUDIT
November 17, 2026 (America/Phoenix)

The coordination infrastructure is substantial, but the current implementation cannot yet be described as accuracy-validated. The audit found 11 implementation/evaluation defects, five capability gaps and one experiment/application protocol mismatch across 80 algorithm/process entries. Four findings are P1 (high priority); 13 are P2. All findings remain open. This was an audit: product code was not changed.

The full existing offline backend suite passed 1,971 tests, with 43 skipped and 16 warnings. All 247 frontend unit/component tests passed. Newly written adversarial probes still reproduced the reported gaps. Passing regression tests demonstrates the tested controls, not the factual correctness or superiority of multi-agent answers.

The application currently executes this bounded graph:

  frozen brief / explicit preflight / admission
    -> intake -> optional user confirmation
    -> selected evidence preparation / optional bounded public research
    -> three independent proposals (no current-run peer outputs/claims)
    -> synthesis -> different-provider audit
    -> at most one corrected synthesis + re-audit if the first audit is negative
    -> deterministic publication of a completed or limited result.

The active protocol is independent_review_provisional. There is no live turn-by-turn round-robin debate, voting/consensus algorithm, learned judge calibration, explicit multi-criteria optimizer, or adaptive convergence rule. The experiment runner contains critique, evidence-review and revision stages. The provider wire output uses free-text dissent/defects, not the richer structured Objection/revision-response records found in the sample Contribution schema. These distinctions are capability boundaries, not defects merely because a more elaborate algorithm could be built.

The strongest existing controls are independent proposal contexts, distinct synthesis/auditor provider identity, finite stages and call counts, durable attempt authorizations and immutable report versions, source hashes and scoped references, exact Decimal calculations, spending reservations, cancellation/generation fences, bounded network operations, explicit uncertainty and conservative simulation labels. Provider diversity establishes identity separation; it does not prove independent errors or factual truth. No funded or human evaluation was performed in this audit.

The complete algorithm inventory is ALGORITHM_INVENTORY.txt (80 entries) and algorithm-inventory.json. The following findings include code locations and reproduction artifacts; the domain review JSON files contain fuller details and caveats. P1 means fix before relying on the affected capability; P2 means a material correctness/process weakness. A capability gap is kept separate from a reproducible implementation defect.

"""
for item in findings:
    report += f"{item['id']} — {item['priority']} — {item['classification']}\n{item.get('title', '')}\n"
    refs = item.get("source_refs", item.get("source", ""))
    report += "Source: " + ("; ".join(refs) if isinstance(refs, list) else refs) + "\n"
    if "description_trigger_impact_validation_fix" in item:
        detail = item["description_trigger_impact_validation_fix"]
        report += ("Trigger:" + detail.split("Trigger:", 1)[1] if "Trigger:" in detail else detail) + "\n"
    else:
        for key, label in (("trigger", "Trigger"), ("actual", "Observed"), ("impact", "Impact"), ("fix", "Fix"), ("recommendation", "Fix"), ("acceptance_check", "Acceptance check"), ("caveat", "Limit")):
            if item.get(key):
                report += f"{label}: {item[key]}\n"
    proof = item.get("proof", item.get("evidence", item.get("validation", "")))
    report += f"Evidence: {item['review_artifact']}; {proof}\n\n"

report += """Accuracy-focused algorithm upgrades

1. Make support proposition-specific. Keep immutable claim nodes and add typed supports/refutes/qualifies edges with exact source span, verification state, source family, freshness and assessor. Separate source reachability from accepted support. A counterexample source can support an opposite proposition; do not globally ban every contradicted source. Recompute affected descendants on correction, preserve conflicting evidence and downgrade unresolved claims. A DAG traversal remains O(V+E); the sophistication belongs in explicit semantics and verification, not an arbitrary numeric confidence score. Acceptance: the contradicted-chain probe cannot become supported without a justified new support/resolution record; an explicitly refuted predecessor remains visible.

2. Make disagreement a durable work item. Give material objections IDs, target claim/requirement, severity, evidence and a status. Every synthesis/correction must respond to each material objection with accepted/qualified/rejected/unresolved plus a basis. The auditor checks this ledger. Unresolved critical items survive all summaries and exports. Store the full list with pagination; a presentation limit must never erase a critical item. Acceptance: more than 20 early low-priority concerns cannot suppress a later audit blocker, and an earlier critical concern requires an explicit disposition.

3. Unify the protocol as one versioned transition policy. Share its stage dependencies, required context, retry eligibility, correction rules and stopping predicates between production and evaluation. Keep separate counters for transient retries, schema repairs and substantive corrections while retaining original aggregate caps. Persist a per-step not_before timestamp checked under the execution lock. Before waiting or scheduling optional work, prove wait + attempt + mandatory remaining critical path + persistence margin fits the remaining active time. Acceptance: duplicate delivery never bypasses not_before; a long optional backoff is declined early enough for useful synthesis/audit; original proposal independence and cancellation fences remain intact.

4. Allocate evidence by unresolved decision need. Reserve retrieval opportunities for cost, feasibility and counterevidence before the first query can consume all pages. Dedupe by recorded source family, select relevant exact passages with surrounding exceptions and dates, and disclose omitted coverage. Preserve the existing approved-term privacy boundary. Token/byte allocation must reserve room for mandatory synthesis/audit context, including objections and claim references. Acceptance: each admitted query class gets its declared coverage or an explicit limitation; a decisive exclusion after character 4,000 is selected when relevant; UTF-8 boundary cases fail preflight instead of all proposals.

5. Make recommendation logic inspectable. Keep deterministic cost and hard-constraint calculations authoritative where inputs support them. Treat disputed feasibility as unknown until justified. Add explicit Pareto/dominance comparisons and sensitivity ranges for uncertain costs or deadlines, then use the user's stated preference ordering. The current preferences are ordinal ranks, so do not silently invent cardinal weights. Ask for the decisive missing fact when ranking is unstable. Keep 'execution complete', 'review coverage complete', 'evidence sufficient' and 'ready to choose' as separate states. Provide an authorized evidence-verification transition if true readiness is a product requirement; do not relabel user-supplied data as verified to make the status reachable.

6. Close the visual feedback loop. Freeze an edit bundle containing selected finding IDs, exact observations/regions, requested changes, declines and locked requirements. Hash and deliver it to the editor, then send it with parent/new pixels to independent reviewers. Derive required regression checks server-side, preserving unaddressed old blockers as pending. Reviewer quorum and improvement are distinct. Acceptance: exact address/decline decisions occur in the actual editor wire prompt; each prior material finding has a child disposition or an explicit pending state.

7. Establish accuracy with paired external evaluation. Compare the single-model, independent-review and fuller-debate paths using the same frozen substantive cases and declared resource ceilings. Run the actual versioned production policy or label omissions as ablations. Normalize superficial case titles/IDs before counting independent cases, review semantic duplicates and development overlap, and retain refusals, failures and unknowns in denominators. Use blind human grades, critical-defect checks, task-level paired outcomes and repeat variability; report uncertainty as well as usefulness, source support, latency and spend. Do not count repeated executions as independent tasks. The 16-case feasibility and 48-case held-out protocols already documented in the repository are plans, not evidence of passed accuracy gates. Additional rounds become justified only when their incremental benefit is measured.

Implementation order

First repair report attribution/disagreement, claim polarity handling, mandatory-time reservation and visual finding transmission (the four P1 findings). Next fix source allocation, dissent carry-forward, retry not_before, queue quotas, UTF-8 preflight, release duplicate detection and activity pagination. Then add the support/objection/regression contracts and shared protocol engine. Run the relevant probes as regressions after fixes, then the existing suites. Only after that should a funded evaluation be used to decide whether the full debate path improves outcomes enough to become standard.

Verification and reproducibility

- root/pytest-all.log: uv run pytest -q — 1,971 passed, 43 skipped, 16 warnings, exit 0; 154.42 seconds.
- root/frontend-tests.log: pnpm --filter @round-robin/web test — 247 passed across five files, exit 0.
- root/probe_protocol.py: four assertions on intake visibility, repair feedback, Unicode packet admission and activity pagination.
- runtime/probe_runtime.py and runtime/probe_queue.py: three reproduced retry/reserve/queue defects.
- evidence/probes.py: six checks covering disagreement attribution, claim polarity, dissent, source allocation, readiness reachability and passage selection.
- evaluation/probe.py: four checks covering visual edit feedback, regression completeness, protocol parity and release-case aliasing.
- evidence/visual_cross_review.py: independent mocked-HTTP cross-check of actual image edit and two-critic request/response paths. Parent pixels and findings DO reach child critics; the missing parts are generator feedback and mandatory regression dispositions.
- evaluation/cross-review.json: independent source review of the strongest evidence/report findings and their limits.

Focused domain test runs (154 runtime, 328 evidence, 133 evaluation/visual) overlap the full backend suite and must not be added to 1,971 as independent coverage. The new probes intentionally assert current faulty behavior so the defects are reproducible; they are not repaired-product regression tests yet. Each Python probe has a command in its domain review or script. PYTHONPATH=backend/src:. works for root/runtime/evaluation; the evidence lane records PYTHONPATH=backend/src:backend/tests.

No production source was edited, paid provider called, person contacted or deployment performed. Tests/probes used temporary databases, simulated adapters or mock HTTP transports. The 43 skipped backend tests require explicit service/fault/native profiles; fresh PostgreSQL/Redis concurrency, real provider transport compatibility and empirical model quality remain unverified by this audit. Existing dependency deprecation warnings remain in the test logs. The workspace has no Git metadata; source-manifest.json binds the observed files and dependency locks, not a commit or a before/after proof. The manifest also includes adjacent source files for reproducibility; inclusion alone is not a claim of full line-by-line review. Backup/security/rendering behavior outside the discussion correctness boundaries is outside this audit's scope.

Specific limits on the conclusions: incorrect infeasibility is a readiness-reducer finding, not proof that a negative audit is marked execution-completed. Dissent remains in original saved contributions even where the report omits it. Claim graph reachability cannot itself decide semantic truth. Visual coverage.complete means two eligible critics reviewed pixels, not that a design passed a quality test. Renamed release cases inflate the numerical usefulness count, but phase_5_passed correctly remains false. No numerical real-world accuracy rate can be inferred from these offline results.
"""
for before, after in (("character4000", "character 4000"), ("at4000", "at 4000"), ("is4249", "is 4249"), ("after4200", "after 4200"), ("selected4000", "selected 4000")):
    report = report.replace(before, after)
(BASE / "REVIEW.txt").write_text(report)

evidence_index = []
for path in sorted(BASE.rglob("*")):
    if path.is_file() and path.name != "evidence-index.json" and "__pycache__" not in path.parts:
        evidence_index.append({"path": str(path.relative_to(BASE)), "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "bytes": path.stat().st_size})
(BASE / "evidence-index.json").write_text(json.dumps(evidence_index, indent=2) + "\n")
print(json.dumps(summary, indent=2))
