import re

import pytest
from round_robin.visual.renderers.interface import (
    NativeRendererError,
    NativeRendererTimeout,
)

from backend.tests.test_exhaustive_visual_renderer import render
from backend.tests.test_exhaustive_visual_renderer import renderer as renderer  # noqa: PLC0414


def test_native_run_is_immutable_bounded_and_cleanup_is_scoped_to_its_name(renderer):
    rendered = render()
    inspect, invocation, cleanup = renderer['calls']
    inspect_args, inspect_content, inspect_limits = inspect
    args, packet, limits = invocation
    cleanup_args, cleanup_content, cleanup_limits = cleanup
    assert inspect_args == ['docker', 'image', 'inspect', 'test-image', '--format', '{{.Id}}']
    assert inspect_content == b'' and inspect_limits == {'timeout': 4, 'stdout_limit': 256}
    assert args[:2] == ['docker', 'run']
    assert '--rm' in args and '--pull=never' in args
    name = args[args.index('--name') + 1]
    assert re.fullmatch(r'rr-native-[a-f0-9]{32}', name)
    assert args[args.index('--label') + 1] == f'org.roundrobin.native-job={name}'
    assert {'--network=none', '--read-only', '--cap-drop=ALL', '--security-opt=no-new-privileges',
            '--user=10001:10001', '--memory=512m', '--memory-swap=512m', '--pids-limit=128',
            '--cpus=1', '--ipc=private', '--shm-size=64m', '--log-driver=none'} <= set(args)
    assert args[args.index('--tmpfs') + 1] == '/tmp:rw,noexec,nosuid,nodev,size=134217728,uid=10001,gid=10001,mode=700'
    assert args[args.index('--ulimit') + 1] == 'nofile=256:256'
    assert [args[i + 1] for i, arg in enumerate(args) if arg == '--env'] == ['LANG=C.UTF-8', 'TZ=UTC', 'TMPDIR=/tmp']
    assert args[args.index('--entrypoint') + 1:] == ['node', '-i', rendered.snapshot.image_id, '/opt/renderer/native-renderer.mjs']
    assert not any(arg.startswith(('--mount', '--volume', '-v')) for arg in args)
    assert b'Fixed fixture' in packet and not any('Fixed fixture' in arg for arg in args)
    assert limits == {'timeout': 25, 'stdout_limit': 12 * 1024 * 1024}
    assert cleanup_args == ['docker', 'rm', '--force', name]
    assert cleanup_content == b'' and cleanup_limits == {'timeout': 3, 'stdout_limit': 256}


@pytest.mark.parametrize(('exit_code', 'exception'), [(1, NativeRendererError), (124, NativeRendererTimeout)])
def test_native_failure_still_attempts_only_own_named_cleanup(renderer, exit_code, exception):
    renderer['run_code'] = exit_code
    with pytest.raises(exception):
        render()
    invocation = renderer['calls'][1][0]
    name = invocation[invocation.index('--name') + 1]
    assert renderer['calls'][-1][0] == ['docker', 'rm', '--force', name]
