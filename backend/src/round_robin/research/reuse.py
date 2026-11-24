"""Persist scoped claims and copy only explicitly selected immutable parent items."""
from copy import deepcopy
from typing import Literal

from pydantic import AwareDatetime, Field
from sqlalchemy import func, select

from round_robin.database.claim_models import ClaimVersion, ReuseRecord
from round_robin.database.models import EvidenceRecord, Meeting, Run, utcnow
from round_robin.database.services import canonical_hash, new_id
from round_robin.reports.read_adapters import read_stored_evidence
from round_robin.schemas.common import Contract, Id, ShortText
from round_robin.schemas.contributions import ClaimSupportBasis
from .claims import ClaimRejected, assign_claim_ids, grounded_claim_aliases


class StoredClaim(Contract):
    id: Id
    version: int = Field(ge=1, strict=True)
    predecessor_id: Id | None = None
    proposition: ShortText
    kind: Literal["fact", "calculation", "assumption", "preference", "recommendation"]
    evidence_status: Literal["supported", "contradicted", "mixed", "not_checked", "fictional"]
    reference_ids: list[Id] = Field(default_factory=list, max_length=40)
    support_bases: list[ClaimSupportBasis] = Field(default_factory=list, max_length=8)
    refuting_reference_ids: list[Id] = Field(default_factory=list, max_length=40)
    support_scope: ShortText | None = None
    time_sensitive: bool
    verified_at: AwareDatetime


def read_claim(row, *, run) -> dict:
    try:
        # Hash exactly the historical shape; additive defaults must not rewrite
        # immutable versions created under an earlier contract.
        content = StoredClaim.model_validate(row.content).model_dump(mode="json", exclude_unset=True)
        if (row.run_id, row.meeting_id, row.owner_id) != (run.id, run.meeting_id, run.owner_id):
            raise ValueError
        if (content["id"], content["version"]) != (row.id, row.version) or canonical_hash(content) != row.sha256:
            raise ValueError
        references = content.get("reference_ids", [])
        if content["id"] in references or content["kind"] in {"fact", "calculation"} and content["evidence_status"] == "supported" and not (references or content.get("support_bases")):
            raise ValueError
        return {**content, "sha256": row.sha256}
    except (ValueError, TypeError, AttributeError):
        raise ClaimRejected("Stored claim failed immutable identity, hash or owner scope") from None


def claim_packet(session, run, *, imported_only=False) -> list[dict]:
    query = select(ClaimVersion).where(ClaimVersion.run_id == run.id).order_by(ClaimVersion.created_at, ClaimVersion.id).execution_options(populate_existing=True)
    if imported_only:
        query = query.where(ClaimVersion.step_id.is_(None))
    try:
        records = list(session.scalars(query.limit(201)))
    except (ValueError, RecursionError):
        raise ClaimRejected("Stored claim failed immutable identity, hash or owner scope") from None
    if len(records) > 200:
        raise ClaimRejected("Claim packet exceeds its bounded capacity")
    contents = [read_claim(row, run=run) for row in records]
    if contents:
        references = _claim_references(contents)
        roots, statuses = _support_reference_data(session, run, references)
        rooted = grounded_claim_aliases([{**content, "alias": content["id"]} for content in contents], support_reference_ids=roots, prior_claims=contents, reference_statuses=statuses)
        if any(content["kind"] in {"fact", "calculation"} and content["evidence_status"] == "supported" and content["id"] not in rooted for content in contents):
            raise ClaimRejected("Saved claim support has no scoped source or available calculation")
    return contents


def _support_reference_ids(session, run, allowed_reference_ids=None):
    return _support_reference_data(session, run, allowed_reference_ids)[0]


def _support_reference_data(session, run, allowed_reference_ids=None):
    from round_robin.orchestration.reporting import comparison_as_of, comparison_data
    from round_robin.schemas.briefs import DraftBrief
    statuses = _reference_statuses(session, run, allowed_reference_ids)
    _, calculations = comparison_data(DraftBrief.model_validate(run.configuration.get("resolved_brief", run.input_snapshot)), as_of=comparison_as_of(run))
    roots = set(statuses) | {item["id"] for item in calculations if item.get("output") is not None}
    return (roots if allowed_reference_ids is None else roots & allowed_reference_ids), statuses


