# Retention governance

`coifesp_harness.retention` is a versioned, fail-closed governance slice for artifact, audit-log, memory, evaluation-report, and release-report storage records. It does not read, copy, or delete storage content. Its manifests and reports contain only a stable scheme-qualified reference and a SHA-256 digest.

Policies use the `1.0.0` schema and a strict semantic policy version. Every rule is an exact match on category, classification, and compartment; there are no wildcard or fallback rules. No match, tenant mismatch, invalid commitment, or changed batch input produces a denial. A batch is single-tenant, and its executor must be distinct from the policy author and approver.

Each rule establishes minimum and maximum retention days and one explicit expiry action: `delete`, `crypto_shred`, or `archive`. Before the effective retention date the action is `retain`. An active legal hold for the same reference, tenant, and compartment always wins. A previously committed longer period is preserved rather than shortened by a newer policy; a commitment below the current minimum is denied for remediation.

Execution is explicit. Dry-run batches only return deterministic planned reports. Non-dry-run batches require a `StorageExecutor` protocol implementation and receive a per-target idempotency key. The included `FakeStorageExecutor` is test-only and never changes storage. Completed receipts are retained in the coordinator so a replay returns the same report and does not reissue actions. Failed entries remain visible and can be retried with the identical immutable batch input.

The governance module deliberately does not claim that a logical delete, crypto-shred, or archive action has affected real storage. That guarantee belongs to the selected external executor and its durable receipt.
