"""Validate ownership and basic saved shape of a visual finish pool."""

from round_robin.orchestration.budget import BudgetDenied, amount


def valid_visual_finish_pool(pool, design):
    if (pool.run_id != design.id or pool.owner_id != design.owner_id
        or pool.kind != "visual_finish_hold" or pool.status not in {"held", "released"}
        or not isinstance(pool.metadata_json, dict)):
        return False
    try:
        reserved = amount(pool.reserved_usd)
        settled = amount(pool.settled_usd) if pool.settled_usd is not None else 0
        booked = amount(pool.metadata_json.get("conservative_booking_usd", "0"))
    except BudgetDenied:
        return False
    if settled != 0 or booked != 0 or (pool.status == "released" and reserved != 0):
        return False
    metadata = pool.metadata_json
    if set(metadata) - {"target_tag", "roles_remaining", "pool_version", "release_reason", "conservative_booking_usd"}:
        return False
    if "release_reason" in metadata and not isinstance(metadata["release_reason"], str):
        return False
    tag = metadata.get("target_tag")
    roles = metadata.get("roles_remaining", [])
    version = metadata.get("pool_version", 0)
    if tag is not None and (not isinstance(tag, str) or not tag):
        return False
    if not isinstance(roles, list) or any(not isinstance(role, str) or role not in {"openai", "anthropic", "google"} for role in roles):
        return False
    if len(set(roles)) != len(roles) or (tag is not None and "roles_remaining" not in metadata) or (tag is None and roles):
        return False
    if type(version) is not int or version < 0:
        return False
    return True
