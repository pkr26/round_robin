"""Reject inflated, stale, incomplete or approximate service coverage evidence."""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from scripts import coverage_gate as gate


@pytest.fixture
def evidence(tmp_path):
    paths = ("backend/src/round_robin/api/example.py", "backend/src/round_robin/worker/example.py", "frontend/app/page.tsx", "scripts/native-renderer.mjs")
    for path in paths:
        file = tmp_path / path
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text("value = 1\n" if path.endswith(".py") else "export const value = 1;\n")
    manifest = {"sources": gate.source_files(tmp_path), "verification_inputs": gate.input_files(tmp_path)}
    python = {"meta": {"branch_coverage": True}, "files": {path: {
        "summary": {"covered_lines": 1, "num_statements": 1, "covered_branches": 0, "num_branches": 0, "excluded_lines": 0},
        "executed_lines": [1], "missing_lines": [], "executed_branches": [], "missing_branches": [], "excluded_lines": [],
    } for path in paths if path.endswith(".py")}}
    def js(path):
        return {"kind": "istanbul_source", "files": {path: {"sha256": manifest["sources"][path], "lines": {"covered": 1, "total": 1}, "branches": {"covered": 0, "total": 0}}}}
    return tmp_path, manifest, python, js(paths[2]), js(paths[3])


def test_complete_current_source_reports_pass_with_named_service_metrics(evidence):
    result = gate.evaluate(*evidence)
    assert result["passed"] and result["source_manifest_verified"]
    assert set(result["services"]) == {"api", "worker", "frontend", "native_renderer"}
    assert result["services"]["worker"]["branches"]["percent"] == 100


def test_one_weak_service_cannot_hide_in_an_aggregate_or_round_up_95(evidence):
    root, manifest, *_ = evidence
    (root / 'backend/src/round_robin/worker/example.py').write_text('value = 1\n' * 20)
    manifest['sources'] = gate.source_files(root)
    row = evidence[2]["files"]["backend/src/round_robin/worker/example.py"]
    row["summary"].update(covered_lines=19, num_statements=20)
    row.update(executed_lines=list(range(1, 20)), missing_lines=[20])
    result = gate.evaluate(*evidence)
    assert not result["passed"]
    assert result["failures"] == ["worker lines must exceed 95% (19/20)"]


@pytest.mark.parametrize('report_index', [2, 3, 4])
def test_executable_source_cannot_report_zero_denominators(evidence, report_index):
    report = evidence[report_index]
    row = next(iter(report['files'].values()))
    if report_index == 2:
        row['summary'].update(covered_lines=0, num_statements=0)
        row['executed_lines'] = []
    else:
        row['lines'] = {'covered': 0, 'total': 0}
    result = gate.evaluate(*evidence)
    assert not result['passed']
    assert any('source inventory' in failure for failure in result['failures'])


def test_real_branch_cannot_be_removed_from_python_inventory(evidence):
    root, manifest, python, *_ = evidence
    identity = 'backend/src/round_robin/api/example.py'
    (root / identity).write_text('if flag:\n    value = 1\nelse:\n    value = 2\n')
    manifest['sources'] = gate.source_files(root)
    row = python['files'][identity]
    row['executed_lines'] = [1, 2, 4]
    row['summary'].update(covered_lines=3, num_statements=3)
    result = gate.evaluate(*evidence)
    assert not result['passed']
    assert any('branches do not match' in failure for failure in result['failures'])


@pytest.mark.parametrize('report_index,identity', [
    (3, 'frontend/app/page.tsx'), (4, 'scripts/native-renderer.mjs'),
])
def test_javascript_branch_denominator_cannot_be_truncated(evidence, report_index, identity):
    root, manifest, *_ = evidence
    (root / identity).write_text('export const value = flag ? 1 : 2;\n')
    manifest['sources'] = gate.source_files(root)
    manifest['verification_inputs'] = gate.input_files(root)
    row = evidence[report_index]['files'][identity]
    row['sha256'] = manifest['sources'][identity]
    result = gate.evaluate(*evidence)
    assert not result['passed']
    assert any('totals do not match executable source' in failure for failure in result['failures'])


