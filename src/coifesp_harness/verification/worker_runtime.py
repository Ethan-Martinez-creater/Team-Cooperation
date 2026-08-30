"""Agent Worker review projection and tenant-scoped crash recovery."""

import logging
from pathlib import Path

from sqlalchemy import or_, select

from ..artifacts.content import ArtifactContentService
from ..artifacts.repository import SQLAlchemyArtifactRepository
from ..artifacts.storage import LocalImmutableArtifactStore
from ..capabilities.repository import SQLAlchemyCapabilityRepository
from ..product.notifications import NotificationService
from ..project_process.repository import SQLAlchemyProjectProcessRepository
from ..project_process.scheduler import (
    ProjectProcessScheduler,
    SQLAlchemyProjectProcessWakeupRepository,
)
from ..sandbox import load_code_profiles
from ..team_agents.accounting import TeamTaskRunAccounting
from ..team_agents.task_projection import TeamTaskResultProjection
from ..team_agents.worker_runtime import TeamTaskTerminalReconciler
from .agent_reviews import AgentReviewChecks
from .repository import AGENT_REVIEWS, TASK_VERIFICATIONS
from .service import TaskVerificationService
from .tool_checks import DurableVerificationChecks

logger = logging.getLogger("coifesp.verification.worker")


class AgentReviewReconciler:
    def __init__(self, verifier, *, batch_size=50, task_reconciler=None):
        if not 1 <= batch_size <= 500:
            raise ValueError("review recovery batch size is invalid")
        self.verifier, self.batch_size = verifier, batch_size
        self._cursors = {}
        self.task_reconciler = task_reconciler

    def reconcile(self, *, tenant_id):
        task_count = (self.task_reconciler.reconcile(tenant_id=tenant_id)
                      if self.task_reconciler is not None else 0)
        statement = select(AGENT_REVIEWS.c.run_id).join(
            TASK_VERIFICATIONS,
            TASK_VERIFICATIONS.c.verification_id == AGENT_REVIEWS.c.verification_id,
        ).where(
            AGENT_REVIEWS.c.owner_team_id == tenant_id,
            or_(AGENT_REVIEWS.c.status == "QUEUED", TASK_VERIFICATIONS.c.status == "PENDING"),
        ).order_by(AGENT_REVIEWS.c.run_id).limit(self.batch_size)
        with self.verifier.repository.transaction() as connection:
            cursor = self._cursors.get(tenant_id)
            rows = connection.execute(statement.where(AGENT_REVIEWS.c.run_id > cursor)
                                      if cursor else statement).scalars().all()
            if not rows and cursor:
                rows = connection.execute(statement).scalars().all()
        self._cursors[tenant_id] = rows[-1] if rows else None
        for run_id in rows:
            try:
                source = self.verifier.review_checks.project_terminal(run_id)
                if source is not None:
                    self.verifier.verify_run(run_id=source)
            except Exception as exc:  # noqa: BLE001 - one unavailable review cannot starve others
                logger.warning("review recovery deferred run_id=%s error_type=%s", run_id, type(exc).__name__)
        return task_count + len(rows)


def configure_worker_reviews(*, settings, engine, service, jobs, audit):
    """Attach the same verifier used by the control plane to a separate Worker."""
    content = ArtifactContentService(
        SQLAlchemyArtifactRepository(engine=engine, audit_log=audit),
        LocalImmutableArtifactStore(Path(settings.artifact_store_root),
                                    max_object_bytes=settings.artifact_max_upload_bytes),
    ) if settings.artifact_store_root else None
    repository = SQLAlchemyProjectProcessRepository(engine)
    scheduler = ProjectProcessScheduler(SQLAlchemyProjectProcessWakeupRepository(engine))

    def enqueue(connection, event):
        scheduler.enqueue_in_transaction(connection, process_id=event.process_id,
            project_id=event.project_id, source_event_id=event.event_id,
            source_event_type=event.event_type, payload={"sequence": event.sequence},
            available_at=event.occurred_at)

    repository.set_event_listener(enqueue)
    verifier = TaskVerificationService(
        repository=repository, artifact_content=content, notifier=NotificationService(engine),
        review_checks=AgentReviewChecks(repository=repository, runs=service.repository,
                                       artifact_content=content),
        tool_checks=DurableVerificationChecks(jobs=jobs, profiles=load_code_profiles(
            settings.sandbox_profiles_json)) if settings.sandbox_profiles_json else None,
    )
    previous = service.terminal_callback
    accounting = TeamTaskRunAccounting(repository=repository, run_repository=service.repository,
        capability_repository=SQLAlchemyCapabilityRepository(engine=engine, audit_log=audit))
    projection = TeamTaskResultProjection(repository=repository, run_repository=service.repository,
                                         artifact_content=content)

    def terminal(run):
        first_error = None
        for callback in (previous, accounting.on_run_terminal, projection.on_run_terminal,
                         verifier.on_run_terminal):
            if callback is not None:
                try:
                    callback(run)
                except Exception as exc:  # noqa: BLE001 - preserve other durable projections
                    first_error = first_error or exc
        if first_error is not None:
            raise first_error

    service.terminal_callback = terminal
    return AgentReviewReconciler(verifier, task_reconciler=TeamTaskTerminalReconciler(
        repository=repository, runs=service.repository, accounting=accounting,
        projection=projection, verifier=verifier))
