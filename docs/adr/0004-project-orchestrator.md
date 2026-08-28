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
