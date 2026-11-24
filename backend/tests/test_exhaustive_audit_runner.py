"""Fresh audit orchestration cannot reuse stale coverage or hide failed checks."""
import fcntl
import json
import os
from pathlib import Path
import runpy
import signal
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from scripts import audit


@pytest.fixture
def harness(tmp_path, monkeypatch):
    root = tmp_path / "workspace"
    root.mkdir()
    monkeypatch.setattr(audit, "ROOT", root)
    state = {"calls": [], "fail_step": None, "root": root, "output": tmp_path / "new-evidence"}
    for name in ("frontend-source", "native-source"):
        directory = root / ".local/exhaustive-audit" / name
        directory.mkdir(parents=True)
        (directory / "stale.json").write_text("STALE_SUCCESS_SENTINEL")
    def run(command, **kwargs):
        state["calls"].append(command)
        assert kwargs["env"]["ROUND_ROBIN_SOURCE_COVERAGE"] == "1"
        assert kwargs["env"]["COVERAGE_FILE"].startswith(str(state["output"]))
        assert not list(root.rglob("stale.json"))
        if "scripts/frontend_source_coverage.mjs" in command or "scripts/native_renderer_coverage.test.mjs" in command:
            name = "frontend-source" if "scripts/frontend_source_coverage.mjs" in command else "native-source"
            directory = root / ".local/exhaustive-audit" / name
            directory.mkdir()
            (directory / "summary.json").write_text('{"fresh":true}')
        if "scripts/verify.py" in command:
            (state["output"] / "checks.json").write_text('{"all_checks_passed":true}')
        if "scripts/coverage_gate.py" in command and "--output" in command:
            (state["output"] / "service-coverage.json").write_text('{"passed":true,"source_manifest_verified":true,"verification_inputs_verified":true}')
        return SimpleNamespace(returncode=1 if len(state["calls"]) == state["fail_step"] else 0)
    monkeypatch.setattr(audit, "_run_command", run)
    return state


@pytest.mark.parametrize("fail_step", [None, 1, 2, 3, 4, 5, 6, 7, 8])
def test_audit_requires_every_stage_and_never_merges_previous_captures(harness, fail_step):
    harness["fail_step"] = fail_step
    assert audit.run_audit(harness["output"]) == (1 if fail_step else 0)
    report = json.loads((harness["output"] / "audit-checks.json").read_text())
    assert report["passed"] is (fail_step is None)
    assert len(report["checks"]) == (fail_step or 8)
    assert "STALE_SUCCESS_SENTINEL" not in str(list(harness["output"].glob("*.json")))
    if fail_step is None:
        assert json.loads((harness["output"] / "frontend-source.json").read_text())["fresh"]


def test_audit_refuses_to_overwrite_previous_run(harness):
    harness["output"].mkdir()
    marker = harness["output"] / "keep.txt"
    marker.write_text("prior evidence")
    assert audit.main(["--output", str(harness["output"])]) == 1
    assert marker.read_text() == "prior evidence" and not harness["calls"]


def test_overlapping_audit_cannot_clear_another_runs_captures(harness):
    lock = harness["root"] / ".local/exhaustive-audit/.audit.lock"
    with lock.open("w") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert audit.main(["--output", str(harness["output"])]) == 1
    assert len(list(harness["root"].rglob("stale.json"))) == 2
    assert not harness["calls"]


def test_audit_cli_refuses_preexisting_output_without_subprocesses(tmp_path, monkeypatch):
    monkeypatch.setattr(__import__("sys"), "argv", ["audit", "--output", str(tmp_path)])
    with pytest.raises(SystemExit) as result:
        runpy.run_module("scripts.audit", run_name="__main__")
    assert result.value.code == 1


@pytest.mark.parametrize("operation", ["open_lock", "cleanup", "spawn", "copy", "close_lock"])
def test_setup_execution_and_cleanup_errors_leave_sanitized_failed_record(harness, monkeypatch, operation):
    def failed(*args, **kwargs): raise OSError("PRIVATE_SENTINEL credentials and paths")
    if operation == "open_lock": monkeypatch.setattr(audit.os, "open", failed)
    elif operation == "cleanup": monkeypatch.setattr(audit.shutil, "rmtree", failed)
    elif operation == "spawn": monkeypatch.setattr(audit, "_run_command", failed)
    elif operation == "copy": monkeypatch.setattr(audit.shutil, "copyfile", failed)
    else:
        close = audit.os.close
        def close_failure(descriptor):
            close(descriptor)
            failed()
        monkeypatch.setattr(audit.os, "close", close_failure)
    assert audit.main(["--output", str(harness["output"])]) == 1
    text = (harness["output"] / "audit-checks.json").read_text()
    assert json.loads(text)["passed"] is False
    assert "PRIVATE_SENTINEL" not in text
    if operation in {"open_lock", "cleanup"}:
        assert not harness["calls"]
        assert len(list(harness["root"].rglob("stale.json"))) == 2


