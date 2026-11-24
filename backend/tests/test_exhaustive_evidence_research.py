"""Lease, budget, replay, packet and source boundary adversarial research cases."""
from datetime import timedelta
import hashlib
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from round_robin.database.models import BudgetOperation, EvidenceRecord, Run, utcnow
from round_robin.orchestration.budget import BudgetDenied
from round_robin.research import service
from round_robin.research.fetcher import FetchResponse, FetchUnavailable
from round_robin.research.queries import PublicScope, QueryRejected
from round_robin.research.search import SearchHit
from test_research_service import research_run, SearchFixture, PageFixture


def schedule(factory):
    with factory.begin() as session:
        run = session.get(Run,"run_local")
        assert service.schedule_research(session,run)
        assert service.schedule_research(session,run)


def execute(factory, search=None, transport=None):
    return service.execute_research("run_local",session_factory=factory,search=search or SearchFixture(),
        transport=transport or PageFixture(),resolver=lambda _: ["93.184.216.34"])


def test_query_plan_groups_long_terms_and_refuses_unrepresentable_scope():
    long = PublicScope(terms=tuple((f"term_{n}","word "*24) for n in range(8)))
    plan = service.query_plan(long)
    assert len(plan) == 6 and {f"term_{n}" for n in range(8)} <= {identity for query in plan for identity in query["term_ids"]}
    assert all(len(item["query"])<=600 and len(item["query"].split())<=75 for item in plan)
    with pytest.raises(QueryRejected,match="templates"): service.query_plan(PublicScope(template_ids=()))
    with pytest.raises(QueryRejected,match="fit"): service.query_plan(PublicScope(terms=(("term_one","x "*60),),region="r "*60))
    with pytest.raises(QueryRejected,match="six-query"): service.query_plan(PublicScope(terms=tuple((f"term_{n}","x "*40) for n in range(8))))
    assert service.query_plan(PublicScope()) == []


def test_schedule_unfit_scope_and_already_done_do_not_authorize_transport(research_run):
    with research_run.begin() as session:
        run = session.get(Run,"run_local")
        run.input_snapshot={**run.input_snapshot,"public_research_terms":[{"id":"term_one","text":"x "*60}]}
        run.configuration={**run.configuration,"research":{**run.configuration["research"],"approved_public_region":"r "*60}}
        assert not service.schedule_research(session,run)
        assert run.configuration["research_summary"]["reason"]=="approved_query_exceeds_bounds"
        assert not service.schedule_research(session,run)


def test_fingerprint_and_finish_time_reserve_fail_before_first_query(research_run):
    schedule(research_run)
    with research_run.begin() as session:
        run = session.get(Run,"run_local")
        fingerprint=run.configuration["public_scope_hash"]
        run.configuration={**run.configuration,"public_scope_hash":"changed"}
    with pytest.raises(BudgetDenied,match="scope_changed"): execute(research_run)
    with research_run.begin() as session:
        run=session.get(Run,"run_local")
        run.configuration={**run.configuration,"public_scope_hash":fingerprint}
        run.active_seconds=390
    assert not execute(research_run)
    with research_run() as session:
        assert session.scalar(select(func.count()).select_from(BudgetOperation))==0
        run=session.get(Run,"run_local")
        assert run.configuration["research_state"]=="done"
        assert run.configuration["research_summary"]["reason"]=="finish_time_reserve"


def test_reconcile_future_invalid_and_absent_operation_leases(research_run):
    with research_run.begin() as session:
        run=session.get(Run,"run_local")
        run.configuration={**run.configuration,"research_state":"running","research_lease_until":(utcnow()+timedelta(minutes=1)).isoformat(),"research_operation_ids":["search_absent"]}
        assert not service.reconcile_public_research(session,run)
        run.configuration={**run.configuration,"research_lease_until":"invalid"}
        assert service.reconcile_public_research(session,run)
        assert run.configuration["research_state"]=="done"
        assert not service.reconcile_public_research(session,run)


