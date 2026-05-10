"""Error hierarchy for the enterprise harness layer."""

from __future__ import annotations


class HarnessError(RuntimeError):
    """Base error for all harness-layer failures."""

    def __init__(self, message: str, *, code: str = "harness_error") -> None:
        super().__init__(message)
        self.code = code


class SessionBusyError(HarnessError):
    """Raised when a session already has an active agent run."""

    def __init__(self, message: str) -> None:
        super().__init__(message, code="session_run_in_progress")


class SessionExpiredError(HarnessError):
    """Raised when a session has exceeded its idle timeout."""

    def __init__(self, session_id: str) -> None:
        super().__init__(f"session {session_id} has expired", code="session_expired")


class WorkflowUrlNotAllowedError(HarnessError):
    """Raised when a workflow URL is not in the whitelist."""

    def __init__(self, url: str, allowed: list[str]) -> None:
        super().__init__(
            f"URL {url!r} is not in the allowed list: {allowed}",
            code="workflow_url_not_allowed",
        )
        self.url = url
        self.allowed = allowed


class WorkflowExecutionError(HarnessError):
    """Raised when a workflow tool call fails at runtime."""

    def __init__(self, message: str) -> None:
        super().__init__(message, code="workflow_execution_error")


class ProtocolOutputError(HarnessError):
    """Raised when agent output does not conform to Assistant Protocol."""

    def __init__(self, message: str) -> None:
        super().__init__(message, code="protocol_output_error")
