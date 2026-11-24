# Round Robin operator runbook

Run the local product in simulated mode first. Live text, search, image and vision requests require separate operator funding configuration and an explicit user Start. This runbook describes implemented commands; it does not establish the human evaluation, live-provider quality, hosted staging or release gates in the [phase plan](IMPLEMENTATION_PHASES.md).

## Start the local workspace

Use Python 3.12, `uv`, pnpm and a running Docker daemon with Compose. From the repository root:

```sh
uv sync --locked
pnpm install --frozen-lockfile
pnpm dev
```

The supervisor starts this project's PostgreSQL and Redis services, applies Alembic migrations, checks configuration, then launches the API, Celery workers, dispatcher and web application. Open `http://127.0.0.1:3000/live` for saved comparisons or `http://127.0.0.1:3000` for the fictional walkthrough. The supervisor explicitly sets development identity, simulated execution and paid access off. It does not load a private `.env` file automatically.

Ctrl+C stops the supervisor's application processes. PostgreSQL volumes and saved comparisons remain. `docker compose stop postgres redis` stops this project's services without deleting their data. Existing processes on ports 3000 or 8000 must be stopped before starting another supervisor.

```sh
uv run python scripts/dev.py --check
uv run python scripts/dev.py --skip-services
```

`--check` verifies services and migrations without launching the application processes. `--skip-services` uses the operator's already configured PostgreSQL and Redis. All supervisor modes first reject non-loopback database/broker targets and connection-query routing overrides, before starting services or running a migration. An inherited remote URL is not a development target; use explicit loopback services. This check does not turn an arbitrary local database into disposable test data.

| Configuration | Local default or behavior |
| --- | --- |
| `ROUND_ROBIN_DATABASE_URL` | The Compose PostgreSQL service on loopback port 55432; credentials are defined in `compose.yaml` |
| `ROUND_ROBIN_BROKER_URL` | Set by the supervisor to `redis://127.0.0.1:56379/0` |
| `ROUND_ROBIN_ARTIFACT_DIRECTORY` | `.local/artifacts`; keep it private to the operator account |
| `ROUND_ROBIN_ENV` | `development`; the identity is accepted only over loopback with allowed Host/Origin and no external proxy headers |
| `ROUND_ROBIN_EXECUTION_MODE` | `simulated` |
| `ROUND_ROBIN_PAID_ACCESS` | `0` |
| `ROUND_ROBIN_TASK_QUEUE` | `round_robin`; verification uses a separate generated queue |

The saved sample remains fictional. Simulated runs process the user's actual saved brief through simulated adapters and retain that label in their reports. Saving or correcting a draft, preflight, reading evidence and exporting an existing report require no inference.

## Operator commands and service lifecycle

Run these from the repository root with the same database, artifact and funding environment as the service:

```sh
export PYTHONPATH="$PWD/backend/src"
uv run python -m round_robin.ops config-check
uv run python -m round_robin.ops usage-inspect
uv run python -m round_robin.ops purge-inspect
```

`config-check` validates the configured profile and actual migration head without calling providers. `/health` reports process/configuration state; `/ready` also checks database/schema compatibility and restoration quarantine. A configured funding flag does not prove a provider connection or override policy locks.

```sh
uv run python -m round_robin.ops drain
uv run python -m round_robin.ops drain --off
uv run python -m round_robin.ops spend-lock
```

Drain pauses new admission while admitted work can finish within its original limits. The spend lock blocks new paid authorizations; requests authorized before the lock or cancellation can still finish and incur charges. Unknown exposure remains counted. `drain --off` does not clear restoration quarantine or unlock spending.

For an upgrade, drain, wait for admitted work to settle, stop application processes, take a private backup, apply `uv run alembic upgrade head`, then restart compatible API/workers/dispatcher and check `/ready`. Run migrations before workers. The current `e7209a4f361c` migration adds immutable `claim_versions` and `reuse_records`; deploy the matching API, worker and operator code together. Backups include both tables, and whole-meeting deletion purges claims and reuse records along with their linked content. Restore verification requires the complete table inventory and matching schema revision; an older backup is not silently accepted by a newer restore implementation. Preserve the compatible restore toolchain and original snapshot during an upgrade. A binary rollback must retain compatible database/accounting/deletion history; do not rewind the database or replay paid commands to make an old binary appear compatible.

## Enable a bounded live text comparison

