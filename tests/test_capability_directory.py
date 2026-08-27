import pytest
import asyncio
import httpx
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from coifesp_harness.audit import AuditEvent
from coifesp_harness.capabilities import CapabilityDirectoryService, SQLAlchemyCapabilityRepository
from coifesp_harness.errors import GovernanceConflictError, PolicyDenied
from coifesp_harness.postgres_audit import AuditSigningKeyring, SQLAlchemyAuditLog
from coifesp_harness.security import Classification, Principal
from coifesp_harness.auth import VerifiedIdentity
from coifesp_harness.config import Settings
from coifesp_harness.control_plane import create_app
from datetime import UTC, datetime, timedelta


def stack():
    engine = create_engine("sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False}, poolclass=StaticPool)
    audit = SQLAlchemyAuditLog(engine=engine, keyring=AuditSigningKeyring(
        active_key_id="audit-v1", verification_keys={"audit-v1": b"a" * 32}))
    audit.create_schema()
    repository = SQLAlchemyCapabilityRepository(engine=engine, audit_log=audit)
    repository.create_schema()
    return engine, audit, CapabilityDirectoryService(repository)


class Verifier:
    def __init__(self, identities):
        self.identities = identities

    async def verify(self, token):
        return self.identities[token]


def verified(principal):
    return VerifiedIdentity(principal=principal, issuer="https://id.example.test",
        audience="control", expires_at=datetime.now(UTC) + timedelta(minutes=5), token_id="id")


async def api_request(app, method, path, **kwargs):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
                                base_url="https://control.example.test") as client:
        return await client.request(method, path, **kwargs)


def publish(service, principal, **overrides):
    values = dict(idempotency_key="publish-1", capability_id="doc-review", version="1.0.0",
        name="Document review", description="Reviews disclosed documents against a contract",
        tags=("office", "review"), protocols=("a2a-1.0",),
        input_contract="urn:contract:doc-review-input:v1",
        output_contract="urn:contract:doc-review-output:v1",
        max_input_classification=Classification.CONFIDENTIAL,
        required_compartments=("program-1",), residency_regions=("cn-north",),
        visible_to_tenants=("team-a", "team-b"))
    values.update(overrides)
    return service.publish(principal=principal, **values)


def test_publication_is_versioned_idempotent_audited_and_secret_safe():
    _, audit, service = stack()
    publisher = Principal("lead-a", "team-a", roles=frozenset({"capability_publisher"}),
        clearance=Classification.RESTRICTED, compartments=frozenset({"program-1"}))
    first = publish(service, publisher)
    second = publish(service, publisher)
    assert not first.duplicate and second.duplicate
    assert first.capability.content_digest == second.capability.content_digest
    assert audit.verify_tenant_chain("team-a") == 1
    with pytest.raises(GovernanceConflictError):
        publish(service, publisher, name="Different")
    with pytest.raises(PolicyDenied, match="secret"):
        publish(service, publisher, idempotency_key="publish-secret", version="1.0.1",
            description="api_key=sk-" + "x" * 32)


def test_discovery_hides_other_tenants_and_enforces_clearance_and_compartments():
    _, _, service = stack()
    publisher = Principal("lead-a", "team-a", roles=frozenset({"capability_publisher"}),
        clearance=Classification.RESTRICTED, compartments=frozenset({"program-1"}))
    publish(service, publisher)
    eligible = Principal("lead-b", "team-b", clearance=Classification.CONFIDENTIAL,
        compartments=frozenset({"program-1"}))
    assert [x.capability_id for x in service.discover(principal=eligible, protocol="a2a-1.0")] == ["doc-review"]
    assert service.discover(principal=Principal("low", "team-b", clearance=Classification.INTERNAL,
        compartments=frozenset({"program-1"}))) == ()
    assert service.discover(principal=Principal("wrong", "team-b", clearance=Classification.RESTRICTED)) == ()
    assert service.discover(principal=Principal("outsider", "team-c", clearance=Classification.RESTRICTED,
        compartments=frozenset({"program-1"}))) == ()
    with pytest.raises(PolicyDenied):
        publish(service, Principal("user", "team-a"))


