"""Intent Router Harness — enterprise shell over deepagent SDK.

Phase 2: classic runtime removed.  Use ``harness_v2`` for the
deepagent-backed runtime with progressive skill loading.
"""

from intent_router_harness.runtime import (
    PromptHarness,
    binding_matches,
    load_harness_spec,
    load_prompt_harness,
)
from intent_router_harness.schema import (
    DeepAgentConfig,
    EvalCase,
    ExperimentSpec,
    HarnessSpec,
    SkillBinding,
    Variant,
)
from intent_router_harness.contracts import (
    AssistantProtocolFrame,
    AssistantServiceResult,
    AssistantTraceEvent,
    PlannedTask,
    PlannerOutput,
    RecognitionPlan,
    RouterMessageRequest,
    SessionState,
    TaskCompletionRequest,
    TaskRuntimeState,
)
from intent_router_harness.service import (
    HarnessHealth,
    IntentRouterHarnessService,
    RegressionCaseSummary,
    RegressionSuiteSummary,
    RegressionValidationRequest,
    RegressionValidationResponse,
    ServiceConfigurationError,
)
from intent_router_harness.session_store import InMemorySessionStore
from intent_router_harness.regression import (
    RegressionCase,
    RegressionExpectation,
    RegressionStep,
    RegressionSuite,
    load_regression_suite,
    validate_case_transcripts,
    validate_step_transcript,
)

__all__ = [
    "AssistantProtocolFrame",
    "AssistantServiceResult",
    "AssistantTraceEvent",
    "DeepAgentConfig",
    "EvalCase",
    "ExperimentSpec",
    "HarnessHealth",
    "HarnessSpec",
    "InMemorySessionStore",
    "IntentRouterHarnessService",
    "PlannedTask",
    "PlannerOutput",
    "PromptHarness",
    "RecognitionPlan",
    "RegressionCase",
    "RegressionCaseSummary",
    "RegressionExpectation",
    "RegressionStep",
    "RegressionSuite",
    "RegressionSuiteSummary",
    "RegressionValidationRequest",
    "RegressionValidationResponse",
    "RouterMessageRequest",
    "ServiceConfigurationError",
    "SessionState",
    "SkillBinding",
    "TaskCompletionRequest",
    "TaskRuntimeState",
    "Variant",
    "binding_matches",
    "load_harness_spec",
    "load_prompt_harness",
    "load_regression_suite",
    "validate_case_transcripts",
    "validate_step_transcript",
]
