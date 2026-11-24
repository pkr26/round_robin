"""No-network tests of failures during bounded provider response cleanup."""
import json
from decimal import Decimal

import httpcore
import pytest
from round_robin.providers.base import ProviderFailure
from round_robin.providers.http import HTTPProvider
from round_robin.providers.images import HTTPImageProvider
from round_robin.providers.transport import DeadlineHTTPTransport

from backend.tests.test_image_provider import synthetic_image_configuration
from backend.tests.test_orchestration import factory as factory  # noqa: PLC0414
from backend.tests.test_providers import configuration, packet, response_for


@pytest.mark.parametrize("provider", ["text", "image"])
@pytest.mark.parametrize("when", ["read", "stream_close", "pool_close"])
@pytest.mark.parametrize("error_type", [httpcore.ReadError, OSError])
def test_stream_failures_become_safe_uncertain_provider_failure(provider, when, error_type, caplog):
    sends = []
    class Body:
        def __iter__(self):
            if when == "read":
                raise error_type("SECRET_UPSTREAM_TEXT")
            yield json.dumps(response_for("openai")).encode()
        def close(self):
            if when == "stream_close":
                raise error_type("SECRET_UPSTREAM_TEXT")
    class Pool:
        def handle_request(self, request):
            sends.append(request)
            return httpcore.Response(200, headers=[(b"x-request-id", b"req_synthetic")], content=Body())
        def close(self):
            if when == "pool_close":
                raise error_type("SECRET_UPSTREAM_TEXT")
    transport = DeadlineHTTPTransport(10000000000)
    transport.pool.close()
    transport.pool = Pool()
    if provider == "text":
        adapter = HTTPProvider("openai", configuration("openai"), api_key="synthetic", transport=transport)
        invoke = lambda: adapter.generate(packet())
        expected = "transport_failure"
    else:
        adapter = HTTPImageProvider("openai", synthetic_image_configuration(), api_key="synthetic", transport=transport)
        invoke = lambda: adapter._http("https://provider.invalid", {}, {}, timeout=3, maximum_bytes=1024 * 1024)
        expected = "image_transport_or_response_unknown"
    if provider == "text" and when == "pool_close":
        result = invoke()
        assert result.provider_request_id == "req_synthetic" and result.cost_usd == Decimal("0.0007")
        assert any(record.message == "Provider response completed; connection cleanup was unavailable." for record in caplog.records)
        assert all("SECRET_UPSTREAM_TEXT" not in record.getMessage() for record in caplog.records)
    else:
        with pytest.raises(ProviderFailure) as failure:
            invoke()
        assert failure.value.code == expected and failure.value.provider_request_id == "req_synthetic"
        assert failure.value.may_have_sent and failure.value.cost_usd is None
        assert "SECRET_UPSTREAM_TEXT" not in str(failure.value)
    assert len(sends) == 1


@pytest.mark.parametrize("provider", ["text", "image"])
@pytest.mark.parametrize("error_type", [httpcore.ReadTimeout, TimeoutError])
@pytest.mark.parametrize("when", ["read", "stream_close", "pool_close"])
def test_timeout_keeps_sent_exposure_uncertain(provider, error_type, when):
    sends = []
    class Body:
        def __iter__(self):
            if when == "read":
                raise error_type("SECRET_TIMEOUT")
            yield json.dumps(response_for("openai")).encode()
        def close(self):
            if when == "stream_close":
                raise error_type("SECRET_TIMEOUT")
    class Pool:
        def handle_request(self, request):
            sends.append(request)
            return httpcore.Response(200, headers=[(b"x-request-id", b"req_synthetic")], content=Body())
        def close(self):
            if when == "pool_close":
                raise error_type("SECRET_TIMEOUT")
    transport = DeadlineHTTPTransport(10000000000)
    transport.pool.close()
    transport.pool = Pool()
    if provider == "text":
        adapter = HTTPProvider("openai", configuration("openai"), api_key="synthetic", transport=transport)
        invoke = lambda: adapter.generate(packet())
        expected = "timeout"
    else:
        adapter = HTTPImageProvider("openai", synthetic_image_configuration(), api_key="synthetic", transport=transport)
        invoke = lambda: adapter._http("https://provider.invalid", {}, {}, timeout=3, maximum_bytes=1024 * 1024)
        expected = "image_transport_or_response_unknown"
    if provider == "text" and when == "pool_close":
        result = invoke()
        assert result.provider_request_id == "req_synthetic" and result.cost_usd == Decimal("0.0007")
    else:
        with pytest.raises(ProviderFailure) as failure:
            invoke()
        assert failure.value.code == expected and failure.value.provider_request_id == "req_synthetic"
        assert failure.value.may_have_sent and failure.value.cost_usd is None
        assert "SECRET_TIMEOUT" not in str(failure.value)
    assert len(sends) == 1


