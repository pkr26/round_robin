"""Ledger call counts require explicit, Boolean model-call metadata."""
from types import SimpleNamespace

import pytest

from round_robin.orchestration.budget import BudgetDenied, model_call_count


def operation(kind, metadata):
    return SimpleNamespace(kind=kind, metadata_json=metadata)


def test_counts_only_true_flags_across_operation_kinds():
    rows = [operation('model', {'model_call': True}),
            operation('model', {'model_call': False}),
            operation('model', {'model_call': True}),
            operation('model', {'model_call': True}),
            operation('finish_hold', {}),
            operation('public_search', {'model_call': False})]
    assert model_call_count(rows) == 3
    assert model_call_count([]) == 0


@pytest.mark.parametrize('metadata', [None, [], 'true', 1, {'model_call': None},
                                      {'model_call': 'true'}, {'model_call': 1}, {'model_call': 0}])
def test_unreadable_or_nonboolean_metadata_fails_closed(metadata):
    with pytest.raises(BudgetDenied, match='ledger_integrity_failed'):
        model_call_count([operation('model', metadata)])


def test_model_operation_requires_flag_even_when_other_kinds_do_not():
    assert model_call_count([operation('public_search', {}), operation('finish_hold', {})]) == 0
    with pytest.raises(BudgetDenied, match='ledger_integrity_failed'):
        model_call_count([operation('model', {})])
    with pytest.raises(BudgetDenied, match='ledger_integrity_failed'):
        model_call_count([operation('public_search', {'model_call': 'false'})])


def test_bad_record_anywhere_fences_whole_count():
    with pytest.raises(BudgetDenied, match='ledger_integrity_failed'):
        model_call_count([operation('model', {'model_call': True}), operation('model', {})])
