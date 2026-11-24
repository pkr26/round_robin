"""Outcome-inclusive timing from committed clock samples, never progress text."""

from datetime import datetime, timezone
import math


def aware(value):
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def clock_sample(payload):
    value = payload.get("active_seconds")
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    try:
        seconds = float(value)
    except OverflowError:
        return None
    return seconds if math.isfinite(seconds) and seconds >= 0 else None


def measure_run(run, events: list[dict], *, now=None) -> dict:
    now = aware(now or datetime.now(timezone.utc))
    created = aware(run.created_at)
    admitted = aware(run.first_admitted_at) if run.first_admitted_at else None
    qualified, completion, finish_at = None, None, None
    qualified_seen, completion_seen = False, False
    user_wait, wait_started = 0.0, None
    resumption_queue, queue_started = 0.0, None
    for event in sorted(events, key=lambda item: item["sequence"]):
        kind, payload, at = event["event_type"], event.get("payload", {}), aware(event["timestamp"])
        if kind == "brief.confirmation_required" and wait_started is None:
            wait_started = at
        elif kind in {"brief.confirmed", "brief.confirmation_accepted"}:
            if wait_started:
                user_wait += max(0, (at - wait_started).total_seconds())
                wait_started = None
            queue_started = at
        elif kind == "run.admitted" and queue_started:
            resumption_queue += max(0, (at - queue_started).total_seconds())
            queue_started = None
        if payload.get("qualifying_content") is True and not qualified_seen:
            qualified_seen = True
            qualified = clock_sample(payload)
        if kind == "report.published":
            finish_at = at
            if payload.get("mandatory_work_complete") is True and not completion_seen:
                completion_seen = True
                completion = clock_sample(payload)
    terminal = run.status in {"completed", "completed_with_limitations", "failed", "cancelled"}
    end = finish_at if terminal and finish_at else now
    if wait_started:
        user_wait += max(0, (end - wait_started).total_seconds())
    if queue_started:
        resumption_queue += max(0, (end - queue_started).total_seconds())
    active = max(0, float(run.active_seconds or 0))
    if run.active_started_at:
        active += max(0, (end - aware(run.active_started_at)).total_seconds())
    successful = run.status == "completed" and completion is not None
    return {"run_id": run.id, "simulated": run.configuration.get("mode", "simulated") == "simulated", "admitted": admitted is not None, "status": run.status, "wall_seconds_from_accepted_start": max(0, (end - created).total_seconds()), "initial_queue_seconds": max(0, (admitted - created).total_seconds()) if admitted else max(0, (end - created).total_seconds()), "resumption_queue_seconds": resumption_queue, "user_input_wait_seconds": user_wait, "active_seconds": active, "first_useful_active_seconds": qualified, "mandatory_completion_active_seconds": completion, "mandatory_work_complete": successful, "deadline_attainment": {"first_useful_within_60": successful and qualified is not None and qualified <= 60, "complete_within_120": successful and completion <= 120, "complete_within_300": successful and completion <= 300}, "historical_missing_clock_samples": qualified is None or completion is None, "scope": "Committed application timing; human usefulness and real-provider performance require separate evaluation."}
