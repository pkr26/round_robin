"""Actual PostgreSQL/Redis/Celery image processes with synthetic adapters only."""
from dataclasses import asdict
from datetime import timedelta
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

from celery import Celery
import pytest
import redis
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import sessionmaker

from backend.tests.test_image_provider import synthetic_image_configuration, pixels
from backend.tests.test_image_worker import seed_image
from round_robin.database.models import Base, BudgetOperation, PolicyBucket, utcnow
from round_robin.database.visual_models import DesignOperation, DesignReview, DesignSession, DesignVersion
from round_robin.orchestration.budget import operations_exposure
from round_robin.providers.images import ImageProviderConfiguration
from round_robin.database.services import canonical_hash
from round_robin.database.safety import require_disposable_postgres, require_disposable_redis
from round_robin.visual.service import version_view

ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture
def image_process_stack(tmp_path):
    database = os.environ.get("ROUND_ROBIN_TEST_DATABASE_URL")
    core_broker = os.environ.get("ROUND_ROBIN_TEST_BROKER_URL")
    if not database or not core_broker or os.environ.get("ROUND_ROBIN_TEST_ALLOW_LOOPBACK") != "1":
        pytest.skip("Requires explicit disposable PostgreSQL/Redis process profile")
    try:
        url = require_disposable_postgres(database)
        require_disposable_redis(core_broker)
    except ValueError:
        pytest.fail("Refusing image process tests outside loopback PostgreSQL *_test and reserved Redis databases 14/15")
    broker = urlsplit(core_broker)
    image_broker = urlunsplit(broker._replace(path="/15"))
    schema = "rr_image_" + uuid4().hex
    image_queue, core_queue = "rr_image_" + uuid4().hex, "rr_core_" + uuid4().hex
    administration = create_engine(database, hide_parameters=True)
    with administration.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    scoped = url.update_query_dict({"options": f"-csearch_path={schema}"}).render_as_string(hide_password=False)
    engine = create_engine(scoped, hide_parameters=True)
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    path = tmp_path / "synthetic-image-configuration.json"
    value = ImageProviderConfiguration.model_validate(synthetic_image_configuration()).model_dump(mode="json")
    path.write_text(json.dumps(value))
    configuration = {**value, "mode": "live", "visual_configuration": True, "operator_configuration_hash": canonical_hash(value)}
    environment = {**os.environ, "PYTHONPATH": str(ROOT / "backend/src") + os.pathsep + str(ROOT), "ROUND_ROBIN_DATABASE_URL": scoped, "ROUND_ROBIN_BROKER_URL": core_broker, "ROUND_ROBIN_IMAGE_BROKER_URL": image_broker, "ROUND_ROBIN_TASK_QUEUE": core_queue, "ROUND_ROBIN_IMAGE_TASK_QUEUE": image_queue, "ROUND_ROBIN_DISPATCH_INTERVAL_SECONDS": ".15", "ROUND_ROBIN_ENV": "development", "ROUND_ROBIN_PAID_ACCESS": "1", "ROUND_ROBIN_IMAGE_CONFIG_FILE": str(path), "ROUND_ROBIN_IMAGE_PROCESS_FIXTURE": "1", "ROUND_ROBIN_ARTIFACT_DIRECTORY": str(tmp_path / "artifacts")}
    processes, logs = [], []
    def launch(module, *arguments, behavior="normal"):
        log = tmp_path / f"image-process-{len(processes)}.log"
        with log.open("wb") as stream:
            process = subprocess.Popen([sys.executable, "-m", module, *arguments], env={**environment, "ROUND_ROBIN_IMAGE_FIXTURE_BEHAVIOR": behavior}, cwd=ROOT, stdout=stream, stderr=subprocess.STDOUT, start_new_session=True)
        processes.append(process)
        logs.append(log)
        return process
    def worker(behavior="normal"):
        return launch("celery", "-A", "backend.tests.helpers.image_worker:image_app", "worker", "--pool=prefork", "--concurrency=1", "--loglevel=WARNING", "--without-gossip", "--without-mingle", "--without-heartbeat", "--hostname", image_queue + "@localhost", "--queues", image_queue, behavior=behavior)
    sender = Celery("image_process_test_sender", broker=image_broker)
    sender.conf.update(task_serializer="json", accept_content=["json"], task_publish_retry=False)
    try:
        yield {"factory": factory, "worker": worker, "dispatcher": lambda: launch("backend.tests.helpers.image_dispatcher"), "logs": logs, "configuration": configuration, "queue": image_queue, "sender": sender, "artifacts": tmp_path / "artifacts"}
    finally:
        for process in processes:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=7)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=3)
        sender.close()
        engine.dispose()
        with administration.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        administration.dispose()
        for address, queue in ((core_broker, core_queue), (image_broker, image_queue)):
            client = redis.Redis.from_url(address)
            client.delete(queue, f"_kombu.binding.{queue}", *(f"{queue}\x06\x16{priority}" for priority in (3, 6, 9)))
            # A killed worker can leave an unacknowledged delivery. Remove only
            # this exact generated queue's delivery tags; never flush a broker.
            for tag, encoded in client.hgetall("unacked").items():
                try:
                    message = json.loads(encoded)
                except (TypeError, ValueError):
                    continue
                if isinstance(message, list) and len(message) == 3 and message[2] == queue:
                    client.hdel("unacked", tag)
                    client.zrem("unacked_index", tag)
            client.close()


