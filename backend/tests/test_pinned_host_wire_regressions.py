from http.client import HTTPConnection
from types import SimpleNamespace

import pytest
from round_robin.research import fetcher
from round_robin.research.fetcher import SocketPublicTransport, Target

from backend.tests.test_public_transport_mutation_regressions import fake_transport as fake_transport  # noqa: PLC0414


@pytest.mark.parametrize("hostname,expected", [("public.example", "public.example"), ("2606:4700:4700::1111", "[2606:4700:4700::1111]")])
def test_actual_http_header_serialization_has_exactly_one_pinned_host(fake_transport, monkeypatch, hostname, expected):
    wire = []

    class WireConnection(HTTPConnection):
        def send(self, data):
            wire.append(data)

        def getresponse(self):
            return SimpleNamespace(status=200, getheaders=lambda: [], read=lambda size: b"data")

    monkeypatch.setattr(fetcher.http.client, "HTTPConnection", WireConnection)
    target = Target(url="https://public.example/facts", hostname=hostname, address="203.0.113.10", request_target="/facts")
    SocketPublicTransport().get(target, timeout=5, byte_limit=4, private_headers={"host": "attacker.invalid", "X-Synthetic": "scoped"})
    serialized = b"".join(wire).decode("ascii").split("\r\n")
    hosts = [line.split(":", 1)[1].strip() for line in serialized if line.partition(":")[0].lower() == "host"]
    assert hosts == [expected]
    assert "X-Synthetic: scoped" in serialized
    assert fake_transport.wraps[0][1] == hostname
