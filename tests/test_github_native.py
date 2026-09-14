from __future__ import annotations

import json

import httpx
import pytest

from coifesp_harness.connectors.github_native import (
    NativeGitHubClient,
    NativeGitHubError,
)

REPOSITORY = "owner/repo"
SHA = "a" * 40
TOKEN = "ghs_private_token"


def client(handler):
    return NativeGitHubClient(
        token=TOKEN,
        repositories=frozenset({REPOSITORY}),
        transport=httpx.MockTransport(handler),
    )


def check(check_id=1, *, name="pytest", status="completed", conclusion="success", head_sha=SHA):
    return {
        "id": check_id,
        "name": name,
        "status": status,
        "conclusion": conclusion,
        "head_sha": head_sha,
    }


def assert_code(error, code):
    assert isinstance(error.value, NativeGitHubError)
    assert error.value.code == code
    assert error.value.status_code == 502


@pytest.mark.asyncio
async def test_execute_projects_issue_workflow_and_checks_with_fixed_origin():
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.url.host == "api.github.com"
        assert request.url.scheme == "https"
        assert request.headers["authorization"] == f"Bearer {TOKEN}"
        assert request.headers["x-github-api-version"] == "2026-03-10"
        assert request.headers["accept"] == "application/vnd.github+json"
        if request.url.path.endswith("/issues"):
            assert request.method == "POST"
            assert json.loads(request.content) == {
                "body": "details",
                "labels": ["bug"],
                "title": "A bug",
            }
            return httpx.Response(201, json={"number": 17, "title": "A bug"})
        if request.url.path.endswith("/dispatches"):
            assert request.method == "POST"
            assert request.url.path == "/repos/owner/repo/actions/workflows/ci.yml/dispatches"
            assert json.loads(request.content) == {"inputs": {"environment": "test"}, "ref": "main"}
            return httpx.Response(200, json={"workflow_run_id": 29, "run_url": "private"})
        assert request.method == "GET"
        assert request.url.path == f"/repos/owner/repo/commits/{SHA}/check-runs"
        assert request.url.params["per_page"] == "100"
        assert request.url.params["filter"] == "latest"
        return httpx.Response(200, json={"total_count": 1, "check_runs": [check()]})

    github = client(handler)
    assert await github.execute(
        "/v1/github/issues",
        {"repository": REPOSITORY, "title": "A bug", "body": "details", "labels": ["bug"]},
    ) == {"repository": REPOSITORY, "issue_number": 17}
    assert await github.execute(
        "/v1/github/workflow-dispatches",
        {"repository": REPOSITORY, "workflow": "ci.yml", "ref": "main", "inputs": {"environment": "test"}},
    ) == {
        "repository": REPOSITORY,
        "workflow": "ci.yml",
        "ref": "main",
        "accepted": True,
        "dispatch_id": "29",
    }
    assert await github.execute(
        "/v1/github/commit-checks", {"repository": REPOSITORY, "commit_sha": SHA}
    ) == {
        "repository": REPOSITORY,
        "commit_sha": SHA,
        "complete": True,
        "checks": [{"id": 1, "name": "pytest", "status": "completed", "conclusion": "success"}],
    }
    assert len(requests) == 3


@pytest.mark.asyncio
async def test_input_injection_and_repository_denial_happen_before_network():
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise AssertionError(f"unexpected network request: {request.url!s}")

    github = client(handler)
    invalid = [
        ("/v1/github/issues?next=https://attacker.test", {"repository": REPOSITORY, "title": "x"}, "unsupported_path"),
        ("/v1/github/issues", {"repository": "other/repo", "title": "x"}, "repository_not_allowed"),
        ("/v1/github/issues", {"repository": "owner/repo?x=1", "title": "x"}, "invalid_repository"),
        ("/v1/github/issues", {"repository": REPOSITORY, "title": "x", "unknown": "value"}, "invalid_body"),
        ("/v1/github/workflow-dispatches", {"repository": REPOSITORY, "workflow": "../../secret", "ref": "main", "inputs": {}}, "invalid_workflow"),
        ("/v1/github/commit-checks", {"repository": REPOSITORY, "commit_sha": "not-a-sha"}, "invalid_commit_sha"),
    ]
    for path, body, code in invalid:
        with pytest.raises(NativeGitHubError) as error:
            await github.execute(path, body)
        assert_code(error, code)
    assert calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "expected_code"),
    [(307, "redirect_rejected"), (429, "rate_limited"), (503, "provider_unavailable")],
)
async def test_redirect_rate_limit_and_provider_errors_are_sanitized_and_not_retried(status, expected_code):
    calls = 0
    secret = "upstream-secret-response"

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        headers = {"location": "https://attacker.test/" + secret} if status == 307 else {}
        return httpx.Response(status, headers=headers, json={"message": secret})

    with pytest.raises(NativeGitHubError) as error:
        await client(handler).execute(
            "/v1/github/issues", {"repository": REPOSITORY, "title": "x"}
        )
    assert_code(error, expected_code)
    assert secret not in str(error.value)
    assert calls == 1


