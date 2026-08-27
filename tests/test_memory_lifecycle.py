import pytest
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from coifesp_harness.audit import AuditEvent
from coifesp_harness.errors import MemoryConflictError, MemoryUnavailableError
from coifesp_harness.memory import (MemoryAdmissionPolicy, MemoryKind, MemoryLifecycleService,
    MemoryScope, MemoryService, MemorySource, MemoryWriteRequest, SourceType,
    SQLAlchemyMemoryRepository, TenantMemoryKeyring, TrustLevel)
from coifesp_harness.postgres_audit import AuditSigningKeyring, SQLAlchemyAuditLog
from coifesp_harness.security import Classification, PolicyEngine, Principal, ResourceLabel


def stack():
    engine = create_engine("sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False}, poolclass=StaticPool)
    audit = SQLAlchemyAuditLog(engine=engine, keyring=AuditSigningKeyring(
        active_key_id="audit-v1", verification_keys={"audit-v1": b"a" * 32}))
    audit.create_schema()
    repository = SQLAlchemyMemoryRepository(engine)
    repository.create_schema()
    lifecycle = MemoryLifecycleService(engine=engine, audit_log=audit)
    lifecycle.create_schema()
    memory = MemoryService(repository=repository,
        keyring=TenantMemoryKeyring(master_key=b"m" * 32, key_id="memory-v1"),
        policy=PolicyEngine(), admission=MemoryAdmissionPolicy(), audit=audit)
    owner = Principal("alice", "team-a", clearance=Classification.CONFIDENTIAL,
        compartments=frozenset({"project-x"}))
    memory.write(MemoryWriteRequest(memory_id="memory-1", idempotency_key="write-1",
        correlation_id="corr-1", principal=owner, scope=MemoryScope.USER_PRIVATE,
        kind=MemoryKind.FACT, content="personal preference",
        label=ResourceLabel("team-a", Classification.CONFIDENTIAL,
            frozenset({"project-x"}), "memory:memory-1"),
        source=MemorySource(SourceType.USER, "alice", None, TrustLevel.LOW),
        owner_principal_id="alice"))
    officer = Principal("privacy", "team-a",
        roles=frozenset({"memory_privacy_officer"}),
        clearance=Classification.RESTRICTED, compartments=frozenset({"project-x"}))
    return repository, audit, memory, lifecycle, owner, officer


def test_deletion_revokes_immediately_and_purges_with_separation_of_duties():
    repository, audit, memory, lifecycle, owner, officer = stack()
    requested = lifecycle.request_deletion(principal=owner, request_id="delete-1",
        memory_id="memory-1", expected_version=1, reason="user requested deletion")
    assert requested.status == "pending"
    with pytest.raises(MemoryUnavailableError):
        memory.read(principal=owner, memory_id="memory-1")
    with pytest.raises(MemoryConflictError, match="separation"):
        lifecycle.decide_deletion(principal=Principal("alice", "team-a",
            roles=frozenset({"memory_privacy_officer"})), request_id="delete-1",
            approve=True, reason="self approve")
    decided = lifecycle.decide_deletion(principal=officer, request_id="delete-1",
        approve=True, reason="identity and hold checks completed")
    assert decided.status == "purged"
    assert repository.get("team-a", "memory-1") is None
    assert audit.verify_tenant_chain("team-a") == 3


def test_legal_hold_blocks_purge_until_independently_released():
    repository, _, memory, lifecycle, owner, officer = stack()
    lifecycle.create_hold(principal=officer, hold_id="hold-1", memory_id="memory-1",
        reason="litigation preservation")
    lifecycle.request_deletion(principal=owner, request_id="delete-1",
        memory_id="memory-1", expected_version=1, reason="user requested deletion")
    with pytest.raises(MemoryConflictError, match="legal hold"):
        lifecycle.decide_deletion(principal=officer, request_id="delete-1",
            approve=True, reason="attempt")
    releaser = Principal("privacy-2", "team-a",
        roles=frozenset({"memory_privacy_officer"}))
    lifecycle.release_hold(principal=releaser, hold_id="hold-1", reason="hold released")
    assert lifecycle.decide_deletion(principal=officer, request_id="delete-1",
        approve=True, reason="hold cleared").status == "purged"
    assert repository.get("team-a", "memory-1") is None


def test_rejected_deletion_restores_previous_visibility():
    _, _, memory, lifecycle, owner, officer = stack()
    lifecycle.request_deletion(principal=owner, request_id="delete-1",
        memory_id="memory-1", expected_version=1, reason="mistaken request")
    assert lifecycle.decide_deletion(principal=officer, request_id="delete-1",
        approve=False, reason="request withdrawn").status == "rejected"
    assert memory.read(principal=owner, memory_id="memory-1").content == "personal preference"
