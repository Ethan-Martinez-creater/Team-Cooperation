import asyncio
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from coifesp_harness.auth import VerifiedIdentity
from coifesp_harness.config import Settings
from coifesp_harness.context import SemanticCheckpointKeyring, SemanticCheckpointService
from coifesp_harness.control_plane import create_app
from coifesp_harness.errors import AuthenticationError
from coifesp_harness.postgres_audit import AuditSigningKeyring, SQLAlchemyAuditLog
from coifesp_harness.security import Classification, Principal


class Verifier:
    def __init__(self, values): self.values = values
    async def verify(self, token):
        if token not in self.values: raise AuthenticationError()
        return self.values[token]


def identity(principal_id, roles=frozenset()):
    return VerifiedIdentity(principal=Principal(principal_id, "team-a", roles=roles,
        clearance=Classification.CONFIDENTIAL, compartments=frozenset({"project-x"})),
        issuer="https://identity.test", audience="control", expires_at=datetime.now(UTC)+timedelta(minutes=5),
        token_id=None)


def stack():
    engine = create_engine("sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False}, poolclass=StaticPool)
    audit = SQLAlchemyAuditLog(engine=engine, keyring=AuditSigningKeyring(
        active_key_id="audit-v1", verification_keys={"audit-v1": b"a"*32}))
    audit.create_schema()
    service = SemanticCheckpointService(engine=engine,
        keyring=SemanticCheckpointKeyring(active_key_id="memory-v1",
            keys={"memory-v1": b"m"*32}), audit=audit)
    service.create_schema()
    settings = Settings.from_environment({"COIFESP_ENV": "test",
        "COIFESP_OIDC_ISSUER": "https://identity.test", "COIFESP_OIDC_AUDIENCE": "control",
        "COIFESP_OIDC_AUTHORIZED_PARTIES": "client", "COIFESP_OIDC_JWKS_URL": "https://identity.test/jwks"})
    app = create_app(settings=settings, semantic_checkpoint_service=service,
        verifier=Verifier({"owner": identity("owner"),
            "reviewer": identity("reviewer", frozenset({"conversation_reviewer"})),
            "other": identity("other")}))
    return app, audit


async def call(app, method, path, token, **kwargs):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app,
            raise_app_exceptions=False), base_url="https://control.test") as client:
        return await client.request(method, path,
            headers={"Authorization": f"Bearer {token}"}, **kwargs)


def test_checkpoint_api_requires_review_and_only_owner_can_resume():
    app, audit = stack()
    body = {"checkpoint_id": "cp-api-1", "conversation_id": "conv-1",
        "messages": [{"role": "user", "content": "Finish migration"}],
        "summary": {"objective": "Finish migration", "constraints": ["No downtime"],
            "decisions": ["Blue-green"], "open_items": ["Cutover approval"],
            "verified_facts": ["Schema v3 exists"]},
        "classification": "confidential", "compartments": ["project-x"]}
    created = asyncio.run(call(app, "POST", "/v1/context/checkpoints", "owner", json=body))
    denied = asyncio.run(call(app, "POST", "/v1/context/checkpoints/cp-api-1:resume", "owner"))
    reviewed = asyncio.run(call(app, "POST", "/v1/context/checkpoints/cp-api-1/review",
        "reviewer", json={"expected_version": 1, "approve": True, "reason": "Checked source"}))
    outsider = asyncio.run(call(app, "POST", "/v1/context/checkpoints/cp-api-1:resume", "other"))
    resumed = asyncio.run(call(app, "POST", "/v1/context/checkpoints/cp-api-1:resume", "owner"))
    assert created.status_code == 201 and created.json()["status"] == "pending"
    assert denied.status_code == 403
    assert reviewed.status_code == 200 and reviewed.json()["version"] == 2
    assert outsider.status_code == 403
    assert resumed.status_code == 200 and '"instruction_trust":"data_only"' in resumed.json()["content"]
    assert audit.verify_tenant_chain("team-a") == 2
