"""Application authorization roles accepted from external identity providers."""

APPLICATION_ROLES = frozenset(
    {
        "lead",
        "contributor",
        "reviewer",
        "observer",
        "tool_approver",
        "memory_curator",
        "collaboration_creator",
        "agent_run_controller",
        "execution_controller",
        "capability_publisher",
        "platform_administrator",
        "agent_worker",
        "tool_worker",
        "execution_worker",
    }
)
