"""Launch the local durable product with explicitly simulated execution."""

import argparse
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend/src"))
from round_robin.operations.processes import terminate_owned


def validate_local_targets(environment):
    """Never migrate or start a development worker against inherited remote data."""
    from round_robin.database.safety import require_disposable_postgres, require_disposable_redis
    from sqlalchemy.engine import make_url
    from sqlalchemy.exc import ArgumentError
    try:
        database = require_disposable_postgres(environment.get("ROUND_ROBIN_DATABASE_URL", "postgresql+psycopg://round_robin:round_robin_local@127.0.0.1:55432/round_robin"), suffix="", environment=environment)
        broker = make_url(environment.get("ROUND_ROBIN_BROKER_URL", "redis://127.0.0.1:56379/0"))
        if not database.database or not (broker.database or "").isdigit():
            raise ValueError
        require_disposable_redis(broker, database=int(broker.database))
    except (ArgumentError, ValueError, TypeError):
        raise SystemExit("Local development requires explicit loopback PostgreSQL and Redis without routing overrides; no services or migrations were started") from None


def available(port):
    with socket.socket() as probe:
        # Match Uvicorn's listener behavior after a graceful stop: recently
        # closed connections in TIME_WAIT must not look like a live server.
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skip-services", action="store_true", help="Use existing configured PostgreSQL/Redis rather than starting Compose services")
    parser.add_argument("--check", action="store_true", help="Check local services and migrations without launching processes")
    args = parser.parse_args()
    app_env = {**os.environ, "PYTHONPATH": str(ROOT / "backend/src"), "ROUND_ROBIN_ENV": "development", "ROUND_ROBIN_EXECUTION_MODE": "simulated", "ROUND_ROBIN_PAID_ACCESS": "0", "ROUND_ROBIN_BROKER_URL": os.environ.get("ROUND_ROBIN_BROKER_URL", "redis://127.0.0.1:56379/0"), "NEXT_TELEMETRY_DISABLED": "1"}
    validate_local_targets(app_env)
    if not args.check:
        for port in (8000, 3000):
            if not available(port):
                raise SystemExit(f"Local port {port} is already in use. Stop its existing app or run the web/API commands separately.")
    if not args.skip_services:
        subprocess.run(["docker", "compose", "up", "-d", "--wait", "postgres", "redis"], cwd=ROOT, check=True, env=app_env)
    subprocess.run(["uv", "run", "alembic", "upgrade", "head"], cwd=ROOT, check=True, env=app_env)
    subprocess.run(["uv", "run", "python", "-m", "round_robin.ops", "config-check"], cwd=ROOT, check=True, env=app_env)
    if args.check:
        return 0
    processes = []
    stopped = False

    def stop(*_):
        nonlocal stopped
        stopped = True

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    commands = [
        ["uv", "run", "uvicorn", "round_robin.app:app", "--host", "127.0.0.1", "--port", "8000"],
        ["uv", "run", "celery", "-A", "round_robin.worker.app:celery_app", "worker", "--pool=prefork", "--concurrency=3", "--loglevel=WARNING"],
        ["uv", "run", "python", "-m", "round_robin.dispatcher.service"],
        ["pnpm", "--filter", "@round-robin/web", "dev"],
    ]
    try:
        for command in commands:
            processes.append(subprocess.Popen(command, cwd=ROOT, env=app_env, start_new_session=True))
        ready = False
        for _ in range(60):
            if stopped or any(process.poll() is not None for process in processes):
                break
            try:
                with urlopen("http://127.0.0.1:8000/ready", timeout=.5) as response:
                    ready = response.status == 200
                if ready:
                    ready = not any(process.poll() is not None for process in processes)
                    break
            except OSError:
                time.sleep(.25)
        if not stopped and not ready:
            print("The local workspace did not become ready before its startup deadline; stopping its processes.", file=sys.stderr)
            return next((process.returncode for process in processes if process.returncode), 1)
        if ready:
            print("\nRound Robin local workspace: http://127.0.0.1:3000/live\nFictional sample: http://127.0.0.1:3000\nExecution is simulated. Stop with Ctrl+C.\n", flush=True)
        while not stopped:
            exited = [process for process in processes if process.poll() is not None]
            if exited:
                print("A local process exited; stopping its sibling processes. Saved comparisons remain in PostgreSQL.", file=sys.stderr)
                return next((process.returncode for process in exited if process.returncode), 1)
            time.sleep(.3)
        return 0
    finally:
        terminate_owned(processes)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except subprocess.CalledProcessError:
        raise SystemExit("Local setup failed. Start Docker (for example colima start), then retry; existing data was not reset.")
