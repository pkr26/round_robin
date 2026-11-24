"""Hostile study records must never manufacture a quality or spending pass."""
from copy import deepcopy
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import runpy
import stat
import sys

import pytest

from backend.tests.test_evaluation import study
from backend.tests.test_release_evaluation import manifest as release_manifest, result as release_result
from backend.tests.test_visual_evaluation import manifest as visual_manifest, output as visual_output
from experiments import evaluation, journal, release, visual_evaluation


@pytest.mark.parametrize("mutate,reason", [
    (lambda d: d["double_review_task_ids"].clear(), "independent reviewers"),
    (lambda d: d["tasks"][0].update(options=[]), "two to four options"),
    (lambda d: d["tasks"][0]["checked_snapshots"].append(deepcopy(d["tasks"][0]["checked_snapshots"][0])), "Snapshot identities"),
    (lambda d: d["tasks"][0]["checked_snapshots"][0].update(origin="invented"), "exact fictional passage"),
    (lambda d: d["tasks"][0].update(version=2), "Task version"),
])
def test_fixed_evidence_dataset_rejects_incomplete_preregistration(mutate, reason):
    data = evaluation.load_dataset()
    mutate(data)
    with pytest.raises(ValueError, match=reason):
        evaluation.validate_dataset(data)


@pytest.mark.parametrize("change,reason", [
    ({"next_action_judgment": " "}, "complete typed"),
    ({"status": "accepted"}, "Unknown trial outcome"),
    ({"trial_id": ""}, "identities are required"),
])
def test_trial_and_grade_records_reject_missing_semantics(change, reason):
    data, outputs, grades, _ = study()
    with pytest.raises(ValueError, match=reason):
        replace(grades[0] if "next_action_judgment" in change else outputs[0], **change)


@pytest.mark.parametrize("change,reason", [
    ({"usefulness": True}, "integer"),
    ({"hidden_critical_defect": "false"}, "must be boolean"),
    ({"hidden_critical_defect": True, "usefulness": 2}, "explicit reason"),
    ({"hard_constraint_findings": ()}, "findings are required"),
    ({"timestamp": 0}, "timezone-aware ISO"),
    ({"timestamp": "2026-11-16T10:00:00"}, "include its timezone"),
    ({"adjudication_reason": "Reason without originals"}, "preserves original IDs"),
    ({"adjudication_of": ("original_one",), "adjudication_reason": "One is insufficient"}, "distinct retained"),
    ({"critical_defect_reason": " "}, "nonblank text"),
    ({"passage_support_findings": [None]}, "arrays of nonblank"),
])
def test_manual_grade_failures_cannot_silently_become_valid_observations(change, reason):
    _, _, grades, _ = study()
    with pytest.raises(ValueError, match=reason):
        replace(grades[0], **change)


def test_grade_mapping_and_adjudication_cannot_invent_outputs_or_reviewers():
    data, outputs, grades, mapping = study()
    with pytest.raises(ValueError, match="Unknown workflow"):
        evaluation.summarize_workflow(data, "invented", outputs, grades, mapping)
    with pytest.raises(ValueError, match="another dataset"):
        evaluation.summarize_workflow(data, "independent_review", outputs, grades, {**mapping, "dataset_fingerprint": "wrong"})
    with pytest.raises(ValueError, match="retained original output"):
        evaluation.summarize_workflow(data, "independent_review", outputs, [replace(grades[0], packet_token="missing")], mapping)
    with pytest.raises(ValueError, match="versioned trial plan"):
        evaluation.build_blinded_packets(data, [replace(outputs[0], trial_id="invented")], seed="test")
    originals = next([item for item in grades if item.packet_token == grade.packet_token] for grade in grades if sum(g.packet_token == grade.packet_token for g in grades) == 2)
    first = originals[0]
    adjudication = replace(first, grade_id="adjudication_one", reviewer_id="reviewer_adjudicator", adjudication_of=tuple(g.grade_id for g in originals), adjudication_reason="Record exact reconciliation")
    with pytest.raises(ValueError, match="Conflicting adjudications"):
        evaluation.summarize_workflow(data, "independent_review", outputs, grades + [adjudication, replace(adjudication, grade_id="adjudication_two", reviewer_id="different")], mapping)
    with pytest.raises(ValueError, match="retain all independent"):
        evaluation.summarize_workflow(data, "independent_review", outputs, grades + [replace(adjudication, adjudication_of=(first.grade_id, "foreign_original"))], mapping)


