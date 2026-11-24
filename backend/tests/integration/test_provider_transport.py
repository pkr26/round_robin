"""Actual loopback TLS evidence for model-provider absolute deadlines."""

import os
import ssl
import time

import httpx
import pytest

from backend.tests.test_fetcher_transport import pin_fixture_connection, tls_files, tls_server
from backend.tests.test_providers import configuration, packet
from round_robin.providers.base import ProviderFailure
from round_robin.providers.http import HTTPProvider
from round_robin.providers.transport import DeadlineHTTPTransport


pytestmark = pytest.mark.skipif(os.environ.get("ROUND_ROBIN_TEST_ALLOW_LOOPBACK") != "1", reason="Actual provider TLS checks require the explicit loopback-only profile")


def fixture_transport(monkeypatch, port, certificate_path):
    destinations = pin_fixture_connection(monkeypatch, port, certificate_path)
    def addresses(host, requested_port, deadline):
        assert host == "example.com" and requested_port == 443
        return ["127.0.0.1"]
    monkeypatch.setattr("round_robin.providers.transport._addresses", addresses)
    context = ssl.create_default_context()
    return destinations, context


def test_provider_transport_actual_tls_preserves_identity_and_complete_body(monkeypatch, tls_files):
    with tls_server(tls_files) as (port, observed, expected):
        destinations, context = fixture_transport(monkeypatch, port, tls_files[0])
        with httpx.Client(transport=DeadlineHTTPTransport(time.monotonic() + 2, ssl_context=context), trust_env=False) as client:
            response = client.get("https://example.com/facts", headers={"Accept-Encoding": "identity"})
    assert response.status_code == 200 and response.content == expected
    assert observed["sni"] == ["example.com"] and observed["host"] == ["example.com"]
    assert destinations == [("127.0.0.1", 443)]


@pytest.mark.parametrize("mode", ["slow_headers", "slow_body"])
def test_model_call_total_deadline_survives_actual_tls_drip(monkeypatch, tls_files, mode):
    from dataclasses import replace
    with tls_server(tls_files, mode=mode) as (port, observed, _):
        destinations, context = fixture_transport(monkeypatch, port, tls_files[0])
        monkeypatch.setattr("round_robin.providers.http.DeadlineHTTPTransport", lambda deadline: DeadlineHTTPTransport(deadline, ssl_context=context))
        adapter = HTTPProvider("openai", configuration("openai"), api_key="synthetic")
        original_payload = adapter._payload
        def local_payload(request):
            _, headers, payload = original_payload(request)
            return "https://example.com/facts", headers, payload
        monkeypatch.setattr(adapter, "_payload", local_payload)
        began = time.monotonic()
        with pytest.raises(ProviderFailure) as error:
            adapter.generate(replace(packet(), timeout_seconds=.25))
        elapsed = time.monotonic() - began
    assert error.value.code == "timeout" and error.value.may_have_sent
    assert .15 <= elapsed < .9, f"A 250 ms total deadline took {elapsed:.3f} seconds"
    assert destinations == [("127.0.0.1", 443)] and observed["connections"] == 1