def test_existing_search_authorization_is_never_replayed(research_run):
    schedule(research_run)
    with research_run.begin() as session:
        run=session.get(Run,"run_local")
        identity="search_"+hashlib.sha256(f"{run.id}:0:{run.configuration['public_scope_hash']}".encode()).hexdigest()[:48]
        session.add(BudgetOperation(id=identity,run_id=run.id,owner_id=run.owner_id,kind="public_search",status="unknown",reserved_usd="0.01",metadata_json={}))
    search=SearchFixture()
    assert execute(research_run,search)
    assert len(search.queries)==2
    with research_run() as session:
        assert "search_outcome_unknown_not_repeated" in session.get(Run,"run_local").configuration["research_summary"]["limitations"]


def test_changed_frozen_query_is_rejected_without_spending(research_run):
    schedule(research_run)
    with research_run.begin() as session:
        run=session.get(Run,"run_local")
        run.configuration={**run.configuration,"research_plan":[{**run.configuration["research_plan"][0],"query":"PRIVATE_INJECTED_QUERY"}]}
    search=SearchFixture()
    assert execute(research_run,search) and search.queries==[]
    with research_run() as session:
        assert session.scalar(select(func.count()).select_from(BudgetOperation))==0


@pytest.mark.parametrize("mutation",["lease","cancel","scope","deadline"])
def test_lease_cancellation_scope_and_total_deadline_fence_later_dispatch_and_commit(research_run,monkeypatch,mutation):
    schedule(research_run)
    clock=[0.0]
    monkeypatch.setattr(service.time,"monotonic",lambda:clock[0])
    class ChangingSearch(SearchFixture):
        def search(self,query,**kwargs):
            self.queries.append(query)
            with research_run.begin() as session:
                run=session.get(Run,"run_local")
                if mutation=="lease": run.configuration={**run.configuration,"research_lease_until":None}
                elif mutation=="cancel": run.status="cancelled"
                elif mutation=="scope": run.input_snapshot={**run.input_snapshot,"public_research_terms":[]}
                else: clock[0]=100
            return []
    search=ChangingSearch()
    if mutation=="scope":
        with pytest.raises(BudgetDenied,match="scope_changed"): execute(research_run,search)
    else:
        assert execute(research_run,search) is (mutation=="deadline")
    assert len(search.queries)==(3 if mutation=="scope" else 1)
    with research_run() as session:
        assert session.scalar(select(func.count()).select_from(EvidenceRecord))==0


def test_page_failure_stays_visible_and_page_allowance_is_bounded(research_run):
    schedule(research_run)
    search=SearchFixture([f"https://example.com/{n}" for n in range(9)])
    transport=SimpleNamespace(get=lambda *a,**kw:FetchResponse(503,{},b""))
    assert execute(research_run,search,transport)
    with research_run() as session:
        summary=session.get(Run,"run_local").configuration["research_summary"]
        assert summary["pages"]==8 and summary["source_count"]==0
        assert "source_unavailable_or_rejected" in summary["limitations"]


@pytest.mark.parametrize("capacity",[0,1,24])
def test_research_cannot_overfill_saved_evidence_or_duplicate_existing_passage(research_run,capacity):
    schedule(research_run)
    text="Synthetic source"
    digest=hashlib.sha256(text.encode()).hexdigest()
    expected="evidence_"+hashlib.sha256("run_local:https://example.com/quote:extracted_text[0:16]".encode()).hexdigest()[:40]
    with research_run.begin() as session:
        run=session.get(Run,"run_local")
        for index in range(capacity):
            identity=expected if index==0 else f"evidence_existing_{index}"
            content={"id":identity,"run_id":run.id,"owner_id":run.owner_id,"source_title":"Synthetic source",
                "text":text,"sha256":digest,"origin":"user_supplied","verification_status":"user_supplied",
                "verified_at":utcnow().isoformat(),"time_sensitive":True,"origin_group":"origin_fixture",
                "locator":"extracted_text[0:16]","scope":"Synthetic supplied evidence for capacity and deduplication"}
            session.add(EvidenceRecord(id=identity,run_id=run.id,meeting_id=run.meeting_id,owner_id=run.owner_id,sha256=digest,content=content))
    transport=SimpleNamespace(get=lambda *a,**kw:FetchResponse(200,{"content-type":"text/plain"},text.encode()))
    assert execute(research_run,SearchFixture(),transport)
    with research_run() as session:
        assert session.scalar(select(func.count()).select_from(EvidenceRecord))==max(capacity,1)
        if capacity==24: assert "passage_packet_limit" in session.get(Run,"run_local").configuration["research_summary"]["limitations"]
