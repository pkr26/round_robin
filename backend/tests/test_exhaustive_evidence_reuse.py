"""Selected immutable reuse through actual owner-scoped HTTP commands."""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta
import hashlib
from types import SimpleNamespace
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import func, select

from round_robin.api.auth import require_owner
from round_robin.database.claim_models import ClaimVersion, ReuseRecord
from round_robin.database.models import Draft, EvidenceRecord, Run, Step, utcnow
from round_robin.database.services import canonical_hash
from round_robin.research.claims import ClaimRejected, assign_claim_ids
from round_robin.research.reuse import claim_packet, persist_claims, read_claim, selected_parent_manifest
from test_operations import prepared
from test_runtime_report import runtime_body
from test_orchestration import factory


def command(**fields):
    return {"command_id": str(uuid4()), **fields}


@pytest.fixture
def parent(prepared):
    app, meeting, run, _, settings = prepared
    factory = app.state.session_factory
    text = "Exact selected parent quote, not instructions."
    with factory.begin() as session:
        row = session.get(Run, run["id"])
        row.status = "completed"
        row.configuration = {**row.configuration, "mode": "live"}
        step = Step(id="step_parent", run_id=row.id, stage="proposal", role_slot_id="proposal_parent", provider="openai", status="succeeded")
        session.add(step)
        session.flush()
        evidence = {"id": "evidence_parent", "run_id": row.id, "owner_id": row.owner_id,
            "source_title": "Selected quote", "text": text, "sha256": hashlib.sha256(text.encode()).hexdigest(),
            "origin": "user_supplied", "verification_status": "supported", "verified_at": utcnow().isoformat(),
            "time_sensitive": True, "origin_group": "origin_parent", "locator": "lines 1-2", "scope": "Supplier supplied quote"}
        session.add(EvidenceRecord(id=evidence["id"], run_id=row.id, meeting_id=row.meeting_id, owner_id=row.owner_id, sha256=evidence["sha256"], content=evidence))
        output = persist_claims(session, row, step, {"claims": [{"alias": "new_claim_1", "proposition": "Selected parent proposition", "kind": "fact", "evidence_status": "supported", "reference_ids": [evidence["id"]]}]}, {evidence["id"]})
        claim = output["claims"][0]
    with TestClient(app) as client:
        response = client.post(f"/api/meetings/{meeting['id']}/follow-ups", json=command(parent_run_id=run["id"], expected_draft_revision=1, brief=meeting["draft"]))
        assert response.status_code == 201, response.text
        revision = response.json()["draft_revision"]
    selections = [{"id": evidence["id"], "kind": "evidence", "version": 1, "sha256": evidence["sha256"]},
                  {"id": claim["id"], "kind": "claim", "version": 1, "sha256": claim["sha256"]}]
    return app, meeting, run, revision, selections, evidence, claim, settings


def preflight(client, parent, selections):
    _, meeting, _, revision, *_ = parent
    return client.post(f"/api/meetings/{meeting['id']}/preflight", json=command(expected_draft_revision=revision, selected_parent_items=selections))


def start(client, parent, quote):
    _, meeting, _, revision, *_ = parent
    payload = command(preflight_id=quote["id"], expected_draft_revision=revision)
    response = client.post(f"/api/meetings/{meeting['id']}/runs", json=payload)
    return response, payload


