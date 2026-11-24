# Continuing implementation across phases

November 16, 2026 · Engineering implementation and acceptance evidence

**Current engineering verification:** the [fresh exhaustive audit](exhaustive-audit/REPORT.md)
supersedes the execution counts below. It started from current source without
using earlier audit findings and includes independent adversarial reviews,
security/data-boundary repairs, bounded correction/re-audit, persisted claim
versions and explicit parent reuse, and atomic replacement of an active parent.
The new `pnpm audit:full` workflow binds coverage to current source and verification
inputs and requires each of 18 groups to exceed 95% for both lines and branches.
See its report and machine-readable records for the accepted run and remaining
external acceptance work. The original construction evidence below is retained
as history; its counts and migration revision are not current totals.
The native container's remaining upstream medium/low OS advisories are explicitly
open; application test/coverage acceptance does not establish their remediation.

The user explicitly authorized construction through the remaining phases on November 16, deferring blockers to the end. This overrides the earlier **construction** dependency on Phase 0 studies. It does not fabricate interviews, provider evaluations, funding authorization or a release decision. Paid access and hosting remain disabled by default.

## Implemented engineering surface

| Phase | Implemented work | Evidence still required for phase/release acceptance |
| --- | --- | --- |
| 0 | Fictional sample, prototype, typed contracts, Decimal/readiness fixtures, durable simulator, 16-case evaluation/grading tooling and explicitly authorized three-arm study runner | Five real-user observations, comprehension study, funded paired outputs and human grades, selected protocol decision |
| 1 | PostgreSQL/SQLAlchemy/Alembic, owner-scoped durable commands/tombstones, revision checks, immutable submitted/confirmed brief history, single-use preflight and Start, Redis/Celery outbox/dispatcher, ordered events/SSE and saved history | Full applicable fault register and operational release review; local test results do not establish user value |
| 2 | Configured HTTP provider adapters, independent proposals/synthesis/distinct audit, admission and active-time limits, conservative aggregate ledger, finish resource conversion, retries, cancellation fences, strict runtime report contracts | Explicit funded real-provider smoke/evaluation and measured actual-provider economics/quality |
| 3 | Canonical private UTF-8 uploads, frozen selection/recipient hashes, deterministic comparable costs, scoped evidence and claims/reuse helpers, public-term/template search, DNS-pinned bounded HTTPS retrieval, research leases and unknown exposure | Human passage-support checks, actual-provider/retrieval evaluation and complete enabled-scenario review |
| 4 | Connected `/live` workspace, draft/history/follow-up/choice/outcome journeys, source/calculation inspection, independent JSON/Markdown/PDF exports, keyboard/narrow-screen checks | Five-user integrated comprehension and full manual screen-reader/accessibility assessment |
| 5 | Fault tests using actual processes, prospective 48-task evaluation validator, outcome-inclusive timing, operator CLI, storage/backup failure gates, private coherent backups and quarantined restoration | Fresh 48-case human evaluation, measured performance/cost gates, full combined-fault/load/retention release exercises |
| 6 | Configured Authlib OIDC, invitations, opaque sessions, CSRF/expiry/revocation, owner isolation, explicit quotas and spending locks, restore/read quarantine | Operator-authorized hosted staging, external OIDC/TLS/proxy/private-storage/retention exercises and timed recovery acceptance |
| 7–8 | Connected `/design`, immutable canonical raster assets, actual-pixel receipts, configured image/vision HTTP adapters, separate bounded worker processes, independent critique/edit/selection, and prospective matched visual-study bookkeeping; default gates remain closed | Demonstrated visual demand, funded capable generator/critic smoke, independent actual-model and blinded improvement/regression evaluation |
| 9 | Editable structured diagrams and isolated native HTML rendering, immutable source/render provenance and actual Docker interaction/security checks; native output remains separately gated | Domain-specific demand, competent human/functional acceptance and hosted boundary tests before public delivery; no professional adequacy claim |

