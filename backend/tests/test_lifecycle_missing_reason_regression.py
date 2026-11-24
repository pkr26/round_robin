from datetime import date

import pytest
from round_robin.fixtures import sample_brief
from round_robin.research.calculator import lifecycle_total
from round_robin.schemas.briefs import Option


@pytest.mark.parametrize('status', ['empty', 'not_applicable'])
def test_missing_cost_explanation_requests_explicit_zero(status):
    option_data = sample_brief().options[0].model_dump(mode='json')
    option_data['cost_components'] = [] if status == 'empty' else [
        {**option_data['cost_components'][0], 'status': 'not_applicable'}
    ]
    option = Option.model_validate(option_data)
    result = lifecycle_total(option, 12, 'USD', calculation_id='calc_test', as_of=date(2026, 1, 1))
    assert result.output is None
    assert result.known_subtotal == '0.00'
    assert [item.casefold() for item in result.missing_components] == [
        'no included cost components were supplied; an explicit zero is required to establish zero cost.'
    ]
