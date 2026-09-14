"""Compose project orchestration against the application's existing stores."""

from uuid import uuid4

from ..team_agents.dispatcher import TeamAgentDispatcher
from ..team_agents.profiles import TeamAgentCapabilityResolver
from ..team_agents.task_contracts import PersistentTaskDispatchFactLoader
from ..work_graph.repository import SQLAlchemyWorkGraphRepository
from .budget_service import ProjectExecutionBudgetService
from .capability_adapter import ProjectCapabilityAdapter
from .command_service import ProjectProcessCommandService
from .human_service import HumanGateService
from .planner import ProjectPlannerIntentService
from .planner_consumer import PlannerCommandConsumer
from .planner_runtime import ProjectPlannerRunLauncher
from .runner import ProjectOrchestratorRunner
from .service import ProjectProcessService
from .verification_effect import VerificationOrchestrationEffect
from .worker_loop import ProjectOrchestratorLoop


def build_project_orchestrator_worker(*, repository, scheduler, agent_run_service,
                                     capability_repository, artifact_content,
                                     snapshot_loader_factory, runtime_resolver=None,
                                     worker_id=None, idle_poll_seconds=1.0,
                                     model_route_policy=None):
    """Construction only: no schema creation, new credentials, or background start.

    The caller owns migrations and the repository's transactional event/wakeup
    listener. Passing the snapshot factory explicitly keeps production loading
    separate from the pure runner and allows assembly tests without fake stores.
    """
    graph = SQLAlchemyWorkGraphRepository(repository.engine)
    capabilities = ProjectCapabilityAdapter(capability_repository)
    facts = PersistentTaskDispatchFactLoader(engine=repository.engine, artifact_content=artifact_content)
    from ..runtime import AuthorizedTool
    from ..team_agents.specialists import specialist_delegation_manifest
    from ..tool_catalog import project_context_manifests

    manifests = (*project_context_manifests(), specialist_delegation_manifest())
    tools = tuple(
        AuthorizedTool(manifest.tool_id, manifest.version, manifest.schema_digest)
        for manifest in manifests
    )
    if artifact_content is not None:
        from ..artifacts.task_publication import task_artifact_manifest

        manifest = task_artifact_manifest()
        tools += (AuthorizedTool(manifest.tool_id, manifest.version, manifest.schema_digest),)
    resolver = runtime_resolver or TeamAgentCapabilityResolver(
        engine=repository.engine,
        tool_policies={"default": tools},
        model_policies=(
            {"default": model_route_policy}
            if model_route_policy is not None
            else None
        ),
    )
    dispatcher = TeamAgentDispatcher(repository=repository, work_graph_repository=graph,
        capability_adapter=capabilities, runtime_resolver=resolver,
        run_service=agent_run_service, fact_loader=facts, artifact_content=artifact_content)
    snapshot_loader = snapshot_loader_factory(repository=repository,
        work_graph_repository=graph, capability_adapter=capabilities,
        fact_loader=facts, artifact_content=artifact_content)
    integration_service = None
    if artifact_content is not None:
        from ..delivery.integration import IntegrationService

        integration_service = IntegrationService(repository=repository,
            work_graph_repository=graph, artifact_content=artifact_content)
    runner = ProjectOrchestratorRunner(repository=repository,
        process_service=ProjectProcessService(repository),
        command_service=ProjectProcessCommandService(repository), scheduler=scheduler,
        snapshot_loader=snapshot_loader,
        command_consumer=PlannerCommandConsumer(repository=repository, work_graph_repository=graph),
        intent_launcher=ProjectPlannerRunLauncher(
            intent_service=ProjectPlannerIntentService(
                repository,
                model_route_policy=model_route_policy,
            ),
            work_graph_repository=graph,
            run_service=agent_run_service,
            budget_service=ProjectExecutionBudgetService(repository),
            human_gate_service=HumanGateService(repository),
        ),
        effect=VerificationOrchestrationEffect(repository=repository,
            work_graph_repository=graph, dispatcher=dispatcher, integration_service=integration_service))
    return ProjectOrchestratorLoop(runner,
        worker_id=worker_id or "project-orchestrator:" + uuid4().hex,
        idle_poll_seconds=idle_poll_seconds)