def test_genuinely_empty_files_can_report_zero_without_hiding_work(evidence):
    root, manifest, python, frontend, native = evidence
    for identity, row in python['files'].items():
        (root / identity).write_text('# No executable declarations.\n')
        row['summary'].update(covered_lines=0, num_statements=0)
        row['executed_lines'] = []
    for report in (frontend, native):
        for identity, row in report['files'].items():
            (root / identity).write_text('// No executable declarations.\n')
            row['lines'] = {'covered': 0, 'total': 0}
    manifest['sources'] = gate.source_files(root)
    for report in (frontend, native):
        for identity, row in report['files'].items():
            row['sha256'] = manifest['sources'][identity]
    manifest['verification_inputs'] = gate.input_files(root)
    assert gate.evaluate(*evidence)['passed']


@pytest.mark.parametrize("mutation,expected", [
    ("branch_disabled", "branch instrumentation"),
    ("missing_file", "every current production file"),
    ("excluded", "Protocol declarations"),
    ("inflated_counts", "recorded execution"),
    ("missing_frontend", "frontend coverage is required"),
    ("approximate", "Approximate"),
    ("js_missing", "include every"),
    ("js_stale", "source hash differs"),
    ("changed_source", "Source files changed"),
])
def test_gate_rejects_untrustworthy_evidence(evidence, mutation, expected):
    root, manifest, python, frontend, native = evidence
    key = "backend/src/round_robin/api/example.py"
    if mutation == "branch_disabled":
        python["meta"]["branch_coverage"] = False
    elif mutation == "missing_file":
        python["files"].pop(key)
    elif mutation == "excluded":
        python["files"][key].update(excluded_lines=[1])
        python["files"][key]["summary"]["excluded_lines"] = 1
    elif mutation == "inflated_counts":
        python["files"][key]["executed_lines"] = []
    elif mutation == "missing_frontend":
        frontend = None
    elif mutation == "approximate":
        frontend["kind"] = "source_mapped_browser_v8"
    elif mutation == "js_missing":
        frontend["files"] = {}
    elif mutation == "js_stale":
        frontend["files"]["frontend/app/page.tsx"]["sha256"] = "changed"
    elif mutation == "changed_source":
        (root / key).write_text("value = 2\n")
    result = gate.evaluate(root, manifest, python, frontend, native)
    assert not result["passed"] and any(expected in error for error in result["failures"])


@pytest.mark.parametrize("value", [-1, 0.5, True, "1"])
def test_counts_reject_negative_fractional_or_coerced_values(value):
    with pytest.raises(ValueError):
        gate._metric(value, 1)
    with pytest.raises(ValueError):
        gate._metric(2, 1)


@pytest.mark.parametrize("floor", [True, -1, 100, "95"])
def test_exclusive_floor_is_validated(evidence, floor):
    with pytest.raises(ValueError):
        gate.evaluate(*evidence, threshold=floor)


def test_path_scope_absolute_paths_and_empty_inventory(evidence, tmp_path):
    root, manifest, python, frontend, native = evidence
    assert gate._relative(str(root / "frontend/app/page.tsx"), root) == "frontend/app/page.tsx"
    with pytest.raises(ValueError):
        gate._relative("../elsewhere.py", root)
    with pytest.raises(ValueError):
        gate._relative(str(root.parent / "elsewhere.py"), root)
    result = gate.evaluate(root / "empty", {"sources": {}, "verification_inputs": {}}, {"meta": {"branch_coverage": True}, "files": {}}, {"kind": "istanbul_source", "files": {}}, {"kind": "istanbul_source", "files": {}})
    assert not result["passed"] and "inventory is empty" in result["failures"][0]


def test_protocol_omissions_cannot_hide_executable_methods(tmp_path):
    file = tmp_path / "protocol.py"
    file.write_text('''from typing import Protocol
class Transport(Protocol):
    """An interface, with no executable implementation."""
    value: int
    def send(self):
        """Contract."""
        ...
    async def read(self):
        pass
class RealWork(Protocol):
    def send(self):
        return "must be measured"
class Hidden(Protocol):
    @staticmethod
    def send(): ...
''')
    assert gate.protocol_declarations(file) == set(range(2, 10))
    assert gate.service_for("backend/migrations/env.py") == "migrations"
    assert gate.service_for("backend/src/round_robin/app.py") == "application"
    assert gate.service_for("scripts/verify.py") == "tooling"
    assert gate.service_for("experiments/runner.py") == "experiments"


