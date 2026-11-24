# Round Robin

Compare two to four small-business project approaches or supplier offers against budget, deadline and practical constraints. Keep the evidence, calculations, alternatives, disagreement and next action inspectable.

The November 17 [algorithm repair record](docs/evidence/algorithm-fixes-2026-11-17/IMPLEMENTATION.txt) documents the current discussion changes: signed claim premises, persistent objections, scoped owner attestations, bounded context and retrieval, durable retry timing, shared protocol decisions, visual edit/regression records and stronger paired evaluation tooling. Existing saved reports retain their original versions. Earlier audits describe their recorded source snapshots; their coverage percentages do not certify this revised source. Real-provider quality and human acceptance remain separate evaluation work.

**Status:** the durable local application includes provider, worker, evidence, export, recovery and security layers, bounded correction/re-audit, immutable claim versions with explicit parent-evidence reuse, and atomic replacement of an active parent run. The [fresh exhaustive audit](docs/evidence/exhaustive-audit/REPORT.md) records new source reviews, adversarial tests, repairs and coverage evidence. Optional visual/native capabilities remain explicitly gated. The default local experience uses **simulated advisers** and makes no paid requests. Real-user studies, funded provider evaluations and hosted/visual release acceptance remain pending.

On November 16, 2026, the user authorized construction through the remaining phases before resolving external blockers. This supersedes earlier construction-order dependencies; it does not establish a passed research or release gate. See the [implementation record](docs/evidence/implementation-phases.md) and the [remaining acceptance work](docs/ACCEPTANCE_BLOCKERS.md).

## Run locally

