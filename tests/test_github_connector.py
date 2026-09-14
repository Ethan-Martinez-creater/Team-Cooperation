from __future__ import annotations

import asyncio

import pytest

from coifesp_harness.connectors import (
    GITHUB_ADAPTER_PATHS,
    ConnectorResponse,
    GitHubTools,
    configured_connector_path_sets,
    configured_connector_paths,
)
from coifesp_harness.security import Classification, RiskLevel
from coifesp_harness.tool_catalog import (
    github_connector_manifests,
    validate_registry_manifests,
)
from coifesp_harness.tool_jobs.worker import (
    _CONTEXT,
    PermanentToolError,
    ToolExecutionContext,
)
from coifesp_harness.tools import ToolRegistry


class RecordingClient:
    def __init__(self) -> None:
        self.requests = []

    async def execute(self, request):
        self.requests.append(request)
        body = {"repository": request.body["repository"]}
        if request.path.endswith("/issues"):
            body["issue_number"] = 12
        elif request.path.endswith("/workflow-dispatches"):
            body.update(accepted=True, dispatch_id="dispatch-1",
                        workflow=request.body["workflow"], ref=request.body["ref"])
        else:
            body.update(commit_sha=request.body["commit_sha"], complete=True, checks=[])
        return ConnectorResponse(200, body, "github-request-1")


def _configuration(paths):
    import json

    return json.dumps(
        [
            {
                "connector_id": "github-main",
                "tenant_id": "team-a",
                "base_url": "https://adapter.example.test",
                "token_endpoint": "https://identity.example.test/token",
                "client_id": "github-adapter",
                "client_secret_env": "COIFESP_CONNECTOR_GITHUB_CLIENT_SECRET",
                "scopes": ["github.project"],
                "allowed_paths": list(paths),
                "max_classification": "internal",
                "timeout_seconds": 15,
                "max_response_bytes": 1048576,
                "max_attempts": 3,
                "circuit_failure_threshold": 5,
                "circuit_cooldown_seconds": 30,
            }
        ]
    )


def test_github_worker_definitions_match_shared_catalog():
    tools = GitHubTools(
        client=RecordingClient(),
        tenant_id="team-a",
        classification=Classification.INTERNAL,
    ).definitions()
    registry = ToolRegistry()
    for tool in tools:
        registry.register(tool)

    validate_registry_manifests(github_connector_manifests(), registry, executor="tool_worker")
    by_name = {tool.name: tool for tool in tools}
    assert by_name["github.create_issue"].risk is RiskLevel.HIGH
    assert by_name["github.create_issue"].approval_review is not None
    assert by_name["github.dispatch_workflow"].approval_review is not None
    assert by_name["github.get_commit_checks"].risk is RiskLevel.LOW
    assert by_name["github.get_commit_checks"].approval_review is None


def test_github_tools_use_fixed_adapter_paths_and_durable_idempotency():
    client = RecordingClient()
    tools = GitHubTools(
        client=client,
        tenant_id="team-a",
        classification=Classification.INTERNAL,
    )
    by_name = {tool.name: tool for tool in tools.definitions()}
    token = _CONTEXT.set(
        ToolExecutionContext("team-a", "job-1", "run-1", "call-1", "provider-idem-1")
    )
    try:
        asyncio.run(
            by_name["github.create_issue"].handler(
                {
                    "connector_id": "github-main",
                    "repository": "owner/repo",
                    "title": "Delivery is blocked",
                    "body": "Reproduction and requested action.",
                    "labels": ["agent-created"],
                }
            )
        )
        asyncio.run(
            by_name["github.dispatch_workflow"].handler(
                {
                    "connector_id": "github-main",
                    "repository": "owner/repo",
                    "workflow": "verify.yml",
                    "ref": "refs/heads/main",
                    "inputs": {"task": "task-1"},
                }
            )
        )
        asyncio.run(
            by_name["github.get_commit_checks"].handler(
                {
                    "connector_id": "github-main",
                    "repository": "owner/repo",
                    "commit_sha": "a" * 40,
                }
            )
        )
    finally:
        _CONTEXT.reset(token)

    assert [request.path for request in client.requests] == [
        "/v1/github/issues",
        "/v1/github/workflow-dispatches",
        "/v1/github/commit-checks",
    ]
    assert {request.idempotency_key for request in client.requests} == {"provider-idem-1"}
    assert all(request.tenant_id == "team-a" for request in client.requests)
    assert client.requests[2].body == {
        "repository": "owner/repo",
        "commit_sha": "a" * 40,
    }


def test_shared_github_tool_rejects_tenant_without_connector_scope():
    client = RecordingClient()
    tool = GitHubTools(
        client=client,
        allowed_tenant_ids=frozenset({"team-engineering"}),
        classification=Classification.INTERNAL,
    ).definitions()[2]
    token = _CONTEXT.set(
        ToolExecutionContext("team-product", "job-x", "run-x", "call-x", "idem-x")
    )
    try:
        with pytest.raises(PermanentToolError, match="github_tenant_mismatch"):
            asyncio.run(tool.handler({
                "connector_id": "github-main", "repository": "owner/repo",
                "commit_sha": "a" * 40,
            }))
    finally:
        _CONTEXT.reset(token)
    assert client.requests == []


def test_github_catalog_is_enabled_only_for_complete_adapter_contract():
    assert configured_connector_paths(_configuration(GITHUB_ADAPTER_PATHS)) == (
        GITHUB_ADAPTER_PATHS
    )
    incomplete = GITHUB_ADAPTER_PATHS - {"/v1/github/commit-checks"}
    assert not GITHUB_ADAPTER_PATHS.issubset(
        configured_connector_paths(_configuration(incomplete))
    )
    fragmented = _configuration({"/v1/github/issues"})
    assert not any(
        GITHUB_ADAPTER_PATHS.issubset(paths)
        for paths in configured_connector_path_sets(fragmented)
    )
