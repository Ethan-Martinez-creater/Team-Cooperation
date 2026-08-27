from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict
from datetime import UTC, datetime

from ..audit import AuditEvent, AuditSink
from ..errors import (
    IdempotencyConflict,
    MemoryError,
    MemoryUnavailableError,
    PolicyDenied,
)
from ..idempotency import ClaimStatus
from ..security.models import Action, Principal
from ..security.policy import DecisionEffect, PolicyEngine
from .crypto import TenantMemoryKeyring
from .models import (
    EncryptedMemoryRecord,
    MemoryScope,
    MemorySearchResult,
    MemoryStatus,
    MemoryView,
    MemoryWriteRequest,
    MemoryWriteResult,
    SourceType,
    TrustLevel,
)
from .policy import AdmissionEffect, MemoryAdmissionPolicy
from .repository import MemoryRepository
from .search import MemoryEmbeddingProvider, cosine, tokenize

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


class MemoryService:
    def __init__(
        self,
        *,
        repository: MemoryRepository,
        keyring: TenantMemoryKeyring,
        policy: PolicyEngine,
        admission: MemoryAdmissionPolicy,
        audit: AuditSink,
        embedding_provider: MemoryEmbeddingProvider | None = None,
    ) -> None:
        self.repository = repository
        self.keyring = keyring
        self.policy = policy
        self.admission = admission
        self.audit = audit
        self.embedding_provider = embedding_provider

    def write(self, request: MemoryWriteRequest) -> MemoryWriteResult:
        self._validate_write_request(request)
        decision = self.policy.decide_resource_access(
            principal=request.principal,
            action=Action.MEMORY_WRITE,
            resource=request.label,
        )
        if decision.effect is not DecisionEffect.PERMIT:
            self._audit_write(request, "denied", decision.reason)
            raise PolicyDenied(decision.reason)
        admission = self.admission.assess(request)
        if admission.effect is AdmissionEffect.DENY:
            self._audit_write(
                request,
                "denied",
                admission.reason,
                indicators=admission.indicators,
            )
            raise MemoryError(admission.reason)

        canonical_request = self._canonical_request(request)
        request_fingerprint = self.keyring.request_fingerprint(
            request.principal.tenant_id, canonical_request
        )
        acceptable_fingerprints = self.keyring.request_fingerprints(
            request.principal.tenant_id, canonical_request
        )
        status = (
            MemoryStatus.ACTIVE
            if admission.effect is AdmissionEffect.ACCEPT
            else MemoryStatus.QUARANTINED
        )
        encrypted = self.keyring.encrypt(
            memory_id=request.memory_id,
            tenant_id=request.principal.tenant_id,
            scope=request.scope,
            kind=request.kind,
            label=request.label,
            plaintext=request.content,
        )
        record = EncryptedMemoryRecord(
            memory_id=request.memory_id,
            tenant_id=request.principal.tenant_id,
            scope=request.scope,
            kind=request.kind,
            status=status,
            label=request.label,
            source=request.source,
            created_by=request.principal.principal_id,
            owner_principal_id=request.owner_principal_id,
            project_id=request.project_id,
            session_id=request.session_id,
            ciphertext=encrypted.ciphertext,
            nonce=encrypted.nonce,
            content_fingerprint=encrypted.content_fingerprint,
            key_id=encrypted.key_id,
            created_at=datetime.now(UTC),
            expires_at=request.expires_at,
        )
        claim = self.repository.add_idempotently(
            record=record,
            namespace="memory.write",
            idempotency_key=request.idempotency_key,
            request_digest=request_fingerprint,
            acceptable_request_digests=acceptable_fingerprints,
            search_terms=self.keyring.search_tokens(
                request.principal.tenant_id, tokenize(request.content)
            ),
        )
        if claim is ClaimStatus.CONFLICT:
            self._audit_write(
                request,
                "denied",
                "memory idempotency key was reused with different content",
            )
            raise IdempotencyConflict("memory idempotency key was reused with different content")
        if claim is ClaimStatus.DUPLICATE:
            existing = self.repository.get(
                request.principal.tenant_id,
                request.memory_id,
            )
            if existing is None:
                raise MemoryError("atomic idempotency invariant is broken")
            self._audit_write(
                request,
                "duplicate",
                "duplicate request suppressed",
                content_fingerprint=existing.content_fingerprint,
            )
            return MemoryWriteResult(
                memory_id=existing.memory_id,
                status=existing.status,
                version=existing.version,
                admission_reason="duplicate request suppressed",
                duplicate=True,
            )
        self._audit_write(
            request,
            status.value,
            admission.reason,
            indicators=admission.indicators,
            content_fingerprint=record.content_fingerprint,
        )
        return MemoryWriteResult(
            memory_id=record.memory_id,
            status=record.status,
            version=record.version,
            admission_reason=admission.reason,
        )

    def read(
        self,
        *,
        principal: Principal,
        memory_id: str,
        session_id: str | None = None,
    ) -> MemoryView:
        record = self.repository.get(principal.tenant_id, memory_id)
        if record is None:
            raise MemoryUnavailableError("memory is not available")
        self._authorize_read(principal, record, session_id=session_id)
        if record.status is not MemoryStatus.ACTIVE:
            raise MemoryUnavailableError("memory is not available")
        if record.expires_at and record.expires_at <= datetime.now(UTC):
            raise MemoryUnavailableError("memory is not available")
        return self._view(record)

    def read_for_review(
        self,
        *,
        principal: Principal,
        memory_id: str,
    ) -> MemoryView:
        self._require_curator(principal)
        record = self.repository.get(principal.tenant_id, memory_id)
        if record is None or record.status is not MemoryStatus.QUARANTINED:
            raise MemoryUnavailableError("quarantined memory is not available")
        decision = self.policy.decide_resource_access(
            principal=principal,
            action=Action.MEMORY_READ,
            resource=record.label,
        )
        if decision.effect is not DecisionEffect.PERMIT:
            raise PolicyDenied(decision.reason)
        return self._view(record)

    def review(
        self,
        *,
        principal: Principal,
        memory_id: str,
        expected_version: int,
        approve: bool,
        reason: str,
    ) -> MemoryStatus:
        self._require_curator(principal)
        if not reason.strip():
            raise MemoryError("memory review reason is required")
        record = self.repository.get(principal.tenant_id, memory_id)
        if record is None or record.status is not MemoryStatus.QUARANTINED:
            raise MemoryUnavailableError("quarantined memory is not available")
        if (
            record.scope in {MemoryScope.TEAM_PROJECT, MemoryScope.ORGANIZATION}
            and record.created_by == principal.principal_id
        ):
            raise MemoryError("shared memory review requires separation of duties")
        new_status = MemoryStatus.ACTIVE if approve else MemoryStatus.REVOKED
        updated = self.repository.transition_status(
            tenant_id=principal.tenant_id,
            memory_id=memory_id,
            expected_version=expected_version,
            expected_status=MemoryStatus.QUARANTINED,
            new_status=new_status,
        )
        self.audit.append(
            AuditEvent(
                tenant_id=principal.tenant_id,
                event_type="memory.review",
                actor_id=principal.principal_id,
                outcome=new_status.value,
                details={
                    "memory_id": memory_id,
                    "version": updated.version,
                    "reason_digest": hashlib.sha256(reason.encode()).hexdigest(),
                },
                correlation_id=memory_id,
            )
        )
        return updated.status

    def recall(
        self,
        *,
        principal: Principal,
        scope: MemoryScope,
        project_id: str | None = None,
        session_id: str | None = None,
        limit: int = 20,
    ) -> tuple[MemoryView, ...]:
        owner = principal.principal_id if scope is MemoryScope.USER_PRIVATE else None
        records = self.repository.list_recallable(
            tenant_id=principal.tenant_id,
            scope=scope,
            owner_principal_id=owner,
            project_id=project_id,
            session_id=session_id,
            limit=limit,
        )
        views: list[MemoryView] = []
        for record in records:
            try:
                self._authorize_read(principal, record, session_id=session_id)
            except (MemoryError, PolicyDenied):
                continue
            views.append(self._view(record))
        return tuple(views)

    def search(self, *, principal: Principal, query: str, scope: MemoryScope,
               project_id: str | None = None, session_id: str | None = None,
               limit: int = 20, hybrid: bool = False,
               allow_external_egress: bool = False) -> tuple[MemorySearchResult, ...]:
        if not query.strip() or len(query) > 2_000 or not 1 <= limit <= 100:
            raise MemoryError("memory search request is invalid")
        terms = tokenize(query)
        if not terms:
            return ()
        if hybrid and self.embedding_provider is None:
            raise MemoryError("hybrid memory search is not configured")
        if hybrid and self.embedding_provider.external and not allow_external_egress:
            raise PolicyDenied("external embedding egress is not authorized")
        owner = principal.principal_id if scope is MemoryScope.USER_PRIVATE else None
        candidates = self.repository.search_candidates(
            tenant_id=principal.tenant_id,
            search_terms=self.keyring.search_tokens(principal.tenant_id, terms),
            scope=scope, owner_principal_id=owner, project_id=project_id,
            session_id=session_id, limit=min(500, max(limit * 5, 50)),
        )
        authorized: list[tuple[MemoryView, float]] = []
        for record, hits in candidates:
            try:
                self._authorize_read(principal, record, session_id=session_id)
            except (MemoryError, PolicyDenied):
                continue
            authorized.append((self._view(record), hits / len(terms)))
        semantic_scores: list[float | None] = [None] * len(authorized)
        if hybrid and authorized:
            if any(int(item.label.classification) > self.embedding_provider.max_data_classification
                   for item, _ in authorized):
                raise PolicyDenied("embedding provider classification boundary is exceeded")
            embeddings = self.embedding_provider.embed([query, *(item.content for item, _ in authorized)])
            if len(embeddings) != len(authorized) + 1:
                raise MemoryError("embedding provider returned an invalid result count")
            semantic_scores = [cosine(embeddings[0], value) for value in embeddings[1:]]
        results = [MemorySearchResult(memory=view, lexical_score=lexical,
            semantic_score=semantic, combined_score=(
                lexical if semantic is None else 0.4 * lexical + 0.6 * ((semantic + 1) / 2)
            )) for (view, lexical), semantic in zip(authorized, semantic_scores)]
        results.sort(key=lambda item: (item.combined_score, item.memory.created_at), reverse=True)
        return tuple(results[:limit])

    def reindex(self, *, principal: Principal, memory_id: str) -> None:
        self._require_curator(principal)
        record = self.repository.get(principal.tenant_id, memory_id)
        if record is None:
            raise MemoryUnavailableError("memory is not available")
        content = self.keyring.decrypt(record)
        self.repository.replace_search_terms(tenant_id=principal.tenant_id,
            memory_id=memory_id,
            search_terms=self.keyring.search_tokens(principal.tenant_id, tokenize(content)))
        self.audit.append(AuditEvent(tenant_id=principal.tenant_id,
            event_type="memory.search_index_rebuilt", actor_id=principal.principal_id,
            outcome="indexed", details={"memory_id": memory_id,
                "key_versions": sorted(self.keyring.available_key_ids)},
            correlation_id=memory_id))

    def _authorize_read(
        self,
        principal: Principal,
        record: EncryptedMemoryRecord,
        *,
        session_id: str | None,
    ) -> None:
        decision = self.policy.decide_resource_access(
            principal=principal,
            action=Action.MEMORY_READ,
            resource=record.label,
        )
        if decision.effect is not DecisionEffect.PERMIT:
            raise PolicyDenied(decision.reason)
        if (
            record.scope is MemoryScope.USER_PRIVATE
            and record.owner_principal_id != principal.principal_id
        ):
            raise MemoryUnavailableError("memory is not available")
        if record.scope is MemoryScope.SESSION and record.session_id != session_id:
            raise MemoryUnavailableError("memory is not available")
        if (
            record.scope is MemoryScope.TEAM_PROJECT
            and record.project_id not in principal.compartments
        ):
            raise MemoryUnavailableError("memory is not available")

    def _validate_write_request(self, request: MemoryWriteRequest) -> None:
        if len(request.memory_id) > 64:
            raise MemoryError("memory_id is invalid")
        for name, value in (
            ("memory_id", request.memory_id),
            ("idempotency_key", request.idempotency_key),
            ("source_id", request.source.source_id),
        ):
            if not _ID_RE.fullmatch(value):
                raise MemoryError(f"{name} is invalid")
        expected_resource = f"memory:{request.memory_id}"
        if request.label.resource_id != expected_resource:
            raise MemoryError(f"memory resource_id must be {expected_resource}")
        if request.label.owner_tenant_id != request.principal.tenant_id:
            raise MemoryError("memory label must be owned by the principal tenant")
        if request.expires_at:
            expiry = request.expires_at
            if expiry.tzinfo is None:
                raise MemoryError("memory expiry must be timezone-aware")
            if expiry <= datetime.now(UTC):
                raise MemoryError("memory expiry must be in the future")
        if request.source.trust_level is TrustLevel.UNTRUSTED and request.expires_at is None:
            raise MemoryError("untrusted memory requires an expiry")
        if request.scope is MemoryScope.USER_PRIVATE:
            if request.owner_principal_id != request.principal.principal_id:
                raise MemoryError("private memory must be owned by its writer")
        elif request.scope is MemoryScope.SESSION:
            if not request.session_id:
                raise MemoryError("session memory requires session_id")
        elif request.scope is MemoryScope.TEAM_PROJECT:
            if not request.project_id or request.project_id not in request.principal.compartments:
                raise MemoryError("team memory requires a project in principal compartments")
        elif request.scope is MemoryScope.ORGANIZATION:
            self._require_curator(request.principal)
        if (
            request.source.trust_level >= TrustLevel.VERIFIED
            and request.source.source_type in {SourceType.USER, SourceType.AGENT}
            and "memory_curator" not in request.principal.roles
        ):
            raise MemoryError("user or agent content cannot self-assert verified trust")

    @staticmethod
    def _require_curator(principal: Principal) -> None:
        if "memory_curator" not in principal.roles:
            raise PolicyDenied("memory curator role is required")

    def _view(self, record: EncryptedMemoryRecord) -> MemoryView:
        return MemoryView(
            memory_id=record.memory_id,
            scope=record.scope,
            kind=record.kind,
            content=self.keyring.decrypt(record),
            label=record.label,
            source=record.source,
            status=record.status,
            created_at=record.created_at,
            expires_at=record.expires_at,
            version=record.version,
        )

    @staticmethod
    def _canonical_request(request: MemoryWriteRequest) -> bytes:
        value = {
            "memory_id": request.memory_id,
            "scope": request.scope.value,
            "kind": request.kind.value,
            "content": request.content,
            "classification": int(request.label.classification),
            "compartments": sorted(request.label.compartments),
            "resource_id": request.label.resource_id,
            "source": {
                **asdict(request.source),
                "source_type": request.source.source_type.value,
                "trust_level": int(request.source.trust_level),
            },
            "owner_principal_id": request.owner_principal_id,
            "project_id": request.project_id,
            "session_id": request.session_id,
            "expires_at": (request.expires_at.isoformat() if request.expires_at else None),
        }
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()

    def _audit_write(
        self,
        request: MemoryWriteRequest,
        outcome: str,
        reason: str,
        **details,
    ) -> None:
        self.audit.append(
            AuditEvent(
                tenant_id=request.principal.tenant_id,
                event_type="memory.write",
                actor_id=request.principal.principal_id,
                outcome=outcome,
                details={
                    "memory_id": request.memory_id,
                    "scope": request.scope.value,
                    "kind": request.kind.value,
                    "classification": request.label.classification.name,
                    "reason": reason,
                    **details,
                },
                correlation_id=request.correlation_id,
            )
        )
