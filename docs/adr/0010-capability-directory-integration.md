# ADR-0010: Capability Directory Integration

## Status

Accepted. Main-thread review completed on 2026-08-28.

## Context

The repository already implements TeamCapability, CapabilityCapacity,
CapabilityMatch, CapacityReservation, CapacityNegotiation and
`CapabilityDirectoryService`. A second capability/capacity registry for Team
Agents would diverge during dispatch.

## Decision

1. `CapabilityDirectoryService` is the only capability, visibility,
   classification, residency and capacity fact source used by orchestration.
2. TeamAgentProfile stores runtime/model/tool/skill/memory/autonomy policy only;
   it does not copy capability tags, contracts or capacity.
3. Dispatch follows:

   ```text
   Contract requirements -> CapabilityDirectoryService.match
   -> project/security/residency/compartment filter
   -> CapacityReservation -> target-team Contract acceptance
   -> derived scheduler readiness
   ```

4. A narrow `TeamCapabilityBinding` may map Product `team_id` to
   `provider_tenant_id`, capability ID/version when identifiers differ. It
   copies no capability content or capacity state.
5. Match/reserve results persist capability/version, policy version, request
   digest and reservation ID. Retry is idempotent and cannot oversubscribe.
6. Capacity exhaustion never dispatches. Deterministic choices are existing
   CapacityNegotiation, Planner replan proposal or Human Gate. While unresolved,
   process state is `WAITING/SCHEDULE`.
7. Contract acceptance does not reserve capacity; reservation does not accept a
   Contract. Derived readiness requires both.
8. TeamAgentCapabilityResolver combines Contract, membership, executed
   principal, deployment registry and profile into the existing authorization,
   context, model-route and RunBudget types.

## Alternatives rejected

- A new Team Agent capability table duplicates the current domain.
- Profile capacity becomes stale under concurrent reservations.
- Dispatch-before-reserve permits oversubscription.
- Treating a match as authorization ignores data/project policy.

## Enforced invariants

- There is one capability/capacity source of truth.
- Every automatic dispatch has a valid unexpired reservation.
- Reservation/release/negotiation are idempotent and audited.
- Capacity failure is fail-closed and creates no AgentRun.
- Resolver output never widens executed-principal authority.

## Compatibility and migration

Existing capability APIs/records remain authoritative. Binding exists only for
proven identifier mismatch. Legacy manual runs may omit a project reservation
in shadow mode but are not reported as automatically orchestrated work.

## Test obligations

- Test match visibility, clearance, compartment and residency filters.
- Race PostgreSQL reservations and prove no oversubscription.
- Test binding mismatch, disabled capability and version change.
- Test capacity zero through negotiation, replan and Gate branches.
- Prove profile changes cannot alter capability facts.

## Consequences

The Orchestrator reuses mature capability/capacity primitives and limits new
code to adaptation and readiness integration.
