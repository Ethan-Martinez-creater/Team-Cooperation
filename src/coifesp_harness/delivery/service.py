"""Human delivery acceptance and deterministic, transactional completion."""

import hashlib
import re
from datetime import UTC, datetime

from sqlalchemy import func, select

from ..errors import GovernanceConflictError, PolicyDenied, ResourceNotFound
from ..product.repository import ACCOUNTS, PROJECT_AGENT_RUNS, PROJECTS, TEAM_TASKS
from ..product.service import TeamCollaborationService
from ..project_process.repository import PROJECT_PROCESSES
from ..project_process.service import ProjectProcessService
from ..verification.human_models import parse_human_decision
from ..verification.service import _digest
from ..work_graph.repository import PROJECT_GOALS
from .completion import ProjectCompletionEvaluator, default_completion_criteria
from .completion_facts import load_completion_facts
from .manifest import create_delivery_manifest, current_delivery_evidence
from .repository import (
    INTEGRATION_RUNS,
    PROJECT_COMPLETION_CONTRACTS,
    PROJECT_COMPLETION_EVALUATIONS,
    PROJECT_DELIVERIES,
    PROJECT_DELIVERY_APPROVALS,
)


def _identity(prefix, *parts):
    return prefix + ":" + hashlib.sha256("\n".join(parts).encode()).hexdigest()


def _positive(value, name, minimum=1):
    if type(value) is not int or value < minimum:
        raise ValueError(f"{name} must be an integer >= {minimum}")
    return value


def _key(value):
    if type(value) is not str or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", value):
        raise ValueError("invalid idempotency_key")
    return value


