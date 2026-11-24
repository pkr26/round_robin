"""Persist-before-send job identities and capability-gated remote recovery."""
from dataclasses import dataclass
import hashlib
import json

from round_robin.providers.images import ImageAuthorization, ImageProvider
from .iterations import VisualLimit, VisualState
from .iterations import decimal


@dataclass
class ImageJob:
    id: str
    operation_id: str
    step_id: str
    provider: str
    request_hash: str
    fence: int
    epoch: int
    owner_id: str
    session_id: str
    reserved_usd: str
    status: str = "authorized"
    provider_job_id: str | None = None
    unknown_outcome: bool = False


def _same_authorization(job: ImageJob, authorization: ImageAuthorization) -> bool:
    try:
        return (job.operation_id, job.owner_id, job.session_id, job.step_id, job.fence, job.epoch) == (authorization.operation_id, authorization.owner_id, authorization.session_id, authorization.step_id, authorization.fence, authorization.epoch) and decimal(job.reserved_usd) == decimal(authorization.bound_usd)
    except VisualLimit:
        return False


def request_hash(packet: dict) -> str:
    # Bytes are identity-bound rather than copied into operational diagnostics.
    def convert(value):
        if isinstance(value, bytes):
            return {"sha256": hashlib.sha256(value).hexdigest(), "byte_size": len(value)}
        if isinstance(value, dict):
            return {key: convert(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [convert(item) for item in value]
        return value
    return hashlib.sha256(json.dumps(convert(packet), sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def submit_saved_job(job: ImageJob, packet: dict, provider: ImageProvider, authorization: ImageAuthorization, *, persist) -> dict:
    caps = provider.capabilities
    if not caps.generates_images and not caps.edits_images:
        raise VisualLimit("Configured endpoint does not generate or edit images")
    if job.status != "authorized" or job.unknown_outcome or job.provider_job_id:
        raise VisualLimit("A possibly submitted job cannot be blindly resubmitted")
    if job.provider != caps.provider or not _same_authorization(job, authorization) or request_hash(packet) != job.request_hash:
        raise VisualLimit("Image request, authorization or immutable identity changed")
    # Ambiguity is durable before the send. A crash after this commit is unknown,
    # not evidence that the remote service never accepted the request.
    job.status, job.unknown_outcome = "submitting", True
    persist(job)
    try:
        result = provider.submit(packet, authorization, idempotency_key=job.id if caps.idempotent_submission else None)
    except BaseException:
        job.status = "unknown"
        persist(job)
        raise
    job.provider_job_id = result.get("job_id") if caps.asynchronous_jobs else None
    if caps.asynchronous_jobs and not job.provider_job_id:
        job.status = "unknown"
        persist(job)
        raise VisualLimit("Asynchronous submission returned no recoverable job identity")
    job.status = "refused" if result.get("status") == "refused" else "pending" if caps.asynchronous_jobs else "completed"
    job.unknown_outcome = False
    persist(job)
    return result


def recover_saved_job(job: ImageJob, state: VisualState, provider: ImageProvider, authorization: ImageAuthorization, *, now: float, persist) -> dict:
    caps = provider.capabilities
    if not (caps.asynchronous_jobs and caps.job_lookup and job.provider_job_id):
        raise VisualLimit("No genuine same-job lookup is supported; retain unknown exposure and do not resubmit")
    if not _same_authorization(job, authorization) or job.provider != caps.provider or job.fence != state.fence or job.epoch != state.epoch or state.status != "active":
        raise VisualLimit("Restored or cancelled jobs cannot resume without their original authority")
    state.poll(now=now)
    persist(state)
    # A paid lookup requires its own shared-ledger authorization supplied by the
    # caller. It does not consume another generation submission.
    result = provider.lookup(job.provider_job_id, authorization)
    job.status = str(result.get("status", "unknown"))
    persist(job)
    return result