The operator must supply actual pinned models, supported capabilities, conservative prices, funding and provider credentials. No prices or model identifiers are inferred from the presence of a key. The authoritative nonsecret file contract is [ProviderConfiguration](../backend/src/round_robin/schemas/provider_configuration.py).

The file requires all three provider models; versioned input/output prices and a bounded billable output/reasoning allowance; capability evidence and dates; prompt/schema/configuration versions; expiry; limits and protected finishing resources; an ordered moderator fallback policy; the Anthropic API version; and explicit owner/deployment daily and monthly caps. Unsupported, expired or changed configurations fail closed. The implemented text protocol is `independent_review_provisional`; deeper debate is not silently enabled.

Inspect the schema without supplying credentials:

```sh
PYTHONPATH=backend/src uv run python -c 'import json; from round_robin.schemas.provider_configuration import ProviderConfiguration; print(json.dumps(ProviderConfiguration.model_json_schema(), indent=2))'
```

Keep the configuration in a private operator-controlled file, set `ROUND_ROBIN_PROVIDER_CONFIG_FILE` to its absolute path and inject `OPENAI_API_KEY`, `ANTHROPIC_API_KEY` and `GEMINI_API_KEY` into server/worker processes through the operator's secret handling. Do not put secrets in that JSON, browser storage, source control, command bodies or verification output.

Use a separate live process environment; `pnpm dev` deliberately keeps its processes simulated:

```sh
export PYTHONPATH="$PWD/backend/src"
export ROUND_ROBIN_ENV=development
export ROUND_ROBIN_EXECUTION_MODE=live
export ROUND_ROBIN_PAID_ACCESS=1
export ROUND_ROBIN_BROKER_URL=redis://127.0.0.1:56379/0
uv run alembic upgrade head
uv run python -m round_robin.ops config-check
uv run python -m round_robin.ops spend-lock --unlock --owner owner_local
```

The configuration path and credentials must already be set. The explicit unlock applies configured caps to both deployment and the named owner. When text and image configurations are both present, each aggregate cap uses their lower value. Either invalid configured file blocks unlock. Existing settled charges, reservations and unknown liabilities are preserved.

Start the following as separate supervised processes using that same environment:

```sh
uv run uvicorn round_robin.app:app --host 127.0.0.1 --port 8000
uv run celery -A round_robin.worker.app:celery_app worker --pool=prefork --concurrency=3 --loglevel=WARNING
uv run python -m round_robin.dispatcher.service
pnpm --filter @round-robin/web dev
```

Live preflight also freezes the concrete request contract: the system instruction, strict output schema, request fields, output-token and packet bounds, and the correction policy. Each live dispatch checks this hash against the running code as well as the pinned operator configuration. A queued or resumed run with an absent or mismatched hash stops before a new send; a matching version label alone is insufficient. Preserve its original results and usage, then obtain a fresh preflight and explicit Start for the updated contract. Do not edit a saved hash or reset counters to resume old authority.

The UI exposes live mode when the service reports a configured live profile. The user must still inspect the exact draft, recipients, approved public terms, protocol, limits and expiry, then Start. A preflight expires after ten minutes and can create only one run. Initial admission expires thirty minutes after accepted Start. Replaying the original command UUID reconciles its existing outcome; it does not create a fresh allowance.

Per-run, logical-step, owner and deployment limits all apply. Finishing money/calls/time are reserved before optional work. Caps constrain authorizations rather than guaranteeing an external bill. Lowering a policy can block remaining work; changing a frozen provider configuration requires a newly authorized comparison instead of silently substituting settings.

An audit response that fails its review or reports critical defects can trigger at most one corrective synthesis and a new audit by a provider different from the corrective synthesizer. This requires remaining original logical-role calls and infrastructure claims, request slots, active time and money for both stages. It reserves a new finish allocation against the existing run/owner/deployment caps. It does not reset any ceiling or replay intake and proposals. If the correction is unaffordable, unavailable or still defective, the saved report retains its limitations; this is not a guarantee that a model finds or fixes every defect.

### Linked follow-ups and selected reuse

A follow-up may explicitly select at most 24 exact parent evidence or claim records from the same owner and meeting. Preflight freezes their identities, versions and hashes; Start revalidates them. The child receives fresh scoped identities and recorded parent provenance. Unselected support is not imported implicitly, and time-sensitive support is marked unchecked rather than being made fresh by copying. Simulated claims remain fictional. Independent proposal requests see only explicitly imported claims, not sibling proposals' newly created claims.

