"""The provider deadline respects both its total and per-operation limits."""
from types import SimpleNamespace

import httpcore
import pytest

from round_robin.providers import transport


def test_remaining_deadline_boundaries_and_per_operation_timeout(monkeypatch):
    monkeypatch.setattr(transport, 'time', SimpleNamespace(monotonic=lambda: 100.0))
    with pytest.raises(httpcore.ReadTimeout):
        transport._remaining(100.0)
    with pytest.raises(httpcore.ReadTimeout):
        transport._remaining(99.0, 1.0)
    assert transport._remaining(100.25) == .25
    assert transport._remaining(110.0) == 10.0
    assert transport._remaining(110.0, 2.0) == 2.0
    assert transport._remaining(110.0, 20.0) == 10.0
    assert transport._remaining(110.0, 0.0) == 0.0
