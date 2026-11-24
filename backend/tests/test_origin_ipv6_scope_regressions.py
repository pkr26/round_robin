"""Hosted origins reject scoped IPv6 authorities."""

import pytest

from round_robin.configuration import _canonical_public_origin


@pytest.mark.parametrize('origin', ['https://[fe80::1%eth0]', 'https://[fe80::1%25eth0]'])
def test_scoped_ipv6_origin_is_rejected(origin):
    assert _canonical_public_origin('https://[::1]:443/') == 'https://[::1]'
    with pytest.raises(ValueError):
        _canonical_public_origin(origin)
