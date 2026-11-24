"""Check exact failure IDs and named implementation slices without passing gates."""

from __future__ import annotations

import argparse
import ast
from collections import Counter
import json
from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "backend" / "tests" / "scenarios" / "manifest.json"
REQUIRED_FIELDS = {"scenario_id", "phase", "implementation_paths", "test_paths", "test_profile",
                   "expected_invariants", "fixture_ids", "evidence_artifact", "status"}


def check_manifest(path: Path = MANIFEST) -> dict:
    manifest = json.loads(path.read_text())
    scenarios = manifest["scenarios"]
    authoritative = set(re.findall(r"^\| ((?:CMD|PRV|JOB|LIM|EVD|RPT|OPS|VIS)-\d{2}) \|",
                                    (ROOT / "docs" / "FAILURE_HANDLING.md").read_text(), re.MULTILINE))
    ids = [scenario["scenario_id"] for scenario in scenarios]
    if len(ids) != 100 or len(set(ids)) != 100 or set(ids) != authoritative:
        raise ValueError("Manifest must preserve every one of the exact 100 failure IDs")
    if sum(not sid.startswith("VIS") for sid in ids) != 88:
        raise ValueError("Expected 88 core and 12 visual requirements")
    if manifest.get("phase_passed") is not False:
        raise ValueError("Implementation slice evidence cannot mark a phase passed")
    for scenario in scenarios:
        if not REQUIRED_FIELDS <= scenario.keys() or not scenario["expected_invariants"]:
            raise ValueError(f"Incomplete scenario entry: {scenario['scenario_id']}")
        if scenario["status"] not in {"pending", "partial_offline_assertions"}:
            raise ValueError("The inventory records pending/partial obligations, not scenario completion")
        if not scenario.get("blocking_dependency"):
            raise ValueError("Every incomplete scenario must state its blocking dependency")
        if scenario["status"] == "partial_offline_assertions":
            for field in ("implementation_paths", "test_paths", "fixture_ids", "evidence_artifact"):
                if not scenario[field]:
                    raise ValueError(f"Partial assertions need {field}: {scenario['scenario_id']}")
        # TypeScript browser preparation uses a file path without a pytest
        # selector. The associated prose identifies the limited assertion;
        # these references never promote later obligations to implemented.
        targets = (scenario["implementation_paths"] + scenario["test_paths"]
                   + scenario.get("preparation_implementation_paths", [])
                   + scenario.get("preparation_test_paths", [])
                   + scenario.get("browser_test_paths", []))
        if scenario.get("preparation_test_paths") and not scenario.get("preparation_scope_note"):
            raise ValueError("Prototype references must state their incomplete preparation scope")
        for target in targets:
            path, _, selector = target.partition("::")
            source = ROOT / path
            if not source.is_file():
                raise ValueError(f"Missing implementation/test path: {target}")
            if selector:
                if source.suffix != ".py":
                    raise ValueError("Non-Python preparation tests use file paths without pytest selectors")
                if selector not in {node.name for node in ast.walk(ast.parse(source.read_text()))
                                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}:
                    raise ValueError(f"Missing test selector: {target}")
    return manifest


def render_summary(manifest: dict) -> str:
    scenarios = manifest["scenarios"]
    counts = Counter(s["status"] for s in scenarios)
    lines = ["# Scenario implementation inventory", "",
             "88 core and 12 visual requirements retained. No scenario or phase is declared passed.", "",
             f"Pending: {counts['pending']}. Partial implementation/test slices: {counts['partial_offline_assertions']}.", "",
             "| Earliest phase | Requirements | Pending | Partial implementation/test slices |",
             "| --- | ---: | ---: | ---: |"]
    for phase in ("0B", "1", "2", "3", "4", "5", "6", "7", "8"):
        group = [s for s in scenarios if s["phase"] == phase]
        status = Counter(s["status"] for s in group)
        lines.append(f"| {phase} | {len(group)} | {status['pending']} | {status['partial_offline_assertions']} |")
    lines += ["", "Partial entries cover only their named unit, browser, mocked-provider and actual-process assertions.",
              "Full scenario invariants, remaining fault combinations, real-provider/human validation and phase",
              "gates remain pending. The 14 core combined-failure cases",
              "remain obligations in FAILURE_HANDLING.md and are not counted as new scenario IDs.", "",
              f"{sum(bool(s.get('preparation_test_paths')) for s in scenarios)} requirements also retain their original fictional-prototype references.",
              "Those preparation references remain distinct from later durable API/process/browser evidence.", ""]
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Validate the manifest before summarizing (always enforced)")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    summary = render_summary(check_manifest())
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(summary)
    else:
        print(summary, end="")


if __name__ == "__main__":
    main()
