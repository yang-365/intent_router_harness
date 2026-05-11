from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from intent_router_harness.assistant_protocol import (
    ProtocolAssertionError,
    parse_sse_text,
)
from intent_router_harness.assistant_service import AssistantProtocolService
from intent_router_harness.contracts import (
    AssistantServiceResult,
    RouterMessageRequest,
    TaskCompletionRequest,
)
from intent_router_harness.llm import LLMClient, LLMRequestError
from intent_router_harness.planner import LLMMessagePlanner, MessagePlanner
from intent_router_harness.regression import (
    RegressionCase,
    RegressionSuite,
    load_regression_suite,
    validate_case_transcripts,
    validate_step_transcript,
)
from intent_router_harness.runtime import PromptHarness, load_prompt_harness
from intent_router_harness.executor import LLMWorkflowExecutor
from intent_router_harness.tool_runtime import load_command_tools
from intent_router_harness.workflow import WorkflowToolClient, load_workflow_tool_specs
from intent_router_harness.workflow_hooks import load_workflow_hooks

logger = logging.getLogger(__name__)


class ServiceConfigurationError(RuntimeError):
    """Raised when a harness service cannot be constructed."""


class HarnessHealth(BaseModel):
    """Static service metadata for health checks."""

    name: str
    version: str
    enabled: bool


class RegressionCaseSummary(BaseModel):
    """Public summary of one regression case."""

    id: str
    title: str
    status: str
    step_count: int
    tags: list[str] = Field(default_factory=list)


class RegressionSuiteSummary(BaseModel):
    """Public summary of a loaded regression suite."""

    version: str
    source_document: str
    primary_mode: str
    event_filter: list[str]
    cases: list[RegressionCaseSummary]


class RegressionValidationRequest(BaseModel):
    """Request for validating one step or one full case transcript."""

    case_id: str
    step_name: str | None = None
    sse_text: str | None = None
    transcripts: dict[str, str] = Field(default_factory=dict)


class RegressionValidationResponse(BaseModel):
    """Regression transcript validation result."""

    ok: bool
    case_id: str
    step_name: str | None = None
    errors: list[str] = Field(default_factory=list)


