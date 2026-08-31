# ADR-0004: Deterministic Project Orchestrator

## Status

Accepted. Main-thread review completed on 2026-08-28.

## Context

State legality, dependencies, budget, capacity, gates and completion are
deterministic. Decomposition and risk analysis may need model reasoning. Giving
both to an unrestricted LLM makes authorization and recovery non-deterministic.

## Decision

1. The Orchestrator is a durable state machine/rule engine with optional bounded
   Planner AgentRuns; it is not a super-agent.
2. Each wakeup claims the process with lease/fencing, loads one canonical graph
   snapshot, applies idempotent commands and sleeps when no immediate work exists.
3. Rules exclusively decide transitions, dependencies/readiness, budget,
   capability/capacity, Gates/Input, verification, delivery and completion.
4. Planner is limited to analysis, decomposition/dependency/replan proposals,
   semantic review and risk analysis.
5. `coifesp.orchestration-decision.v1` includes process version, event
   sequence, graph digest, reason and typed commands.
6. Command vocabulary is closed/versioned. Planner may propose work,
   dependencies, risks, decisions, rework/replan or human input/Gate requests.
   It cannot set state, dispatch, grant access, accept Contract, raise budget,
   approve Gate, execute tools or complete a project.
7. The validator rechecks version, sequence and digest before any command. A
   mismatch rejects the whole decision with zero commands applied and records
   `project.orchestrator.decision_stale`.
8. Planner intent identity is deterministic from process ID/version, event
   sequence, orchestration-reason enum and graph digest.
9. Orchestrator/Planner use principals from ADR-0007 and authorized metadata
   projections. Side effects remain behind domain services and ToolExecutor.

## Alternatives rejected

- A project-manager LLM cannot guarantee policy or replay.
- Bespoke event workers duplicate readiness and transition logic.
- Partial stale-decision application creates mixed graph state.
- Direct database/tool access bypasses Harness controls.

## Enforced invariants

- Deterministic rules run before optional reasoning.
- Wakeups are idempotent under lease and fencing.
- A stale decision applies no command.
- Planner cannot widen budget, identity, context, tools or authority.
- Completion never depends on model opinion.

## Compatibility and migration

Shadow mode consumes projected events and records actions without dispatch.
Automatic dispatch starts only after transition, readiness, budget, capability
and recovery gates pass.

## Test obligations

- Use a Fake Planner for every valid/invalid command type.
- Test stale version, sequence and digest independently and atomically.
- Test duplicate wakeup, lease loss, stale fencing and restart.
- Reach Verification deterministically without a Planner.
- Prove model output cannot change state or execute effects directly.

## Consequences

The Harness remains deterministic where correctness matters and uses LLM
reasoning only behind a small stale-safe command boundary.

## Durable command consumption appendix (2026-08-31)

Projected Planner batches are consumed before the next deterministic wakeup.
The consumer uses a shared database transaction and process/decision row locks
(SQLite explicitly begins an immediate transaction before its savepoint).
It performs no model, tool or network call under the lock.

Before effects it rechecks the completed Run's service identity, tenant and
intent binding, process version, event cursor, graph digest, canonical decision
and command digests, current participating teams, and the entire typed batch.
Stale batches produce no domain effects and emit the existing decision-stale
fact. An open human control prevents new Planner effects.

The whole batch, generated-task/replan counters, per-command result IDs and
decision outcome commit together. A domain rejection rolls back every command
effect; unexpected interruption leaves PENDING for restart retry. Successful
replay does not create work or increment counters again. The new catalog fact
`project.orchestrator.commands_consumed` records decision ID, APPLIED/REJECTED
status, a bounded reason code and result IDs without a state transition.
Human commands can emit their existing control facts within the same batch;
these self-generated cursors do not stale subsequent commands.

Task commands must include the complete structured contract from ADR-0003.
They create PROPOSED, version-1, unaccepted cross-team TeamTasks. Machine source
is `service:project-orchestrator` with immutable source Run/command/decision
references (schema 61), never a fabricated human account. Existing same-team
TeamTask prohibition remains; local team work is not silently represented as a
cross-team contract. Incomplete legacy task proposals remain parseable, but
consumption rejects them without dispatch. Input resources must belong to the
project and preserve sharing mode; team-private inputs cannot cross teams.

Tasks are registered before dependency edges, so forward references work.
Risk and decision commands create proposed domain records and graph nodes;
options are not human-approved decisions. Dependencies reject self/cycles and
reuse a semantically identical existing edge. A collision does not overwrite
another domain record.

Task/replan limits are checked before graph mutation. Over-limit batches create
the existing Budget Gate when the current state permits a Human wait, and apply
no work. If BLOCKED or a non-human wait already owns the state, record the budget
exhaustion with gate_deferred=true, reject the batch and preserve that blocker.
Do not repeatedly attempt an illegal Gate transition or hold the consumer queue.
The blocker must be resolved before a later decision can open its budget Gate.
At most one replan is allowed per
batch: it creates a new snapshot-bound PENDING Planner intent and counts once.
The existing at-limit Run admission rules are unchanged. Planner Run budget
reservation/terminal settlement and automatic intent launch remain a separate
integration step; a durable intent is not evidence that a model was executed.

REQUEST_REWORK creates a human InputRequest carrying the task and reason. It
does not forge a verification failure or overwrite VERIFIED work. Answering
that request alone does not perform rework; existing human review/delivery
rejection paths still own that transition. Human Gates are likewise requests,
not an alternate path to delivery acceptance or project completion.