class DeliveryService:
    def __init__(self, *, repository, work_graph_repository, artifact_content=None, clock=None):
        if work_graph_repository.engine is not repository.engine or (artifact_content is not None
                and artifact_content.repository.engine is not repository.engine):
            raise ValueError("delivery requires one shared database")
        self.repository, self.graph, self.content = repository, work_graph_repository, artifact_content
        self.clock = clock or (lambda: datetime.now(UTC))
        self.evaluator = ProjectCompletionEvaluator()

    def _context(self, connection, project_id, process_id, actor_id, *, owner=False):
        connection.execute(select(PROJECT_PROCESSES.c.process_id).where(
            PROJECT_PROCESSES.c.process_id == process_id,
            PROJECT_PROCESSES.c.project_id == project_id).with_for_update()).scalar_one_or_none()
        process = self.repository.process(connection, process_id)
        if process.project_id != project_id:
            raise ResourceNotFound("project process is unavailable")
        actor = connection.execute(select(ACCOUNTS).where(ACCOUNTS.c.account_id == actor_id)
                                   .with_for_update()).mappings().one_or_none()
        if actor is None or not actor["enabled"] or actor["registration_status"] != "active":
            raise PolicyDenied("delivery requires an active human account")
        TeamCollaborationService._participant(connection, project_id, actor_id)
        if owner:
            team = connection.execute(select(PROJECTS.c.owner_team_id).where(
                PROJECTS.c.project_id == project_id).with_for_update()).scalar_one()
            if actor["team_id"] != team or actor["team_role"] not in {"owner", "admin"}:
                raise PolicyDenied("only the project owning team's administrators may approve completion contracts")
        return process, actor

    def _emit(self, connection, process_id, *, event_id, kind, subject_id, actor, payload):
        repository = self.repository.using_connection(connection)
        process = repository.process(connection, process_id)
        return ProjectProcessService(repository, clock=self.clock).append_fact(
            process_id=process_id, event_id=event_id, event_type=kind,
            expected_version=process.version, expected_event_sequence=process.last_event_sequence,
            subject_type="completion_contract" if kind.startswith("project.completion_contract.") else "delivery",
            subject_id=subject_id, initiated_by=actor, executed_as=actor,
            correlation_id=subject_id, payload=payload)

    def propose_contract(self, *, project_id, process_id, actor_id, payload):
        required = {"expected_process_version", "expected_contract_version", "idempotency_key", "required_human_approvers"}
        if type(payload) is not dict or not required.issubset(payload) or set(payload) - required - {"criteria", "root_goal_id"}:
            raise ValueError("completion contract fields are invalid")
        expected = _positive(payload["expected_process_version"], "expected_process_version")
        previous = _positive(payload["expected_contract_version"], "expected_contract_version", 0)
        key = _key(payload["idempotency_key"])
        approvers = payload["required_human_approvers"]
        if (type(approvers) is not list or not 1 <= len(approvers) <= 32
                or any(type(item) is not str or not item or len(item) > 128 for item in approvers)
                or len(set(approvers)) != len(approvers)):
            raise ValueError("required_human_approvers must contain 1-32 distinct account IDs")
        criteria = payload.get("criteria", default_completion_criteria())
        self.evaluator.evaluate(facts={}, criteria=criteria)
        approvers = sorted(approvers)
        identity = _identity("completion-contract", process_id, actor_id, key)
        with self.repository.transaction() as connection:
            process, _ = self._context(connection, project_id, process_id, actor_id, owner=True)
            goal_id = payload.get("root_goal_id", process.root_goal_id)
            self._goal(connection, process, goal_id)
            digest = _digest({"project_id": project_id, "process_id": process_id, "version": previous + 1,
                "criteria": criteria, "required_human_approvers": approvers, "root_goal_id": goal_id})
            existing = connection.execute(select(PROJECT_COMPLETION_CONTRACTS).where(
                PROJECT_COMPLETION_CONTRACTS.c.contract_id == identity)).mappings().one_or_none()
            if existing is not None:
                proposed = self.repository.event(connection, identity + ":proposed")
                if (existing["content_sha256"] != digest or proposed is None
                        or proposed.payload.get("expected_process_version") != expected):
                    raise GovernanceConflictError("contract idempotency key changed its content")
                return dict(existing)
            self._mutable(process, expected)
            latest = connection.execute(select(func.coalesce(func.max(PROJECT_COMPLETION_CONTRACTS.c.version), 0)).where(
                PROJECT_COMPLETION_CONTRACTS.c.process_id == process_id)).scalar_one()
            if latest != previous:
                raise GovernanceConflictError("completion contract version is stale")
            for account_id in approvers:
                self._context(connection, project_id, process_id, account_id)
            row = {"contract_id": identity, "project_id": project_id, "process_id": process_id,
                "version": previous + 1, "criteria_json": criteria, "required_human_approvers_json": approvers,
                "status": "DRAFT", "content_sha256": digest, "created_by": actor_id,
                "created_at": self.clock(), "approved_by": None, "approved_at": None}
            connection.execute(PROJECT_COMPLETION_CONTRACTS.insert().values(**row))
            self._emit(connection, process_id, event_id=identity + ":proposed", kind="project.completion_contract.proposed",
                subject_id=identity, actor=actor_id, payload={"contract_id": identity, "version": row["version"],
                    "digest": digest, "root_goal_id": goal_id, "expected_process_version": expected})
            return row

    def approve_contract(self, *, project_id, process_id, contract_id, actor_id, payload):
        if type(payload) is not dict or set(payload) != {"expected_process_version", "idempotency_key"}:
            raise ValueError("contract approval fields are invalid")
        expected = _positive(payload["expected_process_version"], "expected_process_version")
        _key(payload["idempotency_key"])
        digest = _digest({**payload, "actor_id": actor_id})
        with self.repository.transaction() as connection:
            process, _ = self._context(connection, project_id, process_id, actor_id, owner=True)
            row = connection.execute(select(PROJECT_COMPLETION_CONTRACTS).where(
                PROJECT_COMPLETION_CONTRACTS.c.contract_id == contract_id,
                PROJECT_COMPLETION_CONTRACTS.c.process_id == process_id)).mappings().one_or_none()
            if row is None:
                raise ResourceNotFound("completion contract is unavailable")
            event = self.repository.event(connection, contract_id + ":approved")
            if event is not None:
                if event.payload.get("request_digest") != digest:
                    raise GovernanceConflictError("contract approval is already decided")
                return dict(row)
            self._mutable(process, expected)
            latest = connection.execute(select(func.max(PROJECT_COMPLETION_CONTRACTS.c.version)).where(
                PROJECT_COMPLETION_CONTRACTS.c.process_id == process_id)).scalar_one()
            if latest != row["version"]:
                raise GovernanceConflictError("only the newest completion contract may be approved")
            locked = connection.execute(select(PROJECT_DELIVERY_APPROVALS.c.approval_id).join(
                PROJECT_DELIVERIES, PROJECT_DELIVERIES.c.delivery_id == PROJECT_DELIVERY_APPROVALS.c.delivery_id
            ).where(PROJECT_DELIVERIES.c.process_id == process_id,
                    PROJECT_DELIVERIES.c.status.in_(["READY", "ACCEPTED"])).limit(1)).first()
            if locked:
                raise GovernanceConflictError("delivery acceptance already pins its completion contract")
            proposed = self.repository.event(connection, contract_id + ":proposed")
            if proposed is None:
                raise GovernanceConflictError("completion contract has no proposal evidence")
            goal_id = proposed.payload.get("root_goal_id")
            self._goal(connection, process, goal_id)
            connection.execute(PROJECT_PROCESSES.update().where(
                PROJECT_PROCESSES.c.process_id == process_id).values(root_goal_id=goal_id))
            for account_id in row["required_human_approvers_json"]:
                self._context(connection, project_id, process_id, account_id)
            values = {"status": "APPROVED", "approved_by": actor_id, "approved_at": self.clock()}
            connection.execute(PROJECT_COMPLETION_CONTRACTS.update().where(
                PROJECT_COMPLETION_CONTRACTS.c.contract_id == contract_id).values(**values))
            self._emit(connection, process_id, event_id=contract_id + ":approved", kind="project.completion_contract.approved",
                subject_id=contract_id, actor=actor_id, payload={"contract_id": contract_id,
                    "root_goal_id": goal_id, "request_digest": digest})
            return {**row, **values}

    def list_deliveries(self, *, project_id, process_id, actor_id):
        with self.repository.transaction() as connection:
            process, _ = self._context(connection, project_id, process_id, actor_id)
            deliveries = connection.execute(select(PROJECT_DELIVERIES).where(
                PROJECT_DELIVERIES.c.process_id == process_id).order_by(PROJECT_DELIVERIES.c.created_at)).mappings().all()
            contracts = connection.execute(select(PROJECT_COMPLETION_CONTRACTS).where(
                PROJECT_COMPLETION_CONTRACTS.c.process_id == process_id).order_by(PROJECT_COMPLETION_CONTRACTS.c.version)).mappings().all()
            return {"process_version": process.version, "phase": process.phase.value, "status": process.status.value,
                    "deliveries": [self._view(connection, row) for row in deliveries],
                    "completion_contracts": [dict(row) for row in contracts]}

    def prepare_delivery(self, *, project_id, process_id, actor_id, expected_process_version):
        with self.repository.transaction() as connection:
            process, _ = self._context(connection, project_id, process_id, actor_id, owner=True)
            self._mutable(process, _positive(expected_process_version, "expected_process_version"))
            if (process.phase.value, process.status.value) != ("DELIVERY", "READY"):
                raise GovernanceConflictError("delivery preparation requires DELIVERY/READY")
            integration = connection.execute(select(INTEGRATION_RUNS).where(
                INTEGRATION_RUNS.c.process_id == process_id, INTEGRATION_RUNS.c.status == "PASS"
            ).order_by(INTEGRATION_RUNS.c.version.desc()).limit(1)).mappings().one_or_none()
            if integration is None:
                raise GovernanceConflictError("delivery has no integration PASS")
            row = create_delivery_manifest(connection, integration=integration, now=self.clock())
            graph = self.graph.snapshot(connection, project_id=project_id)
            if not all(current_delivery_evidence(connection, repository=self.repository, process=process,
                    graph=graph, delivery=row, content=self.content).values()):
                raise GovernanceConflictError("delivery integration evidence is stale")
            return self._view(connection, row)

    def decide(self, *, project_id, process_id, delivery_id, actor_id, payload):
        if type(payload) is not dict or "expected_process_version" not in payload:
            raise ValueError("expected_process_version is required")
        expected = _positive(payload["expected_process_version"], "expected_process_version")
        decision = parse_human_decision({key: value for key, value in payload.items() if key != "expected_process_version"})
        digest = _digest({**decision, "actor_id": actor_id, "expected_process_version": expected})
        with self.repository.transaction() as connection:
            process, _ = self._context(connection, project_id, process_id, actor_id)
            delivery = connection.execute(select(PROJECT_DELIVERIES).where(PROJECT_DELIVERIES.c.delivery_id == delivery_id,
                PROJECT_DELIVERIES.c.process_id == process_id, PROJECT_DELIVERIES.c.project_id == project_id
            ).with_for_update()).mappings().one_or_none()
            if delivery is None:
                raise ResourceNotFound("delivery is unavailable")
            prior = connection.execute(select(PROJECT_DELIVERY_APPROVALS).where(
                PROJECT_DELIVERY_APPROVALS.c.delivery_id == delivery_id,
                PROJECT_DELIVERY_APPROVALS.c.actor_id == actor_id)).mappings().one_or_none()
            if prior is not None:
                if prior["decision_digest"] != digest:
                    raise GovernanceConflictError("delivery decision is immutable")
                return self._view(connection, delivery)
            self._mutable(process, expected)
            if (process.phase.value, process.status.value) != ("DELIVERY", "READY") or delivery["status"] != "READY":
                raise GovernanceConflictError("delivery is not awaiting acceptance")
            if delivery["version"] != decision["expected_version"]:
                raise GovernanceConflictError("delivery version is stale")
            contract = self._approved_contract(connection, process_id)
            approved = self.repository.event(connection, contract["contract_id"] + ":approved")
            if approved is None or approved.payload.get("root_goal_id") != process.root_goal_id:
                raise GovernanceConflictError("approved completion goal binding changed")
            if actor_id not in contract["required_human_approvers_json"]:
                raise PolicyDenied("account is not a required delivery approver")
            requirements = delivery["acceptance_requirements_json"]
            if requirements.get("contract_id") is not None and (
                    requirements.get("contract_id") != contract["contract_id"]
                    or requirements.get("contract_version") != contract["version"]
                    or requirements.get("contract_digest") != contract["content_sha256"]):
                raise GovernanceConflictError("completion contract changed during acceptance")
            approval_id = _identity("delivery-approval", delivery_id, actor_id)
            now = self.clock()
            connection.execute(PROJECT_DELIVERY_APPROVALS.insert().values(approval_id=approval_id,
                project_id=project_id, process_id=process_id, delivery_id=delivery_id,
                contract_id=contract["contract_id"], contract_version=contract["version"], actor_id=actor_id,
                decision=decision["decision"], decision_key=decision["idempotency_key"], decision_digest=digest,
                reason=decision["reason"], expected_delivery_version=delivery["version"],
                expected_process_version=process.version, created_at=now))
            approvals = self._approvals(connection, delivery_id)
            accepted = {row["actor_id"] for row in approvals if row["decision"] == "ACCEPT"}
            final = decision["decision"] == "REJECT" or set(contract["required_human_approvers_json"]).issubset(accepted)
            values = {"version": delivery["version"] + 1, "updated_at": now,
                "acceptance_requirements_json": {**requirements, "contract_id": contract["contract_id"],
                    "contract_version": contract["version"], "contract_digest": contract["content_sha256"]}}
            if final:
                values.update(status="REJECTED" if decision["decision"] == "REJECT" else "ACCEPTED",
                    decision=decision["decision"], decision_key=decision["idempotency_key"], decision_digest=digest,
                    decision_reason=decision["reason"], decided_by=actor_id, decided_at=now,
                    approved_by=actor_id if decision["decision"] == "ACCEPT" else None,
                    accepted_at=now if decision["decision"] == "ACCEPT" else None)
            updated = {**delivery, **values}
            connection.execute(PROJECT_DELIVERIES.update().where(PROJECT_DELIVERIES.c.delivery_id == delivery_id).values(**values))
            if decision["decision"] == "REJECT":
                self._reject(connection, process, updated, actor_id)
            else:
                graph = self.graph.snapshot(connection, project_id=project_id)
                facts = load_completion_facts(connection, repository=self.repository, process=process, graph=graph,
                    delivery=updated, contract=contract, approvals=approvals, content=self.content)
                if not facts["integration_passed"] or not facts["artifact_integrity_valid"]:
                    raise GovernanceConflictError("delivery artifacts or integration evidence changed")
                self._emit(connection, process_id, event_id=approval_id, kind="project.delivery.approval_decided",
                    subject_id=delivery_id, actor=actor_id, payload={"approval_id": approval_id, "decision": "ACCEPT"})
                if final:
                    evaluation = self.evaluator.evaluate(facts=facts, criteria=contract["criteria_json"])
                    if not evaluation.passed:
                        failed = [check["criterion_id"] for check in evaluation.checks if check["status"] == "FAIL"]
                        raise GovernanceConflictError("completion conditions not met: " + ", ".join(failed))
                    self._complete(connection, updated, contract, graph, evaluation, actor_id)
            return self._view(connection, updated)

    def _complete(self, connection, delivery, contract, graph, evaluation, actor_id):
        process = self.repository.process(connection, delivery["process_id"])
        digest = _digest({"delivery_id": delivery["delivery_id"], "delivery_version": delivery["version"],
            "contract_id": contract["contract_id"], "contract_version": contract["version"],
            "graph_digest": graph.digest, "process_version": process.version,
            "event_sequence": process.last_event_sequence, "checks": evaluation.checks})
        identity = _identity("completion-evaluation", process.process_id, digest)
        connection.execute(PROJECT_COMPLETION_EVALUATIONS.insert().values(evaluation_id=identity,
            project_id=process.project_id, process_id=process.process_id, contract_id=contract["contract_id"],
            contract_version=contract["version"], delivery_id=delivery["delivery_id"], delivery_version=delivery["version"],
            based_on_process_version=process.version, based_on_event_sequence=process.last_event_sequence,
            graph_digest=graph.digest, subject_digest=digest, passed=True, checks_json=list(evaluation.checks), created_at=self.clock()))
        self._emit(connection, process.process_id, event_id=identity, kind="project.completion.evaluated",
            subject_id=delivery["delivery_id"], actor="service:project-completer",
            payload={"evaluation_id": identity, "delivery_id": delivery["delivery_id"], "passed": True})
        ProjectProcessService(self.repository.using_connection(connection), clock=self.clock).apply_transition(
            process_id=process.process_id, event_id=delivery["delivery_id"] + ":accepted", event_type="project.delivery.accepted",
            transition_key="delivery.accepted", expected_version=process.version, subject_type="delivery",
            subject_id=delivery["delivery_id"], initiated_by=actor_id, executed_as="service:project-completer",
            correlation_id=delivery["delivery_id"], payload={"delivery_id": delivery["delivery_id"], "evaluation_id": identity})

    def _reject(self, connection, process, delivery, actor_id):
        integration = connection.execute(select(INTEGRATION_RUNS).where(
            INTEGRATION_RUNS.c.integration_id == delivery["integration_id"],
            INTEGRATION_RUNS.c.process_id == process.process_id,
            INTEGRATION_RUNS.c.project_id == process.project_id)).mappings().one_or_none()
        if (integration is None or integration["status"] != "PASS"
                or integration["verification_refs_json"] != delivery["verification_refs_json"]):
            raise GovernanceConflictError("delivery integration binding changed")
        impacted = []
        for ref in integration["verification_refs_json"]:
            task = connection.execute(select(TEAM_TASKS).where(TEAM_TASKS.c.task_id == ref["task_id"],
                TEAM_TASKS.c.process_id == process.process_id).with_for_update()).mappings().one_or_none()
            latest = connection.execute(select(PROJECT_AGENT_RUNS.c.run_id).where(
                PROJECT_AGENT_RUNS.c.process_id == process.process_id,
                PROJECT_AGENT_RUNS.c.team_task_id == ref["task_id"], PROJECT_AGENT_RUNS.c.run_kind == "task_execution"
            ).order_by(PROJECT_AGENT_RUNS.c.execution_attempt.desc()).limit(1)).scalar_one_or_none()
            if (task is not None and task["status"] == "verified" and latest == ref["source_run_id"]
                    and task["accepted_contract_version"] == task["source_contract_version"] == ref["contract_version"]):
                connection.execute(TEAM_TASKS.update().where(TEAM_TASKS.c.task_id == ref["task_id"]).values(
                    status="changes_requested", updated_at=delivery["decided_at"], completed_at=None))
                impacted.append(ref["task_id"])
        ProjectProcessService(self.repository.using_connection(connection), clock=self.clock).apply_transition(
            process_id=process.process_id, event_id=delivery["delivery_id"] + ":rejected", event_type="project.delivery.rejected",
            transition_key="delivery.rejected", expected_version=process.version, subject_type="delivery",
            subject_id=delivery["delivery_id"], initiated_by=actor_id, executed_as="service:project-orchestrator",
            correlation_id=delivery["delivery_id"], payload={"delivery_id": delivery["delivery_id"], "impacted_task_ids": sorted(impacted)})

    @staticmethod
    def _mutable(process, expected):
        if process.phase.value == "TERMINAL" or process.version != expected:
            raise GovernanceConflictError("project process is terminal or its version is stale")

    def _goal(self, connection, process, goal_id):
        if type(goal_id) is not str or not goal_id:
            raise ValueError("root_goal_id must select an approved project goal")
        if process.root_goal_id is not None and process.root_goal_id != goal_id:
            raise GovernanceConflictError("the process already binds a different root goal")
        goal = connection.execute(select(PROJECT_GOALS).where(PROJECT_GOALS.c.goal_id == goal_id,
            PROJECT_GOALS.c.project_id == process.project_id).with_for_update()).mappings().one_or_none()
        graph = self.graph.snapshot(connection, project_id=process.project_id)
        if (goal is None or goal["status"] != "approved" or not goal["approved_by"]
                or not goal["success_criteria_json"] or not any(node.node_type.value == "goal"
                    and node.subject_id == goal_id for node in graph.nodes)):
            raise GovernanceConflictError("root_goal_id must select an approved project goal")

    @staticmethod
    def _approved_contract(connection, process_id):
        row = connection.execute(select(PROJECT_COMPLETION_CONTRACTS).where(
            PROJECT_COMPLETION_CONTRACTS.c.process_id == process_id, PROJECT_COMPLETION_CONTRACTS.c.status == "APPROVED"
        ).order_by(PROJECT_COMPLETION_CONTRACTS.c.version.desc()).limit(1)).mappings().one_or_none()
        if row is None:
            raise GovernanceConflictError("an approved completion contract is required")
        return row

    @staticmethod
    def _approvals(connection, delivery_id):
        return connection.execute(select(PROJECT_DELIVERY_APPROVALS).where(
            PROJECT_DELIVERY_APPROVALS.c.delivery_id == delivery_id).order_by(PROJECT_DELIVERY_APPROVALS.c.actor_id)).mappings().all()

    def _view(self, connection, delivery):
        return {**delivery, "approvals": [dict(row) for row in self._approvals(connection, delivery["delivery_id"])]}
