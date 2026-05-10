from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
from typing import Any, Protocol

from intent_router_harness.contracts import PlannedTask, RouterMessageRequest

class WorkflowToolError(RuntimeError):
    """Raised when a workflow tool call fails or returns an invalid stream."""


@dataclass(frozen=True, slots=True)
class WorkflowToolSpec:
    """Machine-readable contract for one workflow tool."""

    intent_code: str
    allowed_urls: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class WorkflowToolEvent:
    """One workflow SSE message reduced to node metadata."""

    node_id: str | None
    node_title: str | None
    timestamp: str | None
    node_output: Any
    data: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class WorkflowToolResult:
    """Workflow call result used to emit assistant protocol frames."""

    events: tuple[WorkflowToolEvent, ...]

    @property
    def final_output(self) -> Any:
        if not self.events:
            return {}
        return self.events[-1].node_output


@dataclass(frozen=True, slots=True)
class WorkflowSettings:
    """HTTP settings for workflow tool calls."""

    timeout_seconds: float = 60.0


class WorkflowToolClient(Protocol):
    """Boundary for invoking workflow tools."""

    def run_workflow(
        self,
        spec: WorkflowToolSpec,
        *,
        request_payload: WorkflowHTTPRequest,
    ) -> WorkflowToolResult:
        """Invoke a workflow tool and return parsed node outputs."""


def load_workflow_settings(env_file: str | Path = ".env.local") -> WorkflowSettings:
    """Load optional workflow HTTP settings from env and a dotenv file."""
    file_values = _load_env_file(env_file)

    def get(name: str) -> str | None:
        return os.getenv(name) or file_values.get(name)

    return WorkflowSettings(
        timeout_seconds=float(get("ROUTER_WORKFLOW_TIMEOUT_SECONDS") or "60"),
    )


class HTTPWorkflowToolClient:
    """Small synchronous SSE client for workflow use_as_tool endpoints."""

    def __init__(self, settings: WorkflowSettings) -> None:
        self.settings = settings

    def run_workflow(
        self,
        spec: WorkflowToolSpec,
        *,
        request_payload: WorkflowHTTPRequest,
    ) -> WorkflowToolResult:
        if request_payload.method != "POST":
            raise WorkflowToolError(f"unsupported workflow method: {request_payload.method}")
        raise WorkflowToolError("HTTPWorkflowToolClient requires harness_v2 runtime")


def is_workflow_tool_reference_body(body: str) -> bool:
    """Return whether a reference body is Router-only workflow config."""
    del body
    return False


@dataclass(frozen=True, slots=True)
class WorkflowHTTPRequest:
    """A model-produced workflow HTTP request after router validation."""

    method: str
    url: str
    body: dict[str, Any]


def build_workflow_request_payload(
    spec: WorkflowToolSpec,
    *,
    request: RouterMessageRequest,
    task: PlannedTask,
) -> WorkflowHTTPRequest:
    """Validate the model-produced workflow request against tool safety config."""
    del request
    raw = task.workflow_request
    if not isinstance(raw, dict) or not raw:
        raise WorkflowToolError("workflow_request is required for execute mode")
    method = str(raw.get("method") or "").upper()
    url = str(raw.get("url") or "").strip()
    body = raw.get("body")
    if not isinstance(body, dict):
        raise WorkflowToolError("workflow_request.body must be a JSON object")
    if method != "POST":
        raise WorkflowToolError(f"unsupported workflow method: {method}")
    return WorkflowHTTPRequest(method=method, url=url, body=body)


def parse_workflow_sse(text: str, *, require_node_output: bool = True) -> WorkflowToolResult:
    """Parse workflow SSE text and extract every message node_output as a whole."""
    events: list[WorkflowToolEvent] = []
    saw_done = False
    for event_name, raw_data in _iter_sse_events(text):
        if event_name == "done" or raw_data == "[DONE]":
            saw_done = True
            continue
        if event_name != "message":
            continue
        try:
            payload = json.loads(raw_data)
        except json.JSONDecodeError as exc:
            raise WorkflowToolError(f"workflow SSE data is not JSON: {exc}") from exc
        additional = payload.get("additional_kwargs")
        if require_node_output and (not isinstance(additional, dict) or "node_output" not in additional):
            raise WorkflowToolError("workflow SSE message is missing additional_kwargs.node_output")
        if not isinstance(additional, dict):
            additional = {}
        events.append(
            WorkflowToolEvent(
                node_id=_optional_string(additional.get("node_id")),
                node_title=_optional_string(additional.get("node_title")),
                timestamp=_optional_string(additional.get("timestamp")),
                node_output=additional.get("node_output"),
                data=payload,
            )
        )
    if not saw_done:
        raise WorkflowToolError("workflow SSE stream ended without done event")
    return WorkflowToolResult(events=tuple(events))


def workflow_event_output(event: WorkflowToolEvent) -> Any:
    """Return the assistant frame output for one workflow event."""
    return event.node_output


def render_workflow_response_mapping(
    spec: WorkflowToolSpec,
    *,
    phase: str,
    request: RouterMessageRequest,
    task: PlannedTask,
    event: WorkflowToolEvent | None = None,
    last_output: Any = None,
    error_summary: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return the default response mapping for one workflow phase."""
    del spec, request, task
    return _default_response_mapping(phase, event=event, last_output=last_output, error_summary=error_summary)


def _skill_intent_code(*, skill_name: str, intent_codes: tuple[str, ...]) -> str:
    if len(intent_codes) != 1:
        raise WorkflowToolError(
            f"skill {skill_name!r} must declare exactly one intent_code to own workflow references"
        )
    return intent_codes[0]


def _default_response_mapping(
    phase: str,
    *,
    event: WorkflowToolEvent | None,
    last_output: Any,
    error_summary: dict[str, Any] | None,
) -> dict[str, Any]:
    if phase == "message":
        return {
            "status": "waiting_assistant_completion",
            "completion_reason": "workflow_node_output",
            "output": workflow_event_output(event) if event is not None else {},
        }
    if phase == "done":
        return {
            "status": "completed",
            "completion_reason": "workflow_done",
            "output": last_output if last_output is not None else {},
        }
    if phase == "error":
        return {
            "status": "failed",
            "completion_reason": "workflow_error",
            "output": {"error": error_summary or {}},
        }
    raise WorkflowToolError(f"unknown workflow response phase: {phase}")


def _iter_sse_events(text: str):
    normalized = text.replace("\r\n", "\n")
    for raw_frame in normalized.split("\n\n"):
        if not raw_frame.strip():
            continue
        event_name = "message"
        data_lines: list[str] = []
        for line in raw_frame.split("\n"):
            if not line or line.startswith(":"):
                continue
            if line.startswith("event:"):
                event_name = line.removeprefix("event:").strip()
            elif line.startswith("data:"):
                data_lines.append(line.removeprefix("data:").lstrip())
        yield event_name, "\n".join(data_lines)


def _optional_string(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


def _string_list(value: Any) -> list[str]:
    if value is None or value == "":
        return []
    if isinstance(value, list | tuple):
        return [str(item).strip() for item in value if str(item).strip()]
    return [str(value).strip()]


def _load_env_file(path: str | Path) -> dict[str, str]:
    """Parse a simple KEY=VALUE env file, ignoring comments and blanks."""
    env_path = Path(path).expanduser()
    if not env_path.is_file():
        return {}
    result: dict[str, str] = {}
    for line in env_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        result[key.strip()] = value.strip().strip("'\"")
    return result

