import sqlite3

import pytest

from coifesp_harness.audit import AuditEvent, SQLiteAuditLog
from coifesp_harness.errors import IntegrityError


def test_sqlite_audit_chain_detects_tampering(tmp_path) -> None:
    path = tmp_path / "audit.db"
    audit = SQLiteAuditLog(path, b"k" * 32)
    for index in range(2):
        audit.append(
            AuditEvent(
                tenant_id="team-a",
                event_type="test",
                actor_id="alice",
                outcome="ok",
                details={"index": index},
                correlation_id="corr-1",
            )
        )
    assert audit.verify_tenant_chain("team-a") == 2

    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE audit_events SET payload = ? WHERE sequence = 1", ('{"tampered":true}',)
        )
        connection.commit()
    with pytest.raises(IntegrityError):
        audit.verify_tenant_chain("team-a")
