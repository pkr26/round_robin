"""Reproduce an image HTTP total-deadline overrun using only loopback TCP."""
import json
from pathlib import Path
import socket
import sys
import threading
import time

ROOT = Path('/Users/pradeepreddy/Desktop/round_robin')
sys.path.insert(0, str(ROOT / 'backend/src'))
from round_robin.providers.images import HTTPImageProvider
from round_robin.providers.base import ProviderFailure

listener = socket.socket()
listener.bind(('127.0.0.1', 0))
listener.listen()
port = listener.getsockname()[1]

def serve():
    connection, _ = listener.accept()
    try:
        with connection:
            connection.recv(16384)
            connection.sendall(b'HTTP/1.1 200 OK\r\n')
            for index in range(12):
                time.sleep(.025)
                connection.sendall(f'X-Delay-{index}: x\r\n'.encode())
            connection.sendall(b'Content-Length: 2\r\n\r\n{}')
    except (BrokenPipeError, ConnectionResetError):
        pass
    finally:
        listener.close()

thread = threading.Thread(target=serve, daemon=True)
thread.start()
provider = object.__new__(HTTPImageProvider)
provider.transport = None
start = time.monotonic()
error = None
try:
    provider._http(f'http://127.0.0.1:{port}', {}, {}, timeout=.1, maximum_bytes=100)
except ProviderFailure as failure:
    error = failure.code
elapsed = time.monotonic() - start
thread.join(timeout=2)
result = {'probe': 'image_total_deadline', 'declared_timeout_seconds': .1,
          'elapsed_seconds': round(elapsed, 4), 'error': error,
          'confirmed_overrun': elapsed > .2, 'network_scope': 'loopback only'}
print(json.dumps(result, indent=2))
assert result['confirmed_overrun'], result
