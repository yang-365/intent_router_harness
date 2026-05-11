from __future__ import annotations

from datetime import datetime, timedelta, timezone
from http.client import HTTPConnection
import json
from pathlib import Path
from threading import Thread

import pytest

from intent_router_harness.assistant_protocol import parse_sse_text
from intent_router_harness.contracts import (
    PlannedTask,
    PlannerOutput,
    RecognitionPlan,
    RouterMessageRequest,
    TaskCompletionRequest,
    TaskRuntimeState,
)
from intent_router_harness import load_prompt_harness
from intent_router_harness.planner import LLMMessagePlanner, PlannerError
from intent_router_harness.regression import load_regression_suite, validate_step_transcript
from intent_router_harness.server import create_server
from intent_router_harness.service import IntentRouterHarnessService
from intent_router_harness.session_store import InMemorySessionStore
from intent_router_harness.executor import ExecutorResult, LLMWorkflowExecutor
from intent_router_harness.workflow import (
    WorkflowHTTPRequest,
    WorkflowToolError,
    WorkflowToolEvent,
    WorkflowToolResult,
    WorkflowToolSpec,
)


SUITE_PATH = "regressions/assistant_protocol_v0_6.json"


class StaticPlanner:
    def __init__(self, output: PlannerOutput) -> None:
        self.output = output

    def plan_message(
        self,
        request: RouterMessageRequest,
        task_state: TaskRuntimeState,
    ) -> PlannerOutput:
        return self.output


class SequencePlanner:
    def __init__(self, outputs: list[PlannerOutput]) -> None:
        self.outputs = list(outputs)

    def plan_message(
        self,
        request: RouterMessageRequest,
        task_state: TaskRuntimeState,
    ) -> PlannerOutput:
        del request, task_state
        return self.outputs.pop(0)


class FakeLLMClient:
    def __init__(self, responses: list[str]) -> None:
        self.responses = list(responses)
        self.messages: list[list[dict[str, str]]] = []

    def chat(self, messages, max_tokens=None, tools=None, tool_choice=None):
        del max_tokens, tools, tool_choice
        self.messages.append(messages)
        content = self.responses.pop(0)
        return {
            "model": "fake-llm",
            "choices": [
                {
                    "message": {"content": content},
                    "finish_reason": "stop",
                }
            ],
        }


