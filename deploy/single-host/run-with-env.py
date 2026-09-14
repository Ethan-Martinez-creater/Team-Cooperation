"""Execute a command with variables loaded from one dotenv file."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from dotenv import dotenv_values


def main() -> None:
    if len(sys.argv) < 3:
        raise SystemExit("usage: run-with-env.py ENV_FILE COMMAND [ARG ...]")

    env_path = Path(sys.argv[1])
    values = dotenv_values(env_path)
    environment = os.environ.copy()
    environment.update({key: value for key, value in values.items() if value is not None})
    os.execvpe(sys.argv[2], sys.argv[2:], environment)


if __name__ == "__main__":
    main()
