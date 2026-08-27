# Disaster recovery, backup recovery, and drill gate

This slice is an offline verifier. It never creates a backup, restores PostgreSQL, accesses object storage, contacts a KMS, or promotes a region. Operators provide signed/attested digest-only plan and drill-evidence documents from their controlled systems; the gate fail-closes if any required proof is absent or inconsistent.

## Production contract

- PostgreSQL PITR requires a base-backup SHA-256, WAL-manifest SHA-256, UTC recovery target, and backup age at or below the declared RPO.
- Object storage must be marked immutable, retain backups for a positive period, and bind its inventory digest to the plan. The digest is evidence, not a claim that this tool inspected storage.
- Backup encryption, audit verification, and restore authorization use three distinct reference identifiers. Secret material is never accepted or emitted.
- Primary and recovery regions differ. The only accepted drill scenario is `regional_primary_outage`; recovery must meet the declared RTO.
- Recovery is ordered: incident declaration, write fencing, immutable-backup verification, PITR restore, audit-chain verification, regional promotion, idempotent queue replay, then writes reopening.
- A recovery lease epoch must strictly increase after the primary is fenced. Queue replay requires a bound checkpoint, idempotency, and dead-letter review. Data consistency and audit-chain verification must be explicitly attested.
- Incident commander, database recovery, and security approver are distinct principals. Their approvals bind the canonical operational plan digest.

## Run the gate

```text
python scripts/check_disaster_recovery.py --plan deploy/disaster-recovery/recovery-plan.json --evidence deploy/disaster-recovery/drill-evidence-pass.json
```

The committed example intentionally contains placeholder approval and evidence plan digests, so it is not a passing attestation and cannot be mistaken for a real backup or completed drill. Generate the final, canonical digest in the controlled evidence workflow, place it in all approval records and drill evidence, and preserve the generated report as drill evidence. The command exits `0` only for `pass`, `2` for an evaluated failed gate, and `1` for invalid input. Reports are canonical JSON with stable ordering and only IDs, decisions, and gate names—no endpoints, keys, credentials, or backup contents.
