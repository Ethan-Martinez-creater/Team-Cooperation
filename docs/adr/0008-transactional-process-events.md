# ADR-0008: Transactional Process Events

## Status

Accepted. Main-thread review completed on 2026-08-28.

## Context

Project orchestration wakes from durable events. Updating business state and
then publishing best-effort can lose the wakeup; publishing before commit can
make consumers act on rolled-back state.

## Decision

1. When business state and ProjectProcess state share PostgreSQL, the business
   mutation, guarded process transition, authoritative event row and optional
   outbox delivery row are written in one transaction.
2. The authoritative event contains immutable ID, process/project, monotonic
   sequence, event/schema version, subject, both identities,
   correlation/causation identifiers, bounded payload, payload digest and time.
3. `(process_id, sequence)` and `event_id` are unique. Conflicting reuse fails;
   exact retry converges.
4. Cross-service, cross-database and external delivery uses a Transactional
   Outbox. A durable dispatcher publishes at least once and records outcome;
   consumers are idempotent by event ID and sequence.
5. In-process callbacks and notifications may be cache hints but cannot be the
   authoritative trigger for process progression.
6. Unknown schema versions remain pending/visible for operator action and are
   never discarded.
7. External effects use idempotency keys. External success is not evidence that
   a local event or outcome was committed.
8. Payloads exclude raw prompts, secrets and raw tool arguments.

## Alternatives rejected

- Update-then-publish loses events during the crash window.
- Publish-before-commit exposes uncommitted state.
- A separate event database without a local outbox recreates dual write.
- Exactly-once delivery is not assumed; at-least-once plus idempotency is used.
- Per-route publishers destroy ordering and recovery consistency.

## Enforced invariants

- A committed business mutation that advances the process has one
  authoritative event in the same atomic commit.
- No authoritative event exists for a rolled-back mutation.
- Event IDs, sequences and payload digests are immutable.
- Pending/failed outbox entries remain queryable and replayable.
- Duplicate delivery is harmless and conflicting payloads fail loudly.
- No route, Agent, worker or plugin bypasses the event writer/outbox.

## Compatibility and migration

Legacy state-change call sites are moved behind a transaction-aware writer.
During the shadow phase an adapter may append versioned legacy events in the
same transaction. Post-commit notifications can remain non-authoritative until
all consumers migrate. Ambiguous historical gaps are flagged, not guessed.

## Test obligations

- Roll back before commit and prove neither mutation nor event is visible.
- Crash after commit before publish and recover the pending outbox exactly once.
- Test duplicate publish/projection and conflicting payload rejection.
- Race event appenders and verify sequence uniqueness.
- Replay every event twice and assert identical final state.
- Exercise real PostgreSQL constraints and transaction boundaries.
- Test external idempotency keys and delivery outcome persistence.

## Consequences

The Harness gains durable wakeup and replay at the cost of an explicit outbox,
dispatcher, versioned event schema and mandatory crash/concurrency tests.
