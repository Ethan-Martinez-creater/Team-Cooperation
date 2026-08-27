from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, select, update
from sqlalchemy.pool import StaticPool

from coifesp_harness.agent_runs import (
    AGENT_RUNS,
    AgentCheckpointKeyring,
    AgentCheckpointKeyRotationService,
    AgentRunPersistenceError,
    AgentRunService,
    DurableRunStatus,
    SQLAlchemyAgentRunRepository,
)
from coifesp_harness.approvals import ApprovalService, SQLAlchemyApprovalRepository
from coifesp_harness.errors import IdempotencyConflict, PolicyDenied, ResourceNotFound
from coifesp_harness.postgres_audit import AuditSigningKeyring, SQLAlchemyAuditLog
from coifesp_harness.security import Principal


def repository():
    engine = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    value = SQLAlchemyAgentRunRepository(
        engine=engine,
        keyring=AgentCheckpointKeyring(master_key=b"k" * 32, key_id="test-v1"),
    )
    value.create_schema()
    return value


def test_checkpoint_is_encrypted_idempotent_and_tenant_scoped() -> None:
    store = repository()
    checkpoint = {
        "messages": [{"role": "user", "content": "private implementation detail"}],
        "budget": {
            "max_turns": 20,
            "max_tool_calls": 50,
            "max_total_tokens": 100000,
        },
    }
    created = store.create(
        tenant_id="team-a",
        owner_principal_id="alice",
        run_id="run-1",
        correlation_id="corr-1",
        idempotency_key="idem-1",
        checkpoint=checkpoint,
    )
    duplicate = store.create(
        tenant_id="team-a",
        owner_principal_id="alice",
        run_id="run-1",
        correlation_id="corr-1",
        idempotency_key="idem-1",
        checkpoint=checkpoint,
    )
    assert duplicate == created
    assert store.load_checkpoint(tenant_id="team-a", run_id="run-1") == checkpoint
    with store.engine.connect() as connection:
        row = connection.execute(select(AGENT_RUNS)).mappings().one()
    assert b"private implementation detail" not in bytes(row["checkpoint_ciphertext"])
    assert len(row["checkpoint_nonce"]) == 12
    with pytest.raises(IdempotencyConflict):
        store.create(
            tenant_id="team-a",
            owner_principal_id="alice",
            run_id="run-2",
            correlation_id="corr-1",
            idempotency_key="idem-1",
            checkpoint={"messages": []},
        )
    with pytest.raises(ResourceNotFound):
        store.get(tenant_id="team-b", run_id="run-1")
    assert [run.run_id for run in store.list_runs(
        tenant_id="team-a", owner_principal_id="alice")] == ["run-1"]
    assert store.list_runs(tenant_id="team-a", owner_principal_id="bob") == ()


def test_checkpoint_key_rotation_is_atomic_audited_and_version_preserving() -> None:
    store = repository()
    checkpoint = {"messages": [{"role": "user", "content": "rotate safely"}]}
    before = store.create(
        tenant_id="team-a",
        owner_principal_id="alice",
        run_id="run-rotate",
        correlation_id="corr-rotate",
        idempotency_key="idem-rotate",
        checkpoint=checkpoint,
    )
    rotated_keyring = AgentCheckpointKeyring(
        active_key_id="test-v2",
        decryption_keys={"test-v1": b"k" * 32, "test-v2": b"n" * 32},
    )
    audit = SQLAlchemyAuditLog(
        engine=store.engine,
        keyring=AuditSigningKeyring(
            active_key_id="audit-v1", verification_keys={"audit-v1": b"a" * 32}
        ),
    )
    audit.create_schema()
    result = AgentCheckpointKeyRotationService(
        engine=store.engine, keyring=rotated_keyring, audit=audit
    ).rotate_batch(tenant_id="team-a", actor_id="security-admin", source_key_id="test-v1")
    assert result.rotated == 1
    assert result.complete is True
    with store.engine.connect() as connection:
        row = (
            connection.execute(select(AGENT_RUNS).where(AGENT_RUNS.c.run_id == "run-rotate"))
            .mappings()
            .one()
        )
    assert row["checkpoint_key_id"] == "test-v2"
    assert int(row["version"]) == before.version
    assert (
        rotated_keyring.decrypt(
            tenant_id="team-a",
            run_id="run-rotate",
            version=before.version,
            ciphertext=bytes(row["checkpoint_ciphertext"]),
            nonce=bytes(row["checkpoint_nonce"]),
            fingerprint=row["checkpoint_fingerprint"],
            key_id=row["checkpoint_key_id"],
        )
        == checkpoint
    )
    assert audit.verify_tenant_chain("team-a") == 1


