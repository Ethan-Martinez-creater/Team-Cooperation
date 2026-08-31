"""Internal, integration-bound publication of a byte-verified delivery bundle."""

import hashlib
import io
import json
import zipfile
from datetime import UTC, datetime

from sqlalchemy import select

from ..artifacts.models import ArtifactKind, ArtifactManifest, ArtifactProvenance
from ..artifacts.repository import ARTIFACT_MANIFESTS
from ..errors import GovernanceConflictError
from ..product.repository import PROJECT_RESOURCES, PROJECT_TEAMS, PROJECTS
from ..project_process.repository import PROJECT_PROCESSES
from ..security import Classification, Principal, ResourceLabel
from ..work_graph.repository import SQLAlchemyWorkGraphRepository
from .bundle import BundleArtifact, DeliveryBundle, assemble_delivery_bundle
from .repository import INTEGRATION_RUNS

_MAX_BYTES = 64 * 1024 * 1024


class IntegrationArtifactPublisher:
    def __init__(self, *, repository, artifact_content, clock=None):
        if artifact_content.repository.engine is not repository.engine:
            raise ValueError("integration publication requires one database")
        self.repository, self.content = repository, artifact_content
        self.clock = clock or (lambda: datetime.now(UTC))

    def publish(self, connection, *, integration_id, bundle, mutation_fence):
        from .integration import integration_subject_digest, validate_integration_policy

        if (connection.engine is not self.repository.engine or not connection.in_transaction()
                or not callable(mutation_fence)):
            raise ValueError("publication requires caller transaction and live fence")
        mutation_fence(connection)
        row = connection.execute(select(INTEGRATION_RUNS).where(
            INTEGRATION_RUNS.c.integration_id == integration_id).with_for_update()).mappings().one()
        connection.execute(select(PROJECT_PROCESSES.c.process_id).where(
            PROJECT_PROCESSES.c.process_id == row["process_id"]).with_for_update()).scalar_one()
        process = self.repository.process(connection, row["process_id"])
        graph = SQLAlchemyWorkGraphRepository(self.repository.engine).snapshot(
            connection, project_id=process.project_id)
        if (row["status"] != "PENDING" or process.phase.value != "INTEGRATION"
                or process.status.value != "READY" or row["project_id"] != process.project_id
                or row["based_on_process_version"] != process.version
                or row["based_on_event_sequence"] != process.last_event_sequence
                or row["graph_digest"] != graph.digest
                or row["subject_digest"] != integration_subject_digest(row)
                or row["initiated_by"] != "service:project-orchestrator"
                or row["executed_as"] != "service:project-integrator"):
            raise GovernanceConflictError("integration publication snapshot is not current")
        policy = validate_integration_policy(row["integration_policy_json"])
        entries = self._validate_bundle(bundle, policy)
        inputs = row["input_artifact_refs_json"]
        if (len(inputs) != len(entries) or len({item["resource_id"] for item in inputs}) != len(inputs)):
            raise GovernanceConflictError("bundle differs from pinned integration inputs")
        members = set(connection.execute(select(PROJECT_TEAMS.c.team_id).where(
            PROJECT_TEAMS.c.project_id == process.project_id).with_for_update()).scalars())
        owner = connection.execute(select(PROJECTS.c.owner_team_id).where(
            PROJECTS.c.project_id == process.project_id).with_for_update()).scalar_one()
        if owner not in members:
            raise GovernanceConflictError("integration owner no longer participates")
        for pinned, entry in zip(sorted(inputs, key=lambda item: item["resource_id"]), entries, strict=True):
            resource = connection.execute(select(PROJECT_RESOURCES).where(
                PROJECT_RESOURCES.c.resource_id == pinned["resource_id"],
                PROJECT_RESOURCES.c.project_id == process.project_id,
            ).with_for_update()).mappings().one_or_none()
            manifest = connection.execute(select(ARTIFACT_MANIFESTS).where(
                ARTIFACT_MANIFESTS.c.owner_tenant_id == pinned["owner_team_id"],
                ARTIFACT_MANIFESTS.c.artifact_id == pinned["artifact_id"],
            )).mappings().one_or_none()
            if (resource is None or manifest is None or pinned["owner_team_id"] not in members
                    or resource["owner_team_id"] != pinned["owner_team_id"]
                    or resource["artifact_owner_team_id"] != pinned["owner_team_id"]
                    or resource["artifact_id"] != pinned["artifact_id"]
                    or resource["artifact_sha256"] != pinned["sha256"]
                    or resource["propagation"] not in {"project_readonly", "portable"}
                    or resource["title"] != entry["filename"]
                    or any(entry[key] != pinned[key] for key in
                        ("resource_id", "version", "sha256", "media_type", "size_bytes"))
                    or any(manifest[key] != pinned[key] for key in ("sha256", "media_type", "size_bytes"))):
                raise GovernanceConflictError("integration input was withdrawn or replaced")
        identity = "integration-artifact:" + hashlib.sha256(integration_id.encode()).hexdigest()
        producer = "service:project-integrator"
        result = {"resource_id": identity, "owner_team_id": owner, "artifact_id": identity,
            "sha256": bundle.sha256, "media_type": "application/zip", "size_bytes": len(bundle.content),
            "version": "1"}
        existing = connection.execute(select(PROJECT_RESOURCES).where(
            PROJECT_RESOURCES.c.resource_id == identity)).mappings().one_or_none()
        if existing is not None:
            if (existing["project_id"] != process.project_id or existing["process_id"] != process.process_id
                    or existing["source_integration_id"] != integration_id
                    or existing["produced_by_principal_id"] != producer
                    or existing["owner_team_id"] != owner or existing["artifact_sha256"] != bundle.sha256
                    or existing["propagation"] != "project_readonly"):
                raise GovernanceConflictError("integration publication retry changed its bundle")
            mutation_fence(connection)
            return result
        mutation_fence(connection)
        stored = self.content.store.put(tenant_id=owner, chunks=(bundle.content,),
            expected_sha256=bundle.sha256, expected_size=len(bundle.content), idempotency_key=identity)
        mutation_fence(connection)
        now = self.clock()
        principal = Principal(producer, owner, frozenset({"project_integrator"}), Classification.INTERNAL,
            frozenset({f"project:{process.project_id}"}), True)
        manifest = ArtifactManifest(identity, ArtifactKind.GENERIC, "application/zip", stored.storage_uri,
            bundle.sha256, len(bundle.content), ResourceLabel(owner, Classification.INTERNAL,
                principal.compartments, identity),
            ArtifactProvenance(producer, owner, "project.integration", "1", now), frozenset(members))
        self.content.repository._persist_publication(connection, principal=principal,
            idempotency_key=identity, manifest=manifest)
        connection.execute(PROJECT_RESOURCES.insert().values(resource_id=identity, project_id=process.project_id,
            owner_team_id=owner, created_by=None, title=f"Integration bundle {row['version']}",
            artifact_owner_team_id=owner, artifact_id=identity, artifact_sha256=bundle.sha256,
            media_type="application/zip", propagation="project_readonly", created_at=now,
            produced_by_principal_id=producer, source_run_id=None, source_integration_id=integration_id,
            process_id=process.process_id))
        mutation_fence(connection)
        return result

    @staticmethod
    def _validate_bundle(bundle, policy):
        if (not isinstance(bundle, DeliveryBundle) or type(bundle.content) is not bytes
                or type(bundle.manifest) is not bytes or len(bundle.content) > _MAX_BYTES + 1024 * 1024
                or len(bundle.manifest) > 1024 * 1024
                or hashlib.sha256(bundle.content).hexdigest() != bundle.sha256):
            raise GovernanceConflictError("invalid integration bundle bytes")
        try:
            with zipfile.ZipFile(io.BytesIO(bundle.content)) as archive:
                infos = archive.infolist()
                if (not 2 <= len(infos) <= policy["max_artifacts"] + 1
                        or any(info.compress_type != zipfile.ZIP_STORED or info.flag_bits & 1 for info in infos)
                        or sum(info.file_size for info in infos) > policy["max_total_bytes"] + 1024 * 1024
                        or len({info.filename for info in infos}) != len(infos)):
                    raise ValueError("invalid archive bounds")
                if archive.read("manifest.json") != bundle.manifest:
                    raise ValueError("manifest bytes differ")
                metadata = json.loads(bundle.manifest)
                entries = metadata["artifacts"]
                artifacts = [BundleArtifact(entry["resource_id"], entry["version"], entry["filename"],
                    entry["media_type"], entry["sha256"], archive.read(entry["path"])) for entry in entries]
            rebuilt = assemble_delivery_bundle(artifacts, max_artifacts=policy["max_artifacts"],
                max_total_bytes=policy["max_total_bytes"])
            if rebuilt != bundle:
                raise ValueError("noncanonical or extra archive content")
            return entries
        except (ValueError, TypeError, KeyError, zipfile.BadZipFile, UnicodeError) as exc:
            raise GovernanceConflictError("bundle does not match canonical byte manifest") from exc
