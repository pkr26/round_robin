"""Hosted origins reject leading spaces before URL parsing."""

import pytest

from round_robin.configuration import _canonical_public_origin


def test_origin_is_not_silently_trimmed():
    assert _canonical_public_origin('https://app.example/') == 'https://app.example'
    with pytest.raises(ValueError):
        _canonical_public_origin(' https://app.example/')
