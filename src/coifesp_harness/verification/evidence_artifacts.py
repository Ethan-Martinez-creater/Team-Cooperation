"""Publish minimal GitHub verification summaries through the existing artifact store."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime

from sqlalchemy import insert, select

from ..artifacts.models import ArtifactKind, ArtifactManifest, ArtifactProvenance
from ..errors import GovernanceConflictError
from ..product.repository import PROJECT_RESOURCES
from ..security import Classification, Principal, ResourceLabel


def publish_github_evidence(*, connection, content, outcome, binding, verification_id, subject_digest):
    if content is None:
        return outcome
    for check in outcome["checks"]:
        if check.get("tool") != "github.get_commit_checks" or not check.get("receipt_digest"):
            continue
        document = {
            "schema": "coifesp.github-verification-evidence.v1",
            "verification_id": verification_id, "subject_digest": subject_digest,
            "criterion_id": check["criterion_id"], "status": check["status"],
            "receipt_digest": check["receipt_digest"], "attempt": check["tool_attempt"],
        }
        data = json.dumps(document, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
        sha256 = hashlib.sha256(data).hexdigest()
        resource_id = "github-evidence:" + sha256
        team_id, project_id = binding["team_id"], binding["project_id"]
        existing = connection.execute(select(PROJECT_RESOURCES).where(
            PROJECT_RESOURCES.c.resource_id == resource_id,
        )).mappings().one_or_none()
        if existing is not None:
            if (existing["project_id"] != project_id or existing["owner_team_id"] != team_id
                    or existing["artifact_sha256"] != sha256
                    or existing["source_run_id"] != binding["run_id"]):
                raise GovernanceConflictError("GitHub evidence artifact binding changed")
        else:
            stored = content.store.put(tenant_id=team_id, chunks=(data,), expected_sha256=sha256,
                                       expected_size=len(data), idempotency_key=resource_id)
            now = datetime.now(UTC)
            principal = Principal("service:project-verifier", team_id,
                                  frozenset({"team_agent"}), Classification.INTERNAL,
                                  frozenset({f"project:{project_id}"}), True)
            manifest = ArtifactManifest(
                resource_id, ArtifactKind.GENERIC, "application/json", stored.storage_uri,
                sha256, len(data), ResourceLabel(team_id, Classification.INTERNAL,
                    principal.compartments, resource_id),
                ArtifactProvenance(principal.principal_id, team_id, "github.get_commit_checks", "1", now),
                frozenset({team_id}),
            )
            content.repository._persist_publication(connection, principal=principal,
                idempotency_key=resource_id, manifest=manifest)
            connection.execute(insert(PROJECT_RESOURCES).values(
                resource_id=resource_id, project_id=project_id, owner_team_id=team_id,
                created_by=None, title=f"GitHub 验证结果 · 第 {check['tool_attempt']} 次 · {check['status']}",
                artifact_owner_team_id=team_id, artifact_id=resource_id, artifact_sha256=sha256,
                media_type="application/json", propagation="team_private", created_at=now,
                produced_by_principal_id=principal.principal_id, source_run_id=binding["run_id"],
                source_integration_id=None, process_id=binding["process_id"],
            ))
        # The artifact is team-private. The public verification view keeps only
        # the existing submission refs and receipt digest, never this private ID.
    return outcome
