"""Boundary and failure injection across evidence, arithmetic and saved reports."""
from datetime import timedelta
import json
from types import SimpleNamespace

import pytest

from round_robin.fixtures import sample_brief
from round_robin.research.calculator import CalculationError, Operand, Operation, break_even, evaluate, lifecycle_total
from round_robin.research.claims import ClaimRejected, assign_claim_ids
from round_robin.research.fetcher import FetchAllowance, FetchRejected, FetchResponse, FetchUnavailable, collect_source, exact_passages, validate_target
from round_robin.research.queries import PublicScope, QueryRejected, render_query
from round_robin.research.search import BraveSearch


def operand(amount="1", unit="dimensionless", kind="quantity"):
    return Operand(amount=amount, unit=unit, kind=kind)


def test_dimension_precision_and_serialization_limits_are_enforced_independently():
    with pytest.raises(CalculationError, match="dimensionless scale"):
        evaluate(Operation(operation="multiply", operands=[operand(unit="USD"), operand(unit="USD")]))
    with pytest.raises(CalculationError, match="matching units"):
        evaluate(Operation(operation="divide", operands=[operand(unit="USD"), operand(unit="EUR")]))
    assert evaluate(Operation(operation="divide", operands=[operand("10", "USD"), operand("2")])) == ("5", "USD", [])
    assert evaluate(Operation(operation="multiply", operands=[operand("2"), operand("5", "USD")])) == ("10", "USD", [])
    with pytest.raises(CalculationError, match="Inexact nondivision"):
        evaluate(Operation(operation="multiply", operands=[operand("0.123456789012", kind="rate")] * 6))
    with pytest.raises(CalculationError, match="96 characters"):
        evaluate(Operation(operation="divide", operands=[operand(), *[operand("1000000000")] * 12]))


def test_lifecycle_and_break_even_preserve_not_applicable_and_negative_crossing():
    first, second = sample_brief().options[:2]
    ignored = first.cost_components[0].model_copy(update={"status": "not_applicable"})
    first = first.model_copy(update={"cost_components": [*first.cost_components, ignored]})
    assert lifecycle_total(first, 12, "USD", calculation_id="calc_edge").output == "1600.00"
    assert break_even(first, second, "USD").output == "20"
    second = second.model_copy(update={"cost_components": [c.model_copy(update={"amount": "1"}) for c in second.cost_components]})
    with pytest.raises(CalculationError, match="nonnegative"):
        break_even(first, second, "USD")
    second = second.model_copy(update={"cost_components": [c.model_copy(update={"amount": "1" if c.frequency == "monthly" else "3"}) for c in second.cost_components]})
    # Unequal recurring rates that do not divide the one-time difference exactly.
    first = first.model_copy(update={"cost_components": [c.model_copy(update={"amount": "4" if c.frequency == "monthly" else "1"}) for c in first.cost_components]})
    result = break_even(first, second, "USD")
    assert result.output is not None and result.flags == ["division rounded at Decimal precision 64"]


@pytest.mark.parametrize("scope", [
    {"terms": tuple((str(n), "x") for n in range(9))}, {"terms": (("a", ""),)},
    {"terms": (("a", "x\n"),)}, {"terms": (("a", "x"), ("a", "y"))},
    {"region": "x" * 121}, {"template_ids": ("unregistered",)},
])
def test_public_scope_rejects_unapproved_shapes_before_query_construction(scope):
    with pytest.raises(QueryRejected): PublicScope(**scope)


