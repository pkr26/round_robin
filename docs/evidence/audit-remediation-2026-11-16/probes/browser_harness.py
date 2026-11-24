"""Run all connected browser journeys in owned ports/schema/queue/build copy."""
from pathlib import Path
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from urllib.request import urlopen
from uuid import uuid4

from sqlalchemy import create_engine, text
from round_robin.database.safety import require_disposable_postgres, require_disposable_redis

ROOT = Path.cwd()
OUT = Path("/tmp/rr-fix-validation/browser")
OUT.mkdir(parents=True, exist_ok=True)
WORK = Path(tempfile.mkdtemp(prefix="rr-fix-browser-"))
DATABASE = "postgresql+psycopg://round_robin:round_robin_local@127.0.0.1:55432/round_robin_test"
BROKER = "redis://127.0.0.1:56379/14"
url = require_disposable_postgres(DATABASE)
require_disposable_redis(BROKER)
schema = "rr_fix_" + uuid4().hex
queue = "rr_fix_" + uuid4().hex
engine = create_engine(DATABASE)
processes, handles = [], []
created = False
result = {"schema": schema, "queue": queue, "api_port": 18000, "browser_port": 13000, "paid_provider_calls": 0, "checks": []}

def check(command, env, cwd=ROOT, timeout=360):
    name = f"check-{len(result['checks']) + 1:02d}"
    started = time.monotonic()
    with (OUT / f"{name}.log").open("w") as handle:
        completed = subprocess.run(command, cwd=cwd, env=env, stdout=handle, stderr=subprocess.STDOUT, timeout=timeout)
    entry = {"command": command, "exit_code": completed.returncode, "seconds": round(time.monotonic() - started, 2), "log": f"{name}.log"}
    result["checks"].append(entry)
    print(json.dumps(entry), flush=True)
    if completed.returncode:
        raise RuntimeError(f"{name} failed")

