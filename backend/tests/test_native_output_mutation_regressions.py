import json

import pytest
from round_robin.visual.renderers import interface

from backend.tests.test_exhaustive_visual_renderer import renderer as renderer  # noqa: PLC0414


def test_screenshot_base64_rejects_ignored_non_alphabet_bytes(renderer):
    def forge(result):
        encoded = result['screenshot_base64']
        result['screenshot_base64'] = encoded[:8] + '!' + encoded[8:]
    renderer['change'] = forge
    with pytest.raises(interface.NativeRendererError, match='validation'):
        interface.render_interface('<p>fixture</p>', (100, 100), configuration=interface.NativeRendererConfiguration(True, 'test-image'))


def test_nonsquare_viewport_and_custom_deadlines_reach_bounded_process(renderer):
    configuration = interface.NativeRendererConfiguration(True, 'test-image', browser_timeout_ms=6000, host_timeout_seconds=7.5)
    result = interface.render_interface('<p>fixture</p>', (120, 100), configuration=configuration)
    assert (result.raster.width, result.raster.height) == (120, 100)
    assert (result.snapshot.viewport.width, result.snapshot.viewport.height) == (120, 100)
    inspect, invocation, cleanup = renderer['calls']
    assert inspect[1] == b'' and inspect[2] == {'timeout': 4, 'stdout_limit': 256}
    assert invocation[2] == {'timeout': 7.5, 'stdout_limit': 12 * 1024 * 1024}
    assert json.loads(invocation[1])['timeout_ms'] == 6000
    assert cleanup[1] == b'' and cleanup[2] == {'timeout': 3, 'stdout_limit': 256}


def test_combined_serialized_packet_accepts_exact_160_kib_only(renderer):
    configuration = interface.NativeRendererConfiguration(True, 'test-image')
    actions = [{'action': 'fill', 'selector': '#x', 'value': 'x' * 2000} for _ in range(20)]
    source_length = 120000
    interface.render_interface('x' * source_length, (100, 100), actions, configuration=configuration)
    initial_length = len(renderer['calls'][1][1])
    target_source_length = source_length + 160 * 1024 - initial_length
    assert 0 < target_source_length < 128 * 1024
    interface.render_interface('x' * target_source_length, (100, 100), actions, configuration=configuration)
    assert len(renderer['calls'][4][1]) == 160 * 1024
    calls_before = len(renderer['calls'])
    with pytest.raises(interface.NativeRendererError, match='160 KiB'):
        interface.render_interface('x' * (target_source_length + 1), (100, 100), actions, configuration=configuration)
    assert len(renderer['calls']) == calls_before + 1  # Image inspection only; no render was submitted.


def test_pending_action_assertion_is_never_accepted(renderer):
    renderer['change'] = lambda result: result['checks'][0].update(assertion='pending')
    with pytest.raises(interface.NativeRendererError):
        interface.render_interface('<p>fixture</p>', (100, 100), [{'action': 'click', 'selector': '#x'}],
                                   configuration=interface.NativeRendererConfiguration(True, 'test-image'))
