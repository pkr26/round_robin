# Adversarial implementation audit

November 16, 2026 · Corrective engineering review of the implemented phases

## Verdict

The original implementation was not correct in every tested case. The existing
suite passed before this audit, but additional adversarial cases exposed defects
in monetary calculations, execution limits, persistence, concurrent edits,
visual workflows, export fidelity and evaluation bookkeeping. Confirmed defects
were fixed and covered by executable regression tests.

Passing this audit's checks does not prove every edge case or pass a release
gate. Actual provider quality, independent human review, externally configured
hosting and the remaining acceptance obligations are still unverified. The
existing scenario register continues to distinguish partial implementation from
complete acceptance.

## Scope and method

Reviewed the repository's phase plan, implementation evidence, acceptance
blockers and failure contracts, then split independent code review across API
and database behavior, runtime and spending, evidence and security, and visual
and frontend workflows. Reviewed the fixes again across those boundaries.

Started with the existing Python tests, then introduced malformed envelopes,
invalid usage, negative and non-finite values, maximum-size valid briefs,
duplicate identities, stale revisions, out-of-order queue delivery, lost workers,
deadline-dripping TLS servers, incomplete cost inputs, backup races and forged
evaluation records. Browser checks exercise the built application, including
the new form-boundary and exact retry-stage cases.

The repository has no Git metadata, so this review applies to the workspace
files directly. No production database, paid provider, participant contact or
deployment was used. Service tests use disposable schemas, test Redis resources
and synthetic upstreams. The running local development app was stopped for the
combined verification and restarted afterward.

## Confirmed defects corrected

