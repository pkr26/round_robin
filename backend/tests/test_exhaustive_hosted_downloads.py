"""A hosted middleware policy must not remove the native attachment sandbox."""
import hashlib
from types import SimpleNamespace

import pytest

from backend.tests.test_api import command, create
from backend.tests.test_hosted import hosted, session_client
from backend.tests.test_visual import png
from round_robin.visual.assets import validate_raster


@pytest.mark.parametrize("kind,suffix", [("structured_diagram", "svg"), ("structured_diagram", "json"), ("native_interface", "html")])
def test_hosted_native_attachments_keep_sandbox_and_host_framing_restrictions(hosted, monkeypatch, kind, suffix):
    monkeypatch.setenv("ROUND_ROBIN_VISUAL_ENABLED", "1")
    monkeypatch.setenv("ROUND_ROBIN_NATIVE_DIAGRAM_ENABLED", "1")
    client, _, csrf, _ = session_client(hosted)
    client.headers["x-csrf-token"] = csrf
    meeting = create(client)
    response = client.post(f"/api/meetings/{meeting['id']}/design-sessions", json=command(mode="review_only", brief={"purpose": "Inspect private native source", "audience": "Invited reviewer", "domain": kind, **({"width": 500, "height": 200} if kind == "structured_diagram" else {})}, maximum_usd="0"))
    assert response.status_code == 201, response.text
    design = response.json()
    if kind == "structured_diagram":
        body = command(expected_revision=design["revision"], diagram={"width": 500, "height": 200, "nodes": [{"id": "node_one", "label": "Input", "x": 20, "y": 20}], "edges": []})
        route = "native-diagram"
    else:
        from round_robin.api import visual
        monkeypatch.setenv("ROUND_ROBIN_NATIVE_INTERFACE_ENABLED", "1")
        monkeypatch.setenv("ROUND_ROBIN_NATIVE_RENDERER_IMAGE", "synthetic-fixture")
        source = "<script>window.privateSentinel=true</script>"
        digest = hashlib.sha256(source.encode()).hexdigest()
        rendered = SimpleNamespace(source=source, source_hash=digest, raster=validate_raster(png()), renderer_version="fixture", configuration_hash="a" * 64, checks=[], snapshot=SimpleNamespace(model_dump=lambda **kw: {"source_sha256": digest}))
        monkeypatch.setattr(visual, "render_interface", lambda *a, **kw: rendered)
        body, route = command(expected_revision=design["revision"], source=source, actions=[]), "native-interface"
    response = client.post(f"/api/design-sessions/{design['id']}/{route}", json=body)
    assert response.status_code == 201, response.text
    version = response.json()["versions"][0]
    download = client.get(f"/api/design-versions/{version['id']}/source.{suffix}")
    assert download.status_code == 200, download.text
    policies = download.headers.get_list("content-security-policy")
    assert any("sandbox" in policy and "default-src 'none'" in policy for policy in policies)
    assert any("frame-ancestors 'none'" in policy for policy in policies)
    assert download.headers["cache-control"] == "no-store"
    assert "attachment" in download.headers["content-disposition"]
