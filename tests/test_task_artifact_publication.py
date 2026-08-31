"""Actual task Worker -> durable tool -> stored bytes -> verification regression."""

import asyncio
import base64
import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from test_persistent_team_task_contracts import accept, propose, resource
from test_task_verification_service import policy
from test_team_agent_dispatcher import dispatch, record, stack
from test_team_task_result_projection import attach_scheduler, state

from coifesp_harness.agent_runs import DurableAgentWorker
from coifesp_harness.artifacts import (
    ArtifactContentService,
    LocalImmutableArtifactStore,
)
from coifesp_harness.artifacts.repository import (
    ARTIFACT_COMMANDS,
    ARTIFACT_MANIFESTS,
    SQLAlchemyArtifactRepository,
)
from coifesp_harness.artifacts.task_publication import (
    TOOL_NAME,
    TaskArtifactPublicationService,
    TaskArtifactPublicationTool,
    task_artifact_manifest,
)
from coifesp_harness.audit import InMemoryAuditSink
from coifesp_harness.errors import PolicyDenied
from coifesp_harness.idempotency import InMemoryIdempotencyStore
from coifesp_harness.product.repository import (
    PROJECT_RESOURCES,
    PROJECT_TEAMS,
    TEAM_PROJECT_AGENTS,
    TEAM_TASKS,
)
from coifesp_harness.project_process.repository import PROJECT_PROCESS_EVENTS
from coifesp_harness.runtime import AgentLoop, AuthorizedTool, LLMResponse, ToolCall
from coifesp_harness.security import PolicyEngine, Principal
from coifesp_harness.team_agents.accounting import TeamTaskRunAccounting
from coifesp_harness.team_agents.identity import TeamAgentPrincipalResolver
from coifesp_harness.team_agents.profiles import TeamAgentCapabilityResolver
from coifesp_harness.team_agents.task_contracts import PersistentTaskDispatchFactLoader
from coifesp_harness.team_agents.task_projection import TeamTaskResultProjection
from coifesp_harness.tool_jobs import (
    DurableToolWorker,
    SQLAlchemyToolJobRepository,
    ToolBatchCoordinator,
    ToolJobKeyring,
)
from coifesp_harness.tool_jobs.repository import TOOL_JOBS
from coifesp_harness.tool_jobs.worker import ToolExecutionContext
from coifesp_harness.tools import ToolExecutor, ToolRegistry
from coifesp_harness.verification.repository import (
    TASK_VERIFICATIONS,
    VERIFICATION_METADATA,
)
from coifesp_harness.verification.service import TaskVerificationService


def prepare(tmp_path, *, private_input=False, shared_input=False, arguments=None):
    value = stack(accepted=False)
    value.arguments = arguments or {
        "title": "Implementation notes", "media_type": "text/plain",
        "content": "An actual team-produced document.\n", "encoding": "utf8",
        "propagation": "project_readonly",
    }
    artifact_repository = SQLAlchemyArtifactRepository(
        engine=value.engine, audit_log=value.capabilities.audit_log,
    )
    artifact_repository.create_schema()
    store = LocalImmutableArtifactStore(tmp_path)
    overrides = {}
    if private_input or shared_input:
        mode = "team_private" if private_input else "project_readonly"
        resource(value, tmp_path, owner="team-b", propagation=mode)
        overrides["input_manifest"] = {"work_nodes": [], "resources": [{
            "resource_id": "resource-input", "mode": mode, "required": True,
        }]}
    value.content = ArtifactContentService(artifact_repository, store)
    manifest = task_artifact_manifest()
    value.dispatcher.runtime_resolver = TeamAgentCapabilityResolver(
        engine=value.engine, tool_policies={"default": (
            AuthorizedTool(manifest.tool_id, manifest.version, manifest.schema_digest),
        )},
    )
    propose(value, verification_policy=policy(), **overrides)
    accept(value)
    value.dispatcher.fact_loader = PersistentTaskDispatchFactLoader(
        engine=value.engine, artifact_content=value.content,
    )
    record(value, "decision-publication")
    value.dispatched = dispatch(value, decision_id="decision-publication")
    value.jobs = SQLAlchemyToolJobRepository(
        engine=value.engine, keyring=ToolJobKeyring(master_key=b"t" * 32, key_id="test"),
    )
    value.jobs.create_schema()
    value.publisher = TaskArtifactPublicationService(
        repository=value.repository, runs=value.runs, jobs=value.jobs,
        artifact_content=value.content,
    )
    value.coordinator = ToolBatchCoordinator(
        engine=value.engine, agent_runs=value.runs, tool_jobs=value.jobs,
    )

    class Provider:
        calls = 0

        async def complete(self, **_):
            self.calls += 1
            if self.calls == 1:
                return LLMResponse(
                    tool_calls=(ToolCall("publish-one", TOOL_NAME, value.arguments),),
                    input_tokens=4, output_tokens=3,
                )
            with value.engine.connect() as connection:
                refs = list(connection.execute(select(PROJECT_RESOURCES.c.resource_id).where(
                    PROJECT_RESOURCES.c.source_run_id == value.dispatched.run_id,
                )).scalars())
            return LLMResponse(text=json.dumps({
                "schema": "coifesp.task-output.v1", "artifact_refs": refs,
                "summary": "Produced the requested document.", "known_limitations": [],
            }), input_tokens=6, output_tokens=3)

    model_registry = ToolRegistry()
    model_registry.register(manifest.declaration())
    audit = InMemoryAuditSink()
    loop = AgentLoop(
        provider=Provider(), registry=model_registry, durable_tools=True, audit=audit,
        executor=ToolExecutor(registry=model_registry, policy=PolicyEngine(), audit=audit,
                              idempotency=InMemoryIdempotencyStore()),
    )

    class NoHumanResolver:
        async def resolve(self, **_):
            raise AssertionError("task artifact execution cannot impersonate a human")

    value.agent_worker = DurableAgentWorker(
        service=value.dispatcher.run_service, loop=loop, tool_dispatcher=value.coordinator,
        principal_resolver=TeamAgentPrincipalResolver(engine=value.engine, human_resolver=NoHumanResolver()),
    )
    value.worker_identity = Principal("artifact-agent-worker", "team-b",
        roles=frozenset({"agent_worker"}), is_service=True)
    value.tool_registry = ToolRegistry()
    value.tool_registry.register(TaskArtifactPublicationTool(value.publisher).definition())
    return value


