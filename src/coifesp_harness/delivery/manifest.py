"""Version-pinned delivery manifests derived only from real integration PASS."""

import hashlib

from sqlalchemy import func, select

from ..artifacts.repository import ARTIFACT_MANIFESTS
from ..errors import GovernanceConflictError, IntegrityError, ResourceNotFound
from ..product.repository import PROJECT_RESOURCES, PROJECT_TEAMS
from ..verification.project_evidence import load_project_verification_evidence
from ..verification.repository import TASK_VERIFICATIONS
from .repository import INTEGRATION_RUNS, PROJECT_DELIVERIES


def create_delivery_manifest(connection, *, integration, now):
    if integration["status"] != "PASS" or not integration["result_artifact_refs_json"]:
        raise GovernanceConflictError("delivery requires integration PASS with actual artifacts")
    existing = connection.execute(select(PROJECT_DELIVERIES).where(
        PROJECT_DELIVERIES.c.integration_id == integration["integration_id"])).mappings().one_or_none()
    if existing is not None:
        return dict(existing)
    version = connection.execute(select(func.coalesce(func.max(PROJECT_DELIVERIES.c.version), 0)).where(
        PROJECT_DELIVERIES.c.process_id == integration["process_id"])).scalar_one() + 1
    refs = integration["result_artifact_refs_json"]
    row = {"delivery_id": "delivery:" + hashlib.sha256(integration["integration_id"].encode()).hexdigest(),
        "project_id": integration["project_id"], "process_id": integration["process_id"],
        "integration_id": integration["integration_id"], "version": version,
        "graph_digest": integration["graph_digest"], "artifact_refs_json": refs,
        "artifact_version_manifest_json": {ref["resource_id"]: ref["version"] for ref in refs},
        "artifact_digest_manifest_json": {ref["resource_id"]: ref["sha256"] for ref in refs},
        "verification_refs_json": integration["verification_refs_json"],
        "acceptance_requirements_json": {"schema": "coifesp.delivery-acceptance.v1",
                                         "approved_completion_contract_required": True},
        "release_notes_resource_id": None, "status": "READY", "created_by": "service:project-integrator",
        "created_at": now, "updated_at": now, "approved_by": None, "accepted_at": None,
        "decision": None, "decision_key": None, "decision_digest": None, "decision_reason": None,
        "decided_by": None, "decided_at": None}
    connection.execute(PROJECT_DELIVERIES.insert().values(**row))
    return row


def current_delivery_evidence(connection, *, repository, process, graph, delivery, content):
    """Read shared metadata and real bytes; never accept a caller-provided PASS."""
    from .integration import integration_subject_digest

    integration = connection.execute(select(INTEGRATION_RUNS).where(
        INTEGRATION_RUNS.c.integration_id == delivery["integration_id"],
        INTEGRATION_RUNS.c.process_id == process.process_id,
        INTEGRATION_RUNS.c.project_id == process.project_id,
    ).with_for_update()).mappings().one_or_none()
    evidence = load_project_verification_evidence(connection, process=process, graph=graph)
    valid = (integration is not None and integration["status"] == "PASS"
        and integration["graph_digest"] == graph.digest == delivery["graph_digest"]
        and integration["subject_digest"] == integration_subject_digest(integration)
        and integration["executed_as"] == "service:project-integrator"
        and integration["initiated_by"] == "service:project-orchestrator"
        and integration["result_artifact_refs_json"] == delivery["artifact_refs_json"]
        and integration["verification_refs_json"] == delivery["verification_refs_json"]
        and {ref["verification_id"] for ref in delivery["verification_refs_json"]} == set(evidence.verification_ids)
        and evidence.outcome == "PASSED")
    refs = delivery["artifact_refs_json"]
    valid = bool(valid and refs
        and delivery["artifact_version_manifest_json"] == {ref["resource_id"]: ref["version"] for ref in refs}
        and delivery["artifact_digest_manifest_json"] == {ref["resource_id"]: ref["sha256"] for ref in refs})
    members = set(connection.execute(select(PROJECT_TEAMS.c.team_id).where(
        PROJECT_TEAMS.c.project_id == process.project_id).with_for_update()).scalars())
    verifications = connection.execute(select(TASK_VERIFICATIONS).where(
        TASK_VERIFICATIONS.c.verification_id.in_(evidence.verification_ids))).mappings().all()
    inputs = {}
    for verification in verifications:
        for artifact in verification["artifacts_json"]:
            entry = {**artifact, "version": "1"}
            if artifact["resource_id"] in inputs and inputs[artifact["resource_id"]] != entry:
                valid = False
            inputs[artifact["resource_id"]] = entry
    valid = bool(valid and integration["input_artifact_refs_json"] == [inputs[key] for key in sorted(inputs)])
    required = {node.subject_id for node in graph.nodes if node.node_type.value == "artifact"}
    covered = set(inputs) | {ref["resource_id"] for ref in refs}
    exists = bool(refs) and required.issubset(covered)
    integrity = bool(refs) and content is not None
    output_ids = {ref["resource_id"] for ref in refs}
    for ref in [*inputs.values(), *refs]:
        resource = connection.execute(select(PROJECT_RESOURCES).where(
            PROJECT_RESOURCES.c.resource_id == ref["resource_id"],
            PROJECT_RESOURCES.c.project_id == process.project_id).with_for_update()).mappings().one_or_none()
        manifest = connection.execute(select(ARTIFACT_MANIFESTS).where(
            ARTIFACT_MANIFESTS.c.owner_tenant_id == ref["owner_team_id"],
            ARTIFACT_MANIFESTS.c.artifact_id == ref["artifact_id"])).mappings().one_or_none()
        current = (resource is not None and manifest is not None
            and (ref["resource_id"] not in output_ids or (
                resource["source_integration_id"] == delivery["integration_id"]
                and resource["process_id"] == process.process_id
                and resource["produced_by_principal_id"] == "service:project-integrator"))
            and resource["owner_team_id"] == resource["artifact_owner_team_id"] == ref["owner_team_id"]
            and ref["owner_team_id"] in members
            and resource["artifact_id"] == ref["artifact_id"]
            and resource["artifact_sha256"] == ref["sha256"]
            and resource["media_type"] == ref["media_type"]
            and resource["propagation"] in {"project_readonly", "portable"}
            and all(manifest[key] == ref[key] for key in ("sha256", "size_bytes", "media_type")))
        exists = exists and current
        if not current or content is None:
            integrity = False
            continue
        size, digest = 0, hashlib.sha256()
        try:
            for chunk in content.open_policy_authorized(owner_tenant_id=ref["owner_team_id"],
                    sha256=ref["sha256"], expected_size=ref["size_bytes"]):
                size += len(chunk)
                if size > ref["size_bytes"]:
                    raise IntegrityError("delivery exceeds pinned byte size")
                digest.update(chunk)
            integrity = integrity and size == ref["size_bytes"] and digest.hexdigest() == ref["sha256"]
        except (IntegrityError, ResourceNotFound):
            integrity = False
    return {"integration_passed": valid, "all_required_verifications_pass": evidence.outcome == "PASSED",
        "all_required_tasks_terminal": bool(evidence.passed_task_ids) and evidence.outcome == "PASSED",
        "all_required_artifacts_exist": bool(exists), "artifact_integrity_valid": bool(integrity)}
