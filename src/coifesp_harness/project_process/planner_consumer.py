"""Atomic, restart-safe consumption of validated Planner command batches.

The transaction is the claim: process/decision row locks serialize consumers.
No network/model/tool calls are performed while it is held. A batch either
commits all effects, counters and outcomes or remains pending for recovery.
"""

import hashlib
from datetime import UTC, datetime

from sqlalchemy import select

from ..agent_runs.repository import AGENT_RUNS
from ..errors import GovernanceConflictError
from ..product.repository import PROJECT_RESOURCES, PROJECT_TEAMS, TEAM_TASKS
from .command_service import ProjectProcessCommandService
from .command_validator import ProjectOrchestrationCommandValidator
from .commands import ProjectOrchestrationDecisionStatus, ProjectProcessCommandStatus
from .gates import PROJECT_GATE_DECISIONS, ProjectGateType
from .human_service import HumanGateService
from .planner import ProjectPlannerIntentService
from .repository import (
    PROJECT_EXECUTION_POLICIES,
    PROJECT_EXECUTION_USAGE,
    PROJECT_GATES,
    PROJECT_INPUT_REQUESTS,
    PROJECT_ORCHESTRATION_DECISIONS,
    PROJECT_PLANNER_INTENTS,
    PROJECT_PROCESS_COMMANDS,
    PROJECT_PROCESSES,
)
from .runner import ProjectOrchestratorWorkerOutcome, ProjectOrchestratorWorkerStatus
from .service import ProjectProcessService

PRINCIPAL = "service:project-orchestrator"


def identity(prefix, value):
    return prefix + ":" + hashlib.sha256(value.encode()).hexdigest()


