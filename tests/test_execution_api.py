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
