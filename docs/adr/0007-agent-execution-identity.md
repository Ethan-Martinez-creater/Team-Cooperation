# ADR-0007: Agent Execution Identity

## Status

Accepted. Main-thread review completed on 2026-08-28.

## Context

Automatic work may be caused by a human, Project Orchestrator, Team Agent or
recovery worker. Conflating the requester with the executor either loses audit
provenance or lets a scheduler inherit human/team-private permissions.

## Decision

1. Durable commands, process events, AgentRuns, tool invocations, artifact
   writes and cross-team actions distinguish immutable `initiated_by` and
   `executed_as` principals.
2. `initiated_by` is provenance, not authorization. Authorization is evaluated
   for `executed_as` against the operation, project, team, artifact and explicit
   context grants.
3. Automatic orchestration uses verified service/delegated principals:
   `ProjectOrchestratorPrincipal` for coordination and `TeamAgentPrincipal` for
   team-scoped execution.
4. Delegation records are server-created, scoped, time-bounded and auditable.
   Browser input and model output cannot select or modify execution identity.
5. The Orchestrator may read WorkGraph metadata and dispatch already-valid
   work, but does not inherit all Team-private data or tools.
6. A Team Agent executes only within its team, accepted Contract and resolved
   capability/context scope. Explicit sharing projection remains required for
   cross-team private content.
7. Retries and replay preserve both original identities. Recovery records a
   separate event; it does not rewrite the domain executor.
8. Run binding records initiated principal, executed principal and a digest of
   the delegation scope.

## Alternatives rejected

- Copying initiator to executor conflates attribution and permission.
- Impersonating the latest project user makes unattended execution unsafe.
- One global Agent identity cannot express team-scoped policy.
- Orchestrator impersonation creates a cross-team data bypass.
- Client/model-controlled identity fields permit forgery and escalation.

## Enforced invariants

- Automatic AgentRuns never use a real user as their execution principal.
- Both identities are present or an explicit system principal is recorded.
- Identity is immutable across retry, projection and replay.
- Provenance never widens tool, context or artifact authorization.
- Orchestrator access excludes Team-private content without an explicit grant.
- Audit can distinguish human initiation, Harness dispatch, Team Agent
  execution and human approval.

## Compatibility and migration

Legacy actor fields are exposed through an adapter. A provable actor becomes
`initiated_by`; a provable service/team executor becomes `executed_as`. Rows
whose executor cannot be proven remain legacy and are ineligible for automatic
cross-team execution until explicitly backfilled. Local mode maps accounts to
stable local principals and does not require OIDC.

## Test obligations

- Test human, Orchestrator, Team Agent and recovery paths end to end.
- Test persistence through retry, projection and replay.
- Reject forged identity/delegation fields from API and model output.
- Test delegation scope, expiry and revocation.
- Prove Orchestrator cannot read Team-private resources by inheritance.
- Verify Audit preserves identities while redacting private payloads.

## Consequences

Every background operation needs an explicit principal and delegation context.
This adds identity plumbing but prevents automatic execution from becoming a
human impersonation or project-wide data exfiltration path.
