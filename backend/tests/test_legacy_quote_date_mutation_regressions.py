"""Legacy saved briefs keep the accepted local quote date through the workflow."""
from datetime import UTC, date, datetime

from round_robin.database.models import Attempt, ReportVersion, Run, Step
from round_robin.schemas.briefs import DraftBrief
from sqlalchemy import select

from backend.tests.test_evidence_regressions import complete_brief
from backend.tests.test_orchestration import drain, seed
from backend.tests.test_orchestration import factory as factory  # noqa: PLC0414


def test_legacy_brief_without_timezone_preserves_valid_quote_and_frozen_date(factory):
    accepted_local_date = date(2026, 11, 16)
    run_id = seed(factory, brief=complete_brief(valid_until=accepted_local_date))
    with factory.begin() as session:
        run = session.get(Run, run_id)
        raw = {key: value for key, value in run.input_snapshot.items() if key != 'timezone'}
        # Current draft validation supplies this documented default for older
        # saved briefs that predate the explicit timezone field.
        assert DraftBrief.model_validate(raw).timezone == 'America/Phoenix'
        run.input_snapshot = raw
        run.created_at = datetime(2026, 11, 17, 1, tzinfo=UTC)
        assert 'resolved_brief' not in run.configuration
    drain(factory)
    with factory() as session:
        run = session.get(Run, run_id)
        assert run.status == 'completed'
        attempt = session.scalar(select(Attempt).join(Step, Step.id == Attempt.step_id).where(Attempt.run_id == run_id, Step.stage == 'intake'))
        assert attempt is not None
        assert attempt.request_json['cost_as_of'] == accepted_local_date.isoformat()
        report = session.scalar(select(ReportVersion).where(ReportVersion.run_id == run_id))
        assert report is not None
        option = report.content['comparison'][0]
        assert option['total'] == '1600.00'
        assert option['constraint_results'][0]['result'] == 'pass'
        assert not any('quote expired' in reason for reason in option['missing_components'])