def test_capability_api_requires_oidc_role_and_accepts_named_classification():
    _, _, service = stack()
    publisher = Principal("lead-a", "team-a", roles=frozenset({"capability_publisher"}),
        clearance=Classification.RESTRICTED, compartments=frozenset({"program-1"}))
    reader = Principal("lead-b", "team-b", clearance=Classification.CONFIDENTIAL,
        compartments=frozenset({"program-1"}))
    settings = Settings.from_environment({
        "COIFESP_ENV": "test", "COIFESP_OIDC_ISSUER": "https://id.example.test",
        "COIFESP_OIDC_AUDIENCE": "control", "COIFESP_OIDC_AUTHORIZED_PARTIES": "client",
        "COIFESP_OIDC_JWKS_URL": "https://id.example.test/jwks",
    })
    app = create_app(settings=settings, verifier=Verifier({"publisher": verified(publisher),
        "reader": verified(reader)}), capability_service=service)
    body = {"capability_id": "doc-review", "version": "1.0.0", "name": "Document review",
        "description": "Reviews disclosed documents", "tags": ["review"],
        "protocols": ["a2a-1.0"], "input_contract": "urn:input:v1",
        "output_contract": "urn:output:v1", "max_input_classification": "confidential",
        "required_compartments": ["program-1"], "residency_regions": ["cn-north"],
        "visible_to_tenants": ["team-a", "team-b"]}
    created = asyncio.run(api_request(app, "POST", "/v1/capabilities", json=body,
        headers={"Authorization": "Bearer publisher", "Idempotency-Key": "publish-1"}))
    listed = asyncio.run(api_request(app, "GET", "/v1/capabilities?protocol=a2a-1.0",
        headers={"Authorization": "Bearer reader"}))
    assert created.status_code == 201, created.text
    assert created.json()["capability"]["max_input_classification"] == "confidential"
    assert listed.status_code == 200 and [item["capability_id"] for item in listed.json()] == ["doc-review"]


def test_capacity_is_versioned_audited_and_match_is_explainable():
    _, audit, service = stack()
    publisher = Principal("lead-a", "team-a", roles=frozenset({"capability_publisher"}),
        clearance=Classification.RESTRICTED, compartments=frozenset({"program-1"}))
    publish(service, publisher)
    valid = datetime.now(UTC) + timedelta(hours=1)
    capacity = service.declare_capacity(principal=publisher, provider_tenant_id="team-a",
        capability_id="doc-review", version="1.0.0", status="available",
        available_slots=7, valid_until=valid)
    assert capacity.state_version == 1
    with pytest.raises(GovernanceConflictError, match="stale"):
        service.declare_capacity(principal=publisher, provider_tenant_id="team-a",
            capability_id="doc-review", version="1.0.0", status="limited",
            available_slots=2, valid_until=valid)
    reader = Principal("lead-b", "team-b", clearance=Classification.CONFIDENTIAL,
        compartments=frozenset({"program-1"}))
    matched = service.match(principal=reader, required_tags=("office", "review"),
        protocol="a2a-1.0", input_classification=Classification.CONFIDENTIAL,
        compartments=("program-1",), residency_regions=("cn-north",))
    assert matched[0].score == 87
    assert "tag_overlap=2" in matched[0].reasons
    assert audit.verify_tenant_chain("team-a") == 2


def test_match_fails_closed_without_fresh_capacity_or_matching_residency():
    _, _, service = stack()
    publisher = Principal("lead-a", "team-a", roles=frozenset({"capability_publisher"}),
        clearance=Classification.RESTRICTED, compartments=frozenset({"program-1"}))
    publish(service, publisher)
    reader = Principal("lead-b", "team-b", clearance=Classification.CONFIDENTIAL,
        compartments=frozenset({"program-1"}))
    arguments = dict(principal=reader, required_tags=("review",), protocol="a2a-1.0",
        input_classification=Classification.CONFIDENTIAL, compartments=("program-1",),
        residency_regions=("cn-north",))
    assert service.match(**arguments) == ()
    service.declare_capacity(principal=publisher, provider_tenant_id="team-a",
        capability_id="doc-review", version="1.0.0", status="available",
        available_slots=1, valid_until=datetime.now(UTC) + timedelta(minutes=5))
    arguments["residency_regions"] = ("eu-west",)
    assert service.match(**arguments) == ()


