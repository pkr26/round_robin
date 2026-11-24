from copy import deepcopy
from io import BytesIO
import json
import re
from html import unescape
from pathlib import Path

from pypdf import PdfReader
import pytest

from round_robin.fixtures import sample_bundle
from round_robin.reports.exports import ExportFailure, render_saved_report
from round_robin.reports.storage import ArtifactStore, StorageFailure


def report_and_sources():
    bundle = sample_bundle()
    return bundle.scenarios[0].report.model_dump(mode="json"), [item.model_dump(mode="json") for item in bundle.evidence[:3]]


def test_json_is_exact_saved_object_and_markdown_retains_decision_scope():
    report, sources = report_and_sources()
    original = deepcopy(report)
    content, media = render_saved_report(report, "json", title="Sample", sources=sources)
    assert json.loads(content) == report and media == "application/json"
    content, media = render_saved_report(report, "markdown", title="Sample", sources=sources)
    # Markdown escapes are presentation syntax; compare the literal text they
    # represent, while the security regression below checks active syntax.
    text = unescape(re.sub(r"\\([\\`*_{}\[\]()#+!|\-.])", r"\1", content.decode()))
    assert report["id"] in text and "SHA-256" in text
    assert report['readiness'] in text and report['next_action'] in text
    assert all(item['description'] in text for item in report['essential_dependencies'])
    assert report == original


def test_markdown_keeps_untrusted_images_html_and_delimiters_literal():
    report, sources = report_and_sources()
    payload = '![pixel](https://tracking.invalid/pixel) <img src="https://tracking.invalid/html">\n# forged heading\n---\n`code` [link](javascript:alert(1)) &lt;img&gt;'
    report['recommendation']['summary'] = payload
    report['comparison'][0]['label'] = payload
    report['next_action'] = payload
    sources[0]['text'] = payload
    sources[0]['source_title'] = payload
    original = deepcopy(report)
    content, _ = render_saved_report(report, 'markdown', title=payload, sources=sources)
    rendered = content.decode()
    assert '![pixel](' not in rendered
    assert '<img' not in rendered and '<script' not in rendered
    assert '\\!\\[pixel\\]\\(https&#58;//tracking\\.invalid/pixel\\)' in rendered
    assert '&lt;img src="https&#58;//tracking\\.invalid/html"&gt;' in rendered
    assert '\n# forged heading' not in rendered and '\n---\n' not in rendered
    assert '[link](javascript:' not in rendered
    assert '&amp;lt;img&amp;gt;' in rendered
    assert report == original


def test_pdf_contains_same_identity_caveats_costs_sources_and_no_remote_image_fetch():
    report, sources = report_and_sources()
    title = 'Sample <img src="https://example.com/private.png"/>'
    pdf, media = render_saved_report(report, "pdf", title=title, sources=sources)
    reader = PdfReader(BytesIO(pdf))
    text = "\n".join(page.extract_text() for page in reader.pages)
    assert media == "application/pdf" and report["id"] in text
    assert "1600.00" in text and "1840.00" in text and "SIMULATED" in text
    assert "SHA-256" in text and "example.com/private.png" in text.replace("\n", "")
    assert all(not page.get("/Annots") for page in reader.pages)


def test_long_pdf_tables_and_partial_readiness_render_without_invented_recommendation():
    report = sample_bundle().scenarios[1].partial_report.model_dump(mode="json")
    for option in report["comparison"]:
        option["constraint_results"] = [{"requirement_id": f"requirement_{i}", "result": "unknown", "reason": "Long saved explanation with a missing decisive input. " * 7, "basis_refs": []} for i in range(20)]
    content, _ = render_saved_report(report, "pdf", title="Long partial report", sources=[])
    reader = PdfReader(BytesIO(content))
    text = "\n".join(page.extract_text() for page in reader.pages)
    assert len(reader.pages) > 3 and "unavailable" in text
    assert "2200.00" in text and "2080.00" in text
    assert "No valid synthesis" in text


def test_artifact_bytes_are_immutable_integrity_checked_and_path_keys_not_user_selected(tmp_path):
    store = ArtifactStore(tmp_path)
    key, digest = store.put(b"original")
    assert store.read(key, digest) == b"original"
    with pytest.raises(StorageFailure):
        store.read("../../private", digest)
    (tmp_path / key).write_bytes(b"tampered")
    with pytest.raises(StorageFailure):
        store.read(key, digest)
    store.purge(key)
    assert not (tmp_path / key).exists()


def test_artifact_store_refuses_symlink_and_oversized_bytes(tmp_path):
    store = ArtifactStore(tmp_path / "private")
    key, digest = store.put(b"saved")
    (store.directory / key).unlink()
    (store.directory / key).symlink_to(tmp_path / "unrelated")
    (tmp_path / "unrelated").write_bytes(b"saved")
    with pytest.raises(OSError):
        store.read(key, digest)
    with pytest.raises(StorageFailure):
        store.put(b"x" * (10 * 1024 * 1024 + 1))


def test_unsupported_export_is_rejected_without_model_or_network():
    with pytest.raises(ExportFailure):
        render_saved_report({}, "html-remote", title="Unsupported")
