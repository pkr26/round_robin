"""Run fresh offline, real-service and browser checks with strict service coverage."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
INTERRUPT_SIGNALS = (signal.SIGINT, signal.SIGTERM)
STOP_STAGES = ((signal.SIGINT, 45.0), (signal.SIGTERM, 5.0), (signal.SIGKILL, 5.0))


def _interrupt(signum, frame):
    raise KeyboardInterrupt


def _group_exists(process) -> bool:
    # Reap the direct child even when its launcher exited before descendants.
    process.poll()
    try:
        os.killpg(process.pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Darwin can report EPERM while a just-exited group contains only
        # zombies. It still exists: keep polling/reaping, never assume gone.
        return True
    return True


def _wait_group(process, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while _group_exists(process):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(0.05, remaining))
    return True


def _stop_process_group(process) -> None:
    # SIGINT lets verify.py run its own finally block, including cleanup of
    # service sessions and disposable infrastructure outside this group.
    for signum, timeout in STOP_STAGES:
        if not _group_exists(process):
            return
        try:
            os.killpg(process.pid, signum)
        except ProcessLookupError:
            return
        except PermissionError:
            # A zombie-only group may disappear on the next reap. A genuine
            # permission denial must still fail unless disappearance is proven.
            if _wait_group(process, timeout):
                return
            raise
        if _wait_group(process, timeout):
            return
    raise RuntimeError("Owned verification process group did not stop")


def _run_command(command, **kwargs):
    interrupted = False

    def defer_interrupt(signum, frame):
        nonlocal interrupted
        interrupted = True

    previous = {signum: signal.signal(signum, defer_interrupt) for signum in INTERRUPT_SIGNALS}
    process = None
    try:
        # Keep an interrupt between fork and assignment from losing ownership
        # of a newly started command. Do not block signals: a child inherits
        # its parent's signal mask across exec and must receive our cleanup.
        process = subprocess.Popen(command, start_new_session=True, **kwargs)
        for signum in INTERRUPT_SIGNALS:
            signal.signal(signum, _interrupt)
        if interrupted:
            raise KeyboardInterrupt
        returncode = process.wait()
        if _group_exists(process):
            raise RuntimeError("Verification launcher exited before its process group")
        return subprocess.CompletedProcess(command, returncode)
    finally:
        # A second Ctrl-C must not abandon descendants midway through cleanup.
        for signum in INTERRUPT_SIGNALS:
            signal.signal(signum, signal.SIG_IGN)
        try:
            if process is not None:
                _stop_process_group(process)
        finally:
            for signum, handler in previous.items():
                signal.signal(signum, handler)


def run_audit(output: Path) -> int:
    output = output.resolve()
    capture_root = ROOT / ".local/exhaustive-audit"
    captures = [capture_root / name for name in ("frontend-source", "native-source")]
    if any(output == path.resolve() or path.resolve() in output.parents for path in captures):
        raise ValueError("Audit output must be separate from disposable capture directories")
    # Refuse old evidence. Even setup failure after this point leaves an
    # explicitly failed record; a killed run never inherits a successful flag.
    output.mkdir(parents=True, exist_ok=False)
    record = {
        "recorded_at_utc": datetime.now(timezone.utc).isoformat(),
        "passed": False, "checks": [], "audit_error": "Audit did not complete",
        "coverage_floor": "strictly greater than 95% lines and branches in every service",
        "live_provider_calls": 0,
    }
    report_path = output / "audit-checks.json"
    report_path.write_text(json.dumps(record, indent=2) + "\n")
    descriptor = None
    try:
        lock_path = capture_root / ".audit.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("Another fresh audit owns the coverage captures") from None
        for directory in captures:
            if directory.is_symlink():
                raise ValueError("Capture directory must not be a symbolic link")
            if directory.exists():
                shutil.rmtree(directory)
        environment = {**os.environ,
            "COVERAGE_FILE": str(output / ".coverage"),
            "COVERAGE_RCFILE": str(ROOT / ".coveragerc"),
            "ROUND_ROBIN_SOURCE_COVERAGE": "1",
            "ROUND_ROBIN_TEST_DOCKER_FAULTS": "1",
        }
        manifest = output / "source-manifest.json"
        commands = [
            ["uv", "run", "python", "scripts/coverage_gate.py", "--manifest", str(manifest), "--capture"],
            ["uv", "run", "coverage", "run", "scripts/verify.py", "--services", "--native", "--output", str(output / "checks.json")],
            ["pnpm", "--filter", "@round-robin/web", "test"],
            ["node", "--experimental-vm-modules", "--test", "scripts/native_renderer_coverage.test.mjs"],
            ["node", "scripts/frontend_source_coverage.mjs", "--check"],
            ["uv", "run", "coverage", "combine", "--keep"],
            ["uv", "run", "coverage", "json", "--keep-combined", "-o", str(output / "python-coverage.json")],
            ["uv", "run", "python", "scripts/coverage_gate.py", "--manifest", str(manifest),
                "--python", str(output / "python-coverage.json"),
                "--frontend", str(ROOT / ".local/exhaustive-audit/frontend-source/summary.json"),
                "--native", str(ROOT / ".local/exhaustive-audit/native-source/summary.json"),
                "--output", str(output / "service-coverage.json")],
        ]
        for index, command in enumerate(commands):
            log = output / f"step-{index + 1:02d}.log"
            try:
                with log.open("w") as handle:
                    result = _run_command(command, cwd=ROOT, env=environment, stdout=handle, stderr=subprocess.STDOUT)
            except BaseException:
                record["checks"].append({"command": command, "exit_code": None, "log": log.name})
                raise
            record["checks"].append({"command": command, "exit_code": result.returncode, "log": log.name})
            print(f"{'PASS' if result.returncode == 0 else 'FAIL'} {' '.join(command)}", flush=True)
            if result.returncode:
                raise RuntimeError("A verification stage failed")
        # Exit codes alone are insufficient: require the current invocation's
        # affirmative final records, and never accept missing/malformed output.
        checks = json.loads((output / "checks.json").read_text())
        coverage = json.loads((output / "service-coverage.json").read_text())
        if (not isinstance(checks, dict) or checks.get("all_checks_passed") is not True
                or not isinstance(coverage, dict) or coverage.get("passed") is not True
                or coverage.get("source_manifest_verified") is not True
                or coverage.get("verification_inputs_verified") is not True):
            raise ValueError("Final verification records do not attest success")
        for directory in captures:
            source = directory / "summary.json"
            shutil.copyfile(source, output / f"{directory.name}.json")
        record.pop("audit_error")
        record["passed"] = True
    except (Exception, KeyboardInterrupt) as error:
        # Error strings may contain command credentials or private paths.
        record["passed"] = False
        record["audit_error"] = f"Audit could not complete ({type(error).__name__})"
    finally:
        # Publish the final failed/successful record while captures are still
        # locked, after all owned command-group cleanup has finished.
        try:
            report_path.write_text(json.dumps(record, indent=2) + "\n")
        finally:
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    record["passed"] = False
                    record["audit_error"] = "Audit lock cleanup failed"
                    report_path.write_text(json.dumps(record, indent=2) + "\n")
    return 0 if record["passed"] else 1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / ".local/exhaustive-audit" / datetime.now(timezone.utc).strftime("run-%Y%m%dT%H%M%S%fZ"))
    args = parser.parse_args(argv)
    try:
        return run_audit(args.output)
    except (OSError, RuntimeError, ValueError):
        print("Audit setup failed: use a new output directory, stop overlapping audits, and inspect local permissions.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