def test_capacity_reservations_prevent_overbooking_and_release_slots():
    _, _, service = stack()
    publisher = Principal("lead-a", "team-a", roles=frozenset({"capability_publisher"}),
        clearance=Classification.RESTRICTED, compartments=frozenset({"program-1"}))
    publish(service, publisher)
    service.declare_capacity(principal=publisher, provider_tenant_id="team-a",
        capability_id="doc-review", version="1.0.0", status="available",
        available_slots=2, valid_until=datetime.now(UTC) + timedelta(hours=1))
    consumer = Principal("lead-b", "team-b", clearance=Classification.CONFIDENTIAL,
        compartments=frozenset({"program-1"}))
    expires = datetime.now(UTC) + timedelta(minutes=30)
    reservation = service.reserve(principal=consumer, reservation_id="reserve-1",
        provider_tenant_id="team-a", capability_id="doc-review", version="1.0.0",
        slots=2, expires_at=expires)
    assert reservation.status == "active"
    assert service.reserve(principal=consumer, reservation_id="reserve-1",
        provider_tenant_id="team-a", capability_id="doc-review", version="1.0.0",
        slots=2, expires_at=expires).status == "active"
    assert service.match(principal=consumer, required_tags=("review",),
        protocol="a2a-1.0", input_classification=Classification.CONFIDENTIAL,
        compartments=("program-1",), residency_regions=("cn-north",)) == ()
    with pytest.raises(GovernanceConflictError, match="conflicts"):
        service.reserve(principal=consumer, reservation_id="reserve-2",
            provider_tenant_id="team-a", capability_id="doc-review", version="1.0.0",
            slots=1, expires_at=expires)
    assert service.release_reservation(principal=consumer, provider_tenant_id="team-a",
        reservation_id="reserve-1").status == "released"
    assert service.reserve(principal=consumer, reservation_id="reserve-2",
        provider_tenant_id="team-a", capability_id="doc-review", version="1.0.0",
        slots=1, expires_at=expires).status == "active"


def test_capacity_conflict_negotiation_has_bilateral_roles_and_versions():
    _, _, service = stack()
    provider = Principal("lead-a", "team-a", roles=frozenset({"capability_publisher"}),
        clearance=Classification.RESTRICTED, compartments=frozenset({"program-1"}))
    publish(service, provider)
    consumer = Principal("lead-b", "team-b", clearance=Classification.CONFIDENTIAL,
        compartments=frozenset({"program-1"}))
    start = datetime.now(UTC) + timedelta(hours=1)
    value = service.propose_negotiation(principal=consumer, negotiation_id="negotiate-1",
        provider_tenant_id="team-a", capability_id="doc-review", version="1.0.0",
        requested_slots=3, earliest_start=start, latest_end=start + timedelta(hours=2),
        reason="Need alternative review window")
    assert value.status == "proposed" and value.state_version == 1
    with pytest.raises(GovernanceConflictError, match="transition"):
        service.decide_negotiation(principal=consumer, provider_tenant_id="team-a",
            negotiation_id="negotiate-1", expected_version=1, decision="accepted",
            reason="self accept")
    accepted = service.decide_negotiation(principal=provider,
        provider_tenant_id="team-a", negotiation_id="negotiate-1",
        expected_version=1, decision="accepted", reason="Capacity available in window")
    assert accepted.status == "accepted" and accepted.state_version == 2
    with pytest.raises(GovernanceConflictError, match="transition"):
        service.decide_negotiation(principal=provider, provider_tenant_id="team-a",
            negotiation_id="negotiate-1", expected_version=1, decision="rejected",
            reason="stale")
