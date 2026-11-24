"""Show whether the session record generator rejects stale successful logs.

Uses only temporary copies of actual sources/logs. Does not edit prior evidence.
Run: uv run python docs/evidence/session-audit-2026-11-17/root/probe_record_binding.py
"""
import contextlib
import hashlib
import io
import json
from pathlib import Path
import runpy
import shutil
from tempfile import TemporaryDirectory


ROOT = Path(__file__).resolve().parents[4]
FIXES = ROOT / 'docs/evidence/algorithm-fixes-2026-11-17'


def run():
    manifest = json.loads((FIXES / 'source-manifest.json').read_text())
    with TemporaryDirectory(prefix='round-robin-record-binding-audit-') as directory:
        sandbox = Path(directory)
        for entry in manifest:
            destination = sandbox / entry['path']
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / entry['path'], destination)
        old = sandbox / 'docs/evidence/algorithm-audit-2026-11-17'
        old.mkdir(parents=True)
        for name in ('source-manifest.json', 'findings.json'):
            shutil.copyfile(ROOT / 'docs/evidence/algorithm-audit-2026-11-17' / name, old / name)
        copied = sandbox / 'docs/evidence/algorithm-fixes-2026-11-17'
        (copied / 'root').mkdir(parents=True)
        for name in ('pytest-release.log', 'frontend-complete.log', 'postgres.log', 'browser-sample.log',
                     'contracts-check.log', 'build-accepted.log', 'typecheck-accepted.log'):
            shutil.copyfile(FIXES / 'root' / name, copied / 'root' / name)
        shutil.copyfile(FIXES / 'finalize.py', copied / 'finalize.py')
        mutated = sandbox / 'backend/src/round_robin/providers/base.py'
        mutated.write_text('def intentionally_invalid_source(:\n')
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            runpy.run_path(str(copied / 'finalize.py'), run_name='__main__')
        summary = json.loads((copied / 'summary.json').read_text())
        bound = next(item for item in json.loads((copied / 'source-manifest.json').read_text())
                     if item['path'] == 'backend/src/round_robin/providers/base.py')
        closures = json.loads((copied / 'closures.json').read_text())
        result = {
            'temporary_copies_only': True,
            'inputs': 'Actual previous successful logs; copied source changed afterward to invalid Python.',
            'generator_returned_success': True,
            'new_manifest_binds_invalid_source': bound['sha256'] == hashlib.sha256(mutated.read_bytes()).hexdigest(),
            'reported_backend_passed': summary['backend']['passed'],
            'reported_contracts': summary['contracts'],
            'reported_typecheck': summary['typecheck'],
            'reported_build': summary['production_build'],
            'closures_marked_verified': sum(row['status'] == 'implemented_and_regression_verified' for row in closures),
            'evaluation_and_scenario_logs_present': (copied/'root/evaluation-validate.log').exists() or (copied/'root/scenarios.log').exists(),
            'caveat': 'This reproduces future stale-log rebinding; it does not invalidate the independently rerun current test totals.'
        }
        assert result['new_manifest_binds_invalid_source'] and result['closures_marked_verified'] == 17
        return result


if __name__ == '__main__':
    result = run()
    Path(__file__).with_name('record-binding-results.json').write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result, indent=2))