def _claim_references(claims):
    return {identity for claim in claims for identity in [*claim.get("reference_ids", []), *claim.get("refuting_reference_ids", []),
        *(reference for basis in claim.get("support_bases", []) for reference in basis["reference_ids"])]}


def _reference_statuses(session, run, referenced_ids=None):
    from round_robin.reports.read_adapters import UnreadableReport
    try:
        identities = {identity for identity in session.scalars(select(EvidenceRecord.id).where(EvidenceRecord.run_id == run.id))
            if referenced_ids is None or identity in referenced_ids}
        identities = sorted(identities)
        statuses = {}
        for offset in range(0, len(identities), 500):
            query = select(EvidenceRecord).where(EvidenceRecord.run_id == run.id, EvidenceRecord.id.in_(identities[offset:offset + 500])).execution_options(populate_existing=True)
            for record in session.scalars(query):
                statuses[record.id] = read_stored_evidence(record, run_id=run.id, owner_id=run.owner_id, meeting_id=run.meeting_id)["verification_status"]
        return statuses
    except (UnreadableReport, ValueError, RecursionError):
        raise ClaimRejected("Claim evidence failed immutable identity, hash or owner scope") from None


def persist_claims(session, run, step, output: dict, allowed_reference_ids: set[str]) -> dict:
    """Caller holds current run/step authority; insert with its output transaction."""
    if step.run_id != run.id:
        raise ClaimRejected("Claim step is outside the current run")
    claims = output.get("claims", [])
    if not claims:
        return deepcopy(output)
    count = session.scalar(select(func.count()).select_from(ClaimVersion).where(ClaimVersion.run_id == run.id))
    if count + len(claims) > 200:
        raise ClaimRejected("Claim packet exceeds its bounded capacity")
    existing = session.scalar(select(ClaimVersion.id).where(ClaimVersion.step_id == step.id).limit(1))
    if existing is not None:
        raise ClaimRejected("A committed step cannot overwrite its immutable claim versions")
    result, _ = assign_claim_ids(output, allowed_reference_ids=allowed_reference_ids)
    # The packet's references may include many saved evidence IDs; only
    # this run's bounded claim identities need SQL bind parameters.
    claim_ids = list(session.scalars(select(ClaimVersion.id).where(ClaimVersion.run_id == run.id).limit(201)))
    if len(claim_ids) > 200:
        raise ClaimRejected("Claim packet exceeds its bounded capacity")
    selected_ids = [identity for identity in claim_ids if identity in allowed_reference_ids]
    try:
        prior_query = select(ClaimVersion).where(ClaimVersion.run_id == run.id, ClaimVersion.id.in_(selected_ids)).execution_options(populate_existing=True)
        prior_claims = [read_claim(row, run=run) for row in session.scalars(prior_query)]
    except (ValueError, RecursionError):
        raise ClaimRejected("Stored claim failed immutable identity, hash or owner scope") from None
    references = _claim_references([*claims, *prior_claims])
    roots, statuses = _support_reference_data(session, run, allowed_reference_ids & references)
    grounded = grounded_claim_aliases(claims, support_reference_ids=roots, prior_claims=prior_claims, reference_statuses=statuses)
    now = utcnow().isoformat()
    pending = []
    for claim in result["claims"]:
        alias = claim.pop("alias")
        predecessor_id = claim.get("predecessor_id")
        try:
            predecessor = session.get(ClaimVersion, predecessor_id, populate_existing=True) if predecessor_id else None
        except (ValueError, RecursionError):
            raise ClaimRejected("Stored claim failed immutable identity, hash or owner scope") from None
        if predecessor_id:
            if predecessor_id not in allowed_reference_ids:
                raise ClaimRejected("Claim predecessor was not supplied in this step's scope")
            read_claim(predecessor, run=run)
        version = predecessor.version + 1 if predecessor else 1
        if run.configuration.get("mode", "simulated") == "simulated":
            claim["evidence_status"] = "fictional"
        elif claim.get("kind") in {"fact", "calculation"} and claim.get("evidence_status") == "supported" and alias not in grounded:
            claim["evidence_status"] = "mixed" if claim.get("refuting_reference_ids") else "not_checked"
        if claim.get("evidence_status") == "supported" and not claim.get("support_scope"):
            claim["support_scope"] = "Attributed support within supplied evidence; not independent factual certification."
        content = StoredClaim.model_validate({**claim, "version": version, "time_sensitive": True, "verified_at": now}).model_dump(mode="json")
        digest = canonical_hash(content)
        pending.append(ClaimVersion(id=content["id"], version=version, run_id=run.id, meeting_id=run.meeting_id,
            owner_id=run.owner_id, step_id=step.id, alias=alias, sha256=digest, content=content))
        claim.clear()
        claim.update({**content, "sha256": digest})
    # Validation failure may be caught by the worker to settle a failed result.
    # Never leave an earlier claim pending for that transaction to commit.
    session.add_all(pending)
    session.flush()
    return result


