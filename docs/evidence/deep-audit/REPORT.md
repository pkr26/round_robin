# Independent deep implementation audit

Recorded 2026-11-16 15:16 UTC. Local workspace audit; no Git repository was available.

**Verdict: substantial implemented functionality, with repaired defects and three confirmed incomplete planned workflows. The code cannot be certified as fully correct or as completing all eight phases.** The final verification passed **790 unique Python tests, 28 browser tests and 3 JavaScript coverage-tool tests**. No paid provider calls, user outreach or deployment occurred.

## What was actually reviewed

The audit restarted from the source and contracts with three independent reviewers covering runtime/accounting, evidence/security/operations, and frontend/visual/native code. The primary reviewer covered core APIs, configuration, database/migrations, operator paths, isolation and cross-review. Reviewers challenged existing assertions, built failing regressions, and cross-reviewed fixes; a previous passing suite was not treated as proof.

- **121 handwritten source files, 17,604 lines**: whole-file reads and explicit review entries for every inventoried production function, method, nested function, callback and class (1,281 symbol entries).
- **78 test-code files, 10,171 lines**, plus **14 configuration files**: reviewed at their current SHA-256. There are no missing current file or explicit symbol review entries.
- Generated contracts/client types were checked against their generators; locked Python/JavaScript dependencies were verified. Dependency internals, compiled/vendor/cache output and historical prose are outside the handwritten line-review claim. Generated audit records and private local tooling are also separate from the application source inventory.

This is a static review of each source symbol plus measured execution evidence. It does **not** mean each function has an individual unit test, each branch executed, or every possible interleaving was explored. The per-symbol matrix makes that distinction explicit.

Evidence: [file inventory and hashes](inventory.json), [function/class/callback matrix](functions.csv), [full invariant/test mappings](functions.json), [reviewer ledgers](reviews/), [reviewed source archive](../../../.local/deep-audit/reviewed-source.tar.gz).

## Confirmed incomplete implementation

These are engineering gaps, separate from funded studies or deployment acceptance. They remain open after this audit; adding those workflows would be feature implementation.

| Priority / obligation | Observed implementation | Required next engineering work |
| --- | --- | --- |
| High — RPT-03: one funded correction and independent re-audit | The audit branch in [controller.py](../../../backend/src/round_robin/orchestration/controller.py) finalizes both positive and negative audits. No production path increments the substantive stage iteration. Negative findings are retained in a partial/conditional result. | Add one bounded correction/re-audit iteration when every required resource remains available, without renewing limits, replaying old work or classifying successful negative audits as execution failures. [Independent confirmation](reviews/runtime-contract-gaps.json). |
| High — claim/version persistence and EVD-14 selected parent reuse | [claims.py](../../../backend/src/round_robin/research/claims.py) defines claim-ID/reuse helpers with direct tests, but no production pipeline callers. Database/API workflow does not persist a child reuse manifest binding selected immutable parent versions, provenance and freshness. Fresh child-supplied inputs do exist. | Integrate server-issued claim IDs and immutable claim versions, then child-scoped selected-parent import with hash/version/owner/freshness checks and deletion fences. |
| High — LIM-09: atomic replacement of an active parent | [follow_up](../../../backend/src/round_robin/api/drafts.py) rejects active parents and saves a linked draft for terminal parents. Stop → draft → preflight → Start remains a series of separate actions. | Under explicit current authorization and limits, atomically fence/cancel the active parent and create exactly one linked queued child; preserve idempotence and deletion races. |

The [acceptance register](../../ACCEPTANCE_BLOCKERS.md) and [README](../../../README.md) now disclose these gaps. No scenario or phase acceptance has been promoted: the existing scenario inventory still reports 99 partial assertion sets and one pending obligation (EVD-07).

## Defects repaired and regression-tested

The [finding index](findings.csv) links every reviewer finding group to source and regression evidence. Some groups overlap because reviewers coordinated or independently caught a follow-up regression; they are not a count of distinct bugs.

