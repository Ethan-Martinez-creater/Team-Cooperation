# ADR-0011: Integration, Delivery and Deterministic Completion

## Status

Accepted. Main-thread review completed on 2026-08-28.

## Context

All required tasks being verified does not prove their artifacts integrate into
an acceptable delivery. A model statement that a project is complete is not a
machine-verifiable completion condition.

## Decision

1. `IntegrationRun`, `DeliveryManifest` and
   `ProjectCompletionContract` are first-class durable objects.
2. Integration starts only when required execution work is submitted and
   verified. It assembles a versioned artifact set, runs configured verification
   and records structured PASS/FAIL.
3. Integration FAIL identifies impacted work and emits an explicit rework or
   replan path through TransitionGuard; it cannot advance to Delivery.
4. DeliveryManifest status is `ASSEMBLING`, `READY`, `ACCEPTED`,
   `REJECTED`. It records process/project, IntegrationRun, artifact
   IDs/digests, verification evidence, acceptance requirements, version,
   creator/acceptor and timestamps.
5. Only Integration PASS makes a manifest READY. Accept/reject requires the
   configured authorized role, idempotency key and expected manifest/process
   versions.
6. REJECTED records reason and `project.delivery.rejected`, then deterministic
   rework/replan. It never completes the process or silently rewrites tasks.
7. `ProjectCompletionEvaluator` deterministically checks required goal,
   requirements/milestones, no blocking risk/Gate, required verification PASS,
   Integration PASS and `required_delivery_accepted == true`.
8. Only a successful evaluator may cause
   `delivery.accepted -> TERMINAL/COMPLETED`. Model output cannot override it.
9. Evidence is immutable and replay-safe. Later scope change creates a new
   version rather than rewriting accepted evidence.

## Alternatives rejected

- All TeamTasks verified ignores integration and acceptance.
- Chat delivery has no immutable artifact/evidence contract.
- LLM completion judgment is non-deterministic and not authorization.
- Reopening terminal state after rejection violates terminal immutability.

## Enforced invariants

- No Integration PASS means no Delivery READY.
- No accepted DeliveryManifest means no TERMINAL/COMPLETED.
- Delivery rejection remains non-terminal and produces explicit rework.
- Completion evaluation is deterministic, versioned and audited.
- Accepted evidence and artifact digests are immutable.

## Compatibility and migration

Existing verified TeamTasks/ProjectResources seed integration candidates but do
not imply completion. Existing deliverables remain visible while new projects
require a DeliveryManifest. Legacy projects are not retroactively completed
without explicit acceptance evidence.

## Test obligations

- Test missing/failed/stale verification blocks integration/delivery.
- Test Integration PASS/FAIL, retry, crash recovery and duplicate callback.
- Test manifest assembly, optimistic accept/reject and authorization.
- Reject completion without accepted delivery evidence.
- Test rejection to rework/replan and eventual new accepted version.
- Run full Recovery E2E through deterministic completion.

## Consequences

Completion becomes a durable business fact backed by integrated artifacts and
explicit acceptance rather than task counts or model opinion.