def selected_parent_manifest(session, draft, owner: str, selections: list[dict]) -> list[dict]:
    """Preflight and Start independently revalidate exact selected parent content."""
    if not selections:
        return []
    if len(selections) > 24 or len({item["id"] for item in selections}) != len(selections):
        raise ClaimRejected("Parent selection must contain unique bounded identities")
    parent = session.get(Run, draft.parent_run_id) if draft.parent_run_id else None
    meeting = session.get(Meeting, draft.meeting_id)
    if not meeting or meeting.deleted_at or meeting.owner_id != owner or not parent or (parent.meeting_id, parent.owner_id) != (draft.meeting_id, owner):
        raise ClaimRejected("Selected items require an accessible exact parent in the same meeting")
    if any(selected["kind"] == "claim" for selected in selections):
        claim_packet(session, parent)
    manifest = []
    for selected in selections:
        if selected["kind"] == "evidence":
            row = session.get(EvidenceRecord, selected["id"])
            content = read_stored_evidence(row, run_id=parent.id, owner_id=owner, meeting_id=parent.meeting_id)
            version = 1
        elif selected["kind"] == "claim":
            row = session.get(ClaimVersion, selected["id"])
            content = read_claim(row, run=parent)
            version = row.version
        else:
            raise ClaimRejected("Unsupported parent item kind")
        if selected["version"] != version or selected["sha256"] != row.sha256:
            raise ClaimRejected("Selected parent version or content hash changed")
        manifest.append({**selected, "parent_run_id": parent.id, "content_sha256": canonical_hash(content),
            "time_sensitive": content["time_sensitive"], "verified_at": content["verified_at"],
            "verification_status": content.get("verification_status", content.get("evidence_status"))})
    return manifest


def _validated_import_parent(session, run, item):
    """Rebind the selected immutable parent immediately before copying it."""
    try:
        parent_run = session.get(Run, item["parent_run_id"], populate_existing=True)
        if (parent_run is None or parent_run.id != run.parent_run_id
                or (parent_run.meeting_id, parent_run.owner_id) != (run.meeting_id, run.owner_id)):
            raise ValueError
        if item["kind"] == "evidence":
            row = session.get(EvidenceRecord, item["id"], populate_existing=True)
            content = read_stored_evidence(row, run_id=parent_run.id, owner_id=run.owner_id, meeting_id=run.meeting_id)
            version = 1
        elif item["kind"] == "claim":
            row = session.get(ClaimVersion, item["id"], populate_existing=True)
            content = read_claim(row, run=parent_run)
            version = row.version
        else:
            raise ValueError
        if type(item["version"]) is not int or item["version"] != version or item["sha256"] != row.sha256 or item["content_sha256"] != canonical_hash(content):
            raise ValueError
        return row, content
    except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
        raise ClaimRejected("Selected parent item changed before import") from None


