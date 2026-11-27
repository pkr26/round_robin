"""Exercise decision guidance through the saved simulated report and its cited sources."""

from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import sys
from decimal import Decimal
from io import BytesIO

from pypdf import PdfReader
import pytest

from backend.tests.test_api import application, client, command, create, start
from backend.tests.test_evidence_regressions import complete_brief
from backend.tests.test_orchestration import drain


def offers(horizon=12):
    brief = complete_brief().model_dump(mode="json")
    brief.update(deadline=None, horizon_months=horizon, user_facts=[])
    brief["options"] = brief["options"][:2]
    brief["hard_requirements"] = [rule for rule in brief["hard_requirements"] if rule["metric"] == "total_cost"]
    for option, label in zip(brief["options"], ("Amber", "Cobalt"), strict=True):
        option["label"] = label
    return brief


def component(option, kind):
    return next(line for line in option["cost_components"] if line["kind"] == kind)


def publish(application, client, brief):
    started = start(client, create(client, brief))
    assert started.status_code == 202, started.text
    run_id = started.json()["id"]
    drain(application.state.session_factory)
    saved = client.get(f"/api/runs/{run_id}/reports/1")
    assert saved.status_code == 200, saved.text
    report = saved.json()
    assert report["execution_status"] == "completed"
    assert report["simulated"] and report["usage"]["actual_cost"] == "0"
    return report


def downloaded_text(client, report, file_format):
    result = client.post(f"/api/runs/{report['run_id']}/exports", json=command(report_id=report["id"], format=file_format))
    assert result.status_code == 200, result.text
    artifact = client.get(result.json()["download_url"])
    assert artifact.status_code == 200
    if file_format == "pdf":
        return " ".join(page.extract_text() for page in PdfReader(BytesIO(artifact.content)).pages)
    return artifact.text.replace("\\", "")


@pytest.mark.parametrize("months,preferred,totals", [
    (12, "Amber", ["1600.00", "1840.00"]),
    (20, None, ["2000.00", "2000.00"]),
    (24, "Cobalt", ["2200.00", "2080.00"]),
])
def test_horizon_changes_order_at_cited_crossover(application, client, months, preferred, totals):
    report = publish(application, client, offers(months))
    assert (report["recommendation"] or {}).get("option_label") == preferred
    assert [item["total"] for item in report["comparison"]] == totals
    cross, = (item for item in report["calculations"] if item["operation"] == "break_even")
    assert Decimal(cross["output"]) == 20 and cross["unit"] == "months"
    assert cross["id"] in report["tradeoff_basis_refs"]
    assert {item["calculation_id"] for item in report["comparison"]} <= set(report["tradeoff_basis_refs"])
    for text in (report["tradeoff"], report["what_would_change_answer"]):
        assert all(term in text for term in ("Amber", "Cobalt"))
    assert "20" in report["what_would_change_answer"]
    assert "2000" in (report["what_would_change_answer"] + report["tradeoff"]).replace(",", "")
    assert report["next_evidence_count"] == 0
    if months == 12:
        run_url = f"/api/runs/{report['run_id']}"
        inspected = client.get(f"{run_url}/calculations/{cross['id']}", params={"report_id": report["id"]})
        assert inspected.status_code == 200
        assert inspected.json()["formula"] == cross["formula"]
        assert inspected.json()["report_id"] == report["id"]
        for operand in cross["inputs"]:
            source = client.get(f"{run_url}/sources/{operand['source_ref']}")
            assert source.status_code == 200 and source.json()["run_id"] == report["run_id"]
            facts = json.loads(source.json()["text"])
            assert any(fact["component"]["amount"] == operand["amount"]
                       and fact["component"]["currency"] == "USD"
                       and fact["option"] in operand["label"] for fact in facts)
        for file_format in ("markdown", "pdf"):
            text = "".join(downloaded_text(client, report, file_format).split())
            assert "".join(report["what_would_change_answer"].split()) in text
            assert cross["id"] in text


def test_equal_fees_keep_the_upfront_difference(application, client):
    brief = offers()
    component(brief["options"][0], "recurring")["amount"] = "20"
    report = publish(application, client, brief)
    assert report["recommendation"]["option_label"] == "Amber"
    assert all(item["operation"] != "break_even" for item in report["calculations"])
    text = (report["tradeoff"] + " " + report["what_would_change_answer"]).lower()
    assert "amber" in text and "cobalt" in text and "600" in text and "20" in text
    assert "equal" in text or "same" in text
    assert {item["calculation_id"] for item in report["comparison"]} <= set(report["tradeoff_basis_refs"])


