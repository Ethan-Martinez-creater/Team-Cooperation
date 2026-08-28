# Project Harness Fixture Contract v1

## Status and purpose

Accepted normative appendix for B2 fixtures and the future B6 evaluation
executor. A JSON file is design material until it validates against this
contract and every referenced event exists in the v1 event catalog.

## Executable scenario structure

An executable scenario contains:

```text
scenario_id, eval_ref, summary
initial_state
stimuli
expected_steps
required_outputs
forbidden_outcomes
safety_invariants
crash_and_recovery
```

`initial_state` is self-contained after loader normalization and explicitly
contains process triple/version/sequence, graph nodes/relations/digest,
contracts, artifacts, verifications, integration/delivery, execution policy and
usage, active runs/jobs, reservations, open gates/inputs and pending events/
outbox. Missing collections normalize to empty; missing scalars are invalid.
Cross-file implicit inheritance is forbidden.

## Stimulus versus observation

Each stimulus has stable `source_event_id`, `event_type`, `origin`,
`subject_ref`, `idempotency_key` and payload.

Allowed origins are:

```text
HUMAN TEAM AGENT_WORKER TOOL_WORKER TIMER FIXTURE_FAULT
```

Harness-generated transition facts, dispatch success, verification result,
delivery readiness and completion are outputs under observation; the fixture
must not inject them to make its own assertion pass. Fault injection is control
input, never a domain event.

## Expected steps

There is exactly one expected step for every stimulus, in order. A step records:

- expected process triple and relative version change (`0` or `+1`);
- relative event-sequence change;
- emitted canonical domain facts and transition key, if any;
- created/updated durable object references;
- commands/dispatches and their stable IDs;
- audit/outbox expectations.

No-transition windows use an explicit unchanged triple. Natural-language-only
“exactly once” assertions are invalid; they must name event, command,
reservation, run, manifest or side-effect idempotency keys.

## Identity and state rules

- Planner runs execute as `service:project-orchestrator`; task runs execute as
  the relevant `team-agent:<team-id>`.
- Every automatic run has both `initiated_by` and `executed_as`.
- `RUNNING` requires at least one actively running operation.
- Queued/recovering-only AgentRuns use `WAITING/AGENT_RUN`; ToolJobs use
  `WAITING/TOOL_JOB`.
- Contract acceptance, capacity reservation and readiness are separate facts.
- Fixtures use the exact ADR-0006 policy field names including `version`.
- Version expressions are structured operands, not prose strings.

## Loader and validation obligations

The B6 loader rejects unknown keys/enums/events, duplicate IDs, unresolved
references, missing expected steps, forbidden cross-file inheritance and
identity/state contradictions. It first produces one normalized scenario, then
the executor may construct fake repositories and a fake clock.

Fixtures remain completely offline and contain no SQL names, API paths, UUID
requirements, absolute paths, credentials or model/provider dependencies.
