"""Project-scoped capability/capacity adapter tests (A3)."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, insert, select, update
from sqlalchemy.pool import StaticPool

from coifesp_harness.capabilities import (
    CapabilityDirectoryService,
    SQLAlchemyCapabilityRepository,
)
from coifesp_harness.capabilities.repository import CAPABILITY_CAPACITY
from coifesp_harness.errors import GovernanceConflictError, PolicyDenied
from coifesp_harness.postgres_audit import (
    AUDIT_EVENTS,
    AuditSigningKeyring,
    SQLAlchemyAuditLog,
)
from coifesp_harness.product import (
    ProductAccountService,
    ProjectDirectoryService,
    ProjectTeamKind,
    TeamAccountRole,
)
from coifesp_harness.product.repository import PROJECT_TEAMS
from coifesp_harness.project_process import (
    ProjectCapabilityAdapter,
    ProjectCapabilityRequirement,
)
from coifesp_harness.security import Classification, Principal

NOW = datetime.now(UTC).replace(microsecond=0)


def _stack():
    engine = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    accounts = ProductAccountService(engine)
    accounts.create_schema()
    for team_id, handle in (("team-a", "team-a"), ("team-b", "team-b"), ("team-c", "team-c")):
        accounts.register_team(team_id=team_id, team_handle=handle, team_name=handle.upper())
    lead = accounts.ensure_active_account(
        account_id="lead-a",
        username="lead-a",
        display_name="Lead A",
        email="lead-a@example.invalid",
        team_id="team-a",
        team_role=TeamAccountRole.ADMIN,
    )
    providers = {}
    for team_id in ("team-b", "team-c"):
        providers[team_id] = accounts.ensure_active_account(
            account_id=f"lead-{team_id[-1]}",
            username=f"lead-{team_id[-1]}",
            display_name=f"Lead {team_id[-1].upper()}",
            email=f"lead-{team_id[-1]}@example.invalid",
            team_id=team_id,
            team_role=TeamAccountRole.ADMIN,
        )
    project = ProjectDirectoryService(engine).create_project(
        project_id="project-a",
        name="Project A",
        description="A structured project",
        actor_id=lead.account_id,
        owner_assignment_name="Owner",
        owner_kind=ProjectTeamKind.PRODUCT,
    )
    # The adapter checks this authoritative table directly.  This fixture
    # inserts the participation after normal project creation so that team-b
    # is a valid provider and team-c remains a registered outsider.
    with engine.begin() as connection:
        connection.execute(
            insert(PROJECT_TEAMS).values(
                project_id=project.project_id,
                team_id="team-b",
                name="Provider",
                kind=ProjectTeamKind.ENGINEERING.value,
                assigned_by=lead.account_id,
                created_at=NOW,
            )
        )

    audit = SQLAlchemyAuditLog(
        engine=engine,
        keyring=AuditSigningKeyring(active_key_id="audit-v1", verification_keys={"audit-v1": b"a" * 32}),
    )
    audit.create_schema()
    repository = SQLAlchemyCapabilityRepository(engine=engine, audit_log=audit)
    repository.create_schema()
    directory = CapabilityDirectoryService(repository)
    adapter = ProjectCapabilityAdapter(repository, clock=lambda: NOW)
    return engine, accounts, directory, adapter, providers


def _publisher(account):
    return Principal(
        account.account_id,
        account.team_id,
        roles=frozenset({"capability_publisher"}),
        clearance=Classification.RESTRICTED,
        compartments=frozenset({"program-1"}),
    )


def _orchestrator(*, team_id="team-a", clearance=Classification.RESTRICTED, compartments=("program-1",)):
    return Principal(
        "service:project-orchestrator",
        team_id,
        roles=frozenset({"project_orchestrator"}),
        clearance=clearance,
        compartments=frozenset(compartments),
        is_service=True,
    )


def _requirement(**changes):
    values = {
        "project_id": "project-a",
        "consumer_team_id": "team-a",
        "target_team_id": "team-b",
        "tags": ("review",),
        "protocol": "a2a-1.0",
        "input_classification": Classification.CONFIDENTIAL,
        "compartments": ("program-1",),
        "residency": ("cn-north",),
        "slots": 1,
    }
    values.update(changes)
    return ProjectCapabilityRequirement(**values)


def _publish(directory, provider, *, capability_id="doc-review", version="1.0.0", **changes):
    values = {
        "idempotency_key": f"publish-{provider.tenant_id}-{capability_id}-{version}",
        "capability_id": capability_id,
        "version": version,
        "name": "Document review",
        "description": "Reviews project documents",
        "tags": ("review", "office"),
        "protocols": ("a2a-1.0",),
        "input_contract": "urn:contract:doc-review-input:v1",
        "output_contract": "urn:contract:doc-review-output:v1",
        "max_input_classification": Classification.CONFIDENTIAL,
        "required_compartments": ("program-1",),
        "residency_regions": ("cn-north",),
        "visible_to_tenants": ("team-a", provider.tenant_id),
    }
    values.update(changes)
    return directory.publish(principal=provider, **values)


def _declare(directory, provider, capability, *, slots=3, valid_until=None, status="available"):
    return directory.declare_capacity(
        principal=provider,
        provider_tenant_id=provider.tenant_id,
        capability_id=capability.capability_id,
        version=capability.version,
        status=status,
        available_slots=slots,
        valid_until=valid_until or NOW + timedelta(hours=1),
    )


def test_match_requires_project_participants_and_explicit_provider_target():
    _, _, directory, adapter, providers = _stack()
    capability = _publish(directory, _publisher(providers["team-b"]))
    _declare(directory, _publisher(providers["team-b"]), capability.capability)

    matches = adapter.match(principal=_orchestrator(), requirement=_requirement())
    assert [item.capability_id for item in matches] == ["doc-review"]
    assert matches[0].provider_tenant_id == "team-b"

    with pytest.raises(PolicyDenied, match="participate"):
        adapter.match(
            principal=_orchestrator(),
            requirement=_requirement(target_team_id="team-c"),
        )

    # A capability from another provider cannot be selected by changing the
    # natural-language target after the fact; the explicit target is required.
    outsider = _publisher(providers["team-c"])
    outsider_capability = _publish(directory, outsider, capability_id="outsider-review")
    _declare(directory, outsider, outsider_capability.capability)
    assert adapter.match(principal=_orchestrator(), requirement=_requirement()) == matches


def test_match_applies_visibility_protocol_tags_classification_compartments_residency_and_capacity():
    engine, _, directory, adapter, providers = _stack()
    publisher = _publisher(providers["team-b"])
    valid = _publish(directory, publisher, capability_id="valid")
    _declare(directory, publisher, valid.capability, slots=2)
    hidden = _publish(directory, publisher, capability_id="hidden", visible_to_tenants=("team-b",))
    _declare(directory, publisher, hidden.capability)
    wrong_protocol = _publish(directory, publisher, capability_id="protocol", protocols=("mcp",))
    _declare(directory, publisher, wrong_protocol.capability)
    wrong_tags = _publish(directory, publisher, capability_id="tags", tags=("office",))
    _declare(directory, publisher, wrong_tags.capability)
    wrong_class = _publish(
        directory,
        publisher,
        capability_id="classification",
        max_input_classification=Classification.INTERNAL,
    )
    _declare(directory, publisher, wrong_class.capability)
    wrong_compartment = _publish(
        directory,
        publisher,
        capability_id="compartment",
        required_compartments=("other-program",),
    )
    _declare(directory, publisher, wrong_compartment.capability)
    wrong_residency = _publish(
        directory,
        publisher,
        capability_id="residency",
        residency_regions=("eu-west",),
    )
    _declare(directory, publisher, wrong_residency.capability)
    expired = _publish(directory, publisher, capability_id="expired")
    _declare(directory, publisher, expired.capability)
    with engine.begin() as connection:
        connection.execute(
            update(CAPABILITY_CAPACITY)
            .where(
                CAPABILITY_CAPACITY.c.provider_tenant_id == "team-b",
                CAPABILITY_CAPACITY.c.capability_id == "expired",
            )
            .values(valid_until=NOW - timedelta(seconds=1))
        )
    short = _publish(directory, publisher, capability_id="short")
    _declare(directory, publisher, short.capability, slots=1)

    matches = adapter.match(principal=_orchestrator(), requirement=_requirement(slots=2))
    assert [item.capability_id for item in matches] == ["valid"]

    # Capacity visibility is an independent persisted boundary from the
    # capability's visibility list and must be checked as well.
    with engine.begin() as connection:
        connection.execute(
            update(CAPABILITY_CAPACITY)
            .where(
                CAPABILITY_CAPACITY.c.provider_tenant_id == "team-b",
                CAPABILITY_CAPACITY.c.capability_id == "valid",
            )
            .values(visible_to_tenants=["team-b"])
        )
    assert adapter.match(principal=_orchestrator(), requirement=_requirement(slots=2)) == ()


def test_match_sorting_is_stable_and_score_is_explainable():
    _, _, directory, adapter, providers = _stack()
    publisher = _publisher(providers["team-b"])
    first = _publish(directory, publisher, capability_id="first")
    _declare(directory, publisher, first.capability, slots=1)
    second = _publish(directory, publisher, capability_id="second")
    _declare(directory, publisher, second.capability, slots=4)

    requirement = _requirement(slots=1)
    one = adapter.match(principal=_orchestrator(), requirement=requirement)
    two = adapter.match(principal=_orchestrator(), requirement=requirement)
    assert [item.capability_id for item in one] == ["second", "first"]
    assert [item.capability_id for item in two] == ["second", "first"]
    assert one[0].score > one[1].score
    assert "available_slots=4" in one[0].reasons


@pytest.mark.parametrize(
    "principal",
    [
        Principal("human", "team-a"),
        Principal("service:other", "team-a", roles=frozenset({"project_orchestrator"}), is_service=True),
        Principal("service:project-orchestrator", "team-a", is_service=True),
        Principal("service:project-orchestrator", "team-b", roles=frozenset({"project_orchestrator"}), is_service=True),
    ],
)
def test_only_consumer_scoped_project_orchestrator_identity_is_accepted(principal):
    _, _, directory, adapter, providers = _stack()
    capability = _publish(directory, _publisher(providers["team-b"]))
    _declare(directory, _publisher(providers["team-b"]), capability.capability)
    with pytest.raises(PolicyDenied):
        adapter.match(principal=principal, requirement=_requirement())


def test_clearance_and_compartment_boundaries_are_checked_before_matching():
    _, _, directory, adapter, providers = _stack()
    capability = _publish(directory, _publisher(providers["team-b"]))
    _declare(directory, _publisher(providers["team-b"]), capability.capability)
    with pytest.raises(PolicyDenied, match="clearance"):
        adapter.match(
            principal=_orchestrator(clearance=Classification.INTERNAL),
            requirement=_requirement(),
        )
    with pytest.raises(PolicyDenied, match="compartment"):
        adapter.match(
            principal=_orchestrator(compartments=()),
            requirement=_requirement(),
        )


def test_reserve_revalidates_fresh_capacity_is_idempotent_and_audited_without_payload():
    engine, _, directory, adapter, providers = _stack()
    publisher = _publisher(providers["team-b"])
    published = _publish(directory, publisher, capability_id="reserved")
    _declare(directory, publisher, published.capability, slots=1)
    requirement = _requirement()
    match = adapter.match(principal=_orchestrator(), requirement=requirement)[0]
    expiry = NOW + timedelta(minutes=10)

    first = adapter.reserve(
        principal=_orchestrator(),
        requirement=requirement,
        match=match,
        reservation_id="reservation-1",
        expires_at=expiry,
    )
    second = adapter.reserve(
        principal=_orchestrator(),
        requirement=requirement,
        match=match,
        reservation_id="reservation-1",
        expires_at=expiry,
    )
    assert first == second
    assert first.status == "active"
    assert adapter.repository.audit_log.verify_tenant_chain("team-a") == 1
    with engine.connect() as connection:
        payload = connection.execute(
            select(AUDIT_EVENTS.c.payload).where(
                AUDIT_EVENTS.c.tenant_id == "team-a",
                AUDIT_EVENTS.c.event_type == "project.capacity.reserved",
            )
        ).scalar_one()
    assert "program-1" not in payload
    assert "review" not in payload

    with pytest.raises(GovernanceConflictError):
        adapter.reserve(
            principal=_orchestrator(),
            requirement=requirement,
            match=match,
            reservation_id="reservation-1",
            expires_at=NOW + timedelta(minutes=20),
        )


def test_reserve_rejects_stale_match_and_does_not_oversubscribe():
    _, _, directory, adapter, providers = _stack()
    publisher = _publisher(providers["team-b"])
    published = _publish(directory, publisher, capability_id="capacity")
    _declare(directory, publisher, published.capability, slots=1)
    requirement = _requirement()
    match = adapter.match(principal=_orchestrator(), requirement=requirement)[0]
    expiry = NOW + timedelta(minutes=10)
    adapter.reserve(
        principal=_orchestrator(),
        requirement=requirement,
        match=match,
        reservation_id="reservation-a",
        expires_at=expiry,
    )
    with pytest.raises(GovernanceConflictError, match="capacity"):
        adapter.reserve(
            principal=_orchestrator(),
            requirement=requirement,
            match=match,
            reservation_id="reservation-b",
            expires_at=expiry,
        )

    # A fresh match with a changed capacity state cannot be used as if the old
    # snapshot were still authoritative.
    _, _, directory2, adapter2, providers2 = _stack()
    publisher2 = _publisher(providers2["team-b"])
    published2 = _publish(directory2, publisher2, capability_id="stale")
    declared = _declare(directory2, publisher2, published2.capability, slots=2)
    requirement2 = _requirement()
    match2 = adapter2.match(principal=_orchestrator(), requirement=requirement2)[0]
    directory2.declare_capacity(
        principal=publisher2,
        provider_tenant_id="team-b",
        capability_id="stale",
        version="1.0.0",
        status="available",
        available_slots=2,
        valid_until=NOW + timedelta(hours=1),
        expected_version=declared.state_version,
    )
    with pytest.raises(GovernanceConflictError, match="changed"):
        adapter2.reserve(
            principal=_orchestrator(),
            requirement=requirement2,
            match=match2,
            reservation_id="reservation-stale",
            expires_at=expiry,
        )


def test_requirement_is_structured_and_does_not_accept_prose_substitutes():
    _, _, _, adapter, _ = _stack()
    with pytest.raises(TypeError):
        adapter.match(principal=_orchestrator(), requirement={"title": "review documents"})
    with pytest.raises(TypeError):
        adapter.match(
            principal=_orchestrator(),
            requirement=_requirement(tags=["review"]),
        )


def test_project_capacity_release_is_idempotent_and_restores_match_capacity():
    _, _, directory, adapter, providers = _stack()
    publisher = _publisher(providers["team-b"])
    capability = _publish(directory, publisher).capability
    _declare(directory, publisher, capability, slots=1)
    requirement = _requirement()
    match = adapter.match(principal=_orchestrator(), requirement=requirement)[0]
    adapter.reserve(principal=_orchestrator(), requirement=requirement,
                    match=match, reservation_id="release-me")
    assert adapter.match(principal=_orchestrator(), requirement=requirement) == ()
    first = adapter.release(principal=_orchestrator(), requirement=requirement,
                            reservation_id="release-me")
    repeated = adapter.release(principal=_orchestrator(), requirement=requirement,
                               reservation_id="release-me")
    assert first == repeated and first.status == "released"
    assert len(adapter.match(principal=_orchestrator(), requirement=requirement)) == 1


def test_shared_connection_reservation_never_commits_outside_callers_transaction():
    engine, _, directory, adapter, providers = _stack()
    publisher = _publisher(providers["team-b"])
    capability = _publish(directory, publisher).capability
    _declare(directory, publisher, capability, slots=1)
    requirement = _requirement()
    with pytest.raises(RuntimeError, match="rollback"):
        with engine.begin() as connection:
            # Force SQLite's outer transaction before the adapter savepoint.
            connection.execute(CAPABILITY_CAPACITY.update().values(status="available"))
            bound = adapter.using_connection(connection)
            match = bound.match(principal=_orchestrator(), requirement=requirement)[0]
            bound.reserve(principal=_orchestrator(), requirement=requirement,
                          match=match, reservation_id="rollback-me")
            raise RuntimeError("rollback")
    assert len(adapter.match(principal=_orchestrator(), requirement=requirement)) == 1
