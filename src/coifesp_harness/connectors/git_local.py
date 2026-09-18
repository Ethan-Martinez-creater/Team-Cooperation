from __future__ import annotations

import hashlib
import json
import re
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from ..artifacts import ArtifactKind, ArtifactManifest, ArtifactProvenance
from ..errors import PolicyDenied, ResourceNotFound
from ..security import Classification, Principal, ResourceLabel

_OBJECT = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
_REPO_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


@dataclass(frozen=True, slots=True)
class LocalGitRepository:
    repository_id: str
    tenant_id: str
    root: Path


class LocalGitArtifactConnector:
    """Read-only local Git object adapter; never invokes a shell or a remote."""
    def __init__(self, *, allowed_root: Path, repositories: tuple[LocalGitRepository, ...],
                 timeout_seconds: float = 10, max_object_bytes: int = 16 * 1024 * 1024) -> None:
        self.allowed_root = allowed_root.resolve(strict=True)
        self.timeout_seconds, self.max_object_bytes = timeout_seconds, max_object_bytes
        self.repositories = {}
        for item in repositories:
            root = item.root.resolve(strict=True)
            if not _REPO_ID.fullmatch(item.repository_id) or not root.is_relative_to(self.allowed_root):
                raise ValueError("Git repository is outside the allowed root")
            self._git(root, "rev-parse", "--git-dir")
            key = (item.tenant_id, item.repository_id)
            if key in self.repositories: raise ValueError("Git repository registration is duplicated")
            self.repositories[key] = LocalGitRepository(item.repository_id, item.tenant_id, root)

    def _repository(self, tenant_id: str, repository_id: str) -> Path:
        repository = self.repositories.get((tenant_id, repository_id))
        if repository is None:
            raise ResourceNotFound("Git repository is absent or hidden")
        return repository.root

    def _commit_object(self, root: Path, commit: str) -> None:
        if _OBJECT.fullmatch(commit) is None:
            raise ValueError("Git commit must be a full object ID")
        object_type = self._git(root, "cat-file", "-t", commit).strip()
        if object_type != b"commit":
            raise ValueError("Git object is not a commit")

    def list_tree(self, *, principal: Principal, repository_id: str, commit: str,
                  path: str = "") -> tuple[dict, ...]:
        """Read a directory listing at a pinned commit; path must stay in-repo."""
        if principal.is_service:
            raise PolicyDenied("Git read requires a human account")
        root = self._repository(principal.tenant_id, repository_id)
        self._commit_object(root, commit)
        if not isinstance(path, str) or len(path.encode("utf-8")) > 4096:
            raise ValueError("Git tree path is invalid")
        if path:
            self._assert_in_repo(root, path, allow_missing=False)
        arguments = ["ls-tree", "-r", "-t", "--long", commit]
        if path:
            arguments.extend(["--", path])
        listing = self._git(root, *arguments)
        entries = []
        for line in listing.decode("utf-8", errors="replace").splitlines():
            if not line:
                continue
            parts = line.split("\t", 1)[0].split(" ", 3)
            if len(parts) != 4:
                continue
            mode, kind, oid, size = parts
            name = line.split("\t", 1)[1]
            entries.append({
                "mode": mode,
                "kind": kind,
                "oid": oid,
                "size_bytes": int(size) if size.isdigit() else None,
                "path": name,
            })
        return tuple(entries)

    def read_blob(self, *, principal: Principal, repository_id: str, commit: str,
                  path: str, max_bytes: int = 1_048_576) -> bytes:
        """Read a file blob at a pinned commit with path and size safety."""
        if principal.is_service:
            raise PolicyDenied("Git read requires a human account")
        root = self._repository(principal.tenant_id, repository_id)
        self._commit_object(root, commit)
        resolved = self._assert_in_repo(root, path, allow_missing=True)
        if resolved.is_symlink():
            raise ValueError("Git path resolves to a symbolic link")
        size = int(self._git(root, "cat-file", "-s", f"{commit}:{path}").strip())
        if size < 0 or size > max_bytes:
            raise ValueError("Git blob exceeds the read size limit")
        raw = self._git(root, "cat-file", "blob", f"{commit}:{path}")
        if len(raw) != size:
            raise ValueError("Git blob content length is inconsistent")
        return raw

    def search_code(self, *, principal: Principal, repository_id: str, commit: str,
                    query: str, max_hits: int = 200) -> tuple[dict, ...]:
        if principal.is_service:
            raise PolicyDenied("Git read requires a human account")
        root = self._repository(principal.tenant_id, repository_id)
        self._commit_object(root, commit)
        if not isinstance(query, str) or not query.strip() or len(query) > 256:
            raise ValueError("Git search query is invalid")
        if not 1 <= max_hits <= 1000:
            raise ValueError("Git search hit limit is invalid")
        try:
            raw = self._git(root, "grep", "-n", "--no-color", "-I", query, commit)
        except ResourceNotFound:
            return ()
        hits = []
        for line in raw.decode("utf-8", errors="replace").splitlines():
            # git grep -n prints: <commit>:<path>:<line>:<content>
            parts = line.split(":", 3)
            if len(parts) != 4:
                continue
            _, name, lineno, content = parts
            hits.append({"path": name, "line": int(lineno), "content": content})
            if len(hits) >= max_hits:
                break
        return tuple(hits)

    def commit_manifest(self, *, principal: Principal, repository_id: str, commit: str,
                        classification: Classification, compartments: frozenset[str],
                        visible_to_tenants: frozenset[str]) -> ArtifactManifest:
        if principal.is_service or "artifact_publisher" not in principal.roles:
            raise PolicyDenied("Git artifact publication requires artifact_publisher")
        root = self._repository(principal.tenant_id, repository_id)
        self._commit_object(root, commit)
        raw = self._git(root, "cat-file", "commit", commit)
        if len(raw) > self.max_object_bytes: raise ValueError("Git commit object exceeds size limit")
        sha256 = hashlib.sha256(raw).hexdigest()
        return ArtifactManifest(artifact_id=f"git-{repository_id}-{commit}",
            kind=ArtifactKind.SOURCE_CODE, media_type="application/vnd.git.commit",
            content_uri=f"git://{principal.tenant_id}/{repository_id}/{commit}",
            sha256=sha256, size_bytes=len(raw),
            label=ResourceLabel(principal.tenant_id, classification, compartments,
                f"artifact:git-{repository_id}-{commit}"),
            provenance=ArtifactProvenance(principal.principal_id, principal.tenant_id,
                "git-local-readonly", "1", datetime.now(UTC)),
            visible_to_tenants=visible_to_tenants)

    def _assert_in_repo(self, root: Path, path: str, *, allow_missing: bool) -> Path:
        if "\x00" in path or path.startswith("/") or "\\" in path:
            raise ValueError("Git path is invalid")
        candidate = (root / path).resolve(strict=not allow_missing)
        if not candidate.is_relative_to(root):
            raise ValueError("Git path escapes the repository")
        return candidate

    def _git(self, root: Path, *arguments: str) -> bytes:
        try:
            result = subprocess.run(["git", "-c", "protocol.file.allow=never",
                "-c", "credential.helper=", "-C", str(root), *arguments],
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                timeout=self.timeout_seconds, check=True, shell=False)
        except (subprocess.SubprocessError, OSError) as exc:
            raise ResourceNotFound("Git object is absent or unreadable") from exc
        return result.stdout


