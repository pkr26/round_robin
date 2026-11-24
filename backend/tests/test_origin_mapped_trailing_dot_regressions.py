"""IDNA-mapped numeric aliases remain subject to trailing-dot checks."""
import pytest

from round_robin.configuration import _canonical_public_origin


@pytest.mark.parametrize('origin', [
    'https://１２７。０。０。１。',
    'https://１２７．１．',
    'https://０ｘ．',
])
def test_idna_numeric_hosts_with_trailing_dot_are_rejected(origin):
    with pytest.raises(ValueError):
        _canonical_public_origin(origin)


def test_non_numeric_idna_host_trailing_dot_is_preserved():
    assert _canonical_public_origin('https://ｅｘａｍｐｌｅ。com。') == 'https://example.com.'
