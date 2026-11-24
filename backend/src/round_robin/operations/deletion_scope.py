"""Reject deletion when ownership of a linked record is ambiguous."""

from sqlalchemy import (
    String,
    column,
    exists,
    literal,
    or_,
    select,
    true,
    union_all,
    values,
)

from round_robin.database.claim_models import ClaimVersion, ReuseRecord
from round_robin.database.models import (
    Attempt,
    BudgetOperation,
    DeletionRecord,
    EvidenceRecord,
    ExportRecord,
    Meeting,
    Outbox,
    Outcome,
    PreflightRecord,
    Run,
    Step,
    Upload,
)
from round_robin.database.visual_models import (
    DesignReview,
    DesignSession,
    DesignVersion,
)


def _scope_query(meeting_id, owner_id, scopes=None):
    def correlate(query, *models):
        if scopes is not None:
            return query.correlate(scopes, *models)
        return query.correlate(*models) if models else query.correlate(None)

    runs = correlate(select(Run.id).where(Run.meeting_id == meeting_id))
    designs = correlate(select(DesignSession.id).where(DesignSession.meeting_id == meeting_id))
    steps = correlate(select(Step.id).where(Step.run_id.in_(runs)))
    versions = correlate(select(DesignVersion.id).where(DesignVersion.session_id.in_(designs)))
    preflights = correlate(select(PreflightRecord.id).where(PreflightRecord.meeting_id == meeting_id))

    def check(model, *conditions):
        source = model if scopes is None else scopes.join(model, true())
        return select(literal(1)).select_from(source).where(*conditions)

    checks = [check(Meeting, Meeting.id == meeting_id, Meeting.owner_id != owner_id)]
    for model in (Run, DesignSession, Upload, PreflightRecord, DeletionRecord):
        checks.append(check(model, model.meeting_id == meeting_id, model.owner_id != owner_id))
    checks.append(check(Outcome, or_(Outcome.meeting_id == meeting_id, Outcome.run_id.in_(runs)),
        or_(Outcome.owner_id != owner_id, Outcome.meeting_id != meeting_id, ~Outcome.run_id.in_(runs))))
    for model in (EvidenceRecord, ClaimVersion, ReuseRecord):
        checks.append(check(model, or_(model.meeting_id == meeting_id, model.run_id.in_(runs)),
            or_(model.owner_id != owner_id, model.meeting_id != meeting_id, ~model.run_id.in_(runs))))
    checks.append(check(ExportRecord, ExportRecord.run_id.in_(runs), ExportRecord.owner_id != owner_id))
    checks.append(check(BudgetOperation, or_(BudgetOperation.run_id.in_(runs), BudgetOperation.run_id.in_(designs)), BudgetOperation.owner_id != owner_id))
    checks.append(check(Run, Run.meeting_id == meeting_id, ~correlate(exists().where(
        PreflightRecord.id == Run.preflight_id, PreflightRecord.meeting_id == meeting_id,
        PreflightRecord.owner_id == owner_id), Run)))
    checks.append(check(Run, Run.preflight_id.in_(preflights), or_(Run.meeting_id != meeting_id, Run.owner_id != owner_id)))
    checks.append(check(PreflightRecord, PreflightRecord.meeting_id == meeting_id,
        PreflightRecord.consumed_by_run_id.is_not(None), ~correlate(exists().where(Run.id == PreflightRecord.consumed_by_run_id,
            Run.preflight_id == PreflightRecord.id, Run.meeting_id == meeting_id, Run.owner_id == owner_id), PreflightRecord)))
    for model in (Attempt, Outbox, ClaimVersion):
        checks.append(check(model, or_(model.run_id.in_(runs), model.step_id.in_(steps)),
            model.step_id.is_not(None), ~correlate(exists().where(Step.id == model.step_id, Step.run_id == model.run_id), model)))
    checks.append(check(DesignReview, or_(DesignReview.session_id.in_(designs), DesignReview.version_id.in_(versions)),
        ~correlate(exists().where(DesignVersion.id == DesignReview.version_id, DesignVersion.session_id == DesignReview.session_id), DesignReview)))
    owned_paths = correlate(select(ExportRecord.path).where(ExportRecord.run_id.in_(runs), ExportRecord.path.is_not(None)))
    checks.append(check(ExportRecord, ExportRecord.path.in_(owned_paths), ~ExportRecord.run_id.in_(runs)))
    return union_all(*checks).limit(1)


def deletion_scope_consistent(session, meeting_id, owner_id):
    return session.scalar(_scope_query(meeting_id, owner_id)) is None


def deletion_scopes_consistent(session, pairs):
    # Chunk to keep parameter counts bounded for large restore manifests.
    for offset in range(0, len(pairs), 100):
        scopes = values(column("meeting_id", String(96)), column("owner_id", String(96))).data(pairs[offset:offset + 100]).cte("deletion_scopes")
        if session.scalar(_scope_query(scopes.c.meeting_id, scopes.c.owner_id, scopes)) is not None:
            return False
    return True