def wait_for(stack, predicate, *, timeout=35):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with stack["factory"]() as session:
            result = predicate(session)
            if result:
                return result
        time.sleep(.075)
    diagnostics = "\n".join(path.read_text(errors="replace")[-4000:] for path in stack["logs"])
    with stack["factory"]() as session:
        diagnostics += "\nOperation statuses: " + repr([(row.id, row.payload.get("status"), row.payload.get("dispatch_status"), row.payload.get("error_code")) for row in session.scalars(select(DesignOperation))])
    pytest.fail("Disposable image processes did not reach their bounded fixture state:\n" + diagnostics)


def test_actual_image_queue_commits_authorization_before_send_and_two_independent_pixel_reviews(image_process_stack):
    stack = image_process_stack
    design_id, operation_id = seed_image(stack["factory"], stack["configuration"])
    stack["worker"]()
    stack["dispatcher"]()
    wait_for(stack, lambda session: len(list(session.scalars(select(DesignReview)))) == 2)
    with stack["factory"]() as session:
        design = session.get(DesignSession, design_id)
        version = session.scalar(select(DesignVersion))
        reviews = list(session.scalars(select(DesignReview)))
        receipts = list(session.scalars(select(PolicyBucket).where(PolicyBucket.key.like("test-image-receipt:%"))))
        assert version_view(session, version)["coverage"]["complete"]
        assert {review.provider for review in reviews} == {"anthropic", "google"}
        assert all(review.provider != version.generator_provider for review in reviews)
        assert design.state["submission_count"] == 1 and design.state["request_count"] == 3
        assert len(receipts) == 3
        assert {row.metadata_json["provider"] for row in receipts} == {"openai", "anthropic", "google"}
        assert all(row.metadata_json["authorization_seen"] and row.metadata_json["observations"] == row.metadata_json["fake_provider_sends"] == 1 for row in receipts)
        image_hash = hashlib.sha256(pixels()).hexdigest()
        assert all(row.metadata_json["image_sha256"] == [image_hash] and not row.metadata_json["peer_findings_present"] for row in receipts if row.metadata_json["provider"] != "openai")
        operations = list(session.scalars(select(BudgetOperation)))
        assert str(operations_exposure(operations)) == "0.60"
        assert all(operation.status == "unknown" for operation in operations if operation.kind != "visual_finish_hold")


def test_killed_image_process_keeps_unknown_authorization_and_does_not_resend(image_process_stack):
    stack = image_process_stack
    design_id, operation_id = seed_image(stack["factory"], stack["configuration"])
    worker = stack["worker"]("block_after_authorization")
    stack["dispatcher"]()
    key = "test-image-receipt:" + operation_id
    wait_for(stack, lambda session: session.scalar(select(PolicyBucket).where(PolicyBucket.key == key)))
    with stack["factory"]() as session:
        assert session.get(DesignOperation, operation_id).payload["status"] == "authorized"
        assert session.get(BudgetOperation, operation_id).status == "reserved"
        assert session.scalar(select(PolicyBucket).where(PolicyBucket.key == key)).metadata_json["fake_provider_sends"] == 0
    os.killpg(worker.pid, signal.SIGKILL)
    worker.wait(timeout=3)
    with stack["factory"].begin() as session:
        operation = session.get(DesignOperation, operation_id)
        operation.payload = {**operation.payload, "lease_expires_at": (utcnow() - timedelta(seconds=1)).isoformat()}
    wait_for(stack, lambda session: session.get(DesignOperation, operation_id).payload.get("status") == "unknown")
    stack["worker"]()
    stack["sender"].send_task("round_robin.visual.execute", args=[operation_id], queue=stack["queue"], retry=False)
    # A marker task delivered after the duplicate proves the restarted worker
    # consumed its queue; the retired unknown authorization must stay untouched.
    marker = uuid4().hex
    stack["sender"].send_task("round_robin.tests.image.marker", args=[marker], queue=stack["queue"], retry=False)
    wait_for(stack, lambda session: session.scalar(select(PolicyBucket).where(PolicyBucket.key == "test-image-drained:" + marker)))
    with stack["factory"]() as session:
        operation = session.get(DesignOperation, operation_id)
        design = session.get(DesignSession, design_id)
        receipt = session.scalar(select(PolicyBucket).where(PolicyBucket.key == key))
        assert operation.payload["status"] == "unknown"
        assert session.get(BudgetOperation, operation_id).status == "unknown"
        assert receipt.metadata_json["observations"] == 1 and receipt.metadata_json["fake_provider_sends"] == 0
        assert session.scalar(select(DesignVersion)) is None
        assert design.state["submission_count"] == design.state["request_count"] == 1
