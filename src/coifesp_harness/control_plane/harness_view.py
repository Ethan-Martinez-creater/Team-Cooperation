from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.engine import Engine

from ..delivery.repository import (
    PROJECT_COMPLETION_CONTRACTS,
    PROJECT_COMPLETION_EVALUATIONS,
    PROJECT_DELIVERIES,
)
from ..product.repository import PROJECT_TEAMS, TEAM_TASKS
from ..project_process.repository import (
    PROJECT_GATES,
    PROJECT_INPUT_REQUESTS,
    PROJECT_PROCESS_EVENTS,
    PROJECT_PROCESSES,
)
from ..verification.repository import TASK_VERIFICATIONS
from ..work_graph.repository import (
    PROJECT_WORK_NODES,
    PROJECT_WORK_RELATIONS,
    SUBJECT_TABLES,
)

_TERMINAL_PROCESS_STATUSES = {"COMPLETED", "FAILED", "CANCELLED"}
_PHASE_LABELS = {
    "INTAKE": "需求接收",
    "ANALYSIS": "项目分析",
    "PLANNING": "计划制定",
    "EXECUTION": "任务执行",
    "INTEGRATION": "成果集成",
    "VERIFICATION": "质量验证",
    "DELIVERY": "交付确认",
    "TERMINAL": "项目结束",
}
_WAIT_LABELS = {
    "HUMAN_INPUT": "等待成员补充信息",
    "HUMAN_APPROVAL": "等待成员审批",
    "TEAM_RESPONSE": "等待协作团队回复",
    "AGENT_RUN": "Agent 正在处理",
    "TOOL_JOB": "工具任务正在执行",
    "DEPENDENCY": "等待前置任务完成",
    "VERIFICATION": "等待验证结果",
    "SCHEDULE": "等待可用执行容量",
}

# Only these server-owned descriptions may enter the user-facing activity feed.
# Event payloads, principals, correlations, subjects and run ids are never copied.
_ACTIVITY_LABELS = {
    "project.goal.confirmed": ("planning", "项目目标已确认", "completed"),
    "project.analysis.started": ("planning", "Agent 已开始分析项目", "active"),
    "project.analysis.completed": ("planning", "项目分析已完成", "completed"),
    "project.plan.approved": ("planning", "项目计划已获确认", "completed"),
    "project.work.dispatched": ("work", "任务已分派给协作团队", "active"),
    "project.work.required_submitted": ("work", "必需任务均已提交", "completed"),
    "team_task.accepted": ("work", "协作团队已接收任务", "active"),
    "team_task.started": ("work", "协作团队已开始任务", "active"),
    "team_task.submitted": ("work", "协作团队已提交成果", "completed"),
    "team_task.verified": ("verification", "任务成果已通过验证", "completed"),
    "team_task.rejected": ("verification", "任务成果未被接受", "attention"),
    "team_task.changes_requested": ("verification", "任务成果需要修改", "attention"),
    "project.verification.completed": ("verification", "项目验证已完成", "completed"),
    "project.integration.completed": ("delivery", "项目成果集成已完成", "completed"),
    "project.delivery.accepted": ("delivery", "项目交付已被接受", "completed"),
    "project.delivery.rejected": ("delivery", "项目交付需要调整", "attention"),
    "project.delivery.approval_decided": ("delivery", "交付审批已处理", "completed"),
    "project.completion_contract.proposed": (
        "delivery",
        "完成标准草案已生成",
        "active",
    ),
    "project.completion_contract.approved": ("delivery", "完成标准已确认", "completed"),
    "project.completion.evaluated": ("delivery", "项目完成条件已评估", "completed"),
    "project.input.requested": ("blocker", "项目需要成员补充信息", "attention"),
    "project.input.closed": ("blocker", "信息补充项已关闭", "completed"),
    "project.gate.opened": ("blocker", "项目正在等待审批", "attention"),
    "project.gate.decided": ("blocker", "项目审批已作出决定", "completed"),
    "project.gate.closed": ("blocker", "项目审批项已关闭", "completed"),
    "project.budget.exhausted": ("blocker", "当前执行预算需要处理", "attention"),
    "project.capacity.reservation_failed": ("blocker", "执行容量暂不可用", "attention"),
    "project.capacity.reserved": ("work", "执行容量已就绪", "active"),
    "project.capacity.negotiation_resolved": (
        "work",
        "执行容量问题已解决",
        "completed",
    ),
    "project.capability.matched": ("work", "已匹配合适的团队能力", "completed"),
    "project.orchestrator.decision_stale": (
        "planning",
        "项目变化触发重新评估",
        "attention",
    ),
    "project.orchestrator.commands_consumed": (
        "planning",
        "项目计划已更新",
        "completed",
    ),
    "agent_run.completed": ("agent", "Agent 已完成一项工作", "completed"),
    "agent_run.failed": ("agent", "Agent 工作需要重试或人工处理", "attention"),
    "agent_run.cancelled": ("agent", "Agent 工作已取消", "attention"),
    "task_verification.human_review.opened": (
        "verification",
        "验证需要成员复核",
        "attention",
    ),
    "task_verification.human_review.decided": (
        "verification",
        "成员已提交复核决定",
        "completed",
    ),
    "task_verification.human_review.closed": (
        "verification",
        "成员复核已关闭",
        "completed",
    ),
    "task.schedule.changed": ("work", "任务安排已更新", "completed"),
    "artifact.published": ("work", "项目成果已发布", "completed"),
    "exchange.responded": ("collaboration", "协作团队已回复", "completed"),
    "approval.decided": ("blocker", "待审批事项已处理", "completed"),
    "human.input.provided": ("blocker", "成员已补充所需信息", "completed"),
    "risk.created": ("risk", "项目发现新的风险", "attention"),
    "risk.resolved": ("risk", "项目风险已解决", "completed"),
    "project.scope.changed": ("planning", "项目范围已更新", "attention"),
}
_NODE_LABELS = {
    "goal": "目标",
    "requirement": "需求",
    "milestone": "里程碑",
    "phase": "阶段",
    "task": "任务",
    "risk": "风险",
    "decision": "决策",
    "artifact": "成果",
    "verification": "验证",
}


