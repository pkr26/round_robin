import pytest
from round_robin.research.fetcher import SocketPublicTransport, Target

from backend.tests.test_public_transport_mutation_regressions import (
    fake_transport as fake_transport,  # noqa: PLC0414
)


@pytest.mark.parametrize(
    "private_headers",
    [
        {"X-Synthetic": "scoped"},
        {"Host": "untrusted.example", "X-Synthetic": "scoped"},
        {"host": "untrusted.example", "X-Synthetic": "scoped"},
        {"HOST": "untrusted.example", "X-Synthetic": "scoped"},
        {
            "Host": "untrusted-a.example",
            "host": "untrusted-b.example",
            "HOST": "untrusted-c.example",
            "X-Synthetic": "scoped",
        },
    ],
)
def test_case_variant_private_host_cannot_override_pinned_host(
    fake_transport, private_headers
):
    target = Target(
        url="https://public.example/facts",
        hostname="public.example",
        address="203.0.113.10",
        request_target="/facts",
    )
    SocketPublicTransport().get(
        target, timeout=5.0, byte_limit=4, private_headers=private_headers
    )
    headers = fake_transport.requests[0][2]
    assert [value for key, value in headers.items() if key.lower() == "host"] == [
        "public.example"
    ]
    assert headers["X-Synthetic"] == "scoped"


def test_host_protection_keeps_direct_pinned_connection_despite_proxy_environment(
    fake_transport, monkeypatch
):
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
    target = Target(
        url="https://public.example/facts",
        hostname="public.example",
        address="203.0.113.10",
        request_target="/facts",
    )
    SocketPublicTransport().get(
        target, timeout=5.0, byte_limit=4, private_headers={"host": "untrusted.example"}
    )
    assert fake_transport.sockets[0][:2] == (("203.0.113.10", 443), 5.0)
    assert fake_transport.wraps[0][1] == "public.example"
