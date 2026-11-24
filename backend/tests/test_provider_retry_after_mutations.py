"""Provider rate-limit waits and HTTP failures use bounded, explicit policy."""

import httpx
import pytest
from round_robin.providers.base import ProviderFailure
from round_robin.providers.http import HTTPProvider


@pytest.mark.parametrize(
    ("header", "expected"),
    [(None, 3), ("not-a-number", 3), ("-0.01", 3), ("0", 0), ("0.5", 0.5),
     ("7", 7), ("600", 600), ("600.5", 3), ("601", 3), ("inf", 3), ("nan", 3)],
)
def test_text_provider_rate_limit_retry_after_is_bounded(header, expected):
    response = httpx.Response(429, headers={} if header is None else {"retry-after": header})
    adapter = object.__new__(HTTPProvider)
    with pytest.raises(ProviderFailure) as error:
        adapter._http_failure(response)
    assert error.value.code == "rate_limit" and error.value.retryable is True
    assert error.value.retry_after == expected


@pytest.mark.parametrize(
    ("status", "code", "retryable"),
    [(400, "provider_rejected_request", False), (401, "authentication", False),
     (402, "credit_exhausted", False), (403, "authentication", False),
     (404, "model_unavailable", False), (500, "transient", True), (503, "transient", True)],
)
def test_text_provider_http_failure_categories_do_not_disclose_response(status, code, retryable):
    response = httpx.Response(status, content=b"synthetic-private-response")
    adapter = object.__new__(HTTPProvider)
    with pytest.raises(ProviderFailure) as error:
        adapter._http_failure(response)
    assert (error.value.code, error.value.retryable) == (code, retryable)
    assert "synthetic-private-response" not in str(error.value)
