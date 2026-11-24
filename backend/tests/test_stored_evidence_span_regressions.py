import hashlib
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from round_robin.reports.read_adapters import UnreadableReport, read_stored_evidence
from round_robin.schemas.views import StoredEvidence


def read_evidence(**changes):
    text = 'café 🌵'
    data = {'id': 'evidence_a', 'run_id': 'run_a', 'owner_id': 'owner_a',
        'source_title': 'Synthetic source', 'text': text,
        'sha256': hashlib.sha256(text.encode('utf-8')).hexdigest(),
        'origin': 'retrieved', 'verification_status': 'not_checked',
        'verified_at': '2026-01-01T00:00:00Z', 'time_sensitive': False,
        'origin_group': 'group_a', 'locator': 'Paragraph 1', 'scope': 'Synthetic excerpt',
        'selected_start': 2, 'selected_end': 8, 'source_text_length': 12}
    data.update(changes)
    row = SimpleNamespace(id=data['id'], run_id='run_a', owner_id='owner_a', meeting_id='meeting_a',
        sha256=data['sha256'], content=data)
    return read_stored_evidence(row, run_id='run_a', owner_id='owner_a', meeting_id='meeting_a')


def test_unicode_span_and_boundary_are_accepted():
    assert read_evidence()['text'] == 'café 🌵'
    assert read_evidence(selected_start=0, selected_end=6, source_text_length=6)['selected_end'] == 6


def test_legacy_passage_without_offsets_is_accepted():
    assert read_evidence(selected_start=None, selected_end=None, source_text_length=None)['text'] == 'café 🌵'


@pytest.mark.parametrize('changes', [
    {'text': 'cafe 🌵'},
    {'selected_start': 0, 'selected_end': None},
    {'selected_start': None, 'selected_end': 6},
    {'source_text_length': None},
    {'selected_start': 2, 'selected_end': 8, 'source_text_length': 7},
    {'selected_start': 2, 'selected_end': 9},
    {'selected_start': 8, 'selected_end': 8},
])
def test_reader_rejects_altered_content_or_inconsistent_span(changes):
    with pytest.raises(UnreadableReport):
        read_evidence(**changes)


@pytest.mark.parametrize('changes', [
    {'selected_start': None},
    {'source_text_length': None},
])
def test_schema_rejects_missing_span_fields_as_validation_error(changes):
    data = read_evidence()
    data.update(changes)
    with pytest.raises(ValidationError):
        StoredEvidence.model_validate(data)
