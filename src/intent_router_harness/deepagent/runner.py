"""NativeDeepAgentRunner — agent execution backed by the upstream deepagents package."""

from __future__ import annotations

import asyncio
import json
import logging
from contextvars import ContextVar
from threading import Lock, Thread
from typing import Any

from pydantic import BaseModel, Field

from intent_router_harness.contracts import (
    AssistantTraceEvent,
    RouterMessageRequest,
    TaskRuntimeState,
)
from intent_router_harness.deepagent.errors import (
    DeepAgentRuntimeError,
    WorkflowApiCallError,
    WorkflowNoNodeOutputError,
    WorkflowSseParseError,
    WorkflowTimeoutError,
    WorkflowUrlNotAllowedError,
)
from intent_router_harness.deepagent.helpers import (
    _agent_context_text,
    _assistant_protocol_frame_from_payload,
    _deepagent_user_payload,
    _last_message_content,
    _loads_json_object,
    _result_with_workflow_call,
    _slot_memory_from_workflow_body,
    _stream_chunk_mode_data,
    _stream_debug_summary,
    _stream_message_from_data,
    _stream_messages_from_update,
    _truncate,
    _workflow_session_id,
)
from intent_router_harness.deepagent.middleware import _deepagent_runtime_middleware
from intent_router_harness.llm import LLMConfigurationError, load_llm_settings
from intent_router_harness.runtime import PromptHarness
from intent_router_harness.tool_runtime import CommandTool, ToolRuntimeError
from intent_router_harness.workflow import WorkflowToolError, WorkflowToolResult, parse_workflow_sse
from intent_router_harness.workflow_hooks import WorkflowHook, WorkflowHookError, run_workflow_hooks

logger = logging.getLogger(__name__)
_LAST_WORKFLOW_CALL: ContextVar[WorkflowCallSnapshot | None] = ContextVar("last_workflow_call", default=None)


class WorkflowApiCallInput(BaseModel):
    method: str = Field(default="POST", description="HTTP method: GET, POST, PUT, DELETE.")
    url: str = Field(description="Full workflow use_as_tool URL.")
    body: dict[str, Any] = Field(default_factory=dict, description="Request JSON body.")


class WorkflowCallSnapshot:
    __slots__ = ("method", "url", "body", "result", "intent_code", "skill_name", "slot_memory")

    def __init__(
        self,
        *,
        method: str,
        url: str,
        body: dict[str, Any],
        result: WorkflowToolResult,
        intent_code: str | None = None,
        skill_name: str | None = None,
        slot_memory: dict[str, Any] | None = None,
    ) -> None:
        self.method = method
        self.url = url
        self.body = body
        self.result = result
        self.intent_code = intent_code
        self.skill_name = skill_name
        self.slot_memory = slot_memory or {}





