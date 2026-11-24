"""An uninvited identity receives a usable denial from the hosted callback."""
from fastapi.testclient import TestClient
from sqlalchemy import select

from round_robin.database.models import PolicyBucket
from round_robin.security.hosted import SESSION_COOKIE

pytest_plugins = ("test_hosted",)


def test_uninvited_callback_explains_denial_without_creating_session(hosted):
    class VerifiedUninvitedIdentity:
        async def authorize_access_token(self, request):
            return {"id_token": "synthetic", "userinfo": {"iss": "https://identity.example", "sub": "uninvited"}}

    hosted.state.oidc = VerifiedUninvitedIdentity()
    with TestClient(hosted, base_url="https://app.example", follow_redirects=False) as client:
        response = client.get("/auth/callback?state=synthetic&code=synthetic")
    assert response.status_code == 403
    error = response.json()["error"]
    assert error["code"] == "invitation_required"
    assert isinstance(error["message"], str) and error["message"].strip()
    assert not any(value.startswith(SESSION_COOKIE + "=") for value in response.headers.get_list("set-cookie"))
    with hosted.state.session_factory() as session:
        assert not session.scalars(select(PolicyBucket).where(PolicyBucket.key.startswith("session:"))).all()
