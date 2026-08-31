from __future__ import annotations

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
)

# Integration, delivery, and completion evidence intentionally live in their
# own metadata domain. Ownership of the referenced project/process/run,
# verification, and artifact records is checked by services; these tables
# therefore remain free of cross-metadata foreign keys.
DELIVERY_METADATA = MetaData()


INTEGRATION_RUNS = Table(
    "integration_runs",
    DELIVERY_METADATA,
    Column("integration_id", String(128), primary_key=True),
    Column("project_id", String(128), nullable=False),
    Column("process_id", String(128), nullable=False),
    Column("version", Integer, nullable=False),
    Column("graph_digest", String(64), nullable=False),
    Column("subject_digest", String(64), nullable=False),
    Column("based_on_process_version", Integer, nullable=False),
    Column("based_on_event_sequence", Integer, nullable=False),
    Column("input_artifact_refs_json", JSON(none_as_null=True), nullable=False),
    Column("integration_policy_json", JSON(none_as_null=True), nullable=False),
    Column("result_artifact_refs_json", JSON(none_as_null=True), nullable=False),
    Column("verification_refs_json", JSON(none_as_null=True), nullable=False),
    Column("checks_json", JSON(none_as_null=True), nullable=False),
    Column("impacted_work_ids_json", JSON(none_as_null=True), nullable=False),
    Column("status", String(16), nullable=False),
    Column("initiated_by", String(256), nullable=False),
    Column("executed_as", String(256), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    Column("completed_at", DateTime(timezone=True), nullable=True),
    CheckConstraint("version >= 1", name="ck_integration_runs_version"),
    CheckConstraint(
        "length(graph_digest) = 64",
        name="ck_integration_runs_graph_digest",
    ),
    CheckConstraint(
        "length(subject_digest) = 64",
        name="ck_integration_runs_subject_digest",
    ),
    CheckConstraint(
        "based_on_process_version >= 1",
        name="ck_integration_runs_based_on_process_version",
    ),
    CheckConstraint(
        "based_on_event_sequence >= 0",
        name="ck_integration_runs_based_on_event_sequence",
    ),
    CheckConstraint(
        "status IN ('PENDING','PASS','FAIL','STALE')",
        name="ck_integration_runs_status",
    ),
    CheckConstraint(
        "(status = 'PENDING' AND completed_at IS NULL) OR "
        "(status IN ('PASS','FAIL','STALE') AND completed_at IS NOT NULL)",
        name="ck_integration_runs_completion",
    ),
    UniqueConstraint(
        "process_id",
        "subject_digest",
        name="uq_integration_runs_process_subject",
    ),
)


PROJECT_DELIVERIES = Table(
    "project_deliveries",
    DELIVERY_METADATA,
    Column("delivery_id", String(128), primary_key=True),
    Column("project_id", String(128), nullable=False),
    Column("process_id", String(128), nullable=False),
    Column("integration_id", String(128), nullable=False),
    Column("version", Integer, nullable=False),
    Column("graph_digest", String(64), nullable=False),
    Column("artifact_refs_json", JSON(none_as_null=True), nullable=False),
    Column("artifact_version_manifest_json", JSON(none_as_null=True), nullable=False),
    Column("artifact_digest_manifest_json", JSON(none_as_null=True), nullable=False),
    Column("verification_refs_json", JSON(none_as_null=True), nullable=False),
    Column("acceptance_requirements_json", JSON(none_as_null=True), nullable=False),
    Column("release_notes_resource_id", String(128), nullable=True),
    Column("status", String(16), nullable=False),
    Column("created_by", String(256), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    Column("approved_by", String(256), nullable=True),
    Column("accepted_at", DateTime(timezone=True), nullable=True),
    Column("decision", String(16), nullable=True),
    Column("decision_key", String(128), nullable=True),
    Column("decision_digest", String(64), nullable=True),
    Column("decision_reason", Text, nullable=True),
    Column("decided_by", String(256), nullable=True),
    Column("decided_at", DateTime(timezone=True), nullable=True),
    CheckConstraint("version >= 1", name="ck_project_deliveries_version"),
    CheckConstraint(
        "length(graph_digest) = 64",
        name="ck_project_deliveries_graph_digest",
    ),
    CheckConstraint(
        "status IN ('ASSEMBLING','READY','ACCEPTED','REJECTED')",
        name="ck_project_deliveries_status",
    ),
    CheckConstraint(
        "decision IS NULL OR decision IN ('ACCEPT','REJECT')",
        name="ck_project_deliveries_decision",
    ),
    CheckConstraint(
        "decision_digest IS NULL OR length(decision_digest) = 64",
        name="ck_project_deliveries_decision_digest",
    ),
    CheckConstraint(
        "(decision IS NULL AND decision_key IS NULL AND decision_digest IS NULL "
        "AND decision_reason IS NULL AND decided_by IS NULL AND decided_at IS NULL) OR "
        "(decision IS NOT NULL AND decision_key IS NOT NULL "
        "AND decision_digest IS NOT NULL AND decision_reason IS NOT NULL "
        "AND decided_by IS NOT NULL AND decided_at IS NOT NULL)",
        name="ck_project_deliveries_decision_fields",
    ),
    CheckConstraint(
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
        "AND decided_by IS NOT NULL AND decided_at IS NOT NULL)",
        name="ck_project_deliveries_status_consistency",
    ),
    UniqueConstraint("integration_id", name="uq_project_deliveries_integration"),
)


PROJECT_COMPLETION_CONTRACTS = Table(
    "project_completion_contracts",
    DELIVERY_METADATA,
    Column("contract_id", String(128), primary_key=True),
    Column("project_id", String(128), nullable=False),
    Column("process_id", String(128), nullable=False),
    Column("version", Integer, nullable=False),
    Column("criteria_json", JSON(none_as_null=True), nullable=False),
    Column("required_human_approvers_json", JSON(none_as_null=True), nullable=False),
    Column("status", String(16), nullable=False),
    Column("content_sha256", String(64), nullable=False),
    Column("created_by", String(256), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("approved_by", String(256), nullable=True),
    Column("approved_at", DateTime(timezone=True), nullable=True),
    CheckConstraint(
        "version >= 1",
        name="ck_project_completion_contracts_version",
    ),
    CheckConstraint(
        "length(content_sha256) = 64",
        name="ck_project_completion_contracts_content_sha256",
    ),
    CheckConstraint(
        "status IN ('DRAFT','APPROVED')",
        name="ck_project_completion_contracts_status",
    ),
    CheckConstraint(
        "(status = 'DRAFT' AND approved_by IS NULL AND approved_at IS NULL) OR "
        "(status = 'APPROVED' AND approved_by IS NOT NULL AND approved_at IS NOT NULL)",
        name="ck_project_completion_contracts_approval",
    ),
    UniqueConstraint(
        "process_id",
        "version",
        name="uq_project_completion_contracts_process_version",
    ),
)


PROJECT_COMPLETION_EVALUATIONS = Table(
    "project_completion_evaluations",
    DELIVERY_METADATA,
    Column("evaluation_id", String(128), primary_key=True),
    Column("project_id", String(128), nullable=False),
    Column("process_id", String(128), nullable=False),
    Column("contract_id", String(128), nullable=False),
    Column("contract_version", Integer, nullable=False),
    Column("delivery_id", String(128), nullable=False),
    Column("delivery_version", Integer, nullable=False),
    Column("based_on_process_version", Integer, nullable=False),
    Column("based_on_event_sequence", Integer, nullable=False),
    Column("graph_digest", String(64), nullable=False),
    Column("subject_digest", String(64), nullable=False),
    Column("passed", Boolean, nullable=False),
    Column("checks_json", JSON(none_as_null=True), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    CheckConstraint(
        "contract_version >= 1",
        name="ck_project_completion_evaluations_contract_version",
    ),
    CheckConstraint(
        "delivery_version >= 1",
        name="ck_project_completion_evaluations_delivery_version",
    ),
    CheckConstraint(
        "based_on_process_version >= 1",
        name="ck_project_completion_evaluations_based_on_process_version",
    ),
    CheckConstraint(
        "based_on_event_sequence >= 0",
        name="ck_project_completion_evaluations_based_on_event_sequence",
    ),
    CheckConstraint(
        "length(graph_digest) = 64",
        name="ck_project_completion_evaluations_graph_digest",
    ),
    CheckConstraint(
        "length(subject_digest) = 64",
        name="ck_project_completion_evaluations_subject_digest",
    ),
    UniqueConstraint(
        "process_id",
        "subject_digest",
        name="uq_project_completion_evaluations_process_subject",
    ),
)


PROJECT_DELIVERY_APPROVALS = Table(
    "project_delivery_approvals",
    DELIVERY_METADATA,
    Column("approval_id", String(128), primary_key=True),
    Column("project_id", String(128), nullable=False),
    Column("process_id", String(128), nullable=False),
    Column("delivery_id", String(128), nullable=False),
    Column("contract_id", String(128), nullable=False),
    Column("contract_version", Integer, nullable=False),
    Column("actor_id", String(128), nullable=False),
    Column("decision", String(16), nullable=False),
    Column("decision_key", String(128), nullable=False),
    Column("decision_digest", String(64), nullable=False),
    Column("reason", Text, nullable=False),
    Column("expected_delivery_version", Integer, nullable=False),
    Column("expected_process_version", Integer, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    CheckConstraint(
        "length(approval_id) > 0",
        name="ck_project_delivery_approvals_approval_id",
    ),
    CheckConstraint(
        "length(project_id) > 0",
        name="ck_project_delivery_approvals_project_id",
    ),
    CheckConstraint(
        "length(process_id) > 0",
        name="ck_project_delivery_approvals_process_id",
    ),
    CheckConstraint(
        "length(delivery_id) > 0",
        name="ck_project_delivery_approvals_delivery_id",
    ),
    CheckConstraint(
        "length(contract_id) > 0",
        name="ck_project_delivery_approvals_contract_id",
    ),
    CheckConstraint(
        "contract_version >= 1",
        name="ck_project_delivery_approvals_contract_version",
    ),
    CheckConstraint(
        "length(actor_id) > 0",
        name="ck_project_delivery_approvals_actor_id",
    ),
    CheckConstraint(
        "decision IN ('ACCEPT','REJECT')",
        name="ck_project_delivery_approvals_decision",
    ),
    CheckConstraint(
        "length(decision_key) > 0",
        name="ck_project_delivery_approvals_decision_key",
    ),
    CheckConstraint(
        "length(decision_digest) = 64",
        name="ck_project_delivery_approvals_decision_digest",
    ),
    CheckConstraint(
        "length(reason) > 0",
        name="ck_project_delivery_approvals_reason",
    ),
    CheckConstraint(
        "expected_delivery_version >= 1",
        name="ck_project_delivery_approvals_expected_delivery_version",
    ),
    CheckConstraint(
        "expected_process_version >= 1",
        name="ck_project_delivery_approvals_expected_process_version",
    ),
    UniqueConstraint(
        "delivery_id",
        "actor_id",
        name="uq_project_delivery_approvals_delivery_actor",
    ),
)