def test_expired_offer_calls_for_its_own_current_charge(application, client):
    brief = offers()
    component(brief["options"][1], "recurring")["quote_valid_until"] = "2000-01-01"
    report = publish(application, client, brief)
    assert report["recommendation"] is None
    assert all(item["operation"] != "break_even" for item in report["calculations"])
    assert report["comparison"][1]["total"] is None
    top = report["next_evidence"][0]
    assert report["next_action"] == top["question"]
    assert all(term in report["next_action"] for term in ("Cobalt", "2000-01-01"))
    lifecycle = next(item for item in report["calculations"] if item["id"] == report["comparison"][1]["calculation_id"])
    assert {term["source_ref"] for term in lifecycle["inputs"]} <= set(top["basis_refs"])
    assert "Cobalt" in report["what_would_change_answer"]


def test_installation_flips_the_cost_leader_before_it_breaks_the_budget(application, client):
    brief = offers(24)
    installation = component(brief["options"][1], "installation")
    installation.update(amount=None, unknown_reason="Commissioning charge is missing")
    # The first option also needs a quote, but its known charges already exceed the budget.
    deferred = deepcopy(brief["options"][0])
    deferred.update(id="option_deferred", label="Legacy", role="status_quo")
    for line in deferred["cost_components"]:
        line["source_ref"] = "legacy_" + line["kind"]
    component(deferred, "upfront")["amount"] = "5000"
    component(deferred, "tax_delivery").update(status="included", amount=None, unknown_reason="Delivery price is missing")
    brief["options"].insert(0, deferred)
    report = publish(application, client, brief)
    assert report["recommendation"] is None
    assert all(item["operation"] != "break_even" for item in report["calculations"])
    top = report["next_evidence"][0]
    assert report["next_action"] == top["question"] and "Cobalt" in top["question"] and "installation" in top["question"].lower()
    assert "120" in top["affected_conclusion"] and "120" in report["what_would_change_answer"]
    assert {item["calculation_id"] for item in report["comparison"] if item["label"] in {"Amber", "Cobalt"}} <= set(top["basis_refs"])
    assert any(ref in report["evidence_ids"] for ref in top["basis_refs"])
    brief["options"].remove(deferred)
    for charge, winner in (("119", "Cobalt"), ("120", None), ("121", "Amber")):
        installation.update(amount=charge, unknown_reason=None)
        resolved = publish(application, client, brief)
        assert (resolved["recommendation"] or {}).get("option_label") == winner


def test_report_counts_outstanding_questions_before_limiting_the_list(application, client):
    brief = offers()
    brief["hard_requirements"].extend(
        {"id": f"req_{i:02d}", "description": f"Service obligation {i}", "origin": "fictional", "metric": "manual", "operator": "manual"}
        for i in range(11)
    )
    report = publish(application, client, brief)
    assert report["next_evidence_count"] == 22
    assert len(report["next_evidence"]) == 20
    assert report["next_action"] == report["next_evidence"][0]["question"]
    for file_format in ("markdown", "pdf"):
        text = " ".join(downloaded_text(client, report, file_format).split())
        assert "22 outstanding questions" in text and "20" in text


def test_restarting_the_interpreter_preserves_the_published_question_order(application, client):
    from round_robin.fixtures import sample_brief

    brief = sample_brief().model_dump(mode="json")
    brief["deadline"] = None
    report = publish(application, client, brief)
    # Rebuild the worklist in two new interpreters. Source IDs are scoped to each
    # run, so compare the user-facing decision rather than hashes allocated at Start.
    code = """
import json, sys
from datetime import date
from round_robin.orchestration.reporting import comparison_data
from round_robin.reports.insights import decision_insights
from round_robin.reports.questions import ranked_questions
from round_robin.schemas.briefs import DraftBrief
payload = json.load(sys.stdin)
brief = DraftBrief.model_validate(payload["brief"])
day = date.fromisoformat(payload["date"])
rows, _ = comparison_data(brief, as_of=day)
requests = ranked_questions(brief, rows, decision_insights(brief, rows), as_of=day, chosen=None)
print(json.dumps([item["question"] for item in requests]))
"""
    expected = [item["question"] for item in report["next_evidence"]]
    assert expected[0] == report["next_action"]
    root = Path(__file__).resolve().parents[2]
    for seed in ("1", "3"):
        output = subprocess.check_output(
            [sys.executable, "-c", code], text=True, input=json.dumps({"brief": brief, "date": report["valuation_date"]}),
            env={**os.environ, "PYTHONHASHSEED": seed, "PYTHONPATH": str(root / "backend/src")},
            cwd=root, timeout=30,
        )
        assert json.loads(output)[:len(expected)] == expected


