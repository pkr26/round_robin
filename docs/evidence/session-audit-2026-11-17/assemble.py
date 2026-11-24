"""Assemble this read-only audit against the pre-existing immutable input manifest."""
from collections import Counter
import hashlib
import json
from pathlib import Path
import re

BASE = Path(__file__).resolve().parent
ROOT = BASE.parents[2]
FIXES = ROOT / 'docs/evidence/algorithm-fixes-2026-11-17'
expected = json.loads((FIXES / 'source-manifest.json').read_text())
for entry in expected:
    path = ROOT / entry['path']
    if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != entry['sha256']:
        raise RuntimeError('Audit input drift: ' + entry['path'])

preserved = {}
for directory in ('algorithm-audit-2026-11-17', 'algorithm-improvement-2026-11-17', 'algorithm-fixes-2026-11-17'):
    folder = ROOT / 'docs/evidence' / directory
    records = json.loads((folder / 'evidence-index.json').read_text())
    for entry in records:
        if hashlib.sha256((folder / entry['path']).read_bytes()).hexdigest() != entry['sha256']:
            raise RuntimeError('Historical evidence drift: ' + str(folder / entry['path']))
    preserved[directory] = len(records)

reviews = {domain: json.loads((BASE / domain / 'review.json').read_text())
           for domain in ('root', 'runtime', 'evidence', 'evaluation')}
findings, inventory = [], []
for domain, review in reviews.items():
    for original in review['findings']:
        row = {**original, 'domain': domain, 'status': 'open', 'domain_record': f'{domain}/review.json'}
        row['session_origin'] = 'pre_existing_adjacent' if row['id'] == 'SESSION-RUNTIME-03' else 'session_change_or_incomplete_integration'
        findings.append(row)
    for original in review.get('inventory', review.get('algorithm_inventory', [])):
        row = {**original, 'domain': domain, 'entry_id': f'SA-{len(inventory)+1:02d}'}
        row['name'] = row.get('name', row.get('algorithm'))
        inventory.append(row)
findings.sort(key=lambda row: (row['priority'], row['id']))
assert len(findings) == 12 and len({row['id'] for row in findings}) == 12

backend_log = (BASE/'root/pytest.log').read_text()
backend = re.search(r'(\d+) passed, (\d+) skipped, (\d+) warnings in ([\d.]+)s', backend_log)
frontend = re.search(r'Tests\s+(\d+) passed', (BASE/'root/frontend.log').read_text())
if not backend or re.search(r'^FAILED |\d+ failed,', backend_log, re.M) or not frontend:
    raise RuntimeError('Fresh suite results incomplete or failed')
if 'Generated TypeScript contract matches.' not in (BASE/'root/contracts.log').read_text():
    raise RuntimeError('Fresh contract result missing')
if 'error TS' in (BASE/'root/typecheck.log').read_text():
    raise RuntimeError('Fresh TypeScript errors')
if 'Finalizing page optimization' not in (BASE/'root/build.log').read_text():
    raise RuntimeError('Fresh build result missing')

reopened = {
    'E-F04': ['SESSION-E02','SESSION-E03'], 'E-G01': ['SESSION-E01'],
    'RUNTIME-01': ['SESSION-RUNTIME-01'], 'ROOT-01': ['SESSION-RUNTIME-02'],
    'ROOT-02': ['SESSION-UI01'], 'EVAL-01': ['SESSION-EVAL-01'],
    'EVAL-02': ['SESSION-EVAL-03'], 'EVAL-03': ['SESSION-EVAL-02']}
