"""The independently retained deletion history must be read safely."""

import os

import pytest
from round_robin.operations import recovery


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="Platform does not support FIFOs")
def test_manifest_snapshot_rejects_fifo_substituted_before_open(tmp_path, monkeypatch):
    path = tmp_path / "manifest.jsonl"
    path.write_text("")
    real_open = os.open
    seen_flags = []

    def replace_before_open(name, flags, *args, **kwargs):
        if name == path:
            path.unlink()
            os.mkfifo(path)
            seen_flags.append(flags)
            # Force nonblocking in the test harness so the unsafe implementation
            # fails promptly instead of hanging the test process.
            flags |= os.O_NONBLOCK
        return real_open(name, flags, *args, **kwargs)

    monkeypatch.setattr(recovery.os, "open", replace_before_open)
    with pytest.raises(recovery.OperationError, match="regular"):
        recovery.read_manifest(path)
    assert seen_flags and seen_flags[0] & os.O_NONBLOCK
