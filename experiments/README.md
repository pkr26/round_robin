# Phase 0 preparation and explicitly funded studies

These tools prepare RR-03/RR-04 under blueprint Version 7. All 16 task cases,
quote passages and calculations are fictional. The dataset is a draft: no real
participants, frozen paid study, provider calls, human usefulness findings or
validated Standard protocol are represented. The default runner and fictional
evaluation remain offline. A separate approval-gated study CLI can invoke the
configured real adapters; no real study approval or spending is supplied here.

From the repository root:

```sh
uv run python -m experiments.evaluation validate
uv run python -m experiments.evaluation plan --output .local/evaluation/trial-plan.json
uv run python -m experiments.evaluation grade-schema --output .local/evaluation/manual-grade.schema.json
uv run pytest backend/tests/test_evaluation.py
uv run python scripts/scenario_summary.py --check
```

The manifest has four budget/lifecycle tasks and three each for deadline,
missing/stale quote, preference/status quo and misleading/shared evidence. Five
fixed tasks receive three total executions per workflow. Each of the three arms
has 16 primary and 10 repeat executions: 26 per arm, 78 total. Repeats never
replace originals or increase the primary denominator. The 1872-request / 156 USD
evaluation ceiling and three separately funded smoke requests remain arithmetic
envelopes; actual authorized spending is zero.

`build_trial_plan` binds each execution to the complete dataset fingerprint.
`build_blinded_packets` emits public reviewer packets and a separate private
mapping. `save_blinded_packets` writes separate files with mode 0600 and refuses
to overwrite an existing packet version. Keep the mapping in operator-only local
storage, away from reviewers and published artifacts. Save raw outputs immutably before packet
creation. Provider and workflow metadata stays private; common identity names
in output text are redacted. An operator still must inspect each packet for
indirect identity clues before any actual study. Use a fixed recorded
randomization seed and retain both raw/public hashes.

`ManualGrade` records the original reviewer, task/output version, exact packet,
timestamp, constraint and passage-support findings, usefulness, next-action
judgment and critical-defect reason. Append an adjudication with original grade
IDs and a reason; retain independent originals. At least eight fixed tasks need
two independent reviewers, as does every suspected critical defect. A hidden
critical defect cannot score 4 or 5. Disagreement defaults conservatively until
documented adjudication. Missing/failed/cancelled primary results remain misses.
`summarize_workflow` checks 13/16 numerically and always reports Phase 0 as pending:
real study evidence, baseline benefit and the protocol decision are separate
requirements. A useful explicitly conditional or no-choice result can earn 4/5
when the human rubric supports it.

`deadline_attainment` keeps every admitted result in the deadline denominator;
failed/cancelled/missing and incomplete mandatory work are misses. Never-admitted
queue entries are counted separately. Successful-run nearest-rank percentiles
are supplementary. Production instrumentation must additionally preserve
wall-clock, queue and user-input waits; these fixture calculations do not prove
the application's speed.

Before real study entry, complete RR-01/02 with five consenting target users,
resolve evidence-format blockers, select a strong baseline on separate examples,
freeze task/model/prompt/pricing/policy versions and permitted revisions, declare
whole-batch and aggregate funding/request limits, and pass the runner safeguards.
Possession of credentials or a plan file does not authorize calls.

The funded runner is `experiments.study_runner`. Export its authoritative schemas
without credentials, approval or inference:

```sh
uv run python -m experiments.study_runner --schemas .local/evaluation/study-schemas.json
```

An operator supplies an expiring `StudyApproval`, a `FrozenStudyManifest` with
exactly 16 typed briefs and hash-checked passages, and a pinned
`ProviderConfiguration`. Approval binds each file's exact SHA-256, the five-user
consent/discovery/comprehension record, a strong self-checking baseline selected
on separate development cases, and the preregistered independent reviewer plan.
The source files must exist and pass validation. The bundled fictional draft
does not pass these funded-entry requirements.

After preparing and inspecting those actual records, the operator can invoke:

```sh
uv run python -m experiments.study_runner --approval-file /private/round-robin-study/approval.json --set-spending-lock unlocked
uv run python -m experiments.study_runner --live --approval-file /private/round-robin-study/approval.json
uv run python -m experiments.study_runner --approval-file /private/round-robin-study/approval.json --set-spending-lock locked
```

These are nonsecret command examples; they were not used to authorize or run a
real study. The three provider keys stay in the operator environment and never
enter packets or journal diagnostics. Missing keys block entry before admission.
The single-provider arm includes proposal, refinement and same-provider
self-check. Independent review uses three hidden-peer proposals, synthesis and
a distinct audit. Experimental debate additionally uses bounded independent
critique, fixed-evidence review and any necessary author revisions; it does not
enable debate in the application.

The study freezes the concrete text request contract in its saved provider configuration: system instruction, strict output schema, request fields, token/packet bounds and application correction-policy descriptor. Every send rechecks that contract and the original file hashes. An old or mismatched live study journal/configuration cannot silently resume under a new prompt or schema. Preserve its retained trials and uncertain usage; an amended study needs explicit new reviewed authority rather than rewritten hashes or reset counters. Linked completed outputs remain bound to their original input and configuration hashes.

