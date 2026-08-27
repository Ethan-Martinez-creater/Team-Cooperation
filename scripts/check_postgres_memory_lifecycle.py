from __future__ import annotations

import sys
import uuid
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import create_engine, text

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from coifesp_harness.config import Settings
from coifesp_harness.memory import (MemoryAdmissionPolicy, MemoryKind, MemoryLifecycleService,
    MemoryScope, MemoryService, MemorySource, MemoryWriteRequest, SourceType,
    SQLAlchemyMemoryRepository, TenantMemoryKeyring, TrustLevel)
from coifesp_harness.postgres_audit import AuditSigningKeyring, SQLAlchemyAuditLog
from coifesp_harness.security import Classification, PolicyEngine, Principal, ResourceLabel


def main() -> None:
    load_dotenv(ROOT / ".env", override=False)
    settings = Settings.from_environment()
    engine = create_engine(settings.database_url, hide_parameters=True)
    audit = SQLAlchemyAuditLog(engine=engine, keyring=AuditSigningKeyring.from_settings(settings))
    repository = SQLAlchemyMemoryRepository(engine)
    memory = MemoryService(repository=repository,
        keyring=TenantMemoryKeyring.from_settings(settings), policy=PolicyEngine(),
        admission=MemoryAdmissionPolicy(), audit=audit)
    lifecycle = MemoryLifecycleService(engine=engine, audit_log=audit)
    suffix = uuid.uuid4().hex[:12]
    tenant, memory_id = f"memory-life-{suffix}", f"memory-{suffix}"
    owner = Principal("owner", tenant, clearance=Classification.CONFIDENTIAL)
    officer = Principal("privacy", tenant, roles=frozenset({"memory_privacy_officer"}),
        clearance=Classification.RESTRICTED)
    memory.write(MemoryWriteRequest(memory_id=memory_id, idempotency_key=f"write-{suffix}",
        correlation_id=f"corr-{suffix}", principal=owner, scope=MemoryScope.USER_PRIVATE,
        kind=MemoryKind.FACT, content=f"searchable rollback evidence {suffix}",
        label=ResourceLabel(tenant, Classification.CONFIDENTIAL, resource_id=f"memory:{memory_id}"),
        source=MemorySource(SourceType.USER, "owner", None, TrustLevel.LOW),
        owner_principal_id="owner"))
    assert memory.search(principal=owner, query="rollback evidence",
        scope=MemoryScope.USER_PRIVATE)[0].memory.memory_id == memory_id
    assert repository.get(f"other-{suffix}", memory_id) is None
    request_id = f"delete-{suffix}"
    lifecycle.request_deletion(principal=owner, request_id=request_id,
        memory_id=memory_id, expected_version=1, reason="smoke deletion")
    assert memory.search(principal=owner, query="rollback",
        scope=MemoryScope.USER_PRIVATE) == ()
    assert lifecycle.decide_deletion(principal=officer, request_id=request_id,
        approve=True, reason="verified no hold").status == "purged"
    assert repository.get(tenant, memory_id) is None
    assert audit.verify_tenant_chain(tenant) == 3
    with engine.connect() as connection:
        revision = connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
        protected = set(connection.execute(text("""
            SELECT relname FROM pg_class WHERE relname=ANY(:names)
              AND relrowsecurity AND relforcerowsecurity
        """), {"names": ["memory_search_terms", "memory_legal_holds",
                            "memory_deletion_requests"]}).scalars())
    assert revision == "20260813_28" and len(protected) == 3
    engine.dispose()
    print("MEMORY_LIFECYCLE_OK revision=20260813_28 blind_search=yes cross_tenant=hidden deletion=purged audit=verified rls=forced")


if __name__ == "__main__":
    main()