def test_alias_collision_unknown_target_and_reference_budget_are_closed():
    claims = {"claims": [{"alias": "new_claim_1"}, {"alias": "new_claim_2"}]}
    with pytest.raises(ClaimRejected, match="new and unique"):
        assign_claim_ids(claims, allowed_reference_ids=set(), id_factory=lambda: "claim_same")
    with pytest.raises(ClaimRejected, match="typed arrays"):
        assign_claim_ids({"reference_ids": "source_one"}, allowed_reference_ids={"source_one"})
    with pytest.raises(ClaimRejected, match="not scoped"):
        assign_claim_ids({"target_reference_id": "source_foreign"}, allowed_reference_ids=set())
    result, _ = assign_claim_ids({"claims": [{"alias": "new_claim_1"}], "objections": [{"target_reference_id": "new_claim_1"}]}, allowed_reference_ids=set(), id_factory=lambda: "claim_server")
    assert result["objections"][0]["target_reference_id"] == "claim_server"
    refs = {f"source_{index}" for index in range(41)}
    with pytest.raises(ClaimRejected, match="forty"):
        assign_claim_ids({"reference_ids": list(refs)}, allowed_reference_ids=refs)


@pytest.mark.parametrize("response", [
    FetchResponse(503, {}, b"private upstream failure"), FetchResponse(200, {}, b"x" * 262145),
    FetchResponse(200, {}, b"[]"), FetchResponse(200, {}, b'{"web":{"results":{}}}'),
    FetchResponse(200, {}, b'{"web":{"results":[{"url":1}]}}'),
    FetchResponse(200, {}, b'{"web":{"results":[{"url":"https://example.com","title":false}]}}'),
])
def test_search_transport_schema_and_size_failures_are_sanitized(response):
    search = BraveSearch(transport=SimpleNamespace(get=lambda *a, **kw: response), resolver=lambda _: ["93.184.216.34"], api_key="synthetic")
    with pytest.raises(FetchUnavailable) as failure: search.search("approved public scope")
    assert "private" not in str(failure.value)


@pytest.mark.parametrize("query,key", [("", "synthetic"), ("x" * 601, "synthetic"), ("x " * 76, "synthetic"), ("approved", "")])
def test_search_never_dispatches_unbounded_or_unconfigured_query(query, key):
    search = BraveSearch(transport=SimpleNamespace(get=lambda *a, **kw: pytest.fail("dispatch")), api_key=key)
    with pytest.raises(FetchRejected): search.search(query)


def test_dns_expiry_does_not_dispatch_search_or_source(monkeypatch):
    from round_robin.research import search as search_module, fetcher
    clock = [0.0]
    monkeypatch.setattr(search_module.time, "monotonic", lambda: clock[0])
    def resolve(_):
        clock[0] = 9.0
        return ["93.184.216.34"]
    transport = SimpleNamespace(get=lambda *a, **kw: pytest.fail("post-deadline dispatch"))
    with pytest.raises(FetchUnavailable, match="DNS exhausted"):
        BraveSearch(transport=transport, resolver=resolve, api_key="synthetic").search("approved", timeout=5)
    clock[0] = 0
    with pytest.raises(FetchUnavailable, match="DNS deadline"):
        collect_source("candidate", FetchAllowance(registered_urls={"candidate": "https://example.com/"}), transport=transport, resolver=resolve, timeout=5)


@pytest.mark.parametrize("response,exception", [
    (FetchResponse(307, {}, b""), FetchRejected), (FetchResponse(404, {}, b""), FetchUnavailable),
    (FetchResponse(200, {"content-type": "text/html"}, b"<script>only code</script>"), FetchUnavailable),
])
def test_source_redirect_status_and_empty_extraction_are_not_evidence(response, exception):
    with pytest.raises(exception):
        collect_source("candidate", FetchAllowance(registered_urls={"candidate": "https://example.com/"}),
            transport=SimpleNamespace(get=lambda *a, **kw: response), resolver=lambda _: ["93.184.216.34"])


def test_url_and_passage_extreme_bounds_reject_before_transport():
    with pytest.raises(FetchRejected, match="nested"):
        validate_target("https://example.com/?q=" + "%25" + "25" * 18, resolver=lambda _: pytest.fail("DNS"))
    with pytest.raises(FetchRejected, match="Credential-like"):
        validate_target("https://example.com/sk-syntheticPrivateCredential", resolver=lambda _: pytest.fail("DNS"))
    with pytest.raises(FetchUnavailable): validate_target("https://example.com/", resolver=lambda _: [])
    with pytest.raises(FetchUnavailable): validate_target("https://example.com/", resolver=lambda _: ["bad-address"])
    with pytest.raises(FetchRejected): exact_passages(SimpleNamespace(text="private"), size=0)


