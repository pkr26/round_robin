"""Findings must refer only to image views and prior findings actually delivered."""
import socket
from copy import deepcopy

import pytest
from pydantic import ValidationError
from round_robin.visual.assets import validate_raster
from round_robin.visual.reviews import (
    PixelView,
    issue_review_receipt,
    validate_critique,
)
from round_robin.visual.schemas import Critique, Finding, ImageCapabilities

from backend.tests.test_visual import png


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('Receipt validation must not connect to a network')
    monkeypatch.setattr(socket.socket, 'connect', forbidden)
    monkeypatch.setattr(socket.socket, 'connect_ex', forbidden)
    monkeypatch.setattr(socket.socket, 'sendto', forbidden)
    monkeypatch.setattr(socket, 'create_connection', forbidden)
    monkeypatch.setattr(socket, 'getaddrinfo', forbidden)


def delivery(view_count):
    raster = validate_raster(png())
    capabilities = ImageCapabilities(provider='anthropic', endpoint_version='synthetic', understands_images=True, supported_media=['image/png'])
    views = [PixelView(raster.canonical_bytes, raster.canonical_hash) for _ in range(view_count)]
    return issue_review_receipt(capabilities, 'version_test', raster.canonical_hash, views)


def critique(receipt, index, previous=None):
    finding = Finding(id='finding_test', description='Synthetic observation', category='observed_defect', severity='material', view_index=index, previous_finding_id=previous)
    return Critique(provider=receipt.provider, version_id=receipt.version_id, canonical_hash=receipt.canonical_hash, receipt_id=receipt.id, findings=[finding])


@pytest.mark.parametrize('count', [1, 2, 3, 4])
def test_exact_view_count_accepts_last_delivered_and_rejects_first_undelivered(count):
    receipt = delivery(count)
    valid = critique(receipt, count - 1)
    before = deepcopy(valid.model_dump(mode='json'))
    assert validate_critique(valid, receipt) is valid
    assert valid.model_dump(mode='json') == before
    if count == 4:
        with pytest.raises(ValidationError):
            critique(receipt, count)
    else:
        missing = critique(receipt, count)
        with pytest.raises(ValueError) as failure:
            validate_critique(missing, receipt)
        assert 'view' in str(failure.value).lower()


def test_prior_finding_link_requires_a_supplied_parent_identity():
    receipt = delivery(2)
    linked = critique(receipt, 1, previous='finding_prior')
    assert validate_critique(linked, receipt, prior_finding_ids={'finding_prior'}) is linked
    with pytest.raises(ValueError) as failure:
        validate_critique(linked, receipt, prior_finding_ids=set())
    assert 'prior-version' in str(failure.value).lower()
    with pytest.raises(ValueError):
        validate_critique(linked, receipt, prior_finding_ids={'finding_other'})


@pytest.mark.parametrize(('field', 'foreign'), [
    ('provider', 'google'),
    ('version_id', 'version_other'),
    ('canonical_hash', 'f' * 64),
    ('receipt_id', 'receipt_other'),
])
def test_critique_identity_must_match_actual_receipt(field, foreign):
    receipt = delivery(1)
    value = critique(receipt, 0).model_copy(update={field: foreign})
    with pytest.raises(ValueError) as failure:
        validate_critique(value, receipt)
    assert 'receipt' in str(failure.value).lower()
