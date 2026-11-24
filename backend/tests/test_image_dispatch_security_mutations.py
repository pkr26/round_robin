"""The image outbox must respect recovery quarantine and durable dispatch intent."""

from datetime import timedelta

import pytest
from round_robin.database.models import BudgetOperation, utcnow
from round_robin.database.visual_models import DesignOperation, DesignSession
from round_robin.worker import image_tasks
from sqlalchemy import select

from backend.tests.test_image_worker import seed_image

pytest_plugins = ("backend.tests.test_image_provider", "backend.tests.test_image_worker")


@pytest.mark.parametrize("custom_queue", [False, True])
def test_default_image_outbox_publishes_only_stable_identity_to_configured_queue(
    image_factory, image_configuration, monkeypatch, custom_queue
):
    _, operation_id = seed_image(image_factory, image_configuration)
    if custom_queue:
        monkeypatch.setenv("ROUND_ROBIN_IMAGE_TASK_QUEUE", "synthetic_custom_queue")
    else:
        monkeypatch.delenv("ROUND_ROBIN_IMAGE_TASK_QUEUE", raising=False)
    published = []
    monkeypatch.setattr(image_tasks.image_app, "send_task", lambda *args, **kwargs: published.append((args, kwargs)))

    assert image_tasks.dispatch_pending_image_operations(session_factory=image_factory) == 1
    assert published == [(("round_robin.visual.execute",), {
        "args": [operation_id], "queue": "synthetic_custom_queue" if custom_queue else "round_robin_images", "retry": False,
    })]
    with image_factory() as session:
        saved = session.get(DesignOperation, operation_id).payload
        assert saved["status"] == "queued" and saved["dispatch_status"] == "published" and saved["published_at"]
        assert session.scalar(select(BudgetOperation)) is None


def test_image_outbox_uses_session_durability_quarantine_before_publication(
    image_factory, image_configuration, monkeypatch, tmp_path
):
    _, operation_id = seed_image(image_factory, image_configuration)
    default_control = tmp_path / "default-control"
    session_control = tmp_path / "session-control"
    session_control.mkdir()
    # A malformed marker also fails closed; no production control directory is used.
    marker = session_control / "failure_test.json"
    marker.write_text('{"unreadable": true}')
    monkeypatch.setenv("ROUND_ROBIN_DURABILITY_CONTROL_DIRECTORY", str(default_control))
    image_factory.configure(info={"durability_control_directory": session_control})
    sent = []
    assert image_tasks.dispatch_pending_image_operations(session_factory=image_factory, sender=sent.append) == 0
    assert sent == []
    with image_factory() as session:
        assert session.get(DesignOperation, operation_id).payload["status"] == "queued"
        assert session.scalar(select(BudgetOperation)) is None

    marker.unlink()
    assert image_tasks.dispatch_pending_image_operations(session_factory=image_factory, sender=sent.append) == 1
    assert sent == [operation_id]


@pytest.mark.parametrize("first_state", ["future_due", "recently_published", "done"])
def test_ineligible_outbox_entry_does_not_prevent_later_eligible_dispatch(image_factory, image_configuration, first_state):
    _, first_id = seed_image(image_factory, image_configuration)
    with image_factory.begin() as session:
        first = session.get(DesignOperation, first_id)
        queued = dict(first.payload)
        if first_state == "future_due":
            first.payload = {**queued, "due_at": (utcnow() + timedelta(minutes=10)).isoformat()}
        elif first_state == "recently_published":
            first.payload = {**queued, "dispatch_status": "published", "published_at": utcnow().isoformat()}
        else:
            first.payload = {**queued, "dispatch_status": "done"}
        second_id = "image_operation_eligible"
        session.add(DesignOperation(id=second_id, session_id=first.session_id, provider=first.provider, payload=queued))
    sent = []
    assert image_tasks.dispatch_pending_image_operations(session_factory=image_factory, sender=sent.append) == 1
    assert sent == [second_id]


@pytest.mark.parametrize("state", ["legacy_no_dispatch_status", "old_published", "past_due"])
def test_image_outbox_redelivers_old_or_legacy_durable_intent(image_factory, image_configuration, state):
    _, operation_id = seed_image(image_factory, image_configuration)
    with image_factory.begin() as session:
        operation = session.get(DesignOperation, operation_id)
        payload = dict(operation.payload)
        if state == "legacy_no_dispatch_status":
            payload.pop("dispatch_status")
        elif state == "old_published":
            payload.update(dispatch_status="published", published_at=(utcnow() - timedelta(minutes=1)).isoformat())
        else:
            payload.update(due_at=(utcnow() - timedelta(minutes=1)).isoformat())
        operation.payload = payload
    sent = []
    assert image_tasks.dispatch_pending_image_operations(session_factory=image_factory, sender=sent.append) == 1
    assert sent == [operation_id]


