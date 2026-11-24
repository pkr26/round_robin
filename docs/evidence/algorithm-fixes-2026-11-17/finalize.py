"""Record verified repair outcomes and bind their current executable inputs."""
from pathlib import Path
import hashlib
import json
import re

BASE = Path(__file__).resolve().parent
ROOT = BASE.parents[2]


def read_log(name):
    return (BASE / 'root' / name).read_text()


backend = read_log('pytest-release.log')
match = re.search(r'(\d+) passed, (\d+) skipped, (\d+) warnings in ([\d.]+)s', backend)
assert match and not re.search(r'^FAILED |\d+ failed,', backend, re.M), 'Final backend checks are not clean'
front = re.search(r'Tests\s+(\d+) passed', read_log('frontend-complete.log'))
postgres = re.search(r'(\d+) passed', read_log('postgres.log'))
browser = re.search(r'(\d+) passed', read_log('browser-sample.log'))
assert front and postgres and browser
assert 'Generated TypeScript contract matches.' in read_log('contracts-check.log')
assert 'Finalizing page optimization' in read_log('build-accepted.log')
assert 'error TS' not in read_log('typecheck-accepted.log')

rows = [
    ('E-F01', 'Disputed constraints retain both assessors and citation sets; unsupported opposition stays unknown.', 'backend/tests/test_algorithm_evidence_fixes.py'),
    ('E-F02', 'Objective page/passages are fairly allocated; unusable searches and context overflow are prevented and disclosed.', 'backend/tests/test_algorithm_evidence_fixes.py'),
    ('E-F03', 'AND required premises, OR alternatives, signed refutation and ancestor-status checks prevent support laundering.', 'backend/tests/test_algorithm_evidence_fixes.py'),
    ('E-F04', 'Full immutable objection events survive summary limits and require an explicit reviewed disposition.', 'backend/tests/test_algorithm_evidence_fixes.py'),
    ('E-G01', 'Exact owner attestations bind target/evidence hashes and publish a new report without inference; source-wide verification is never inferred.', 'backend/tests/test_algorithm_evidence_fixes.py'),
    ('E-G02', 'Requirement/exception passage selection covers document tails, retains exact spans/hashes and records uninspected coverage.', 'backend/tests/test_algorithm_evidence_fixes.py'),
    ('RUNTIME-01', 'Mandatory finishing envelopes, capacity priority and optional cutoffs apply before waiting or retrying.', 'backend/tests/test_runtime_algorithm_fixes.py'),
    ('RUNTIME-02', 'Persistent not_before and immutable dispatch-generation capture fence premature/stale duplicate messages.', 'backend/tests/test_runtime_algorithm_fixes.py'),
    ('RUNTIME-03', 'Start, clarification and retry use one transactional queue-capacity guard.', 'backend/tests/test_runtime_algorithm_fixes.py'),
    ('ROOT-01', 'Canonical UTF-8 admission, conservative contribution footprints and post-research initial caps reserve final-review context.', 'backend/tests/test_algorithm_context_and_repair.py'),
    ('ROOT-02', 'Browser consumes event pages through the original snapshot cutoff, deduplicates and fences stale reads.', 'frontend/tests/live-workspace.test.tsx'),
    ('ROOT-G01', 'Intake sees selected passages without prematurely freezing them; typed proposed changes require explicit confirmation.', 'backend/tests/test_runtime_algorithm_fixes.py'),
    ('ROOT-G02', 'Schema repair receives bounded validation codes/paths and stage-specific repair instructions.', 'backend/tests/test_algorithm_context_and_repair.py'),
    ('EVAL-01', 'Hashed exact finding/decision bundles reach actual image edit and critic requests.', 'backend/tests/test_algorithm_evaluation_visual_fixes.py'),
    ('EVAL-02', 'Production/study share versioned stage, issue, reserve and correction policies; fixed-input study adapters and optional revisions are explicitly identified.', 'backend/tests/test_algorithm_evaluation_visual_fixes.py'),
    ('EVAL-03', 'Server-owned regression matrices preserve missing/contradictory dispositions and ancestry; old reviews have append-only recovery.', 'backend/tests/test_algorithm_evaluation_visual_fixes.py'),
    ('EVAL-04', 'Substantive fingerprints reject title/ID aliases; paired measurement retains failures, repeats and uncertainty.', 'backend/tests/test_algorithm_evaluation_visual_fixes.py'),
]
original = json.loads((ROOT / 'docs/evidence/algorithm-audit-2026-11-17/findings.json').read_text())
assert {item['id'] for item in original} == {row[0] for row in rows}
closures = [dict(id=identity, status='implemented_and_regression_verified', change=change, regression=regression) for identity, change, regression in rows]
(BASE / 'closures.json').write_text(json.dumps(closures, indent=2) + '\n')

