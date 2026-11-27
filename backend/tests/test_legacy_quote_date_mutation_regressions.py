"""New work on a legacy brief uses the default timezone at dispatch and publication."""
from datetime import timedelta
from zoneinfo import ZoneInfo

from round_robin.database.models import Attempt, ReportVersion, Run, Step, utcnow
from round_robin.orchestration.budget import aware
from round_robin.orchestration.reporting import comparison_as_of, comparison_data
from round_robin.schemas.briefs import DraftBrief
from sqlalchemy import select

from backend.tests.test_evidence_regressions import complete_brief
from backend.tests.test_orchestration import drain, seed
from backend.tests.test_orchestration import factory as factory  # noqa: PLC0414


def test_legacy_brief_without_timezone_preserves_original_inputs_and_expires_new_work(factory):
    zone = ZoneInfo("America/Phoenix")
    accepted_at = utcnow() - timedelta(days=3)
    accepted_local_date = accepted_at.astimezone(zone).date()
    run_id = seed(factory, brief=complete_brief(valid_until=accepted_local_date))
    with factory.begin() as session:
        run = session.get(Run, run_id)
        raw = {key: value for key, value in run.input_snapshot.items() if key != 'timezone'}
        # Current draft validation supplies this documented default for older
        # saved briefs that predate the explicit timezone field.
        assert DraftBrief.model_validate(raw).timezone == 'America/Phoenix'
        run.input_snapshot = raw
        run.created_at = accepted_at
        original, _ = comparison_data(DraftBrief.model_validate(raw), as_of=comparison_as_of(run))
        assert original[0]['total'] == '1600.00'
        assert 'resolved_brief' not in run.configuration
    drain(factory)
    with factory() as session:
        run = session.get(Run, run_id)
        assert run.status == 'completed'
        attempt = session.scalar(select(Attempt).join(Step, Step.id == Attempt.step_id).where(Attempt.run_id == run_id, Step.stage == 'intake'))
        assert attempt is not None
        assert attempt.request_json['cost_as_of'] == aware(attempt.authorized_at).astimezone(zone).date().isoformat()
        assert attempt.request_json['calculations'][0]['output'] is None
        report = session.scalar(select(ReportVersion).where(ReportVersion.run_id == run_id))
        assert report is not None
        option = report.content['comparison'][0]
        assert report.content['valuation_date'] == aware(report.created_at).astimezone(zone).date().isoformat()
        assert report.content['valuation_timezone'] == zone.key
        assert option['total'] is None and option['known_subtotal'] == '1600.00'
        assert option['constraint_results'][0]['result'] == 'unknown'
        assert any('quote expired' in reason for reason in option['missing_components'])
