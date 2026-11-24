from datetime import date

from round_robin.fixtures import sample_brief
from round_robin.research.calculator import lifecycle_total
from round_robin.schemas.briefs import Option


def test_formula_renders_operators_between_cost_components():
    data = sample_brief().options[0].model_dump(mode="json")
    data["cost_components"] = data["cost_components"][:2]
    discount = dict(data["cost_components"][0])
    discount.update(id="discount_test", kind="discount", amount="100")
    data["cost_components"].append(discount)
    option = Option.model_validate(data)

    result = lifecycle_total(option, 12, "USD", calculation_id="calc_test", as_of=date(2026, 1, 1))

    assert result.output == "1500.00"
    assert result.formula == "1000 × 1 + 50 × 1 × 12 months − 100 × 1"
