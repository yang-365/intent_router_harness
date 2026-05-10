"""Workflow tool — registered as a deepagent StructuredTool."""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx

from intent_router_harness.harness_v2.errors import WorkflowExecutionError

logger = logging.getLogger(__name__)


def create_workflow_tool(
    *,
    timeout_seconds: float = 60.0,
) -> Any:
    """Create the ``workflow_api_call`` LangChain StructuredTool.

    URL whitelist enforcement is done by ``WorkflowGatewayMiddleware``,
    not here — keeping the tool itself generic.
    """
    try:
        from langchain_core.tools import StructuredTool
        from pydantic import BaseModel, Field
    except ImportError as exc:
        raise RuntimeError(
            "langchain-core is required; install the 'deepagent' extra"
        ) from exc

    class WorkflowApiCallInput(BaseModel):
        """Input schema for workflow_api_call tool."""

        method: str = Field(default="POST", description="HTTP method")
        url: str = Field(description="Full absolute http(s) URL of the workflow SSE endpoint")
        body: dict[str, Any] = Field(default_factory=dict, description="JSON body to POST")

    def workflow_api_call(method: str = "POST", url: str = "", body: dict[str, Any] | None = None) -> str:
        """Call a workflow API endpoint and parse SSE node_output events."""
        body = body or {}
        logger.info("workflow_api_call method=%s url=%s", method, url)
        try:
            result = _execute_workflow_sse(method, url, body, timeout=timeout_seconds)
            return json.dumps(result, ensure_ascii=False)
        except WorkflowExecutionError:
            raise
        except Exception as exc:
            raise WorkflowExecutionError(f"workflow call failed: {exc}") from exc

    return StructuredTool.from_function(
        func=workflow_api_call,
        name="workflow_api_call",
        description=(
            "Call one allowed workflow use_as_tool SSE endpoint. Use this only when "
            "executionMode is execute and all required slots are available. The url "
            "must be the full absolute http(s) URL from the skill reference. Do not "
            "invent or modify URLs."
        ),
        args_schema=WorkflowApiCallInput,
    )


def _execute_workflow_sse(
    method: str,
    url: str,
    body: dict[str, Any],
    *,
    timeout: float = 60.0,
) -> dict[str, Any]:
    """Execute an HTTP request to a workflow SSE endpoint and parse the result."""
    events: list[dict[str, Any]] = []
    try:
        with httpx.Client(timeout=timeout) as client:
            with client.stream(method.upper(), url, json=body) as response:
                response.raise_for_status()
                buffer = ""
                for chunk in response.iter_text():
                    buffer += chunk
                    while "\n\n" in buffer:
                        event_text, buffer = buffer.split("\n\n", 1)
                        parsed = _parse_sse_event(event_text)
                        if parsed is not None:
                            events.append(parsed)
    except httpx.TimeoutException as exc:
        raise WorkflowExecutionError(f"workflow request timed out after {timeout}s") from exc
    except httpx.HTTPStatusError as exc:
        raise WorkflowExecutionError(
            f"workflow HTTP {exc.response.status_code}: {exc.response.text[:200]}"
        ) from exc
    except httpx.HTTPError as exc:
        raise WorkflowExecutionError(f"workflow HTTP error: {exc}") from exc

    if not events:
        raise WorkflowExecutionError("workflow returned no SSE events")

    last = events[-1]
    node_output = last.get("node_output")
    if node_output is None:
        raise WorkflowExecutionError("workflow final event has no node_output")

    return {
        "node_output": node_output,
        "event_count": len(events),
        "events": events,
    }


def _parse_sse_event(text: str) -> dict[str, Any] | None:
    """Parse a single SSE event block into a dict."""
    data_lines: list[str] = []
    for line in text.strip().splitlines():
        if line.startswith("data: "):
            data_lines.append(line[6:])
        elif line.startswith("data:"):
            data_lines.append(line[5:])
    if not data_lines:
        return None
    raw = "\n".join(data_lines)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"raw": raw}
