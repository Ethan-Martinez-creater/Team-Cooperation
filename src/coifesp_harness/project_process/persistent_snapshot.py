"""Production, transaction-bound Project Orchestration snapshots.

The deterministic orchestrator consumes a pure ProjectOrchestrationSnapshot.
This adapter is the persistence boundary that builds that value from one fenced
database transaction. It deliberately has no mutation path: reservations,
dispatches, Agent Runs and process events belong to the effect side of the
orchestrator.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.engine import Connection

from ..agent_runs.models import TERMINAL_RUN_STATES
from ..agent_runs.repository import AGENT_RUNS, SQLAlchemyAgentRunRepository
from ..capabilities.repository import CAPABILITY_CAPACITY
from ..errors import GovernanceConflictError, PolicyDenied, ResourceNotFound
from ..product.repository import PROJECT_AGENT_RUNS, TEAM_PROJECT_AGENTS, TEAM_TASKS
from ..product.service import TeamCollaborationService
from ..team_agents.dispatcher import TaskDispatchFacts
from ..team_agents.identity import project_orchestrator_principal
from ..team_agents.task_contracts import PersistentTaskDispatchFactLoader
from ..verification.project_evidence import load_project_verification_evidence
from ..verification.repository import AGENT_REVIEWS
from ..work_graph.models import ProjectGraphSnapshot, WorkNodeType
from .budget import ProjectExecutionPolicy, ProjectExecutionUsage
from .capability_adapter import ProjectCapabilityRequirement
from .models import ProjectProcess
from .readiness import (
    ActiveOperationSnapshot,
    CapabilityReadinessSnapshot,
    ContractReadinessSnapshot,
    ProcessReadinessSnapshot,
    ProjectExecutionReadinessSnapshot,
    TeamReadinessSnapshot,
)
from .readiness_adapter import ProjectReadinessAdapter, TaskReadinessFacts
from .repository import (
    PROJECT_EXECUTION_POLICIES,
    PROJECT_EXECUTION_RESERVATIONS,
    PROJECT_GATES,
    PROJECT_INPUT_REQUESTS,
    PROJECT_PLANNER_INTENTS,
    PROJECT_PROCESSES,
    SQLAlchemyProjectProcessRepository,
)
from .runner import ProjectOrchestrationSnapshot as RunnerSnapshot

_DISPATCHABLE_STATUSES = frozenset({"accepted", "changes_requested"})
_ACTIVE_PROJECT_RUN_KINDS = frozenset(
    {"planning", "task_execution", "verification", "replanning", "specialist"}
)
_TERMINAL_AGENT_RUN_STATUSES = frozenset(item.value for item in TERMINAL_RUN_STATES)


class PersistentProjectOrchestrationSnapshotLoader:
    """Load one immutable orchestration view from one database transaction.

    The fact_loader is injectable for wiring and migration tests, but the
    default is the persistent accepted-contract loader. No supplied loader is
    allowed to turn an absent contract, capability, capacity or input into
    readiness; failures while resolving a dispatchable task are represented by
    missing readiness facts and therefore remain blocked by the pure adapter.
    """

    def __init__(
        self,
        *,
        repository: SQLAlchemyProjectProcessRepository,
        work_graph_repository,
        capability_adapter,
        fact_loader=None,
        artifact_content=None,
        clock: Callable[[], datetime] | None = None,
        readiness_adapter: ProjectReadinessAdapter | None = None,
    ) -> None:
        for name, value in (
            ("repository", repository),
            ("work_graph_repository", work_graph_repository),
            ("capability_adapter", capability_adapter),
        ):
            if value is None:
                raise TypeError(f"{name} is required")
        engine = getattr(repository, "engine", None)
        if engine is None:
            raise TypeError("repository must expose its SQLAlchemy engine")
        for name, value in (
            ("work_graph_repository", work_graph_repository),
            ("capability_adapter", capability_adapter),
        ):
            other_engine = getattr(value, "engine", None)
            if name == "capability_adapter":
                other_engine = getattr(getattr(value, "repository", None), "engine", None)
            if other_engine is not engine:
                raise ValueError("snapshot loader dependencies must share one database engine")

        if fact_loader is None:
            fact_loader = PersistentTaskDispatchFactLoader(
                engine=engine,
                artifact_content=artifact_content,
            )
        loader_engine = getattr(fact_loader, "engine", None)
        if loader_engine is not None and loader_engine is not engine:
            raise ValueError("fact loader must use the same database engine")

        self.repository = repository
        self.work_graph_repository = work_graph_repository
        self.capability_adapter = capability_adapter
        self.fact_loader = fact_loader
        self.integration_available = artifact_content is not None
        self.readiness_adapter = readiness_adapter or ProjectReadinessAdapter()
        self.clock = clock or (lambda: datetime.now(UTC))

    def __call__(self, process: ProjectProcess) -> RunnerSnapshot:
        if not isinstance(process, ProjectProcess):
            raise TypeError("process must be a ProjectProcess")
        with self.repository.transaction() as connection:
            current = self._locked_current_process(connection, process)
            graph = self.work_graph_repository.snapshot(
                connection,
                project_id=current.project_id,
            )
            task_rows = self._task_rows(connection, current, graph)
            policy, usage = self._policy_and_usage(connection, current)
            active_operations = self._active_operations(
                connection,
                current,
                graph,
                usage=usage,
            )
            has_open_input, has_open_gate = self._open_human_controls(
                connection, current
            )
            task_facts, contracts, capabilities = self._dispatch_facts(
                connection,
                current,
                graph,
                task_rows,
            )
            teams = self._team_facts(
                connection,
                current,
                graph,
                policy=policy,
            )
            execution = self._execution_facts(
                connection,
                current,
                policy=policy,
                usage=usage,
                active_operations=active_operations,
            )
            process_readiness = None
            if has_open_input or has_open_gate:
                process_readiness = ProcessReadinessSnapshot(
                    phase=current.phase.value,
                    status=current.status.value,
                    wait_reason=current.wait_reason.value,
                    dispatch_allowed=False,
                )
            bound = self.readiness_adapter.adapt(
                graph,
                current,
                contracts=contracts,
                teams=teams,
                capabilities=capabilities,
                active_operations=active_operations,
                execution=execution,
                process_readiness=process_readiness,
                task_facts=task_facts,
            )
            evidence = load_project_verification_evidence(
                connection,
                process=current,
                graph=graph,
            )
            return RunnerSnapshot(
                graph_digest=graph.digest,
                readiness=bound.evaluation,
                has_open_input=has_open_input,
                has_open_gate=has_open_gate,
                has_active_operation=bool(active_operations),
                verification_outcome=evidence.outcome,
                # Assembly rechecks evidence in its fenced transaction. Neither
                # the snapshot nor the model may manufacture integration PASS.
                integration_available=self.integration_available and evidence.outcome == "PASSED",
                integration_outcome=None,
                delivery_outcome=None,
            )

    @staticmethod
    def _locked_current_process(connection: Connection, supplied: ProjectProcess) -> ProjectProcess:
        row = (
            connection.execute(
                select(PROJECT_PROCESSES)
                .where(PROJECT_PROCESSES.c.process_id == supplied.process_id)
                .with_for_update()
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise ResourceNotFound("project process is unavailable")
        current = SQLAlchemyProjectProcessRepository._process(row)
        if (
            current.process_id != supplied.process_id
            or current.project_id != supplied.project_id
            or current.version != supplied.version
            or current.last_event_sequence != supplied.last_event_sequence
        ):
            raise GovernanceConflictError("project process snapshot is stale")
        return current

    @staticmethod
    def _task_rows(
        connection: Connection,
        process: ProjectProcess,
        graph: ProjectGraphSnapshot,
    ) -> dict[str, dict]:
        task_nodes = tuple(
            node for node in graph.nodes if node.node_type is WorkNodeType.TASK
        )
        task_ids = tuple(sorted({node.subject_id for node in task_nodes}))
        if not task_ids:
            return {}
        rows = (
            connection.execute(
                select(TEAM_TASKS)
                .where(
                    TEAM_TASKS.c.project_id == process.project_id,
                    TEAM_TASKS.c.task_id.in_(task_ids),
                )
                .with_for_update()
            )
            .mappings()
            .all()
        )
        by_id = {row["task_id"]: dict(row) for row in rows}
        if set(by_id) != set(task_ids):
            raise GovernanceConflictError("work graph task is absent from its project")
        subjects = {
            subject["subject_id"]: subject
            for subject in graph.subjects
            if subject.get("node_type") == WorkNodeType.TASK.value
        }
        for task_id, row in by_id.items():
            subject = subjects.get(task_id, {})
            value = subject.get("value")
            if not isinstance(value, dict):
                raise GovernanceConflictError("work graph task subject is unresolved")
            if any(
                value.get(field) != row[field]
                for field in ("task_id", "source_team_id", "target_team_id", "status")
            ):
                raise GovernanceConflictError("work graph task changed during snapshot")
        return by_id

    def _dispatch_facts(
        self,
        connection: Connection,
        process: ProjectProcess,
        graph: ProjectGraphSnapshot,
        task_rows: dict[str, dict],
    ) -> tuple[
        dict[str, TaskReadinessFacts],
        tuple[ContractReadinessSnapshot, ...],
        tuple[CapabilityReadinessSnapshot, ...],
    ]:
        facts_by_task: dict[str, TaskReadinessFacts] = {}
        contracts: list[ContractReadinessSnapshot] = []
        capabilities: dict[str, CapabilityReadinessSnapshot] = {}
        for task_id in sorted(task_rows):
            row = task_rows[task_id]
            if row["status"] not in _DISPATCHABLE_STATUSES:
                continue
            if row["source_contract_version"] is None:
                continue
            task = TeamCollaborationService._task(row)
            try:
                facts = self.fact_loader(
                    connection=connection,
                    process=process,
                    task=task,
                )
            except (GovernanceConflictError, PolicyDenied, ResourceNotFound):
                # Missing/withdrawn input, an unaccepted contract, or stale
                # project binding is a durable blocked fact, never readiness.
                continue
            if not isinstance(facts, TaskDispatchFacts):
                raise TypeError("task dispatch fact loader must return TaskDispatchFacts")
            self._validate_dispatch_facts(facts, process=process, task=task)
            contract = ContractReadinessSnapshot(
                facts.contract.contract_id,
                task_id,
                facts.contract_accepted,
            )
            contracts.append(contract)
            principal = project_orchestrator_principal(
                process.project_id,
                task.source_team_id,
            )
            requirement = facts.requirement
            try:
                matches = self.capability_adapter.using_connection(connection).match(
                    principal=principal,
                    requirement=requirement,
                    limit=200,
                )
            except (GovernanceConflictError, PolicyDenied):
                # The capability directory itself is scoped by the two
                # participating teams. A rejected scope is not evidence of a
                # usable capability and stays blocked.
                matches = ()
                fallback = ()
            else:
                matches = tuple(
                    item
                    for item in matches
                    if item.capability.input_contract == facts.contract.input_contract_ref
                    and item.capability.output_contract == facts.contract.output_contract_ref
                )
                fallback = () if matches else tuple(
                    self._fallback_capabilities(
                        connection,
                        principal=principal,
                        requirement=requirement,
                        input_contract=facts.contract.input_contract_ref,
                        output_contract=facts.contract.output_contract_ref,
                    )
                )
            if matches:
                selected = matches[0]
                # A capacity pool is shared by tasks, but not by different
                # provider/version tuples. Preserve that key in the pure view
                # instead of duplicating the pool once for each task.
                capability_id = json.dumps(
                    [selected.provider_tenant_id, selected.capability_id, selected.version],
                    separators=(",", ":"),
                )
                capabilities[capability_id] = CapabilityReadinessSnapshot(
                        capability_id,
                        task.target_team_id,
                        selected.capacity.available_slots,
                        status=selected.capacity.status,
                        enabled=True,
                        valid=True,
                )
            elif fallback:
                # Diagnostic fallback is never admission. A denied match for
                # this task must neither turn ready nor poison another task's
                # legitimate match to the same underlying capability.
                capability_id = f"unavailable-capability:{task_id}"
                capabilities[capability_id] = replace(
                    fallback[0], capability_id=capability_id, valid=False,
                )
            else:
                # This marker is deliberately not a capability record. The
                # evaluator therefore emits capability_missing instead of
                # accidentally admitting a contract with no live directory
                # match.
                capability_id = f"missing-capability:{task_id}"
            facts_by_task[task_id] = TaskReadinessFacts(
                required_capabilities=(capability_id,),
                required_slots=requirement.slots,
                contract_id=facts.contract.contract_id,
                contract_required=True,
                team_available=True,
            )
        return facts_by_task, tuple(contracts), tuple(capabilities[key] for key in sorted(capabilities))

    @staticmethod
    def _validate_dispatch_facts(facts: TaskDispatchFacts, *, process, task) -> None:
        contract = facts.contract
        requirement = facts.requirement
        if (
            contract.task_id != task.task_id
            or contract.project_id != process.project_id
            or contract.target_team_id != task.target_team_id
            or requirement.project_id != process.project_id
            or requirement.consumer_team_id != task.source_team_id
            or requirement.target_team_id != task.target_team_id
            or set(requirement.tags) != set(contract.required_capability_tags)
        ):
            raise GovernanceConflictError("task dispatch facts are bound to another project or task")

    def _fallback_capabilities(
        self,
        connection: Connection,
        *,
        principal,
        requirement: ProjectCapabilityRequirement,
        input_contract: str,
        output_contract: str,
    ) -> tuple[CapabilityReadinessSnapshot, ...]:
        """Project real directory records into unavailable capability facts."""
        repository = self.capability_adapter.repository
        try:
            records = repository.list(connection, limit=200)
            capacities = repository.capacities(connection)
            visibility = {
                (row["provider_tenant_id"], row["capability_id"], row["version"]): tuple(
                    row["visible_to_tenants"]
                )
                for row in connection.execute(select(CAPABILITY_CAPACITY)).mappings()
            }
        except (GovernanceConflictError, PolicyDenied, ResourceNotFound):
            return ()
        now = self._now()
        for capability in records:
            if (
                capability.provider_tenant_id != requirement.target_team_id
                or requirement.consumer_team_id not in capability.visible_to_tenants
                or requirement.protocol not in capability.protocols
                or not set(requirement.tags).issubset(capability.tags)
                or capability.input_contract != input_contract
                or capability.output_contract != output_contract
                or requirement.input_classification > capability.max_input_classification
                or not set(capability.required_compartments).issubset(requirement.compartments)
                or (
                    requirement.residency
                    and not set(requirement.residency).intersection(capability.residency_regions)
                )
            ):
                continue
            key = (
                capability.provider_tenant_id,
                capability.capability_id,
                capability.version,
            )
            capacity = capacities.get(key)
            if capacity is None:
                return (
                    CapabilityReadinessSnapshot(
                        capability.capability_id,
                        requirement.target_team_id,
                        0,
                        status="unavailable",
                        valid=False,
                    ),
                )
            valid_until = self._aware(capacity.valid_until)
            visible = requirement.consumer_team_id in visibility.get(key, ())
            available_status = capacity.status in {"available", "limited"}
            return (
                CapabilityReadinessSnapshot(
                    capability.capability_id,
                    requirement.target_team_id,
                    max(0, int(capacity.available_slots)),
                    status=capacity.status,
                    enabled=visible,
                    valid=visible and available_status and valid_until > now,
                ),
            )
        return ()

    def _team_facts(
        self,
        connection: Connection,
        process: ProjectProcess,
        graph: ProjectGraphSnapshot,
        *,
        policy: ProjectExecutionPolicy | None,
    ) -> tuple[TeamReadinessSnapshot, ...]:
        task_teams = set()
        for node in graph.nodes:
            if node.node_type is not WorkNodeType.TASK:
                continue
            for subject in graph.subjects:
                if (
                    subject.get("node_type") == WorkNodeType.TASK.value
                    and subject.get("subject_id") == node.subject_id
                    and isinstance(subject.get("value"), dict)
                ):
                    target = subject["value"].get("target_team_id")
                    if isinstance(target, str) and target:
                        task_teams.add(target)
                    break
        task_teams = tuple(sorted(task_teams))
        if not task_teams:
            return ()
        active = set(
            connection.execute(
                select(TEAM_PROJECT_AGENTS.c.team_id).where(
                    TEAM_PROJECT_AGENTS.c.project_id == process.project_id,
                    TEAM_PROJECT_AGENTS.c.team_id.in_(task_teams),
                    TEAM_PROJECT_AGENTS.c.status == "active",
                )
            ).scalars()
        )
        max_active = policy.max_active_runs_per_team if policy is not None else None
        return tuple(
            TeamReadinessSnapshot(
                team_id,
                available=team_id in active,
                max_active_operations=max_active,
            )
            for team_id in task_teams
        )

    def _policy_and_usage(
        self,
        connection: Connection,
        process: ProjectProcess,
    ) -> tuple[ProjectExecutionPolicy | None, ProjectExecutionUsage | None]:
        row = (
            connection.execute(
                select(PROJECT_EXECUTION_POLICIES).where(
                    PROJECT_EXECUTION_POLICIES.c.policy_id == process.execution_policy_id,
                    PROJECT_EXECUTION_POLICIES.c.version == process.execution_policy_version,
                )
            )
            .mappings()
            .one_or_none()
        )
        try:
            usage = self.repository.usage(connection, process.process_id)
        except ResourceNotFound:
            usage = None
        if row is None or row["project_id"] != process.project_id:
            return None, usage
        if usage is not None and usage.project_id != process.project_id:
            return None, None
        try:
            policy = ProjectExecutionPolicy(
                row["policy_id"],
                row["project_id"],
                row["max_agent_runs"],
                row["max_total_tokens"],
                row["max_model_cost_microusd"],
                row["max_replans"],
                row["max_generated_tasks"],
                row["max_active_agent_runs"],
                row["max_active_runs_per_team"],
                row["max_specialist_depth"],
                row["max_specialist_runs_per_task"],
                self._aware(row["deadline_at"]) if row["deadline_at"] is not None else None,
                row["version"],
            )
        except (TypeError, ValueError):
            policy = None
        return policy, usage

    def _active_operations(
        self,
        connection: Connection,
        process: ProjectProcess,
        graph: ProjectGraphSnapshot,
        *,
        usage: ProjectExecutionUsage | None,
    ) -> tuple[ActiveOperationSnapshot, ...]:
        task_ids = {
            node.subject_id for node in graph.nodes if node.node_type is WorkNodeType.TASK
        }
        operations: list[ActiveOperationSnapshot] = []
        seen: set[str] = set()
        bindings = (
            connection.execute(
                select(PROJECT_AGENT_RUNS)
                .where(
                    PROJECT_AGENT_RUNS.c.project_id == process.project_id,
                    PROJECT_AGENT_RUNS.c.process_id == process.process_id,
                    PROJECT_AGENT_RUNS.c.run_kind.in_(_ACTIVE_PROJECT_RUN_KINDS),
                )
                .order_by(PROJECT_AGENT_RUNS.c.run_id)
            )
            .mappings()
            .all()
        )
        for binding in bindings:
            run_id = binding["run_id"]
            tenant_id = binding["team_id"]
            # AgentRun rows are tenant protected. Set the tenant explicitly
            # for every query rather than relying on a previous binding.
            status = self._agent_run_status(connection, tenant_id, run_id)
            if status in _TERMINAL_AGENT_RUN_STATUSES:
                continue
            if run_id in seen:
                continue
            seen.add(run_id)
            work_id = binding["team_task_id"]
            if work_id not in task_ids:
                work_id = process.process_id
            operations.append(
                ActiveOperationSnapshot(
                    run_id,
                    work_id,
                    tenant_id,
                    status or "unknown",
                )
            )

        planner_intents = (
            connection.execute(
                select(PROJECT_PLANNER_INTENTS)
                .where(
                    PROJECT_PLANNER_INTENTS.c.project_id == process.project_id,
                    PROJECT_PLANNER_INTENTS.c.process_id == process.process_id,
                    PROJECT_PLANNER_INTENTS.c.status.in_(("PENDING", "RUNNING")),
                    PROJECT_PLANNER_INTENTS.c.run_id.is_not(None),
                )
                .order_by(PROJECT_PLANNER_INTENTS.c.planner_intent_id)
            )
            .mappings()
            .all()
        )
        for intent in planner_intents:
            run_id = intent["run_id"]
            tenant_id = intent["owner_team_id"]
            status = self._agent_run_status(connection, tenant_id, run_id)
            if status in _TERMINAL_AGENT_RUN_STATUSES:
                # A terminal Planner Run is no longer active; its durable
                # intent remains for reconciliation and is not a live run.
                continue
            if run_id in seen:
                continue
            seen.add(run_id)
            operations.append(
                ActiveOperationSnapshot(run_id, process.process_id, tenant_id, status or "unknown")
            )

        reviews = (
            connection.execute(
                select(AGENT_REVIEWS)
                .where(
                    AGENT_REVIEWS.c.project_id == process.project_id,
                    AGENT_REVIEWS.c.process_id == process.process_id,
                    AGENT_REVIEWS.c.status == "QUEUED",
                )
                .order_by(AGENT_REVIEWS.c.review_id)
            )
            .mappings()
            .all()
        )
        for review in reviews:
            run_id = review["run_id"]
            tenant_id = review["owner_team_id"]
            status = self._agent_run_status(connection, tenant_id, run_id)
            # QUEUED is itself an outstanding reviewer operation. If its
            # protected AgentRun row is absent or terminal, retain the review
            # operation so an incomplete reconciliation cannot look idle.
            if run_id in seen:
                continue
            seen.add(run_id)
            work_id = review["task_id"] if review["task_id"] in task_ids else process.process_id
            operations.append(
                ActiveOperationSnapshot(
                    run_id,
                    work_id,
                    tenant_id,
                    status or "review_queued",
                )
            )

        reservations = (
            connection.execute(
                select(PROJECT_EXECUTION_RESERVATIONS).where(
                    PROJECT_EXECUTION_RESERVATIONS.c.process_id == process.process_id,
                    PROJECT_EXECUTION_RESERVATIONS.c.project_id == process.project_id,
                    PROJECT_EXECUTION_RESERVATIONS.c.status == "RESERVED",
                )
            )
            .mappings()
            .all()
        )
        node_to_task = {
            node.node_id: node.subject_id
            for node in graph.nodes
            if node.node_type is WorkNodeType.TASK
        }
        for reservation in reservations:
            operation_id = reservation["agent_run_id"] or reservation["reservation_id"]
            if operation_id in seen:
                continue
            seen.add(operation_id)
            operations.append(
                ActiveOperationSnapshot(
                    operation_id,
                    node_to_task.get(reservation["work_node_id"], process.process_id),
                    reservation["team_id"],
                    "reserved",
                )
            )

        expected_active = usage.active_agent_runs if usage is not None else 0
        missing = max(0, expected_active - len(operations))
        for index in range(missing):
            operation_id = f"usage:{process.process_id}:{index}"
            operations.append(
                ActiveOperationSnapshot(operation_id, process.process_id, None, "active")
            )
        return tuple(sorted(operations, key=lambda item: (item.work_id, item.operation_id)))

    @staticmethod
    def _agent_run_status(
        connection: Connection,
        tenant_id: str,
        run_id: str,
    ) -> str | None:
        """Read one protected AgentRun after selecting its tenant explicitly."""
        SQLAlchemyAgentRunRepository._set_tenant(connection, tenant_id)
        return connection.execute(
            select(AGENT_RUNS.c.status).where(
                AGENT_RUNS.c.tenant_id == tenant_id,
                AGENT_RUNS.c.run_id == run_id,
            )
        ).scalar_one_or_none()

    def _open_human_controls(
        self,
        connection: Connection,
        process: ProjectProcess,
    ) -> tuple[bool, bool]:
        input_rows = connection.execute(
            select(PROJECT_INPUT_REQUESTS.c.project_id)
            .where(
                PROJECT_INPUT_REQUESTS.c.process_id == process.process_id,
                PROJECT_INPUT_REQUESTS.c.status == "OPEN",
            )
            .with_for_update()
        ).all()
        gate_rows = connection.execute(
            select(PROJECT_GATES.c.project_id)
            .where(
                PROJECT_GATES.c.process_id == process.process_id,
                PROJECT_GATES.c.status == "OPEN",
            )
            .with_for_update()
        ).all()
        if any(row[0] != process.project_id for row in (*input_rows, *gate_rows)):
            raise GovernanceConflictError("human control belongs to another project")
        return bool(input_rows), bool(gate_rows)

    def _execution_facts(
        self,
        connection: Connection,
        process: ProjectProcess,
        *,
        policy: ProjectExecutionPolicy | None,
        usage: ProjectExecutionUsage | None,
        active_operations: tuple[ActiveOperationSnapshot, ...],
    ) -> ProjectExecutionReadinessSnapshot:
        active_count = len(active_operations)
        max_active = policy.max_active_agent_runs if policy is not None else None
        concurrency_available = (
            policy is not None and active_count < policy.max_active_agent_runs
        )
        budget_available = False
        if policy is not None and usage is not None:
            reserved_tokens, reserved_cost = self._reserved_budget(connection, process)
            now = self._now()
            budget_available = (
                usage.agent_runs_started < policy.max_agent_runs
                and usage.replan_count < policy.max_replans
                and usage.generated_task_count < policy.max_generated_tasks
                and usage.total_tokens + reserved_tokens < policy.max_total_tokens
                and usage.model_cost_microusd + reserved_cost < policy.max_model_cost_microusd
                and (policy.deadline_at is None or now < policy.deadline_at)
            )
        return ProjectExecutionReadinessSnapshot(
            budget_available=budget_available,
            active_operations=active_count,
            max_active_operations=max_active,
            concurrency_available=concurrency_available,
        )

    @staticmethod
    def _reserved_budget(connection: Connection, process: ProjectProcess) -> tuple[int, int]:
        row = connection.execute(
            select(
                func.coalesce(func.sum(PROJECT_EXECUTION_RESERVATIONS.c.reserved_tokens), 0),
                func.coalesce(
                    func.sum(PROJECT_EXECUTION_RESERVATIONS.c.reserved_model_cost_microusd),
                    0,
                ),
            ).where(
                PROJECT_EXECUTION_RESERVATIONS.c.process_id == process.process_id,
                PROJECT_EXECUTION_RESERVATIONS.c.project_id == process.project_id,
                PROJECT_EXECUTION_RESERVATIONS.c.status == "RESERVED",
            )
        ).one()
        return int(row[0] or 0), int(row[1] or 0)

    def _now(self) -> datetime:
        value = self.clock()
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise ValueError("clock must return a timezone-aware datetime")
        return value.astimezone(UTC)

    @staticmethod
    def _aware(value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)


__all__ = ["PersistentProjectOrchestrationSnapshotLoader"]
