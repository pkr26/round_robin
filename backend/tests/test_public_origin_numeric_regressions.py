"""Numeric authority aliases are rejected and valid IDNs remain canonical."""
import pytest
from round_robin.configuration import _canonical_public_origin


@pytest.mark.parametrize('origin', [
    'https://127.0.0.1.',
    'https://example.sub.123',
    'https://example.sub.0x2',
    'https://127.1',
    'https://0x7f.1',
])
def test_noncanonical_ipv4_aliases_are_rejected(origin):
    with pytest.raises(ValueError):
        _canonical_public_origin(origin)


@pytest.mark.parametrize(('origin', 'expected'), [
    ('https://192.0.2.1', 'https://192.0.2.1'),
    ('https://example.com', 'https://example.com'),
    ('https://ＡＢＣ.example', 'https://abc.example'),
    ('https://faß.example', 'https://xn--fa-hia.example'),
    ('https://example。com', 'https://example.com'),
])
def test_ipv4_and_idna_authorities_are_canonical(origin, expected):
    assert _canonical_public_origin(origin) == expected
