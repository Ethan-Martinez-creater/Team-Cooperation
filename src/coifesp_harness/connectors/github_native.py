"""Bounded, allowlisted GitHub REST operations used by the harness.

This module intentionally exposes only the three operations needed by the
native GitHub connector.  It does not retry requests: issue creation and
workflow dispatch are writes, and a retry after an ambiguous network failure
could create a duplicate side effect.
"""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import quote

import httpx

_BASE_URL = "https://api.github.com"
_API_VERSION = "2026-03-10"
_MAX_RESPONSE_BYTES = 1_048_576
_MAX_REQUEST_BYTES = 1_048_576

_REPOSITORY = re.compile(
    r"^[A-Za-z0-9](?:[A-Za-z0-9._-]{0,38})/[A-Za-z0-9](?:[A-Za-z0-9._-]{0,99})$"
)
_COMMIT_SHA = re.compile(r"^[0-9a-fA-F]{40}$")
_WORKFLOW = re.compile(r"^[A-Za-z0-9_.!@/+\-]{1,256}$")
_SAFE_CODE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")

_ISSUE_FIELDS = frozenset({"repository", "title", "body", "labels"})
_WORKFLOW_FIELDS = frozenset({"repository", "workflow", "ref", "inputs"})
_CHECK_FIELDS = frozenset({"repository", "commit_sha"})


class NativeGitHubError(RuntimeError):
    """Sanitized error raised by the native GitHub adapter.

    Error text is deliberately limited to a stable local code.  Upstream
    response bodies, URLs, headers, and credentials are never copied into the
    exception message.
    """

    def __init__(self, code: str, status_code: int = 502) -> None:
        if not isinstance(code, str) or _SAFE_CODE.fullmatch(code) is None:
            code = "native_github_error"
        if not isinstance(status_code, int) or isinstance(status_code, bool):
            status_code = 502
        self.code = code
        self.status_code = status_code
        super().__init__(code)


