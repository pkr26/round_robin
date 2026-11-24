import pytest
from round_robin.research import fetcher


class FakeSocket:
    def __init__(self):
        self.timeouts = []
        self.reads = 0
        self.closed = 0

    def settimeout(self, value):
        self.timeouts.append(value)

    def recv_into(self, buffer):
        self.reads += 1
        buffer[0] = ord("x")
        return 1

    def close(self):
        self.closed += 1


@pytest.mark.parametrize("now", [10.0, 10.001])
def test_response_stream_never_reads_at_or_after_total_deadline(monkeypatch, now):
    monkeypatch.setattr(fetcher.time, "monotonic", lambda: now)
    sock = FakeSocket()
    stream = fetcher.DeadlineSocket(sock, 10.0)
    reader = stream.makefile("rb")
    with pytest.raises(TimeoutError):
        reader.read(1)
    assert sock.timeouts == [] and sock.reads == 0
    stream.close()
    assert sock.closed == 0
    reader.close()
    assert sock.closed == 1


def test_response_stream_reads_before_deadline_with_remaining_budget(monkeypatch):
    monkeypatch.setattr(fetcher.time, "monotonic", lambda: 9.75)
    sock = FakeSocket()
    stream = fetcher.DeadlineSocket(sock, 10.0)
    reader = stream.makefile("rb")
    assert reader.read(1) == b"x"
    assert sock.timeouts == [0.25] and sock.reads == 1
    reader.close()
    stream.close()
    assert sock.closed == 1