def test_missing_or_nonaffirmative_final_records_cannot_pass_on_exit_codes_alone(harness, monkeypatch):
    run = audit._run_command
    def missing(command, **kwargs):
        result = run(command, **kwargs)
        if "--output" in command and "scripts/coverage_gate.py" in command:
            (harness["output"] / "service-coverage.json").write_text('{"passed":true,"source_manifest_verified":true,"verification_inputs_verified":false}')
        return result
    monkeypatch.setattr(audit, "_run_command", missing)
    assert audit.run_audit(harness["output"]) == 1
    assert not json.loads((harness["output"] / "audit-checks.json").read_text())["passed"]


@pytest.mark.parametrize("target", ["checks.json", "service-coverage.json"])
@pytest.mark.parametrize("content", [None, "[]", "{", '{"passed":true}'])
def test_missing_or_malformed_final_records_fail_closed(harness, monkeypatch, target, content):
    run = audit._run_command
    def corrupt(command, **kwargs):
        result = run(command, **kwargs)
        if "--output" in command and "scripts/coverage_gate.py" in command:
            path = harness["output"] / target
            if content is None: path.unlink()
            else: path.write_text(content)
        return result
    monkeypatch.setattr(audit, "_run_command", corrupt)
    assert audit.run_audit(harness["output"]) == 1
    assert json.loads((harness["output"] / "audit-checks.json").read_text())["passed"] is False


def test_capture_symlink_is_not_followed_or_cleared(harness):
    import shutil
    directory = harness["root"] / ".local/exhaustive-audit/frontend-source"
    shutil.rmtree(directory)
    protected = harness["root"] / "protected"
    protected.mkdir()
    (protected / "keep").write_text("original")
    directory.symlink_to(protected, target_is_directory=True)
    assert audit.run_audit(harness["output"]) == 1
    assert (protected / "keep").read_text() == "original"
    assert not harness["calls"]


def test_audit_output_cannot_be_destroyed_by_capture_cleanup(harness):
    output = harness["root"] / ".local/exhaustive-audit/frontend-source/result"
    assert audit.main(["--output", str(output)]) == 1
    assert not output.exists() and not harness["calls"]


def test_interruption_preserves_incomplete_record_and_releases_lock(harness, monkeypatch):
    def interrupt(*args, **kwargs): raise KeyboardInterrupt()
    monkeypatch.setattr(audit, "_run_command", interrupt)
    assert audit.run_audit(harness["output"]) == 1
    assert not json.loads((harness["output"] / "audit-checks.json").read_text())["passed"]
    with (harness["root"] / ".local/exhaustive-audit/.audit.lock").open("w") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)


def test_first_audit_with_no_previous_capture_directories_can_complete(harness):
    import shutil
    for name in ("frontend-source", "native-source"):
        shutil.rmtree(harness["root"] / ".local/exhaustive-audit" / name)
    assert audit.run_audit(harness["output"]) == 0
    assert json.loads((harness["output"] / "audit-checks.json").read_text())["passed"]


