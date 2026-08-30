from __future__ import annotations

DOMAIN_FACTS = frozenset(
    {
        "project.goal.confirmed",
        "project.analysis.started",
        "project.analysis.completed",
        "project.plan.approved",
        "project.work.dispatched",
        "project.work.required_submitted",
        "project.verification.completed",
        "project.integration.completed",
        "project.delivery.accepted",
        "project.delivery.rejected",
        "project.scope.changed",
        "team_task.accepted",
        "team_task.started",
        "team_task.submitted",
        "team_task.verified",
        "team_task.rejected",
        "team_task.changes_requested",
        "task_verification.human_review.opened",
        "task_verification.human_review.decided",
        "task_verification.human_review.closed",
        "task.schedule.changed",
        "artifact.published",
        "exchange.responded",
        "agent_run.completed",
        "agent_run.failed",
        "agent_run.cancelled",
        "approval.decided",
        "human.input.provided",
        "risk.created",
        "risk.resolved",
        "project.input.requested",
        "project.gate.opened",
        "project.gate.decided",
        "project.budget.exhausted",
        "project.capability.matched",
        "project.capacity.reserved",
        "project.capacity.reservation_failed",
        "project.capacity.negotiation_resolved",
        "project.orchestrator.decision_stale",
        "project.completion.evaluated",
        # v2 additions for durable terminal Human objects.
        "project.input.closed",
        "project.gate.closed",
    }
)

TRANSITION_SELECTORS = {
    "project.goal.confirmed": frozenset({"goal.confirmed"}),
    "project.analysis.started": frozenset({"analysis.started"}),
    "project.analysis.completed": frozenset({"analysis.completed"}),
    "project.plan.approved": frozenset({"plan.approved"}),
    "project.work.dispatched": frozenset({"work.dispatched"}),
    "project.work.required_submitted": frozenset({"all_required_work_submitted"}),
    "project.verification.completed": frozenset({"verification.failed", "verification.passed"}),
    "project.integration.completed": frozenset({"integration.passed"}),
    "project.delivery.accepted": frozenset({"delivery.accepted"}),
    "project.delivery.rejected": frozenset({"delivery.rejected"}),
    "project.scope.changed": frozenset({"scope.changed"}),
    "project.input.requested": frozenset({"human_input.opened"}),
    "project.gate.opened": frozenset({"human_approval.opened"}),
}


def validate_event_contract(*, event_type: str, transition_key: str | None, schema_version: str) -> None:
    if event_type not in DOMAIN_FACTS:
        raise ValueError("project process event type is not in the event catalog")
    if schema_version not in {"v1", "v2"}:
        raise ValueError("project process event schema version is unsupported for writes")
    if event_type in {"project.input.closed", "project.gate.closed"} and schema_version != "v2":
        raise ValueError("project Human close facts require schema v2")
    if transition_key is not None and transition_key not in TRANSITION_SELECTORS.get(
        event_type, frozenset()
    ):
        raise ValueError("project process event and transition selector do not match")
