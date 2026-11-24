"""Verify offline code, optionally with isolated PostgreSQL/Redis process checks."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import platform
import signal
import socket
import subprocess
import sys
import tempfile
import time
from urllib.request import urlopen
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
# This repository intentionally is not an installed Python package. The
# verifier itself also imports the shared guards, beyond its configured child
# processes, so direct CLI invocation needs the same source path.
sys.path.insert(0, str(ROOT / 'backend/src'))
from round_robin.operations.processes import terminate_owned


def free_port(port):
    with socket.socket() as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind(('127.0.0.1', port))
            return True
        except OSError:
            return False


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / '.local/evidence/implementation-checks.json')
    parser.add_argument('--services', action='store_true', help='Also check actual service processes and connected browser journeys in a disposable schema')
    parser.add_argument('--native', action='store_true', help='Also execute the explicitly built native-renderer Docker sandbox checks')
    args = parser.parse_args()
    app_env = {**os.environ, 'PYTHONPATH': str(ROOT / 'backend/src'), 'NEXT_TELEMETRY_DISABLED': '1', 'CI': 'true', 'ROUND_ROBIN_ENV': 'development', 'ROUND_ROBIN_EXECUTION_MODE': 'simulated', 'ROUND_ROBIN_PAID_ACCESS': '0', 'ROUND_ROBIN_TEST_ALLOW_LOOPBACK': '0', 'ROUND_ROBIN_TEST_NATIVE_RENDERER': '0', 'ROUND_ROBIN_NATIVE_INTERFACE_ENABLED': '0'}
    app_env.pop('ROUND_ROBIN_DURABILITY_CONTROL_DIRECTORY', None)
    record = {'recorded_at_utc': datetime.now(timezone.utc).isoformat(), 'scope': 'Phase-by-phase implementation; simulated product execution and bounded disposable services', 'service_profile': args.services, 'native_profile': args.native, 'docker_broker_fault_profile': args.services and os.environ.get('ROUND_ROBIN_TEST_DOCKER_FAULTS') == '1', 'phase_or_release_gates_passed': False, 'live_provider_calls': 0, 'environment': {'python': platform.python_version(), 'platform': platform.system(), 'machine': platform.machine()}, 'checks': []}
    def save_record():
        args.output.parent.mkdir(parents=True, exist_ok=True)
        temporary = args.output.with_name(args.output.name + '.' + uuid4().hex + '.pending')
        try:
            temporary.write_text(json.dumps(record, indent=2) + '\n')
            os.replace(temporary, args.output)
        finally:
            temporary.unlink(missing_ok=True)

    # Invalidate a previous success before starting any checks or cleanup.
    record.update(all_checks_passed=False, status='running')
    save_record()
    failed = False
    disposable = tempfile.TemporaryDirectory(prefix='round-robin-verification-')
    # Unscoped imports/CLI checks must never inherit an operator's actual DB,
    # storage or browser API target. Individual tests still supply their own
    # databases; provider credentials remain intact so collection rejects them.
    app_env.update({
        'ROUND_ROBIN_DATABASE_URL': 'sqlite:///' + str(Path(disposable.name) / 'offline.sqlite'),
        'ROUND_ROBIN_ARTIFACT_DIRECTORY': str(Path(disposable.name) / 'offline-artifacts'),
        'NEXT_PUBLIC_ROUND_ROBIN_API_URL': 'http://127.0.0.1:8000',
        'ROUND_ROBIN_E2E_API_URL': 'http://127.0.0.1:8000',
    })
    processes, log_handles = [], []
    test_engine, schema, schema_created = None, None, False

    def check(command, environment=None):
        nonlocal failed
        started = time.monotonic()
        result = subprocess.run(command, cwd=ROOT, env=environment or app_env, capture_output=True, text=True)
        clean = lambda value: str(value).replace(str(ROOT), '<workspace>').replace(disposable.name, '<disposable-verification-directory>')
        output, display = clean(result.stdout + result.stderr), [clean(item) for item in command]
        record['checks'].append({'command': display, 'exit_code': result.returncode, 'elapsed_seconds': round(time.monotonic() - started, 3), 'output': output})
        print(f"{'PASS' if result.returncode == 0 else 'FAIL'} {' '.join(display)}", flush=True)
        if result.returncode:
            print(output[-18000:], file=sys.stderr)
            failed = True
            raise RuntimeError('verification_failed')

    def services_alive():
        if any(process.poll() is not None for process in processes):
            raise RuntimeError('An owned disposable service exited before verification completed')

    try:
        if args.services and (not free_port(8000) or not free_port(3000)):
            raise RuntimeError('Stop the dev supervisor before isolated verification; ports 8000 and 3000 must be free')
        # Credential/network guards run before any service or worker launch.
        for command in [
            ['uv', 'run', 'pytest', '-q'],
            ['uv', 'run', 'python', '-m', 'round_robin.export_contracts', '--check'],
            ['node', 'scripts/check_client.mjs'],
            ['node', '--test', 'scripts/frontend_coverage.test.mjs'],
            ['uv', 'run', 'python', '-m', 'experiments.evaluation', 'validate'],
            ['uv', 'run', 'python', 'scripts/scenario_summary.py', '--check'],
            ['pnpm', 'typecheck'], ['pnpm', 'build'],
        ]:
            check(command)
        smoke = ['uv', 'run', 'python', '-m', 'experiments.runner', '--journal-dir', str(Path(disposable.name) / 'journal'), '--batch-id', 'verification-001', '--simulate']
        check(smoke)
        check(smoke)
        if args.services:
            from sqlalchemy import create_engine, text
            from round_robin.database.safety import require_disposable_postgres, require_disposable_redis
            database = os.environ.get('ROUND_ROBIN_TEST_DATABASE_URL', 'postgresql+psycopg://round_robin:round_robin_local@127.0.0.1:55432/round_robin_test')
            broker = os.environ.get('ROUND_ROBIN_TEST_BROKER_URL', 'redis://127.0.0.1:56379/14')
            try:
                url, broker_url = require_disposable_postgres(database), require_disposable_redis(broker)
            except ValueError:
                raise RuntimeError('Service checks require loopback PostgreSQL *_test and isolated Redis database 14')
            if not free_port(8000) or not free_port(3000):
                raise RuntimeError('Stop the dev supervisor before isolated verification; ports 8000 and 3000 must be free')
            service_env = {**app_env, 'ROUND_ROBIN_TEST_ALLOW_LOOPBACK': '1', 'ROUND_ROBIN_TEST_DATABASE_URL': database, 'ROUND_ROBIN_TEST_BROKER_URL': broker}
            check(['uv', 'run', 'pytest', 'backend/tests/integration', '-q'], service_env)
            check(['uv', 'run', 'pytest', 'backend/tests/test_fetcher_transport.py', '-q'], service_env)
            schema = 'rr_browser_' + uuid4().hex
            test_engine = create_engine(database, hide_parameters=True)
            with test_engine.begin() as connection:
                connection.execute(text(f'CREATE SCHEMA "{schema}"'))
            schema_created = True
            browser_url = url.update_query_dict({'options': f'-csearch_path={schema}'}).render_as_string(hide_password=False)
            service_env = {**service_env, 'ROUND_ROBIN_DATABASE_URL': browser_url, 'ROUND_ROBIN_BROKER_URL': broker, 'ROUND_ROBIN_TASK_QUEUE': 'rr_verify_' + uuid4().hex, 'ROUND_ROBIN_ARTIFACT_DIRECTORY': str(Path(disposable.name) / 'artifacts'), 'ROUND_ROBIN_DURABILITY_CONTROL_DIRECTORY': str(Path(disposable.name) / 'durability-control'), 'CI': 'true'}
            check(['uv', 'run', 'alembic', 'upgrade', 'head'], service_env)
            check(['uv', 'run', 'alembic', 'check'], service_env)
            for name, command in [
                ('api', ['uv', 'run', 'uvicorn', 'round_robin.app:app', '--host', '127.0.0.1', '--port', '8000']),
                ('worker', ['uv', 'run', 'celery', '-A', 'round_robin.worker.app:celery_app', 'worker', '--pool=prefork', '--concurrency=3', '--loglevel=WARNING']),
                ('dispatcher', ['uv', 'run', 'python', '-m', 'round_robin.dispatcher.service']),
            ]:
                handle = (Path(disposable.name) / f'{name}.log').open('w+')
                log_handles.append(handle)
                processes.append(subprocess.Popen(command, cwd=ROOT, env=service_env, stdout=handle, stderr=subprocess.STDOUT, start_new_session=True))
            for _ in range(100):
                services_alive()
                try:
                    with urlopen('http://127.0.0.1:8000/ready', timeout=.3) as response:
                        if response.status == 200:
                            break
                except OSError:
                    time.sleep(.2)
            else:
                raise RuntimeError('The disposable API did not become ready')
            services_alive()
            check(['pnpm', 'test:e2e'], service_env)
            services_alive()
        else:
            check(['pnpm', 'test:e2e', 'prototype.spec.ts'])
        if args.native:
            native_env = {**app_env, 'ROUND_ROBIN_TEST_NATIVE_RENDERER': '1', 'ROUND_ROBIN_NATIVE_RENDERER_IMAGE': os.environ.get('ROUND_ROBIN_NATIVE_RENDERER_IMAGE', 'round-robin-native-renderer:local')}
            check(['docker', 'image', 'inspect', native_env['ROUND_ROBIN_NATIVE_RENDERER_IMAGE'], '--format', '{{.Id}}'], native_env)
            check(['uv', 'run', 'pytest', 'backend/tests/integration/test_native_renderer.py', '-q'], native_env)
        services_alive()
    except KeyboardInterrupt:
        failed = True
        record['verification_error'] = 'Verification interrupted'
        record['status'] = 'interrupted'
        save_record()
    except Exception as exception:
        failed = True
        record['verification_error'] = str(exception) if isinstance(exception, RuntimeError) else 'Local verification setup failed; protected diagnostics required'
        print(record['verification_error'], file=sys.stderr)
    finally:
        try:
            terminate_owned(processes)
        except Exception:
            failed = True
            record['process_cleanup_error'] = 'An owned process group could not be stopped'
        if failed and log_handles:
            record['service_diagnostics'] = []
            for handle in log_handles:
                handle.flush()
                handle.seek(0)
                record['service_diagnostics'].append(handle.read()[-6000:].replace(str(ROOT), '<workspace>').replace(disposable.name, '<disposable-verification-directory>'))
        for handle in log_handles:
            handle.close()
        if test_engine is not None:
            try:
                if schema_created:
                    from sqlalchemy import text
                    with test_engine.begin() as connection:
                        connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
            except Exception:
                failed = True
                record['cleanup_error'] = 'Owned disposable schema could not be removed; no production resource was targeted'
            finally:
                test_engine.dispose()
        disposable.cleanup()
    record['all_checks_passed'] = not failed
    if record['status'] != 'interrupted':
        record['status'] = 'failed' if failed else 'completed'
    save_record()
    print(f'Evidence saved to {args.output}')
    return 0 if not failed else 1


if __name__ == '__main__':
    raise SystemExit(main())
