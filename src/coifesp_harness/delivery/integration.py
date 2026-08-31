"""Deterministic integration of current, verified task artifacts.

This service owns the fenced process transaction. A composition is evidence,
not human acceptance; success can enter DELIVERY, never TERMINAL.
"""

import hashlib
from datetime import UTC, datetime

from sqlalchemy import func, select

from ..artifacts.repository import ARTIFACT_MANIFESTS
from ..errors import GovernanceConflictError, IntegrityError, ResourceNotFound
from ..product.repository import PROJECT_RESOURCES, TEAM_TASKS
from ..project_process.orchestrator import VerificationOutcome
from ..project_process.repository import (
    PROJECT_GATES,
    PROJECT_INPUT_REQUESTS,
    PROJECT_PROCESSES,
)
from ..project_process.service import ProjectProcessService
from ..verification.project_evidence import load_project_verification_evidence
from ..verification.repository import TASK_VERIFICATIONS
from ..verification.service import _digest
from .bundle import BundleArtifact, assemble_delivery_bundle
from .repository import INTEGRATION_RUNS

DEFAULT_INTEGRATION_POLICY = {
    "schema": "coifesp.integration-policy.v1", "mode": "artifact_composition",
    "max_artifacts": 256, "max_total_bytes": 64 * 1024 * 1024,
}


def integration_subject_digest(row):
    return _digest({"project_id": row["project_id"], "process_id": row["process_id"],
        "graph_digest": row["graph_digest"], "integration_policy": row["integration_policy_json"],
        "verification_refs": row["verification_refs_json"], "input_artifacts": row["input_artifact_refs_json"]})


def validate_integration_policy(policy):
    value = dict(DEFAULT_INTEGRATION_POLICY if policy is None else policy)
    if (set(value) != set(DEFAULT_INTEGRATION_POLICY)
            or value["schema"] != DEFAULT_INTEGRATION_POLICY["schema"]
            or value["mode"] != "artifact_composition"):
        raise ValueError("unsupported integration policy")
    for name in ("max_artifacts", "max_total_bytes"):
        if type(value[name]) is not int or not 1 <= value[name] <= DEFAULT_INTEGRATION_POLICY[name]:
            raise ValueError("integration limits must be positive and bounded")
    return value