| Area | Failure found | Corrected behavior and main implementation |
| --- | --- | --- |
| Comparable costs | Excluded charges could disappear from an apparently complete all-in total; break-even accepted empty or excluded costs. | Missing applicable charges keep totals unavailable; break-even requires complete comparable inputs. `research/calculator.py` |
| Calculation bounds | Valid maximum-length labels/reasons and 30 unknown lines plus omitted categories overflowed generated contracts. | Bounded display excerpts preserve original inputs; generated missing-component collections support the maximum 35 gaps. Calculator and calculation/report schemas |
| Report construction | Maximum-length option IDs and disagreement text could prevent report publication. | Derived IDs use deterministic hashes when needed; bounded excerpts link back to saved source contributions. `orchestration/reporting.py` |
| Provider transport | Per-read inactivity timeouts permitted slow headers/body to exceed a call's total deadline. | Absolute DNS, connect, TLS, write and read deadlines, with bounded outstanding DNS work and no transport retries. `providers/transport.py` |
| Provider envelopes | Unexpected envelope/usage shapes escaped controlled recovery or discarded valid content. | Malformed content is classified; usable content with invalid usage retains unknown monetary exposure. `providers/http.py` |
| Provider accounting | Negative, boolean or impossible usage counts could corrupt settled cost accounting. | Usage is validated before billing arithmetic; invalid usage remains conservative and unknown. `providers/http.py` |
| Provider output | Invalid database text, oversized individual items, duplicate constraint assessments and excessive references could reach persistence. | Output validation rejects these cases; reported usage survives schema failure. `providers/base.py` |
| Lost workers | Authorized calls on terminal/cancelled runs could occupy execution capacity forever. | Capacity expires with its bounded executor lease; orphaned authorizations reconcile to unknown without releasing financial exposure. Admission and dispatcher |
| Finish reservations | Permanent final-stage failures could retain finishing money that no eligible retry could use. | Finish retention and manual retry share one eligibility policy. `orchestration/policies.py`, controller |
| Manual retry | One stage could borrow another stage's eligibility; historical iterations and lower original limits were mishandled. | The exact requested step must satisfy its original cumulative role/request/time limits. Legacy synthesis retry cannot reuse an earlier successful audit. Shared retry policy and API |
| Retry controls | Buttons inferred eligibility from a bounded historical event page, exposing stale stages or omitting eligible ones. | Snapshot supplies authoritative `retryable_stages`; the frontend renders that list. API, view schema and live workspace |
| Draft follow-ups | A stale tab could overwrite a newer draft when creating a linked follow-up. | Follow-up requires `expected_draft_revision`, checked under the meeting lock. API, request schema and frontend |
| Queue fairness | Later broker delivery could admit newer queued work ahead of older tickets. | Admission uses durable FIFO tickets; resumed work joins at its new ticket time; expired tickets do not block valid work. `orchestration/admission.py` |
| Event pages | A concurrent commit could return events beyond the page's declared cutoff; negative/huge cursors could fail responses. | Event query is bounded by the loaded cutoff and normalized cursor; malformed SSE IDs are rejected. `api/events.py` |
| Intake validation | NUL text, unusable public search terms and duplicate selected attachments could be accepted before later failure. | These are rejected at the typed intake boundary. Common and brief schemas |
| Research requests | Long approved terms could exceed search transport bounds or crowd out counterevidence templates. | Bounded query packing preserves approved scope and template diversity. `research/service.py` |
| Public URL checks | Deep percent encoding could bypass credential-bearing URL rejection. | Credential checks account for nested encodings before DNS or retrieval. `research/fetcher.py` |
| Backup consistency | Manifest reads ignored the append lock and backup validation/archive used separate reads. | Shared locking reads one complete validated byte snapshot for backup. `operations/recovery.py` |
| PDF fidelity | Unsupported saved Unicode silently became missing glyphs while the export reported success. | PDF fails with `pdf_font_unsupported`; the UI offers exact JSON/Markdown recovery. Export renderer, artifact API and client |
| CSRF rejection | Non-ASCII CSRF headers could cause server errors. | Constant-time byte comparison produces a controlled forbidden response. `security/hosted.py` |
| Visual allowances | A lower user allowance overwrote frozen operator configuration and was rejected at admission. | Immutable operator configuration and the user's lower allowance remain separate. `api/visual.py` |
| Visual settlement | Nested reducer objects mutated SQLAlchemy JSON before assignment, losing persisted settlement updates. | Reducer state is detached before mutation. `visual/iterations.py` |
| Invalid visual results | Malformed images/critique rolled back known charges or escaped persistence. | Publication uses a savepoint; usage settles and a terminal failure receipt is retained. `worker/image_tasks.py` |
| Visual failure history | Blocked/fenced operations could disappear; repeated failures could overflow the response contract. | Failure states remain visible in bounded views while durable receipts are preserved. Visual service/views |
| Visual UI state | Reopened designs used stale history; delayed polls could replace newer versions. | Reopening reads authoritative state and revision fences reject stale polls. Design workspace |
| Native diagrams | XML-invalid labels were accepted; known render failure could leave a pending receipt. | Label validation rejects invalid text; failures have terminal reconcilable receipts. Diagram renderer and visual API |
| Report inspection | Calculation reads bypassed report validation; the UI could inspect a newer version than the displayed report. | Inspection uses the validated immutable report and supplies its exact report ID. Artifact API and live workspace |
| Form limits | Requirements/preferences silently dropped extra lines or exceeded available requirement slots. | Every line remains visible; form validity enforces count/length limits including budget/deadline slots before persistence. Live workspace |
| Timing evaluations | Truthy strings/numbers could establish admission or completion success. | Timing metrics require actual booleans. `experiments/evaluation.py` |
| Blinded evaluations | Forged private mappings and alias packets could misbind output hashes or double-count one reviewer. | Public hashes and frozen trial identities are recomputed; independence is counted by actual trial/reviewer. Evaluation tools |
| Release grades | Invalid timestamps, reused grade identities and anonymous single-grade adjudications were accepted. | Prospective aware timestamps, unique originals and attributed output-bound adjudications preserve review provenance. `experiments/release.py` |
| Visual study budgets | Decimal precision could round a just-over-ceiling exposure down to its allowance. | Bounded operands and exact high-precision arithmetic preserve the violation. `experiments/visual_evaluation.py` |
| Local restart | Port probes treated recently closed TIME_WAIT connections as live servers, preventing a clean restart after verification. | Probes match the listener's socket-reuse policy while still rejecting actual listeners. Development and verification scripts |

## Executable regression coverage

New audit cases are in:

- `backend/tests/test_api_adversarial.py`
- `backend/tests/test_admission_audit.py`
- `backend/tests/test_runtime_audit.py`
- `backend/tests/integration/test_provider_transport.py`
- `backend/tests/integration/test_launcher_ports.py`
- `backend/tests/test_audit_evidence_security.py`
- `backend/tests/test_audit_evaluation.py`
- `backend/tests/test_visual_audit.py`
- `backend/tests/test_artifacts_audit.py`