def pending(value):
    outcome = asyncio.run(value.agent_worker.process_once(worker=value.worker_identity))
    assert outcome.status.value == "awaiting_tool"


def claim(value):
    pending(value)
    lease = value.jobs.claim_next(tenant_id="team-b", worker_id="publisher", lease_seconds=60)
    assert lease is not None
    value.jobs.start(tenant_id="team-b", worker_id="publisher",
                     job_id=lease.job.job_id, lease_token=lease.lease_token)
    return ToolExecutionContext("team-b", lease.job.job_id, value.dispatched.run_id,
        lease.job.call_id, lease.job.idempotency_key, "publisher", lease.lease_token)


def publications(value):
    with value.engine.connect() as connection:
        return connection.execute(select(PROJECT_RESOURCES).where(
            PROJECT_RESOURCES.c.source_run_id == value.dispatched.run_id,
        )).mappings().all()


def published_events(value):
    with value.engine.connect() as connection:
        return connection.execute(select(PROJECT_PROCESS_EVENTS).where(
            PROJECT_PROCESS_EVENTS.c.event_type == "artifact.published",
        )).mappings().all()


def test_actual_workers_publish_new_bytes_then_submit_and_verify_once(tmp_path):
    value = prepare(tmp_path)
    attach_scheduler(value)
    VERIFICATION_METADATA.create_all(value.engine)
    accounting = TeamTaskRunAccounting(repository=value.repository,
        run_repository=value.runs, capability_repository=value.capabilities)
    projection = TeamTaskResultProjection(repository=value.repository,
        run_repository=value.runs, artifact_content=value.content)
    verifier = TaskVerificationService(repository=value.repository, artifact_content=value.content)

    def terminal(run):
        accounting.on_run_terminal(run)
        projection.on_run_terminal(run)
        verifier.on_run_terminal(run)

    value.dispatcher.run_service.terminal_callback = terminal
    pending(value)
    worker = DurableToolWorker(repository=value.jobs, registry=value.tool_registry,
        tenant_id="team-b", worker_id="publisher", reconciler=value.coordinator)
    assert asyncio.run(worker.run_once())
    jobs = value.jobs.list_for_run(tenant_id="team-b", run_id=value.dispatched.run_id)
    assert [job.status.value for job in jobs] == ["succeeded"]
    rows = publications(value)
    assert len(rows) == 1
    row = rows[0]
    assert row["created_by"] is None and row["produced_by_principal_id"] == "team-agent:team-b"
    assert row["process_id"] == "process-a" and row["source_integration_id"] is None
    assert b"".join(value.content.store.open(tenant_id="team-b", sha256=row["artifact_sha256"])) == value.arguments["content"].encode()
    assert len(published_events(value)) == 1
    assert value.arguments["content"] not in str(published_events(value))
    outcome = asyncio.run(value.agent_worker.process_once(worker=value.worker_identity))
    assert outcome.status.value == "completed"
    assert state(value)[0]["status"] == "verified"
    assert json.loads(state(value)[0]["artifact_resource_ids"]) == [row["resource_id"]]
    verifier.verify_run(run_id=value.dispatched.run_id)
    assert projection.replay_pending() == 0
    with value.engine.connect() as connection:
        assert connection.execute(select(func.count()).select_from(TASK_VERIFICATIONS)).scalar_one() == 1
    assert not asyncio.run(worker.run_once())


