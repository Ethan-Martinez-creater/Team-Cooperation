"""Persist integration, delivery, and completion evidence.

Revision ID: 20260831_58
Revises: 20260831_57
Create Date: 2026-08-31
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260831_58"
down_revision: str | None = "20260831_57"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLES = (
    "integration_runs",
    "project_deliveries",
    "project_completion_contracts",
    "project_completion_evaluations",
)
_INTEGRATION_COMPLETION_CHECK = (
    "(status = 'PENDING' AND completed_at IS NULL) OR "
    "(status IN ('PASS','FAIL','STALE') AND completed_at IS NOT NULL)"
)
_DELIVERY_DECISION_FIELDS_CHECK = (
    "(decision IS NULL AND decision_key IS NULL AND decision_digest IS NULL "
    "AND decision_reason IS NULL AND decided_by IS NULL AND decided_at IS NULL) OR "
    "(decision IS NOT NULL AND decision_key IS NOT NULL "
    "AND decision_digest IS NOT NULL AND decision_reason IS NOT NULL "
    "AND decided_by IS NOT NULL AND decided_at IS NOT NULL)"
)
_DELIVERY_STATUS_CHECK = (
    "(status IN ('ASSEMBLING','READY') AND approved_by IS NULL "
    "AND accepted_at IS NULL AND decision IS NULL AND decision_key IS NULL "
    "AND decision_digest IS NULL AND decision_reason IS NULL "
    "AND decided_by IS NULL AND decided_at IS NULL) OR "
    "(status = 'ACCEPTED' AND approved_by IS NOT NULL AND accepted_at IS NOT NULL "
    "AND decision = 'ACCEPT' AND decision_key IS NOT NULL "
    "AND decision_digest IS NOT NULL AND decision_reason IS NOT NULL "
    "AND decided_by IS NOT NULL AND decided_at IS NOT NULL) OR "
    "(status = 'REJECTED' AND approved_by IS NULL AND accepted_at IS NULL "
    "AND decision = 'REJECT' AND decision_key IS NOT NULL "
    "AND decision_digest IS NOT NULL AND decision_reason IS NOT NULL "
    "AND decided_by IS NOT NULL AND decided_at IS NOT NULL)"
)
_CONTRACT_APPROVAL_CHECK = (
    "(status = 'DRAFT' AND approved_by IS NULL AND approved_at IS NULL) OR "
    "(status = 'APPROVED' AND approved_by IS NOT NULL AND approved_at IS NOT NULL)"
)


def upgrade() -> None:
    # Keep this migration literal and standalone.  The delivery evidence
    # tables use service-level ownership checks and must not acquire
    # cross-metadata foreign keys.
    op.create_table(
        "integration_runs",
        sa.Column("integration_id", sa.String(128), primary_key=True),
        sa.Column("project_id", sa.String(128), nullable=False),
        sa.Column("process_id", sa.String(128), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("graph_digest", sa.String(64), nullable=False),
        sa.Column("subject_digest", sa.String(64), nullable=False),
        sa.Column("based_on_process_version", sa.Integer(), nullable=False),
        sa.Column("based_on_event_sequence", sa.Integer(), nullable=False),
        sa.Column("input_artifact_refs_json", sa.JSON(none_as_null=True), nullable=False),
        sa.Column("integration_policy_json", sa.JSON(none_as_null=True), nullable=False),
        sa.Column("result_artifact_refs_json", sa.JSON(none_as_null=True), nullable=False),
        sa.Column("verification_refs_json", sa.JSON(none_as_null=True), nullable=False),
        sa.Column("checks_json", sa.JSON(none_as_null=True), nullable=False),
        sa.Column("impacted_work_ids_json", sa.JSON(none_as_null=True), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("initiated_by", sa.String(256), nullable=False),
        sa.Column("executed_as", sa.String(256), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("version >= 1", name="ck_integration_runs_version"),
        sa.CheckConstraint(
            "length(graph_digest) = 64",
            name="ck_integration_runs_graph_digest",
        ),
        sa.CheckConstraint(
            "length(subject_digest) = 64",
            name="ck_integration_runs_subject_digest",
        ),
        sa.CheckConstraint(
            "based_on_process_version >= 1",
            name="ck_integration_runs_based_on_process_version",
        ),
        sa.CheckConstraint(
            "based_on_event_sequence >= 0",
            name="ck_integration_runs_based_on_event_sequence",
        ),
        sa.CheckConstraint(
            "status IN ('PENDING','PASS','FAIL','STALE')",
            name="ck_integration_runs_status",
        ),
        sa.CheckConstraint(
            _INTEGRATION_COMPLETION_CHECK,
            name="ck_integration_runs_completion",
        ),
        sa.UniqueConstraint(
            "process_id",
            "subject_digest",
            name="uq_integration_runs_process_subject",
        ),
    )
    op.create_table(
        "project_deliveries",
        sa.Column("delivery_id", sa.String(128), primary_key=True),
        sa.Column("project_id", sa.String(128), nullable=False),
        sa.Column("process_id", sa.String(128), nullable=False),
        sa.Column("integration_id", sa.String(128), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("graph_digest", sa.String(64), nullable=False),
        sa.Column("artifact_refs_json", sa.JSON(none_as_null=True), nullable=False),
        sa.Column(
            "artifact_version_manifest_json",
            sa.JSON(none_as_null=True),
            nullable=False,
        ),
        sa.Column(
            "artifact_digest_manifest_json",
            sa.JSON(none_as_null=True),
            nullable=False,
        ),
        sa.Column("verification_refs_json", sa.JSON(none_as_null=True), nullable=False),
        sa.Column(
            "acceptance_requirements_json",
            sa.JSON(none_as_null=True),
            nullable=False,
        ),
        sa.Column("release_notes_resource_id", sa.String(128), nullable=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("created_by", sa.String(256), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("approved_by", sa.String(256), nullable=True),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decision", sa.String(16), nullable=True),
        sa.Column("decision_key", sa.String(128), nullable=True),
        sa.Column("decision_digest", sa.String(64), nullable=True),
        sa.Column("decision_reason", sa.Text(), nullable=True),
        sa.Column("decided_by", sa.String(256), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("version >= 1", name="ck_project_deliveries_version"),
        sa.CheckConstraint(
            "length(graph_digest) = 64",
            name="ck_project_deliveries_graph_digest",
        ),
        sa.CheckConstraint(
            "status IN ('ASSEMBLING','READY','ACCEPTED','REJECTED')",
            name="ck_project_deliveries_status",
        ),
        sa.CheckConstraint(
            "decision IS NULL OR decision IN ('ACCEPT','REJECT')",
            name="ck_project_deliveries_decision",
        ),
        sa.CheckConstraint(
            "decision_digest IS NULL OR length(decision_digest) = 64",
            name="ck_project_deliveries_decision_digest",
        ),
        sa.CheckConstraint(
            _DELIVERY_DECISION_FIELDS_CHECK,
            name="ck_project_deliveries_decision_fields",
        ),
        sa.CheckConstraint(
            _DELIVERY_STATUS_CHECK,
            name="ck_project_deliveries_status_consistency",
        ),
        sa.UniqueConstraint(
            "integration_id",
            name="uq_project_deliveries_integration",
        ),
    )
    op.create_table(
        "project_completion_contracts",
        sa.Column("contract_id", sa.String(128), primary_key=True),
        sa.Column("project_id", sa.String(128), nullable=False),
        sa.Column("process_id", sa.String(128), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("criteria_json", sa.JSON(none_as_null=True), nullable=False),
        sa.Column(
            "required_human_approvers_json",
            sa.JSON(none_as_null=True),
            nullable=False,
        ),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("content_sha256", sa.String(64), nullable=False),
        sa.Column("created_by", sa.String(256), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("approved_by", sa.String(256), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "version >= 1",
            name="ck_project_completion_contracts_version",
        ),
        sa.CheckConstraint(
            "length(content_sha256) = 64",
            name="ck_project_completion_contracts_content_sha256",
        ),
        sa.CheckConstraint(
            "status IN ('DRAFT','APPROVED')",
            name="ck_project_completion_contracts_status",
        ),
        sa.CheckConstraint(
            _CONTRACT_APPROVAL_CHECK,
            name="ck_project_completion_contracts_approval",
        ),
        sa.UniqueConstraint(
            "process_id",
            "version",
            name="uq_project_completion_contracts_process_version",
        ),
    )
    op.create_table(
        "project_completion_evaluations",
        sa.Column("evaluation_id", sa.String(128), primary_key=True),
        sa.Column("project_id", sa.String(128), nullable=False),
        sa.Column("process_id", sa.String(128), nullable=False),
        sa.Column("contract_id", sa.String(128), nullable=False),
        sa.Column("contract_version", sa.Integer(), nullable=False),
        sa.Column("delivery_id", sa.String(128), nullable=False),
        sa.Column("delivery_version", sa.Integer(), nullable=False),
        sa.Column("based_on_process_version", sa.Integer(), nullable=False),
        sa.Column("based_on_event_sequence", sa.Integer(), nullable=False),
        sa.Column("graph_digest", sa.String(64), nullable=False),
        sa.Column("subject_digest", sa.String(64), nullable=False),
        sa.Column("passed", sa.Boolean(), nullable=False),
        sa.Column("checks_json", sa.JSON(none_as_null=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "contract_version >= 1",
            name="ck_project_completion_evaluations_contract_version",
        ),
        sa.CheckConstraint(
            "delivery_version >= 1",
            name="ck_project_completion_evaluations_delivery_version",
        ),
        sa.CheckConstraint(
            "based_on_process_version >= 1",
            name="ck_project_completion_evaluations_based_on_process_version",
        ),
        sa.CheckConstraint(
            "based_on_event_sequence >= 0",
            name="ck_project_completion_evaluations_based_on_event_sequence",
        ),
        sa.CheckConstraint(
            "length(graph_digest) = 64",
            name="ck_project_completion_evaluations_graph_digest",
        ),
        sa.CheckConstraint(
            "length(subject_digest) = 64",
            name="ck_project_completion_evaluations_subject_digest",
        ),
        sa.UniqueConstraint(
            "process_id",
            "subject_digest",
            name="uq_project_completion_evaluations_process_subject",
        ),
    )


def downgrade() -> None:
    # These rows are durable evidence and cannot be represented by revision
    # 57. Refuse before issuing any DDL, preserving all evidence on rollback.
    connection = op.get_bind()
    for table in _TABLES:
        existing = connection.execute(sa.text(f"SELECT 1 FROM {table} LIMIT 1")).first()
        if existing is not None:
            raise RuntimeError(
                "Cannot downgrade 20260831_58: delivery evidence exists; "
                "preserve the evidence rows."
            )

    for table in reversed(_TABLES):
        op.drop_table(table)

