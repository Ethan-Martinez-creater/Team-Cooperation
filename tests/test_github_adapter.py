import asyncio
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from fastapi.testclient import TestClient

from coifesp_harness.connectors.github_adapter import GitHubAdapterSettings, create_app


class Native:
    def __init__(self):
        self.calls = []
        self.fail = False

    async def execute(self, path, body):
        self.calls.append((path, body))
        if self.fail:
            raise RuntimeError("secret upstream response")
        return {"repository": body["repository"], "issue_number": 42}


def setup(tmp_path):
    settings = GitHubAdapterSettings("team-a", "s" * 32, "private-token",
                                     frozenset({"owner/repo"}), tmp_path / "adapter.db")
    native = Native()
    return settings, native, TestClient(create_app(settings, native_client=native))


def headers(client, key="job-1"):
    response = client.post("/oauth/token", data={"grant_type": "client_credentials",
        "client_id": "team-a", "client_secret": "s" * 32, "scope": "github.adapter"})
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    return {"Authorization": "Bearer " + response.json()["access_token"], "Idempotency-Key": key}


def test_write_receipt_survives_restart_without_resend(tmp_path):
    settings, native, client = setup(tmp_path)
    auth = headers(client)
    body = {"repository": "owner/repo", "title": "private title", "body": "private body"}
    assert client.post("/v1/github/issues", headers=auth, json=body).json()["issue_number"] == 42
    restarted = TestClient(create_app(settings, native_client=native))
    assert restarted.post("/v1/github/issues", headers=auth, json=body).status_code == 200
    assert len(native.calls) == 1
    assert b"private body" not in settings.ledger_path.read_bytes()
    assert client.post("/v1/github/issues", headers=auth, json={**body, "title": "changed"}).status_code == 409


def test_unknown_write_outcome_is_not_retried(tmp_path):
    settings, native, client = setup(tmp_path)
    native.fail = True
    auth = headers(client)
    body = {"repository": "owner/repo"}
    response = client.post("/v1/github/issues", headers=auth, json=body)
    assert response.status_code == 502
    assert "secret" not in response.text
    native.fail = False
    restarted = TestClient(create_app(settings, native_client=native))
    assert restarted.post("/v1/github/issues", headers=auth, json=body).status_code == 409
    assert len(native.calls) == 1


def test_crash_before_result_commit_keeps_unknown_intent(tmp_path, monkeypatch):
    from coifesp_harness.connectors.github_adapter import AdapterLedger
    settings, native, client = setup(tmp_path)
    original = AdapterLedger.complete

    def crash(*args):
        raise RuntimeError("crash")

    monkeypatch.setattr(AdapterLedger, "complete", crash)
    auth = headers(client)
    with pytest.raises(RuntimeError, match="crash"):
        client.post("/v1/github/issues", headers=auth, json={"repository": "owner/repo"})
    monkeypatch.setattr(AdapterLedger, "complete", original)
    restarted = TestClient(create_app(settings, native_client=native))
    assert restarted.post("/v1/github/issues", headers=auth, json={"repository": "owner/repo"}).status_code == 409
    assert len(native.calls) == 1


def test_auth_bounds_and_repository_allowlist(tmp_path):
    _, native, client = setup(tmp_path)
    assert client.post("/v1/github/issues", json={}).status_code == 401
    assert client.post("/oauth/token", data={"client_secret": "bad"}).status_code == 401
    auth = headers(client)
    assert client.post("/v1/github/issues", headers=auth, json={"repository": "other/repo"}).status_code == 400
    assert client.post("/v1/github/issues", headers=auth, content=b"x" * 1_048_577).status_code == 413
    assert client.post("/v1/github/unknown", headers=auth, json={}).status_code == 404
    assert not native.calls


def test_read_queries_can_refresh_without_write_ledger(tmp_path):
    _, native, client = setup(tmp_path)
    auth = headers(client)
    for _ in range(2):
        assert client.post("/v1/github/commit-checks", headers=auth,
                           json={"repository": "owner/repo"}).status_code == 200
    assert len(native.calls) == 2


def test_nested_json_is_rejected_before_native_call(tmp_path):
    _, native, client = setup(tmp_path)
    response = client.post("/v1/github/issues", headers=headers(client),
                           content=b"[" * 2000 + b"0" + b"]" * 2000)
    assert response.status_code == 400
    assert not native.calls


def test_empty_native_write_result_is_never_resent(tmp_path):
    settings, native, client = setup(tmp_path)

    async def empty(path, body):
        native.calls.append((path, body))

    native.execute = empty
    auth = headers(client)
    assert client.post("/v1/github/issues", headers=auth, json={"repository": "owner/repo"}).status_code == 502
    restarted = TestClient(create_app(settings, native_client=native))
    assert restarted.post("/v1/github/issues", headers=auth, json={"repository": "owner/repo"}).status_code == 409
    assert len(native.calls) == 1