class NativeDeepAgentRunner:
    """DeepAgent runner that uses the upstream deepagents package when installed."""

    def __init__(
        self,
        *,
        harness: PromptHarness,
        workflow_tool: CommandTool | None = None,
        workflow_hooks: tuple[WorkflowHook, ...] = (),
    ) -> None:
        self.harness = harness
        self.workflow_tool = workflow_tool
        self.workflow_hooks = workflow_hooks
        self._agent: Any | None = None
        self._agent_lock = Lock()
        self._skill_files_cache: dict[str, Any] | None = None
        self._workflow_calls_lock = Lock()
        self._workflow_calls: dict[str, WorkflowCallSnapshot] = {}

    def run_message(self, context: Any) -> Any:

        token = _LAST_WORKFLOW_CALL.set(None)
        try:
            response: Any | None = None
            try:
                response = _run_async_blocking(self._run_agent_stream(context))
            except DeepAgentRuntimeError:
                workflow_call = self._completed_workflow_call(context.request.sessionId)
                if workflow_call is not None:
                    return _result_with_workflow_call(
                        context,
                        workflow_call=workflow_call,
                        parsed_result=None,
                    )
                raise
            except Exception as exc:
                workflow_call = self._completed_workflow_call(context.request.sessionId)
                if workflow_call is not None:
                    return _result_with_workflow_call(
                        context,
                        workflow_call=workflow_call,
                        parsed_result=None,
                    )
                raise DeepAgentRuntimeError(f"deepagent invocation failed: {exc}") from exc
            workflow_call = self._completed_workflow_call(context.request.sessionId)
            if workflow_call is not None:
                parsed_result = _try_parse_deepagent_response(context.request, response)
                return _result_with_workflow_call(
                    context,
                    workflow_call=workflow_call,
                    parsed_result=parsed_result,
                )
            return _parse_deepagent_response(context.request, response)
        finally:
            _LAST_WORKFLOW_CALL.reset(token)

    async def _run_agent_stream(self, context: Any) -> dict[str, Any]:
        """Run DeepAgent with the same stream shape used by the official CLI."""
        agent = self._load_agent()
        input_payload: dict[str, Any] = {
            "messages": [{"role": "user", "content": _deepagent_user_payload(context)}],
        }
        input_payload["files"] = self._deepagent_skill_files()
        config = {
            "configurable": {"thread_id": context.thread_id},
            "recursion_limit": _deepagent_graph_recursion_limit(
                self.harness.spec.deepagent.max_iterations
            ),
        }
        messages: list[Any] = []
        updates: list[Any] = []
        try:
            async for chunk in _agent_astream(
                agent,
                input_payload,
                config=config,
                stream_mode=["messages", "updates"],
                subgraphs=True,
                durability="exit",
            ):
                mode, data = _stream_chunk_mode_data(chunk)
                if mode == "messages":
                    message = _stream_message_from_data(data)
                    if message is not None:
                        messages.append(message)
                elif mode == "updates":
                    updates.append(data)
                    messages.extend(_stream_messages_from_update(data))
        except Exception as exc:
            logger.debug("deepagent partial stream before failure: %s", _stream_debug_summary(messages))
            raise DeepAgentRuntimeError(f"deepagent invocation failed: {exc}") from exc
        return {"messages": messages, "updates": updates}

    def _load_agent(self) -> Any:
        if self._agent is not None:
            return self._agent
        with self._agent_lock:
            if self._agent is not None:
                return self._agent
            model = _resolve_deepagent_model(self.harness.spec.deepagent.model)
            try:
                from deepagents import create_deep_agent
                from langgraph.checkpoint.memory import MemorySaver
            except ImportError as exc:
                raise DeepAgentRuntimeError(
                    "deepagents is not installed; install the 'deepagent' extra or switch agent_runtime to 'classic'"
                ) from exc
            self._agent = create_deep_agent(
                model=model,
                tools=[self._workflow_api_call_tool()],
                system_prompt=_deepagent_system_prompt(self.harness),
                middleware=_deepagent_runtime_middleware(
                    self.harness.spec.deepagent.max_iterations,
                    harness=self.harness,
                    workflow_executor=self._workflow_api_call,
                ),
                skills=list(self.harness.spec.deepagent.skills),
                memory=list(self.harness.spec.deepagent.memory),
                checkpointer=MemorySaver(),
            )
            return self._agent

    def _workflow_api_call_tool(self) -> Any:
        try:
            from langchain_core.tools import StructuredTool
        except ImportError as exc:
            raise DeepAgentRuntimeError(
                "langchain-core StructuredTool is required to register workflow_api_call"
            ) from exc
        return StructuredTool.from_function(
            func=self._workflow_api_call,
            name="workflow_api_call",
            description=(
                "Call one allowed workflow use_as_tool SSE endpoint. Use this only when "
                "executionMode is execute and all required slots are available. The url "
                "must be the full absolute http(s) URL from the skill reference. Do not "
                "include or control headers."
            ),
            args_schema=WorkflowApiCallInput,
        )

    def _workflow_api_call(self, method: str, url: str, body: dict[str, Any]) -> dict[str, Any]:
        """Call an allowed workflow use_as_tool endpoint and return parsed node outputs."""
        if self.workflow_tool is None:
            raise DeepAgentRuntimeError("workflow-api-call tool is not configured")
        allowed_urls = list(self.harness.spec.workflow.allowed_urls)
        if url not in allowed_urls:
            raise WorkflowUrlNotAllowedError(url)
        payload = {
            "request": {"method": method, "url": url, "body": body},
            "allowed_urls": allowed_urls,
        }
        try:
            run_workflow_hooks(self.workflow_hooks, event="before_workflow_tool_call", payload=payload)
        except WorkflowHookError as exc:
            raise WorkflowApiCallError(
                f"before_workflow_tool_call hook rejected: {exc}",
                code="workflow_hook_rejected",
            ) from exc
        timeout_seconds = 60.0
        try:
            result = self.workflow_tool.run(
                {
                    "method": method,
                    "url": url,
                    "body": body,
                },
                timeout_seconds=timeout_seconds,
            )
        except ToolRuntimeError as exc:
            error_msg = str(exc)
            if "timed out" in error_msg.lower() or "timeout" in error_msg.lower():
                raise WorkflowTimeoutError(url, timeout_seconds) from exc
            raise WorkflowApiCallError(
                f"workflow tool error: {exc}", code="workflow_tool_error"
            ) from exc
        raw_sse = str(result.get("text") or "")
        try:
            workflow_result = parse_workflow_sse(raw_sse, require_node_output=True)
        except WorkflowToolError as exc:
            error_msg = str(exc)
            if "node_output" in error_msg:
                raise WorkflowNoNodeOutputError(url) from exc
            raise WorkflowSseParseError(url, detail=str(exc)) from exc
        try:
            run_workflow_hooks(self.workflow_hooks, event="after_workflow_tool_call", payload={
                "request": {"method": method, "url": url, "body": body},
                "result": {
                    "event_count": len(workflow_result.events),
                    "final_output": workflow_result.final_output,
                },
            })
        except WorkflowHookError as exc:
            logger.warning("after_workflow_tool_call hook error (non-fatal): %s", exc)
        snapshot = WorkflowCallSnapshot(
            method=method,
            url=url,
            body=body,
            result=workflow_result,
            intent_code=self._intent_code_for_workflow_url(url),
            skill_name=self._skill_name_for_workflow_url(url),
            slot_memory=_slot_memory_from_workflow_body(body),
        )
        _LAST_WORKFLOW_CALL.set(snapshot)
        self._record_workflow_call(body, snapshot)
        return {
            "events": [
                {
                    "node_id": event.node_id,
                    "node_title": event.node_title,
                    "timestamp": event.timestamp,
                    "node_output": event.node_output,
                }
                for event in workflow_result.events
            ],
            "final_output": workflow_result.final_output,
        }

    def _record_workflow_call(self, body: dict[str, Any], snapshot: WorkflowCallSnapshot) -> None:
        session_id = _workflow_session_id(body)
        if not session_id:
            return
        with self._workflow_calls_lock:
            self._workflow_calls[session_id] = snapshot

    def _pop_workflow_call(self, session_id: str) -> WorkflowCallSnapshot | None:
        with self._workflow_calls_lock:
            return self._workflow_calls.pop(session_id, None)

    def _completed_workflow_call(self, session_id: str) -> WorkflowCallSnapshot | None:
        workflow_call = self._pop_workflow_call(session_id)
        if workflow_call is None:
            workflow_call = _LAST_WORKFLOW_CALL.get()
        return workflow_call

    def _intent_code_for_workflow_url(self, url: str) -> str | None:
        skill_name = self._skill_name_for_workflow_url(url)
        if not skill_name:
            return None
        skill = self.harness.skills.get(skill_name)
        if skill is None or not skill.intent_codes:
            return None
        return skill.intent_codes[0]

    def _skill_name_for_workflow_url(self, url: str) -> str | None:
        for skill_name in self.harness.skills.names():
            skill = self.harness.skills.get(skill_name)
            if skill is None:
                continue
            for reference in skill.references:
                if url in reference.body:
                    return skill.name
        return None

    def _deepagent_skill_files(self) -> dict[str, Any]:
        if self._skill_files_cache is not None:
            return self._skill_files_cache
        try:
            from deepagents.backends.utils import create_file_data
        except ImportError as exc:
            raise DeepAgentRuntimeError(
                "deepagents filesystem helpers are unavailable; install a compatible deepagents package"
            ) from exc

        files: dict[str, Any] = {}
        for skill_name in self.harness.skills.names():
            skill = self.harness.skills.get(skill_name)
            if skill is None:
                continue
            skill_dir = skill.path.parent
            virtual_dir = f"/skills/{skill_dir.name}"
            for path in sorted(item for item in skill_dir.rglob("*") if item.is_file()):
                if any(part.startswith(".") for part in path.relative_to(skill_dir).parts):
                    continue
                relative_path = path.relative_to(skill_dir).as_posix()
                files[f"{virtual_dir}/{relative_path}"] = create_file_data(
                    path.read_text(encoding="utf-8")
                )
        self._skill_files_cache = files
        return files


