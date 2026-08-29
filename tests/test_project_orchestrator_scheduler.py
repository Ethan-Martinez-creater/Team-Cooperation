from __future__ import annotations

import importlib.util
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import Column, MetaData, String, Table, create_engine, inspect, select
from sqlalchemy.pool import StaticPool

from coifesp_harness.errors import GovernanceConflictError
from coifesp_harness.project_process.scheduler import (
    PROJECT_PROCESS_WAKEUPS,
    ProjectProcessScheduler,
    ProjectProcessWakeupStatus,
    SQLAlchemyProjectProcessWakeupRepository,
)

NOW = datetime(2026, 8, 29, 12, 0, tzinfo=UTC)


class FrozenClock:
    def __init__(self, value: datetime = NOW) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value


@pytest.fixture
def stack():
    engine = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    base = MetaData()
    Table(
        "project_processes",
        base,
        Column("process_id", String(128), primary_key=True),
    )
    base.create_all(engine)
    with engine.begin() as connection:
        connection.execute(
            base.tables["project_processes"].insert().values(process_id="process-a")
        )
    repository = SQLAlchemyProjectProcessWakeupRepository(engine)
    repository.create_schema()
    clock = FrozenClock()
    scheduler = ProjectProcessScheduler(repository, clock=clock, retry_delay=lambda attempt: 7)
    return engine, repository, scheduler, clock


def enqueue(scheduler: ProjectProcessScheduler, **overrides):
    values = {
        "process_id": "process-a",
        "source_event_id": "event-1",
        "source_event_type": "project.analysis.completed",
        "payload": {"event_id": "event-1", "version": 2},
        "project_id": "project-a",
        "retry_budget": 2,
    }
    values.update(overrides)
    return scheduler.enqueue(**values)


def test_enqueue_is_deduplicated_and_conflicts_fail_closed(stack):
    _, repository, scheduler, _ = stack
    first = enqueue(scheduler)
    duplicate = enqueue(scheduler)
    assert duplicate.wakeup_id == first.wakeup_id
    with repository.transaction() as connection:
        assert connection.execute(select(PROJECT_PROCESS_WAKEUPS)).fetchall().__len__() == 1
    with pytest.raises(GovernanceConflictError, match="different content"):
        enqueue(scheduler, payload={"event_id": "event-1", "version": 9})
    with pytest.raises(GovernanceConflictError, match="different content"):
        enqueue(scheduler, source_event_type="project.delivery.accepted")


def test_claim_is_exclusive_and_heartbeat_complete_are_fenced(stack):
    _, _, scheduler, clock = stack
    created = enqueue(scheduler)
    first = scheduler.claim(owner="worker-a", lease_seconds=10)
    assert first is not None
    assert first.wakeup_id == created.wakeup_id
    assert first.status is ProjectProcessWakeupStatus.LEASED
    assert first.attempt == 1
    assert first.fencing_token == 1
    assert scheduler.claim(owner="worker-b") is None

    extended = scheduler.heartbeat(
        wakeup_id=first.wakeup_id,
        owner="worker-a",
        fencing_token=first.fencing_token,
        lease_token=first.lease_token,
        lease_seconds=20,
    )
    assert extended.lease_expires_at == clock.value + timedelta(seconds=20)
    done = scheduler.complete(
        wakeup_id=first.wakeup_id,
        owner="worker-a",
        fencing_token=first.fencing_token,
        lease_token=first.lease_token,
    )
    assert done.status is ProjectProcessWakeupStatus.COMPLETED
    with pytest.raises(GovernanceConflictError, match="fencing"):
        scheduler.complete(
            wakeup_id=first.wakeup_id,
            owner="worker-a",
            fencing_token=first.fencing_token,
            lease_token=first.lease_token,
        )


