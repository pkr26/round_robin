import pytest
from pydantic import ValidationError

from round_robin.schemas.runtime import RuntimeResearchSummary
from round_robin.schemas.views import EventView


def event(payload):
    return EventView(run_id='run_a', sequence=1, event_type='research.test', schema_version='1.0',
        timestamp='2026-01-01T00:00:00Z', payload=payload)


def test_valid_nested_unicode_is_preserved():
    payload = {'outer': [{'name': 'café 🌵'}, ('東京', {'status': 'ok'})]}
    assert event(payload).payload == payload


@pytest.mark.parametrize('payload', [
    {'outer': 'bad\x00text'},
    {'outer': {'bad\x00key': 'safe'}},
    {'outer': [{'nested': 'bad\x00text'}]},
    {'outer': ('safe', 'bad\x00text')},
    {'outer': {'nested': 'bad\ud800text'}},
    {'outer': {'bad\udfffkey': 'safe'}},
])
def test_event_nested_invalid_database_text_rejected(payload):
    with pytest.raises(ValidationError):
        event(payload)


def test_research_summary_objective_rejects_bad_nested_text():
    with pytest.raises(ValidationError):
        RuntimeResearchSummary(status='retrieved_unverified', queries=1, pages=0,
            objectives=[{'nested': ['bad\x00text']}])