# ---------------------------------------------------------------------------
# Module-level helpers used by NativeDeepAgentRunner
# ---------------------------------------------------------------------------


def _parse_deepagent_response(request: RouterMessageRequest, response: Any) -> Any:
    from intent_router_harness.deepagent.service import DeepAgentRunResult

    raw_content = _last_message_content(response)
    try:
        payload = _loads_json_object(raw_content)
    except json.JSONDecodeError as exc:
        raise DeepAgentRuntimeError(
            f"deepagent final response is not JSON: {exc}; content={_truncate(raw_content, 300)!r}"
        ) from exc
    if not isinstance(payload, dict):
        raise DeepAgentRuntimeError("deepagent final response must be a JSON object")
    raw_frames = payload.get("frames") or payload.get("assistant_protocol_frames")
    if isinstance(raw_frames, list) and raw_frames:
        frames = tuple(_assistant_protocol_frame_from_payload(frame) for frame in raw_frames)
    elif "status" not in payload and _assistant_turn_output(payload) is not None:
        frames = (_assistant_protocol_frame_from_payload(payload),)
    else:
        frame_payload = payload.get("frame") if isinstance(payload.get("frame"), dict) else payload
        frames = (_assistant_protocol_frame_from_payload(frame_payload),)
    trace_event = AssistantTraceEvent(
        stage="deepagent_run_completed",
        title="DeepAgent\u8fd0\u884c\u5b8c\u6210",
        summary=f"session={request.sessionId} frames={len(frames)}",
        data={"thread_id": f"{request.custID}:{request.sessionId}"},
    )
    task_state_payload = payload.get("task_state")
    task_state = (
        TaskRuntimeState.model_validate(task_state_payload)
        if isinstance(task_state_payload, dict)
        else None
    )
    return DeepAgentRunResult(frames=frames, trace_events=(trace_event,), task_state=task_state)