@pytest.mark.parametrize("provider", ["text", "image"])
@pytest.mark.parametrize("when", ["stream_close", "pool_close"])
def test_primary_rate_limit_survives_secondary_cleanup_failure(provider, when):
    sends = []
    class Body:
        def __iter__(self):
            yield b'{}'
        def close(self):
            if when == "stream_close":
                raise OSError("SECRET_CLEANUP")
    class Pool:
        def handle_request(self, request):
            sends.append(request)
            return httpcore.Response(429, headers=[(b"x-request-id", b"req_synthetic"), (b"retry-after", b"17")], content=Body())
        def close(self):
            if when == "pool_close":
                raise OSError("SECRET_CLEANUP")
    transport = DeadlineHTTPTransport(10000000000)
    transport.pool.close()
    transport.pool = Pool()
    if provider == "text":
        adapter = HTTPProvider("openai", configuration("openai"), api_key="synthetic", transport=transport)
        invoke = lambda: adapter.generate(packet())
    else:
        adapter = HTTPImageProvider("openai", synthetic_image_configuration(), api_key="synthetic", transport=transport)
        invoke = lambda: adapter._http("https://provider.invalid", {}, {}, timeout=3, maximum_bytes=1024 * 1024)
    with pytest.raises(ProviderFailure) as failure:
        invoke()
    assert failure.value.code == "rate_limit" and failure.value.retryable
    assert failure.value.provider_request_id == "req_synthetic"
    if provider == "text":
        assert failure.value.retry_after == 17
    assert failure.value.may_have_sent and failure.value.cost_usd is None and len(sends) == 1
    assert "SECRET_CLEANUP" not in str(failure.value)


@pytest.mark.parametrize("provider", ["text", "image"])
def test_unrelated_handled_exception_does_not_hide_new_cleanup_failure(provider):
    class Body:
        def __iter__(self):
            yield json.dumps(response_for("openai")).encode()
        def close(self):
            raise OSError("SECRET_CLOSE")
    class Pool:
        def handle_request(self, request):
            return httpcore.Response(200, headers=[(b"x-request-id", b"req_synthetic")], content=Body())
        def close(self):
            pass
    transport = DeadlineHTTPTransport(10000000000)
    transport.pool.close()
    transport.pool = Pool()
    if provider == "text":
        adapter = HTTPProvider("openai", configuration("openai"), api_key="synthetic", transport=transport)
        invoke = lambda: adapter.generate(packet())
        expected = "transport_failure"
    else:
        adapter = HTTPImageProvider("openai", synthetic_image_configuration(), api_key="synthetic", transport=transport)
        invoke = lambda: adapter._http("https://provider.invalid", {}, {}, timeout=3, maximum_bytes=1024 * 1024)
        expected = "image_transport_or_response_unknown"
    try:
        raise ValueError("unrelated exception already handled")
    except ValueError:
        with pytest.raises(ProviderFailure) as failure:
            invoke()
    assert failure.value.code == expected and failure.value.provider_request_id == "req_synthetic"
    assert failure.value.may_have_sent and failure.value.cost_usd is None