def test_repeat_publication_reuses_resource_manifest_and_fact(tmp_path):
    value = prepare(tmp_path)
    context = claim(value)
    first = value.publisher.publish(context=context, arguments=value.arguments)
    second = value.publisher.publish(context=context, arguments=value.arguments)
    assert first["resource_id"] == second["resource_id"]
    assert not first["duplicate"] and second["duplicate"]
    assert len(publications(value)) == len(published_events(value)) == 1
    with value.engine.connect() as connection:
        for table in (ARTIFACT_MANIFESTS, ARTIFACT_COMMANDS):
            assert connection.execute(select(func.count()).select_from(table)).scalar_one() == 1


@pytest.mark.parametrize("encoding,raw", [("base64", b"\x00\xff\x01"), ("utf8", b"")])
def test_binary_and_empty_artifacts_preserve_exact_bytes(tmp_path, encoding, raw):
    args = {"title": "Output", "media_type": "text/plain", "encoding": encoding,
            "content": base64.b64encode(raw).decode() if encoding == "base64" else "",
            "propagation": "project_readonly"}
    value = prepare(tmp_path, arguments=args)
    result = value.publisher.publish(context=claim(value), arguments=args)
    assert result["sha256"] == hashlib.sha256(raw).hexdigest()
    assert b"".join(value.content.store.open(tenant_id="team-b", sha256=result["sha256"])) == raw


@pytest.mark.parametrize("mutation", ["wrong_call", "wrong_token", "changed_payload", "expired", "task_stale", "agent_revoked"])
def test_unbound_or_stale_publication_cannot_write(tmp_path, mutation):
    value = prepare(tmp_path)
    context = claim(value)
    arguments = dict(value.arguments)
    if mutation == "wrong_call":
        context = replace(context, call_id="not-requested")
    elif mutation == "wrong_token":
        context = replace(context, lease_token="stale")
    elif mutation == "changed_payload":
        arguments["content"] = "Not authorized by this pending tool call"
    else:
        with value.engine.begin() as connection:
            if mutation == "expired":
                connection.execute(TOOL_JOBS.update().values(lease_expires_at=datetime.now(UTC) - timedelta(seconds=1)))
            elif mutation == "task_stale":
                connection.execute(TEAM_TASKS.update().values(source_contract_version=2, accepted_contract_version=2))
            else:
                connection.execute(TEAM_PROJECT_AGENTS.update().values(status="archived"))
    with pytest.raises(PolicyDenied):
        value.publisher.publish(context=context, arguments=arguments)
    assert not publications(value) and not published_events(value)


@pytest.mark.parametrize("shared", [True, False])
def test_private_inputs_allow_private_draft_but_never_automatic_sharing(tmp_path, shared):
    args = {"title": "Private result", "media_type": "text/plain", "encoding": "utf8",
            "content": "Internal analysis", "propagation": "project_readonly" if shared else "team_private"}
    value = prepare(tmp_path, private_input=True, arguments=args)
    context = claim(value)
    if shared:
        with pytest.raises(PolicyDenied, match="disclosure review"):
            value.publisher.publish(context=context, arguments=args)
        assert not publications(value)
    else:
        result = value.publisher.publish(context=context, arguments=args)
        assert result["propagation"] == "team_private"
        assert len(publications(value)) == 1
    assert not published_events(value)


def test_event_failure_rolls_back_resource_registry_and_receipt_then_retries(tmp_path):
    value = prepare(tmp_path)
    context = claim(value)

    def crash(connection, event):
        raise RuntimeError("simulated transaction interruption")

    value.repository.set_event_listener(crash)
    with pytest.raises(RuntimeError, match="interruption"):
        value.publisher.publish(context=context, arguments=value.arguments)
    assert not publications(value) and not published_events(value)
    with value.engine.connect() as connection:
        for table in (ARTIFACT_MANIFESTS, ARTIFACT_COMMANDS):
            assert connection.execute(select(func.count()).select_from(table)).scalar_one() == 0
    value.repository.set_event_listener(None)
    assert not value.publisher.publish(context=context, arguments=value.arguments)["duplicate"]
    assert len(publications(value)) == len(published_events(value)) == 1


