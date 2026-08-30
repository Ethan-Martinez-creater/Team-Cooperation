from dataclasses import replace

import pytest
from test_project_orchestrator_runner import _events, _process, _stack


@pytest.mark.parametrize("change", ["event", "graph"])
def test_unapplied_pending_decision_is_retired_when_its_snapshot_changes(change):
    repository, service, _, runner, state = _stack(with_effect=False)
    first = runner.process_once(worker_id="worker-a")
    assert first.status.value == "RETRY"
    if change == "event":
        process = _process(repository)
        service.append_fact(process_id=process.process_id, event_id="risk-after-failure",
            event_type="risk.created", expected_version=process.version,
            expected_event_sequence=process.last_event_sequence, subject_type="risk", subject_id="risk",
            initiated_by="lead-a", executed_as="lead-a", correlation_id="risk", payload={})
    else:
        original = runner.snapshot_loader
        runner.snapshot_loader = lambda process: replace(original(process), graph_digest="sha256:" + "b" * 64)

    def forbidden(**kwargs):
        raise AssertionError("stale decision must not dispatch")

    runner.effect = forbidden
    second = runner.process_once(worker_id="worker-a")
    assert second.status.value == "STALE" and second.decision_id == first.decision_id
    assert not state["effects"]
    with repository.transaction() as connection:
        assert repository.decision(connection, first.decision_id).status.value == "STALE"
    assert len([event for event in _events(repository)
                if event.event_type == "project.orchestrator.decision_stale"]) == 1


def test_already_committed_effect_is_not_invalidated_by_its_own_transition():
    calls = []

    def crash():
        calls.append(True)
        if len(calls) == 1:
            raise RuntimeError("before ack")

    repository, _, _, runner, state = _stack(after_effect=crash)
    first = runner.process_once(worker_id="worker-a")
    assert first.status.value == "RETRY"
    runner.snapshot_loader = lambda _: (_ for _ in ()).throw(AssertionError("committed work must only ack"))
    second = runner.process_once(worker_id="worker-a")
    assert second.status.value == "APPLIED"
    assert len(state["effects"]) == 1
    assert not [event for event in _events(repository)
                if event.event_type == "project.orchestrator.decision_stale"]
