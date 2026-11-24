"""Show an anonymous login blocks the ASGI event loop on a real PostgreSQL lock."""
import asyncio
import json
import tempfile
import threading
import time
from pathlib import Path
from uuid import uuid4

import httpx
from fastapi.responses import RedirectResponse
from sqlalchemy import create_engine, text

from backend.tests.test_hosted import hosted_settings
from round_robin.app import create_app
from round_robin.database.models import Base
from round_robin.database.safety import require_disposable_postgres
from round_robin.database.services import lock_policies

DATABASE = "postgresql+psycopg://round_robin:round_robin_local@127.0.0.1:55432/round_robin_test"
url = require_disposable_postgres(DATABASE)
schema = "rr_login_audit_" + uuid4().hex
admin = create_engine(DATABASE)
result = {"provider_calls": 0, "lock_hold_seconds": 1.0}

class LocalOIDC:
    async def authorize_redirect(self, request, redirect):
        return RedirectResponse("https://identity.example/synthetic")

try:
    with admin.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    with tempfile.TemporaryDirectory(prefix="rr-audit2-login-") as directory:
        settings = hosted_settings(Path(directory))
        app = create_app(url.update_query_dict({"options": f"-csearch_path={schema}"}).render_as_string(hide_password=False), settings=settings)
        # This creates tables only in this explicitly disposable, owned schema.
        Base.metadata.create_all(app.state.engine)
        app.state.oidc = LocalOIDC()
        with app.state.session_factory.begin() as session:
            lock_policies(session, "owner_operator")
        acquired = threading.Event()
        errors = []

        def hold_normal_policy_lock():
            try:
                with app.state.session_factory.begin() as session:
                    lock_policies(session, "owner_operator")
                    acquired.set()
                    time.sleep(result["lock_hold_seconds"])
            except BaseException as exc:
                errors.append(type(exc).__name__)
                acquired.set()

        async def run():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://app.example") as client:
                result["baseline_health"] = (await client.get("/health")).status_code
                thread = threading.Thread(target=hold_normal_policy_lock)
                thread.start()
                assert acquired.wait(5) and not errors
                started = time.monotonic()
                login = asyncio.create_task(client.get("/auth/login"))
                # This should be a 20ms heartbeat, independent of the login DB lock.
                await asyncio.sleep(.02)
                result["twenty_ms_heartbeat_seconds"] = round(time.monotonic() - started, 3)
                result["concurrent_health"] = (await client.get("/health")).status_code
                result["login_status"] = (await login).status_code
                thread.join(5)
                assert not errors
                assert result["twenty_ms_heartbeat_seconds"] > .8
        asyncio.run(run())
        app.state.engine.dispose()
finally:
    with admin.begin() as connection:
        connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
    admin.dispose()
    result["schema_cleaned"] = True
print(json.dumps(result, indent=2))
