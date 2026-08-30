"""Execution boundary for persisted, submission-bound sandbox checks."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import secrets
import stat
import tempfile
from dataclasses import asdict
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from ..errors import IntegrityError, PolicyDenied, ResourceNotFound
from ..product.repository import PROJECT_AGENT_RUNS
from ..sandbox.models import SandboxErrorCode, SandboxRequest, WorkspaceAccess
from ..sandbox.oci import OCISandbox
from ..sandbox.tools import CodeProfile
from ..sandbox.workspace import filesystem_path
from ..security import RiskLevel
from ..tool_jobs import (
    PermanentToolError,
    RetryableToolError,
    current_tool_execution_context,
)
from ..tools import ToolDefinition
from .checks import _validate_artifacts
from .repository import TASK_VERIFICATIONS

_RESULT_SCHEMA = "coifesp.verification-tool-result.v1"
_TOOL_NAME = "verification.run_profile"
_REQUEST_FIELDS = (
    "verification_id",
    "subject_digest",
    "criterion_id",
    "profile_id",
    "profile_digest",
)
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_PROFILE_ID = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")
_MAX_ARTIFACTS = 128
_MAX_TOTAL_BYTES = 64 * 1024 * 1024
_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


def profile_digest(profile: CodeProfile) -> str:
    """Return a canonical SHA-256 digest for an administrator profile."""

    try:
        canonical = json.dumps(
            asdict(profile),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("sandbox profile cannot be canonically serialized") from exc
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class SandboxedVerificationTool:
    """Run one persisted verification criterion in a network-disabled OCI sandbox."""

    def __init__(
        self,
        *,
        engine,
        sandbox: OCISandbox,
        profiles: tuple[CodeProfile, ...],
        workspace_root: Path,
        artifact_content,
    ) -> None:
        values = tuple(profiles)
        if not values or len({item.profile_id for item in values}) != len(values):
            raise ValueError("verification sandbox profiles are absent or duplicated")
        if not isinstance(workspace_root, Path) or not workspace_root.is_absolute():
            raise ValueError("verification sandbox workspace root must be absolute")
        self.engine = engine
        self.sandbox = sandbox
        self.profiles = {item.profile_id: item for item in values}
        self.workspace_root = filesystem_path(workspace_root.resolve(strict=False))
        self.artifact_content = artifact_content

    def definition(self) -> ToolDefinition:
        digest_schema = {"type": "string", "pattern": _DIGEST.pattern}
        return ToolDefinition(
            name=_TOOL_NAME,
            description=(
                "Run one persisted verification profile over its pinned artifact "
                "snapshot in an isolated, network-disabled workspace."
            ),
            handler=self.run_profile,
            parameters_schema={
                "type": "object",
                "properties": {
                    "verification_id": {"type": "string", "minLength": 1, "maxLength": 128},
                    "subject_digest": digest_schema,
                    # Criterion identifiers are opaque contract strings.  The
                    # persisted task-contract validator only requires a
                    # non-empty string, so this tool must not impose a
                    # narrower character or length policy here.
                    "criterion_id": {"type": "string", "minLength": 1},
                    "profile_id": {"type": "string", "pattern": _PROFILE_ID.pattern},
                    "profile_digest": digest_schema,
                },
                "required": list(_REQUEST_FIELDS),
                "additionalProperties": False,
            },
            required_roles=frozenset({"tool_worker"}),
            risk=RiskLevel.LOW,
            # The worker timeout must leave room for the OCI profile itself to
            # reach its configured deadline and for cleanup/reconciliation.
            timeout_seconds=max(item.limits.timeout_seconds for item in self.profiles.values())
            + 60,
            max_output_chars=1_000_000,
            executor="tool_worker",
        )

    async def run_profile(self, arguments: dict[str, Any]) -> dict[str, Any]:
        context = current_tool_execution_context()
        request = self._request(arguments)
        profile = self.profiles.get(request["profile_id"])
        if (
            profile is None
            or profile_digest(profile) != request["profile_digest"]
            or profile.workspace_access is not WorkspaceAccess.READ_ONLY
        ):
            raise PermanentToolError("verification_profile_denied")
        try:
            Draft202012Validator(
                profile.arguments_schema or {"type": "array", "maxItems": 0}
            ).validate([])
        except (TypeError, ValueError, ValidationError) as exc:
            raise PermanentToolError("verification_profile_denied") from exc

        # Storage reads must not stop the durable worker's lease heartbeat.
        # Cancellation stops before execution; a late staging thread can only
        # finish its own fresh directory, never launch an OCI process.
        execution_workspace = await asyncio.to_thread(
            self._prepare_execution_workspace, request=request, context=context,
        )
        try:
            sandbox_request = SandboxRequest(
                execution_id=context.job_id,
                image=profile.image,
                argv=(profile.executable, *profile.fixed_arguments),
                workspace=execution_workspace,
                workspace_access=WorkspaceAccess.READ_ONLY,
                limits=profile.limits,
            )
            result = await self.sandbox.execute(sandbox_request)
        except ValueError as exc:
            raise PermanentToolError("verification_sandbox_invalid_request") from exc
        if getattr(result, "execution_id", None) != context.job_id:
            raise PermanentToolError("verification_sandbox_result_invalid")
        return self._sandbox_result(result, request)

    def _prepare_execution_workspace(self, *, request, context):
        row = self._load_authorized_verification(
            verification_id=request["verification_id"],
            subject_digest=request["subject_digest"],
            criterion_id=request["criterion_id"],
            profile_id=request["profile_id"],
            profile_digest_value=request["profile_digest"],
            run_id=context.run_id,
            job_id=context.job_id,
            tenant_id=context.tenant_id,
        )
        artifacts = self._authorized_snapshot(row, tenant_id=context.tenant_id)
        if artifacts and not callable(
            getattr(self.artifact_content, "open_policy_authorized", None)
        ):
            raise PermanentToolError("verification_content_unavailable")

        job_workspace = self._prepared_job_workspace(
            tenant_id=context.tenant_id, job_id=context.job_id
        )
        # Always mount a fresh, readable input directory.  The prepared job
        # directory may contain old attempts or worker markers and is never
        # exposed to the OCI process.
        for _ in range(3):
            execution_workspace = self._new_attempt_workspace(job_workspace)
            try:
                self._materialize_inputs_into(execution_workspace, artifacts, request)
                break
            except _StageConflict:
                continue
        else:
            raise PermanentToolError("verification_workspace_denied")
        return execution_workspace

    @staticmethod
    def _request(arguments: object) -> dict[str, str]:
        if type(arguments) is not dict or set(arguments) != set(_REQUEST_FIELDS):
            raise PermanentToolError("verification_request_denied")
        values: dict[str, str] = {}
        for key in _REQUEST_FIELDS:
            value = arguments[key]
            if type(value) is not str or not value or "\x00" in value:
                raise PermanentToolError("verification_request_denied")
            values[key] = value
        if (
            _IDENTIFIER.fullmatch(values["verification_id"]) is None
            or _PROFILE_ID.fullmatch(values["profile_id"]) is None
            or _DIGEST.fullmatch(values["subject_digest"]) is None
            or _DIGEST.fullmatch(values["profile_digest"]) is None
        ):
            raise PermanentToolError("verification_request_denied")
        return values

    def _load_authorized_verification(
        self,
        *,
        verification_id: str,
        subject_digest: str,
        criterion_id: str,
        profile_id: str,
        profile_digest_value: str,
        run_id: str,
        job_id: str,
        tenant_id: str,
    ) -> dict[str, Any]:
        expected_tool = f"sandbox.profile:{profile_id}"
        try:
            with self.engine.connect() as connection:
                row = (
                    connection.execute(
                        select(TASK_VERIFICATIONS)
                        .join(
                            PROJECT_AGENT_RUNS,
                            PROJECT_AGENT_RUNS.c.run_id == TASK_VERIFICATIONS.c.source_run_id,
                        )
                        .where(
                            TASK_VERIFICATIONS.c.verification_id == verification_id,
                            TASK_VERIFICATIONS.c.status == "PENDING",
                            TASK_VERIFICATIONS.c.source_run_id == run_id,
                            TASK_VERIFICATIONS.c.subject_digest == subject_digest,
                            PROJECT_AGENT_RUNS.c.team_id == tenant_id,
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
        except SQLAlchemyError as exc:
            raise RetryableToolError("verification_state_unavailable") from exc
        if row is None:
            raise PermanentToolError("verification_request_denied")

        checks = row.get("checks_json")
        if type(checks) is not list:
            raise PermanentToolError("verification_request_denied")
        bound = [
            check
            for check in checks
            if type(check) is dict
            and check.get("criterion_id") == criterion_id
            and check.get("tool_job_id") == job_id
            and check.get("tool") == expected_tool
            and check.get("profile_digest") == profile_digest_value
        ]
        if len(bound) != 1:
            raise PermanentToolError("verification_request_denied")

        policy = row.get("policy_json")
        if type(policy) is not dict or type(policy.get("criteria")) is not list:
            raise PermanentToolError("verification_request_denied")
        policy_matches = [
            criterion
            for criterion in policy["criteria"]
            if type(criterion) is dict
            and criterion.get("criterion_id") == criterion_id
            and criterion.get("type") == "tool_check"
            and criterion.get("tool") == expected_tool
        ]
        if len(policy_matches) != 1:
            raise PermanentToolError("verification_request_denied")
        return dict(row)

    @staticmethod
    def _authorized_snapshot(row: dict[str, Any], *, tenant_id: str) -> list[dict[str, Any]]:
        try:
            artifacts = _validate_artifacts(row.get("artifacts_json"))
        except (TypeError, ValueError) as exc:
            raise PermanentToolError("verification_snapshot_denied") from exc
        if len(artifacts) > _MAX_ARTIFACTS:
            raise PermanentToolError("verification_snapshot_denied")
        total = 0
        for artifact in artifacts:
            if artifact["owner_team_id"] != tenant_id:
                raise PermanentToolError("verification_snapshot_denied")
            total += artifact["size_bytes"]
            if total > _MAX_TOTAL_BYTES:
                raise PermanentToolError("verification_snapshot_denied")
        return artifacts

    def _prepared_job_workspace(self, *, tenant_id: str, job_id: str) -> Path:
        if _IDENTIFIER.fullmatch(tenant_id) is None or _IDENTIFIER.fullmatch(job_id) is None:
            raise PermanentToolError("verification_workspace_denied")
        root = self.workspace_root
        try:
            self._require_directory(root)
            tenant = root / tenant_id
            job = tenant / job_id
            self._require_directory(tenant)
            self._require_directory(job)
            resolved = job.resolve(strict=True)
            if resolved != job or not resolved.is_relative_to(root):
                raise _UnsafeWorkspace
            self._check_direct_children(job)
            marker = job / ".coifesp-workspace.json"
            if marker.exists():
                self._require_regular(marker)
                expected = {
                    "schema": "coifesp.sandbox-workspace.v1",
                    "tenant_id": tenant_id,
                    "job_id": job_id,
                }
                try:
                    valid = json.loads(marker.read_text(encoding="utf-8")) == expected
                except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
                    valid = False
                if not valid:
                    raise _UnsafeWorkspace
            return job
        except (_UnsafeWorkspace, OSError, RuntimeError, ValueError) as exc:
            raise PermanentToolError("verification_workspace_denied") from exc

    def _materialize_inputs_into(
        self,
        workspace: Path,
        artifacts: list[dict[str, Any]],
        request: dict[str, str],
    ) -> None:
        try:
            self._require_directory(workspace)
            if workspace.stat().st_mode & 0o777 != 0o755:
                os.chmod(workspace, 0o755)
            self._check_direct_children(workspace)
            artifacts_dir = workspace / "artifacts"
            if artifacts_dir.exists():
                self._require_directory(artifacts_dir)
            else:
                artifacts_dir.mkdir(mode=0o755)
                self._require_directory(artifacts_dir)
            os.chmod(artifacts_dir, 0o755)
            for artifact in artifacts:
                target = artifacts_dir / artifact["sha256"]
                if target.exists():
                    self._require_regular(target)
                    if not self._file_matches(target, artifact):
                        raise _StageConflict
                    continue
                self._write_artifact(target, artifact)
            manifest = workspace / "verification-inputs.json"
            expected = self._manifest_bytes(artifacts, request)
            if manifest.exists():
                self._require_regular(manifest)
                if manifest.read_bytes() != expected and not self._manifest_matches(
                    manifest, artifacts, request
                ):
                    raise _StageConflict
            elif not self._create_exclusive_file(manifest, expected):
                self._require_regular(manifest)
                if not self._manifest_matches(manifest, artifacts, request):
                    raise _StageConflict
        except _StageConflict:
            raise
        except PermanentToolError:
            raise
        except (_UnsafeWorkspace, OSError, RuntimeError, ValueError, TypeError) as exc:
            raise PermanentToolError("verification_artifact_corrupt") from exc

    def _write_artifact(self, target: Path, artifact: dict[str, Any]) -> None:
        reader = self.artifact_content.open_policy_authorized
        temporary: Path | None = None
        try:
            descriptor, name = tempfile.mkstemp(
                prefix=".verification-artifact-", dir=str(target.parent)
            )
            temporary = Path(name)
            digest = hashlib.sha256()
            size = 0
            with os.fdopen(descriptor, "wb", closefd=True) as stream:
                try:
                    chunks = reader(
                        owner_tenant_id=artifact["owner_team_id"],
                        sha256=artifact["sha256"],
                        expected_size=artifact["size_bytes"],
                    )
                    for chunk in chunks:
                        if type(chunk) is not bytes or not chunk:
                            raise _ArtifactCorrupt
                        size += len(chunk)
                        if size > artifact["size_bytes"] or size > _MAX_TOTAL_BYTES:
                            raise _ArtifactCorrupt
                        digest.update(chunk)
                        stream.write(chunk)
                except (IntegrityError, PolicyDenied, ResourceNotFound, ValueError, TypeError):
                    raise _ArtifactCorrupt
                except (OSError, TimeoutError, ConnectionError, RuntimeError) as exc:
                    raise _ContentUnavailable from exc
                stream.flush()
                os.fsync(stream.fileno())
            if size != artifact["size_bytes"] or digest.hexdigest() != artifact["sha256"]:
                raise _ArtifactCorrupt
            try:
                os.link(temporary, target)
            except FileExistsError:
                self._require_regular(target)
                if not self._file_matches(target, artifact):
                    raise _StageConflict
            # Windows read-only attributes apply to all hard links. Remove
            # our temporary name before making the final input read-only.
            temporary.unlink()
            temporary = None
            os.chmod(target, 0o444)
        except _ArtifactCorrupt as exc:
            raise PermanentToolError("verification_artifact_corrupt") from exc
        except _ContentUnavailable as exc:
            raise RetryableToolError("verification_content_unavailable") from exc
        finally:
            if temporary is not None:
                try:
                    temporary.unlink()
                except FileNotFoundError:
                    pass
                except OSError:
                    pass

    def _new_attempt_workspace(self, job_workspace: Path) -> Path:
        for _ in range(16):
            candidate = job_workspace / f".verification-attempt-{secrets.token_hex(12)}"
            try:
                candidate.mkdir(mode=0o755)
            except FileExistsError:
                continue
            try:
                self._require_directory(candidate)
                return candidate
            except (_UnsafeWorkspace, OSError, RuntimeError, ValueError):
                raise PermanentToolError("verification_workspace_denied")
        raise PermanentToolError("verification_workspace_denied")

    @staticmethod
    def _manifest_payload(
        artifacts: list[dict[str, Any]], request: dict[str, str]
    ) -> dict[str, Any]:
        return {
            "schema": "coifesp.verification-inputs.v1",
            "verification_id": request["verification_id"],
            "subject_digest": request["subject_digest"],
            "criterion_id": request["criterion_id"],
            "profile_id": request["profile_id"],
            "profile_digest": request["profile_digest"],
            "artifacts": [
                {**artifact, "relative_path": f"artifacts/{artifact['sha256']}"}
                for artifact in artifacts
            ],
        }

    @classmethod
    def _manifest_bytes(cls, artifacts: list[dict[str, Any]], request: dict[str, str]) -> bytes:
        return json.dumps(
            cls._manifest_payload(artifacts, request),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")

    @classmethod
    def _manifest_matches(
        cls,
        path: Path,
        artifacts: list[dict[str, Any]],
        request: dict[str, str],
    ) -> bool:
        try:
            return json.loads(path.read_text(encoding="utf-8")) == cls._manifest_payload(
                artifacts, request
            )
        except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
            return False

    @staticmethod
    def _create_exclusive_file(path: Path, content: bytes) -> bool:
        descriptor = None
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "wb", closefd=True) as stream:
                descriptor = None
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(path, 0o444)
            return True
        except FileExistsError:
            return False
        finally:
            if descriptor is not None:
                os.close(descriptor)

    @staticmethod
    def _file_matches(path: Path, artifact: dict[str, Any]) -> bool:
        try:
            before = path.stat()
            if before.st_size != artifact["size_bytes"]:
                return False
            digest = hashlib.sha256()
            size = 0
            with path.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1_048_576), b""):
                    size += len(chunk)
                    if size > artifact["size_bytes"]:
                        return False
                    digest.update(chunk)
            after = path.stat()
            return (
                before.st_size == after.st_size
                and size == artifact["size_bytes"]
                and digest.hexdigest() == artifact["sha256"]
            )
        except (OSError, ValueError):
            return False

    @classmethod
    def _sandbox_result(cls, result: object, request: dict[str, str]) -> dict[str, Any]:
        try:
            error_code = result.error_code
            exit_code = result.exit_code
            timed_out = result.timed_out
            output_truncated = result.output_truncated
        except AttributeError as exc:
            raise PermanentToolError("verification_sandbox_result_invalid") from exc
        if type(timed_out) is not bool or type(output_truncated) is not bool:
            raise PermanentToolError("verification_sandbox_result_invalid")
        if error_code is SandboxErrorCode.RUNTIME_UNAVAILABLE:
            raise RetryableToolError("verification_runtime_unavailable")
        structured_errors = {
            None,
            SandboxErrorCode.EXECUTION_FAILED,
            SandboxErrorCode.TIMED_OUT,
            SandboxErrorCode.OUTPUT_LIMIT,
        }
        if error_code not in structured_errors or type(exit_code) is not int:
            raise PermanentToolError("verification_sandbox_failed")
        if error_code is SandboxErrorCode.EXECUTION_FAILED and exit_code == 0:
            raise PermanentToolError("verification_sandbox_result_invalid")
        if error_code is SandboxErrorCode.TIMED_OUT:
            timed_out = True
        if error_code is SandboxErrorCode.OUTPUT_LIMIT:
            output_truncated = True
        return {
            "schema": _RESULT_SCHEMA,
            "verification_id": request["verification_id"],
            "subject_digest": request["subject_digest"],
            "criterion_id": request["criterion_id"],
            "profile_digest": request["profile_digest"],
            "exit_code": exit_code,
            "timed_out": timed_out,
            "output_truncated": output_truncated,
        }

    @staticmethod
    def _check_direct_children(path: Path) -> None:
        try:
            for child in path.iterdir():
                if SandboxedVerificationTool._is_link_or_reparse(child):
                    raise _UnsafeWorkspace
        except OSError as exc:
            raise _UnsafeWorkspace from exc

    @staticmethod
    def _is_link_or_reparse(path: Path) -> bool:
        try:
            if path.is_symlink():
                return True
            attributes = getattr(path.stat(follow_symlinks=False), "st_file_attributes", 0)
            return bool(attributes & _REPARSE_POINT)
        except OSError as exc:
            raise _UnsafeWorkspace from exc

    @staticmethod
    def _require_directory(path: Path) -> None:
        if SandboxedVerificationTool._is_link_or_reparse(path) or not path.is_dir():
            raise _UnsafeWorkspace

    @staticmethod
    def _require_regular(path: Path) -> None:
        if SandboxedVerificationTool._is_link_or_reparse(path) or not path.is_file():
            raise _UnsafeWorkspace


class _UnsafeWorkspace(Exception):
    pass


class _StageConflict(Exception):
    pass


class _ArtifactCorrupt(Exception):
    pass


class _ContentUnavailable(Exception):
    pass


__all__ = ["SandboxedVerificationTool", "profile_digest"]