def load_local_git_connector(
    *, allowed_root: str | Path, repositories_json: str
) -> LocalGitArtifactConnector:
    """Build the bounded read-only Git connector from deployment metadata."""
    if len(repositories_json.encode("utf-8")) > 262_144:
        raise ValueError("Git repository registry is too large")
    try:
        values = json.loads(repositories_json)
    except json.JSONDecodeError as exc:
        raise ValueError("Git repository registry is invalid JSON") from exc
    fields = {"repository_id", "tenant_id", "path"}
    if not isinstance(values, list) or not 1 <= len(values) <= 64:
        raise ValueError("Git repository registry must contain 1 to 64 repositories")
    root = Path(allowed_root)
    repositories = []
    for value in values:
        if not isinstance(value, dict) or set(value) != fields:
            raise ValueError("Git repository registry fields are invalid")
        path = value["path"]
        if (
            not isinstance(path, str)
            or not path
            or path.startswith(("/", "\\"))
            or Path(path).is_absolute()
            or ".." in Path(path).parts
        ):
            raise ValueError("Git repository path must be relative to the allowed root")
        repositories.append(
            LocalGitRepository(
                repository_id=value["repository_id"],
                tenant_id=value["tenant_id"],
                root=root / path,
            )
        )
    return LocalGitArtifactConnector(
        allowed_root=root,
        repositories=tuple(repositories),
    )
