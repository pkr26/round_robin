"""Adversarial destination, supervision and evidence-manifest boundaries."""
from contextlib import contextmanager
from copy import deepcopy
import json
from pathlib import Path
import runpy
import subprocess
from types import SimpleNamespace

import pytest

from scripts import dev, scenario_summary, verify
from backend.tests.test_deep_evidence_supervisors import verifier


@pytest.mark.parametrize("database,broker", [
    ("postgresql://operator@production.example/customers", "redis://127.0.0.1/0"),
    ("postgresql://local@127.0.0.1/app?hostaddr=198.51.100.1", "redis://127.0.0.1/0"),
    ("postgresql://local@127.0.0.1/", "redis://127.0.0.1/0"),
    ("sqlite:///PRIVATE_SENTINEL", "redis://127.0.0.1/0"),
    ("postgresql://local@127.0.0.1/app", "redis://remote.example/0"),
    ("postgresql://local@127.0.0.1/app", "redis://127.0.0.1/"),
    ("postgresql://local@127.0.0.1/app", "redis://127.0.0.1/1?db=0"),
    ("postgresql://local@127.0.0.1/app", "not a URL PRIVATE_SENTINEL"),
])
def test_development_rejects_remote_routing_before_migrations_or_processes(monkeypatch, database, broker):
    monkeypatch.setattr(dev.sys, "argv", ["dev", "--check"])
    monkeypatch.setenv("ROUND_ROBIN_DATABASE_URL", database)
    monkeypatch.setenv("ROUND_ROBIN_BROKER_URL", broker)
    monkeypatch.setattr(dev.subprocess, "run", lambda *a, **k: pytest.fail("Must reject before any mutation"))
    with pytest.raises(SystemExit, match="explicit loopback") as error:
        dev.main()
    assert "PRIVATE_SENTINEL" not in str(error.value)


@pytest.fixture
def developer(monkeypatch):
    monkeypatch.setenv("ROUND_ROBIN_DATABASE_URL", "postgresql://local@127.0.0.1/app")
    monkeypatch.setenv("ROUND_ROBIN_BROKER_URL", "redis://127.0.0.1/0")
    monkeypatch.setattr(dev.sys, "argv", ["dev", "--skip-services"])
    state = {"commands": [], "processes": [], "signals": [], "handlers": {}, "mode": "stop"}
    monkeypatch.setattr(dev, "available", lambda port: True)
    monkeypatch.setattr(dev.subprocess, "run", lambda command, **kw: state["commands"].append(command))
    monkeypatch.setattr(dev.signal, "signal", lambda number, handler: state["handlers"].update({number: handler}))
    class Process:
        def __init__(self):
            self.pid, self.returncode = 900000 + len(state["processes"]), None
        def poll(self):
            return self.returncode
        def wait(self, timeout=None):
            if state.get("timeout") and timeout is not None and timeout > 1:
                raise subprocess.TimeoutExpired("bounded-child", timeout)
            self.returncode = self.returncode or 0
    def launch(*a, **kw):
        process = Process()
        state["processes"].append(process)
        if state["mode"] == "early_exit":
            process.returncode = 3
        if state["mode"] == "early_stop":
            state["handlers"][dev.signal.SIGTERM]()
        return process
    monkeypatch.setattr(dev.subprocess, "Popen", launch)
    @contextmanager
    def ready(*a, **kw):
        yield SimpleNamespace(status=200)
    monkeypatch.setattr(dev, "urlopen", ready)
    def sleep(seconds):
        if state["mode"] == "exit":
            state["processes"][0].returncode = 7
        else:
            state["handlers"][dev.signal.SIGINT]()
    monkeypatch.setattr(dev.time, "sleep", sleep)
    def signal(pid, number):
        if number == 0:
            raise ProcessLookupError
        state["signals"].append((pid, number))
        if state.get("signal_race"):
            raise ProcessLookupError
    monkeypatch.setattr(dev.os, "killpg", signal)
    return state


@pytest.mark.parametrize("mode,expected", [("stop", 0), ("exit", 7), ("early_exit", 3), ("early_stop", 0)])
def test_development_cleans_all_siblings_on_stop_or_owned_child_exit(developer, mode, expected):
    developer["mode"] = mode
    assert dev.main() == expected
    assert [number for _, number in developer['signals']] == [dev.signal.SIGTERM] * 4


def test_development_timeout_cleanup_survives_process_disappearance(developer):
    developer.update(timeout=True, signal_race=True)
    assert dev.main() == 0
    assert len(developer["signals"]) == 4


