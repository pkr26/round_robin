"""A saved renderer receipt must describe its actual isolated invocation."""

import hashlib
import json

from backend.tests.test_exhaustive_visual_renderer import render
from backend.tests.test_exhaustive_visual_renderer import renderer as renderer  # noqa: PLC0414


def test_saved_sandbox_receipt_matches_the_sandboxed_invocation(renderer):
    rendered = render()
    arguments = renderer["calls"][1][0]
    sandbox = rendered.snapshot.sandbox

    assert sandbox["version"] == "docker-native-v1"
    assert sandbox["network"] == "none" and "--network=none" in arguments
    assert sandbox["read_only"] is True and "--read-only" in arguments
    assert sandbox["capabilities"] == "none" and "--cap-drop=ALL" in arguments
    assert sandbox["no_new_privileges"] is True and "--security-opt=no-new-privileges" in arguments
    assert sandbox["user"] == "10001:10001" and f"--user={sandbox['user']}" in arguments
    assert sandbox["memory_bytes"] == 512 * 1024 * 1024 and "--memory=512m" in arguments
    assert sandbox["pids"] == 128 and f"--pids-limit={sandbox['pids']}" in arguments
    assert sandbox["cpus"] == 1 and f"--cpus={sandbox['cpus']}" in arguments
    assert sandbox["tmpfs_bytes"] == 128 * 1024 * 1024
    assert f"size={sandbox['tmpfs_bytes']}" in arguments[arguments.index("--tmpfs") + 1]
    assert sandbox["host_mounts"] is False
    assert not any(arg.startswith(("--mount", "--volume", "-v")) for arg in arguments)
    assert sandbox["operator_environment"] is False
    assert [arguments[index + 1] for index, arg in enumerate(arguments) if arg == "--env"] == [
        "LANG=C.UTF-8", "TZ=UTC", "TMPDIR=/tmp"
    ]
    assert sandbox["cookies"] == "new_empty_context"
    assert sandbox["service_workers"] == "blocked"

    # The persisted receipt is bound into the identity of the saved render.
    settings = {
        "image_id": rendered.snapshot.image_id,
        "renderer_version": rendered.renderer_version,
        "viewport": {"width": 100, "height": 100},
        "actions": [],
        "browser_timeout_ms": 20000,
        "host_timeout_seconds": 25,
        "sandbox": sandbox,
    }
    expected_hash = hashlib.sha256(json.dumps(settings, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    assert rendered.configuration_hash == expected_hash == rendered.snapshot.configuration_hash