def test_ledger_has_one_owner_under_concurrent_requests(tmp_path):
    from fastapi import HTTPException

    from coifesp_harness.connectors.github_adapter import AdapterLedger
    ledger = AdapterLedger(tmp_path / "race.sqlite3")

    def reserve(_):
        try:
            return ledger.begin("same-key", "same-digest")
        except HTTPException as error:
            return error.status_code

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(reserve, range(4)))
    assert results.count(None) == 1
    assert results.count(409) == 3


def test_token_invalid_scope_duplicate_field_and_expiry(tmp_path):
    import time

    import jwt
    _, native, client = setup(tmp_path)
    data = {"grant_type": "client_credentials", "client_id": "team-a",
            "client_secret": "s" * 32, "scope": "unexpected"}
    assert client.post("/oauth/token", data=data).status_code == 400
    assert client.post("/oauth/token", content="client_id=a&client_id=b").status_code == 400
    expired = jwt.encode({"sub": "team-a", "aud": "github-adapter", "scope": "github.adapter",
                          "iat": int(time.time()) - 60, "exp": int(time.time()) - 1},
                         "s" * 32, algorithm="HS256")
    response = client.post("/v1/github/issues", json={"repository": "owner/repo"},
                           headers={"Authorization": "Bearer " + expired, "Idempotency-Key": "key"})
    assert response.status_code == 401
    assert not native.calls


def test_existing_secure_client_protocol_connects(tmp_path):
    from coifesp_harness.config import SecretValue
    from coifesp_harness.connectors import (
        ConnectorCatalog,
        ConnectorEndpoint,
        ConnectorRequest,
        SecureConnectorClient,
    )
    from coifesp_harness.security import Classification
    settings, native, _ = setup(tmp_path)
    app = create_app(settings, native_client=native)
    transport = httpx.ASGITransport(app=app)
    endpoint = ConnectorEndpoint("github-main", "team-a", "https://adapter.example",
        "https://adapter.example/oauth/token", "team-a", SecretValue("s" * 32),
        ("github.adapter",), frozenset({"/v1/github/issues"}))

    def factory(**kwargs):
        return httpx.AsyncClient(transport=transport, **kwargs)

    catalog = ConnectorCatalog()
    catalog.register(endpoint)
    client = SecureConnectorClient(catalog=catalog, client_factory=factory)
    result = asyncio.run(client.execute(ConnectorRequest("github-main", "team-a", "/v1/github/issues",
        {"repository": "owner/repo", "title": "test", "body": "test"}, "job-1", Classification.INTERNAL)))
    assert result.body["issue_number"] == 42
    assert len(native.calls) == 1


def test_native_checks_flow_through_adapter_and_existing_client(tmp_path):
    from coifesp_harness.config import SecretValue
    from coifesp_harness.connectors import (
        ConnectorCatalog,
        ConnectorEndpoint,
        ConnectorRequest,
        SecureConnectorClient,
    )
    from coifesp_harness.connectors.github_native import NativeGitHubClient
    from coifesp_harness.security import Classification
    settings, _, _ = setup(tmp_path)
    requests = []
    sha = "a" * 40

    def github(request):
        requests.append(request)
        assert request.url.host == "api.github.com"
        assert request.url.path == f"/repos/owner/repo/commits/{sha}/check-runs"
        return httpx.Response(200, json={"total_count": 1, "check_runs": [
            {"id": 123, "name": "pytest", "head_sha": sha, "status": "completed", "conclusion": "success"},
        ]})

    native = NativeGitHubClient(token=settings.github_token, repositories=settings.repositories,
                                transport=httpx.MockTransport(github))
    transport = httpx.ASGITransport(app=create_app(settings, native_client=native))
    catalog = ConnectorCatalog()
    catalog.register(ConnectorEndpoint("github-main", "team-a", "https://adapter.example",
        "https://adapter.example/oauth/token", "team-a", SecretValue("s" * 32),
        ("github.adapter",), frozenset({"/v1/github/commit-checks"})))

    def factory(**kwargs):
        return httpx.AsyncClient(transport=transport, **kwargs)

    client = SecureConnectorClient(catalog=catalog, client_factory=factory)
    result = asyncio.run(client.execute(ConnectorRequest("github-main", "team-a", "/v1/github/commit-checks",
        {"repository": "owner/repo", "commit_sha": sha}, "job-check", Classification.INTERNAL)))
    assert result.body == {"repository": "owner/repo", "commit_sha": sha, "complete": True,
                          "checks": [{"id": 123, "name": "pytest", "status": "completed", "conclusion": "success"}]}
    assert len(requests) == 1
