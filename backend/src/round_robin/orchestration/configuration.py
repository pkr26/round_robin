"""A frozen live run never silently adopts a replacement model/price/config."""

from datetime import datetime

from pydantic import ValidationError
from round_robin.configuration import settings
from round_robin.database.models import utcnow
from round_robin.database.services import canonical_hash
from round_robin.operations.config_files import read_operator_configuration
from round_robin.schemas.provider_configuration import ProviderConfiguration
from round_robin.schemas.briefs import DraftBrief
from .budget import BudgetDenied, aware


def verify_frozen_briefs(run):
    """Reject corrupt saved inputs before authorizing another provider call."""
    try:
        DraftBrief.model_validate(run.input_snapshot)
        if "resolved_brief" in run.configuration:
            DraftBrief.model_validate(run.configuration["resolved_brief"])
    except ValidationError:
        raise BudgetDenied("invalid_frozen_inputs") from None


def verify_saved_discussion(session, run):
    """Fence work if the persisted objection ledger cannot be trusted."""
    from round_robin.research.claims import ClaimRejected
    from round_robin.research.discussion import verify_discussion_provenance
    try:
        verify_discussion_provenance(session, run)
    except ClaimRejected:
        raise BudgetDenied("discussion_integrity_failed") from None


def verify_frozen_configuration(run, *, operator_settings=None):
    if run.configuration.get("visual_configuration") is True:
        from round_robin.providers.images import ImageFundingLocked, verify_image_configuration
        try:
            verify_image_configuration(run.configuration, operator_settings)
        except ImageFundingLocked:
            raise BudgetDenied("visual_configuration_changed_or_unfunded") from None
        return
    if run.configuration.get("mode", "simulated") == "simulated":
        return
    configured = operator_settings or settings
    if not configured.paid_access_enabled or configured.provider_config_file is None:
        raise BudgetDenied("operator_funding_disabled")
    try:
        expiry = aware(datetime.fromisoformat(run.configuration["expires_at"]))
        if expiry <= utcnow():
            raise BudgetDenied("frozen_configuration_expired")
        current = ProviderConfiguration.model_validate_json(read_operator_configuration(configured.provider_config_file))
        if current.expires_at <= utcnow() or canonical_hash(current.model_dump(mode="json")) != run.configuration["operator_configuration_hash"]:
            raise BudgetDenied("frozen_configuration_changed")
        if configured.deployment_epoch != run.deployment_epoch:
            raise BudgetDenied("execution_epoch_changed")
        from round_robin.providers.http import request_contract_hash
        if run.configuration.get("request_contract_hash") != request_contract_hash():
            raise BudgetDenied("frozen_request_contract_changed")
    except BudgetDenied:
        raise
    except (KeyError, ValueError, TypeError, OSError):
        raise BudgetDenied("frozen_configuration_unavailable") from None