def test_reuse_copies_selected_bytes_to_new_ids_preserves_provenance_and_downgrades_freshness(parent):
    app, meeting, run, _, selections, evidence, claim, _ = parent
    with TestClient(app) as client:
        visible = client.get(f"/api/runs/{run['id']}/claims")
        assert visible.status_code == 200 and visible.json()["items"][0] == claim
        quote = preflight(client, parent, selections).json()
        assert len(quote["reuse_manifest"]) == 2 and quote["parent_run_id"] == run["id"]
        response, payload = start(client, parent, quote)
        assert response.status_code == 202, response.text
        child = response.json()
        assert client.post(f"/api/meetings/{meeting['id']}/runs", json=payload).json()["id"] == child["id"]
    with app.state.session_factory() as session:
        entries = list(session.scalars(select(ReuseRecord).where(ReuseRecord.run_id == child["id"])))
        assert len(entries) == 2
        mapping = {entry.parent_id: entry.child_id for entry in entries}
        copied = session.get(EvidenceRecord, mapping[evidence["id"]])
        assert copied.content["text"] == evidence["text"] and copied.sha256 == evidence["sha256"]
        assert datetime.fromisoformat(copied.content["verified_at"]) == datetime.fromisoformat(evidence["verified_at"])
        assert copied.content["verification_status"] == "not_checked"
        child_claim = claim_packet(session, session.get(Run, child["id"]), imported_only=True)[0]
        assert child_claim["reference_ids"] == [copied.id]
        assert child_claim["evidence_status"] == "not_checked"
        assert claim["id"] not in child_claim["reference_ids"] and child_claim["id"] != claim["id"]
        assert session.get(ClaimVersion, claim["id"]).content["evidence_status"] == "supported"
        assert session.get(Run, run["id"]).status == "completed"
        assert len(session.get(Run, child["id"]).configuration["reuse_manifest"]) == 2


def test_claim_without_selected_support_loses_parent_reference_and_cannot_claim_support(parent):
    app, _, _, _, selections, _, _, _ = parent
    with app.state.session_factory.begin() as session:
        row = session.get(ClaimVersion, selections[1]["id"])
        row.content = {**row.content, "time_sensitive": False}
        row.sha256 = canonical_hash(row.content)
        selection = {**selections[1], "sha256": row.sha256}
    with TestClient(app) as client:
        quote = preflight(client, parent, [selection]).json()
        response, _ = start(client, parent, quote)
        assert response.status_code == 202, response.text
    with app.state.session_factory() as session:
        copied = claim_packet(session, session.get(Run, response.json()["id"]))[0]
        assert copied["reference_ids"] == [] and copied["evidence_status"] == "not_checked"


@pytest.mark.parametrize("mutation", ["hash", "version", "foreign", "duplicate", "missing", "owner", "corrupt"])
def test_forged_parent_selection_fails_before_child_or_reuse_commit(parent, mutation):
    app, _, _, _, selections, _, _, _ = parent
    selected = deepcopy(selections)
    if mutation == "hash": selected[0]["sha256"] = "0" * 64
    elif mutation == "version": selected[1]["version"] = 2
    elif mutation == "duplicate": selected.append(selected[0])
    elif mutation == "missing": selected[0]["id"] = "evidence_missing"
    else:
        if mutation == "foreign":
            from test_orchestration import seed
            foreign_id = seed(app.state.session_factory, suffix="other")
        with app.state.session_factory.begin() as session:
            row = session.get(EvidenceRecord, selected[0]["id"])
            if mutation == "foreign": row.run_id = foreign_id
            elif mutation == "owner": row.owner_id = "owner_foreign"
            else: row.content = {**row.content, "text": "PRIVATE_CORRUPT_SENTINEL"}
    with TestClient(app) as client:
        result = preflight(client, parent, selected)
        assert result.status_code == 409, result.text
        assert result.json()["error"]["code"] == "reuse_scope_invalid"
        assert "PRIVATE_CORRUPT_SENTINEL" not in result.text
    with app.state.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(Run)) == (2 if mutation == "foreign" else 1)
        assert session.scalar(select(func.count()).select_from(ReuseRecord)) == 0


def test_changed_parent_metadata_after_preflight_rejects_start_and_leaves_preflight_unconsumed(parent):
    app, _, _, _, selections, _, _, _ = parent
    with TestClient(app) as client:
        quote = preflight(client, parent, selections).json()
        with app.state.session_factory.begin() as session:
            evidence = session.get(EvidenceRecord, selections[0]["id"])
            evidence.content = {**evidence.content, "verification_status": "contradicted"}
        response, _ = start(client, parent, quote)
        assert response.status_code == 409
    with app.state.session_factory() as session:
        from round_robin.database.models import PreflightRecord
        assert session.get(PreflightRecord, quote["id"]).consumed_by_run_id is None


