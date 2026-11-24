# Fresh exhaustive implementation audit

November 16, 2026 · Round Robin · Local engineering verification

**Accepted:** the clean full audit passed on November 16, 2026 at 17:57 UTC. All **18 service/package groups exceed 95% for both lines and branches**. Established first-party defects are repaired and regression-tested. **27 upstream OS CVEs remain open** in the native container because vendor fixes are unavailable; this is not an all-vulnerabilities-cleared or release-acceptance claim.

## Scope and independent review

The user requested a fresh, deep implementation/security/data-leak audit, multiple adversarial reviews, repairs, more than 95% coverage in each service, and updated documentation. Previous audit findings, conclusions and coverage were explicitly discarded as evidence. Existing tests remain regression checks; the new review derived its challenges from current source, current product requirements and observed behavior.

The initial implementation was hashed and archived before repairs. Four reviewers read the application, runtime/provider/worker, evidence/identity/operations, and visual/frontend/native surfaces. Their combined ledgers cover all **118 executable files** in the final service inventory. The coverage manifest separately binds **155 verification inputs**, including tests, dependency locks, configuration, contracts, fixture data, instrumentation and container/CI definitions. Third-party package internals were dependency-scanned rather than represented as first-party reviewed source. This workspace has no Git metadata; the before inventory, source archive and final hashes provide the change evidence.

The review included multiple independent passes:

1. Source and requirement tracing: owner/session/run/version boundaries, authorization before sends, original financial/time/call ceilings, immutable provenance, persistence and recovery.
2. New adversarial execution: cross-owner identifiers, malformed contracts, request replay, callback interleavings, stale UI responses, forged hashes and receipts, symlinks/FIFOs, partial writes, failed commits, invalid clocks, unpriced usage, expired leases, cancellation and concurrent admission.
3. Cross-review outside each author's assigned area: evidence reviewed runtime accounting; visual reviewed hosted identity and response policies; runtime reviewed API/parent authority and image workers; root reviewed claim/reuse persistence and frontend behavior.
4. Before-fix replay against the snapshot taken at this audit's start, using isolated paths. New tests reproduced the original behavioral failures; prior audit findings were not used for these comparisons.
5. An independent audit of the verifier and coverage gate, including adversarial malformed/stale evidence. A test-input race and a Protocol-exclusion compatibility error were detected in earlier final attempts. A subsequent clean run exposed an intermittent native-editor test failure: independent review and a deterministic delayed-effect test established an edit-before-load-effect ordering defect, which was repaired before another full capture. These attempts remain failed records; acceptance uses a new capture from unchanged inputs.

Detailed findings, fixes, reproduction names and review hashes are preserved in the [application review](root-review.md), [runtime review](runtime-review.md), [evidence/identity review](evidence-review.md), [visual/frontend review](visual-review.md), and [independent verifier review](verifier-review.md). Diagnostic counts in those ledgers are explicitly historical within this session; only the final records linked below establish current acceptance.

## Repairs

The first-party defects established by this fresh review have been repaired. The ledgers distinguish security failures from reliability defects and missing required workflows; they do not imply every finding was an externally exploitable vulnerability. The native image still has upstream OS advisories without vendor fixes, recorded below as open rather than falsely marked repaired.

