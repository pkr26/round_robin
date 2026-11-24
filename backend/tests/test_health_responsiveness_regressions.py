import asyncio
from dataclasses import replace
import threading

import anyio.to_thread
import httpx

from backend.tests.test_hosted import hosted_settings
from round_robin.app import create_app


def test_unconfigured_health_remains_available_when_workers_are_busy(tmp_path):
    settings = replace(hosted_settings(tmp_path), provider_config_file=None)
    app = create_app(f"sqlite:///{tmp_path / 'health.db'}", settings=settings, initialize=True)
    entered, release = threading.Event(), threading.Event()

    def occupy_worker():
        entered.set()
        assert release.wait(10)

    async def run():
        anyio.to_thread.current_default_thread_limiter().total_tokens = 1
        worker = asyncio.create_task(anyio.to_thread.run_sync(occupy_worker))
        try:
            async def wait_for_worker():
                while not entered.is_set():
                    await asyncio.sleep(0.005)

            await asyncio.wait_for(wait_for_worker(), timeout=3)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://app.example") as client:
                response = await asyncio.wait_for(client.get("/health"), timeout=0.5)
            assert response.status_code == 200
            assert response.json()["paid_access_configured"] is False
            assert not worker.done()
        finally:
            release.set()
            await worker

    asyncio.run(run())


def test_configured_health_checks_provider_off_the_event_loop(tmp_path, monkeypatch):
    from round_robin.api import preflight

    settings = replace(hosted_settings(tmp_path), provider_config_file=tmp_path / "provider.json")
    app = create_app(f"sqlite:///{tmp_path / 'configured-health.db'}", settings=settings, initialize=True)
    entered, release, watchdog_fired = threading.Event(), threading.Event(), threading.Event()
    worker_threads = []

    def blocked_configuration(configuration, mode):
        worker_threads.append(threading.get_ident())
        entered.set()
        assert release.wait(10)
        return {"funded": True}

    monkeypatch.setattr(preflight, "run_configuration", blocked_configuration)

    def watchdog_release():
        watchdog_fired.set()
        release.set()

    async def run():
        event_loop_thread = threading.get_ident()
        watchdog = threading.Timer(5, watchdog_release)
        watchdog.daemon = True
        watchdog.start()
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://app.example") as client:
                request = asyncio.create_task(client.get("/health"))
                try:
                    async def wait_for_configuration():
                        while not entered.is_set():
                            await asyncio.sleep(0.005)

                    await asyncio.wait_for(wait_for_configuration(), timeout=3)
                    assert not watchdog_fired.is_set()
                    assert not request.done()
                    assert len(worker_threads) == 1
                    assert worker_threads[0] != event_loop_thread
                finally:
                    release.set()
                response = await request
                assert response.status_code == 200
                assert response.json()["paid_access_configured"] is True
        finally:
            release.set()
            watchdog.cancel()

    asyncio.run(run())
