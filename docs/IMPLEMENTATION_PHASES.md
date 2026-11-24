# Round Robin implementation phases and verification plan

Version 7 acceptance contracts • Implementation status updated November 16, 2026

Build Round Robin in phases that produce reviewable evidence of usefulness, correct execution, and recoverable failures. The first milestone combines target-user discovery with a bounded real-provider comparison. Start with comparisons of two to four project approaches or supplier offers, include the current approach or doing nothing when feasible, and make comparable costs reproducible. The original construction sequence placed broad infrastructure after workflow and protocol evidence; the explicit construction authorization below supersedes that sequencing, while release still requires the evidence.

**Current status (November 16):** construction through the later phases was authorized while external acceptance blockers were deferred. The [fresh audit report](evidence/exhaustive-audit/REPORT.md) is the current source of engineering findings and final verification results when available. The earlier [implementation record](evidence/implementation-phases.md) and [Phase 0 record](evidence/phase0-preparation.md) remain historical snapshots. The durable product, provider/worker boundaries, recovery/hosted controls and gated visual/native implementation are present. The deliverables below remain acceptance contracts, not a claim that every listed module still needs to be built.

Current implementation includes one bounded corrective synthesis and independent re-audit after a negative/critical audit when original resources permit; immutable claim persistence and explicit selected-parent evidence/claim reuse; and atomic active-parent replacement under a fresh preflight and explicit parent version. Correction does not reset calls, infrastructure claims, active time or money; reuse does not import unselected support or renew stale verification; replacement preserves existing exposure. These integrations are no longer pending engineering blockers. The [remaining acceptance register](ACCEPTANCE_BLOCKERS.md) preserves real-user, funded-provider, held-out quality, manual accessibility, hosted staging and visual demand/evaluation obligations. A clean audit run is still required to state actual verification results; implementation availability alone does not pass those gates.

The [product audit](PRODUCT_AUDIT.md) explains the revised priorities. The [delivery backlog](DELIVERY_BACKLOG.md) tracks the next concrete work and its acceptance evidence. The [implementation blueprint](IMPLEMENTATION_BLUEPRINT.md) specifies the cross-cutting schemas, commands, policy calculations, bounded context, readiness rules and operational contracts used by these phases. None substitutes planned work for implemented behavior.

Unless expanded, backend module paths below are relative to `backend/src/round_robin/`; test and frontend paths are relative to the repository root.

Re-estimate implementation effort after Phase 0 using measured provider behavior and the selected protocol. Do not treat an aggregate day count as a delivery commitment. Each phase passes through evidence, not elapsed time. Phases 0–5 deliver the core local product. Phase 6 is an optional operator-funded, invited hosted pilot; its access controls are required before any internet exposure. The visual extension begins only after Phase 5 and its separate demand gate. Phase 7 does not require hosted deployment. Phase 9 is an optional native-output track; it does not gate a raster release that passes Phases 7–8. Visual work is additional scope and is not required for the core MVP.

## Preparation versus passing a gate

The fictional labeled sample, typed contracts, calculator fixtures, simulated adapters, evaluation manifests and runner journaling are implemented alongside the durable application. Maintain and refine these offline artifacts without treating fictional evidence as observed findings. Their availability needs neither recruited users nor funded provider access.

Phase completion has stricter dependencies. Five-user discovery/comprehension evidence is still required to pass Phase 0A; funded paired results are required to pass Phase 0B. Construction has proceeded under explicit authorization, but a validated production protocol and release acceptance still require those gates. Missing participants or funding blocks the dependent study, not authorized no-cost engineering work. Do not claim a completed phase, real-provider support or a validated default from fixtures alone.

## What completes a phase

- Implement its deliverables and failure handling together. Recovery tests begin with the capability they protect; Phase 5 integrates them rather than introducing reliability for the first time.
- Map each applicable exact scenario ID from the failure contracts to its implementation, test, expected invariant, and execution evidence. A family name or prose promise alone is not test coverage.
- Record unimplemented scenarios explicitly with their phase and blocking dependency. An applicable failed or missing gate prevents phase completion; reclassifying a failure does not constitute a fix.
- A disabled optional protocol or output type has an explicit applicability decision and blocking feature gate. Record why its tests are inapplicable to this release; enabling it makes its contracts and tests mandatory. This cannot waive a requirement of the selected Standard path.
- Keep model-output correctness distinct from application correctness. Deterministic tests can verify references, permissions, counts, state, and persistence. Human review and evaluations assess whether evidence supports a claim and whether a recommendation is useful.
- Save artifacts that let another developer reproduce the result: configuration versions, nonsecret fixtures, test command, environment, exit status, and relevant sanitized diagnostics.

Maintain the implemented coverage manifest under `backend/tests/scenarios/` and its generated summary in CI. Each entry records `scenario_id`, `phase`, `implementation_paths`, `test_paths`, `test_profile`, `expected_invariants`, `fixture_ids`, and the evidence artifact location. Use the exact core IDs in `FAILURE_HANDLING.md` and visual IDs linked from `VISUAL_DESIGN_WORKFLOW.md`; do not create a second numbering scheme. Map browser scenarios to their API or reporting scenario as well as their Playwright test.

The initial allocation below identifies the first mandatory assertions for each scenario. Cross-cutting scenarios need additional tests when later capabilities appear: for example, an adapter error contract in Phase 0 does not prove durable worker recovery, and local owner scoping does not prove hosted session security. Expand each range into individual manifest entries. This allocation is planned coverage, not evidence that any scenario has passed.

| First assertions | Exact failure-contract IDs |
| --- | --- |
| Phase 0B | `PRV-01`–`PRV-10`, `PRV-16`; `LIM-02`, `LIM-04`, `LIM-11`, `LIM-12`; `OPS-05`, `OPS-10` |
| Phase 1 | `CMD-01`–`CMD-06`, `CMD-12`, `CMD-13`; `JOB-01`, `JOB-03`–`JOB-08`, `JOB-10`, `JOB-13`, `JOB-14`; `OPS-03`, `OPS-09` |
| Phase 2 | `CMD-07`, `CMD-09`–`CMD-11`; `PRV-11`–`PRV-15`; `JOB-02`, `JOB-09`, `JOB-11`, `JOB-12`; `LIM-01`, `LIM-03`, `LIM-05`–`LIM-07`, `LIM-09`, `LIM-10`; `RPT-01`–`RPT-04`, `RPT-08`, `RPT-09`; `OPS-02`, `OPS-06` |
| Phase 3 | `EVD-01`–`EVD-10`, `EVD-12`–`EVD-14` |
| Phase 4 | `EVD-11`; `RPT-05`–`RPT-07` |
| Phase 5 | `LIM-08`; `OPS-01`, `OPS-08` |
| Phase 6 | `CMD-08`; `OPS-04`, `OPS-07` |
| Phase 7 | `VIS-01`–`VIS-03`, `VIS-08`, `VIS-09` |
| Phase 8 | `VIS-04`–`VIS-07`, `VIS-10`–`VIS-12` |
| Phase 9 | Native-renderer and hosted-visual regression extensions to earlier allocations; no new individual IDs |

