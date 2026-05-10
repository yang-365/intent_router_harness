from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from threading import Event, Thread

import pytest

from intent_router_harness.contracts import (
    AssistantProtocolFrame,
    PlannedTask,
    RouterMessageRequest,
    TaskRuntimeState,
)
from intent_router_harness.deepagent_service import (
    DeepAgentRunContext,
    DeepAgentRunResult,
    NativeDeepAgentRunner,
    SessionRunLockStore,
    WorkflowApiCallError,
    _deepagent_user_payload,
    _deepagent_system_prompt,
    _deepagent_runtime_middleware,
    _deepagent_graph_recursion_limit,
    _harness_virtual_files,
    _loads_json_object,
    _harness_context_block,
    _parse_deepagent_response,
    _result_with_workflow_call,
    _result_with_workflow_final_output,
    _resolve_deepagent_model,
    _try_parse_deepagent_response,
    _workflow_request_from_content,
    _workflow_session_id,
)
from intent_router_harness.service import IntentRouterHarnessService


class FakeDeepAgentRunner:
    def __init__(self) -> None:
        self.contexts: list[DeepAgentRunContext] = []

    def run_message(self, context: DeepAgentRunContext) -> DeepAgentRunResult:
        self.contexts.append(context)
        return DeepAgentRunResult(
            frames=(
                AssistantProtocolFrame(
                    ok=True,
                    status="completed",
                    completion_state=2,
                    completion_reason="deepagent_done",
                    output={
                        "thread_id": context.thread_id,
                        "skill_names": sorted(context.skills),
                        "config_variables": context.config_variables,
                    },
                ),
            ),
            task_state=TaskRuntimeState(),
        )


class BlockingDeepAgentRunner:
    def __init__(self) -> None:
        self.started = Event()
        self.release = Event()

    def run_message(self, context: DeepAgentRunContext) -> DeepAgentRunResult:
        self.started.set()
        self.release.wait(timeout=5)
        return DeepAgentRunResult(
            frames=(
                AssistantProtocolFrame(
                    ok=True,
                    status="completed",
                    completion_state=2,
                    completion_reason="deepagent_done",
                    output={"thread_id": context.thread_id},
                ),
            ),
            task_state=TaskRuntimeState(),
        )


class FakeWorkflowCommandTool:
    def __init__(self, sse_text: str) -> None:
        self.sse_text = sse_text
        self.payloads: list[dict[str, object]] = []

    def run(self, payload: dict[str, object], *, timeout_seconds: float = 60.0) -> dict[str, str]:
        del timeout_seconds
        self.payloads.append(payload)
        return {"text": self.sse_text}


class StreamingWorkflowAgent:
    def __init__(self, runner: NativeDeepAgentRunner) -> None:
        self.runner = runner
        self.stream_kwargs: dict[str, object] | None = None
        self.invoke_called = False

    def invoke(self, *_args: object, **_kwargs: object) -> object:
        self.invoke_called = True
        raise AssertionError("NativeDeepAgentRunner must use astream")

    async def astream(
        self,
        input_payload: dict[str, object],
        *,
        config: dict[str, object],
        stream_mode: list[str],
        subgraphs: bool,
        durability: str,
    ):
        self.stream_kwargs = {
            "input_payload": input_payload,
            "config": config,
            "stream_mode": stream_mode,
            "subgraphs": subgraphs,
            "durability": durability,
        }
        yield ((), "messages", ({"content": "开始规划转账"}, {}))
        self.runner._workflow_api_call(
            "POST",
            "http://127.0.0.1:9876/agent-api/workflow-agent-1-1b14f16b/chatabc/use_as_tool",
            {
                "session_id": "s1",
                "txt": "给陈广荣转500元",
                "stream": True,
                "config_variables": [
                    {"name": "custID", "value": "C0001"},
                    {"name": "sessionID", "value": "s1"},
                    {"name": "currentDisplay", "value": ""},
                    {"name": "agentSessionID", "value": "s1"},
                    {"name": "slots_data", "value": '{"payee_name":"陈广荣","amount":"500"}'},
                ],
            },
        )
        yield ((), "updates", {"agent": {"messages": [{"content": "工作流已执行"}]}})


