import json

import pytest
from sqlalchemy import create_engine, update
from sqlalchemy.pool import StaticPool

from coifesp_harness.context import (SemanticCheckpointKeyring,
    SemanticCheckpointService, SemanticSummary)
from coifesp_harness.context.checkpoints import SEMANTIC_CHECKPOINTS
from coifesp_harness.errors import IntegrityError, PolicyDenied, ResourceNotFound
from coifesp_harness.postgres_audit import AuditSigningKeyring, SQLAlchemyAuditLog
from coifesp_harness.runtime import Message
from coifesp_harness.security import Classification, Principal


def stack():
    engine = create_engine("sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False}, poolclass=StaticPool)
    audit = SQLAlchemyAuditLog(engine=engine, keyring=AuditSigningKeyring(
        active_key_id="audit-v1", verification_keys={"audit-v1": b"a" * 32}))
    audit.create_schema()
    service = SemanticCheckpointService(engine=engine,
        keyring=SemanticCheckpointKeyring(active_key_id="memory-v1",
            keys={"memory-v1": b"m" * 32}), audit=audit)
    service.create_schema()
    owner = Principal("owner", "team-a", clearance=Classification.CONFIDENTIAL,
        compartments=frozenset({"project-x"}))
    reviewer = Principal("reviewer", "team-a", roles=frozenset({"conversation_reviewer"}),
        clearance=Classification.CONFIDENTIAL, compartments=frozenset({"project-x"}))
    return engine, audit, service, owner, reviewer


def summary():
    return SemanticSummary(objective="Complete migration safely",
        constraints=("No downtime",), decisions=("Use blue-green",),
        open_items=("Approve cutover",), verified_facts=("Schema v3 is deployed",))


def test_checkpoint_is_encrypted_source_bound_reviewed_and_resumable():
    engine, audit, service, owner, reviewer = stack()
    messages = (Message("system", "policy"), Message("user", "migrate database"),
                Message("assistant", "draft plan"))
    proposed = service.propose(principal=owner, checkpoint_id="checkpoint-1",
        conversation_id="conversation-1", messages=messages, summary=summary(),
        classification=Classification.CONFIDENTIAL, compartments=frozenset({"project-x"}))
    assert proposed.status == "pending"
    with engine.connect() as connection:
        row = connection.execute(SEMANTIC_CHECKPOINTS.select()).mappings().one()
    assert b"blue-green" not in bytes(row["ciphertext"])
    assert len(row["source_manifest"]) == 3
    with pytest.raises(PolicyDenied):
        service.resume_message(principal=owner, checkpoint_id="checkpoint-1")
    approved = service.review(principal=reviewer, checkpoint_id="checkpoint-1",
        expected_version=1, approve=True, reason="Compared with original transcript")
    assert approved.status == "approved" and approved.version == 2
    resumed = json.loads(service.resume_message(principal=owner,
        checkpoint_id="checkpoint-1").content)
    assert resumed["summary"]["decisions"] == ["Use blue-green"]
    assert resumed["source_digest"] == proposed.source_digest
    assert audit.verify_tenant_chain("team-a") == 2


def test_review_requires_independence_and_security_labels():
    _, _, service, owner, reviewer = stack()
    service.propose(principal=owner, checkpoint_id="checkpoint-1",
        conversation_id="conversation-1", messages=(Message("user", "hello"),),
        summary=summary(), classification=Classification.CONFIDENTIAL,
        compartments=frozenset({"project-x"}))
    with pytest.raises(IntegrityError, match="separation"):
        service.review(principal=Principal("owner", "team-a",
            roles=frozenset({"conversation_reviewer"}),
            clearance=Classification.CONFIDENTIAL, compartments=frozenset({"project-x"})),
            checkpoint_id="checkpoint-1", expected_version=1, approve=True, reason="self")
    with pytest.raises(ResourceNotFound):
        service.read_for_review(principal=Principal("limited", "team-a",
            roles=frozenset({"conversation_reviewer"}), clearance=Classification.INTERNAL,
            compartments=frozenset({"project-x"})), checkpoint_id="checkpoint-1")
    assert service.review(principal=reviewer, checkpoint_id="checkpoint-1",
        expected_version=1, approve=False, reason="summary omitted a constraint").status == "rejected"


def test_ciphertext_tampering_fails_closed():
    engine, _, service, owner, reviewer = stack()
    service.propose(principal=owner, checkpoint_id="checkpoint-1",
        conversation_id="conversation-1", messages=(Message("user", "hello"),),
        summary=summary(), classification=Classification.CONFIDENTIAL,
        compartments=frozenset({"project-x"}))
    with engine.begin() as connection:
        row = connection.execute(SEMANTIC_CHECKPOINTS.select()).mappings().one()
        changed = bytes(row["ciphertext"]); changed = bytes([changed[0] ^ 1]) + changed[1:]
        connection.execute(update(SEMANTIC_CHECKPOINTS).values(ciphertext=changed))
    with pytest.raises(IntegrityError, match="authentication"):
        service.read_for_review(principal=reviewer, checkpoint_id="checkpoint-1")
