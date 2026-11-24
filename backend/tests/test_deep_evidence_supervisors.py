"""Mocked supervisor lifecycles never launch processes or access real services."""

from contextlib import contextmanager
import json
from types import SimpleNamespace

import pytest
import sqlalchemy

from scripts import dev, verify


@pytest.fixture
def verifier(tmp_path, monkeypatch):
    state = {"processes": [], "commands": [], "signals": [], "sql": [], "ready_calls": 0, "exit_role": None, "exit_when": None}
    output = tmp_path / "verification.json"
    monkeypatch.setattr(verify.sys, "argv", ["verify", "--services", "--output", str(output)])
    monkeypatch.setattr(verify, "free_port", lambda port: True)
    monkeypatch.setattr(verify.time, "sleep", lambda seconds: None)
    class Engine:
        @contextmanager
        def begin(self):
            yield SimpleNamespace(execute=lambda statement: state["sql"].append(str(statement)))
        def dispose(self):
            state["disposed"] = True
    monkeypatch.setattr(sqlalchemy, "create_engine", lambda *args, **kwargs: Engine())
    class Process:
        def __init__(self, role):
            self.role, self.pid = role, 700000 + len(state["processes"])
            self.returncode = 1 if state["exit_when"] == "before_ready" and state["exit_role"] == role else None
        def poll(self):
            return self.returncode
        def wait(self, timeout=None):
            if self.returncode is None:
                self.returncode = 0
            return self.returncode
    def launch(*args, **kwargs):
        process = Process(("api", "worker", "dispatcher")[len(state["processes"])])
        state["processes"].append(process)
        return process
    monkeypatch.setattr(verify.subprocess, "Popen", launch)
    def run(command, **kwargs):
        state["commands"].append((command, kwargs["env"]))
        if command == ["pnpm", "test:e2e"] and state["exit_when"] == "after_browser":
            next(process for process in state["processes"] if process.role == state["exit_role"]).returncode = 1
        return SimpleNamespace(returncode=0, stdout="", stderr="")
    monkeypatch.setattr(verify.subprocess, "run", run)
    class Ready:
        status = 200
        def __enter__(self):
            return self
        def __exit__(self, *_):
            if state["exit_when"] == "after_ready":
                next(process for process in state["processes"] if process.role == state["exit_role"]).returncode = 1
    def readiness(*args, **kwargs):
        state["ready_calls"] += 1
        return Ready()
    monkeypatch.setattr(verify, "urlopen", readiness)
    def terminate(pid, number):
        if number == 0:
            raise ProcessLookupError
        state["signals"].append((pid, number))
        if state.get("signal_race") and len(state["signals"]) == 1:
            raise ProcessLookupError("The child exited after its liveness check")
    monkeypatch.setattr(verify.os, "killpg", terminate)
    state["output"] = output
    return state


@pytest.mark.parametrize("role", ["api", "worker", "dispatcher"])
@pytest.mark.parametrize("when", ["before_ready", "after_ready", "after_browser"])
def test_verifier_cannot_accept_rival_readiness_or_ignore_owned_service_exit(verifier, role, when):
    verifier.update(exit_role=role, exit_when=when)
    assert verify.main() == 1
    report = json.loads(verifier["output"].read_text())
    assert not report["all_checks_passed"]
    assert not report["phase_or_release_gates_passed"]
    assert any("DROP SCHEMA" in statement for statement in verifier["sql"])
    if when != "after_browser":
        assert not any(command == ["pnpm", "test:e2e"] for command, _ in verifier["commands"])


def test_verifier_cleanup_race_does_not_skip_siblings_schema_or_evidence(verifier):
    verifier["signal_race"] = True
    assert verify.main() == 0
    assert [number for _, number in verifier['signals']] == [verify.signal.SIGTERM] * 3
    assert verifier["disposed"] and any("DROP SCHEMA" in statement for statement in verifier["sql"])
    assert json.loads(verifier["output"].read_text())["all_checks_passed"]


