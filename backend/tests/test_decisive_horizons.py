"""Saved decisions use eligible rounded totals at supported whole-month horizons."""

from copy import deepcopy
from decimal import Decimal

from backend.tests.test_api import application, client
from backend.tests.test_decisive_report import component, offers, publish


def prices(option, upfront, monthly):
    component(option, "upfront")["amount"] = str(upfront)
    component(option, "recurring")["amount"] = str(monthly)


def add_option(brief, label):
    option = deepcopy(brief["options"][0])
    option.update(id="option_" + label.lower(), label=label)
    for line in option["cost_components"]:
        line["source_ref"] = option["id"]
    brief["options"].append(option)
    return option


def test_only_a_whole_month_leader_is_a_decisive_challenger(application, client):
    brief = offers()
    copper = add_option(brief, "Copper")
    prices(brief["options"][0], 100, 50)
    prices(brief["options"][1], 345, 30)
    prices(copper, 600, 10)
    report = publish(application, client, brief)
    assert report["recommendation"]["option_label"] == "Amber"
    assert "Copper" in report["next_action"] and "Cobalt" not in report["next_action"]
    crossing, = (calc for calc in report["calculations"] if calc["operation"] == "break_even")
    assert Decimal(crossing["output"]) == Decimal("12.5")
    assert crossing["id"] in report["tradeoff_basis_refs"]
    assert "13" in report["what_would_change_answer"]
    brief["horizon_months"] = 13
    assert publish(application, client, brief)["recommendation"]["option_label"] == "Copper"


def test_budget_exhausts_before_the_pair_crosses(application, client):
    brief = offers()
    brief["budget"] = brief["hard_requirements"][0]["threshold"] = "1900"
    report = publish(application, client, brief)
    change = report["what_would_change_answer"]
    assert all(term in change for term in ("18", "19", "1900", "Amber"))
    assert "19" in report["next_action"] and "20 months" not in report["next_action"]
    assert not any(calc["operation"] == "break_even" for calc in report["calculations"])
    cited = {calc["output"] for calc in report["calculations"]
             if calc["horizon_months"] == 19 and calc["id"] in report["tradeoff_basis_refs"]}
    assert cited == {"1950.00", "1980.00"}


def test_all_five_quotes_are_inspectable_on_each_side_of_a_crossover(application, client):
    brief = offers(12)
    for label, amount in (("Copper", 2200), ("Jade", 2350), ("Retain", 2400)):
        option = add_option(brief, label)
        if label == "Retain":
            option["role"] = "status_quo"
        prices(option, amount, 0)
    report = publish(application, client, brief)
    assert report["recommendation"]["option_label"] == "Amber"
    crossing, = (calc for calc in report["calculations"] if calc["operation"] == "break_even")
    assert Decimal(crossing["output"]) == 20
    assert {calc["id"] for calc in report["calculations"]} <= set(report["tradeoff_basis_refs"])
    for month in (12, 19, 20, 21):
        quotes = [calc for calc in report["calculations"] if calc["horizon_months"] == month]
        assert len(quotes) == 5
        assert {Decimal(calc["output"]) for calc in quotes} == {
            1000 + 50 * month, 1600 + 20 * month, 2200, 2350, 2400,
        }
    for calc in report["calculations"]:
        inspected = client.get(f"/api/runs/{report['run_id']}/calculations/{calc['id']}",
                               params={"report_id": report["id"]})
        assert inspected.status_code == 200
        assert inspected.json() == {**calc, "report_id": report["id"], "report_version": report["version"]}


def test_credit_cannot_establish_a_reversal_at_negative_totals(application, client):
    brief = offers()
    for option, fixed, credit in zip(brief["options"], (100, 120), ("5", "5.5"), strict=True):
        prices(option, fixed, 0)
        discount = dict(component(option, "recurring"), kind="discount", id="credit", label="Monthly credit",
                        amount=credit, source_ref=option["id"] + "_credit")
        option["cost_components"].append(discount)
    report = publish(application, client, brief)
    assert report["recommendation"]["option_label"] == "Amber"
    assert "21" in report["what_would_change_answer"]  # Amber's first negative lifecycle subtotal
    assert not any(calc["operation"] == "break_even" for calc in report["calculations"])
    assert "40 months" not in report["next_action"]