@pytest.mark.asyncio
async def test_response_body_is_streamed_and_capped_at_one_megabyte():
    class OversizedStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"x" * (1_048_576 + 1)

        async def aclose(self):
            return None

    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, stream=OversizedStream())

    with pytest.raises(NativeGitHubError) as error:
        await client(handler).execute(
            "/v1/github/commit-checks", {"repository": REPOSITORY, "commit_sha": SHA}
        )
    assert_code(error, "response_too_large")
    assert calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(201, json={"id": 17}),
        httpx.Response(200, json={"workflow_run_id": "29"}),
        httpx.Response(200, json={"total_count": 1, "check_runs": [{"id": 1}]}),
    ],
)
async def test_malformed_success_responses_are_rejected(response):
    def handler(request: httpx.Request) -> httpx.Response:
        return response

    if response.status_code == 201:
        path, body = "/v1/github/issues", {"repository": REPOSITORY, "title": "x"}
    elif "workflow_run_id" in response.text:
        path, body = "/v1/github/workflow-dispatches", {
            "repository": REPOSITORY, "workflow": "ci.yml", "ref": "main", "inputs": {},
        }
    else:
        path, body = "/v1/github/commit-checks", {"repository": REPOSITORY, "commit_sha": SHA}
    with pytest.raises(NativeGitHubError) as error:
        await client(handler).execute(path, body)
    assert_code(error, "invalid_provider_response")


@pytest.mark.asyncio
async def test_check_results_are_bounded_and_never_claim_incomplete_pages_complete():
    checks = [check(index, name=f"check-{index}") for index in range(1, 102)]
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            headers={"Link": '<https://api.github.com/?page=2>; rel="next"'},
            json={"total_count": 101, "check_runs": checks},
        )

    result = await client(handler).execute(
        "/v1/github/commit-checks", {"repository": REPOSITORY, "commit_sha": SHA}
    )
    assert result["complete"] is False
    assert len(result["checks"]) == 100
    assert calls == 1


@pytest.mark.asyncio
async def test_check_state_normalization_head_sha_and_duplicate_identity_validation():
    def normalized(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "total_count": 1,
            "check_runs": [check(status="waiting", conclusion="failure")],
        })

    result = await client(normalized).execute(
        "/v1/github/commit-checks", {"repository": REPOSITORY, "commit_sha": SHA}
    )
    assert result["checks"] == [{"id": 1, "name": "pytest", "status": "queued", "conclusion": None}]

    def duplicate(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"total_count": 2, "check_runs": [check(), check()]})

    with pytest.raises(NativeGitHubError) as error:
        await client(duplicate).execute(
            "/v1/github/commit-checks", {"repository": REPOSITORY, "commit_sha": SHA}
        )
    assert_code(error, "invalid_provider_response")

    def wrong_head(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"total_count": 1, "check_runs": [check(head_sha="b" * 40)]})

    with pytest.raises(NativeGitHubError) as error:
        await client(wrong_head).execute(
            "/v1/github/commit-checks", {"repository": REPOSITORY, "commit_sha": SHA}
        )
    assert_code(error, "invalid_provider_response")


def test_deep_request_json_is_sanitized():
    value = {}
    value["self"] = value
    with pytest.raises(NativeGitHubError) as error:
        NativeGitHubClient._encode_request(value)
    assert_code(error, "invalid_body")

    deep = b"[" * 2_000 + b"0" + b"]" * 2_000
    with pytest.raises(NativeGitHubError) as error:
        NativeGitHubClient._decode_json(deep, required=True)
    assert_code(error, "invalid_provider_response")