def test_image_refusal_survives_pool_cleanup_failure():
    class Body:
        def __iter__(self):
            yield b'{"error":{"code":"content_policy_violation"}}'
        def close(self):
            pass
    class Pool:
        def handle_request(self, request):
            return httpcore.Response(400, headers=[(b"x-request-id", b"req_refusal")], content=Body())
        def close(self):
            raise OSError("SECRET_CLEANUP")
    transport = DeadlineHTTPTransport(10000000000)
    transport.pool.close()
    transport.pool = Pool()
    adapter = HTTPImageProvider("openai", synthetic_image_configuration(), api_key="synthetic", transport=transport)
    with pytest.raises(ProviderFailure) as failure:
        adapter._http("https://provider.invalid", {}, {}, timeout=3, maximum_bytes=1024 * 1024)
    assert failure.value.code == "refusal" and not failure.value.retryable
    assert failure.value.provider_request_id == "req_refusal" and failure.value.may_have_sent
    assert failure.value.cost_usd is None and "SECRET_CLEANUP" not in str(failure.value)


@pytest.mark.parametrize("variant,expected_code,expected_cost", [
    ("refusal", "refusal", Decimal("0.0007")),
    ("schema_invalid", "schema_invalid", Decimal("0.0007")),
    ("invalid_reference", "invalid_reference", Decimal("0.0007")),
    ("missing_usage", "transport_failure", None),
    ("malformed_usage", "transport_failure", None),
    ("malformed_json", "malformed_response", None),
])
def test_completed_text_response_preserves_known_usage_and_primary_errors_on_pool_close(variant, expected_code, expected_cost):
    raw = response_for("openai")
    if variant == "refusal":
        raw["output"] = [{"type": "message", "content": [{"type": "refusal", "refusal": "Synthetic refusal"}]}]
    elif variant == "schema_invalid":
        raw["output"][0]["content"][0]["text"] = "{}"
    elif variant == "invalid_reference":
        content = json.loads(raw["output"][0]["content"][0]["text"])
        content["reference_ids"] = ["foreign_evidence"]
        raw["output"][0]["content"][0]["text"] = json.dumps(content)
    elif variant == "missing_usage":
        raw.pop("usage")
    elif variant == "malformed_usage":
        raw["usage"] = {"input_tokens": True, "output_tokens": 3}
    body = b"{" if variant == "malformed_json" else json.dumps(raw).encode()
    class Body:
        def __iter__(self):
            yield body
        def close(self):
            pass
    class Pool:
        def handle_request(self, request):
            return httpcore.Response(200, headers=[(b"x-request-id", b"req_reported")], content=Body())
        def close(self):
            raise OSError("SECRET_CLEANUP")
    transport = DeadlineHTTPTransport(10000000000)
    transport.pool.close()
    transport.pool = Pool()
    adapter = HTTPProvider("openai", configuration("openai"), api_key="synthetic", transport=transport)
    if variant in {"missing_usage", "malformed_usage"}:
        result = adapter.generate(packet())
        assert result.cost_usd is None and result.provider_request_id == "req_reported"
    else:
        with pytest.raises(ProviderFailure) as failure:
            adapter.generate(packet())
        assert failure.value.code == expected_code and failure.value.cost_usd == expected_cost
        assert failure.value.provider_request_id == "req_reported" and failure.value.may_have_sent
        assert "SECRET_CLEANUP" not in str(failure.value)


def test_incomplete_stream_does_not_settle_even_if_prefix_contains_complete_looking_usage():
    body = json.dumps(response_for("openai")).encode()
    class Body:
        def __iter__(self):
            yield body
            raise httpcore.ReadError("SECRET_STREAM")
        def close(self):
            pass
    class Pool:
        def handle_request(self, request):
            return httpcore.Response(200, headers=[(b"x-request-id", b"req_partial")], content=Body())
        def close(self):
            pass
    transport = DeadlineHTTPTransport(10000000000)
    transport.pool.close()
    transport.pool = Pool()
    adapter = HTTPProvider("openai", configuration("openai"), api_key="synthetic", transport=transport)
    with pytest.raises(ProviderFailure) as failure:
        adapter.generate(packet())
    assert failure.value.code == "transport_failure" and failure.value.cost_usd is None
    assert failure.value.provider_request_id == "req_partial" and failure.value.may_have_sent


