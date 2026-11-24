"""A slow public resolver must remain bounded without blocking process exit."""

import socket
import threading

from round_robin.research import fetcher


def test_dns_worker_can_finish_within_deadline_and_is_daemon(monkeypatch):
    started = threading.Event()
    release = threading.Event()
    daemon_states = []

    def resolve(*args, **kwargs):
        daemon_states.append(threading.current_thread().daemon)
        started.set()
        assert release.wait(1)
        return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("93.184.216.34", 443))]

    def release_after_resolver_starts():
        if started.wait(1):
            threading.Event().wait(0.03)
            release.set()

    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    releaser = threading.Thread(target=release_after_resolver_starts, daemon=True)
    releaser.start()
    try:
        assert fetcher.public_addresses("evidence.invalid", timeout=1) == ["93.184.216.34"]
        assert daemon_states == [True]
    finally:
        release.set()
        releaser.join(timeout=1)


def test_dns_accepts_the_maximum_supported_deadline(monkeypatch):
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("93.184.216.34", 443))],
    )
    assert fetcher.public_addresses("evidence.invalid", timeout=30) == ["93.184.216.34"]
