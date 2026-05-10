"""Error hierarchy for the DeepAgent harness runtime."""

from __future__ import annotations


class DeepAgentRuntimeError(RuntimeError):
    """Raised when the DeepAgent runtime cannot run or returns invalid output."""

    def __init__(self, message: str, *, code: str = "deepagent_runtime_error") -> None:
        super().__init__(message)
        self.code = code


class WorkflowApiCallError(DeepAgentRuntimeError):
    """Raised when the DeepAgent workflow tool cannot complete."""

    def __init__(self, message: str, *, code: str = "workflow_error") -> None:
        super().__init__(message, code=code)


class WorkflowUrlNotAllowedError(WorkflowApiCallError):
    """URL failed the workflow allowed_urls whitelist check."""

    def __init__(self, url: str) -> None:
        super().__init__(f"workflow URL not in allowed_urls whitelist: {url}", code="workflow_url_not_allowed")


class WorkflowTimeoutError(WorkflowApiCallError):
    """Workflow call exceeded configured timeout."""

    def __init__(self, url: str, timeout: float) -> None:
        super().__init__(f"workflow call timed out after {timeout}s: {url}", code="workflow_timeout")


class WorkflowHttpError(WorkflowApiCallError):
    """Upstream workflow returned non-success HTTP status."""

    def __init__(self, url: str, status_code: int) -> None:
        super().__init__(f"workflow HTTP {status_code}: {url}", code="workflow_http_error")


class WorkflowSseParseError(WorkflowApiCallError):
    """Workflow SSE response could not be parsed."""

    def __init__(self, url: str, detail: str = "") -> None:
        suffix = f": {detail}" if detail else ""
        super().__init__(f"workflow SSE parse error{suffix}: {url}", code="workflow_sse_parse_error")


class WorkflowNoNodeOutputError(WorkflowApiCallError):
    """SSE events lack required node_output."""

    def __init__(self, url: str) -> None:
        super().__init__(f"workflow SSE events missing node_output: {url}", code="workflow_no_node_output")


class SessionRunInProgressError(RuntimeError):
    """Raised when one session already has an active agent run."""
