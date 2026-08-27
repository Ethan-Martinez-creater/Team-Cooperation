from __future__ import annotations

import asyncio
import base64
import json
import pathlib
import shutil
from types import SimpleNamespace

import httpx
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from coifesp_harness.config import Settings
from coifesp_harness.control_plane import create_app
from coifesp_harness.control_plane.agent_capabilities import AgentCapabilityService
from coifesp_harness.product import (
    NotificationService,
    ProductAccountService,
    ProjectDirectoryService,
    TeamCollaborationService,
)
from coifesp_harness.security.policy import PolicyEngine
from coifesp_harness.skills import SkillCatalog, SkillTrustStore
from coifesp_harness.tool_catalog import build_builtin_manifests
from coifesp_harness.agent_runs import (
    AgentCheckpointKeyring,
    AgentRunService,
    AgentControlKeyring,
    SQLAlchemyAgentRunRepository,
)

PASSWORD = "Admin-Correct-Horse-42!"


def _write_skill_package(root, private, *, tenant, name="contract-review", version="1.0.0",
                         required_tools=()):
    package = root / tenant / name / version
    package.mkdir(parents=True, exist_ok=True)
    body = (
        "---\n"
        f"name: {name}\n"
        f"version: {version}\n"
        f"description: Review contracts for the team.\n"
        f"tenant_id: {tenant}\n"
        "classification: INTERNAL\n"
        "compartments: []\n"
        f"required_tools: {json.dumps(list(required_tools))}\n"
        "signer_key_id: key-1\n"
        "---\n"
        "Review every clause carefully.\n"
    ).encode()
    (package / "SKILL.md").write_bytes(body)
    (package / "SKILL.sig").write_bytes(base64.b64encode(private.sign(body)))


def _build_catalog(root, private, tenant):
    trust = SkillTrustStore()
    trust.register(tenant_id=tenant, key_id="key-1", public_key=private.public_key())
    catalog = SkillCatalog(root=root, trust_store=trust, policy=PolicyEngine())
    catalog.scan()
    return catalog


def stack(*, office=True, with_skills=True, skill_root_name=".test-cap-skills",
          skill_required_tools=()):
    engine = create_engine(
        "sqlite+pysqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    accounts = ProductAccountService(engine)
    accounts.create_schema()
    # Register the team first so signed skill packages target its real ID.
    team, bootstrap = accounts.register_team(
        team_id="team-cap", team_handle="cap-team", team_name="Cap"
    )
    accounts.change_initial_password(
        login=bootstrap.username,
        current_password=bootstrap.initial_password,
        new_password=PASSWORD,
    )
    session = accounts.login(login=bootstrap.username, password=PASSWORD)
    skill_root = pathlib.Path(skill_root_name)
    if skill_root.exists():
        shutil.rmtree(skill_root)
    skill_root.mkdir()
    catalog = None
    private = Ed25519PrivateKey.generate()
    if with_skills:
        _write_skill_package(
            skill_root, private, tenant=team.team_id,
            required_tools=skill_required_tools,
        )
        catalog = _build_catalog(skill_root, private, team.team_id)
    capabilities = AgentCapabilityService(
        manifests=build_builtin_manifests(
            sandbox_profile_ids=["python.isolated"],
            office_connector_configured=office,
        ),
        skill_catalog=catalog,
        policy=PolicyEngine(),
        llm_providers=(),
    )
    run_repository = SQLAlchemyAgentRunRepository(
        engine=engine,
        keyring=AgentCheckpointKeyring(master_key=b"k" * 32, key_id="test-v1"),
        control_keyring=AgentControlKeyring(master_key=b"c" * 32, key_id="control-v1"),
    )
    run_repository.create_schema()
    agent_run_service = AgentRunService(run_repository)
    app = create_app(
        settings=Settings.from_environment(
            {"COIFESP_ENV": "test", "COIFESP_AUTH_MODE": "builtin"}
        ),
        product_account_service=accounts,
        project_directory_service=ProjectDirectoryService(engine),
        team_collaboration_service=TeamCollaborationService(engine),
        notification_service=NotificationService(engine),
        agent_capabilities=capabilities,
        skill_catalog=catalog,
        agent_run_service=agent_run_service,
    )
    return app, SimpleNamespace(
        team_id=team.team_id, token=session.token, engine=engine
    ), skill_root


async def call(app, method, path, token=None, **kwargs):
    headers = kwargs.pop("headers", {})
    if token:
        headers["Authorization"] = f"Bearer {token}"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    ) as client:
        return await client.request(method, path, headers=headers, **kwargs)


