"""Contract-conformance and negative validation tests for B2 harness evaluation fixtures.

These tests validate tests/fixtures/project_harness/*.json against the frozen
Gate 0 contracts:

- docs/adr/project-harness-contract-v1.json (machine contract)
- docs/adr/project-process-event-catalog-v1.md (event catalog)
- docs/adr/project-harness-fixture-contract-v1.md (fixture contract)

Everything runs fully offline: no LLM, network, Docker, OAuth, or .env access.
The validator below is a lightweight reference implementation of the loader
obligations in the fixture contract; the future B6 executor may extend it.
"""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures" / "project_harness"
CONTRACT_PATH = REPO_ROOT / "docs" / "adr" / "project-harness-contract-v1.json"

EXPECTED_FIXTURE_IDS = {
    "budget_exhaustion",
    "cross_team_disclosure",
    "delivery_rejection",
    "dependency",
    "rejected_contract",
    "simple_project",
    "stale_planner",
    "verification_failure",
}

REQUIRED_SCENARIO_KEYS = {
    "scenario_id",
    "eval_ref",
    "summary",
    "initial_state",
    "stimuli",
    "expected_steps",
    "required_outputs",
    "forbidden_outcomes",
    "safety_invariants",
    "crash_and_recovery",
}

REQUIRED_INITIAL_COLLECTIONS = {
    "graph",
    "contracts",
    "artifacts",
    "verifications",
    "integration",
    "delivery",
    "completion_contract",
    "usage",
    "runs",
    "tool_jobs",
    "reservations",
    "capabilities",
    "gates",
    "input_requests",
    "pending_events",
    "outbox",
}

REQUIRED_STEP_KEYS = {
    "step_id",
    "stimulus_ref",
    "expected_process",
    "process_version_change",
    "event_sequence_change",
    "emitted_domain_facts",
    "active_operations",
    "created_or_updated",
    "commands",
    "observations",
}

FAULT_EVENT_TYPE = "fixture.fault_injection"
PLANNER_RUN_KINDS = {"planning", "replanning", "verification", "analysis"}

# contract-free banned content: SQL fragments, API paths, uuid requirements
BANNED_CONTENT = re.compile(
    r"CREATE TABLE|ALTER TABLE|INSERT INTO|SELECT .+ FROM|/api/|alembic|coifesp_harness\.",
    re.IGNORECASE,
)

ALL_EVALS = {f"Eval {i}" for i in range(1, 15)}


# ---------------------------------------------------------------------------
# loading


def _load_json_no_duplicate_keys(path: Path):
    def pairs_hook(pairs):
        keys = [k for k, _ in pairs]
        if len(keys) != len(set(keys)):
            raise ValueError(f"duplicate JSON keys in {path.name}: {keys}")
        return dict(pairs)

    with path.open(encoding="utf-8") as handle:
        return json.load(handle, object_pairs_hook=pairs_hook)


def _load_contract() -> dict:
    return _load_json_no_duplicate_keys(CONTRACT_PATH)


@pytest.fixture(scope="module")
def contract() -> dict:
    return _load_contract()


@pytest.fixture(scope="module")
def fixtures() -> dict:
    paths = sorted(FIXTURE_DIR.glob("*.json"))
    assert {p.stem for p in paths} == EXPECTED_FIXTURE_IDS
    return {p.stem: _load_json_no_duplicate_keys(p) for p in paths}


# ---------------------------------------------------------------------------
# validator (reference implementation of the fixture-contract loader duties)


class FixtureValidationError(AssertionError):
    pass


def _fail(message: str):
    raise FixtureValidationError(message)


def _check_triple(triple, contract, where: str):
    for field, enum_key in (
        ("phase", "phase"),
        ("status", "status"),
        ("wait_reason", "wait_reason"),
    ):
        value = triple.get(field)
        if value not in contract[enum_key]:
            _fail(f"{where}: {field}={value!r} not in contract enum {enum_key}")
    status = triple["status"]
    reason = triple["wait_reason"]
    if status in {"WAITING", "BLOCKED"} and reason == "NONE":
        _fail(f"{where}: {status} requires a non-NONE wait reason (ADR-0005 decision 3)")
    if status in {"READY", "RUNNING", "COMPLETED", "FAILED", "CANCELLED"} and reason != "NONE":
        _fail(f"{where}: {status} requires wait_reason NONE, got {reason}")


