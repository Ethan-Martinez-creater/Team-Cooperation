# Project Process Event Catalog v1

## Status and scope

Accepted normative appendix to ADR-0005 and ADR-0008. It freezes event naming
before ProjectProcess implementation and replaces ambiguous use of the word
“event” across the expert plan.

## Three layers

1. **Domain fact**: committed business truth that may wake the Orchestrator.
   It is stored in the authoritative process-event envelope.
2. **Transition key**: internal `ProjectTransitionGuard` matrix selector. It is
   stored in a transition event but is not published as a second business fact.
3. **Audit event**: security/operational record. It may summarize a fact but
   cannot drive process state.

Every domain fact envelope contains `event_id`, `event_type`,
`schema_version`, process/project/subject identity, source aggregate version,
`process_version_before`, optional `process_version_after`, event sequence,
both execution identities, correlation/causation IDs, bounded payload and
payload digest.

## Main-chain mapping

| Domain fact | Transition key | Target |
|---|---|---|
| `project.goal.confirmed` | `goal.confirmed` | `ANALYSIS/READY` |
| `project.analysis.started` | `analysis.started` | `ANALYSIS/RUNNING` |
| `project.analysis.completed` | `analysis.completed` | `PLANNING/READY` |
| `project.plan.approved` | `plan.approved` | `EXECUTION/READY` |
| `project.work.dispatched` | `work.dispatched` | `EXECUTION/RUNNING` |
| `project.work.required_submitted` | `all_required_work_submitted` | `VERIFICATION/READY` |
| `project.verification.completed` with FAIL | `verification.failed` | `EXECUTION/READY` |
| `project.verification.completed` with PASS | `verification.passed` | `INTEGRATION/READY` |
| `project.integration.completed` with PASS | `integration.passed` | `DELIVERY/READY` |
| `project.delivery.accepted` | `delivery.accepted` | `TERMINAL/COMPLETED` |
| `project.delivery.rejected` | `delivery.rejected` | `EXECUTION/READY` |
| `project.scope.changed` | `scope.changed` | guarded rework target |

Integration FAIL uses `project.integration.completed` with a structured FAIL
result and an explicit guarded rework/replan command; it never enters Delivery.

## Wakeup domain facts

The initial closed catalog additionally contains:

```text
team_task.accepted
team_task.started
team_task.submitted
team_task.verified
team_task.rejected
team_task.changes_requested
task.schedule.changed
artifact.published
exchange.responded
agent_run.completed
agent_run.failed
agent_run.cancelled
approval.decided
human.input.provided
risk.created
risk.resolved
project.input.requested
project.gate.opened
project.gate.decided
project.budget.exhausted
project.capability.matched
project.capacity.reserved
project.capacity.reservation_failed
project.capacity.negotiation_resolved
project.orchestrator.decision_stale
project.orchestrator.commands_consumed
project.completion.evaluated
project.completion_contract.proposed
project.completion_contract.approved
project.delivery.approval_decided
```

The acceptance producer writes the three additional v1 facts in the same SQL
transaction as their contract/approval rows. Proposal identity is derived from
process, human actor and idempotency key; approval identity from contract ID;
delivery approval identity from delivery and actor. Payloads contain only
contract/approval/goal IDs, versions, digests, request cursor and decision, never
private task context. Contract proposal/approval and partial delivery acceptance
advance event sequence without a state transition. The final acceptance writes
completion evaluation and `project.delivery.accepted` atomically; rejection uses
the existing `project.delivery.rejected` transition with impacted task IDs.
Service and HTTP regressions cover retries, partial acceptance, outbox failure
rollback and rejection through a new verified TaskRun to final completion.

Adding a domain fact requires schema version, producer transaction boundary,
idempotency key, payload allowlist and consumer tests. Aliases such as
`verification.failed`, `capacity.negotiation.*`, `input_request.created`
or `plan.replanned` are not accepted public facts unless added by a later
catalog revision.

## Version and transition rules

- Every accepted domain fact advances `last_event_sequence` exactly once.
- Only an accepted state transition advances `process.version`.
- One fact may cause zero or one immediate process transition.
- Transition-generated Harness output is observed, not reinjected as a second
  input event.
- Duplicate `event_id` plus identical digest converges; any conflict fails.
- Planner stale guard checks process version, event sequence and graph digest.

## Audit names

Audit uses the expert-plan `project.*` observation vocabulary such as
`project.process.transitioned`, `project.orchestrator.decision`,
`project.contract.accepted`, `project.agent.dispatched`,
`project.delivery.accepted` and `project.completed`. An Audit row is never
treated as the corresponding domain fact.

## Compatibility

Legacy producers may emit their existing fact names through an adapter. The
adapter writes the canonical envelope and preserves the legacy name in metadata;
consumers subscribe only to canonical v1 names.