def import_parent_items(session, run, manifest: list[dict]):
    """Fresh child identities, exact bytes, explicit provenance and stale support."""
    if (not isinstance(manifest, list) or len(manifest) > 24
            or any(not isinstance(item, dict) or not isinstance(item.get("id"), str)
                   or not item["id"] or not isinstance(item.get("kind"), str)
                   or item["kind"] not in {"evidence", "claim"} for item in manifest)
            or len({item["id"] for item in manifest}) != len(manifest)):
        raise ClaimRejected("Selected parent manifest is unavailable")
    identities = {item["id"]: new_id("evidence" if item["kind"] == "evidence" else "claim") for item in manifest}
    for item in manifest:
        identity = identities[item["id"]]
        parent, validated_content = _validated_import_parent(session, run, item)
        if item["kind"] == "evidence":
            content = validated_content
            content.update(id=identity, run_id=run.id, owner_id=run.owner_id)
            content.pop("original_alias", None)
            content.pop("input_id", None)
            if content["time_sensitive"]:
                content["verification_status"] = "not_checked"
            session.add(EvidenceRecord(id=identity, run_id=run.id, meeting_id=run.meeting_id,
                owner_id=run.owner_id, sha256=content["sha256"], content=content))
        else:
            content = deepcopy(validated_content)
            content.pop("sha256", None)
            # Unselected parent support is not an implicit permission to read it.
            refs = content.get("reference_ids", [])
            original_bases = content.get("support_bases", [])
            all_refs = [*refs, *content.get("refuting_reference_ids", []),
                *(reference for basis in content.get("support_bases", []) for reference in basis["reference_ids"])]
            content.update(id=identity, version=1, predecessor_id=identities.get(content.get("predecessor_id")), reference_ids=[identities[ref] for ref in refs if ref in identities])
            # Losing any required premise removes that entire sufficient basis.
            if "support_bases" in content:
                content["support_bases"] = [{"reference_ids": [identities[ref] for ref in basis["reference_ids"]]} for basis in content["support_bases"] if all(ref in identities for ref in basis["reference_ids"])]
            if "refuting_reference_ids" in content:
                content["refuting_reference_ids"] = [identities[ref] for ref in content["refuting_reference_ids"] if ref in identities]
            # An omitted sufficient basis need not invalidate another complete
            # basis. Keep missing generic/counter references conservative, and
            # never fall back to generic refs when every explicit basis was lost.
            selected_basis_sufficient = bool(original_bases and content.get("support_bases"))
            missing_other_refs = any(ref not in identities for ref in [*refs, *parent.content.get("refuting_reference_ids", [])])
            omitted_only_alternatives = (content["evidence_status"] == "supported" and selected_basis_sufficient and not missing_other_refs)
            if content["evidence_status"] != "fictional" and (content["time_sensitive"] or any(ref not in identities for ref in all_refs) and not omitted_only_alternatives):
                content["evidence_status"] = "not_checked"
            session.add(ClaimVersion(id=identity, version=1, run_id=run.id, meeting_id=run.meeting_id,
                owner_id=run.owner_id, step_id=None, alias=None, sha256=canonical_hash(content), content=content))
        session.add(ReuseRecord(id=new_id("reuse"), run_id=run.id, meeting_id=run.meeting_id, owner_id=run.owner_id,
            parent_run_id=item["parent_run_id"], parent_id=item["id"], parent_version=item["version"],
            parent_sha256=item["sha256"], child_id=identity, kind=item["kind"]))
    session.flush()
    # Re-evaluate the copied graph after every new evidence and claim has been
    # written: evidence freshness can invalidate claims that are individually
    # non-temporal, including claims supported through other imported claims.
    imported = list(session.scalars(select(ClaimVersion).where(ClaimVersion.run_id == run.id, ClaimVersion.id.in_(identities.values()))))
    contents = [read_claim(row, run=run) for row in imported]
    if contents:
        references = _claim_references(contents)
        roots, statuses = _support_reference_data(session, run, references)
        grounded = grounded_claim_aliases([{**item, "alias": item["id"]} for item in contents],
            support_reference_ids=roots, prior_claims=contents, reference_statuses=statuses)
        for row, content in zip(imported, contents, strict=True):
            if content["evidence_status"] != "supported":
                continue
            has_references = bool(content["reference_ids"] or content.get("support_bases") or content.get("refuting_reference_ids"))
            if content["kind"] in {"fact", "calculation"}:
                support_available = content["id"] in grounded
            elif has_references:
                # Evaluate this claim's cited basis without treating any other
                # non-factual claim as a source. Its stored kind is unchanged.
                probe = {**content, "alias": content["id"], "kind": "fact"}
                support_available = content["id"] in grounded_claim_aliases([probe], support_reference_ids=roots, prior_claims=contents, reference_statuses=statuses)
            else:
                support_available = True
            if not support_available:
                content.pop("sha256")
                content["evidence_status"] = "not_checked"
                row.content, row.sha256 = content, canonical_hash(content)
    session.flush()
    return [{**item, "child_id": identities[item["id"]]} for item in manifest]