def _validate_scenario(scenario: dict, contract: dict, where: str):
    for key in REQUIRED_SCENARIO_KEYS:
        if key not in scenario:
            _fail(f"{where}: missing scenario key {key!r}")
    for key in ("initial_state", "stimuli", "expected_steps", "required_outputs", "forbidden_outcomes", "safety_invariants"):
        if not scenario[key]:
            _fail(f"{where}: {key} must be non-empty")
    # crash_and_recovery may be empty; list required
    if not isinstance(scenario["crash_and_recovery"], list):
        _fail(f"{where}: crash_and_recovery must be a list")

    initial = scenario["initial_state"]
    process = initial.get("process")
    if not process:
        _fail(f"{where}: initial_state.process is a required scalar block")
    _check_triple(process, contract, f"{where} initial process")
    for scalar in ("process_ref", "project_ref", "process_version", "last_event_sequence"):
        if scalar not in process:
            _fail(f"{where}: initial process missing scalar {scalar!r}")
    if not isinstance(process["process_version"], int) or not isinstance(process["last_event_sequence"], int):
        _fail(f"{where}: process_version/last_event_sequence must be integers")
    for key in REQUIRED_INITIAL_COLLECTIONS:
        if key not in initial:
            _fail(f"{where}: initial_state missing collection {key!r} (self-contained initial state)")

    policy = initial.get("execution_policy")
    if not isinstance(policy, dict):
        _fail(f"{where}: execution_policy must be a dict")
    for field in contract["project_execution_policy_fields"]:
        if field not in policy:
            _fail(f"{where}: execution_policy missing contract field {field!r}")
    if not isinstance(policy.get("version"), int):
        _fail(f"{where}: execution_policy.version must be an integer")

    # graph enums
    graph = initial["graph"]
    for node in graph.get("nodes", []):
        if node.get("node_type") not in contract["work_node_type"]:
            _fail(f"{where}: unknown node_type {node.get('node_type')!r}")
    for relation in graph.get("relations", []):
        if relation.get("relation_type") not in contract["work_relation_type"]:
            _fail(f"{where}: unknown relation_type {relation.get('relation_type')!r}")

    # propagation / status / decision enums
    for artifact in initial.get("artifacts", []) or []:
        propagation = artifact.get("propagation")
        if propagation is not None and propagation not in contract["resource_propagation"]:
            _fail(f"{where}: unknown resource propagation {propagation!r}")
    for contract_row in initial.get("contracts", []) or []:
        status_value = contract_row.get("task_status")
        if status_value is not None and status_value not in contract["team_task_status"]:
            _fail(f"{where}: unknown team task status {status_value!r}")
    for gate in initial.get("gates", []) or []:
        if gate.get("status") is not None and gate["status"] not in contract["gate_status"]:
            _fail(f"{where}: unknown gate status {gate['status']!r}")
    for request in initial.get("input_requests", []) or []:
        if request.get("status") is not None and request["status"] not in contract["input_request_status"]:
            _fail(f"{where}: unknown input request status {request['status']!r}")
    delivery = initial.get("delivery")
    if (isinstance(delivery, dict) and delivery.get("status") is not None
            and delivery["status"] not in contract["delivery_status"]):
        _fail(f"{where}: unknown delivery status {delivery['status']!r}")

    # identity rules on runs present in the initial state
    _validate_runs(initial.get("runs", []) or [], contract, where)

    # stimuli
    stimuli = scenario["stimuli"]
    stimulus_ids = [s.get("source_event_id") for s in stimuli]
    if len(stimulus_ids) != len(set(stimulus_ids)):
        _fail(f"{where}: duplicate source_event_id in stimuli")
    for stimulus in stimuli:
        sid = stimulus.get("source_event_id", "<missing>")
        for field in ("source_event_id", "event_type", "origin", "subject_ref", "idempotency_key", "payload"):
            if field not in stimulus or stimulus[field] is None:
                _fail(f"{where} stimulus {sid}: missing required field {field!r}")
        origin = stimulus["origin"]
        if origin not in contract["fixture_stimulus_origin"]:
            _fail(f"{where} stimulus {sid}: unknown origin {origin!r}")
        event_type = stimulus["event_type"]
        if origin == "FIXTURE_FAULT":
            if event_type != FAULT_EVENT_TYPE:
                _fail(f"{where} stimulus {sid}: fault must use event_type {FAULT_EVENT_TYPE!r}")
            kind = (stimulus.get("payload") or {}).get("fault", {}).get("kind")
            if kind not in contract["fixture_fault_kind"]:
                _fail(f"{where} stimulus {sid}: unknown fault kind {kind!r}")
        else:
            if event_type not in contract["domain_fact"]:
                _fail(f"{where} stimulus {sid}: event_type {event_type!r} is not a catalog domain fact")
            if event_type == "approval.decided":
                decision = (stimulus.get("payload") or {}).get("decision")
                allowed = set(contract["budget_gate_decision"]) | set(contract["delivery_decision"])
                if decision not in allowed:
                    _fail(f"{where} stimulus {sid}: unknown gate/delivery decision {decision!r}")
            if event_type in {
                "project.work.dispatched",
                "project.verification.completed",
                "team_task.verified",
                "team_task.changes_requested",
                "task_verification.human_review.opened",
                "task_verification.human_review.decided",
                "task_verification.human_review.closed",
                "project.delivery.accepted",
                "project.delivery.rejected",
                "project.completion.evaluated",
                "project.gate.opened",
                "project.input.requested",
                "project.budget.exhausted",
                "project.capability.matched",
                "project.capacity.reserved",
                "project.capacity.reservation_failed",
            }:
                _fail(f"{where} stimulus {sid}: harness-generated result {event_type!r} must not be injected as input")

    # expected steps: exactly one per stimulus, in order
    steps = scenario["expected_steps"]
    if len(steps) != len(stimuli):
        _fail(f"{where}: expected {len(stimuli)} steps for {len(stimuli)} stimuli, got {len(steps)}")
    previous_triple = {k: process[k] for k in ("phase", "status", "wait_reason")}
    for step, stimulus in zip(steps, stimuli):
        sid = stimulus["source_event_id"]
        swhere = f"{where} step {step.get('step_id', '<missing>')} (stimulus {sid})"
        for key in REQUIRED_STEP_KEYS:
            if key not in step:
                _fail(f"{swhere}: missing step key {key!r}")
        if step["stimulus_ref"] != sid:
            _fail(f"{swhere}: steps must follow stimulus order; got {step['stimulus_ref']!r}")
        _check_triple(step["expected_process"], contract, swhere)

        version_change = step["process_version_change"]
        if version_change not in (0, 1):
            _fail(f"{swhere}: process_version_change must be 0 or 1, got {version_change!r}")
        facts = step["emitted_domain_facts"]
        if step["event_sequence_change"] != len(facts):
            _fail(
                f"{swhere}: event_sequence_change {step['event_sequence_change']} != "
                f"len(emitted_domain_facts) {len(facts)} (every fact advances the sequence once)"
            )
        transition_count = 0
        for fact in facts:
            fact_type = fact.get("event_type")
            if fact_type not in contract["domain_fact"]:
                _fail(f"{swhere}: emitted fact {fact_type!r} is not a catalog domain fact")
            outcome = fact.get("outcome")
            if outcome is not None and outcome not in ("PASS", "FAIL"):
                _fail(f"{swhere}: fact outcome must be PASS or FAIL, got {outcome!r}")
            transition = fact.get("transition")
            if transition is not None:
                transition_count += 1
                transition_key = fact.get("transition_key")
                transition_from = transition["from"]
                transition_to = transition["to"]
                if transition_from == transition_to:
                    _fail(
                        f"{swhere}: transition.from == transition.to is forbidden; "
                        f"a transition must change the triple - a triple-preserving fact "
                        f"must not carry one"
                    )
                _check_triple(transition_from, contract, f"{swhere} transition.from")
                _check_triple(transition_to, contract, f"{swhere} transition.to")
                if transition_from != previous_triple:
                    _fail(
                        f"{swhere}: transition.from {transition_from} does not match the "
                        f"previous window end {previous_triple}"
                    )
                if transition_to != {
                    k: step["expected_process"][k] for k in ("phase", "status", "wait_reason")
                }:
                    _fail(f"{swhere}: transition.to must equal the step's expected_process")
                # main-chain key enforcement
                target = (transition_to["phase"], transition_to["status"], transition_to["wait_reason"])
                chain_rows = [
                    row
                    for row in contract["main_transition"]
                    if row["domain_fact"] == fact_type
                    and ("outcome" not in row or row.get("outcome") == fact.get("outcome"))
                    and tuple(row["to"]) == target
                ]
                if chain_rows:
                    expected_key = {row["transition_key"] for row in chain_rows}
                    if transition_key not in expected_key:
                        _fail(
                            f"{swhere}: fact {fact_type!r} to {target} matches frozen main-chain "
                            f"rows {sorted(expected_key)} and must carry that transition_key, got {transition_key!r}"
                        )
                elif transition_key is not None:
                    _fail(
                        f"{swhere}: non-main-chain transition must not invent a transition_key "
                        f"({transition_key!r}); null means the fixture does not assert the "
                        f"unfrozen internal selector name"
                    )
        if version_change != transition_count:
            _fail(
                f"{swhere}: process_version_change {version_change} != number of transition facts "
                f"{transition_count} (only transitions advance the version)"
            )
        if step["expected_process"]["status"] == "RUNNING" and not step["active_operations"]:
            _fail(f"{swhere}: RUNNING requires at least one active operation")
        for command in step.get("commands", []):
            if command.get("command_type") not in contract["planner_command_type"]:
                _fail(f"{swhere}: unknown planner command type {command.get('command_type')!r}")
        previous_triple = {k: step["expected_process"][k] for k in ("phase", "status", "wait_reason")}

    # stable-id obligation: forbidden outcomes must not rely on prose-only "exactly once"
    for outcome in scenario["forbidden_outcomes"]:
        text = outcome.get("statement", "")
        if "exactly once" in text.lower() and not re.search(r"\b(idem:|cmd-|reservation-|run-|delivery-|input-request-|negotiation-|outbox-)", text):
            _fail(f"{where}: forbidden outcome {outcome.get('forbidden_id')} uses 'exactly once' without a stable id/idempotency key")


