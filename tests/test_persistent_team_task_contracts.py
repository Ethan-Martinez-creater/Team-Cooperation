import asyncio
import hashlib
import json
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI, Request
from sqlalchemy import select
from test_team_agent_dispatcher import assert_no_dispatch, dispatch, record, stack

from coifesp_harness.agent_runs import AgentRunCheckpointCodec
from coifesp_harness.artifacts import (
    ArtifactContentService,
    LocalImmutableArtifactStore,
)
from coifesp_harness.artifacts.repository import ARTIFACT_MANIFESTS, ARTIFACT_METADATA
from coifesp_harness.context import ContentTrust, ContextSource
from coifesp_harness.control_plane.product_routes import build_product_router
from coifesp_harness.errors import (
    GovernanceConflictError,
    PolicyDenied,
    ResourceNotFound,
)
from coifesp_harness.product import TeamCollaborationService
from coifesp_harness.product.repository import PROJECT_RESOURCES, TEAM_TASKS
from coifesp_harness.project_process.context import ProjectAgentContextBuilder
from coifesp_harness.team_agents.dispatcher import TeamAgentDispatcher
from coifesp_harness.team_agents.task_contracts import (
    PersistentTaskDispatchFactLoader,
    TeamTaskContractService,
)


def proposal(value, **overrides):
    capability = value.facts.requirement
    payload = {
        "expected_version": 0,
        "process_id": "process-a",
        "work_node_id": "node:task:task-a",
        "requested_capability": {
            "tags": list(capability.tags),
            "protocol": capability.protocol,
            "input_contract_ref": value.facts.contract.input_contract_ref,
            "output_contract_ref": value.facts.contract.output_contract_ref,
            "verification_policy_ref": "verification:review:v1",
        },
        "input_manifest": {"resources": [], "work_nodes": []},
        "output_contract": {
            "artifact_types": ["text/plain"],
            "required": True,
            "max_count": 2,
        },
        "verification_policy": {
            "criteria": [
                {"criterion_id": "review", "type": "agent_review", "required": True},
            ]
        },
        "autonomy_requirement": "supervised",
    }
    return {**payload, **overrides}


def propose(value, **overrides):
    return TeamTaskContractService(value.engine).propose(
        project_id="project-a",
        task_id="task-a",
        actor_id="lead-a",
        **proposal(value, **overrides),
    )


def accept(value, version=1):
    return TeamCollaborationService(value.engine).respond_task(
        project_id="project-a",
        task_id="task-a",
        actor_id="lead-b",
        accept=True,
        expected_contract_version=version,
    )


def load(value, reader=None):
    with value.repository.transaction() as connection:
        row = TeamCollaborationService._task_row(connection, "project-a", "task-a")
        return PersistentTaskDispatchFactLoader(
            engine=value.engine, artifact_content=reader
        )(
            connection=connection,
            process=value.repository.process(connection, "process-a"),
            task=TeamCollaborationService._task(row),
        )


def resource(value, tmp_path, *, owner="team-a", propagation="project_readonly"):
    raw = b"contracted project input"
    digest = hashlib.sha256(raw).hexdigest()
    ARTIFACT_METADATA.create_all(value.engine)
    store = LocalImmutableArtifactStore(tmp_path)
    store.put(
        tenant_id=owner,
        chunks=[raw],
        expected_sha256=digest,
        expected_size=len(raw),
        idempotency_key="test-input",
    )
    with value.engine.begin() as connection:
        connection.execute(
            ARTIFACT_MANIFESTS.insert().values(
                owner_tenant_id=owner,
                artifact_id="artifact-input",
                kind="document",
                media_type="text/plain",
                content_uri="artifact-store://input",
                sha256=digest,
                size_bytes=len(raw),
                classification=1,
                compartments=[],
                producer_principal_id="lead-a",
                source_tool="fixture",
                source_version="v1",
                created_at=datetime.now(UTC),
                visible_to_tenants=[],
                content_digest="b" * 64,
                metadata_schema="test.input.v1",
            )
        )
        connection.execute(
            PROJECT_RESOURCES.insert().values(
                resource_id="resource-input",
                project_id="project-a",
                owner_team_id=owner,
                created_by="lead-a",
                title="Input",
                artifact_owner_team_id=owner,
                artifact_id="artifact-input",
                artifact_sha256=digest,
                media_type="text/plain",
                propagation=propagation,
                created_at=datetime.now(UTC),
            )
        )
    return ArtifactContentService(SimpleNamespace(engine=value.engine), store)


