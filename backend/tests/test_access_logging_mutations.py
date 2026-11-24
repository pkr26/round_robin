"""Access-log filtering remains safe for unrelated records and startup."""

import logging

from round_robin.security.logging import RedactAccessQueries, install_access_filter


def test_filter_leaves_extra_argument_records_alone():
    args = ("127.0.0.1", "GET", "/path?secret=value", "HTTP/1.1", 200, "extra")
    record = logging.LogRecord("uvicorn.access", logging.INFO, "", 0, "message", args, None)

    assert RedactAccessQueries().filter(record) is True
    assert record.args == args


def test_install_access_filter_is_idempotent_and_preserves_other_filters(monkeypatch):
    logger = logging.Logger("isolated.access")  # noqa: LOG001 - isolate from the global logger registry
    existing = logging.Filter()
    logger.addFilter(existing)
    get_logger = logging.getLogger
    monkeypatch.setattr(logging, "getLogger", lambda name=None: logger if name == "uvicorn.access" else get_logger(name))

    install_access_filter()
    install_access_filter()

    assert logger.filters[0] is existing
    assert len(logger.filters) == 2
    assert isinstance(logger.filters[1], RedactAccessQueries)