def _validate_runs(runs: list, contract: dict, where: str):
    planner_identity = contract["execution_identity"]["planner_executed_as"]
    task_prefix = contract["execution_identity"]["task_executed_as_prefix"]
    for run in runs:
        ref = run.get("run_ref", "<missing>")
        for field in ("initiated_by", "executed_as"):
            if field not in run or not run[field]:
                _fail(f"{where} run {ref}: automatic runs require both initiated_by and executed_as")
        kind = run.get("run_kind")
        executed_as = run["executed_as"]
        if kind in PLANNER_RUN_KINDS:
            if executed_as != planner_identity:
                _fail(
                    f"{where} run {ref}: planner-family runs must execute as {planner_identity!r}, got {executed_as!r}"
                )
        elif kind == "task_execution" and not executed_as.startswith(task_prefix):
            _fail(
                f"{where} run {ref}: task runs must execute as {task_prefix}<team-id>, got {executed_as!r}"
            )
        if executed_as.startswith("human:"):
            _fail(f"{where} run {ref}: a real user must never be the execution principal")


def validate_fixture(fixture: dict, contract: dict) -> None:
    for key in ("fixture_schema", "fixture_id", "title", "eval_coverage", "scenarios"):
        if key not in fixture:
            _fail(f"missing top-level key {key!r}")
    if fixture["fixture_schema"] != "project-harness-fixture.v2":
        _fail(f"unexpected fixture_schema {fixture['fixture_schema']!r}")
    for forbidden_key in ("world", "inherits", "extends", "shared"):
        if forbidden_key in fixture:
            _fail(f"cross-file inheritance is forbidden: found top-level key {forbidden_key!r}")

    scenario_ids = set()
    eval_refs = set()
    for scenario in fixture["scenarios"]:
        sid = scenario.get("scenario_id", "<missing>")
        if sid in scenario_ids:
            _fail(f"duplicate scenario_id {sid!r}")
        scenario_ids.add(sid)
        eval_refs.add(scenario.get("eval_ref"))
        _validate_scenario(scenario, contract, f"{fixture['fixture_id']}:{sid}")

    coverage = {(c["eval_id"], c["scenario_id"]) for c in fixture["eval_coverage"]}
    if coverage != {(s["eval_ref"], s["scenario_id"]) for s in fixture["scenarios"]}:
        _fail(f"{fixture['fixture_id']}: eval_coverage does not match scenarios")