def test_claims_are_private_and_whole_meeting_deletion_purges_entire_reuse_lineage(parent):
    app, meeting, run, _, selections, _, _, _ = parent
    with TestClient(app) as client:
        quote = preflight(client, parent, selections).json()
        child, _ = start(client, parent, quote)
        assert child.status_code == 202
        app.dependency_overrides[require_owner] = lambda: "owner_intruder"
        assert client.get(f"/api/runs/{run['id']}/claims").status_code == 404
        app.dependency_overrides.clear()
        assert client.request("DELETE", f"/api/meetings/{meeting['id']}", json=command()).status_code == 200
        assert client.get(f"/api/runs/{run['id']}/claims").status_code == 410
    with app.state.session_factory() as session:
        assert session.scalar(select(func.count()).select_from(ClaimVersion)) == 0
        assert session.scalar(select(func.count()).select_from(ReuseRecord)) == 0


def test_claim_row_integrity_and_packet_scope_fail_closed(parent):
    app, _, run, _, _, _, claim, _ = parent
    with app.state.session_factory.begin() as session:
        row, current = session.get(ClaimVersion, claim["id"]), session.get(Run, run["id"])
        assert claim_packet(session, current, imported_only=True) == []
        row.content = {**row.content, "id": "claim_invented"}
        with pytest.raises(ClaimRejected): read_claim(row, run=current)
    with TestClient(app) as client:
        response = client.get(f"/api/runs/{run['id']}/claims")
        assert response.status_code == 409


@pytest.mark.parametrize("output", [None, {"claims": None}, {"claims": [None]},
    {"claims": [{"alias": "new_claim_1"}], "reference_ids": [None]},
    {"claims": [{"alias": "new_claim_1"}], "target_reference_id": []}])
def test_malformed_claim_helpers_return_controlled_rejection(output):
    with pytest.raises(ClaimRejected):
        assign_claim_ids(output, allowed_reference_ids=set())


def test_parent_helper_requires_exact_version_and_unique_selection():
    from round_robin.research.claims import validate_reuse
    parents={"parent_one":{"meeting_id":"meeting_one","version":2,"content_hash":"a"*64,"time_sensitive":False,"verification_status":"supported"}}
    selected={"id":"parent_one","version":2,"content_hash":"a"*64}
    assert validate_reuse(selected_items=[selected],parent_items=parents,meeting_id="meeting_one",child_run_id="run_child")[0]["verification_status"]=="supported"
    for bad in [[{**selected,"version":1}],[{**selected,"version":True}],[selected,selected],[None]]:
        with pytest.raises(ClaimRejected):validate_reuse(selected_items=bad,parent_items=parents,meeting_id="meeting_one",child_run_id="run_child")


def test_claim_persistence_is_append_only_scoped_and_bounded(parent):
    app,_,run,_,_,_,claim,_=parent
    with app.state.session_factory.begin() as session:
        current=session.get(Run,run["id"]);step=session.get(Step,"step_parent")
        new={"claims":[{"alias":"new_claim_1","proposition":"New","kind":"fact","evidence_status":"not_checked","reference_ids":[]}]}
        with pytest.raises(ClaimRejected,match="overwrite"):persist_claims(session,current,step,new,set())
        with pytest.raises(ClaimRejected,match="outside"):persist_claims(session,current,SimpleNamespace(run_id="run_foreign"),new,set())
        row=session.get(ClaimVersion,claim["id"])
        with pytest.raises(ClaimRejected):read_claim(row,run=SimpleNamespace(id=current.id,meeting_id="meeting_foreign",owner_id=current.owner_id))
        for index in range(200):
            session.add(ClaimVersion(id=f"claim_overflow_{index}",version=1,run_id=current.id,meeting_id=current.meeting_id,owner_id=current.owner_id,content={},sha256="0"*64))
        session.flush()
        with pytest.raises(ClaimRejected,match="capacity"):claim_packet(session,current)
        with pytest.raises(ClaimRejected,match="capacity"):persist_claims(session,current,step,new,set())