def test_assert_fence_in_transaction_requires_current_random_token(stack):
    _, repository, scheduler, _ = stack
    created = enqueue(scheduler)
    lease = scheduler.claim(owner="worker-a", lease_seconds=10)
    assert lease is not None

    with repository.transaction() as connection:
        asserted = scheduler.assert_fence_in_transaction(
            connection,
            wakeup_id=created.wakeup_id,
            owner="worker-a",
            fencing_token=lease.fencing_token,
            lease_token=lease.lease_token,
        )
    assert asserted.lease_token == lease.lease_token

    with repository.transaction() as connection, pytest.raises(
        GovernanceConflictError, match="fencing"
    ):
        scheduler.assert_fence_in_transaction(
            connection,
            wakeup_id=created.wakeup_id,
            owner="worker-a",
            fencing_token=lease.fencing_token,
            lease_token="stale-random-token",
        )
    with pytest.raises(ValueError, match="lease token"):
        scheduler.complete(
            wakeup_id=created.wakeup_id,
            owner="worker-a",
            fencing_token=lease.fencing_token,
        )


def test_expired_lease_is_reclaimed_with_new_fencing_token(stack):
    _, _, scheduler, clock = stack
    created = enqueue(scheduler)
    old = scheduler.claim(owner="worker-a", lease_seconds=5)
    assert old is not None
    clock.value = NOW + timedelta(seconds=6)
    recovered = scheduler.claim(owner="worker-b", lease_seconds=5)
    assert recovered is not None
    assert recovered.wakeup_id == created.wakeup_id
    assert recovered.fencing_token == old.fencing_token + 1
    assert recovered.lease_token != old.lease_token
    with pytest.raises(GovernanceConflictError, match="fencing"):
        scheduler.heartbeat(
            wakeup_id=old.wakeup_id,
            owner="worker-a",
            fencing_token=old.fencing_token,
            lease_token=old.lease_token,
        )
    with pytest.raises(GovernanceConflictError, match="fencing"):
        scheduler.complete(
            wakeup_id=old.wakeup_id,
            owner="worker-a",
            fencing_token=old.fencing_token,
            lease_token=old.lease_token,
        )


def test_retry_delay_is_persisted_without_sleep_and_budget_exhausts(stack):
    _, _, scheduler, clock = stack
    enqueue(scheduler, source_event_id="event-retry")
    first = scheduler.claim(owner="worker-a", lease_seconds=30)
    assert first is not None
    waiting = scheduler.retry(
        wakeup_id=first.wakeup_id,
        owner="worker-a",
        fencing_token=first.fencing_token,
        lease_token=first.lease_token,
        error="temporary",
    )
    assert waiting.status is ProjectProcessWakeupStatus.RETRY_WAIT
    assert waiting.available_at == NOW + timedelta(seconds=7)
    clock.value = NOW + timedelta(seconds=6)
    assert scheduler.claim(owner="worker-b") is None
    clock.value = NOW + timedelta(seconds=7)
    second = scheduler.claim(owner="worker-b", lease_seconds=30)
    assert second is not None and second.attempt == 2
    waiting = scheduler.retry(
        wakeup_id=second.wakeup_id,
        owner="worker-b",
        fencing_token=second.fencing_token,
        lease_token=second.lease_token,
        error="temporary again",
        retry_after_seconds=0,
    )
    assert waiting.status is ProjectProcessWakeupStatus.RETRY_WAIT
    third = scheduler.claim(owner="worker-c", lease_seconds=30)
    assert third is not None and third.attempt == 3
    failed = scheduler.retry(
        wakeup_id=third.wakeup_id,
        owner="worker-c",
        fencing_token=third.fencing_token,
        lease_token=third.lease_token,
        error="permanent",
    )
    assert failed.status is ProjectProcessWakeupStatus.FAILED
    assert failed.terminal_at == clock.value


def test_fail_forcefully_terminates_only_current_lease(stack):
    _, _, scheduler, _ = stack
    enqueue(scheduler, source_event_id="event-fail")
    lease = scheduler.claim(owner="worker-a")
    assert lease is not None
    failed = scheduler.fail(
        wakeup_id=lease.wakeup_id,
        owner="worker-a",
        fencing_token=lease.fencing_token,
        lease_token=lease.lease_token,
        error="non-retryable",
    )
    assert failed.status is ProjectProcessWakeupStatus.FAILED
    assert failed.last_error == "non-retryable"
    assert scheduler.claim(owner="worker-b") is None


