"""Socket ownership outlives its buffered response readers when needed."""

from round_robin.research.fetcher import DeadlineSocket


class RecordingSocket:
    def __init__(self):
        self.close_count = 0

    def close(self):
        self.close_count += 1


def test_closing_last_reader_does_not_close_an_open_transport():
    raw = RecordingSocket()
    transport = DeadlineSocket(raw, deadline=100)
    first = transport.makefile("rb")
    second = transport.makefile("rb")

    first.close()
    second.close()
    assert raw.close_count == 0
    transport.close()
    assert raw.close_count == 1


def test_closing_transport_defers_socket_close_until_last_reader():
    raw = RecordingSocket()
    transport = DeadlineSocket(raw, deadline=100)
    first = transport.makefile("rb")
    second = transport.makefile("rb")

    transport.close()
    first.close()
    assert raw.close_count == 0
    second.close()
    assert raw.close_count == 1


def test_reader_uses_transport_socket_and_remaining_deadline(monkeypatch):
    from round_robin.research import fetcher

    class ReadSocket(RecordingSocket):
        def settimeout(self, timeout):
            self.timeout = timeout

        def recv_into(self, buffer):
            buffer[:2] = b"ok"
            return 2

    monkeypatch.setattr(fetcher.time, "monotonic", lambda: 50.0)
    raw = ReadSocket()
    transport = DeadlineSocket(raw, deadline=60.0)
    reader = transport.makefile("rb")

    assert reader.read(2) == b"ok"
    assert raw.timeout == 10.0

    reader.close()
    transport.close()
    assert raw.close_count == 1
