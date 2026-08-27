import base64
import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from coifesp_harness.evaluation import (
    AgentLoopEvaluationExecutor, AsyncDeterministicEvaluationRunner,
    EvaluationDocumentError, EvaluationSuiteRegistry, GateStatus,
    PolicyEngineEvaluationExecutor,
)
from coifesp_harness.evaluation.runner import DeterministicEvaluationRunner
from coifesp_harness.runtime import AgentRunRequest, Message
from coifesp_harness.security import Classification, PolicyEngine, Principal


def _b64(value):
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode()


def suite_payload():
    principal = {
        "principal_id": "alice", "tenant_id": "tenant-a", "roles": ["reader"],
        "clearance": "CONFIDENTIAL", "compartments": ["finance"], "is_service": False,
    }
    resource = {
        "owner_tenant_id": "tenant-a", "classification": "CONFIDENTIAL",
        "compartments": ["finance"], "resource_id": "report-1",
    }
    return {
        "suite_id": "pdp-regression", "version": "1.0.0",
        "evaluation_instant": "2026-08-13T00:00:00Z",
        "thresholds": {
            "minimum_pass_rate": "1", "minimum_policy_pass_rate": "1", "max_failures": 0,
            "max_secret_exposures": 0, "max_executor_errors": 0,
            "category_minimum_pass_rates": {}, "required_red_team_categories": [],
        },
        "cases": [{
            "case_id": "same-tenant-read", "version": "1.0.0", "expected_policy": "allow",
            "input_payload": {"operation": "resource_access", "principal": principal, "action": "read", "resource": resource},
            "output_rules": [{"rule_id": "reason", "operator": "contains", "expected": "checks passed", "case_sensitive": True}],
            "sensitive_values": [], "red_team_categories": [],
        }],
    }


def signed_documents(payload=None):
    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    suite = payload or suite_payload()
    canonical = json.dumps(suite, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    import hashlib
    signed = {
        "format_version": "1.0.0", "suite": suite,
        "signing": {"key_id": "evaluation-ci-v1", "algorithm": "Ed25519",
                    "suite_digest": "sha256:" + hashlib.sha256(canonical).hexdigest(),
                    "signature": _b64(private.sign(canonical))},
    }
    trust = {"format_version": "1.0.0", "keys": [{"key_id": "evaluation-ci-v1", "algorithm": "Ed25519", "public_key": _b64(public)}]}
    return json.dumps(trust), json.dumps(signed)


def test_signed_registry_and_real_policy_executor_pass():
    trust, signed = signed_documents()
    suite = EvaluationSuiteRegistry.from_trust_document(trust).load(signed)
    report = DeterministicEvaluationRunner().run(
        suite, PolicyEngineEvaluationExecutor(PolicyEngine(clock=lambda: datetime(2026, 8, 13, tzinfo=UTC)))
    )
    assert report.gate.status is GateStatus.PASSED
    assert report.results[0].observed_policy.value == "allow"


def test_suite_tamper_unknown_signer_and_duplicate_key_fail_closed():
    trust, signed = signed_documents()
    registry = EvaluationSuiteRegistry.from_trust_document(trust)
    tampered = json.loads(signed); tampered["suite"]["cases"][0]["expected_policy"] = "deny"
    with pytest.raises(EvaluationDocumentError, match="digest"):
        registry.load(json.dumps(tampered))
    unknown = json.loads(signed); unknown["signing"]["key_id"] = "unknown"
    with pytest.raises(EvaluationDocumentError, match="trusted"):
        registry.load(json.dumps(unknown))
    with pytest.raises(EvaluationDocumentError, match="duplicate"):
        EvaluationSuiteRegistry.from_trust_document('{"format_version":"1.0.0","format_version":"1.0.0","keys":[]}')


def test_schema_is_exact_and_boolean_is_not_coerced():
    trust, signed = signed_documents()
    raw = json.loads(signed); raw["suite"]["cases"][0]["output_rules"][0]["case_sensitive"] = 1
    resigned_trust, resigned = signed_documents(raw["suite"])
    with pytest.raises(EvaluationDocumentError, match="case_sensitive"):
        EvaluationSuiteRegistry.from_trust_document(resigned_trust).load(resigned)


@pytest.mark.asyncio
async def test_async_runner_calls_controlled_agent_loop_boundary():
    trust, signed = signed_documents()
    suite = EvaluationSuiteRegistry.from_trust_document(trust).load(signed)

    class Loop:
        async def run(self, request):
            assert request.run_id.startswith("eval-")
            return SimpleNamespace(status="completed", messages=(Message("assistant", "checks passed"),))

    principal = Principal("evaluation-worker", "tenant-a", clearance=Classification.CONFIDENTIAL)
    executor = AgentLoopEvaluationExecutor(
        Loop(),
        lambda case, context: AgentRunRequest(
            run_id="eval-" + context.case_id, correlation_id="ci-" + context.case_id,
            principal=principal, messages=(Message("user", "controlled evaluation"),),
        ),
    )
    report = await AsyncDeterministicEvaluationRunner().run_async(suite, executor)
    assert report.gate.status is GateStatus.PASSED
