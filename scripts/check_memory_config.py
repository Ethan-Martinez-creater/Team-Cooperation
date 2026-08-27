from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from coifesp_harness.config import Settings  # noqa: E402


def main() -> int:
    try:
        from dotenv import load_dotenv

        env_path = PROJECT_ROOT / ".env"
        if not env_path.is_file():
            print("MEMORY_CONFIG_FAILED reason=env_missing")
            return 2
        load_dotenv(dotenv_path=env_path, override=True)
        settings = Settings.from_environment()
        settings.validate(require_memory=True)
        print(
            f"MEMORY_CONFIG_OK key_id={settings.memory_key_id} "
            "key_material=redacted decoded_bytes=32"
        )
        return 0
    except Exception as exc:
        print(f"MEMORY_CONFIG_FAILED error_type={type(exc).__name__} reason={exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
