"""Canonical timezone names also work on case-sensitive zone databases."""
from datetime import UTC, date, datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones

from round_robin.orchestration import reporting
from round_robin.schemas.briefs import DraftBrief

from backend.tests.test_evidence_regressions import complete_brief


def test_legacy_default_uses_an_exact_installed_timezone_name(monkeypatch):
    # macOS may resolve incorrectly cased file paths. Requiring the exact
    # locally enumerated identifier models a case-sensitive zoneinfo store;
    # the actual conversion still uses the installed ZoneInfo implementation.
    names = available_timezones()
    assert 'America/Phoenix' in names
    assert 'america/phoenix' not in names and 'AMERICA/PHOENIX' not in names
    def strict_zone(name):
        if name not in names:
            raise ZoneInfoNotFoundError('No exact time zone identifier')
        return ZoneInfo(name)
    monkeypatch.setattr(reporting, 'ZoneInfo', strict_zone)
    brief = complete_brief(valid_until=date(2026, 11, 16)).model_dump(mode='json')
    del brief['timezone']
    assert DraftBrief.model_validate(brief).timezone == 'America/Phoenix'
    run = SimpleNamespace(created_at=datetime(2026, 11, 17, 1, tzinfo=UTC), input_snapshot=brief, configuration={})
    comparisons, _ = reporting.comparison_data(DraftBrief.model_validate(brief), as_of=reporting.comparison_as_of(run))
    assert comparisons[0]['total'] == '1600.00'
    assert comparisons[0]['constraint_results'][0]['result'] == 'pass'