| Area | Reproduced failures and resulting fixes |
| --- | --- |
| Spending and provider outcomes | Refusals, truncated/empty/malformed results and timeouts dropped observed usage or reconciliation request IDs. Invalid/nonfinite usage was accepted. Ambient Decimal precision/traps could round boundary amounts or break finish transfers. A truthy string could assert final settlement. Preserve known receipts, validate certainty/usage types and isolate exact monetary contexts. |
| Worker and lifecycle correctness | Stale iteration/generation delivery could authorize or finalize newer work; unrelated outbox families could starve dispatch; late definitive not-sent receipts could not reconcile unknown exposure. Strengthen generation/stage/authority checks, owned-family selection and evidence-based settlement. Clock regression now preserves known time, permanently fences additional active allowance and still permits honest partial finalization/cancellation. |
| Evidence and saved reports | Successful research hostnames violated typed origin-group IDs; untrusted NUL metadata could reach storage; selected-upload aliases could be overwritten. Saved report row identity/version/cutoff, cost subtotals and readiness references were insufficiently bound. An audit failure could disappear when synthesis omitted the assessment. Direct saved-source reads bypassed row/text/hash scope validation, and malformed committed-event envelopes escaped safe report handling. Validate before commit, preserve scoped support and bind public results to exact saved evidence/calculations. |
| Hosted identity and privacy | Concurrent first logins raced owner creation. Accepted origin variants failed legitimate CSRF/CORS; malformed origins and OIDC shapes escaped controlled handling. Account expiry/change and late requests could leave/repopulate private frontend content. Canonicalize origins, serialize owner creation, sanitize OIDC errors and fence asynchronous private state by access generation. |
| Visual/native/upload behavior | Image JSON shapes escaped typed failures; generation omitted targeted user intent; renderer receipts could contradict action/assertion/network outcomes. Revision conflicts and disconnects could leave pending commands, holds or orphan artifacts. Native-source reopening retained stale text. A Playwright timeout could beat the outer deadline timer and be misclassified as a generic renderer failure. Validate receipts/counters/shapes, settle conflicts, clean only uncommitted artifacts and clear stale reads. |
| Durability, research and operator isolation | SQLite backups could span inconsistent physical snapshots; a first fix needed compatibility with an already open transaction. PostgreSQL test/restore URLs allowed routing overrides. DNS thread-start failure leaked capacity in both provider and research guards. SQLite foreign keys were unenforced. Use coherent conditional transactions, strict disposable destination guards, released DNS permits and enforced foreign keys. |
| Evaluation and verification integrity | Altered manifests/invented trials, journal bool/float substitution, incomplete grade provenance/chronology and dropped negative audits weakened evaluation claims. Supervisors accepted rival readiness or ignored exited children; cleanup races and inherited production targets invalidated isolation. Harden immutable approval/grade records and actual owned-process checks; normalize safe failure paths. |

Every repair is tied to a regression in the ledgers. Follow-up checks also caught audit-tool weaknesses: file review must not imply explicit symbol review; failed runs must not leave a successful status; stale coverage must not be reused. The final records are bound to artifact and code hashes in [evidence-integrity.json](evidence-integrity.json). [Five negative evidence-tool checks](evidence-guard-checks.json) also reject missing coverage, changed/omitted source, failed verification and absent per-symbol review without overwriting existing reports. A later focused coverage combine temporarily replaced full-run data; that diagnostic is retained as discarded, and the complete final run was repeated after preserving original shards and a separate combined database backup.

## Attempts to break the implementation

Tests exercise actual HTTP entry points as well as services. Provider mocks substitute network responses while retaining request construction, parsers, usage handling, authorization and database commits. They cover malformed/missing/wrong-type envelopes, invalid JSON, refusals/truncation, connection/read/protocol failures, observed usage with failed content and ambiguous charge outcomes. OIDC tests mock its real client boundary. Image journeys use actual configuration/factory/adapter paths with MockTransport, exact parent pixels/history, stale revisions, UUID replay, and cancellation during send.

Other targeted attacks include wrong-owner and wrong-run reads/writes, changed/consumed preflights, stale/deleted inputs, shared request ceilings, finish reservations, Decimal boundary values and caller traps, negative/regressing clocks, stale worker tokens/generations, duplicate publication/redelivery, receipt reconciliation, cross-run references, mismatched report versions and corrupt artifacts. Source pagination seeds both foreign-run and foreign-owner rows and snapshots persistent state to prove reads cannot enqueue or spend. Seven independent saved-evidence corruptions are exercised through actual source/report/export routes and the worker packet gate; snapshots bind row scope and damaged support removes public readiness while preserving run inspection. Unrelated corrupt evidence outside a saved report’s referenced cutoff scope does not invalidate its history. Logout tests replay the revoked cookie. Hypothesis runs bounded command state-machine traces (20 examples, up to 20 steps each) and generated visual authority cases.

Research tests mock only getaddrinfo for the production DNS wrapper, attacking public/private mixed answers, rebinding redirects, capacity exhaustion, native resolver delay/exceptions and thread launch failure. Loopback TLS tests exercise real pinned HTTP transport. Browser tests exercise user actions, stale native reads, account changes and asynchronous privacy races. Native tests execute the actual Docker/browser image, clicks/fills/presses, assertions, narrow viewports, network blocking, resource-limit inspection and forced deadline failures.

