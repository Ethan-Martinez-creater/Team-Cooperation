# ADR-0003: Team Agent Boundary and Work Contract

## Status

Accepted. Main-thread review completed on 2026-08-28.

## Context

A team needs a stable Agent identity and private context boundary, but not a
permanently running model session. Cross-team work also requires an accepted,
machine-readable contract rather than unconstrained Agent chat.

## Decision

1. Existing `TeamProjectAgent` is the logical per-project team identity. Its
   versioned profile stores runtime/model/tool/skill/memory/autonomy policy only.
2. Capability and capacity are not stored in that profile; ADR-0010 defines the
   existing Capability Directory as their sole source.
3. Existing `TeamTask` evolves into the cross-team Project Work Contract. No
   additional Task state machine is introduced.
4. A Contract identifies process/work node, requester/provider teams, goal,
   input manifest, permitted shared context, expected output/artifact contract,
   acceptance criteria, verification, deadline and autonomy/approval policy.
5. Only an authorized target-team human accepts/rejects a proposed cross-team
   Contract. Agent output may draft but cannot accept for the team.
6. `TeamTask.status == ACCEPTED` is necessary but insufficient. Readiness is:

   ```text
   accepted AND dependencies_satisfied AND required_inputs_accessible
   AND capacity_reserved AND no_open_gate AND project_budget_available
   AND process_allows_dispatch
   ```

7. `READY` is initially derived, not a new TeamTask status. `PROPOSED` is
   never automatically dispatched.
8. Contract governs work; existing human-confirmed Exchange governs scoped
   communication. Free Agent chat cannot accept work or mutate state.
9. A Team Agent run receives team-private, project-shared and explicit Contract
   inputs only through existing authorization.

## Alternatives rejected

- A permanent Team Agent process confuses identity with execution.
- A second capability registry would drift.
- Agent auto-acceptance removes the team's authorization boundary.
- ACCEPTED-as-runnable ignores dependencies and policy.
- Free multi-agent chat cannot provide durable output contracts.

## Enforced invariants

- One logical TeamProjectAgent exists per project/team pair.
- Team-private context never crosses teams without an explicit grant.
- Contract acceptance and readiness remain distinct.
- Dispatch records Contract version and readiness evidence.
- Contract output is an Artifact/Task Submission, not chat text.

## Compatibility and migration

Existing TeamTask and Exchange remain operational. TaskExecutionService moves
from Governance Assignment to accepted Project Work Contract only after shadow
comparison; legacy Assignment creation then stops.

## Test obligations

- Test identity/context isolation across projects and teams.
- Test authorized acceptance/rejection and optimistic conflicts.
- Test every readiness predicate independently and together.
- Prove proposed/rejected contracts cannot dispatch.
- Test inaccessible inputs and artifact contract enforcement.

## Consequences

Teams become durable logical Agent boundaries while execution remains a bounded
AgentRun governed by an accepted, verifiable work contract.
