"""Opt-in real PostgreSQL/Redis/Celery processes, never eager mode.

Set ROUND_ROBIN_TEST_DATABASE_URL ending in _test, a loopback broker database
14, and ROUND_ROBIN_TEST_ALLOW_LOOPBACK=1. Each test owns one random
PostgreSQL schema, Redis queue and process group; cleanup touches only those.
"""

from contextlib import contextmanager
from datetime import timedelta
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from uuid import uuid4

import pytest
import redis
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import sessionmaker

from round_robin.database.models import Attempt, Base, BudgetOperation, Event, Outbox, ReportVersion, Run, Step, utcnow
from round_robin.database.safety import require_disposable_postgres, require_disposable_redis


ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "backend" / "tests"))
from test_orchestration import seed


def _process(process, log_path, *, stop=False):
    if stop and process.poll() is None:
        os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=8)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)


@pytest.fixture
def process_stack(tmp_path):
    database = os.getenv("ROUND_ROBIN_TEST_DATABASE_URL")
    broker = os.getenv("ROUND_ROBIN_TEST_BROKER_URL")
    if not database or not broker or os.getenv("ROUND_ROBIN_TEST_ALLOW_LOOPBACK") != "1":
        pytest.skip("requires explicit disposable PostgreSQL/Redis process profile")
    try:
        require_disposable_postgres(database)
        require_disposable_redis(broker)
    except ValueError:
        pytest.fail("refusing non-disposable process test resources")
    db_parts = urlsplit(database)
    schema, queue = "rr_worker_" + uuid4().hex, "rr_worker_" + uuid4().hex
    admin = create_engine(database)
    with admin.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    options = dict(parse_qsl(db_parts.query))
    options["options"] = f"-csearch_path={schema}"
    scoped_database = urlunsplit(db_parts._replace(query=urlencode(options)))
    engine = create_engine(scoped_database)
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    env = {**os.environ, "PYTHONPATH": str(ROOT / "backend" / "src"), "ROUND_ROBIN_DATABASE_URL": scoped_database, "ROUND_ROBIN_BROKER_URL": broker, "ROUND_ROBIN_TASK_QUEUE": queue, "ROUND_ROBIN_DISPATCH_INTERVAL_SECONDS": "0.15", "ROUND_ROBIN_EXECUTION_MODE": "simulated"}
    processes = []
    logs = []
    def start(module, *args):
        log = tmp_path / f"process-{len(processes)}.log"
        stream = log.open("wb")
        process = subprocess.Popen([sys.executable, "-m", module, *args], env=env, cwd=ROOT, stdout=stream, stderr=subprocess.STDOUT, start_new_session=True)
        stream.close()
        processes.append(process)
        logs.append(log)
        return process
    def worker():
        return start("celery", "-A", "round_robin.worker.app:celery_app", "worker", "--pool=prefork", "--concurrency=3", "--loglevel=WARNING", "--without-gossip", "--without-mingle", "--without-heartbeat", "--queues", queue)
    try:
        yield {"factory": factory, "worker": worker, "dispatcher": lambda: start("round_robin.dispatcher.service"), "logs": logs, "queue": queue}
    finally:
        for process, log in zip(processes, logs):
            _process(process, log, stop=True)
        engine.dispose()
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()
        # Kombu queue keys have explicit queue names; no flushdb or broad scan.
        client = redis.Redis.from_url(broker)
        for key in [queue, f"_kombu.binding.{queue}", *(f"{queue}\x06\x16{priority}" for priority in (3, 6, 9))]:
            client.delete(key)
        client.close()


def wait_for(factory, run_id, statuses, logs, *, timeout=60):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with factory() as session:
            run = session.get(Run, run_id)
            if run and run.status in statuses:
                return run.status
        time.sleep(0.1)
    diagnostics = "\n".join(path.read_text(errors="replace")[-4000:] for path in logs)
    pytest.fail(f"disposable worker did not settle: {diagnostics}")


def test_actual_worker_dispatcher_delivers_once_across_processes(process_stack):
    from round_robin.orchestration.admission import lock_work_run
    from round_robin.orchestration.controller import advance_run
    from round_robin.database.services import enqueue
    stack = process_stack
    factory = stack["factory"]
    run_id = seed(factory)
    with factory() as session, session.begin():
        run = lock_work_run(session, run_id)
        advance_run(session, run)
        step = session.scalar(select(Step).where(Step.run_id == run_id))
        enqueue(session, run, "execute", step_id=step.id)
        enqueue(session, run, "execute", step_id=step.id)
    stack["worker"]()
    stack["dispatcher"]()
    assert wait_for(factory, run_id, {"completed", "failed", "completed_with_limitations"}, stack["logs"]) == "completed"
    with factory() as session:
        attempts = list(session.scalars(select(Attempt).where(Attempt.run_id == run_id)))
        assert len(attempts) == 6 and all(attempt.status == "succeeded" for attempt in attempts)
        steps = list(session.scalars(select(Step).where(Step.run_id == run_id)))
        assert all(step.calls == 1 for step in steps)
        assert len(list(session.scalars(select(ReportVersion)))) == 1
        events = list(session.scalars(select(Event).where(Event.run_id == run_id).order_by(Event.sequence)))
        assert [event.sequence for event in events] == list(range(1, len(events) + 1))


def test_killed_worker_after_authorization_retains_unknown_without_paid_replay(process_stack):
    from round_robin.api.preflight import run_configuration
    from round_robin.configuration import Settings
    stack = process_stack
    factory = stack["factory"]
    config = run_configuration(Settings(), "simulated")
    config["simulated_faults"] = {"openai": "test_slow_success"}
    run_id = seed(factory, config=config)
    worker = stack["worker"]()
    stack["dispatcher"]()
    deadline = time.monotonic() + 30
    attempt_id = None
    while time.monotonic() < deadline:
        with factory() as session:
            attempt_id = session.scalar(select(Attempt.id).where(Attempt.run_id == run_id, Attempt.status == "authorized"))
        if attempt_id:
            break
        time.sleep(0.05)
    assert attempt_id is not None
    os.killpg(worker.pid, signal.SIGKILL)
    worker.wait(timeout=5)
    with factory() as session, session.begin():
        step = session.scalar(select(Step).where(Step.run_id == run_id))
        step.lease_expires_at = utcnow() - timedelta(seconds=1)
    stack["worker"]()
    assert wait_for(factory, run_id, {"failed", "completed_with_limitations"}, stack["logs"]) == "failed"
    with factory() as session:
        assert session.get(Attempt, attempt_id).usage_certainty == "unknown"
        assert session.get(BudgetOperation, attempt_id).status == "unknown"
        assert len(list(session.scalars(select(Attempt).where(Attempt.run_id == run_id)))) == 1
        report = session.scalar(select(ReportVersion)).content
        assert report["recommendation"] is None and report["usage"]["unknown_operation_ids"] == [attempt_id]