@pytest.mark.parametrize("interrupt, ignore_interrupt", [
    (signal.SIGINT, False), (signal.SIGTERM, False), (signal.SIGINT, True),
])
def test_real_interrupted_audit_cleans_grandchild_before_unlocking(tmp_path, interrupt, ignore_interrupt):
    """Only signal audit.py: a launcher and its child must both be cleaned up."""
    root = tmp_path / "workspace"
    root.mkdir()
    output = tmp_path / "evidence"
    launcher = root / "uv"
    launcher.write_text(f"#!{sys.executable}\nignore_interrupt = {ignore_interrupt!r}\n" + '''
import json, os, signal, subprocess, sys, time
from pathlib import Path
root = Path(__file__).parent
if len(sys.argv) > 1 and sys.argv[1] == "grandchild":
    def stop(signum, frame):
        (root / "cleanup-signal").write_text(str(signum))
        (root / "grandchild-interrupted").touch()
        while not (root / "release-cleanup").exists():
            time.sleep(.01)
        (root / "grandchild-cleaned").touch()
        sys.exit(0)
    signal.signal(signal.SIGINT, signal.SIG_IGN if ignore_interrupt else stop)
    signal.signal(signal.SIGTERM, stop)
    (root / "grandchild-ready").touch()
    while True: signal.pause()
child = subprocess.Popen([sys.executable, __file__, "grandchild"])
def stop(signum, frame):
    child.wait(timeout=15)
    (root / "launcher-cleaned").touch()
    sys.exit(0)
signal.signal(signal.SIGINT, signal.SIG_IGN if ignore_interrupt else stop)
signal.signal(signal.SIGTERM, stop)
while not (root / "grandchild-ready").exists(): time.sleep(.01)
(root / "ready.json").write_text(json.dumps({"launcher": os.getpid(), "grandchild": child.pid}))
while True: signal.pause()
''')
    launcher.chmod(0o700)
    environment = {**os.environ, "PATH": str(root) + os.pathsep + os.environ["PATH"]}
    stages = "audit.STOP_STAGES=((audit.signal.SIGINT,.1),(audit.signal.SIGTERM,5),(audit.signal.SIGKILL,5));" if ignore_interrupt else ""
    code = f"from pathlib import Path; from scripts import audit; audit.ROOT=Path({str(root)!r}); {stages} raise SystemExit(audit.main(['--output', {str(output)!r}]))"
    unrelated = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True)
    runner = subprocess.Popen([sys.executable, "-c", code], cwd=audit.ROOT, env=environment,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
    pids = None
    def await_file(path):
        deadline = time.monotonic() + 10
        while not path.exists():
            assert runner.poll() is None, runner.communicate()
            assert time.monotonic() < deadline, f"Timed out waiting for {path.name}"
            time.sleep(.01)
    try:
        await_file(root / "ready.json")
        pids = json.loads((root / "ready.json").read_text())
        os.kill(runner.pid, interrupt)
        await_file(root / "grandchild-interrupted")
        # A second interruption cannot break the cleanup critical section.
        os.kill(runner.pid, signal.SIGINT)
        assert runner.poll() is None
        with (root / ".local/exhaustive-audit/.audit.lock").open("r") as handle:
            with pytest.raises(BlockingIOError):
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert json.loads((output / "audit-checks.json").read_text())["passed"] is False
        (root / "release-cleanup").touch()
        stdout, stderr = runner.communicate(timeout=10)
        assert runner.returncode == 1, (stdout, stderr)
        assert (root / "grandchild-cleaned").exists()
        assert (root / "launcher-cleaned").exists()
        assert int((root / "cleanup-signal").read_text()) == (signal.SIGTERM if ignore_interrupt else signal.SIGINT)
        for pid in pids.values():
            with pytest.raises(ProcessLookupError): os.kill(pid, 0)
        assert unrelated.poll() is None
        record = json.loads((output / "audit-checks.json").read_text())
        assert record["passed"] is False and "KeyboardInterrupt" in record["audit_error"]
        assert len(record["checks"]) == 1 and record["checks"][0]["exit_code"] is None
        with (root / ".local/exhaustive-audit/.audit.lock").open("r") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        (root / "release-cleanup").touch()
        for process in (runner, unrelated):
            try: os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError: pass
            process.wait(timeout=10)
        if pids:
            try: os.killpg(pids["launcher"], signal.SIGKILL)
            except ProcessLookupError: pass


def test_actual_early_launcher_exit_cannot_leave_child_or_attest_success(tmp_path, monkeypatch):
    worker = tmp_path / "worker.py"
    worker.write_text('''
import json, os, signal, subprocess, sys, time
from pathlib import Path
root = Path(__file__).parent
if sys.argv[-1] == "child":
    def stop(signum, frame):
        (root / "cleaned").touch()
        sys.exit(0)
    signal.signal(signal.SIGINT, stop)
    (root / "ready").touch()
    while True: signal.pause()
child = subprocess.Popen([sys.executable, __file__, "child"])
while not (root / "ready").exists(): time.sleep(.01)
(root / "pids.json").write_text(json.dumps([os.getpid(), child.pid]))
# Successful launcher exit must not count as completed verification.
''')
    monkeypatch.setattr(audit, "STOP_STAGES", ((signal.SIGINT, 3), (signal.SIGTERM, 1), (signal.SIGKILL, 1)))
    try:
        with pytest.raises(RuntimeError, match="launcher exited before its process group"):
            audit._run_command([sys.executable, str(worker), "parent"])
        assert (tmp_path / "cleaned").exists()
        for pid in json.loads((tmp_path / "pids.json").read_text()):
            with pytest.raises(ProcessLookupError): os.kill(pid, 0)
    finally:
        if (tmp_path / "pids.json").exists():
            try: os.killpg(json.loads((tmp_path / "pids.json").read_text())[0], signal.SIGKILL)
            except ProcessLookupError: pass


def test_actual_uncooperative_command_is_killed_and_reaped(tmp_path):
    worker = tmp_path / "worker.py"
    worker.write_text('''
import os, signal
from pathlib import Path
signal.signal(signal.SIGINT, signal.SIG_IGN)
signal.signal(signal.SIGTERM, signal.SIG_IGN)
Path(__file__).with_suffix(".pid").write_text(str(os.getpid()))
while True: signal.pause()
''')
    code = f'''import signal
from scripts import audit
audit.STOP_STAGES=((signal.SIGINT,.1),(signal.SIGTERM,.1),(signal.SIGKILL,3))
try: audit._run_command([{sys.executable!r}, {str(worker)!r}])
except KeyboardInterrupt: raise SystemExit(23)
'''
    runner = subprocess.Popen([sys.executable, "-c", code], cwd=audit.ROOT,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
    pid = None
    try:
        deadline = time.monotonic() + 10
        while not worker.with_suffix(".pid").exists():
            assert runner.poll() is None
            assert time.monotonic() < deadline
            time.sleep(.01)
        pid = int(worker.with_suffix(".pid").read_text())
        os.kill(runner.pid, signal.SIGINT)
        stdout, stderr = runner.communicate(timeout=10)
        assert runner.returncode == 23, (stdout, stderr)
        with pytest.raises(ProcessLookupError): os.kill(pid, 0)
    finally:
        if runner.poll() is None: os.killpg(runner.pid, signal.SIGKILL)
        runner.wait(timeout=10)
        if pid:
            try: os.killpg(pid, signal.SIGKILL)
            except ProcessLookupError: pass


def test_group_probe_reaps_launcher_and_distinguishes_live_descendants(monkeypatch):
    calls = []
    process = SimpleNamespace(pid=4123, poll=lambda: calls.append("reap"))
    def present(pid, signum): calls.append((pid, signum))
    monkeypatch.setattr(audit.os, "killpg", present)
    assert audit._group_exists(process)
    assert calls == ["reap", (4123, 0)]
    def gone(*args): raise ProcessLookupError
    monkeypatch.setattr(audit.os, "killpg", gone)
    assert not audit._group_exists(process)


def test_exit_between_poll_and_group_probe_waits_for_proven_disappearance(monkeypatch):
    """Darwin may return EPERM for a zombie between poll() and killpg()."""
    polls = []
    signals = []
    process = SimpleNamespace(pid=4123, poll=lambda: polls.append("reap"))
    outcomes = iter([PermissionError("group is exiting"), PermissionError("zombie only"), ProcessLookupError()])
    def send(pid, signum):
        signals.append((pid, signum))
        raise next(outcomes)
    monkeypatch.setattr(audit.os, "killpg", send)
    audit._stop_process_group(process)
    assert signals == [(4123, 0), (4123, signal.SIGINT), (4123, 0)]
    assert polls == ["reap", "reap"]


def test_persistent_group_permission_denial_stays_failed_and_bounded(monkeypatch):
    calls = []
    elapsed = [0.0]
    denied = PermissionError("PRIVATE permission failure")
    process = SimpleNamespace(pid=4123, poll=lambda: calls.append("reap"))
    def send(pid, signum):
        calls.append((pid, signum))
        raise denied
    def sleep(delay): elapsed[0] += delay
    monkeypatch.setattr(audit, "STOP_STAGES", ((signal.SIGINT, .1), (signal.SIGTERM, .1)))
    monkeypatch.setattr(audit.os, "killpg", send)
    monkeypatch.setattr(audit.time, "monotonic", lambda: elapsed[0])
    monkeypatch.setattr(audit.time, "sleep", sleep)
    with pytest.raises(PermissionError) as caught: audit._stop_process_group(process)
    assert caught.value is denied
    assert elapsed[0] == .1
    assert [item for item in calls if isinstance(item, tuple) and item[1]] == [(4123, signal.SIGINT)]
    assert calls.count("reap") >= 2


def test_actual_zombie_group_is_not_confused_with_completed_cleanup(monkeypatch):
    process = subprocess.Popen([sys.executable, "-c", "import os; os._exit(0)"], start_new_session=True)
    original_poll = process.poll
    try:
        # Observe the child as a zombie without reaping it. This makes the
        # exit-after-poll window deterministic instead of hoping to hit a race.
        deadline = time.monotonic() + 5
        while True:
            state = subprocess.run(["ps", "-p", str(process.pid), "-o", "stat="],
                                   check=True, capture_output=True, text=True).stdout.strip()
            if state.startswith("Z"): break
            assert time.monotonic() < deadline
            time.sleep(.01)
        if sys.platform == "darwin":
            with pytest.raises(PermissionError): os.killpg(process.pid, 0)
        monkeypatch.setattr(process, "poll", lambda: None)
        assert audit._group_exists(process) is True
        monkeypatch.setattr(process, "poll", original_poll)
        assert audit._wait_group(process, 1) is True
        assert process.returncode == 0
    finally:
        monkeypatch.setattr(process, "poll", original_poll)
        process.wait(timeout=5)


def test_group_wait_is_bounded_and_rechecks_after_short_sleep(monkeypatch):
    clock = iter([10.0, 10.0, 10.1])
    sleeps = []
    monkeypatch.setattr(audit.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(audit.time, "sleep", sleeps.append)
    monkeypatch.setattr(audit, "_group_exists", lambda process: True)
    assert not audit._wait_group(object(), .1)
    assert sleeps == [.05]
    monkeypatch.setattr(audit.time, "monotonic", lambda: 10.0)
    monkeypatch.setattr(audit, "_group_exists", lambda process: False)
    assert audit._wait_group(object(), .1)


@pytest.mark.parametrize("stop_at", [0, 1, 2, 3, None, "disappeared"])
def test_group_cleanup_grace_then_bounded_escalation(monkeypatch, stop_at):
    signals = []
    waits = []
    process = SimpleNamespace(pid=4123)
    monkeypatch.setattr(audit, "_group_exists", lambda process: stop_at != 0)
    def send(pid, signum):
        assert pid == process.pid
        if stop_at == "disappeared": raise ProcessLookupError
        signals.append(signum)
    def wait(process, timeout):
        waits.append(timeout)
        return len(waits) == stop_at
    monkeypatch.setattr(audit.os, "killpg", send)
    monkeypatch.setattr(audit, "_wait_group", wait)
    if stop_at is None:
        with pytest.raises(RuntimeError, match="did not stop"): audit._stop_process_group(process)
    else:
        audit._stop_process_group(process)
    expected = 0 if stop_at in (0, "disappeared") else stop_at or 3
    assert signals == [stage[0] for stage in audit.STOP_STAGES[:expected]]
    assert waits == [stage[1] for stage in audit.STOP_STAGES[:expected]]


@pytest.mark.parametrize("scenario", ["success", "wait_failure", "spawn_failure", "spawn_interrupt", "cleanup_failure", "orphan"])
def test_command_owns_session_defers_spawn_interrupt_and_restores_handlers(monkeypatch, scenario):
    handlers = {signum: signal.getsignal(signum) for signum in audit.INTERRUPT_SIGNALS}
    observed = []
    def wait():
        if scenario == "wait_failure": raise OSError("PRIVATE_SENTINEL")
        observed.append("waited")
        return 7
    process = SimpleNamespace(wait=wait, pid=4123)
    def spawn(command, **kwargs):
        assert kwargs == {"start_new_session": True, "cwd": "/local"}
        if scenario == "spawn_failure": raise OSError("PRIVATE_SENTINEL")
        if scenario == "spawn_interrupt": signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)
        return process
    def cleanup(owned):
        assert owned is process
        assert all(signal.getsignal(signum) == signal.SIG_IGN for signum in audit.INTERRUPT_SIGNALS)
        observed.append("cleaned")
        if scenario == "cleanup_failure": raise RuntimeError("Cleanup did not complete")
    monkeypatch.setattr(audit.subprocess, "Popen", spawn)
    monkeypatch.setattr(audit, "_stop_process_group", cleanup)
    monkeypatch.setattr(audit, "_group_exists", lambda owned: scenario == "orphan")
    error = {"wait_failure": OSError, "spawn_failure": OSError,
             "spawn_interrupt": KeyboardInterrupt, "cleanup_failure": RuntimeError, "orphan": RuntimeError}.get(scenario)
    if error:
        with pytest.raises(error): audit._run_command(["command"], cwd="/local")
    else:
        assert audit._run_command(["command"], cwd="/local").returncode == 7
    assert observed == ([] if scenario == "spawn_failure" else
                        ["cleaned"] if scenario in {"wait_failure", "spawn_interrupt"} else ["waited", "cleaned"])
    assert {signum: signal.getsignal(signum) for signum in audit.INTERRUPT_SIGNALS} == handlers
