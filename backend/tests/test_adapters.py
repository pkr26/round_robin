"""Provider failure fixtures establish control contracts, not live support."""

from decimal import Decimal
import json
import socket

import pytest

from experiments.adapters import ContractError, SimulatedAdapter, classify_failure, prepare_packet, require_offline_environment, retry_is_eligible, validate_contribution


@pytest.fixture(autouse=True)
def no_credentials(monkeypatch):
    for name in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GOOGLE_API_KEY", "GEMINI_API_KEY", "AZURE_OPENAI_API_KEY"):
        monkeypatch.delenv(name, raising=False)


def test_timeout_send_certainty_and_unknown_usage_remain_explicit():
    before = SimulatedAdapter("timeout_before_send").generate({})
    after = SimulatedAdapter("timeout_after_send").generate({})
    assert before.failure.may_have_sent is False and before.reported_cost == Decimal("0")
    assert after.failure.may_have_sent is True and after.reported_cost is None
    result = SimulatedAdapter("unknown_usage").generate({})
    assert result.content is not None and result.reported_cost is None


@pytest.mark.parametrize("code", ["authentication", "exhausted_credit", "invalid_configuration", "unsupported_model", "refusal", "off_topic", "unknown"])
def test_permanent_refusal_and_unclassified_failures_do_not_retry(code):
    failure = classify_failure(code)
    assert failure.retryable is False
    assert failure.repair_allowed is False


@pytest.mark.parametrize("code", ["transient", "dns", "tls", "malformed", "truncated", "empty", "context_overflow"])
def test_transient_and_repair_recovery_share_the_second_call(code):
    failure = classify_failure(code)
    assert retry_is_eligible(failure, calls_used=1, remaining_seconds=60, retry_cost=Decimal("0.1"), available_money=Decimal("1"))
    assert not retry_is_eligible(failure, calls_used=2, remaining_seconds=60, retry_cost=Decimal("0.1"), available_money=Decimal("1"))
    assert SimulatedAdapter(code).generate({}).content is None


def test_rate_limit_wait_must_fit_original_allowance():
    long_wait = classify_failure("rate_limit", 1000)
    assert not retry_is_eligible(long_wait, calls_used=1, remaining_seconds=60, retry_cost=Decimal("0.1"), available_money=Decimal("1"))
    invalid_wait = classify_failure("rate_limit", float("nan"))
    assert invalid_wait.retry_after_seconds is None


def test_context_is_bounded_without_dropping_requirements_or_independence():
    packet = {"role": "proposal", "hard_requirements": ["Never silently remove this"], "evidence": "x" * 40000}
    with pytest.raises(ContractError, match="context_limit"):
        prepare_packet(packet)
    with pytest.raises(ContractError, match="peer"):
        prepare_packet({"role": "proposal", "peer_proposals": ["secret peer draft"]})
    with pytest.raises(ContractError, match="invalid values"):
        prepare_packet({"amount": float("inf")})


@pytest.mark.parametrize("scenario", ["invented_refs", "invalid_numeric"])
def test_well_formed_but_invalid_references_and_values_never_validate(scenario):
    result = SimulatedAdapter(scenario).generate({"allowed_ids": ["owned-evidence"]})
    assert result.content is None
    assert result.failure.repair_allowed
    with pytest.raises(ContractError):
        validate_contribution({"simulated": True, "summary": "plausible", "confidence": ".8", "reference_ids": ["other-run"]}, allowed_ids={"owned-evidence"})


def test_simulation_rejects_credentials_and_diagnostics_do_not_echo_secrets(monkeypatch):
    sentinel = "secret-sentinel-do-not-leak"
    monkeypatch.setenv("OPENAI_API_KEY", sentinel)
    with pytest.raises(ContractError) as error:
        require_offline_environment()
    assert sentinel not in str(error.value)


def test_simulated_adapter_never_uses_network_and_labels_every_result(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError("network forbidden")
    monkeypatch.setattr(socket, "socket", denied)
    adapter = SimulatedAdapter()
    result = adapter.generate({"allowed_ids": ["fictional-evidence"]})
    assert result.simulated and result.content["simulated"]
    assert "Fictional" in result.content["summary"]
    assert adapter.call_count == 1
    assert "API_KEY" not in json.dumps(result.content)
