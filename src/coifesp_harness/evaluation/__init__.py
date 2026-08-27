"""Versioned, privacy-safe quality and policy regression evaluation."""

from .matcher import OutputMatchResult, RuleResult, SafeOutputMatcher
from .models import (
    EvaluationCase,
    EvaluationSuite,
    GateThresholds,
    MatchOperator,
    OutputRule,
    PolicyExpectation,
    RedTeamCategory,
    SensitiveValue,
)
from .reporting import (
    REPORT_SCHEMA_VERSION,
    CaseResult,
    EvaluationReport,
    GateAssessment,
    GateStatus,
    GateViolation,
)
from .runner import (
    AsyncDeterministicEvaluationRunner,
    AsyncEvaluationExecutor,
    RUNNER_VERSION,
    DeterministicEvaluationRunner,
    EvaluationExecutor,
    EvaluationObservation,
    RunContext,
)
from .registry import EvaluationDocumentError, EvaluationSuiteRegistry, TrustedEvaluationKey
from .executors import AgentLoopEvaluationExecutor, PolicyEngineEvaluationExecutor

__all__ = [
    "REPORT_SCHEMA_VERSION",
    "RUNNER_VERSION",
    "CaseResult",
    "AgentLoopEvaluationExecutor",
    "AsyncDeterministicEvaluationRunner",
    "AsyncEvaluationExecutor",
    "DeterministicEvaluationRunner",
    "EvaluationCase",
    "EvaluationExecutor",
    "EvaluationDocumentError",
    "EvaluationObservation",
    "EvaluationReport",
    "EvaluationSuite",
    "EvaluationSuiteRegistry",
    "GateAssessment",
    "GateStatus",
    "GateThresholds",
    "GateViolation",
    "MatchOperator",
    "OutputMatchResult",
    "OutputRule",
    "PolicyExpectation",
    "PolicyEngineEvaluationExecutor",
    "RedTeamCategory",
    "RuleResult",
    "RunContext",
    "SafeOutputMatcher",
    "SensitiveValue",
    "TrustedEvaluationKey",
]