def test_storage_unavailable_keeps_job_retryable_then_publishes_once(tmp_path, monkeypatch):
    value = prepare(tmp_path)
    pending(value)
    original = value.content.store.put

    def unavailable(**kwargs):
        raise OSError("temporary object store outage")

    monkeypatch.setattr(value.content.store, "put", unavailable)
    worker = DurableToolWorker(repository=value.jobs, registry=value.tool_registry,
        tenant_id="team-b", worker_id="publisher", reconciler=value.coordinator)
    assert asyncio.run(worker.run_once())
    jobs = value.jobs.list_for_run(tenant_id="team-b", run_id=value.dispatched.run_id)
    assert jobs[0].status.value == "retry_wait"
    assert jobs[0].error_code == "task_artifact_publication_unavailable"
    assert not publications(value) and not published_events(value)
    monkeypatch.setattr(value.content.store, "put", original)
    with value.engine.begin() as connection:
        connection.execute(TOOL_JOBS.update().values(available_at=datetime.now(UTC) - timedelta(seconds=1)))
    assert asyncio.run(worker.run_once())
    assert value.jobs.list_for_run(tenant_id="team-b", run_id=value.dispatched.run_id)[0].status.value == "succeeded"
    assert len(publications(value)) == len(published_events(value)) == 1


def test_lease_expiring_during_object_write_does_not_commit_publication(tmp_path, monkeypatch):
    value = prepare(tmp_path)
    context = claim(value)
    original = value.content.store.put

    def slow_storage(**kwargs):
        stored = original(**kwargs)
        value.publisher.clock = lambda: datetime.now(UTC) + timedelta(minutes=5)
        return stored

    monkeypatch.setattr(value.content.store, "put", slow_storage)
    with pytest.raises(PolicyDenied, match="expired"):
        value.publisher.publish(context=context, arguments=value.arguments)
    assert not publications(value) and not published_events(value)
    with value.engine.connect() as connection:
        assert connection.execute(select(func.count()).select_from(ARTIFACT_COMMANDS)).scalar_one() == 0


def test_crash_after_publication_before_job_receipt_recovers_without_duplicate(tmp_path):
    value = prepare(tmp_path)
    context = claim(value)
    original = value.publisher.publish(context=context, arguments=value.arguments)
    # The side-effect committed, but the old worker died before jobs.succeed.
    with value.engine.begin() as connection:
        connection.execute(TOOL_JOBS.update().values(lease_expires_at=datetime.now(UTC) - timedelta(seconds=1)))
    value.jobs.recover_expired(tenant_id="team-b", actor_id="recovery", retry_delay_seconds=1)
    with value.engine.begin() as connection:
        connection.execute(TOOL_JOBS.update().values(available_at=datetime.now(UTC) - timedelta(seconds=1)))
    worker = DurableToolWorker(repository=value.jobs, registry=value.tool_registry,
        tenant_id="team-b", worker_id="replacement", reconciler=value.coordinator)
    assert asyncio.run(worker.run_once())
    job = value.jobs.get(tenant_id="team-b", job_id=context.job_id, include_payloads=True)
    assert job.status.value == "succeeded"
    assert job.result["resource_id"] == original["resource_id"] and job.result["duplicate"]
    assert len(publications(value)) == len(published_events(value)) == 1


def test_sharing_and_delegation_reads_use_postgresql_row_locks(tmp_path):
    from sqlalchemy import event
    from sqlalchemy.dialects import postgresql

    value = prepare(tmp_path, shared_input=True)
    context = claim(value)
    statements = []

    def observe(connection, clause, multiparams, params, execution_options):
        if getattr(clause, "_for_update_arg", None) is not None:
            statements.append(str(clause.compile(dialect=postgresql.dialect())))

    event.listen(value.engine, "before_execute", observe)
    try:
        value.publisher.publish(context=context, arguments=value.arguments)
    finally:
        event.remove(value.engine, "before_execute", observe)
    for table in (PROJECT_TEAMS, TEAM_PROJECT_AGENTS, PROJECT_RESOURCES):
        assert any(f"FROM {table.name}" in sql and "FOR UPDATE" in sql for sql in statements), statements


def test_custom_governance_context_cannot_bypass_project_scope(tmp_path):
    from types import SimpleNamespace

    from coifesp_harness.context import ContextSource
    from coifesp_harness.security import Classification, ResourceLabel

    value = prepare(tmp_path)
    with value.engine.connect() as connection:
        task = connection.execute(select(TEAM_TASKS)).mappings().one()
        item = SimpleNamespace(source=ContextSource.GOVERNANCE,
            label=ResourceLabel("other-team", Classification.INTERNAL,
                frozenset({"project:another-project"}), "private-note"))
        with pytest.raises(PolicyDenied, match="task scope"):
            value.publisher._assert_shareable_inputs(connection, task, {"context_items": (item,)})
