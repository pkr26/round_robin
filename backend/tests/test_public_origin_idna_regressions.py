"""Hosted origins normalize internationalized DNS and HTTPS ports."""
import pytest
from round_robin.configuration import _canonical_public_origin

@pytest.mark.parametrize(("origin", "expected"), [
    ("https://ＥＸＡＭＰＬＥ.com", "https://example.com"),
    ("https://faß.de", "https://xn--fa-hia.de"),
    ("https://bücher.example:443/", "https://xn--bcher-kva.example"),
    ("https://bücher.example:444", "https://xn--bcher-kva.example:444"),
])
def test_unicode_hosts_and_https_port_serialization(origin, expected):
    assert _canonical_public_origin(origin) == expected

@pytest.mark.parametrize("origin", ["https://underscore_name.example", "https://example！.test"])
def test_disallowed_dns_characters_are_rejected(origin):
    with pytest.raises(ValueError):
        _canonical_public_origin(origin)