def test_validated_text_result_commits_once_despite_pool_close_error(factory, caplog, monkeypatch):
    from round_robin.api.preflight import run_configuration
    from round_robin.configuration import Settings
    from round_robin.database.models import Attempt, BudgetOperation, Step
    from round_robin.database.services import lock_policies
    from round_robin.orchestration.admission import lock_work_run
    from round_robin.orchestration.controller import advance_run
    from round_robin.providers.simulated import SimulatedProvider
    from round_robin.worker import tasks
    from sqlalchemy import select

    from backend.tests.test_orchestration import seed

    monkeypatch.setattr("round_robin.orchestration.configuration.verify_frozen_configuration", lambda _: None)
    config = run_configuration(Settings(), "simulated")
    config.update(mode="live", funded=True)
    run_id = seed(factory, config=config)
    with factory.begin() as session:
        for policy in lock_policies(session, "owner_test"):
            policy.spending_locked = False
            policy.limit_usd = "10"
            policy.metadata_json = {"daily_usd": "10", "monthly_usd": "50"}
        advance_run(session, lock_work_run(session, run_id))
        step_id = session.scalar(select(Step.id).where(Step.run_id == run_id))
    sends = []
    class Body:
        def __init__(self, content):
            self.content = content
        def __iter__(self):
            yield self.content
        def close(self):
            pass
    class Pool:
        content = None
        def handle_request(self, request):
            sends.append(request)
            return httpcore.Response(200, headers=[(b"x-request-id", b"req_worker")], content=Body(self.content))
        def close(self):
            raise OSError("SECRET_CLEANUP")
    transport = DeadlineHTTPTransport(10000000000)
    transport.pool.close()
    transport.pool = pool = Pool()
    class Provider(HTTPProvider):
        def generate(self, request):
            simulated = SimulatedProvider("openai").generate(request)
            body = response_for("openai", simulated.output.model_dump(mode="json"))
            pool.content = json.dumps(body).encode()
            return super().generate(request)
    provider = Provider("openai", configuration("openai"), api_key="synthetic", transport=transport)
    result = tasks.execute_step(step_id, session_factory=factory, adapter_registry={"openai": provider})
    assert result["status"] == "persisted" and len(sends) == 1, result
    with factory() as session:
        step = session.get(Step, step_id)
        attempt = session.scalar(select(Attempt).where(Attempt.step_id == step_id))
        ledger = session.get(BudgetOperation, attempt.id)
        assert step.status == "succeeded" and step.error_code is None
        assert attempt.status == "succeeded" and attempt.usage_certainty == "reported"
        assert attempt.provider_request_id == "req_worker" and Decimal(attempt.settled_usd) == Decimal("0.0007")
        assert ledger.status == "settled"
    assert any(record.message == "Provider response completed; connection cleanup was unavailable." for record in caplog.records)
    assert all("SECRET_CLEANUP" not in record.getMessage() for record in caplog.records)


@pytest.mark.parametrize("provider", ["text", "image"])
@pytest.mark.parametrize("when", ["stream_close", "pool_close"])
def test_read_timeout_is_not_masked_by_later_cleanup_error(provider, when):
    class Body:
        def __iter__(self):
            raise httpcore.ReadTimeout("SECRET_READ")
            yield b""
        def close(self):
            if when == "stream_close":
                raise OSError("SECRET_CLOSE")
    class Pool:
        def handle_request(self, request):
            return httpcore.Response(200, headers=[(b"x-request-id", b"req_read")], content=Body())
        def close(self):
            if when == "pool_close":
                raise OSError("SECRET_CLOSE")
    transport = DeadlineHTTPTransport(10000000000)
    transport.pool.close()
    transport.pool = Pool()
    if provider == "text":
        adapter = HTTPProvider("openai", configuration("openai"), api_key="synthetic", transport=transport)
        invoke = lambda: adapter.generate(packet())
        expected = "timeout"
    else:
        adapter = HTTPImageProvider("openai", synthetic_image_configuration(), api_key="synthetic", transport=transport)
        invoke = lambda: adapter._http("https://provider.invalid", {}, {}, timeout=3, maximum_bytes=1024 * 1024)
        expected = "image_transport_or_response_unknown"
    with pytest.raises(ProviderFailure) as failure:
        invoke()
    assert failure.value.code == expected and failure.value.provider_request_id == "req_read"
    assert failure.value.may_have_sent and failure.value.cost_usd is None
    assert "SECRET" not in str(failure.value)
