"""An oversized operator file is rejected before reading its contents."""

import pytest

from round_robin.operations import config_files


def test_oversized_metadata_is_rejected_before_read(tmp_path, monkeypatch):
    path = tmp_path / 'config'
    path.write_bytes(b'x' * (128 * 1024 + 1))

    def unexpected_read_wrapper(*args, **kwargs):
        pytest.fail('oversized configuration should not enter the read path')

    monkeypatch.setattr(config_files.os, 'fdopen', unexpected_read_wrapper)
    with pytest.raises(ValueError):
        config_files.read_operator_configuration(path)
