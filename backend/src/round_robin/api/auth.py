from fastapi import HTTPException, Request


def require_owner(request: Request) -> str:
    from sqlalchemy import select
    from round_robin.database.models import PolicyBucket
    from round_robin.operations.durability import marker_pending
    settings = request.app.state.settings
    safe_cancel = request.method == "POST" and request.url.path.endswith("/cancel") and request.url.path.startswith(("/api/runs/", "/api/design-sessions/"))
    if request.method not in {"GET", "HEAD", "OPTIONS"} and not safe_cancel and marker_pending(settings.artifact_directory, control_directory=settings.durability_control_directory):
        raise HTTPException(503, detail={"code": "durability_compromised", "message": "Independent storage recovery is required before writes resume", "retry_scope": "none"})
    with request.app.state.session_factory() as session:
        policy = session.scalar(select(PolicyBucket).where(PolicyBucket.key == "deployment"))
        if policy and policy.metadata_json.get("restore_quarantine"):
            raise HTTPException(503, detail={"code": "restore_quarantine", "message": "Restoration must be reconciled before account access", "retry_scope": "none"})
        safe_cancel = request.method == "POST" and request.url.path.endswith("/cancel") and request.url.path.startswith(("/api/runs/", "/api/design-sessions/"))
        if policy and policy.metadata_json.get("durability_compromised") and request.method not in {"GET", "HEAD", "OPTIONS"} and not safe_cancel:
            raise HTTPException(503, detail={"code": "durability_compromised", "message": "Storage or backup durability requires operator recovery before writes resume", "retry_scope": "none"})
    identity_provider = getattr(request.app.state, "identity_provider", None)
    if identity_provider is not None:
        return identity_provider(request)
    settings = request.app.state.settings
    if not settings.development:
        raise HTTPException(503, detail={"code": "hosted_access_disabled", "message": "Hosted sessions are not enabled", "retry_scope": "none"})
    host = request.client.host if request.client else ""
    if host not in {"127.0.0.1", "::1", "localhost", "testclient"}:
        raise HTTPException(403, detail={"code": "local_identity_only", "message": "Development identity requires a loopback connection", "retry_scope": "none"})
    return "owner_local"


def get_db(request: Request):
    from round_robin.operations.durability import marker_pending
    settings = request.app.state.settings
    safe_cancel = request.method == "POST" and request.url.path.endswith("/cancel") and request.url.path.startswith(("/api/runs/", "/api/design-sessions/"))
    if request.method not in {"GET", "HEAD", "OPTIONS"} and not safe_cancel and marker_pending(settings.artifact_directory, control_directory=settings.durability_control_directory):
        raise HTTPException(503, detail={"code": "durability_compromised", "message": "Independent storage recovery is required before writes resume", "retry_scope": "none"})
    with request.app.state.session_factory() as session:
        yield session
