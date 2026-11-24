from copy import deepcopy

from round_robin.database.models import Attempt, Draft, ReportVersion, Run
from round_robin.fixtures import sample_brief
from round_robin.providers.base import ProviderRequest
from round_robin.reports.read_adapters import read_runtime_report
from sqlalchemy import select
from test_api import command, create
from test_orchestration import drain, seed

pytest_plugins = ("test_api", "test_orchestration")


def test_preflight_packet_describes_empty_costs_without_certifying_zero(application, client, monkeypatch):
    meeting = create(client)
    with application.state.session_factory.begin() as session:
        draft = session.get(Draft, meeting["id"])
        brief = deepcopy(draft.payload)
        brief["options"][0]["cost_components"] = []
        draft.payload = brief
    packets = []
    original = ProviderRequest.packet
    def capture(self):
        packet = original(self)
        packets.append(packet)
        return packet
    monkeypatch.setattr(ProviderRequest, "packet", capture)
    response = client.post(f"/api/meetings/{meeting['id']}/preflight", json=command(expected_draft_revision=1))
    assert response.status_code == 200, response.text
    calc = next(item for item in packets[0]["calculations"] if item["label"].startswith("Option A"))
    assert calc["formula"] == "No included cost amounts"
    assert calc["output"] is None and calc["missing_components"]


def test_worker_packet_and_new_report_preserve_unknown_and_old_report_bytes(factory):
    brief = sample_brief()
    options = list(brief.options)
    options[0] = options[0].model_copy(update={"cost_components": []})
    brief = brief.model_copy(update={"options": options})
    run_id = seed(factory, brief=brief)
    drain(factory)
    with factory.begin() as session:
        run = session.get(Run, run_id)
        assert run.status in {"completed", "completed_with_limitations"}
        attempt = session.scalar(select(Attempt).where(Attempt.run_id == run_id, Attempt.request_json["stage"].as_string() == "proposal"))
        assert attempt is not None
        calc = next(item for item in attempt.request_json["calculations"] if item["label"].startswith("Option A"))
        assert calc["formula"] == "No included cost amounts" and calc["output"] is None
        report = session.scalar(select(ReportVersion).where(ReportVersion.run_id == run_id))
        content = deepcopy(report.content)
        index = next(i for i, item in enumerate(content["calculations"]) if item["label"].startswith("Option A"))
        assert content["calculations"][index]["formula"] == "No included cost amounts"
        assert content["calculations"][index]["output"] is None
        content["calculations"][index]["formula"] = "Explicit zero cost"
        report.content = content
    with factory() as session:
        report = session.scalar(select(ReportVersion).where(ReportVersion.run_id == run_id))
        before = deepcopy(report.content)
        observed = read_runtime_report(session, report)
        assert observed["calculations"][index]["formula"] == "Explicit zero cost"
        assert report.content == before
