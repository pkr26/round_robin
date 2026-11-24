from urllib.parse import quote

import pytest

from round_robin.database.safety import require_disposable_postgres, require_disposable_redis


@pytest.mark.parametrize("key,value", [
    ("host", "db.production.example"), ("hostaddr", "203.0.113.9"), ("dbname", "production"),
    ("database", "production"), ("service", "production"), ("servicefile", "/operator/services"),
    ("port", "5432"), ("user", "production_role"), ("Host", "db.production.example"),
])
def test_postgres_query_cannot_override_an_apparently_local_test_destination(key, value):
    with pytest.raises(ValueError, match="without routing overrides"):
        require_disposable_postgres(f"postgresql+psycopg://operator@127.0.0.1/round_robin_test?{key}={quote(value)}")


@pytest.mark.parametrize("value", [
    "postgresql+psycopg://operator@db.example/round_robin_test", "postgresql+psycopg://operator@127.0.0.1/round_robin",
    "sqlite:///round_robin_test", "postgresql+psycopg://operator@127.0.0.1:0/round_robin_test",
    "postgresql+psycopg://operator@127.0.0.1:70000/round_robin_test",
    "not a database URL",
])
def test_disposable_postgres_rejects_non_test_or_invalid_destinations(value):
    with pytest.raises(ValueError):
        require_disposable_postgres(value)


def test_disposable_postgres_preserves_supported_schema_options():
    url = require_disposable_postgres("postgresql+psycopg://operator@127.0.0.1/round_robin_test?options=-csearch_path%3Drr_isolated&connect_timeout=5")
    assert url.query["options"] == "-csearch_path=rr_isolated"
    assert url.database == "round_robin_test"


@pytest.mark.parametrize("value", [
    "redis://127.0.0.1/0", "redis://cache.example/14", "redis://127.0.0.1/14?db=0",
    "redis://127.0.0.1/14?host=cache.example", "http://127.0.0.1/14", "redis://127.0.0.1:0/14", "not a broker URL",
])
def test_disposable_redis_rejects_ambiguous_or_non_test_destinations(value):
    with pytest.raises(ValueError):
        require_disposable_redis(value)


def test_disposable_redis_accepts_exact_test_database():
    assert require_disposable_redis("redis://127.0.0.1:56379/14").database == "14"
