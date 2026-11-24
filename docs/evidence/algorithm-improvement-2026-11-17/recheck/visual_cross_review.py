"""Independent transport-level cross-check with injected HTTP mock only."""
import base64
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory

import httpx
from pytest import MonkeyPatch

from test_image_provider import authorization, generation_packet, image_configuration, pixels
from round_robin.providers.images import HTTPImageProvider
from round_robin.visual.reviews import PixelView, coverage, independent_review_packet, issue_review_receipt, validate_critique
from round_robin.visual.schemas import Critique, ImageCapabilities, VisualBrief


def run():
    with TemporaryDirectory() as temporary, MonkeyPatch.context() as monkeypatch:
        config = image_configuration.__wrapped__(Path(temporary), monkeypatch)
        captures = []
        def edit_send(request):
            captures.append(json.loads(request.content))
            return httpx.Response(200, json={"data": [{"b64_json": base64.b64encode(pixels()).decode()}]})
        packet = generation_packet("edit")
        packet["finding_dispositions"] = {"finding_selected": "address", "finding_declined": "decline"}
        packet["findings"] = [{"id": "finding_selected", "description": "UNIQUE_SELECTED_CHANGE"}]
        HTTPImageProvider("openai", config, transport=httpx.MockTransport(edit_send), api_key="synthetic-only").submit(packet, authorization("edit"))
        assert all(marker not in captures[0]["prompt"] for marker in ("finding_selected", "finding_declined", "UNIQUE_SELECTED_CHANGE"))
        raw_child, raw_parent = pixels("green"), pixels("blue")
        child_hash, parent_hash = hashlib.sha256(raw_child).hexdigest(), hashlib.sha256(raw_parent).hexdigest()
        prior = {"parent_version_id": "version_parent", "parent_hash": parent_hash, "edit_intent": "Correct the issue",
            "findings": [{"id": "finding_prior_critical", "severity": "critical", "description": "Critical prior problem"}]}
        views = [PixelView(raw_child, child_hash), PixelView(raw_parent, parent_hash, "comparison_parent_version_version_parent")]
        critiques, receipts, sent_prior_contexts = [], [], []
        for provider in ("anthropic", "google"):
            receipt = issue_review_receipt(ImageCapabilities.model_validate(config["capabilities"][provider]), "version_child", child_hash, views)
            review_packet = {**independent_review_packet(VisualBrief(purpose="Review child", audience="Test"), receipt, views, prior_context=prior),
                "kind": "vision", "owner_id": "owner_one", "session_id": "session_one", "step_id": "step_one", "timeout_seconds": 10}
            def vision_send(request, provider=provider):
                body = json.loads(request.content)
                assert "finding_prior_critical" in request.content.decode()
                assert all(base64.b64encode(raw).decode() in request.content.decode() for raw in (raw_child, raw_parent))
                sent_prior_contexts.append(provider)
                content = json.dumps({"findings": [], "limitations": []})
                return httpx.Response(200, json={"stop_reason": "end_turn", "content": [{"type": "text", "text": content}]} if provider == "anthropic" else
                    {"candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": content}]}}]})
            result = HTTPImageProvider(provider, config, transport=httpx.MockTransport(vision_send), api_key="synthetic-only").review(review_packet, authorization("vision"))
            critique = Critique.model_validate(result["critique"])
            validate_critique(critique, receipt, prior_finding_ids={"finding_prior_critical"})
            critiques.append(critique)
            receipts.append(receipt)
        result = coverage(critiques, receipts, version_id="version_child", canonical_hash=child_hash, generator_provider="openai")
        assert result["complete"] and not result["limitations"]
        return {"all_assertions_passed": True, "external_network_calls": 0, "mock_http_requests": 3,
            "actual_edit_http_prompt_omits_selected_findings_and_dispositions": True,
            "child_critics_received_both_images_and_prior_critical_finding": sent_prior_contexts,
            "empty_child_findings_accepted": True, "coverage": result,
            "qualification": "Quorum measures eligible reviewers, not quality. UI labels this accurately. Missing regression dispositions are a separate completeness gap; parent history is retained."}


if __name__ == "__main__":
    result = run()
    Path(__file__).with_name("visual-cross-review.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))
