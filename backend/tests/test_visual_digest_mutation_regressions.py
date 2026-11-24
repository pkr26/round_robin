"""Persisted edit bundles and regression identities survive code upgrades."""

from copy import deepcopy

import pytest
from round_robin.visual.feedback import (
    build_edit_bundle,
    digest,
    regression_targets,
    verify_edit_bundle,
)


def saved_brief():
    # Deliberately not in sorted order: persisted JSON object key order is not identity.
    return {"width": 64, "purpose": "Café", "locked_requirements": ["Conserver été"],
            "domain": "raster_concept", "height": 64, "audience": "Visiteurs", "required_text": []}


def saved_bundle():
    return {"schema_version": "visual-edit-1", "parent_version_id": "version_parent", "parent_hash": "a" * 64,
            "brief_hash": "e3021c5bc42e751776980d04d82c05314343afe9ccdc6826fb7c2c2310f717d0", "intent": "Éclaircir",
            "selected_findings": [], "preserve_requirements": ["Conserver été"],
            "regression_targets": [{"id": "requirement_e7fab9475ded8c42271ccd82356c46c0", "kind": "locked_requirement",
                                    "description": "Conserver été", "severity": "critical"}]}


def test_preexisting_unicode_bundle_and_requirement_ids_remain_readable():
    brief, bundle = saved_brief(), saved_bundle()
    saved_hash = "f21b6a0dd7a6ac1693da41f1999574be5baf4f2ae24d6f915cacc87ce58bde16"
    read = verify_edit_bundle(bundle, saved_hash, brief=brief, parent_version_id="version_parent", parent_hash="a" * 64)
    assert read == bundle
    assert regression_targets(brief, []) == bundle["regression_targets"]
    created, created_hash = build_edit_bundle(brief=brief, parent_version_id="version_parent", parent_hash="a" * 64,
                                               findings=[], dispositions={}, intent="Éclaircir")
    assert created == bundle and created_hash == saved_hash
    brief_reordered = dict(reversed(list(brief.items())))
    reordered = dict(reversed(list(bundle.items())))
    assert verify_edit_bundle(reordered, saved_hash, brief=brief_reordered, parent_version_id="version_parent", parent_hash="a" * 64) == bundle
    changed = deepcopy(bundle)
    changed["intent"] = "Change this saved instruction"
    with pytest.raises(ValueError):
        verify_edit_bundle(changed, saved_hash, brief=brief, parent_version_id="version_parent", parent_hash="a" * 64)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_nonfinite_values_cannot_enter_a_json_integrity_digest(value):
    with pytest.raises(ValueError):
        digest({"unexpected_value": value})
