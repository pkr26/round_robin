import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest
from round_robin.orchestration import configuration


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFO is unavailable")
@pytest.mark.parametrize("consumer", ["frozen", "preflight", "visual"])
@pytest.mark.parametrize("swap", [False, True])
def test_operator_config_fifo_fails_closed_without_blocking(tmp_path, consumer, swap):
    config = tmp_path / "synthetic-provider-config"
    if swap:
        config.write_text("{}")
    else:
        os.mkfifo(config)
    loaded = Path(configuration.__file__).resolve()
    env = {**os.environ, "PYTHONPATH": str(loaded.parents[2]), "TEST_EXPECTED_CONFIG_MODULE": str(loaded)}
    script = """
import os, signal, sys
from pathlib import Path
from types import SimpleNamespace
from datetime import datetime, timedelta, timezone
signal.alarm(3)
from round_robin.orchestration import configuration
from round_robin.orchestration.budget import BudgetDenied
from round_robin.providers.images import ImageFundingLocked
from fastapi import HTTPException
assert Path(configuration.__file__).resolve() == Path(os.environ['TEST_EXPECTED_CONFIG_MODULE']).resolve()
path = Path(sys.argv[1]); consumer = sys.argv[2]
if sys.argv[3] == 'swap':
    original_open = os.open
    def swapping_open(candidate, flags, *args):
        if str(candidate) == str(path):
            os.unlink(path)
            os.mkfifo(path)
        return original_open(candidate, flags, *args)
    os.open = swapping_open
settings = SimpleNamespace(provider_config_file=path, paid_access_enabled=True, deployment_epoch=1)
run = SimpleNamespace(configuration={'mode': 'live', 'expires_at': (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()}, deployment_epoch=1)
try:
    if consumer == 'frozen':
        configuration.verify_frozen_configuration(run, operator_settings=settings)
    elif consumer == 'preflight':
        from round_robin.api.preflight import run_configuration
        run_configuration(settings, 'live')
    else:
        from round_robin.providers.images import load_image_configuration
        os.environ['ROUND_ROBIN_IMAGE_CONFIG_FILE'] = str(path)
        load_image_configuration(settings)
except BudgetDenied as error:
    assert consumer == 'frozen' and str(error) == 'frozen_configuration_unavailable'
    print('rejected', flush=True)
except HTTPException as error:
    assert consumer == 'preflight' and error.status_code == 422 and error.detail['code'] == 'provider_configuration_invalid'
    print('rejected', flush=True)
except ImageFundingLocked as error:
    assert consumer == 'visual' and str(error) == 'Visual operator configuration is missing, expired or invalid'
    print('rejected', flush=True)
else:
    print('accepted', flush=True)
"""
    process = subprocess.Popen([sys.executable, "-c", script, str(config), consumer, "swap" if swap else "none"], env=env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
    try:
        stdout, stderr = process.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        stdout, stderr = process.communicate()
        pytest.fail("owned child exceeded deadline")
    assert process.returncode == 0, (process.returncode, stdout, stderr)
    assert stdout.strip() == b"rejected"
