# Phase 0 implementation and verification record

November 15, 2026 · Blueprint Version 1 / product and backlog Version 7

This record covers offline preparation for RR-02, RR-03, RR-04 and RR-05. No ticket or phase is complete. All sample evidence, contributions and evaluation tasks are fictional; paid access is disabled. No user interviews, provider calls, deployment or outreach occurred.

## Implemented preparation

| Area | Reviewable artifact | Scope and limits |
| --- | --- | --- |
| Fictional comparison walkthrough | `frontend/app/`, static build in `frontend/out/` | Sample, directly editable draft, explicit save, preflight rehearsal, simulated progress, limited/cancelled reports, Overview/Evidence/Activity, exact passages/calculations, linked changed horizon and quote/site-check brief. Session memory by default; opt-in fictional browser draft storage. No server persistence or paid command implementation. |
| Authoritative contracts | `backend/src/round_robin/schemas/`, `contracts/`, `packages/api-client/src/schema.ts` | Pydantic schemas, generated JSON/OpenAPI/TypeScript and Ajv runtime validation. All fixture reports are immutable authored examples. Contract drift checks compare committed outputs. |
| Cost and readiness rules | `research/calculator.py`, `reports/readiness.py` under the Python package | Bounded Decimal operations, common horizons, explicit unknown components, provenance and application-derived readiness. These rules validate structured assessments; they do not establish semantic truth. |
| Read-only fixture API | `backend/src/round_robin/fixture_api.py` | Loopback Host/Origin/proxy restrictions; no mutation, inference, uploads, authentication claim or paid dispatch. |
| Safe simulator | `experiments/journal.py`, `runner.py`, `adapters.py` | Exclusive process lock, fsynced hash-linked journal/head, stable attempts, crash uncertainty, aggregate exposure/rollover, kill switch, two-call/24-call bounds, finish money/calls/time. Live execution unavailable. |
| Evaluation preparation | `experiments/evaluation.py`, `experiments/fixtures/evaluation_tasks.json` | 16 draft fictional tasks, fixed repeats (26 trials/arm, 78 total), blinded packets with private map, immutable packet saves, manual grades/adjudication, primary denominator and admitted deadline accounting. No grades or useful-model findings fabricated. |
| Scenario traceability | `backend/tests/scenarios/manifest.json` | Every exact 88 core and 12 visual ID retained. Partial offline assertions identify later obligations; no scenario is declared fully passed. Browser preparation assertions map to their eventual command/report requirements. |
| Checks and CI configuration | `backend/tests/`, `tests/e2e/`, `.github/workflows/offline.yml`, `scripts/verify.py` | Unit, boundary, actual-process journal fault tests and browser journeys. CI configuration exists; local results do not claim a hosted CI run. |

## Important observable examples

- At 12 months, A is USD 1,600 and B is USD 1,840. At 24 months, A is USD 2,200 and B is USD 2,080; the original report remains available. Both complete-cost fixtures tie at 20 months.
- B's missing installation cost produces an unknown all-in total, a visible known subtotal and conditional readiness. It cannot establish the cheapest all-in choice.
- Partial and cancelled fixtures preserve each scenario's exact cost horizon/comparison and contain no invented synthesis, recommendation or readiness.
- A Stop request first shows unconfirmed cancellation, then a distinct simulated acknowledgement. This demonstrates wording and state comprehension; it is not proof of worker cancellation fences.
- Saving custom edits performs no inference. The browser refuses to show an unrelated fixed report for unsupported inputs. Its preflight expiry/binding is a rehearsal, not the Phase 1 atomic server authorization contract.

## Reproducible evidence

The actual final verification output is saved in [offline-checks.json](offline-checks.json). It records environment, commands, exit codes and outputs. Reproduce with:

```sh
uv sync --frozen
pnpm install --frozen-lockfile
pnpm exec playwright install chromium
uv run python scripts/verify.py --output .local/evidence/offline-checks.json
```

The verifier runs Python tests, Python/TypeScript contract checks, dataset/inventory validation, TypeScript checking, production build, browser journeys, then a simulated run and same-batch replay in one disposable journal. The second invocation must execute no new work. Timing in this engineering record describes local checks, not product latency targets.

Browser inspection covers desktop and a 390-pixel viewport, keyboard tabs, native dialog Escape/focus return, source/calculation inspection, custom save/restore, unknown totals, exact JSON export, follow-up lineage, preflight expiry, partial output and cancellation acknowledgement. Representative screenshots were inspected locally. Screen-reader review, full WCAG 2.2 AA assessment and five-user comprehension observations remain pending.

Production verification also caught cold-load clicks before hydration and resets from the standard-library preview server's small connection backlog. Initial controls now wait for hydration, and browser verification uses a loopback-only threaded static server with a bounded parallel test profile. Final integration review corrected reopening of partial/cancelled fixtures, canonical follow-up parents and active-scenario evidence in both the UI and Markdown output.

Default pytest rejects provider credential variables before collection and intercepts actual Python DNS/TCP/UDP operations. Explicit disposable loopback fixtures are opt-in. Browser checks reject non-loopback HTTP requests. Python socket guards are not operating-system isolation for arbitrary subprocess executables. No live adapters are installed or exposed by this implementation.

## Remaining gates and risks

Phase 0A still needs five consented, sanitized recent-decision observations and at least four-of-five prototype comprehension/intake successes; use [RESEARCH_PROTOCOL.md](../RESEARCH_PROTOCOL.md). Phase 0B still needs an approved frozen real-task/configuration manifest, explicit bounded funding, real outputs, independent blinded human grades, benefit over the strong baseline and a recorded protocol decision. Simulated results meet none of these study gates.

Broad Phase 1 remains pending: PostgreSQL transactions and migrations, server identity and owner-scoped commands, atomic single-use preflight/Start, durable run/report history, Redis/Celery/outbox delivery and true replay/recovery. The local prototype deliberately does not claim those capabilities. Production retrieval, upload privacy, PDF export, final-stage retry helpers, complete SDK adapter behavior, distributed accounting and release operations remain pending. Native final-stage/role prices and time policies must be frozen with the selected protocol before paid use; the simulator's values are test policy only.

The single-process journal is not protection against an operator deliberately deleting or relocating its history and does not prove distributed locking. Preserve its aggregate directory across batches. Unknown usage survives restart and period rollover; simulated conservative booking never becomes provider evidence. The optional hosted pilot and all visual phases retain their original gates and remain disabled.
