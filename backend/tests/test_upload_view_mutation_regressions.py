"""Upload receipts expose canonical metadata without exposing private content."""

import hashlib
from uuid import uuid4

from round_robin.fixtures import sample_bundle

from backend.tests.test_artifacts_api import artifacts as artifacts  # noqa: PLC0414


def test_canonical_upload_receipt_and_listing_share_exact_owned_metadata(artifacts):
    _, client, meeting, _, _ = artifacts
    private = b"Private supplier terms\r\nSecond line\r\n"
    canonical = b"Private supplier terms\nSecond line\n"
    headers = {"Content-Type": "text/plain", "X-Command-Id": str(uuid4()), "X-Expected-Revision": "1",
               "X-Filename": "supplier-terms.txt", "X-Content-SHA256": hashlib.sha256(canonical).hexdigest()}
    response = client.post(f"/api/meetings/{meeting}/uploads", content=private, headers=headers)
    assert response.status_code == 201, response.text
    receipt = response.json()
    assert receipt["meeting_id"] == meeting
    assert receipt["filename"] == "supplier-terms.txt"
    assert receipt["sha256"] == hashlib.sha256(canonical).hexdigest()
    assert receipt["status"] == "ready" and receipt["byte_size"] == len(canonical)
    assert set(receipt["recipients"]) == {"openai", "anthropic", "google"}
    assert "Private supplier terms" not in response.text and "canonical_text" not in receipt
    assert client.post(f"/api/meetings/{meeting}/uploads", content=private, headers=headers).json() == receipt
    listing = client.get(f"/api/meetings/{meeting}/uploads")
    assert listing.status_code == 200 and listing.json()["items"] == [receipt]
    assert "Private supplier terms" not in listing.text
    other = client.post("/api/meetings", json={"command_id": str(uuid4()), "brief": sample_bundle().brief.model_dump(mode="json")})
    assert other.status_code == 201
    assert client.get(f"/api/meetings/{other.json()['id']}/uploads").json()["items"] == []