The inventory comprises 88 core and 12 visual individual requirements: 100 total. Phase 9 adds tests to existing IDs rather than new individual cases. The fourteen core combined-failure cases remain applicable; visual combinations are specified separately in the visual workflow. Counts describe the planned register, not test results.

Phase 0B begins `LIM-11`/`LIM-12` against the single-process runner's durable journal. Phase 2 repeats and expands them against PostgreSQL authorization and competing workers. Earlier passing fixtures never waive the later distributed implementation's tests.

## Prospective release scorecard

Every value below is an unmeasured target or provisional authorization default. Save actual observations, workload, environment, uncertainty, and pass/fail evidence before claiming a gate is met. Phase 0A may revise targets once using documented target-user evidence before Phase 0B's paid comparisons are frozen; a missed target cannot silently become a passed gate.

| Measure | Proposed gate or default | First evidence |
| --- | --- | --- |
| Decision usefulness | At least 13/16 preregistered primary task results score 4 or 5 in Phase 0B; repeats do not increase independent task count. Held-out gate is at least 39/48; hidden critical defects cannot qualify | Phase 0B feasibility; fresh held-out confirmation in Phase 5 |
| First useful content | At least 90% of admitted trials commit qualifying content within 60 active seconds; stage counters, echoed briefs and progress commentary do not qualify | Phase 2 instrumentation; integrated Phase 5 gate |
| Standard completion | At least 50% of admitted trials complete within 120 active seconds and at least 90% within 300 active seconds | Phase 2 instrumentation; integrated Phase 5 gate |
| Per-run spending authorization | Provisional default maximum of $2 USD; not a sale price or normal-cost claim; no automatic increase to finish a run | Phase 0B measurement; Phase 2 enforcement |
| Aggregate personal authorization | Configurable per-owner and deployment limits, initially $10 USD per day and $50 USD per calendar month, in addition to each run's limit | Phase 2 concurrent-authorization and boundary tests |
| Hosted spending | Operator explicitly configures funding, owner/deployment quotas, and access before any paid work; no implicit paid access | Phase 6 staging checks |
| First-use comprehension | At least four of five representative users complete without assistance and identify the recommendation or why no choice is established, principal caveat, exact supporting passage/calculation, and next action | Phase 0A prototype; Phase 4 integrated confirmation |
| Accessibility | WCAG 2.2 AA target with automated checks and manual keyboard, screen-reader, reflow, error and dialog review | Begin Phase 1; complete integrated checks in Phase 4 |

Use the canonical product scorecard: measure admitted active time, including retrieval, provider/capacity waits and backoff, while excluding pre-admission queue and user-input waits. Also report wall-clock time from accepted Start, queue delay and user-input delay separately. Deadline attainment uses every admitted trial: failed, cancelled or missing results are misses. Report those outcomes separately too, together with successful-run latency distributions; never calculate an apparently passing deadline gate by dropping failures. A queued run never admitted is reported in queue/admission statistics rather than fabricated as an execution trial. The existing 600-second text active ceiling remains an execution bound, not the normal completion target. Aggregate budgets include reservations and unknown exposure across runs and retries; neither a new run nor a calendar-window transition erases an existing liability. Use stored UTC accounting windows and show reset times in the user's timezone.

## Verification profiles

| Verification profile | What runs | Network and spending rules |
| --- | --- | --- |
| Local fast checks | Python unit and adapter contracts, TypeScript checks, frontend component tests, schema generation comparison | No provider credentials; provider traffic blocked. Use fixtures and injected clocks. |
| Pull request service checks | PostgreSQL, Redis, actual worker and dispatcher processes, API integration, browser smoke tests, deterministic fault barriers | Disposable test resources and local fake provider/retrieval servers only. Never use Celery eager mode as proof of broker or crash recovery. |
| Extended integration and fault checks | Repeated races, process kills, broker/database outages, schema history, storage failures, combined faults and bounded load | Isolated test stack with no provider credentials. Use the full audit service profile; a separately scheduled CI lane remains an operator scheduling choice. |
| Explicit real-provider checks | Small adapter smoke test or an evaluation batch selected by an operator | Opt-in invocation, specified models, dataset, maximum requests, monetary ceiling, and saved usage journal. Never part of routine CI. |
| Hosted pilot checks | Two test identities, deployed session/proxy/storage behavior, restore and capacity exercises | Isolated staging account and synthetic data. Funded provider work requires the same explicit limits as other real-provider checks. |

Default CI must fail closed when a test tries to contact a real provider or when production credentials are present. Deny external network access except explicitly allowed local test services. Mocks complement this boundary; mocks alone do not establish that paid traffic is blocked.

The same boundary covers vision review, image generation, image editing, provider job submission, and status polling. Default checks use synthetic images, local fake image providers/jobs, and isolated renderer fixtures. Explicit real visual tests require request, image-attempt, polling, time, and monetary limits; they must not start merely because an image-provider credential exists.

Fault tests target only resources created for that test stack. Refuse production database/storage targets, scope test cleanup to its own resource identifiers, and preserve failure diagnostics. Do not test by deleting a real account, corrupting user data, sending messages, making purchases, or stopping production services.

## Phase 0 prove the product direction

Discovery and paired evaluation remain required before a validated protocol or product-release decision. Broad construction was separately authorized and is present. Real-user discovery and the funded paired study remain unstarted; the [original preparation record](evidence/phase0-preparation.md) is historical. A sample or prototype must be labeled; it cannot claim real execution or measured benefits.

### Phase 0A validate the decision workflow

**Preparation dependency:** none beyond the supplied product scope; use fictional fixtures until real task examples exist. **Completion dependency:** access to five target users and recent real decisions they can discuss without exposing unrelated private information. Paid providers are unnecessary for this track.

**Deliverables and review**

- Interview five individual owners/operators about a recent comparison of two to four project approaches or supplier offers. Record the actual decision, current workflow, evidence available, decisive constraints, status quo or no-action alternative, and what a better result would change.
- Inspect the formats their evidence actually takes. Record whether supported text/Markdown and manually entered quote items are sufficient; do not assume future PDF or spreadsheet extraction solves an essential launch dependency.
- Build a labeled fictional sample and lightweight intake-to-report prototype now; refine it with consented task examples when available. Ask users to identify the recommendation or why no choice is established, caveat, exact supporting passage/calculation, next action, and conditions that would change the answer. Observe correction of constraints, reopening a saved result, and interpretation of a partial result.
- Rehearse local operator setup and invited hosted use. Validate acceptable setup effort, price of a decision, and wait time. Hosted participants do not need three provider keys; provider credentials and funding belong to the operator for the invited pilot. User billing is outside this release.
- Freeze the first task definition, report acceptance rubric, prospective cost/wait targets, evidence-format decision, and initial paired-evaluation plan. Preserve anonymized findings and unresolved demand assumptions.

