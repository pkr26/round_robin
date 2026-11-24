"""Actual loopback TLS drip regression for the image adapter total deadline."""
import os
import time

import pytest

from backend.tests.test_fetcher_transport import tls_files, tls_server
from backend.tests.integration.test_provider_transport import fixture_transport
from backend.tests.test_image_provider import image_configuration
from round_robin.providers.base import ProviderFailure
from round_robin.providers.images import HTTPImageProvider
from round_robin.providers.transport import DeadlineHTTPTransport

pytestmark = pytest.mark.skipif(os.environ.get("ROUND_ROBIN_TEST_ALLOW_LOOPBACK") != "1", reason="Actual image TLS checks require the explicit loopback-only profile")


@pytest.mark.parametrize("mode", ["slow_headers", "slow_body"])
def test_image_total_deadline_includes_headers_and_body(monkeypatch, tls_files, image_configuration, mode):
    with tls_server(tls_files, mode=mode) as (port, observed, _):
        destinations, context = fixture_transport(monkeypatch, port, tls_files[0])
        monkeypatch.setattr("round_robin.providers.images.DeadlineHTTPTransport", lambda deadline: DeadlineHTTPTransport(deadline, ssl_context=context))
        adapter = HTTPImageProvider("openai", image_configuration, api_key="synthetic")
        started = time.monotonic()
        with pytest.raises(ProviderFailure) as failure:
            adapter._http("https://example.com/facts", {}, {}, timeout=.25, maximum_bytes=32768)
        elapsed = time.monotonic() - started
    assert failure.value.may_have_sent
    assert .15 <= elapsed < .9
    assert destinations == [("127.0.0.1", 443)] and observed["connections"] == 1
