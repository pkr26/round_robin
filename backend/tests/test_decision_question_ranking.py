"""Question priority and measurement requests in locally published decision reports."""

from copy import deepcopy

import pytest

from backend.tests.test_api import application, client, command, create, start
from backend.tests.test_decisive_report import component, offers, publish
from backend.tests.test_orchestration import drain


def publish_without_budget(application, client, brief):
    started = start(client, create(client, brief))
    assert started.status_code == 202
    url = f"/api/runs/{started.json()['id']}"
    drain(application.state.session_factory)
    run = client.get(f"{url}/snapshot").json()["run"]
    assert run["status"] == "awaiting_input"
    confirmation = client.post(f"{url}/clarifications", json=command(expected_run_version=run["version"], brief=brief))
    assert confirmation.status_code == 200
    drain(application.state.session_factory)
    saved = client.get(f"{url}/reports/1")
    assert saved.status_code == 200, saved.text
    report = saved.json()
    assert report["execution_status"] == "completed" and report["simulated"] and report["usage"]["actual_cost"] == "0"
    return report


@pytest.mark.parametrize("bravo_role", ["candidate", "status_quo"])
def test_all_incomplete_quotes_rank_rebates_ahead_of_dominated_and_zero_quantity_charges(application, client, bravo_role):
    brief = offers()
    brief.update(budget=None, hard_requirements=[])
    template = brief["options"][0]
    brief["options"] = []
    for label, base, kind in (("Alpha", "500", "installation"), ("Bravo", "100", "discount"),
                              ("Charlie", "101", "discount")):
        option = deepcopy(template)
        option.update(id=f"option_{label.lower()}", label=label)
        component(option, "upfront")["amount"] = base
        component(option, "recurring")["amount"] = "0"
        for line in option["cost_components"]:
            line["source_ref"] = f"{label.lower()}_{line['kind']}"
        gap = component(option, "installation")
        if kind == "discount":
            gap = {**gap, "id": "supplier_rebate", "label": "Supplier rebate", "kind": kind,
                   "source_ref": f"{label.lower()}_rebate"}
            option["cost_components"].append(gap)
        gap.update(amount=None, status="included", unknown_reason="Supplier price is outstanding.")
        brief["options"].append(option)
    charlie = brief["options"][-1]
    brief["options"][1]["role"] = bravo_role
    component(charlie, "tax_delivery").update(quantity="0", amount=None, status="included",
                                              unknown_reason="The price per unused unit is missing.")
    report = publish_without_budget(application, client, brief)
    assert report["recommendation"] is None
    rows = {row["label"]: row for row in report["comparison"]}
    assert all(row["total"] is None for row in rows.values())
    questions = report["next_evidence"]
    assert report["next_evidence_count"] == len(questions) == 4
    assert report["next_action"] == questions[0]["question"]
    assert "Charlie" in questions[0]["question"] and "rebate" in questions[0]["question"].lower()
    assert "Bravo" in questions[1]["question"] and "rebate" in questions[1]["question"].lower()
    effect = questions[0]["affected_conclusion"]
    assert "1.00" in effect and "100.00" in effect and "net to zero" in effect
    assert {rows[label]["calculation_id"] for label in ("Bravo", "Charlie")} <= set(questions[0]["basis_refs"])

    for option in brief["options"]:
        for line in option["cost_components"]:
            if line["amount"] is None:
                line.update(amount="0", unknown_reason=None)
    for rebate, winner in (("0", "Bravo"), ("1", None), ("2", "Charlie")):
        component(charlie, "discount")["amount"] = rebate
        resolved = publish_without_budget(application, client, brief)
        assert (resolved["recommendation"] or {}).get("option_label") == winner


def test_one_committed_lead_time_answers_both_delivery_window_checks(application, client):
    brief = offers()
    brief["hard_requirements"].extend(
        {"id": identity, "description": "Delivery window", "origin": "fictional", "metric": "lead_time_days",
         "operator": operator, "threshold": value}
        for identity, operator, value in (("max_delivery", "lte", "10"), ("min_delivery", "gte", "7"))
    )
    report = publish(application, client, brief)
    assert report["next_evidence_count"] == 2
    assert report["next_action"] == report["next_evidence"][0]["question"]
    for item in report["next_evidence"]:
        assert all(term in item["question"] for term in ("lead time in days", "at least 7 days", "at most 10 days"))
        assert "supplier schedule" in item["verification_method"]


def test_incomplete_price_bounds_establish_the_cheaper_offer_without_an_actionable_cost_question(application, client):
    brief = offers()
    brief.update(budget=None, hard_requirements=[])
    for option, label, base, quantity in zip(brief["options"], ("Alpha", "Bravo"), ("500", "100"), ("1", "0"), strict=True):
        option["label"] = label
        component(option, "upfront")["amount"] = base
        component(option, "recurring")["amount"] = "0"
        component(option, "installation").update(amount=None, quantity=quantity, status="included",
                                                 unknown_reason="The installation unit price is outstanding.")
    report = publish_without_budget(application, client, brief)
    assert report["next_evidence_count"] == 2
    assert report["recommendation"] is None and all(row["total"] is None for row in report["comparison"])
    assert report["next_action"] not in {item["question"] for item in report["next_evidence"]}
    text = report["tradeoff"].lower()
    for name, bound in (("bravo", "100"), ("alpha", "500")):
        assert any(name in sentence and bound in sentence for sentence in text.split(". "))
    assert any(term in text for term in ("lower", "less", "cheaper", "lowest"))
    assert {row["calculation_id"] for row in report["comparison"]} <= set(report["tradeoff_basis_refs"])
