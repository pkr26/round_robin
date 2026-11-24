"""Explicit Docker fault profile; only the test's UUID-named Redis is stopped."""

import os
import socket
import subprocess
import time
from uuid import uuid4

import pytest
import redis
from sqlalchemy import select

from backend.tests.integration.test_worker_processes import process_stack, seed, wait_for
from round_robin.database.models import Attempt, Outbox, ReportVersion


@pytest.fixture
def isolated_broker(monkeypatch):
    if os.environ.get('ROUND_ROBIN_TEST_DOCKER_FAULTS') != '1' or os.environ.get('ROUND_ROBIN_TEST_ALLOW_LOOPBACK') != '1':
        pytest.skip('Broker restart faults require the explicit disposable Docker profile')
    name = 'rr-broker-fault-' + uuid4().hex
    with socket.socket() as allocation:
        allocation.bind(('127.0.0.1', 0))
        port = allocation.getsockname()[1]
    # A random Docker publication is reassigned on container restart. Bind the
    # selected host port explicitly so recovery tests the same broker endpoint.
    subprocess.run(['docker','run','--detach','--name',name,'--label','round_robin.test=broker-recovery','--publish',f'127.0.0.1:{port}:6379','redis:8-alpine','redis-server','--save','','--appendonly','no'], check=True, capture_output=True)
    try:
        mapping = subprocess.check_output(['docker','port',name,'6379/tcp'], text=True).strip()
        port = int(mapping.rsplit(':',1)[1])
        url = f'redis://127.0.0.1:{port}/14'
        client = redis.Redis.from_url(url, socket_connect_timeout=.3, socket_timeout=.3)
        for _ in range(50):
            try:
                if client.ping(): break
            except redis.RedisError: time.sleep(.1)
        else: pytest.fail('The owned disposable Redis did not become ready')
        client.close()
        monkeypatch.setenv('ROUND_ROBIN_TEST_BROKER_URL',url)
        yield name, url
    finally:
        subprocess.run(['docker','rm','--force',name], check=False, capture_output=True)


@pytest.fixture
def restarting_stack(isolated_broker, request):
    return isolated_broker, request.getfixturevalue('process_stack')


def test_real_broker_outage_and_lost_publish_ack_preserve_work_without_duplicate_calls(restarting_stack):
    (container, broker), stack = restarting_stack
    from celery import Celery
    from round_robin.dispatcher.service import dispatch_once, reconcile_once
    from round_robin.orchestration.admission import lock_work_run
    from round_robin.orchestration.controller import advance_run
    factory = stack['factory']
    run_id = seed(factory)
    with factory.begin() as session:
        advance_run(session, lock_work_run(session, run_id))
    # No app-owned Redis instance or unrelated queue is touched.
    subprocess.run(['docker','stop','--time','1',container], check=True, capture_output=True)
    client = Celery('broker-recovery-fixture', broker=broker)
    client.conf.update(task_publish_retry=False, broker_connection_timeout=.3, broker_transport_options={'socket_connect_timeout':.3,'socket_timeout':.3})
    def unavailable(kind, payload):
        client.send_task(f'round_robin.{kind}',args=[payload],queue='unused_during_owned_outage',retry=False)
    assert dispatch_once(session_factory=factory, sender=unavailable) == 0
    with factory() as session:
        assert not list(session.scalars(select(Attempt)))
        assert list(session.scalars(select(Outbox).where(Outbox.status=='pending')))
    subprocess.run(['docker','start',container], check=True, capture_output=True)
    probe = redis.Redis.from_url(broker, socket_connect_timeout=.3, socket_timeout=.3)
    try:
        for _ in range(50):
            try:
                if probe.ping():
                    break
            except redis.RedisError:
                time.sleep(.1)
        else:
            pytest.fail('The owned Redis did not return after restart')
    finally:
        probe.close()
    # Publish succeeded but its acknowledgement was lost. The original outbox
    # remains pending, so the real dispatcher redelivers the same step identity.
    from datetime import timedelta
    from round_robin.database.models import utcnow
    with factory.begin() as session:
        for work in session.scalars(select(Outbox)):
            work.due_at = utcnow()-timedelta(seconds=1)
    queue = stack['queue']
    def lost_ack(kind, payload):
        client.send_task(f'round_robin.{kind}',args=[payload],queue=queue,retry=False)
        raise ConnectionError('Synthetic lost acknowledgement after publication')
    dispatch_once(session_factory=factory, sender=lost_ack)
    stack['worker'](); stack['dispatcher']()
    assert wait_for(factory,run_id,{'completed','failed','completed_with_limitations'},stack['logs'],timeout=60)=='completed'
    with factory() as session:
        attempts = list(session.scalars(select(Attempt).where(Attempt.run_id==run_id)))
        assert len(attempts)==6 and all(attempt.status=='succeeded' for attempt in attempts)
        assert len(list(session.scalars(select(ReportVersion))))==1
    client.close()
