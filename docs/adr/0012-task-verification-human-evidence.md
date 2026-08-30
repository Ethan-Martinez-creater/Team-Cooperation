# ADR-0012: Task Verification Human Evidence and Composite Checks

## Status

Accepted. Main-thread design review on 2026-08-31. Narrow clarification of
ADR-0009's project-wide gates; no existing gate type, selector or decision is removed.

## Decision

1. Budget and final delivery authorization continue to use ProjectGate and
   WAITING/HUMAN_APPROVAL exactly as ADR-0009 specifies. Task-scoped subjective
   acceptance instead supplies a HumanReview evidence record to Verification.
   It cannot increase budget, approve delivery or assign a ProjectProcess state.
   Waiting for one task's evidence must not suspend other runnable project tasks.
2. HumanReview is not a third Task lifecycle or a generic approval framework.
   It binds verification ID, source Run, fixed subject digest, accepted contract,
   criterion, requesting team and an optimistic object version. The Product
   TeamTask remains the only task status authority. Invalidating the submission
   invalidates open human evidence requests.
3. Only an enabled, active human account in the task's requesting team may
   ACCEPT or REJECT, with a bounded reason. The executing team and Agent service
   identities cannot self-approve. Both task parties may read review evidence;
   reasons are feedback to those parties, not private notes.
4. The existing criteria array represents COMPOSITE verification by conjunction
   of all required checks. No new nested policy language or majority voting is
   introduced. Human requests open only after required deterministic and Agent
   checks PASS. Human acceptance cannot override their FAIL or PENDING. Optional
   human criteria are explicitly not requested and do not block.
5. Decision idempotency binds key, actor and exact payload including original
   expected object version. Exact retry returns historical evidence, never acts
   on a new submission; changed payload or second decision conflicts. Current
   task/contract/resource binding is rechecked before a new decision. Unrelated
   project event-sequence movement does not invalidate fixed task evidence.
6. Request/decision/close, verification aggregate, task status and applicable
   canonical facts/outbox share a transaction. There is no open Agent Run held
   while waiting for a human. Startup replay reuses durable requests.

## Added canonical facts

All use schema v1 and are triple-preserving (no transition selector):

| Fact | Meaning |
|---|---|
| `task_verification.human_review.opened` | Bound task evidence is ready for a human |
| `task_verification.human_review.decided` | Authorized immutable ACCEPT/REJECT recorded |
| `task_verification.human_review.closed` | An open request became stale |

The event contains bound IDs, status, contract/object version and subject digest,
not artifact bodies, review reason or internal dialogue. Facts advance event
sequence and notify the existing transactional listener, never process version.
The existing Orchestrator and TransitionGuard retain authority over next project
state; full all-work verification, integration and delivery remain separate.

## Acceptance tests

Mixed tool/Agent/human checks, required versus optional, two required reviewers,
reject/accept, stale contract or withdrawn sharing, disabled/wrong-team/service
actors, optimistic conflicts, exact retries, response-loss recovery, transaction
rollback including outbox, and durable restart without duplicate requests.
