"""Hosted origin hosts use browser-compatible canonical identities."""

import pytest

from round_robin.configuration import _canonical_public_origin


@pytest.mark.parametrize('origin', [
    'https://0x7f000001',
    'https://0x7f.1',
    'https://example.123',
    'https://127.1',
    'https://[fe80::1%25eth0]',
])
def test_ambiguous_numeric_and_scoped_hosts_are_rejected(origin):
    with pytest.raises(ValueError):
        _canonical_public_origin(origin)


@pytest.mark.parametrize(('origin', 'expected'), [
    ('https://ExAmPle.COM', 'https://example.com'),
    ('https://127.0.0.1:8443', 'https://127.0.0.1:8443'),
    ('https://[2001:0DB8:0:0:0:0:0:1]', 'https://[2001:db8::1]'),
])
def test_standard_hosts_are_canonicalized(origin, expected):
    assert _canonical_public_origin(origin) == expected
