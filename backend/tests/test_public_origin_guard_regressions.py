"""Public origins reject credentials and URL components at their boundaries."""
import pytest

from round_robin.configuration import _canonical_public_origin


@pytest.mark.parametrize('origin', [
    'https://example.test?token=x',
    'https://example.test#section',
    'https://user@example.test',
    'https://user:password@example.test',
    'https://:password@example.test',
    'https://example.test/path',
    'http://example.test',
    'https:///path',
    'https://example.test:0',
    'https://example.test:65536',
])
def test_public_origin_rejects_non_origin_components(origin):
    with pytest.raises(ValueError):
        _canonical_public_origin(origin)


@pytest.mark.parametrize(('origin', 'expected'), [
    ('https://example.test', 'https://example.test'),
    ('https://example.test/', 'https://example.test'),
    ('https://example.test:1', 'https://example.test:1'),
    ('https://example.test:65535', 'https://example.test:65535'),
])
def test_public_origin_accepts_port_and_path_boundaries(origin, expected):
    assert _canonical_public_origin(origin) == expected
