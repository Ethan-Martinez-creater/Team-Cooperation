import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ADR_DIR = ROOT / "docs" / "adr"
CONTRACT_PATH = ADR_DIR / "project-harness-contract-v1.json"

ADR_FILES = {
    "0001-project-process-vs-agent-run.md",
    "0002-project-work-graph.md",
    "0003-team-agent-contract.md",
    "0004-project-orchestrator.md",
    "0005-project-process-transition-matrix.md",
    "0006-project-budget-and-concurrency.md",
    "0007-agent-execution-identity.md",
    "0008-transactional-process-events.md",
    "0009-human-gates-and-input.md",
    "0010-capability-directory-integration.md",
    "0011-integration-and-delivery.md",
}


def _contract():
    return json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))


def test_gate_zero_has_all_accepted_adrs_and_normative_appendices():
    assert ADR_FILES <= {path.name for path in ADR_DIR.glob("*.md")}
    for name in sorted(ADR_FILES):
        body = (ADR_DIR / name).read_text(encoding="utf-8")
        assert "## Status\n\nAccepted." in body, name
    assert (ADR_DIR / "project-process-event-catalog-v1.md").is_file()
    assert (ADR_DIR / "project-harness-fixture-contract-v1.md").is_file()


def test_machine_contract_freezes_audited_enums():
    contract = _contract()
    assert contract["schema"] == "coifesp.project-harness-contract.v1"
    assert contract["phase"] == [
        "INTAKE", "ANALYSIS", "PLANNING", "EXECUTION", "INTEGRATION",
        "VERIFICATION", "DELIVERY", "TERMINAL",
    ]
    assert contract["status"] == [
        "READY", "RUNNING", "WAITING", "BLOCKED", "COMPLETED", "FAILED",
        "CANCELLED",
    ]
    assert contract["wait_reason"] == [
        "NONE", "HUMAN_INPUT", "HUMAN_APPROVAL", "TEAM_RESPONSE",
        "AGENT_RUN", "TOOL_JOB", "DEPENDENCY", "VERIFICATION", "SCHEDULE",
    ]
    assert contract["resource_propagation"] == [
        "team_private", "project_readonly", "portable",
    ]


def test_event_catalog_and_transition_targets_are_closed_and_valid():
    contract = _contract()
    facts = set(contract["domain_fact"])
    transition_keys = set()
    for item in contract["main_transition"]:
        assert item["domain_fact"] in facts
        assert item["transition_key"] not in transition_keys
        transition_keys.add(item["transition_key"])
        phase, status, wait_reason = item["to"]
        assert phase in contract["phase"]
        assert status in contract["status"]
        assert wait_reason in contract["wait_reason"]
    assert contract["version_policy"] == {
        "domain_fact_advances_event_sequence": True,
        "only_transition_advances_process_version": True,
        "planner_stale_inputs": [
            "process_version", "last_event_sequence", "graph_snapshot_digest",
        ],
    }


def test_policy_planner_identity_and_fixture_vocabularies_are_unique():
    contract = _contract()
    keys = [
        "project_execution_policy_fields",
        "orchestration_reason",
        "planner_command_type",
        "fixture_stimulus_origin",
        "fixture_fault_kind",
        "domain_fact",
    ]
    for key in keys:
        values = contract[key]
        assert values
        assert len(values) == len(set(values)), key
    assert contract["execution_identity"]["planner_executed_as"] == (
        "service:project-orchestrator"
    )
    assert contract["execution_identity"]["task_executed_as_prefix"] == (
        "team-agent:"
    )
