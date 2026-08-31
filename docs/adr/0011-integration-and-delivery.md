# ADR-0011: Integration, Delivery and Deterministic Completion

## Status

Accepted. Main-thread review completed on 2026-08-28.

## Context

All required tasks being verified does not prove their artifacts integrate into
an acceptable delivery. A model statement that a project is complete is not a
machine-verifiable completion condition.

## Decision

1. `IntegrationRun`, `DeliveryManifest` and
   `ProjectCompletionContract` are first-class durable objects.
2. Integration starts only when required execution work is submitted and
   verified. It assembles a versioned artifact set, runs configured verification
   and records structured PASS/FAIL.
3. Integration FAIL identifies impacted work and emits an explicit rework or
   replan path through TransitionGuard; it cannot advance to Delivery.
4. DeliveryManifest status is `ASSEMBLING`, `READY`, `ACCEPTED`,
   `REJECTED`. It records process/project, IntegrationRun, artifact
   IDs/digests, verification evidence, acceptance requirements, version,
   creator/acceptor and timestamps.
5. Only Integration PASS makes a manifest READY. Accept/reject requires the
   configured authorized role, idempotency key and expected manifest/process
   versions.
6. REJECTED records reason and `project.delivery.rejected`, then deterministic
   rework/replan. It never completes the process or silently rewrites tasks.
7. `ProjectCompletionEvaluator` deterministically checks required goal,
   requirements/milestones, no blocking risk/Gate, required verification PASS,
   Integration PASS and `required_delivery_accepted == true`.
8. Only a successful evaluator may cause
   `delivery.accepted -> TERMINAL/COMPLETED`. Model output cannot override it.
9. Evidence is immutable and replay-safe. Later scope change creates a new
   version rather than rewriting accepted evidence.

### Artifact-composition execution boundary

The first executable integration policy is `artifact_composition` with bounded
artifact count and total input bytes. `assemble_integration` is a durable
Orchestrator decision; the fenced adapter pins the current graph, process/event
versions, task verification receipts and exact resource manifests. It reads
the immutable bytes again and publishes a deterministic ZIP with an embedded
version/digest manifest. It does not claim code-build or semantic acceptance.

IntegrationRun, result resource/registry metadata, completion fact and Guard
transition commit in one transaction. Object bytes may survive a rolled-back
transaction; deterministic object/publication identities make retry safe.
Storage/lease/database failures roll back instead of becoming business FAIL.

The non-main-chain `integration.failed` selector maps
`INTEGRATION/READY/NONE -> EXECUTION/READY/NONE`, driven only by the recorded
`project.integration.completed` FAIL evidence. Affected tasks become
`changes_requested` in that transaction; their accepted contract and historical
PASS verification remain immutable. Re-dispatch must validate this integration
failure against the latest task attempt, not invent a failed task verification.
The main-chain `integration.passed` transition enters `DELIVERY/READY`, never
`TERMINAL`. Manifest readiness, human acceptance and completion evaluation
remain separately enforced boundaries.

### Executable acceptance and completion boundary (2026-08-31)

Integration PASS now creates its READY manifest in the integration transaction.
It does not accept the delivery. `DeliveryService` exposes human acceptance via
the existing account and project-team membership model; no additional identity
provider or external connector is required.

The owning team's owner/admin proposes and approves a versioned completion
contract, selecting an already approved WorkGraph root goal and 1-32 distinct
active participating human accounts. Approval binds an unbound process root;
it cannot replace another bound goal. The proposal event records the goal and
request cursor, and the content digest covers the goal, criteria and approvers.
Contracts cannot disable any of the fourteen required completion checks.

Each approver submits an immutable ACCEPT/REJECT with the current delivery and
process versions plus an idempotency key. Revision 60 stores one decision per
delivery/account. The first decision pins contract ID/version/digest. Partial
acceptance keeps the delivery READY and advances its revision. Replacing the
approved contract after acceptance has started is rejected. A required account
that has left the project or been disabled does not count toward final approval.

The last ACCEPT runs the evaluator in the same transaction as the approval,
accepted manifest, evaluation record and `delivery.accepted` Guard transition.
Failure rolls the entire last decision back; no accepted-but-incomplete row is
left behind. Earlier partial approvals remain. Failed evaluations currently
return failed criterion IDs in a conflict response, not a persisted evaluation
history. Only successful completion evaluations are persisted by this endpoint.