summary = {
    'audit_date': '2026-11-17', 'timezone': 'America/Phoenix', 'scope': 'Everything built during the algorithm audit/design/repair session and its relevant integration boundaries',
    'inventory_entries': len(inventory), 'inventory_by_domain': dict(Counter(row['domain'] for row in inventory)),
    'findings': len(findings), 'priority_counts': dict(Counter(row['priority'] for row in findings)),
    'session_change_findings': 11, 'pre_existing_adjacent_findings': 1,
    'all_findings_status': 'open', 'application_source_changes': [],
    'source_test_contract_inputs_unchanged': len(expected), 'historical_artifacts_preserved': preserved,
    'fresh_checks': {'backend_passed':int(backend[1]),'backend_skipped':int(backend[2]),'backend_warnings':int(backend[3]),'backend_seconds':float(backend[4]),
        'frontend_passed':int(frontend[1]),'contracts':'passed','typecheck':'passed','production_build':'passed'},
    'paid_provider_calls':0, 'new_deployments':0, 'new_postgres_or_native_profiles_run':False,
    'prior_closures_requiring_qualification':reopened,
    'scope_limits':['Inventory entries are reviewed responsibilities, not distinct mathematical algorithms or formal absence-of-bug proofs.',
        'The original source-input manifest was checked before review and again at assembly; audit probes and temporary copies did not modify product/test/history inputs.',
        'Fresh probes assert reproduced failures; they are separate from the ordinary suites and are not repaired-product regression passes.',
        'The 40-finding ancestry API500 is source-confirmed; bounded feedback failure and readable existing version are directly reproduced.',
        'Fresh PostgreSQL/Redis worker-process, native browser/container, hosted staging and funded human/provider evaluations remain outside this run.',
        'Current numerical test totals were independently confirmed; the stale-log finding concerns the repair record generator and future rebinding, not fabricated current results.']}

for name, value in (('findings.json',findings),('review-inventory.json',inventory),('summary.json',summary),('source-manifest.json',expected)):
    (BASE/name).write_text(json.dumps(value,indent=2)+'\n')

report = f'''ROUND ROBIN — AUDIT OF THIS SESSION'S BUILT WORK
November 17, 2026 (America/Phoenix)

The earlier blanket completion claim was too broad. This independent audit found12 open issues:3 high-priority defects and9 medium-priority defects. Eleven concern session changes or incomplete integration; one is a pre-existing fallback defect in a touched module. No application source, existing tests or earlier audit records were changed during this audit.

The highest-priority problems are new reassessments that remain ready after quote expiry; objection resolutions that can cite an obsolete synthesis answer; and activity pagination that can hide Stop while a run continues. Passing the standard suites does not cover these scenarios.

Fresh verification

Backend: {backend[1]} passed, {backend[2]} skipped, {backend[3]} warnings, {backend[4]} seconds (root/pytest.log).
Frontend: {frontend[1]} passed (root/frontend.log).
Contracts/client drift check, TypeScript check and production build passed (root/contracts.log, root/typecheck.log, root/build.log).
The repair manifest's287 current source/test/contract inputs still match their recorded hashes. Historical audit, design and repair evidence indexes were also checked unchanged. Focused domain checks overlap the full suite and are not added to its total.

Review scope

This audit covers{len(inventory)} reviewed responsibilities across runtime/providers/context admission, claims/objections/attestations/retrieval, deterministic report/readiness/exports, visual feedback/regression/supplements, experimental identity/measurement/blinded artifacts, frontend flows/generated contracts, regression boundaries and the session's audit/design/repair tooling and completion claims. Responsibilities overlap; their count is not a count of unique mathematical algorithms or a claim that every unrelated existing source line was formally verified.

Complete machine-readable findings and exact references: findings.json. Complete review inventory: review-inventory.json. Domain records include their source inventories, fresh test commands, probes and caveats. All findings below remain open; this task was an audit.

'''
for row in findings:
    report += f"{row['id']} — {row['priority']} — {row['title']}\nOrigin: {row['session_origin']}\n"
    refs = row.get('source_refs',[])
    report += 'Source: ' + ('; '.join(refs) if isinstance(refs,list) else refs) + '\n'
    for keys, label in ((('trigger',),'Trigger'),(('actual','observed','defect'),'Observed'),(('impact',),'Impact'),
                        (('recommendation','fix','fix_direction'),'Repair direction'),(('caveat','qualification'),'Limit')):
        value = next((row[key] for key in keys if row.get(key)),None)
        if value: report += f'{label}: {value}\n'
    proof = row.get('proof',row.get('validation',row.get('evidence','')))
    report += f"Evidence: {row['domain_record']}; {proof}\n\n"

