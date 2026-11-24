"""Fail closed unless every first-party service exceeds its coverage floor.

Read coverage.py JSON plus Istanbul source coverage. Every current production
file must be represented; aggregate percentages cannot hide a weak service.
The manifest binds reports to the source tree captured before verification.
"""
from __future__ import annotations

import argparse
import ast
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[1]
PYTHON_ROOTS = ("backend/src/round_robin", "backend/migrations", "experiments", "scripts")
FRONTEND_ROOT = "frontend/app"
INPUT_DIRECTORIES = ("backend/tests", "tests", "frontend/tests", "contracts", "packages/api-client/src", "experiments/fixtures", ".github/workflows", "frontend/public")
INPUT_NAMES = (".coveragerc", "pyproject.toml", "uv.lock", "package.json", "pnpm-lock.yaml", "pnpm-workspace.yaml", "playwright.config.ts", "compose.yaml", "alembic.ini", ".python-version", ".dockerignore", "backend/migrations/script.py.mako")


def input_files(root: Path) -> dict[str, str]:
    """Bind test/configuration inputs without treating them as executable targets.

    Documentation, result directories, caches and generated build intermediates
    are deliberately outside this inventory. Generated public contracts are in.
    """
    paths = {root / name for name in INPUT_NAMES}
    paths.update(path for directory in INPUT_DIRECTORIES for path in (root / directory).rglob("*"))
    paths.update(path for path in (root / "scripts").rglob("*") if path.suffix in {".js", ".mjs", ".cjs", ".Dockerfile"})
    paths.update(path for path in (root / "frontend").glob("*") if path.suffix in {".json", ".ts", ".mts", ".mjs", ".cjs"} and path.name != "next-env.d.ts")
    paths.update((root / FRONTEND_ROOT).rglob("*.css"))
    return {path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(paths) if path.is_file() and path.suffix not in {".pyc", ".pyo", ".tsbuildinfo"}
            and not {"__pycache__", ".pytest_cache", "test-results", "playwright-report"}.intersection(path.relative_to(root).parts)}


def source_files(root: Path) -> dict[str, str]:
    paths = [path for directory in PYTHON_ROOTS for path in (root / directory).rglob("*.py")]
    paths += [path for path in (root / FRONTEND_ROOT).rglob("*") if path.suffix in {".ts", ".tsx"}]
    paths += [root / "scripts/native-renderer.mjs"]
    return {path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(paths) if path.is_file() and "__pycache__" not in path.parts}


def service_for(path: str) -> str:
    if path.startswith("backend/src/round_robin/"):
        suffix = path.removeprefix("backend/src/round_robin/").split("/")
        return suffix[0] if len(suffix) > 1 else "application"
    if path.startswith("backend/migrations/"):
        return "migrations"
    if path.startswith("frontend/app/"):
        return "frontend"
    if path == "scripts/native-renderer.mjs":
        return "native_renderer"
    return "tooling" if path.startswith("scripts/") else "experiments"


def _count(value) -> int:
    if type(value) is not int or value < 0:
        raise ValueError("Coverage counts must be nonnegative integers")
    return value


def _metric(covered, total) -> tuple[int, int]:
    covered, total = _count(covered), _count(total)
    if covered > total:
        raise ValueError("Covered counts cannot exceed their denominator")
    return covered, total


def _relative(path: str, root: Path) -> str:
    if not isinstance(path, str) or not path:
        raise ValueError("Coverage paths must be nonempty strings")
    candidate = Path(path)
    if candidate.is_absolute():
        return candidate.resolve().relative_to(root.resolve()).as_posix()
    if ".." in candidate.parts:
        raise ValueError("Coverage paths cannot escape the source tree")
    return candidate.as_posix()


def _object(value) -> dict:
    if not isinstance(value, dict):
        raise ValueError("Coverage objects must be JSON mappings")
    return value


def _files(value, root: Path) -> dict:
    result = {}
    for path, data in _object(value).items():
        normalized = _relative(path, root)
        if normalized in result:
            raise ValueError("Duplicate normalized coverage path")
        result[normalized] = _object(data)
    return result


