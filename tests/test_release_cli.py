import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_offline_cli_accepts_fixture_and_writes_report(tmp_path):
    report_path = tmp_path / "release-report.json"
    completed = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/check_release_governance.py"),
            "--matrix",
            str(ROOT / "deploy/release/compatibility-matrix.json"),
            "--plan",
            str(ROOT / "deploy/release/release-plan.json"),
            "--observations",
            str(ROOT / "deploy/release/observations-pass.json"),
            "--report",
            str(report_path),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout)["decision"] == "complete"
    assert json.loads(report_path.read_text(encoding="utf-8"))["promotion_authorized"] is True


def test_offline_cli_fail_closed_on_invalid_document(tmp_path):
    invalid = tmp_path / "invalid.json"
    invalid.write_text("{}", encoding="utf-8")
    completed = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/check_release_governance.py"),
            "--matrix",
            str(invalid),
            "--plan",
            str(ROOT / "deploy/release/release-plan.json"),
            "--observations",
            str(ROOT / "deploy/release/observations-pass.json"),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert completed.returncode == 1
    payload = json.loads(completed.stdout)
    assert payload["decision"] == "blocked"
    assert payload["reason"] == "invalid_release_input"
