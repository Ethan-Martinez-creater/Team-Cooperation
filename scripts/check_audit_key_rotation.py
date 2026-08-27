from __future__ import annotations

import argparse
import sys
import uuid
from pathlib import Path

from sqlalchemy import create_engine, select

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from coifesp_harness.audit import AuditEvent  # noqa: E402
from coifesp_harness.control_plane.bootstrap import load_environment_settings  # noqa: E402
from coifesp_harness.postgres_audit import (  # noqa: E402
    AUDIT_EVENTS,
    AuditSigningKeyring,
    SQLAlchemyAuditLog,
)


def check(expected_active: str, tenant_id: str) -> None:
    settings = load_environment_settings(ROOT / ".env")
    keyring = AuditSigningKeyring.from_settings(settings)
    if keyring.active_key_id != expected_active:
        raise RuntimeError("configured active audit key does not match the expected stage")
    if len(settings.audit_keys) < 2:
        raise RuntimeError("audit key rotation requires at least two configured keys")
    engine = create_engine(settings.database_url, pool_pre_ping=True, hide_parameters=True)
    audit = SQLAlchemyAuditLog(engine=engine, keyring=keyring)
    event_id = f"audit-key-rotation-{uuid.uuid4()}"
    try:
        historical = audit.verify_tenant_chain(tenant_id)
        with engine.connect() as connection:
            transaction = connection.begin()
            try:
                audit.append_in_transaction(
                    connection,
                    AuditEvent(
                        tenant_id=tenant_id,
                        event_type="audit.key_rotation_probe",
                        actor_id="security-admin",
                        outcome="verified",
                        details={"expected_active_key_id": expected_active},
                        correlation_id=event_id,
                        event_id=event_id,
                    ),
                )
                stored_key_id = connection.execute(
                    select(AUDIT_EVENTS.c.key_id).where(
                        AUDIT_EVENTS.c.tenant_id == tenant_id,
                        AUDIT_EVENTS.c.event_id == event_id,
                    )
                ).scalar_one()
                if stored_key_id != expected_active:
                    raise RuntimeError("new audit event used an unexpected signing key")
                verified_with_probe = audit.verify_tenant_chain_in_transaction(
                    connection, tenant_id
                )
                if verified_with_probe != historical + 1:
                    raise RuntimeError("mixed audit chain length is invalid")
            finally:
                transaction.rollback()
        retained = (
            engine.connect()
            .execute(
                select(AUDIT_EVENTS.c.event_id).where(
                    AUDIT_EVENTS.c.tenant_id == tenant_id,
                    AUDIT_EVENTS.c.event_id == event_id,
                )
            )
            .scalar_one_or_none()
        )
        if retained is not None:
            raise RuntimeError("audit rotation probe was retained after rollback")
        print(
            "AUDIT_KEY_ROTATION_OK "
            f"active={expected_active} keys={len(settings.audit_keys)} "
            f"historical_events={historical} mixed_chain=yes probe_rolled_back=yes "
            "secrets=redacted"
        )
    finally:
        engine.dispose()


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify an audit key rotation stage safely.")
    parser.add_argument("--expected-active", required=True)
    parser.add_argument("--tenant-id", default="team-a")
    arguments = parser.parse_args()
    try:
        check(arguments.expected_active, arguments.tenant_id)
        return 0
    except Exception as exc:
        print(
            f"AUDIT_KEY_ROTATION_FAILED error_type={type(exc).__name__} "
            f"reason={exc} secrets=redacted"
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