def _assistant_turn_output(payload: dict[str, Any]) -> Any:
    if "output" in payload:
        return payload["output"]
    content = payload.get("content")
    if isinstance(content, dict) and "output" in content:
        return content["output"]
    return None


def _try_parse_deepagent_response(
    request: RouterMessageRequest,
    response: Any,
) -> Any | None:
    try:
        return _parse_deepagent_response(request, response)
    except Exception:
        return None


def _resolve_deepagent_model(configured_model: str | None) -> Any:
    if configured_model:
        return configured_model
    try:
        settings = load_llm_settings()
    except LLMConfigurationError as exc:
        raise DeepAgentRuntimeError(
            "deepagent.model is not configured and ROUTER_LLM_API_BASE_URL, "
            "ROUTER_LLM_API_KEY, ROUTER_LLM_MODEL are not available"
        ) from exc
    try:
        from langchain_openai import ChatOpenAI
    except ImportError as exc:
        raise DeepAgentRuntimeError(
            "langchain-openai is required to use ROUTER_LLM_* with agent_runtime='deepagent'"
        ) from exc

    extra_body: dict[str, Any] = {}
    if settings.enable_thinking is not None:
        extra_body["enable_thinking"] = settings.enable_thinking
    if settings.thinking_budget is not None:
        extra_body["thinking_budget"] = settings.thinking_budget
    return ChatOpenAI(
        model=settings.model,
        api_key=settings.api_key,
        base_url=settings.base_url,
        temperature=settings.temperature,
        timeout=settings.timeout_seconds,
        extra_body=extra_body or None,
        use_responses_api=False,
    )


