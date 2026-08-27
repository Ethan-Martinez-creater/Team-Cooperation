import json

from scripts.check_evaluation_gate import main
from test_evaluation_registry import signed_documents


def test_ci_gate_passes_and_writes_deterministic_report(tmp_path, capsys):
    trust, suite = signed_documents()
    trust_path, suite_path, report_path = tmp_path / "trust.json", tmp_path / "suite.json", tmp_path / "report.json"
    trust_path.write_text(trust, encoding="utf-8"); suite_path.write_text(suite, encoding="utf-8")
    assert main(["--trust", str(trust_path), "--suite", str(suite_path), "--report", str(report_path)]) == 0
    emitted = json.loads(capsys.readouterr().out)
    assert emitted["gate"]["status"] == "passed"
    assert json.loads(report_path.read_text(encoding="utf-8")) == emitted


def test_ci_gate_reports_stable_error_without_parser_details(tmp_path, capsys):
    trust, suite = signed_documents(); suite_path = tmp_path / "suite.json"; trust_path = tmp_path / "trust.json"
    trust_path.write_text(trust, encoding="utf-8"); suite_path.write_text(suite + "x", encoding="utf-8")
    assert main(["--trust", str(trust_path), "--suite", str(suite_path)]) == 1
    assert json.loads(capsys.readouterr().out) == {
        "decision": "blocked", "error_type": "invalid_evaluation_input", "report_version": "1.0.0"
    }