The authoritative loader checks current TaskRun/contract/PASS evidence, graph
digest, IntegrationRun bindings, all graph artifact coverage, shared metadata
(including media type), and actual source/output byte hashes. It checks root
goal confirmation, blocking risks/dependencies, open Gates/InputRequests and all
required human approvals. It does not ask an LLM whether the project is done.

Requirement satisfaction requires explicit `implements`/`delivers`/`part_of`
coverage leading to verified tasks. Plan v2 can provide these semantic relations
through its `dependencies` collection; absent coverage fails closed. The loader
does not invent task-to-requirement mappings from the goal or task counts.
Milestone completion is derived from its covered verified tasks under the
supported `all_tasks_verified` policy (empty/default and v1 compatibility policy
have the same meaning). `planned`, `active`, `in_progress` and `completed` are
eligible states; blocked/cancelled states and opaque custom policies do not pass.
This is an evaluation result, not an automatic rewrite of historical graph rows.

REJECT records the human decision and guarded transition together, reopens only
still-current verified tasks tied to the manifest, and preserves historical PASS
records. The next TaskRun receives the explicit project-visible rejection reason
as delivery-rework context, under the existing accepted task contract. It must
publish/submit and verify a new attempt before producing a new delivery. A stale
delivery or an arbitrary `changes_requested` row does not authorize re-dispatch.
Rework currency is checked against the task node, latest TaskRun/accepted
contract, rejection timestamp, approval cursors and original Integration refs.
The current dispatch decision separately pins the current graph: a byte-for-byte
comparison with the pre-rejection graph would be wrong because rejection itself
changes task status and therefore the graph digest.

The API prefix is `/v1/projects/{project_id}/processes/{process_id}`:

- `GET /deliveries`: manifests, approval records, contracts and process cursor.
- `POST /completion-contracts`: `expected_process_version`,
  `expected_contract_version` (0 initially), `idempotency_key`,
  `required_human_approvers`, and `root_goal_id` when the process is unbound.
  Optional `criteria` must retain every required literal-true check.
- `POST /completion-contracts/{contract_id}:approve`:
  `expected_process_version`, `idempotency_key`.
- `POST /deliveries:prepare`: `expected_process_version`; idempotently builds a
  missing legacy manifest from actual current Integration PASS, never from
  caller-supplied evidence.
- `POST /deliveries/{delivery_id}:decide`: `decision` (ACCEPT/REJECT), `reason`,
  `idempotency_key`, `expected_version` and `expected_process_version`.

These endpoints are wired in production bootstrap. UI acceptance controls are
still part of the later workspace phase; API tests are not browser acceptance.
Schema 60 tests cover metadata, SQLite data preservation and PostgreSQL offline
DDL, not a real PostgreSQL upgrade. No migration is run automatically here.

## Alternatives rejected

- All TeamTasks verified ignores integration and acceptance.
- Chat delivery has no immutable artifact/evidence contract.
- LLM completion judgment is non-deterministic and not authorization.
- Reopening terminal state after rejection violates terminal immutability.

## Enforced invariants

- No Integration PASS means no Delivery READY.
- No accepted DeliveryManifest means no TERMINAL/COMPLETED.
- Delivery rejection remains non-terminal and produces explicit rework.
- Completion evaluation is deterministic, versioned and audited.
- Accepted evidence and artifact digests are immutable.

## Compatibility and migration

Existing verified TeamTasks/ProjectResources seed integration candidates but do
not imply completion. Existing deliverables remain visible while new projects
require a DeliveryManifest. Legacy projects are not retroactively completed
without explicit acceptance evidence.

## Test obligations

- Test missing/failed/stale verification blocks integration/delivery.
- Test Integration PASS/FAIL, retry, crash recovery and duplicate callback.
- Test manifest assembly, optimistic accept/reject and authorization.
- Reject completion without accepted delivery evidence.
- Test rejection to rework/replan and eventual new accepted version.
- Run full Recovery E2E through deterministic completion.

## Consequences

Completion becomes a durable business fact backed by integrated artifacts and
explicit acceptance rather than task counts or model opinion.
