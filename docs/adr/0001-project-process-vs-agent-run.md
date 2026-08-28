# ADR-0001: Project Process vs Agent Run

## Status

Accepted. Main-thread review completed on 2026-08-28.

## Context

The existing `AgentRun` is a bounded, budgeted and recoverable execution unit.
A project lasts across many runs, tool jobs, human decisions and team handoffs.
Extending one AgentRun into a project-long loop would erase durable business
state and make recovery depend on one model session.

## Decision

1. `ProjectProcess` is the durable aggregate for the complete project
   lifecycle. At most one non-terminal process may be active for a project.
2. `AgentRun` remains a finite execution attempt with its existing lease,
   heartbeat, fencing, checkpoint, budget and terminal semantics.
3. A process may create many AgentRuns and ToolJobs, wait for teams, humans or
   verification, and resume after restart. A run never owns the project phase.
4. AgentRun output affects authoritative state only through validated domain
   services and the `ProjectTransitionGuard`.
5. Conversation and Memory are inputs/projections, not process or WorkGraph
   state sources.
6. When a run needs human input, the first version terminates that run, persists
   a `ProjectInputRequest`, waits, then creates a new run after an answer. No
   `AWAITING_INPUT` AgentRun status is introduced.
7. Run bindings carry process, work node, team agent, task/contract, run kind,
   orchestration decision and execution identities outside the generic Run core.
8. Process recovery re-evaluates committed state and unconsumed events; it does
   not depend on provider-session continuity.

## Alternatives rejected

- A project-long AgentRun cannot provide durable human/team waiting semantics.
- Conversation-driven orchestration loses structured facts.
- Copying process phase into Run status creates competing state machines.
- Another runtime would duplicate proven AgentRun recovery controls.

## Enforced invariants

- ProjectProcess and AgentRun identities, versions and lifecycles are distinct.
- Run terminal state does not imply process terminal state.
- Process restart does not require conversation or provider-session continuity.
- Automatic work uses a bounded AgentRun or durable ToolJob.
- Existing AgentRun and ToolExecutor safety boundaries remain in force.

## Compatibility and migration

Existing conversation-triggered runs continue. During shadow mode they may bind
to a ProjectProcess and emit non-dispatching events. Legacy rows without a
binding remain readable and are not guessed into orchestration history.

## Test obligations

- Prove one process spans multiple completed/failed runs and survives restart.
- Prove a completed run cannot directly complete a project.
- Test durable human-input wait, answer and new-run resumption.
- Test duplicate terminal callbacks and wakeups converge.
- Verify conversation projection failure cannot corrupt process state.

## Consequences

The project becomes a durable Harness-managed process while AgentRun stays a
small, reusable and recoverable execution primitive.
