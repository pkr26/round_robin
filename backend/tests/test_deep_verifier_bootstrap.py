"""The real script entry point must bootstrap its own shared guard imports."""
import os
from pathlib import Path
import subprocess
import sys


def test_verifier_guard_is_importable_without_shell_pythonpath():
    root = Path(__file__).resolve().parents[2]
    environment = dict(os.environ)
    environment.pop("PYTHONPATH", None)
    code = "import runpy,sys; sys.path=[p for p in sys.path if not p.endswith('backend/src')]; runpy.run_path('scripts/verify.py', run_name='bootstrap_audit'); from round_robin.database.safety import require_disposable_postgres; assert require_disposable_postgres('postgresql+psycopg://operator@127.0.0.1/audit_test').database == 'audit_test'"
    result = subprocess.run([sys.executable, "-c", code], cwd=root, env=environment, capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
