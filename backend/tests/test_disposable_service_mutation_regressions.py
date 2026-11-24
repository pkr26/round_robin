"""Disposable service guards must respect libpq's effective routing defaults."""

import pytest
from psycopg import pq
from round_robin.database.safety import (
    require_disposable_postgres,
    require_disposable_redis,
)

from scripts.dev import validate_local_targets


@pytest.mark.parametrize("via_service", [False, True])
def test_postgres_rejects_ambient_libpq_routing_overrides(tmp_path, monkeypatch, via_service):
    # Inspect libpq's defaults without opening a socket. Both forms add a
    # hostaddr independently of an explicitly supplied URL host.
    monkeypatch.delenv("PGHOSTADDR", raising=False)
    monkeypatch.delenv("PGSERVICE", raising=False)
    if via_service:
        service_file = tmp_path / "services.conf"
        service_file.write_text("[guard_probe]\nhostaddr=192.0.2.42\n")
        monkeypatch.setenv("PGSERVICEFILE", str(service_file))
        monkeypatch.setenv("PGSERVICE", "guard_probe")
    else:
        monkeypatch.setenv("PGHOSTADDR", "192.0.2.42")

    defaults = {option.keyword: option.val for option in pq.Conninfo.get_defaults()}
    assert defaults[b"hostaddr"] == b"192.0.2.42"
    with pytest.raises(ValueError, match="without routing overrides") as failure:
        require_disposable_postgres("postgresql+psycopg://127.0.0.1/round_robin_test")
    assert "192.0.2.42" not in str(failure.value)


@pytest.mark.parametrize("routing_key", ["PGHOSTADDR", "PGSERVICE"])
def test_dev_launcher_rejects_routing_overrides_in_its_child_environment(monkeypatch, routing_key):
    # The launcher validates a supplied child environment, which need not be
    # identical to the parent process environment used by this test.
    monkeypatch.delenv("PGHOSTADDR", raising=False)
    monkeypatch.delenv("PGSERVICE", raising=False)
    environment = {"ROUND_ROBIN_DATABASE_URL": "postgresql+psycopg://127.0.0.1/round_robin",
                   "ROUND_ROBIN_BROKER_URL": "redis://127.0.0.1/0", routing_key: "unsafe-routing"}
    with pytest.raises(SystemExit, match="no services or migrations were started"):
        validate_local_targets(environment)


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "[::1]"])
def test_disposable_postgres_accepts_loopback_and_supported_query(host, monkeypatch):
    monkeypatch.delenv("PGHOSTADDR", raising=False)
    monkeypatch.delenv("PGSERVICE", raising=False)
    url = require_disposable_postgres(
        f"postgresql+psycopg://{host}:65535/round_robin_test"
        "?options=-csearch_path%3Drr_isolated&sslmode=disable"
        "&connect_timeout=5&application_name=verification"
    )
    assert url.port == 65535
    assert url.query["options"] == "-csearch_path=rr_isolated"
    assert url.query["sslmode"] == "disable"
    assert url.query["application_name"] == "verification"


@pytest.mark.parametrize("scheme", ["redis", "rediss"])
@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "[::1]"])
def test_disposable_redis_accepts_loopback_and_transport(scheme, host):
    url = require_disposable_redis(f"{scheme}://{host}:65535/14")
    assert url.port == 65535
    assert url.database == "14"


@pytest.mark.parametrize("scheme,path", [("postgresql+psycopg", "round_robin_test"), ("redis", "14")])
def test_disposable_service_rejects_port_above_network_range(scheme, path):
    guard = require_disposable_redis if scheme == "redis" else require_disposable_postgres
    with pytest.raises(ValueError):
        guard(f"{scheme}://127.0.0.1:65536/{path}")


def test_disposable_redis_accepts_lowest_network_port():
    url = require_disposable_redis("redis://127.0.0.1:1/14")
    assert url.port == 1


def test_disposable_postgres_accepts_lowest_network_port():
    url = require_disposable_postgres("postgresql+psycopg://127.0.0.1:1/round_robin_test", environment={})
    assert url.port == 1