def register_second_team(app, handle="other-team", name="Other"):
    team = asyncio.run(
        call(app, "POST", "/v1/teams/register", json={"handle": handle, "name": name})
    )
    details = team.json()
    asyncio.run(
        call(
            app,
            "POST",
            "/v1/accounts/change-initial-password",
            json={
                "login": details["administrator_username"],
                "current_password": details["administrator_initial_password"],
                "new_password": PASSWORD,
            },
        )
    )
    session = asyncio.run(
        call(
            app,
            "POST",
            "/v1/sessions",
            json={"login": details["administrator_username"], "password": PASSWORD},
        )
    )
    return details["team"]["team_id"], session.json()["access_token"]


def checkpoint_body(prompt="分析合同"):
    return {
        "schema": "coifesp.agent-run-checkpoint.v1",
        "messages": [
            {"role": "user", "content": prompt, "name": None, "tool_call_id": None,
             "tool_calls": []}
        ],
        "budget": {
            "max_turns": 4, "max_tool_calls": 4, "max_total_tokens": 4000,
            "max_model_cost_microusd": 100000,
        },
        "usage": {"turns": 0, "tool_calls": 0, "total_tokens": 0,
                  "model_cost_microusd": 0},
        "approval_bindings": [],
        "context": {"purpose": "workspace.agent", "budget": None, "items": []},
        "control_cursor": 0,
        "model_route_policy": {
            "data_classification": 1, "required_capabilities": [],
            "allowed_provider_ids": [], "residency_regions": [],
            "allow_external_egress": False, "max_call_cost_microusd": 100000,
            "max_output_tokens": 512, "max_call_total_tokens": 4000,
        },
    }


