"""Validate disposable service destinations before a driver can connect.

SQLAlchemy's URL host is insufficient: libpq query parameters such as hostaddr,
service and dbname can replace the actual network/database destination.
"""
import os
from collections.abc import Mapping

from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import ArgumentError


def require_disposable_postgres(value: str | URL, *, suffix: str = "_test", environment: Mapping[str, str] | None = None) -> URL:
    try:
        url = make_url(value)
        # libpq applies these independently of the URL's explicit host: an
        # environment hostaddr or service can redirect a loopback-looking URL.
        effective_environment = os.environ if environment is None else environment
        if (effective_environment.get("PGHOSTADDR") or effective_environment.get("PGSERVICE")
                or url.get_backend_name() != "postgresql" or url.host not in {"127.0.0.1", "localhost", "::1"}
                or not (url.database or "").endswith(suffix)
                or not set(url.query) <= {"options", "sslmode", "connect_timeout", "application_name"}
                or any(not isinstance(item, str) for item in url.query.values())):
            raise ValueError
        if url.port is not None and not 1 <= url.port <= 65535:
            raise ValueError
    except (ArgumentError, TypeError, ValueError) as exc:
        raise ValueError("Disposable PostgreSQL requires a loopback test database without routing overrides") from exc
    return url


def require_disposable_redis(value: str | URL, *, database: int = 14) -> URL:
    try:
        url = make_url(value)
        if (url.drivername not in {"redis", "rediss"} or url.host not in {"127.0.0.1", "localhost", "::1"}
                or url.database != str(database) or url.query):
            raise ValueError
        if url.port is not None and not 1 <= url.port <= 65535:
            raise ValueError
    except (ArgumentError, TypeError, ValueError) as exc:
        raise ValueError("Disposable Redis requires an explicit loopback test database without query overrides") from exc
    return url
