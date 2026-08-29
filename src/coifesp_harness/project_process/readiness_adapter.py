"""Pure binding from the Work Graph to the Project readiness evaluator.

The Work Graph is the authoritative source for task identity, ownership and
dependency edges.  It intentionally does *not* contain the derived facts that
are needed to decide whether a task may be dispatched.  This module joins the
two contracts without making those facts up from task prose: callers provide
contracts, team/capability facts, active operations and execution limits
explicitly, and the adapter only projects them into the existing pure
``ProjectReadinessEvaluator``.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace

from ..work_graph.models import ProjectGraphSnapshot, WorkNodeType, WorkRelationType
from .models import (
    ProjectProcess,
    ProjectProcessPhase,
    ProjectProcessStatus,
    ProjectProcessWaitReason,
    validate_process_state,
)
from .readiness import (
    ActiveOperationSnapshot,
    CapabilityReadinessSnapshot,
    ContractReadinessSnapshot,
    ProcessReadinessSnapshot,
    ProjectExecutionReadinessSnapshot,
    ProjectReadinessEvaluator,
    ProjectReadinessSnapshot,
    ReadinessEvaluation,
    TeamReadinessSnapshot,
    WorkItemSnapshot,
    WorkItemStatus,
    WorkRelationSnapshot,
)


def _text(value: object) -> str:
    """Return a stable enum/string value without consulting free-form text."""

    raw = getattr(value, "value", value)
    return str(raw)


def _required_text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} is required")
    return value


@dataclass(frozen=True, slots=True)
class TaskReadinessFacts:
    """Explicit readiness inputs for one graph task.

    These facts are deliberately separate from the graph subject.  In
    particular, ``required_capabilities`` and ``contract_required`` are never
    inferred from ``title``, ``description`` or ``acceptance_criteria``.  A
    same-team/internal task can opt out of the contract requirement only by
    setting ``contract_required=False`` (or by naming it in
    ``internal_opt_out_task_ids`` when binding a graph).
    """

    required_capabilities: tuple[str, ...] = ()
    required_slots: int = 1
    contract_id: str | None = None
    contract_required: bool = True
    team_available: bool = True
    active_operation_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if isinstance(self.required_capabilities, str):
            raise TypeError("required_capabilities must be an iterable of identifiers")
        capabilities = tuple(self.required_capabilities)
        if any(not isinstance(item, str) or not item.strip() for item in capabilities):
            raise ValueError("required capability identifiers cannot be empty")
        if len(set(capabilities)) != len(capabilities):
            raise ValueError("required capability identifiers must be unique")
        if isinstance(self.active_operation_ids, str):
            raise TypeError("active_operation_ids must be an iterable of identifiers")
        operations = tuple(self.active_operation_ids)
        if any(not isinstance(item, str) or not item.strip() for item in operations):
            raise ValueError("active operation identifiers cannot be empty")
        if len(set(operations)) != len(operations):
            raise ValueError("active operation identifiers must be unique")
        if isinstance(self.required_slots, bool) or not isinstance(self.required_slots, int):
            raise TypeError("required_slots must be an integer")
        if self.required_slots < 1:
            raise ValueError("required_slots must be positive")
        if self.contract_id is not None:
            _required_text(self.contract_id, "contract_id")
        if not isinstance(self.contract_required, bool):
            raise TypeError("contract_required must be boolean")
        if not isinstance(self.team_available, bool):
            raise TypeError("team_available must be boolean")
        object.__setattr__(self, "required_capabilities", capabilities)
        object.__setattr__(self, "active_operation_ids", operations)


# A descriptive alias for callers that model a task's requirements separately.
TaskReadinessSnapshot = TaskReadinessFacts


@dataclass(frozen=True, slots=True)
class BoundReadinessSnapshot:
    """A deterministic readiness input and its evaluation bound to graph data."""

    graph_digest: str
    snapshot: ProjectReadinessSnapshot
    evaluation: ReadinessEvaluation


def _as_tuple(values: Iterable[object] | None, expected: type, name: str) -> tuple:
    if values is None:
        return ()
    if isinstance(values, (str, bytes)):
        raise TypeError(f"{name} must be an iterable of {expected.__name__}")
    result = tuple(values)
    if any(not isinstance(item, expected) for item in result):
        raise TypeError(f"{name} must contain only {expected.__name__} values")
    return result


def _canonical_process_readiness(
    process: ProjectProcess,
    supplied: ProcessReadinessSnapshot | None,
) -> ProcessReadinessSnapshot:
    try:
        phase = ProjectProcessPhase(process.phase)
        status = ProjectProcessStatus(process.status)
        wait_reason = ProjectProcessWaitReason(process.wait_reason)
    except (TypeError, ValueError) as exc:
        raise ValueError("process contains an invalid phase, status or wait reason") from exc
    try:
        validate_process_state(phase, status, wait_reason)
    except ValueError as exc:
        raise ValueError("process state is invalid") from exc

    derived = ProcessReadinessSnapshot(
        phase=phase.value,
        status=status.value,
        wait_reason=wait_reason.value,
        dispatch_allowed=(
            phase is ProjectProcessPhase.EXECUTION
            and status in {ProjectProcessStatus.READY, ProjectProcessStatus.RUNNING}
        ),
    )
    if supplied is None:
        return derived
    if not isinstance(supplied, ProcessReadinessSnapshot):
        raise TypeError("process_readiness must be ProcessReadinessSnapshot")
    if (
        _text(supplied.phase).upper() != derived.phase
        or _text(supplied.status).upper() != derived.status
        or _text(supplied.wait_reason).upper() != derived.wait_reason
    ):
        raise ValueError("process_readiness does not match ProjectProcess")
    return supplied


def _subject_index(graph: ProjectGraphSnapshot) -> dict[tuple[str, str], Mapping[str, object]]:
    subjects: dict[tuple[str, str], Mapping[str, object]] = {}
    for raw_subject in graph.subjects:
        if not isinstance(raw_subject, Mapping):
            raise TypeError("graph subject must be an object")
        try:
            subject_type = _text(raw_subject["node_type"])
            subject_id = _required_text(raw_subject["subject_id"], "subject.subject_id")
        except KeyError as exc:
            raise ValueError("graph subject is missing node_type or subject_id") from exc
        key = (subject_type, subject_id)
        if key in subjects:
            raise ValueError("duplicate graph subject")
        subjects[key] = raw_subject
    return subjects


def _graph_tasks(graph: ProjectGraphSnapshot) -> tuple[WorkItemSnapshot, ...]:
    subject_index = _subject_index(graph)
    node_ids: set[str] = set()
    task_subject_ids: set[str] = set()
    tasks: list[WorkItemSnapshot] = []

    for node in graph.nodes:
        node_id = _required_text(node.node_id, "graph node.node_id")
        project_id = _required_text(node.project_id, "graph node.project_id")
        if project_id != graph.project_id:
            raise ValueError("graph node belongs to another project")
        if node_id in node_ids:
            raise ValueError("duplicate graph node_id")
        node_ids.add(node_id)
        try:
            node_type = WorkNodeType(node.node_type)
        except (TypeError, ValueError) as exc:
            raise ValueError("graph node has an invalid node_type") from exc
        if node_type is not WorkNodeType.TASK:
            continue

        subject_id = _required_text(node.subject_id, "task node.subject_id")
        if subject_id in task_subject_ids:
            raise ValueError("duplicate task subject")
        task_subject_ids.add(subject_id)
        subject = subject_index.get((WorkNodeType.TASK.value, subject_id))
        if subject is None:
            raise ValueError("task subject is unresolved")
        if subject.get("unresolved"):
            raise ValueError("task subject is unresolved")
        value = subject.get("value")
        if not isinstance(value, Mapping):
            raise TypeError("task subject value must be an object")
        for field_name in ("task_id", "source_team_id", "target_team_id", "status"):
            if field_name not in value:
                raise ValueError(f"task subject is missing {field_name}")
            _required_text(value[field_name], f"task.{field_name}")
        if value["task_id"] != subject_id:
            raise ValueError("task subject task_id does not match its graph node")
        if "project_id" in value and value["project_id"] != graph.project_id:
            raise ValueError("task subject belongs to another project")
        try:
            status = WorkItemStatus(value["status"])
        except (TypeError, ValueError) as exc:
            raise ValueError("task subject has an invalid status") from exc

        # Only durable TeamTask identity/ownership/status fields are consumed.
        # Free-form title, description and acceptance criteria are intentionally
        # not read here; the caller supplies any additional readiness facts.
        tasks.append(
            WorkItemSnapshot(
                work_id=value["task_id"],
                status=status,
                team_id=value["target_team_id"],
                node_id=node_id,
                requester_team_id=value["source_team_id"],
            )
        )

    return tuple(sorted(tasks, key=lambda item: (item.work_id, item.node_id or "")))


def _graph_relations(graph: ProjectGraphSnapshot) -> tuple[WorkRelationSnapshot, ...]:
    node_ids = {
        _required_text(node.node_id, "graph node.node_id")
        for node in graph.nodes
    }
    relations: list[WorkRelationSnapshot] = []
    for relation in graph.relations:
        relation_project = _required_text(relation.project_id, "graph relation.project_id")
        if relation_project != graph.project_id:
            raise ValueError("graph relation belongs to another project")
        relation_id = _required_text(relation.relation_id, "graph relation.relation_id")
        source = _required_text(relation.source_node_id, "graph relation.source_node_id")
        target = _required_text(relation.target_node_id, "graph relation.target_node_id")
        if source not in node_ids or target not in node_ids:
            raise ValueError("graph relation references a missing node")
        try:
            relation_type = WorkRelationType(relation.relation_type)
        except (TypeError, ValueError) as exc:
            raise ValueError("graph relation has an invalid relation_type") from exc
        if relation_type is WorkRelationType.DEPENDS_ON:
            relations.append(
                WorkRelationSnapshot(
                    relation_id=relation_id,
                    source_node_id=source,
                    target_node_id=target,
                    relation_type=relation_type.value,
                )
            )
    return tuple(
        sorted(
            relations,
            key=lambda item: (
                item.source_node_id,
                item.target_node_id,
                item.relation_id,
            ),
        )
    )


def _task_facts(
    task_ids: set[str],
    supplied: Mapping[str, TaskReadinessFacts] | None,
    internal_opt_out_task_ids: Iterable[str],
) -> dict[str, TaskReadinessFacts]:
    if supplied is None:
        supplied = {}
    if not isinstance(supplied, Mapping):
        raise TypeError("task_facts must be a mapping keyed by task_id")
    facts: dict[str, TaskReadinessFacts] = {}
    for task_id, fact in supplied.items():
        _required_text(task_id, "task_facts key")
        if task_id not in task_ids:
            raise ValueError("task_facts contains an unknown task_id")
        if not isinstance(fact, TaskReadinessFacts):
            raise TypeError("task_facts values must be TaskReadinessFacts")
        facts[task_id] = fact
    if isinstance(internal_opt_out_task_ids, (str, bytes)):
        raise TypeError("internal_opt_out_task_ids must be an iterable of task ids")
    opt_outs = tuple(internal_opt_out_task_ids)
    if any(not isinstance(task_id, str) or not task_id.strip() for task_id in opt_outs):
        raise ValueError("internal opt-out task ids cannot be empty")
    if any(task_id not in task_ids for task_id in opt_outs):
        raise ValueError("internal opt-out contains an unknown task_id")
    for task_id in opt_outs:
        facts[task_id] = replace(facts.get(task_id, TaskReadinessFacts()), contract_required=False)
    return facts


def _sorted_facts(
    contracts: Iterable[ContractReadinessSnapshot] | None,
    teams: Iterable[TeamReadinessSnapshot] | None,
    capabilities: Iterable[CapabilityReadinessSnapshot] | None,
    active_operations: Iterable[ActiveOperationSnapshot] | None,
) -> tuple[
    tuple[ContractReadinessSnapshot, ...],
    tuple[TeamReadinessSnapshot, ...],
    tuple[CapabilityReadinessSnapshot, ...],
    tuple[ActiveOperationSnapshot, ...],
]:
    normalized_contracts = _as_tuple(contracts, ContractReadinessSnapshot, "contracts")
    normalized_teams = _as_tuple(teams, TeamReadinessSnapshot, "teams")
    normalized_capabilities = _as_tuple(
        capabilities, CapabilityReadinessSnapshot, "capabilities"
    )
    normalized_operations = _as_tuple(
        active_operations, ActiveOperationSnapshot, "active_operations"
    )
    return (
        tuple(sorted(normalized_contracts, key=lambda item: (item.work_id, item.contract_id))),
        tuple(sorted(normalized_teams, key=lambda item: item.team_id)),
        tuple(
            sorted(normalized_capabilities, key=lambda item: (item.team_id, item.capability_id))
        ),
        tuple(sorted(normalized_operations, key=lambda item: (item.work_id, item.operation_id))),
    )


class ProjectReadinessAdapter:
    """Bind graph tasks and explicit runtime facts for deterministic evaluation."""

    def __init__(self, evaluator: ProjectReadinessEvaluator | None = None) -> None:
        self.evaluator = evaluator or ProjectReadinessEvaluator()

    def adapt(
        self,
        graph: ProjectGraphSnapshot,
        process: ProjectProcess,
        *,
        contracts: Iterable[ContractReadinessSnapshot] | None = None,
        teams: Iterable[TeamReadinessSnapshot] | None = None,
        capabilities: Iterable[CapabilityReadinessSnapshot] | None = None,
        active_operations: Iterable[ActiveOperationSnapshot] | None = None,
        execution: ProjectExecutionReadinessSnapshot | None = None,
        execution_readiness: ProjectExecutionReadinessSnapshot | None = None,
        process_readiness: ProcessReadinessSnapshot | None = None,
        task_facts: Mapping[str, TaskReadinessFacts] | None = None,
        internal_opt_out_task_ids: Iterable[str] = (),
    ) -> BoundReadinessSnapshot:
        if not isinstance(graph, ProjectGraphSnapshot):
            raise TypeError("graph must be ProjectGraphSnapshot")
        if not isinstance(process, ProjectProcess):
            raise TypeError("process must be ProjectProcess")
        project_id = _required_text(graph.project_id, "graph.project_id")
        if _required_text(process.project_id, "process.project_id") != project_id:
            raise ValueError("graph and process belong to different projects")
        graph_digest = _required_text(graph.digest, "graph.digest")
        if execution is not None and execution_readiness is not None and execution != execution_readiness:
            raise ValueError("execution and execution_readiness disagree")
        resolved_execution = execution if execution is not None else execution_readiness
        if resolved_execution is None:
            resolved_execution = ProjectExecutionReadinessSnapshot()
        if not isinstance(resolved_execution, ProjectExecutionReadinessSnapshot):
            raise TypeError("execution must be ProjectExecutionReadinessSnapshot")

        process_snapshot = _canonical_process_readiness(process, process_readiness)
        tasks = _graph_tasks(graph)
        relations = _graph_relations(graph)
        task_ids = {item.work_id for item in tasks}
        facts_by_task = _task_facts(task_ids, task_facts, internal_opt_out_task_ids)
        (
            normalized_contracts,
            normalized_teams,
            normalized_capabilities,
            normalized_operations,
        ) = _sorted_facts(contracts, teams, capabilities, active_operations)

        default_task_facts = TaskReadinessFacts()
        enriched_tasks: list[WorkItemSnapshot] = []
        for task in tasks:
            fact = facts_by_task.get(task.work_id, default_task_facts)
            enriched_tasks.append(
                replace(
                    task,
                    required_capabilities=fact.required_capabilities,
                    required_slots=fact.required_slots,
                    contract_id=fact.contract_id,
                    contract_required=fact.contract_required,
                    team_available=fact.team_available,
                    active_operation_ids=fact.active_operation_ids,
                )
            )
        enriched_tasks = tuple(enriched_tasks)
        snapshot = ProjectReadinessSnapshot(
            tasks=enriched_tasks,
            relations=relations,
            contracts=normalized_contracts,
            teams=normalized_teams,
            capabilities=normalized_capabilities,
            active_operations=normalized_operations,
            process=process_snapshot,
            execution=resolved_execution,
        )
        return BoundReadinessSnapshot(
            graph_digest=graph_digest,
            snapshot=snapshot,
            evaluation=self.evaluator.evaluate(snapshot),
        )

    def build(self, *args, **kwargs) -> BoundReadinessSnapshot:
        """Alias for :meth:`adapt` used by callers that build a bound view."""

        return self.adapt(*args, **kwargs)


def bind_project_readiness(
    graph: ProjectGraphSnapshot,
    process: ProjectProcess,
    **kwargs,
) -> BoundReadinessSnapshot:
    """Functional convenience wrapper around :class:`ProjectReadinessAdapter`."""

    return ProjectReadinessAdapter().adapt(graph, process, **kwargs)


adapt_project_readiness = bind_project_readiness
build_project_readiness = bind_project_readiness


__all__ = [
    "BoundReadinessSnapshot",
    "ProjectReadinessAdapter",
    "TaskReadinessFacts",
    "TaskReadinessSnapshot",
    "adapt_project_readiness",
    "bind_project_readiness",
    "build_project_readiness",
]
