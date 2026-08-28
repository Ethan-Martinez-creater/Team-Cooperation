# ADR-0006: Project Budget and Concurrency

## Status

Accepted. Main-thread review completed on 2026-08-28.

## Context

`RunBudget` limits one AgentRun. A ProjectProcess may create many runs, replans,
tasks and specialist delegations across teams. Run-level limits alone cannot
prevent aggregate cost growth or concurrent oversubscription after retries.

## Decision

1. Every active process references a versioned, persisted
   `ProjectExecutionPolicy` containing at least:

   ```text
   max_agent_runs
   max_total_tokens
   max_model_cost_microusd
   max_replans
   max_generated_tasks
   max_active_agent_runs
   max_active_runs_per_team
   max_specialist_depth
   max_specialist_runs_per_task
   deadline_at
   version
   ```

2. Persisted `ProjectExecutionUsage` contains at least runs started/completed,
   total tokens, model cost, replan count, generated task count and active run
   count. Usage is authoritative database state, not a reconstructed UI value.
3. Dispatch ordering is fixed: project budget check, team concurrency check,
   AgentRun budget derivation, then dispatch. Passing a project check never
   grants tool/data authority and never replaces `RunBudget`.
4. Admission and reservation are atomic. A deterministic reservation key ties
   one process, work item, team, policy revision and execution attempt. Retries
   converge on it and cannot double-count usage.
5. Capacity released after crash or lease expiry becomes available only after
   the prior reservation/run is durably settled or recovered.
6. When a project limit is reached, the process becomes `WAITING` with
   `HUMAN_APPROVAL` and opens a durable Gate offering budget increase, scope
   reduction or termination. The Orchestrator cannot raise its own policy.
7. Policy updates apply to future reservations by default. Re-budgeting active
   work requires a guarded, audited command; existing reservations retain the
   revision under which they were admitted.
8. Processes waiting on a Gate/input, blocked, failed, cancelled or terminal
   cannot admit new work.

## Alternatives rejected

- Reusing `RunBudget` cannot account for aggregate project consumption.
- In-memory semaphores do not survive workers and cannot coordinate replicas.
- Accounting after run creation permits crash-window oversubscription.
- Planner-controlled budget increases defeat the protection.
- Independent team limits without a project policy cannot bound total cost.

## Enforced invariants

- Project and run budgets are distinct and both must pass.
- Usage and reservations cannot exceed the accepted policy.
- Reservation retry is idempotent and conflicting reuse is rejected.
- Token/cost accounting uses terminal AgentRun usage and is applied once.
- Replan and generated-task limits apply before graph mutation.
- Specialist delegation is bounded by depth and per-task run count.
- Budget exhaustion never causes an automatic retry loop.

## Compatibility and migration

Existing manually started AgentRuns retain their RunBudget behavior and are
marked legacy during the shadow-process phase. New automatically dispatched
work requires a policy and reservation. Historical usage is not guessed from
ambiguous old rows; any imported baseline is explicit and read-only.

## Test obligations

- Test every policy dimension at limit, below limit and above limit.
- Race reservations in PostgreSQL and prove limits cannot be exceeded.
- Test duplicate reservation, settlement, release and crash recovery.
- Prove project admission and RunBudget can reject independently.
- Test usage projection idempotency from duplicate terminal callbacks.
- Test policy update/version conflicts and Gate-based budget increases.
- Test per-team concurrency and specialist depth/count ceilings.

## Consequences

The ProjectProcess gains deterministic cost and concurrency admission while the
existing AgentRun safety model remains intact. The cost is persisted accounting,
atomic reservations and stronger concurrency/recovery testing.
