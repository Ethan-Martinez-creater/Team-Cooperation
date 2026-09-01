import asyncio
import json
from types import SimpleNamespace

from sqlalchemy import update
from test_agent_first_workspace import _call, _local_app
from test_verification_orchestration_effect import stack

from coifesp_harness.control_plane.conversation_models import ProjectHarnessView
from coifesp_harness.control_plane.harness_view import ProjectHarnessViewService
from coifesp_harness.delivery.repository import DELIVERY_METADATA
from coifesp_harness.project_process.repository import (
    PROJECT_PROCESS_EVENTS,
    SQLAlchemyProjectProcessRepository,
)
from coifesp_harness.verification.repository import VERIFICATION_METADATA
from coifesp_harness.work_graph.repository import SQLAlchemyWorkGraphRepository


def _view(tmp_path):
    value = stack(tmp_path, fail=False)
    DELIVERY_METADATA.create_all(value.engine)
    workspace = SimpleNamespace(
        workspace=lambda **kwargs: SimpleNamespace(project_id=kwargs["project_id"])
    )
    service = ProjectHarnessViewService(value.engine, workspace)
    return value, service.view(project_id="project-a", actor_id="lead-a")


def test_harness_view_projects_authority_into_user_semantics(tmp_path):
    _, view = _view(tmp_path)

    assert view["process"]["phase_label"] == "质量验证"
    assert view["process"]["semantic_status"]
    assert view["process"]["next_step"]
    assert view["work_graph"]["nodes"]
    assert view["tasks"][0]["title"]
    assert view["tasks"][0]["contract_ready"] is True
    assert view["verification"] == {
        "total": 1,
        "pending": 0,
        "passed": 1,
        "failed": 0,
        "stale": 0,
    }
    assert view["completion"]["tasks_total"] == 1
    ProjectHarnessView.model_validate(view)


def test_harness_activity_never_echoes_internal_event_content(tmp_path):
    value, _ = _view(tmp_path)
    with value.engine.begin() as connection:
        connection.execute(
            update(PROJECT_PROCESS_EVENTS)
            .where(PROJECT_PROCESS_EVENTS.c.process_id == "process-a")
            .values(
                payload_json={"secret": "DO-NOT-LEAK", "prompt": "private prompt"},
                initiated_by="run-private-identifier",
                executed_as="team-agent:private-team",
                correlation_id="private-correlation",
            )
        )
    workspace = SimpleNamespace(workspace=lambda **_: object())
    view = ProjectHarnessViewService(value.engine, workspace).view(
        project_id="project-a", actor_id="lead-a"
    )
    encoded = json.dumps(view, ensure_ascii=False)
    assert "DO-NOT-LEAK" not in encoded
    assert "private prompt" not in encoded
    assert "run-private-identifier" not in encoded
    assert "private-team" not in encoded
    assert "private-correlation" not in encoded
    assert "run_id" not in encoded
    assert "payload" not in encoded


def test_harness_view_reuses_workspace_participant_authorization(tmp_path):
    value = stack(tmp_path, fail=False)
    DELIVERY_METADATA.create_all(value.engine)

    class DenyWorkspace:
        def workspace(self, **_):
            raise LookupError("not a participant")

    service = ProjectHarnessViewService(value.engine, DenyWorkspace())
    try:
        service.view(project_id="project-a", actor_id="outsider")
    except LookupError as error:
        assert str(error) == "not a participant"
    else:
        raise AssertionError("the Harness projection bypassed project authorization")


def test_harness_view_route_returns_stable_empty_authority_for_new_project():
    app = _local_app()
    engine = app.state.project_workspace_service.engine
    SQLAlchemyProjectProcessRepository(engine).create_schema()
    SQLAlchemyWorkGraphRepository(engine).create_schema()
    VERIFICATION_METADATA.create_all(engine)
    DELIVERY_METADATA.create_all(engine)
    login = asyncio.run(
        _call(app, "POST", "/app/local-session", json={"profile_id": "lead"})
    )
    response = asyncio.run(
        _call(
            app,
            "GET",
            "/v1/projects/project-coifesp-demo/harness-view",
            headers={"Authorization": f"Bearer {login.json()['access_token']}"},
        )
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["process"] is None
    assert body["work_graph"] == {"nodes": [], "edges": []}
    assert body["activity"] == body["blockers"] == []
