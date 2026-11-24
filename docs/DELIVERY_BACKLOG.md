# Round Robin delivery backlog

November 16, 2026 • Version 7 execution order • Later construction authorized; acceptance evidence tracked separately

This is the delivery and acceptance register. Completed engineering work remains traceable to its original ticket; its remaining external acceptance evidence is recorded separately. The [product plan](PROJECT_PLAN.md) owns scope and scorecard definitions; the [implementation blueprint](IMPLEMENTATION_BLUEPRINT.md) specifies schemas, algorithms, API/command identities and operational contracts; the [phase plan](IMPLEMENTATION_PHASES.md) owns gates; [failure handling](FAILURE_HANDLING.md) owns invariants and exact scenarios. A ticket is complete only with its saved output and review evidence. Do not mark a ticket complete because this document describes it.

**Current implementation:** the user authorized building through the later phases on November 16 while deferring external acceptance work. The [fresh audit report](evidence/exhaustive-audit/REPORT.md) is the current engineering review and records the accepted full verification run and per-service coverage. The earlier [implementation record](evidence/implementation-phases.md) is historical. Construction-order dependencies below describe the original sequence; the authorization supersedes them, while real-user, funded-provider and release acceptance still apply.

The durable product and gated visual/native extensions are present. The audit also integrated one bounded corrective synthesis and independent re-audit, immutable claim persistence with explicit selected parent reuse, and atomic active-parent replacement authorized at Start. Those are implemented behaviors, not outstanding integration tickets. Correction preserves original limits; selected reuse preserves provenance and stale-support labels; replacement preserves existing charges and uncertain exposure. Their human/model-quality acceptance remains part of the relevant tickets.

## Next acceptance work

Maintain the implemented fictional sample, durable comparison workflow, generated contracts, simulated adapters, evaluation tools and operator safeguards. Collect the still-missing consented research/comprehension, explicitly funded evaluation, held-out release, manual accessibility and deployed staging evidence described below and in [acceptance blockers](ACCEPTANCE_BLOCKERS.md). Closed visual/native gates remain closed until their separate demand and evaluation requirements are met.