Saving a follow-up draft does not stop its parent. To replace an active parent, the user must approve the parent version recorded in the new preflight and submit it with Start. Fencing the parent, recording its cancellation/finalization intent and creating the child commit in one transaction. A stale parent version, consumed preflight, changed selection, locked policy or full queue leaves the replacement uncommitted. Existing parent charges and uncertain requests remain counted; the new run is not a refund.

### Public research

Research remains disabled unless the text configuration's `research` block explicitly enables it with `pricing_version`, a conservative `per_request_usd` and an approved public region. Inject `BRAVE_SEARCH_API_KEY` only on the server/worker. The user approves public terms before Start. Private question text, quote contents and the private location field are not search fallbacks.

The service constructs allowlisted templates from approved term IDs, allows at most six queries and eight page attempts, validates every public destination/redirect and stores bounded passages. Search transport credentials go only to the configured search endpoint. Retrieved text remains unverified until its support is assessed; a successful fetch does not establish freshness or factual correctness.

## Usage uncertainty and user deletion

`usage-inspect` separates held/reserved, unknown and settled operations. An unknown request is not refunded by cancellation, worker expiry, a new run, calendar rollover or a conservative operator booking. Without a frozen supported cache tariff, nonzero Anthropic cache creation/read input or OpenAI cached input remains unknown rather than being settled at the ordinary input rate. Raw usage is retained for reconciliation.

While a receipt remains nonfinal, exposure retains the greater of its reserved bound and the highest observed amount. A later lower unknown receipt cannot release that exposure. Only a valid final settlement or definitive proof that the request was not sent can lower it. An expired lease can free a concurrency slot while its uncertain monetary liability stays counted.

Set `RR_OPERATION_ID` from the protected usage inspector and `RR_CONSERVATIVE_USD` from a documented conservative bound:

```sh
uv run python -m round_robin.ops usage-reconcile "$RR_OPERATION_ID" --conservative-usd "$RR_CONSERVATIVE_USD"
```

This leaves liability unknown and counted. Final reconciliation requires a genuine final provider receipt in `RR_PROVIDER_RECEIPT`, bound to the same operation, recorded provider and recorded provider request ID, with final usage evidence and reported USD cost:

```sh
uv run python -m round_robin.ops usage-reconcile "$RR_OPERATION_ID" --provider-receipt "$RR_PROVIDER_RECEIPT"
```

If the provider request identity is unresolved, only conservative booking is available until external accounting recovery establishes it. A receipt for another request cannot release this liability.

