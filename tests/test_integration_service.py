import io
import zipfile

import pytest
from sqlalchemy import func, select
from test_verification_orchestration_effect import stack

from coifesp_harness.artifacts import (
    ArtifactContentService,
    SQLAlchemyArtifactRepository,
)
from coifesp_harness.delivery.integration import IntegrationService
from coifesp_harness.delivery.repository import DELIVERY_METADATA, INTEGRATION_RUNS
from coifesp_harness.errors import GovernanceConflictError
from coifesp_harness.product.repository import PROJECT_RESOURCES, TEAM_TASKS
from coifesp_harness.project_process.repository import PROJECT_PROCESS_EVENTS
from coifesp_harness.verification.repository import TASK_VERIFICATIONS
from coifesp_harness.work_graph.repository import SQLAlchemyWorkGraphRepository


def prepare(tmp_path, *, publisher=None):
    value = stack(tmp_path, fail=False)
    assert value.runner.process_once(worker_id="integration-setup").status.value == "APPLIED"
    DELIVERY_METADATA.create_all(value.engine)
    registry = SQLAlchemyArtifactRepository(engine=value.engine, audit_log=value.capabilities.audit_log)
    registry.create_schema()
    value.content = ArtifactContentService(registry, value.content.store)
    value.integration = IntegrationService(repository=value.repository,
        work_graph_repository=SQLAlchemyWorkGraphRepository(value.engine),
        artifact_content=value.content, publisher=publisher)
    with value.repository.transaction() as connection:
        process = value.repository.process(connection, "process-a")
        graph = value.integration.work_graph.snapshot(connection, project_id="project-a")
    value.arguments = {"process_id": process.process_id, "expected_version": process.version,
        "expected_event_sequence": process.last_event_sequence, "expected_graph_digest": graph.digest,
        "event_id": "integration:test", "mutation_fence": lambda _: None}
    return value


def execute(value, **overrides):
    return value.integration.execute(**{**value.arguments, **overrides})


def assert_rolled_back(value):
    with value.repository.transaction() as connection:
        assert connection.execute(select(func.count()).select_from(INTEGRATION_RUNS)).scalar_one() == 0
        process = value.repository.process(connection, "process-a")
        assert process.phase.value == "INTEGRATION"
        assert process.version == value.arguments["expected_version"]
        assert value.repository.event(connection, "integration:test") is None
        assert connection.execute(select(TEAM_TASKS.c.status)).scalar_one() == "verified"


def test_real_bundle_pass_moves_to_delivery_not_completion_and_retry_is_exact(tmp_path):
    value = prepare(tmp_path)
    result = execute(value)
    assert result["status"] == "PASS"
    assert result["checks_json"][0]["code"] == "verified_artifact_bundle"
    ref = result["result_artifact_refs_json"][0]
    content = b"".join(value.content.open_policy_authorized(owner_tenant_id=ref["owner_team_id"],
        sha256=ref["sha256"], expected_size=ref["size_bytes"]))
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        assert archive.read("artifacts/000001") == b"contracted project input"
    assert execute(value)["integration_id"] == result["integration_id"]
    with value.repository.transaction() as connection:
        process = value.repository.process(connection, "process-a")
        assert (process.phase.value, process.status.value) == ("DELIVERY", "READY")
        assert process.version == value.arguments["expected_version"] + 1
        assert process.last_event_sequence == value.arguments["expected_event_sequence"] + 1
        resource = connection.execute(select(PROJECT_RESOURCES).where(
            PROJECT_RESOURCES.c.resource_id == ref["resource_id"])).mappings().one()
        assert resource["created_by"] is None
        assert resource["source_integration_id"] == result["integration_id"]
        assert resource["source_run_id"] is None
        assert connection.execute(select(func.count()).select_from(INTEGRATION_RUNS)).scalar_one() == 1


