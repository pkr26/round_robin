"""Operator CLI. None of these commands invokes a model or sends outreach."""
import argparse
import json
import os
from pathlib import Path

from sqlalchemy import select, text

from .configuration import Settings
from .database.models import BudgetOperation, DeletionRecord, PolicyBucket, Run, utcnow
from .database.services import lock_policies
from .database.session import configure_database
from .operations.recovery import OperationError, backup, purge_pending, reconcile, restore_verify, schema_head


def funding_configuration(settings):
    """Explicit operator action merges configured modalities at the lower caps."""
    if not settings.paid_access_enabled:
        raise OperationError("Explicit paid-access authorization is required")
    configurations, funding = [], []
    if settings.provider_config_file is not None:
        from .api.preflight import run_configuration
        text = run_configuration(settings, "live")
        if not text.get("funded"):
            raise OperationError("Text provider configuration is not explicitly funded")
        configurations.append(text["version"])
        funding.append(text["aggregate_funding"])
    if os.environ.get("ROUND_ROBIN_IMAGE_CONFIG_FILE"):
        from .providers.images import ImageFundingLocked, load_image_configuration
        try:
            visual = load_image_configuration(settings)
        except ImageFundingLocked:
            raise OperationError("Image configuration is missing, expired or invalid") from None
        configurations.append(visual["version"])
        funding.append({key: visual[key] for key in ("deployment_daily_usd", "deployment_monthly_usd", "owner_daily_usd", "owner_monthly_usd")})
    if not funding:
        raise OperationError("A validated explicitly funded text or image configuration is required")
    from decimal import Decimal
    caps = {key: str(min(Decimal(item[key]) for item in funding)) for key in funding[0]}
    return {"versions": configurations, "caps": caps}


def config_check(settings, engine):
    settings.validate()
    if settings.paid_access_enabled:
        funding_configuration(settings)
    with engine.connect() as connection:
        connection.execute(text("SELECT 1"))
        actual = connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
    if actual != schema_head():
        raise OperationError("Database migration revision is incompatible; migrate before starting API and workers")
    return {"configuration": "valid", "schema_revision": actual, "profile": "development" if settings.development else "invited_hosted", "paid_access_enabled": settings.paid_access_enabled, "provider_requests": 0}