def test_persisted_contract_acceptance_is_version_pinned_and_loaded():
    value = stack(accepted=False)
    initial_digest = value.graph.snapshot(project_id="project-a").digest
    assert propose(value)["version"] == 1
    assert value.graph.snapshot(project_id="project-a").digest != initial_digest
    for version in (None, 2, True):
        with pytest.raises(
            GovernanceConflictError, match="current task contract version"
        ):
            accept(value, version)
    assert_no_dispatch(value)
    accept(value)
    facts = load(value)
    assert facts.contract_accepted
    assert facts.requirement == value.facts.requirement
    details = json.loads(facts.shared_items[0].content)
    assert details["version"] == 1 and details["output_contract"]["required"]
    assert details["verification_policy"]["criteria"][0]["type"] == "agent_review"
    with pytest.raises(GovernanceConflictError, match="immutable"):
        propose(value, expected_version=1)


def test_first_contract_after_legacy_acceptance_requires_fresh_target_confirmation():
    value = stack(accepted=True)
    contract = propose(value)
    assert contract["version"] == 1
    assert contract["accepted_version"] is None
    with value.engine.connect() as connection:
        task = connection.execute(select(TEAM_TASKS)).mappings().one()
    assert task["status"] == "proposed"
    assert task["assigned_account_id"] is None
    accept(value, 1)
    assert load(value).contract_accepted


def test_proposal_revision_requires_source_and_current_version_and_resets_rejection():
    value = stack(accepted=False)
    service = TeamTaskContractService(value.engine)
    with pytest.raises(PolicyDenied):
        service.propose(
            project_id="project-a",
            task_id="task-a",
            actor_id="lead-b",
            **proposal(value),
        )
    propose(value)
    with pytest.raises(GovernanceConflictError, match="version changed"):
        propose(value)
    TeamCollaborationService(value.engine).respond_task(
        project_id="project-a",
        task_id="task-a",
        actor_id="lead-b",
        accept=False,
        expected_contract_version=1,
    )
    assert propose(value, expected_version=1)["accepted_version"] is None
    with pytest.raises(GovernanceConflictError):
        accept(value, 1)
    accept(value, 2)
    assert load(value).contract_accepted


@pytest.mark.parametrize(
    "override", [{"process_id": "wrong"}, {"work_node_id": "wrong"}]
)
def test_wrong_process_or_node_rejected_without_partial_write(override):
    value = stack(accepted=False)
    with pytest.raises(GovernanceConflictError):
        propose(value, **override)
    with value.engine.connect() as connection:
        assert (
            connection.execute(
                select(TEAM_TASKS.c.source_contract_version)
            ).scalar_one()
            is None
        )


def test_legacy_accepted_tasks_do_not_acquire_fabricated_contracts():
    value = stack()
    with pytest.raises(GovernanceConflictError, match="version-pinned"):
        load(value)
    with pytest.raises(ResourceNotFound):
        TeamTaskContractService(value.engine).get(
            project_id="project-a", task_id="task-a", actor_id="lead-b"
        )


def test_real_dispatch_uses_persisted_contract_and_encrypted_structured_context():
    value = stack(accepted=False)
    propose(value)
    accept(value)
    previous = value.dispatcher
    value.dispatcher = TeamAgentDispatcher(
        repository=value.repository,
        work_graph_repository=previous.work_graph,
        capability_adapter=previous.capabilities,
        runtime_resolver=previous.runtime_resolver,
        run_service=previous.run_service,
    )
    record(value, "decision-structured")
    result = dispatch(value, decision_id="decision-structured")
    checkpoint = AgentRunCheckpointCodec().decode(
        value.runs.load_checkpoint(
            tenant_id="team-b",
            run_id=result.run_id,
        )
    )
    details = [json.loads(item.content) for item in checkpoint["context_items"]]
    assert any(
        item.get("schema") == "coifesp.team-task-contract.v1" for item in details
    )
    graph = next(
        item
        for item in details
        if item.get("schema") == "coifesp.team-task-work-graph.v1"
    )
    assert "input_manifest_json" not in json.dumps(graph)
    assert dispatch(value, decision_id="decision-structured").duplicate


