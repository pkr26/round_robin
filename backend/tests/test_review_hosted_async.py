import asyncio
import threading
import time

import httpx
from fastapi.responses import RedirectResponse
import pytest

from backend.tests.test_hosted import hosted
from round_robin.security.hosted import OpaqueLoginState


@pytest.mark.parametrize('phase', ['_reserve', '_save'])
def test_login_database_wait_never_stops_an_unrelated_asgi_request(hosted, monkeypatch, phase):
    entered, release = threading.Event(), threading.Event()
    original = getattr(OpaqueLoginState, phase)
    def contended(*args):
        entered.set()
        # Bounded release also makes a regression fail rather than hang pytest.
        assert release.wait(2)
        return original(*args)
    monkeypatch.setattr(OpaqueLoginState, phase, contended)
    class LocalOIDC:
        async def authorize_redirect(self, request, redirect):
            return RedirectResponse('https://identity.example/synthetic')
    hosted.state.oidc = LocalOIDC()
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=hosted), base_url='https://app.example') as client:
            task = asyncio.create_task(client.get('/auth/login'))
            try:
                for _ in range(100):
                    if entered.is_set():
                        break
                    await asyncio.sleep(.005)
                assert entered.is_set()
                started = time.monotonic()
                health = await asyncio.wait_for(client.get('/health'), timeout=.5)
                assert health.status_code == 200 and time.monotonic() - started < .5
                assert not task.done()
            finally:
                release.set()
            assert (await task).status_code == 307
    asyncio.run(run())
