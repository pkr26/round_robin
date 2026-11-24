"""Saved discussion identities stay stable across readers and writers."""
from types import SimpleNamespace

from round_robin.research.discussion import _issue_id, input_hash


def test_discussion_input_hash_tracks_resolved_brief_and_legacy_snapshot():
    run = SimpleNamespace(configuration={'resolved_brief': {'goal': 'resolved'}}, input_snapshot={'goal': 'initial'})
    assert input_hash(run) == '8396f52eb34ee7384da0de47d7d8b05569b151d3a2f718f68155e07e51e9ce23'
    run.configuration = {}
    assert input_hash(run) == 'd1de0ccdd3763b019c86a3ba9f0a7b91e35ac202ee730cb729da94e97ff40197'


def test_objection_identity_is_stable_and_distinguishes_sources():
    args = ('run_a', 'step_b', 'structured', 'alias_c')
    identity = _issue_id(*args)
    assert identity == 'objection_a3a793a48b7afc94556a2486e33c0d8cb4b3418a02835cd0'
    for index in range(len(args)):
        changed = list(args)
        changed[index] += '_different'
        assert _issue_id(*changed) != identity