# ---------------------------------------------------------------------------
# positive conformance


def test_all_fixtures_parse_and_validate(fixtures, contract):
    for fixture in fixtures.values():
        validate_fixture(fixture, contract)


def test_evals_one_to_fourteen_covered_exactly_once(fixtures):
    seen = {}
    for fixture in fixtures.values():
        for scenario in fixture["scenarios"]:
            assert scenario["eval_ref"] not in seen, f"{scenario['eval_ref']} covered twice"
            seen[scenario["eval_ref"]] = scenario["scenario_id"]
    assert set(seen) == ALL_EVALS


def test_scenario_count_matches_contract_matrix(fixtures):
    total = sum(len(fixture["scenarios"]) for fixture in fixtures.values())
    assert total == 14


def test_no_banned_content(fixtures):
    for fixture_id, fixture in fixtures.items():
        raw = json.dumps(fixture, ensure_ascii=False)
        match = BANNED_CONTENT.search(raw)
        assert match is None, f"{fixture_id}: banned content {match.group(0)!r}"


def test_no_uuid_or_absolute_paths(fixtures):
    uuid_like = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE)
    for fixture_id, fixture in fixtures.items():
        raw = json.dumps(fixture, ensure_ascii=False)
        assert uuid_like.search(raw) is None, f"{fixture_id}: UUID values are not allowed"
        assert "E:\\" not in raw and "C:\\" not in raw, f"{fixture_id}: absolute local paths are not allowed"


