"""An IDNA-mapped numeric authority is checked before returning an origin."""
from dataclasses import replace

import pytest

from backend.tests.test_hosted import hosted_settings
from round_robin.app import create_app
from round_robin.configuration import _canonical_public_origin


@pytest.mark.parametrize('origin', [
    'https://１２７.１',
    'https://０ｘ７ｆ.１',
    'https://１２７。１',
    'https://example.１２３',
    'https://０１２７.０.０.１',
    'https://0x',
    'https://0X.',
    'https://example.0x',
    'https://127.0x',
    'https://０ｘ',
    'https://example.０ｘ',
    'https://１２７.０x',
])
def test_mapped_noncanonical_numeric_authority_is_rejected(origin):
    with pytest.raises(ValueError):
        _canonical_public_origin(origin)


@pytest.mark.parametrize(('origin', 'expected'), [
    ('https://１２７.０.０.１/', 'https://127.0.0.1'),
    ('https://１92.０.２.１:443', 'https://192.0.2.1'),
    ('https://ｆｏｏ.example/', 'https://foo.example'),
    ('https://faß.example/', 'https://xn--fa-hia.example'),
    ('https://example。com/', 'https://example.com'),
    ('https://[::1]:443/', 'https://[::1]'),
    ('https://0xg', 'https://0xg'),
    ('https://ｅｘａｍｐｌｅ.０ｘｚ', 'https://example.0xz'),
])
def test_valid_mapped_authorities_remain_canonical(origin, expected):
    assert _canonical_public_origin(origin) == expected


@pytest.mark.parametrize('origin', ['https://１２７.１', 'https://０ｘ７ｆ.１', 'https://example.１２３', 'https://0x', 'https://example.0x', 'https://１２７.０x'])
def test_hosted_startup_rejects_mapped_numeric_alias(tmp_path, origin):
    settings = replace(hosted_settings(tmp_path), public_origin=origin)
    with pytest.raises(ValueError):
        settings.validate()
    with pytest.raises(ValueError):
        create_app(f'sqlite:///{tmp_path / "unused.db"}', settings=settings)
    assert not (tmp_path / 'unused.db').exists()


@pytest.mark.parametrize('origin', ['https://0x', 'https://example.0x', 'https://１２７.０x'])
def test_hosted_app_startup_rejects_empty_hex_final_label(tmp_path, origin):
    settings = replace(hosted_settings(tmp_path), public_origin=origin)
    with pytest.raises(ValueError):
        create_app('sqlite:///:memory:', settings=settings)