def test_verifier_owns_browser_server_and_isolates_durability_control(verifier, monkeypatch):
    monkeypatch.setenv("ROUND_ROBIN_DURABILITY_CONTROL_DIRECTORY", "/PRIVATE_OPERATOR_CONTROL_SENTINEL")
    assert verify.main() == 0
    browser_environment = next(environment for command, environment in verifier["commands"] if command == ["pnpm", "test:e2e"])
    assert browser_environment.get("CI") == "true"
    assert browser_environment.get("ROUND_ROBIN_DURABILITY_CONTROL_DIRECTORY") != "/PRIVATE_OPERATOR_CONTROL_SENTINEL"
    assert "round-robin-verification-" in browser_environment["ROUND_ROBIN_DURABILITY_CONTROL_DIRECTORY"]
    # The nonservice profile must also ask Playwright to launch its own server.
    assert all(environment.get("CI") == "true" for _, environment in verifier["commands"])


def test_verifier_preserves_subprocess_and_browser_coverage_configuration(verifier, monkeypatch):
    values = {"COVERAGE_PROCESS_START": "/disposable/coverage.ini", "COVERAGE_PROCESS_KEY": "supervisor-fixture", "COVERAGE_FILE": "/disposable/.coverage", "ROUND_ROBIN_E2E_COVERAGE": "1"}
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    assert verify.main() == 0
    assert all(all(environment.get(key) == value for key, value in values.items()) for _, environment in verifier["commands"])


def test_verifier_never_inherits_production_database_storage_or_frontend_targets(verifier, monkeypatch):
    monkeypatch.setenv("ROUND_ROBIN_DATABASE_URL", "postgresql+psycopg://operator@production.example/customer_data")
    monkeypatch.setenv("ROUND_ROBIN_ARTIFACT_DIRECTORY", "/PRIVATE_PRODUCTION_ARTIFACTS")
    monkeypatch.setenv("NEXT_PUBLIC_ROUND_ROBIN_API_URL", "https://production.example")
    monkeypatch.setenv("ROUND_ROBIN_E2E_API_URL", "https://production.example")
    assert verify.main() == 0
    offline = verifier["commands"][0][1]
    assert offline["ROUND_ROBIN_DATABASE_URL"].startswith("sqlite:///")
    assert "round-robin-verification-" in offline["ROUND_ROBIN_ARTIFACT_DIRECTORY"]
    for _, environment in verifier["commands"]:
        assert environment["NEXT_PUBLIC_ROUND_ROBIN_API_URL"] == "http://127.0.0.1:8000"
        assert environment["ROUND_ROBIN_E2E_API_URL"] == "http://127.0.0.1:8000"


def test_development_supervisor_fails_when_api_readiness_deadline_expires(monkeypatch):
    monkeypatch.setenv("ROUND_ROBIN_DATABASE_URL", "postgresql+psycopg://local@127.0.0.1:55432/round_robin")
    monkeypatch.setenv("ROUND_ROBIN_BROKER_URL", "redis://127.0.0.1:56379/0")
    monkeypatch.setattr(dev.sys, "argv", ["dev", "--skip-services"])
    monkeypatch.setattr(dev, "available", lambda port: True)
    monkeypatch.setattr(dev.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(returncode=0))
    processes, signals = [], []
    class Process:
        def __init__(self):
            self.pid, self.returncode = 710000 + len(processes), None
        def poll(self):
            return self.returncode
        def wait(self, timeout=None):
            self.returncode = 0
            return 0
    def launch(*args, **kwargs):
        process = Process()
        processes.append(process)
        return process
    monkeypatch.setattr(dev.subprocess, "Popen", launch)
    monkeypatch.setattr(dev.signal, "signal", lambda *args: None)
    def terminate(pid, number):
        if number == 0:
            raise ProcessLookupError
        signals.append(pid)
    monkeypatch.setattr(dev.os, "killpg", terminate)
    monkeypatch.setattr(dev, "urlopen", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("Not ready")))
    def no_infinite_monitor(seconds):
        if seconds == .3:
            raise AssertionError("Supervisor entered its monitor despite readiness exhaustion")
    monkeypatch.setattr(dev.time, "sleep", no_infinite_monitor)
    assert dev.main() == 1
    assert len(signals) == 4