| Boundary | Repairs and consequential verification |
| --- | --- |
| Private evidence and outbound disclosure | Reject encoded-query credential concealment before DNS; bind selected records to exact owner/run/version/hash; preserve only explicitly selected parent provenance; downgrade time-sensitive reused support; restrict image decoders before untrusted parsing. |
| Identity and browser data | Consume failed callback state, fence overlapping OIDC responses, bound anonymous login storage, reject damaged/inconsistent session metadata, clear private UI on revoked access, and recheck account generation after asynchronous hashing/blob/text reads. |
| Native editor state | Capture source-loading authority when the candidate opens, before its editable DOM commits. A late load cannot overwrite an edit made either before the load effect starts or while its request is pending. Deterministic tests assert a visible, enabled editor, the actual local edit and its preservation. |
| Private files and downloads | Enforce namespaces, hashes, regular-file/size checks and no-follow opens; reject linked ancestors/FIFOs; clean failed-publication artifacts; preserve route sandbox CSP alongside hosted policy; make deletion manifests private, locked and durable. |
| Money and dispatch | Retain the highest observed uncertain exposure; keep unsupported cached usage unknown; settle proven-unsent heartbeat startup failures; avoid foreign outbox cleanup; release expired capacity without forgiving financial exposure; pin the concrete live request contract. |
| Workflow completeness | Implement one correction/re-audit opportunity within original limits, immutable claim versions and explicit selected reuse, and atomic active-parent fencing plus child Start. Preserve original parent history and accounting. |
| Races and failure recovery | Serialize cross-owner export capacity, reject expired finalization, purge failed commits, validate batches before insertion, dispose failed restore resources, preserve quarantine, reject invalid clocks and unsafe journal/study locks. |
| API and local operations | Bound body-read time, validate fixture origins, reject invalid database initialization before global mutation, and validate local startup targets/ports before migrations. |
| Dependencies | Upgrade locked Pillow 12.3.0 to 13.0.0 after fresh dependency findings; rescan Python and Node dependencies. |
| Native container dependencies | Scan the actual immutable renderer image, remove unused build-time npm and multimedia components, and rebuild a minimal Chromium runtime on a pinned supported newer base. Keep package metadata and rescan the resulting image; dependency manifests alone do not cover this boundary. |
| Verification trust | Capture original frontend/native source, disable stale transform reuse in coverage mode, reject stale/malformed/incomplete evidence, bind tests/configuration as well as production source, and enforce a separate strict line/branch floor for every service group. |
| Verification cancellation | Keep exclusive capture ownership through bounded cleanup of the interrupted command and its owned process group. Exercise actual child/grandchild cleanup, TERM/KILL escalation and an unrelated sentinel. Handle Darwin's transient zombie-group permission errors through bounded reaping; persistent denial remains a failure. |

The new linked-reuse browser journey uses the actual simulated API, workers and PostgreSQL. It selects exactly one parent source, refreshes preflight, starts a child and checks fresh child identity with retained parent bytes/hash/timestamp, unchecked freshness, no implicit imports and unchanged parent history. Active replacement also has a real PostgreSQL two-command race: one child commits, the loser receives 409, only one cancellation/fence/finalize intent exists, and prior unknown exposure remains counted.

## Accepted verification

Accepted invocation (all eight stages passed):

```sh
ROUND_ROBIN_NATIVE_RENDERER_IMAGE=round-robin-native-renderer:exhaustive-audit \
  pnpm audit:full --output .local/exhaustive-audit/verified-final
```

| Profile | Accepted result |
| --- | --- |
| Offline Python unit/contract/adversarial suite | **1,854 passed**; 31 service/TLS/native cases deferred to their explicit profiles below |
| Actual PostgreSQL/Redis/Celery/process/broker-restart checks | **19 passed**; seven native cases deferred to their Docker profile |
| Actual loopback TLS retrieval | **5 passed** |
| Actual native Docker rendering/security/source binding | **7 passed** on the repaired immutable image |
| Connected/sample/hosted/visual browser journeys | **29 passed**, including explicit selected parent reuse |
| Original-source frontend unit tests | **205 passed** |
| Original-source native JavaScript tests | **45 passed** with controlled browser imports; actual Docker checks are separate above |
| Frontend coverage-tool regressions | **3 passed** |
| Contracts/build/types/scenario inventory | JSON/OpenAPI and generated-client drift checks, TypeScript, production build, evaluation validation and scenario checks passed |
| Migrations and replay | Alembic upgrade/check passed; the same simulated study batch ran twice without new authority |

This is **1,885 executed Python tests** across the explicit profiles, plus the separately counted JavaScript/browser checks. All 31 initially gated Python cases ran in their required profile; the initial skips are not missing final execution. Real paid-provider calls: **zero**. Local environment: Python 3.12.14, macOS arm64; renderer containers ran linux/arm64. Hosted CI and amd64 execution are not claimed.