def _deepagent_system_prompt(harness: PromptHarness) -> str:
    return "\n\n".join(
        part
        for part in [
            _agent_context_text(harness),
            "\u4f60\u662f DeepAgent \u7248\u672c\u7684 intent router harness\u3002\u5fc5\u987b\u6309\u53d7\u63a7\u987a\u5e8f\u6267\u884c\uff0c\u4e0d\u8981\u81ea\u7531\u6539\u5199\u6d41\u7a0b\u3002",
            "\u6267\u884c\u987a\u5e8f\u56fa\u5b9a\u4e3a\uff1a1) \u5148\u6839\u636e\u7528\u6237\u8f93\u5165\u505a\u610f\u56fe\u8bc6\u522b\uff1b2) \u5355\u610f\u56fe\u53ea\u52a0\u8f7d\u547d\u4e2d\u7684\u5bf9\u5e94 skill \u548c\u5fc5\u8981 reference\uff0c\u591a\u610f\u56fe\u52a0\u8f7d\u6bcf\u4e2a\u547d\u4e2d\u4efb\u52a1\u5bf9\u5e94\u7684 skill/reference\uff1b3) \u6839\u636e skill/reference \u63d0\u53d6\u53c2\u6570\u548c\u8865\u9f50\u69fd\u4f4d\uff1b4) \u69fd\u4f4d\u9f50\u5168\u4e14 executionMode=execute \u65f6\u89e6\u53d1 LangChain \u5de5\u5177\u8c03\u7528 workflow_api_call\uff1b5) \u8fd4\u56de\u5de5\u5177\u7ed3\u679c\u5bf9\u5e94\u7684 Assistant Protocol\u3002",
            "\u5982\u679c\u7528\u6237\u4e00\u6b21\u8868\u8fbe\u591a\u4e2a\u610f\u56fe\u6216\u591a\u4e2a\u4efb\u52a1\uff0c\u5fc5\u987b\u4f7f\u7528 DeepAgent \u539f\u751f write_todos \u505a\u4efb\u52a1\u89c4\u5212\u3002\u4efb\u52a1\u987a\u5e8f\u5fc5\u987b\u4e0e\u7528\u6237\u8868\u8fbe\u987a\u5e8f\u4e00\u81f4\uff0c\u8f93\u51fa Assistant Protocol \u65f6\u5fc5\u987b\u5305\u542b task_list\uff0c\u5e76\u7528 current_task \u8868\u793a\u5f53\u524d\u6b63\u5728\u63a8\u8fdb\u7684\u4efb\u52a1\u3002",
            "\u591a\u4efb\u52a1\u573a\u666f\u5fc5\u987b\u4e32\u884c\u63a8\u8fdb\uff1a\u4e00\u6b21\u53ea\u63a8\u8fdb\u4e00\u4e2a current_task\u3002\u4e0d\u8981\u5e76\u884c\u89e6\u53d1\u591a\u4e2a\u8d44\u91d1\u7c7b workflow_api_call\u3002\u5f53\u524d\u4efb\u52a1\u7f3a\u69fd\u65f6\u5148\u8ffd\u95ee\u5f53\u524d\u4efb\u52a1\uff1b\u5f53\u524d\u4efb\u52a1\u5b8c\u6210\u540e\u518d\u63a8\u8fdb\u4e0b\u4e00\u4e2a\u4efb\u52a1\u3002",
            "\u5982\u679c\u610f\u56fe\u4e0d\u660e\u786e\u3001\u6ca1\u6709\u547d\u4e2d skill\u3001\u6216\u5fc5\u586b\u69fd\u4f4d\u7f3a\u5931\uff0c\u4e0d\u8981\u8c03\u7528\u4efb\u4f55\u5de5\u5177\uff0c\u5e94\u8fd4\u56de waiting_user_input \u6216 failed/unsupported \u7684 Assistant Protocol JSON\u3002",
            "\u5fc5\u987b\u4fdd\u6301\u591a\u7528\u6237\u4f1a\u8bdd\u9694\u79bb\uff1b\u53ea\u80fd\u5904\u7406\u5f53\u524d\u8bf7\u6c42\u7ed9\u51fa\u7684 sessionId/custID\u3002",
            "\u9700\u8981\u6267\u884c\u5b50\u5de5\u4f5c\u6d41\u65f6\uff0c\u5fc5\u987b\u89e6\u53d1 LangChain \u5de5\u5177\u8c03\u7528 workflow_api_call(method, url, body)\u3002\u4e0d\u8981\u628a tool_use\u3001workflow_api_call \u6216 workflow_request \u5199\u6210\u666e\u901a JSON/Markdown \u6587\u672c\u3002",
            "workflow_api_call \u7684 url \u5fc5\u987b\u662f reference \u4e2d\u7ed9\u51fa\u7684\u5b8c\u6574 http(s) \u5730\u5740\uff0c\u4e0d\u8981\u8f93\u51fa\u76f8\u5bf9\u8def\u5f84\uff0c\u4e0d\u8981\u8ba9\u6a21\u578b\u63a7\u5236 headers\u3002",
            "\u5de5\u5177\u8c03\u7528\u5b8c\u6210\u540e\uff0c\u5fc5\u987b\u6700\u7ec8\u53ea\u8fd4\u56de Assistant Protocol JSON\uff0c\u4e0d\u8981\u8fd4\u56de Markdown\u3002\u53ef\u4ee5\u8fd4\u56de {\"frames\":[...],\"task_state\":{...}}\u3002",
            "\u5982\u679c workflow_api_call \u8fd4\u56de final_output\uff0c\u5fc5\u987b\u628a\u5b8c\u6574 final_output \u539f\u6837\u4f5c\u4e3a AssistantProtocolFrame.output\uff1b\u4e0d\u8981\u63d0\u53d6 final_output.output \u5b57\u6bb5\u3002",
        ]
        if part.strip()
    )


