from datetime import timedelta

import pytest
from round_robin.database.models import Run
from round_robin.orchestration import circuits
from round_robin.providers.base import ProviderFailure

pytest_plugins = ("backend.tests.test_circuits",)


@pytest.mark.parametrize("other_failure", ["authentication", "schema_invalid", "refusal"])
def test_nontransient_outcome_breaks_transient_failure_streak(circuit_context, other_failure):
    app, run_id, _ = circuit_context
    with app.state.session_factory.begin() as session:
        run = session.get(Run, run_id)
        for code in ("timeout", other_failure, "timeout", "timeout"):
            circuits.record_outcome(session, run, "openai", ProviderFailure(code))
        circuits.before_attempt(session, run, "openai")
        circuits.record_outcome(session, run, "openai", ProviderFailure("timeout"))
        with pytest.raises(ProviderFailure, match="circuit_open"):
            circuits.before_attempt(session, run, "openai")


def test_nontransient_outcome_does_not_clear_an_open_cooldown_or_other_scope(circuit_context):
    app, run_id, clock = circuit_context
    start = clock[0]
    with app.state.session_factory.begin() as session:
        run = session.get(Run, run_id)
        for _ in range(3):
            circuits.record_outcome(session, run, "openai", ProviderFailure("timeout"))
        for _ in range(2):
            circuits.record_outcome(session, run, "anthropic", ProviderFailure("timeout"))
        circuits.record_outcome(session, run, "openai", ProviderFailure("schema_invalid"))
        with pytest.raises(ProviderFailure, match="circuit_open"):
            circuits.before_attempt(session, run, "openai")
        circuits.record_outcome(session, run, "anthropic", ProviderFailure("timeout"))
        with pytest.raises(ProviderFailure, match="circuit_open"):
            circuits.before_attempt(session, run, "anthropic")
    clock[0] = start + timedelta(seconds=30)
    with app.state.session_factory.begin() as session:
        circuits.before_attempt(session, session.get(Run, run_id), "openai")