def test_enqueue_in_transaction_rolls_back_with_business_mutation(stack):
    _, repository, scheduler, _ = stack
    with pytest.raises(RuntimeError), repository.transaction() as connection:
        scheduler.enqueue_in_transaction(
            connection,
            process_id="process-a",
            source_event_id="event-transaction",
            source_event_type="project.goal.confirmed",
            payload={"same_commit": True},
            project_id="project-a",
        )
        raise RuntimeError("rollback")
    with repository.transaction() as connection:
        assert repository.by_source(connection, "process-a", "event-transaction") is None


def test_two_independent_connections_compete_for_one_claim():
    database = Path.cwd() / f".a3-project-process-{uuid4().hex}.sqlite"
    engine_a = engine_b = None
    try:
        url = f"sqlite+pysqlite:///{database}"
        engine_a = create_engine(
            url, connect_args={"check_same_thread": False, "timeout": 5}
        )
        engine_b = create_engine(
            url, connect_args={"check_same_thread": False, "timeout": 5}
        )
        repository_a = SQLAlchemyProjectProcessWakeupRepository(engine_a)
        repository_b = SQLAlchemyProjectProcessWakeupRepository(engine_b)
        repository_a.create_schema()
        scheduler_a = ProjectProcessScheduler(repository_a, clock=FrozenClock())
        scheduler_b = ProjectProcessScheduler(repository_b, clock=FrozenClock())
        enqueue(scheduler_a, source_event_id="event-concurrent")
        ready = Barrier(2)

        def compete(scheduler, owner):
            ready.wait()
            return scheduler.claim(owner=owner, lease_seconds=30)

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = tuple(
                executor.submit(compete, scheduler, owner)
                for scheduler, owner in (
                    (scheduler_a, "worker-a"),
                    (scheduler_b, "worker-b"),
                )
            )
            leases = tuple(future.result(timeout=10) for future in futures)

        assert sum(lease is not None for lease in leases) == 1
        with repository_a.transaction() as connection:
            rows = repository_a.list_for_process(connection, "process-a")
        assert len(rows) == 1
        assert rows[0].status is ProjectProcessWakeupStatus.LEASED
        assert rows[0].lease_owner in {"worker-a", "worker-b"}
    finally:
        if engine_a is not None:
            engine_a.dispose()
        if engine_b is not None:
            engine_b.dispose()
        if database.exists():
            database.unlink()


def _load_migration(name: str):
    path = Path(__file__).parents[1] / "alembic" / "versions" / name
    spec = importlib.util.spec_from_file_location(name.replace(".", "_"), path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _run_migration(module, connection, method: str) -> None:
    module.op = Operations(MigrationContext.configure(connection))
    getattr(module, method)()


def test_migration_48_upgrade_and_downgrade_smoke():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    migration_47 = _load_migration("20260829_47_project_process_runtime.py")
    migration_48 = _load_migration("20260829_48_project_process_wakeups.py")
    with engine.begin() as connection:
        _run_migration(migration_47, connection, "upgrade")
        _run_migration(migration_48, connection, "upgrade")
    inspector = inspect(engine)
    assert "project_process_wakeups" in inspector.get_table_names()
    columns = {item["name"] for item in inspector.get_columns("project_process_wakeups")}
    assert {
        "source_event_id",
        "available_at",
        "attempt",
        "retry_budget",
        "lease_owner",
        "lease_expires_at",
        "fencing_token",
        "last_error",
    }.issubset(columns)
    unique_names = {
        item["name"]
        for item in inspector.get_unique_constraints("project_process_wakeups")
        if item["name"]
    }
    assert "uq_project_process_wakeup_source" in unique_names
    with engine.begin() as connection:
        _run_migration(migration_48, connection, "downgrade")
    assert "project_process_wakeups" not in inspect(engine).get_table_names()
