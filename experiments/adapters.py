"""Synthetic provider contracts. This module has no network implementation."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import json
import math
import os
from typing import Any


class ContractError(ValueError):
    pass


def require_offline_environment() -> None:
    """Refuse credentials instead of silently pretending they are unused."""
    names = ("OPENAI_API_KEY", "OPENAI_ADMIN_KEY", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "GOOGLE_API_KEY", "GEMINI_API_KEY", "GOOGLE_APPLICATION_CREDENTIALS", "AZURE_OPENAI_API_KEY", "BRAVE_SEARCH_API_KEY")
    if any(os.environ.get(name) for name in names):
        raise ContractError("provider credentials must be absent in simulation mode")


@dataclass(frozen=True)
class Failure:
    code: str
    retryable: bool
    may_have_sent: bool
    usage_known: bool = False
    retry_after_seconds: float | None = None
    repair_allowed: bool = False


@dataclass(frozen=True)
class AdapterResult:
    content: dict[str, Any] | None
    failure: Failure | None
    reported_cost: Decimal | None
    simulated: bool = True


def classify_failure(code: str, retry_after: float | None = None) -> Failure:
    """Structured fixtures, never string matching against secret-bearing errors."""
    if code == "timeout_before_send":
        return Failure(code, True, False, True)
    if code in {"timeout_after_send", "transient", "dns", "tls", "truncated", "empty"}:
        return Failure(code, True, True)
    if code == "rate_limit":
        wait = retry_after if retry_after is not None and math.isfinite(retry_after) and retry_after >= 0 else None
        return Failure(code, True, True, False, wait)
    if code in {"malformed", "context_overflow", "invalid_reference", "invalid_numeric"}:
        return Failure(code, True, True, False, None, True)
    # A refusal is not malformed output. Permanent and unknown failures do not retry.
    return Failure(code if code in {"authentication", "exhausted_credit", "invalid_configuration", "unsupported_model", "refusal", "off_topic"} else "unclassified", False, True)


def retry_is_eligible(failure: Failure, *, calls_used: int, remaining_seconds: float, retry_cost: Decimal, available_money: Decimal) -> bool:
    if not retry_cost.is_finite() or not available_money.is_finite():
        return False
    if not failure.retryable or calls_used >= 2 or calls_used < 0 or retry_cost < 0 or retry_cost > available_money:
        return False
    wait = failure.retry_after_seconds if failure.retry_after_seconds is not None else 3.0
    return math.isfinite(remaining_seconds) and remaining_seconds > wait + 25


def prepare_packet(packet: dict[str, Any], *, max_bytes: int = 32768) -> dict[str, Any]:
    """Reject overflow instead of silently truncating requirements or evidence."""
    try:
        raw = json.dumps(packet, sort_keys=True, allow_nan=False, separators=(",", ":")).encode()
    except (TypeError, ValueError) as exc:
        raise ContractError("packet contains invalid values") from exc
    if len(raw) > max_bytes:
        raise ContractError("context_limit: packet cannot be represented faithfully")
    if packet.get("role") == "proposal" and packet.get("peer_proposals"):
        raise ContractError("independent proposal packet contains peer proposals")
    return json.loads(raw)


def validate_contribution(content: dict[str, Any], *, allowed_ids: set[str]) -> dict[str, Any]:
    if not isinstance(content, dict) or content.get("simulated") is not True:
        raise ContractError("unlabeled simulated contribution")
    if not isinstance(content.get("summary"), str) or not content["summary"].strip():
        raise ContractError("empty contribution")
    if not isinstance(content.get("reference_ids"), list) or any(not isinstance(ref, str) or ref not in allowed_ids for ref in content["reference_ids"]):
        raise ContractError("invalid contribution reference")
    try:
        confidence = Decimal(str(content.get("confidence")))
        if not confidence.is_finite() or not 0 <= confidence <= 1:
            raise InvalidOperation
    except (InvalidOperation, ValueError) as exc:
        raise ContractError("invalid numeric value") from exc
    # These checks establish shape/references only, not the truth or usefulness of the text.
    return content


class SimulatedAdapter:
    """Explicit deterministic fixture selection; never imports provider SDKs."""

    def __init__(self, scenario: str = "success"):
        require_offline_environment()
        self.scenario = scenario
        self.call_count = 0

    def generate(self, packet: dict[str, Any]) -> AdapterResult:
        require_offline_environment()
        packet = prepare_packet(packet)
        self.call_count += 1
        if self.scenario not in {"success", "unknown_usage", "invented_refs", "invalid_numeric"}:
            return AdapterResult(None, classify_failure(self.scenario), Decimal("0") if self.scenario == "timeout_before_send" else None)
        content = {
            "simulated": True,
            "summary": "Fictional simulated contribution; no provider reviewed this decision.",
            "reference_ids": list(packet.get("allowed_ids", [])),
            "confidence": "0.5",
        }
        if self.scenario == "invented_refs":
            content["reference_ids"] = ["foreign-evidence"]
        if self.scenario == "invalid_numeric":
            content["confidence"] = "NaN"
        try:
            validate_contribution(content, allowed_ids=set(packet.get("allowed_ids", [])))
        except ContractError:
            code = "invalid_reference" if self.scenario == "invented_refs" else "invalid_numeric"
            return AdapterResult(None, classify_failure(code), Decimal("0.01"))
        return AdapterResult(content, None, None if self.scenario == "unknown_usage" else Decimal("0.01"))
