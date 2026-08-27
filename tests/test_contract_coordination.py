from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, insert, select
from sqlalchemy.pool import StaticPool

from coifesp_harness.collaboration.repository import (
    GOVERNANCE_ASSIGNMENTS,
    GOVERNANCE_MEMBERS,
    GOVERNANCE_PLANS,
    GOVERNANCE_PROGRAMS,
    SQLAlchemyGovernanceRepository,
)
from coifesp_harness.contracts import (
    Compatibility,
    ContractCoordinationService,
    ContractKind,
    ImpactState,
    SQLAlchemyContractRepository,
)
from coifesp_harness.contracts.repository import CHANGE_IMPACTS, CONTRACT_EVENTS, CONTRACT_OUTBOX
from coifesp_harness.errors import GovernanceError, PolicyDenied, ResourceNotFound
from coifesp_harness.postgres_audit import AuditSigningKeyring, SQLAlchemyAuditLog
from coifesp_harness.security import Principal


def setup_services():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    audit = SQLAlchemyAuditLog(
        engine=engine,
        keyring=AuditSigningKeyring(
            active_key_id="audit-v1", verification_keys={"audit-v1": b"k" * 32}
        ),
    )
    audit.create_schema()
    governance = SQLAlchemyGovernanceRepository(engine=engine, audit_log=audit)
    governance.create_schema()
    repository = SQLAlchemyContractRepository(engine=engine, audit_log=audit)
    repository.create_schema()
    now = datetime.now(UTC)
    with engine.begin() as connection:
        connection.execute(
            insert(GOVERNANCE_PROGRAMS).values(
                program_id="program-1",
                owner_tenant_id="team-a",
                title="delivery",
                objective="coordinate",
                classification=2,
                compartments=["program-1"],
                participant_tenant_ids=["team-a", "team-b", "team-c"],
                aggregate_version=1,
                last_event_sequence=0,
                created_by="lead-a",
                created_at=now,
                updated_at=now,
            )
        )
        for principal, tenant, role in (
            ("lead-a", "team-a", "lead"),
            ("producer-a", "team-a", "contributor"),
            ("lead-b", "team-b", "lead"),
            ("consumer-b", "team-b", "contributor"),
            ("consumer-c", "team-c", "contributor"),
        ):
            connection.execute(
                insert(GOVERNANCE_MEMBERS).values(
                    program_id="program-1",
                    principal_id=principal,
                    tenant_id=tenant,
                    role=role,
                    added_by="lead-a",
                    visible_to_tenants=["team-a", "team-b", "team-c"],
                    joined_at=now,
                )
            )
        connection.execute(
            insert(GOVERNANCE_PLANS).values(
                program_id="program-1",
                plan_id="unused",
                version=1,
                title="approved",
                objective="test",
                lead_id="lead-a",
                content_digest="a" * 64,
                state="approved",
                visible_to_tenants=["team-a", "team-b", "team-c"],
                created_at=now,
                updated_at=now,
            )
        )
        for assignment, assignee, visibility in (
            ("producer-task", "producer-a", ["team-a", "team-b"]),
            ("consumer-task", "consumer-b", ["team-a", "team-b"]),
            ("hidden-task", "consumer-c", ["team-a", "team-c"]),
        ):
            connection.execute(
                insert(GOVERNANCE_ASSIGNMENTS).values(
                    program_id="program-1",
                    assignment_id=assignment,
                    plan_id="unused",
                    plan_digest="a" * 64,
                    title=assignment,
                    description="",
                    deliverable_contract="contract",
                    proposed_by="lead-a",
                    assignee_id=assignee,
                    state="in_progress",
                    response_reason=None,
                    verification_note=None,
                    visible_to_tenants=visibility,
                    created_at=now,
                    updated_at=now,
                )
            )
    return engine, audit, repository, ContractCoordinationService(repository)