Engineering fixes and offline verification can continue without funding or recruitment. No offline artifact substitutes for five real observations, a funded paired study or hosted staging. Use `pnpm audit:full` for the fresh source-bound per-service coverage gate, with the disposable services and renderer specified in the [operator runbook](OPERATIONS.md#reproduce-verification); final command outcomes belong in the fresh audit report, not assumptions in this backlog.

## First milestone: prove one useful decision

### RR-01 — Validate recent real comparisons

**Phase:** 0A. **Preparation dependency:** the supplied audience/task definition. **Completion dependency:** access to five representative owners/operators. **Owner role:** product lead.

Observe each user's most recent relevant decision, current workaround, options, decisive constraints, source formats, acceptable cost/wait and frequency of the next comparable task. Rehearse operator setup separately from end-user use. Establish whether pasted/structured quote summaries preserve essential conditions.

**Acceptance:** five consented, sanitized research records; candidate evaluation briefs; input-format findings; initial willingness to use; documented proceed/narrow decision. If essential evidence needs PDF extraction, scope that blocker before paid pilot construction. Do not invent user interviews or contact people without authorization.

### RR-02 — Prototype the complete comparison experience

**Phase:** 0A. **Preparation dependency:** none; use fictional cases now. **Validation dependency:** RR-01's consented task examples and users. **Owner role:** product/design.

Build a low-cost inspectable prototype and one clearly labeled fictional sample. Cover sample → editable brief → options/status quo → preflight → running/partial states → recommendation/readiness → exact passage/calculation → changed-input follow-up. Include a quote/site-check next action. No live inference is needed.

**Acceptance:** a reviewable fictional prototype exists independently of recruitment. Passing the ticket additionally requires at least four of five target users to correct/save a draft within three minutes and find the recommendation or why no choice is established, caveat, passage/calculation and next action unaided. Record confusion about partial, unverified and Stop not confirmed. Include keyboard and narrow-screen inspection; prototype availability is not user-validation evidence.

### RR-03 — Freeze evaluation and funding policy

**Phase:** 0B entry. **Preparation dependency:** provisional brief/report contracts. **Freeze/live-study dependency:** completed RR-01/02 and actual bounded funding. **Owner role:** evaluation lead/operator.

Create a manifest of 16 tasks: four budget/lifecycle-cost, three deadline/feasibility, three missing/stale quote, three preference/status-quo and three misleading/shared-evidence cases. Record hard requirements, acceptable alternatives, critical dependencies, checked passages, paired workflows, baseline selection, randomized reviewer packets and two-reviewer allocation. Five fixed tasks run three times total per workflow, including their preregistered primary executions: 26 trials/workflow and 78 across three workflows. Primary usefulness is at least 13/16 task results; repeats report variability without inflating independent sample size or replacing originals. Freeze protocol/model/prompt/pricing versions, request/time/currency limits and scorecard targets before the paid batch.

**Acceptance:** immutable dataset/configuration fingerprints, primary/repeat assignment and grading packets, plus explicit whole-batch funding/request limits before live use. The evaluation envelope is at most 1,872 requests at 24/trial and $156 USD at $2/trial; at most three separate one-request adapter smokes bring the request ceiling to 1,875. Declare smoke funding separately. These are upper arithmetic envelopes, not permission to spend or raise personal/aggregate limits; the lower actual funded batch/owner/deployment policy wins. Record at most two substantial revisions, then continue/narrow/pivot. Neither the audit nor possession of keys authorizes this batch.

### RR-04 — Implement and evaluate the bounded runner

**Phase:** 0B. **Offline dependency:** provisional schemas and fixture inputs. **Live-study/completion dependency:** frozen RR-03 and funded provider access. **Owner role:** backend/evaluation.

Build minimal Python schemas, adapters, read-only API/UI contract fixtures, local request journal and fail-closed simulated profile without paid access. Before live use, enforce an exclusive process lock and fsynced authorization/settlement journal, stable attempt/batch identities, disabled SDK retries, original call/time ceilings, protected finish resources and whole-batch/owner/deployment caps. A restart retains authorized-but-unsettled exposure; corrupt/missing required journal state blocks dispatch. Compare a strong single-provider workflow, independent proposals/synthesis/audit and bounded debate using matched inputs and comparable funding ceilings. Retain failures and unknown exposure.

**Acceptance:** offline crash/restart/lock and aggregate-cap fixtures pass before any live call; begin `LIM-11`/`LIM-12` with existing Phase 0 IDs. Live completion requires saved task/output/configuration IDs, blinded original/adjudicated grades, primary and repeated outputs, conservative cost per useful result, admitted-trial deadline counts and a recorded protocol decision. At least 13/16 primary task results must score 4/5 with user-valued benefit over the baseline; no-choice answers can qualify when useful and correct. Fixed-evidence protocol evidence is labeled separately from later end-to-end retrieval evaluation. No useful multi-provider benefit requires a narrow/pivot decision before claiming a validated multi-provider release, even though construction was separately authorized.

## First build: a saved comparison, end to end

### RR-05 — Deliver the durable fixture vertical slice

**Phase:** 1. **Contract/fixture preparation:** may begin offline in Phase 0. **Original construction dependency:** Phase 0; construction proceeded under explicit authorization. **Acceptance dependency:** the applicable Phase 0 evidence remains required. **Owner role:** full stack.

Scaffold the documented stack and generated contracts around the blueprint's draft, preflight, input-manifest and report schemas. Save a draft with two to four options and constraints; obtain a no-inference preflight; explicitly start one fixture-backed job; inspect comparison/readiness; reopen the saved result. Preflight binds draft/configuration/input hashes, shared three-provider recipients and policy versions, expires in ten minutes, and produces an initial-admission deadline 30 minutes after accepted Start. Keep the development identity on localhost and fixture data visibly labeled.

**Acceptance:** draft/preflight perform no inference; stale draft/configuration/recipient scope cannot Start; expired initial queue admission makes no paid call. One idempotent Start survives lost response/duplicate delivery; retained noncontent paid UUID tombstones prevent replay after body cleanup or meeting deletion. A crash preserves committed work and ordered events replay into the same report. Extend `CMD-01`/`CMD-09`/`CMD-10`/`CMD-13` and `OPS-03`; this replaces a generic background-task demonstration.

Review the one-run-per-preflight constraint and separate initial versus resumption ticket guards explicitly; different command UUIDs cannot spend the same preflight twice, and confirmation after a legitimate input wait cannot reset allowances or be rejected by a stale first-admission check.

### RR-06 — Enforce paid-work authorization and finishing resources

**Phase:** 2. **Dependency:** RR-05. **Owner role:** backend.

Implement counted attempts, unknown usage, owner/deployment daily/monthly budgets, operator kill switch, admission, active-time accounting and protected final money/call slots/time. Apply the blueprint's single-pool exposure conversions: a run/finish reservation becomes an attempt reservation rather than being charged a second time, and every cap references the same exposure once. Defaults are personal only; hosted paid access needs explicit quotas. Period rollover, a new run, fallback or follow-up must not clear unresolved exposure.

**Acceptance:** deterministic authorization/rollover/conversion races and restore fixtures prove conserved exposure, no double counting and no fresh allowance from identity changes. Expand Phase 0B's `LIM-11`/`LIM-12` checks to actual PostgreSQL/worker races with existing limit cases. A deterministic partial report needs zero inference. A configured cap constrains authorization rather than guaranteeing an external bill.

### RR-07 — Complete the selected real Standard protocol

**Phase:** 2. **Dependency:** RR-06. **Owner role:** backend/full stack.

The implemented provisional path uses independent proposals with fixed shared inputs, synthesis and a distinct final auditor. A negative/critical audit can fund one corrective synthesis and independent re-audit within the original logical-role calls, infrastructure claims, request/time/money limits and fences. No correction replays intake/proposals or resets authority; insufficient headroom yields a limited result. Retain only explicitly enabled stages. Bind every context packet to frozen input/configuration IDs and the concrete request-contract hash; no retry may substitute recipients, prompt/schema semantics or limits. Preserve missing participation, refusal, cancellation, fallback, eligible retry and partial output. Application rules compute readiness from selected recommendation, hard-requirement/dependency states and actual required review coverage; a model-proposed readiness enum has no authority. Missing assessment stays null.

**Acceptance:** one explicitly funded real comparison plus relevant deterministic provider/race tests. `RPT-09` catches readiness/report contradictions; stale preflight/configuration, lost paid UUID, expired initial admission, unsupported context and recipient changes are covered through existing `CMD`/`EVD`/`LIM`/`OPS-03` cases. If debate is disabled, its tests are recorded as deferred with the evaluation gate, rather than passed or silently required for release.

### RR-08 — Make every decisive fact and number inspectable

**Phase:** 3. **Dependency:** RR-07. **Owner role:** backend/evaluation.

Add controlled retrieval using user-approved public terms and allowlisted query templates, stored passages, source grouping, freshness and bounded counterevidence. Models select approved term/template IDs rather than dispatching arbitrary private text. Freeze attachment IDs/hashes and the same three-provider recipient set for all selected inputs; fence input deletion. Use bounded Decimal calculations with explicit units, currencies, common horizon and missing components. Show per-option hard requirements and status quo. Immutable claim versions now commit atomically with successful output. Explicit parent evidence/claim selections bind exact versions and hashes at preflight and Start, receive child-scoped identities, retain provenance and do not renew stale support. Independent proposals receive imported claims without sibling-proposal claim leakage.

**Acceptance:** `EVD-10`/`EVD-13`/`EVD-14` plus existing evidence cases; passage-support human review; copied-source errors and wrong arithmetic cannot become verified facts. Missing quote components are unknown, not zero. Query dispatch accepts only approved public term/template combinations; private model text or URL secrets cannot enter search through a permissive free-form fallback. Record the outgoing query contract and rejected unsupported requests.

### RR-09 — Finish accessible decision and return journeys

**Phase:** 4. **Dependency:** RR-08; build UI incrementally from RR-05. **Owner role:** frontend/design.

Finish Overview, Evidence and Activity, preflight recipients/allowances, actual progress, meaningful partial/Stop states, private uploads, source dialogs, next-evidence checklist, choice/outcomes and linked follow-up changes. Render the copyable quote/site-check brief from saved records. Keep advanced model settings secondary. Active-parent replacement now requires the explicit parent version from a fresh preflight: fencing the parent and creating its linked child commit together, preserving charges and unknown exposure. Saving or editing the follow-up draft leaves the parent unchanged.

**Acceptance:** four-of-five real-product comprehension gate for recommendation or no-choice explanation, caveat, evidence/calculation and next action; manual keyboard/screen-reader/focus/error/reflow checks against the WCAG 2.2 AA target. Reconnect never submits a new paid command; changed inputs preserve report lineage and stale-evidence labels. Instrument qualifying-content and completion events for deadline attainment while keeping queue/input waits separate.

### RR-10 — Export the exact saved version

**Phase:** 4. **Dependency:** saved structured report contract. **Owner role:** full stack.

Deliver web, Markdown, JSON and PDF using the same saved report/version cutoff. Preserve readiness, caveats, cost basis, passages and next action. Keep source links safe; prevent external resource loads and arbitrary remote images.

**Acceptance:** export retry calls no model; malformed or unavailable storage never claims ready; report/version consistency checks pass. Render and visually inspect representative long PDFs, tables and partial reports before enabling PDF download.

## Release and expansion gates

### RR-11 — Prove the integrated local release

**Phase:** 5. **Dependency:** RR-05–10 and their required gates. **Owner role:** engineering/evaluation.

Freeze 48 held-out cases, eight in each of six strata: budget/cost, deadline/constraint, missing/stale evidence, preference/status-quo, evidence/deception and clarification/tool/provider limitation. Evaluate the actual application retrieval/calculation/report workflow, separately from Phase 0B's fixed-evidence protocol experiment. Run actual-process fault combinations, contract history, deletion/restore quarantine, bounded load and scorecard measurements. Keep failed/cancelled/missing results in admitted-trial deadline accounting. Close all enabled core scenarios with implementation/test/evidence records, and implement the blueprint's migration, startup/drain, rollback and backup/restore operator entry points.

**Acceptance:** immutable dataset/configuration IDs and original/adjudicated grade records; at least 39/48 useful primary task results without hidden critical defects. At least 90% of admitted trials produce qualifying content within 60 active seconds, at least 50% complete within 120, and at least 90% complete within 300. Missing/failed/cancelled results are misses; report their counts and successful-run distributions separately. Require explicit cost/comprehension results, passed recovery/deletion/compatibility and tested operator commands. The release decision states domain limits and deferred expansions. Simulated success cannot satisfy the real-provider gate.

### RR-12 — Enable an invited hosted pilot

**Phase:** 6, only if requested. **Dependency:** RR-11 and hosting authorization. **Owner role:** operator/security.

Configure operator-funded provider access, explicit owner/deployment quotas, storage/queue/export limits, secure sessions, owner isolation, CSRF, private artifacts, TLS/SSE proxy, secrets, retention and staging recovery. Freeze the blueprint's deployment profile and implemented operator runbook: configuration validation, migrate-before-worker startup, incompatible-version readiness rejection, drain, rollback without paid replay, kill switch, backup interval/retention and tested restore objective. Participants do not configure three provider accounts. Checkout and subscription billing stay outside scope.

**Acceptance:** two-user route/stream/download isolation, session expiry, quota/kill-switch and staging restore tests; documented reset/retention policies and pilot scorecard. No deployment is authorized merely because the plan describes it.

### RR-13 — Decide whether visual work is warranted

**Phase:** before 7. **Dependency:** RR-11 and documented core use. **Owner role:** product/evaluation.

Collect at least three target users' concrete unmet visual jobs in one raster domain. Record the attempted text workaround and why it fails. Compare need with evidence-import blockers and core reliability work. Do not count broad enthusiasm or the existence of vision APIs as demand.

**Acceptance:** recorded proceed/defer decision, one supported raster domain, competent reviewers, relevant fixtures and a separately funded evaluation plan. Without this evidence, keep the implemented optional visual features closed for release and continue improving the core comparison; feature availability is not demand validation.

### RR-14 — Earn raster improvement before native expansion

**Phase:** 7/8, conditional. **Dependency:** RR-13. **Owner role:** visual/backend/evaluation.

The gated implementation includes safe canonical upload, exact-pixel receipts, independent review, bounded generation/editing, immutable V1–V3, comparison and user selection. Preserve their engineering invariants while collecting the real-model capability and human-quality evidence needed for release. Before releasing automatic improvement, run the matched generation-only/one-critic/multi-critic human evaluation specified in the visual workflow. Phase 9 native renderers have their own demand, isolation and functional gates.

**Acceptance:** relevant `VIS-01`–`VIS-12` plus core requirements pass; blinded useful-improvement/regression evidence supports release; incomplete reviews remain visible. Hosted raster delivery additionally passes extended Phase 6 controls. Native output is neither silently included nor required to release justified raster functionality.

## Review record

For each implementation change, record ticket/phase and blueprint contract version, enabled scope, exact scenario IDs, implementation paths, API/schema/migration changes, relevant state transitions, actual commands and evidence, paid-work limits/exposure conservation, user-visible saved artifact, remaining risk and next gate. Evaluations additionally include immutable task/output/configuration IDs, primary/repeat allocation, original/adjudicated grades, admitted-trial deadline counts and fixed-evidence versus end-to-end mode. Default CI uses no real-provider credentials or traffic. Human research and paid experiments require their actual prerequisites; pending access is not proof of completion.
