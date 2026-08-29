"""Pure, deterministic scheduler-readiness evaluation for Project Work Graphs.

The evaluator in this module deliberately has no repository, clock, model, or
side-effect dependency.  It consumes a caller-owned snapshot of authoritative
facts and returns a derived decision.  The result is therefore safe to use in
an orchestrator wake-up, to compare with a previous decision, or to replay
after a process restart.  It does not create an AgentRun or reserve capacity.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import StrEnum


class WorkItemStatus(StrEnum):
    """Product TeamTask states understood by the scheduler."""

    PROPOSED = "proposed"
    ACCEPTED = "accepted"
    IN_PROGRESS = "in_progress"
    SUBMITTED = "submitted"
    VERIFIED = "verified"
    CHANGES_REQUESTED = "changes_requested"
    REJECTED = "rejected"


class ReadinessBlockReason(StrEnum):
    """Stable machine-readable explanations for a non-dispatchable work item."""

    DEPENDENCY_UNSATISFIED = "dependency_unsatisfied"
    # Short aliases keep the public vocabulary convenient without introducing
    # a second meaning for any reason.
    DEPENDENCY = "dependency_unsatisfied"
    MISSING_DEPENDENCY = "missing_dependency"
    DEPENDENCY_CYCLE = "dependency_cycle"
    CONTRACT_NOT_ACCEPTED = "contract_not_accepted"
    CONTRACT_UNACCEPTED = "contract_not_accepted"
    TEAM_UNAVAILABLE = "team_unavailable"
    CAPABILITY_MISSING = "capability_missing"
    CAPABILITY_UNAVAILABLE = "capability_unavailable"
    CAPACITY_UNAVAILABLE = "capacity_unavailable"
    BUDGET_EXHAUSTED = "budget_exhausted"
    PROJECT_CONCURRENCY = "project_concurrency"
    CONCURRENCY_LIMIT = "project_concurrency"
    ACTIVE_OPERATION = "active_operation"
    TERMINAL = "terminal"
    NOT_DISPATCHABLE = "not_dispatchable"
    PROCESS_WAITING = "process_waiting"
    PROCESS_BLOCKED = "process_blocked"
    PROCESS_TERMINAL = "process_terminal"
    PROCESS_NOT_READY = "process_not_ready"


def _as_text(value: object) -> str:
    """Return enum values and strings in their canonical comparison form."""

    raw = getattr(value, "value", value)
    return str(raw)


def _normalized_status(value: object) -> str:
    return _as_text(value).strip().lower()


def _normalized_relation(value: object) -> str:
    return _as_text(value).strip().lower()


@dataclass(frozen=True, slots=True)
class WorkItemSnapshot:
    """The task facts needed for readiness.

    ``work_id`` is the stable business/work identity.  ``node_id`` is the
    optional Work Graph identity; relations may address either identity.  A
    missing ``contract_accepted`` value means that the evaluator resolves the
    value from ``ProjectReadinessSnapshot.contracts``.  The default is useful
    for local/same-team work that has no cross-team Contract.
    """

    work_id: str
    status: str | WorkItemStatus = WorkItemStatus.PROPOSED
    team_id: str = ""
    node_id: str | None = None
    required_capabilities: tuple[str, ...] = ()
    required_slots: int = 1
    contract_id: str | None = None
    contract_accepted: bool | None = True
    requester_team_id: str | None = None
    team_available: bool = True
    active_operation_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not str(self.work_id).strip():
            raise ValueError("work_id is required")
        if not str(self.team_id).strip():
            raise ValueError("team_id is required")
        if self.node_id is not None and not str(self.node_id).strip():
            raise ValueError("node_id cannot be empty")
        if self.required_slots < 1:
            raise ValueError("required_slots must be positive")
        if any(not str(item).strip() for item in self.required_capabilities):
            raise ValueError("required capability identifiers cannot be empty")
        if any(not str(item).strip() for item in self.active_operation_ids):
            raise ValueError("active operation identifiers cannot be empty")
        object.__setattr__(self, "work_id", str(self.work_id))
        object.__setattr__(self, "team_id", str(self.team_id))
        object.__setattr__(
            self,
            "required_capabilities",
            tuple(str(item) for item in self.required_capabilities),
        )
        object.__setattr__(self, "active_operation_ids", tuple(self.active_operation_ids))


# A graph-oriented name makes the relationship to WorkGraph explicit for
# callers while retaining the concise WorkItemSnapshot API.
WorkNodeSnapshot = WorkItemSnapshot


@dataclass(frozen=True, slots=True)
class WorkRelationSnapshot:
    """A Work Graph relation fact.

    Only ``depends_on`` relations affect this evaluator.  Other graph
    relations remain available to later orchestration rules and are ignored.
    """

    relation_id: str
    source_node_id: str
    target_node_id: str
    relation_type: str = "depends_on"

    def __post_init__(self) -> None:
        if not str(self.relation_id).strip():
            raise ValueError("relation_id is required")
        if not str(self.source_node_id).strip() or not str(self.target_node_id).strip():
            raise ValueError("relation endpoints are required")
        if not str(self.relation_type).strip():
            raise ValueError("relation_type is required")
        object.__setattr__(self, "relation_id", str(self.relation_id))
        object.__setattr__(self, "source_node_id", str(self.source_node_id))
        object.__setattr__(self, "target_node_id", str(self.target_node_id))

    @classmethod
    def depends_on(cls, source: str, target: str, relation_id: str | None = None):
        return cls(relation_id or f"depends:{source}:{target}", source, target)


@dataclass(frozen=True, slots=True)
class DependencySnapshot:
    """Compact dependency fact for callers that do not need other relations."""

    source_work_id: str
    prerequisite_work_id: str

    def __post_init__(self) -> None:
        if not str(self.source_work_id).strip() or not str(self.prerequisite_work_id).strip():
            raise ValueError("dependency endpoints are required")
        object.__setattr__(self, "source_work_id", str(self.source_work_id))
        object.__setattr__(self, "prerequisite_work_id", str(self.prerequisite_work_id))


@dataclass(frozen=True, slots=True)
class ContractReadinessSnapshot:
    """The acceptance fact for a work Contract."""

    contract_id: str
    work_id: str
    accepted: bool

    def __post_init__(self) -> None:
        if not str(self.contract_id).strip() or not str(self.work_id).strip():
            raise ValueError("contract and work identifiers are required")
        object.__setattr__(self, "contract_id", str(self.contract_id))
        object.__setattr__(self, "work_id", str(self.work_id))


# This alias is intentionally public: “ContractSnapshot” is the natural name
# in an orchestrator adapter.
ContractSnapshot = ContractReadinessSnapshot


@dataclass(frozen=True, slots=True)
class TeamReadinessSnapshot:
    """Explicit team availability/capacity facts used by the scheduler."""

    team_id: str
    available: bool = True
    max_active_operations: int | None = None

    def __post_init__(self) -> None:
        if not str(self.team_id).strip():
            raise ValueError("team_id is required")
        if self.max_active_operations is not None and self.max_active_operations < 0:
            raise ValueError("max_active_operations cannot be negative")
        object.__setattr__(self, "team_id", str(self.team_id))


@dataclass(frozen=True, slots=True)
class CapabilityReadinessSnapshot:
    """A point-in-time capability/capacity fact supplied by the directory.

    ``valid`` is explicit so checking remains independent of wall-clock time;
    the capability service must evaluate expiry before constructing this
    snapshot.
    """

    capability_id: str
    team_id: str
    available_slots: int
    status: str = "available"
    enabled: bool = True
    valid: bool = True

    def __post_init__(self) -> None:
        if not str(self.capability_id).strip() or not str(self.team_id).strip():
            raise ValueError("capability and team identifiers are required")
        if self.available_slots < 0:
            raise ValueError("available_slots cannot be negative")
        object.__setattr__(self, "capability_id", str(self.capability_id))
        object.__setattr__(self, "team_id", str(self.team_id))

    @property
    def available(self) -> bool:
        return (
            self.enabled
            and self.valid
            and _normalized_status(self.status) not in {"unavailable", "disabled", "offline"}
            and self.available_slots > 0
        )


CapabilitySnapshot = CapabilityReadinessSnapshot


@dataclass(frozen=True, slots=True)
class ActiveOperationSnapshot:
    """An already active operation that must not be dispatched again."""

    operation_id: str
    work_id: str
    team_id: str | None = None
    status: str = "active"

    def __post_init__(self) -> None:
        if not str(self.operation_id).strip() or not str(self.work_id).strip():
            raise ValueError("active operation identifiers are required")
        object.__setattr__(self, "operation_id", str(self.operation_id))
        object.__setattr__(self, "work_id", str(self.work_id))


@dataclass(frozen=True, slots=True)
class ProcessReadinessSnapshot:
    """Current process facts relevant to dispatch permission."""

    phase: str = "EXECUTION"
    status: str = "READY"
    wait_reason: str = "NONE"
    dispatch_allowed: bool = True


@dataclass(frozen=True, slots=True)
class ProjectExecutionReadinessSnapshot:
    """Project-level budget and concurrency facts.

    Counts and booleans are computed by the caller from the durable Project
    Process/usage/reservation records.  No implicit policy or time lookup is
    performed here.
    """

    budget_available: bool = True
    active_operations: int = 0
    max_active_operations: int | None = None
    concurrency_available: bool = True

    def __post_init__(self) -> None:
        if self.active_operations < 0:
            raise ValueError("active_operations cannot be negative")
        if self.max_active_operations is not None and self.max_active_operations < 0:
            raise ValueError("max_active_operations cannot be negative")


@dataclass(frozen=True, slots=True)
class ProjectReadinessSnapshot:
    """Immutable input to :class:`ProjectReadinessEvaluator`."""

    tasks: tuple[WorkItemSnapshot, ...] = ()
    relations: tuple[WorkRelationSnapshot, ...] = ()
    dependencies: tuple[DependencySnapshot, ...] = ()
    contracts: tuple[ContractReadinessSnapshot, ...] = ()
    teams: tuple[TeamReadinessSnapshot, ...] = ()
    capabilities: tuple[CapabilityReadinessSnapshot, ...] = ()
    active_operations: tuple[ActiveOperationSnapshot, ...] = ()
    process: ProcessReadinessSnapshot = field(default_factory=ProcessReadinessSnapshot)
    execution: ProjectExecutionReadinessSnapshot = field(
        default_factory=ProjectExecutionReadinessSnapshot
    )

    def __post_init__(self) -> None:
        for name in (
            "tasks",
            "relations",
            "dependencies",
            "contracts",
            "teams",
            "capabilities",
            "active_operations",
        ):
            object.__setattr__(self, name, tuple(getattr(self, name)))
        ids = [item.work_id for item in self.tasks]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate work_id in readiness snapshot")
        team_ids = [item.team_id for item in self.teams]
        if len(team_ids) != len(set(team_ids)):
            raise ValueError("duplicate team_id in readiness snapshot")
        capability_keys = [(item.team_id, item.capability_id) for item in self.capabilities]
        if len(capability_keys) != len(set(capability_keys)):
            raise ValueError("duplicate capability fact in readiness snapshot")
        contract_ids = [item.contract_id for item in self.contracts]
        if len(contract_ids) != len(set(contract_ids)):
            raise ValueError("duplicate contract fact in readiness snapshot")
        contract_work_ids = [item.work_id for item in self.contracts]
        if len(contract_work_ids) != len(set(contract_work_ids)):
            raise ValueError("multiple contract facts for one work item")
        operation_ids = [item.operation_id for item in self.active_operations]
        if len(operation_ids) != len(set(operation_ids)):
            raise ValueError("duplicate active operation in readiness snapshot")

    @property
    def work_items(self) -> tuple[WorkItemSnapshot, ...]:
        return self.tasks

    @property
    def project_active_operations(self) -> int:
        return self.execution.active_operations

    @property
    def budget_available(self) -> bool:
        return self.execution.budget_available


ReadinessSnapshot = ProjectReadinessSnapshot
WorkGraphReadinessSnapshot = ProjectReadinessSnapshot


@dataclass(frozen=True, slots=True)
class WorkReadiness:
    """Derived decision for one work item."""

    work: WorkItemSnapshot
    ready: bool
    reasons: tuple[ReadinessBlockReason, ...] = ()
    blocked_by: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "reasons", tuple(ReadinessBlockReason(item) for item in self.reasons))
        object.__setattr__(self, "blocked_by", tuple(str(item) for item in self.blocked_by))
        if self.ready and self.reasons:
            raise ValueError("ready work cannot have block reasons")
        if not self.ready and not self.reasons:
            raise ValueError("blocked work requires a block reason")

    @property
    def work_id(self) -> str:
        return self.work.work_id

    @property
    def node_id(self) -> str:
        return self.work.node_id or self.work.work_id

    @property
    def team_id(self) -> str:
        return self.work.team_id

    @property
    def blocked_reasons(self) -> tuple[ReadinessBlockReason, ...]:
        return self.reasons

    @property
    def blocked_reason(self) -> ReadinessBlockReason | None:
        return self.reasons[0] if self.reasons else None

    @property
    def task_id(self) -> str:
        return self.work_id


ReadinessDecision = WorkReadiness


@dataclass(frozen=True, slots=True)
class ReadinessEvaluation:
    """Complete derived scheduler view returned by the evaluator."""

    ready_work: tuple[WorkReadiness, ...]
    blocked_work: tuple[WorkReadiness, ...]
    all_work_terminal: bool
    verification_ready: bool
    dependency_cycles: tuple[tuple[str, ...], ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "ready_work", tuple(self.ready_work))
        object.__setattr__(self, "blocked_work", tuple(self.blocked_work))
        object.__setattr__(self, "dependency_cycles", tuple(tuple(item) for item in self.dependency_cycles))

    @property
    def ready(self) -> tuple[WorkReadiness, ...]:
        return self.ready_work

    @property
    def blocked(self) -> tuple[WorkReadiness, ...]:
        return self.blocked_work

    @property
    def ready_tasks(self) -> tuple[WorkReadiness, ...]:
        return self.ready_work

    @property
    def blocked_tasks(self) -> tuple[WorkReadiness, ...]:
        return self.blocked_work

    def for_work(self, work_id: str) -> WorkReadiness | None:
        for item in (*self.ready_work, *self.blocked_work):
            if item.work_id == work_id:
                return item
        return None


ReadinessResult = ReadinessEvaluation


_DISPATCHABLE_STATUSES = frozenset(
    {
        WorkItemStatus.PROPOSED.value,
        WorkItemStatus.ACCEPTED.value,
        WorkItemStatus.CHANGES_REQUESTED.value,
    }
)
_SUCCESS_STATUSES = frozenset({WorkItemStatus.VERIFIED.value, "completed", "succeeded", "done"})
_TERMINAL_STATUSES = _SUCCESS_STATUSES | {WorkItemStatus.REJECTED.value}
_PROCESS_TERMINAL_STATUSES = frozenset({"COMPLETED", "FAILED", "CANCELLED"})
_REASON_ORDER = {
    ReadinessBlockReason.PROCESS_TERMINAL: 0,
    ReadinessBlockReason.PROCESS_BLOCKED: 1,
    ReadinessBlockReason.PROCESS_WAITING: 2,
    ReadinessBlockReason.PROCESS_NOT_READY: 3,
    ReadinessBlockReason.TERMINAL: 4,
    ReadinessBlockReason.NOT_DISPATCHABLE: 5,
    ReadinessBlockReason.ACTIVE_OPERATION: 6,
    ReadinessBlockReason.MISSING_DEPENDENCY: 7,
    ReadinessBlockReason.DEPENDENCY_CYCLE: 8,
    ReadinessBlockReason.DEPENDENCY_UNSATISFIED: 9,
    ReadinessBlockReason.CONTRACT_NOT_ACCEPTED: 10,
    ReadinessBlockReason.TEAM_UNAVAILABLE: 11,
    ReadinessBlockReason.CAPABILITY_MISSING: 12,
    ReadinessBlockReason.CAPABILITY_UNAVAILABLE: 13,
    ReadinessBlockReason.CAPACITY_UNAVAILABLE: 14,
    ReadinessBlockReason.BUDGET_EXHAUSTED: 15,
    ReadinessBlockReason.PROJECT_CONCURRENCY: 16,
}


def _unique_reasons(reasons: Iterable[ReadinessBlockReason]) -> tuple[ReadinessBlockReason, ...]:
    unique = set(reasons)
    return tuple(sorted(unique, key=lambda item: (_REASON_ORDER[item], item.value)))


def _find_dependency_cycles(graph: dict[str, set[str]]) -> tuple[tuple[str, ...], ...]:
    """Return deterministic simple cycles, and no false positive for DAGs."""

    cycles: set[tuple[str, ...]] = set()
    visiting: list[str] = []
    active: set[str] = set()
    completed: set[str] = set()

    def canonical(items: list[str]) -> tuple[str, ...]:
        minimum = min(range(len(items)), key=lambda index: items[index])
        rotated = items[minimum:] + items[:minimum]
        return tuple(rotated)

    def visit(node: str) -> None:
        if node in completed:
            return
        if node in active:
            start = visiting.index(node)
            cycles.add(canonical(visiting[start:]))
            return
        active.add(node)
        visiting.append(node)
        for target in sorted(graph.get(node, ())):
            visit(target)
        visiting.pop()
        active.remove(node)
        completed.add(node)

    for node in sorted(graph):
        visit(node)
    return tuple(sorted(cycles))


class ProjectReadinessEvaluator:
    """Derive dispatch readiness from one explicit snapshot.

    Evaluation is deterministic: work is processed by ``(work_id, node_id)``
    order, and newly admitted items consume project/team/capability slots in
    that same order.  This prevents a caller from over-admitting parallel work
    merely because the input tuple was supplied in a different order.
    """

    def evaluate(self, snapshot: ProjectReadinessSnapshot) -> ReadinessEvaluation:
        if not isinstance(snapshot, ProjectReadinessSnapshot):
            raise TypeError("snapshot must be ProjectReadinessSnapshot")

        tasks = tuple(sorted(snapshot.tasks, key=lambda item: (item.work_id, item.node_id or "")))
        aliases: dict[str, str] = {}
        for task in tasks:
            for alias in (task.work_id, task.node_id):
                if alias is None:
                    continue
                prior = aliases.setdefault(alias, task.work_id)
                if prior != task.work_id:
                    raise ValueError("work and node identifiers are ambiguous")

        dependency_graph: dict[str, set[str]] = {task.work_id: set() for task in tasks}
        missing_dependencies: dict[str, set[str]] = {task.work_id: set() for task in tasks}

        for relation in snapshot.relations:
            if _normalized_relation(relation.relation_type) != "depends_on":
                continue
            source = aliases.get(relation.source_node_id)
            target = aliases.get(relation.target_node_id)
            if source is not None:
                if target is None:
                    missing_dependencies[source].add(relation.target_node_id)
                else:
                    dependency_graph[source].add(target)

        for dependency in snapshot.dependencies:
            source = aliases.get(dependency.source_work_id)
            target = aliases.get(dependency.prerequisite_work_id)
            if source is not None:
                if target is None:
                    missing_dependencies[source].add(dependency.prerequisite_work_id)
                else:
                    dependency_graph[source].add(target)

        cycles = _find_dependency_cycles(dependency_graph)
        cycle_members = {member for cycle in cycles for member in cycle}
        contracts_by_id = {item.contract_id: item for item in snapshot.contracts}
        contracts_by_work: dict[str, ContractReadinessSnapshot] = {}
        for contract in snapshot.contracts:
            contracts_by_work.setdefault(contract.work_id, contract)
        teams = {item.team_id: item for item in snapshot.teams}
        capabilities = {
            (item.team_id, item.capability_id): item for item in snapshot.capabilities
        }
        active_by_work: dict[str, tuple[ActiveOperationSnapshot, ...]] = {}
        active_by_team: dict[str, int] = {}
        for operation in snapshot.active_operations:
            active_by_work.setdefault(operation.work_id, ())
            active_by_work[operation.work_id] = (*active_by_work[operation.work_id], operation)
            if operation.team_id is not None:
                active_by_team[operation.team_id] = active_by_team.get(operation.team_id, 0) + 1
        for task in tasks:
            if task.active_operation_ids:
                active_by_work.setdefault(task.work_id, ())
                active_by_work[task.work_id] = active_by_work[task.work_id] + tuple(
                    ActiveOperationSnapshot(operation_id, task.work_id, task.team_id)
                    for operation_id in task.active_operation_ids
                )
                active_by_team[task.team_id] = active_by_team.get(task.team_id, 0) + len(
                    task.active_operation_ids
                )

        projected_project_operations = max(
            snapshot.execution.active_operations,
            len(snapshot.active_operations),
        )
        projected_team_operations = dict(active_by_team)
        projected_capacities = {
            key: item.available_slots for key, item in capabilities.items()
        }
        decisions: list[WorkReadiness] = []

        for task in tasks:
            reasons: list[ReadinessBlockReason] = []
            blocked_by: set[str] = set()
            process_status = _as_text(snapshot.process.status).upper()
            process_phase = _as_text(snapshot.process.phase).upper()
            if not snapshot.process.dispatch_allowed:
                reasons.append(ReadinessBlockReason.PROCESS_NOT_READY)
            elif process_status in _PROCESS_TERMINAL_STATUSES:
                reasons.append(ReadinessBlockReason.PROCESS_TERMINAL)
            elif process_status == "WAITING":
                reasons.append(ReadinessBlockReason.PROCESS_WAITING)
            elif process_status == "BLOCKED":
                reasons.append(ReadinessBlockReason.PROCESS_BLOCKED)
            elif process_status not in {"READY", "RUNNING"}:
                reasons.append(ReadinessBlockReason.PROCESS_NOT_READY)
            if process_phase not in {"EXECUTION"}:
                reasons.append(ReadinessBlockReason.PROCESS_NOT_READY)

            status = _normalized_status(task.status)
            if status in _TERMINAL_STATUSES:
                reasons.append(ReadinessBlockReason.TERMINAL)
            elif status not in _DISPATCHABLE_STATUSES:
                reasons.append(ReadinessBlockReason.NOT_DISPATCHABLE)

            if active_by_work.get(task.work_id):
                reasons.append(ReadinessBlockReason.ACTIVE_OPERATION)

            if missing_dependencies[task.work_id]:
                reasons.append(ReadinessBlockReason.MISSING_DEPENDENCY)
                blocked_by.update(missing_dependencies[task.work_id])
            if task.work_id in cycle_members:
                reasons.append(ReadinessBlockReason.DEPENDENCY_CYCLE)
                blocked_by.update(
                    member
                    for cycle in cycles
                    if task.work_id in cycle
                    for member in cycle
                    if member != task.work_id
                )
            for prerequisite in sorted(dependency_graph[task.work_id]):
                if task.work_id in cycle_members and prerequisite in cycle_members:
                    # The cycle itself is the more precise fail-closed reason;
                    # do not add a misleading unsatisfied-status reason for
                    # the same cyclic edge.
                    continue
                prerequisite_task = next(item for item in tasks if item.work_id == prerequisite)
                if _normalized_status(prerequisite_task.status) not in _SUCCESS_STATUSES:
                    reasons.append(ReadinessBlockReason.DEPENDENCY_UNSATISFIED)
                    blocked_by.add(prerequisite)

            contract = contracts_by_id.get(task.contract_id) if task.contract_id else None
            if contract is None:
                contract = contracts_by_work.get(task.work_id)
            contract_accepted = task.contract_accepted
            if contract is not None:
                contract_accepted = contract.accepted
            elif contract_accepted is None:
                contract_accepted = not (
                    task.requester_team_id is not None
                    and task.requester_team_id != task.team_id
                )
            if not contract_accepted:
                reasons.append(ReadinessBlockReason.CONTRACT_NOT_ACCEPTED)

            team_fact = teams.get(task.team_id)
            team_available = task.team_available and (
                team_fact.available if team_fact is not None else True
            )
            if not team_available:
                reasons.append(ReadinessBlockReason.TEAM_UNAVAILABLE)
            team_limit = team_fact.max_active_operations if team_fact is not None else None
            if team_limit is not None and projected_team_operations.get(task.team_id, 0) >= team_limit:
                reasons.append(ReadinessBlockReason.CAPACITY_UNAVAILABLE)

            for capability_id in sorted(set(task.required_capabilities)):
                capability = capabilities.get((task.team_id, capability_id))
                if capability is None:
                    reasons.append(ReadinessBlockReason.CAPABILITY_MISSING)
                    continue
                if not capability.enabled or not capability.valid or _normalized_status(
                    capability.status
                ) in {"unavailable", "disabled", "offline"}:
                    reasons.append(ReadinessBlockReason.CAPABILITY_UNAVAILABLE)
                    continue
                if projected_capacities[(task.team_id, capability_id)] < task.required_slots:
                    reasons.append(ReadinessBlockReason.CAPACITY_UNAVAILABLE)

            if not snapshot.execution.budget_available:
                reasons.append(ReadinessBlockReason.BUDGET_EXHAUSTED)
            if not snapshot.execution.concurrency_available:
                reasons.append(ReadinessBlockReason.PROJECT_CONCURRENCY)
            project_limit = snapshot.execution.max_active_operations
            if project_limit is not None and projected_project_operations >= project_limit:
                reasons.append(ReadinessBlockReason.PROJECT_CONCURRENCY)

            unique_reasons = _unique_reasons(reasons)
            if unique_reasons:
                decisions.append(
                    WorkReadiness(task, False, unique_reasons, tuple(sorted(blocked_by)))
                )
                continue

            # Admission is a derived projection only.  It is not a reservation
            # and has no database effect; a real orchestrator still reserves
            # slots atomically before dispatch.
            projected_project_operations += 1
            projected_team_operations[task.team_id] = (
                projected_team_operations.get(task.team_id, 0) + 1
            )
            for capability_id in set(task.required_capabilities):
                key = (task.team_id, capability_id)
                projected_capacities[key] -= task.required_slots
            decisions.append(WorkReadiness(task, True))

        ready = tuple(item for item in decisions if item.ready)
        blocked = tuple(item for item in decisions if not item.ready)
        statuses = {_normalized_status(task.status) for task in tasks}
        all_terminal = bool(tasks) and statuses.issubset(_TERMINAL_STATUSES)
        verification_ready = (
            bool(tasks)
            and statuses.issubset(_SUCCESS_STATUSES)
            and all_terminal
            and not cycles
            and not any(missing_dependencies.values())
            and not any(active_by_work.values())
        )
        return ReadinessEvaluation(
            ready_work=ready,
            blocked_work=blocked,
            all_work_terminal=all_terminal,
            verification_ready=verification_ready,
            dependency_cycles=cycles,
        )


ReadinessEvaluator = ProjectReadinessEvaluator


def evaluate_readiness(snapshot: ProjectReadinessSnapshot) -> ReadinessEvaluation:
    """Functional convenience wrapper around :class:`ProjectReadinessEvaluator`."""

    return ProjectReadinessEvaluator().evaluate(snapshot)


def derive_readiness(snapshot: ProjectReadinessSnapshot) -> ReadinessEvaluation:
    """Backward-compatible descriptive alias for ``evaluate_readiness``."""

    return evaluate_readiness(snapshot)


__all__ = [
    "ActiveOperationSnapshot",
    "CapabilityReadinessSnapshot",
    "CapabilitySnapshot",
    "ContractReadinessSnapshot",
    "ContractSnapshot",
    "DependencySnapshot",
    "ProcessReadinessSnapshot",
    "ProjectExecutionReadinessSnapshot",
    "ProjectReadinessEvaluator",
    "ProjectReadinessSnapshot",
    "ReadinessBlockReason",
    "ReadinessDecision",
    "ReadinessEvaluation",
    "ReadinessEvaluator",
    "ReadinessResult",
    "ReadinessSnapshot",
    "TeamReadinessSnapshot",
    "WorkGraphReadinessSnapshot",
    "WorkItemSnapshot",
    "WorkItemStatus",
    "WorkNodeSnapshot",
    "WorkReadiness",
    "WorkRelationSnapshot",
    "derive_readiness",
    "evaluate_readiness",
]
