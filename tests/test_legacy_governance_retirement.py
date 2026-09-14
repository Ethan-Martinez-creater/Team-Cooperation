from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from coifesp_harness.collaboration import SQLAlchemyGovernanceRepository
from coifesp_harness.collaboration.legacy_inventory import (
    collect_legacy_governance_inventory,
)
from coifesp_harness.execution import SQLAlchemyTaskRepository
from coifesp_harness.postgres_audit import AuditSigningKeyring, SQLAlchemyAuditLog


def engine_with_legacy_schema():
    engine = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    audit = SQLAlchemyAuditLog(
        engine=engine,
        keyring=AuditSigningKeyring(
            active_key_id="audit-v1",
            verification_keys={"audit-v1": b"a" * 32},
        ),
    )
    audit.create_schema()
    SQLAlchemyGovernanceRepository(engine=engine, audit_log=audit).create_schema()
    SQLAlchemyTaskRepository(engine=engine).create_schema()
    return engine


def test_empty_legacy_inventory_satisfies_both_exit_gates() -> None:
    inventory = collect_legacy_governance_inventory(engine_with_legacy_schema())

    assert inventory.is_retired
    assert not inventory.has_active_work
    assert inventory.as_dict()["execution_count"] == 0


def test_active_legacy_execution_blocks_retirement() -> None:
    engine = engine_with_legacy_schema()
    SQLAlchemyTaskRepository(engine=engine).enqueue(
        tenant_id="team-a",
        actor_id="worker-a",
        idempotency_key="legacy-inventory",
        task_id="legacy-execution",
        queue="coding",
        payload={"operation": "legacy"},
        program_id="program-a",
        assignment_id="assignment-a",
    )

    inventory = collect_legacy_governance_inventory(engine)

    assert inventory.execution_count == 1
    assert inventory.active_execution_count == 1
    assert inventory.has_active_work
    assert not inventory.is_retired