The raw test records retain 16 offline warnings: two upstream HTTP-client deprecations and expected `runpy` warnings from CLI-entrypoint tests. They are not hidden or represented as zero warnings.

Accepted machine evidence: [runner stages](accepted/audit-checks.json), [all verification commands/output](accepted/checks.json), [per-service coverage](accepted/service-coverage.json), [source and verification-input hashes](accepted/source-manifest.json), [Python coverage](accepted/python-coverage.json), [frontend source coverage](accepted/frontend-source.json), and [native source coverage](accepted/native-source.json). Per-stage logs are stored alongside those records. [Review coverage](review-coverage.json) accounts for every executable file, and the [change inventory](change-inventory.json) compares current bytes with this audit's initial snapshot without inventing Git history.


## Service coverage

The gate requires **strictly more than 95% lines and branches in every group**, not an aggregate repository average. It includes unexecuted first-party production files, migrations and operational tooling. Python reports use branch instrumentation including subprocess execution. Frontend reports use Istanbul on original TypeScript/TSX; native reports instrument the original JavaScript. Source hashes and counter maps must match. Generated contracts, configuration and JavaScript build/instrumentation helpers are reviewed and hash-bound verification inputs rather than application coverage targets. The JavaScript helpers receive functional verification; they are not a separately coverage-gated service. Only verified nonexecutable Python Protocol declarations and their contiguous nonexecutable trailing lines use the existing omission; no executable exclusions or lowered thresholds were added to reach the floor.

| Service/package group | Lines | Branches |
| --- | ---: | ---: |
| api | 98.92% | 97.91% |
| application | 98.77% | 97.02% |
| database | 99.75% | 95.45% |
| dispatcher | 99.35% | 98.21% |
| experiments | 99.16% | 97.80% |
| frontend | 100.00% | 96.95% |
| migrations | 100.00% | 100.00% |
| native_renderer | 100.00% | 100.00% |
| operations | 99.59% | 98.31% |
| orchestration | 99.64% | 98.92% |
| providers | 98.80% | 96.43% |
| reports | 100.00% | 99.32% |
| research | 99.76% | 99.06% |
| schemas | 100.00% | 100.00% |
| security | 100.00% | 100.00% |
| tooling | 100.00% | 98.82% |
| visual | 99.29% | 96.75% |
| worker | 99.52% | 97.60% |

Lowest group: **98.77% lines; 95.45% branches**. The frontend additionally passed its statement/function floors (98.53% / 99.79%); native source line and branch coverage are both 100%. All 118 executable source files are represented, and both manifest attestations passed.

## Dependency and static checks

Python and Node lockfile scans found no known advisories after the Pillow upgrade. An additional immutable-image scan found vulnerable build-only npm packages and unused multimedia dependencies bundled into the native renderer image. The rebuilt Chromium-only image uses a pinned supported Ubuntu 26.04 base and preserves the original pinned Node/browser binaries. Unused npm, other browsers, multimedia components and the base's unused Pebble binary are absent; OS package metadata is retained for scanning.

The fresh container scan fell from **671 to 40 package/advisory matches**. The final image has **zero high/critical findings, zero fixable findings reported by the vendor feed, and zero findings in its two runtime Node packages**. The remaining **40 OS matches cover 27 distinct CVEs: 34 medium and six low matches**, with no fixed version in the Ubuntu feed. They remain open. An independently built, supported Debian 13 alternative passed the native tests but had 450 OS matches including high/critical findings, so it was not selected. The detailed container record includes vendor status and runtime context; mitigations and unused vulnerable code paths are not represented as upstream fixes. A clean application lockfile scan does not cover this boundary.

The [container security record](container-security.md) lists every remaining package/CVE pair and its exact vendor status, image/binary hashes, supported-base comparison and scanner commands. Severities use the selected distribution feed; some upstream ratings are higher. Node 26.13.1 was checked against its official security release, Playwright 1.65.1 against its latest release, and the bundled Chrome 159.0.8154.6 against Google's current **Beta** snapshot. No newer required patch was established, but the OS/npm scan is not comprehensive standalone Node/Chrome CVE coverage.

