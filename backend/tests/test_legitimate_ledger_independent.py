"""Independent check of a genuinely persisted resolved objection workflow."""
from copy import deepcopy
from dataclasses import replace

import pytest
from round_robin.database.claim_models import ClaimVersion
from round_robin.database.models import Attempt, Event, Run, Step, utcnow
from round_robin.database.services import canonical_hash
from round_robin.orchestration.admission import lock_work_run
from round_robin.orchestration.controller import advance_run
from round_robin.providers.base import validate_output
from round_robin.providers.simulated import SimulatedProvider
from round_robin.research.claims import ClaimRejected
from round_robin.research.discussion import input_hash, verify_discussion_provenance
from round_robin.research.reuse import StoredClaim
from round_robin.schemas.contributions import (
    Claim,
    DiscussionObjection,
    ObjectionResponse,
)
from round_robin.worker.tasks import execute_step
from sqlalchemy import select

from backend.tests.test_orchestration import drain, seed
from backend.tests.test_orchestration import factory as factory  # noqa: PLC0414


@pytest.mark.parametrize("withdraw", [False, True])
def test_legitimate_open_answer_and_independent_resolution_keep_working(factory, withdraw):
    class Scripted(SimulatedProvider):
        def generate(self, request):
            result = super().generate(request)
            values = {}
            if request.stage == "proposal" and self.provider == "openai":
                source = request.evidence[0]["id"]
                calculation = request.calculations[0]["id"]
                values["claims"] = [Claim(alias="new_claim_1", proposition="A scoped assertion", kind="fact", evidence_status="not_checked", reference_ids=[source])]
                imported = next(item["id"] for item in request.claims if item["id"] == "claim_imported")
                values["objections"] = [DiscussionObjection(alias="new_objection_1", concern="Check this supplied source", severity="material", target_reference_id="new_claim_1", reference_ids=[source, calculation, "new_claim_1", imported])]
            if request.stage in {"synthesis", "audit"} and request.open_objections:
                source = request.evidence[0]["id"]
                # A synthesis request for resolution is intentionally reduced
                # to an answer until a distinct auditor approves it.
                disposition = "withdrawn" if withdraw and request.stage == "synthesis" else "resolved"
                values["objection_responses"] = [ObjectionResponse(objection_id=request.open_objections[0]["id"], disposition=disposition, response="Review the supplied source", reference_ids=[source, request.calculations[0]["id"], request.peer_outputs[0]["id"], request.claims[0]["id"]])]
            if not values:
                return result
            output = result.output.model_copy(update=values)
            validate_output(output, request)
            return replace(result, output=output)

    run_id = seed(factory)
    with factory.begin() as session:
        run = lock_work_run(session, run_id)
        advance_run(session, run)
        intake_id = session.scalar(select(Step.id).where(Step.run_id == run_id, Step.stage == "intake"))
    execute_step(intake_id, session_factory=factory)
    with factory.begin() as session:
        run = lock_work_run(session, run_id)
        claim = StoredClaim.model_validate({"id": "claim_imported", "version": 1, "proposition": "An imported assumption", "kind": "assumption", "evidence_status": "not_checked", "reference_ids": [], "time_sensitive": True, "verified_at": utcnow().isoformat()}).model_dump(mode="json", exclude_unset=True)
        session.add(ClaimVersion(id=claim["id"], version=1, run_id=run_id, meeting_id=run.meeting_id, owner_id=run.owner_id, step_id=None, sha256=canonical_hash(claim), content=claim))
    drain(factory, {name: Scripted(name) for name in ("openai", "anthropic", "google")})
    with factory() as session:
        run = session.get(Run, run_id)
        candidates = [item for item in run.configuration["discussion_issues"] if item["concern"] == "Check this supplied source"]
        assert candidates, [(step.stage, step.provider, step.status, step.error_code) for step in session.scalars(select(Step).where(Step.run_id == run_id))]
        issue = candidates[0]
        assert issue["status"] == ("withdrawn" if withdraw else "resolved")
        assert [event["disposition"] for event in issue["events"]] == (["opened", "withdrawn"] if withdraw else ["opened", "answered", "resolved"])
        assert all(event["input_hash"] == input_hash(run) for event in issue["events"])
        if withdraw:
            assert issue["events"][1]["provider"] == issue["source_provider"]
        else:
            assert issue["events"][1]["provider"] != issue["events"][2]["provider"]
        events = [event.payload for event in session.scalars(select(Event).where(Event.run_id == run_id, Event.event_type == "discussion.obligation_recorded"))]
        assert all({"objection_id": issue["id"], "event": item} in events for item in issue["events"])
        for item in issue["events"]:
            step = session.get(Step, item["step_id"])
            assert step.run_id == run_id and step.status == "succeeded"
            assert (item["provider"], item["stage"]) == (step.provider, step.stage)
            attempt = session.get(Attempt, step.result_json["attempt_id"])
            assert attempt.status == "succeeded" and attempt.provider == step.provider
            assert attempt.response_json == step.result_json
            local_claim_ids = {claim["id"] for claim in step.result_json["output"].get("claims", [])}
            assert set(item["reference_ids"]) <= set(attempt.request_json["allowed_reference_ids"]) | local_claim_ids
        if not withdraw:
            original_configuration = deepcopy(run.configuration)
            reordered_configuration = deepcopy(run.configuration)
            changed_issue = next(item for item in reordered_configuration["discussion_issues"] if item["id"] == issue["id"])
            changed_issue["events"][1], changed_issue["events"][2] = changed_issue["events"][2], changed_issue["events"][1]
            run.configuration = reordered_configuration
            session.flush()
            with pytest.raises(ClaimRejected):
                verify_discussion_provenance(session, run)
            run.configuration = original_configuration
            session.flush()
            verify_discussion_provenance(session, run)
        local_claim = session.get(ClaimVersion, issue["target_reference_id"])
        correct_step = local_claim.step_id
        other_step = session.scalar(select(Step.id).where(Step.run_id == run_id, Step.status == "succeeded", Step.id != correct_step).limit(1))
        local_claim.step_id = other_step
        session.flush()
        with pytest.raises(ClaimRejected):
            verify_discussion_provenance(session, run)
        local_claim.step_id = correct_step
        session.flush()
        verify_discussion_provenance(session, run)
        if not withdraw:
            audit = session.get(Step, issue["events"][-1]["step_id"])
            altered = deepcopy(audit.result_json)
            altered["output"]["objection_responses"][0]["disposition"] = "answered"
            audit.result_json = altered
            session.flush()
            # The durable journal claims a resolution, but the committed
            # contribution now only answers; it cannot certify closure.
            with pytest.raises(ClaimRejected):
                verify_discussion_provenance(session, run)
