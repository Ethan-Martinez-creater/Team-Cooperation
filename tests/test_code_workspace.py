from __future__ import annotations

import asyncio
import pathlib
import subprocess

import httpx
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from coifesp_harness.config import Settings
from coifesp_harness.connectors import LocalGitArtifactConnector, LocalGitRepository
from coifesp_harness.control_plane import create_app
from coifesp_harness.product import (
    CodeWorkspaceService,
    NotificationService,
    ProductAccountService,
    ProjectDirectoryService,
    TeamCollaborationService,
)

PASSWORD = "Admin-Correct-Horse-42!"


def _git(path: pathlib.Path, *args):
    return subprocess.run(
        ["git", "-C", str(path), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _make_repository(tmp_path: pathlib.Path, name: str = "repo-1") -> tuple[pathlib.Path, str]:
    repo = tmp_path / name
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.test")
    _git(repo, "config", "user.name", "Test")
    (repo / "README.md").write_text("hello world\n", encoding="utf-8")
    (repo / "app.py").write_text("def main():\n    return 1\n", encoding="utf-8")
    (repo / ".env").write_text("SECRET=leak\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "initial")
    commit = _git(repo, "rev-parse", "HEAD")
    return repo, commit


def stack(tmp_path: pathlib.Path):
    engine = create_engine(
        "sqlite+pysqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    accounts = ProductAccountService(engine)
    accounts.create_schema()
    notifications = NotificationService(engine)
    directory = ProjectDirectoryService(engine)
    collaboration = TeamCollaborationService(engine, notifier=notifications)
    # Register the team first so the git connector targets its real tenant ID.
    team, bootstrap = accounts.register_team(
        team_id="team-code-alpha", team_handle="alpha-team", team_name="Alpha"
    )
    accounts.change_initial_password(
        login=bootstrap.username,
        current_password=bootstrap.initial_password,
        new_password=PASSWORD,
    )
    session = accounts.login(login=bootstrap.username, password=PASSWORD)
    repo, commit = _make_repository(tmp_path)
    git_connector = LocalGitArtifactConnector(
        allowed_root=tmp_path,
        repositories=(LocalGitRepository("repo-1", team.team_id, repo),),
    )
    workspace_root = tmp_path / "workspaces"
    code_workspace = CodeWorkspaceService(
        engine=engine,
        git_connector=git_connector,
        workspace_root=workspace_root,
    )
    app = create_app(
        settings=Settings.from_environment(
            {"COIFESP_ENV": "test", "COIFESP_AUTH_MODE": "builtin"}
        ),
        product_account_service=accounts,
        project_directory_service=directory,
        project_resource_service=None,
        team_collaboration_service=collaboration,
        notification_service=notifications,
        code_workspace_service=code_workspace,
    )
    return app, repo, commit, team.team_id, session.token


async def call(app, method, path, token=None, **kwargs):
    headers = kwargs.pop("headers", {})
    if token:
        headers["Authorization"] = f"Bearer {token}"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    ) as client:
        return await client.request(method, path, headers=headers, **kwargs)


def login_team(app, handle="alpha-team", name="Alpha"):
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


def create_project(app, token, team_id, name="Code Project"):
    response = asyncio.run(
        call(
            app,
            "POST",
            "/v1/projects",
            token,
            json={
                "name": name,
                "description": "iteration 3",
                "owner_assignment_name": "产品",
                "owner_kind": "product",
            },
        )
    )
    assert response.status_code == 201, response.text
    return response.json()["project_id"]


def test_repository_binding_and_cross_team_isolation(tmp_path):
    app, _, commit, team_id, token = stack(tmp_path)
    project_id = create_project(app, token, team_id)
    bound = asyncio.run(
        call(
            app,
            "POST",
            f"/v1/projects/{project_id}/code/repositories",
            token,
            json={
                "repository_id": "repo-1",
                "connector_id": "git-local",
                "connector_version": 1,
                "remote_repository_id": "remote/repo-1",
                "default_branch": "main",
                "available_operations": ["read_tree", "read_blob", "search_code"],
            },
        )
    )
    assert bound.status_code == 201, bound.text
    assert bound.json()["repository_id"] == "repo-1"

    listing = asyncio.run(
        call(app, "GET", f"/v1/projects/{project_id}/code/repositories", token)
    )
    assert listing.status_code == 200
    assert len(listing.json()) == 1

    # A second team cannot see the project at all (no membership).
    _, other_token = login_team(app, handle="beta-team", name="Beta")
    foreign = asyncio.run(
        call(app, "GET", f"/v1/projects/{project_id}/code/repositories", other_token)
    )
    assert foreign.status_code == 404


def test_read_only_tree_blob_search_and_sensitive_paths(tmp_path):
    app, _, commit, team_id, token = stack(tmp_path)
    project_id = create_project(app, token, team_id)
    asyncio.run(
        call(
            app,
            "POST",
            f"/v1/projects/{project_id}/code/repositories",
            token,
            json={
                "repository_id": "repo-1",
                "connector_id": "git-local",
                "connector_version": 1,
                "remote_repository_id": "remote/repo-1",
                "default_branch": "main",
            },
        )
    )
    tree = asyncio.run(
        call(
            app,
            "GET",
            f"/v1/projects/{project_id}/code/repositories/repo-1/tree",
            token,
            params={"commit": commit},
        )
    )
    assert tree.status_code == 200, tree.text
    paths = {item["path"] for item in tree.json()}
    assert "README.md" in paths
    assert "app.py" in paths
    assert ".env" in paths  # tree lists it, but reading it is denied

    blob = asyncio.run(
        call(
            app,
            "GET",
            f"/v1/projects/{project_id}/code/repositories/repo-1/blob",
            token,
            params={"commit": commit, "path": "app.py"},
        )
    )
    assert blob.status_code == 200, blob.text
    assert "def main" in blob.json()["text"]

    sensitive = asyncio.run(
        call(
            app,
            "GET",
            f"/v1/projects/{project_id}/code/repositories/repo-1/blob",
            token,
            params={"commit": commit, "path": ".env"},
        )
    )
    assert sensitive.status_code == 403

    traversal = asyncio.run(
        call(
            app,
            "GET",
            f"/v1/projects/{project_id}/code/repositories/repo-1/blob",
            token,
            params={"commit": commit, "path": "../../etc/passwd"},
        )
    )
    assert traversal.status_code == 422

    search = asyncio.run(
        call(
            app,
            "GET",
            f"/v1/projects/{project_id}/code/repositories/repo-1/search",
            token,
            params={"commit": commit, "query": "def main"},
        )
    )
    assert search.status_code == 200, search.text
    assert any(hit["path"] == "app.py" for hit in search.json())

    status = asyncio.run(
        call(
            app,
            "GET",
            f"/v1/projects/{project_id}/code/repositories/repo-1/status",
            token,
        )
    )
    assert status.status_code == 200
    assert status.json()["repository_bound"] is True
    assert status.json()["reason"] == ""


def test_change_draft_diff_approve_apply_and_audit(tmp_path):
    app, _, commit, team_id, token = stack(tmp_path)
    project_id = create_project(app, token, team_id)
    asyncio.run(
        call(
            app,
            "POST",
            f"/v1/projects/{project_id}/code/repositories",
            token,
            json={
                "repository_id": "repo-1",
                "connector_id": "git-local",
                "connector_version": 1,
                "remote_repository_id": "remote/repo-1",
                "default_branch": "main",
            },
        )
    )
    created = asyncio.run(
        call(
            app,
            "POST",
            f"/v1/projects/{project_id}/code/change-drafts",
            token,
            json={
                "repository_id": "repo-1",
                "base_commit": commit,
                "files": [
                    {"path": "app.py", "new_content": "def main():\n    return 2\n"},
                ],
                "reason": "bump return value",
            },
        )
    )
    assert created.status_code == 201, created.text
    draft = created.json()
    assert draft["status"] == "pending"
    assert draft["version"] == 1
    assert "def main" in draft["patch_text"]
    assert "return 2" in draft["patch_text"]

    # Pending drafts produce no workspace files and no artifact.
    workspace_root = tmp_path / "workspaces"
    assert not (workspace_root / team_id).exists()

    decided = asyncio.run(
        call(
            app,
            "POST",
            f"/v1/projects/{project_id}/code/change-drafts/{draft['draft_id']}:decide",
            token,
            json={"approve": True, "expected_version": 1},
        )
    )
    assert decided.status_code == 200, decided.text
    applied = decided.json()
    assert applied["status"] == "applied"
    assert applied["decided_by"]
    assert applied["patch_artifact_id"]
    assert len(applied["patch_artifact_sha256"]) == 64


def test_draft_rejection_and_stale_version_conflict(tmp_path):
    app, _, commit, team_id, token = stack(tmp_path)
    project_id = create_project(app, token, team_id)
    asyncio.run(
        call(
            app,
            "POST",
            f"/v1/projects/{project_id}/code/repositories",
            token,
            json={
                "repository_id": "repo-1",
                "connector_id": "git-local",
                "connector_version": 1,
                "remote_repository_id": "remote/repo-1",
                "default_branch": "main",
            },
        )
    )
    created = asyncio.run(
        call(
            app,
            "POST",
            f"/v1/projects/{project_id}/code/change-drafts",
            token,
            json={
                "repository_id": "repo-1",
                "base_commit": commit,
                "files": [{"path": "app.py", "new_content": "def main():\n    return 3\n"}],
                "reason": "reject me",
            },
        )
    )
    draft = created.json()
    rejected = asyncio.run(
        call(
            app,
            "POST",
            f"/v1/projects/{project_id}/code/change-drafts/{draft['draft_id']}:decide",
            token,
            json={"approve": False, "expected_version": 1},
        )
    )
    assert rejected.status_code == 200
    assert rejected.json()["status"] == "rejected"

    # Deciding an already-decided draft is a conflict.
    again = asyncio.run(
        call(
            app,
            "POST",
            f"/v1/projects/{project_id}/code/change-drafts/{draft['draft_id']}:decide",
            token,
            json={"approve": True, "expected_version": 1},
        )
    )
    assert again.status_code == 409

    # Stale expected_version is rejected before any side effect.
    second = asyncio.run(
        call(
            app,
            "POST",
            f"/v1/projects/{project_id}/code/change-drafts",
            token,
            json={
                "repository_id": "repo-1",
                "base_commit": commit,
                "files": [{"path": "app.py", "new_content": "def main():\n    return 4\n"}],
                "reason": "stale check",
            },
        )
    )
    stale = asyncio.run(
        call(
            app,
            "POST",
            f"/v1/projects/{project_id}/code/change-drafts/{second.json()['draft_id']}:decide",
            token,
            json={"approve": True, "expected_version": 99},
        )
    )
    assert stale.status_code == 409


def test_unapproved_draft_cannot_write_workspace_files(tmp_path):
    app, _, commit, team_id, token = stack(tmp_path)
    project_id = create_project(app, token, team_id)
    asyncio.run(
        call(
            app,
            "POST",
            f"/v1/projects/{project_id}/code/repositories",
            token,
            json={
                "repository_id": "repo-1",
                "connector_id": "git-local",
                "connector_version": 1,
                "remote_repository_id": "remote/repo-1",
                "default_branch": "main",
            },
        )
    )
    created = asyncio.run(
        call(
            app,
            "POST",
            f"/v1/projects/{project_id}/code/change-drafts",
            token,
            json={
                "repository_id": "repo-1",
                "base_commit": commit,
                "files": [{"path": "app.py", "new_content": "def main():\n    return 5\n"}],
                "reason": "no write before approval",
            },
        )
    )
    assert created.status_code == 201
    workspace_root = tmp_path / "workspaces"
    assert not (workspace_root / team_id).exists()
    # The git repository itself is untouched.
    assert "return 1" in (tmp_path / "repo-1" / "app.py").read_text(encoding="utf-8")


def test_agent_cannot_read_unselected_repository(tmp_path):
    """An Agent run created without a bound repository gets an explicit reason."""
    app, _, commit, team_id, token = stack(tmp_path)
    project_id = create_project(app, token, team_id)
    status = asyncio.run(
        call(
            app,
            "GET",
            f"/v1/projects/{project_id}/code/repositories/repo-1/status",
            token,
        )
    )
    assert status.status_code == 200
    assert status.json()["repository_bound"] is False
    assert "未绑定" in status.json()["reason"]

    read = asyncio.run(
        call(
            app,
            "GET",
            f"/v1/projects/{project_id}/code/repositories/repo-1/blob",
            token,
            params={"commit": commit, "path": "app.py"},
        )
    )
    assert read.status_code == 404
