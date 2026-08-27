from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from coifesp_harness.disaster_recovery import (  # noqa: E402
    DisasterRecoveryDocumentError, DisasterRecoveryPlan, DrillEvidence, evaluate_drill,
)

MAX_DOCUMENT_BYTES = 1_048_576


def _read(path: Path) -> str:
    if path.stat().st_size > MAX_DOCUMENT_BYTES:
        raise DisasterRecoveryDocumentError("input exceeds size limit")
    return path.read_text(encoding="utf-8")


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(content + "\n", encoding="utf-8", newline="\n")
        temporary.replace(path)
    finally:
        if temporary.exists():
            temporary.unlink()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Offline, fail-closed disaster-recovery drill gate.")
    parser.add_argument("--plan", required=True, type=Path)
    parser.add_argument("--evidence", required=True, type=Path)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args(argv)
    try:
        report = evaluate_drill(DisasterRecoveryPlan.from_json(_read(args.plan)), DrillEvidence.from_json(_read(args.evidence)))
        rendered = json.dumps(report, sort_keys=True, separators=(",", ":"))
        if args.report:
            _write(args.report, rendered)
        print(rendered)
        return 0 if report["decision"] == "pass" else 2
    except (OSError, UnicodeError, DisasterRecoveryDocumentError):
        print('{"decision":"blocked","error_type":"invalid_disaster_recovery_input","report_version":"1.0.0"}')
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
