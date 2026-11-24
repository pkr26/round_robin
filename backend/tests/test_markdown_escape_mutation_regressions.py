"""Saved report text must remain literal across Markdown export contexts."""

import html
import json
import re
from copy import deepcopy

import pytest
from round_robin.fixtures import sample_bundle
from round_robin.reports.exports import render_saved_report


def test_fenced_code_markers_in_valid_report_cannot_absorb_saved_headings():
    saved = sample_bundle().scenarios[0].report
    report = saved.model_dump(mode="json")
    report["recommendation"]["summary"] = "~~~"
    report["tradeoff"] = "~~~"
    report = type(saved).model_validate(report).model_dump(mode="json")
    original = deepcopy(report)
    raw, media = render_saved_report(report, "markdown", title="Synthetic", sources=[])
    rendered = raw.decode()
    assert media.startswith("text/markdown")
    assert "\n~~~\n" not in rendered
    assert rendered.count("&#126;~&#126;") == 2
    for heading in ("## Readiness basis", "## Option comparison", "## Decisive tradeoff"):
        assert heading in rendered
    assert report == original


def test_tildes_are_literal_in_titles_nested_content_and_evidence_without_changing_json():
    saved = sample_bundle().scenarios[0].report
    report = saved.model_dump(mode="json")
    payload = "Estimate ~30; ~~uncertain~~; &#126; and ~~~"
    report["recommendation"]["summary"] = payload
    report["next_action"] = payload
    report = type(saved).model_validate(report).model_dump(mode="json")
    sources = [{"id": "source_test", "text": payload, "source_title": payload}]
    before = deepcopy((report, sources))
    raw, _ = render_saved_report(report, "markdown", title=payload, sources=sources)
    plain = html.unescape(re.sub(r"\\([\\`*_{}\[\]()#+!|\-.])", r"\1", raw.decode()))
    assert plain.count(payload) == 5
    assert "Estimate ~30" in raw.decode()
    assert "~~uncertain~~" not in raw.decode()
    assert "&#126;" in raw.decode()
    json_bytes, _ = render_saved_report(report, "json", title=payload, sources=sources)
    assert json.loads(json_bytes) == report
    assert (report, sources) == before


@pytest.mark.parametrize("underline", ["=", "==", "===", "  =  ", "\t===\t"])
@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"])
def test_saved_equal_sign_line_cannot_create_a_setext_heading(underline, newline):
    saved = sample_bundle().scenarios[0].report
    report = saved.model_dump(mode="json")
    payload = "Untrusted text" + newline + underline
    report["recommendation"]["summary"] = payload
    report = type(saved).model_validate(report).model_dump(mode="json")
    raw, _ = render_saved_report(report, "markdown", title="Cost = 30", sources=[])
    rendered = raw.decode()
    assert payload not in rendered
    assert "&#61;" in rendered
    assert "Cost = 30" in rendered
    plain = html.unescape(re.sub(r"\\([\\`*_{}\[\]()#+!|\-.])", r"\1", rendered))
    assert payload in plain


@pytest.mark.parametrize("url", ["https://example.com", "HTTP://example.com", "ftp://example.com", "mailto:person@example.com"])
def test_saved_url_cannot_become_a_misleading_automatic_link(url):
    saved = sample_bundle().scenarios[0].report
    report = saved.model_dump(mode="json")
    report["recommendation"]["summary"] = url
    report = type(saved).model_validate(report).model_dump(mode="json")
    raw, _ = render_saved_report(report, "markdown", title="Time 12:34", sources=[])
    rendered = raw.decode()
    assert url.split(":", 1)[0] + "&#58;" in rendered
    assert "Time 12:34" in rendered
    assert url in html.unescape(re.sub(r"\\([\\`*_{}\[\]()#+!|\-.])", r"\1", rendered))


@pytest.mark.parametrize("indent", ["    ", "\t", " \t", "  \t", "   \t"])
@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"])
def test_saved_indented_line_cannot_create_a_code_block(indent, newline):
    saved = sample_bundle().scenarios[0].report
    report = saved.model_dump(mode="json")
    payload = "Untrusted text" + newline + indent + "indented text"
    report["recommendation"]["summary"] = payload
    report = type(saved).model_validate(report).model_dump(mode="json")
    raw, _ = render_saved_report(report, "markdown", title="Synthetic", sources=[])
    rendered = raw.decode()
    assert payload not in rendered
    plain = html.unescape(re.sub(r"\\([\\`*_{}\[\]()#+!|\-.])", r"\1", rendered))
    assert payload in plain