def bootstrap(service):
    lead_a = Principal("lead-a", "team-a")
    service.register_contract(
        principal=lead_a,
        idempotency_key="register",
        program_id="program-1",
        contract_id="orders-api",
        producer_assignment_id="producer-task",
        name="Orders API",
        kind=ContractKind.OPENAPI,
        visible_to_tenants=frozenset({"team-a", "team-b"}),
    )
    service.release(
        principal=lead_a,
        idempotency_key="release-1",
        program_id="program-1",
        contract_id="orders-api",
        version="1.0.0",
        content_digest="1" * 64,
        artifact_ref="artifact:orders-openapi-v1",
        compatibility=Compatibility.UNKNOWN,
        predecessor_version=None,
    )
    service.declare_dependency(
        principal=Principal("consumer-b", "team-b"),
        idempotency_key="dependency",
        program_id="program-1",
        dependency_id="orders-for-b",
        contract_id="orders-api",
        consumer_assignment_id="consumer-task",
        version_constraint="^1.0.0",
        baseline_version="1.0.0",
    )
    return lead_a


def test_breaking_release_creates_bilateral_impact_and_requires_consumer_acceptance():
    engine, audit, repository, service = setup_services()
    lead_a = bootstrap(service)
    result = service.release(
        principal=lead_a,
        idempotency_key="release-2",
        program_id="program-1",
        contract_id="orders-api",
        version="2.0.0",
        content_digest="2" * 64,
        artifact_ref="artifact:orders-openapi-v2",
        compatibility=Compatibility.COMPATIBLE,
        predecessor_version="1.0.0",
    )
    assert result.impacts_created == 1
    with repository.transaction("team-b") as connection:
        impact = repository.list_impacts(
            connection, program_id="program-1", assignment_id="consumer-task"
        )[0]
        assert impact.state is ImpactState.PENDING
        assert impact.compatibility is Compatibility.BREAKING
        with pytest.raises(GovernanceError, match="unresolved"):
            service.assert_assignment_verifiable(
                connection, program_id="program-1", assignment_id="consumer-task"
            )

    service.respond_to_impact(
        principal=Principal("consumer-b", "team-b"),
        idempotency_key="block",
        program_id="program-1",
        impact_id=impact.impact_id,
        block=True,
        note="client regeneration and migration are required",
    )
    with pytest.raises(PolicyDenied, match="consumer"):
        service.accept_impact(
            principal=lead_a,
            idempotency_key="producer-cannot-accept",
            program_id="program-1",
            impact_id=impact.impact_id,
            note="producer considers it complete",
        )
    service.propose_remediation(
        principal=lead_a,
        idempotency_key="remediation",
        program_id="program-1",
        impact_id=impact.impact_id,
        remediation="dual-run v1 and v2 through the consumer migration window",
    )
    service.accept_impact(
        principal=Principal("lead-b", "team-b"),
        idempotency_key="accept",
        program_id="program-1",
        impact_id=impact.impact_id,
        note="consumer migration plan approved",
    )
    with repository.transaction("team-b") as connection:
        service.assert_assignment_verifiable(
            connection, program_id="program-1", assignment_id="consumer-task"
        )
        assert repository.list_impacts(
            connection, program_id="program-1", assignment_id="consumer-task"
        )[0].state is ImpactState.ACCEPTED
    with engine.connect() as connection:
        assert len(connection.execute(select(CONTRACT_EVENTS)).all()) == 8
        assert len(connection.execute(select(CONTRACT_OUTBOX)).all()) == 16
    assert audit.verify_tenant_chain("team-a") == 5
    assert audit.verify_tenant_chain("team-b") == 3


def test_dependency_visibility_and_idempotency_are_fail_closed():
    _, _, _, service = setup_services()
    bootstrap(service)
    duplicate = service.register_contract(
        principal=Principal("lead-a", "team-a"),
        idempotency_key="register",
        program_id="program-1",
        contract_id="orders-api",
        producer_assignment_id="producer-task",
        name="Orders API",
        kind=ContractKind.OPENAPI,
        visible_to_tenants=frozenset({"team-a", "team-b"}),
    )
    assert duplicate.duplicate
    with pytest.raises(ResourceNotFound, match="hidden"):
        service.declare_dependency(
            principal=Principal("consumer-c", "team-c"),
            idempotency_key="hidden-dependency",
            program_id="program-1",
            dependency_id="orders-for-c",
            contract_id="orders-api",
            consumer_assignment_id="hidden-task",
            version_constraint="^1.0.0",
            baseline_version="1.0.0",
        )