Existing browser journeys were strengthened and two new live journeys cover
manual-entry bounds and authoritative retry-stage selection. Some new cases
also preserve previously correct behavior, including invalid Unicode rejection,
negative/non-finite study inputs and no audit after failed synthesis.

Authoritative machine-readable final results are in
[adversarial-audit-checks.json](adversarial-audit-checks.json). The final command:

```sh
ROUND_ROBIN_TEST_DOCKER_FAULTS=1 uv run python scripts/verify.py --services --native --output docs/evidence/adversarial-audit-checks.json
```

The combined command passed every check. Its recorded results:

| Check | Result |
| --- | --- |
| Default Python unit/contract/fault profile | 479 passed; 24 explicit service/native checks deferred to the profiles below |
| Actual PostgreSQL/Redis/worker/broker/restore and provider TLS profile | 15 passed; four native checks deferred to their explicit profile |
| Actual pinned retrieval TLS profile | 5 passed |
| Actual native Docker renderer | 4 passed |
| Chromium production-build journeys | 22 passed |
| Authoritative contracts and generated client drift | Passed |
| TypeScript and production frontend build | Passed |
| Alembic migration upgrade and schema drift | Passed |
| Durable offline batch replay | Passed twice without new allowance |

After that run, restarting development exposed the TIME_WAIT probe defect above.
The corrected probe was tested against an actual listening and subsequently
closed loopback socket: one additional check passed. Development was restarted
successfully; `/health`, `/ready` and `/live` returned 200, with simulated mode
and paid access disabled. That follow-up is recorded separately in
[adversarial-audit-restart-checks.json](adversarial-audit-restart-checks.json).

This is **504 executed Python checks and 22 browser journeys** across the
profiles, 115 more Python checks than the earlier 389-check combined record.
All 24 Python checks skipped by the default profile ran in the explicit
profiles. Passing checks did not enable any phase, hosted or paid release gate.
The first development restart failed on the port probe; the final restart and
readiness checks passed after its fix. The existing persistent database was
preserved.

## Phase coverage and remaining limits

| Phase | Audited engineering scope | What this audit cannot establish |
| --- | --- | --- |
| 0 | Simulation, study authorization/accounting, frozen identities, grading and timing validators | Real participant observations or a useful real-provider protocol |
| 1 | Durable commands, drafts, preflight/Start, revision conflicts, FIFO admission and event replay | Every possible database/broker failure schedule |
| 2 | Provider envelopes/deadlines, independent roles, retries, reservations, cancellation and worker recovery | Actual provider pricing accuracy, model capability or output truth |
| 3 | Cost/evidence completeness, query scope, bounded retrieval, URL checks and private-input boundaries | Human passage-support judgments or correct real-world research |
| 4 | Connected browser journeys, history, follow-ups, form bounds and saved-version exports | Full manual screen-reader/accessibility and user-comprehension acceptance |
| 5 | Actual service faults, backup/restore integrity, evaluation identity and conservative measurement | Full sustained-load, retention and operational recovery acceptance |
| 6 | Owner isolation, opaque sessions, CSRF, expiry/revocation and restore quarantine tests | External OIDC/TLS/proxy/private-storage staging deployment |
| 7–8 | Gated image jobs, usage settlement, immutable versions, review receipts and UI state | Actual image/vision provider quality or blinded human improvement benefit |
| 9, optional | Structured diagram validation and actual isolated native browser checks | Native domain demand, professional adequacy or public delivery acceptance |

Multilingual PDF fonts and shaping remain unimplemented; affected PDF exports
now fail explicitly instead of corrupting text. JSON and Markdown preserve it.
The contract changes require updated clients: follow-ups now send
`expected_draft_revision`, snapshots include `retryable_stages`, and generated
missing-cost collections permit 35 entries. Browser contracts were regenerated.
`httpcore` is now an explicit locked dependency of the deadline transport.

No unconditional statement that every edge case is correct is justified. See
[the acceptance blockers](../ACCEPTANCE_BLOCKERS.md) for the remaining release
work; passing synthetic tests does not waive those requirements.
