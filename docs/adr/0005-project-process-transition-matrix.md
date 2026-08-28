# ADR-0005: Project Process Transition Matrix and Guard

## Status

Accepted. Main-thread review completed on 2026-08-28.

## Context

The product already has separate project, plan, task, AgentRun, approval, and
exchange state machines. A long-lived project process also needs durable gates,
budget waits, verification, delivery, and restart-safe orchestration. Those
concerns cannot be represented safely by a caller-defined status update or a
single flat lifecycle.

The audited plan defines three independent dimensions:

- Phase: `INTAKE`, `ANALYSIS`, `PLANNING`, `EXECUTION`, `INTEGRATION`,
  `VERIFICATION`, `DELIVERY`, `TERMINAL`;
- Status: `READY`, `RUNNING`, `WAITING`, `BLOCKED`, `COMPLETED`, `FAILED`,
  `CANCELLED`;
- WaitReason: `NONE`, `HUMAN_INPUT`, `HUMAN_APPROVAL`, `TEAM_RESPONSE`,
  `AGENT_RUN`, `TOOL_JOB`, `DEPENDENCY`, `VERIFICATION`, `SCHEDULE`.

## Decision

1. `ProjectTransitionGuard` is the only entry point for changing a
   `ProjectProcess`. Routes, services, orchestrators, recovery code and admin
   APIs submit a typed event and expected version; none may assign the three
   state fields directly.
2. A versioned matrix is keyed by current phase, status, wait reason and
   `event_type`. The guard validates `expected_version`, prerequisites and the
   target combination before returning a new immutable process state.
3. WaitReason is a closed enum. `WAITING` and `BLOCKED` require a non-`NONE`
   reason. `READY`, `RUNNING` and terminal states require `NONE`.
   Status semantics are fixed:

   - `READY`: a deterministic next action is eligible and no execution is
     currently responsible for it;
   - `RUNNING`: at least one claimed/running AgentRun, ToolJob, verification
     or integration operation is actively advancing the phase;
   - `WAITING`: progress depends on a named durable external fact. Use
     `AGENT_RUN` or `TOOL_JOB` only when all outstanding executions are
     queued/recovering and none is currently running;
   - `BLOCKED`: no in-flight operation can resolve the condition. Team
     acceptance uses `TEAM_RESPONSE`, unmet graph prerequisites use
     `DEPENDENCY`, and a failed verification awaiting rework uses
     `VERIFICATION`;
   - capacity/scheduling delay uses `WAITING/SCHEDULE`; open InputRequest and
     Gate use `WAITING/HUMAN_INPUT` and `WAITING/HUMAN_APPROVAL`.
4. The initial main chain is:

   ```text
   INTAKE/WAITING --goal.confirmed--> ANALYSIS/READY
   ANALYSIS/READY --analysis.started--> ANALYSIS/RUNNING
   ANALYSIS/RUNNING --analysis.completed--> PLANNING/READY
   PLANNING/READY --plan.approved--> EXECUTION/READY
   EXECUTION/READY --work.dispatched--> EXECUTION/RUNNING
   EXECUTION/RUNNING --all_required_work_submitted--> VERIFICATION/READY
   VERIFICATION/READY --verification.failed--> EXECUTION/READY
   VERIFICATION/READY --verification.passed--> INTEGRATION/READY
   INTEGRATION/READY --integration.passed--> DELIVERY/READY
   DELIVERY/READY --delivery.accepted--> TERMINAL/COMPLETED
   ```

5. Delivery can return to execution only through explicit events such as
   `delivery.rejected` or `scope.changed`. Verification can return to execution
   only through verification failure or an accepted rework decision.
6. Every accepted transition increments version exactly once and appends one
   durable event with source/target state, identities, correlation/causation
   identifiers and timestamp in the same logical commit.
   A committed business fact that does not change the process triple increments
   `last_event_sequence` but not `process.version`. Snapshot digest and event
   sequence therefore participate in stale-decision validation.
7. Terminal processes cannot return to a non-terminal phase. A privileged
   recovery transition, if ever introduced, requires an explicit reason and
   Audit event; it is not an unguarded status patch.
8. Planner commands cannot specify arbitrary phase or status values. They can
   only propose commands whose resulting events are recognized by the guard.
9. Event naming has three distinct layers:

   - namespaced domain facts are authoritative inputs/wakeups;
   - unprefixed transition keys are internal matrix selectors;
   - namespaced Audit events describe security/operational observation.

   They are not interchangeable. The normative v1 mapping and payload rules are
   defined by `project-process-event-catalog-v1.md`.

## Alternatives rejected

- Per-route or per-worker transition logic creates competing semantics.
- A flat status enum loses phase and wait semantics.
- Treating `TeamTask` status as process state ignores gates, integration and
  delivery.
- Arbitrary admin or Planner state patches bypass prerequisites and replay.
- Best-effort event publication can leave a committed process asleep forever.

## Enforced invariants

- One matrix and one guard govern every transition path.
- Phase, status and wait reason always use the fixed enums above.
- Illegal combinations and stale expected versions are rejected.
- One successful transition produces one version increment and one event.
- Two commands against the same expected version cannot both succeed.
- Duplicate events converge; conflicting same-ID events fail.
- `TERMINAL` is immutable for ordinary commands.
- A non-transition fact never increments process version, but always advances
  the event sequence exactly once.

## Compatibility and migration

Existing Product and Governance statuses remain readable. Compatibility
adapters translate their changes into typed process events and call the guard;
they never write process state directly. Ambiguous legacy state maps to an
explicit waiting/blocking condition for review rather than being guessed.

## Test obligations

- Exhaustively test the matrix and illegal state combinations.
- Test stale versions, terminal immutability and privileged recovery rejection.
- Race two transitions at one expected version and assert one winner.
- Verify every route/service/worker/replay path passes through the guard.
- Verify crash/replay never advances state without its durable event.
- Run PostgreSQL constraint and migration tests before automatic dispatch.

## Consequences

Every new lifecycle feature needs a named event and reviewed matrix entry. This
adds explicit domain code and tests, but gives the Harness deterministic,
auditable and replay-safe project progression.
