"""Persisted scoped circuit state; probes are ordinary counted authorizations."""

from datetime import datetime, timedelta
import hashlib

from round_robin.database.models import utcnow
from round_robin.database.services import lock_policies
from round_robin.providers.base import ProviderFailure
from .budget import aware


TRANSIENT = {"timeout", "rate_limit", "transient", "connection_failure", "transport_failure"}


def _scope(run, provider):
    model = run.configuration.get("models", {}).get(provider, provider)
    credential = run.configuration.get("credential_versions", {}).get(provider, run.configuration.get("version", "simulated"))
    return hashlib.sha256(f"{provider}:{model}:{credential}".encode()).hexdigest()


def before_attempt(session, run, provider):
    bucket, _ = lock_policies(session, run.owner_id)
    circuits = dict(bucket.metadata_json.get("circuits", {}))
    scope = _scope(run, provider)
    circuit = dict(circuits.get(scope, {}))
    until = datetime.fromisoformat(circuit["open_until"]) if circuit.get("open_until") else None
    if until:
        probe_until = datetime.fromisoformat(circuit["probe_expires_at"]) if circuit.get("probe_expires_at") else utcnow()
        if aware(until) > utcnow() or circuit.get("probe_in_flight") and aware(probe_until) > utcnow():
            raise ProviderFailure("circuit_open", may_have_sent=False, cost_usd=None)
        circuit["probe_in_flight"] = True
        circuit["probe_expires_at"] = (utcnow() + timedelta(seconds=150)).isoformat()
        circuits[scope] = circuit
        bucket.metadata_json = {**bucket.metadata_json, "circuits": circuits}


def record_outcome(session, run, provider, failure=None):
    bucket, _ = lock_policies(session, run.owner_id)
    circuits = dict(bucket.metadata_json.get("circuits", {}))
    scope = _scope(run, provider)
    circuit = dict(circuits.get(scope, {}))
    if failure is None:
        circuits.pop(scope, None)
    elif failure.code in TRANSIENT:
        cutoff = utcnow() - timedelta(seconds=60)
        failures = [value for value in circuit.get("failures", []) if aware(datetime.fromisoformat(value)) >= cutoff]
        failures.append(utcnow().isoformat())
        circuit = {"failures": failures[-3:], "probe_in_flight": False}
        if len(failures) >= 3:
            circuit["open_until"] = (utcnow() + timedelta(seconds=30)).isoformat()
        circuits[scope] = circuit
    else:
        # A failed half-open probe cannot remain occupied forever.
        if circuit.get("probe_in_flight"):
            circuit["probe_in_flight"] = False
            circuit["open_until"] = (utcnow() + timedelta(seconds=30)).isoformat()
            circuits[scope] = circuit
        elif not circuit.get("open_until"):
            # A nontransient result breaks a not-yet-open consecutive failure
            # streak. A previously opened cooldown must remain in force.
            circuits.pop(scope, None)
    bucket.metadata_json = {**bucket.metadata_json, "circuits": circuits}
