"""Default dispatcher routing reaches the ordinary worker without a broker."""

import os
import signal
import subprocess
import sys

import pytest


def test_default_dispatch_routes_to_the_default_worker_queue(tmp_path):
    code = '''import socket

def forbidden(*args, **kwargs):
    raise AssertionError("network calls forbidden")
socket.socket.connect = forbidden
socket.socket.connect_ex = forbidden
socket.create_connection = forbidden
socket.getaddrinfo = forbidden

from round_robin.dispatcher import service
seen = []
def capture(name, *, args, queue, retry):
    routed = service.celery_app.amqp.router.route({"queue": queue}, name, args=args)
    seen.append((name, args, routed["queue"].name, retry))
service.celery_app.send_task = capture
service._send("execute", {"step_id": "step_synthetic", "dispatch_generation": 0})
assert service.celery_app.conf.task_default_queue == "round_robin"
assert seen == [("round_robin.execute", [{"step_id": "step_synthetic", "dispatch_generation": 0}], "round_robin", False)]
'''
    env = os.environ.copy()
    env.pop("ROUND_ROBIN_TASK_QUEUE", None)
    env.pop("MUTANT_UNDER_TEST", None)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["ROUND_ROBIN_DATABASE_URL"] = f"sqlite:///{tmp_path / 'unused.sqlite'}"
    env["ROUND_ROBIN_BROKER_URL"] = "redis://127.0.0.1:1/0"
    env["PYTHONPATH"] = os.pathsep.join(path for path in sys.path if path)
    process = subprocess.Popen([sys.executable, "-c", code], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
    try:
        stdout, stderr = process.communicate(timeout=10)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.communicate()
        pytest.fail("Default queue routing check did not finish")
    assert process.returncode == 0, (stdout.decode(), stderr.decode())
