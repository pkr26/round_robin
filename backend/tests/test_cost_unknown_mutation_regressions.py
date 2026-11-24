from datetime import date

import pytest
from pydantic import ValidationError

from round_robin.research.calculator import lifecycle_total
from round_robin.schemas.briefs import CostComponent, Option


def component(**changes):
    fields = {'id': 'cost_one', 'label': 'Purchase', 'kind': 'upfront',
        'amount': '10', 'currency': 'USD', 'frequency': 'once', 'source_ref': 'source_one'}
    fields.update(changes)
    return CostComponent.model_validate(fields)


def test_explicit_zero_and_missing_quote_remain_distinct_in_lifecycle():
    zero = component(id='cost_zero', amount='0')
    unknown = component(id='cost_unknown', amount=None, unknown_reason='Quote pending')
    option = Option(id='option_a', label='A', provenance='user_supplied', cost_components=[zero, unknown])
    result = lifecycle_total(option, 12, 'USD', calculation_id='calc_a', as_of=date(2026, 1, 1))
    assert result.output is None
    assert result.known_subtotal == '0.00'
    assert any('Quote pending' in entry for entry in result.missing_components)


@pytest.mark.parametrize('status', ['included', 'excluded', 'not_applicable'])
def test_known_amount_and_unknown_reason_cannot_coexist(status):
    with pytest.raises(ValidationError):
        component(status=status, unknown_reason='Quote pending')


def test_included_missing_amount_requires_reason():
    with pytest.raises(ValidationError):
        component(status='included', amount=None)


@pytest.mark.parametrize('status', ['excluded', 'not_applicable'])
def test_nonincluded_missing_amount_does_not_require_reason(status):
    assert component(status=status, amount=None).unknown_reason is None


def test_known_nonzero_amount_remains_a_cost():
    option = Option(id='option_a', label='A', provenance='user_supplied', cost_components=[component()])
    result = lifecycle_total(option, 12, 'USD', calculation_id='calc_a', as_of=date(2026, 1, 1))
    assert result.output == result.known_subtotal == '10.00'