def _locations(value, *, branches=False) -> set:
    if not isinstance(value, list):
        raise ValueError("Coverage locations must be arrays")
    result = set()
    for item in value:
        if branches:
            if not isinstance(item, list) or len(item) != 2 or any(type(line) is not int or line == 0 for line in item) or item[0] < 0:
                raise ValueError("Coverage branches require source/target integer pairs")
            item = tuple(item)
        elif type(item) is not int or item <= 0:
            raise ValueError("Coverage lines require positive integers")
        if item in result:
            raise ValueError("Duplicate coverage location")
        result.add(item)
    return result


def protocol_declarations(path: Path) -> set[int]:
    """Allow coverage.py's built-in omission of nonexecutable Protocol stubs."""
    def declaration(node):
        if isinstance(node, (ast.AnnAssign, ast.Pass)):
            return True
        if isinstance(node, ast.Expr):
            return isinstance(node.value, ast.Constant) and (isinstance(node.value.value, str) or node.value.value is Ellipsis)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return not node.decorator_list and all(declaration(item) for item in node.body)
        return False
    allowed = set()
    source = path.read_text()
    source_lines = source.splitlines()
    for node in ast.walk(ast.parse(source)):
        if (isinstance(node, ast.ClassDef) and not node.decorator_list
                and any(isinstance(base, ast.Name) and base.id == "Protocol" for base in node.bases)
                and all(declaration(item) for item in node.body)):
            end = node.end_lineno
            # coverage.py's built-in Protocol omission includes trailing blank
            # and comment-only lines up to the next statement. They are outside
            # the AST node but contain no executable work to hide.
            while end < len(source_lines) and (not source_lines[end].strip() or source_lines[end].lstrip().startswith("#")):
                end += 1
            allowed.update(range(node.lineno, end + 1))
    return allowed