def test_leases_fence_workers_and_approval_checkpoint_requeues() -> None:
    store = repository()
    store.create(
        tenant_id="team-a",
        owner_principal_id="alice",
        run_id="run-approval",
        correlation_id="corr-approval",
        idempotency_key="idem-approval",
        checkpoint={"messages": [{"role": "user", "content": "send"}]},
    )
    lease = store.claim_next(tenant_id="team-a", worker_id="worker-1")
    assert lease is not None
    with pytest.raises(AgentRunPersistenceError, match="lease"):
        store.start(
            tenant_id="team-a",
            run_id="run-approval",
            worker_id="worker-1",
            lease_token="stale-token",
        )
    running = store.start(
        tenant_id="team-a",
        run_id="run-approval",
        worker_id="worker-1",
        lease_token=lease.lease_token,
    )
    extended_expiry = store.heartbeat(
        tenant_id="team-a",
        run_id="run-approval",
        worker_id="worker-1",
        lease_token=lease.lease_token,
        lease_seconds=120,
    )
    assert extended_expiry > lease.lease_expires_at
    waiting_checkpoint = {
        "messages": [{"role": "tool", "content": "approval required", "tool_call_id": "call-1"}],
        "usage": {"turns": 1, "tool_calls": 1, "total_tokens": 15},
    }
    waiting = store.checkpoint(
        tenant_id="team-a",
        run_id="run-approval",
        worker_id="worker-1",
        lease_token=lease.lease_token,
        target=DurableRunStatus.AWAITING_APPROVAL,
        checkpoint=waiting_checkpoint,
        turns=1,
        tool_calls=1,
        total_tokens=15,
        pending_call_id="call-1",
        pending_approval_id="approval-1",
    )
    assert running.status is DurableRunStatus.RUNNING
    assert waiting.status is DurableRunStatus.AWAITING_APPROVAL
    assert waiting.pending_approval_id == "approval-1"
    resumed_checkpoint = {
        **waiting_checkpoint,
        "approval_bindings": [{"call_id": "call-1", "approval_id": "approval-1"}],
    }
    queued = store.requeue_after_approval(
        tenant_id="team-a",
        run_id="run-approval",
        actor_id="alice",
        approval_id="approval-1",
        expected_version=waiting.version,
        checkpoint=resumed_checkpoint,
    )
    assert queued.status is DurableRunStatus.QUEUED
    assert queued.pending_approval_id is None
    new_lease = store.claim_next(tenant_id="team-a", worker_id="worker-2")
    assert new_lease is not None
    assert new_lease.lease_token != lease.lease_token
    assert new_lease.checkpoint == resumed_checkpoint
    events = store.list_events(tenant_id="team-a", run_id="run-approval")
    assert [event.sequence for event in events] == list(range(1, len(events) + 1))
    assert events[-2].event_type == "agent_run.approval_resumed"