def _run_async_blocking(coro: Any) -> Any:
    """Run an async DeepAgent call from the harness' synchronous service boundary."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    result: list[Any] = []
    errors: list[BaseException] = []

    def run_in_thread() -> None:
        try:
            result.append(asyncio.run(coro))
        except BaseException as exc:  # pragma: no cover - propagated below
            errors.append(exc)

    thread = Thread(target=run_in_thread)
    thread.start()
    thread.join()
    if errors:
        raise errors[0]
    return result[0] if result else None


def _deepagent_graph_recursion_limit(max_iterations: int) -> int:
    """Allow tool-node graph steps while model calls are capped by middleware."""
    return max(40, max_iterations * 2)


async def _agent_astream(
    agent: Any,
    input_payload: dict[str, Any],
    *,
    config: dict[str, Any],
    stream_mode: list[str],
    subgraphs: bool,
    durability: str,
):
    """Call agent.astream with compatibility for nearby DeepAgent/LangGraph versions."""
    attempts = (
        {"stream_mode": stream_mode, "subgraphs": subgraphs, "durability": durability},
        {"stream_mode": stream_mode, "subgraphs": subgraphs},
        {"stream_mode": stream_mode},
    )
    last_error: TypeError | None = None
    for kwargs in attempts:
        try:
            stream = agent.astream(input_payload, config=config, **kwargs)
        except TypeError as exc:
            last_error = exc
            continue
        async for chunk in stream:
            yield chunk
        return
    if last_error is not None:
        raise last_error
