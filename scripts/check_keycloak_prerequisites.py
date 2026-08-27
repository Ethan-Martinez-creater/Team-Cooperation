import socket
import sys
from pathlib import Path

from sqlalchemy import create_engine, text

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from coifesp_harness.config import ConfigurationError, Settings  # noqa: E402


def port_available(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        try:
            listener.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def main() -> int:
    from dotenv import load_dotenv

    load_dotenv(PROJECT_ROOT / ".env", override=True)
    settings = Settings.from_environment()
    if not settings.database_url:
        raise ConfigurationError("COIFESP_DATABASE_URL is required")
    engine = create_engine(settings.database_url, hide_parameters=True, pool_pre_ping=True)
    try:
        with engine.connect() as connection:
            row = connection.execute(
                text(
                    """
                    SELECT current_setting('server_version'),
                           r.rolcreatedb, r.rolcreaterole, r.rolsuper
                    FROM pg_catalog.pg_roles AS r
                    WHERE r.rolname = current_user
                    """
                )
            ).one()
        print(
            "POSTGRES_CAPABILITIES "
            f"version={row[0]} createdb={str(row[1]).lower()} "
            f"createrole={str(row[2]).lower()} superuser={str(row[3]).lower()} "
            "secrets=redacted"
        )
        for port in (8080, 9000):
            print(
                f"KEYCLOAK_PORT port={port} "
                f"available={'yes' if port_available(port) else 'no'}"
            )
        return 0
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
