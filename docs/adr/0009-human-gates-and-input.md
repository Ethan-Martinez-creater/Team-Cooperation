# ADR-0009: Durable Human Gates and Input Requests

## Status

Accepted. Main-thread review completed on 2026-08-28.

Task-scoped Verification evidence is clarified by ADR-0012 (2026-08-31).
The waits and Gate requirements below govern project-wide input/authorization;
task subjective acceptance cannot authorize project advancement by itself.

## Context

Projects may wait hours or days for information or approval. A wait-reason
string or active AgentRun cannot preserve request, authorization, answer,
version and audit evidence across restart.

## Decision

1. Human information uses `ProjectInputRequest`; authorization/choice uses
   `ProjectGate`. Both are durable, process/project scoped and versioned.
2. InputRequest status is `OPEN`, `ANSWERED`, `CANCELLED`, `EXPIRED`.
   It stores work/run/agent requester, question, input schema, bounded context,
   response, creator/answerer and timestamps.
3. Gate status is `OPEN`, `DECIDED`, `CANCELLED`, `EXPIRED`. It stores
   gate type, subject, required roles, allowed decisions, decision, reason,
   creator/decider and timestamps.
4. Decisions are defined by each versioned gate type. Initial budget values are
   `INCREASE_BUDGET`, `REDUCE_SCOPE`, `TERMINATE`; delivery uses `ACCEPT`
   or `REJECT`. Unknown values fail closed.
5. Creating InputRequest produces `WAITING/HUMAN_INPUT`; creating Gate produces
   `WAITING/HUMAN_APPROVAL`. Object, process transition and event are atomic.
6. Answer/decision requires authorization, idempotency key, expected object
   version and expected process version. Exact retry converges; conflict fails.
7. Answer/decision emits a canonical fact and wakes the Orchestrator. The HTTP
   path never assigns the next process state directly.
8. A run requesting input ends with a typed command. After an answer the
   Orchestrator creates a new run; the original is not resumed in place.
9. Context/response/event projections exclude secrets and unrestricted private
   content and follow existing project/team policy.

## Alternatives rejected

- In-memory prompts disappear on restart.
- Keeping a run open for days complicates lease recovery.
- A free-text approval comment has no authorized decision contract.
- Direct HTTP resume bypasses other open blockers.

## Enforced invariants

- HUMAN_INPUT/HUMAN_APPROVAL waits have matching open durable objects.
- One object is answered/decided at most once per version.
- Only required roles/scoped principals may answer or decide.
- Restart preserves wait and accepted response exactly.
- Planner/Team Agent cannot approve their own Gate or raise policy directly.

## Compatibility and migration

Existing Approval remains the tool/high-risk action primitive. ProjectGate
coordinates project progression and may reference Approval; it does not replace
it. Legacy transient prompts are not imported without provable state/authority.

## Test obligations

- Test create, answer/decide, cancel, expire and conflicting retry.
- Test role authorization, stale object/process versions and idempotency.
- Restart while waiting and resume via answer event.
- Resolving one of multiple waits must not resume prematurely.
- Test payload redaction and team visibility.

## Consequences

Human pauses become durable, explainable and recoverable without stretching an
AgentRun or trusting transient conversation state.
