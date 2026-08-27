from coifesp_harness.idempotency import ClaimStatus, SQLiteIdempotencyStore


def test_sqlite_claim_survives_store_restart_and_detects_conflict(tmp_path) -> None:
    path = tmp_path / "idempotency.db"
    first = SQLiteIdempotencyStore(path)
    values = {
        "namespace": "tool.execute",
        "tenant_id": "team-a",
        "idempotency_key": "idem-1",
    }
    assert first.claim(**values, request_digest="digest-a") is ClaimStatus.CLAIMED

    restarted = SQLiteIdempotencyStore(path)
    assert restarted.claim(**values, request_digest="digest-a") is ClaimStatus.DUPLICATE
    assert restarted.claim(**values, request_digest="digest-b") is ClaimStatus.CONFLICT
