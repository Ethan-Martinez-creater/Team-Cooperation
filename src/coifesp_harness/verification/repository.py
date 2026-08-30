from __future__ import annotations

from sqlalchemy import (
    JSON,
    CheckConstraint,
    Column,
    DateTime,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    UniqueConstraint,
)

# Verification evidence is deliberately isolated from the process, product,
# and run metadata domains.  Ownership and lifecycle checks are service-level
# validations; this table must remain usable with a ProjectProcessRepository
# connection without introducing cross-metadata foreign keys.
VERIFICATION_METADATA = MetaData()

TASK_VERIFICATIONS = Table(
    "task_verifications",
    VERIFICATION_METADATA,
    Column("verification_id", String(128), primary_key=True),
    Column("project_id", String(128), nullable=False),
    Column("process_id", String(128), nullable=False),
    Column("task_id", String(128), nullable=False),
    Column("source_run_id", String(128), nullable=False),
    Column("contract_version", Integer, nullable=False),
    Column("subject_digest", String(64), nullable=False),
    Column("policy_json", JSON(none_as_null=True), nullable=False),
    Column("artifacts_json", JSON(none_as_null=True), nullable=False),
    Column("checks_json", JSON(none_as_null=True), nullable=False),
    Column("status", String(16), nullable=False),
    Column("initiated_by", String(256), nullable=False),
    Column("executed_as", String(256), nullable=False),
    Column("version", Integer, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    Column("completed_at", DateTime(timezone=True), nullable=True),
    CheckConstraint(
        "contract_version >= 1",
        name="ck_task_verifications_contract_version",
    ),
    CheckConstraint(
        "length(subject_digest) = 64",
        name="ck_task_verifications_subject_digest",
    ),
    CheckConstraint(
        "status IN ('PENDING','PASS','FAIL','STALE')",
        name="ck_task_verifications_status",
    ),
    CheckConstraint(
        "version >= 1",
        name="ck_task_verifications_version",
    ),
    CheckConstraint(
        "(status = 'PENDING' AND completed_at IS NULL) OR "
        "(status IN ('PASS','FAIL','STALE') AND completed_at IS NOT NULL)",
        name="ck_task_verifications_completion",
    ),
    UniqueConstraint(
        "source_run_id",
        "subject_digest",
        name="uq_task_verifications_source_subject",
    ),
)

Index(
    "ix_task_verifications_project_task_created",
    TASK_VERIFICATIONS.c.project_id,
    TASK_VERIFICATIONS.c.task_id,
    TASK_VERIFICATIONS.c.created_at,
)
Index(
    "ix_task_verifications_status_created",
    TASK_VERIFICATIONS.c.status,
    TASK_VERIFICATIONS.c.created_at,
)