class FakeToolCallLLMClient:
    """LLM client that returns a function call response for executor tests."""

    def __init__(self, tool_call_arguments: dict) -> None:
        self._arguments = tool_call_arguments
        self.calls: list[dict] = []

    def chat(self, messages, max_tokens=None, tools=None, tool_choice=None):
        del max_tokens
        self.calls.append({"messages": messages, "tools": tools, "tool_choice": tool_choice})
        return {
            "model": "fake-llm",
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call_001",
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


def _fake_skill_library() -> "SkillLibrary":
    """Build a minimal SkillLibrary with a transfer skill and workflow_request reference."""
    from intent_router_harness.skills import SkillDocument, SkillLibrary, SkillReference

    wf_ref = SkillReference(
        id="workflow_request",
        path=Path("fake/workflow_request.md"),
        body=(
            "# 转账子工作流请求组装\n"
            "method: POST\n"
            "url: http://127.0.0.1:9876/agent-api/workflow-agent-1-1b14f16b/chatabc/use_as_tool\n"
        ),
    )
    skill = SkillDocument(
        name="transfer-routing",
        description="转账路由",
        path=Path("fake/SKILL.md"),
        body="transfer skill body",
        intent_codes=("AG_TRANS",),
        required_slots=("payee_name", "amount"),
        references=(wf_ref,),
    )
    return SkillLibrary({"transfer-routing": skill})


def _build_test_executor(
    *,
    tool_call_arguments: dict,
    workflow_client: "FakeWorkflowClient",
) -> LLMWorkflowExecutor:
    """Build an executor with fake LLM (returning tool call) and fake workflow client."""
    return LLMWorkflowExecutor(
        llm_client=FakeToolCallLLMClient(tool_call_arguments),
        skill_library=_fake_skill_library(),
        workflow_client=workflow_client,
        workflow_tools={"AG_TRANS": _transfer_workflow_spec()},
    )


def _transfer_workflow_spec() -> WorkflowToolSpec:
    return WorkflowToolSpec(
        intent_code="AG_TRANS",
        allowed_urls=("http://127.0.0.1:9876/agent-api/workflow-agent-1-1b14f16b/chatabc/use_as_tool",),
    )


def _transfer_workflow_request(
    *,
    session_id: str = "1635501196813426",
    cust_id: str = "1631102265490929",
    text: str = "给陈广荣转500元",
    current_display: str = "",
    slots_json: str = '{"payee_name": "陈广荣", "amount": "500"}',
) -> dict:
    return {
        "method": "POST",
        "url": "http://127.0.0.1:9876/agent-api/workflow-agent-1-1b14f16b/chatabc/use_as_tool",
        "body": {
            "session_id": session_id,
            "txt": text,
            "stream": True,
            "config_variables": [
                {"name": "custID", "value": cust_id},
                {"name": "sessionID", "value": session_id},
                {"name": "currentDisplay", "value": current_display},
                {"name": "agentSessionID", "value": session_id},
                {"name": "slots_data", "value": slots_json},
            ],
        },
    }


def _write_minimal_harness(tmp_path: Path) -> Path:
    spec_path = tmp_path / "harness.toml"
    spec_path.write_text(
        "\n".join(
            [
                'name = "assistant-protocol-test"',
                'version = "2026.04"',
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return spec_path


def test_v1_message_stream_emits_recognition_before_business(tmp_path: Path) -> None:
    task = PlannedTask(
        taskId="task_transfer",
        intent_code="AG_TRANS",
        status="waiting_user_input",
        title="给小明转账",
        slot_memory={"payee_name": "小明"},
    )
    planner = StaticPlanner(
        PlannerOutput(
            mode="slot_filling",
            status="waiting_user_input",
            completion_state=0,
            completion_reason="router_waiting_user_input",
            intent_code="AG_TRANS",
            recognition=RecognitionPlan(intent_code="AG_TRANS"),
            slot_memory={"payee_name": "小明"},
            task_list=[task],
            current_task=task,
            message="请提供金额",
        )
    )
    service = IntentRouterHarnessService.from_spec(
        _write_minimal_harness(tmp_path),
        message_planner=planner,
    )
    server = create_server(service, host="127.0.0.1", port=0)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    conn: HTTPConnection | None = None
    try:
        host, port = server.server_address
        conn = HTTPConnection(host, port, timeout=5)
        conn.request(
            "POST",
            "/api/v1/message",
            body=json.dumps(
                {
                    "sessionId": "assistant_tc_s01",
                    "txt": "给小明转账",
                    "custID": "C0001",
                    "stream": True,
                }
            ),
            headers={"Content-Type": "application/json"},
        )
        response = conn.getresponse()
        body = response.read().decode("utf-8")
    finally:
        if conn is not None:
            conn.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    events = parse_sse_text(body)
    step = load_regression_suite(SUITE_PATH).case_by_id("TC-S01").steps[0]

    assert response.status == 200
    assert [event.event for event in events] == ["message", "message", "done"]
    validate_step_transcript(step, events)


def test_v1_message_stream_can_emit_debug_trace_events(tmp_path: Path) -> None:
    task = PlannedTask(
        taskId="task_transfer",
        intent_code="AG_TRANS",
        status="waiting_user_input",
        title="给小明转账",
    )
    planner = StaticPlanner(
        PlannerOutput(
            mode="slot_filling",
            status="waiting_user_input",
            completion_state=0,
            completion_reason="router_waiting_user_input",
            intent_code="AG_TRANS",
            recognition=RecognitionPlan(intent_code="AG_TRANS"),
            task_list=[task],
            current_task=task,
            message="请提供金额",
        )
    )
    service = IntentRouterHarnessService.from_spec(
        _write_minimal_harness(tmp_path),
        message_planner=planner,
    )
    server = create_server(service, host="127.0.0.1", port=0)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    conn: HTTPConnection | None = None
    try:
        host, port = server.server_address
        conn = HTTPConnection(host, port, timeout=5)
        conn.request(
            "POST",
            "/api/v1/message",
            body=json.dumps(
                {
                    "sessionId": "assistant_trace",
                    "txt": "给小明转账",
                    "custID": "C0001",
                    "stream": True,
                    "debugTrace": True,
                }
            ),
            headers={"Content-Type": "application/json"},
        )
        response = conn.getresponse()
        body = response.read().decode("utf-8")
    finally:
        if conn is not None:
            conn.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    events = parse_sse_text(body)
    trace_events = [event for event in events if event.event == "trace"]
    message_events = [event for event in events if event.event == "message"]

    assert response.status == 200
    assert len(trace_events) >= 4
    assert trace_events[0].data["stage"] == "request_received"
    assert any(event.data["stage"] == "intent_recognition" for event in trace_events)
    assert len(message_events) == 2
    assert events[-1].is_done


def test_v1_message_non_stream_returns_final_business_frame(tmp_path: Path) -> None:
    task = PlannedTask(
        taskId="task_transfer",
        intent_code="AG_TRANS",
        status="ready_for_dispatch",
        title="给小明转账200",
        slot_memory={"payee_name": "小明", "amount": "200"},
        output={"ishandover": True, "handOverReason": "router_only_ready_for_dispatch"},
    )
    planner = StaticPlanner(
        PlannerOutput(
            mode="single_task",
            status="ready_for_dispatch",
            completion_state=0,
            completion_reason="router_ready_for_dispatch",
            intent_code="AG_TRANS",
            recognition=RecognitionPlan(intent_code="AG_TRANS"),
            slot_memory={"payee_name": "小明", "amount": "200"},
            task_list=[task],
            current_task=task,
            output={"ishandover": True, "handOverReason": "router_only_ready_for_dispatch"},
        )
    )
    service = IntentRouterHarnessService.from_spec(
        _write_minimal_harness(tmp_path),
        message_planner=planner,
    )
    server = create_server(service, host="127.0.0.1", port=0)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    conn: HTTPConnection | None = None
    try:
        host, port = server.server_address
        conn = HTTPConnection(host, port, timeout=5)
        conn.request(
            "POST",
            "/api/v1/message",
            body=json.dumps(
                {
                    "sessionId": "assistant_non_stream",
                    "txt": "给小明转账200",
                    "custID": "C0001",
                    "stream": False,
                    "executionMode": "router_only",
                }
            ),
            headers={"Content-Type": "application/json"},
        )
        response = conn.getresponse()
        payload = json.loads(response.read().decode("utf-8"))
    finally:
        if conn is not None:
            conn.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert response.status == 200
    assert payload["status"] == "ready_for_dispatch"
    assert payload["completion_reason"] == "router_ready_for_dispatch"
    assert isinstance(payload["output"], dict)
    assert "snapshot" not in payload


def test_task_completion_stream_confirms_current_task(tmp_path: Path) -> None:
    task = PlannedTask(
        taskId="task_transfer",
        intent_code="AG_TRANS",
        status="waiting_assistant_completion",
        title="给小明转账200",
        slot_memory={"payee_name": "小明", "amount": "200"},
        output={"message": "已向小明转账 200 CNY，转账成功"},
    )
    planner = StaticPlanner(
        PlannerOutput(
            mode="single_task",
            status="waiting_assistant_completion",
            completion_state=1,
            completion_reason="assistant_confirmation_required",
            intent_code="AG_TRANS",
            recognition=RecognitionPlan(intent_code="AG_TRANS"),
            slot_memory={"payee_name": "小明", "amount": "200"},
            task_list=[task],
            current_task=task,
            output={"message": "已向小明转账 200 CNY，转账成功"},
        )
    )
    service = IntentRouterHarnessService.from_spec(
        _write_minimal_harness(tmp_path),
        message_planner=planner,
    )
    server = create_server(service, host="127.0.0.1", port=0)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    conn: HTTPConnection | None = None
    try:
        host, port = server.server_address
        conn = HTTPConnection(host, port, timeout=5)
        conn.request(
            "POST",
            "/api/v1/message",
            body=json.dumps(
                {
                    "sessionId": "assistant_tc_completion",
                    "txt": "给小明转账200",
                    "custID": "C0001",
                    "stream": False,
                }
            ),
            headers={"Content-Type": "application/json"},
        )
        conn.getresponse().read()
        conn.request(
            "POST",
            "/api/v1/task/completion",
            body=json.dumps(
                {
                    "sessionId": "assistant_tc_completion",
                    "custID": "C0001",
                    "taskId": "task_transfer",
                    "completionSignal": 2,
                    "stream": True,
                }
            ),
            headers={"Content-Type": "application/json"},
        )
        response = conn.getresponse()
        body = response.read().decode("utf-8")
    finally:
        if conn is not None:
            conn.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    events = parse_sse_text(body)

    assert response.status == 200
    assert [event.event for event in events] == ["message", "done"]
    assert events[0].data["status"] == "completed"
    assert events[0].data["completion_reason"] == "assistant_final_done"
    assert events[-1].is_done


def test_final_task_completion_clears_active_runtime_memory(tmp_path: Path) -> None:
    task = PlannedTask(
        taskId="task_transfer",
        intent_code="AG_TRANS",
        status="waiting_assistant_completion",
        title="给小明转账200",
        slot_memory={"payee_name": "小明", "amount": "200"},
        output={"message": "done"},
    )
    service = IntentRouterHarnessService.from_spec(
        _write_minimal_harness(tmp_path),
        message_planner=StaticPlanner(
            PlannerOutput(
                mode="single_task",
                status="waiting_assistant_completion",
                completion_state=1,
                completion_reason="assistant_confirmation_required",
                intent_code="AG_TRANS",
                recognition=RecognitionPlan(intent_code="AG_TRANS"),
                task_list=[task],
                current_task=task,
                output={"message": "done"},
                diagnostics={
                    "_router_context": {
                        "skill_names": ["transfer-routing"],
                        "reference_ids": ["ref_001"],
                    }
                },
            )
        ),
    )
    assert service.assistant is not None

    service.handle_message(RouterMessageRequest(custID="C0001", sessionId="final_clear_session", txt="给小明转账200"))
    result = service.handle_task_completion(
        TaskCompletionRequest(custID="C0001", 
            sessionId="final_clear_session",
            taskId="task_transfer",
            completionSignal=2,
        )
    )
    saved = service.assistant.sessions.get_task_state("final_clear_session")

    assert result.final_frame.status == "completed"
    assert saved.current_task is None
    assert saved.slot_memory == {}
    assert saved.task_list == []
    assert saved.active_context == {}
    assert saved.context_leases == []


def test_assistant_service_saves_and_releases_context_lease(tmp_path: Path) -> None:
    task = PlannedTask(
        taskId="task_transfer",
        intent_code="AG_TRANS",
        status="waiting_user_input",
        title="转账",
    )
    planner = StaticPlanner(
        PlannerOutput(
            mode="slot_filling",
            status="waiting_user_input",
            completion_state=0,
            completion_reason="router_waiting_user_input",
            intent_code="AG_TRANS",
            recognition=RecognitionPlan(intent_code="AG_TRANS"),
            task_list=[task],
            current_task=task,
            message="请提供金额",
            diagnostics={
                "_router_context": {
                    "agent_contexts": ["/tmp/agent.md"],
                    "metadata_skills": ["transfer-routing"],
                    "skill_names": ["transfer-routing"],
                    "reference_ids": ["ref_001"],
                }
            },
        )
    )
    service = IntentRouterHarnessService.from_spec(
        _write_minimal_harness(tmp_path),
        message_planner=planner,
    )
    assert service.assistant is not None

    service.handle_message(
        RouterMessageRequest(custID="C0001", 
            sessionId="lease_session",
            txt="我要转账",
            stream=False,
            executionMode="router_only",
        )
    )
    task_state = service.assistant.sessions.get_task_state("lease_session")

    assert task_state.active_context["task_id"] == "task_transfer"
    assert task_state.active_context["skill_names"] == ["transfer-routing"]
    assert task_state.active_context["reference_ids"] == ["ref_001"]

    service.handle_task_completion(
        TaskCompletionRequest(custID="C0001", 
            sessionId="lease_session",
            taskId="task_transfer",
            completionSignal=2,
            stream=False,
        )
    )

    saved_after_completion = service.assistant.sessions.get_task_state("lease_session")
    assert saved_after_completion.task_list == []
    assert saved_after_completion.active_context == {}


def test_terminal_message_plan_clears_persisted_current_task_and_context(tmp_path: Path) -> None:
    task = PlannedTask(
        taskId="task_transfer",
        intent_code="AG_TRANS",
        status="cancelled",
        title="转账",
        slot_memory={"payee_name": "小明"},
    )
    service = IntentRouterHarnessService.from_spec(
        _write_minimal_harness(tmp_path),
        message_planner=StaticPlanner(
            PlannerOutput(
                mode="cancel",
                status="cancelled",
                completion_state=2,
                completion_reason="assistant_cancel",
                intent_code="AG_TRANS",
                recognition=RecognitionPlan(intent_code="AG_TRANS"),
                slot_memory={"payee_name": "小明"},
                task_list=[task],
                current_task=task,
                message="已取消转账任务",
                diagnostics={
                    "_router_context": {
                        "skill_names": ["transfer-routing"],
                        "reference_ids": ["ref_001"],
                    }
                },
            )
        ),
    )
    assert service.assistant is not None

    result = service.handle_message(
        RouterMessageRequest(custID="C0001", sessionId="cancel_session", txt="取消")
    )
    saved = service.assistant.sessions.get_task_state("cancel_session")

    assert result.final_frame.status == "cancelled"
    assert result.final_frame.current_task is not None
    assert saved.current_task is None
    assert saved.slot_memory == {}
    assert saved.task_list == []
    assert saved.active_context == {}
    assert saved.context_leases == []


def test_terminal_current_task_without_task_list_updates_runtime_state(tmp_path: Path) -> None:
    initial_task = PlannedTask(
        taskId="task_transfer",
        intent_code="AG_TRANS",
        status="waiting_user_input",
        title="转账",
        slot_memory={"payee_name": "小明"},
    )
    service = IntentRouterHarnessService.from_spec(
        _write_minimal_harness(tmp_path),
        message_planner=StaticPlanner(
            PlannerOutput(
                mode="slot_filling",
                status="waiting_user_input",
                completion_state=0,
                completion_reason="router_waiting_user_input",
                intent_code="AG_TRANS",
                recognition=RecognitionPlan(intent_code="AG_TRANS"),
                task_list=[initial_task],
                current_task=initial_task,
                diagnostics={
                    "_router_context": {
                        "skill_names": ["transfer-routing"],
                    }
                },
            )
        ),
    )
    assert service.assistant is not None
    service.handle_message(RouterMessageRequest(custID="C0001", sessionId="terminal_without_list_session", txt="我要转账"))

    cancelled_task = initial_task.model_copy(update={"status": "cancelled"})
    service.assistant.planner = StaticPlanner(
        PlannerOutput(
            mode="cancel",
            status="cancelled",
            completion_state=2,
            completion_reason="assistant_cancel",
            intent_code="AG_TRANS",
            recognition=RecognitionPlan(intent_code="AG_TRANS"),
            current_task=cancelled_task,
            message="已取消转账任务",
        )
    )

    service.handle_message(RouterMessageRequest(custID="C0001", sessionId="terminal_without_list_session", txt="取消"))
    saved = service.assistant.sessions.get_task_state("terminal_without_list_session")

    assert saved.current_task is None
    assert saved.slot_memory == {}
    assert saved.active_context == {}
    assert saved.task_list == []


def test_task_completion_advances_to_next_waiting_task(tmp_path: Path) -> None:
    first_task = PlannedTask(
        taskId="task_001",
        intent_code="AG_TRANS",
        status="ready_for_dispatch",
        title="转账给王阳明",
        slot_memory={"payee_name": "王阳明", "amount": "100"},
    )
    second_task = PlannedTask(
        taskId="task_002",
        intent_code="AG_TRANS",
        status="waiting_user_input",
        title="转账给李正义",
        slot_memory={"payee_name": "李正义"},
    )
    service = IntentRouterHarnessService.from_spec(
        _write_minimal_harness(tmp_path),
        message_planner=StaticPlanner(
            PlannerOutput(
                mode="multi_task",
                status="ready_for_dispatch",
                completion_state=0,
                completion_reason="router_ready_for_dispatch",
                intent_code="AG_TRANS",
                recognition=RecognitionPlan(intent_code="AG_TRANS"),
                slot_memory={"payee_name": "王阳明", "amount": "100"},
                task_list=[first_task, second_task],
                current_task=first_task,
                output={
                    "ishandover": True,
                    "handOverReason": "router_only_ready_for_dispatch",
                },
                diagnostics={
                    "_router_context": {
                        "agent_contexts": ["/tmp/agent.md"],
                        "metadata_skills": ["transfer-routing"],
                        "skill_names": ["transfer-routing"],
                        "reference_ids": [],
                    }
                },
            )
        ),
    )
    assert service.assistant is not None

    service.handle_message(
        RouterMessageRequest(custID="C0001", 
            sessionId="multi_completion_session",
            txt="我要先给王阳明转账，再给李正义转账",
            executionMode="router_only",
        )
    )
    result = service.handle_task_completion(
        TaskCompletionRequest(custID="C0001", 
            sessionId="multi_completion_session",
            taskId="task_001",
            completionSignal=2,
        )
    )
    saved = service.assistant.sessions.get_task_state("multi_completion_session")

    assert [frame.status for frame in result.frames] == ["completed", "waiting_user_input"]
    assert result.final_frame.current_task is not None
    assert result.final_frame.current_task["taskId"] == "task_002"
    assert result.final_frame.slot_memory == {"payee_name": "李正义"}
    assert saved.current_task is not None
    assert saved.current_task.taskId == "task_002"
    assert saved.slot_memory == {"payee_name": "李正义"}
    assert [task.taskId for task in saved.task_list] == ["task_002"]
    assert saved.active_context["task_id"] == "task_002"


def test_ready_task_persists_next_waiting_task_for_followup_input(tmp_path: Path) -> None:
    first_task = PlannedTask(
        taskId="task_001",
        intent_code="AG_TRANS",
        status="ready_for_dispatch",
        title="转账给王阳明",
        slot_memory={"payee_name": "王阳明", "amount": "100"},
    )
    second_task = PlannedTask(
        taskId="task_002",
        intent_code="AG_TRANS",
        status="waiting_user_input",
        title="转账给李正义",
        slot_memory={"payee_name": "李正义"},
    )
    service = IntentRouterHarnessService.from_spec(
        _write_minimal_harness(tmp_path),
        message_planner=StaticPlanner(
            PlannerOutput(
                mode="multi_task",
                status="ready_for_dispatch",
                completion_state=0,
                completion_reason="router_ready_for_dispatch",
                intent_code="AG_TRANS",
                recognition=RecognitionPlan(intent_code="AG_TRANS"),
                slot_memory={"payee_name": "王阳明", "amount": "100"},
                task_list=[first_task, second_task],
                current_task=first_task,
                diagnostics={
                    "_router_context": {
                        "agent_contexts": ["/tmp/agent.md"],
                        "metadata_skills": ["transfer-routing"],
                        "skill_names": ["transfer-routing"],
                        "reference_ids": [],
                    }
                },
            )
        ),
    )
    assert service.assistant is not None

    result = service.handle_message(
        RouterMessageRequest(
            custID="C0001",
            sessionId="ready_followup_session",
            txt="我要先给王阳明转账，再给李正义转账",
            executionMode="router_only",
        )
    )
    saved = service.assistant.sessions.get_task_state("ready_followup_session")

    assert result.final_frame.current_task is not None
    assert result.final_frame.current_task["taskId"] == "task_001"
    assert saved.current_task is not None
    assert saved.current_task.taskId == "task_002"
    assert saved.slot_memory == {"payee_name": "李正义"}


def test_cross_skill_followup_task_keeps_its_context_lease(tmp_path: Path) -> None:
    bill_waiting = PlannedTask(
        taskId="task_001",
        intent_code="AG_PAY_BILL",
        status="waiting_user_input",
        title="缴费",
    )
    bill_ready = bill_waiting.model_copy(
        update={
            "status": "ready_for_dispatch",
            "slot_memory": {"payment_item": "水电费", "amount": "100"},
        }
    )
    transfer_waiting = PlannedTask(
        taskId="task_002",
        intent_code="AG_TRANS",
        status="waiting_user_input",
        title="转账",
    )
    all_skill_context = {
        "agent_contexts": ["/tmp/agent.md"],
        "metadata_skills": ["bill-payment-routing", "transfer-routing"],
        "skill_names": ["bill-payment-routing", "transfer-routing"],
        "skill_intent_map": {
            "bill-payment-routing": ["AG_PAY_BILL"],
            "transfer-routing": ["AG_TRANS"],
        },
        "intent_skill_map": {
            "AG_PAY_BILL": ["bill-payment-routing"],
            "AG_TRANS": ["transfer-routing"],
        },
    }
    bill_only_context = {
        "agent_contexts": ["/tmp/agent.md"],
        "metadata_skills": ["bill-payment-routing", "transfer-routing"],
        "skill_names": ["bill-payment-routing"],
        "skill_intent_map": {
            "bill-payment-routing": ["AG_PAY_BILL"],
        },
        "intent_skill_map": {
            "AG_PAY_BILL": ["bill-payment-routing"],
        },
    }
    service = IntentRouterHarnessService.from_spec(
        _write_minimal_harness(tmp_path),
        message_planner=SequencePlanner(
            [
                PlannerOutput(
                    mode="multi_task",
                    status="waiting_user_input",
                    completion_state=0,
                    completion_reason="router_waiting_user_input",
                    intent_code="AG_PAY_BILL",
                    recognition=RecognitionPlan(intent_code="AG_PAY_BILL"),
                    task_list=[bill_waiting, transfer_waiting],
                    current_task=bill_waiting,
                    message="请提供缴费名目和缴费金额",
                    diagnostics={"_router_context": all_skill_context},
                ),
                PlannerOutput(
                    mode="slot_filling",
                    status="ready_for_dispatch",
                    completion_state=0,
                    completion_reason="router_ready_for_dispatch",
                    intent_code="AG_PAY_BILL",
                    recognition=RecognitionPlan(intent_code="AG_PAY_BILL"),
                    slot_memory={"payment_item": "水电费", "amount": "100"},
                    task_list=[bill_ready, transfer_waiting],
                    current_task=bill_ready,
                    diagnostics={"_router_context": bill_only_context},
                ),
            ]
        ),
    )
    assert service.assistant is not None

    service.handle_message(
        RouterMessageRequest(custID="C0001", sessionId="cross_skill_session", txt="先缴费，再转账")
    )
    state_after_seed = service.assistant.sessions.get_task_state("cross_skill_session")
    transfer_lease = next(
        lease for lease in state_after_seed.context_leases if lease["task_id"] == "task_002"
    )
    assert transfer_lease["skill_names"] == ["transfer-routing"]

    service.handle_message(
        RouterMessageRequest(custID="C0001", sessionId="cross_skill_session", txt="缴水电费100元")
    )
    state_after_bill_slots = service.assistant.sessions.get_task_state("cross_skill_session")
    transfer_lease = next(
        lease for lease in state_after_bill_slots.context_leases if lease["task_id"] == "task_002"
    )
    assert transfer_lease["skill_names"] == ["transfer-routing"]

    result = service.handle_task_completion(
        TaskCompletionRequest(
            custID="C0001",
            sessionId="cross_skill_session",
            taskId="task_001",
            completionSignal=2,
        )
    )
    saved = service.assistant.sessions.get_task_state("cross_skill_session")

    assert result.final_frame.status == "waiting_user_input"
    assert result.final_frame.intent_code == "AG_TRANS"
    assert saved.current_task is not None
    assert saved.current_task.taskId == "task_002"
    assert saved.active_context["skill_names"] == ["transfer-routing"]


def test_cross_skill_single_message_keeps_transfer_lease_after_bill_completion(
    tmp_path: Path,
) -> None:
    bill_waiting = PlannedTask(
        taskId="task_001",
        intent_code="AG_PAY_BILL",
        status="waiting_user_input",
        title="缴费",
    )
    transfer_waiting = PlannedTask(
        taskId="task_002",
        intent_code="AG_TRANS",
        status="waiting_user_input",
        title="转账",
    )
    router_context = {
        "agent_contexts": ["/tmp/agent.md"],
        "metadata_skills": ["bill-payment-routing", "transfer-routing"],
        "skill_names": ["bill-payment-routing", "transfer-routing"],
        "skill_intent_map": {
            "bill-payment-routing": ["AG_PAY_BILL"],
            "transfer-routing": ["AG_TRANS"],
        },
        "intent_skill_map": {
            "AG_PAY_BILL": ["bill-payment-routing"],
            "AG_TRANS": ["transfer-routing"],
        },
    }
    service = IntentRouterHarnessService.from_spec(
        _write_minimal_harness(tmp_path),
        message_planner=SequencePlanner(
            [
                PlannerOutput(
                    mode="multi_task",
                    status="waiting_user_input",
                    completion_state=0,
                    completion_reason="router_waiting_user_input",
                    intent_code="AG_PAY_BILL",
                    recognition=RecognitionPlan(intent_code="AG_PAY_BILL"),
                    task_list=[bill_waiting, transfer_waiting],
                    current_task=bill_waiting,
                    message="请提供缴费名目和缴费金额",
                    diagnostics={"_router_context": router_context},
                )
            ]
        ),
    )
    assert service.assistant is not None

    result = service.handle_message(
        RouterMessageRequest(
            custID="C0001",
            sessionId="single_turn_cross_skill",
            txt="先缴费，再转账",
        )
    )
    saved = service.assistant.sessions.get_task_state("single_turn_cross_skill")

    assert result.final_frame.task_list[1]["intent_code"] == "AG_TRANS"
    assert saved.context_leases[1]["skill_names"] == ["transfer-routing"]

    completion = service.handle_task_completion(
        TaskCompletionRequest(
            custID="C0001",
            sessionId="single_turn_cross_skill",
            taskId="task_001",
            completionSignal=2,
        )
    )
    saved_after_completion = service.assistant.sessions.get_task_state("single_turn_cross_skill")

    assert completion.final_frame.status == "waiting_user_input"
    assert completion.final_frame.intent_code == "AG_TRANS"
    assert saved_after_completion.current_task is not None
    assert saved_after_completion.current_task.taskId == "task_002"
    assert saved_after_completion.active_context["skill_names"] == ["transfer-routing"]


def test_current_task_status_is_not_downgraded_to_running(tmp_path: Path) -> None:
    current_task = PlannedTask(
        taskId="task_001",
        intent_code="AG_TRANS",
        status="waiting_user_input",
        title="转账给王阳明",
        slot_memory={"payee_name": "王阳明"},
    )
    service = IntentRouterHarnessService.from_spec(
        _write_minimal_harness(tmp_path),
        message_planner=StaticPlanner(
            PlannerOutput(
                mode="multi_task",
                status="running",
                completion_state=0,
                completion_reason="router_waiting_user_input",
                intent_code="AG_TRANS",
                recognition=RecognitionPlan(intent_code="AG_TRANS"),
                slot_memory={"payee_name": "王阳明"},
                task_list=[
                    current_task,
                    PlannedTask(
                        taskId="task_002",
                        intent_code="AG_TRANS",
                        status="waiting_user_input",
                        title="转账给李正义",
                        slot_memory={"payee_name": "李正义"},
                    ),
                ],
                current_task=current_task,
                message="请提供给王阳明的转账金额",
            )
        ),
    )
    assert service.assistant is not None

    result = service.handle_message(
        RouterMessageRequest(custID="C0001", 
            sessionId="multi_status_session",
            txt="我要先给王阳明转账，再给李正义转账",
        )
    )
    saved = service.assistant.sessions.get_task_state("multi_status_session")

    assert result.final_frame.status == "waiting_user_input"
    assert result.final_frame.current_task is not None
    assert result.final_frame.current_task["status"] == "waiting_user_input"
    assert result.final_frame.current_task["slot_memory"] == {"payee_name": "王阳明"}
    assert result.final_frame.task_list[0]["status"] == "waiting_user_input"
    assert saved.current_task is not None
    assert saved.current_task.status == "waiting_user_input"


def test_current_task_slot_memory_updates_protocol_and_session(tmp_path: Path) -> None:
    current_task = PlannedTask(
        taskId="task_001",
        intent_code="AG_TRANS",
        status="ready_for_dispatch",
        title="转账给王阳明",
        slot_memory={"payee_name": "王阳明", "amount": "100"},
    )
    service = IntentRouterHarnessService.from_spec(
        _write_minimal_harness(tmp_path),
        message_planner=StaticPlanner(
            PlannerOutput(
                mode="slot_filling",
                status="ready_for_dispatch",
                completion_state=0,
                completion_reason="router_ready_for_dispatch",
                intent_code="AG_TRANS",
                recognition=RecognitionPlan(intent_code="AG_TRANS"),
                slot_memory={"payee_name": "王阳明"},
                task_list=[current_task],
                current_task=current_task,
                output={
                    "ishandover": True,
                    "handOverReason": "router_only_ready_for_dispatch",
                },
            )
        ),
    )
    assert service.assistant is not None

    result = service.handle_message(
        RouterMessageRequest(custID="C0001", 
            sessionId="current_task_slots_session",
            txt="100元",
            executionMode="router_only",
        )
    )
    saved = service.assistant.sessions.get_task_state("current_task_slots_session")

    assert result.final_frame.slot_memory == {"payee_name": "王阳明", "amount": "100"}
    assert saved.slot_memory == {"payee_name": "王阳明", "amount": "100"}


def test_execute_ready_task_invokes_validated_workflow_request(tmp_path: Path) -> None:
    current_task = PlannedTask(
        taskId="task_001",
        intent_code="AG_TRANS",
        status="ready_for_dispatch",
        title="转账给陈广荣",
        slot_memory={"payee_name": "陈广荣", "amount": "500"},
    )
    workflow_client = FakeWorkflowClient(
        [
            WorkflowToolEvent(
                node_id="start",
                node_title="开始",
                timestamp="2026-05-08 19:18:58.035",
                node_output={"started": True},
            ),
            WorkflowToolEvent(
                node_id="end",
                node_title="结束",
                timestamp="2026-05-08 19:19:04.553",
                node_output={"output": "opaque", "exception": None},
            ),
        ]
    )
    executor = _build_test_executor(
        tool_call_arguments=_transfer_workflow_request(),
        workflow_client=workflow_client,
    )
    service = IntentRouterHarnessService.from_spec(
        _write_minimal_harness(tmp_path),
        message_planner=StaticPlanner(
            PlannerOutput(
                mode="single_task",
                status="ready_for_dispatch",
                completion_state=0,
                completion_reason="router_ready_for_dispatch",
                intent_code="AG_TRANS",
                recognition=RecognitionPlan(intent_code="AG_TRANS"),
                task_list=[current_task],
                current_task=current_task,
            )
        ),
    )
    assert service.assistant is not None
    service.assistant.executor = executor

    result = service.handle_message(
        RouterMessageRequest(
            custID="1631102265490929",
            sessionId="1635501196813426",
            txt="给陈广荣转500元",
            executionMode="execute",
            config_variables=[
                {"name": "sessionID", "value": "1635501196813426"},
                {"name": "currentDisplay", "value": ""},
                {"name": "agentSessionID", "value": "1635501196813426"},
            ],
        )
    )
    saved = service.assistant.sessions.get_task_state("1635501196813426")

    assert len(workflow_client.calls) == 1
    _, payload = workflow_client.calls[0]
    assert payload.method == "POST"
    assert payload.url == "http://127.0.0.1:9876/agent-api/workflow-agent-1-1b14f16b/chatabc/use_as_tool"
    assert [frame.completion_reason for frame in result.frames][-3:] == [
        "workflow_node_output",
        "workflow_node_output",
        "workflow_done",
    ]
    assert result.frames[-3].output == {"started": True}
    assert result.frames[-2].output == {"output": "opaque", "exception": None}
    assert result.final_frame.status == "completed"
    assert result.final_frame.output == {"output": "opaque", "exception": None}
    assert saved.current_task is None
    assert saved.task_list == []
    assert saved.slot_memory == {}


def test_router_only_ready_task_does_not_invoke_workflow(tmp_path: Path) -> None:
    current_task = PlannedTask(
        taskId="task_001",
        intent_code="AG_TRANS",
        status="ready_for_dispatch",
        slot_memory={"payee_name": "陈广荣", "amount": "500"},
    )
    workflow_client = FakeWorkflowClient(
        [WorkflowToolEvent(node_id="end", node_title="结束", timestamp=None, node_output={"done": True})]
    )
    executor = _build_test_executor(
        tool_call_arguments=_transfer_workflow_request(cust_id="C0001"),
        workflow_client=workflow_client,
    )
    service = IntentRouterHarnessService.from_spec(
        _write_minimal_harness(tmp_path),
        message_planner=StaticPlanner(
            PlannerOutput(
                mode="single_task",
                status="ready_for_dispatch",
                completion_state=0,
                completion_reason="router_ready_for_dispatch",
                intent_code="AG_TRANS",
                recognition=RecognitionPlan(intent_code="AG_TRANS"),
                task_list=[current_task],
                current_task=current_task,
            )
        ),
    )
    assert service.assistant is not None
    service.assistant.executor = executor

    result = service.handle_message(
        RouterMessageRequest(
            custID="C0001",
            sessionId="router_only_workflow_session",
            txt="给陈广荣转500元",
            executionMode="router_only",
        )
    )

    assert workflow_client.calls == []
    assert result.final_frame.status == "ready_for_dispatch"
    assert result.final_frame.completion_reason == "router_ready_for_dispatch"


def test_workflow_node_output_is_opaque_for_string_and_array_values(tmp_path: Path) -> None:
    current_task = PlannedTask(
        taskId="task_001",
        intent_code="AG_TRANS",
        status="ready_for_dispatch",
        slot_memory={"payee_name": "陈广荣", "amount": "500"},
    )
    workflow_client = FakeWorkflowClient(
        [
            WorkflowToolEvent(node_id="string", node_title="字符串", timestamp=None, node_output="opaque-string"),
            WorkflowToolEvent(node_id="array", node_title="数组", timestamp=None, node_output=["opaque", 1]),
        ]
    )
    executor = _build_test_executor(
        tool_call_arguments=_transfer_workflow_request(cust_id="C0001"),
        workflow_client=workflow_client,
    )
    service = IntentRouterHarnessService.from_spec(
        _write_minimal_harness(tmp_path),
        message_planner=StaticPlanner(
            PlannerOutput(
                mode="single_task",
                status="ready_for_dispatch",
                completion_state=0,
                completion_reason="router_ready_for_dispatch",
                intent_code="AG_TRANS",
                recognition=RecognitionPlan(intent_code="AG_TRANS"),
                task_list=[current_task],
                current_task=current_task,
            )
        ),
    )
    assert service.assistant is not None
    service.assistant.executor = executor

    result = service.handle_message(
        RouterMessageRequest(
            custID="C0001",
            sessionId="opaque_workflow_session",
            txt="给陈广荣转500元",
            executionMode="execute",
        )
    )

    assert result.frames[-3].output == "opaque-string"
    assert result.frames[-2].output == ["opaque", 1]
    assert result.final_frame.output == ["opaque", 1]


def test_workflow_error_returns_failed_frame_and_clears_runtime(tmp_path: Path) -> None:
    current_task = PlannedTask(
        taskId="task_001",
        intent_code="AG_TRANS",
        status="ready_for_dispatch",
        slot_memory={"payee_name": "陈广荣", "amount": "500"},
    )
    executor = _build_test_executor(
        tool_call_arguments=_transfer_workflow_request(cust_id="C0001"),
        workflow_client=FakeWorkflowClient(error=WorkflowToolError("boom")),
    )
    service = IntentRouterHarnessService.from_spec(
        _write_minimal_harness(tmp_path),
        message_planner=StaticPlanner(
            PlannerOutput(
                mode="single_task",
                status="ready_for_dispatch",
                completion_state=0,
                completion_reason="router_ready_for_dispatch",
                intent_code="AG_TRANS",
                recognition=RecognitionPlan(intent_code="AG_TRANS"),
                task_list=[current_task],
                current_task=current_task,
            )
        ),
    )
    assert service.assistant is not None
    service.assistant.executor = executor

    result = service.handle_message(
        RouterMessageRequest(
            custID="C0001",
            sessionId="workflow_error_session",
            txt="给陈广荣转500元",
            executionMode="execute",
        )
    )
    saved = service.assistant.sessions.get_task_state("workflow_error_session")

    assert result.final_frame.ok is False
    assert result.final_frame.status == "failed"
    assert result.final_frame.completion_reason == "workflow_error"
    assert saved.current_task is None
    assert saved.task_list == []
    assert saved.slot_memory == {}


def test_session_binds_to_single_user_and_rejects_mismatch(tmp_path: Path) -> None:
    task = PlannedTask(taskId="task_transfer", intent_code="AG_TRANS", status="waiting_user_input")
    service = IntentRouterHarnessService.from_spec(
        _write_minimal_harness(tmp_path),
        message_planner=StaticPlanner(
            PlannerOutput(
                mode="slot_filling",
                status="waiting_user_input",
                completion_state=0,
                completion_reason="router_waiting_user_input",
                intent_code="AG_TRANS",
                recognition=RecognitionPlan(intent_code="AG_TRANS"),
                task_list=[task],
                current_task=task,
                message="请提供金额",
            )
        ),
    )
    assert service.assistant is not None

    service.handle_message(
        RouterMessageRequest(
            custID="cust_001",
            sessionId="owned_session",
            txt="我要转账",
        )
    )

    try:
        service.handle_message(
            RouterMessageRequest(
                custID="cust_002",
                sessionId="owned_session",
                txt="我要转账",
            )
        )
    except RuntimeError as exc:
        assert "already bound" in str(exc)
    else:
        raise AssertionError("session user mismatch should be rejected")


def test_session_expires_after_idle_timeout_and_clears_memory(tmp_path: Path) -> None:
    now = datetime(2026, 5, 7, 10, 0, tzinfo=timezone.utc)

    def clock() -> datetime:
        return now

    task = PlannedTask(taskId="task_transfer", intent_code="AG_TRANS", status="waiting_user_input")
    service = IntentRouterHarnessService.from_spec(
        _write_minimal_harness(tmp_path),
        message_planner=StaticPlanner(
            PlannerOutput(
                mode="slot_filling",
                status="waiting_user_input",
                completion_state=0,
                completion_reason="router_waiting_user_input",
                intent_code="AG_TRANS",
                recognition=RecognitionPlan(intent_code="AG_TRANS"),
                slot_memory={"payee_name": "小明"},
                task_list=[task],
                current_task=task,
                message="请提供金额",
                diagnostics={
                    "_router_context": {
                        "skill_names": ["transfer-routing"],
                        "reference_ids": ["ref_001"],
                    }
                },
            )
        ),
    )
    assert service.assistant is not None
    service.assistant.sessions = InMemorySessionStore(clock=clock)

    service.handle_message(
        RouterMessageRequest(
            custID="cust_001",
            sessionId="expiring_session",
            txt="给小明转账",
        )
    )
    saved = service.assistant.sessions.get_task_state("expiring_session")
    assert saved.slot_memory == {"payee_name": "小明"}
    assert saved.context_leases

    now = now + timedelta(minutes=31)
    expired = service.assistant.sessions.load("expiring_session", user_binding_id="cust_001")

    assert expired.expired is True
    assert expired.task_state.slot_memory == {}
    assert expired.task_state.task_list == []
    assert expired.task_state.current_task is None
    assert expired.task_state.active_context == {}
    assert expired.task_state.context_leases == []


def test_task_completion_rejects_replay_after_runtime_task_is_cleared(tmp_path: Path) -> None:
    task = PlannedTask(
        taskId="task_transfer",
        intent_code="AG_TRANS",
        status="waiting_assistant_completion",
        output={"message": "done"},
    )
    service = IntentRouterHarnessService.from_spec(
        _write_minimal_harness(tmp_path),
        message_planner=StaticPlanner(
            PlannerOutput(
                mode="single_task",
                status="waiting_assistant_completion",
                completion_state=1,
                completion_reason="assistant_confirmation_required",
                intent_code="AG_TRANS",
                recognition=RecognitionPlan(intent_code="AG_TRANS"),
                task_list=[task],
                current_task=task,
                output={"message": "done"},
            )
        ),
    )
    assert service.assistant is not None

    service.handle_message(RouterMessageRequest(custID="C0001", sessionId="replay_session", txt="给小明转账200"))
    first = service.handle_task_completion(
        TaskCompletionRequest(custID="C0001", 
            sessionId="replay_session",
            taskId="task_transfer",
            completionSignal=2,
        )
    )
    second = service.handle_task_completion(
        TaskCompletionRequest(custID="C0001", 
            sessionId="replay_session",
            taskId="task_transfer",
            completionSignal=2,
        )
    )

    assert first.final_frame.status == "completed"
    assert second.final_frame.ok is False
    assert second.final_frame.errorCode == "TASK_NOT_FOUND"


def test_task_completion_rejects_user_mismatch(tmp_path: Path) -> None:
    task = PlannedTask(
        taskId="task_transfer",
        intent_code="AG_TRANS",
        status="waiting_assistant_completion",
        output={"message": "done"},
    )
    service = IntentRouterHarnessService.from_spec(
        _write_minimal_harness(tmp_path),
        message_planner=StaticPlanner(
            PlannerOutput(
                mode="single_task",
                status="waiting_assistant_completion",
                completion_state=1,
                completion_reason="assistant_confirmation_required",
                intent_code="AG_TRANS",
                recognition=RecognitionPlan(intent_code="AG_TRANS"),
                task_list=[task],
                current_task=task,
                output={"message": "done"},
            )
        ),
    )
    assert service.assistant is not None

    service.handle_message(
        RouterMessageRequest(
            custID="cust_001",
            sessionId="completion_owned_session",
            txt="给小明转账200",
        )
    )

    with pytest.raises(RuntimeError, match="already bound"):
        service.handle_task_completion(
            TaskCompletionRequest(
                custID="cust_002",
                sessionId="completion_owned_session",
                taskId="task_transfer",
                completionSignal=2,
            )
        )


def test_llm_planner_rejects_intent_not_declared_by_loaded_skill(tmp_path: Path) -> None:
    skills_root = tmp_path / "skills"
    skill_dir = skills_root / "transfer-routing"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "\n".join(
            [
                "---",
                "name: transfer-routing",
                "description: 转账路由规则",
                'intent_codes: ["AG_TRANS"]',
                "---",
                "# 转账",
                "只能输出 AG_TRANS。",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    spec_path = tmp_path / "planner-harness.toml"
    spec_path.write_text(
        "\n".join(
            [
                'name = "planner-intent-validation"',
                'version = "2026.05"',
                f'skill_roots = ["{skills_root.as_posix()}"]',
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    harness = load_prompt_harness(spec_path)
    assert harness is not None
    planner = LLMMessagePlanner(
        harness=harness,
        llm_client=FakeLLMClient(
            [
                '{"skill_names":["transfer-routing"],"reason":"transfer"}',
                json.dumps(
                    {
                        "mode": "single_task",
                        "status": "waiting_user_input",
                        "completion_reason": "router_waiting_user_input",
                        "intent_code": "AG_BALANCE",
                        "recognition": {"intent_code": "AG_BALANCE"},
                        "task_list": [
                            {
                                "taskId": "task_balance",
                                "intent_code": "AG_BALANCE",
                                "status": "waiting_user_input",
                            }
                        ],
                        "current_task": {
                            "taskId": "task_balance",
                            "intent_code": "AG_BALANCE",
                            "status": "waiting_user_input",
                        },
                    }
                ),
            ]
        ),
    )

    with pytest.raises(PlannerError, match="not declared by loaded skills"):
        planner.plan_message(
            RouterMessageRequest(custID="C0001", sessionId="invalid_intent_session", txt="我要转账"),
            TaskRuntimeState(),
        )


def test_llm_planner_allows_existing_unloaded_task_intent_in_task_list(tmp_path: Path) -> None:
    skills_root = tmp_path / "skills"
    transfer_dir = skills_root / "transfer-routing"
    transfer_dir.mkdir(parents=True)
    (transfer_dir / "SKILL.md").write_text(
        "\n".join(
            [
                "---",
                "name: transfer-routing",
                "description: 转账路由规则",
                'intent_codes: ["AG_TRANS"]',
                "---",
                "# 转账",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    bill_dir = skills_root / "bill-routing"
    bill_dir.mkdir(parents=True)
    (bill_dir / "SKILL.md").write_text(
        "\n".join(
            [
                "---",
                "name: bill-routing",
                "description: 缴费路由规则",
                'intent_codes: ["AG_PAY_BILL"]',
                "---",
                "# 缴费",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    spec_path = tmp_path / "planner-harness.toml"
    spec_path.write_text(
        "\n".join(
            [
                'name = "planner-existing-task-validation"',
                'version = "2026.05"',
                f'skill_roots = ["{skills_root.as_posix()}"]',
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    harness = load_prompt_harness(spec_path)
    assert harness is not None
    transfer_task = PlannedTask(
        taskId="task_001",
        intent_code="AG_TRANS",
        status="waiting_user_input",
        title="转账",
    )
    bill_task = PlannedTask(
        taskId="task_002",
        intent_code="AG_PAY_BILL",
        status="waiting_user_input",
        title="缴费",
    )
    planner = LLMMessagePlanner(
        harness=harness,
        llm_client=FakeLLMClient(
            [
                json.dumps(
                    {
                        "mode": "slot_filling",
                        "status": "ready_for_dispatch",
                        "completion_reason": "router_ready_for_dispatch",
                        "intent_code": "AG_TRANS",
                        "recognition": {"intent_code": "AG_TRANS"},
                        "slot_memory": {"payee_name": "孙双双", "amount": "8999"},
                        "task_list": [
                            {
                                "taskId": "task_001",
                                "intent_code": "AG_TRANS",
                                "status": "ready_for_dispatch",
                                "slot_memory": {"payee_name": "孙双双", "amount": "8999"},
                            },
                            {
                                "taskId": "task_002",
                                "intent_code": "AG_PAY_BILL",
                                "status": "waiting_user_input",
                            },
                        ],
                        "current_task": {
                            "taskId": "task_001",
                            "intent_code": "AG_TRANS",
                            "status": "ready_for_dispatch",
                            "slot_memory": {"payee_name": "孙双双", "amount": "8999"},
                        },
                    }
                )
            ]
        ),
    )

    plan = planner.plan_message(
        RouterMessageRequest(custID="C0001", sessionId="existing_task_intent_session", txt="给孙双双转8999"),
        TaskRuntimeState(
            task_list=[transfer_task, bill_task],
            current_task=transfer_task,
            active_context={
                "task_id": "task_001",
                "intent_code": "AG_TRANS",
                "skill_names": ["transfer-routing"],
            },
            context_leases=[
                {"task_id": "task_001", "intent_code": "AG_TRANS", "skill_names": ["transfer-routing"]},
                {"task_id": "task_002", "intent_code": "AG_PAY_BILL", "skill_names": ["bill-routing"]},
            ],
        ),
    )

    assert [task.intent_code for task in plan.task_list] == ["AG_TRANS", "AG_PAY_BILL"]


def test_llm_planner_exposes_config_variables_but_sanitizes_other_context(tmp_path: Path) -> None:
    spec_path = tmp_path / "planner-harness.toml"
    spec_path.write_text(
        "\n".join(
            [
                'name = "planner-private-context"',
                'version = "2026.05"',
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    harness = load_prompt_harness(spec_path)
    assert harness is not None
    llm_client = FakeLLMClient(
        [
            json.dumps(
                {
                    "mode": "failed",
                    "status": "failed",
                    "completion_reason": "no_intent",
                }
            )
        ]
    )
    planner = LLMMessagePlanner(harness=harness, llm_client=llm_client)

    planner.plan_message(
        RouterMessageRequest(
            custID="cust_should_not_reach_llm",
            sessionId="session_should_not_reach_llm",
            txt="你好",
            config_variables=[
                {"name": "sessionID", "value": "session_should_not_reach_llm"},
                {"name": "agentSessionID", "value": "agent_should_not_reach_llm"},
                {"name": "custID", "value": "cust_should_not_reach_llm"},
                {"name": "currentDisplay", "value": "validator_page"},
                {
                    "name": "nested",
                    "value": {
                        "sessionId": "nested_session_should_not_reach_llm",
                        "business": "kept",
                    },
                },
            ],
            recommendTask=[
                {
                    "title": "推荐任务",
                    "sessionId": "recommend_session_should_not_reach_llm",
                    "payload": {"agentSessionID": "recommend_agent_should_not_reach_llm"},
                }
            ],
            currentDisplay=[
                {
                    "page": "validator",
                    "agent_session_id": "display_agent_should_not_reach_llm",
                }
            ],
        ),
        TaskRuntimeState(),
    )

    rendered_prompt = "\n".join(message["content"] for message in llm_client.messages[-1])
    assert "session_should_not_reach_llm" in rendered_prompt
    assert "agent_should_not_reach_llm" in rendered_prompt
    assert "cust_should_not_reach_llm" in rendered_prompt
    assert "nested_session_should_not_reach_llm" in rendered_prompt
    assert "recommend_session_should_not_reach_llm" not in rendered_prompt
    assert "recommend_agent_should_not_reach_llm" not in rendered_prompt
    assert "display_agent_should_not_reach_llm" not in rendered_prompt
    assert "validator_page" in rendered_prompt
    assert "business" in rendered_prompt


def test_llm_planner_loads_slot_filling_reference_by_default(tmp_path: Path) -> None:
    skills_root = tmp_path / "skills"
    skill_dir = skills_root / "transfer-routing"
    reference_dir = skill_dir / "references"
    reference_dir.mkdir(parents=True)
    (reference_dir / "slot_filling.md").write_text("默认提槽规则：必须提取 payee_name 和 amount。", encoding="utf-8")
    (skill_dir / "SKILL.md").write_text(
        "\n".join(
            [
                "---",
                "name: transfer-routing",
                "description: 转账路由规则",
                'intent_codes: ["AG_TRANS"]',
                'required_slots: ["payee_name", "amount"]',
                'references: [{"id":"slot_filling","path":"references/slot_filling.md","purpose":"转账提槽规则"}]',
                "---",
                "# 转账路由",
                "只描述意图边界。",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    spec_path = tmp_path / "planner-harness.toml"
    spec_path.write_text(
        "\n".join(
            [
                'name = "planner-slot-reference"',
                'version = "2026.05"',
                f'skill_roots = ["{skills_root.as_posix()}"]',
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    harness = load_prompt_harness(spec_path)
    assert harness is not None
    llm_client = FakeLLMClient(
        [
            json.dumps(
                {
                    "tasks": [
                        {
                            "taskId": "task_001",
                            "intent_code": "AG_TRANS",
                            "title": "转账",
                            "source_text": "给陈广荣转500元",
                        }
                    ]
                }
            ),
            json.dumps({"slot_memory": {"payee_name": "陈广荣", "amount": 500}}),
        ]
    )
    planner = LLMMessagePlanner(harness=harness, llm_client=llm_client)

    plan = planner.plan_message(
        RouterMessageRequest(custID="C0001", sessionId="slot_ref_session", txt="给陈广荣转500元"),
        TaskRuntimeState(),
    )

    slot_prompt = "\n".join(message["content"] for message in llm_client.messages[-1])
    assert plan.status == "ready_for_dispatch"
    assert "默认提槽规则" in slot_prompt