All 78 trial identities and five repeated cases are fixed before execution.
Primaries run before repeats, with a recorded shuffled order across arms. Saved
primaries cannot be replaced by favorable reruns. Each trial retains the original
24-call, two-call-per-step and 600-active-second ceilings. Explicit lower trial,
batch, owner and deployment caps win; USD 156 / 1872 requests is an outer study
envelope, not permission. No smoke request runs automatically.

The application's provisional independent-review protocol can now schedule one funded corrective synthesis and independent re-audit after a successful audit reports defects, within all original limits. The study CLI executes its separately frozen arms described above and does not add that application correction round after its final audit. Its bounded-debate author revision happens before synthesis/audit. A study intended to evaluate the application's correction round must explicitly revise and preregister the workflow; do not present current study-arm results as evidence for an unevaluated workflow.

Every send follows a confirmed fsynced authorization under exclusive journal
ownership and the shared operator policy lock. The operator lock command
serializes policy changes against authorizations. Raw visible model output,
input/configuration hashes, usage uncertainty, request counts and latency are
saved in private immutable artifacts. Unknown attempts retain conservative
exposure across restarts and UTC accounting periods. A crashed trial is retained
as interrupted, and its uncertain request is never automatically sent again.
Corrupt/missing journals or changed original output hashes block continuation. A backwards study clock also blocks progress rather than renewing the time allowance. Policy, lock and immutable artifact files must be real private files: symbolic links and nonregular lock files are rejected, and spending-lock changes require an exact boolean authority internally.

Nonzero cached-input usage without a frozen supported cache tariff is recorded as unknown, preserving the authorization bound and the raw usage for evidenced reconciliation. Conservative bookings cannot lower an already observed uncertain exposure; a later lower nonfinal amount does not establish a refund. Final settlement must be evidenced, and cancellation or restarting a batch does not erase unknown attempts.

Study spending uses the approval's one persistent authority journal. The approval
also declares other paid execution disabled; this CLI does not transact against
the application's PostgreSQL ledger concurrently. Preserve that isolation and
reuse the same journal across batches. An exhausted or locked batch stops with
its original outputs retained and unexecuted trials reported separately.

Blinded packet bundles preserve separate private identity mappings and original
output hashes. Human passage-support review, adjudication, user-valued benefit,
the protocol decision and held-out confirmation remain necessary. The runner
never automatically declares Phase 0 passed. **Actual funding, participants and
real-provider study results in this workspace remain zero.**

Application claims and selected follow-up reuse are separate from this fixed-evidence study journal. The application assigns immutable server claim identities, imports only explicitly selected exact parent versions, and keeps time-sensitive reused support unchecked. Replacing an active parent requires an explicit preflight version and commits its fence together with the child Start; it retains prior exposure. Before deploying the matching application workers, apply the claim/reuse migration and preserve those records in deletion and backup workflows. See the [operator runbook](../docs/OPERATIONS.md) for the migration and recovery sequence; the study runner does not bypass application authorization or share its ledger concurrently.

Prospective visual evaluation is available separately:

```sh
uv run python -m experiments.visual_evaluation OPERATOR_MANIFEST.json --plan
uv run python -m experiments.visual_evaluation OPERATOR_MANIFEST.json --results RESULTS.json
```

That module validates a frozen matched raster manifest, three arms, at least 12
independent briefs, four repeated briefs and 60 trials for the initial 12-brief
design. It retains failures, refusals, unknown exposure, neutral outcomes, rater
disagreements and regressions. It invokes no image provider and does not release
automatic improvement. Its manifest and grade fields are defined in
`experiments/visual_evaluation.py`.

The exact 100 failure IDs remain in `backend/tests/scenarios/manifest.json`.
Partial offline assertions retain their full later obligations and blockers.
`scripts/scenario_summary.py` checks the inventory and can write Markdown with
`--output PATH`; it never reports a phase as passed.

## Fresh implementation verification

From an environment without production provider credentials, run `pnpm audit:full` after preparing the disposable PostgreSQL/Redis services and local native-renderer image described in the [operator runbook](../docs/OPERATIONS.md#reproduce-verification). Stop the development supervisor first. `pnpm dev`, including its check/skip-services modes, rejects inherited non-loopback targets and routing overrides before services or migrations start.

The full audit uses a new output directory, fresh source-instrumented frontend/native captures, Python branch coverage and a source/verification-input manifest. Every backend package, application, migrations, experiments, tooling, frontend and native-renderer group must exceed 95% for both lines and branches. Current unexecuted production files remain in the counts; changing sources or verification inputs invalidates the record. The services CI lane runs the same profile, including real disposable service and browser/native checks. These checks issue no real provider requests and do not authorize a study.

Read the [fresh audit report](../docs/evidence/exhaustive-audit/REPORT.md) for the final recorded execution when available. Neither a configured CI job nor these reproduction instructions assert a completed passing run. Human discovery/comprehension, explicitly funded comparisons, blinded support/usefulness review, held-out confirmation and hosted staging acceptance remain separate obligations.