class ProjectHarnessViewService:
    """Build the participant-visible, privacy-safe projection of Harness authority."""

    def __init__(self, engine: Engine, workspace_service) -> None:
        self.engine = engine
        self.workspace_service = workspace_service

    def view(self, *, project_id: str, actor_id: str) -> dict:
        self.workspace_service.workspace(project_id=project_id, actor_id=actor_id)
        with self.engine.connect() as connection:
            process_rows = (
                connection.execute(
                    select(PROJECT_PROCESSES)
                    .where(PROJECT_PROCESSES.c.project_id == project_id)
                    .order_by(PROJECT_PROCESSES.c.updated_at.desc())
                )
                .mappings()
                .all()
            )
            process = next(
                (
                    row
                    for row in process_rows
                    if row["status"] not in _TERMINAL_PROCESS_STATUSES
                ),
                process_rows[0] if process_rows else None,
            )
            tasks = self._tasks(connection, project_id)
            result = {
                "process": self._process(process) if process else None,
                "work_graph": self._graph(connection, project_id, tasks),
                "tasks": list(tasks.values()),
                "activity": [],
                "blockers": [],
                "verification": self._verification(connection, project_id),
                "completion": self._completion(connection, project_id, tasks),
            }
            if process:
                result["activity"] = self._activity(connection, process["process_id"])
                result["blockers"] = self._blockers(connection, process["process_id"])
            return result

    @staticmethod
    def _tasks(connection, project_id: str) -> dict[str, dict]:
        teams = {
            row["team_id"]: row["name"]
            for row in connection.execute(
                select(PROJECT_TEAMS.c.team_id, PROJECT_TEAMS.c.name).where(
                    PROJECT_TEAMS.c.project_id == project_id
                )
            ).mappings()
        }
        rows = (
            connection.execute(
                select(TEAM_TASKS)
                .where(TEAM_TASKS.c.project_id == project_id)
                .order_by(TEAM_TASKS.c.created_at, TEAM_TASKS.c.task_id)
            )
            .mappings()
            .all()
        )
        return {
            row["task_id"]: {
                "task_id": row["task_id"],
                "title": row["title"],
                "status": row["status"],
                "team_id": row["target_team_id"],
                "team_name": teams.get(row["target_team_id"]),
                "priority": row["priority"],
                "due_at": _iso(row["due_at"]),
                "contract_ready": all(
                    row[name] is not None
                    for name in (
                        "process_id",
                        "work_node_id",
                        "requested_capability",
                        "input_manifest_json",
                        "output_contract_json",
                        "verification_policy_json",
                        "source_contract_version",
                        "autonomy_requirement",
                    )
                )
                and row["accepted_contract_version"] == row["source_contract_version"],
                "contract_version": row["source_contract_version"],
            }
            for row in rows
        }

    @staticmethod
    def _graph(connection, project_id: str, tasks: dict[str, dict]) -> dict:
        nodes = (
            connection.execute(
                select(PROJECT_WORK_NODES)
                .where(PROJECT_WORK_NODES.c.project_id == project_id)
                .order_by(PROJECT_WORK_NODES.c.node_type, PROJECT_WORK_NODES.c.node_id)
            )
            .mappings()
            .all()
        )
        relations = (
            connection.execute(
                select(PROJECT_WORK_RELATIONS)
                .where(PROJECT_WORK_RELATIONS.c.project_id == project_id)
                .order_by(PROJECT_WORK_RELATIONS.c.created_at)
            )
            .mappings()
            .all()
        )
        dependencies: dict[str, list[str]] = {}
        edges = []
        for row in relations:
            edges.append(
                {
                    "source": row["source_node_id"],
                    "target": row["target_node_id"],
                    "type": row["relation_type"],
                }
            )
            if row["relation_type"] in {"depends_on", "blocks", "part_of"}:
                dependencies.setdefault(row["source_node_id"], []).append(
                    row["target_node_id"]
                )
        result = []
        for index, row in enumerate(nodes, start=1):
            task = tasks.get(row["subject_id"]) if row["node_type"] == "task" else None
            result.append(
                {
                    "node_id": row["node_id"],
                    "type": row["node_type"],
                    "label": task["title"]
                    if task
                    else _subject_label(connection, row, index),
                    "status": task["status"] if task else None,
                    "team_name": task["team_name"] if task else None,
                    "depends_on": sorted(dependencies.get(row["node_id"], [])),
                }
            )
        return {"nodes": result, "edges": edges}

    @staticmethod
    def _process(row) -> dict:
        status, wait_reason = row["status"], row["wait_reason"]
        if status in {"WAITING", "BLOCKED"}:
            semantic = _WAIT_LABELS.get(wait_reason, "项目正在等待处理")
            next_step = semantic
        elif status == "RUNNING":
            semantic = "Harness 正在推进项目"
            next_step = "等待当前工作完成，系统将自动推进下一步"
        elif status == "READY":
            semantic = "项目已准备继续"
            next_step = "Harness 将根据计划启动下一项工作"
        elif status == "COMPLETED":
            semantic, next_step = "项目已完成", "查看交付结果与验证记录"
        elif status == "FAILED":
            semantic, next_step = "项目需要人工处理", "查看阻塞与失败活动后决定恢复方式"
        else:
            semantic, next_step = "项目已取消", "确认是否需要重新启动项目"
        return {
            "phase": row["phase"],
            "phase_label": _PHASE_LABELS.get(row["phase"], row["phase"]),
            "status": status,
            "wait_reason": wait_reason,
            "semantic_status": semantic,
            "next_step": next_step,
            "version": row["version"],
            "updated_at": _iso(row["updated_at"]),
        }

    @staticmethod
    def _activity(connection, process_id: str) -> list[dict]:
        rows = (
            connection.execute(
                select(
                    PROJECT_PROCESS_EVENTS.c.event_type,
                    PROJECT_PROCESS_EVENTS.c.occurred_at,
                )
                .where(PROJECT_PROCESS_EVENTS.c.process_id == process_id)
                .order_by(PROJECT_PROCESS_EVENTS.c.sequence.desc())
                .limit(40)
            )
            .mappings()
            .all()
        )
        return [
            {
                "category": mapped[0],
                "label": mapped[1],
                "status": mapped[2],
                "occurred_at": _iso(row["occurred_at"]),
            }
            for row in rows
            if (mapped := _ACTIVITY_LABELS.get(row["event_type"])) is not None
        ][:20]

    @staticmethod
    def _blockers(connection, process_id: str) -> list[dict]:
        inputs = connection.execute(
            select(PROJECT_INPUT_REQUESTS.c.created_at).where(
                (PROJECT_INPUT_REQUESTS.c.process_id == process_id)
                & (PROJECT_INPUT_REQUESTS.c.status == "OPEN")
            )
        ).mappings()
        gates = connection.execute(
            select(PROJECT_GATES.c.created_at).where(
                (PROJECT_GATES.c.process_id == process_id)
                & (PROJECT_GATES.c.status == "OPEN")
            )
        ).mappings()
        result = [
            {
                "kind": "input",
                "label": "需要项目成员补充信息",
                "created_at": _iso(row["created_at"]),
            }
            for row in inputs
        ]
        result.extend(
            {
                "kind": "approval",
                "label": "需要项目成员完成审批",
                "created_at": _iso(row["created_at"]),
            }
            for row in gates
        )
        return sorted(result, key=lambda item: item["created_at"] or "")

    @staticmethod
    def _verification(connection, project_id: str) -> dict:
        rows = connection.execute(
            select(
                TASK_VERIFICATIONS.c.task_id,
                TASK_VERIFICATIONS.c.status,
                TASK_VERIFICATIONS.c.created_at,
            )
            .where(TASK_VERIFICATIONS.c.project_id == project_id)
            .order_by(TASK_VERIFICATIONS.c.created_at.desc())
        ).mappings()
        latest_by_task = {}
        for row in rows:
            latest_by_task.setdefault(row["task_id"], row["status"])
        statuses = Counter(latest_by_task.values())
        return {
            "total": sum(statuses.values()),
            "pending": statuses["PENDING"],
            "passed": statuses["PASS"],
            "failed": statuses["FAIL"],
            "stale": statuses["STALE"],
        }

    @staticmethod
    def _completion(connection, project_id: str, tasks: dict[str, dict]) -> dict:
        delivery = (
            connection.execute(
                select(PROJECT_DELIVERIES.c.status, PROJECT_DELIVERIES.c.updated_at)
                .where(PROJECT_DELIVERIES.c.project_id == project_id)
                .order_by(PROJECT_DELIVERIES.c.updated_at.desc())
                .limit(1)
            )
            .mappings()
            .one_or_none()
        )
        contract = (
            connection.execute(
                select(PROJECT_COMPLETION_CONTRACTS.c.status)
                .where(PROJECT_COMPLETION_CONTRACTS.c.project_id == project_id)
                .order_by(PROJECT_COMPLETION_CONTRACTS.c.version.desc())
                .limit(1)
            )
            .mappings()
            .one_or_none()
        )
        evaluation = (
            connection.execute(
                select(PROJECT_COMPLETION_EVALUATIONS.c.passed)
                .where(PROJECT_COMPLETION_EVALUATIONS.c.project_id == project_id)
                .order_by(PROJECT_COMPLETION_EVALUATIONS.c.created_at.desc())
                .limit(1)
            )
            .mappings()
            .one_or_none()
        )
        return {
            "tasks_done": sum(task["status"] == "verified" for task in tasks.values()),
            "tasks_total": len(tasks),
            "contract_status": contract["status"] if contract else None,
            "delivery_status": delivery["status"] if delivery else None,
            "evaluation_passed": evaluation["passed"] if evaluation else None,
            "updated_at": _iso(delivery["updated_at"]) if delivery else None,
        }


def _subject_label(connection, node, index: int) -> str:
    for enum_type, (table, key) in SUBJECT_TABLES.items():
        if enum_type.value != node["node_type"]:
            continue
        for name in ("title", "name"):
            if name in table.c:
                value = connection.execute(
                    select(table.c[name]).where(key == node["subject_id"])
                ).scalar_one_or_none()
                if value:
                    return str(value)
        break
    return f"{_NODE_LABELS.get(node['node_type'], '工作项')} {index}"


def _iso(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.isoformat()
    return str(value)
