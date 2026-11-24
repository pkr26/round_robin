"""Real Celery worker with test-only synthetic image adapters and a network fence."""
import hashlib
import os
import time

if os.environ.get("ROUND_ROBIN_IMAGE_PROCESS_FIXTURE") != "1":
    raise RuntimeError("Synthetic image process helper requires its explicit test profile")

import pytest
from backend.tests.conftest import PROVIDER_CREDENTIAL_NAMES, _install_network_boundary

if any(os.environ.get(name) for name in PROVIDER_CREDENTIAL_NAMES):
    raise RuntimeError("Synthetic image process helper refuses provider credentials")
_BOUNDARY = pytest.MonkeyPatch()
_install_network_boundary(_BOUNDARY)

from sqlalchemy import select
from round_robin.database.models import BudgetOperation, PolicyBucket
from round_robin.database.session import SessionLocal
from round_robin.database.visual_models import DesignOperation, DesignSession
from round_robin.visual.schemas import Critique, ImageCapabilities
from backend.tests.test_image_provider import pixels
from round_robin.worker import image_tasks


class ProcessImageAdapter:
    def __init__(self, provider, configuration):
        self.provider = provider
        self.capabilities = ImageCapabilities.model_validate(configuration["capabilities"][provider])

    def _received(self, packet, authorization):
        key = "test-image-receipt:" + authorization.operation_id
        # This is a fresh database connection after the application's authorized
        # transaction has acknowledged its commit, at the simulated I/O boundary.
        with SessionLocal.begin() as session:
            budget = session.get(BudgetOperation, authorization.operation_id)
            operation = session.get(DesignOperation, authorization.operation_id)
            design = session.get(DesignSession, authorization.session_id)
            assert budget is not None and budget.status == "reserved"
            assert operation.payload["status"] == "authorized"
            assert design.state["step_calls"][authorization.step_id] <= 2
            marker = session.scalar(select(PolicyBucket).where(PolicyBucket.key == key).with_for_update())
            previous = marker.metadata_json if marker else {}
            observed = {"synthetic_fixture": True, "provider": self.provider, "authorization_seen": True, "observations": previous.get("observations", 0) + 1, "fake_provider_sends": previous.get("fake_provider_sends", 0), "image_sha256": [hashlib.sha256(value).hexdigest() for value in packet.get("images", [])], "peer_findings_present": "peer_findings" in packet}
            if marker is None:
                marker = PolicyBucket(id="receipt_" + hashlib.sha256(key.encode()).hexdigest(), key=key, limit_usd="0", spending_locked=True, metadata_json=observed)
                session.add(marker)
            else:
                marker.metadata_json = observed
        if os.environ.get("ROUND_ROBIN_IMAGE_FIXTURE_BEHAVIOR") == "block_after_authorization" and packet["kind"] == "generation":
            time.sleep(60)  # Parent kills only this owned worker process group.
        with SessionLocal.begin() as session:
            marker = session.scalar(select(PolicyBucket).where(PolicyBucket.key == key).with_for_update())
            marker.metadata_json = {**marker.metadata_json, "fake_provider_sends": marker.metadata_json["fake_provider_sends"] + 1}

    def submit(self, packet, authorization, *, idempotency_key):
        self._received(packet, authorization)
        assert idempotency_key is None
        return {"status": "completed", "image_bytes": pixels(), "actual_usd": None, "provider_request_id": "synthetic-process-generation", "usage": {}}

    def review(self, packet, authorization):
        self._received(packet, authorization)
        assert packet["images"][0] == pixels()
        receipt = packet["receipt"]
        critique = Critique(provider=self.provider, version_id=receipt["version_id"], canonical_hash=receipt["canonical_hash"], receipt_id=receipt["id"], findings=[], limitations=["Synthetic process fixture; no model quality judgment."])
        return {"status": "completed", "actual_usd": None, "provider_request_id": "synthetic-process-critic-" + self.provider, "usage": {}, "critique": critique.model_dump(mode="json")}


image_tasks.configured_image_provider = lambda provider, configuration: ProcessImageAdapter(provider, configuration)
image_app = image_tasks.image_app
image_app.conf.worker_enable_remote_control = False


@image_app.task(name="round_robin.tests.image.marker")
def processed_marker(identity):
    if not isinstance(identity, str) or len(identity) != 32 or any(char not in "0123456789abcdef" for char in identity):
        raise ValueError("Invalid synthetic marker")
    with SessionLocal.begin() as session:
        session.add(PolicyBucket(id="marker_" + identity, key="test-image-drained:" + identity, limit_usd="0", spending_locked=True, metadata_json={"processed": True, "synthetic_fixture": True}))