try:
    for port in (18000, 13000):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", port))
    for name in ("frontend", "contracts", "packages", "scripts"):
        shutil.copytree(ROOT / name, WORK / name, ignore=shutil.ignore_patterns("node_modules", ".next", "out", "__pycache__"))
    for name in ("package.json", "pnpm-lock.yaml", "pnpm-workspace.yaml", "tsconfig.json"):
        if (ROOT / name).exists():
            shutil.copy2(ROOT / name, WORK / name)
    (WORK / "node_modules").symlink_to(ROOT / "node_modules", target_is_directory=True)
    (WORK / "frontend/node_modules").symlink_to(ROOT / "frontend/node_modules", target_is_directory=True)
    preview = WORK / "scripts/serve_prototype.py"
    preview.write_text(preview.read_text().replace('("127.0.0.1", 3000)', '("127.0.0.1", 13000)'))
    with engine.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    created = True
    env = {**os.environ,
        "PYTHONPATH": str(ROOT / "backend/src") + ":" + str(ROOT),
        "ROUND_ROBIN_ENV": "development", "ROUND_ROBIN_EXECUTION_MODE": "simulated",
        "ROUND_ROBIN_PAID_ACCESS": "0", "ROUND_ROBIN_IMAGE_CONFIG_FILE": "",
        "ROUND_ROBIN_VISUAL_ENABLED": "1", "ROUND_ROBIN_NATIVE_DIAGRAM_ENABLED": "1",
        "ROUND_ROBIN_NATIVE_INTERFACE_ENABLED": "0", "ROUND_ROBIN_TEST_NATIVE_RENDERER": "0",
        "ROUND_ROBIN_TEST_ALLOW_LOOPBACK": "1",
        "ROUND_ROBIN_DATABASE_URL": url.update_query_dict({"options": f"-csearch_path={schema}"}).render_as_string(hide_password=False),
        "ROUND_ROBIN_BROKER_URL": BROKER, "ROUND_ROBIN_TASK_QUEUE": queue,
        "ROUND_ROBIN_ARTIFACT_DIRECTORY": str(WORK / "artifacts"),
        "ROUND_ROBIN_DURABILITY_CONTROL_DIRECTORY": str(WORK / "durability-control"),
        "ROUND_ROBIN_ALLOWED_ORIGINS": "http://127.0.0.1:13000",
        "ROUND_ROBIN_PUBLIC_ORIGIN": "http://127.0.0.1:13000",
        "NEXT_PUBLIC_ROUND_ROBIN_API_URL": "http://127.0.0.1:18000",
        "ROUND_ROBIN_E2E_API_URL": "http://127.0.0.1:18000",
        "NEXT_TELEMETRY_DISABLED": "1", "CI": "true",
        "OPENAI_API_KEY": "", "ANTHROPIC_API_KEY": "", "GEMINI_API_KEY": "", "GOOGLE_API_KEY": "", "BRAVE_SEARCH_API_KEY": "",
        "ROUND_ROBIN_E2E_COVERAGE": "0", "ROUND_ROBIN_SOURCE_COVERAGE": "0"}
    check(["pnpm", "build"], env, WORK)
    check([str(ROOT / ".venv/bin/alembic"), "upgrade", "head"], env)
    check([str(ROOT / ".venv/bin/alembic"), "check"], env)
    commands = [
        ("api", [str(ROOT / ".venv/bin/python"), "-m", "uvicorn", "round_robin.app:app", "--host", "127.0.0.1", "--port", "18000"]),
        ("worker", [str(ROOT / ".venv/bin/celery"), "-A", "round_robin.worker.app:celery_app", "worker", "--pool=prefork", "--concurrency=3", "--loglevel=WARNING"]),
        ("dispatcher", [str(ROOT / ".venv/bin/python"), "-m", "round_robin.dispatcher.service"]),
        ("web", [str(ROOT / ".venv/bin/python"), str(preview)])]
    for name, command in commands:
        handle = (OUT / f"{name}.log").open("w")
        handles.append(handle)
        processes.append(subprocess.Popen(command, cwd=ROOT, env=env, stdout=handle, stderr=subprocess.STDOUT, start_new_session=True))
    for endpoint in ("http://127.0.0.1:18000/ready", "http://127.0.0.1:13000"):
        for _ in range(100):
            if any(process.poll() is not None for process in processes):
                raise RuntimeError("Owned service exited")
            try:
                with urlopen(endpoint, timeout=.5) as response:
                    if response.status == 200:
                        break
            except OSError:
                pass
            time.sleep(.2)
        else:
            raise RuntimeError("Owned service readiness timeout")
    config = OUT / "playwright.config.cjs"
    config.write_text("const { defineConfig, devices } = require(" + json.dumps(str(ROOT / "node_modules/@playwright/test")) + ");\nmodule.exports = defineConfig(" + json.dumps({
        "testDir": str(ROOT / "tests/e2e"), "fullyParallel": True, "workers": 2,
        "forbidOnly": True, "retries": 0,
        "reporter": [["list"], ["json", {"outputFile": str(OUT / "playwright-results.json")}]],
        "outputDir": str(OUT / "test-results"),
        "use": {"baseURL": "http://127.0.0.1:13000", "browserName": "chromium", "viewport": {"width": 1280, "height": 720}, "trace": "retain-on-failure", "screenshot": "only-on-failure"}}) + ");\n")
    check(["pnpm", "exec", "playwright", "test", "--config", str(config)], env, timeout=600)
    result["status"] = "passed"
except Exception as exc:
    result["status"] = "failed"
    result["error"] = type(exc).__name__ + ": " + str(exc)
finally:
    for process in reversed(processes):
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
    for process in processes:
        try:
            process.wait(timeout=8)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=5)
    for handle in handles:
        handle.close()
    if created:
        with engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
    engine.dispose()
    from redis import Redis
    redis = Redis.from_url(BROKER)
    for pattern in (queue + "*", "_kombu.binding." + queue + "*"):
        keys = list(redis.scan_iter(match=pattern))
        if keys:
            redis.delete(*keys)
    redis.close()
    shutil.rmtree(WORK)
    result["owned_resources_cleaned"] = True
    (OUT / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2), flush=True)
sys.exit(0 if result["status"] == "passed" else 1)
