"""Durable project Planner intents and ordinary AgentRun launch protocol."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime

from sqlalchemy import and_, select
from sqlalchemy.exc import IntegrityError

from ..agent_runs import AgentRunCheckpointCodec, AgentRunService
from ..context import ContentTrust, ContextItem, ContextSource, InstructionTrust
from ..errors import GovernanceConflictError, ResourceNotFound
from ..product.repository import PROJECT_TEAMS
from ..runtime import AgentRunRequest, Message, RunBudget
from ..security import Classification, Principal, ResourceLabel
from ..work_graph import ProjectGraphSnapshot
from .commands import ProjectPlannerIntent, ProjectPlannerIntentStatus
from .repository import PROJECT_PLANNER_INTENTS, SQLAlchemyProjectProcessRepository
from .runner import ORCHESTRATOR_PRINCIPAL_ID

PLANNER_DECISION_SCHEMA = "coifesp.orchestration-decision.v1"
PLANNER_INTENT_REASONS = frozenset(
    {
        "ANALYSIS",
        "INITIAL_PLANNING",
        "TASK_DECOMPOSITION",
        "CONTRACT_REJECTED",
        "VERIFICATION_FAILED",
        "DELIVERY_REJECTED",
        "SCOPE_CHANGED",
        "RISK_CHANGED",
        "CAPACITY_UNAVAILABLE",
    }
)
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_TERMINAL_INTENT_STATUSES = frozenset(
    {
        ProjectPlannerIntentStatus.PROJECTED,
        ProjectPlannerIntentStatus.STALE,
        ProjectPlannerIntentStatus.REJECTED,
        ProjectPlannerIntentStatus.FAILED,
        ProjectPlannerIntentStatus.CANCELLED,
    }
)


class ProjectPlannerIntentService:
    def __init__(self, repository: SQLAlchemyProjectProcessRepository, *, clock=None) -> None:
        self.repository = repository
        self.clock = clock or (lambda: datetime.now(UTC))

    def create(
        self,
        *,
        process_id: str,
        project_id: str,
        owner_team_id: str,
        reason: str,
        based_on_process_version: int,
        based_on_event_sequence: int,
        graph: ProjectGraphSnapshot,
    ) -> ProjectPlannerIntent:
        process_id = self._identifier(process_id, "process_id")
        project_id = self._identifier(project_id, "project_id")
        owner_team_id = self._identifier(owner_team_id, "owner_team_id")
        if reason not in PLANNER_INTENT_REASONS:
            raise ValueError("planner intent reason is invalid")
        if graph.project_id != project_id:
            raise GovernanceConflictError("planner graph belongs to another project")
        material = {
            "process_id": process_id,
            "project_id": project_id,
            "reason": reason,
            "process_version": based_on_process_version,
            "event_sequence": based_on_event_sequence,
            "graph_snapshot_digest": graph.digest,
        }
        intent_id = f"planner-intent:{self._digest(material)}"
        now = self.clock()
        values = {
            "planner_intent_id": intent_id,
            "process_id": process_id,
            "project_id": project_id,
            "owner_team_id": owner_team_id,
            "reason": reason,
            "based_on_process_version": based_on_process_version,
            "based_on_event_sequence": based_on_event_sequence,
            "graph_snapshot_digest": graph.digest,
            "status": ProjectPlannerIntentStatus.PENDING.value,
            "run_id": None,
            "decision_id": None,
            "error_code": None,
            "created_at": now,
            "updated_at": now,
            "projected_at": None,
        }
        try:
            with self.repository.transaction() as connection:
                process = self.repository.process(connection, process_id)
                if process.project_id != project_id:
                    raise GovernanceConflictError(
                        "project process belongs to another project"
                    )
                if (
                    process.version != based_on_process_version
                    or process.last_event_sequence != based_on_event_sequence
                ):
                    raise GovernanceConflictError("planner intent snapshot is stale")
                participant = connection.execute(
                    select(PROJECT_TEAMS.c.team_id).where(
                        and_(
                            PROJECT_TEAMS.c.project_id == project_id,
                            PROJECT_TEAMS.c.team_id == owner_team_id,
                        )
                    )
                ).scalar_one_or_none()
                if participant is None:
                    raise GovernanceConflictError(
                        "planner owner team is not a project participant"
                    )
                connection.execute(PROJECT_PLANNER_INTENTS.insert().values(**values))
        except IntegrityError:
            existing = self.get(intent_id)
            self._assert_same(existing, values)
            return existing
        return self.get(intent_id)

    def request(
        self,
        *,
        process_id: str,
        owner_team_id: str,
        reason: str,
        work_graph,
        run_service,
    ):
        """Create-or-resume the one Planner Run for the current process snapshot."""

        with self.repository.transaction() as connection:
            process = self.repository.process(connection, process_id)
        graph = work_graph.snapshot(project_id=process.project_id)
        intent = self.create(
            process_id=process.process_id,
            project_id=process.project_id,
            owner_team_id=owner_team_id,
            reason=reason,
            based_on_process_version=process.version,
            based_on_event_sequence=process.last_event_sequence,
            graph=graph,
        )
        run = self.launch(intent=intent, graph=graph, run_service=run_service)
        return self.get(intent.planner_intent_id), run

    def launch(self, *, intent: ProjectPlannerIntent, graph: ProjectGraphSnapshot, run_service):
        if intent.status not in {
            ProjectPlannerIntentStatus.PENDING,
            ProjectPlannerIntentStatus.RUNNING,
        }:
            raise GovernanceConflictError("planner intent is already terminal")
        if graph.project_id != intent.project_id or graph.digest != intent.graph_snapshot_digest:
            raise GovernanceConflictError("planner graph snapshot changed before launch")
        principal = Principal(
            ORCHESTRATOR_PRINCIPAL_ID,
            intent.owner_team_id,
            frozenset({"agent_run_controller"}),
            Classification.INTERNAL,
            frozenset(),
            True,
        )
        protocol = self.protocol(intent)
        authoritative_context = {
            "intent": protocol,
            "graph": {
                "project_id": graph.project_id,
                "digest": graph.digest,
                "nodes": [
                    {
                        "node_id": item.node_id,
                        "node_type": item.node_type.value,
                        "subject_id": item.subject_id,
                    }
                    for item in graph.nodes
                ],
                "relations": [
                    {
                        "relation_id": item.relation_id,
                        "source_node_id": item.source_node_id,
                        "relation_type": item.relation_type.value,
                        "target_node_id": item.target_node_id,
                    }
                    for item in graph.relations
                ],
                "subjects": list(graph.subjects),
            },
        }
        run_digest = self._digest({"intent": intent.planner_intent_id})
        run_id = f"run-planner-{run_digest[:32]}"
        context = ContextItem(
            item_id=f"planner-context:{intent.planner_intent_id}",
            content=json.dumps(authoritative_context, ensure_ascii=False, sort_keys=True),
            source=ContextSource.GOVERNANCE,
            source_id=f"process:{intent.process_id}",
            label=ResourceLabel(
                intent.owner_team_id,
                Classification.INTERNAL,
                frozenset(),
                intent.project_id,
            ),
            content_trust=ContentTrust.AUTHORITATIVE,
            instruction_trust=InstructionTrust.DATA_ONLY,
            created_at=(intent.created_at if intent.created_at.tzinfo is not None
                        else intent.created_at.replace(tzinfo=UTC)),
        )
        request = AgentRunRequest(
            run_id=run_id,
            correlation_id=f"planner-intent:{intent.planner_intent_id}",
            principal=principal,
            messages=(
                Message("system", self.system_prompt(), "coifesp-harness"),
                Message(
                    "user",
                    "Analyze the authoritative project snapshot and return the command decision.",
                    None,
                ),
            ),
            budget=RunBudget(max_turns=4, max_tool_calls=1),
            context_items=(context,),
            context_purpose=f"project-orchestration:{intent.project_id}",
            tool_authorization=None,
        )
        checkpoint = AgentRunCheckpointCodec().initial(request)
        # The worker resolves the service principal from this exact binding.
        # Publish binding and queued Run together so it can never claim an
        # otherwise valid Planner before its delegation is visible.
        if run_service.repository.engine is not self.repository.engine:
            raise ValueError("Planner launch requires a shared database engine")
        with self.repository.transaction() as connection:
            bound_intents = ProjectPlannerIntentService(
                self.repository.using_connection(connection), clock=self.clock
            )
            current_row = connection.execute(select(PROJECT_PLANNER_INTENTS).where(
                PROJECT_PLANNER_INTENTS.c.planner_intent_id == intent.planner_intent_id
            ).with_for_update()).mappings().one()
            if current_row["run_id"] is not None:
                existing_run = run_service.repository.using_connection(connection).get(
                    tenant_id=intent.owner_team_id, run_id=current_row["run_id"]
                )
                if (existing_run.run_id != run_id
                        or existing_run.owner_principal_id != principal.principal_id
                        or existing_run.correlation_id != request.correlation_id):
                    raise GovernanceConflictError("Planner Run does not match its intent binding")
                return existing_run
            bound_intents.bind_run(intent.planner_intent_id, run_id)
            bound_runs = AgentRunService(
                run_service.repository.using_connection(connection),
                checkpoint_codec=run_service.checkpoint_codec,
            )
            return bound_runs.create(
                principal=principal,
                run_id=run_id,
                correlation_id=request.correlation_id,
                idempotency_key=f"planner:{intent.planner_intent_id}",
                checkpoint=checkpoint,
                max_failures=3,
            )

    def bind_run(self, planner_intent_id: str, run_id: str) -> ProjectPlannerIntent:
        planner_intent_id = self._identifier(planner_intent_id, "planner_intent_id")
        run_id = self._identifier(run_id, "run_id")
        with self.repository.transaction() as connection:
            row = connection.execute(
                select(PROJECT_PLANNER_INTENTS).with_for_update().where(
                    PROJECT_PLANNER_INTENTS.c.planner_intent_id == planner_intent_id
                )
            ).mappings().one_or_none()
            if row is None:
                raise ResourceNotFound("planner intent is unavailable")
            if row["run_id"] not in {None, run_id}:
                raise GovernanceConflictError("planner intent is bound to another run")
            if ProjectPlannerIntentStatus(row["status"]) in _TERMINAL_INTENT_STATUSES:
                raise GovernanceConflictError("planner intent is already terminal")
            connection.execute(
                PROJECT_PLANNER_INTENTS.update()
                .where(
                    PROJECT_PLANNER_INTENTS.c.planner_intent_id == planner_intent_id
                )
                .values(
                    run_id=run_id,
                    status=ProjectPlannerIntentStatus.RUNNING.value,
                    updated_at=self.clock(),
                )
            )
        return self.get(planner_intent_id)

    def finish(
        self,
        planner_intent_id: str,
        *,
        status: ProjectPlannerIntentStatus,
        decision_id: str | None = None,
        error_code: str | None = None,
    ) -> ProjectPlannerIntent:
        status = ProjectPlannerIntentStatus(status)
        if status not in _TERMINAL_INTENT_STATUSES:
            raise ValueError("planner intent terminal status is invalid")
        if decision_id is not None:
            decision_id = self._identifier(decision_id, "decision_id")
        if error_code is not None:
            error_code = self._identifier(error_code, "error_code")
        with self.repository.transaction() as connection:
            row = connection.execute(
                select(PROJECT_PLANNER_INTENTS).with_for_update().where(
                    PROJECT_PLANNER_INTENTS.c.planner_intent_id == planner_intent_id
                )
            ).mappings().one_or_none()
            if row is None:
                raise ResourceNotFound("planner intent is unavailable")
            current = ProjectPlannerIntentStatus(row["status"])
            if current in _TERMINAL_INTENT_STATUSES:
                if (
                    current is status
                    and row["decision_id"] == decision_id
                    and row["error_code"] == error_code
                ):
                    return self._from_row(row)
                raise GovernanceConflictError("planner intent is already terminal")
            now = self.clock()
            connection.execute(
                PROJECT_PLANNER_INTENTS.update()
                .where(
                    PROJECT_PLANNER_INTENTS.c.planner_intent_id == planner_intent_id
                )
                .values(
                    status=status.value,
                    decision_id=decision_id,
                    error_code=error_code,
                    updated_at=now,
                    projected_at=now,
                )
            )
        return self.get(planner_intent_id)

    def get(self, planner_intent_id: str) -> ProjectPlannerIntent:
        with self.repository.transaction() as connection:
            row = connection.execute(
                select(PROJECT_PLANNER_INTENTS).where(
                    PROJECT_PLANNER_INTENTS.c.planner_intent_id == planner_intent_id
                )
            ).mappings().one_or_none()
        if row is None:
            raise ResourceNotFound("planner intent is unavailable")
        return self._from_row(row)

    def find_by_run(self, run_id: str) -> ProjectPlannerIntent | None:
        with self.repository.engine.connect() as connection:
            row = connection.execute(
                select(PROJECT_PLANNER_INTENTS).where(
                    PROJECT_PLANNER_INTENTS.c.run_id == run_id
                )
            ).mappings().one_or_none()
        return None if row is None else self._from_row(row)

    def pending(self) -> tuple[ProjectPlannerIntent, ...]:
        with self.repository.engine.connect() as connection:
            rows = connection.execute(
                select(PROJECT_PLANNER_INTENTS)
                .where(
                    PROJECT_PLANNER_INTENTS.c.status.in_(
                        [
                            ProjectPlannerIntentStatus.PENDING.value,
                            ProjectPlannerIntentStatus.RUNNING.value,
                        ]
                    )
                )
                .order_by(PROJECT_PLANNER_INTENTS.c.created_at)
            ).mappings()
            return tuple(self._from_row(row) for row in rows)

    @staticmethod
    def protocol(intent: ProjectPlannerIntent) -> dict:
        return {
            "schema": PLANNER_DECISION_SCHEMA,
            "planner_intent_id": intent.planner_intent_id,
            "process_id": intent.process_id,
            "project_id": intent.project_id,
            "reason": intent.reason,
            "based_on_process_version": intent.based_on_process_version,
            "based_on_event_sequence": intent.based_on_event_sequence,
            "graph_snapshot_digest": intent.graph_snapshot_digest,
            "commands": [],
        }

    @staticmethod
    def system_prompt() -> str:
        return (
            "You are the COIFESP project Planner. Return exactly one JSON object and no "
            "Markdown using schema coifesp.orchestration-decision.v1. Copy every binding "
            "field from the authoritative intent unchanged. Populate commands only with "
            "these exact forms: propose_task(type,task_id,team_id,title,description,"
            "dependencies,contract); propose_dependency(type,source_id,target_id); propose_risk("
            "type,risk_id,title,description,severity,likelihood,mitigation); "
            "propose_decision(type,decision_id,title,description,options); request_rework("
            "type,task_id,reason); request_replan(type,reason); request_human_input(type,"
            "question,input_schema); request_human_gate(type,gate_type,subject_id,reason,"
            "allowed_decisions). Unknown fields are forbidden. You cannot execute tools, "
            "SQL, team Agents, or state transitions. An empty commands array is valid. "
            "At most one request_replan is allowed per batch. A proposed task is not "
            "accepted or dispatched automatically. Its contract must contain exactly "
            "requested_capability, input_manifest, output_contract, verification_policy, "
            "autonomy_requirement. requested_capability contains tags (array), protocol, "
            "input_contract_ref, output_contract_ref, verification_policy_ref. "
            "input_manifest contains resources (resource_id, required boolean, mode of "
            "team_private/project_readonly/portable) and work_nodes (existing node IDs). "
            "Do not invent resource IDs or share team-private inputs with another team. "
            "output_contract contains artifact_types, required boolean and max_count; "
            "verification_policy contains criteria (criterion_id, type, required boolean; "
            "type is tool_check/agent_review/human_review, tool_check requires tool). "
            "autonomy_requirement is a nonempty capability autonomy label (max 32 characters). "
            "Use request_human_input when a complete executable contract cannot be specified. "
            "request_rework opens a human review request; it cannot manufacture a failed "
            "verification or overwrite a verified task."
        )

    @staticmethod
    def _from_row(row) -> ProjectPlannerIntent:
        return ProjectPlannerIntent(
            planner_intent_id=row["planner_intent_id"],
            process_id=row["process_id"],
            project_id=row["project_id"],
            owner_team_id=row["owner_team_id"],
            reason=row["reason"],
            based_on_process_version=row["based_on_process_version"],
            based_on_event_sequence=row["based_on_event_sequence"],
            graph_snapshot_digest=row["graph_snapshot_digest"],
            status=ProjectPlannerIntentStatus(row["status"]),
            run_id=row["run_id"],
            decision_id=row["decision_id"],
            error_code=row["error_code"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            projected_at=row["projected_at"],
        )

    @staticmethod
    def _assert_same(intent: ProjectPlannerIntent, values: dict) -> None:
        for name in (
            "process_id",
            "project_id",
            "owner_team_id",
            "reason",
            "based_on_process_version",
            "based_on_event_sequence",
            "graph_snapshot_digest",
        ):
            if getattr(intent, name) != values[name]:
                raise GovernanceConflictError("planner intent idempotency conflict")

    @staticmethod
    def _identifier(value, field):
        if not isinstance(value, str) or not _ID.fullmatch(value):
            raise ValueError(f"{field} is invalid")
        return value

    @staticmethod
    def _digest(value) -> str:
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(encoded).hexdigest()


__all__ = [
    "PLANNER_DECISION_SCHEMA",
    "PLANNER_INTENT_REASONS",
    "ProjectPlannerIntentService",
]