def test_corrupt_bytes_reopens_only_impacted_work_without_rewriting_verification(tmp_path):
    # No publisher may run on a failed composition.
    class NeverPublish:
        def publish(self, *args, **kwargs):
            raise AssertionError("failed integration must not publish")

    value = prepare(tmp_path, publisher=NeverPublish())
    with value.engine.connect() as connection:
        before = dict(connection.execute(select(TASK_VERIFICATIONS)).mappings().one())
    value.content.open_policy_authorized = lambda **_: iter([b"corrupted"])
    result = execute(value)
    assert result["status"] == "FAIL" and result["impacted_work_ids_json"] == ["task-a"]
    assert result["result_artifact_refs_json"] == []
    assert result["checks_json"][0]["code"] == "artifact_integrity_failed"
    with value.repository.transaction() as connection:
        process = value.repository.process(connection, "process-a")
        assert (process.phase.value, process.status.value) == ("EXECUTION", "READY")
        task = connection.execute(select(TEAM_TASKS)).mappings().one()
        assert task["status"] == "changes_requested" and task["completed_at"] is None
        assert dict(connection.execute(select(TASK_VERIFICATIONS)).mappings().one()) == before
        assert value.repository.event(connection, "integration:test").transition_key == "integration.failed"
    assert execute(value)["integration_id"] == result["integration_id"]


@pytest.mark.parametrize("field,value", [("expected_version", -1), ("expected_event_sequence", -1),
    ("expected_graph_digest", "stale")])
def test_stale_integration_snapshot_has_no_effect(tmp_path, field, value):
    state = prepare(tmp_path)
    with pytest.raises(GovernanceConflictError):
        execute(state, **{field: value})
    assert_rolled_back(state)


def test_withdrawn_shared_input_cannot_be_composed(tmp_path):
    value = prepare(tmp_path)
    with value.engine.begin() as connection:
        connection.execute(PROJECT_RESOURCES.update().values(propagation="team_private"))
    with pytest.raises(GovernanceConflictError):
        execute(value)
    assert_rolled_back(value)


def test_storage_outage_rolls_back_instead_of_recording_business_failure(tmp_path):
    value = prepare(tmp_path)

    def unavailable(**_):
        raise OSError("storage unavailable")

    value.content.open_policy_authorized = unavailable
    with pytest.raises(OSError):
        execute(value)
    assert_rolled_back(value)


def test_fence_lost_after_publication_rolls_back_registry_and_process(tmp_path):
    value = prepare(tmp_path)
    publisher = value.integration.publisher.publish

    def publish_then_lose(*args, **kwargs):
        publisher(*args, **kwargs)
        raise GovernanceConflictError("lease lost")

    value.integration.publisher.publish = publish_then_lose
    with pytest.raises(GovernanceConflictError):
        execute(value)
    assert_rolled_back(value)
    with value.engine.connect() as connection:
        assert connection.execute(select(func.count()).select_from(PROJECT_RESOURCES)).scalar_one() == 1


def test_event_listener_failure_rolls_back_entire_integration(tmp_path):
    value = prepare(tmp_path)

    def unavailable(connection, event):
        if event.event_type == "project.integration.completed":
            raise OSError("outbox unavailable")

    value.repository.set_event_listener(unavailable)
    with pytest.raises(OSError):
        execute(value)
    assert_rolled_back(value)


def test_retry_rejects_changed_policy_or_cursors(tmp_path):
    value = prepare(tmp_path)
    execute(value)
    with pytest.raises(GovernanceConflictError):
        execute(value, expected_event_sequence=999)
    with pytest.raises(GovernanceConflictError):
        execute(value, policy={"schema": "coifesp.integration-policy.v1", "mode": "artifact_composition",
            "max_artifacts": 1, "max_total_bytes": 1024})


def test_bounded_composition_failure_has_explicit_rework_event(tmp_path):
    value = prepare(tmp_path)
    result = execute(value, policy={"schema": "coifesp.integration-policy.v1", "mode": "artifact_composition",
        "max_artifacts": 1, "max_total_bytes": 1})
    assert result["status"] == "FAIL"
    assert result["checks_json"][0]["code"] == "bundle_limit_exceeded"
    with value.engine.connect() as connection:
        assert connection.execute(select(PROJECT_PROCESS_EVENTS.c.transition_key).where(
            PROJECT_PROCESS_EVENTS.c.event_id == "integration:test")).scalar_one() == "integration.failed"
