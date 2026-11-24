import json
import os
from dataclasses import replace
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from round_robin.api.preflight import run_configuration
from round_robin.configuration import Settings
from round_robin.operations import config_files
from round_robin.orchestration.budget import BudgetDenied
from round_robin.orchestration.configuration import verify_frozen_configuration
from round_robin.providers.images import ImageFundingLocked, load_image_configuration

from backend.tests.test_image_provider import synthetic_image_configuration
from backend.tests.test_provider_configuration import synthetic_configuration


@pytest.mark.parametrize("symlink", [False, True])
def test_valid_regular_and_symlink_configuration_preserve_all_three_paths(tmp_path, monkeypatch, symlink):
    text_path = tmp_path / "text.json"
    image_path = tmp_path / "image.json"
    text_path.write_text(json.dumps(synthetic_configuration()))
    image_path.write_text(json.dumps(synthetic_image_configuration()))
    if symlink:
        text_link, image_link = tmp_path / "text-link", tmp_path / "image-link"
        text_link.symlink_to(text_path); image_link.symlink_to(image_path)
        text_path, image_path = text_link, image_link
    settings = replace(Settings(), paid_access_enabled=True, provider_config_file=text_path)
    frozen = run_configuration(settings, "live")
    assert frozen["funded"] and frozen["operator_configuration_hash"]
    verify_frozen_configuration(SimpleNamespace(configuration=frozen, deployment_epoch=settings.deployment_epoch), operator_settings=settings)
    monkeypatch.setenv("ROUND_ROBIN_IMAGE_CONFIG_FILE", str(image_path))
    image = load_image_configuration(settings)
    assert image["visual_configuration"] and image["operator_configuration_hash"]


@pytest.mark.parametrize("extra", [0, 1])
def test_regular_configuration_byte_limit_is_inclusive(tmp_path, extra):
    path = tmp_path / "config"
    content = b"{}" + b" " * (128 * 1024 - 2 + extra)
    path.write_bytes(content)
    if extra:
        with pytest.raises(ValueError):
            config_files.read_operator_configuration(path)
    else:
        assert config_files.read_operator_configuration(path).encode() == content


def test_size_rechecked_after_open_even_if_metadata_is_stale(tmp_path, monkeypatch):
    path = tmp_path / "config"
    path.write_bytes(b"x" * (128 * 1024 + 1))
    real_fstat = os.fstat
    def stale_size(descriptor):
        result = real_fstat(descriptor)
        return SimpleNamespace(st_mode=result.st_mode, st_size=0)
    monkeypatch.setattr(config_files.os, "fstat", stale_size)
    with pytest.raises(ValueError):
        config_files.read_operator_configuration(path)


def test_descriptor_closed_if_validation_fails(tmp_path, monkeypatch):
    path = tmp_path / "config"
    path.write_text("{}")
    real_open, real_fstat = os.open, os.fstat
    opened = []
    def tracked_open(path, flags, *args):
        fd = real_open(path, flags, *args)
        opened.append(fd)
        return fd
    def fail_stat(fd):
        if fd in opened:
            raise OSError("synthetic private data")
        return real_fstat(fd)
    with monkeypatch.context() as patch:
        patch.setattr(config_files.os, "open", tracked_open)
        patch.setattr(config_files.os, "fstat", fail_stat)
        with pytest.raises(OSError):
            config_files.read_operator_configuration(path)
    assert len(opened) == 1
    with pytest.raises(OSError):
        real_fstat(opened[0])


def test_invalid_config_errors_do_not_expose_filename(tmp_path, monkeypatch):
    path = tmp_path / "private-config-sentinel"
    path.write_bytes(b"\xff")
    settings = replace(Settings(), paid_access_enabled=True, provider_config_file=path)
    with pytest.raises(HTTPException) as preflight:
        run_configuration(settings, "live")
    assert "private-config-sentinel" not in str(preflight.value.detail)
    frozen = {"mode": "live", "expires_at": "2099-01-01T00:00:00+00:00"}
    with pytest.raises(BudgetDenied) as gate:
        verify_frozen_configuration(SimpleNamespace(configuration=frozen, deployment_epoch=settings.deployment_epoch), operator_settings=settings)
    assert "private-config-sentinel" not in str(gate.value)
    monkeypatch.setenv("ROUND_ROBIN_IMAGE_CONFIG_FILE", str(path))
    with pytest.raises(ImageFundingLocked) as visual:
        load_image_configuration(settings)
    assert "private-config-sentinel" not in str(visual.value)
