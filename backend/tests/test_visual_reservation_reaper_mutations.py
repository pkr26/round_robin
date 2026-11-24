"""A stale visual hold may remove only its owned unreferenced temporary files."""

from datetime import timedelta, timezone
from uuid import uuid4

import pytest
from round_robin.database.models import Command, utcnow
from round_robin.database.visual_models import DesignOperation, DesignVersion
from round_robin.visual import service
from round_robin.visual.service import reap_asset_reservations

from backend.tests.test_image_worker import seed_image

pytest_plugins = ("backend.tests.test_image_provider", "backend.tests.test_image_worker")


def add_hold(session, design_id, identity, now, *, age, kind="asset_reservation", status="reserved", external=None):
    payload = {"kind": kind, "status": status, "maximum_bytes": 1024}
    if external is not None:
        payload["external_operation_id"] = external
    session.add(DesignOperation(id=identity, session_id=design_id, provider="local", payload=payload,
                                created_at=now - timedelta(seconds=age)))


@pytest.mark.parametrize("case", ["fresh", "released", "wrong_kind", "external"])
def test_reaper_never_cleans_assets_for_ineligible_holds(image_factory, image_configuration, tmp_path, case):
    design_id, _ = seed_image(image_factory, image_configuration)
    namespace = tmp_path / "visual" / "owner_image" / design_id
    namespace.mkdir(parents=True)
    staged = namespace / "synthetic-staged"
    staged.write_bytes(b"synthetic pending visual")
    now = utcnow()
    args = {"fresh": {"age": 599}, "released": {"age": 900, "status": "released"},
            "wrong_kind": {"age": 900, "kind": "different_local_operation"},
            "external": {"age": 900, "external": "external_request"}}[case]
    with image_factory.begin() as session:
        add_hold(session, design_id, "visual_hold_one", now, **args)
    with image_factory.begin() as session:
        assert reap_asset_reservations(session, tmp_path, now=now) == 0
    assert staged.read_bytes() == b"synthetic pending visual"


@pytest.mark.parametrize("age", [600, 601])
def test_reaper_expires_old_hold_without_removing_referenced_visual_assets(image_factory, image_configuration, tmp_path, age):
    design_id, _ = seed_image(image_factory, image_configuration)
    namespace = tmp_path / "visual" / "owner_image" / design_id
    namespace.mkdir(parents=True)
    retained = [namespace / name for name in ("canonical.png", "native.svg", "native.json")]
    for path in retained:
        path.write_bytes(b"synthetic saved asset")
    orphan = namespace / "synthetic-orphan"
    orphan.write_bytes(b"synthetic pending asset")
    now = utcnow()
    with image_factory.begin() as session:
        session.add(DesignVersion(id="version_one", session_id=design_id, version=1, canonical_hash="a" * 64,
                                  original_hash="a" * 64, asset_path=str(retained[0]), width=64, height=64,
                                  source_kind="synthetic", provenance={"svg_path": str(retained[1]), "source_path": str(retained[2])}))
        add_hold(session, design_id, "visual_hold_one", now, age=age)
    with image_factory.begin() as session:
        assert reap_asset_reservations(session, tmp_path, now=now) == 1
        assert session.get(DesignOperation, "visual_hold_one").payload["status"] == "released"
    assert all(path.read_bytes() == b"synthetic saved asset" for path in retained)
    assert not orphan.exists()


def test_reaper_continues_after_ineligible_hold_and_reconciles_original_command(image_factory, image_configuration, tmp_path):
    design_id, _ = seed_image(image_factory, image_configuration)
    now = utcnow()
    command_id = str(uuid4())
    stale_id = "visual_upload_" + command_id.replace("-", "")
    with image_factory.begin() as session:
        add_hold(session, design_id, "visual_hold_fresh", now, age=1)
        add_hold(session, design_id, stale_id, now, age=601)
        session.add(Command(owner_id="owner_image", command_id=command_id, command_type="upload_design",
                            body_hash="a" * 64, result={"upload_status": "pending"}))
    with image_factory.begin() as session:
        assert reap_asset_reservations(session, tmp_path, now=now) == 1
        assert session.get(DesignOperation, "visual_hold_fresh").payload["status"] == "reserved"
        assert session.get(DesignOperation, stale_id).payload["status"] == "released"
        result = session.get(Command, ("owner_image", command_id)).result
        assert result["upload_status"] == result["render_status"] == "expired"
    with image_factory.begin() as session:
        assert reap_asset_reservations(session, tmp_path, now=now) == 0


def test_reaper_locks_the_reservation_owner_policy(image_factory, image_configuration, tmp_path, monkeypatch):
    design_id, _ = seed_image(image_factory, image_configuration)
    now = utcnow()
    with image_factory.begin() as session:
        add_hold(session, design_id, "visual_hold_old", now, age=601)
    original_lock = service.lock_policies
    owners = []

    def capture_lock(session, owner_id):
        owners.append(owner_id)
        return original_lock(session, owner_id)

    monkeypatch.setattr(service, "lock_policies", capture_lock)
    with image_factory.begin() as session:
        assert reap_asset_reservations(session, tmp_path, now=now) == 1
    assert owners == ["owner_image"]


def test_reaper_does_not_expire_fresh_aware_non_utc_hold(image_factory, image_configuration, tmp_path, monkeypatch):
    design_id, _ = seed_image(image_factory, image_configuration)
    now = utcnow()
    with image_factory.begin() as session:
        add_hold(session, design_id, "visual_hold_fresh", now, age=599)
    namespace = tmp_path / "visual" / "owner_image" / design_id
    namespace.mkdir(parents=True)
    staged = namespace / "synthetic-staged"
    staged.write_bytes(b"synthetic pending visual")
    with image_factory.begin() as session:
        original_scalars = session.scalars

        def with_aware_creation_time(*args, **kwargs):
            rows = list(original_scalars(*args, **kwargs))
            for row in rows:
                if isinstance(row, DesignOperation) and row.id == "visual_hold_fresh":
                    row.created_at = (now - timedelta(seconds=599)).astimezone(timezone(-timedelta(hours=5)))
            return iter(rows)

        # SQLite drops timezone offsets, so inject a valid aware timestamp at
        # the ORM boundary without connecting to a database service.
        monkeypatch.setattr(session, "scalars", with_aware_creation_time)
        assert reap_asset_reservations(session, tmp_path, now=now) == 0
    assert staged.read_bytes() == b"synthetic pending visual"


@pytest.mark.parametrize("first_case", ["missing_design", "settled_after_scan"])
def test_reaper_continues_after_invalid_or_concurrently_settled_candidate(
    image_factory, image_configuration, tmp_path, monkeypatch, first_case
):
    design_id, _ = seed_image(image_factory, image_configuration)
    now = utcnow()
    first_id, second_id = "visual_hold_first", "visual_hold_second"
    with image_factory.begin() as session:
        add_hold(session, "missing_design" if first_case == "missing_design" else design_id, first_id, now, age=601)
        add_hold(session, design_id, second_id, now, age=601)
    with image_factory.begin() as session:
        if first_case == "settled_after_scan":
            original_get = session.get

            def settled_on_lock(model, identity, *args, **kwargs):
                row = original_get(model, identity, *args, **kwargs)
                if model is DesignOperation and identity == first_id and row is not None:
                    row.payload = {**row.payload, "status": "released"}
                return row

            monkeypatch.setattr(session, "get", settled_on_lock)
        assert reap_asset_reservations(session, tmp_path, now=now) == 1
        assert session.get(DesignOperation, second_id).payload["status"] == "released"
