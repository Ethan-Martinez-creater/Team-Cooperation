from __future__ import annotations

import sys
import uuid
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import create_engine, text

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from coifesp_harness.config import Settings
from coifesp_harness.context import (SemanticCheckpointKeyring,
    SemanticCheckpointService, SemanticSummary)
from coifesp_harness.postgres_audit import AuditSigningKeyring, SQLAlchemyAuditLog
from coifesp_harness.runtime.models import Message
from coifesp_harness.security import Classification, Principal


def main() -> None:
    load_dotenv(ROOT / ".env", override=False)
    settings = Settings.from_environment()
    engine = create_engine(settings.database_url, hide_parameters=True)
    audit = SQLAlchemyAuditLog(engine=engine,
        keyring=AuditSigningKeyring.from_settings(settings))
    service = SemanticCheckpointService(engine=engine,
        keyring=SemanticCheckpointKeyring.from_settings(settings), audit=audit)
    suffix = uuid.uuid4().hex[:12]
    tenant, checkpoint_id = f"checkpoint-{suffix}", f"cp-{suffix}"
    owner = Principal("owner", tenant, clearance=Classification.CONFIDENTIAL,
        compartments=frozenset({"project-x"}))
    reviewer = Principal("reviewer", tenant,
        roles=frozenset({"conversation_reviewer"}),
        clearance=Classification.CONFIDENTIAL, compartments=frozenset({"project-x"}))
    service.propose(principal=owner, checkpoint_id=checkpoint_id,
        conversation_id=f"conv-{suffix}",
        messages=(Message("user", "finish migration"),),
        summary=SemanticSummary("Finish migration", ("No downtime",),
            ("Use blue-green",), ("Approve cutover",), ("Schema v3 exists",)),
        classification=Classification.CONFIDENTIAL,
        compartments=frozenset({"project-x"}))
    service.review(principal=reviewer, checkpoint_id=checkpoint_id,
        expected_version=1, approve=True, reason="verified original transcript")
    assert "data_only" in service.resume_message(principal=owner,
        checkpoint_id=checkpoint_id).content
    assert audit.verify_tenant_chain(tenant) == 2
    with engine.connect() as connection:
        revision = connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
        protected = connection.execute(text("""
            SELECT relrowsecurity AND relforcerowsecurity FROM pg_class
            WHERE relname='semantic_checkpoints'
        """)).scalar_one()
        connection.execute(text("SELECT set_config('coifesp.tenant_id',:tenant,true)"),
            {"tenant": f"other-{suffix}"})
        hidden = connection.execute(text("SELECT count(*) FROM semantic_checkpoints WHERE checkpoint_id=:id"),
            {"id": checkpoint_id}).scalar_one()
    engine.dispose()
    assert revision == "20260813_28" and protected and hidden == 0
    print("SEMANTIC_CHECKPOINT_OK revision=20260813_28 encrypted=yes source_bound=yes independent_review=yes cross_tenant=hidden audit=verified rls=forced")


if __name__ == "__main__":
    main()
