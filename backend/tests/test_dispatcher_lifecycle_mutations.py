"""Isolated controls for dispatcher lifecycle, cadence and maintenance routing."""
import signal
import socket
from types import SimpleNamespace

import pytest
from round_robin.dispatcher import service
from round_robin.operations import maintenance
from round_robin.worker import image_tasks


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("network calls forbidden")
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)


class BoundedEvent:
    def __init__(self):
        self.stopped = False
        self.waits = []

    def is_set(self):
        if len(self.waits) > 2:
            raise AssertionError("dispatcher did not stop after signal")
        return self.stopped

    def set(self):
        self.stopped = True

    def wait(self, seconds):
        self.waits.append(seconds)


def no_external_work(monkeypatch):
    monkeypatch.setattr(service, "dispatch_once", lambda: None)
    monkeypatch.setattr(image_tasks, "reconcile_image_operations", lambda: None)
    monkeypatch.setattr(image_tasks, "dispatch_pending_image_operations", lambda: None)
    monkeypatch.setattr(maintenance, "maintenance", lambda *args: None)
    monkeypatch.setattr(service, "time", SimpleNamespace(monotonic=lambda: 100.0))


@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGINT])
def test_supported_shutdown_signals_stop_the_dispatcher(monkeypatch, signum):
    stop = BoundedEvent()
    handlers = {}
    def register(which, handler):
        assert which in {signal.SIGTERM, signal.SIGINT}
        assert callable(handler)
        handlers[which] = handler
    monkeypatch.setattr(service, "threading", SimpleNamespace(Event=lambda: stop))
    monkeypatch.setattr(service, "signal", SimpleNamespace(signal=register, SIGTERM=signal.SIGTERM, SIGINT=signal.SIGINT))
    no_external_work(monkeypatch)
    def reconcile():
        handlers[signum](signum, None)
    monkeypatch.setattr(service, "reconcile_once", reconcile)
    service.main()
    assert stop.stopped and len(stop.waits) == 1


def test_loop_honors_configured_poll_interval_and_maintenance_scope(monkeypatch, tmp_path):
    stop = BoundedEvent()
    monkeypatch.setattr(service, "threading", SimpleNamespace(Event=lambda: stop))
    monkeypatch.setattr(service, "signal", SimpleNamespace(signal=lambda *_: None, SIGTERM=signal.SIGTERM, SIGINT=signal.SIGINT))
    monkeypatch.setenv("ROUND_ROBIN_DISPATCH_INTERVAL_SECONDS", "2.5")
    no_external_work(monkeypatch)
    monkeypatch.setattr(service, "reconcile_once", lambda: stop.set())
    from round_robin import configuration
    expected_directory = tmp_path / "owned-artifacts"
    monkeypatch.setattr(configuration, "Settings", lambda: SimpleNamespace(artifact_directory=expected_directory))
    expected_factory = object()
    monkeypatch.setattr(service, "SessionLocal", expected_factory)
    calls = []
    monkeypatch.setattr(maintenance, "maintenance", lambda factory, directory: calls.append((factory, directory)))
    service.main()
    assert stop.waits == [2.5]
    assert calls == [(expected_factory, expected_directory)]