**Completion proof:** five interview records, recent-task examples, sample/prototype observations, evidence-format findings, and an explicit decision that the narrow workflow is worth a bounded provider experiment. Target four of five users completing the prototype without assistance and identifying all four report elements; a miss requires a revision or documented scope decision.

**No-go:** the useful task cannot be described consistently, required evidence is unavailable within launch scope, or target users cannot understand or value the result. Revise the workflow before investing in durable application infrastructure.

### Phase 0B compare the protocols

**Preparation dependency:** provisional task/report contracts; schemas, simulator, fixtures, read-only API/UI contracts and safe runner code can be implemented without live access. **Live-study/completion dependency:** Phase 0A's task definition, evidence and rubric decisions, plus explicitly funded provider access and the runner safeguards below. Broad application infrastructure is not a prerequisite.

**Deliverables**

- Create `experiments/` with a small Python runner and exactly 16 primary briefs: four budget/lifecycle-cost, three deadline/feasibility, three missing/stale quote, three preference/status-quo and three misleading/shared-evidence cases. Store manually checked snapshots, randomized review packets and saved nonsecret results. Include two to four options, a feasible status quo baseline, comparable costs, missing information and conflicting requirements.
- Establish minimal contracts in `backend/src/round_robin/schemas/` and `providers/` that can be reused by the application. Implement OpenAI, Claude, and Gemini adapters plus deterministic simulated responses.
- Compare a strong single-model baseline, three independent proposals followed by synthesis and audit, and the full bounded debate path. Select the baseline on separate development examples, use matched briefs and evidence, and record actual work and comparable spending ceilings.
- Predeclare the primary execution for each task, critical defects, reviewer instructions, five-task repeated subset, acceptable extra time/cost and protocol-selection rules. Execute those five tasks three times total per workflow, counting their primary executions: 26 trials per workflow and 78 across all three. Repeats describe variability, not 26 independent tasks or a chance to replace unfavorable originals.
- Fix each trial at at most 24 counted inference requests and its declared monetary cap. The 78-trial envelope is at most 1,872 evaluation requests and $156 USD at a $2/trial cap. Allow at most three separately counted one-request adapter smokes, for at most 1,875 requests including smoke. Smoke funding is separately declared; the arithmetic envelope is not permission to spend. A lower explicitly funded batch, owner or deployment cap always wins; do not raise a daily/monthly cap or schedule fresh batch IDs silently to complete the study.
- Before the first paid runner request, hold an exclusive process lock and append/fsync authorization, stable attempt identity, conservative reservation and policy versions to a durable journal. Disable SDK retries. Enforce per-step, trial, whole-batch, owner and deployment request/money/time policies before dispatch, with one exposure record rather than double-counted nested reservations. On restart, treat authorized-but-unsettled calls as unknown, carry their exposure, resolve the original journal identity and require spare original allowance before any replacement. A corrupt/incomplete journal blocks live execution. This is a single-process implementation of the paid-work invariants, not a waiver until PostgreSQL arrives in Phase 2.
- Blind workflow/provider identity and randomize packets. Save each grade's task/output IDs and versions, randomized arm token, expected-fact/source basis, hard-constraint results, passage-support checks, critical-defect reasons, usefulness score, reviewer, timestamp and adjudication. Two independent reviewers score all workflow outputs for at least eight fixed tasks and every suspected critical defect; retain original scores and disagreement. No-choice outcomes can earn high scores when they correctly identify the decisive gap or failed requirements.
- Prepare authoritative schemas and read-only API/UI contract fixtures for draft, preflight, run snapshot, report/readiness, evidence/calculation and outcome records. Keep public model explanations separate from hidden reasoning and bind every fixture to its selected input/configuration versions.

**Tests and review**

- Adapter contract fixtures cover valid output, refusal, malformed schema, incomplete response, timeout, unavailable model, and absent usage. Prove that SDK retries cannot create hidden requests.
- Unit checks reject missing limits, unknown prices without a conservative bound, exhausted request allowance, and secrets in saved diagnostics. Use simulated adapters for these checks.
- Kill the simulated runner before/after authorization, send and journal settlement; restart with the same batch identity. Test exclusive-lock rejection, truncated journal, aggregate-window rollover, unknown exposure and protected synthesis/audit money/calls/time. No crash may make a request free, refresh a batch allowance or repeat a possibly paid call without remaining authorization. Begin `LIM-11`/`LIM-12` here and expand their distributed tests in Phase 2.
- Run an explicitly limited real-provider smoke test, then the funded comparison. Blind human review checks constraints, passage support, uncertainty, practical usefulness, reproducible costs, latency, and benefit over the baseline. Repeat the declared subset and preserve neutral or worse outcomes.
- Begin the `PRV` and `LIM` coverage manifests with exact scenario IDs. Carry remaining application-only scenarios into their dependent phases.

**Completion proof:** immutable manifest/configuration fingerprints, saved briefs/evidence, model/prompt versions, primary/repeated outputs, request journal, original/adjudicated grades and a written Standard-path decision or scope revision. At least one multi-provider path must score 4 or 5 on at least 13 of the 16 preregistered primary task results and provide a user-valued benefit over the strong single-provider baseline. Report secondary repeat variability and every critical defect without inflating the independent sample. The matched fixed-evidence experiment establishes protocol evidence only; Phase 5 separately tests the actual retrieval/application workflow. Build and release deeper debate only if repeated paired review demonstrates benefit worth its measured additional cost and latency.

**No-go:** no multi-provider path both meets the usefulness target and provides a user-valued benefit over the single-model baseline, a required provider cannot perform the contract, or spending uncertainty has no conservative bound. Success by the single-model baseline alone cannot pass this gate. Allow at most two substantial revisions of the initial feasibility approach; then record an explicit continue, narrow, or pivot decision with evidence. Continuing requires a passing selected direction rather than an indefinite series of unproven feature additions.

## Phase 1 deliver one durable comparison slice

**Dependency:** Phase 0 has selected a useful direction and supplied adapter fixtures.

**Deliverables**