def evaluate(root: Path, manifest: dict, python: dict, frontend: dict | None = None, native: dict | None = None, *, threshold: int = 95) -> dict:
    if type(threshold) is not int or not 0 <= threshold < 100:
        raise ValueError("Exclusive coverage floor must be an integer from zero to ninety-nine")
    manifest, python = _object(manifest), _object(python)
    current, inputs = source_files(root), input_files(root)
    failures = []
    if manifest.get("verification_inputs") != inputs:
        failures.append("Verification inputs changed, appeared or disappeared after the manifest was captured")
    if manifest.get("sources") != current:
        failures.append("Source files changed, appeared or disappeared after the verification manifest was captured")
    if _object(python.get("meta", {})).get("branch_coverage") is not True:
        failures.append("Python branch instrumentation is required")
    python_files = _files(python.get("files", {}), root)
    expected_python = {path for path in current if path.endswith(".py")}
    if not expected_python:
        failures.append("Production Python inventory is empty")
    if set(python_files) != expected_python:
        failures.append("Python report must contain every current production file and no excluded or unrelated files")
    metrics = {}
    from coverage.parser import PythonParser
    for path in sorted(expected_python & set(python_files)):
        value = python_files[path]
        summary = _object(value["summary"])
        lines = _metric(summary["covered_lines"], summary["num_statements"])
        branches = _metric(summary["covered_branches"], summary["num_branches"])
        excluded = _locations(value.get("excluded_lines", []))
        executed, missing = _locations(value["executed_lines"]), _locations(value["missing_lines"])
        parser = PythonParser(filename=str(root / path))
        parser.parse_source()
        expected_statements = parser.statements - excluded
        if executed | missing != expected_statements:
            failures.append(f"Coverage locations do not match executable source inventory: {path}")
        executed_branches, missing_branches = _locations(value["executed_branches"], branches=True), _locations(value["missing_branches"], branches=True)
        branch_lines = {line for line, exits in parser.exit_counts().items() if exits > 1 and line in expected_statements}
        expected_branches = {arc for arc in parser.arcs() if arc[0] in branch_lines}
        if executed_branches | missing_branches != expected_branches:
            failures.append(f"Coverage branches do not match executable source inventory: {path}")
        if executed & missing or executed_branches & missing_branches or excluded & (executed | missing):
            failures.append(f"Covered, missing and excluded locations must be disjoint: {path}")
        if _count(summary.get("excluded_lines", 0)) != len(excluded) or excluded - protocol_declarations(root / path):
            failures.append(f"Only nonexecutable Protocol declarations may be omitted: {path}")
        if (len(executed) != lines[0] or len(missing) + lines[0] != lines[1]
                or len(executed_branches) != branches[0] or len(missing_branches) + branches[0] != branches[1]):
            failures.append(f"Coverage counts do not match recorded execution: {path}")
        metrics[path] = (lines, branches)
    js_paths = sorted(path for path in current if path.endswith(('.ts', '.tsx', '.mjs')))
    inventory_result = subprocess.run(
        ['node', str(Path(__file__).with_name('coverage_inventory.mjs'))],
        input=json.dumps([str((root / path).resolve()) for path in js_paths]),
        capture_output=True, text=True, check=True,
    )
    js_inventory = json.loads(inventory_result.stdout)
    for name, report, expected in (
        ("frontend", frontend, {path for path in current if path.startswith(FRONTEND_ROOT + "/")}),
        ("native_renderer", native, {path for path in current if path == "scripts/native-renderer.mjs"}),
    ):
        if report is None:
            failures.append(f"Source-instrumented {name} coverage is required")
            continue
        report = _object(report)
        if report.get("kind") != "istanbul_source":
            failures.append(f"Approximate compiled/source-map {name} coverage cannot satisfy the gate")
            continue
        files = _files(report.get("files", {}), root)
        if set(files) != expected:
            failures.append(f"{name} report must include every current production file")
        for path in sorted(expected & set(files)):
            value = files[path]
            if value.get("sha256") != current[path]:
                failures.append(f"Coverage source hash differs: {path}")
            line_counts, branch_counts = _object(value["lines"]), _object(value["branches"])
            lines = _metric(line_counts["covered"], line_counts["total"])
            branches = _metric(branch_counts["covered"], branch_counts["total"])
            expected_counts = js_inventory[str((root / path).resolve())]
            if lines[1] != expected_counts['lines'] or branches[1] != expected_counts['branches']:
                failures.append(f"Coverage totals do not match executable source inventory: {path}")
            metrics[path] = (lines, branches)
    services = {}
    for path, (lines, branches) in metrics.items():
        service = service_for(path)
        item = services.setdefault(service, {"files": [], "lines": {"covered": 0, "total": 0}, "branches": {"covered": 0, "total": 0}})
        item["files"].append(path)
        for kind, (covered, total) in (("lines", lines), ("branches", branches)):
            item[kind]["covered"] += covered
            item[kind]["total"] += total
    for name, value in services.items():
        for kind in ("lines", "branches"):
            counts = value[kind]
            counts["percent"] = round(100 * counts["covered"] / counts["total"], 4) if counts["total"] else 100.0
            if counts["total"] and 100 * counts["covered"] <= threshold * counts["total"]:
                failures.append(f"{name} {kind} must exceed {threshold}% ({counts['covered']}/{counts['total']})")
    return {"recorded_at_utc": datetime.now(timezone.utc).isoformat(), "passed": not failures,
            "threshold_exclusive_percent": threshold, "services": services, "failures": failures,
            "source_manifest_verified": manifest.get("sources") == current,
            "verification_inputs_verified": manifest.get("verification_inputs") == inputs,
            "limits": "Execution coverage does not prove correctness or exhaust all concurrency interleavings. Generated types, dependencies, styles and nonexecutable Protocol declarations are outside executable source counts."}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--capture", action="store_true", help="Capture current source hashes before running tests")
    parser.add_argument("--python", type=Path)
    parser.add_argument("--frontend", type=Path)
    parser.add_argument("--native", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    if args.capture:
        args.manifest.parent.mkdir(parents=True, exist_ok=True)
        args.manifest.write_text(json.dumps({"sources": source_files(ROOT), "verification_inputs": input_files(ROOT)}, indent=2) + "\n")
        return 0
    if not args.python or not args.output:
        parser.error("--python and --output are required when checking coverage")
    read = lambda path: json.loads(path.read_text()) if path else None
    try:
        result = evaluate(ROOT, read(args.manifest), read(args.python), read(args.frontend), read(args.native))
    except (OSError, ValueError, KeyError, TypeError, AttributeError, SyntaxError, RecursionError, subprocess.SubprocessError) as error:
        result = {"passed": False, "services": {}, "failures": [f"Coverage input could not be validated ({type(error).__name__})"]}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    for name, metrics in sorted(result["services"].items()):
        print(f"{name}: lines {metrics['lines']['percent']:.2f}%; branches {metrics['branches']['percent']:.2f}%")
    for error in result["failures"]:
        print("FAIL " + error)
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