def test_completed_without_time_is_counted_as_missing_performance_and_chronology_checked():
    assert evaluation.deadline_attainment([{"admitted": True, "status": "completed", "mandatory_work_complete": True}])["successful_run_count"] == 0
    with pytest.raises(ValueError, match="cannot follow"):
        evaluation.deadline_attainment([{"admitted": True, "status": "completed", "mandatory_work_complete": True, "active_seconds": 1, "first_useful_active_seconds": 2}])


def test_blinded_mapping_directory_is_durable_before_packet_publication(tmp_path, monkeypatch):
    calls = []
    original = os.fsync
    def sync(fd):
        calls.append("directory" if stat.S_ISDIR(os.fstat(fd).st_mode) else "file")
        return original(fd)
    monkeypatch.setattr(evaluation.os, "fsync", sync)
    evaluation.save_blinded_packets(tmp_path / "packets", [{"public": "packet"}], {"private": "mapping"})
    assert calls == ["file", "directory", "file", "directory"]


@pytest.mark.parametrize("mutation,reason", [
    (lambda d: d.update(mode="fictional"), "held-out manifest"),
    (lambda d: d["tasks"][0].update(evidence_manifest=[]), "evidence_manifest"),
    (lambda d: d.update(protocol_decision_id=None), "selected protocol"),
])
def test_release_manifest_requires_actual_frozen_identity(mutation, reason):
    data = release_manifest()
    mutation(data)
    with pytest.raises(ValueError, match=reason):
        release.validate_manifest(data)


@pytest.mark.parametrize("mutation,reason", [
    (lambda r: r.update(dataset_fingerprint="foreign"), "frozen study"),
    (lambda r: r.update(status="ready"), "explicit outcome"),
    (lambda r: r.update(recorded_at="2020-01-01T00:00:00+00:00"), "prospective freeze"),
    (lambda r: r["original_grades"].append(deepcopy(r["original_grades"][0])), "distinct reviewer"),
    (lambda r: r["original_grades"][0].update(usefulness=True), "usefulness score"),
    (lambda r: r["original_grades"][0].update(critical_defect=True, critical_defect_reason=None), "attributable reason"),
])
def test_release_result_and_grade_attacks_cannot_raise_usefulness(mutation, reason):
    data = release_manifest()
    row = release_result(data["tasks"][0], release.validate_manifest(data))
    mutation(row)
    with pytest.raises(ValueError, match=reason):
        release.summarize(data, [row])


@pytest.mark.parametrize("change,reason", [
    ({"grade_id": ""}, "own reviewer"),
    ({"timestamp": "2026-11-16T12:59:59+00:00"}, "cannot predate"),
])
def test_release_adjudication_retains_identity_and_chronology(change, reason):
    data = release_manifest()
    row = release_result(data["tasks"][0], release.validate_manifest(data))
    row["original_grades"].append({**row["original_grades"][0], "grade_id": "grade_second", "reviewer_id": "reviewer_second"})
    row["adjudication"] = {"grade_id": "grade_adjudicated", "reviewer_id": "adjudicator", "timestamp": "2026-11-16T13:01:00+00:00", "output_hash": row["output_hash"], "usefulness": 4, "critical_defect": False, "original_grade_ids": [g["grade_id"] for g in row["original_grades"]], "reason": "Reconciled actual records", **change}
    with pytest.raises(ValueError, match=reason):
        release.summarize(data, [row])


@pytest.mark.parametrize("mutate,reason", [
    (lambda d: d.update(status="draft"), "frozen matched"),
    (lambda d: d.update(domain=""), "domain-competent"),
    (lambda d: d.update(blinding_seed="short"), "blinding seed"),
    (lambda d: d["configuration_fingerprints"].update(generation_only="invalid"), "immutable configuration"),
    (lambda d: d["per_trial_limits"].update(maximum_usd="0"), "positive per-trial"),
    (lambda d: d["batch_limits"].update(polls=True), "whole-batch"),
    (lambda d: d.update(benefit_and_regression_rule=""), "useful benefit"),
])
def test_visual_study_manifest_cannot_relax_frozen_authority(mutate, reason):
    data = visual_manifest()
    mutate(data)
    with pytest.raises(ValueError, match=reason):
        visual_evaluation.validate_manifest(data)


