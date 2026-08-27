from __future__ import annotations

import argparse
import sys
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

from sqlalchemy import create_engine, select, text

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from coifesp_harness.collaboration import SignedEnvelopeCodec  # noqa: E402
from coifesp_harness.collaboration.models import CollaborationEnvelope  # noqa: E402
from coifesp_harness.collaboration.repository import GOVERNANCE_OUTBOX  # noqa: E402
from coifesp_harness.control_plane.bootstrap import load_environment_settings  # noqa: E402
from coifesp_harness.errors import IntegrityError  # noqa: E402


def issue(codec: SignedEnvelopeCodec, suffix: str) -> CollaborationEnvelope:
    return codec.issue(
        message_id=f"envelope-rotation-{suffix}",
        idempotency_key=f"envelope-rotation-idem-{suffix}",
        correlation_id=f"envelope-rotation-corr-{suffix}",
        sender_tenant_id="team-a",
        sender_principal_id="security-admin",
        recipient_tenant_id="team-b",
        purpose="key-rotation-probe",
        classification="INTERNAL",
        compartments=(),
        content="fixed non-sensitive envelope rotation probe",
    )


def must_reject(codec: SignedEnvelopeCodec, envelope: CollaborationEnvelope) -> None:
    try:
        codec.verify(envelope)
    except IntegrityError:
        return
    raise RuntimeError("tampered collaboration envelope was accepted")


def check(expected_active: str, tenant_id: str) -> None:
    settings = load_environment_settings(ROOT / ".env")
    codec = SignedEnvelopeCodec.from_settings(settings, max_age=timedelta(days=3650))
    if codec.active_key_id != expected_active:
        raise RuntimeError("configured active envelope key does not match the expected stage")
    if settings.envelope_legacy_v1_key_id != "envelope-v1":
        raise RuntimeError("legacy v1 envelope key mapping is invalid")
    if len(settings.envelope_keys) != 2:
        raise RuntimeError("envelope rotation requires exactly two configured keys")

    old = next((item for item in settings.envelope_keys if item.key_id == "envelope-v1"), None)
    if old is None:
        raise RuntimeError("legacy envelope-v1 key is unavailable")
    legacy_codec = SignedEnvelopeCodec(old.material.reveal().encode("utf-8"))
    legacy = issue(legacy_codec, "legacy-v1")
    current = issue(codec, f"current-{expected_active}")
    codec.verify(legacy)
    codec.verify(current)
    if current.schema_version != "coifesp.collaboration.v2":
        raise RuntimeError("current envelope does not use schema v2")
    if current.signing_key_id != expected_active:
        raise RuntimeError("current envelope recorded an unexpected signing key ID")
    must_reject(codec, replace(current, content=current.content + "-tampered"))
    must_reject(codec, replace(current, signing_key_id="unknown-envelope-key"))

    historical = 0
    engine = create_engine(settings.database_url, pool_pre_ping=True, hide_parameters=True)
    try:
        with engine.connect() as connection:
            connection.execute(
                text("SELECT set_config('coifesp.tenant_id', :tenant_id, true)"),
                {"tenant_id": tenant_id},
            )
            rows = connection.execute(
                select(GOVERNANCE_OUTBOX.c.envelope).where(
                    GOVERNANCE_OUTBOX.c.producer_tenant_id == tenant_id,
                    GOVERNANCE_OUTBOX.c.envelope.is_not(None),
                )
            ).scalars()
            for value in rows:
                codec.verify(CollaborationEnvelope(**value))
                historical += 1
    finally:
        engine.dispose()
    print(
        "ENVELOPE_KEY_ROTATION_OK "
        f"active={expected_active} legacy_v1=envelope-v1 keys=2 "
        f"historical_verified={historical} synthetic_v1=yes current_v2=yes "
        "tamper_rejected=yes unknown_key_rejected=yes database_writes=none secrets=redacted"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify an Envelope key rotation stage.")
    parser.add_argument("--expected-active", required=True)
    parser.add_argument("--tenant-id", default="team-a")
    arguments = parser.parse_args()
    try:
        check(arguments.expected_active, arguments.tenant_id)
        return 0
    except Exception as exc:
        print(
            f"ENVELOPE_KEY_ROTATION_FAILED error_type={type(exc).__name__} "
            f"reason={exc} secrets=redacted"
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