def set_policy_flag(factory, *, name: str, value: bool):
    with factory.begin() as session:
        deployment, _ = lock_policies(session, "owner_operator")
        if name == "spending_locked":
            if not value and deployment.metadata_json.get("restore_quarantine"):
                raise OperationError("Restoration quarantine must be independently reconciled before paid authorization")
            deployment.spending_locked = value
        else:
            deployment.metadata_json = {**deployment.metadata_json, name: value}
        deployment.version += 1
        deployment.metadata_json = {**deployment.metadata_json, "operator_action": {"name": name, "value": value, "at": utcnow().isoformat()}}
    return {name: value, "provider_requests": 0}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("config-check")
    spending = commands.add_parser("spend-lock")
    spending.add_argument("--unlock", action="store_true")
    spending.add_argument("--owner", default="owner_local", help="Opaque owner namespace to configure; default personal development owner")
    commands.add_parser("usage-inspect")
    storage = commands.add_parser("storage-check")
    storage.add_argument("--clear", action="store_true", help="Clear only the durability gate after fsynced capacity/integrity checks; spending remains locked")
    reconcile_command = commands.add_parser("usage-reconcile")
    reconcile_command.add_argument("operation_id")
    group = reconcile_command.add_mutually_exclusive_group(required=True)
    group.add_argument("--conservative-usd")
    group.add_argument("--provider-receipt", type=Path)
    purge = commands.add_parser("purge-inspect")
    purge.add_argument("--apply", action="store_true", help="Reapply manifest to active storage without erasing backup history")
    backup_command = commands.add_parser("backup")
    backup_command.add_argument("destination", type=Path)
    restore = commands.add_parser("restore-verify")
    restore.add_argument("source", type=Path)
    restore.add_argument("--target-url", default=os.environ.get("ROUND_ROBIN_RESTORE_TEST_DATABASE_URL"))
    restore.add_argument("--target-artifacts", type=Path, required=True)
    drain = commands.add_parser("drain")
    drain.add_argument("--off", action="store_true")
    args = parser.parse_args(argv)
    settings = Settings()
    engine, factory = configure_database(settings.database_url)
    manifest = settings.artifact_directory.parent / "deletion-manifest.jsonl"
    try:
        if args.command == "config-check":
            result = config_check(settings, engine)
        elif args.command == "spend-lock":
            if args.unlock:
                from .operations.durability import marker_pending
                if marker_pending(settings.artifact_directory, control_directory=settings.durability_control_directory):
                    raise OperationError("Independent storage recovery must complete before funding is enabled")
                configuration = funding_configuration(settings)
                with factory.begin() as session:
                    deployment, owner = lock_policies(session, args.owner)
                    if deployment.metadata_json.get("restore_quarantine") or deployment.metadata_json.get("durability_compromised"):
                        raise OperationError("Restore or storage durability quarantine must be reconciled before funding is enabled")
                    funding = configuration["caps"]
                    for policy, prefix in ((deployment, "deployment"), (owner, "owner")):
                        policy.limit_usd = funding[f"{prefix}_daily_usd"]
                        policy.spending_locked = False
                        policy.version += 1
                        policy.metadata_json = {**policy.metadata_json, "daily_usd": funding[f"{prefix}_daily_usd"], "monthly_usd": funding[f"{prefix}_monthly_usd"], "deployment_epoch": settings.deployment_epoch, "funding_configuration_versions": configuration["versions"], "operator_authorized_at": utcnow().isoformat()}
                result = {"spending_locked": False, "owner_id": args.owner, "funding_configuration_versions": configuration["versions"], "provider_requests": 0}
            else:
                result = set_policy_flag(factory, name="spending_locked", value=True)
        elif args.command == "drain":
            result = set_policy_flag(factory, name="draining", value=not args.off)
        elif args.command == "usage-inspect":
            with factory() as session:
                result = {"items": [{"id": row.id, "run_id": row.run_id, "kind": row.kind, "status": row.status, "reserved_usd": row.reserved_usd, "settled_usd": row.settled_usd, "conservative_booking_usd": row.metadata_json.get("conservative_booking_usd")} for row in session.scalars(select(BudgetOperation).order_by(BudgetOperation.authorized_at))]}
        elif args.command == "usage-reconcile":
            result = reconcile(factory, args.operation_id, conservative_usd=args.conservative_usd, provider_receipt=args.provider_receipt)
        elif args.command == "storage-check":
            from .operations.durability import storage_check
            result = storage_check(factory, settings.artifact_directory, clear=args.clear, control_directory=settings.durability_control_directory)
        elif args.command == "purge-inspect":
            if args.apply:
                result = purge_pending(factory, settings.artifact_directory, manifest)
                from .operations.maintenance import maintenance
                result['retention_cleanup'] = maintenance(factory, settings.artifact_directory)
            else:
                with factory() as session:
                    result = {"items": [{"meeting_id": row.meeting_id, "status": row.status, "execution_fence": row.execution_fence} for row in session.scalars(select(DeletionRecord))], "backup_erasure": "separate_from_active_purge"}
        elif args.command == "backup":
            result = backup(engine, settings.artifact_directory, args.destination, manifest, control_directory=settings.durability_control_directory)
        else:
            if not args.target_url:
                raise OperationError("An explicit empty synthetic restore target is required")
            result = restore_verify(args.source, args.target_url, args.target_artifacts, manifest)
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    except OperationError as exception:
        print(json.dumps({"status": "blocked", "reason": str(exception)}))
        return 1
    except Exception:
        print(json.dumps({"status": "blocked", "reason": "operation_failed; inspect protected local diagnostics"}))
        return 1
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
