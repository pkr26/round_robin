"""Production DNS guard and CLI paths, with local fixtures and no external I/O."""

import hashlib
import json
import socket
import threading

import pytest
from pydantic import ValidationError

from backend.tests.test_release_evaluation import manifest as release_manifest, result as release_result
from backend.tests.test_visual_evaluation import manifest as visual_manifest, output as visual_output
from experiments import release, visual_evaluation
from round_robin.research import fetcher
from round_robin.schemas.preflight import InputItem


def dns_answers(*addresses):
    return [(socket.AF_INET6 if ":" in address else socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (address, 443)) for address in addresses]


def test_production_public_dns_wrapper_and_default_target_resolver_return_public_answers(monkeypatch):
    calls = []
    def resolve(host, port, *, type):
        calls.append((host, port, type))
        return dns_answers("93.184.216.34", "2606:4700:4700::1111", "93.184.216.34")
    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    assert set(fetcher.public_addresses("evidence.invalid", timeout=.5)) == {"93.184.216.34", "2606:4700:4700::1111"}
    target = fetcher.validate_target("https://evidence.invalid/facts", resolution_timeout=.5)
    assert target.hostname == "evidence.invalid" and target.address in {"93.184.216.34", "2606:4700:4700::1111"}
    assert calls == [("evidence.invalid", 443, socket.SOCK_STREAM)] * 2


@pytest.mark.parametrize("private", ["127.0.0.1", "169.254.169.254", "::1", "::ffff:127.0.0.1"])
def test_default_production_dns_wrapper_rejects_mixed_public_private_answers(monkeypatch, private):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *args, **kwargs: dns_answers("93.184.216.34", private))
    with pytest.raises(fetcher.FetchRejected, match="Every resolved address"):
        fetcher.validate_target("https://evidence.invalid/facts", resolution_timeout=.5)


def test_redirect_rebinding_through_production_dns_guard_never_dispatches_private_address(monkeypatch):
    lookups, dispatched = [], []
    def resolve(host, *args, **kwargs):
        lookups.append(host)
        return dns_answers("93.184.216.34" if len(lookups) == 1 else "127.0.0.1")
    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    class Redirect:
        def get(self, target, **kwargs):
            dispatched.append(target.address)
            return fetcher.FetchResponse(302, {"location": "/redirected"}, b"")
    allowance = fetcher.FetchAllowance(registered_urls={"candidate": "https://evidence.invalid/facts"})
    with pytest.raises(fetcher.FetchRejected):
        fetcher.collect_source("candidate", allowance, transport=Redirect(), timeout=.5)
    assert lookups == ["evidence.invalid", "evidence.invalid"]
    assert dispatched == ["93.184.216.34"] and allowance.pages == 1


@pytest.mark.parametrize("failure", [socket.gaierror("PRIVATE_DNS_FAILURE"), OSError("PRIVATE_DNS_FAILURE"), RuntimeError("PRIVATE_DNS_FAILURE")])
def test_production_dns_worker_exceptions_are_bounded_and_sanitized(monkeypatch, failure):
    def resolve(*args, **kwargs):
        raise failure
    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    with pytest.raises(fetcher.FetchUnavailable) as caught:
        fetcher.public_addresses("evidence.invalid", timeout=.5)
    assert "PRIVATE_DNS_FAILURE" not in str(caught.value)


def test_slow_dns_threads_hold_capacity_until_the_native_resolver_finishes(monkeypatch):
    capacity = threading.BoundedSemaphore(2)
    monkeypatch.setattr(fetcher, "_DNS_CAPACITY", capacity)
    release = threading.Event()
    entered = threading.Semaphore(0)
    calls = []
    before = set(threading.enumerate())
    def resolve(*args, **kwargs):
        calls.append(True)
        entered.release()
        if not release.wait(timeout=2):
            raise AssertionError("Test did not release its mocked resolver")
        return dns_answers("93.184.216.34")
    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    try:
        for _ in range(2):
            with pytest.raises(fetcher.FetchUnavailable, match="total deadline"):
                fetcher.public_addresses("evidence.invalid", timeout=.02)
            assert entered.acquire(timeout=1)
        with pytest.raises(fetcher.FetchUnavailable, match="capacity"):
            fetcher.public_addresses("evidence.invalid", timeout=.02)
        assert len(calls) == 2
    finally:
        release.set()
        workers = [worker for worker in threading.enumerate() if worker not in before and worker.name == "round-robin-public-dns"]
        for worker in workers:
            worker.join(timeout=1)
            assert not worker.is_alive()
    assert capacity.acquire(blocking=False) and capacity.acquire(blocking=False)
    assert not capacity.acquire(blocking=False)
    capacity.release(); capacity.release()


