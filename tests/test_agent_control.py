import asyncio
import json
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.pool import StaticPool
from starlette.websockets import WebSocketDisconnect

from coifesp_harness.agent_runs import (
    AGENT_RUN_COMMANDS,
    AgentCheckpointKeyring,
    AgentControlKeyring,
    AgentControlStatus,
    AgentControlType,
    AgentRunCheckpointCodec,
    AgentRunPersistenceError,
    AgentRunService,
    AgentWorkerOutcomeStatus,
    DurableAgentWorker,
    DurableRunStatus,
    SQLAlchemyAgentRunRepository,
)
from coifesp_harness.audit import InMemoryAuditSink
from coifesp_harness.auth import VerifiedIdentity
from coifesp_harness.config import Settings
from coifesp_harness.control_plane import create_app
from coifesp_harness.control_plane.agent_control_routes import CONTROL_SUBPROTOCOL
from coifesp_harness.errors import IdempotencyConflict, ResourceNotFound
from coifesp_harness.idempotency import InMemoryIdempotencyStore
from coifesp_harness.runtime import AgentLoop, AgentRunRequest, LLMResponse, Message
from coifesp_harness.security import PolicyEngine, Principal
from coifesp_harness.tools import ToolExecutor, ToolRegistry


class Verifier:
    def __init__(self, identities):
        self.identities = identities

    async def verify(self, token):
        identity = self.identities.get(token)
        if identity is None:
            from coifesp_harness.errors import AuthenticationError

            raise AuthenticationError("invalid_token")
        return identity


class Resolver:
    def __init__(self, principal):
        self.principal = principal

    async def resolve(self, **_):
        return self.principal


class CapturingProvider:
    def __init__(self):
        self.calls = []

    async def complete(self, *, messages, **_):
        self.calls.append(messages)
        return LLMResponse(text="done", input_tokens=3, output_tokens=1)


class CompletionRaceService(AgentRunService):
    def __init__(self, repository, owner):
        super().__init__(repository)
        self.owner = owner
        self.polls = 0
        self.injected = False

    def pending_control(self, **kwargs):
        commands = super().pending_control(**kwargs)
        self.polls += 1
        if self.polls == 2 and not self.injected:
            run = self.get(principal=self.owner, run_id=kwargs["run_id"])
            self.submit_control(
                principal=self.owner,
                run_id=run.run_id,
                command_id="completion-race",
                command_type=AgentControlType.FOLLOW_UP,
                content="continue with the newly arrived partner feedback",
                expected_run_version=run.version,
            )
            self.injected = True
        return commands


def _settings():
    return Settings.from_environment(
        {
            "COIFESP_ENV": "test",
            "COIFESP_OIDC_ISSUER": "https://identity.example.test",
            "COIFESP_OIDC_AUDIENCE": "coifesp",
            "COIFESP_OIDC_AUTHORIZED_PARTIES": "test-client",
        }
    )


def _identity(principal):
    return VerifiedIdentity(
        principal=principal,
        issuer="https://identity.example.test",
        audience="coifesp",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
        token_id=f"token-{principal.principal_id}",
    )


def _stack():
    engine = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    repository = SQLAlchemyAgentRunRepository(
        engine=engine,
        keyring=AgentCheckpointKeyring(master_key=b"k" * 32, key_id="checkpoint-v1"),
        control_keyring=AgentControlKeyring(master_key=b"k" * 32, key_id="control-v1"),
    )
    repository.create_schema()
    return engine, repository, AgentRunService(repository)


def _request(owner, run_id="run-control"):
    return AgentRunRequest(
        run_id=run_id,
        correlation_id=f"corr-{run_id}",
        principal=owner,
        messages=(Message("user", "complete the team task"),),
    )


def _create(service, owner, run_id="run-control"):
    value = _request(owner, run_id)
    return service.create(
        principal=owner,
        run_id=run_id,
        correlation_id=value.correlation_id,
        idempotency_key=f"idem-{run_id}",
        checkpoint=AgentRunCheckpointCodec().initial(value),
    )