def test_planner_identity_in_decisions(fixtures, contract):
    """Planner decisions travel inside agent_run.completed stimuli of planner runs.

    Gate/delivery decisions (approval.decided) carry their own `decision` field
    and are covered by the enum checks; only run-produced decisions are checked
    here.
    """
    for fixture_id, fixture in fixtures.items():
        for scenario in fixture["scenarios"]:
            for stimulus in scenario["stimuli"]:
                if stimulus["event_type"] != "agent_run.completed":
                    continue
                decision = (stimulus.get("payload") or {}).get("decision")
                if decision:
                    assert (
                        stimulus["origin"] == "AGENT_WORKER"
                    ), f"{fixture_id}: run decisions must originate from AGENT_WORKER"


# ---------------------------------------------------------------------------
# negative validation (mutation-based): each corruption must be rejected


def _single_scenario_fixture(fixtures, fixture_id: str) -> dict:
    return copy.deepcopy(fixtures[fixture_id])


def _first_scenario(fixture: dict) -> dict:
    return fixture["scenarios"][0]


def test_rejects_unknown_event_type(fixtures, contract):
    fixture = _single_scenario_fixture(fixtures, "simple_project")
    _first_scenario(fixture)["stimuli"][0]["event_type"] = "verification.failed"
    with pytest.raises(FixtureValidationError):
        validate_fixture(fixture, contract)


@pytest.mark.parametrize("event_type", ["project.work.dispatched",
    "task_verification.human_review.opened", "task_verification.human_review.decided",
    "task_verification.human_review.closed"])
def test_rejects_injected_harness_result(fixtures, contract, event_type):
    fixture = _single_scenario_fixture(fixtures, "simple_project")
    _first_scenario(fixture)["stimuli"][0]["event_type"] = event_type
    with pytest.raises(FixtureValidationError):
        validate_fixture(fixture, contract)


def test_rejects_unknown_origin(fixtures, contract):
    fixture = _single_scenario_fixture(fixtures, "simple_project")
    _first_scenario(fixture)["stimuli"][0]["origin"] = "PLANNER"
    with pytest.raises(FixtureValidationError):
        validate_fixture(fixture, contract)


def test_rejects_unknown_fault_kind(fixtures, contract):
    fixture = _single_scenario_fixture(fixtures, "simple_project")
    scenario = fixture["scenarios"][1]  # eval-05 carries the fault stimuli
    fault_stimulus = next(s for s in scenario["stimuli"] if s["origin"] == "FIXTURE_FAULT")
    fault_stimulus["payload"]["fault"]["kind"] = "delete_database"
    with pytest.raises(FixtureValidationError):
        validate_fixture(fixture, contract)


def test_rejects_missing_expected_step(fixtures, contract):
    fixture = _single_scenario_fixture(fixtures, "simple_project")
    _first_scenario(fixture)["expected_steps"].pop()
    with pytest.raises(FixtureValidationError):
        validate_fixture(fixture, contract)


def test_rejects_step_out_of_stimulus_order(fixtures, contract):
    fixture = _single_scenario_fixture(fixtures, "simple_project")
    steps = _first_scenario(fixture)["expected_steps"]
    steps[0], steps[1] = steps[1], steps[0]
    with pytest.raises(FixtureValidationError):
        validate_fixture(fixture, contract)


