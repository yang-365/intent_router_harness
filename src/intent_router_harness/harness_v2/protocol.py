"""Assistant Protocol models — same wire format as v1 for full compatibility."""

from __future__ import annotations

import contextvars
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

AssistantStatus = Literal[
    "running",
    "waiting_user_input",
    "ready_for_dispatch",
    "waiting_assistant_completion",
    "completed",
    "cancelled",
    "failed",
]


class MessageRequest(BaseModel):
    """Inbound request — identical to v1 ``RouterMessageRequest``."""

    model_config = ConfigDict(extra="allow")

    sessionId: str
    txt: str
    custID: str
    stream: bool = False
    debugTrace: bool = False
    executionMode: str = "execute"
    config_variables: list[dict[str, Any]] = Field(default_factory=list)
    recommendTask: list[dict[str, Any]] = Field(default_factory=list)
    currentDisplay: list[dict[str, Any]] = Field(default_factory=list)

    @field_validator("sessionId", "txt", "custID")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be blank")
        return value


class TaskCompletionRequest(BaseModel):
    """Task completion callback — identical to v1."""

    model_config = ConfigDict(extra="allow")

    sessionId: str
    custID: str
    taskId: str
    completionSignal: Literal[1, 2]
    stream: bool = False
    debugTrace: bool = False

    @field_validator("sessionId", "custID", "taskId")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be blank")
        return value


class AssistantProtocolFrame(BaseModel):
    """One assistant protocol message frame — same wire format as v1."""

    ok: bool = True
    status: AssistantStatus
    intent_code: str | None = None
    completion_state: int = 0
    completion_reason: str = ""
    stage: str | None = None
    details: dict[str, Any] | None = None
    output: Any = Field(default_factory=dict)
    slot_memory: dict[str, Any] = Field(default_factory=dict)
    message: str | None = None
    task_list: list[dict[str, Any]] = Field(default_factory=list)
    current_task: dict[str, Any] | None = None
    errorCode: str | None = None
    graph: dict[str, Any] | None = None
    actions: list[dict[str, Any]] = Field(default_factory=list)

    def protocol_dump(self) -> dict[str, Any]:
        """Return protocol JSON without unset optional null fields."""
        return self.model_dump(mode="json", exclude_none=True)


class TraceEvent(BaseModel):
    """Structured trace event for debug SSE stream."""

    stage: str
    title: str = ""
    summary: str = ""
    data: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Per-request trace collector (used by middleware to emit fine-grained events)
# ---------------------------------------------------------------------------

_trace_var: contextvars.ContextVar[list[TraceEvent]] = contextvars.ContextVar("_trace_var")


def trace_collector_init() -> list[TraceEvent]:
    """Initialise a new per-request trace buffer and bind it to the context."""
    buf: list[TraceEvent] = []
    _trace_var.set(buf)
    return buf


def emit_trace(stage: str, title: str, summary: str = "", **data: Any) -> None:
    """Append a trace event to the current request's collector (no-op if none)."""
    buf = _trace_var.get(None)
    if buf is None:
        return
    buf.append(TraceEvent(stage=stage, title=title, summary=summary, data=data))
