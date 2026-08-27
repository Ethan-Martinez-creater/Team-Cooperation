import sys
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import create_engine, text

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from coifesp_harness.config import Settings  # noqa: E402

TABLES = {
    "collaboration_contracts", "collaboration_contract_releases",
    "collaboration_contract_dependencies", "collaboration_change_impacts",
    "collaboration_contract_events", "collaboration_contract_outbox",
    "collaboration_contract_commands",
}


def main() -> int:
    load_dotenv(ROOT / ".env", override=True)
    settings = Settings.from_environment()
    if not settings.database_url:
        print("POSTGRES_CONTRACTS_FAILED reason=database_url_missing secrets=redacted")
        return 1
    engine = create_engine(settings.database_url, pool_pre_ping=True, hide_parameters=True)
    try:
        with engine.connect() as connection:
            revision = connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
            rows = connection.execute(text("SELECT c.relname,c.relrowsecurity,c.relforcerowsecurity FROM pg_class c WHERE c.relname = ANY(:tables)"), {"tables": sorted(TABLES)}).all()
            protected = {name for name, rls, forced in rows if rls and forced}
            policies = connection.execute(text("SELECT count(*) FROM pg_policies WHERE tablename = ANY(:tables)"), {"tables": sorted(TABLES)}).scalar_one()
            triggers = connection.execute(text("SELECT count(*) FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid WHERE c.relname='collaboration_contract_events' AND NOT t.tgisinternal")).scalar_one()
        if revision != "20260813_28" or protected != TABLES or policies != 8 or triggers != 2:
            raise RuntimeError("contract schema security invariants are incomplete")
        print("POSTGRES_CONTRACTS_OK revision=20260813_28 tables=7 forced_rls=yes policies=8 immutable_event_triggers=2 secrets=redacted")
        return 0
    except Exception as exc:
        print(f"POSTGRES_CONTRACTS_FAILED error_type={type(exc).__name__} reason={exc} secrets=redacted")
        return 1
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