The original [implementation-checks.json](implementation-checks.json) records the construction run's actual commands, exit statuses and outputs. The earlier [Phase 0 evidence](phase0-preparation.md) remains a historical preparation record. The [acceptance work list](../ACCEPTANCE_BLOCKERS.md) gathers the deferred prerequisites without inventing their results. Current verification is linked from the fresh audit above.

## Historical construction verification

The following local command passed on November 16, 2026:

```sh
ROUND_ROBIN_TEST_DOCKER_FAULTS=1 uv run python scripts/verify.py --services --native --output docs/evidence/implementation-checks.json
```

| Verification scope | Actual result |
| --- | --- |
| Credential-free Python unit/contract/fault suite | 368 passed; 21 service/TLS/native checks intentionally deferred to the explicit profiles below |
| Actual PostgreSQL, Redis, Celery prefork and broker-restart profile | 12 passed, including ordinary/image worker crashes, durable pre-send authorization, duplicate delivery, lost publish acknowledgment, concurrent API admission and full synthetic PostgreSQL restoration |
| Actual TLS retrieval transport | 5 passed, including pinned destination/TLS identity, certificate rejection and absolute header/body deadlines |
| Actual isolated native Docker rendering | 4 passed, including interaction checks, network blocking, container boundaries and killing an infinite script without stopping an unrelated sentinel |
| Chromium browser journeys against the production frontend | 20 passed: eight fictional prototype, eight connected core, two hosted-session boundary checks and two optional visual/native-diagram journeys |
| Contracts, TypeScript and production frontend | Authoritative JSON/OpenAPI and generated client drift checks, typecheck and static production build passed |
| Migrations | Disposable PostgreSQL upgraded to `d1260b7f94de`; Alembic reported no outstanding schema operations |
| Durable offline replay | The same simulated batch ran twice without a fresh allowance |

This is 389 executed Python checks and 20 browser journeys across the combined
profiles. The exact 100 failure IDs remain present: 99 link to named partial
implementation/test slices; EVD-07's semantic support/incorrect-consensus
evaluation remains pending. Partial slices retain their unverified obligations;
neither this inventory nor the test count declares a complete phase or release
passed. The funded study runner's full 78-trial test used MockHTTP only, with
520 counted synthetic requests and no actual provider spending.

JSON, Markdown and PDF exports use the saved report's identity and cutoff.
Four representative PDF pages were rendered with Poppler and visually inspected;
native sandbox tests exercised actual Chromium in the isolated container.
Hosted browser session tests include mocked identity responses; they do not
establish a successful external OIDC/TLS staging deployment.

After verification, `pnpm dev` was started successfully against the persistent
local database. `/health` reported simulated mode with paid access unconfigured;
`/ready` returned 200. Desktop `/live` and narrow `/design` screenshots were
visually inspected; the narrow layout had no horizontal overflow.

## Local development

```sh
uv sync --frozen
pnpm install --frozen-lockfile
colima start                   # or start another local Docker daemon
pnpm dev
```

The supervisor starts PostgreSQL/Redis, applies migrations, then starts the local API, Celery workers, dispatcher and Next.js. Open `http://127.0.0.1:3000/live`. The fictional sample stays at `/`. The default supervisor explicitly selects simulation and disables paid access. Stop its owned processes with Ctrl+C; the database volumes preserve saved local comparisons.

```sh
pnpm contracts
pnpm verify                    # no paid providers; fixture browser journeys
pnpm verify --services         # stop dev first; disposable actual-service + connected browser checks
ROUND_ROBIN_TEST_DOCKER_FAULTS=1 pnpm verify --services --native
pnpm audit:full                # build the native image first; strict per-service source coverage
```

The service profile refuses non-loopback/non-test databases and uses an isolated PostgreSQL schema and Redis test namespace. It never targets production resources. Local verification results are distinct from a hosted CI execution.

## External prerequisites, not invented results

No target users were contacted, no paid provider/search/image requests were sent, and no site was deployed. Provider keys alone do not enable paid work: pinned models, capabilities, conservative prices, expiry, funding ceilings and operator policy authorization are required. The research, human-quality, accessibility, hosted staging and visual-demand records remain pending. Engineering feature availability and passing deterministic tests are not claims that these phase gates have passed.
