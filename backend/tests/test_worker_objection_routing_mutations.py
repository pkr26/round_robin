"""Workers route unresolved discussion obligations to the proper stages."""

import socket
from types import SimpleNamespace

import pytest
from round_robin.database.models import Run, Step
from round_robin.worker.tasks import build_request

from backend.tests.test_orchestration import seed

pytest_plugins = ("backend.tests.test_orchestration",)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("network calls forbidden")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)


def step(stage):
    return SimpleNamespace(
        id="request_step",
        stage=stage,
        stage_iteration=0,
        role_slot_id=stage,
        calls=0,
        error_code=None,
    )


def test_proposals_do_not_see_other_proposals_obligations_but_synthesis_and_audit_do(
    factory,
):
    run_id = seed(factory, suffix="discussion")
    foreign_id = seed(factory, suffix="foreign_discussion")
    with factory.begin() as session:
        for owner, identity, concern in (
            (run_id, "local_proposal", "Synthetic material concern"),
            (foreign_id, "foreign_proposal", "Other run private concern"),
        ):
            output = {
                "summary": "Synthetic proposal",
                "objections": [
                    {
                        "alias": "objection_1",
                        "severity": "material",
                        "concern": concern,
                        "reference_ids": [],
                    }
                ],
                "critical_defects": ["Synthetic critical concern"],
                "dissent": ["Minor observation"],
            }
            session.add(
                Step(
                    id=identity,
                    run_id=owner,
                    stage="proposal",
                    role_slot_id="proposal",
                    provider="openai",
                    status="succeeded",
                    result_json={"output": output},
                )
            )
    with factory() as session:
        run = session.get(Run, run_id)
        proposal = build_request(session, run, step("proposal")).packet()
        assert proposal["open_objections"] == []
        for stage in ("synthesis", "audit"):
            packet = build_request(session, run, step(stage)).packet()
            assert {issue["concern"] for issue in packet["open_objections"]} == {
                "Synthetic material concern",
                "Synthetic critical concern",
            }
            assert {issue["severity"] for issue in packet["open_objections"]} == {
                "material",
                "critical",
            }
            assert all(issue["status"] == "open" for issue in packet["open_objections"])


def test_initial_intake_has_no_other_objections(factory):
    run_id = seed(factory, suffix="initial")
    with factory() as session:
        request = build_request(session, session.get(Run, run_id), step("intake"))
        assert request.packet()["open_objections"] == []
