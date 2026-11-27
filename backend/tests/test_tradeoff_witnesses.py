"""The saved tradeoff is supported by the decisive quote entries and their checked totals."""

from decimal import Decimal

import pytest

from backend.tests.test_api import application, client, command
from backend.tests.test_decisive_horizons import add_option, prices
from backend.tests.test_decisive_report import component, offers, publish


def test_ranked_charge_and_peer_remain_the_headline_when_offers_are_reordered(application, client):
    brief = offers(6)
    indigo, quartz = brief["options"]
    garnet = add_option(brief, "Garnet")
    indigo["label"], quartz["label"] = "Indigo", "Quartz"
    prices(indigo, 140, 0)
    prices(quartz, 200, 0)
    prices(garnet, 900, 0)
    charge = component(indigo, "installation")
    charge.update(label="Commissioning", amount=None, unknown_reason="Commissioning has not been quoted.")
    repair = component(indigo, "maintenance")
    repair.update(status="included", label="Repairs", amount="700", quantity="0", quote_valid_until="2000-01-01")

    guidance = []
    for peers in ((garnet, quartz), (quartz, garnet)):
        brief["options"] = [*peers, indigo]
        saved = publish(application, client, brief)
        headline, change, action = (saved[field] for field in ("tradeoff", "what_would_change_answer", "next_action"))
        assert all(word in headline for word in ("Commissioning", "Indigo", "140", "Quartz", "200"))
        assert "Repairs" not in headline and "Garnet" not in headline
        assert "Commissioning" in change and "Quartz" in change and "60.00" in change
        assert action == saved["next_evidence"][0]["question"] and "Commissioning" in action
        refs = set(saved["tradeoff_basis_refs"])
        assert {row["label"] for row in saved["comparison"] if row["calculation_id"] in refs} == {"Indigo", "Quartz"}
        guidance.append((headline, change, action))
    assert guidance[0] == guidance[1]

    repair.update(amount="999999", quote_valid_until=None)
    for fee, winner in (("59", "Indigo"), ("60", None), ("61", "Quartz")):
        charge.update(amount=fee, unknown_reason=None)
        resolved = publish(application, client, brief)
        assert (resolved["recommendation"] or {}).get("option_label") == winner


@pytest.mark.parametrize("cap,low,high,discount", [("1000", "1080", "1230", "80.00"), ("900", "1350", "2500", "450.00")])
def test_unknown_freight_cannot_close_a_documented_budget_shortfall(application, client, cap, low, high, discount):
    brief = offers(6)
    brief["budget"] = brief["hard_requirements"][0]["threshold"] = cap
    for option, base in zip(brief["options"], (high, low), strict=True):
        prices(option, base, 0)
        component(option, "tax_delivery").update(label="Freight", status="included", amount=None,
                                                  unknown_reason="The freight charge has not been quoted.")
    saved = publish(application, client, brief)
    assert saved["next_evidence_count"] == 2 and saved["recommendation"] is None
    assert all(row["total"] is None for row in saved["comparison"])
    assert all(word in saved["tradeoff"] for word in (high, low, cap, "Cobalt", "Amber", "6 months"))
    assert all(word in saved["what_would_change_answer"] for word in (discount, cap, "Cobalt", "at least"))
    assert all(word in saved["next_action"] for word in (discount, cap, "Cobalt", "Freight"))
    assert {row["calculation_id"] for row in saved["comparison"]} <= set(saved["tradeoff_basis_refs"])


