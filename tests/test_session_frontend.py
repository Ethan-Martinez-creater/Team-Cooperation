import shutil
import subprocess
from pathlib import Path

import pytest

FRONTEND = Path(__file__).with_name("frontend") / "session.test.mjs"


def test_frontend_session_coordinator_unit_tests():
    """Run the browser-session coordinator unit tests under Node."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not available on this machine")
    result = subprocess.run(
        [node, str(FRONTEND)],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, f"node session tests failed:\n{result.stdout}\n{result.stderr}"
    assert "OK" in result.stdout
