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
    Text,
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


AGENT_REVIEWS = Table(
    "task_agent_reviews",
    VERIFICATION_METADATA,
    Column("review_id", String(128), primary_key=True),
    Column("verification_id", String(128), nullable=False),
    Column("source_run_id", String(128), nullable=False),
    Column("run_id", String(128), nullable=False),
    Column("project_id", String(128), nullable=False),
    Column("process_id", String(128), nullable=False),
    Column("task_id", String(128), nullable=False),
    Column("owner_team_id", String(128), nullable=False),
    Column("criterion_id", Text, nullable=False),
    Column("criterion_key", String(64), nullable=False),
    Column("subject_digest", String(64), nullable=False),
    Column("contract_version", Integer, nullable=False),
    Column("attempt", Integer, nullable=False),
    Column("budget_reservation_id", String(128), nullable=False),
    Column("status", String(16), nullable=False),
    Column("result_json", JSON(none_as_null=True), nullable=True),
    Column("error_code", String(128), nullable=True),
    Column("initiated_by", String(256), nullable=False),
    Column("executed_as", String(256), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    Column("completed_at", DateTime(timezone=True), nullable=True),
    CheckConstraint(
        "length(criterion_id) > 0",
        name="ck_task_agent_reviews_criterion_id",
    ),
    CheckConstraint(
        "length(criterion_key) = 64",
        name="ck_task_agent_reviews_criterion_key",
    ),
    CheckConstraint(
        "length(subject_digest) = 64",
        name="ck_task_agent_reviews_subject_digest",
    ),
    CheckConstraint(
        "contract_version >= 1",
        name="ck_task_agent_reviews_contract_version",
    ),
    CheckConstraint(
        "attempt >= 1",
        name="ck_task_agent_reviews_attempt",
    ),
    CheckConstraint(
        "status IN ('QUEUED','PASS','FAIL','UNAVAILABLE','STALE')",
        name="ck_task_agent_reviews_status",
    ),
    CheckConstraint(
        "(status = 'QUEUED' AND completed_at IS NULL) OR "
        "(status IN ('PASS','FAIL','UNAVAILABLE','STALE') AND completed_at IS NOT NULL)",
        name="ck_task_agent_reviews_completion",
    ),
    CheckConstraint(
        "status NOT IN ('PASS','FAIL') OR result_json IS NOT NULL",
        name="ck_task_agent_reviews_result",
    ),
    UniqueConstraint("run_id", name="uq_task_agent_reviews_run_id"),
    UniqueConstraint(
        "budget_reservation_id",
        name="uq_task_agent_reviews_budget_reservation_id",
    ),
    UniqueConstraint(
        "verification_id",
        "criterion_key",
        "attempt",
        name="uq_task_agent_reviews_verification_criterion_attempt",
    ),
)

Index(
    "ix_task_agent_reviews_verification_criterion_attempt",
    AGENT_REVIEWS.c.verification_id,
    AGENT_REVIEWS.c.criterion_key,
    AGENT_REVIEWS.c.attempt,
)
Index(
    "ix_task_agent_reviews_status_created",
    AGENT_REVIEWS.c.status,
    AGENT_REVIEWS.c.created_at,
)