def test_capability_report_covers_all_four_statuses():
    app, principal, root = stack()
    try:
        response = asyncio.run(
            call(app, "GET", "/v1/agent-capabilities", principal.token)
        )
        assert response.status_code == 200, response.text
        report = response.json()
        statuses = {item["tool_id"]: item["status"] for item in report["tools"]}
        assert statuses["code.run_profile"] == "available"
        assert statuses["office.send_message"] == "approval_required"
        assert statuses["list_skills"] == "available"
        assert statuses["load_skill"] == "available"
        reasons = {item["tool_id"]: item["reason"] for item in report["tools"]}
        assert "审批" in reasons["office.send_message"]
        skills = report["skills"]
        assert [item["name"] for item in skills] == ["contract-review"]
        assert skills[0]["publisher_team"] == principal.team_id
        assert report["models"][0]["status"] == "not_configured"
        lowered = response.text.lower()
        for banned in ("password", "secret", "token", "http://", "https://"):
            assert banned not in lowered, banned
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_capability_report_marks_unconfigured_tools():
    app, principal, root = stack(office=False, with_skills=False)
    try:
        response = asyncio.run(
            call(app, "GET", "/v1/agent-capabilities", principal.token)
        )
        statuses = {item["tool_id"]: item["status"] for item in response.json()["tools"]}
        assert statuses["office.send_message"] == "not_configured"
        assert statuses["list_skills"] == "not_configured"
        assert statuses["load_skill"] == "not_configured"
        assert statuses["code.run_profile"] == "available"
        assert response.json()["skills"] == []
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_skills_listing_and_version_metadata_without_instructions():
    app, principal, root = stack()
    try:
        listing = asyncio.run(call(app, "GET", "/v1/skills", principal.token))
        assert listing.status_code == 200
        assert listing.json()[0]["name"] == "contract-review"
        assert listing.json()[0]["instructions"] is None

        detail = asyncio.run(
            call(app, "GET", "/v1/skills/contract-review/versions/1.0.0", principal.token)
        )
        assert detail.status_code == 200
        assert detail.json()["content_digest"]
        assert detail.json()["instructions"] is None

        explicit = asyncio.run(
            call(
                app,
                "GET",
                "/v1/skills/contract-review/versions/1.0.0?include_instructions=true",
                principal.token,
            )
        )
        assert "Review every clause" in explicit.json()["instructions"]

        missing = asyncio.run(
            call(app, "GET", "/v1/skills/contract-review/versions/9.9.9", principal.token)
        )
        assert missing.status_code == 404
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_agent_run_with_tool_and_skill_authorization_snapshot():
    app, principal, root = stack()
    try:
        created = asyncio.run(
            call(
                app,
                "POST",
                "/v1/agent-runs",
                principal.token,
                json={
                    "run_id": "run-cap-1",
                    "correlation_id": "corr-1",
                    "checkpoint": checkpoint_body(),
                    "max_failures": 3,
                    "tool_ids": ["code.run_profile", "load_skill", "list_skills"],
                    "skill_refs": ["contract-review@1.0.0"],
                },
                headers={"Idempotency-Key": "cap-1"},
            )
        )
        assert created.status_code == 201, created.text
        authorizations = asyncio.run(
            call(app, "GET", "/v1/agent-runs/run-cap-1/authorizations", principal.token)
        )
        assert authorizations.status_code == 200, authorizations.text
        snapshot = authorizations.json()
        assert {item["tool_id"] for item in snapshot["tools"]} == {
            "code.run_profile", "load_skill", "list_skills",
        }
        assert snapshot["tools"][0]["schema_digest"]
        assert snapshot["catalog_digest"]
        assert snapshot["skills"][0]["name"] == "contract-review"
        assert snapshot["skills"][0]["content_digest"]
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_run_creation_rejects_unconfigured_and_duplicate_tools():
    app, principal, root = stack(office=False)
    try:
        unconfigured = asyncio.run(
            call(
                app, "POST", "/v1/agent-runs", principal.token,
                json={
                    "run_id": "run-cap-2",
                    "correlation_id": "corr-2",
                    "checkpoint": checkpoint_body(),
                    "tool_ids": ["office.send_message"],
                },
                headers={"Idempotency-Key": "cap-2"},
            )
        )
        assert unconfigured.status_code == 422
        assert "未配置" in unconfigured.json()["detail"]

        duplicated = asyncio.run(
            call(
                app, "POST", "/v1/agent-runs", principal.token,
                json={
                    "run_id": "run-cap-3",
                    "correlation_id": "corr-3",
                    "checkpoint": checkpoint_body(),
                    "tool_ids": ["code.run_profile", "code.run_profile"],
                },
                headers={"Idempotency-Key": "cap-3"},
            )
        )
        assert duplicated.status_code == 422
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_run_creation_rejects_unknown_and_malformed_skill_refs():
    app, principal, root = stack()
    try:
        unknown = asyncio.run(
            call(
                app, "POST", "/v1/agent-runs", principal.token,
                json={
                    "run_id": "run-cap-4",
                    "correlation_id": "corr-4",
                    "checkpoint": checkpoint_body(),
                    "tool_ids": ["load_skill"],
                    "skill_refs": ["missing-skill@1.0.0"],
                },
                headers={"Idempotency-Key": "cap-4"},
            )
        )
        assert unknown.status_code == 422

        malformed = asyncio.run(
            call(
                app, "POST", "/v1/agent-runs", principal.token,
                json={
                    "run_id": "run-cap-5",
                    "correlation_id": "corr-5",
                    "checkpoint": checkpoint_body(),
                    "skill_refs": ["contract-review"],
                },
                headers={"Idempotency-Key": "cap-5"},
            )
        )
        assert malformed.status_code == 422
        assert "name@version" in malformed.json()["detail"]
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_skill_cannot_expand_run_tool_authorization():
    app, principal, root = stack(skill_required_tools=["office.send_message"])
    try:
        response = asyncio.run(
            call(
                app, "POST", "/v1/agent-runs", principal.token,
                json={
                    "run_id": "run-expand-1",
                    "correlation_id": "corr-expand",
                    "checkpoint": checkpoint_body(),
                    "tool_ids": ["load_skill"],
                    "skill_refs": ["contract-review@1.0.0"],
                },
                headers={"Idempotency-Key": "expand-1"},
            )
        )
        assert response.status_code == 422, response.text
        detail = response.json()["detail"]
        assert "未授权的工具" in detail
        assert "office.send_message" in detail
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_authorization_endpoint_requires_run_owner():
    app, principal, root = stack()
    try:
        created = asyncio.run(
            call(
                app, "POST", "/v1/agent-runs", principal.token,
                json={
                    "run_id": "run-cap-6",
                    "correlation_id": "corr-6",
                    "checkpoint": checkpoint_body(),
                    "tool_ids": ["load_skill"],
                },
                headers={"Idempotency-Key": "cap-6"},
            )
        )
        assert created.status_code == 201, created.text
        _, other_token = register_second_team(app)
        foreign = asyncio.run(
            call(app, "GET", "/v1/agent-runs/run-cap-6/authorizations", other_token)
        )
        assert foreign.status_code == 404
        owner = asyncio.run(
            call(app, "GET", "/v1/agent-runs/run-cap-6/authorizations", principal.token)
        )
        assert owner.status_code == 200
    finally:
        shutil.rmtree(root, ignore_errors=True)