def test_cli_captures_and_validates_then_replaces_stale_success_on_failure(evidence, monkeypatch):
    root, _, python, frontend, native = evidence
    monkeypatch.setattr(gate, "ROOT", root)
    manifest = root / "manifest.json"
    assert gate.main(["--manifest", str(manifest), "--capture"]) == 0
    files = []
    for name, value in (("python", python), ("frontend", frontend), ("native", native)):
        path = root / (name + ".json")
        path.write_text(json.dumps(value))
        files.extend(["--" + name, str(path)])
    output = root / "result.json"
    args = ["--manifest", str(manifest), "--output", str(output), *files]
    assert gate.main(args) == 0
    assert json.loads(output.read_text())["passed"]
    (root / "python.json").write_text("invalid json PRIVATE_SENTINEL")
    assert gate.main(args) == 1
    assert not json.loads(output.read_text())["passed"]
    assert "PRIVATE_SENTINEL" not in output.read_text()
    with pytest.raises(SystemExit):
        gate.main(["--manifest", str(manifest)])


@pytest.mark.parametrize("path", [
    "backend/tests/test_guard.py", "tests/e2e/security.spec.ts", "frontend/tests/unit.test.tsx",
    "contracts/application.openapi.json", "packages/api-client/src/application.ts", "experiments/fixtures/tasks.json",
    ".github/workflows/verification.yml", "frontend/public/icon.svg", ".coveragerc", "uv.lock", "pnpm-lock.yaml",
    "pyproject.toml", "package.json", "pnpm-workspace.yaml", "playwright.config.ts", "compose.yaml",
    "alembic.ini", ".python-version", ".dockerignore", "backend/migrations/script.py.mako",
    "scripts/native-renderer.Dockerfile", "scripts/pdf-qa.Dockerfile", "scripts/frontend_instrumentation.cjs",
    "frontend/vitest.config.mts", "frontend/next.config.ts", "frontend/tsconfig.json", "frontend/app/globals.css",
])
def test_verification_input_changes_additions_and_removals_invalidate_attestation(evidence, path):
    root, manifest, *reports = evidence
    file = root / path
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text("first fixture input")
    assert not gate.evaluate(*evidence)["passed"]
    manifest["verification_inputs"] = gate.input_files(root)
    assert gate.evaluate(*evidence)["passed"]
    file.write_text("changed fixture input")
    result = gate.evaluate(*evidence)
    assert not result["passed"] and not result["verification_inputs_verified"]
    manifest["verification_inputs"] = gate.input_files(root)
    file.unlink()
    assert not gate.evaluate(*evidence)["passed"]


def test_documentation_results_and_generated_build_intermediates_are_not_verification_inputs(evidence):
    root, manifest, *_ = evidence
    for name in ("docs/audit.md", ".local/results.json", "test-results/report.json", "playwright-report/index.html",
                 "frontend/next-env.d.ts", "frontend/tsconfig.tsbuildinfo", "frontend/.next/build.json",
                 "backend/tests/__pycache__/test.cpython-312.pyc", "frontend/tests/test-results/last.json"):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("generated result")
    assert gate.input_files(root) == manifest["verification_inputs"]
    assert gate.evaluate(*evidence)["passed"]


@pytest.mark.parametrize("collection, values", [
    ("executed_lines", [1, 1]), ("missing_lines", [2, 2]), ("excluded_lines", [3, 3]),
    ("executed_lines", [True]), ("missing_lines", [0]), ("executed_lines", "1"),
    ("executed_branches", [[1, -1], [1, -1]]), ("missing_branches", [[1, 2], [1, 2]]),
    ("executed_branches", [[1]]), ("missing_branches", [[1, True]]),
    ("executed_branches", [[0, 1]]), ("executed_branches", [[-1, 2]]),
])
def test_location_arrays_cannot_inflate_counts_with_duplicates_or_invalid_shapes(evidence, collection, values):
    evidence[2]["files"]["backend/src/round_robin/api/example.py"][collection] = values
    with pytest.raises(ValueError):
        gate.evaluate(*evidence)


@pytest.mark.parametrize("kind", ["line", "branch", "excluded"])
def test_covered_missing_and_excluded_sets_are_disjoint(evidence, kind):
    row = evidence[2]["files"]["backend/src/round_robin/api/example.py"]
    if kind == "line":
        row["missing_lines"] = [1]
        row["summary"]["num_statements"] = 2
    elif kind == "branch":
        row["executed_branches"] = row["missing_branches"] = [[1, -1]]
        row["summary"].update(covered_branches=1, num_branches=2)
    else:
        row["excluded_lines"] = [1]
        row["summary"]["excluded_lines"] = 1
    result = gate.evaluate(*evidence)
    assert not result["passed"] and any("disjoint" in message for message in result["failures"])


