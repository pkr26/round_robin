"""The provider packet keeps every request field under its documented name."""
from round_robin.providers.base import ProviderRequest


def test_packet_preserves_request_metadata_and_context_fields():
    request = ProviderRequest(
        run_id='run_test', step_id='step_test', stage='critique', role_slot='critic',
        brief={'title': 'Comparison'}, evidence=[], peer_outputs=[],
        allowed_reference_ids=frozenset({'source_b', 'source_a'}),
        max_output_tokens=256, timeout_seconds=10, repair=True,
        calculations=({'id': 'calc_a'},), claims=({'id': 'claim_a'},),
        cost_as_of='2026-01-01', repair_diagnostics=({'code': 'missing_support'},),
        open_objections=({'id': 'issue_a', 'status': 'open', 'events': []},),
        assessments=({'id': 'assessment_a'},),
    )
    assert request.packet() == {
        'stage': 'critique', 'role': 'critic', 'brief': {'title': 'Comparison'},
        'evidence': [], 'peer_outputs': [], 'allowed_reference_ids': ['source_a', 'source_b'],
        'calculations': [{'id': 'calc_a'}], 'claims': [{'id': 'claim_a'}],
        'cost_as_of': '2026-01-01', 'repair': True,
        'repair_diagnostics': [{'code': 'missing_support'}],
        'open_objections': [{'id': 'issue_a', 'status': 'open'}],
        'assessments': [{'id': 'assessment_a'}],
    }
