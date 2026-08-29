# Project Process Event Catalog v2

## Status and compatibility

Accepted implementation appendix to ADR-0005, ADR-0008 and ADR-0009. It
extends, and does not rename or remove, the v1 catalog. Existing v1 envelopes
remain valid.

## Added domain facts

| Domain fact | Schema | Meaning | Immediate transition |
|---|---|---|---|
| `project.input.closed` | `v2` | An InputRequest was cancelled or expired | none; wake Orchestrator |
| `project.gate.closed` | `v2` | A Gate was cancelled or expired | none; wake Orchestrator |

The terminal object status, resolver identity, object version and stable
resolution idempotency key are committed with the fact. Resolving one object
never assigns the next ProjectProcess phase directly.

## Frozen non-main-chain selectors

| Domain fact | Transition selector | Target rule |
|---|---|---|
| `project.input.requested` | `human_input.opened` | same phase, `WAITING/HUMAN_INPUT` |
| `project.gate.opened` | `human_approval.opened` | same phase, `WAITING/HUMAN_APPROVAL` |

These selectors are legal only when the triple changes. Opening an additional
human object while an equal or higher-priority human wait is already active is
a triple-preserving fact with no selector and no process-version increment.
They may not overwrite `BLOCKED` or a non-human durable wait.

## Budget exhaustion transaction

Budget admission failure commits the following in one transaction:

1. `project.budget.exhausted` (triple-preserving fact);
2. an OPEN `BUDGET` ProjectGate whose decisions are exactly
   `INCREASE_BUDGET`, `REDUCE_SCOPE`, `TERMINATE`;
3. `project.gate.opened` with selector `human_approval.opened`;
4. Process state `WAITING/HUMAN_APPROVAL` and both Outbox rows.

The Planner and Team Agent cannot decide this Gate or update the policy.

## Writer validation

- Writers accept only catalogued facts and supported write schemas.
- A non-null selector must match the fact mapping.
- `project.input.closed` and `project.gate.closed` require schema `v2`.
- Unknown inbound schema versions remain durable for operator handling, but a
  local producer cannot create an unknown schema version.