old_manifest = json.loads((ROOT / 'docs/evidence/algorithm-audit-2026-11-17/source-manifest.json').read_text())
changed = [item['path'] for item in old_manifest if hashlib.sha256((ROOT / item['path']).read_bytes()).hexdigest() != item['sha256']]
paths = set()
for directory in ('backend/src', 'backend/tests', 'experiments', 'frontend/app', 'frontend/tests', 'packages/api-client/src', 'contracts', 'scripts', 'tests/e2e'):
    paths.update(p for p in (ROOT / directory).rglob('*') if p.is_file() and '__pycache__' not in p.parts and p.suffix in {'.py','.ts','.tsx','.json','.mjs','.cjs'})
paths.update(ROOT / name for name in ('README.md','pyproject.toml','uv.lock','package.json','pnpm-lock.yaml','frontend/package.json','playwright.config.ts','frontend/tsconfig.json','frontend/vitest.config.mts','frontend/next.config.ts'))
manifest = [{'path': str(p.relative_to(ROOT)), 'sha256': hashlib.sha256(p.read_bytes()).hexdigest(), 'bytes': p.stat().st_size} for p in sorted(paths)]
(BASE / 'source-manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
summary = {
    'date': '2026-11-17', 'timezone': 'America/Phoenix', 'audit_items_addressed': len(rows),
    'backend': {'passed': int(match[1]), 'skipped': int(match[2]), 'warnings': int(match[3]), 'seconds': float(match[4]), 'log': 'root/pytest-release.log'},
    'frontend_tests': int(front[1]), 'postgres_tests': int(postgres[1]), 'sample_browser_tests': int(browser[1]),
    'contracts': 'passed', 'typecheck': 'passed', 'production_build': 'passed',
    'paid_provider_calls': 0, 'deployment_performed': False, 'empirical_model_accuracy_established': False,
    'changed_preexisting_source_and_lock_files': changed, 'bound_source_test_contract_inputs': len(manifest),
    'limitations': ['43 default-suite tests require optional service/native/fault profiles; 12 selected PostgreSQL checks additionally passed on disposable schemas.',
        'Fixed-evidence study profiles deliberately omit live intake/retrieval infrastructure and do not establish end-to-end product quality.',
        'No funded human-reviewed provider evaluation was run; best-model/protocol and factual accuracy rates remain unestablished.',
        'Prior exhaustive coverage percentages bind older source snapshots; the exhaustive coverage gate was not rerun.',
        'The context envelope is deliberately conservative (48,000 initial bytes and 8,000 contribution footprint) and can reject large inputs/responses rather than omit required material.']}
(BASE / 'summary.json').write_text(json.dumps(summary, indent=2) + '\n')

text = f'''ROUND ROBIN — ALGORITHM REPAIRS IMPLEMENTED
November 17, 2026 (America/Phoenix)

All 17 items from the discussion-algorithm audit have implementation changes and automated regression checks. The implementation adds scoped claim premises/refutation, a durable objection ledger, explicit owner attestation, bounded retrieval/context admission, retry scheduling and queue invariants, shared versioned protocol decisions, complete visual feedback/regression records and stronger evaluation identity/measurement. No paid provider calls or deployment occurred. These engineering repairs do not establish a real-world accuracy percentage or a best-performing protocol.

Verification

Complete offline backend suite: {match[1]} passed, {match[2]} skipped, {match[3]} warnings ({match[4]} seconds), root/pytest-release.log.
Frontend unit/component suite: {front[1]} passed, root/frontend-complete.log.
Real PostgreSQL transaction/concurrency checks: {postgres[1]} passed, root/postgres.log. Existing loopback test database, random disposable schemas, automatic cleanup; no production data or service restart.
Chromium sample journeys: {browser[1]} passed, root/browser-sample.log.
Generated contracts/client drift check, TypeScript check and production build passed. Evaluation-fixture validation and 100-scenario register check also passed.

Focused lane results overlap the full suite and must not be added to its count. Earlier root logs retain interrupted or failed intermediate runs for transparency. The accepted result is pytest-release.log after the relevant repairs and fixture upgrades, including the final claim-kind support guard. Existing tests were updated where they previously assumed immediate retries, globally verified sources, output claims during unconfirmed intake, unconditional debate revisions or oversized admitted packets; their assertions were replaced with explicit checks of the strengthened contract. New regressions exercise adversarial behaviors, rather than copying implementation branches.

Repairs and regressions

'''
for identity, change, regression in rows:
    text += f'{identity}: {change}\nRegression: {regression}\n\n'
text += '''Behavior and compatibility

Initial proposal packets stay independent. Intake receives selected evidence read-only and can propose typed changes; the user confirms the brief before claims are committed. Invalid-output retries receive sanitized field paths and permitted-reference instructions. Every stage remains within the original run, logical-call, budget and deadline limits.

Objections are recorded once with immutable provenance/events. A synthesis answer remains pending until an eligible distinct auditor explicitly resolves it against scoped references. Old material concerns cannot disappear merely because a later model omits them or says audit_passed. Complete issue records, scoped assessments, comparison margins and readiness dimensions appear in saved reports, exports and the interface.

Scoped human attestation is available through GET/POST /api/runs/{id}/assessments and the Evidence view. It checks the exact owner/run/target/evidence hashes, requires the current run version, rejects active runs and expired cost quotes, and appends a new report version with zero inference. It remains labeled as owner attestation. Supplied source records are never globally promoted to independently verified. A model/source support graph cannot prove semantic truth.

Context controls now measure canonical encoded bytes, deduplicate delivered claim content and retain the last objection answer needed by the auditor. The standard finite path reserves downstream room by admitting at most48,000 initial bytes and an8,000-byte conservative per-contribution footprint (3 x canonical response bytes plus640 bytes per claim/opening/response). Optional research observes the same initial bound before search and before adding passages. Oversized inputs/responses fail explicitly; no required concern or premise is silently dropped. These conservative constants are operating limits, not measured optimal settings or guarantees of provider availability.

The application and studies share stage/quorum, correction, resource and objection reduction rules. Declared study profiles use preconfirmed fixed inputs and their journal executor; they intentionally remain distinct from live intake/retrieval/durability. Minor dissent is retained but does not alone schedule a debate revision; a concrete unresolved material issue may admit bounded revision/correction. Model/prompt/schema/policy identities are frozen. Old reports remain readable; existing paid authorizations with incompatible frozen request hashes fail closed and require a fresh linked preflight instead of silently receiving the new prompt or extra allowance.

Visual editors receive the exact approved finding/decision bundle and parent pixels. Critics receive its hash, prior pixels and mandatory checks. Reviewer quorum, regression completeness and unresolved blockers are separate. Missing child responses remain pending; unresolved ancestry survives later versions. Historical reviews can receive append-only supplements within their original two-call/allowance caps; original reviews and candidate versions remain unchanged. Exhausted or expired scopes remain exhausted rather than gaining implicit authorization.

Evaluation records reject cosmetic aliases, retain all primary failures and repeated-task clusters, and expose paired usefulness/critical-error/cost/latency outcomes with uncertainty. The tooling remains prospective: no actual provider superiority, human usefulness or release gate was established. Those claims need the documented frozen, funded, independently graded evaluation.

Remaining verification scope

The default suite skips43 optional service/native/fault tests. Twelve focused real PostgreSQL checks additionally passed; full actual Redis/worker-process, Docker-native, hosted staging and funded provider profiles were not run here. Existing dependency deprecation/runtime warnings remain in the logs. The previous exhaustive coverage evidence is historical after these changes. source-manifest.json binds the current source/tests/contracts and locks; evidence-index.json binds these repair records. This workspace has no Git metadata, so these are file hashes rather than a commit or pull-request identity.
'''
text = text.replace('at most48,000','at most 48,000').replace('an8,000','an 8,000').replace('plus640','plus 640').replace('skips43','skips 43')
(BASE / 'IMPLEMENTATION.txt').write_text(text)
index = [{'path': str(p.relative_to(BASE)), 'sha256': hashlib.sha256(p.read_bytes()).hexdigest(), 'bytes': p.stat().st_size}
         for p in sorted(BASE.rglob('*')) if p.is_file() and p.name != 'evidence-index.json' and '__pycache__' not in p.parts]
(BASE / 'evidence-index.json').write_text(json.dumps(index, indent=2) + '\n')
print(json.dumps(summary, indent=2))