def test_failed_dns_thread_start_releases_capacity_and_reports_unavailable(monkeypatch):
    capacity = threading.BoundedSemaphore(2)
    monkeypatch.setattr(fetcher, "_DNS_CAPACITY", capacity)
    monkeypatch.setattr(threading.Thread, "start", lambda self: (_ for _ in ()).throw(RuntimeError("PRIVATE_THREAD_START_FAILURE")))
    with pytest.raises(fetcher.FetchUnavailable) as caught:
        fetcher.public_addresses("evidence.invalid", timeout=.5)
    assert "PRIVATE_THREAD_START_FAILURE" not in str(caught.value)
    assert capacity.acquire(blocking=False) and capacity.acquire(blocking=False)
    capacity.release(); capacity.release()


@pytest.mark.parametrize("timeout", [0, -1, 31, float("nan")])
def test_invalid_production_dns_deadlines_never_start_resolution(monkeypatch, timeout):
    calls = []
    monkeypatch.setattr(socket, "getaddrinfo", lambda *args, **kwargs: calls.append(args))
    with pytest.raises(fetcher.FetchUnavailable):
        fetcher.public_addresses("evidence.invalid", timeout=timeout)
    assert calls == []


def write_immutable(path, value):
    path.write_text(json.dumps(value))
    path.chmod(0o444)
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.mark.parametrize("kind,mode", [("release", "manifest"), ("release", "results"), ("visual", "manifest"), ("visual", "plan"), ("visual", "results")])
def test_actual_evaluation_cli_reads_immutable_fixtures_without_declaring_release(tmp_path, monkeypatch, capsys, kind, mode):
    module = release if kind == "release" else visual_evaluation
    manifest = release_manifest() if kind == "release" else visual_manifest()
    path = tmp_path / "frozen-manifest.json"
    original = write_immutable(path, manifest)
    argv = ["study-cli", str(path)]
    result_path = tmp_path / "immutable-results.json"
    result_hash = None
    if mode == "results":
        records = [release_result(manifest["tasks"][0], release.validate_manifest(manifest))] if kind == "release" else [visual_output(visual_evaluation.build_plan(manifest)[2])]
        result_hash = write_immutable(result_path, records)
        argv += ["--results", str(result_path)]
    elif mode == "plan":
        argv += ["--plan"]
    monkeypatch.setattr("sys.argv", argv)
    module.main()
    output = json.loads(capsys.readouterr().out)
    if mode == "plan":
        assert len(output) == 60 and len({trial["trial_id"] for trial in output}) == 60
    else:
        assert output["phase_5_passed" if kind == "release" else "phase_8_passed"] is False
    assert hashlib.sha256(path.read_bytes()).hexdigest() == original
    if result_hash:
        assert hashlib.sha256(result_path.read_bytes()).hexdigest() == result_hash


@pytest.mark.parametrize("kind,invalid", [("release", "manifest"), ("release", "results"), ("visual", "manifest"), ("visual", "results")])
def test_actual_evaluation_cli_rejects_malformed_provenance_before_publishing_summary(tmp_path, monkeypatch, capsys, kind, invalid):
    module = release if kind == "release" else visual_evaluation
    manifest = release_manifest() if kind == "release" else visual_manifest()
    records = [release_result(manifest["tasks"][0], release.validate_manifest(manifest))] if kind == "release" else [visual_output(visual_evaluation.build_plan(manifest)[2])]
    if invalid == "manifest":
        (manifest["tasks"] if kind == "release" else manifest["briefs"]).pop()
    elif kind == "release":
        records[0]["original_grades"][0]["output_hash"] = "an unrelated output"
    else:
        records[0]["original_grades"][0]["pixel_packet_hash"] = "f" * 64
    path, result_path = tmp_path / "manifest.json", tmp_path / "results.json"
    original, results_hash = write_immutable(path, manifest), write_immutable(result_path, records)
    monkeypatch.setattr("sys.argv", ["study-cli", str(path), "--results", str(result_path)])
    with pytest.raises(ValueError):
        module.main()
    assert capsys.readouterr().out == ""
    assert hashlib.sha256(path.read_bytes()).hexdigest() == original
    assert hashlib.sha256(result_path.read_bytes()).hexdigest() == results_hash


def test_selected_core_input_cannot_omit_a_provider_by_repeating_another_recipient():
    item = {"id": "upload_selected", "content_hash": "a" * 64, "version": 1, "recipients": ["google", "openai", "anthropic"]}
    assert set(InputItem.model_validate(item).recipients) == {"google", "openai", "anthropic"}
    with pytest.raises(ValidationError, match="all three providers"):
        InputItem.model_validate({**item, "recipients": ["google", "openai", "openai"]})