class NativeGitHubClient:
    """Execute a small, fixed-origin set of GitHub REST operations.

    ``repositories`` is an explicit allowlist of ``owner/repository`` names.
    The caller supplies the allowlist, while this client owns URL construction
    and validates every request against it before opening a network client.
    """

    def __init__(
        self,
        token: str,
        repositories: frozenset[str],
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not isinstance(token, str) or not token or any(ord(char) < 0x20 for char in token):
            raise NativeGitHubError("invalid_token")
        try:
            configured = frozenset(repositories)
        except (TypeError, ValueError) as exc:
            raise NativeGitHubError("invalid_repositories") from exc
        if not configured or any(
            not isinstance(repository, str) or _REPOSITORY.fullmatch(repository) is None
            for repository in configured
        ):
            raise NativeGitHubError("invalid_repositories")
        if transport is not None and not isinstance(transport, httpx.AsyncBaseTransport):
            raise NativeGitHubError("invalid_transport")
        self._token = token
        self._repositories = configured
        self._transport = transport

    async def execute(self, path: str, body: dict) -> dict:
        """Execute one supported operation and return its safe projection."""

        if not isinstance(path, str):
            raise NativeGitHubError("invalid_path")
        if not isinstance(body, dict):
            raise NativeGitHubError("invalid_body")
        if path == "/v1/github/issues":
            operation = self._prepare_issue(body)
        elif path == "/v1/github/workflow-dispatches":
            operation = self._prepare_workflow_dispatch(body)
        elif path == "/v1/github/commit-checks":
            operation = self._prepare_commit_checks(body)
        else:
            raise NativeGitHubError("unsupported_path")

        method, endpoint, payload, repository, metadata = operation
        request_body = self._encode_request(payload)
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {self._token}",
            "X-GitHub-Api-Version": _API_VERSION,
            "User-Agent": "coifesp-harness-github-native/1",
        }
        if method == "POST":
            headers["Content-Type"] = "application/json"

        try:
            async with httpx.AsyncClient(
                base_url=_BASE_URL,
                follow_redirects=False,
                trust_env=False,
                timeout=httpx.Timeout(15.0),
                transport=self._transport,
            ) as client, client.stream(
                method,
                endpoint,
                content=request_body if method == "POST" else None,
                headers=headers,
            ) as response:
                self._check_content_length(response)
                if 300 <= response.status_code < 400:
                    raise NativeGitHubError("redirect_rejected")
                if response.status_code == 429:
                    raise NativeGitHubError("rate_limited")
                if response.status_code < 200 or response.status_code >= 300:
                    if response.status_code in {408, 500, 502, 503, 504}:
                        raise NativeGitHubError("provider_unavailable")
                    raise NativeGitHubError("provider_rejected")
                raw = await self._read_bounded(response)
                return self._project_response(
                    path,
                    repository=repository,
                    metadata=metadata,
                    status_code=response.status_code,
                    raw=raw,
                    link_header=response.headers.get("link"),
                )
        except NativeGitHubError:
            raise
        except (httpx.HTTPError, OSError, ValueError, RuntimeError) as exc:
            raise NativeGitHubError("network_failure") from exc

    def _prepare_issue(self, body: dict) -> tuple[str, str, dict, str, dict]:
        self._reject_unknown_fields(body, _ISSUE_FIELDS)
        repository = self._repository(body)
        title = body.get("title")
        if not isinstance(title, str) or not title.strip() or len(title) > 256:
            raise NativeGitHubError("invalid_issue")
        issue_payload: dict[str, Any] = {"title": title}
        if "body" in body:
            issue_body = body["body"]
            if not isinstance(issue_body, str) or len(issue_body) > 65_536:
                raise NativeGitHubError("invalid_issue")
            issue_payload["body"] = issue_body
        if "labels" in body:
            labels = body["labels"]
            if (
                not isinstance(labels, list)
                or len(labels) > 20
                or any(not isinstance(label, str) or not 1 <= len(label) <= 64 for label in labels)
                or len(set(labels)) != len(labels)
            ):
                raise NativeGitHubError("invalid_issue")
            issue_payload["labels"] = list(labels)
        return (
            "POST",
            self._repository_endpoint(repository) + "/issues",
            issue_payload,
            repository,
            {},
        )

    def _prepare_workflow_dispatch(self, body: dict) -> tuple[str, str, dict, str, dict]:
        self._reject_unknown_fields(body, _WORKFLOW_FIELDS)
        repository = self._repository(body)
        workflow = body.get("workflow")
        if (
            not isinstance(workflow, str)
            or _WORKFLOW.fullmatch(workflow) is None
            or workflow.startswith("/")
            or any(part in {"", ".", ".."} for part in workflow.split("/"))
        ):
            raise NativeGitHubError("invalid_workflow")
        ref = body.get("ref")
        self._validate_ref(ref)
        payload: dict[str, Any] = {"ref": ref}
        if "inputs" in body:
            payload["inputs"] = self._validate_inputs(body["inputs"])
        return (
            "POST",
            self._repository_endpoint(repository)
            + "/actions/workflows/"
            + quote(workflow, safe="")
            + "/dispatches",
            payload,
            repository,
            {"workflow": workflow, "ref": ref},
        )

    def _prepare_commit_checks(self, body: dict) -> tuple[str, str, dict, str, dict]:
        self._reject_unknown_fields(body, _CHECK_FIELDS)
        repository = self._repository(body)
        commit_sha = body.get("commit_sha")
        if not isinstance(commit_sha, str) or _COMMIT_SHA.fullmatch(commit_sha) is None:
            raise NativeGitHubError("invalid_commit_sha")
        return (
            "GET",
            self._repository_endpoint(repository)
            + "/commits/"
            + quote(commit_sha, safe="")
            + "/check-runs?per_page=100&filter=latest",
            {},
            repository,
            {"commit_sha": commit_sha},
        )

    def _project_response(
        self,
        path: str,
        *,
        repository: str,
        metadata: dict,
        status_code: int,
        raw: bytes,
        link_header: str | None,
    ) -> dict:
        if path == "/v1/github/issues":
            if status_code != 201:
                raise NativeGitHubError("invalid_provider_response")
            value = self._decode_json(raw, required=True)
            if not isinstance(value, dict):
                raise NativeGitHubError("invalid_provider_response")
            number = value.get("number")
            if isinstance(number, bool) or not isinstance(number, int) or number < 1:
                raise NativeGitHubError("invalid_provider_response")
            return {"repository": repository, "issue_number": number}

        if path == "/v1/github/workflow-dispatches":
            if status_code != 200:
                raise NativeGitHubError("invalid_provider_response")
            value = self._decode_json(raw, required=True)
            if not isinstance(value, dict):
                raise NativeGitHubError("invalid_provider_response")
            workflow_run_id = value.get("workflow_run_id")
            if (
                isinstance(workflow_run_id, bool)
                or not isinstance(workflow_run_id, int)
                or workflow_run_id < 1
            ):
                raise NativeGitHubError("invalid_provider_response")
            dispatch_id = str(workflow_run_id)
            return {
                "repository": repository,
                "workflow": metadata["workflow"],
                "ref": metadata["ref"],
                "accepted": True,
                "dispatch_id": dispatch_id,
            }

        if path == "/v1/github/commit-checks":
            if status_code != 200:
                raise NativeGitHubError("invalid_provider_response")
            value = self._decode_json(raw, required=True)
            return self._project_checks(
                value,
                repository=repository,
                commit_sha=metadata["commit_sha"],
                link_header=link_header,
            )
        raise NativeGitHubError("unsupported_path")

    def _project_checks(
        self,
        value: Any,
        *,
        repository: str,
        commit_sha: str,
        link_header: str | None,
    ) -> dict:
        if not isinstance(value, dict):
            raise NativeGitHubError("invalid_provider_response")
        total_count = value.get("total_count")
        check_runs = value.get("check_runs")
        if (
            isinstance(total_count, bool)
            or not isinstance(total_count, int)
            or total_count < 0
            or not isinstance(check_runs, list)
        ):
            raise NativeGitHubError("invalid_provider_response")

        # Never claim completeness for a response whose count exceeds the
        # bounded projection, whose page advertises another page, or whose
        # count and returned list disagree.
        complete = (
            total_count <= 100
            and len(check_runs) == total_count
            and len(check_runs) <= 100
            and not self._has_next_page(link_header)
        )
        projected: list[dict] = []
        identities: set[int] = set()
        for index, check_run in enumerate(check_runs):
            projected_check = self._project_check_run(check_run, commit_sha)
            identity = projected_check["id"]
            if identity in identities:
                raise NativeGitHubError("invalid_provider_response")
            identities.add(identity)
            if index < 100:
                projected.append(projected_check)
        return {
            "repository": repository,
            "commit_sha": commit_sha,
            "complete": complete,
            "checks": projected,
        }

    @staticmethod
    def _project_check_run(value: Any, commit_sha: str) -> dict:
        if not isinstance(value, dict):
            raise NativeGitHubError("invalid_provider_response")
        check_id = value.get("id")
        name = value.get("name")
        status = value.get("status")
        conclusion = value.get("conclusion")
        head_sha = value.get("head_sha")
        if status in {"waiting", "requested", "pending"}:
            status = "queued"
            conclusion = None
        conclusions = {
            "success",
            "failure",
            "cancelled",
            "timed_out",
            "neutral",
            "skipped",
            "action_required",
            "stale",
            "startup_failure",
        }
        if (
            isinstance(check_id, bool)
            or not isinstance(check_id, int)
            or check_id < 1
            or not isinstance(name, str)
            or not name
            or len(name) > 256
            or status not in {"queued", "in_progress", "completed"}
            or (status == "completed" and conclusion not in conclusions)
            or (status != "completed" and conclusion is not None)
            or not isinstance(head_sha, str)
            or _COMMIT_SHA.fullmatch(head_sha) is None
            or head_sha.lower() != commit_sha.lower()
        ):
            raise NativeGitHubError("invalid_provider_response")
        return {"id": check_id, "name": name, "status": status, "conclusion": conclusion}

    @staticmethod
    def _reject_unknown_fields(body: dict, allowed: frozenset[str]) -> None:
        if any(not isinstance(key, str) or key not in allowed for key in body):
            raise NativeGitHubError("invalid_body")

    def _repository(self, body: dict) -> str:
        repository = body.get("repository")
        if not isinstance(repository, str) or _REPOSITORY.fullmatch(repository) is None:
            raise NativeGitHubError("invalid_repository")
        if repository not in self._repositories:
            raise NativeGitHubError("repository_not_allowed")
        return repository

    @staticmethod
    def _repository_endpoint(repository: str) -> str:
        owner, name = repository.split("/", 1)
        return "/repos/" + quote(owner, safe="") + "/" + quote(name, safe="")

    @staticmethod
    def _validate_ref(value: Any) -> None:
        if not isinstance(value, str) or not value or len(value) > 256:
            raise NativeGitHubError("invalid_ref")
        if any(ord(char) < 0x20 or char in {"\\", "?", "#"} for char in value):
            raise NativeGitHubError("invalid_ref")

    @staticmethod
    def _validate_inputs(value: Any) -> dict[str, str]:
        if not isinstance(value, dict) or len(value) > 20:
            raise NativeGitHubError("invalid_workflow_inputs")
        result: dict[str, str] = {}
        for key, item in value.items():
            if (
                not isinstance(key, str)
                or not key
                or len(key) > 256
                or any(ord(char) < 0x20 for char in key)
                or not isinstance(item, str)
                or len(item) > 1000
            ):
                raise NativeGitHubError("invalid_workflow_inputs")
            result[key] = item
        return result

    @staticmethod
    def _encode_request(payload: dict) -> bytes:
        try:
            encoded = json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        except (TypeError, ValueError, UnicodeError, OverflowError, RecursionError) as exc:
            raise NativeGitHubError("invalid_body") from exc
        if len(encoded) > _MAX_REQUEST_BYTES:
            raise NativeGitHubError("request_too_large")
        return encoded

    @staticmethod
    def _check_content_length(response: httpx.Response) -> None:
        value = response.headers.get("content-length")
        if value is None:
            return
        try:
            length = int(value)
        except (TypeError, ValueError):
            return
        if length < 0 or length > _MAX_RESPONSE_BYTES:
            raise NativeGitHubError("response_too_large")

    @staticmethod
    async def _read_bounded(response: httpx.Response) -> bytes:
        chunks: list[bytes] = []
        size = 0
        async for chunk in response.aiter_bytes():
            size += len(chunk)
            if size > _MAX_RESPONSE_BYTES:
                raise NativeGitHubError("response_too_large")
            chunks.append(chunk)
        return b"".join(chunks)

    @staticmethod
    def _decode_json(raw: bytes, *, required: bool) -> Any:
        if not raw:
            if required:
                raise NativeGitHubError("invalid_provider_response")
            return None
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError, RecursionError):
            raise NativeGitHubError("invalid_provider_response") from None

    @staticmethod
    def _has_next_page(link_header: str | None) -> bool:
        if not link_header:
            return False
        return bool(re.search(r"rel\s*=\s*[\"']next[\"']", link_header, re.IGNORECASE))

__all__ = ["NativeGitHubClient", "NativeGitHubError"]