class WorkflowThenLoopAgent:
    def __init__(self, runner: NativeDeepAgentRunner) -> None:
        self.runner = runner

    def invoke(self, *_args: object, **_kwargs: object) -> object:
        raise AssertionError("NativeDeepAgentRunner must use astream")

    async def astream(self, *_args: object, **_kwargs: object):
        self.runner._workflow_api_call(
            "POST",
            "http://127.0.0.1:9876/agent-api/workflow-agent-1-1b14f16b/chatabc/use_as_tool",
            {
                "session_id": "s1",
                "config_variables": [
                    {"name": "sessionID", "value": "s1"},
                    {"name": "slots_data", "value": '{"payee_name":"陈广荣","amount":"500"}'},
                ],
            },
        )
        yield ((), "updates", {"agent": {"messages": [{"content": "工作流已完成但模型继续循环"}]}})
        raise RuntimeError("simulated graph recursion after workflow")


def _write_deepagent_harness(tmp_path: Path) -> Path:
    skills_root = tmp_path / "skills"
    skill_dir = skills_root / "transfer-routing"
    references_dir = skill_dir / "references"
    references_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "\n".join(
            [
                "---",
                "name: transfer-routing",
                "description: 转账路由规则",
                'intent_codes: ["AG_TRANS"]',
                'required_slots: ["payee_name", "amount"]',
                'references: [{"id":"slot_filling","path":"references/slot_filling.md","purpose":"转账提槽规则"},{"id":"workflow_request","path":"references/workflow_request.md","purpose":"转账工作流调用"}]',
                "---",
                "# 转账路由",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    (references_dir / "slot_filling.md").write_text("# 转账提槽规则\n", encoding="utf-8")
    (references_dir / "workflow_request.md").write_text(
        "# 转账工作流调用\n\n"
        "调用 URL: http://127.0.0.1:9876/agent-api/workflow-agent-1-1b14f16b/chatabc/use_as_tool\n",
        encoding="utf-8",
    )
    spec_path = tmp_path / "deepagent-harness.toml"
    spec_path.write_text(
        "\n".join(
            [
                'name = "deepagent-test"',
                'version = "2026.05"',
                'agent_runtime = "deepagent"',
                f'skill_roots = ["{skills_root.as_posix()}"]',
                "[deepagent]",
                'model = "fake:model"',
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return spec_path


def test_deepagent_runtime_routes_message_to_runner_with_session_thread(tmp_path: Path) -> None:
    runner = FakeDeepAgentRunner()
    service = IntentRouterHarnessService.from_spec(
        _write_deepagent_harness(tmp_path),
        deepagent_runner=runner,
    )

    result = service.handle_message(
        RouterMessageRequest(
            sessionId="s1",
            custID="C0001",
            txt="给陈广荣转500元",
            stream=True,
            executionMode="execute",
            config_variables=[
                {"name": "custID", "value": "C0001"},
                {"name": "sessionID", "value": "s1"},
            ],
        )
    )

    assert result.final_frame.status == "completed"
    assert result.final_frame.completion_reason == "deepagent_done"
    assert result.final_frame.output["thread_id"] == "C0001:s1"
    assert result.final_frame.output["skill_names"] == ["transfer-routing"]
    assert result.final_frame.output["config_variables"] == {"custID": "C0001", "sessionID": "s1"}
    assert runner.contexts[0].request.txt == "给陈广荣转500元"
    assert "transfer-routing" in runner.contexts[0].references
    model_payload = json.loads(_deepagent_user_payload(runner.contexts[0]))
    assert model_payload["config_variables"] == {"custID": "C0001", "sessionID": "s1"}
    assert "workflow_allowed_urls" not in model_payload
    assert "loaded_skills_and_references" not in model_payload


def test_deepagent_runtime_emits_original_protocol_trace_steps(tmp_path: Path) -> None:
    service = IntentRouterHarnessService.from_spec(
        _write_deepagent_harness(tmp_path),
        deepagent_runner=FakeDeepAgentRunner(),
    )

    result = service.handle_message(
        RouterMessageRequest(
            sessionId="s1",
            custID="C0001",
            txt="给陈广荣转500元",
            stream=True,
            debugTrace=True,
        )
    )

    assert [event.stage for event in result.trace_events] == [
        "request_received",
        "session_loaded",
        "task_runtime_loaded",
        "deepagent_runtime_start",
        "assistant_protocol_frames",
    ]


def test_deepagent_runtime_enforces_session_ownership(tmp_path: Path) -> None:
    runner = FakeDeepAgentRunner()
    service = IntentRouterHarnessService.from_spec(
        _write_deepagent_harness(tmp_path),
        deepagent_runner=runner,
    )

    service.handle_message(RouterMessageRequest(sessionId="s1", custID="C0001", txt="hi"))

    with pytest.raises(Exception, match="already bound"):
        service.handle_message(RouterMessageRequest(sessionId="s1", custID="C0002", txt="hi"))


def test_deepagent_runtime_uses_configured_in_memory_session_timeout(tmp_path: Path) -> None:
    spec_path = _write_deepagent_harness(tmp_path)
    spec_path.write_text(
        spec_path.read_text(encoding="utf-8") + "\n[session]\nidle_timeout_seconds = 7\n",
        encoding="utf-8",
    )

    service = IntentRouterHarnessService.from_spec(
        spec_path,
        deepagent_runner=FakeDeepAgentRunner(),
    )

    assert service.assistant.sessions.idle_timeout == timedelta(seconds=7)


def test_session_run_lock_rejects_same_session_concurrency() -> None:
    locks = SessionRunLockStore()

    with locks.acquire(user_id="C0001", session_id="s1"):
        with pytest.raises(Exception, match="already has an active agent run"):
            with locks.acquire(user_id="C0001", session_id="s1"):
                pass


def test_deepagent_service_rejects_concurrent_run_for_same_session(tmp_path: Path) -> None:
    runner = BlockingDeepAgentRunner()
    service = IntentRouterHarnessService.from_spec(
        _write_deepagent_harness(tmp_path),
        deepagent_runner=runner,
    )
    first_result: list[object] = []

    def run_first_request() -> None:
        first_result.append(
            service.handle_message(RouterMessageRequest(sessionId="s1", custID="C0001", txt="hi"))
        )

    thread = Thread(target=run_first_request)
    thread.start()
    try:
        assert runner.started.wait(timeout=5)

        second_result = service.handle_message(RouterMessageRequest(sessionId="s1", custID="C0001", txt="again"))

        assert second_result.final_frame.status == "failed"
        assert second_result.final_frame.completion_reason == "session_run_in_progress"
        assert second_result.final_frame.errorCode == "session_run_in_progress"
    finally:
        runner.release.set()
        thread.join(timeout=5)

    assert first_result


def test_native_runner_seeds_skill_files_for_deepagent_backend(tmp_path: Path) -> None:
    pytest.importorskip("deepagents.backends.utils")
    service = IntentRouterHarnessService.from_spec(
        _write_deepagent_harness(tmp_path),
        deepagent_runner=FakeDeepAgentRunner(),
    )
    runner = NativeDeepAgentRunner(harness=service.harness)

    files = runner._deepagent_skill_files()

    assert "/skills/transfer-routing/SKILL.md" in files
    assert "/skills/transfer-routing/references/slot_filling.md" in files


def test_harness_context_middleware_builds_reference_context(tmp_path: Path) -> None:
    service = IntentRouterHarnessService.from_spec(
        _write_deepagent_harness(tmp_path),
        deepagent_runner=FakeDeepAgentRunner(),
    )

    context = _harness_context_block(service.harness)

    assert "## Intent Router Harness Loaded Context" in context
    assert "Skill: transfer-routing" in context
    assert "Reference: transfer-routing/workflow_request" in context
    assert "http://127.0.0.1:9876/agent-api/workflow-agent-1-1b14f16b/chatabc/use_as_tool" in context


def test_harness_virtual_files_use_deepagent_skill_paths(tmp_path: Path) -> None:
    service = IntentRouterHarnessService.from_spec(
        _write_deepagent_harness(tmp_path),
        deepagent_runner=FakeDeepAgentRunner(),
    )

    files = _harness_virtual_files(service.harness)

    assert "/skills/transfer-routing/SKILL.md" in files
    assert "/skills/transfer-routing/references/slot_filling.md" in files
    assert "/skills/transfer-routing/references/workflow_request.md" in files


def test_workflow_request_can_be_recovered_from_model_json_text() -> None:
    content = json.dumps(
        {
            "workflow_request": {
                "method": "POST",
                "url": "http://127.0.0.1:9876/agent-api/workflow-agent-1-1b14f16b/chatabc/use_as_tool",
                "body": {"session_id": "s1"},
            }
        },
        ensure_ascii=False,
    )

    assert _workflow_request_from_content(content) == {
        "method": "POST",
        "url": "http://127.0.0.1:9876/agent-api/workflow-agent-1-1b14f16b/chatabc/use_as_tool",
        "body": {"session_id": "s1"},
    }


def test_deepagent_model_can_fall_back_to_router_llm_env(monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("langchain_openai")
    monkeypatch.setenv("ROUTER_LLM_API_BASE_URL", "http://example.test/v1")
    monkeypatch.setenv("ROUTER_LLM_API_KEY", "sk-test")
    monkeypatch.setenv("ROUTER_LLM_MODEL", "Qwen/Qwen3-Coder-30B-A3B-Instruct")
    monkeypatch.setenv("ROUTER_LLM_TIMEOUT_SECONDS", "120")
    monkeypatch.setenv("ROUTER_LLM_ENABLE_THINKING", "false")

    model = _resolve_deepagent_model(None)

    assert model.model_name == "Qwen/Qwen3-Coder-30B-A3B-Instruct"
    assert model.openai_api_base == "http://example.test/v1"
    assert model.request_timeout == 120
    assert model.extra_body == {"enable_thinking": False}
    assert model.use_responses_api is False


def test_parse_deepagent_assistant_turn_as_completed_frame() -> None:
    result = _parse_deepagent_response(
        RouterMessageRequest(sessionId="s1", custID="C0001", txt="hi"),
        {"messages": [{"content": '{"frames":[{"type":"text","output":{"output":"mock-transfer-done","exception":null}}]}'}]},
    )

    assert result.frames[0].status == "completed"
    assert result.frames[0].completion_reason == "deepagent_done"
    assert result.frames[0].output == {"output": "mock-transfer-done", "exception": None}


def test_try_parse_deepagent_response_ignores_invalid_model_summary_after_tool_call() -> None:
    result = _try_parse_deepagent_response(
        RouterMessageRequest(sessionId="s1", custID="C0001", txt="hi"),
        {"messages": [{"content": '{"frames":[{"type":"business","status":"completed","output":"done"}]}'}]},
    )

    assert result is None


def test_workflow_final_output_overrides_model_summary() -> None:
    result = DeepAgentRunResult(
        frames=(
            AssistantProtocolFrame(
                ok=True,
                status="completed",
                completion_state=2,
                completion_reason="deepagent_done",
                output="mock-transfer-done",
            ),
        )
    )

    updated = _result_with_workflow_final_output(
        result,
        final_output={"output": "mock-transfer-done", "exception": None},
    )

    assert updated.frames[0].completion_reason == "workflow_done"
    assert updated.frames[0].output == {"output": "mock-transfer-done", "exception": None}


def test_native_workflow_tool_records_all_node_outputs_as_protocol_frames(tmp_path: Path) -> None:
    sse_text = "\n".join(
        [
            "event:message",
            'data:{"additional_kwargs":{"node_id":"start","node_title":"开始","node_output":{"step":"start"},"timestamp":"t1"}}',
            "",
            "event:message",
            'data:{"additional_kwargs":{"node_id":"end","node_title":"结束","node_output":{"output":"mock-transfer-done","exception":null},"timestamp":"t2"}}',
            "",
            "event:done",
            "data:[DONE]",
            "",
        ]
    )
    service = IntentRouterHarnessService.from_spec(
        _write_deepagent_harness(tmp_path),
        deepagent_runner=FakeDeepAgentRunner(),
    )
    runner = NativeDeepAgentRunner(
        harness=service.harness,
        workflow_tool=FakeWorkflowCommandTool(sse_text),
    )
    body = {
        "session_id": "s1",
        "txt": "给陈广荣转500元",
        "stream": True,
        "config_variables": [
            {"name": "sessionID", "value": "s1"},
            {"name": "slots_data", "value": '{"payee_name":"陈广荣","amount":"500"}'},
        ],
    }

    runner._workflow_api_call(
        "POST",
        "http://127.0.0.1:9876/agent-api/workflow-agent-1-1b14f16b/chatabc/use_as_tool",
        body,
    )
    workflow_call = runner._pop_workflow_call("s1")
    assert workflow_call is not None

    result = _result_with_workflow_call(
        DeepAgentRunContext(
            request=RouterMessageRequest(sessionId="s1", custID="C0001", txt="给陈广荣转500元"),
            task_state=TaskRuntimeState(),
            thread_id="C0001:s1",
            agent_context="",
            skills={},
            references={},
            workflow_allowed_urls=(),
            config_variables={},
        ),
        workflow_call=workflow_call,
        parsed_result=None,
    )

    assert [frame.completion_reason for frame in result.frames] == [
        "workflow_node_output",
        "workflow_node_output",
        "workflow_done",
    ]
    assert result.frames[0].output == {"step": "start"}
    assert result.frames[1].details == {
        "node_id": "end",
        "node_title": "结束",
        "timestamp": "t2",
        "workflow_url": "http://127.0.0.1:9876/agent-api/workflow-agent-1-1b14f16b/chatabc/use_as_tool",
    }
    assert result.frames[-1].output == {"output": "mock-transfer-done", "exception": None}
    assert result.frames[-1].intent_code == "AG_TRANS"
    assert result.frames[-1].slot_memory == {"payee_name": "陈广荣", "amount": "500"}
    assert result.frames[-1].current_task["status"] == "completed"


def test_native_runner_uses_deepagent_astream_for_tool_calls(tmp_path: Path) -> None:
    sse_text = "\n".join(
        [
            "event:message",
            'data:{"additional_kwargs":{"node_id":"start","node_title":"开始","node_output":{"step":"start"}}}',
            "",
            "event:message",
            'data:{"additional_kwargs":{"node_id":"end","node_title":"结束","node_output":{"output":"mock-transfer-done","exception":null}}}',
            "",
            "event:done",
            "data:[DONE]",
            "",
        ]
    )
    service = IntentRouterHarnessService.from_spec(
        _write_deepagent_harness(tmp_path),
        deepagent_runner=FakeDeepAgentRunner(),
    )
    runner = NativeDeepAgentRunner(
        harness=service.harness,
        workflow_tool=FakeWorkflowCommandTool(sse_text),
    )
    agent = StreamingWorkflowAgent(runner)
    runner._agent = agent
    runner._skill_files_cache = {}

    result = runner.run_message(
        DeepAgentRunContext(
            request=RouterMessageRequest(
                sessionId="s1",
                custID="C0001",
                txt="给陈广荣转500元",
                stream=True,
                executionMode="execute",
            ),
            task_state=TaskRuntimeState(),
            thread_id="C0001:s1",
            agent_context="",
            skills={},
            references={},
            workflow_allowed_urls=(),
            config_variables={},
        )
    )

    assert not agent.invoke_called
    assert agent.stream_kwargs is not None
    assert agent.stream_kwargs["stream_mode"] == ["messages", "updates"]
    assert agent.stream_kwargs["subgraphs"] is True
    assert agent.stream_kwargs["durability"] == "exit"
    assert agent.stream_kwargs["config"]["recursion_limit"] == 40
    assert [frame.completion_reason for frame in result.frames] == [
        "workflow_node_output",
        "workflow_node_output",
        "workflow_done",
    ]
    assert result.frames[-1].output == {"output": "mock-transfer-done", "exception": None}
    assert result.frames[-1].slot_memory == {"payee_name": "陈广荣", "amount": "500"}


def test_deepagent_graph_recursion_limit_leaves_room_for_tool_nodes() -> None:
    assert _deepagent_graph_recursion_limit(20) == 40
    assert _deepagent_graph_recursion_limit(64) == 128


def test_workflow_completion_middleware_ends_before_followup_model_call() -> None:
    messages = pytest.importorskip("langchain_core.messages")
    middleware = _deepagent_runtime_middleware(20)[0]

    result = middleware.before_model(
        {
            "messages": [
                messages.ToolMessage(
                    content='{"events":[]}',
                    tool_call_id="tool_001",
                    name="workflow_api_call",
                )
            ]
        },
        None,
    )

    assert result is not None
    assert result["jump_to"] == "end"
    payload = json.loads(result["messages"][0].content)
    assert payload["frames"][0]["completion_reason"] == "workflow_result_available"


def test_native_runner_returns_workflow_result_when_graph_loops_after_tool_call(tmp_path: Path) -> None:
    sse_text = "\n".join(
        [
            "event:message",
            'data:{"additional_kwargs":{"node_id":"end","node_title":"结束","node_output":{"output":"mock-transfer-done","exception":null}}}',
            "",
            "event:done",
            "data:[DONE]",
            "",
        ]
    )
    service = IntentRouterHarnessService.from_spec(
        _write_deepagent_harness(tmp_path),
        deepagent_runner=FakeDeepAgentRunner(),
    )
    runner = NativeDeepAgentRunner(
        harness=service.harness,
        workflow_tool=FakeWorkflowCommandTool(sse_text),
    )
    runner._agent = WorkflowThenLoopAgent(runner)
    runner._skill_files_cache = {}

    result = runner.run_message(
        DeepAgentRunContext(
            request=RouterMessageRequest(
                sessionId="s1",
                custID="C0001",
                txt="给陈广荣转500元",
                stream=True,
                executionMode="execute",
            ),
            task_state=TaskRuntimeState(),
            thread_id="C0001:s1",
            agent_context="",
            skills={},
            references={},
            workflow_allowed_urls=(),
            config_variables={},
        )
    )

    assert result.frames[-1].status == "completed"
    assert result.frames[-1].completion_reason == "workflow_done"
    assert result.frames[-1].output == {"output": "mock-transfer-done", "exception": None}


def test_workflow_completion_retains_next_planned_task_in_runtime_state(tmp_path: Path) -> None:
    sse_text = "\n".join(
        [
            "event:message",
            'data:{"additional_kwargs":{"node_output":{"output":"mock-transfer-done","exception":null}}}',
            "",
            "event:done",
            "data:[DONE]",
            "",
        ]
    )
    service = IntentRouterHarnessService.from_spec(
        _write_deepagent_harness(tmp_path),
        deepagent_runner=FakeDeepAgentRunner(),
    )
    runner = NativeDeepAgentRunner(
        harness=service.harness,
        workflow_tool=FakeWorkflowCommandTool(sse_text),
    )
    runner._workflow_api_call(
        "POST",
        "http://127.0.0.1:9876/agent-api/workflow-agent-1-1b14f16b/chatabc/use_as_tool",
        {
            "session_id": "s1",
            "config_variables": [
                {"name": "sessionID", "value": "s1"},
                {"name": "slots_data", "value": '{"payee_name":"陈广荣","amount":"500"}'},
            ],
        },
    )
    workflow_call = runner._pop_workflow_call("s1")
    assert workflow_call is not None
    current_task = PlannedTask(
        taskId="task_001",
        intent_code="AG_TRANS",
        status="ready_for_dispatch",
        title="转账",
        slot_memory={"payee_name": "陈广荣", "amount": "500"},
    )
    next_task = PlannedTask(
        taskId="task_002",
        intent_code="AG_BILL",
        status="waiting_user_input",
        title="缴费",
    )

    result = _result_with_workflow_call(
        DeepAgentRunContext(
            request=RouterMessageRequest(sessionId="s1", custID="C0001", txt="先转账再缴费"),
            task_state=TaskRuntimeState(),
            thread_id="C0001:s1",
            agent_context="",
            skills={},
            references={},
            workflow_allowed_urls=(),
            config_variables={},
        ),
        workflow_call=workflow_call,
        parsed_result=DeepAgentRunResult(
            frames=(
                AssistantProtocolFrame(
                    ok=True,
                    status="waiting_assistant_completion",
                    intent_code="AG_TRANS",
                    completion_state=1,
                    completion_reason="assistant_confirmation_required",
                    slot_memory=current_task.slot_memory,
                    task_list=[
                        current_task.model_dump(mode="json"),
                        next_task.model_dump(mode="json"),
                    ],
                    current_task=current_task.model_dump(mode="json"),
                ),
            ),
            task_state=TaskRuntimeState(
                task_list=[current_task, next_task],
                current_task=current_task,
            ),
        ),
    )

    assert result.task_state is not None
    assert [task.taskId for task in result.task_state.task_list] == ["task_002"]
    assert result.task_state.current_task == next_task
    assert [task["taskId"] for task in result.frames[-1].task_list] == ["task_001", "task_002"]


def test_workflow_tool_error_uses_workflow_error_code(tmp_path: Path) -> None:
    service = IntentRouterHarnessService.from_spec(
        _write_deepagent_harness(tmp_path),
        deepagent_runner=FakeDeepAgentRunner(),
    )
    runner = NativeDeepAgentRunner(
        harness=service.harness,
        workflow_tool=FakeWorkflowCommandTool(
            "\n".join(
                [
                    "event:message",
                    "data:not-json",
                    "",
                    "event:done",
                    "data:[DONE]",
                    "",
                ]
            )
        ),
    )

    with pytest.raises(WorkflowApiCallError) as exc_info:
        runner._workflow_api_call(
            "POST",
            "http://127.0.0.1:9876/agent-api/workflow-agent-1-1b14f16b/chatabc/use_as_tool",
            {"session_id": "s1"},
        )

    assert exc_info.value.code == "workflow_error"


def test_loads_json_object_extracts_markdown_json_fence() -> None:
    payload = _loads_json_object(
        '根据规则返回：\n```json\n{"frames":[{"type":"text","output":"done"}]}\n```'
    )

    assert payload == {"frames": [{"type": "text", "output": "done"}]}


def test_native_runner_registers_langchain_workflow_tool(tmp_path: Path) -> None:
    pytest.importorskip("langchain_core.tools")
    service = IntentRouterHarnessService.from_spec(
        _write_deepagent_harness(tmp_path),
        deepagent_runner=FakeDeepAgentRunner(),
    )
    runner = NativeDeepAgentRunner(harness=service.harness)

    tool = runner._workflow_api_call_tool()

    assert tool.name == "workflow_api_call"
    assert set(tool.args_schema.model_fields) == {"method", "url", "body"}


def test_workflow_session_id_can_use_config_variables() -> None:
    assert (
        _workflow_session_id(
            {
                "config_variables": [
                    {"name": "custID", "value": "C0001"},
                    {"name": "sessionID", "value": "s1"},
                ]
            }
        )
        == "s1"
    )


def test_deepagent_system_prompt_enforces_controlled_sequence(tmp_path: Path) -> None:
    service = IntentRouterHarnessService.from_spec(
        _write_deepagent_harness(tmp_path),
        deepagent_runner=FakeDeepAgentRunner(),
    )

    prompt = _deepagent_system_prompt(service.harness)

    assert "先根据用户输入做意图识别" in prompt
    assert "只加载命中的对应 skill" in prompt
    assert "根据 skill/reference 提取参数" in prompt
    assert "触发 LangChain 工具调用 workflow_api_call" in prompt
    assert "必填槽位缺失，不要调用任何工具" in prompt


def test_deepagent_system_prompt_enforces_native_multi_task_planning(tmp_path: Path) -> None:
    service = IntentRouterHarnessService.from_spec(
        _write_deepagent_harness(tmp_path),
        deepagent_runner=FakeDeepAgentRunner(),
    )

    prompt = _deepagent_system_prompt(service.harness)

    assert "多个意图或多个任务" in prompt
    assert "DeepAgent 原生 write_todos" in prompt
    assert "task_list" in prompt
    assert "current_task" in prompt
    assert "不要并行触发多个资金类 workflow_api_call" in prompt
