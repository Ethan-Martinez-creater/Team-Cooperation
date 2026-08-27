from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from coifesp_harness.control_plane.bootstrap import load_environment_settings  # noqa: E402


def main() -> int:
    path = ROOT / ".env"
    values: list[str] = []
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        match = re.match(r"^\s*COIFESP_AUDIT_KEY_ID\s*=\s*(.*)$", line)
        if match:
            values.append(match.group(1).strip().strip('"').strip("'"))
    parsed = load_environment_settings(path).audit_key_id
    print(
        "AUDIT_ACTIVE_DIAGNOSTIC "
        f"occurrences={len(values)} file_values={','.join(values) if values else 'absent'} "
        f"parsed={parsed or 'absent'} secrets=none"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