report += '''Consequences for the previous closure claims

E-F04, E-G01, RUNTIME-01, ROOT-01/02 and EVAL-01/02/03 need qualification at the new integration boundaries recorded in summary.json. Their repairs and regression tests exist, but those tests do not establish that every intended scenario is closed. This audit does not claim that all work was ineffective: independent proposals, due-time guards, scoped identities, conditional decisions, immutable raw reports and many existing controls passed their reviewed checks. No new defect was observed in several inspected responsibilities; that is not an absence-of-fault proof.

The obsolete-synthesis and quote-expiry issues are deterministic workflow acceptance gaps. They do not claim the application can prove semantic truth or that every live model would produce the bad sequence. The expiry case was additionally reproduced through a real authenticated owner API against a temporary completed fixture: a Nov17 requirement attestation returned201 and produced ready reportv7 while all cost receipts remained datedOct28 and the quotes expiredNov10. Fixture labels/usage were synthetic and no paid model call occurred.

Markdown and PDF lose actual resolution events even though raw JSON and the UI retain them. A visually plausible export therefore cannot substitute for reference/event fidelity. The exported counter-basis omission is also noted in the domain review; the principal reproduced finding is disappearance of the resolution explanations.

The visual prompt failure was reproduced through the actual worker with an injected MockTransport:0 HTTP requests,1 consumed request/submission,0.40 USD retained as unknown exposure. That is falsely unresolved conservative exposure, not a demonstrated external charge. The80-blocker boundary preserves the findings and keeps the existing version readable; it fails to offer a usable next edit and has an uncaught route exception. The study-reference finding affects blinded reviewer output; private stage storage retains the claim definition.

The reporting-tool defect was reproduced on temporary copies of real successful logs and source. Source was changed to invalid Python after those logs; finalize.py then created a new manifest binding that source and marked all17 closures verified. Its declarations about some checks are unconditional. This audit independently reran the current suites, so the existing2014/250 results are confirmed. One subordinate evidence-lane source hash for claims.py is stale after the final claim-kind guard; the later root repair manifest correctly covers that current file. Root-manifest consistency and execution provenance are different properties.

Suggested repair order

1. Bind reassessment freshness to the new assessment date and invalidate expired cost support while preserving historical reports.
2. Bind objection answers/resolutions to the exact current synthesis, claim versions and packet; require explicit reviewed carry-forward after replacement.
3. Publish valid run/report state before independently loading history, retaining Stop and result access during activity failures.
4. Unify text/image capacity priority; run actual provider payload/context checks before accepting Start; move all pure visual prompt checks before authorization and classify provably unsent failures correctly.
5. Preserve full ledger/counter-basis content in exports, support bounded visual overflow with an explicit recoverable outcome, and include durable claim definitions in the blinded study reference closure.
6. Add assessment-read recovery, bind verification receipts to command exits and test-start source inputs, and repair fallback traversal without broadening any funding or retry ceiling.

Verification limits

No funded model/human evaluation or new hosted/native/worker-process profile ran.43 optional default-suite tests remain skipped. The earlier12 PostgreSQL and8 sample-browser checks remain historical results for the unchanged source; this audit freshly reran backend/frontend/contracts/type/build and added adverse component/worker/API/export/report probes. No exhaustive coverage percentage is asserted. The ordinary suites passing does not supersede the12 open findings. New probe assertions deliberately verify the defective current behavior so that repairs can later turn them into regression tests asserting the desired behavior.
'''
for before,after in (('found12','found 12'),('issues:3','issues: 3'),('and9','and 9'),('covers'+str(len(inventory)),'covers '+str(len(inventory))),
                     ("manifest's287","manifest's 287"),('returned201','returned 201'),('reportv7','report v7'),('datedOct28','dated Oct28'),
                     ('expiredNov10','expired Nov10'),('requests,1','requests, 1'),('submission,0.40','submission, 0.40'),('all17','all 17'),
                     ('existing2014/250','existing 2014/250'),('ran.43','ran. 43'),('earlier12','earlier 12'),('and8','and 8'),('the12','the 12'),('The80','The 80'),
                     ('contains12,000','contains 12,000'),('characters,4,000','characters, 4,000'),('characters and4,000','characters and 4,000'),
                     ('context32,768','context 32,768'),('is32,325','is 32,325'),('under48K','under 48K'),('rebound to2014','rebound to 2014'),
                     ('current2014','current 2014'),('MockTransport:0','MockTransport: 0')):
    report=report.replace(before,after)
(BASE/'REPORT.txt').write_text(report)
index = [{'path':str(path.relative_to(BASE)),'sha256':hashlib.sha256(path.read_bytes()).hexdigest(),'bytes':path.stat().st_size}
         for path in sorted(BASE.rglob('*')) if path.is_file() and '__pycache__' not in path.parts and path.name!='evidence-index.json']
(BASE/'evidence-index.json').write_text(json.dumps(index,indent=2)+'\n')
print(json.dumps(summary,indent=2))