class PlannerCommandConsumer:
    def __init__(
        self,
        *,
        repository,
        work_graph_repository,
        graph_effects=None,
        clock=None,
        after_effect=None,
    ):
        if repository.engine is not work_graph_repository.engine:
            raise ValueError("Planner consumption requires one shared database")
        if graph_effects is None:
            from .planner_graph_effects import PlannerGraphMutations

            graph_effects = PlannerGraphMutations(work_graph_repository)
        self.repository, self.graph, self.graph_effects = (
            repository,
            work_graph_repository,
            graph_effects,
        )
        self.clock = clock or (lambda: datetime.now(UTC))
        self.after_effect = after_effect

    def process_once(self, *, worker_id, process_id=None, lease_seconds=30):
        # No expiring lease is needed for an all-database transaction; unlike a
        # TeamRun dispatcher this component does not release the lock for I/O.
        with self.repository.transaction() as connection:
            query = (
                select(PROJECT_ORCHESTRATION_DECISIONS.c.decision_id)
                .join(
                    PROJECT_PLANNER_INTENTS,
                    PROJECT_PLANNER_INTENTS.c.decision_id
                    == PROJECT_ORCHESTRATION_DECISIONS.c.decision_id,
                )
                .where(
                    PROJECT_ORCHESTRATION_DECISIONS.c.status == "PENDING",
                    PROJECT_PLANNER_INTENTS.c.status == "PROJECTED",
                )
            )
            if process_id is not None:
                query = query.where(
                    PROJECT_ORCHESTRATION_DECISIONS.c.process_id == process_id
                )
            candidate = connection.execute(
                query.order_by(
                    PROJECT_ORCHESTRATION_DECISIONS.c.created_at,
                    PROJECT_ORCHESTRATION_DECISIONS.c.decision_id,
                ).limit(1)
            ).scalar_one_or_none()
        if candidate is None:
            return None
        return self.consume(candidate)

    def consume(self, decision_id):
        with self.repository.transaction() as connection:
            # sqlite3 legacy transaction mode does not BEGIN for SELECT. Without
            # an outer BEGIN, releasing the first SAVEPOINT would commit effects
            # before the enclosing transaction can roll back.
            if (
                connection.dialect.name == "sqlite"
                and not connection.connection.driver_connection.in_transaction
            ):
                connection.exec_driver_sql("BEGIN IMMEDIATE")
            decision = self.repository.decision(connection, decision_id)
            if decision is None:
                raise GovernanceConflictError("Planner decision is unavailable")
            connection.execute(
                select(PROJECT_PROCESSES.c.process_id)
                .where(PROJECT_PROCESSES.c.process_id == decision.process_id)
                .with_for_update()
            ).scalar_one()
            connection.execute(
                select(PROJECT_ORCHESTRATION_DECISIONS.c.decision_id)
                .where(PROJECT_ORCHESTRATION_DECISIONS.c.decision_id == decision_id)
                .with_for_update()
            ).scalar_one()
            decision = self.repository.decision(connection, decision_id)
            if decision.status is not ProjectOrchestrationDecisionStatus.PENDING:
                return self._outcome(decision_id, decision.status.value)
            process = self.repository.process(connection, decision.process_id)
            graph = self.graph.snapshot(connection, project_id=process.project_id)
            intent = (
                connection.execute(
                    select(PROJECT_PLANNER_INTENTS).where(
                        PROJECT_PLANNER_INTENTS.c.decision_id == decision_id,
                        PROJECT_PLANNER_INTENTS.c.process_id == process.process_id,
                        PROJECT_PLANNER_INTENTS.c.project_id == process.project_id,
                        PROJECT_PLANNER_INTENTS.c.status == "PROJECTED",
                    )
                )
                .mappings()
                .one_or_none()
            )
            if intent is None:
                raise GovernanceConflictError(
                    "decision has no projected Planner source"
                )
            commands = self.repository.commands_for_decision(connection, decision_id)
            if (
                process.phase.value == "TERMINAL"
                or process.version != decision.based_on_process_version
                or process.last_event_sequence != decision.based_on_event_sequence
                or graph.digest != decision.graph_snapshot_digest
            ):
                self._finish(
                    connection, decision, commands, "STALE", "snapshot_changed"
                )
                return self._outcome(decision_id, "STALE")
            if any(
                connection.execute(
                    select(table.c.process_id)
                    .where(
                        table.c.process_id == process.process_id,
                        table.c.status == "OPEN",
                    )
                    .limit(1)
                ).first()
                for table in (PROJECT_GATES, PROJECT_INPUT_REQUESTS)
            ):
                self._finish(
                    connection, decision, commands, "REJECTED", "human_control_open"
                )
                return self._outcome(decision_id, "REJECTED")
            try:
                validated = self._validate(
                    connection, process, graph, intent, decision, commands
                )
            except (ValueError, TypeError, KeyError, GovernanceConflictError):
                self._finish(
                    connection, decision, commands, "REJECTED", "invalid_command_batch"
                )
                return self._outcome(decision_id, "REJECTED")
            tasks = sum(item.command_type.value == "propose_task" for item in validated)
            replans = sum(
                item.command_type.value == "request_replan" for item in validated
            )
            usage = self.repository.usage(connection, process.process_id)
            policy = (
                connection.execute(
                    select(PROJECT_EXECUTION_POLICIES).where(
                        PROJECT_EXECUTION_POLICIES.c.policy_id
                        == process.execution_policy_id,
                        PROJECT_EXECUTION_POLICIES.c.version
                        == process.execution_policy_version,
                    )
                )
                .mappings()
                .one()
            )
            if (
                usage.generated_task_count + tasks > policy["max_generated_tasks"]
                or usage.replan_count + replans > policy["max_replans"]
            ):
                opened = self._budget_gate(connection, process, decision_id)
                self._finish(
                    connection,
                    decision,
                    commands,
                    "REJECTED",
                    (
                        "generation_budget_exhausted"
                        if opened
                        else "generation_budget_exhausted_existing_wait"
                    ),
                )
                return self._outcome(decision_id, "REJECTED")
            results = {}
            try:
                with connection.begin_nested():
                    # Forward task references are valid within a batch. Create
                    # every task node before materializing any dependency edge.
                    for command in validated:
                        if command.command_type.value == "propose_task":
                            results[command.command_id] = self._task(
                                connection, process, intent, command
                            )
                    for command in validated:
                        results[command.command_id] = self._effect(
                            connection,
                            process,
                            intent,
                            command,
                            results.get(command.command_id),
                        )
                        if self.after_effect:
                            self.after_effect(connection, command)
                    if tasks or replans:
                        changed = connection.execute(
                            PROJECT_EXECUTION_USAGE.update()
                            .where(
                                PROJECT_EXECUTION_USAGE.c.process_id
                                == process.process_id,
                                PROJECT_EXECUTION_USAGE.c.version == usage.version,
                            )
                            .values(
                                generated_task_count=usage.generated_task_count + tasks,
                                replan_count=usage.replan_count + replans,
                                version=usage.version + 1,
                                updated_at=self.clock(),
                            )
                        ).rowcount
                        if changed != 1:
                            raise GovernanceConflictError(
                                "project generation usage changed"
                            )
                    self._finish(
                        connection, decision, commands, "APPLIED", "applied", results
                    )
                    # Replanning is a new intent, never a model call or a replay
                    # of the old snapshot. Its snapshot includes this batch's facts.
                    for command in validated:
                        if command.command_type.value == "request_replan":
                            fresh = self.repository.process(
                                connection, process.process_id
                            )
                            ProjectPlannerIntentService(
                                self.repository.using_connection(connection),
                                clock=self.clock,
                            ).create(
                                process_id=fresh.process_id,
                                project_id=fresh.project_id,
                                owner_team_id=intent["owner_team_id"],
                                reason="SCOPE_CHANGED",
                                based_on_process_version=fresh.version,
                                based_on_event_sequence=fresh.last_event_sequence,
                                graph=self.graph.snapshot(
                                    connection, project_id=fresh.project_id
                                ),
                            )
            except (ValueError, GovernanceConflictError):
                self._finish(
                    connection,
                    decision,
                    commands,
                    "REJECTED",
                    "command_effect_rejected",
                )
                return self._outcome(decision_id, "REJECTED")
            return self._outcome(decision_id, "APPLIED")

    def _validate(self, connection, process, graph, intent, decision, commands):
        run = (
            connection.execute(
                select(AGENT_RUNS).where(
                    AGENT_RUNS.c.run_id == intent["run_id"],
                    AGENT_RUNS.c.tenant_id == intent["owner_team_id"],
                )
            )
            .mappings()
            .one_or_none()
        )
        if (
            run is None
            or run["status"] != "completed"
            or run["owner_principal_id"] != PRINCIPAL
            or run["correlation_id"] != "planner-intent:" + intent["planner_intent_id"]
            or intent["based_on_process_version"] != decision.based_on_process_version
            or intent["based_on_event_sequence"] != decision.based_on_event_sequence
            or intent["graph_snapshot_digest"] != graph.digest
        ):
            raise GovernanceConflictError(
                "Planner source or snapshot binding is invalid"
            )
        teams = set(
            connection.execute(
                select(PROJECT_TEAMS.c.team_id)
                .where(PROJECT_TEAMS.c.project_id == process.project_id)
                .with_for_update()
            ).scalars()
        )
        if intent["owner_team_id"] not in teams:
            raise GovernanceConflictError("Planner owner is no longer a participant")
        _, digest = self.repository.canonical_payload(decision.decision_json)
        if digest != decision.decision_digest:
            raise GovernanceConflictError("decision digest changed")
        validated = ProjectOrchestrationCommandValidator().validate(
            planner_intent_id=intent["planner_intent_id"],
            project_id=process.project_id,
            graph=graph,
            graph_snapshot_digest=graph.digest,
            participating_team_ids=teams,
            commands=decision.decision_json["commands"],
        )
        rows = {item.command_id: item for item in commands}
        batch_digest = ProjectProcessCommandService.compute_command_batch_digest(
            {
                "command_id": row.command_id,
                "command_type": row.command_type.value,
                "request_digest": row.request_digest,
                "request_json": row.request_json,
            }
            for row in commands
        )
        if batch_digest != decision.command_batch_digest:
            raise GovernanceConflictError("command batch digest changed")
        if sum(item.command_type.value == "request_replan" for item in validated) > 1:
            raise ValueError("a Planner batch may request only one replan")
        if set(rows) != {item.command_id for item in validated}:
            raise GovernanceConflictError(
                "persisted command set differs from Planner decision"
            )
        for item in validated:
            row = rows[item.command_id]
            _, request_digest = self.repository.canonical_payload(item.request)
            _, stored_digest = self.repository.canonical_payload(row.request_json)
            if (
                row.status is not ProjectProcessCommandStatus.PENDING
                or stored_digest != request_digest
                or row.request_digest != request_digest
                or row.command_type != item.command_type
                or row.process_id != process.process_id
                or row.project_id != process.project_id
                or row.based_on_process_version != process.version
                or row.based_on_event_sequence != process.last_event_sequence
                or row.graph_snapshot_digest != graph.digest
            ):
                raise GovernanceConflictError("persisted command binding changed")
            if (
                item.command_type.value == "propose_task"
                and "contract" not in item.request
            ):
                raise ValueError(
                    "automatic task creation requires a complete proposed execution contract"
                )
        return tuple(rows[item.command_id] for item in validated)

    def _task(self, connection, process, intent, command):
        request = command.request_json
        contract = request["contract"]
        if request["team_id"] == intent["owner_team_id"]:
            raise GovernanceConflictError("TeamTask requires a distinct receiving team")
        if connection.execute(
            select(TEAM_TASKS.c.task_id).where(
                TEAM_TASKS.c.task_id == request["task_id"]
            )
        ).first():
            raise GovernanceConflictError("proposed task ID is already used")
        for item in contract["input_manifest"]["resources"]:
            resource = (
                connection.execute(
                    select(PROJECT_RESOURCES)
                    .where(
                        PROJECT_RESOURCES.c.resource_id == item["resource_id"],
                        PROJECT_RESOURCES.c.project_id == process.project_id,
                    )
                    .with_for_update()
                )
                .mappings()
                .one_or_none()
            )
            if (
                resource is None
                or resource["propagation"] != item["mode"]
                or (
                    item["mode"] == "team_private"
                    and resource["owner_team_id"] != request["team_id"]
                )
            ):
                raise GovernanceConflictError(
                    "Planner task input is not shared with its target team"
                )
        now = self.clock()
        node_id = "node:task:" + request["task_id"]
        if len(node_id) > 128:
            raise ValueError("generated task node ID exceeds the storage contract")
        nodes = self.graph.snapshot(connection, project_id=process.project_id).nodes
        known_nodes = {node.node_id for node in nodes}
        if not set(contract["input_manifest"]["work_nodes"]).issubset(known_nodes):
            raise GovernanceConflictError(
                "task input work nodes must belong to this project"
            )
        connection.execute(
            TEAM_TASKS.insert().values(
                task_id=request["task_id"],
                project_id=process.project_id,
                source_team_id=intent["owner_team_id"],
                target_team_id=request["team_id"],
                created_by=None,
                produced_by_principal_id=PRINCIPAL,
                source_planner_run_id=intent["run_id"],
                source_planner_command_id=command.command_id,
                title=request["title"],
                description=request["description"],
                acceptance_criteria="Satisfy the proposed output contract and every required verification criterion.",
                status="proposed",
                assigned_account_id=None,
                artifact_resource_ids="[]",
                review_note="",
                priority="normal",
                schedule_version=1,
                created_at=now,
                updated_at=now,
                process_id=process.process_id,
                work_node_id=node_id,
                source_decision_id=command.decision_id,
                requested_capability=contract["requested_capability"],
                input_manifest_json=contract["input_manifest"],
                output_contract_json=contract["output_contract"],
                verification_policy_json=contract["verification_policy"],
                autonomy_requirement=contract["autonomy_requirement"],
                source_contract_version=1,
                accepted_contract_version=None,
            )
        )
        self.graph.register_node(
            connection,
            values={
                "node_id": node_id,
                "project_id": process.project_id,
                "node_type": "task",
                "subject_id": request["task_id"],
                "created_at": now,
            },
        )
        return request["task_id"]

    def _effect(self, connection, process, intent, command, task_result):
        from dataclasses import replace

        from .commands import ProjectProcessCommandType

        kind, request = command.command_type.value, command.request_json
        if kind == "propose_task":
            for target in request["dependencies"]:
                dependency = replace(
                    command,
                    command_id=identity(
                        "dependency", command.command_id + ":" + target
                    ),
                    command_type=ProjectProcessCommandType.PROPOSE_DEPENDENCY,
                    request_json={"source_id": request["task_id"], "target_id": target},
                )
                self.graph_effects.apply(
                    connection,
                    command=dependency,
                    process=process,
                    source_run_id=intent["run_id"],
                    now=self.clock(),
                )
            return task_result
        if kind in {"propose_dependency", "propose_risk", "propose_decision"}:
            return self.graph_effects.apply(
                connection,
                command=command,
                process=process,
                source_run_id=intent["run_id"],
                now=self.clock(),
            )
        if kind == "request_replan":
            return process.process_id
        current = self.repository.process(connection, process.process_id)
        human = HumanGateService(
            self.repository.using_connection(connection), clock=self.clock
        )
        control_id = identity("planner-control", command.command_id)
        common = {
            "process_id": process.process_id,
            "created_by": PRINCIPAL,
            "event_id": identity("event", command.command_id),
            "expected_process_version": current.version,
            "expected_event_sequence": current.last_event_sequence,
            "correlation_id": command.command_id,
        }
        if kind in {"request_human_input", "request_rework"}:
            rework = kind == "request_rework"
            human.create_input_request(
                request_id=control_id,
                work_node_id=None,
                requested_by_run_id=intent["run_id"],
                requested_by_agent_id=PRINCIPAL,
                question=(
                    "Review the requested task rework: " + request["reason"]
                    if rework
                    else request["question"]
                ),
                input_schema=(
                    {
                        "type": "object",
                        "properties": {"decision": {"enum": ["REWORK", "KEEP"]}},
                        "required": ["decision"],
                        "additionalProperties": False,
                    }
                    if rework
                    else request["input_schema"]
                ),
                context_projection={
                    "command_id": command.command_id,
                    "request": request,
                },
                **common,
            )
            return control_id
        if kind == "request_human_gate":
            human.create_gate(
                gate_id=control_id,
                gate_type=request["gate_type"],
                subject_type="work_node",
                subject_id=request["subject_id"],
                required_roles=("owner", "admin", "lead"),
                allowed_decisions=tuple(request["allowed_decisions"]),
                reason=request["reason"],
                **common,
            )
            return control_id
        raise ValueError("unsupported Planner effect")

    def _budget_gate(self, connection, process, decision_id):
        if process.status.value == "BLOCKED" or (
            process.status.value == "WAITING"
            and process.wait_reason.value not in {"HUMAN_INPUT", "HUMAN_APPROVAL"}
        ):
            # Preserve the existing blocker. Trying an illegal Human transition
            # here would roll back and poison the oldest pending batch forever.
            ProjectProcessService(
                self.repository.using_connection(connection), clock=self.clock
            ).append_fact(
                process_id=process.process_id,
                event_id=identity("budget-event", decision_id),
                event_type="project.budget.exhausted",
                expected_version=process.version,
                expected_event_sequence=process.last_event_sequence,
                subject_type="project_execution_policy",
                subject_id=process.execution_policy_id,
                initiated_by=PRINCIPAL,
                executed_as=PRINCIPAL,
                correlation_id=decision_id,
                payload={
                    "decision_id": decision_id,
                    "reason": "generation_budget_exhausted",
                    "gate_deferred": True,
                    "existing_wait_reason": process.wait_reason.value,
                },
            )
            return False
        HumanGateService(
            self.repository.using_connection(connection), clock=self.clock
        ).create_gate(
            gate_id=identity("planner-budget", decision_id),
            process_id=process.process_id,
            gate_type="BUDGET",
            subject_type="project_execution_policy",
            subject_id=process.execution_policy_id,
            required_roles=("owner", "admin", "lead"),
            allowed_decisions=PROJECT_GATE_DECISIONS[ProjectGateType.BUDGET],
            reason="Planner generation budget exhausted",
            created_by=PRINCIPAL,
            event_id=identity("budget-event", decision_id),
            expected_process_version=process.version,
            expected_event_sequence=process.last_event_sequence,
            correlation_id=decision_id,
            cause_event_type="project.budget.exhausted",
            cause_subject_type="project_execution_policy",
            cause_subject_id=process.execution_policy_id,
            cause_payload={
                "decision_id": decision_id,
                "reason": "generation_budget_exhausted",
            },
        )
        return True

    def _finish(self, connection, decision, commands, status, reason, results=None):
        results = results or {}
        for command in commands:
            connection.execute(
                PROJECT_PROCESS_COMMANDS.update()
                .where(
                    PROJECT_PROCESS_COMMANDS.c.command_id == command.command_id,
                    PROJECT_PROCESS_COMMANDS.c.status == "PENDING",
                )
                .values(
                    status=status,
                    result_subject_id=results.get(command.command_id),
                    applied_at=self.clock(),
                )
            )
        changed = connection.execute(
            PROJECT_ORCHESTRATION_DECISIONS.update()
            .where(
                PROJECT_ORCHESTRATION_DECISIONS.c.decision_id == decision.decision_id,
                PROJECT_ORCHESTRATION_DECISIONS.c.status == "PENDING",
            )
            .values(status=status, applied_at=self.clock())
        ).rowcount
        if changed != 1:
            raise GovernanceConflictError("Planner batch changed concurrently")
        process = self.repository.process(connection, decision.process_id)
        # Terminal processes stay immutable, including their event cursor.
        if process.phase.value != "TERMINAL":
            ProjectProcessService(
                self.repository.using_connection(connection), clock=self.clock
            ).append_fact(
                process_id=process.process_id,
                event_id=identity("planner-consumed", decision.decision_id),
                event_type=(
                    "project.orchestrator.decision_stale"
                    if status == "STALE"
                    else "project.orchestrator.commands_consumed"
                ),
                expected_version=process.version,
                expected_event_sequence=process.last_event_sequence,
                subject_type="orchestration_decision",
                subject_id=decision.decision_id,
                initiated_by=PRINCIPAL,
                executed_as=PRINCIPAL,
                correlation_id=decision.decision_id,
                payload={
                    "decision_id": decision.decision_id,
                    "status": status,
                    "reason": reason,
                    "results": results,
                },
            )

    @staticmethod
    def _outcome(decision_id, status):
        return ProjectOrchestratorWorkerOutcome(
            ProjectOrchestratorWorkerStatus.STALE
            if status == "STALE"
            else ProjectOrchestratorWorkerStatus.APPLIED,
            decision_id=decision_id,
        )
