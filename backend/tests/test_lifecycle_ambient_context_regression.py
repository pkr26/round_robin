from datetime import date
from decimal import Inexact, localcontext

from round_robin.fixtures import sample_brief
from round_robin.research.calculator import lifecycle_total


def test_prior_inexact_decimal_operation_does_not_mark_exact_cost_as_rounded():
    option = sample_brief().options[0]
    with localcontext() as context:
        context.clear_flags()
        context.flags[Inexact] = True
        result = lifecycle_total(option, 12, 'USD', calculation_id='calc_test', as_of=date(2026, 1, 1))
    assert result.output == '1600.00'
    assert result.flags == []