def test_empty_report_pdf_size_failure_and_running_measurement_bounds(monkeypatch):
    from round_robin.reports import exports
    from round_robin.reports.measurements import measure_run
    from test_measurements import BASE, event, run
    assert exports.report_pdf({}, title="Empty preserved report", sources=[]).startswith(b"%PDF-")
    monkeypatch.setattr(exports, "MAX_EXPORT_BYTES", 1)
    with pytest.raises(exports.ExportFailure, match="ten MiB"):
        exports.render_saved_report({}, "json", title="Bound")
    current = run("awaiting_input")
    current.created_at = BASE.isoformat()
    current.first_admitted_at = None
    current.active_started_at = BASE + timedelta(seconds=10)
    measured = measure_run(current, [event(1, "brief.confirmation_required", 20)], now=BASE+timedelta(seconds=40))
    assert measured["user_input_wait_seconds"] == 20 and measured["active_seconds"] == 70
    queued = measure_run(current, [event(1, "brief.confirmed", 20), event(2,"run.admitted",30)], now=BASE+timedelta(seconds=40))
    assert queued["resumption_queue_seconds"] == 10
    ongoing = measure_run(current, [event(1, "brief.confirmation_accepted", 20)], now=BASE+timedelta(seconds=40))
    assert ongoing["resumption_queue_seconds"] == 20 and not ongoing["mandatory_work_complete"]


def test_socket_deadline_stream_contract_and_repeated_close(monkeypatch):
    from round_robin.research import fetcher
    calls=[]
    sock=SimpleNamespace(close=lambda:calls.append("close"),settimeout=lambda value:None,
        sendall=lambda value:calls.append("send"),recv_into=lambda buffer:0)
    monkeypatch.setattr(fetcher.time,"monotonic",lambda:10.0)
    stream=fetcher.DeadlineSocket(sock,9.0)
    with pytest.raises(TimeoutError):stream.sendall(b"private")
    with pytest.raises(FetchRejected):stream.makefile("wb")
    reader=fetcher.DeadlineReader(sock,20.0)
    reader.close();reader.close()
    assert calls==[]


@pytest.mark.parametrize("length,body", [("100",b""),(None,b"12345")])
def test_real_http_transport_enforces_declared_and_actual_byte_ceiling(monkeypatch,length,body):
    from round_robin.research import fetcher
    # Replace only sockets/TLS and HTTP receipt, retaining production target,
    # request, size limit and finally cleanup behavior.
    closed=[]
    raw=SimpleNamespace(settimeout=lambda value:None,close=lambda:closed.append("raw"))
    response=SimpleNamespace(status=200,getheaders=lambda:[] if length is None else [("content-length",length)],read=lambda count:body)
    class Connection:
        sock=None
        def __init__(self,*a,**kw):pass
        def request(self,*a,**kw):assert kw["headers"]["Host"]=="example.com"
        def getresponse(self):return response
        def close(self):closed.append("http")
    monkeypatch.setattr(fetcher.socket,"create_connection",lambda *a,**kw:raw)
    monkeypatch.setattr(fetcher.ssl,"create_default_context",lambda:SimpleNamespace(wrap_socket=lambda *a,**kw:raw))
    monkeypatch.setattr(fetcher.http.client,"HTTPConnection",Connection)
    target=fetcher.validate_target("https://example.com/",resolver=lambda _:["93.184.216.34"])
    with pytest.raises(FetchRejected,match="byte limit"):
        fetcher.SocketPublicTransport().get(target,timeout=1,byte_limit=4)
    assert closed==["http"]


def test_equal_recurring_costs_have_no_unique_break_even():
    option=sample_brief().options[0]
    with pytest.raises(CalculationError,match="No unique"):
        break_even(option,option,"USD")