- Scaffold Next.js, FastAPI, PostgreSQL, Redis, migrations, generated contracts, and CI around one vertical slice: draft brief → frozen fixture evidence → labeled sample option comparison → saved report → reopen. Use `backend/src/round_robin/api/commands.py`, `api/events.py`, `database/models.py` and `database/services.py`, `dispatcher/service.py`, and `worker/tasks.py` for this bounded durable comparison.
- Add command idempotency, expected-version checks, owner scoping, an outbox, deployment epoch, execution leases and fences, ordered committed events, and replay. Implement owner-scoped `GET /api/commands/:command_id` for a command whose response was lost; a missing lookup result does not prove an in-flight command cannot still commit.
- Create the thin `frontend/app/` intake, comparison, source-inspection and saved-report views now, using a fake provider. Show two to four approaches, hard constraints, a feasible status quo alternative and comparable cost inputs. Keep the development identity restricted to localhost.
- Autosave a server-side draft without inference or provider dispatch, show pending/saved/error state honestly, and resume it after refresh. Draft mutations use command identity and expected versions; they do not alter a frozen run or restart paid work. Begin keyboard, semantic form/table, focus, error and reflow checks with this slice.
- Implement the blueprint's draft-to-preflight-to-Start contract with fixture execution: preflight binds draft revision, selected input hashes, the shared three-provider recipient set, protocol/models, pricing/policy versions and configuration hash. It expires after ten minutes. Start rejects stale/mismatched preflight and commits the accepted configuration/input snapshot atomically. Set the initial admission deadline to 30 minutes after accepted Start; expiry creates a deterministic no-spend result rather than executing a stale queued decision. This initial queue deadline is distinct from the admitted active-time limit.
- Enforce one consuming run per preflight even for two different command UUIDs. Its ten-minute expiry applies only before accepted Start; the initial 30-minute queue deadline applies only before `first_admitted_at`. Valid confirmation/retry receives its own stable 30-minute admission ticket with unchanged counters/active time/finish pool. Test admission at 20 minutes, two hours awaiting input, then confirmation/requeue; reject expired resumption tickets without reviving work.
- Preserve noncontent paid-command UUID tombstones under the blueprint's retention policy. Replaying an old Start, including after meeting deletion or body cleanup, cannot create a second run; a changed body conflicts and a deleted resource remains inaccessible. Keep sensitive command bodies out of routine logs and retained tombstones.

**Tests**

- Unit-test command validation, idempotency body conflicts, step ownership, event payloads, and allowed state transitions. Generate and compare API/event contracts in CI.
- With real PostgreSQL/Redis and worker processes, inject failure after database commit but before publication, after publication but before marking it, and after a result commit but before acknowledgement.
- Deliver duplicate tasks, expire a worker lease, roll back a transaction, restart the broker, and reconnect the browser. Assert one committed logical result, preserved pending work, and ordered replay.
- Exercise `CMD`, `JOB`, and foundational `OPS` scenarios by exact ID. Verify a public bind cannot enable the local development identity.
- Exercise `CMD-13` with refresh, lost autosave response, interrupted connectivity, conflicting tabs and draft-to-run freeze. Assert that autosave and reopen make zero inference calls and that an unconfirmed edit is never labeled saved.
- Extend `CMD-01`, `CMD-09`, `CMD-10` and `CMD-13` with stale preflight, changed configuration/model/pricing policy, missing shared-recipient permission, initial-queue expiry and old paid UUID replay after deletion/body cleanup. Confirm no new upstream request or hidden input/recipient change. Extend `OPS-03` contract fixtures to unsupported policy/schema versions.

**Completion proof:** the draft-to-saved-comparison journey survives controlled duplicate delivery and a process crash, retains its command identity, and replays committed events without gaps or duplicate cards. A user can refresh the draft, inspect fixture evidence, and reopen the same saved report. Save database assertions, browser evidence and process logs alongside test results.

**No-go:** pending work disappears, a stale worker overwrites a result, command outcome cannot be reconciled, or duplicate delivery produces duplicate committed results.

## Phase 2 implement bounded meetings and recovery

**Dependency:** the durable task and command contract from Phase 1 pass their real-process checks.

**Deliverables**

- Implement only the selected Standard protocol in `orchestration/controller.py` and `orchestration/policies.py`; add reservations in `orchestration/budget.py`, admission in `orchestration/admission.py`, and complete the provider adapters. Deeper debate is built and released only after its Phase 0B benefit gate passes. Record inapplicable disabled-path tests rather than requiring an unproven protocol to ship.
- Preserve frozen briefs, exact selected input hashes, independent proposals, synthesis/audit, cancellation, eligible manual retry and deterministic partial reports in `orchestration/reporting.py` and `reports/read_adapters.py`. The implemented provisional protocol allows one corrective synthesis and independent re-audit after a negative/critical audit only when original limits permit. For material context changes, a fresh preflight and explicit parent-version Start atomically fence an active parent and queue its linked child; draft edits alone do not cancel it. Preserve the existing web, Markdown and JSON output contracts.
- Enforce at most two actual upstream calls for an immutable logical role slot, including retries, repair, eligible role fallback, and manual retry. Record the actual provider on the attempt. Execution retry keeps the step/counters; manual retry increments recovery generation for barrier history, while only an authorized content correction advances stage iteration. All calls remain subject to run limits.
- Permit a preconfigured, visible role fallback only under the failure contracts, within that step's two-call ceiling. Proposals, critiques, and author revisions never switch providers. Keep final audit on a different provider from synthesis or publish the resulting limitation.
- Add minimal inspection in `ops.py` and `reports/measurements.py` now: run/step/attempt identity, status, last progress, failure class, actual coverage, and remaining limits/unknown exposure. Phase 5 completes the diagnostics view; developers must already be able to explain recovery decisions in this phase.
- Atomically reserve against the run, owner and deployment spending limits. Start with the provisional personal defaults in the scorecard; carry unknown liability across retries, linked runs and accounting-window boundaries. Add an operator kill switch that blocks new paid authorizations without erasing reports or unknown exposure. Protect monetary, actual-call and remaining-time headroom for synthesis, audit and deterministic partial-result finalization before scheduling optional work.
- Use the blueprint's single-exposure conversion rules: reserving money for a run or its finish envelope is not a second charge when it becomes an attempt reservation. Apply the same exposure to each relevant cap without adding it twice; convert held amounts to reported/estimated/unknown settlement idempotently and carry unresolved exposure across period changes. Test budget conservation in addition to ceiling checks.
- Validate preflight/configuration identity and the frozen concrete request-contract hash again before paid authorization, enforce the immutable shared-recipient scope, bound every context packet and reject stale/unsupported configuration without hidden provider substitution. Check the first-admission deadline before acquiring run capacity. Stable command identity, input manifests and allowed context transforms survive retries and process restarts.
- Make report completeness and decision readiness validated application contracts. The recommendation, main reasons, decisive tradeoff, largest uncertainty, condition that changes the answer, next action, and essential dependencies must survive synthesis, audit and export. Application rules compute readiness from per-option hard requirements, essential-dependency status, selected recommendation and required actual review coverage; model-proposed enums cannot override a failed/unknown requirement or missing audit. A missing assessment stays null. Instrument accepted Start, admission intervals, first qualifying committed content and terminal/report publication timestamps for deadline attainment and separate wall-clock metrics.

**Tests**

