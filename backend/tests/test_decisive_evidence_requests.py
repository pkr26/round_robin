"""Decision boundaries that require a particular quote line or a revised brief."""

import json

import pytest

from backend.tests.test_api import application, client
from backend.tests.test_decisive_report import component, offers, publish


def fixed_prices(alpha, bravo, cap):
    brief = offers()
    brief["budget"] = str(cap)
    brief["hard_requirements"][0].update(threshold=str(cap), description=f"Total cost at most USD {cap}")
    for option, name, amount in zip(brief["options"], ("Alpha", "Bravo"), (alpha, bravo), strict=True):
        option["label"] = name
        component(option, "upfront")["amount"] = str(amount)
        component(option, "recurring")["amount"] = "0"
    return brief


def test_renewing_an_expired_rebate_can_restore_eligibility_and_change_the_winner(application, client):
    brief = fixed_prices(120, 95, 100)
    alpha = brief["options"][0]
    rebate = dict(component(alpha, "upfront"), kind="discount", label="Supplier rebate", id="supplier_rebate",
                  source_ref="rebate_letter", amount="0", status="not_applicable", quote_valid_until="2000-01-01")
    alpha["cost_components"].append(rebate)
    report = publish(application, client, brief)
    request, = report["next_evidence"]
    assert report["next_action"] == request["question"]
    assert all(term in request["question"] for term in ("Alpha", "renew", "Supplier rebate", "2000-01-01"))
    assert all(term in request["affected_conclusion"] for term in ("120", "100", "20.00", "95", "25", "Bravo"))
    assert {row["calculation_id"] for row in report["comparison"]} <= set(request["basis_refs"])
    for amount, winner in (("19", "Bravo"), ("20", "Bravo"), ("25", None), ("30", "Alpha")):
        rebate.update(amount=amount, status="included", quote_valid_until="2099-01-01")
        resolved = publish(application, client, brief)
        assert (resolved["recommendation"] or {}).get("option_label") == winner


def test_same_label_quote_lines_keep_their_identity_and_combined_cost_boundary(application, client):
    brief = fixed_prices(100, 120, 150)
    alpha = brief["options"][0]
    east = component(alpha, "installation")
    east.update(label="Commissioning", id="east_line", source_ref="east_proposal",
                amount=None, unknown_reason="Awaiting quote")
    west = dict(east, id="west_line", source_ref="west_proposal")
    alpha["cost_components"].append(west)
    report = publish(application, client, brief)
    assert report["next_evidence_count"] == 2
    for item, expected in zip(report["next_evidence"], ("east_line", "west_line"), strict=True):
        assert expected in item["question"]
        assert "combined" in item["affected_conclusion"].lower() and "20.00" in item["affected_conclusion"]
        source_ids = [ref for ref in item["basis_refs"] if ref in report["evidence_ids"]]
        linked = client.get(f"/api/runs/{report['run_id']}/sources/{source_ids[0]}")
        assert linked.status_code == 200
        assert expected in {fact["component"]["id"] for fact in json.loads(linked.json()["text"])}
    east.update(amount="10", unknown_reason=None)
    one_open = publish(application, client, brief)
    assert one_open["next_evidence_count"] == 1
    remaining, = one_open["next_evidence"]
    assert "west_line" in remaining["question"] and "10.00" in remaining["affected_conclusion"]
    for charge, winner in (("9", "Alpha"), ("10", None), ("11", "Bravo")):
        west.update(amount=charge, unknown_reason=None)
        resolved = publish(application, client, brief)
        assert (resolved["recommendation"] or {}).get("option_label") == winner


@pytest.mark.parametrize("lower_op,upper_op,upper_description", [
    ("gte", "lte", "Delivery window"), ("gte", "lte", "Latest acceptable arrival"),
    ("eq", "lte", "Delivery window"), ("gte", "eq", "Delivery window"), ("eq", "eq", "Delivery window"),
])
def test_impossible_delivery_window_requires_one_owner_decision(application, client, lower_op, upper_op, upper_description):
    brief = fixed_prices(100, 120, 150)
    brief["hard_requirements"].extend(
        {"id": identity, "description": description, "origin": "fictional", "metric": "lead_time_days",
         "operator": op, "threshold": days}
        for identity, description, op, days in (("earliest", "Delivery window", lower_op, "7"),
                                                 ("latest", upper_description, upper_op, "5"))
    )
    report = publish(application, client, brief)
    assert report["next_evidence_count"] == 1
    question, = report["next_evidence"]
    assert question["question"] == report["next_action"]
    assert all(term in question["question"].lower() for term in ("owner", "revise", "5 days", "7 days"))
    assert "supplier" not in question["verification_method"].lower()


def test_delivery_revision_cannot_fix_unaffordable_quotes(application, client):
    brief = fixed_prices(120, 130, 100)
    for option in brief["options"]:
        component(option, "installation").update(amount=None, unknown_reason="Installation quote pending")
    brief["hard_requirements"].extend(
        {"id": op + "_days", "description": "Delivery window", "origin": "fictional", "metric": "lead_time_days",
         "operator": op, "threshold": days} for op, days in (("gte", "7"), ("lte", "5"))
    )
    report = publish(application, client, brief)
    assert report["next_evidence_count"] == 3
    assert any("decision owner" in item["question"] for item in report["next_evidence"])
    assert "100" in report["next_action"] and "delivery limit" not in report["next_action"]


def test_rebate_can_reach_only_a_tie_under_an_exact_cost_requirement(application, client):
    brief = fixed_prices(120, 95, 95)
    brief["hard_requirements"][0].update(operator="eq", description="Spend exactly USD 95")
    alpha = brief["options"][0]
    alpha["cost_components"].append(dict(component(alpha, "upfront"), kind="discount", label="Rebate",
                                         id="rebate_line", source_ref="rebate_letter", amount="0",
                                         status="not_applicable", quote_valid_until="2000-01-01"))
    report = publish(application, client, brief)
    request, = report["next_evidence"]
    assert "Rebate" in report["next_action"] and "25.00" in request["affected_conclusion"]
    assert "only eligible total would tie" in request["affected_conclusion"]