def test_development_busy_port_cannot_apply_a_migration(developer, monkeypatch):
    monkeypatch.setattr(dev, "available", lambda port: port != 3000)
    with pytest.raises(SystemExit, match="3000"):
        dev.main()
    assert not developer["commands"] and not developer["processes"]


def test_check_mode_starts_declared_local_services_without_app_processes(developer, monkeypatch):
    monkeypatch.setattr(dev.sys, "argv", ["dev", "--check"])
    assert dev.main() == 0
    assert developer["commands"][0] == ["docker", "compose", "up", "-d", "--wait", "postgres", "redis"]
    assert len(developer["commands"]) == 3 and not developer["processes"]
    dev.validate_local_targets({})


@pytest.mark.parametrize("port_helper", [dev.available, verify.free_port])
@pytest.mark.parametrize("busy", [True, False])
def test_listener_probe_distinguishes_busy_ports_without_network(monkeypatch, port_helper, busy):
    class Socket:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def setsockopt(self, *args): pass
        def bind(self, target):
            assert target == ("127.0.0.1", 8000)
            if busy: raise OSError("in use")
    monkeypatch.setattr(dev.socket, "socket", Socket)
    assert port_helper(8000) is not busy


@pytest.mark.parametrize("mode", ["busy_initial", "busy_late", "nonzero", "readiness_timeout", "bad_database", "bad_broker", "setup_error", "cleanup_error", "shutdown_timeout"])
def test_verification_failure_paths_cannot_leave_success_evidence(verifier, monkeypatch, mode):
    if mode.startswith("busy"):
        calls = iter([False] if mode == "busy_initial" else [True, True, False])
        monkeypatch.setattr(verify, "free_port", lambda port: next(calls))
    elif mode == "nonzero":
        monkeypatch.setattr(verify.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=2, stdout="failed", stderr=""))
    elif mode == "readiness_timeout":
        monkeypatch.setattr(verify, "urlopen", lambda *a, **k: (_ for _ in ()).throw(OSError("unavailable")))
    elif mode == "bad_database":
        monkeypatch.setenv("ROUND_ROBIN_TEST_DATABASE_URL", "postgresql://user@remote.example/app_test")
    elif mode == "bad_broker":
        monkeypatch.setenv("ROUND_ROBIN_TEST_BROKER_URL", "redis://127.0.0.1/0")
    elif mode == "setup_error":
        monkeypatch.setattr(verify.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(OSError("PRIVATE_SENTINEL")))
    elif mode == "cleanup_error":
        import sqlalchemy
        original = sqlalchemy.create_engine
        def engine(*a, **k):
            result = original(*a, **k)
            begin = result.begin
            @contextmanager
            def transaction():
                if verifier["processes"]:
                    raise OSError("PRIVATE_SENTINEL")
                with begin() as connection: yield connection
            result.begin = transaction
            return result
        monkeypatch.setattr(sqlalchemy, "create_engine", engine)
    else:
        launch = verify.subprocess.Popen
        def process(*a, **k):
            result = launch(*a, **k)
            wait = result.wait
            def bounded_wait(timeout=None):
                if timeout and timeout > 1: raise subprocess.TimeoutExpired("child", timeout)
                return wait()
            result.wait = bounded_wait
            return result
        monkeypatch.setattr(verify.subprocess, "Popen", process)
        monkeypatch.setattr(verify.os, "killpg", lambda *a: (_ for _ in ()).throw(ProcessLookupError()))
    assert verify.main() == (0 if mode == "shutdown_timeout" else 1)
    output = json.loads(verifier["output"].read_text())
    assert output["all_checks_passed"] is (mode == "shutdown_timeout")
    assert "PRIVATE_SENTINEL" not in verifier["output"].read_text()


def test_offline_and_native_profiles_use_explicit_inert_environment(verifier, monkeypatch):
    monkeypatch.setattr(verify.sys, "argv", ["verify", "--native", "--output", str(verifier["output"])])
    assert verify.main() == 0
    assert not verifier["processes"]
    commands = [command for command, _ in verifier["commands"]]
    assert ["pnpm", "test:e2e", "prototype.spec.ts"] in commands
    assert any(command[:3] == ["docker", "image", "inspect"] for command in commands)