- Unit-test the selected state path, reference routing, stopping reasons, request counts, reservation arithmetic, unknown usage, and partial-report construction. Test both paths only when both are enabled. Prove adapters cannot hide tool follow-up or SDK retry calls.
- Race cancellation against authorization; reservation against parallel dispatch; manual retry against cancellation; and stale worker completion against a replacement. Assert ownership and cost invariants at the database boundary. Retry-versus-settlement and barrier/successor intentions commit atomically; recovering a failed synthesis/audit cannot replay proposals or reset either call or claim ceilings.
- Test one-provider failure, fewer than two successful participants, refusal, malformed output, repair exhaustion, fallback eligibility, circuit opening and a single funded half-open probe. Authentication/configuration failures must not spin through retries.
- Introduce timeout tests using proposed values: provider deadline at most 90 seconds, further bounded by remaining run time and task-soft-limit time after reserving 20 seconds for persistence and five seconds for cleanup; at most three persistence-only attempts within 20 seconds; Celery soft limit 115 seconds and hard limit 120 seconds; lease 150 seconds with 10-second heartbeat; visibility timeout 180 seconds; reconciler interval 5 seconds. Enforce at most three execution claims per logical step independently of the two-paid-call ceiling. These are tuning inputs, not validated production guarantees.
- Test the proposed circuit policy of three consecutive transient failures for the same configured provider/model/credential version in 60 seconds, open for 30 seconds, then one funded half-open probe. Inject clocks for policy tests and verify real process behavior separately.
- Exercise `CMD`, `PRV`, `JOB`, `LIM`, and initial `RPT` scenarios by exact ID, including calls lost after authorization. Unknown external outcome must remain honest and funded rather than being described as recovered output.
- Exercise `LIM-11` and `LIM-12`: race multiple runs against owner/deployment reservations, cross daily/monthly boundaries with unresolved attempts, toggle the kill switch during dispatch, and exhaust money/call/time headroom before synthesis or audit. Assert no overspend authorization, no reset of liability, and honest deterministic partial results. Exercise `RPT-09` against omitted required report elements and missing essential dependencies.
- Expand `CMD-01`/`CMD-09`/`CMD-10`/`CMD-13`, `EVD-13`/`EVD-14`, `LIM-11`/`LIM-12`, `RPT-09` and `OPS-03` for stale preflight/config hashes, recipient mismatch, queued initial-admission expiry, bounded-context rejection, retained paid UUIDs, single-pool exposure conversion and deterministic readiness. These are additional assertions under existing IDs; they do not add fictitious scenario coverage or waive later evidence/privacy tests.

**Completion proof:** three real providers contribute through the selected Standard path in an explicitly funded meeting; deterministic fault cases preserve committed work, cancellation fences, original remaining limits, aggregate reservations and uncertainty. The thin browser journey can start, inspect progress, stop, reopen, and read an honest partial or complete saved result. Save initial scorecard observations without calling an unmeasured target achieved.

**No-go:** a retry resets limits, an uncertain call releases its reservation without reconciliation, cancellation revives, participation is fabricated, or deeper paid debate starts without the selected protocol.

## Phase 3 make evidence and recommendations inspectable

**Dependency:** complete bounded meetings work with checked fixture evidence.

**Deliverables**

- Implement controlled retrieval in `research/fetcher.py`, claim/passages checks in `research/verifier.py`, and reference-aware report validation in `reports/read_adapters.py`.
- Preserve exact passages, applicability, source origin groupings, retrieval/check dates, failed fetches, evidence versions and essential recommendation dependencies. The implemented `research/reuse.py` and `database/claim_models.py` commit immutable claims with successful output and map explicitly selected parent versions into child-scoped records. Start revalidates selections and hashes; stale support remains unchecked and whole-meeting deletion includes the lineage. Retain bounded counterevidence and freshness checks.
- Apply hard requirements and ranked preferences to the two-to-four-option comparison, including the feasible current approach or doing nothing. Explain what could change the recommendation; keep missing decision-changing facts visibly conditional.
- Implement a bounded, deterministic Decimal calculation service for comparable costs and schedules. Accept typed inputs and allowlisted operations, record units/currency, use a common comparison horizon, and expose formulas, assumptions, source inputs, tax/contingency treatment and ranges. Reject nonfinite values, invalid units, excessive precision or operation counts, and implicit currency conversions. It cannot execute arbitrary model-written code. Calculations establish arithmetic, not the truth of quoted inputs.
- Bind evidence to exact selected private attachment IDs, hashes and the frozen brief. Every selected input has the same approved three-provider recipient set in this release; do not pretend to offer per-file per-provider routing. Construct public queries only through user-approved public terms and allowlisted query templates. A model may propose a permitted template/term ID, but cannot dispatch free-form private text. If approved public terms cannot support useful retrieval, retain the gap or request an explicit revised scope rather than guessing a privacy filter can prove arbitrary generated text safe.

**Tests**

- Use local retrieval fixtures for redirects, private addresses, changing DNS answers, excessive size/time, malformed content, copied sources, unavailable search, and stale evidence. Do not send fault traffic to real metadata or private services.
- Assert valid reference ownership, passage existence, calculation inputs/units, evidence snapshot membership, and preserved unverified states. Human evaluation must separately assess actual passage support.
- Seed incorrect shared evidence, three-adviser incorrect consensus, contradictory requirements, and a decisive missing fact. Require correction, conditional output, or an explicit absence of a feasible option.
- Inject adversarial retrieved and peer text; verify that tool authorization, secrets boundaries, budgets, and stopping rules remain controlled by application code. Retain these fixtures for later regression checks.
- Exercise applicable `EVD`, `LIM`, and `RPT` scenarios by exact ID, including follow-up reuse of a failed freshness check.
- Exercise `EVD-10`, `EVD-13` and `EVD-14`: incompatible units/horizons, omitted recurring costs, decimal rounding, invalid numeric limits, changed attachment selection/hash, foreign attachment references, credential-bearing URLs and private text embedded in a proposed search query. Assert reproducible comparable calculations, frozen-input ownership and no unauthorized private-search dispatch.
- Test bounded Decimal scale/magnitude/depth and same-meeting child reuse manifests: approved parent versions receive child-scoped IDs with provenance/freshness, while arbitrary parent/foreign IDs remain invalid. Retain immutable original versions and purge the whole lineage on meeting deletion.

**Completion proof:** every consequential recommendation exposes its essential factual basis or qualification. A reviewer can open the exact passage and see its date, scope, and remaining gaps, and reproduce a cost comparison from declared inputs, horizon and units. Frozen attachments and outgoing search-query scope have inspection evidence.

**No-go:** a working URL is treated as proof, failed retrieval appears verified, copied evidence is presented as independent corroboration, or a known critical dependency disappears during synthesis.

## Phase 4 complete the product and recovery experience

**Dependency:** bounded meetings and inspectable evidence pass their gates. This phase completes the thin interface and accessibility work begun in Phase 1; it does not defer the first usable journey until now.

**Deliverables**

- Complete `frontend/app/`: Project option comparison template, server-saved drafts, labeled saved sample, local operator configuration, invited hosted-use design, Overview, discussion/source inspection, follow-up change summary, and user choice/outcome records. Hosted target users do not configure three provider keys; do not add billing or subscription scope.
- Complete `api/artifacts.py` for supported text/Markdown input and `reports/exports.py` for consistent web/Markdown/JSON/PDF exports. Reconcile pending commands and streams without creating new paid work.
- Distinguish offline, queued, waiting for provider, awaiting input, stop unconfirmed, stopping, limited, and completed states. Show precise recovery actions using structured errors rather than raw exceptions.