Requirements: Node.js 24, pnpm 9.15.9, [uv](https://docs.astral.sh/uv/), and a running local Docker daemon. On this Mac, `colima start` starts the available daemon.

```sh
uv sync --frozen
pnpm install --frozen-lockfile
pnpm dev
```

Open **http://127.0.0.1:3000/live**. The supervisor starts PostgreSQL and Redis, applies migrations, then launches FastAPI, Celery workers, the dispatcher/reconciler and Next.js. It explicitly selects simulated execution and disables paid access. Ctrl+C stops its owned app processes; database volumes preserve saved comparisons.

- `/` is the fictional sample, available without inference.
- `/live` is the connected workspace: save/autosave drafts, review preflight, start a simulation, reopen results, inspect evidence/calculations, create linked follow-ups, export, and record choices/outcomes.
- `/design` is the optional visual workspace; the visual gate is closed until explicitly configured.
- The API runs at `http://127.0.0.1:8000`; `/docs` describes its typed routes.

`pnpm dev:web` starts only the frontend. Settings use environment variables; [.env.example](.env.example) documents the local defaults. No provider keys are required for simulation. See the [operator runbook](docs/OPERATIONS.md) for explicit live configuration, hosted identity, backups, recovery and native rendering.

## What is built

- Durable owner-scoped meetings, revision-checked drafts, immutable brief/report history, command UUID tombstones, expiring single-use preflight/Start, clarification and linked follow-ups.
- Independent proposals, synthesis, a distinct audit and bounded correction/re-audit within the original allowance, with configured HTTP provider adapters, bounded retries, active deadlines, cancellation fences, queue recovery and ordered event replay.
- A shared conservative spending ledger with run/owner/deployment ceilings, unresolved-exposure carryover, kill switch, and reserved finishing money/calls/time. Keys alone never authorize spending.
- Canonical selected UTF-8 evidence, scope/hash checks, immutable claim versions, explicit parent-evidence selection and freshness downgrade, Decimal calculations, application-derived readiness, approved public-term search and DNS-pinned bounded retrieval. Missing or unchecked evidence stays explicit.
- JSON, Markdown and visually checked PDF exports from the same saved report version. Export retries do not call models.
- Authlib OIDC/invitations, opaque sessions, CSRF/expiry/revocation, private artifact access, retention cleanup, operator commands, coherent backups and quarantined synthetic restore.
- Gated raster versions, pixel-bound review contracts, bounded image jobs and native-source rendering. Actual provider/image quality and improvement need their own funded evaluation.

## Verify

The [fresh exhaustive audit](docs/evidence/exhaustive-audit/REPORT.md) starts from
the implementation itself and does not reuse prior audit conclusions. It records
independent domain reviews and cross-reviews, adversarial regressions, actual
service/browser/native checks, dependency scans and strict coverage evidence.
Earlier audits remain historical records. Engineering verification does not
establish complete phase or release acceptance.

The native renderer image was reduced to its required Chromium runtime and
rescanned. Its remaining upstream OS advisories have no vendor fixes and are
tracked as open in the audit report; passing tests and coverage do not clear
those advisories for public release.

```sh
pnpm exec playwright install chromium   # once
pnpm contracts                         # regenerate authoritative browser contracts
pnpm verify                            # offline unit/fault/contracts/build + sample browser checks
pnpm verify --services                 # stop dev first; isolated actual-service + connected browser checks
pnpm audit:full                        # all profiles + source-bound coverage; build native image first
```

The service profile requires a loopback PostgreSQL database named `round_robin_test` and Redis database 14. Create the test database once when using the supplied Compose stack:

```sh
docker compose exec -T postgres createdb -U round_robin round_robin_test
```

The verifier creates and removes its own schema; process tests use disposable resources. It refuses production targets. Default tests reject provider credentials and external Python socket traffic; browser tests reject non-loopback requests. No paid requests run in CI. These application guards are distinct from operating-system isolation for arbitrary executables.

Native sandbox checks are a separate explicit profile after building the renderer image described in the runbook. A broker outage/restart check can additionally create and stop its own disposable Redis container:

```sh
pnpm verify --services --native
ROUND_ROBIN_TEST_DOCKER_FAULTS=1 pnpm verify --services --native
```

`pnpm audit:full` requires **strictly more than 95% line and branch coverage in
each of 18 service/package groups**, including frontend, native renderer,
migrations and operational tooling. It captures original TypeScript/TSX and
JavaScript source, checks all executable files are represented, and binds the
results to hashes of source, tests, configuration and dependency locks. A fresh
output directory is required; changed inputs or incomplete final records fail
the gate. See the [operator runbook](docs/OPERATIONS.md) for the image build and
audit commands.

Standalone verifier commands save results to `.local/evidence/implementation-checks.json` by default. The full audit creates an isolated timestamped directory, or accepts `--output <new-directory>`. Checked-in current results are linked from the [fresh audit report](docs/evidence/exhaustive-audit/REPORT.md); the [implementation evidence](docs/evidence/implementation-phases.md) retains earlier construction history. CI configuration exists in [.github/workflows/verification.yml](.github/workflows/verification.yml); local results do not claim a hosted CI run.

Useful checks and tools:

```sh
uv run pytest -q
pnpm contracts:check
uv run python scripts/scenario_summary.py --check
uv run python -m experiments.evaluation validate
PYTHONPATH=backend/src uv run python -m round_robin.ops config-check
```

## Plans and remaining acceptance

| Document | Purpose |
| --- | --- |
| [Product plan](docs/PROJECT_PLAN.md) | Audience, scope and prospective scorecard |
| [Implementation blueprint](docs/IMPLEMENTATION_BLUEPRINT.md) | Data, authorization, privacy, budget and operational contracts |
| [Implementation phases](docs/IMPLEMENTATION_PHASES.md) | Phase evidence and release gates |
| [Failure handling](docs/FAILURE_HANDLING.md) | Exact 100 scenario obligations and combined failures |
| [Delivery backlog](docs/DELIVERY_BACKLOG.md) | Work order and acceptance evidence |
| [Visual workflow](docs/VISUAL_DESIGN_WORKFLOW.md) | Gated image review/improvement and native output |
| [Research protocol](docs/RESEARCH_PROTOCOL.md) | Consented real-user observations and study preparation |
| [Evaluation tooling](experiments/README.md) | Frozen identities, repeats, blind grades and conservative measurement |

The external acceptance work remains: five-user discovery/comprehension, an explicitly funded three-arm provider comparison, fresh 48-task end-to-end human evaluation, actual-provider speed/cost measurements, full manual accessibility review, hosted staging, and visual demand/quality studies. Tests and model agreement do not establish real-world correctness. No users were contacted, no paid providers were invoked, and no deployment was performed during implementation.