@pytest.mark.parametrize("value", ["invalid", object(), "1e-65", True, float("nan")])
def test_visual_study_currency_cannot_be_nonfinite_or_inexact(value):
    with pytest.raises(ValueError):
        visual_evaluation._money(value)


@pytest.mark.parametrize("value", [None, "2026-11-16T12:00:00"])
def test_visual_timestamps_need_explicit_timezones(value):
    with pytest.raises(ValueError):
        visual_evaluation._timestamp(value)


@pytest.mark.parametrize("mutate,reason", [
    (lambda r: r.update(status="invented"), "explicit outcome"),
    (lambda r: r.update(final_candidate_hash=None), "actual initial and final"),
    (lambda r: r.update(final_candidate_hash="a" * 64), "silently substitute"),
    (lambda r: r.update(recorded_at="2020-01-01T00:00:00+00:00"), "prospective freeze"),
    (lambda r: r["original_grades"].append(deepcopy(r["original_grades"][0])), "unique grade and reviewer"),
    (lambda r: r["original_grades"][0].update(timestamp="2020-01-01T00:00:00+00:00"), "cannot predate"),
    (lambda r: r["original_grades"][0].update(hard_regression="false"), "actual defect"),
    (lambda r: r.update(user_selection="auto_accept"), "explicit separate"),
])
def test_visual_grade_and_outcome_cannot_manufacture_preference(mutate, reason):
    data = visual_manifest()
    row = visual_output(visual_evaluation.build_plan(data)[0])
    mutate(row)
    with pytest.raises(ValueError, match=reason):
        visual_evaluation.summarize(data, [row])


def test_visual_review_requires_exact_pixels_and_retains_primary_regressions():
    data = visual_manifest()
    trial = visual_evaluation.build_plan(data)[2]
    with pytest.raises(ValueError, match="pixel hashes"):
        visual_evaluation.review_packet(data, trial, "bad", "a" * 64)
    result = visual_evaluation.summarize(data, [visual_output(trial, regression=True)])
    assert result["arms"]["multi_critic_revision"]["original_hard_regression_primary"] == ["b0"]
    assert not result["arms"]["multi_critic_revision"]["human_prefers_final_primary"]


def test_journal_rejects_bad_profile_nonobject_event_and_closed_append(tmp_path):
    with pytest.raises(journal.JournalError, match="execution profile"):
        journal.Journal(tmp_path, mode="unrestricted")
    with pytest.raises(journal.JournalError, match="exclusively open"):
        journal.Journal(tmp_path).append({"event": "test"})
    path = tmp_path / "journal"
    with journal.Journal(path) as record:
        with pytest.raises(journal.JournalError, match="events must be objects"):
            record.append([])
        record.append({"event": "still usable"})
    with journal.Journal(path) as record:
        assert record.events == [{"event": "still usable"}]


@pytest.mark.parametrize("file,content,reason", [("manifest.json", "invalid", "invalid journal manifest"), ("head.json", "invalid", "invalid durable journal head"), ("events.jsonl", "[]\n", "integrity check")])
def test_corrupt_journal_files_fail_closed_without_recreating_history(tmp_path, file, content, reason):
    path = tmp_path / "journal"
    with journal.Journal(path):
        pass
    (path / file).write_text(content)
    with pytest.raises(journal.JournalError, match=reason):
        with journal.Journal(path):
            pass
    assert (path / file).read_text() == content


def test_journal_rejects_linked_lock_or_history(tmp_path):
    path = tmp_path / "journal"
    with journal.Journal(path):
        pass
    external = tmp_path / "unrelated"
    external.write_text("private")
    for file in ("lock", "events.jsonl"):
        target = path / file
        original = target.read_bytes()
        target.unlink()
        target.symlink_to(external)
        with pytest.raises(journal.JournalError, match="symlink|owned regular"):
            with journal.Journal(path):
                pass
        target.unlink()
        target.write_bytes(original)
    assert external.read_text() == "private"


