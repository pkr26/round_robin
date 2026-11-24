"""Running migrations must not silence existing application loggers."""

import os
import subprocess
import sys
from pathlib import Path


def test_migration_keeps_existing_application_logger_enabled():
    root = Path(__file__).resolve().parents[2]
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(root / "backend" / "src")
    environment["ROUND_ROBIN_DATABASE_URL"] = "postgresql+psycopg://operator@127.0.0.1/offline_test"
    script = """
import io
import logging
from alembic import command
from alembic.config import Config

logger = logging.getLogger('round_robin.dispatcher.service')
assert not logger.disabled
records = []
class Capture(logging.Handler):
    def emit(self, record):
        records.append(record.getMessage())
logger.addHandler(Capture())
config = Config('alembic.ini', output_buffer=io.StringIO())
command.upgrade(config, 'head', sql=True)
assert not logger.disabled
logger.warning('migration logger probe')
assert records == ['migration logger probe']
"""
    result = subprocess.run(
        [sys.executable, "-c", script], cwd=root, env=environment,
        capture_output=True, text=True, timeout=30, check=False,
    )
    assert result.returncode == 0, result.stderr