def test_price_parallel_runner_up_does_not_hide_the_decisive_crossover(application, client):
    brief = offers()
    parallel = deepcopy(brief["options"][0])
    parallel.update(id="option_parallel", label="Copper")
    for line in parallel["cost_components"]:
        line["source_ref"] = "copper_" + line["kind"]
    component(parallel, "upfront")["amount"] = "1001"
    brief["options"].insert(1, parallel)
    report = publish(application, client, brief)
    assert report["recommendation"]["option_label"] == "Amber"
    cross, = (item for item in report["calculations"] if item["operation"] == "break_even")
    assert Decimal(cross["output"]) == 20 and "Cobalt" in cross["label"]
    assert "Cobalt" in report["what_would_change_answer"]


@pytest.mark.parametrize("upfront", ["5000", "1800"])
def test_an_outstanding_fee_on_a_noncompetitive_offer_preserves_the_viable_crossover(application, client, upfront):
    brief = offers()
    third = deepcopy(brief["options"][0])
    third.update(id="option_civic", label="Civic")
    for line in third["cost_components"]:
        line["source_ref"] = f"civic_{line['kind']}"
    component(third, "upfront")["amount"] = upfront
    component(third, "installation").update(amount=None, unknown_reason="Installation charge pending.")
    brief["options"].insert(0, third)
    report = publish(application, client, brief)

    civic = report["comparison"][0]
    # Its best possible installation price is zero. Even the lower-priced
    # variant (within budget at month 12) cannot undercut Amber's same-rate offer.
    assert civic["total"] is None
    assert Decimal(civic["known_subtotal"]) == Decimal(upfront) + 600
    assert report["recommendation"] is None  # The original incomplete-quote policy still applies.
    question, = report["next_evidence"]
    assert report["next_evidence_count"] == 1
    assert "Civic" in question["question"] and "installation" in question["question"].lower()
    assert civic["calculation_id"] in question["basis_refs"]
    assert "Civic" not in report["next_action"]
    assert all(term in report["next_action"] for term in ("Amber", "Cobalt", "20"))
    crossing, = (item for item in report["calculations"] if item["operation"] == "break_even")
    assert Decimal(crossing["output"]) == 20 and crossing["id"] in report["tradeoff_basis_refs"]
    for fmt in ("markdown", "pdf"):
        text = "".join(downloaded_text(client, report, fmt).split())
        assert "".join(report["next_action"].split()) in text
        assert "".join(question["question"].split()) in text
        assert crossing["id"] in text and "1outstandingquestion" in text


def test_workshop_crossover_is_scoped_when_deadline_and_status_quo_are_unresolved(application, client):
    brief = complete_brief().model_dump(mode="json")
    report = publish(application, client, brief)
    assert report["recommendation"] is None
    cross, = (item for item in report["calculations"] if item["operation"] == "break_even")
    assert Decimal(cross["output"]) == 20 and cross["id"] in report["tradeoff_basis_refs"]
    text = (report["tradeoff"] + " " + report["what_would_change_answer"]).replace(",", "").lower()
    assert all(term in text for term in ("1600", "1840", "2000", "20", brief["deadline"]))
    assert any(term in text for term in ("unresolved", "unconfirmed", "unknown"))
    assert all(any(result["requirement_id"] == "requirement_server_deadline" and result["result"] == "unknown"
                   for result in item["constraint_results"]) for item in report["comparison"])


def test_missing_increase_outranks_a_discount_that_would_widen_the_lead(application, client):
    brief = offers(24)
    installation = component(brief["options"][1], "installation")
    installation.update(amount=None, unknown_reason="Commissioning charge missing")
    brief["options"][1]["cost_components"].append({
        **installation, "id": "rebate", "kind": "discount", "label": "Annual rebate",
        "source_ref": "rebate_quote", "unknown_reason": "Rebate unconfirmed",
    })
    report = publish(application, client, brief)
    assert report["next_evidence_count"] == 2
    assert "installation" in report["next_action"].lower()
    assert "120" in report["next_evidence"][0]["affected_conclusion"]


def test_unconfirmed_status_quo_does_not_hide_the_missing_purchase_quote(application, client):
    brief = complete_brief().model_dump(mode="json")
    brief["horizon_months"] = 24
    second = brief["options"][1]
    component(second, "installation").update(amount=None, unknown_reason="Current installation quote missing.")
    report = publish(application, client, brief)
    assert report["recommendation"] is None
    assert report["next_action"] == report["next_evidence"][0]["question"]
    assert second["label"] in report["next_action"] and "installation" in report["next_action"].lower()
    assert "120" in report["what_would_change_answer"]
    assert report["comparison"][1]["known_subtotal"] == "2080.00"
    assert report["comparison"][2]["option_id"] in report["decision_insights"]["unresolved_option_ids"]
