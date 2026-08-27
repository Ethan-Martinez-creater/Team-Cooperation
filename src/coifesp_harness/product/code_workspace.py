from __future__ import annotations

import difflib
import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable

from sqlalchemy import and_, insert, select, update
from sqlalchemy.engine import Engine

from ..audit import AuditEvent
from ..errors import GovernanceConflictError, PolicyDenied, ResourceNotFound
from ..postgres_audit import SQLAlchemyAuditLog
from ..security import Classification, Principal
from .models import (
    CodeChangeDraft,
    CodeDraftStatus,
    ProjectRepository,
    RepositoryBlob,
    RepositoryEntry,
)
from .repository import (
    CODE_CHANGE_DRAFTS,
    PROJECT_REPOSITORIES,
    PROJECTS,
    PROJECT_TEAMS,
)
from .service import ProductAccountService

_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_COMMIT = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
_SENSITIVE_NAME = re.compile(
    r"(^|[/\\])(\.env|.*\.pem$|.*\.key$|.*id_rsa.*|.*id_ed25519.*|"
    r".*secret.*|.*token.*|.*password.*|.*credential.*|.*\.git/config$|.*\.gitmodules$)",
    re.IGNORECASE,
)
_MAX_TEXT_BYTES = 4 * 1024 * 1024
_MAX_FILES_PER_DRAFT = 64


@dataclass(frozen=True, slots=True)
class GitContextStatus:
    """What Git/Issue/PR context is genuinely available to a run."""

    repository_bound: bool
    repository_id: str | None
    default_branch: str | None
    reason: str


