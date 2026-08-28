# ADR-0002: Project Work Graph

## Status

Accepted. Main-thread review completed on 2026-08-28.

## Context

Project facts live across plans, tasks, resources and conversations. The
Orchestrator needs one structured snapshot, but copied Task or Artifact status
would create a third source of truth.

## Decision

1. The Work Graph is the authoritative topology. Referenced business objects
   remain authoritative for their fields and lifecycle.
2. `project_work_nodes` is an identity registry containing `node_id`,
   `project_id`, `node_type`, `subject_id`, `created_at`. Initial types:

   ```text
   goal requirement milestone phase task risk decision artifact verification
   ```

3. Nodes never copy subject status/content. Reads resolve the actual domain row.
4. Relations contain source, target, type, creator, source run and creation
   time. Initial types:

   ```text
   depends_on blocks implements delivers verifies derived_from supersedes
   relates_to part_of
   ```

5. Self-reference, missing nodes, cross-project edges and `depends_on` cycles
   are rejected. Duplicate semantic edges converge using deterministic identity
   and a database uniqueness constraint.
6. `ProjectGraphSnapshot` resolves process, goal, requirements, milestones,
   phases, tasks, risks, decisions, artifacts and relations in stable order.
   Canonical serialization produces `graph_snapshot_digest`.
7. Planning/orchestration decisions bind process version, event sequence and
   graph digest; any mismatch makes the whole decision stale.
8. `coifesp.project-plan.v2` uses plan-local stable IDs and materializes goal,
   requirements, milestones, phases, tasks, dependencies and risks. Plan v1 is
   supported through an explicit compatibility projection.

## Alternatives rejected

- Conversation/Memory cannot be project fact sources.
- A denormalized graph with copied state would drift.
- A third Task model would prolong Product/Governance divergence.
- Arbitrary model-generated edges would permit invalid topology.

## Enforced invariants

- A node belongs to one project and references one existing subject.
- A subject has at most one graph identity per project/type.
- Topology changes are transactional, audited and idempotent.
- Snapshot order and digest are deterministic.
- Planner output cannot bypass graph validation or stale checks.

## Compatibility and migration

Plan v1 continues existing Topic/TeamTask materialization and projects only facts
it can prove. Legacy Governance Assignment becomes a compatibility projection;
Product TeamTask evolves into the task/contract source.

## Test obligations

- Test every node/relation type and subject resolution.
- Test self edge, missing node, cross-project edge, duplicate and cycle.
- Test stable snapshot/digest across insertion order and restart.
- Test Plan v1 compatibility and Plan v2 deterministic materialization.
- Crash during projection and prove retry creates no duplicates.

## Consequences

Orchestration gains one stable topology without copying domain state or adding
another task lifecycle.
