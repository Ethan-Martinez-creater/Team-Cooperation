from datetime import UTC, datetime, timedelta

import pytest

from coifesp_harness.retention import (
    FakeStorageExecutor, LegalHold, RetentionAction, RetentionBatch, RetentionCategory,
    RetentionGovernance, RetentionPolicy, RetentionRule, RetentionTarget,
)

NOW = datetime(2026, 8, 1, tzinfo=UTC)


def target(*, reference="artifact:tenant-a/report-1", committed_days=30, compartment="finance"):
    return RetentionTarget(reference, "a" * 64, "tenant-a", RetentionCategory.ARTIFACT,
                           "confidential", compartment, NOW - timedelta(days=40),
                           NOW - timedelta(days=40) + timedelta(days=committed_days))


def policy(*, action=RetentionAction.DELETE, author="policy-author", approver="policy-approver"):
    return RetentionPolicy("records-policy", "2.1.0", "tenant-a", author, approver,
                           (RetentionRule(RetentionCategory.ARTIFACT, "confidential", "finance",
                                          30, 365, action),))


def batch(items, *, dry_run=True, batch_id="batch-1", operator="retention-operator"):
    return RetentionBatch(batch_id, "tenant-a", "requester", operator, policy(), tuple(items), NOW, dry_run)


def test_expired_target_uses_exact_rule_and_dry_run_is_receipt_free():
    report = RetentionGovernance().run(batch([target()], dry_run=True))
    item = report.items[0]
    assert (item.action, item.status, item.receipt_id) == (RetentionAction.DELETE, "planned", None)


def test_unmatched_classification_is_fail_closed():
    unmatched = RetentionTarget("artifact:tenant-a/other", "b" * 64, "tenant-a",
                                RetentionCategory.ARTIFACT, "restricted", "finance",
                                NOW - timedelta(days=40), NOW - timedelta(days=30))
    decision = RetentionGovernance().decide(policy(), unmatched, as_of=NOW)
    assert (decision.action, decision.reason) == (RetentionAction.DENY, "no_matching_policy_rule")


def test_legal_hold_wins_over_expiry_and_is_compartment_scoped():
    item = target()
    hold = LegalHold(item.reference, "tenant-a", "finance", "hold-9", "legal-custodian")
    decision = RetentionGovernance().decide(policy(), item, as_of=NOW, holds=(hold,))
    assert (decision.action, decision.reason) == (RetentionAction.RETAIN, "legal_hold")


def test_minimum_retention_cannot_be_shortened():
    item = target(committed_days=20)
    decision = RetentionGovernance().decide(policy(), item, as_of=NOW)
    assert decision.action is RetentionAction.DENY
    assert decision.reason == "committed_retention_below_policy_minimum"


def test_historic_longer_commitment_is_preserved_despite_new_maximum():
    item = target(committed_days=400)
    decision = RetentionGovernance().decide(policy(), item, as_of=NOW)
    assert (decision.action, decision.reason) == (RetentionAction.RETAIN, "historic_commitment_preserved")


def test_cross_tenant_isolation_is_enforced_twice():
    foreign = RetentionTarget("artifact:tenant-b/secret", "c" * 64, "tenant-b",
                              RetentionCategory.ARTIFACT, "confidential", "finance",
                              NOW - timedelta(days=40), NOW - timedelta(days=10))
    assert RetentionGovernance().decide(policy(), foreign, as_of=NOW).action is RetentionAction.DENY
    with pytest.raises(ValueError, match="cross-tenant"):
        batch([foreign])


def test_separation_of_duties_rejects_policy_author_as_executor():
    with pytest.raises(ValueError, match="separated"):
        batch([target()], operator="policy-author")
    with pytest.raises(ValueError, match="different principals"):
        policy(author="same", approver="same")
    with pytest.raises(ValueError, match="requester"):
        batch([target()], operator="requester")


def test_execution_is_idempotent_and_report_is_deterministic():
    service, fake = RetentionGovernance(), FakeStorageExecutor()
    work = batch([target(reference="artifact:tenant-a/z"), target(reference="artifact:tenant-a/a")], dry_run=False)
    first, second = service.run(work, executor=fake), service.run(work, executor=fake)
    assert first.canonical_json() == second.canonical_json()
    assert len(fake.calls) == 2
    assert [item.reference for item in first.items] == ["artifact:tenant-a/a", "artifact:tenant-a/z"]


def test_failed_item_is_reported_and_recovers_on_retry():
    bad, good = target(reference="artifact:tenant-a/bad"), target(reference="artifact:tenant-a/good")
    service = RetentionGovernance()
    work = batch([bad, good], dry_run=False)
    first = service.run(work, executor=FakeStorageExecutor(frozenset({bad.reference})))
    retry_executor = FakeStorageExecutor()
    second = service.run(work, executor=retry_executor)
    assert [item.status for item in first.items] == ["failed", "executed"]
    assert [item.status for item in second.items] == ["executed", "executed"]
    assert len(retry_executor.calls) == 1


def test_batch_identifier_cannot_be_reused_with_changed_input():
    service = RetentionGovernance()
    service.run(batch([target()]))
    with pytest.raises(ValueError, match="immutable input"):
        service.run(batch([target(reference="artifact:tenant-a/changed")]))


def test_batch_fingerprint_binds_policy_target_metadata_and_holds():
    service = RetentionGovernance(); work = batch([target()])
    service.run(work)
    hold = LegalHold(target().reference, "tenant-a", "finance", "hold-1", "legal")
    with pytest.raises(ValueError, match="immutable input"):
        service.run(work, holds=(hold,))


def test_crypto_shred_and_archive_are_explicit_expiry_actions():
    for action in (RetentionAction.CRYPTO_SHRED, RetentionAction.ARCHIVE):
        work = RetentionBatch("batch-" + action.value, "tenant-a", "requester", "operator", policy(action=action), (target(),), NOW, False)
        result = RetentionGovernance().run(work, executor=FakeStorageExecutor())
        assert result.items[0].action is action


def test_report_omits_classification_compartment_and_payload_data():
    report = RetentionGovernance().run(batch([target()]))
    rendered = report.canonical_json()
    assert "confidential" not in rendered and "finance" not in rendered
