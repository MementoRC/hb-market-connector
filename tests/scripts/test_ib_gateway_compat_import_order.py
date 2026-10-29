"""Regression: ib_gateway_compat.py run as a file must not fail with ModuleNotFoundError.

`_server_info` imported `market_connector...` before `_connection_spec` put REPO_ROOT
on sys.path, so a plain file run died at import time.
"""

import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "ib_gateway_compat.py"


def test_script_file_run_has_no_module_not_found(tmp_path: Path) -> None:
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    env.update(
        IB_GATEWAY_HOST="127.0.0.1",
        IB_GATEWAY_PORT="1",
        IB_CLIENT_ID="958",
    )

    result = subprocess.run(
        [sys.executable, "-I", str(SCRIPT), "--channel", "stable", "--dry-run"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )

    output = result.stdout + result.stderr
    assert "ModuleNotFoundError" not in output