def test_expired_running_lease_is_recovered_with_checkpoint_intact() -> None:
    store = repository()
    checkpoint = {"messages": [{"role": "user", "content": "resume safely"}]}
    store.create(
        tenant_id="team-a",
        owner_principal_id="alice",
        run_id="run-recover",
        correlation_id="corr-recover",
        idempotency_key="idem-recover",
        checkpoint=checkpoint,
    )
    lease = store.claim_next(tenant_id="team-a", worker_id="worker-1")
    assert lease is not None
    store.start(
        tenant_id="team-a",
        run_id="run-recover",
        worker_id="worker-1",
        lease_token=lease.lease_token,
    )
    with store.engine.begin() as connection:
        connection.execute(
            update(AGENT_RUNS)
            .where(AGENT_RUNS.c.run_id == "run-recover")
            .values(lease_expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )
    assert store.recover_expired(tenant_id="team-a") == 1
    scheduled = store.get(tenant_id="team-a", run_id="run-recover")
    assert scheduled.status is DurableRunStatus.QUEUED
    assert scheduled.failure_count == 1
    assert scheduled.next_attempt_at is not None
    with store.engine.begin() as connection:
        connection.execute(
            update(AGENT_RUNS)
            .where(AGENT_RUNS.c.run_id == "run-recover")
            .values(next_attempt_at=datetime.now(UTC) - timedelta(seconds=1))
        )
    recovered = store.claim_next(tenant_id="team-a", worker_id="worker-2")
    assert recovered is not None
    assert recovered.checkpoint == checkpoint
    assert recovered.lease_token != lease.lease_token


def test_approval_resume_fails_closed_until_tool_approval_is_approved() -> None:
    store = repository()
    approval_repository = SQLAlchemyApprovalRepository(engine=store.engine)
    approval_repository.create_schema()
    approvals = ApprovalService(approval_repository)
    service = AgentRunService(store, approval_service=approvals)
    owner = Principal("alice", "team-a")
    worker = Principal(
        "worker-1",
        "team-a",
        roles=frozenset({"agent_worker"}),
        is_service=True,
    )
    initial = {
        "schema": "coifesp.agent-run-checkpoint.v1",
        "messages": [{"role": "user", "content": "send"}],
        "budget": {
            "max_turns": 20,
            "max_tool_calls": 50,
            "max_total_tokens": 100000,
        },
        "usage": {"turns": 0, "tool_calls": 0, "total_tokens": 0},
        "approval_bindings": [],
    }
    service.create(
        principal=owner,
        run_id="run-verified-approval",
        correlation_id="corr-verified-approval",
        idempotency_key="idem-verified-approval",
        checkpoint=initial,
    )
    lease = service.claim(worker=worker)
    assert lease is not None
    service.start(
        worker=worker,
        run_id="run-verified-approval",
        lease_token=lease.lease_token,
    )
    waiting_checkpoint = {
        **initial,
        "usage": {"turns": 1, "tool_calls": 1, "total_tokens": 15},
    }
    waiting = service.checkpoint(
        worker=worker,
        run_id="run-verified-approval",
        lease_token=lease.lease_token,
        target=DurableRunStatus.AWAITING_APPROVAL,
        checkpoint=waiting_checkpoint,
        turns=1,
        tool_calls=1,
        total_tokens=15,
        pending_call_id="call-verified",
        pending_approval_id="approval-verified",
    )
    pending = approvals.request_for_tool(
        principal=owner,
        approval_id="approval-verified",
        tool_name="send_external",
        request_digest="a" * 64,
        review_projection={
            "schema": "coifesp.approval-review.v1",
            "tool_name": "send_external",
            "fields": [
                {
                    "name": "destination",
                    "json_pointer": "/destination",
                    "disclosure": "hash",
                    "value_digest": "b" * 64,
                    "redaction_findings": [],
                }
            ],
        },
        label=None,
        expires_in_seconds=900,
        required_approver_role="tool_approver",
    )
    with pytest.raises(PolicyDenied, match="not approved"):
        service.resume_approval(
            principal=owner,
            run_id="run-verified-approval",
            approval_id="approval-verified",
            expected_version=waiting.version,
        )
    approvals.decide(
        principal=Principal(
            "reviewer",
            "team-a",
            roles=frozenset({"tool_approver"}),
        ),
        approval_id="approval-verified",
        approve=True,
        expected_version=pending.version,
    )
    resumed = service.resume_approval(
        principal=owner,
        run_id="run-verified-approval",
        approval_id="approval-verified",
        expected_version=waiting.version,
    )
    assert resumed.status is DurableRunStatus.QUEUED