## Actual final verification

| Check | Actual result |
| --- | --- |
| Default Python unit, contract, fault and state-machine suite | 762 passed; 28 service-gated cases deferred to explicit profiles |
| Actual PostgreSQL/Redis/process integration profile | 17 passed; 6 native cases deferred to the native profile |
| Loopback TLS evidence retrieval profile | 5 passed |
| Actual native Docker/browser profile | 6 passed |
| Unique executed Python tests across those profiles | **790 passed**; all 28 default-profile skips executed in explicit profiles |
| Production browser tests | **28 passed** |
| JavaScript coverage conversion regressions | **3 passed** |
| TypeScript, production Next build, generated contracts/client drift, scenario manifest consistency | Passed |
| PostgreSQL upgrade → head/check → downgrade → base → re-upgrade/check in isolated schema; backup/restore/process/broker faults | Passed within the explicit suites |
| Frozen Python/JavaScript locks and image/source script checksum | Passed |
| Development restart | `/health`, `/ready`, `/live` returned 200; simulated mode, paid access unconfigured |

The read-route tests were rerun after independent review, with three additional saved-support/readiness regressions; they are already included in the 790 unique total. The final complete run includes those stronger assertions. Deprecation warnings from the Starlette/Authlib HTTP client compatibility layers and pnpm's Node URL usage remain visible in raw output; no test failures were hidden.

Raw records: [final commands/stdout](checks.json), [coverage/build/restart driver](audit-run.json), [frozen-lock/image checks](metadata-checks.json), [focused read-route verification](post-checks.json), [health/restart](restart.json). Earlier attempts are retained as `*-attempt-1/2/3/4/5/6.json`; the first combined attempt failed on a verifier import/bootstrap issue, which was fixed and tested. Attempt 4 exposed an intermittent native deadline classification defect; the final run follows its fix and deterministic regression. Attempt 5 passed before the subsequent saved-support read-boundary hardening; the final run includes that fix and the malformed committed-event error guard. Earlier passing attempts are historical rather than the final evidence.

## Measured execution coverage and its limits

| Instrumented source | Lines/statements | Branches |
| --- | ---: | ---: |
| Python application, experiments, scripts and migrations | 9,374/10,528 (89.04%) | 2,638/3,520 (74.94%) |
| Browser source-map approximation for shipped application JS | 2,432/2,765 (87.95%) | 860/951 (90.43%) |

Python still has **1,154 statements and 882 branches without recorded hits**. [Python coverage JSON](python-coverage.json) retains exact missing lines/branches and function summaries. HTML is available privately at [coverage/html/index.html](../../../.local/deep-audit/coverage/html/index.html).

Browser mappings verify bundle and original-source checksums and exclude framework/dependency code. They are incomplete after Next compilation/minification. Across files with browser source-map coverage, the separate frontend AST inventory contains 453 functions; 104 have a matched mapping, 349 have no matched mapping, and 29 mapped functions show zero hits. Server-only/unavailable frontend files and their functions are listed separately and excluded from those totals. Native JavaScript and other Node helper functions are reviewed/tested but are outside the browser mapping denominator. Istanbul's compiled function entries are a different denominator and must not be treated as all source functions. [Browser coverage](frontend-coverage.json) retains these limits and unmapped entries.

The [coverage gap matrix](coverage-gaps.json) labels no-recorded-hit and unmapped symbols explicitly. Some are protocol/abstract declarations or unused helpers; others need more scenarios. SIGKILL fault tests can lose in-memory worker traces. The Python preview server launched from Node also lacks trace collection despite actually serving browser tests. A missing recorded hit therefore does not prove nonexecution. Conversely, entering a function does not establish every branch or semantic invariant.

## Remaining acceptance and risk

A finite audit cannot establish all possible edge cases or real-world answer quality. The three implementation gaps above remain actionable. Further fault combinations, wider load/soak, manual screen-reader/accessibility work, actual hosted staging/TLS/OIDC/storage recovery, real provider compatibility/cost/latency, target-user comprehension and funded text/visual studies remain open in the [acceptance register](../../ACCEPTANCE_BLOCKERS.md). EVD-07's copied/false-evidence counterverification obligation is still pending. Unsupported PDF font coverage remains an explicit export failure with saved JSON/Markdown alternatives.

The safe local simulation remains available. Additional paid execution, user contact or release acceptance was neither inferred nor performed. These audit results support the named tested invariants and recorded fixes; they do not establish that every planned phase or every failure obligation is complete.
