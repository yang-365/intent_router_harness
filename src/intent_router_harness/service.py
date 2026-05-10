"""Application service boundary — regression validation and health checks.

Classic runtime modules (assistant_service, planner, llm, skills) have been
removed in Phase 2.  The runtime is now exclusively harness_v2 + deepagent SDK.
This module retains regression testing, health endpoints, and the spec-loading
path so existing TOML specs keep working.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from intent_router_harness.assistant_protocol import (
    ProtocolAssertionError,
    parse_sse_text,
)
from intent_router_harness.regression import (
    RegressionCase,
    RegressionSuite,
    load_regression_suite,
    validate_case_transcripts,
    validate_step_transcript,
)
from intent_router_harness.runtime import PromptHarness, load_prompt_harness

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
    """Application service — health, regression validation, spec loading.

    The classic assistant runtime has been removed.  Use ``harness_v2``
    (via ``create_app()``) for the deepagent-backed runtime with
    progressive skill loading.
    """

    def __init__(
        self,
        harness: PromptHarness,
        *,
        regression_suite: RegressionSuite | None = None,
    ) -> None:
        self.harness = harness
        self.regression_suite = regression_suite

    @classmethod
    def from_spec(
        cls,
        spec_path: str | Path,
        *,
        skill_roots: list[str] | None = None,
        regression_suite_path: str | Path | None = None,
        **_kwargs: Any,
    ) -> IntentRouterHarnessService:
        """Load a service from a harness spec file.

        Args:
            spec_path: Path to TOML spec.
            skill_roots: Additional skill root directories.
            regression_suite_path: Path to regression suite JSON.
        """
        harness = load_prompt_harness(spec_path, skill_roots=skill_roots)
        if harness is None:
            raise ServiceConfigurationError(f"harness spec is disabled: {spec_path}")
        regression_suite = (
            load_regression_suite(regression_suite_path)
            if regression_suite_path is not None
            else None
        )
        service = cls(harness, regression_suite=regression_suite)
        logger.info(
            "initialized harness service name=%s version=%s regression=%s",
            service.harness.spec.name,
            service.harness.spec.version,
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

    def _require_regression_suite(self) -> RegressionSuite:
        if self.regression_suite is None:
            raise ServiceConfigurationError("regression suite is not loaded")
        return self.regression_suite


def _step_by_name(case: RegressionCase, step_name: str) -> Any:
    for step in case.steps:
        if step.name == step_name:
            return step
    raise KeyError(step_name)
