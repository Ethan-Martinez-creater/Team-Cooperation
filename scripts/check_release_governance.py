from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from coifesp_harness.release import (  # noqa: E402
    CompatibilityMatrix,
    ReleaseDecision,
    ReleaseDocumentError,
    ReleasePlan,
    evaluate_release,
    parse_observations,
)

MAX_DOCUMENT_BYTES = 1_048_576


def read_document(path: Path) -> str:
    size = path.stat().st_size
    if size > MAX_DOCUMENT_BYTES:
        raise ReleaseDocumentError(f"{path.name} exceeds {MAX_DOCUMENT_BYTES} bytes")
    return path.read_text(encoding="utf-8")


def write_report(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(content + "\n", encoding="utf-8", newline="\n")
        temporary.replace(path)
    finally:
        if temporary.exists():
            temporary.unlink()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Offline, fail-closed release compatibility and canary gate evaluator."
    )
    parser.add_argument("--matrix", required=True, type=Path)
    parser.add_argument("--plan", required=True, type=Path)
    parser.add_argument("--observations", required=True, type=Path)
    parser.add_argument("--report", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        matrix = CompatibilityMatrix.from_json(read_document(args.matrix))
        plan = ReleasePlan.from_json(read_document(args.plan))
        observations = parse_observations(read_document(args.observations))
        report = evaluate_release(plan, matrix, observations)
        serialized = report.to_json()
        if args.report is not None:
            write_report(args.report, serialized)
        print(serialized)
        return 0 if report.decision in {ReleaseDecision.PROMOTE, ReleaseDecision.COMPLETE} else 2
    except (OSError, UnicodeError, ReleaseDocumentError) as exc:
        print(
            '{"decision":"blocked","error_type":'
            f'"{type(exc).__name__}","reason":"invalid_release_input",'
            '"report_version":"1.0.0"}'
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
