import asyncio
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from coifesp_harness.auth import VerifiedIdentity
from coifesp_harness.config import Settings
from coifesp_harness.control_plane import create_app
from coifesp_harness.errors import AuthenticationError
from coifesp_harness.execution import SQLAlchemyTaskRepository, TaskExecutionService
from coifesp_harness.execution.models import ExecutionTask, TaskStatus
from coifesp_harness.security import Classification, Principal


class StubVerifier:
    def __init__(self, identities):
        self.identities = identities

    async def verify(self, token: str):
        identity = self.identities.get(token)
        if identity is None:
            raise AuthenticationError()
        return identity


class UnusedGovernance:
    pass


class ProjectWorkExecutionStub:
    def __init__(self) -> None:
        self.request = None

    def enqueue_project_work(self, **kwargs):
        self.request = kwargs
        now = datetime.now(UTC)
        return ExecutionTask(
            task_id="project-work:task-1-v3",
            tenant_id=kwargs["principal"].tenant_id,
            queue=kwargs["principal"].tenant_id,
            payload={"schema": "coifesp.project-work-execution.v1"},
            request_digest="digest",
            status=TaskStatus.QUEUED,
            priority=10,
            max_attempts=3,
            attempt_count=0,
            available_at=now,
            dependencies=(),
            created_by=kwargs["principal"].principal_id,
            project_id="project-1",
            process_id=kwargs["process_id"],
            team_task_id=kwargs["team_task_id"],
            work_node_id=kwargs["work_node_id"],
            contract_version=kwargs["contract_version"],
        )


def identity(principal: Principal) -> VerifiedIdentity:
    return VerifiedIdentity(
        principal=principal,
        issuer="https://identity.example.test",
        audience="coifesp-control-plane",
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
        token_id=None,
    )


async def request(app, method: str, path: str, **kwargs):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="https://control.example.test",
    ) as client:
        return await client.request(method, path, **kwargs)


def test_worker_api_uses_service_identity_and_fenced_lifecycle() -> None:
    engine = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    repository = SQLAlchemyTaskRepository(engine=engine)
    repository.create_schema()
    repository.enqueue(
        tenant_id="team-a",
        actor_id="contributor-a",
        idempotency_key="enqueue-1",
        task_id="task-1",
        queue="coding",
        payload={"operation": "test"},
    )
    service = TaskExecutionService(
        repository=repository,
        governance=UnusedGovernance(),  # type: ignore[arg-type]
    )
    user = Principal("contributor-a", "team-a")
    worker = Principal(
        "worker-a",
        "team-a",
        roles=frozenset({"execution_worker"}),
        clearance=Classification.INTERNAL,
        is_service=True,
    )
    settings = Settings.from_environment(
        {
            "COIFESP_ENV": "test",
            "COIFESP_OIDC_ISSUER": "https://identity.example.test",
            "COIFESP_OIDC_AUDIENCE": "coifesp-control-plane",
            "COIFESP_OIDC_AUTHORIZED_PARTIES": "test-client",
            "COIFESP_OIDC_JWKS_URL": "https://identity.example.test/jwks",
        }
    )
    app = create_app(
        settings=settings,
        verifier=StubVerifier(
            {
                "user-token": identity(user),
                "worker-token": identity(worker),
            }
        ),
        task_execution_service=service,
    )

    denied = asyncio.run(
        request(
            app,
            "POST",
            "/v1/worker/queues/coding:claim",
            json={"lease_seconds": 60},
            headers={"Authorization": "Bearer user-token"},
        )
    )
    claimed = asyncio.run(
        request(
            app,
            "POST",
            "/v1/worker/queues/coding:claim",
            json={"lease_seconds": 60},
            headers={"Authorization": "Bearer worker-token"},
        )
    )
    assert denied.status_code == 403
    assert claimed.status_code == 200, claimed.text
    lease_token = claimed.json()["lease_token"]
    assert claimed.json()["task"]["status"] == "leased"

    started = asyncio.run(
        request(
            app,
            "POST",
            "/v1/worker/executions/task-1:start",
            json={"lease_token": lease_token},
            headers={"Authorization": "Bearer worker-token"},
        )
    )
    completed = asyncio.run(
        request(
            app,
            "POST",
            "/v1/worker/executions/task-1:succeed",
            json={
                "lease_token": lease_token,
                "result": {"artifact_ref": "artifact://team-a/report"},
            },
            headers={"Authorization": "Bearer worker-token"},
        )
    )
    read = asyncio.run(
        request(
            app,
            "GET",
            "/v1/executions/task-1",
            headers={"Authorization": "Bearer user-token"},
        )
    )
    assert started.status_code == 200, started.text
    assert completed.status_code == 200, completed.text
    assert read.status_code == 200
    assert read.json()["status"] == "succeeded"
    assert lease_token not in read.text


def test_project_execution_api_uses_project_work_contract_without_assignment() -> None:
    service = ProjectWorkExecutionStub()
    user = Principal("contributor-a", "team-a")
    orchestrator = Principal(
        "service:project-orchestrator",
        "team-a",
        roles=frozenset({"project_orchestrator"}),
        is_service=True,
    )
    settings = Settings.from_environment(
        {
            "COIFESP_ENV": "test",
            "COIFESP_OIDC_ISSUER": "https://identity.example.test",
            "COIFESP_OIDC_AUDIENCE": "coifesp-control-plane",
            "COIFESP_OIDC_AUTHORIZED_PARTIES": "test-client",
            "COIFESP_OIDC_JWKS_URL": "https://identity.example.test/jwks",
        }
    )
    app = create_app(
        settings=settings,
        verifier=StubVerifier(
            {
                "user-token": identity(user),
                "orchestrator-token": identity(orchestrator),
            }
        ),
        task_execution_service=service,
    )

    denied = asyncio.run(
        request(
            app,
            "POST",
            "/v1/project-executions",
            json={
                "process_id": "process-1",
                "team_task_id": "team-task-1",
                "work_node_id": "work-node-1",
                "contract_version": 3,
            },
            headers={
                "Authorization": "Bearer user-token",
                "Idempotency-Key": "enqueue-project-work-denied",
            },
        )
    )
    response = asyncio.run(
        request(
            app,
            "POST",
            "/v1/project-executions",
            json={
                "process_id": "process-1",
                "team_task_id": "team-task-1",
                "work_node_id": "work-node-1",
                "contract_version": 3,
            },
            headers={
                "Authorization": "Bearer orchestrator-token",
                "Idempotency-Key": "enqueue-project-work-1",
            },
        )
    )

    assert denied.status_code == 403
    assert response.status_code == 201, response.text
    assert service.request == {
        "principal": orchestrator,
        "idempotency_key": "enqueue-project-work-1",
        "process_id": "process-1",
        "team_task_id": "team-task-1",
        "work_node_id": "work-node-1",
        "contract_version": 3,
    }
    body = response.json()
    assert body["program_id"] is None
    assert body["assignment_id"] is None
    assert body["project_id"] == "project-1"
    assert body["process_id"] == "process-1"
    assert body["team_task_id"] == "team-task-1"
    assert body["work_node_id"] == "work-node-1"
    assert body["contract_version"] == 3