class IntentRouterHarnessService:
    """Application service boundary around prompt harness operations."""

    def __init__(
        self,
        harness: PromptHarness,
        *,
        regression_suite: RegressionSuite | None = None,
        llm_client: LLMClient | None = None,
        message_planner: MessagePlanner | None = None,
        workflow_client: WorkflowToolClient | None = None,
    ) -> None:
        self.harness = harness
        self.regression_suite = regression_suite
        self.llm_client = llm_client
        self.workflow_client = workflow_client
        workflow_hooks = load_workflow_hooks(list(harness.hook_roots))
        command_tools = load_command_tools(list(harness.tool_roots))
        if workflow_client is not None and hasattr(workflow_client, "tool"):
            workflow_client.tool = command_tools.get("workflow-api-call")
        workflow_tools = load_workflow_tool_specs(
            harness.skills,
            allowed_urls=tuple(harness.spec.workflow.allowed_urls),
        )
        planner = message_planner
        if planner is None and llm_client is not None:
            planner = LLMMessagePlanner(harness=harness, llm_client=llm_client)
        executor = None
        if llm_client is not None and workflow_client is not None:
            executor = LLMWorkflowExecutor(
                llm_client=llm_client,
                skill_library=harness.skills,
                workflow_client=workflow_client,
                workflow_tools=workflow_tools,
                workflow_hooks=workflow_hooks,
            )
        self.assistant = (
            AssistantProtocolService(
                planner=planner,
                executor=executor,
            )
            if planner is not None
            else None
        )

    @classmethod
    def from_spec(
        cls,
        spec_path: str | Path,
        *,
        skill_roots: list[str] | None = None,
        regression_suite_path: str | Path | None = None,
        llm_client: LLMClient | None = None,
        message_planner: MessagePlanner | None = None,
        workflow_client: WorkflowToolClient | None = None,
    ) -> "IntentRouterHarnessService":
        """Load a service from a harness spec file."""
        logger.info(
            "building harness service spec_path=%s regression_suite_path=%s skill_roots=%s llm_configured=%s message_planner_configured=%s",
            spec_path,
            regression_suite_path,
            skill_roots or [],
            llm_client is not None,
            message_planner is not None,
        )
        harness = load_prompt_harness(spec_path, skill_roots=skill_roots)
        if harness is None:
            raise ServiceConfigurationError(f"harness spec is disabled: {spec_path}")
        regression_suite = (
            load_regression_suite(regression_suite_path)
            if regression_suite_path is not None
            else None
        )
        if regression_suite is not None:
            logger.info(
                "loaded regression suite version=%s source_document=%s case_count=%d",
                regression_suite.version,
                regression_suite.source_document,
                len(regression_suite.cases),
        )
        service = cls(
            harness,
            regression_suite=regression_suite,
            llm_client=llm_client,
            message_planner=message_planner,
            workflow_client=workflow_client,
        )
        logger.info(
            "initialized harness service name=%s version=%s llm_configured=%s assistant_configured=%s regression_suite_loaded=%s",
            service.harness.spec.name,
            service.harness.spec.version,
            service.llm_client is not None,
            service.assistant is not None,
            service.regression_suite is not None,
        )
        return service

    def health(self) -> HarnessHealth:
        """Return deterministic metadata for liveness and startup checks."""
        return HarnessHealth(
            name=self.harness.spec.name,
            version=self.harness.spec.version,
            enabled=self.harness.spec.enabled,
        )

    def regression_summary(self) -> RegressionSuiteSummary:
        """Return a summary of the loaded regression suite."""
        suite = self._require_regression_suite()
        return RegressionSuiteSummary(
            version=suite.version,
            source_document=suite.source_document,
            primary_mode=suite.primary_mode,
            event_filter=list(suite.event_filter),
            cases=[
                RegressionCaseSummary(
                    id=case.id,
                    title=case.title,
                    status=case.status,
                    step_count=len(case.steps),
                    tags=list(case.tags),
                )
                for case in suite.cases
            ],
        )

    def regression_case(self, case_id: str) -> RegressionCase:
        """Return one loaded regression case."""
        return self._require_regression_suite().case_by_id(case_id)

    def validate_regression(
        self,
        request: RegressionValidationRequest,
    ) -> RegressionValidationResponse:
        """Validate a step or full case SSE transcript against the loaded suite."""
        try:
            case = self.regression_case(request.case_id)
            if request.step_name is not None:
                step = _step_by_name(case, request.step_name)
                if request.sse_text is None:
                    raise ProtocolAssertionError("sse_text is required for step validation")
                validate_step_transcript(step, parse_sse_text(request.sse_text))
                return RegressionValidationResponse(
                    ok=True,
                    case_id=request.case_id,
                    step_name=request.step_name,
                )

            if not request.transcripts:
                raise ProtocolAssertionError("transcripts is required for case validation")
            validate_case_transcripts(
                case,
                {
                    name: parse_sse_text(sse_text)
                    for name, sse_text in request.transcripts.items()
                },
            )
            return RegressionValidationResponse(ok=True, case_id=request.case_id)
        except (KeyError, ProtocolAssertionError) as exc:
            return RegressionValidationResponse(
                ok=False,
                case_id=request.case_id,
                step_name=request.step_name,
                errors=[str(exc)],
            )

    def handle_message(self, request: RouterMessageRequest) -> AssistantServiceResult:
        """Handle an assistant protocol message request."""
        if self.assistant is None:
            raise ServiceConfigurationError("assistant planner is not configured")
        return self.assistant.handle_message(request)

    def handle_task_completion(self, request: TaskCompletionRequest) -> AssistantServiceResult:
        """Handle an assistant protocol task completion request."""
        if self.assistant is None:
            raise ServiceConfigurationError("assistant planner is not configured")
        return self.assistant.handle_task_completion(request)

    def _require_regression_suite(self) -> RegressionSuite:
        if self.regression_suite is None:
            raise ServiceConfigurationError("regression suite is not loaded")
        return self.regression_suite


def _step_by_name(case: RegressionCase, step_name: str):
    for step in case.steps:
        if step.name == step_name:
            return step
    raise KeyError(step_name)


def _chat_message_content(response: dict[str, Any]) -> tuple[str, str | None]:
    try:
        choice = response["choices"][0]
        message = choice["message"]
        content = str(message["content"]).strip()
        finish_reason = choice.get("finish_reason")
        return content, str(finish_reason) if finish_reason is not None else None
    except (KeyError, IndexError, TypeError) as exc:
        raise LLMRequestError("chat completion response did not contain choices[0].message.content") from exc
