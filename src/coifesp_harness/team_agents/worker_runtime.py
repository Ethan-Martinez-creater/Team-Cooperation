"""Tenant-bounded recovery of task execution accounting and result projection."""

import logging

from sqlalchemy import and_, exists, or_, select

from ..agent_runs.models import TERMINAL_RUN_STATES
from ..agent_runs.repository import AGENT_RUNS
from ..product.repository import PROJECT_AGENT_RUNS
from ..project_process.repository import PROJECT_EXECUTION_RESERVATIONS
from ..verification.repository import AGENT_REVIEWS, TASK_VERIFICATIONS

logger = logging.getLogger("coifesp.team_task.worker")


class TeamTaskTerminalReconciler:
    def __init__(self, *, repository, runs, accounting, projection, verifier, batch_size=50):
        if type(batch_size) is not int or not 1 <= batch_size <= 500:
            raise ValueError("task recovery batch size is invalid")
        self.repository, self.runs = repository, runs
        self.accounting, self.projection, self.verifier = accounting, projection, verifier
        self.batch_size, self._cursors = batch_size, {}

    def reconcile(self, *, tenant_id):
        review_owned = exists(select(AGENT_REVIEWS.c.review_id).where(
            AGENT_REVIEWS.c.verification_id == TASK_VERIFICATIONS.c.verification_id))
        statement = select(PROJECT_AGENT_RUNS.c.run_id).join(AGENT_RUNS, and_(
            AGENT_RUNS.c.run_id == PROJECT_AGENT_RUNS.c.run_id,
            AGENT_RUNS.c.tenant_id == PROJECT_AGENT_RUNS.c.team_id,
        )).outerjoin(PROJECT_EXECUTION_RESERVATIONS,
            PROJECT_EXECUTION_RESERVATIONS.c.reservation_id == PROJECT_AGENT_RUNS.c.project_budget_reservation_id,
        ).outerjoin(TASK_VERIFICATIONS,
            TASK_VERIFICATIONS.c.source_run_id == PROJECT_AGENT_RUNS.c.run_id,
        ).where(
            PROJECT_AGENT_RUNS.c.team_id == tenant_id,
            PROJECT_AGENT_RUNS.c.run_kind == "task_execution",
            AGENT_RUNS.c.status.in_([status.value for status in TERMINAL_RUN_STATES]),
            or_(PROJECT_AGENT_RUNS.c.task_result_status.is_(None),
                PROJECT_EXECUTION_RESERVATIONS.c.status == "RESERVED",
                and_(PROJECT_AGENT_RUNS.c.task_result_status == "submitted",
                     or_(TASK_VERIFICATIONS.c.verification_id.is_(None),
                         and_(TASK_VERIFICATIONS.c.status == "PENDING", ~review_owned)))),
        ).order_by(PROJECT_AGENT_RUNS.c.run_id).limit(self.batch_size)
        with self.repository.transaction() as connection:
            self.runs._set_tenant(connection, tenant_id)
            cursor = self._cursors.get(tenant_id)
            rows = connection.execute(statement.where(PROJECT_AGENT_RUNS.c.run_id > cursor)
                                      if cursor else statement).scalars().all()
            if not rows and cursor:
                rows = connection.execute(statement).scalars().all()
        self._cursors[tenant_id] = rows[-1] if rows else None
        for run_id in rows:
            try:
                # Each step is independently durable and replayable. Settlement
                # precedes review admission so real usage frees reserved capacity.
                self.accounting.settle(run_id=run_id)
                self.projection.project(run_id=run_id)
                self.verifier.verify_run(run_id=run_id)
            except Exception as exc:  # noqa: BLE001 - bounded independent recovery
                logger.warning("task recovery deferred run_id=%s error_type=%s", run_id, type(exc).__name__)
        return len(rows)
