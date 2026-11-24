from collections.abc import Mapping

from authlib.integrations.base_client.errors import OAuthError
from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
import httpx
from starlette.concurrency import run_in_threadpool

from round_robin.security.hosted import SESSION_COOKIE, create_session, forbidden, revoke_session
from .auth import require_owner

router = APIRouter()


def enabled(request):
    if request.app.state.settings.development or not request.app.state.settings.hosted_enabled:
        forbidden("hosted_access_disabled", "Hosted sign-in is disabled in this profile", 404)


@router.get("/auth/login")
async def login(request: Request):
    enabled(request)
    redirect = request.app.state.settings.public_origin.rstrip("/") + "/auth/callback"
    try:
        return await request.app.state.oidc.authorize_redirect(request, redirect)
    except (httpx.HTTPError, OAuthError, ValueError, KeyError):
        forbidden("oidc_unavailable", "Sign-in is temporarily unavailable; restart sign-in when the identity provider recovers", 503)


@router.get("/auth/callback")
async def callback(request: Request):
    enabled(request)
    try:
        token = await request.app.state.oidc.authorize_access_token(request)
    except httpx.HTTPError:
        forbidden("oidc_unavailable", "Sign-in is temporarily unavailable; restart sign-in when the identity provider recovers", 503)
    except (OAuthError, ValueError, KeyError):
        forbidden("oidc_callback_invalid", "Sign-in could not be verified; restart sign-in", 400)
    if not isinstance(token, Mapping):
        forbidden("oidc_identity_invalid", "A verified issuer and subject are required", 400)
    user = token.get("userinfo")
    if (not isinstance(token.get("id_token"), str) or not token["id_token"]
            or not isinstance(user, Mapping) or not isinstance(user.get("sub"), str) or not user["sub"]
            or user.get("iss") != request.app.state.settings.oidc_issuer):
        forbidden("oidc_identity_invalid", "A verified issuer and subject are required", 400)
    opaque, _, _ = await run_in_threadpool(create_session, request.app.state.session_factory, request.app.state.settings, user["iss"], user["sub"])
    response = RedirectResponse(request.app.state.settings.public_origin.rstrip("/") + "/live", status_code=303)
    response.set_cookie(SESSION_COOKIE, opaque, max_age=43200, secure=True, httponly=True, samesite="lax", path="/")
    return response


@router.get("/auth/session")
def session_info(request: Request, owner=Depends(require_owner)):
    if request.app.state.settings.development:
        return {"owner_id": owner, "profile": "development", "csrf_token": None}
    _, details = request.app.state.identity_provider.authenticate(request)
    return {"owner_id": owner, "profile": "invited_hosted", "csrf_token": details["csrf_token"], "expires_at": details["expires_at"]}


@router.post("/auth/logout")
def logout(request: Request, owner=Depends(require_owner)):
    enabled(request)
    revoke_session(request.app.state.session_factory, request.cookies.get(SESSION_COOKIE, ""))
    response = RedirectResponse(request.app.state.settings.public_origin, status_code=303)
    response.delete_cookie(SESSION_COOKIE, secure=True, httponly=True, samesite="lax", path="/")
    return response