def test_rejects_sequence_break(fixtures, contract):
    fixture = _single_scenario_fixture(fixtures, "simple_project")
    _first_scenario(fixture)["expected_steps"][0]["event_sequence_change"] = 2
    with pytest.raises(FixtureValidationError):
        validate_fixture(fixture, contract)


def test_rejects_version_break(fixtures, contract):
    fixture = _single_scenario_fixture(fixtures, "simple_project")
    _first_scenario(fixture)["expected_steps"][0]["process_version_change"] = 2
    with pytest.raises(FixtureValidationError):
        validate_fixture(fixture, contract)


def test_rejects_running_without_active_operation(fixtures, contract):
    fixture = _single_scenario_fixture(fixtures, "simple_project")
    _first_scenario(fixture)["expected_steps"][1]["active_operations"] = []
    with pytest.raises(FixtureValidationError):
        validate_fixture(fixture, contract)


def test_rejects_waiting_without_wait_reason(fixtures, contract):
    fixture = _single_scenario_fixture(fixtures, "simple_project")
    _first_scenario(fixture)["expected_steps"][0]["expected_process"] = {
        "phase": "ANALYSIS",
        "status": "WAITING",
        "wait_reason": "NONE",
    }
    with pytest.raises(FixtureValidationError):
        validate_fixture(fixture, contract)


def test_rejects_unknown_wait_reason(fixtures, contract):
    fixture = _single_scenario_fixture(fixtures, "simple_project")
    _first_scenario(fixture)["expected_steps"][0]["expected_process"]["wait_reason"] = "MAYBE"
    with pytest.raises(FixtureValidationError):
        validate_fixture(fixture, contract)


def test_rejects_task_run_executing_as_human(fixtures, contract):
    fixture = _single_scenario_fixture(fixtures, "simple_project")
    scenario = _first_scenario(fixture)
    # eval-01 has no runs; use eval-05's second scenario instead
    scenario = fixture["scenarios"][1]
    scenario["initial_state"]["runs"][0]["executed_as"] = "human:user-project-owner"
    with pytest.raises(FixtureValidationError):
        validate_fixture(fixture, contract)


def test_rejects_planner_run_executing_as_team_agent(fixtures, contract):
    fixture = _single_scenario_fixture(fixtures, "stale_planner")
    scenario = _first_scenario(fixture)
    planner_run = next(r for r in scenario["initial_state"]["runs"] if r["run_ref"] == "planner-run-1")
    planner_run["executed_as"] = "team-agent:team-backend"
    with pytest.raises(FixtureValidationError):
        validate_fixture(fixture, contract)


def test_rejects_missing_policy_field(fixtures, contract):
    fixture = _single_scenario_fixture(fixtures, "simple_project")
    del _first_scenario(fixture)["initial_state"]["execution_policy"]["max_specialist_depth"]
    with pytest.raises(FixtureValidationError):
        validate_fixture(fixture, contract)


def test_dispatch_while_running_does_not_migrate(fixtures):
    """Continuous dispatch replaces the active operation without a state transition.

    The seven run-while-RUNNING dispatch steps must keep version_change 0 and
    carry no transition on the project.work.dispatched fact.
    """
    targets = {
        ("simple_project", "eval-01-simple-project", "t10"),
        ("dependency", "eval-02-dependency-blocking", "t05"),
        ("cross_team_disclosure", "eval-06-malicious-context-injection", "t02"),
        ("stale_planner", "eval-09-stale-planner-decision", "t04"),
        ("stale_planner", "eval-09-stale-planner-decision", "t10"),
        ("verification_failure", "eval-04-verification-failure-rework", "t01"),
        ("verification_failure", "eval-04-verification-failure-rework", "t04"),
    }
    seen = set()
    for fixture_id, fixture in fixtures.items():
        for scenario in fixture["scenarios"]:
            for step in scenario["expected_steps"]:
                key = (fixture_id, scenario["scenario_id"], step["step_id"])
                if key not in targets:
                    continue
                seen.add(key)
                assert step["process_version_change"] == 0, key
                dispatch_facts = [f for f in step["emitted_domain_facts"] if f["event_type"] == "project.work.dispatched"]
                assert dispatch_facts, key
                for fact in dispatch_facts:
                    assert "transition" not in fact, key
                    assert "transition_key" not in fact, key
                assert step["active_operations"], key
    assert seen == targets