def test_selection_requires_parent_and_supported_kind_and_preserves_fresh_support(parent):
    app,meeting,run,_,selections,_,claim,_=parent
    with app.state.session_factory.begin() as session:
        draft=session.get(Draft,meeting["id"])
        with pytest.raises(ClaimRejected,match="accessible"):
            selected_parent_manifest(session,SimpleNamespace(parent_run_id=None,meeting_id=meeting["id"]),"owner_local",selections)
        with pytest.raises(ClaimRejected,match="kind"):
            selected_parent_manifest(session,draft,"owner_local",[{**selections[0],"kind":"unregistered"}])
        for model,item in [(EvidenceRecord,selections[0]),(ClaimVersion,selections[1])]:
            row=session.get(model,item["id"])
            row.content={**row.content,"time_sensitive":False}
            if model is ClaimVersion:
                row.sha256=canonical_hash(row.content);item["sha256"]=row.sha256
    with TestClient(app) as client:
        quote=preflight(client,parent,selections).json()
        child,_=start(client,parent,quote)
        assert child.status_code==202
    with app.state.session_factory() as session:
        copied=claim_packet(session,session.get(Run,child.json()["id"]))[0]
        assert copied["evidence_status"]=="supported"


def test_legacy_command_hash_replays_after_new_optional_fields_are_added(prepared):
    import hmac,json
    from round_robin.database.models import Command
    app,meeting,run,start_body,settings=prepared
    with app.state.session_factory() as session:
        receipt=session.get(Command,("owner_local",start_body["command_id"]))
        legacy={"kind":"start","body":{"meeting_id":meeting["id"],**start_body}}
        digest=hmac.new(settings.command_hash_key.encode(),json.dumps(legacy,sort_keys=True,separators=(",",":"),ensure_ascii=False).encode(),hashlib.sha256).hexdigest()
        assert receipt.body_hash==digest
        pre=session.scalar(select(Command).where(Command.command_type=="preflight"))
        legacy={"kind":"preflight","body":{"meeting_id":meeting["id"],"command_id":pre.command_id,"expected_draft_revision":1,"mode":"simulated"}}
        assert pre.body_hash==hmac.new(settings.command_hash_key.encode(),json.dumps(legacy,sort_keys=True,separators=(",",":"),ensure_ascii=False).encode(),hashlib.sha256).hexdigest()
    with TestClient(app) as client:
        assert client.post(f"/api/meetings/{meeting['id']}/runs",json={**start_body,"replace_parent_run_version":None}).json()["id"]==run["id"]
        assert client.post(f"/api/meetings/{meeting['id']}/preflight",json={"command_id":pre.command_id,"expected_draft_revision":1,"selected_parent_items":[]}).json()["id"]==pre.resource_id


@pytest.mark.parametrize("mutation",["none","late","corrupt","foreign_scope"])
def test_saved_report_claims_are_hash_owner_and_cutoff_bound(factory,runtime_body,mutation):
    from round_robin.database.models import ReportVersion
    from round_robin.reports.read_adapters import UnreadableReport,read_runtime_report
    with factory.begin() as session:
        report=session.get(ReportVersion,runtime_body["id"])
        run=session.get(Run,report.run_id)
        step=session.scalar(select(Step).where(Step.run_id==run.id,Step.stage=="proposal"))
        content={"id":"claim_saved_report","version":1,"predecessor_id":None,"proposition":"Historical exact claim","kind":"fact","evidence_status":"not_checked","reference_ids":[],"time_sensitive":True,"verified_at":utcnow().isoformat().replace("+00:00","Z")}
        created=report.created_at+timedelta(seconds=1) if mutation=="late" else report.created_at-timedelta(seconds=1)
        claim=ClaimVersion(id=content["id"],version=1,run_id=run.id,meeting_id=run.meeting_id,owner_id=run.owner_id,step_id=step.id,sha256=canonical_hash(content),content=content,created_at=created)
        session.add(claim);session.flush()
        report.content={**report.content,"claim_ids":[claim.id]}
        if mutation=="corrupt":claim.content={**content,"proposition":"PRIVATE_ALTERED_CLAIM"}
        elif mutation=="foreign_scope":claim.owner_id="owner_foreign"
        if mutation=="none":assert read_runtime_report(session,report)["claim_ids"]==[claim.id]
        else:
            with pytest.raises(UnreadableReport) as rejected:read_runtime_report(session,report)
            assert "PRIVATE_ALTERED_CLAIM" not in str(rejected.value)


