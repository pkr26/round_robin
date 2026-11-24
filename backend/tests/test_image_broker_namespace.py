"""Image workers must validate Redis database identity before startup."""
import os
import signal
import socket
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('Broker validation must not connect to a network')
    monkeypatch.setattr(socket.socket, 'connect', forbidden)
    monkeypatch.setattr(socket.socket, 'connect_ex', forbidden)
    monkeypatch.setattr(socket.socket, 'sendto', forbidden)
    monkeypatch.setattr(socket, 'create_connection', forbidden)
    monkeypatch.setattr(socket, 'getaddrinfo', forbidden)


@pytest.mark.parametrize('url', [
    'redis://broker.invalid/X1',
    'redis://broker.invalid/1X',
    'redis://broker.invalid/X1X',
    'redis://broker.invalid/1.5',
    'redis://broker.invalid/not_a_database',
])
def test_malformed_broker_database_is_rejected_without_network(url):
    from round_robin.worker.image_tasks import _broker_database
    with pytest.raises(ValueError):
        _broker_database(url)


@pytest.mark.parametrize(('url', 'expected'), [
    ('redis://broker.invalid', 0),
    ('redis://broker.invalid/0', 0),
    ('rediss://broker.invalid/2', 2),
    ('redis://broker.invalid/02', 2),
])
def test_supported_broker_database_identity_is_preserved(url, expected):
    from round_robin.worker.image_tasks import _broker_database
    assert _broker_database(url) == expected


@pytest.mark.parametrize(('core', 'image', 'allowed'), [
    ('redis://broker.invalid/0', 'redis://broker.invalid/0', False),
    ('redis://broker.invalid/1', 'rediss://broker.invalid/01', False),
    ('redis://broker.invalid/0', 'redis://broker.invalid/1', True),
    ('redis://broker.invalid/0', 'redis://broker.invalid/X1', False),
])
def test_image_worker_startup_rejects_shared_database_without_redis_or_postgres(tmp_path, core, image, allowed):
    code = '''import socket

def forbidden(*args, **kwargs):
    raise RuntimeError("network access forbidden during worker import")
socket.socket.connect = forbidden
socket.socket.connect_ex = forbidden
socket.socket.sendto = forbidden
socket.create_connection = forbidden
socket.getaddrinfo = forbidden
try:
    import round_robin.worker.image_tasks
except ValueError:
    raise SystemExit(23)
'''
    from round_robin.worker import image_tasks
    source_root = Path(image_tasks.__file__).resolve().parents[2]
    env={**os.environ, 'ROUND_ROBIN_DATABASE_URL':f'sqlite:///{tmp_path / "empty.sqlite"}',
         'ROUND_ROBIN_BROKER_URL':core, 'ROUND_ROBIN_IMAGE_BROKER_URL':image,
         'PYTHONPATH':str(source_root) + os.pathsep + os.environ.get('PYTHONPATH', '')}
    process=subprocess.Popen([sys.executable,'-c',code],env=env,stdout=subprocess.PIPE,stderr=subprocess.PIPE,start_new_session=True)
    try:
        stdout,stderr=process.communicate(timeout=10)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid,signal.SIGKILL)
        process.communicate()
        pytest.fail('Worker import did not finish within ten seconds')
    assert process.returncode == (0 if allowed else 23), (stdout.decode(),stderr.decode())


def test_unsupported_broker_scheme_explains_the_required_isolation():
    from round_robin.worker.image_tasks import _broker_database
    with pytest.raises(ValueError) as failure:
        _broker_database('https://broker.invalid/2')
    assert 'redis database' in str(failure.value).lower()