def test_journal_short_write_invalidates_instance_and_releases_lock(tmp_path):
    path = tmp_path / "journal"
    with journal.Journal(path) as record:
        stream = record._stream
        class ShortWriter:
            def write(self, data):
                return len(data) - 1
            def close(self):
                stream.close()
        record._stream = ShortWriter()
        with pytest.raises(OSError, match="short journal write"):
            record.append({"event": "would be incomplete"})
        assert record._stream is None and record._lock is None
    with journal.Journal(path) as record:
        assert record.events == []


def test_journal_root_alias_and_special_lock_cannot_redirect_accounting(tmp_path):
    import os
    path = tmp_path / "journal"
    with journal.Journal(path):
        pass
    alias = tmp_path / "redirected-journal"
    alias.symlink_to(path, target_is_directory=True)
    with pytest.raises(journal.JournalError, match="directory cannot be a symlink"):
        journal.Journal(alias)
    (path / "lock").unlink()
    os.mkfifo(path / "lock")
    with pytest.raises(journal.JournalError, match="regular operator"):
        with journal.Journal(path):
            pytest.fail("Special lock admitted")
    assert (path / "events.jsonl").read_bytes() == b""


@pytest.mark.parametrize("rival_kind", ["directory", "file"])
def test_journal_directory_creation_race_never_recreates_missing_history(tmp_path, monkeypatch, rival_kind):
    from pathlib import Path
    path = tmp_path / "journal"
    mkdir = Path.mkdir
    def raced(candidate, *args, **kwargs):
        if candidate == path:
            if rival_kind == "directory":
                mkdir(candidate)
            else:
                candidate.write_text("rival")
            raise FileExistsError
        return mkdir(candidate, *args, **kwargs)
    monkeypatch.setattr(Path, "mkdir", raced)
    with pytest.raises(journal.JournalError, match="incomplete|not a directory"):
        with journal.Journal(path):
            pytest.fail("Rival creation admitted")


def test_journal_rejects_foreign_owner_and_rehashed_nonobject_event(tmp_path, monkeypatch):
    import hashlib
    import os
    path = tmp_path / "journal"
    with journal.Journal(path):
        pass
    owner = os.geteuid()
    with monkeypatch.context() as changed:
        changed.setattr(os, "geteuid", lambda: owner + 1)
        with pytest.raises(journal.JournalError, match="owned by the current"):
            with journal.Journal(path): pass
    payload = {"sequence": 1, "previous_hash": "0" * 64, "event": []}
    digest = hashlib.sha256(journal.canonical(payload)).hexdigest()
    (path / "events.jsonl").write_bytes(journal.canonical({**payload, "hash": digest}) + b"\n")
    with pytest.raises(journal.JournalError, match="integrity"):
        with journal.Journal(path): pass


@pytest.mark.parametrize("module,flags", [
    ("experiments.evaluation", ["plan"]),
    ("experiments.evaluation", ["grade-schema"]),
    ("experiments.evaluation", ["validate"]),
    ("experiments.release", []),
    ("experiments.release", ["--results"]),
    ("experiments.visual_evaluation", []),
    ("experiments.visual_evaluation", ["--plan"]),
    ("experiments.visual_evaluation", ["--results"]),
])
def test_study_cli_entrypoints_emit_prospective_outputs_only(tmp_path, monkeypatch, capsys, module, flags):
    path = tmp_path / "manifest.json"
    results = tmp_path / "results.json"
    results.write_text("[]")
    if module == "experiments.evaluation":
        args = [module, *flags]
        if flags == ["plan"]:
            args += ["--output", str(tmp_path / "plan.json")]
    else:
        path.write_text(json.dumps(release_manifest() if module == "experiments.release" else visual_manifest()))
        args = [module, str(path), *flags]
        if flags == ["--results"]:
            args.append(str(results))
    monkeypatch.setattr(sys, "argv", args)
    runpy.run_module(module, run_name="__main__")
    text = (tmp_path / "plan.json").read_text() if (tmp_path / "plan.json").exists() else capsys.readouterr().out
    result = json.loads(text)
    assert result is not None
    assert '"phase_passed": true' not in text and '"phase_5_passed": true' not in text and '"phase_8_passed": true' not in text