def _worker():
    return Principal(
        "worker",
        "team-a",
        roles=frozenset({"agent_worker"}),
        is_service=True,
    )


def _control_message(command, content):
    return Message(
        "user",
        json.dumps(
            {
                "schema": "coifesp.agent-control.v1",
                "instruction_trust": "user_instruction",
                "sequence": command.sequence,
                "command_id": command.command_id,
                "command_type": command.command_type.value,
                "content": content,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ),
    )


def test_control_is_encrypted_idempotent_tenant_scoped_and_reopens_completed_run():
    engine, repository, service = _stack()
    owner = Principal("lead", "team-a")
    outsider = Principal("lead", "team-b")
    created = _create(service, owner)
    secret = "change the private deployment sequence"
    command = service.submit_control(
        principal=owner,
        run_id=created.run_id,
        command_id="command-1",
        command_type=AgentControlType.STEER,
        content=secret,
        expected_run_version=created.version,
    )
    duplicate = service.submit_control(
        principal=owner,
        run_id=created.run_id,
        command_id="command-1",
        command_type=AgentControlType.STEER,
        content=secret,
        expected_run_version=created.version + 100,
    )
    assert duplicate.sequence == command.sequence
    with pytest.raises(IdempotencyConflict):
        service.submit_control(
            principal=owner,
            run_id=created.run_id,
            command_id="command-1",
            command_type=AgentControlType.STEER,
            content="different",
            expected_run_version=created.version,
        )
    with pytest.raises(ResourceNotFound):
        service.list_control(principal=outsider, run_id=created.run_id)
    with engine.connect() as connection:
        row = connection.execute(select(AGENT_RUN_COMMANDS)).mappings().one()
    assert secret.encode() not in bytes(row["content_ciphertext"])
    assert row["status"] == "pending"

    _, _, service = _stack()
    completed_id = "run-completed"
    completed = _create(service, owner, completed_id)
    worker = _worker()
    lease = service.claim(worker=worker)
    assert lease is not None and lease.run.run_id == completed_id
    service.start(worker=worker, run_id=completed_id, lease_token=lease.lease_token)
    checkpoint = lease.checkpoint
    checkpoint["usage"] = {"turns": 1, "tool_calls": 0, "total_tokens": 1}
    completed = service.checkpoint(
        worker=worker,
        run_id=completed_id,
        lease_token=lease.lease_token,
        target=DurableRunStatus.COMPLETED,
        checkpoint=checkpoint,
        turns=1,
        tool_calls=0,
        total_tokens=1,
    )
    follow_up = service.submit_control(
        principal=owner,
        run_id=completed_id,
        command_id="follow-up-1",
        command_type=AgentControlType.FOLLOW_UP,
        content="prepare the release notes",
        expected_run_version=completed.version,
    )
    assert follow_up.status is AgentControlStatus.PENDING
    assert service.get(principal=owner, run_id=completed_id).status is DurableRunStatus.QUEUED


def test_checkpoint_application_is_content_bound_and_completion_race_requeues():
    _, _, service = _stack()
    owner = Principal("lead", "team-a")
    created = _create(service, owner)
    content = "use the reviewed interface contract"
    command = service.submit_control(
        principal=owner,
        run_id=created.run_id,
        command_id="command-1",
        command_type=AgentControlType.STEER,
        content=content,
        expected_run_version=created.version,
    )
    worker = _worker()
    lease = service.claim(worker=worker)
    assert lease is not None
    service.start(worker=worker, run_id=created.run_id, lease_token=lease.lease_token)

    bad = dict(lease.checkpoint)
    bad["control_cursor"] = command.sequence
    bad["usage"] = {"turns": 1, "tool_calls": 0, "total_tokens": 1}
    with pytest.raises(AgentRunPersistenceError):
        service.checkpoint(
            worker=worker,
            run_id=created.run_id,
            lease_token=lease.lease_token,
            target=DurableRunStatus.COMPLETED,
            checkpoint=bad,
            turns=1,
            tool_calls=0,
            total_tokens=1,
            applied_control_sequences=(command.sequence,),
        )
    assert service.list_control(principal=owner, run_id=created.run_id)[0].status is AgentControlStatus.PENDING

    raced = dict(lease.checkpoint)
    raced["usage"] = {"turns": 1, "tool_calls": 0, "total_tokens": 1}
    requeued = service.checkpoint(
        worker=worker,
        run_id=created.run_id,
        lease_token=lease.lease_token,
        target=DurableRunStatus.COMPLETED,
        checkpoint=raced,
        turns=1,
        tool_calls=0,
        total_tokens=1,
    )
    assert requeued.status is DurableRunStatus.QUEUED

    next_lease = service.claim(worker=worker)
    assert next_lease is not None
    service.start(worker=worker, run_id=created.run_id, lease_token=next_lease.lease_token)
    applied = dict(next_lease.checkpoint)
    applied["messages"] = [*applied["messages"], AgentRunCheckpointCodec._message_json(_control_message(command, content))]
    applied["control_cursor"] = command.sequence
    applied["usage"] = {"turns": 2, "tool_calls": 0, "total_tokens": 2}
    finished = service.checkpoint(
        worker=worker,
        run_id=created.run_id,
        lease_token=next_lease.lease_token,
        target=DurableRunStatus.COMPLETED,
        checkpoint=applied,
        turns=2,
        tool_calls=0,
        total_tokens=2,
        applied_control_sequences=(command.sequence,),
    )
    assert finished.status is DurableRunStatus.COMPLETED
    stored = service.list_control(principal=owner, run_id=created.run_id)[0]
    assert stored.status is AgentControlStatus.APPLIED
    assert stored.applied_run_version == finished.version


def test_worker_polls_and_atomically_applies_durable_control():
    _, _, service = _stack()
    owner = Principal("lead", "team-a")
    created = _create(service, owner)
    content = "include the compatibility test requested by the partner team"
    service.submit_control(
        principal=owner,
        run_id=created.run_id,
        command_id="command-1",
        command_type=AgentControlType.STEER,
        content=content,
        expected_run_version=created.version,
    )
    provider = CapturingProvider()
    registry = ToolRegistry()
    audit = InMemoryAuditSink()
    loop = AgentLoop(
        provider=provider,
        registry=registry,
        executor=ToolExecutor(
            registry=registry,
            policy=PolicyEngine(),
            audit=audit,
            idempotency=InMemoryIdempotencyStore(),
        ),
        audit=audit,
    )
    durable_worker = DurableAgentWorker(
        service=service,
        loop=loop,
        principal_resolver=Resolver(owner),
        heartbeat_interval_seconds=1,
    )
    outcome = asyncio.run(durable_worker.process_once(worker=_worker()))
    assert outcome.status is AgentWorkerOutcomeStatus.COMPLETED
    assert any(content in message.content for message in provider.calls[0])
    command = service.list_control(principal=owner, run_id=created.run_id)[0]
    assert command.status is AgentControlStatus.APPLIED
    assert command.applied_run_version == service.get(principal=owner, run_id=created.run_id).version
    events = service.events(principal=owner, run_id=created.run_id)
    applied_event = next(event for event in events if event.event_type == "agent_run.control_applied")
    assert applied_event.data["sequence"] == command.sequence
    assert content not in json.dumps([event.data for event in events])


def test_worker_reports_continuation_when_follow_up_races_completion():
    _, repository, base_service = _stack()
    owner = Principal("lead", "team-a")
    _create(base_service, owner)
    service = CompletionRaceService(repository, owner)
    provider = CapturingProvider()
    registry = ToolRegistry()
    audit = InMemoryAuditSink()
    loop = AgentLoop(
        provider=provider,
        registry=registry,
        executor=ToolExecutor(
            registry=registry,
            policy=PolicyEngine(),
            audit=audit,
            idempotency=InMemoryIdempotencyStore(),
        ),
        audit=audit,
    )
    worker = DurableAgentWorker(
        service=service,
        loop=loop,
        principal_resolver=Resolver(owner),
        heartbeat_interval_seconds=1,
    )
    outcome = asyncio.run(worker.process_once(worker=_worker()))
    assert outcome.status is AgentWorkerOutcomeStatus.CONTINUATION_QUEUED
    assert service.get(principal=owner, run_id="run-control").status is DurableRunStatus.QUEUED
    command = service.list_control(principal=owner, run_id="run-control")[0]
    assert command.status is AgentControlStatus.PENDING


def test_terminal_failure_rejects_pending_control_with_reason():
    _, _, service = _stack()
    owner = Principal("lead", "team-a")
    created = _create(service, owner)
    service.submit_control(
        principal=owner,
        run_id=created.run_id,
        command_id="command-1",
        command_type=AgentControlType.STEER,
        content="do not publish the unreviewed artifact",
        expected_run_version=created.version,
    )
    worker = _worker()
    lease = service.claim(worker=worker)
    assert lease is not None
    service.start(worker=worker, run_id=created.run_id, lease_token=lease.lease_token)
    service.abort(
        worker=worker,
        run_id=created.run_id,
        lease_token=lease.lease_token,
        error_code="policy_denied",
    )
    command = service.list_control(principal=owner, run_id=created.run_id)[0]
    assert command.status is AgentControlStatus.REJECTED
    assert command.rejected_at is not None
    assert command.rejection_code == "run_failed"


def test_websocket_requires_auth_and_subprotocol_and_supports_resume_without_ack_leakage():
    _, _, service = _stack()
    owner = Principal("lead", "team-a")
    outsider = Principal("outsider", "team-b")
    created = _create(service, owner)
    app = create_app(
        settings=_settings(),
        verifier=Verifier({"owner": _identity(owner), "outsider": _identity(outsider)}),
        agent_run_service=service,
    )
    with TestClient(app) as client:
        with pytest.raises(WebSocketDisconnect) as missing_protocol:
            with client.websocket_connect(
                f"/v1/agent-runs/{created.run_id}/control",
                headers={"Authorization": "Bearer owner"},
            ):
                pass
        assert missing_protocol.value.code == 1002
        with pytest.raises(WebSocketDisconnect) as hidden:
            with client.websocket_connect(
                f"/v1/agent-runs/{created.run_id}/control",
                headers={"Authorization": "Bearer outsider"},
                subprotocols=[CONTROL_SUBPROTOCOL],
            ):
                pass
        assert hidden.value.code == 1008

        secret = "private partner deployment detail"
        with client.websocket_connect(
            f"/v1/agent-runs/{created.run_id}/control",
            headers={"Authorization": "Bearer owner"},
            subprotocols=[CONTROL_SUBPROTOCOL],
        ) as websocket:
            ready = websocket.receive_json()
            assert ready["type"] == "control.ready"
            assert websocket.accepted_subprotocol == CONTROL_SUBPROTOCOL
            websocket.send_json(
                {
                    "type": "command.submit",
                    "command_id": "command-ws-1",
                    "command_type": "steer",
                    "content": secret,
                    "expected_run_version": ready["run"]["version"],
                }
            )
            accepted = websocket.receive_json()
            assert accepted["type"] == "command.accepted"
            assert "content" not in accepted["command"]
            assert secret not in json.dumps(accepted)
            websocket.send_json(
                {
                    "type": "session.resume",
                    "after_command_sequence": 0,
                    "after_event_sequence": 0,
                }
            )
            snapshot = websocket.receive_json()
            assert snapshot["type"] == "session.snapshot"
            assert snapshot["commands"][0]["content"] == secret
            websocket.send_json(
                {
                    "type": "command.submit",
                    "command_id": "command-ws-2",
                    "command_type": "steer",
                    "content": "another instruction",
                    "expected_run_version": 999,
                }
            )
            conflict = websocket.receive_json()
            assert conflict == {
                "type": "control.error",
                "code": "state_conflict",
                "command_id": "command-ws-2",
            }
