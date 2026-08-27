from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from coifesp_harness.evaluation import (  # noqa: E402
    DeterministicEvaluationRunner, EvaluationDocumentError, EvaluationSuiteRegistry,
    GateStatus, PolicyEngineEvaluationExecutor,
)
from coifesp_harness.security import PolicyEngine  # noqa: E402


def _read(path: Path) -> str:
    if path.stat().st_size > 4_194_304:
        raise EvaluationDocumentError("input exceeds size limit")
    return path.read_text(encoding="utf-8")


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(text + "\n", encoding="utf-8", newline="\n")
        temporary.replace(path)
    finally:
        if temporary.exists():
            temporary.unlink()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Signed COIFESP PDP evaluation CI gate")
    parser.add_argument("--trust", required=True, type=Path)
    parser.add_argument("--suite", required=True, type=Path)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args(argv)
    try:
        registry = EvaluationSuiteRegistry.from_trust_document(_read(args.trust))
        suite = registry.load(_read(args.suite))
        if suite.evaluation_instant is None:
            raise EvaluationDocumentError("signed suite lacks evaluation_instant")
        report = DeterministicEvaluationRunner().run(
            suite, PolicyEngineEvaluationExecutor(PolicyEngine(clock=lambda: suite.evaluation_instant))
        )
        rendered = report.to_json()
        if args.report:
            _write(args.report, rendered)
        print(rendered)
        return 0 if report.gate.status is GateStatus.PASSED else 2
    except (OSError, UnicodeError, EvaluationDocumentError, ValueError):
        print(json.dumps({"decision": "blocked", "error_type": "invalid_evaluation_input", "report_version": "1.0.0"}, sort_keys=True, separators=(",", ":")))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