@pytest.mark.parametrize("first_outcome", ["both_sent", "removed_before_lock", "send_error"])
def test_image_outbox_continues_after_stale_or_failed_item(image_factory, image_configuration, monkeypatch, first_outcome):
    _, first_id = seed_image(image_factory, image_configuration)
    with image_factory.begin() as session:
        first = session.get(DesignOperation, first_id)
        second_id = "image_operation_next"
        session.add(DesignOperation(id=second_id, session_id=first.session_id, provider=first.provider, payload=dict(first.payload)))
    if first_outcome == "removed_before_lock":
        original_lock = image_tasks._locked_design

        def lock_unless_removed(session, identity):
            return (None, None, None) if identity == first_id else original_lock(session, identity)

        monkeypatch.setattr(image_tasks, "_locked_design", lock_unless_removed)
    sent = []

    def sender(identity):
        if identity == first_id and first_outcome == "send_error":
            raise RuntimeError("synthetic broker failure")
        sent.append(identity)

    expected = [first_id, second_id] if first_outcome == "both_sent" else [second_id]
    assert image_tasks.dispatch_pending_image_operations(session_factory=image_factory, sender=sender) == len(expected)
    assert sent == expected


def test_image_outbox_redelivers_after_fifteen_seconds(image_factory, image_configuration, monkeypatch):
    _, operation_id = seed_image(image_factory, image_configuration)
    now = utcnow()
    with image_factory.begin() as session:
        operation = session.get(DesignOperation, operation_id)
        operation.payload = {**operation.payload, "dispatch_status": "published", "published_at": (now - timedelta(seconds=15.5)).isoformat()}
    monkeypatch.setattr(image_tasks, "utcnow", lambda: now)
    sent = []
    assert image_tasks.dispatch_pending_image_operations(session_factory=image_factory, sender=sent.append) == 1
    assert sent == [operation_id]


@pytest.mark.parametrize("boundary", ["due_time", "redelivery_time"])
def test_image_outbox_is_eligible_at_the_due_or_redelivery_time(image_factory, image_configuration, monkeypatch, boundary):
    _, operation_id = seed_image(image_factory, image_configuration)
    now = utcnow()
    with image_factory.begin() as session:
        operation = session.get(DesignOperation, operation_id)
        if boundary == "due_time":
            operation.payload = {**operation.payload, "due_at": now.isoformat()}
        else:
            operation.payload = {**operation.payload, "dispatch_status": "published", "published_at": (now - timedelta(seconds=15)).isoformat()}
    monkeypatch.setattr(image_tasks, "utcnow", lambda: now)
    sent = []
    assert image_tasks.dispatch_pending_image_operations(session_factory=image_factory, sender=sent.append) == 1
    assert sent == [operation_id]


def test_image_outbox_rechecks_status_after_lock(image_factory, image_configuration, monkeypatch):
    seed_image(image_factory, image_configuration)
    original_lock = image_tasks._locked_design

    def settled_after_scan(session, identity):
        design, operation, scope = original_lock(session, identity)
        operation.payload = {**operation.payload, "status": "unknown"}
        return design, operation, scope

    monkeypatch.setattr(image_tasks, "_locked_design", settled_after_scan)
    sent = []
    assert image_tasks.dispatch_pending_image_operations(session_factory=image_factory, sender=sent.append) == 0
    assert sent == []


def test_expired_image_session_does_not_block_a_different_session(image_factory, image_configuration):
    design_id, operation_id = seed_image(image_factory, image_configuration)
    with image_factory.begin() as session:
        design = session.get(DesignSession, design_id)
        first = session.get(DesignOperation, operation_id)
        values = {column.name: getattr(design, column.name) for column in DesignSession.__table__.columns}
        values["id"] = "design_other"
        session.add(DesignSession(**values))
        session.flush()
        second_id = "image_operation_other"
        session.add(DesignOperation(id=second_id, session_id="design_other", provider=first.provider,
                                    payload={**first.payload, "session_id": "design_other"}))
        first.payload = {**first.payload, "initial_admission_expires_at": (utcnow() - timedelta(minutes=1)).isoformat()}
    sent = []
    assert image_tasks.dispatch_pending_image_operations(session_factory=image_factory, sender=sent.append) == 1
    assert sent == [second_id]
    with image_factory() as session:
        assert session.get(DesignSession, design_id).status == "expired"
        assert session.get(DesignOperation, operation_id).payload["status"] == "blocked"