@pytest.mark.parametrize("mutation,reason", [
    (lambda x: x["scenarios"].pop(), "exact 100"),
    (lambda x: x.update(phase_passed=True), "phase passed"),
    (lambda x: x["scenarios"][0].update(expected_invariants=[]), "Incomplete"),
    (lambda x: x["scenarios"][0].update(status="passed"), "not scenario completion"),
    (lambda x: x["scenarios"][0].update(blocking_dependency=""), "blocking dependency"),
    (lambda x: x["scenarios"][0].update(status="partial_offline_assertions", implementation_paths=[]), "Partial assertions"),
    (lambda x: x["scenarios"][0].update(preparation_test_paths=["README.md"], preparation_scope_note=""), "incomplete preparation"),
    (lambda x: x["scenarios"][0].update(implementation_paths=["not-a-file.py"]), "Missing implementation"),
    (lambda x: x["scenarios"][0].update(implementation_paths=["README.md::test_missing"]), "Non-Python"),
    (lambda x: x["scenarios"][0].update(implementation_paths=["scripts/verify.py::test_missing"]), "Missing test selector"),
])
def test_scenario_manifest_cannot_inflate_coverage_or_completion(tmp_path, mutation, reason):
    manifest = json.loads(scenario_summary.MANIFEST.read_text())
    mutation(manifest)
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match=reason):
        scenario_summary.check_manifest(path)


def test_scenario_cli_preserves_pending_obligations_in_saved_and_console_summary(tmp_path, monkeypatch, capsys):
    path = tmp_path / "nested" / "summary.md"
    monkeypatch.setattr(scenario_summary.sys if hasattr(scenario_summary, "sys") else __import__("sys"), "argv", ["scenario", "--output", str(path)])
    runpy.run_module("scripts.scenario_summary", run_name="__main__")
    assert "No scenario or phase is declared passed" in path.read_text()
    monkeypatch.setattr(__import__("sys"), "argv", ["scenario", "--check"])
    scenario_summary.main()
    assert path.read_text() == capsys.readouterr().out


def test_scenario_ids_cannot_silently_move_a_core_requirement_into_visual(tmp_path, monkeypatch):
    manifest = json.loads(scenario_summary.MANIFEST.read_text())
    manifest["scenarios"][0]["scenario_id"] = "VIS-99"
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "FAILURE_HANDLING.md").write_text("\n".join(f"| {row['scenario_id']} |" for row in manifest["scenarios"]))
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest))
    monkeypatch.setattr(scenario_summary, "ROOT", tmp_path)
    with pytest.raises(ValueError, match="88 core"):
        scenario_summary.check_manifest(path)


def test_development_cli_sanitizes_failed_setup_without_restarting_or_resetting(monkeypatch):
    monkeypatch.setenv("ROUND_ROBIN_DATABASE_URL", "postgresql://local@127.0.0.1/app")
    monkeypatch.setenv("ROUND_ROBIN_BROKER_URL", "redis://127.0.0.1/0")
    monkeypatch.setattr(__import__("sys"), "argv", ["dev", "--check"])
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(subprocess.CalledProcessError(1, "PRIVATE_SENTINEL")))
    with pytest.raises(SystemExit, match="existing data was not reset") as error:
        runpy.run_module("scripts.dev", run_name="__main__")
    assert "PRIVATE_SENTINEL" not in str(error.value)


@pytest.mark.parametrize("built", [False, True])
def test_static_preview_is_loopback_only_and_requires_a_completed_build(monkeypatch, built):
    import http.server
    state = []
    class Server:
        def __init__(self, target, handler):
            state.append(target)
            assert target == ("127.0.0.1", 3000)
            assert str(handler.keywords["directory"]).endswith("frontend/out")
        def __enter__(self): return self
        def __exit__(self, *args): state.append("closed")
        def serve_forever(self): state.append("served")
    monkeypatch.setattr(http.server, "ThreadingHTTPServer", Server)
    original = Path.is_file
    monkeypatch.setattr(Path, "is_file", lambda path: built if path.name == "index.html" else original(path))
    if built:
        runpy.run_module("scripts.serve_prototype", run_name="__main__")
        assert state == [("127.0.0.1", 3000), "served", "closed"]
    else:
        with pytest.raises(SystemExit, match="Build the fictional prototype"):
            runpy.run_module("scripts.serve_prototype", run_name="__main__")
        assert state == []


@pytest.mark.parametrize("path,expected", [("/live?private=value", "/live.html"), ("/design", "/design.html"), ("/_next/chunk.js", "/_next/chunk.js")])
def test_preview_routes_strip_queries_only_for_static_route_mapping(monkeypatch, path, expected):
    from scripts.serve_prototype import PreviewHandler
    import http.server
    calls = []
    monkeypatch.setattr(http.server.SimpleHTTPRequestHandler, "do_GET", lambda handler: calls.append(handler.path))
    handler = PreviewHandler.__new__(PreviewHandler)
    handler.path = path
    handler.do_GET()
    handler.log_message("Private request target %s", path)
    assert calls == [expected]