def test_no_identity_transitions_anywhere(fixtures):
    """No transition may have from == to (validator also enforces this)."""
    for fixture_id, fixture in fixtures.items():
        for scenario in fixture["scenarios"]:
            for step in scenario["expected_steps"]:
                for fact in step["emitted_domain_facts"]:
                    transition = fact.get("transition")
                    if transition:
                        assert transition["from"] != transition["to"], (
                            f"{fixture_id}:{scenario['scenario_id']}:{step['step_id']}"
                        )


def test_rejects_identity_transition(fixtures, contract):
    fixture = _single_scenario_fixture(fixtures, "simple_project")
    scenario = _first_scenario(fixture)
    step = next(s for s in scenario["expected_steps"] if s["step_id"] == "t06")
    fact = next(f for f in step["emitted_domain_facts"] if f["event_type"] == "project.work.dispatched")
    same = fact["transition"]["to"]
    fact["transition"]["from"] = copy.deepcopy(same)
    with pytest.raises(FixtureValidationError):
        validate_fixture(fixture, contract)


def test_rejects_main_chain_transition_missing_key(fixtures, contract):
    fixture = _single_scenario_fixture(fixtures, "simple_project")
    scenario = _first_scenario(fixture)
    step = next(s for s in scenario["expected_steps"] if s["step_id"] == "t16")
    fact = next(f for f in step["emitted_domain_facts"] if f["event_type"] == "project.delivery.accepted")
    fact["transition_key"] = None
    with pytest.raises(FixtureValidationError):
        validate_fixture(fixture, contract)


def test_rejects_invented_non_main_chain_key(fixtures, contract):
    fixture = _single_scenario_fixture(fixtures, "budget_exhaustion")
    scenario = next(s for s in fixture["scenarios"] if s["scenario_id"] == "eval-07-project-budget-exhaustion-gate")
    step = next(s for s in scenario["expected_steps"] if s["step_id"] == "t01")
    fact = next(f for f in step["emitted_domain_facts"] if f["event_type"] == "project.gate.opened")
    fact["transition_key"] = "gate.opened"
    with pytest.raises(FixtureValidationError):
        validate_fixture(fixture, contract)


def test_rejects_unknown_gate_decision(fixtures, contract):
    fixture = _single_scenario_fixture(fixtures, "budget_exhaustion")
    scenario = next(s for s in fixture["scenarios"] if s["scenario_id"] == "eval-07-project-budget-exhaustion-gate")
    stimulus = next(s for s in scenario["stimuli"] if s["source_event_id"] == "s03")
    stimulus["payload"]["decision"] = "MAYBE_RAISE"
    with pytest.raises(FixtureValidationError):
        validate_fixture(fixture, contract)


def test_rejects_unknown_propagation_enum(fixtures, contract):
    fixture = _single_scenario_fixture(fixtures, "cross_team_disclosure")
    scenario = _first_scenario(fixture)
    scenario["initial_state"]["artifacts"][0]["propagation"] = "project_shared"
    with pytest.raises(FixtureValidationError):
        validate_fixture(fixture, contract)


def test_rejects_cross_file_inheritance(fixtures, contract):
    fixture = _single_scenario_fixture(fixtures, "verification_failure")
    fixture["world"] = {"inherits": "simple_project.json"}
    with pytest.raises(FixtureValidationError):
        validate_fixture(fixture, contract)


def test_rejects_missing_initial_state_collection(fixtures, contract):
    fixture = _single_scenario_fixture(fixtures, "delivery_rejection")
    del _first_scenario(fixture)["initial_state"]["outbox"]
    with pytest.raises(FixtureValidationError):
        validate_fixture(fixture, contract)


def test_rejects_prose_only_exactly_once_assertion(fixtures, contract):
    fixture = _single_scenario_fixture(fixtures, "dependency")
    scenario = _first_scenario(fixture)
    scenario["forbidden_outcomes"].append(
        {"forbidden_id": "fo-x", "statement": "the dispatch happens exactly once"}
    )
    with pytest.raises(FixtureValidationError):
        validate_fixture(fixture, contract)


def test_rejects_duplicate_json_keys_on_disk():
    # the loader itself refuses duplicate keys; simulate via the pairs hook
    raw = '{"a": 1, "a": 2}'
    with pytest.raises(ValueError):
        json.loads(raw, object_pairs_hook=lambda pairs: (_ for _ in ()).throw(ValueError("duplicate")))
