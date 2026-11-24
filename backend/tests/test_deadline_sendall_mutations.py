from types import SimpleNamespace

import pytest
from round_robin.research import fetcher


def test_sendall_at_exact_deadline_never_sends_or_extends_timeout(monkeypatch):
    calls = []
    raw = SimpleNamespace(
        settimeout=lambda value: calls.append(("timeout", value)),
        sendall=lambda value: calls.append(("send", value)),
    )
    monkeypatch.setattr(fetcher.time, "monotonic", lambda: 100.0)
    with pytest.raises(TimeoutError):
        fetcher.DeadlineSocket(raw, 100.0).sendall(b"synthetic request")
    assert calls == []


def test_sendall_passes_exact_request_bytes_and_remaining_deadline(monkeypatch):
    calls = []
    raw = SimpleNamespace(
        settimeout=lambda value: calls.append(("timeout", value)),
        sendall=lambda value: calls.append(("send", value)),
    )
    monkeypatch.setattr(fetcher.time, "monotonic", lambda: 100.0)
    fetcher.DeadlineSocket(raw, 100.25).sendall(b"synthetic request")
    assert calls == [("timeout", 0.25), ("send", b"synthetic request")]
