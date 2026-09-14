# ADR-0013: Legacy Governance Retirement Gate

## Status

Accepted. Main-thread review on 2026-09-14. This ADR defines retirement criteria;
it does not delete legacy schema or alter historical records.

## Context

The production composition already disables new legacy Assignment writes and
legacy Assignment-backed execution. Project Work uses `enqueue_project_work()`
with ProjectProcess, WorkGraph, TeamTask and contract-version bindings. Legacy
Program, Plan and TaskAssignment models remain mounted for historical reads and
test compatibility, and Program/Plan routes can still create records.

There is no setting named `allow_legacy_governance`. The current boundaries are
`allow_legacy_assignment_writes=False` on GovernanceService and
`allow_legacy_assignments=False` on TaskExecutionService.

## Decision

1. New production execution must use `enqueue_project_work()`; legacy
   Assignment enqueue and new Assignment creation remain disabled.
2. Legacy tables are retained until a privileged, read-only inventory proves
   there are no active legacy plans, assignments or executions.
3. `scripts/check_legacy_governance_inventory.py --require-no-active` is the
   migration readiness gate. It never mutates data and must run with a database
   role whose RLS visibility is sufficient for the intended inventory scope.
4. After active work reaches zero, Program/Plan mutation routes move behind an
   explicit compatibility mode. Historical reads remain available for at least
   one release while API-use telemetry is observed.
5. Schema removal requires the stronger `--require-retired` gate: every legacy
   Program, Plan, Assignment and legacy-bound ExecutionTask must be migrated or
   archived outside the live tables.
6. Removal proceeds in separate releases: disable all writes, observe/read only,
   remove routes and service dependencies, then remove schema. No release may
   combine an unverified data migration with destructive schema removal.

## Acceptance gates

- Production composition rejects `propose_assignment()` and
  `enqueue_assignment()`.
- All new project executions have non-null project/process/team-task/work-node
  bindings and null Program/Assignment bindings.
- The inventory reports zero active legacy work before read-only mode begins.
- Two consecutive releases show no legacy mutation API usage.
- The inventory reports `is_retired=true` before any legacy table is dropped.
- Backup/restore and audit retention requirements are signed off separately.

## Consequences

Legacy compatibility remains deliberate rather than indefinite. The project
gets a measurable exit gate without weakening current production behavior or
prematurely deleting history.