**Tests**

- Playwright covers duplicate submission, lost `202`, offline/reconnect, invalid or unavailable SSE cursor, multi-tab version conflict, invalid/stale clarification, retry conflict, stop acknowledgement loss, interrupted upload, failed export, and refresh/navigation during work.
- Retain the implemented hosted session-expiry/revocation tests and repeat them in actual Phase 6 staging; the local development identity stays restricted to loopback. Local authentication fixtures do not establish deployed session/proxy security.
- Target [WCAG 2.2 AA](https://www.w3.org/TR/WCAG22/) for complete create/clarify/stop/reopen/inspect-source/export journeys. Combine automated checks with manual keyboard, screen-reader, zoom/reflow, error identification and recovery, dialog focus/return, and narrow-viewport review. Evidence status cannot depend on color; comparison tables and source inspection remain readable and operable in a linear layout.
- Render short, long, and partial PDF fixtures for visual inspection. Check report identity/version, working links, table breaks, and consistent substantive content across formats. Export retry must invoke zero model calls.
- Exercise corresponding `CMD`, `RPT`, `EVD`, and `OPS` scenarios by exact ID. Test backend limits even when client validation is bypassed.

**Completion proof:** at least four of five representative users complete a comparison without assistance and correctly identify the recommendation or why no choice is established, principal caveat, exact supporting passage/calculation, and next action. Save usability observations, browser evidence, accessibility findings, and export previews. Prototype success from Phase 0A does not substitute for this integrated check. Local observed sessions can use an isolated localhost workstation; do not expose an unauthenticated development service to recruit remote participants.

**No-go:** users see a false saved/stopped/completed status, cannot recover a lost response, cannot reach critical actions with a keyboard, or downloads expose the wrong report version.

## Phase 5 verify the integrated local release

**Dependency:** all prior phase gates pass. Previously introduced failure handling remains continuously tested.

**Deliverables**

- Complete `ops.py` and `reports/measurements.py`, historical schema read adapters, deletion/purge handling, and `operations/recovery.py` procedures for local backup/restore. Add held-out evaluation fixtures and a release evidence index.
- Finish exact-ID coverage across all core failure-contract families. Explain any deployment-only cases assigned to Phase 6 and prevent their absence from being mistaken for hosted readiness. Visual-extension cases begin in Phase 7 and do not block this core local gate.
- Implement the blueprint's startup, shutdown/drain, migration compatibility, rollback, backup/restore and policy-validation entry points. Record the chosen local deployment profile, backup interval/retention, tested restore objective and operator commands. Readiness must fail closed for unsupported schemas/policies or an incomplete restore gate; rollback cannot replay paid work or erase newer accounting.

**Tests**

- Run the complete deterministic suite, historical completed/limited/cancelled fixtures, and extended actual-process faults. Exercise deletion during generation and export; late results or artifacts must not recreate content. Inject crashes around durable external deletion-intent write, database tombstone commit and acknowledgment; successful DELETE requires both durable records.
- Combine faults: lost command response plus duplicate dispatch; cancellation plus timeout and worker takeover; database outage after provider response; broker restart plus expired lease; export failure plus deletion; restore plus deleted records.
- Restore only synthetic backups into isolated resources with workers, dispatcher, and user access disabled. Advance the deployment epoch, isolate old processes/messages, establish and reapply the complete external deletion manifest, and quarantine restored queued/active runs. Missing/incomplete manifest keeps restoration closed. Reconcile post-backup usage and retain uncertainty before permitting explicitly authorized new paid work; never automatically resume restored work. Verify checksums and historical reports before opening reads.
- Run bounded load with simulated upstream latency and faults. Measure visible queueing, provider/application admission, active-time accounting, SSE replay, and export isolation; record the tested workload rather than claiming unlimited scale.
- Freeze 48 held-out task cases: eight budget/cost, eight deadline/constraint, eight missing/stale evidence, eight preference/status-quo, eight evidence/deception and eight clarification/tool/provider-limitation cases. Run the actual application's intake, public-query policy, retrieval, calculation and report path; do not substitute Phase 0B's manually checked snapshots for end-to-end evidence. Predeclare primary executions and any repeat subset, preserve configuration/dataset fingerprints and grade records, and require at least 39/48 primary results to score 4 or 5. Record every critical defect, actual passage support, deadline attainment and conservative cost. Retuning on failures requires fresh held-out coverage before another release claim.
- Evaluate the selected Standard path against every scorecard gate on the recorded workload. Count all admitted failed/cancelled/missing results as deadline misses, retain unknown cost exposure, and report successful-run distributions and queue/input waits separately. A normal path that needs a higher cap or longer wait requires an explicit product decision and fresh evaluation; do not silently raise limits to make release evidence pass. Save separate protocol-only and end-to-end retrieval evaluation records.

**Completion proof:** release evidence links every applicable scenario to passing tests, retained diagnostics, evaluated quality, the chosen Standard path, and measured scorecard results. Include a restore record and the workload actually exercised. State disabled protocols and deployment/visual scope explicitly.

**No-go:** deleted content returns, old records cannot render, costs become unbounded, known critical defects are hidden, or applicable usefulness, publication, accessibility or performance gates fail. A local release does not authorize internet exposure.

## Phase 6 verify a hosted pilot if requested

**Dependency:** local release evidence is complete and an operator-funded, invited hosted pilot is authorized. User billing, subscriptions and end-user three-key setup are outside this pilot.

**Deliverables**

- Implement `api/auth.py`, secure sessions, ownership enforcement, CSRF protection, private storage/downloads, TLS, an SSE-capable proxy, secrets management, deployment configuration, and a pilot runbook.
- Complete hosted backup/restore and deletion-manifest handling in `operations/recovery.py`. Configure bounded admission, request limits, operational alerting, and deployment-specific retention.
- Require explicit operator provider configuration, owner/deployment spending quotas, funded access and invitation policy before paid requests are admitted. The existence of a credential is not paid access. Show participants, recipients and the run authorization limit to invited users; keep provider secrets and operational configuration server-side.
- Freeze the hosted deployment profile and operator runbook against the blueprint: configuration validation, schema/policy compatibility, migration-before-worker startup, graceful drain, rollback, spending-lock control, backup/restore schedule and retention, tested recovery objective and incident entry points. Supply actual implemented commands and their expected safe failure states before exposure; prose promises or placeholder commands do not pass this gate.

**Tests**

- Use two test identities to probe every meeting, run, stream, source, outcome, command, upload, report, artifact, and diagnostics route. Check session expiry/revocation, CSRF, forbidden ownership, signed-link expiry, and rate limits.
- Verify proxy buffering is disabled, streams reconnect correctly, and refresh after sign-in reauthorizes access without replaying paid commands. Health checks must not call a paid model.
- Restore a synthetic staging backup through the same dispatch quarantine, deployment-epoch, deletion-manifest, and accounting gates used in Phase 5. Exercise rollback/migration recovery without modifying production data or automatically resuming restored paid work.
- Repeat bounded capacity tests in staging, including storage outage and slow provider fixtures. Any real-provider pilot smoke test is explicitly funded and excluded from default CI.
- Close remaining exact-ID `CMD`, `RPT`, and `OPS` deployment scenarios and any affected `JOB`/`LIM` scenarios.
- Verify an uninvited or unfunded account cannot authorize a paid call, that one owner's activity cannot bypass deployment quotas, and that the operator kill switch works across all hosted worker instances. Repeat first-use comprehension and scorecard observations in the actual hosted journey.

**Completion proof:** cross-account isolation, deployed session/SSE behavior, private downloads, and restoration all pass in staging. Record the deployment, configuration, retention, tested workload, and remaining limits.

**No-go:** cross-account access, public artifacts, bypassable sessions/CSRF, missing deletion replay, production-targeted fault tests, or automatic unbounded provider spending.

## Phase 7 establish visual artifacts and independent critique

**Dependency:** Phase 5's local durability, limits, deletion, and recovery gates pass. The separate demand gate requires at least three target users with concrete failed visual jobs in the same supported raster domain and a documented assessment of the text-workaround. Save the jobs, unmet needs, proposed domain and acceptance rubric; these are tests to perform, not achieved evidence. Phase 6 is not required for local work; apply its controls before exposing visual features over the internet.

**Deliverables**

- Extend `api/artifacts.py`, artifact persistence, and private storage for safely decoded PNG/JPEG originals, bounded previews, immutable versions, parent lineage, and hashes. Retain immutable canonical V1 bytes and provenance/hashes; raw metadata-bearing upload bytes are temporary and discarded after safe normalization. Distinguish canonical images, derivatives and previews by identity and retention rules.
- Extend `providers/` with capability contracts for actual image inputs, supported formats/limits, image generation/editing, asynchronous jobs, cancellation, and status lookup. Declare only capabilities supported by the selected real API/model; a textual description is not image delivery or visual inspection.
- Add read-only visual critique in `orchestration/controller.py`, `reports/read_adapters.py`, and `frontend/app/`. Every review binds to the actual artifact/version/hash and any region or native element it discusses. Reviewers receive the same immutable image before seeing peer feedback.
- Require two actual vision critics from distinct configured providers, both different from the actual generator provider when known, for full visual review. Self-critique is supplementary. For unknown uploaded provenance require two distinct critics and disclose that origin, without inventing independence from an unknown generator.
- Limit the first visual release to the supported raster domain justified by the demand evidence. Interfaces, diagrams and construction concepts do not become supported merely because a critic can describe their pixels. Add a text equivalent to findings and keyboard-operable image/version inspection; preserve a linear comparison alternative.

**Tests**

- Introduce `VIS-01`–`VIS-03`, `VIS-08`, and `VIS-09` assertions. Verify actual image bytes reach the adapter, feedback cannot attach to a different hash/version, and unsupported image capability never counts as completed visual review.
- Use synthetic PNG/JPEG fixtures for invalid bytes, misleading extensions, excessive dimensions/decoded size, metadata, truncated upload, and interrupted storage. Assert that no partial or unsafe derivative becomes a usable attachment.
- Test independent input snapshots, unavailable critics, generator identity changes, unknown origin, forged regions/references, cross-owner artifacts, deletion during review, and missing/corrupt preview bytes. Reuse applicable core command, evidence, report, and worker cases.
- Perform explicitly funded capability smoke tests for the configured real vision models. A mocked image payload or simulated reviewer cannot pass this real-input gate. Use keyboard and narrow-viewport checks for source image inspection and region feedback.

**Completion proof:** a user can upload an image, inspect independent critiques anchored to its exact bytes, and identify unavailable review. Save capability evidence, outgoing input hashes, critique records, browser evidence, and tests; no paid generation/editing is introduced by this phase.

**No-go:** reviewers discuss an image they did not receive, feedback shifts to a newer version without rechecking, private artifacts leak, or self-review/unavailable reviewers are presented as full independent coverage.

## Phase 8 implement bounded image improvement and comparison

**Dependency:** Phase 7 passes, and the chosen generation/edit API has real capability evidence and conservative pricing bounds.

**Deliverables**

- Extend `providers/`, `orchestration/controller.py`, `orchestration/policies.py`, `orchestration/budget.py`, and `orchestration/admission.py` with generation/edit steps, preservation instructions, critique dispositions, candidate comparison, and independent re-review. Add a separate `worker/image_tasks.py` entry point when blocking image calls need the longer envelope.
- Save `V1` as the uploaded or generated original and at most `V2` and `V3` as two intentional edits. Preserve every version. Feedback on an earlier version remains pending recheck for a new one. Advisers recommend candidates; users may select any version, retain V1, or decline all without overwriting history.
- Enforce a maximum of four dispatched image-generation/edit attempts across the run, including rejected, failed, unknown and repeated submissions whether charged or not. Each stable step retains its two-call ceiling. All actual upstream vision, image and other inference attempts share the 24-call ceiling regardless of billing, with one monetary budget and reserved review headroom.
- Select the visual preset before starting: at most 1,800 admitted active seconds, without increasing the core preset's 600 seconds. Allow one application-wide image-generation job within shared application/provider admission. Persist submission identity before dispatch and retain ambiguous usage reservations through crash, cancellation, and restore.
- Use asynchronous job lookup/cancellation only where the actual API supports it; allow at most 60 status polls per run. Otherwise use a separate image-worker queue and isolated broker namespace/database: request at most 600 seconds, soft task limit 630, hard limit 660, lease 720, ten-second heartbeat and visibility 900 seconds. Sharing a namespace requires consistent visibility; separate queues alone are insufficient. Bound the call by remaining run/soft-task time with 20 seconds for persistence and five for cleanup. Ordinary text settings remain unchanged. Fault tests must validate these proposed values.

**Tests**

- Introduce `VIS-04`–`VIS-07` and `VIS-10`–`VIS-12` assertions and repeat Phase 7's artifact/critique cases against edited candidates. Check synchronized before/after views, exact parent hashes, preserved requirements, resolved/persistent/regressed findings, and explicit selection or decline.
- Combine generation failure, refusal, lost response, schema repair, retry, and exhausted review allowance. Assert at most four image submissions, two calls per step, 24 upstream inference attempts regardless of billing, three design versions, two intentional edits, and original money/time limits. A new version, fallback or manual retry cannot reset counters. Image retries require an eligible active run; terminal continuation uses a linked run. Refusal never triggers evasive fallback.
- With actual worker processes and fake image jobs, kill execution before and after submission, lose job-ID acknowledgments, duplicate status responses, exhaust polling, cancel, delete, and restore a backup. Never blindly resubmit a possibly accepted generation or equate local cancellation with confirmed remote termination.
- Test real asynchronous recovery only for APIs that actually expose it. For a blocking provider, test the isolated image-worker envelope, heartbeat/lease takeover, bounded same-response persistence, and unknown usage. Ensure a slow image job does not starve text status reads or independent export capacity.
- Test insufficient review headroom and critics sharing the actual generator's provider. A candidate can remain available with incomplete review, but cannot be labeled fully reviewed or automatically declared better. Save explicitly funded real generation/edit smoke evidence separately from deterministic CI.
- Before releasing raster automatic improvement, compare generation alone, one-critic revision and multi-critic revision on matched briefs and initial candidates with comparable image-operation budgets. Predeclare domain criteria, hard-constraint defects, regression rules, reviewer instructions and acceptable extra cost/wait. Blind human review evaluates actual resolved defects, regressions, preference and decline; preserve neutral or worse results. Automatic improvement requires a user-valued benefit over the simpler paths and passing artifact/privacy/recovery gates. A single successful journey or model vote cannot pass this quality gate.

**Completion proof:** a bounded real generation/edit journey produces immutable candidates, actual independent visual feedback, an honest before/after comparison, and user selection or decline. Matched blinded human evaluation establishes benefit for the enabled raster domain within declared cost/wait limits. Deterministic faults demonstrate persisted attempt/job identities, shared budgets, bounded polling, correct partial outcomes, and no deleted-content resurrection. A raster release that passes these gates does not require Phase 9.

**No-go:** a fourth version or third intentional edit appears, failure resets image/model budgets, a timeout causes blind duplicate generation, review is unfunded but claimed complete, comparative human evaluation fails to establish useful improvement, or a candidate is accepted solely because advisers agreed it looks better.

## Phase 9 verify optional native design outputs

**Dependency:** an explicit native-output demand decision and Phase 7's shared artifact/review contracts. This is a separate optional track; it does not gate the Phase 8 raster release. Any enabled native revision loop must satisfy Phase 8's bounded-iteration and human-evaluation contracts, but native review does not require implementing raster generation. Public native delivery additionally requires Phase 6 controls extended and verified for every new route and artifact type.

**Deliverables**

- Add isolated native renderers with versioned source, render configuration, viewport, and output hashes. Preserve editable SVG for diagrams and native components/styles for interfaces. Extend `reports/exports.py` and `frontend/app/` to deliver the selected version with its source and review history where supported.
- Run deterministic structure and browser checks alongside actual rendered-image critique. A screenshot cannot establish navigation, responsive behavior, semantic diagram correctness, or accessible interaction. Bound renderer counts, CPU/time, memory, decoded pixels, output bytes, and network access even when rendering incurs no provider charge.
- Define domain-specific evaluation fixtures for each enabled native output type, rather than promising diagrams and interfaces together. Farm/building output remains a concept or review brief with stated site dependencies; visual quality does not certify structural adequacy, local compliance, or construction readiness.

**Tests**

- Extend applicable `VIS-01`–`VIS-12` and core security/job/limit/report cases to each native renderer. Test malformed source, renderer hangs/crashes, scripts, external-resource attempts, oversized output, missing fonts/assets, inconsistent viewport, source/render hash mismatch, and changed content during export.
- For interfaces, test actual browser interactions, narrow viewports, keyboard/focus behavior, and the WCAG 2.2 AA acceptance checks used for the product. For diagrams, test source validity, editable delivery, labels/relationships, clipping, and legibility. Re-render from the recorded source/configuration and verify the candidate being reviewed is the one delivered.
- Evaluate initial versus revised versions with blinded human review, fixed brief criteria, hard-constraint failures, regressions, and user preference/decline. Retain neutral or worse outcomes; do not establish improvement through model votes or aesthetic scores alone.
- Before public visual use, test two-user access for originals, derivatives, critique regions, previews, candidates, selected files, and source bundles; session expiry; signed downloads; sanitized exports; deletion; and backup restoration. Use the complete Phase 6 boundary and account for already delivered bytes rather than promising recall.
- Exercise visual combined-failure cases from the workflow and bounded load with slow generation, concurrent review, export, cancellation, and storage outage. Record the tested workload and preserve the application-wide image-job limit. Paid image/vision operations remain subject to Phase 8's limits; deterministic rendering is separately bounded, not unlimited.

**Completion proof:** each enabled native output type has source/render lineage, passed functional/structure checks appropriate to that type, human-reviewed visual evaluation, reproducible failures, and correct selected-version delivery. A hosted native release also has passing extended Phase 6 evidence. Do not reopen an already justified raster release solely because an optional native type remains unimplemented.

**No-go:** attractive renders conceal broken behavior or incorrect labels, source and delivered render diverge, rendering escapes its resource/network boundary, private visual files bypass hosted controls, or professional adequacy is inferred from appearance.

## Implemented verification commands

Run from the repository root without production provider credentials:

```sh
uv run pytest backend/tests -q
pnpm contracts:check
pnpm typecheck
pnpm --filter @round-robin/web test
pnpm verify
```

The authoritative combined profile is:

```sh
pnpm audit:full
```

Prepare the disposable PostgreSQL/Redis services and native-renderer image, and stop the development supervisor first, as described in the [operator runbook](OPERATIONS.md#reproduce-verification). This profile runs actual services, browser/native checks and source-instrumented coverage. It requires strictly greater than 95% line and branch coverage in every service group, binds production source and verification inputs to the captured manifest, includes unexecuted production files and refuses stale or changed captures. Each invocation uses a new output directory. The services CI job runs this profile; a configured job is not evidence that a hosted CI run passed.

For narrower diagnosis, `pnpm verify --services --native` runs the integrated service/native verifier without replacing the full coverage gate. The [fresh audit report](evidence/exhaustive-audit/REPORT.md) records actual command outcomes, the source state and remaining limits when final verification completes. No pending invocation is described as passed here. Real-provider studies remain separate, explicitly funded and bounded by dataset/model/request/money authority; default checks never start paid work merely because credentials exist.

## Evidence to attach to each implementation review

- The phase deliverable and exact failure-contract IDs implemented.
- Changed module paths and any database/contract migration.
- The successful behavior and expected behavior when the dependency fails.
- Unit, integration, contract, browser, fault, security, accessibility, or evaluation evidence appropriate to the change.
- A reproducible fixture and database assertions for a race or recovery fix.
- Proof that requests, retries, repair, fallback, and unknown usage respect original limits.
- Visible user recovery states and any remaining restriction.
- Sanitized logs and report/export previews when those behaviors changed.
- Confirmation that default CI used no real-provider credentials or traffic.
- A specific go/no-go result, unresolved defects, and the next gate; never label planned or simulated checks as real-provider or production validation.
- The blueprint contract version and linked backlog slice, input/configuration fingerprints, relevant state transitions and the exact saved artifact demonstrating the user-visible result.
- Evidence that stale preflight, initial-admission expiry, old paid UUID replay, bounded context and single-pool exposure conversion obey the chosen policy when touched by the change.
- For an evaluation or release change: immutable task/output IDs, primary-versus-repeat accounting, grade/adjudication records, admitted-trial deadline counts, conservative batch/aggregate spending and the distinction between fixed-evidence and actual-retrieval modes.