@pytest.mark.parametrize("operator,maximum,minimum,terms", [
    ("eq", "100", None, "exactly USD 100.00"), ("lte", "100", "95", "between USD 95.00 and USD 100.00"),
    ("lte", "100.005", "99.995", "exactly USD 100.00"),
])
def test_cost_revision_respects_the_full_required_interval_even_for_complete_quotes(application, client, operator, maximum, minimum, terms):
    brief = offers()
    brief["budget"] = maximum
    ceiling = brief["hard_requirements"][0]
    ceiling.update(operator=operator, threshold=maximum)
    if minimum:
        brief["hard_requirements"].append(dict(ceiling, id="minimum", operator="gte", threshold=minimum))
    for option, amount in zip(brief["options"], (115, 130), strict=True):
        prices(option, amount, 0)
    saved = publish(application, client, brief)
    assert terms in saved["tradeoff"] and terms in saved["next_action"]
    assert maximum in saved["tradeoff"]
    assert "15.00" in saved["what_would_change_answer"] and "Amber" in saved["next_action"]


def test_a_cheaper_offer_with_a_cited_noncost_failure_cannot_hide_the_viable_budget_revision(application, client):
    brief = offers()
    brief["budget"] = brief["hard_requirements"][0]["threshold"] = "100"
    brief["hard_requirements"].append({"id": "service", "description": "On-site service", "origin": "user_supplied",
                                       "metric": "manual", "operator": "manual"})
    amber, cobalt = brief["options"]
    prices(amber, 90, 0)
    prices(cobalt, 115, 0)
    service_exclusion = "Amber purchase quote explicitly excludes on-site service"
    component(amber, "upfront").update(label=service_exclusion, source_ref="amber_service_exclusion")
    component(cobalt, "tax_delivery").update(label="Freight", status="included", amount=None, unknown_reason="Price pending")
    original = publish(application, client, brief)
    path = f"/api/runs/{original['run_id']}"
    view = client.get(f"{path}/assessments").json()
    target = next(item for item in view["targets"] if item["requirement_id"] == "service" and item["option_id"] == amber["id"])
    refs = [ref for ref in target["reference_ids"] if service_exclusion in client.get(f"{path}/sources/{ref}").json()["text"]]
    assert len(refs) == 1
    response = client.post(f"{path}/assessments", json=command(
        expected_run_version=view["run_version"], target_type="requirement", option_id=amber["id"], requirement_id="service",
        target_hash=target["target_hash"], reference_ids=refs, result="fail",
        attestation="Amber's quotation has no on-site service.", valid_until=None))
    assert response.status_code == 201, response.text
    revised = client.get(f"{path}/reports/{response.json()['report_version']}").json()
    assert revised["version"] == 2
    assert all(term in revised["tradeoff"] for term in ("Amber", "On-site service", "Cobalt", "115", "100"))
    assert all(term in revised["next_action"] for term in ("Cobalt", "15.00", "Freight"))
    assert set(refs) <= set(revised["tradeoff_basis_refs"])


def test_five_offer_budget_boundary_cites_the_full_comparison_on_both_sides(application, client):
    brief = offers()
    brief["budget"] = brief["hard_requirements"][0]["threshold"] = "1900"
    for index, label in enumerate(("Copper", "Gold", "Silver")):
        option = add_option(brief, label)
        prices(option, 1200 + 100 * index, 50)
    brief["options"][-1]["role"] = "status_quo"
    saved = publish(application, client, brief)
    assert all(term in saved["what_would_change_answer"] for term in ("month 18", "1900.00", "month 19"))
    refs = set(saved["tradeoff_basis_refs"])
    witnesses = [calc for calc in saved["calculations"] if calc["id"] in refs and calc["horizon_months"] in {18, 19}]
    names = {row["label"] for row in saved["comparison"]}
    for month in (18, 19):
        at_month = [calc for calc in witnesses if calc["horizon_months"] == month]
        assert {calc["label"] for calc in at_month} == {f"{name}: {month}-month cost" for name in names}
        eligible = [calc for calc in at_month if Decimal(calc["output"]) <= 1900]
        assert [calc["label"] for calc in eligible] == (["Amber: 18-month cost"] if month == 18 else [])
        for calc in at_month:
            detail = client.get(f"/api/runs/{saved['run_id']}/calculations/{calc['id']}", params={"report_id": saved["id"]})
            assert detail.status_code == 200
            assert detail.json()["formula"] == calc["formula"]
