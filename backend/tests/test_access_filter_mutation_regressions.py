"""Access log filtering removes complete queries without disturbing other records."""

from types import SimpleNamespace

import pytest

from round_robin.security.logging import RedactAccessQueries


def test_multiple_query_delimiters_do_not_preserve_credentials():
    record = SimpleNamespace(args=('127.0.0.1', 'GET', '/callback?code=SECRET?state=PRIVATE', 'HTTP/1.1', 200))
    assert RedactAccessQueries().filter(record) is True
    assert record.args == ('127.0.0.1', 'GET', '/callback?[redacted]', 'HTTP/1.1', 200)


@pytest.mark.parametrize('target', ['/path', 42, None])
def test_query_free_or_nonstring_target_is_unchanged(target):
    args = ('127.0.0.1', 'GET', target, 'HTTP/1.1', 204)
    record = SimpleNamespace(args=args)
    assert RedactAccessQueries().filter(record) is True
    assert record.args == args


@pytest.mark.parametrize('args', [(), ('one',), ['one', 'two', '/path?token=SECRET', 'four', 200], None])
def test_unrelated_record_arguments_are_unchanged(args):
    record = SimpleNamespace(args=args)
    assert RedactAccessQueries().filter(record) is True
    assert record.args == args