Meeting deletion first fsyncs a noncontent intent to `.local/deletion-manifest.jsonl` (the artifact directory's parent), then commits access revocation, execution fences, command tombstones and purge work. Content used across linked runs is covered by whole-meeting deletion. New reads/downloads are denied immediately after the tombstone commits; already delivered bytes cannot be recalled.

```sh
uv run python -m round_robin.ops purge-inspect
uv run python -m round_robin.ops purge-inspect --apply
```

The apply command reapplies the independent manifest and runs bounded retention cleanup. Temporary/failed uploads expire after one hour; regenerable exports after seven days. Saved reports, source history and outcomes remain until explicit meeting deletion. Active-storage purge and backup expiry are separate states. Retain the complete deletion manifest independently of database snapshots.

## Storage or backup failure

A filesystem write failure or failed backup records a persistent durability
gate: `/ready` returns 503, new application writes and dispatch stop, and spending
is locked. Existing readable reports remain available, and Stop may still
commit its cancellation fence when the database is healthy. An export with failed
storage never becomes ready. Alerts contain fixed failure codes rather than
private file paths or report content. Before attempting the database gate, the
service atomically writes and fsyncs a private marker containing only a fixed
failure code and timestamp. Its default directory is `durability-control` beside
the artifact directory. For a separate operator control filesystem, set
`ROUND_ROBIN_DURABILITY_CONTROL_DIRECTORY` consistently on API, dispatcher,
workers and operator CLI; provision its private writable parent first. API and
worker checks honor this marker after a database outage or process restart.
Never remove marker files manually. If both the database and independent control
storage cannot persist the failure, external operator quarantine is required:
stop dispatch/workers and close writes until both stores are checked. Software
cannot promise persistence when every available persistence store has failed.

Restore capacity, inspect the underlying storage and preserve the original
history. Then run the operator probe, which requires at least 64 MiB free, fsyncs
a private write/delete and checks all retained ready export/visual/native assets:

```sh
uv run python -m round_robin.ops storage-check
```

A failed backup also requires a new successfully verified backup. After resolving
the fault, clear only the durability gate explicitly:

```sh
uv run python -m round_robin.ops storage-check --clear
```

The command removes markers only after the database clear commits; a marker from
a failed backup still requires a newer verified backup even if its original
database write failed. This does not unlock spending, clear restoration quarantine, end a drain or
remove a dispatch lock that existed before the storage failure. Inspect those
controls separately. A corrupt retained artifact prevents clearing the gate;
recover it or explicitly apply the relevant deletion/retention policy first.

## Backup and synthetic restoration

Use private independently retained backup storage. The initial operating policy is a daily backup with seven-day snapshot retention, fourteen-day noncontent log retention and an active-purge target of twenty-four hours. Configure those schedules/storage lifecycles in the operator environment; the CLI does not install a scheduler or prune arbitrary backup directories. The proposed twenty-four-hour recovery point and four-hour recovery time remain targets until a timed staging exercise proves them.

Choose a new private directory in `RR_BACKUP_DIRECTORY` for each backup:

```sh
umask 077
uv run python -m round_robin.ops backup "$RR_BACKUP_DIRECTORY"
```

The backup uses a consistent PostgreSQL snapshot and includes referenced export and optional native/visual assets with hashes. It refuses to overwrite an existing backup. Keep the current deletion manifest independently available after the snapshot; restoring only the old manifest would resurrect later deletions.

`restore-verify` deliberately accepts only a new synthetic SQLite `restore_test` file or an empty loopback PostgreSQL database whose name ends `_restore_test`. It refuses production/nonempty targets. Set `ROUND_ROBIN_RESTORE_TEST_DATABASE_URL` to that explicit disposable target and choose an empty artifact directory:

```sh
uv run python -m round_robin.ops restore-verify "$RR_BACKUP_DIRECTORY" --target-artifacts "$RR_RESTORE_ARTIFACT_DIRECTORY"
```

The restore migrates the empty target, persistently closes reads/dispatch/spending before importing content, verifies asset hashes, reapplies the complete current deletion manifest, revokes restored sessions, advances the deployment epoch and quarantines old active work. Authorized unresolved requests remain unknown. Failure leaves the target closed. There is no automatic restoration activation or paid-work replay; a production cutover needs its own reviewed reconciliation and staging evidence.

## Invited hosted profile

No deployment is performed by this runbook. Before internet exposure, configure the following explicitly and validate them in isolated staging:

| Setting | Required meaning |
| --- | --- |
| `ROUND_ROBIN_ENV` | A non-development profile; never expose the local identity through a proxy |
| `ROUND_ROBIN_HOSTED_ENABLED=1` | Explicit hosted-access configuration |
| `ROUND_ROBIN_PUBLIC_ORIGIN` | One credential-free HTTPS origin for the frontend, `/api` and `/auth` |
| `ROUND_ROBIN_OIDC_ISSUER` | The configured HTTPS OIDC issuer |
| `ROUND_ROBIN_OIDC_CLIENT_ID`, `ROUND_ROBIN_OIDC_CLIENT_SECRET` | Private Authlib client configuration; register the exact `/auth/callback` URI |
| `ROUND_ROBIN_INVITE_ALLOWLIST` | JSON pairs of actual issuer and immutable subject; email addresses are not the identity key |
| `ROUND_ROBIN_COMMAND_HASH_KEY` | A persistent private key of at least thirty-two characters, shared across compatible restarts |
| `ROUND_ROBIN_HOSTED_QUOTAS_CONFIGURED=1` | Operator-reviewed queue, storage, export and spending quotas |
| `ROUND_ROBIN_PRIVATE_STORAGE_CONFIGURED=1` | Private artifact/database/broker storage and access boundaries |
| `ROUND_ROBIN_RETENTION_CONFIGURED=1` | Disclosed retention, independent deletion manifest and backup policy |

Build the web application with an empty `NEXT_PUBLIC_ROUND_ROBIN_API_URL` for relative same-origin requests. The reverse proxy must terminate TLS, route `/api` and `/auth` privately to FastAPI, pass SSE without buffering and preserve cancellation/session disconnect behavior. Keep PostgreSQL/Redis private and authenticated. The Compose credentials are local development settings.

Authlib handles authorization code, PKCE, state and nonce verification. Login state and session records are stored server-side; browser cookies contain opaque tokens. Session cookies are Secure/HttpOnly/SameSite=Lax. Sessions expire after thirty idle minutes or twelve absolute hours. Commands require the session CSRF token and allowed origin; revocation is checked at protected command/read and SSE heartbeat boundaries. Provider credentials and funding remain operator-owned.

Verify two real staging identities across routes, streams, source downloads and exports, session expiry/logout/CSRF, private storage, quotas, proxy behavior, deletion and timed restore. Automated local identity tests are supporting implementation evidence; they do not establish a hosted staging pass.

## Optional raster and native outputs

`ROUND_ROBIN_VISUAL_ENABLED=1` opens the optional visual workspace. Live generation/editing/vision additionally requires `ROUND_ROBIN_PAID_ACCESS=1` and a validated `ROUND_ROBIN_IMAGE_CONFIG_FILE`. Inspect [ImageProviderConfiguration](../backend/src/round_robin/providers/images.py) for the authoritative schema. It binds actual generator/critic models, capabilities, conservative whole-request image/vision bounds, reasoning exposure, expiry and owner/deployment caps. Two distinct actual critics outside the generator are required for complete generated-image review. Keys remain server-side.

Use the same explicit `spend-lock --unlock --owner ...` workflow. Image-only funding is supported; if text and image files are both configured, their lower aggregate caps apply. Image funding does not authorize a user session or start inference by itself.

Image tasks use an isolated broker namespace and long-call worker:

```sh
export ROUND_ROBIN_IMAGE_BROKER_URL=redis://127.0.0.1:56379/1
uv run celery -A round_robin.worker.image_tasks:image_app worker --concurrency=1 --pool=prefork --loglevel=WARNING -Q round_robin_images
```

Do not use the ordinary worker broker database for this worker. The image worker has a 630-second soft limit, 660-second hard limit, 900-second broker visibility, 720-second lease and ten-second heartbeat. OpenAI Images support is synchronous; no unsupported job lookup/cancellation endpoint is assumed. Valid pixels can still have unknown final billing.

Optional design controls cap V1–V3, four image submissions, twenty-four shared inference calls, bounded polling and 1,800 active seconds. Demand, independent review and human improvement/regression evaluation remain required before a visual release.

Structured diagrams additionally require `ROUND_ROBIN_NATIVE_DIAGRAM_ENABLED=1`. They accept bounded typed nodes/relationships rather than executable SVG input.

### Isolated native interfaces

Build the local renderer explicitly. The Dockerfile pins the official Playwright base digest and package version:

```sh
docker build --pull=false -f scripts/native-renderer.Dockerfile -t round-robin-native-renderer:local .
export ROUND_ROBIN_NATIVE_INTERFACE_ENABLED=1
export ROUND_ROBIN_NATIVE_RENDERER_IMAGE=round-robin-native-renderer:local
```

The renderer is closed by default and never pulls an image during a render. It inspects the configured local image and executes its immutable image ID. HTML is sent as JSON over Docker stdin, with no host mounts, operator environment, cookies or source files exposed to the container.

Each invocation runs nonroot with network disabled, a read-only filesystem, all capabilities dropped, no-new-privileges, 512 MiB memory, 128 PIDs, one CPU and a limited 128 MiB `/tmp`. Browser service workers and external requests/WebSockets are blocked. Chromium's process sandbox is disabled inside this container; the Docker boundary provides the execution isolation.

The renderer accepts at most 128 KiB HTML, sixteen megapixels, twenty allowlisted click/fill/press actions, a twenty-second browser deadline and a twenty-five-second host process deadline. The complete serialized renderer packet is bounded at 160 KiB; the API's 128 KiB JSON body limit can impose a lower source allowance. Timeout cleanup targets only that invocation's generated UUID container. Screenshots and return JSON are bounded.

Saved provenance includes the exact source SHA, Docker image ID, Playwright/browser versions, renderer/configuration hash, raw and canonical screenshot hashes, viewport, declared assertions and blocked-request hashes. Appearance and passing declared assertions do not establish complete functionality, accessibility or engineering correctness. Downloadable HTML remains an attachment; execution belongs only in the isolated renderer.

## Reproduce verification

Use a separate shell without production provider credentials. The default test profile refuses them and blocks external traffic.

```sh
uv run pytest -q
pnpm contracts:check
pnpm typecheck
pnpm build
uv run python scripts/verify.py
```

Service verification requires the running Compose services, a separate `round_robin_test` PostgreSQL database and reserved Redis databases 14 (core) and 15 (image process fixtures). Create the test database once if it does not already exist:

```sh
docker compose exec -T postgres createdb -U round_robin round_robin_test
```

Stop the dev supervisor so ports 3000 and 8000 are free, then run:

```sh
uv run python scripts/verify.py --services --native
```

The service lane creates disposable schemas/queues and generated restore-test databases, runs actual workers/dispatcher and browser journeys, and removes only resources it created. The native lane requires the explicitly built renderer image. The combined record is written to `.local/evidence/implementation-checks.json` by default and retains command outcomes and sanitized diagnostics.

For the complete audit profile, use the same disposable services and renderer, leave ports 3000 and 8000 free, and run:

```sh
pnpm audit:full
```

Each invocation requires a new output directory and exclusively owns fresh coverage captures. To choose a location, use `pnpm audit:full --output test-results/audit-unique-run`; an existing directory is rejected. The profile runs service/native verification, frontend unit and browser checks, source-instrumented frontend/native coverage and Python branch coverage, then requires **strictly greater than 95% line and branch coverage in every service group**. Python groups are each backend package plus application, migrations, experiments and tooling; frontend and native renderer are separate groups. Aggregate repository coverage cannot hide a failing group. The frontend source checker also requires its aggregate statement and function coverage to exceed 95%.

The gate binds production sources and verification inputs to the captured manifest, rejects changed/missing source or mismatched captures, and includes unexecuted current production files in the denominator. Only nonexecutable Protocol declarations may use the existing Python omission. A successful subprocess exit without affirmative final verification and coverage records is insufficient. Inspect `audit-checks.json`, `checks.json`, `service-coverage.json`, source manifests and per-step logs in the invocation directory. The [services CI job](../.github/workflows/verification.yml) runs this same full profile with PostgreSQL, Redis and the built renderer; the offline CI lane remains separate.

The [fresh audit report](evidence/exhaustive-audit/REPORT.md) records the final invocation and its actual results. Coverage measures exercised code, not the absence of vulnerabilities or every concurrency ordering. The local audit also demonstrated that Docker VM disk exhaustion can stop PostgreSQL: check available VM space before building comparison images, preserve database volumes, and remove only identified disposable artifacts when recovering. Do not use a global prune as a substitute for identifying ownership.

The native renderer now uses a minimal, pinned Ubuntu 26.04 runtime with the exact Node and Chromium binaries from the supported pinned Playwright image. The [container security record](evidence/exhaustive-audit/container-security.md) identifies the immutable tested image, binary hashes, current Chrome Beta provenance, and all unresolved OS advisories. At the recorded scan, 40 medium/low package matches across 27 CVEs had no vendor fixes. Runtime isolation mitigates impact; it does not clear those findings or authorize public native delivery. Rebuild, run actual native checks and rescan when vendor fixes become available.

| Surface | Reproducible checks |
| --- | --- |
| Draft/preflight/commands/history/typing | `backend/tests/test_api.py`, `test_application_contracts.py`, `test_provider_configuration.py` |
| Real PostgreSQL races and stale identity refresh | `backend/tests/integration/test_postgres_api.py` |
| Actual PostgreSQL backup/restore and deletion replay | `backend/tests/integration/test_postgres_restore.py` |
| Actual separate image queue, committed authorization, pixel critics and killed-worker uncertainty | `backend/tests/integration/test_image_processes.py` |
| Session isolation/expiry/CSRF and conservative recovery | `backend/tests/test_hosted.py`, `test_operations.py` |
| Actual pinned TLS, SNI/Host, certificate and deadline behavior | `backend/tests/test_fetcher_transport.py`, with `ROUND_ROBIN_TEST_ALLOW_LOOPBACK=1` |
| Actual sandboxed browser actions, network denial and timeout cleanup | `backend/tests/integration/test_native_renderer.py`, with `ROUND_ROBIN_TEST_NATIVE_RENDERER=1` |

Human discovery/comprehension, bounded funded provider comparisons, held-out quality, measured product economics and hosted staging remain separate evidence obligations. No command here contacts participants, sends messages, deploys a service or authorizes a paid evaluation batch merely by being documented.