def test_sub_month_intersection_is_not_a_change_of_decision(application, client):
    brief = offers()
    prices(brief["options"][0], 99, 30)
    prices(brief["options"][1], 100, 20)
    report = publish(application, client, brief)
    assert report["recommendation"]["option_label"] == "Cobalt"
    assert "0.1" not in report["next_action"]
    assert not any(calc["operation"] == "break_even" for calc in report["calculations"])


def test_cost_excluded_today_can_qualify_at_a_different_horizon(application, client):
    brief = offers()
    brief["budget"] = brief["hard_requirements"][0]["threshold"] = "1500"
    prices(brief["options"][0], 1200, 0)
    prices(brief["options"][1], 100, 150)
    report = publish(application, client, brief)
    assert report["recommendation"]["option_label"] == "Amber"
    assert "Cobalt" in report["what_would_change_answer"] and "7" in report["what_would_change_answer"]
    brief["horizon_months"] = 7
    assert publish(application, client, brief)["recommendation"]["option_label"] == "Cobalt"


def test_exact_intersection_at_supported_endpoint_is_a_tie(application, client):
    brief = offers(119)
    prices(brief["options"][0], 0, 2)
    prices(brief["options"][1], 120, 1)
    report = publish(application, client, brief)
    assert report["recommendation"]["option_label"] == "Amber"
    assert "Both total USD 240.00 at month 120" in report["tradeoff"]
    assert "month 121" not in report["what_would_change_answer"]
    crossing, = (calc for calc in report["calculations"] if calc["operation"] == "break_even")
    assert Decimal(crossing["output"]) == 120


def test_hard_minimum_can_reverse_eligibility_while_cheaper_price_fails(application, client):
    brief = offers(9)
    brief["budget"] = None
    brief["hard_requirements"][0].update(operator="gte", threshold="100", description="Minimum spending obligation")
    prices(brief["options"][0], 0, 10)
    prices(brief["options"][1], 200, 0)
    discount = dict(component(brief["options"][1], "recurring"), kind="discount", id="credit", label="Credit",
                    amount="10", source_ref="monthly_credit")
    brief["options"][1]["cost_components"].append(discount)
    report = publish(application, client, brief)
    assert report["recommendation"]["option_label"] == "Cobalt"
    assert "Cobalt costs less before" not in report["what_would_change_answer"]
    assert "at month 10" in report["what_would_change_answer"].lower()
    assert "tie" in report["what_would_change_answer"]


def test_unattainable_ceiling_takes_precedence_over_price_gap(application, client):
    brief = offers()
    brief["budget"] = brief["hard_requirements"][0]["threshold"] = "500"
    brief["hard_requirements"].append({"id": "support", "description": "Telephone support",
                                       "metric": "manual", "operator": "manual", "origin": "fictional"})
    report = publish(application, client, brief)
    assert report["recommendation"] is None
    assert report["next_evidence_count"] == 2
    assert "Telephone support" not in report["next_action"]
    assert "cost requirement" in report["next_action"]
    assert "500" in report["what_would_change_answer"] and "gap" not in report["what_would_change_answer"]


def test_only_complete_quote_fails_the_cap_and_its_incomplete_peer_cannot_qualify(application, client):
    brief = offers()
    brief["budget"] = brief["hard_requirements"][0]["threshold"] = "1000"
    component(brief["options"][1], "installation").update(amount=None, unknown_reason="Installation quote pending")
    report = publish(application, client, brief)
    assert report["recommendation"] is None and report["next_evidence_count"] == 1
    assert "1000" in report["tradeoff"] and "no current quote" in report["tradeoff"].lower()
    assert "1000" in report["what_would_change_answer"]
    assert report["next_action"] != report["next_evidence"][0]["question"]
