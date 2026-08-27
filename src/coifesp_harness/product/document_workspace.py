from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any, Iterable

from sqlalchemy import and_, insert, select, update
from sqlalchemy.engine import Engine

from ..artifacts import ArtifactKind, ArtifactManifest, ArtifactProvenance
from ..audit import AuditEvent
from ..errors import GovernanceConflictError, PolicyDenied, ResourceNotFound
from ..postgres_audit import SQLAlchemyAuditLog
from ..security import Classification, Principal, ResourceLabel
from .document_editing import (
    DocumentModificationError,
    apply_modification,
    render_pdf_review,
    validate_modification,
)
from .document_parsing import StructuredDocument, parse_document_bytes
from .models import (
    DerivativeStatus,
    DerivativeType,
    DocumentChangeDraft,
    DocumentChangeDraftStatus,
    ResourceAction,
    ResourceDerivative,
    ResourceVersion,
)
from .repository import (
    DOCUMENT_CHANGE_DRAFTS,
    RESOURCE_DERIVATIVES,
    RESOURCE_VERSIONS,
)
from .service import ProductAccountService

_MAX_DERIVATIVE_BYTES = 8 * 1024 * 1024


class DocumentWorkspaceService:
    """Office document workspace: safe parsing, preview and versioned edits.

    The original artifact is immutable; every approved edit produces a new
    immutable version and a reviewable draft in between. Parse failures are
    recorded as a stable error category so the original file stays available
    without leaking internal stack traces.
    """

    def __init__(
        self,
        *,
        engine: Engine,
        resource_service=None,
        content_service=None,
        audit_log: SQLAlchemyAuditLog | None = None,
    ) -> None:
        self.engine = engine
        self.resource_service = resource_service
        self.content_service = content_service
        self.audit_log = audit_log

    def create_schema(self) -> None:
        RESOURCE_VERSIONS.create(self.engine, checkfirst=True)
        RESOURCE_DERIVATIVES.create(self.engine, checkfirst=True)
        DOCUMENT_CHANGE_DRAFTS.create(self.engine, checkfirst=True)

    # ------------------------------------------------------------- versions

    def ensure_initial_version(self, *, actor_id: str, resource_id: str) -> ResourceVersion:
        """Create version 1 pointing at the original uploaded artifact."""
        resource = self._authorized_resource(actor_id, resource_id)
        with self.engine.connect() as connection:
            existing = (
                connection.execute(
                    select(RESOURCE_VERSIONS)
                    .where(
                        and_(
                            RESOURCE_VERSIONS.c.resource_id == resource_id,
                            RESOURCE_VERSIONS.c.version_number == 1,
                        )
                    )
                )
                .mappings()
                .one_or_none()
            )
        if existing is not None:
            return self._version(existing)
        version_id = f"doc-v1-{hashlib.sha256(resource_id.encode()).hexdigest()[:24]}"
        now = datetime.now(UTC)
        with self.engine.begin() as connection:
            connection.execute(
                insert(RESOURCE_VERSIONS).values(
                    version_id=version_id,
                    resource_id=resource_id,
                    version_number=1,
                    artifact_id=resource.artifact_id,
                    artifact_sha256=resource.artifact_sha256,
                    parent_version_id=None,
                    created_by=actor_id,
                    reason="原始上传",
                    created_at=now,
                )
            )
            actor = ProductAccountService._account_row(connection, actor_id)
            self._audit(
                connection,
                tenant_id=actor["team_id"],
                actor_id=actor_id,
                event="document.version_created",
                outcome="created",
                details={"resource_id": resource_id, "version": 1},
            )
        return ResourceVersion(
            version_id,
            resource_id,
            1,
            resource.artifact_id,
            resource.artifact_sha256,
            None,
            actor_id,
            "原始上传",
            now,
        )

    def list_versions(self, *, actor_id: str, resource_id: str) -> tuple[ResourceVersion, ...]:
        self._authorized_resource(actor_id, resource_id)
        with self.engine.connect() as connection:
            rows = (
                connection.execute(
                    select(RESOURCE_VERSIONS)
                    .where(RESOURCE_VERSIONS.c.resource_id == resource_id)
                    .order_by(RESOURCE_VERSIONS.c.version_number)
                )
                .mappings()
                .all()
            )
        return tuple(self._version(row) for row in rows)

    def get_version(
        self, *, actor_id: str, resource_id: str, version_id: str
    ) -> ResourceVersion:
        self._authorized_resource(actor_id, resource_id)
        with self.engine.connect() as connection:
            row = (
                connection.execute(
                    select(RESOURCE_VERSIONS).where(
                        and_(
                            RESOURCE_VERSIONS.c.resource_id == resource_id,
                            RESOURCE_VERSIONS.c.version_id == version_id,
                        )
                    )
                )
                .mappings()
                .one_or_none()
            )
        if row is None:
            raise ResourceNotFound("document version is unavailable")
        return self._version(row)

    # ---------------------------------------------------------- derivatives

    def parse_resource(self, *, actor_id: str, resource_id: str) -> ResourceDerivative:
        """Parse the current document and persist structured content as an
        immutable artifact. A parse failure records a stable category and
        leaves the original artifact untouched and downloadable.
        """
        resource = self._authorized_resource(actor_id, resource_id)
        version = self.ensure_initial_version(actor_id=actor_id, resource_id=resource_id)
        raw = b"".join(
            self.content_service.open(
                principal=self._principal(actor_id),
                owner_tenant_id=resource.artifact_owner_team_id,
                artifact_id=resource.artifact_id,
                expected_sha256=resource.artifact_sha256,
            )
        )
        if len(raw) > _MAX_DERIVATIVE_BYTES:
            parsed = StructuredDocument(
                media_type=resource.media_type,
                title=resource.title,
                error_category="too_large",
            )
        else:
            parsed = parse_document_bytes(raw, resource.media_type, resource.title)
        derivative_id = (
            f"doc-deriv-{hashlib.sha256((resource_id + version.version_id).encode()).hexdigest()[:24]}"
        )
        status = DerivativeStatus.FAILED if parsed.error_category else DerivativeStatus.READY
        error_category = parsed.error_category
        content_artifact_id = None
        content_sha256 = None
        if status is DerivativeStatus.READY:
            body = parsed.to_json().encode("utf-8")
            content_sha256 = hashlib.sha256(body).hexdigest()
            content_artifact_id = f"doc-content-{content_sha256[:32]}"
            self._publish_derivative_artifact(
                actor_id=actor_id,
                resource=resource,
                artifact_id=content_artifact_id,
                sha256=content_sha256,
                body=body,
                idempotency_key=f"doc-deriv-{resource_id}-{content_sha256[:32]}"[:128],
            )
            summary = f"解析成功：{len(parsed.items)} 个定位片段"
        else:
            summary = f"解析失败：{error_category}"
        now = datetime.now(UTC)
        with self.engine.begin() as connection:
            actor = ProductAccountService._account_row(connection, actor_id)
            existing = (
                connection.execute(
                    select(RESOURCE_DERIVATIVES).where(
                        and_(
                            RESOURCE_DERIVATIVES.c.resource_id == resource_id,
                            RESOURCE_DERIVATIVES.c.version_id == version.version_id,
                        )
                    )
                )
                .mappings()
                .one_or_none()
            )
            if existing is not None:
                return self._derivative(existing)
            connection.execute(
                insert(RESOURCE_DERIVATIVES).values(
                    derivative_id=derivative_id,
                    resource_id=resource_id,
                    version_id=version.version_id,
                    derivative_type=DerivativeType.STRUCTURED_CONTENT.value,
                    status=status.value,
                    summary=summary[:512],
                    size_bytes=0,
                    error_category=error_category,
                    content_artifact_id=content_artifact_id,
                    content_artifact_sha256=content_sha256,
                    created_at=now,
                )
            )
            self._audit(
                connection,
                tenant_id=actor["team_id"],
                actor_id=actor_id,
                event="document.parsed",
                outcome=status.value,
                details={
                    "resource_id": resource_id,
                    "version": version.version_number,
                    "error_category": error_category,
                },
            )
        return ResourceDerivative(
            derivative_id,
            resource_id,
            version.version_id,
            DerivativeType.STRUCTURED_CONTENT,
            status,
            summary,
            0,
            error_category,
            now,
        )

    def list_derivatives(self, *, actor_id: str, resource_id: str) -> tuple[ResourceDerivative, ...]:
        self._authorized_resource(actor_id, resource_id)
        with self.engine.connect() as connection:
            rows = (
                connection.execute(
                    select(RESOURCE_DERIVATIVES)
                    .where(RESOURCE_DERIVATIVES.c.resource_id == resource_id)
                    .order_by(RESOURCE_DERIVATIVES.c.created_at.desc())
                )
                .mappings()
                .all()
            )
        return tuple(self._derivative(row) for row in rows)

    def get_derivative_content(
        self, *, actor_id: str, resource_id: str, derivative_id: str
    ) -> dict[str, Any]:
        resource = self._authorized_resource(actor_id, resource_id)
        with self.engine.connect() as connection:
            row = (
                connection.execute(
                    select(RESOURCE_DERIVATIVES).where(
                        and_(
                            RESOURCE_DERIVATIVES.c.resource_id == resource_id,
                            RESOURCE_DERIVATIVES.c.derivative_id == derivative_id,
                        )
                    )
                )
                .mappings()
                .one_or_none()
            )
        if row is None:
            raise ResourceNotFound("document derivative is unavailable")
        if row["status"] != DerivativeStatus.READY.value:
            raise ResourceNotFound("document derivative is not ready")
        if not row["content_artifact_id"] or not row["content_artifact_sha256"]:
            raise ResourceNotFound("document derivative content is unavailable")
        body = b"".join(
            self.content_service.open(
                principal=self._principal(actor_id),
                owner_tenant_id=resource.artifact_owner_team_id,
                artifact_id=row["content_artifact_id"],
                expected_sha256=row["content_artifact_sha256"],
            )
        )
        try:
            return json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise ResourceNotFound("document derivative content is malformed") from exc

    # ------------------------------------------------------------- drafts

    def create_change_draft(
        self,
        *,
        actor_id: str,
        resource_id: str,
        source_version_id: str,
        modification: dict[str, Any],
        reason: str,
    ) -> DocumentChangeDraft:
        """Create a reviewable document edit draft; nothing is written."""
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 2000:
            raise ValueError("document draft reason is invalid")
        resource = self._authorized_resource(actor_id, resource_id)
        validated = validate_modification(modification)
        with self.engine.connect() as connection:
            source = (
                connection.execute(
                    select(RESOURCE_VERSIONS).where(
                        and_(
                            RESOURCE_VERSIONS.c.resource_id == resource_id,
                            RESOURCE_VERSIONS.c.version_id == source_version_id,
                        )
                    )
                )
                .mappings()
                .one_or_none()
            )
        if source is None:
            raise ResourceNotFound("document source version is unavailable")
        body = json.dumps(validated, ensure_ascii=False, sort_keys=True)
        draft_id = (
            f"doc-draft-{hashlib.sha256((resource_id + body + reason).encode()).hexdigest()[:24]}"
        )
        now = datetime.now(UTC)
        with self.engine.begin() as connection:
            actor = ProductAccountService._account_row(connection, actor_id)
            connection.execute(
                insert(DOCUMENT_CHANGE_DRAFTS).values(
                    draft_id=draft_id,
                    resource_id=resource_id,
                    source_version_id=source_version_id,
                    modification_json=body,
                    generated_version_id=None,
                    status=DocumentChangeDraftStatus.PENDING.value,
                    version=1,
                    created_by=actor_id,
                    created_at=now,
                    decided_by=None,
                    decided_at=None,
                )
            )
            self._audit(
                connection,
                tenant_id=actor["team_id"],
                actor_id=actor_id,
                event="document.draft_created",
                outcome="pending",
                details={"draft_id": draft_id, "resource_id": resource_id},
            )
        return DocumentChangeDraft(
            draft_id,
            resource_id,
            source_version_id,
            body,
            None,
            DocumentChangeDraftStatus.PENDING,
            1,
            actor_id,
            now,
            None,
            None,
        )

    def list_change_drafts(
        self, *, actor_id: str, resource_id: str
    ) -> tuple[DocumentChangeDraft, ...]:
        self._authorized_resource(actor_id, resource_id)
        with self.engine.connect() as connection:
            rows = (
                connection.execute(
                    select(DOCUMENT_CHANGE_DRAFTS)
                    .where(DOCUMENT_CHANGE_DRAFTS.c.resource_id == resource_id)
                    .order_by(DOCUMENT_CHANGE_DRAFTS.c.created_at.desc())
                )
                .mappings()
                .all()
            )
        return tuple(self._draft(row) for row in rows)

    def get_change_draft(
        self, *, actor_id: str, resource_id: str, draft_id: str
    ) -> DocumentChangeDraft:
        self._authorized_resource(actor_id, resource_id)
        with self.engine.connect() as connection:
            row = (
                connection.execute(
                    select(DOCUMENT_CHANGE_DRAFTS).where(
                        and_(
                            DOCUMENT_CHANGE_DRAFTS.c.resource_id == resource_id,
                            DOCUMENT_CHANGE_DRAFTS.c.draft_id == draft_id,
                        )
                    )
                )
                .mappings()
                .one_or_none()
            )
        if row is None:
            raise ResourceNotFound("document change draft is unavailable")
        return self._draft(row)

    def decide_change_draft(
        self,
        *,
        actor_id: str,
        resource_id: str,
        draft_id: str,
        approve: bool,
        expected_version: int,
    ) -> DocumentChangeDraft:
        """Approve (emit a new immutable version) or reject an edit draft."""
        if not isinstance(expected_version, int) or expected_version < 1:
            raise ValueError("document draft version is invalid")
        resource = self._authorized_resource(actor_id, resource_id)
        now = datetime.now(UTC)
        with self.engine.begin() as connection:
            actor = ProductAccountService._account_row(connection, actor_id)
            row = (
                connection.execute(
                    select(DOCUMENT_CHANGE_DRAFTS)
                    .where(
                        and_(
                            DOCUMENT_CHANGE_DRAFTS.c.resource_id == resource_id,
                            DOCUMENT_CHANGE_DRAFTS.c.draft_id == draft_id,
                        )
                    )
                    .with_for_update()
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                raise ResourceNotFound("document change draft is unavailable")
            if row["status"] != DocumentChangeDraftStatus.PENDING.value:
                raise GovernanceConflictError("document change draft is already decided")
            if int(row["version"]) != expected_version:
                raise GovernanceConflictError("document change draft version is stale")
            modification = json.loads(row["modification_json"])
            if not approve:
                connection.execute(
                    update(DOCUMENT_CHANGE_DRAFTS)
                    .where(
                        and_(
                            DOCUMENT_CHANGE_DRAFTS.c.resource_id == resource_id,
                            DOCUMENT_CHANGE_DRAFTS.c.draft_id == draft_id,
                        )
                    )
                    .values(
                        status=DocumentChangeDraftStatus.REJECTED.value,
                        decided_by=actor_id,
                        decided_at=now,
                    )
                )
                self._audit(
                    connection,
                    tenant_id=actor["team_id"],
                    actor_id=actor_id,
                    event="document.draft_rejected",
                    outcome="rejected",
                    details={"draft_id": draft_id, "resource_id": resource_id},
                )
                return DocumentChangeDraft(
                    draft_id,
                    resource_id,
                    row["source_version_id"],
                    row["modification_json"],
                    None,
                    DocumentChangeDraftStatus.REJECTED,
                    int(row["version"]),
                    row["created_by"],
                    self._aware(row["created_at"]),
                    actor_id,
                    now,
                )

            source = (
                connection.execute(
                    select(RESOURCE_VERSIONS)
                    .where(
                        and_(
                            RESOURCE_VERSIONS.c.resource_id == resource_id,
                            RESOURCE_VERSIONS.c.version_id == row["source_version_id"],
                        )
                    )
                    .with_for_update()
                )
                .mappings()
                .one_or_none()
            )
            if source is None:
                raise ResourceNotFound("document source version is unavailable")
            new_version = self._apply_and_emit_version(
                connection=connection,
                actor=actor,
                resource=resource,
                source=source,
                modification=modification,
                now=now,
            )
            connection.execute(
                update(DOCUMENT_CHANGE_DRAFTS)
                .where(
                    and_(
                        DOCUMENT_CHANGE_DRAFTS.c.resource_id == resource_id,
                        DOCUMENT_CHANGE_DRAFTS.c.draft_id == draft_id,
                    )
                )
                .values(
                    status=DocumentChangeDraftStatus.APPROVED.value,
                    generated_version_id=new_version.version_id,
                    decided_by=actor_id,
                    decided_at=now,
                )
            )
            self._audit(
                connection,
                tenant_id=actor["team_id"],
                actor_id=actor_id,
                event="document.draft_approved",
                outcome="approved",
                details={
                    "draft_id": draft_id,
                    "resource_id": resource_id,
                    "version": new_version.version_number,
                },
            )
        return DocumentChangeDraft(
            draft_id,
            resource_id,
            row["source_version_id"],
            row["modification_json"],
            new_version.version_id,
            DocumentChangeDraftStatus.APPROVED,
            int(row["version"]),
            row["created_by"],
            self._aware(row["created_at"]),
            actor_id,
            now,
        )

    def _apply_and_emit_version(
        self,
        *,
        connection,
        actor,
        resource,
        source,
        modification: dict[str, Any],
        now: datetime,
    ) -> ResourceVersion:
        if self.content_service is None:
            raise ValueError("document content service is not configured")
        raw = b"".join(
            self.content_service.open(
                principal=self._principal(actor["account_id"]),
                owner_tenant_id=resource.artifact_owner_team_id,
                artifact_id=source["artifact_id"],
                expected_sha256=source["artifact_sha256"],
            )
        )
        document_format = modification["format"]
        if document_format == "pdf":
            new_bytes, new_media_type = render_pdf_review(modification)
            kind = ArtifactKind.DOCUMENT
        else:
            new_bytes, new_media_type = apply_modification(raw, resource.media_type, modification)
            kind = _artifact_kind(new_media_type)
        sha256 = hashlib.sha256(new_bytes).hexdigest()
        next_number = (
            connection.execute(
                select(RESOURCE_VERSIONS.c.version_number)
                .where(RESOURCE_VERSIONS.c.resource_id == resource.resource_id)
                .order_by(RESOURCE_VERSIONS.c.version_number.desc())
                .limit(1)
            )
            .scalar_one_or_none()
        )
        version_number = int(next_number or 0) + 1
        version_id = (
            f"doc-v{version_number}-{hashlib.sha256(resource.resource_id.encode()).hexdigest()[:20]}"
        )
        artifact_id = f"doc-content-{sha256[:32]}"
        principal = self._principal(actor["account_id"])
        visible = frozenset({resource.artifact_owner_team_id})
        manifest = ArtifactManifest(
            artifact_id=artifact_id,
            kind=kind,
            media_type=new_media_type,
            content_uri="artifact-store://pending",
            sha256=sha256,
            size_bytes=len(new_bytes),
            label=ResourceLabel(
                resource.artifact_owner_team_id,
                Classification.INTERNAL,
                frozenset(),
                f"artifact:{artifact_id}",
            ),
            provenance=ArtifactProvenance(
                actor["account_id"],
                resource.artifact_owner_team_id,
                "document-workspace",
                "1",
                now,
            ),
            visible_to_tenants=visible,
        )
        chunks = (
            new_bytes[index : index + 1_048_576]
            for index in range(0, len(new_bytes), 1_048_576)
        )
        idempotency_key = f"doc-{resource.resource_id}-{version_id}"[:128]
        self.content_service.publish(
            principal=principal,
            idempotency_key=idempotency_key,
            manifest=manifest,
            chunks=chunks,
        )
        connection.execute(
            insert(RESOURCE_VERSIONS).values(
                version_id=version_id,
                resource_id=resource.resource_id,
                version_number=version_number,
                artifact_id=artifact_id,
                artifact_sha256=sha256,
                parent_version_id=source["version_id"],
                created_by=actor["account_id"],
                reason="Agent 辅助修改经人工确认",
                created_at=now,
            )
        )
        return ResourceVersion(
            version_id,
            resource.resource_id,
            version_number,
            artifact_id,
            sha256,
            source["version_id"],
            actor["account_id"],
            "Agent 辅助修改经人工确认",
            now,
        )

    # ------------------------------------------------------------- helpers

    def _publish_derivative_artifact(
        self,
        *,
        actor_id: str,
        resource,
        artifact_id: str,
        sha256: str,
        body: bytes,
        idempotency_key: str,
    ) -> None:
        if self.content_service is None:
            raise ValueError("document content service is not configured")
        manifest = ArtifactManifest(
            artifact_id=artifact_id,
            kind=ArtifactKind.DOCUMENT,
            media_type="application/json",
            content_uri="artifact-store://pending",
            sha256=sha256,
            size_bytes=len(body),
            label=ResourceLabel(
                resource.artifact_owner_team_id,
                Classification.INTERNAL,
                frozenset(),
                f"artifact:{artifact_id}",
            ),
            provenance=ArtifactProvenance(
                actor_id,
                resource.artifact_owner_team_id,
                "document-workspace",
                "1",
                datetime.now(UTC),
            ),
            visible_to_tenants=frozenset({resource.artifact_owner_team_id}),
        )
        chunks = (body[index : index + 1_048_576] for index in range(0, len(body), 1_048_576))
        self.content_service.publish(
            principal=self._principal(actor_id),
            idempotency_key=idempotency_key,
            manifest=manifest,
            chunks=chunks,
        )

    def _authorized_resource(self, actor_id: str, resource_id: str):
        if self.resource_service is None:
            raise ResourceNotFound("document workspace is not configured")
        return self.resource_service.get_authorized(
            actor_id=actor_id,
            resource_id=resource_id,
            action=ResourceAction.VIEW,
            project_id=None,
        )

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
    def _version(row) -> ResourceVersion:
        return ResourceVersion(
            row["version_id"],
            row["resource_id"],
            int(row["version_number"]),
            row["artifact_id"],
            row["artifact_sha256"],
            row["parent_version_id"],
            row["created_by"],
            row["reason"],
            DocumentWorkspaceService._aware(row["created_at"]),
        )

    @staticmethod
    def _derivative(row) -> ResourceDerivative:
        return ResourceDerivative(
            row["derivative_id"],
            row["resource_id"],
            row["version_id"],
            DerivativeType(row["derivative_type"]),
            DerivativeStatus(row["status"]),
            row["summary"],
            int(row["size_bytes"]),
            row["error_category"],
            DocumentWorkspaceService._aware(row["created_at"]),
        )

    @staticmethod
    def _draft(row) -> DocumentChangeDraft:
        return DocumentChangeDraft(
            row["draft_id"],
            row["resource_id"],
            row["source_version_id"],
            row["modification_json"],
            row["generated_version_id"],
            DocumentChangeDraftStatus(row["status"]),
            int(row["version"]),
            row["created_by"],
            DocumentWorkspaceService._aware(row["created_at"]),
            row["decided_by"],
            DocumentWorkspaceService._aware(row["decided_at"]) if row["decided_at"] else None,
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
                correlation_id=str(details.get("draft_id") or details.get("resource_id") or ""),
            ),
        )


def _artifact_kind(media_type: str) -> ArtifactKind:
    media_type = media_type.lower()
    if "pdf" in media_type:
        return ArtifactKind.PDF
    if "wordprocessing" in media_type or "msword" in media_type:
        return ArtifactKind.DOCUMENT
    if "spreadsheet" in media_type or "ms-excel" in media_type or "csv" in media_type:
        return ArtifactKind.SPREADSHEET
    if "presentation" in media_type or "ms-powerpoint" in media_type:
        return ArtifactKind.PRESENTATION
    return ArtifactKind.DOCUMENT
