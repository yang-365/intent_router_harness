"""Tests for the LLMWorkflowExecutor module."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from intent_router_harness.contracts import (
    PlannedTask,
    RouterMessageRequest,
    TaskRuntimeState,
)
from intent_router_harness.executor import (
    ExecutorError,
    ExecutorResult,
    LLMWorkflowExecutor,
    _build_tool_definition,
    _parse_tool_call_response,
    _workflow_reference_body,
)
from intent_router_harness.skills import SkillDocument, SkillLibrary, SkillReference
from intent_router_harness.workflow import (
    WorkflowHTTPRequest,
    WorkflowToolError,
    WorkflowToolEvent,
    WorkflowToolResult,
    WorkflowToolSpec,
)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeToolCallLLM:
    """Returns a tool_call response with the given arguments."""

    def __init__(self, arguments: dict[str, Any]) -> None:
        self._arguments = arguments
        self.calls: list[dict] = []

    def chat(
        self,
        messages: list[dict[str, str]],
        *,
        max_tokens: int | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: dict[str, Any] | str | None = None,
    ) -> dict[str, Any]:
        del max_tokens
        self.calls.append({"messages": messages, "tools": tools, "tool_choice": tool_choice})
        return {
            "model": "fake",
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call_test",
                                "type": "function",
                                "function": {
                                    "name": "workflow_api_call",
                                    "arguments": json.dumps(self._arguments, ensure_ascii=False),
                                },
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
        }


class FakeWorkflowClient:
    def __init__(
        self,
        events: list[WorkflowToolEvent] | None = None,
        error: Exception | None = None,
    ) -> None:
        self.events = events or []
        self.error = error
        self.calls: list[tuple[WorkflowToolSpec, WorkflowHTTPRequest]] = []

    def run_workflow(
        self,
        spec: WorkflowToolSpec,
        *,
        request_payload: WorkflowHTTPRequest,
    ) -> WorkflowToolResult:
        self.calls.append((spec, request_payload))
        if self.error is not None:
            raise self.error
        return WorkflowToolResult(events=tuple(self.events))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _skill_with_wf_ref() -> SkillDocument:
    return SkillDocument(
        name="transfer-routing",
        description="转账路由",
        path=Path("fake/SKILL.md"),
        body="test",
        intent_codes=("AG_TRANS",),
        required_slots=("payee_name", "amount"),
        references=(
            SkillReference(
                id="workflow_request",
                path=Path("fake/workflow_request.md"),
                body="method: POST\nurl: http://localhost:9876/api\n",
            ),
        ),
    )


def _skill_library() -> SkillLibrary:
    skill = _skill_with_wf_ref()
    return SkillLibrary({skill.name: skill})


def _spec() -> WorkflowToolSpec:
    return WorkflowToolSpec(intent_code="AG_TRANS", allowed_urls=("http://localhost:9876/api",))


def _request(**overrides: Any) -> RouterMessageRequest:
    defaults: dict[str, Any] = {
        "sessionId": "s1",
        "custID": "C1",
        "txt": "hi",
        "executionMode": "execute",
    }
    defaults.update(overrides)
    return RouterMessageRequest(**defaults)


def _task_state(current_task: PlannedTask | None = None) -> TaskRuntimeState:
    return TaskRuntimeState(
        slot_memory=current_task.slot_memory if current_task else {},
        task_list=[current_task] if current_task else [],
        current_task=current_task,
    )


def _ready_task() -> PlannedTask:
    return PlannedTask(
        taskId="t1",
        intent_code="AG_TRANS",
        status="ready_for_dispatch",
        slot_memory={"payee_name": "张三", "amount": 100},
    )


def _tool_call_args() -> dict[str, Any]:
    return {
        "method": "POST",
        "url": "http://localhost:9876/api",
        "body": {"session_id": "s1", "txt": "hi"},
    }


# ---------------------------------------------------------------------------
# Tests — pure helpers
# ---------------------------------------------------------------------------


def test_workflow_reference_body_returns_body() -> None:
    skill = _skill_with_wf_ref()
    assert _workflow_reference_body(skill) is not None


def test_workflow_reference_body_returns_none_when_missing() -> None:
    skill = SkillDocument(
        name="no-ref", description="", path=Path("x"), body="", intent_codes=("X",),
    )
    assert _workflow_reference_body(skill) is None


def test_build_tool_definition_includes_reference_in_description() -> None:
    defn = _build_tool_definition("some reference text")
    assert "some reference text" in defn["function"]["description"]
    assert defn["function"]["name"] == "workflow_api_call"


def test_parse_tool_call_response_extracts_request() -> None:
    response = {
        "choices": [
            {
                "message": {
                    "tool_calls": [
                        {
                            "function": {
                                "name": "workflow_api_call",
                                "arguments": json.dumps(_tool_call_args()),
                            }
                        }
                    ]
                }
            }
        ]
    }
    result = _parse_tool_call_response(response)
    assert result == WorkflowHTTPRequest(
        method="POST", url="http://localhost:9876/api", body={"session_id": "s1", "txt": "hi"},
    )


def test_parse_tool_call_response_rejects_missing_tool_calls() -> None:
    with pytest.raises(ExecutorError, match="tool_call"):
        _parse_tool_call_response({"choices": [{"message": {"content": "no tool"}}]})


def test_parse_tool_call_response_rejects_non_post() -> None:
    bad = {"choices": [{"message": {"tool_calls": [
        {"function": {"name": "x", "arguments": json.dumps({"method": "GET", "url": "u", "body": {}})}}
    ]}}]}
    with pytest.raises(ExecutorError, match="unsupported"):
        _parse_tool_call_response(bad)


# ---------------------------------------------------------------------------
# Tests — executor integration
# ---------------------------------------------------------------------------


def test_executor_skips_when_not_ready_for_dispatch() -> None:
    task = PlannedTask(taskId="t1", intent_code="AG_TRANS", status="waiting_user_input")
    executor = LLMWorkflowExecutor(
        llm_client=FakeToolCallLLM(_tool_call_args()),
        skill_library=_skill_library(),
        workflow_client=FakeWorkflowClient(),
        workflow_tools={"AG_TRANS": _spec()},
    )
    result = executor.execute(_request(), _task_state(task))
    assert result.frames == ()


def test_executor_skips_when_execution_mode_is_router_only() -> None:
    executor = LLMWorkflowExecutor(
        llm_client=FakeToolCallLLM(_tool_call_args()),
        skill_library=_skill_library(),
        workflow_client=FakeWorkflowClient(),
        workflow_tools={"AG_TRANS": _spec()},
    )
    result = executor.execute(_request(executionMode="router_only"), _task_state(_ready_task()))
    assert result.frames == ()


def test_executor_runs_workflow_and_produces_frames() -> None:
    wf_client = FakeWorkflowClient([
        WorkflowToolEvent(node_id="s", node_title="开始", timestamp=None, node_output={"ok": True}),
    ])
    executor = LLMWorkflowExecutor(
        llm_client=FakeToolCallLLM(_tool_call_args()),
        skill_library=_skill_library(),
        workflow_client=wf_client,
        workflow_tools={"AG_TRANS": _spec()},
    )
    result = executor.execute(_request(), _task_state(_ready_task()))

    assert len(wf_client.calls) == 1
    assert len(result.frames) == 2
    assert result.frames[0].completion_reason == "workflow_node_output"
    assert result.frames[0].output == {"ok": True}
    assert result.frames[1].status == "completed"
    assert result.frames[1].completion_reason == "workflow_done"


def test_executor_returns_failed_frame_on_workflow_error() -> None:
    executor = LLMWorkflowExecutor(
        llm_client=FakeToolCallLLM(_tool_call_args()),
        skill_library=_skill_library(),
        workflow_client=FakeWorkflowClient(error=WorkflowToolError("boom")),
        workflow_tools={"AG_TRANS": _spec()},
    )
    result = executor.execute(_request(), _task_state(_ready_task()))

    assert len(result.frames) == 1
    assert result.frames[0].ok is False
    assert result.frames[0].status == "failed"


def test_executor_is_stateless_across_calls() -> None:
    """Verify no per-request state leaks between invocations."""
    wf_client = FakeWorkflowClient([
        WorkflowToolEvent(node_id="e", node_title="结束", timestamp=None, node_output={"n": 1}),
    ])
    executor = LLMWorkflowExecutor(
        llm_client=FakeToolCallLLM(_tool_call_args()),
        skill_library=_skill_library(),
        workflow_client=wf_client,
        workflow_tools={"AG_TRANS": _spec()},
    )

    r1 = executor.execute(_request(sessionId="a"), _task_state(_ready_task()))
    assert len(r1.frames) == 2

    wf_client.events = [
        WorkflowToolEvent(node_id="e", node_title="结束", timestamp=None, node_output={"n": 2}),
    ]
    r2 = executor.execute(_request(sessionId="b"), _task_state(_ready_task()))
    assert len(r2.frames) == 2
    assert r2.frames[0].output == {"n": 2}