class CodeWorkspaceService:
    """Project-bound code workspace: read-only Git context and change drafts.

    All writes flow through an explicit draft that a human approves; the
    approved patch is applied only inside an isolated workspace directory and
    shipped as an immutable patch artifact. The user's repository is never
    written to by this service.
    """

    def __init__(
        self,
        *,
        engine: Engine,
        git_connector=None,
        audit_log: SQLAlchemyAuditLog | None = None,
        workspace_root: Path | None = None,
    ) -> None:
        self.engine = engine
        self.git_connector = git_connector
        self.audit_log = audit_log
        self.workspace_root = workspace_root

    def create_schema(self) -> None:
        PROJECT_REPOSITORIES.create(self.engine, checkfirst=True)
        CODE_CHANGE_DRAFTS.create(self.engine, checkfirst=True)

    # ------------------------------------------------------------------ repo

    def bind_repository(
        self,
        *,
        actor_id: str,
        project_id: str,
        repository_id: str,
        connector_id: str,
        connector_version: int,
        remote_repository_id: str,
        default_branch: str,
        available_operations: Iterable[str] = ("read_tree", "read_blob", "search_code"),
    ) -> ProjectRepository:
        for value in (repository_id, connector_id, remote_repository_id, default_branch):
            if not isinstance(value, str) or not value.strip() or len(value.encode("utf-8")) > 2048:
                raise ValueError("repository binding metadata is invalid")
        ProductAccountService._identifier(project_id)
        ProductAccountService._identifier(repository_id)
        if not isinstance(connector_version, int) or connector_version < 1:
            raise ValueError("repository connector version is invalid")
        operations = tuple(dict.fromkeys(available_operations))
        allowed = {"read_tree", "read_blob", "search_code", "read_issue", "read_pull_request"}
        if not operations or any(item not in allowed for item in operations):
            raise ValueError("repository operations are invalid")
        now = datetime.now(UTC)
        with self.engine.begin() as connection:
            actor = ProductAccountService._account_row(connection, actor_id)
            owner = connection.execute(
                select(PROJECTS.c.owner_team_id).where(PROJECTS.c.project_id == project_id)
            ).scalar_one_or_none()
            if owner is None:
                raise ResourceNotFound("project is unavailable")
            if actor["team_id"] != owner:
                raise PolicyDenied("only the project lead team can bind repositories")
            exists = (
                connection.execute(
                    select(PROJECT_REPOSITORIES).where(
                        and_(
                            PROJECT_REPOSITORIES.c.project_id == project_id,
                            PROJECT_REPOSITORIES.c.repository_id == repository_id,
                        )
                    )
                )
                .mappings()
                .one_or_none()
            )
            if exists is not None:
                raise GovernanceConflictError("project repository binding already exists")
            connection.execute(
                insert(PROJECT_REPOSITORIES).values(
                    project_id=project_id,
                    repository_id=repository_id,
                    connector_id=connector_id,
                    connector_version=connector_version,
                    remote_repository_id=remote_repository_id,
                    default_branch=default_branch,
                    available_operations=json.dumps(list(operations), ensure_ascii=False),
                    created_by=actor_id,
                    created_at=now,
                )
            )
            self._audit(
                connection,
                tenant_id=actor["team_id"],
                actor_id=actor_id,
                event="code.repository_bound",
                outcome="bound",
                details={
                    "project_id": project_id,
                    "repository_id": repository_id,
                    "connector_id": connector_id,
                },
            )
        return ProjectRepository(
            project_id,
            repository_id,
            connector_id,
            connector_version,
            remote_repository_id,
            default_branch,
            operations,
            actor_id,
            now,
        )

    def list_repositories(self, *, actor_id: str, project_id: str) -> tuple[ProjectRepository, ...]:
        ProductAccountService._identifier(project_id)
        with self.engine.connect() as connection:
            self._assert_participant(connection, project_id, actor_id)
            rows = (
                connection.execute(
                    select(PROJECT_REPOSITORIES)
                    .where(PROJECT_REPOSITORIES.c.project_id == project_id)
                    .order_by(PROJECT_REPOSITORIES.c.created_at)
                )
                .mappings()
                .all()
            )
        return tuple(self._repository(row) for row in rows)

    def git_context_status(
        self, *, actor_id: str, project_id: str, repository_id: str
    ) -> GitContextStatus:
        """Report what Git context is genuinely available, never faking it."""
        repository = None
        for candidate in self.list_repositories(actor_id=actor_id, project_id=project_id):
            if candidate.repository_id == repository_id:
                repository = candidate
        if repository is None:
            return GitContextStatus(False, None, None, "仓库未绑定到本项目")
        if self.git_connector is None:
            return GitContextStatus(
                True,
                repository.repository_id,
                repository.default_branch,
                "Git 连接器未配置，无法读取仓库内容",
            )
        return GitContextStatus(
            True,
            repository.repository_id,
            repository.default_branch,
            "",
        )

    # ------------------------------------------------------------- read-only

    def list_tree(
        self, *, actor_id: str, project_id: str, repository_id: str, commit: str, path: str = ""
    ) -> tuple[RepositoryEntry, ...]:
        self._assert_commit(commit)
        with self.engine.connect() as connection:
            self._assert_participant(connection, project_id, actor_id)
            self._assert_bound(connection, project_id, repository_id)
        if self.git_connector is None:
            raise PolicyDenied("Git 连接器未配置，无法读取仓库内容")
        raw = self.git_connector.list_tree(
            principal=self._principal(actor_id),
            repository_id=repository_id,
            commit=commit,
            path=path,
        )
        return tuple(
            RepositoryEntry(
                path=item["path"],
                kind=item["kind"],
                mode=item["mode"],
                size_bytes=item["size_bytes"],
            )
            for item in raw
        )

    def read_blob(
        self, *, actor_id: str, project_id: str, repository_id: str, commit: str, path: str
    ) -> RepositoryBlob:
        self._assert_commit(commit)
        self._assert_path(path)
        with self.engine.connect() as connection:
            self._assert_participant(connection, project_id, actor_id)
            self._assert_bound(connection, project_id, repository_id)
        if self.git_connector is None:
            raise PolicyDenied("Git 连接器未配置，无法读取仓库内容")
        raw = self.git_connector.read_blob(
            principal=self._principal(actor_id),
            repository_id=repository_id,
            commit=commit,
            path=path,
            max_bytes=_MAX_TEXT_BYTES,
        )
        text = None
        if self._looks_binary(raw):
            text = None
        else:
            text = raw.decode("utf-8", errors="replace")
        return RepositoryBlob(
            path=path,
            commit=commit,
            size_bytes=len(raw),
            sha256=hashlib.sha256(raw).hexdigest(),
            text=text,
        )

    def search_code(
        self, *, actor_id: str, project_id: str, repository_id: str, commit: str, query: str
    ) -> tuple[dict, ...]:
        self._assert_commit(commit)
        if not isinstance(query, str) or not query.strip() or len(query) > 256:
            raise ValueError("code search query is invalid")
        with self.engine.connect() as connection:
            self._assert_participant(connection, project_id, actor_id)
            self._assert_bound(connection, project_id, repository_id)
        if self.git_connector is None:
            raise PolicyDenied("Git 连接器未配置，无法读取仓库内容")
        return self.git_connector.search_code(
            principal=self._principal(actor_id),
            repository_id=repository_id,
            commit=commit,
            query=query,
        )

    # ------------------------------------------------------------- drafts

    def create_change_draft(
        self,
        *,
        actor_id: str,
        project_id: str,
        repository_id: str,
        base_commit: str,
        files: Iterable[dict[str, str]],
        reason: str,
    ) -> CodeChangeDraft:
        """Create a reviewable code change draft; nothing is written anywhere."""
        self._assert_commit(base_commit)
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 2000:
            raise ValueError("draft reason is invalid")
        draft_id = None
        ProductAccountService._identifier(project_id)
        with self.engine.connect() as connection:
            self._assert_participant(connection, project_id, actor_id)
            self._assert_bound(connection, project_id, repository_id)
        file_list = list(files)
        if not file_list or len(file_list) > _MAX_FILES_PER_DRAFT:
            raise ValueError("draft file list is empty or too large")
        if self.git_connector is None:
            raise PolicyDenied("Git 连接器未配置，无法生成修改草案")
        normalized = []
        for item in file_list:
            if not isinstance(item, dict) or set(item) != {"path", "new_content"}:
                raise ValueError("draft file entry is invalid")
            path = item["path"]
            self._assert_path(path)
            new_content = item["new_content"]
            if not isinstance(new_content, str) or len(new_content.encode("utf-8")) > _MAX_TEXT_BYTES:
                raise ValueError("draft file content is invalid or too large")
            normalized.append((path, new_content))
        principal = self._principal(actor_id)
        old_by_path: dict[str, bytes] = {}
        for path, _ in normalized:
            raw = self.git_connector.read_blob(
                principal=principal,
                repository_id=repository_id,
                commit=base_commit,
                path=path,
                max_bytes=_MAX_TEXT_BYTES,
            )
            old_by_path[path] = raw
        patch_parts = []
        for path, new_content in normalized:
            old = old_by_path[path].decode("utf-8", errors="replace").splitlines(keepends=True)
            new = new_content.splitlines(keepends=True)
            diff = difflib.unified_diff(
                old,
                new,
                fromfile=f"a/{path}",
                tofile=f"b/{path}",
                n=3,
            )
            patch_parts.append("".join(diff))
        patch_text = "".join(patch_parts)
        draft_seed = f"{project_id}\0{reason}\0{patch_text}".encode()
        draft_id = f"code-draft-{hashlib.sha256(draft_seed).hexdigest()[:24]}"
        now = datetime.now(UTC)
        with self.engine.begin() as connection:
            actor = ProductAccountService._account_row(connection, actor_id)
            connection.execute(
                insert(CODE_CHANGE_DRAFTS).values(
                    draft_id=draft_id,
                    project_id=project_id,
                    repository_id=repository_id,
                    base_commit=base_commit,
                    files_json=json.dumps(
                        [
                            {"path": path, "new_content": content}
                            for path, content in normalized
                        ],
                        ensure_ascii=False,
                    ),
                    patch_text=patch_text,
                    status=CodeDraftStatus.PENDING.value,
                    version=1,
                    created_by=actor_id,
                    created_at=now,
                    decided_by=None,
                    decided_at=None,
                    patch_artifact_id=None,
                    patch_artifact_sha256=None,
                )
            )
            self._audit(
                connection,
                tenant_id=actor["team_id"],
                actor_id=actor_id,
                event="code.draft_created",
                outcome="pending",
                details={"draft_id": draft_id, "project_id": project_id},
            )
        return CodeChangeDraft(
            draft_id,
            project_id,
            repository_id,
            base_commit,
            tuple(path for path, _ in normalized),
            patch_text,
            CodeDraftStatus.PENDING,
            1,
            actor_id,
            now,
            None,
            None,
            None,
            None,
        )

    def list_change_drafts(self, *, actor_id: str, project_id: str) -> tuple[CodeChangeDraft, ...]:
        ProductAccountService._identifier(project_id)
        with self.engine.connect() as connection:
            self._assert_participant(connection, project_id, actor_id)
            rows = (
                connection.execute(
                    select(CODE_CHANGE_DRAFTS)
                    .where(CODE_CHANGE_DRAFTS.c.project_id == project_id)
                    .order_by(CODE_CHANGE_DRAFTS.c.created_at.desc())
                )
                .mappings()
                .all()
            )
        return tuple(self._draft(row) for row in rows)

    def get_change_draft(
        self, *, actor_id: str, project_id: str, draft_id: str
    ) -> CodeChangeDraft:
        ProductAccountService._identifier(project_id)
        ProductAccountService._identifier(draft_id)
        with self.engine.connect() as connection:
            self._assert_participant(connection, project_id, actor_id)
            row = (
                connection.execute(
                    select(CODE_CHANGE_DRAFTS).where(
                        and_(
                            CODE_CHANGE_DRAFTS.c.project_id == project_id,
                            CODE_CHANGE_DRAFTS.c.draft_id == draft_id,
                        )
                    )
                )
                .mappings()
                .one_or_none()
            )
        if row is None:
            raise ResourceNotFound("code change draft is unavailable")
        return self._draft(row)

    def decide_change_draft(
        self,
        *,
        actor_id: str,
        project_id: str,
        draft_id: str,
        approve: bool,
        expected_version: int,
        patch_content_service=None,
    ) -> CodeChangeDraft:
        """Approve (apply in an isolated workspace + emit patch artifact) or reject."""
        ProductAccountService._identifier(project_id)
        ProductAccountService._identifier(draft_id)
        if not isinstance(expected_version, int) or expected_version < 1:
            raise ValueError("draft version is invalid")
        now = datetime.now(UTC)
        with self.engine.begin() as connection:
            actor = ProductAccountService._account_row(connection, actor_id)
            self._assert_participant(connection, project_id, actor_id)
            row = (
                connection.execute(
                    select(CODE_CHANGE_DRAFTS)
                    .where(
                        and_(
                            CODE_CHANGE_DRAFTS.c.project_id == project_id,
                            CODE_CHANGE_DRAFTS.c.draft_id == draft_id,
                        )
                    )
                    .with_for_update()
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                raise ResourceNotFound("code change draft is unavailable")
            if row["status"] != CodeDraftStatus.PENDING.value:
                raise GovernanceConflictError("code change draft is already decided")
            if int(row["version"]) != expected_version:
                raise GovernanceConflictError("code change draft version is stale")
            if not approve:
                connection.execute(
                    update(CODE_CHANGE_DRAFTS)
                    .where(
                        and_(
                            CODE_CHANGE_DRAFTS.c.project_id == project_id,
                            CODE_CHANGE_DRAFTS.c.draft_id == draft_id,
                        )
                    )
                    .values(
                        status=CodeDraftStatus.REJECTED.value,
                        decided_by=actor_id,
                        decided_at=now,
                    )
                )
                self._audit(
                    connection,
                    tenant_id=actor["team_id"],
                    actor_id=actor_id,
                    event="code.draft_rejected",
                    outcome="rejected",
                    details={"draft_id": draft_id, "project_id": project_id},
                )
                decided = CodeChangeDraft(
                    draft_id,
                    project_id,
                    row["repository_id"],
                    row["base_commit"],
                    tuple(item["path"] for item in json.loads(row["files_json"])),
                    row["patch_text"],
                    CodeDraftStatus.REJECTED,
                    int(row["version"]),
                    row["created_by"],
                    self._aware(row["created_at"]),
                    actor_id,
                    now,
                    None,
                    None,
                )
                return decided

            # Approved: apply inside an isolated workspace, never in the repo.
            if self.workspace_root is None:
                raise ValueError("code workspace root is not configured")
            patch_bytes = row["patch_text"].encode("utf-8")
            artifact_id = f"code-patch-{hashlib.sha256(patch_bytes).hexdigest()[:32]}"
            artifact_sha256 = hashlib.sha256(patch_bytes).hexdigest()
            if patch_content_service is not None:
                from ..artifacts import ArtifactKind, ArtifactManifest, ArtifactProvenance
                from ..security import ResourceLabel

                principal = self._principal(actor_id)
                manifest = ArtifactManifest(
                    artifact_id=artifact_id,
                    kind=ArtifactKind.SOURCE_CODE,
                    media_type="text/x-patch",
                    content_uri="artifact-store://pending",
                    sha256=artifact_sha256,
                    size_bytes=len(patch_bytes),
                    label=ResourceLabel(
                        actor["team_id"],
                        Classification.INTERNAL,
                        frozenset(),
                        f"artifact:{artifact_id}",
                    ),
                    provenance=ArtifactProvenance(
                        actor_id,
                        actor["team_id"],
                        "code-workspace",
                        "1",
                        now,
                    ),
                    visible_to_tenants=frozenset({actor["team_id"]}),
                )
                patch_content_service.publish(
                    principal=principal,
                    idempotency_key=f"code-patch-{draft_id}"[:128],
                    manifest=manifest,
                    chunks=(
                        patch_bytes[index : index + 1_048_576]
                        for index in range(0, len(patch_bytes), 1_048_576)
                    ),
                )
            workspace = self._prepare_workspace(actor_id, draft_id)
            file_entries = json.loads(row["files_json"])
            for entry in file_entries:
                self._write_workspace_file(
                    workspace,
                    entry["path"],
                    entry["new_content"],
                    base_commit=row["base_commit"],
                )
            connection.execute(
                update(CODE_CHANGE_DRAFTS)
                .where(
                    and_(
                        CODE_CHANGE_DRAFTS.c.project_id == project_id,
                        CODE_CHANGE_DRAFTS.c.draft_id == draft_id,
                    )
                )
                .values(
                    status=CodeDraftStatus.APPLIED.value,
                    decided_by=actor_id,
                    decided_at=now,
                    patch_artifact_id=artifact_id,
                    patch_artifact_sha256=artifact_sha256,
                )
            )
            self._audit(
                connection,
                tenant_id=actor["team_id"],
                actor_id=actor_id,
                event="code.draft_applied",
                outcome="applied",
                details={
                    "draft_id": draft_id,
                    "project_id": project_id,
                    "patch_artifact_id": artifact_id,
                },
            )
            return CodeChangeDraft(
                draft_id,
                project_id,
                row["repository_id"],
                row["base_commit"],
                tuple(item["path"] for item in file_entries),
                row["patch_text"],
                CodeDraftStatus.APPLIED,
                int(row["version"]),
                row["created_by"],
                self._aware(row["created_at"]),
                actor_id,
                now,
                artifact_id,
                artifact_sha256,
            )

    # ------------------------------------------------------------- helpers

    def _prepare_workspace(self, actor_id: str, draft_id: str) -> Path:
        root = self.workspace_root.resolve(strict=False)
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        tenant = root / actor_id
        tenant.mkdir(mode=0o700, exist_ok=True)
        workspace = tenant / draft_id
        if workspace.exists():
            import shutil

            shutil.rmtree(workspace)
        workspace.mkdir(mode=0o700)
        for candidate in (root, tenant, workspace):
            if candidate.is_symlink() or not candidate.resolve(strict=True).is_relative_to(root):
                raise ValueError("code workspace contains an unsafe link")
        return workspace

    def _write_workspace_file(
        self, workspace: Path, path: str, new_content: str, *, base_commit: str
    ) -> None:
        """Write the approved content inside the isolated workspace only.

        The user's repository is never touched; this scratch copy exists so the
        applied patch can be reviewed and archived as an immutable artifact.
        """
        self._assert_path(path)
        target = workspace / path
        target.parent.mkdir(parents=True, exist_ok=True)
        resolved = target.resolve(strict=False)
        if not resolved.is_relative_to(workspace.resolve(strict=True)):
            raise ValueError("code workspace path escapes its root")
        if target.is_symlink() or any(
            component.is_symlink() for component in resolved.parents if workspace in component.parents
        ):
            raise ValueError("code workspace contains an unsafe link")
        target.write_text(new_content, encoding="utf-8", newline="\n")
        marker = target.parent / f".coifesp-draft-{base_commit[:12]}.json"
        marker.write_text(
            json.dumps(
                {"schema": "coifesp.code-workspace.v1", "path": path, "base_commit": base_commit},
                sort_keys=True,
            ),
            encoding="utf-8",
        )

    def _assert_participant(self, connection, project_id: str, actor_id: str) -> None:
        actor = ProductAccountService._account_row(connection, actor_id)
        exists = (
            connection.execute(
                select(PROJECT_TEAMS.c.team_id).where(
                    and_(
                        PROJECT_TEAMS.c.project_id == project_id,
                        PROJECT_TEAMS.c.team_id == actor["team_id"],
                    )
                )
            )
            .scalar_one_or_none()
        )
        if exists is None:
            raise ResourceNotFound("project is unavailable")

    @staticmethod
    def _assert_bound(connection, project_id: str, repository_id: str) -> None:
        exists = (
            connection.execute(
                select(PROJECT_REPOSITORIES.c.repository_id).where(
                    and_(
                        PROJECT_REPOSITORIES.c.project_id == project_id,
                        PROJECT_REPOSITORIES.c.repository_id == repository_id,
                    )
                )
            )
            .scalar_one_or_none()
        )
        if exists is None:
            raise ResourceNotFound("project repository is unavailable")

    @staticmethod
    def _assert_commit(commit: str) -> None:
        if not isinstance(commit, str) or _COMMIT.fullmatch(commit) is None:
            raise ValueError("Git commit must be a full object ID")

    @staticmethod
    def _assert_path(path: str) -> None:
        if (
            not isinstance(path, str)
            or not path
            or "\x00" in path
            or path.startswith("/")
            or "\\" in path
            or path.startswith("..")
            or "/../" in path
            or path.endswith("/..")
        ):
            raise ValueError("code path is invalid")
        if _SENSITIVE_NAME.search(path):
            raise PolicyDenied("code path refers to a sensitive file")

    @staticmethod
    def _looks_binary(data: bytes) -> bool:
        if b"\x00" in data[:8192]:
            return True
        try:
            data.decode("utf-8")
            return False
        except UnicodeDecodeError:
            return True

    def _principal(self, actor_id: str) -> Principal:
        with self.engine.connect() as connection:
            actor = ProductAccountService._account_row(connection, actor_id)
        return Principal(
            actor_id,
            actor["team_id"],
            roles=frozenset({"artifact_publisher", "contributor"}),
            clearance=Classification.INTERNAL,
            compartments=frozenset(),
        )

    @staticmethod
    def _repository(row) -> ProjectRepository:
        return ProjectRepository(
            row["project_id"],
            row["repository_id"],
            row["connector_id"],
            int(row["connector_version"]),
            row["remote_repository_id"],
            row["default_branch"],
            tuple(json.loads(row["available_operations"])),
            row["created_by"],
            CodeWorkspaceService._aware(row["created_at"]),
        )

    @staticmethod
    def _draft(row) -> CodeChangeDraft:
        entries = json.loads(row["files_json"])
        return CodeChangeDraft(
            row["draft_id"],
            row["project_id"],
            row["repository_id"],
            row["base_commit"],
            tuple(item["path"] for item in entries),
            row["patch_text"],
            CodeDraftStatus(row["status"]),
            int(row["version"]),
            row["created_by"],
            CodeWorkspaceService._aware(row["created_at"]),
            row["decided_by"],
            CodeWorkspaceService._aware(row["decided_at"]) if row["decided_at"] else None,
            row["patch_artifact_id"],
            row["patch_artifact_sha256"],
        )

    @staticmethod
    def _aware(value: datetime) -> datetime:
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)

    def _audit(
        self,
        connection,
        *,
        tenant_id: str,
        actor_id: str,
        event: str,
        outcome: str,
        details: dict[str, Any],
    ) -> None:
        if self.audit_log is None:
            return
        self.audit_log.append_in_transaction(
            connection,
            AuditEvent(
                tenant_id=tenant_id,
                event_type=event,
                actor_id=actor_id,
                outcome=outcome,
                details=details,
                correlation_id=str(details.get("draft_id") or details.get("repository_id") or ""),
            ),
        )
