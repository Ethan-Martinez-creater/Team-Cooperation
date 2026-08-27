from __future__ import annotations

import os
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import create_engine, text

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from coifesp_harness.capabilities import CapabilityDirectoryService, SQLAlchemyCapabilityRepository
from coifesp_harness.config import Settings
from coifesp_harness.postgres_audit import AuditSigningKeyring, SQLAlchemyAuditLog
from coifesp_harness.security import Classification, Principal


def main() -> None:
    load_dotenv(ROOT / ".env", override=False)
    settings = Settings.from_environment()
    if not settings.database_url:
        raise RuntimeError("database URL is missing")
    engine = create_engine(settings.database_url, hide_parameters=True)
    audit = SQLAlchemyAuditLog(engine=engine, keyring=AuditSigningKeyring.from_settings(settings))
    service = CapabilityDirectoryService(SQLAlchemyCapabilityRepository(engine=engine, audit_log=audit))
    suffix = uuid.uuid4().hex[:12]
    provider = f"cap-provider-{suffix}"
    consumer = f"cap-consumer-{suffix}"
    outsider = f"cap-outsider-{suffix}"
    capability_id = f"review-{suffix}"
    publisher = Principal("capability-check", provider,
        roles=frozenset({"capability_publisher"}), clearance=Classification.RESTRICTED,
        compartments=frozenset({"check"}))
    result = service.publish(principal=publisher, idempotency_key=f"publish-{suffix}",
        capability_id=capability_id, version="1.0.0", name="Capability check",
        description="Bounded contract-only interoperability check", tags=("check",),
        protocols=("a2a-1.0",), input_contract="urn:check:input:v1",
        output_contract="urn:check:output:v1",
        max_input_classification=Classification.CONFIDENTIAL,
        required_compartments=("check",), residency_regions=("local",),
        visible_to_tenants=(provider, consumer))
    eligible = Principal("consumer-check", consumer, clearance=Classification.CONFIDENTIAL,
        compartments=frozenset({"check"}))
    hidden = Principal("outsider-check", outsider, clearance=Classification.RESTRICTED,
        compartments=frozenset({"check"}))
    assert [item.capability_id for item in service.discover(principal=eligible)] == [capability_id]
    assert service.discover(principal=hidden) == ()
    service.declare_capacity(principal=publisher, provider_tenant_id=provider,
        capability_id=capability_id, version="1.0.0", status="available",
        available_slots=3, valid_until=datetime.now(UTC) + timedelta(minutes=15))
    matches = service.match(principal=eligible, required_tags=("check",),
        protocol="a2a-1.0", input_classification=Classification.CONFIDENTIAL,
        compartments=("check",), residency_regions=("local",))
    assert matches and matches[0].capability.capability_id == capability_id
    reservation = service.reserve(principal=eligible, reservation_id=f"reserve-{suffix}",
        provider_tenant_id=provider, capability_id=capability_id, version="1.0.0",
        slots=3, expires_at=datetime.now(UTC) + timedelta(minutes=10))
    assert reservation.status == "active"
    from coifesp_harness.errors import GovernanceConflictError
    try:
        service.reserve(principal=eligible, reservation_id=f"reserve-conflict-{suffix}",
            provider_tenant_id=provider, capability_id=capability_id, version="1.0.0",
            slots=1, expires_at=datetime.now(UTC) + timedelta(minutes=10))
    except GovernanceConflictError:
        pass
    else:
        raise AssertionError("capacity overbooking was accepted")
    assert audit.verify_tenant_chain(provider) == 2
    assert audit.verify_tenant_chain(consumer) == 1
    start = datetime.now(UTC) + timedelta(hours=1)
    negotiation = service.propose_negotiation(principal=eligible,
        negotiation_id=f"negotiate-{suffix}", provider_tenant_id=provider,
        capability_id=capability_id, version="1.0.0", requested_slots=2,
        earliest_start=start, latest_end=start + timedelta(hours=1),
        reason="request alternate capacity window")
    assert negotiation.status == "proposed"
    assert service.decide_negotiation(principal=publisher,
        provider_tenant_id=provider, negotiation_id=negotiation.negotiation_id,
        expected_version=1, decision="accepted", reason="window is available").status == "accepted"
    assert audit.verify_tenant_chain(provider) == 3
    assert audit.verify_tenant_chain(consumer) == 2
    with engine.connect() as connection:
        revision = connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
        rls = connection.execute(text("""
            SELECT bool_and(relrowsecurity AND relforcerowsecurity)
            FROM pg_class WHERE relname = ANY(:names)
        """), {"names": ["team_capabilities", "team_capability_commands", "team_capability_events",
                            "team_capability_capacity", "team_capacity_reservations",
                            "team_capacity_negotiations"]}).scalar_one()
        triggers = set(connection.execute(text("""
            SELECT tgname FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid
            WHERE c.relname='team_capability_events' AND NOT t.tgisinternal
        """)).scalars())
    assert revision == "20260813_28" and rls
    assert {"trg_capability_events_immutable", "trg_capability_events_reject_truncate"}.issubset(triggers)
    engine.dispose()
    print("CAPABILITY_DIRECTORY_OK revision=20260813_28 rls=forced audit=verified cross_tenant=hidden capacity=fresh matching=explainable reservation=atomic negotiation=bilateral")


if __name__ == "__main__":
    main()
