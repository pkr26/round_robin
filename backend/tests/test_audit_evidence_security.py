"""Adversarial checks for costs, approved public retrieval and recovery snapshots."""

import fcntl
import json
from threading import Event, Thread
from urllib.parse import quote

import pytest

from round_robin.fixtures import sample_brief
from round_robin.operations.recovery import read_manifest
from round_robin.orchestration.reporting import comparison_data
from round_robin.research.calculator import CalculationError, break_even, lifecycle_total
from round_robin.research.fetcher import FetchRejected, validate_target
from round_robin.research.queries import PublicScope
from round_robin.research.service import query_plan


def test_excluded_installation_cannot_manufacture_a_complete_all_in_total():
    brief = sample_brief()
    option = brief.options[0]
    components = [component.model_copy(update={"status": "excluded", "amount": None, "unknown_reason": None}) if component.kind == "installation" else component for component in option.cost_components]
    option = option.model_copy(update={"cost_components": components})
    calculation = lifecycle_total(option, 12, "USD", calculation_id="calc_audit")
    assert calculation.output is None
    assert any("installation" in item.lower() and "excluded" in item.lower() for item in calculation.missing_components)
    comparisons, _ = comparison_data(brief.model_copy(update={"options": [option]}))
    assert comparisons[0]["total"] is None


@pytest.mark.parametrize("mode", ["empty", "excluded"])
def test_break_even_requires_explicit_complete_costs_in_both_options(mode):
    first, second = sample_brief().options[:2]
    if mode == "empty":
        first = first.model_copy(update={"cost_components": []})
    else:
        first = first.model_copy(update={"cost_components": [component.model_copy(update={"status": "excluded"}) if component.kind == "installation" else component for component in first.cost_components]})
    with pytest.raises(CalculationError, match="complete costs"):
        break_even(first, second, "USD")


def test_every_planned_search_obeys_transport_query_bounds_and_preserves_terms():
    # The cost template is longer than the counterevidence template. Packing by
    # the latter alone used to create authorized queries rejected at transport.
    scope = PublicScope(terms=tuple((f"term_{index}", chr(97 + index) * 111) for index in range(8)))
    plan = query_plan(scope)
    assert plan
    assert all(len(item["query"]) <= 600 and len(item["query"].split()) <= 75 for item in plan)
    assert {identity for item in plan for identity in item["term_ids"]} == {identity for identity, _ in scope.terms}


def test_deeply_percent_encoded_credentials_are_rejected_before_dns():
    key = "token"
    for _ in range(8):
        key = quote(key.replace("t", "%74"), safe="")
    lookups = []
    with pytest.raises(FetchRejected):
        validate_target(f"https://example.com/?{key}=PRIVATE_SENTINEL", lambda host: lookups.append(host) or ["93.184.216.34"])
    assert lookups == []


def test_manifest_reader_waits_for_a_fsynced_whole_append(tmp_path):
    path = tmp_path / "deletion-manifest.jsonl"
    record = {"meeting_id": "meeting_audit", "owner_id": "owner_audit", "execution_fence": 1}
    encoded = json.dumps(record)
    finished = Event()
    result = []
    def read():
        try:
            result.append(read_manifest(path))
        except Exception as error:
            result.append(error)
        finally:
            finished.set()
    with path.open("w", encoding="utf-8") as writer:
        fcntl.flock(writer.fileno(), fcntl.LOCK_EX)
        writer.write(encoded[:20]); writer.flush()
        reader = Thread(target=read)
        reader.start()
        try:
            assert not finished.wait(.1), "Reader observed an in-progress deletion append"
        finally:
            writer.write(encoded[20:] + "\n"); writer.flush()
            fcntl.flock(writer.fileno(), fcntl.LOCK_UN)
            reader.join(timeout=2)
    assert not reader.is_alive()
    assert result == [[record]]


def test_maximum_valid_cost_labels_and_unknown_reasons_remain_calculable():
    first, second = sample_brief().options[:2]
    first = first.model_copy(update={"label": "A" * 500, "cost_components": [component.model_copy(update={"label": "B" * 500}) for component in first.cost_components]})
    result = lifecycle_total(first, 12, "USD", calculation_id="calc_audit")
    assert result.output == "1600.00" and len(result.label) <= 500
    assert break_even(first, second, "USD").output == "20"
    unknown = first.cost_components[0].model_copy(update={"amount": None, "unknown_reason": "C" * 500})
    result = lifecycle_total(first.model_copy(update={"cost_components": [unknown] * 30}), 12, "USD", calculation_id="calc_audit")
    assert result.output is None and len(result.missing_components) == 30
    assert all(len(reason) <= 500 for reason in result.missing_components)


def test_no_excluded_cost_count_can_overflow_missing_component_contract():
    first = sample_brief().options[0]
    excluded = first.cost_components[0].model_copy(update={"label": "E" * 500, "status": "excluded"})
    result = lifecycle_total(first.model_copy(update={"cost_components": [excluded] * 30}), 12, "USD", calculation_id="calc_audit")
    assert result.output is None and len(result.missing_components) == 30


def test_long_approved_scope_preserves_template_diversity():
    scope = PublicScope(terms=tuple((f"term_{index}", chr(97 + index) * 120) for index in range(8)), region="R" * 120)
    plan = query_plan(scope)
    assert len(plan) == 6
    assert {item["template_id"] for item in plan} == set(scope.template_ids)
    assert {identity for item in plan for identity in item["term_ids"]} == {identity for identity, _ in scope.terms}
    assert all(len(item["query"]) <= 600 for item in plan)


@pytest.mark.parametrize("text", ["中文 evidence", "العربية", "evidence 😀"])
def test_pdf_never_silently_drops_unsupported_saved_text(text):
    from round_robin.reports.exports import UnsupportedPdfText, render_saved_report
    report = {"id": "report_audit", "version": 1, "event_cutoff": 1, "recommendation": text}
    with pytest.raises(UnsupportedPdfText):
        render_saved_report(report, "pdf", title="Saved evidence")
    exact_json, _ = render_saved_report(report, "json", title="Saved evidence")
    markdown, _ = render_saved_report(report, "markdown", title="Saved evidence")
    assert json.loads(exact_json)["recommendation"] == text
    assert text in markdown.decode()


def test_non_ascii_csrf_header_is_rejected_as_a_command_error(tmp_path):
    from backend.tests.test_hosted import hosted_settings, session_client
    from round_robin.app import create_app
    app = create_app(f"sqlite:///{tmp_path/'csrf.db'}", settings=hosted_settings(tmp_path), initialize=True)
    client, _, _, _ = session_client(app)
    response = client.post('/api/meetings', content=b'{}', headers=[(b'content-type',b'application/json'),(b'x-csrf-token',b'\xff')])
    assert response.status_code == 403
    assert response.json()['error']['code'] == 'csrf_invalid'