@pytest.mark.parametrize(
    "owner,mode,allowed",
    [
        ("team-a", "team_private", False),
        ("team-b", "team_private", True),
        ("team-a", "project_readonly", True),
        ("team-a", "portable", True),
    ],
)
def test_input_visibility_reuses_project_policy(tmp_path, owner, mode, allowed):
    value = stack(accepted=False)
    content = resource(value, tmp_path, owner=owner, propagation=mode)
    propose(
        value,
        input_manifest={
            "resources": [
                {"resource_id": "resource-input", "required": True, "mode": mode},
            ]
        },
    )
    accept(value)
    if not allowed:
        with pytest.raises(PolicyDenied):
            load(value, content)
    else:
        facts = load(value, content)
        assert facts.contract.input_resource_ids == ("resource-input",)
        assert facts.shared_items[1].source is ContextSource.DOCUMENT
        assert facts.shared_items[1].content_trust is ContentTrust.UNTRUSTED
        assert (
            json.loads(facts.shared_items[1].content)["text"]
            == "contracted project input"
        )


def test_revoked_required_input_prevents_new_run(tmp_path):
    value = stack(accepted=False)
    content = resource(value, tmp_path)
    propose(
        value,
        input_manifest={
            "resources": [
                {
                    "resource_id": "resource-input",
                    "required": True,
                    "mode": "project_readonly",
                },
            ]
        },
    )
    accept(value)
    with value.engine.begin() as connection:
        connection.execute(
            PROJECT_RESOURCES.update().values(propagation="team_private")
        )
    value.dispatcher.fact_loader = PersistentTaskDispatchFactLoader(
        engine=value.engine, artifact_content=content
    )
    record(value, "decision-revoked")
    with pytest.raises(PolicyDenied):
        dispatch(value, decision_id="decision-revoked")
    assert_no_dispatch(value)


def test_optional_private_input_not_in_context_or_contract(tmp_path):
    value = stack(accepted=False)
    content = resource(value, tmp_path, propagation="team_private")
    propose(
        value,
        input_manifest={
            "resources": [
                {
                    "resource_id": "resource-input",
                    "required": False,
                    "mode": "team_private",
                },
            ]
        },
    )
    accept(value)
    facts = load(value, content)
    assert not facts.contract.input_resource_ids
    assert "resource-input" not in "".join(item.content for item in facts.shared_items)


def test_missing_reader_and_foreign_work_nodes_fail_closed(tmp_path):
    value = stack(accepted=False)
    resource(value, tmp_path)
    propose(
        value,
        input_manifest={
            "resources": [
                {
                    "resource_id": "resource-input",
                    "required": True,
                    "mode": "project_readonly",
                },
            ]
        },
    )
    accept(value)
    with pytest.raises(GovernanceConflictError, match="reader"):
        load(value)
    other = stack(accepted=False)
    propose(other, input_manifest={"work_nodes": ["missing-node"]})
    accept(other)
    with pytest.raises(GovernanceConflictError, match="work node"):
        load(other)


def test_contract_http_roundtrip_and_explicit_acceptance_version():
    value = stack(accepted=False)
    app = FastAPI()

    async def auth(request: Request):
        return SimpleNamespace(
            principal=SimpleNamespace(principal_id=request.headers["x-test-actor"])
        )

    app.include_router(
        build_product_router(
            authenticator=auth,
            accounts=None,
            directory=None,
            collaboration=TeamCollaborationService(value.engine),
        )
    )

    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            path = "/v1/projects/project-a/tasks/task-a"
            result = await client.put(
                path + "/execution-contract",
                json=proposal(value),
                headers={"x-test-actor": "lead-a"},
            )
            assert result.status_code == 200, result.text
            result = await client.get(
                path + "/execution-contract", headers={"x-test-actor": "lead-b"}
            )
            assert result.json()["version"] == 1
            result = await client.post(
                path + ":respond",
                json={"accept": True, "expected_contract_version": True},
                headers={"x-test-actor": "lead-b"},
            )
            assert result.status_code == 422
            result = await client.post(
                path + ":respond",
                json={"accept": True, "expected_contract_version": 1},
                headers={"x-test-actor": "lead-b"},
            )
            assert result.status_code == 200, result.text
            assert result.json()["status"] == "accepted"

    asyncio.run(scenario())


def test_graph_projection_does_not_broadcast_other_teams_private_manifest():
    subjects = (
        {
            "node_type": "task",
            "subject_id": "other-task",
            "value": {
                "title": "Public project task",
                "input_manifest_json": {"secret": "private-file"},
                "artifact_resource_ids": "[private-file]",
                "source_contract_version": 2,
            },
        },
        {
            "node_type": "artifact",
            "subject_id": "private-file",
            "value": {"title": "Secret"},
        },
    )
    view = ProjectAgentContextBuilder._execution_subjects(subjects)
    assert "private-file" not in json.dumps(view)
    assert view[0]["value"]["source_contract_version"] == 2
    assert subjects[0]["value"]["input_manifest_json"] == {"secret": "private-file"}
