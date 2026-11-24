"""Search rejects unapproved or oversized queries before transport."""
from types import SimpleNamespace

import pytest
from round_robin.research import search as search_module
from round_robin.research.fetcher import FetchRejected, FetchResponse
from round_robin.research.search import BraveSearch


@pytest.mark.parametrize('query', ['', 'x' * 601, ' '.join(['word'] * 76)])
def test_invalid_queries_never_reach_validation_or_transport(monkeypatch, query):
    calls = []
    def validation(url, resolver, *, resolution_timeout=5):
        calls.append(('validation', url))
        return SimpleNamespace(url=url)
    class Transport:
        def get(self, target, **kwargs):
            calls.append(('transport', target))
            return FetchResponse(200, {}, b'{"web":{"results":[]}}')
    monkeypatch.setattr(search_module, 'validate_target', validation)
    with pytest.raises(FetchRejected):
        BraveSearch(transport=Transport(), api_key='synthetic').search(query)
    assert calls == []


def test_missing_key_is_rejected_before_validation(monkeypatch):
    calls = []
    monkeypatch.setattr(search_module, 'validate_target', lambda *a, **k: calls.append('validation'))
    class Transport:
        def get(self, *args, **kwargs):
            calls.append('transport')
    with pytest.raises(FetchRejected):
        BraveSearch(transport=Transport(), api_key='').search('approved terms')
    assert calls == []