def test_historical_report_without_runtime_tag_or_owning_run_is_safely_unreadable(factory,runtime_body):
    from round_robin.reports.read_adapters import UnreadableReport,read_runtime_report
    with factory() as session:
        with pytest.raises(UnreadableReport,match="Unsupported historical"):
            read_runtime_report(session,SimpleNamespace(content={}))
        content={**runtime_body,"run_id":"run_missing","evidence_ids":[],"contribution_ids":[],"claim_ids":[]}
        row=SimpleNamespace(content=content,id=content["id"],run_id=content["run_id"],version=content["version"],event_cutoff=content["event_cutoff"],created_at=utcnow())
        with pytest.raises(UnreadableReport):read_runtime_report(session,row)


def test_claim_revisions_bind_predecessor_without_overwriting_and_partial_failure_adds_nothing(parent):
    app,_,run,_,_,_,claim,_=parent
    with app.state.session_factory.begin() as session:
        current=session.get(Run,run["id"])
        step=Step(id="step_revision",run_id=current.id,stage="synthesis",role_slot_id="synthesis",stage_iteration=1,status="succeeded")
        session.add(step);session.flush()
        revised={"claims":[{"alias":"new_claim_1","proposition":"Corrected claim","kind":"fact","evidence_status":"mixed","reference_ids":[],"predecessor_id":claim["id"]}]}
        result=persist_claims(session,current,step,revised,{claim["id"]})
        child=result["claims"][0]
        assert child["version"]==2 and child["predecessor_id"]==claim["id"] and child["id"]!=claim["id"]
        assert session.get(ClaimVersion,claim["id"]).content["proposition"]=="Selected parent proposition"
        other=Step(id="step_invalid_claims",run_id=current.id,stage="audit",role_slot_id="audit",status="failed")
        session.add(other);session.flush()
        broken={"claims":[{**revised["claims"][0],"predecessor_id":None},{**revised["claims"][0],"alias":"new_claim_2","predecessor_id":"claim_foreign"}]}
        with pytest.raises(ClaimRejected,match="not supplied"):persist_claims(session,current,other,broken,{claim["id"]})
        session.flush()
        assert session.scalar(select(func.count()).select_from(ClaimVersion).where(ClaimVersion.step_id==other.id))==0
        with pytest.raises(ClaimRejected):persist_claims(session,current,other,broken,{claim["id"],"claim_foreign"})


def test_simulated_claims_and_reused_simulated_support_remain_fictional(parent):
    app,meeting,run,_,_,_,_,_=parent
    with app.state.session_factory.begin() as session:
        current=session.get(Run,run["id"])
        current.configuration={**current.configuration,"mode":"simulated"}
        step=Step(id="step_sim_claim",run_id=current.id,stage="audit",role_slot_id="audit",status="succeeded")
        session.add(step);session.flush()
        result=persist_claims(session,current,step,{"claims":[{"alias":"new_claim_1","proposition":"A simulated assertion","kind":"fact","evidence_status":"supported","reference_ids":[]}]},set())
        claim=result["claims"][0]
        assert claim["evidence_status"]=="fictional"
    with TestClient(app) as client:
        quote=preflight(client,parent,[{"id":claim["id"],"kind":"claim","version":1,"sha256":claim["sha256"]}]).json()
        child,_=start(client,parent,quote)
        assert child.status_code==202
    with app.state.session_factory() as session:
        assert claim_packet(session,session.get(Run,child.json()["id"]))[0]["evidence_status"]=="fictional"