See the [security scan record](security-scans.md) for commands, timestamps, hashes, dependency counts and static finding triage. Known-advisory scans are time-bound registry evidence, not a guarantee about unknown vulnerabilities. The bounded credential-pattern scan records file counts and hashes without publishing matched values. Heuristic static findings are reviewed in their actual code context rather than hidden to produce a zero-warning claim.

## Reproduce

Use the local versions and setup in the [operator runbook](../../OPERATIONS.md#reproduce-verification). Stop the development supervisor so the verifier can own its loopback ports. Prepare disposable PostgreSQL `round_robin_test`, Redis test database 14, and the native renderer image, then run:

```sh
docker build --pull=false -f scripts/native-renderer.Dockerfile -t round-robin-native-renderer:exhaustive-audit .
ROUND_ROBIN_NATIVE_RENDERER_IMAGE=round-robin-native-renderer:exhaustive-audit \
  pnpm audit:full --output .local/exhaustive-audit/a-new-unique-run
```

The runner requires a new output directory, serializes capture ownership and writes failed status before starting. Every command and required final artifact must pass. It executes the real service/browser/native profiles, fresh frontend unit and native source captures, source coverage validation and the strict per-service gate. The services CI job now uses this same command; this report does not claim a hosted CI execution.

Raw logs, retained Python shards and before-fix replay evidence remain under `.local/exhaustive-audit/`. Checked-in review ledgers, accepted compact machine records and source hashes make the result reviewable without checking generated build output into source control.

A later attempt failed when Docker's VM filled during PostgreSQL tests. Only three obsolete images created by this audit were removed; their scan/build evidence and the repaired final image were retained. The existing PostgreSQL volume was preserved, its container restarted healthy, and verification restarted in a new directory. The [environment recovery record](environment-recovery.json) records the exact scope. Failed or interrupted captures were never relabeled as successful.

## Limits and remaining acceptance

Finite tests, static scans and high coverage do not prove that every possible vulnerability or race has been found. All established first-party defects were repaired; **the upstream container CVEs above remain unresolved because vendor fixes are unavailable**. This audit therefore does not claim that every dependency issue has been eliminated or that the native feature is ready for public release. Assertions were reviewed for behavior: private bytes, outgoing request counts, persisted identities, retained liabilities, rendered state and effects on other owners. Coverage is execution evidence, not proof that every assertion or product conclusion is correct.

No paid provider/search/image calls, external user outreach, production deployment or public data upload occurred. Synthetic providers and mocked identity responses cannot establish actual-provider output quality/billing or external OIDC behavior. Real loopback services, TLS and Docker/browser boundaries were exercised separately. Manual screen-reader testing, real-user studies, a funded provider comparison, hosted proxy/TLS/identity/private-storage staging, production-scale load and timed production recovery remain external acceptance work in [ACCEPTANCE_BLOCKERS.md](../../ACCEPTANCE_BLOCKERS.md).

Filesystem checks assume private storage is not writable by a hostile operating-system peer; path checks do not establish a kernel-enforced directory-handle sandbox. The component tests exercise mounted parent/run/meeting switches; terminal-parent reuse has a connected browser journey, while active-parent replacement has component/API and actual PostgreSQL concurrency tests. No single browser journey combines both cases. The experiment study arms retain their own frozen protocol and do not acquire the application's correction round implicitly.

The README, operations and experiment runbooks, implementation phase record, backlog, product-audit status and acceptance work list have been updated to distinguish repaired engineering work from these unperformed external gates. Historical audit records are retained as history and were not used to justify this fresh result.

The original local development workspace was restored after verification. `/health`, `/ready` and `/live` returned HTTP 200; health reports simulated execution and paid access unconfigured. PostgreSQL data volumes were preserved and the current migration applied before workers started. The [restoration record](development-restored.json) contains the supervisor and observed health evidence. Open [the local workspace](http://127.0.0.1:3000/live).