@pytest.mark.parametrize("report_index", [2, 3, 4])
def test_duplicate_normalized_file_paths_cannot_overwrite_evidence(evidence, report_index):
    root = evidence[0]
    report = evidence[report_index]
    path, data = next(iter(report["files"].items()))
    report["files"][str(root/path)] = deepcopy(data)
    with pytest.raises(ValueError, match="Duplicate normalized"):
        gate.evaluate(*evidence)


@pytest.mark.parametrize("report, key, value", [
    ("manifest", None, []), ("python", None, None), ("python", "meta", []),
    ("python", "files", []), ("python", "row", None), ("python", "summary", []),
    ("frontend", None, []), ("frontend", "files", []), ("frontend", "lines", None),
    ("native", "branches", []),
])
def test_malformed_nested_json_always_replaces_stale_success_with_sanitized_failure(evidence, monkeypatch, report, key, value, capsys):
    root, manifest, python, frontend, native = evidence
    monkeypatch.setattr(gate, "ROOT", root)
    reports = dict(manifest=manifest, python=python, frontend=frontend, native=native)
    if key is None:
        reports[report] = value
    elif key in {"meta", "files"}:
        reports[report][key] = value
    else:
        identity = next(iter(reports[report]["files"]))
        if key == "row": reports[report]["files"][identity] = value
        else: reports[report]["files"][identity][key] = value
    args = []
    for name, content in reports.items():
        path = root / (name + ".json")
        path.write_text(json.dumps(content))
        args.extend(["--" + name, str(path)])
    output = root / "decision.json"
    output.write_text('{"passed":true,"private":"PRIVATE_SENTINEL"}')
    assert gate.main([*args, "--output", str(output)]) == 1
    assert not json.loads(output.read_text())["passed"]
    assert "PRIVATE_SENTINEL" not in output.read_text() + capsys.readouterr().out


def test_empty_or_nonstring_path_and_boolean_exclusion_counts_are_rejected(evidence):
    for path in (None, "", 3):
        with pytest.raises(ValueError): gate._relative(path, evidence[0])
    evidence[2]["files"]["backend/src/round_robin/api/example.py"]["summary"]["excluded_lines"] = False
    with pytest.raises(ValueError): gate.evaluate(*evidence)


def test_manifest_cli_entrypoint_binds_actual_source_and_verification_inputs(tmp_path, monkeypatch):
    import runpy
    import sys
    manifest = tmp_path / "manifest.json"
    monkeypatch.setattr(sys, "argv", ["coverage_gate", "--manifest", str(manifest), "--capture"])
    with pytest.raises(SystemExit) as result:
        runpy.run_module("scripts.coverage_gate", run_name="__main__")
    assert result.value.code == 0
    captured = json.loads(manifest.read_text())
    assert captured["sources"] == gate.source_files(gate.ROOT)
    assert captured["verification_inputs"] == gate.input_files(gate.ROOT)


def test_real_coverage_protocol_omissions_allow_trailing_trivia_but_not_executable_code(evidence):
    import subprocess
    import sys
    root, manifest, python, *_ = evidence
    identity = "backend/src/round_robin/api/example.py"
    file = root / identity
    file.write_text('''from typing import Protocol
class Transport(Protocol):
    def send(self): ...

# A nonexecutable explanatory comment.

value = 1
''')
    manifest["sources"] = gate.source_files(root)
    assert gate.protocol_declarations(file) == {2, 3, 4, 5, 6}
    report = root / "actual-coverage.json"
    code = """import coverage, sys
from pathlib import Path
file = Path(sys.argv[1])
cov = coverage.Coverage(branch=True, config_file=False, data_file=None, source=[str(file.parent)])
cov.start()
exec(compile(file.read_text(), str(file), 'exec'), {})
cov.stop()
cov.json_report(outfile=sys.argv[2])
"""
    subprocess.run([sys.executable, "-c", code, str(file), str(report)], check=True, capture_output=True, text=True)
    row = next(value for path, value in json.loads(report.read_text())["files"].items() if path.endswith("api/example.py"))
    assert {4, 5}.issubset(row["excluded_lines"])
    python["files"][identity] = row
    assert gate.evaluate(*evidence)["passed"]
    # A forged report may not extend that omission to the following assignment.
    row["excluded_lines"].append(7)
    row["summary"]["excluded_lines"] += 1
    result = gate.evaluate(*evidence)
    assert not result["passed"] and any("Protocol declarations" in item for item in result["failures"])
