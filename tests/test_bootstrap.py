import base64

import pytest
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from coifesp_harness.collaboration import GovernanceService
from coifesp_harness.config import ConfigurationError, Settings
from coifesp_harness.control_plane import DatabaseReadinessProbe, build_application
from coifesp_harness.memory import MemoryService, SQLAlchemyMemoryRepository
from coifesp_harness.postgres_audit import SQLAlchemyAuditLog
from coifesp_harness.project_process import (
    HumanGateService,
    ProjectExecutionBudgetService,
    ProjectProcessService,
)
from coifesp_harness.team_agents.task_projection import TeamTaskResultProjection


class StubVerifier:
    async def verify(self, token):
        raise AssertionError("verification is not part of this wiring test")


def production_settings() -> Settings:
    return Settings.from_environment(
        {
            "COIFESP_ENV": "production",
            "COIFESP_DATABASE_URL": "postgresql+psycopg://user:password@db.example/coifesp",
            "COIFESP_OIDC_ISSUER": "https://identity.example.test",
            "COIFESP_OIDC_AUDIENCE": "coifesp-control-plane",
            "COIFESP_OIDC_AUTHORIZED_PARTIES": "test-client",
            "COIFESP_OIDC_JWKS_URL": "https://identity.example.test/jwks",
            "COIFESP_AUDIT_KEY_ID": "audit-v1",
            "COIFESP_AUDIT_SIGNING_KEY": "a" * 32,
            "COIFESP_ENVELOPE_SIGNING_KEY": "b" * 32,
            "COIFESP_MEMORY_KEY_ID": "memory-v1",
            "COIFESP_MEMORY_MASTER_KEY": base64.urlsafe_b64encode(b"m" * 32).decode("ascii"),
        }
    )


def sqlite_engine():
    return create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )


def test_bootstrap_wires_durable_memory_audit_and_readiness() -> None:
    engine = sqlite_engine()
    probe_calls = []

    app = build_application(
        settings=production_settings(),
        engine=engine,
        verifier=StubVerifier(),
        readiness_probe=lambda: probe_calls.append(True) or True,
    )

    assert isinstance(app.state.memory_service, MemoryService)
    assert isinstance(app.state.memory_service.repository, SQLAlchemyMemoryRepository)
    assert app.state.memory_service.repository.engine is engine
    assert isinstance(app.state.audit_log, SQLAlchemyAuditLog)
    assert isinstance(app.state.governance_service, GovernanceService)
    assert isinstance(app.state.project_process_service, ProjectProcessService)
    assert isinstance(app.state.project_execution_budget_service, ProjectExecutionBudgetService)
    assert isinstance(app.state.human_gate_service, HumanGateService)
    assert app.state.governance_service.repository.engine is engine
    assert app.state.audit_log.engine is engine
    assert app.state.database_engine is engine
    assert isinstance(app.state.team_task_result_projection, TeamTaskResultProjection)
    assert app.state.team_task_result_projection.repository.engine is engine
    assert app.state.team_task_result_projection.runs is app.state.agent_run_service.repository
    assert app.state.database_readiness() is True
    assert probe_calls == [True]


def test_database_readiness_rejects_non_postgresql_backends() -> None:
    with pytest.raises(ConfigurationError, match="PostgreSQL"):
        DatabaseReadinessProbe(sqlite_engine())()
