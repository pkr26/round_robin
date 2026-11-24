"""Pure, versioned decisions shared by durable and experimental executors.

The graph is deliberately finite. Resource bounds protect an opportunity for
mandatory review; they cannot promise provider availability or factual truth.
"""
from hashlib import sha256
import json


POLICY = {
    "version": "bounded-independent-review-v2",
    "stages": ["intake", "proposals", "synthesis", "audit", "publication"],
    "independent_proposals": 3,
    "minimum_proposals": 2,
    "maximum_corrections": 1,
    "maximum_logical_calls": 2,
    "maximum_infrastructure_claims": 3,
    "request_bound_seconds": 90,
    "persistence_margin_seconds": 25,
    "retry_not_before": True,
    "correction_requires_material_target": True,
    "profiles": {
        "independent_review": {"minimum_proposals": 2, "after_proposals": "synthesis"},
        "single_provider": {"minimum_proposals": 1, "after_proposals": "synthesis"},
        "bounded_debate": {"minimum_proposals": 2, "after_proposals": "critique"},
    },
}


def policy_descriptor(profile="independent_review"):
    if profile not in POLICY["profiles"]:
        raise ValueError("Unsupported discussion policy profile")
    return {"version": POLICY["version"], "profile": profile,
            "sha256": sha256(json.dumps({"policy": POLICY, "profile": profile}, sort_keys=True, separators=(",", ":")).encode()).hexdigest()}


def verify_policy(configuration):
    # Legacy accepted runs retain their finite graph/counters. Safety repairs
    # do not add calls or renew their allowance. Unknown identities fail closed.
    frozen = configuration.get("discussion_policy")
    return frozen is None or frozen == policy_descriptor()


def next_stage(current, successful_providers=(), *, profile="independent_review",
               confirmation_required=False, material_findings=False, correction_admitted=False):
    """Transition after a settled barrier; executor separately proves admission.

    Distinct successful identities, rather than response count, satisfy quorum.
    Experiments explicitly select their ablation profile; production uses only
    independent_review. All profiles terminate after the separately bounded
    correction decision.
    """
    if profile not in POLICY["profiles"]:
        raise ValueError("Unsupported discussion policy profile")
    successful = len(set(successful_providers))
    if current == "intake":
        return "publication" if not successful else "awaiting_input" if confirmation_required else "proposals"
    if current in {"proposal", "proposals"}:
        selected = POLICY["profiles"][profile]
        return selected["after_proposals"] if successful >= selected["minimum_proposals"] else "publication"
    if current == "critique" and profile == "bounded_debate":
        return "evidence_review"
    if current == "evidence_review" and profile == "bounded_debate":
        return "revision" if material_findings else "synthesis"
    if current == "revision" and profile == "bounded_debate":
        return "synthesis"
    if current == "synthesis":
        return "audit" if successful else "publication"
    if current == "audit":
        return "synthesis" if correction_admitted else "publication"
    raise ValueError("Unsupported discussion stage")


def mandatory_tail_seconds(stage, reserved_seconds=190):
    # The configured tail may be larger, never smaller than two bounded calls
    # plus five seconds of per-stage persistence. Retry/fallback is contingent
    # unless separately admitted against the original remaining allowance.
    tail = max(190.0, float(reserved_seconds))
    return 0.0 if stage == "audit" else tail / 2 if stage == "synthesis" else tail


def recovery_fits(*, stage, remaining_seconds, wait_seconds, request_seconds=90,
                  reserved_finish_seconds=190, capacity_delay_seconds=0):
    required = (max(0.0, wait_seconds) + max(0.0, request_seconds)
                + mandatory_tail_seconds(stage, reserved_finish_seconds)
                + max(0.0, capacity_delay_seconds) + POLICY["persistence_margin_seconds"])
    return remaining_seconds > required


def optional_cutoff_reached(*, remaining_seconds, reserved_finish_seconds=190, capacity_delay_seconds=0):
    return remaining_seconds <= mandatory_tail_seconds("proposal", reserved_finish_seconds) + max(0.0, capacity_delay_seconds) + POLICY["persistence_margin_seconds"]


def correction_required(*, audit_passed, critical_defects=(), iteration=0, open_objections=()):
    if iteration >= POLICY["maximum_corrections"]:
        return False
    unresolved = any(item.get("severity") in {"material", "critical"}
                     and item.get("status", "open") in {"open", "answered_pending_review"}
                     for item in open_objections)
    # A negative boolean alone does not supply an actionable correction target.
    target = bool(critical_defects) or unresolved
    return target and (audit_passed is not True or bool(critical_defects) or unresolved)
