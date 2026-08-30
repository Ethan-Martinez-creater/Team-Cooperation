from __future__ import annotations

import os
import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.pool import NullPool

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from coifesp_harness.config import ConfigurationError  # noqa: E402
from coifesp_harness.approvals import APPROVAL_METADATA  # noqa: E402
from coifesp_harness.artifacts import ARTIFACT_METADATA  # noqa: E402
from coifesp_harness.agent_runs import AGENT_RUN_METADATA  # noqa: E402
from coifesp_harness.collaboration.repository import (  # noqa: E402
    GOVERNANCE_METADATA,
)
from coifesp_harness.contracts.repository import CONTRACT_METADATA  # noqa: E402, F401
from coifesp_harness.execution import EXECUTION_METADATA  # noqa: E402
from coifesp_harness.context.checkpoints import CHECKPOINT_METADATA  # noqa: E402
from coifesp_harness.connectors import CONNECTOR_METADATA  # noqa: E402
from coifesp_harness.memory.repository import MEMORY_METADATA  # noqa: E402
from coifesp_harness.postgres_audit import AUDIT_METADATA  # noqa: E402
from coifesp_harness.tool_jobs import TOOL_JOB_METADATA  # noqa: E402
from coifesp_harness.verification.repository import VERIFICATION_METADATA  # noqa: E402

config = context.config
if config.config_file_name:
    fileConfig(config.config_file_name)

# Migrations only need the database URL. Loading the full Settings object is
# deliberately avoided: unrelated configuration problems (for example an
# invalid LLM provider entry in .env) must not block schema operations.
load_dotenv(PROJECT_ROOT / ".env", override=False)
database_url = os.environ.get("COIFESP_DATABASE_URL", "").strip()
if not database_url:
    raise ConfigurationError("COIFESP_DATABASE_URL is required for database migrations")

target_metadata = [
    MEMORY_METADATA,
    AUDIT_METADATA,
    GOVERNANCE_METADATA,
    EXECUTION_METADATA,
    APPROVAL_METADATA,
    AGENT_RUN_METADATA,
    TOOL_JOB_METADATA,
    CHECKPOINT_METADATA,
    ARTIFACT_METADATA,
    CONNECTOR_METADATA,
    VERIFICATION_METADATA,
]


def _require_postgresql() -> None:
    backend = make_url(database_url).get_backend_name()
    if backend != "postgresql":
        raise ConfigurationError("production migrations require a PostgreSQL database URL")


def run_migrations_offline() -> None:
    _require_postgresql()
    context.configure(
        url=database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    _require_postgresql()
    engine = create_engine(
        database_url,
        poolclass=NullPool,
        pool_pre_ping=True,
        hide_parameters=True,
    )
    try:
        with engine.connect() as connection:
            context.configure(
                connection=connection,
                target_metadata=target_metadata,
                compare_type=True,
                compare_server_default=True,
                transaction_per_migration=True,
            )
            with context.begin_transaction():
                context.run_migrations()
    finally:
        engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
