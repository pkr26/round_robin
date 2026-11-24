from datetime import date

import pytest
from round_robin.fixtures import sample_brief
from round_robin.research.calculator import lifecycle_total
from round_robin.schemas.briefs import Option


@pytest.mark.parametrize('length', [12, 470, 471, 500])
def test_lifecycle_label_preserves_and_bounds_option_name(length):
    data = sample_brief().options[0].model_dump(mode='json')
    data['label'] = 'A' * length
    option = Option.model_validate(data)
    calculation = lifecycle_total(option, 12, 'USD', calculation_id='calc_label', as_of=date(2026, 1, 1))
    expected = option.label if length <= 470 else option.label[:469] + '…'
    assert calculation.label == expected + ': 12-month cost'
    assert calculation.output == '1600.00'