class IntegrationService:
    def __init__(self, *, repository, work_graph_repository, artifact_content, publisher=None, clock=None):
        if artifact_content is None:
            raise ValueError("integration requires readable immutable artifact storage")
        if (work_graph_repository.engine is not repository.engine
                or artifact_content.repository.engine is not repository.engine):
            raise ValueError("integration requires one shared database")
        if publisher is None:
            from .publication import IntegrationArtifactPublisher

            publisher = IntegrationArtifactPublisher(repository=repository, artifact_content=artifact_content, clock=clock)
        self.repository, self.work_graph = repository, work_graph_repository
        self.content, self.publisher = artifact_content, publisher
        self.clock = clock or (lambda: datetime.now(UTC))

    def execute(self, *, process_id, expected_version, expected_event_sequence,
                expected_graph_digest, event_id, mutation_fence, policy=None, decision_id=None,
                bound_connection=None):
        if not callable(mutation_fence):
            raise TypeError("integration requires a live execution fence")
        policy = validate_integration_policy(policy)
        repository = (self.repository if bound_connection is None
                      else self.repository.using_connection(bound_connection))
        with repository.transaction() as connection:
            mutation_fence(connection)
            connection.execute(select(PROJECT_PROCESSES.c.process_id).where(
                PROJECT_PROCESSES.c.process_id == process_id).with_for_update()).scalar_one()
            process = self.repository.process(connection, process_id)
            previous = self.repository.event(connection, event_id)
            if previous is not None:
                row = connection.execute(select(INTEGRATION_RUNS).where(
                    INTEGRATION_RUNS.c.integration_id == previous.subject_id,
                    INTEGRATION_RUNS.c.process_id == process_id,
                )).mappings().one_or_none()
                if (row is None or previous.event_type != "project.integration.completed"
                        or previous.process_id != process_id
                        or previous.payload.get("decision_id") != decision_id
                        or previous.payload.get("subject_digest") != row["subject_digest"]
                        or row["integration_policy_json"] != policy
                        or row["graph_digest"] != expected_graph_digest
                        or row["based_on_process_version"] != expected_version
                        or row["based_on_event_sequence"] != expected_event_sequence
                        or row["subject_digest"] != integration_subject_digest(row)
                        or row["initiated_by"] != "service:project-orchestrator"
                        or row["executed_as"] != "service:project-integrator"
                        or previous.payload.get("outcome") != row["status"]
                        or row["status"] not in {"PASS", "FAIL"}):
                    raise GovernanceConflictError("integration retry differs from committed execution")
                mutation_fence(connection)
                return dict(row)
            if (process.phase.value != "INTEGRATION" or process.status.value != "READY"
                    or process.version != expected_version
                    or process.last_event_sequence != expected_event_sequence):
                raise GovernanceConflictError("integration snapshot is stale or not ready")
            for table in (PROJECT_GATES, PROJECT_INPUT_REQUESTS):
                if connection.execute(select(table.c.process_id).where(
                    table.c.process_id == process_id, table.c.status == "OPEN").limit(1)).first():
                    raise GovernanceConflictError("integration is blocked by a human decision")
            graph = self.work_graph.snapshot(connection, project_id=process.project_id)
            if graph.digest != expected_graph_digest:
                raise GovernanceConflictError("integration graph changed")
            evidence = load_project_verification_evidence(connection, process=process, graph=graph)
            if evidence.outcome is not VerificationOutcome.PASSED:
                raise GovernanceConflictError("integration requires current verification PASS")
            verifications = connection.execute(select(TASK_VERIFICATIONS).where(
                TASK_VERIFICATIONS.c.verification_id.in_(evidence.verification_ids),
            ).order_by(TASK_VERIFICATIONS.c.task_id)).mappings().all()
            refs, inputs, owners = [], {}, {}
            for verification in verifications:
                refs.append({key: verification[key] for key in (
                    "verification_id", "task_id", "source_run_id", "contract_version", "subject_digest")})
                for manifest in verification["artifacts_json"]:
                    entry = {**manifest, "version": "1"}
                    resource_id = entry["resource_id"]
                    if resource_id in inputs and inputs[resource_id] != entry:
                        raise GovernanceConflictError("verified artifact bindings disagree")
                    inputs[resource_id] = entry
                    owners.setdefault(resource_id, set()).add(verification["task_id"])
            now = self.clock()
            row = {"project_id": process.project_id, "process_id": process_id,
                "graph_digest": graph.digest, "based_on_process_version": process.version,
                "based_on_event_sequence": process.last_event_sequence,
                "input_artifact_refs_json": [inputs[key] for key in sorted(inputs)],
                "integration_policy_json": policy, "verification_refs_json": refs,
                "result_artifact_refs_json": [], "checks_json": [], "impacted_work_ids_json": [],
                "initiated_by": "service:project-orchestrator", "executed_as": "service:project-integrator",
                "status": "PENDING", "created_at": now, "updated_at": now, "completed_at": None}
            row["subject_digest"] = integration_subject_digest(row)
            row["integration_id"] = "integration:" + hashlib.sha256(
                f"{process_id}:{row['subject_digest']}".encode()).hexdigest()
            row["version"] = connection.execute(select(func.coalesce(func.max(INTEGRATION_RUNS.c.version), 0)).where(
                INTEGRATION_RUNS.c.process_id == process_id)).scalar_one() + 1
            if connection.execute(select(INTEGRATION_RUNS.c.integration_id).where(
                INTEGRATION_RUNS.c.integration_id == row["integration_id"])).first():
                raise GovernanceConflictError("integration subject already exists without this event")
            connection.execute(INTEGRATION_RUNS.insert().values(**row))
            artifacts, checks, impacted = [], [], set()
            total = sum(item["size_bytes"] for item in inputs.values())
            if not inputs:
                checks.append(self._check("FAIL", "empty_artifact_set", []))
                impacted.update(evidence.passed_task_ids)
            elif len(inputs) > policy["max_artifacts"] or total > policy["max_total_bytes"]:
                checks.append(self._check("FAIL", "bundle_limit_exceeded", list(inputs)))
                impacted.update(evidence.passed_task_ids)
            else:
                for resource_id, entry in sorted(inputs.items()):
                    resource = connection.execute(select(PROJECT_RESOURCES).where(
                        PROJECT_RESOURCES.c.resource_id == resource_id,
                        PROJECT_RESOURCES.c.project_id == process.project_id,
                    ).with_for_update()).mappings().one_or_none()
                    manifest = connection.execute(select(ARTIFACT_MANIFESTS).where(
                        ARTIFACT_MANIFESTS.c.owner_tenant_id == entry["owner_team_id"],
                        ARTIFACT_MANIFESTS.c.artifact_id == entry["artifact_id"],
                    )).mappings().one_or_none()
                    if (resource is None or manifest is None
                            or resource["propagation"] not in {"project_readonly", "portable"}
                            or resource["artifact_owner_team_id"] != entry["owner_team_id"]
                            or resource["artifact_id"] != entry["artifact_id"]
                            or resource["artifact_sha256"] != entry["sha256"]
                            or manifest["sha256"] != entry["sha256"]
                            or manifest["size_bytes"] != entry["size_bytes"]
                            or manifest["media_type"] != entry["media_type"]):
                        raise GovernanceConflictError("integration input is no longer shared/current")
                    try:
                        raw = self._read(entry)
                    except (IntegrityError, ResourceNotFound):
                        checks.append(self._check("FAIL", "artifact_integrity_failed", [resource_id]))
                        impacted.update(owners[resource_id])
                        continue
                    artifacts.append(BundleArtifact(resource_id, "1", resource["title"],
                        entry["media_type"], entry["sha256"], raw))
            if not checks:
                try:
                    bundle = assemble_delivery_bundle(artifacts,
                        max_artifacts=policy["max_artifacts"], max_total_bytes=policy["max_total_bytes"])
                except ValueError:
                    checks.append(self._check("FAIL", "bundle_invalid", list(inputs)))
                    impacted.update(evidence.passed_task_ids)
                else:
                    result = self.publisher.publish(connection, integration_id=row["integration_id"],
                        bundle=bundle, mutation_fence=mutation_fence)
                    row["result_artifact_refs_json"] = [result]
                    checks.append(self._check("PASS", "verified_artifact_bundle", list(inputs)))
            now = self.clock()
            row.update(status="FAIL" if impacted else "PASS", checks_json=checks,
                       impacted_work_ids_json=sorted(impacted), updated_at=now, completed_at=now)
            if impacted:
                changed = connection.execute(TEAM_TASKS.update().where(
                    TEAM_TASKS.c.process_id == process_id, TEAM_TASKS.c.task_id.in_(impacted),
                    TEAM_TASKS.c.status == "verified",
                ).values(status="changes_requested", updated_at=now, completed_at=None)).rowcount
                if changed != len(impacted):
                    raise GovernanceConflictError("integration rework task changed")
            connection.execute(INTEGRATION_RUNS.update().where(
                INTEGRATION_RUNS.c.integration_id == row["integration_id"],
                INTEGRATION_RUNS.c.status == "PENDING",
            ).values(**{key: row[key] for key in ("status", "checks_json", "impacted_work_ids_json",
                "result_artifact_refs_json", "updated_at", "completed_at")}))
            ProjectProcessService(self.repository.using_connection(connection), clock=self.clock).apply_transition(
                process_id=process_id, event_id=event_id, event_type="project.integration.completed",
                transition_key="integration.failed" if impacted else "integration.passed",
                expected_version=process.version, subject_type="integration", subject_id=row["integration_id"],
                initiated_by=row["initiated_by"], executed_as=row["executed_as"],
                correlation_id=row["integration_id"], payload={"integration_id": row["integration_id"],
                    "outcome": row["status"], "subject_digest": row["subject_digest"],
                    "decision_id": decision_id, "impacted_work_ids": sorted(impacted)},
                mutation_fence=mutation_fence)
            mutation_fence(connection)
            return row

    def _read(self, entry):
        chunks, size, digest = [], 0, hashlib.sha256()
        for chunk in self.content.open_policy_authorized(owner_tenant_id=entry["owner_team_id"],
                sha256=entry["sha256"], expected_size=entry["size_bytes"]):
            size += len(chunk)
            if size > entry["size_bytes"]:
                raise IntegrityError("integration bytes exceed pinned size")
            chunks.append(chunk)
            digest.update(chunk)
        if size != entry["size_bytes"] or digest.hexdigest() != entry["sha256"]:
            raise IntegrityError("integration bytes differ from verified artifact")
        return b"".join(chunks)

    @staticmethod
    def _check(status, code, resources):
        return {"criterion_id": "artifact-bundle", "type": "artifact_composition",
                "status": status, "code": code, "resource_ids": sorted(resources)}
