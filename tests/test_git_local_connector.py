import subprocess
from pathlib import Path

import pytest

from coifesp_harness.connectors import (
    LocalGitArtifactConnector,
    LocalGitRepository,
    load_local_git_connector,
)
from coifesp_harness.security import Classification, Principal


def git(path: Path, *args):
    return subprocess.run(["git", "-C", str(path), *args], check=True,
        capture_output=True, text=True).stdout.strip()


def test_local_git_connector_reads_full_commit_and_builds_content_bound_manifest(tmp_path):
    repository = tmp_path / "repo"; repository.mkdir()
    git(repository, "init"); git(repository, "config", "user.email", "test@example.test")
    git(repository, "config", "user.name", "Test")
    (repository / "README.md").write_text("safe content", encoding="utf-8")
    git(repository, "add", "README.md"); git(repository, "commit", "-m", "initial")
    commit = git(repository, "rev-parse", "HEAD")
    connector = LocalGitArtifactConnector(allowed_root=tmp_path,
        repositories=(LocalGitRepository("repo-1", "team-a", repository),))
    principal = Principal("publisher", "team-a", roles=frozenset({"artifact_publisher"}),
        clearance=Classification.CONFIDENTIAL, compartments=frozenset({"project-x"}))
    manifest = connector.commit_manifest(principal=principal, repository_id="repo-1",
        commit=commit, classification=Classification.CONFIDENTIAL,
        compartments=frozenset({"project-x"}), visible_to_tenants=frozenset({"team-a"}))
    assert manifest.content_uri == f"git://team-a/repo-1/{commit}"
    assert manifest.sha256 and manifest.size_bytes > 0
    with pytest.raises(ValueError, match="full object ID"):
        connector.commit_manifest(principal=principal, repository_id="repo-1",
            commit="HEAD", classification=Classification.INTERNAL, compartments=frozenset(),
            visible_to_tenants=frozenset({"team-a"}))


def test_local_git_connector_rejects_repository_outside_allowed_root(tmp_path):
    allowed = tmp_path / "allowed"; allowed.mkdir()
    outside = tmp_path / "outside"; outside.mkdir(); git(outside, "init")
    with pytest.raises(ValueError, match="outside"):
        LocalGitArtifactConnector(allowed_root=allowed,
            repositories=(LocalGitRepository("repo", "team-a", outside),))


def test_load_local_git_connector_from_bounded_deployment_registry(tmp_path):
    repository = tmp_path / "team-a" / "repo"
    repository.mkdir(parents=True)
    git(repository, "init")
    connector = load_local_git_connector(
        allowed_root=tmp_path,
        repositories_json=(
            '[{"repository_id":"repo-1","tenant_id":"team-a",'
            '"path":"team-a/repo"}]'
        ),
    )
    assert connector.repositories[("team-a", "repo-1")].root == repository.resolve()


@pytest.mark.parametrize(
    "registry",
    [
        "[]",
        '[{"repository_id":"repo-1","tenant_id":"team-a","path":"../repo"}]',
        '[{"repository_id":"repo-1","tenant_id":"team-a","path":"/tmp/repo"}]',
        '[{"repository_id":"repo-1","tenant_id":"team-a","path":"repo","extra":1}]',
    ],
)
def test_load_local_git_connector_rejects_unbounded_registry(tmp_path, registry):
    with pytest.raises(ValueError):
        load_local_git_connector(allowed_root=tmp_path, repositories_json=registry)
