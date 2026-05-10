from __future__ import annotations

import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import json
import logging
import re
from threading import Lock, Thread
from typing import Any, Protocol

from pydantic import BaseModel, Field

from intent_router_harness.contracts import (
    AssistantProtocolFrame,
    AssistantServiceResult,
    AssistantTraceEvent,
    RouterMessageRequest,
    TaskCompletionRequest,
    TaskRuntimeState,
)
from intent_router_harness.llm import LLMConfigurationError, load_llm_settings
from intent_router_harness.runtime import PromptHarness
from intent_router_harness.session_store import InMemorySessionStore, SessionNotFoundError
from intent_router_harness.tool_runtime import CommandTool, ToolRuntimeError
from intent_router_harness.trace import emit_trace
from intent_router_harness.workflow import WorkflowToolError, parse_workflow_sse
from intent_router_harness.workflow import WorkflowToolEvent, WorkflowToolResult
from intent_router_harness.workflow_hooks import WorkflowHook, WorkflowHookError, run_workflow_hooks

logger = logging.getLogger(__name__)
_LAST_WORKFLOW_CALL: ContextVar[WorkflowCallSnapshot | None] = ContextVar("last_workflow_call", default=None)


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


class WorkflowApiCallInput(BaseModel):
    """Input schema for the LangChain workflow_api_call tool."""

    method: str = Field(description="HTTP method, usually POST.")
    url: str = Field(description="Full absolute http(s) workflow endpoint URL from the skill reference.")
    body: dict[str, Any] = Field(description="Workflow JSON request body.")


@dataclass(frozen=True, slots=True)
class DeepAgentRunContext:
    """Context passed from the harness into one DeepAgent run."""

    request: RouterMessageRequest
    task_state: TaskRuntimeState
    thread_id: str
    agent_context: str
    skills: dict[str, str]
    references: dict[str, dict[str, str]]
    workflow_allowed_urls: tuple[str, ...]
    config_variables: dict[str, Any]


@dataclass(frozen=True, slots=True)
class DeepAgentRunResult:
    """Structured result returned by a DeepAgent runner."""

    frames: tuple[AssistantProtocolFrame, ...]
    trace_events: tuple[AssistantTraceEvent, ...] = ()
    task_state: TaskRuntimeState | None = None


@dataclass(frozen=True, slots=True)
class WorkflowCallSnapshot:
    """Workflow call metadata captured from one LangChain tool invocation."""

    method: str
    url: str
    body: dict[str, Any]
    result: WorkflowToolResult
    intent_code: str | None = None
    skill_name: str | None = None
    slot_memory: dict[str, Any] | None = None


class DeepAgentRunner(Protocol):
    """Boundary for native DeepAgent execution."""

    def run_message(self, context: DeepAgentRunContext) -> DeepAgentRunResult:
        """Run one user message through a DeepAgent loop."""


class SessionRunLockStore:
    """Per-user, per-session non-reentrant run locks."""

    def __init__(self) -> None:
        self._guard = Lock()
        self._locks: dict[tuple[str, str], Lock] = {}

    @contextmanager
    def acquire(self, *, user_id: str, session_id: str):
        key = (user_id, session_id)
        with self._guard:
            lock = self._locks.setdefault(key, Lock())
        if not lock.acquire(blocking=False):
            raise SessionRunInProgressError(f"session {session_id!r} already has an active agent run")
        try:
            yield
        finally:
            lock.release()


class DeepAgentAssistantProtocolService:
    """Assistant protocol adapter backed by one DeepAgent loop."""

    def __init__(
        self,
        *,
        harness: PromptHarness,
        runner: DeepAgentRunner,
        sessions: InMemorySessionStore | None = None,
        run_locks: SessionRunLockStore | None = None,
    ) -> None:
        self.harness = harness
        self.runner = runner
        self.sessions = sessions or InMemorySessionStore()
        self.run_locks = run_locks or SessionRunLockStore()

    def handle_message(self, request: RouterMessageRequest) -> AssistantServiceResult:
        trace_events: list[AssistantTraceEvent] = []
        try:
            with self.run_locks.acquire(user_id=request.custID, session_id=request.sessionId):
                _append_trace(
                    trace_events,
                    AssistantTraceEvent(
                        stage="request_received",
                        title="请求进入Router",
                        summary=f"session={request.sessionId}，executionMode={request.executionMode}",
                        data={
                            "session_id": request.sessionId,
                            "stream": request.stream,
                            "execution_mode": request.executionMode,
                            "text": request.txt,
                            "user_binding_id": request.custID,
                            "agent_runtime": "deepagent",
                        },
                    ),
                )
                loaded = self.sessions.load(request.sessionId, user_binding_id=request.custID)
                _append_trace(
                    trace_events,
                    AssistantTraceEvent(
                        stage="session_loaded",
                        title="Session生命周期读取",
                        summary=(
                            f"expired={loaded.expired}，"
                            f"expires_at={loaded.session.expires_at.isoformat() if loaded.session.expires_at else ''}"
                        ),
                        data={
                            "session_id": loaded.session.session_id,
                            "user_binding_id": loaded.session.user_binding_id,
                            "expired": loaded.expired,
                            "user_bound": loaded.user_bound,
                            "expires_at": loaded.session.expires_at.isoformat()
                            if loaded.session.expires_at
                            else None,
                        },
                    ),
                )
                _append_trace(
                    trace_events,
                    AssistantTraceEvent(
                        stage="task_runtime_loaded",
                        title="任务运行态读取",
                        summary=f"task_count={len(loaded.task_state.task_list)}",
                        data={
                            "slot_memory": loaded.task_state.slot_memory,
                            "current_task": loaded.task_state.current_task.model_dump(mode="json")
                            if loaded.task_state.current_task
                            else None,
                            "task_list": [
                                task.model_dump(mode="json")
                                for task in loaded.task_state.task_list
                            ],
                        },
                    ),
                )
                _append_trace(
                    trace_events,
                    AssistantTraceEvent(
                        stage="deepagent_runtime_start",
                        title="DeepAgent运行开始",
                        summary=f"thread_id={_thread_id(request)}",
                        data={
                            "thread_id": _thread_id(request),
                            "skill_count": len(self.harness.skills.names()),
                        },
                    ),
                )
                context = DeepAgentRunContext(
                    request=request,
                    task_state=loaded.task_state,
                    thread_id=_thread_id(request),
                    agent_context=_agent_context_text(self.harness),
                    skills=_skill_bodies(self.harness),
                    references=_reference_bodies(self.harness),
                    workflow_allowed_urls=tuple(self.harness.spec.workflow.allowed_urls),
                    config_variables=_config_variable_map(request),
                )
                result = self.runner.run_message(context)
                if result.task_state is not None:
                    self.sessions.save_task_state(request.sessionId, result.task_state)
                for event in result.trace_events:
                    _append_trace(trace_events, event)
                _append_trace(
                    trace_events,
                    AssistantTraceEvent(
                        stage="assistant_protocol_frames",
                        title="SSE业务帧生成",
                        summary=f"生成 {len(result.frames)} 个 message frame，最终状态 {result.frames[-1].status}",
                        data={
                            "frame_count": len(result.frames),
                            "frames": [frame.protocol_dump() for frame in result.frames],
                        },
                    ),
                )
                return AssistantServiceResult(
                    frames=list(result.frames),
                    trace_events=trace_events,
                )
        except SessionRunInProgressError as exc:
            return _single_failed_result(
                request,
                code="session_run_in_progress",
                message=str(exc),
                trace_events=trace_events,
            )
        except DeepAgentRuntimeError as exc:
            _append_trace(
                trace_events,
                AssistantTraceEvent(
                    stage="deepagent_runtime_failed",
                    title="DeepAgent运行失败",
                    summary=f"{exc.code}: {exc}",
                    data={
                        "session_id": request.sessionId,
                        "thread_id": _thread_id(request),
                        "error_code": exc.code,
                    },
                ),
            )
            return _single_failed_result(
                request,
                code=exc.code,
                message=str(exc),
                trace_events=trace_events,
            )

    def handle_task_completion(self, request: TaskCompletionRequest) -> AssistantServiceResult:
        try:
            self.sessions.load_existing(request.sessionId, user_binding_id=request.custID)
        except SessionNotFoundError as exc:
            return _single_failed_result(
                request,
                code="session_not_found",
                message=str(exc),
            )
        status = "completed" if request.completionSignal == 1 else "failed"
        return AssistantServiceResult(
            frames=[
                AssistantProtocolFrame(
                    ok=request.completionSignal == 1,
                    status=status,
                    completion_state=2,
                    completion_reason="task_completion_received",
                    output={"taskId": request.taskId, "completionSignal": request.completionSignal},
                )
            ]
        )


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

    def run_message(self, context: DeepAgentRunContext) -> DeepAgentRunResult:
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

    async def _run_agent_stream(self, context: DeepAgentRunContext) -> dict[str, Any]:
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


def _append_trace(trace_events: list[AssistantTraceEvent], event: AssistantTraceEvent) -> None:
    trace_events.append(event)
    emit_trace(event)


def _deepagent_runtime_middleware(
    max_iterations: int,
    *,
    harness: PromptHarness | None = None,
    workflow_executor: Any | None = None,
) -> list[Any]:
    """Middleware that turns banking runtime invariants into hard loop controls."""
    try:
        from langchain.agents.middleware import AgentMiddleware, ModelCallLimitMiddleware
        from langchain.agents.middleware.types import hook_config
        from langchain_core.messages import AIMessage, SystemMessage, ToolMessage
    except ImportError as exc:
        raise DeepAgentRuntimeError(
            "langchain middleware is required for DeepAgent runtime stability controls"
        ) from exc

    class HarnessContextMiddleware(AgentMiddleware):
        @property
        def name(self) -> str:
            return "HarnessContextMiddleware"

        def wrap_model_call(self, request: Any, handler: Any) -> Any:
            return handler(self._with_harness_context(request))

        async def awrap_model_call(self, request: Any, handler: Any) -> Any:
            return await handler(self._with_harness_context(request))

        def _with_harness_context(self, request: Any) -> Any:
            if harness is None:
                return request
            context_block = _harness_context_block(harness)
            if not context_block:
                return request
            existing = request.system_message
            existing_text = existing.text if existing is not None else ""
            if "## Intent Router Harness Loaded Context" in existing_text:
                return request
            system_message = SystemMessage(
                content=f"{existing_text}\n\n{context_block}" if existing_text else context_block
            )
            return request.override(system_message=system_message)

    class HarnessSkillFileMiddleware(AgentMiddleware):
        @property
        def name(self) -> str:
            return "HarnessSkillFileMiddleware"

        def wrap_tool_call(self, request: Any, handler: Any) -> Any:
            message = self._maybe_read_harness_file(request)
            if message is not None:
                return message
            return handler(request)

        async def awrap_tool_call(self, request: Any, handler: Any) -> Any:
            message = self._maybe_read_harness_file(request)
            if message is not None:
                return message
            return await handler(request)

        def _maybe_read_harness_file(self, request: Any) -> Any | None:
            if harness is None or _tool_call_name(request) != "read_file":
                return None
            args = _tool_call_args(request)
            raw_path = str(
                args.get("file_path")
                or args.get("path")
                or args.get("filename")
                or args.get("name")
                or ""
            ).strip()
            files = _harness_virtual_files(harness)
            normalized = _normalize_virtual_file_path(raw_path)
            if normalized in files:
                return ToolMessage(
                    content=files[normalized],
                    tool_call_id=_tool_call_id(request),
                    name="read_file",
                )
            if normalized.startswith("/skills"):
                return ToolMessage(
                    content=(
                        "The requested harness skill file was not found. "
                        "Use one of these already-loaded paths instead:\n"
                        + "\n".join(f"- {path}" for path in sorted(files))
                    ),
                    tool_call_id=_tool_call_id(request),
                    name="read_file",
                )
            return None

    class WorkflowRequestMiddleware(AgentMiddleware):
        @property
        def name(self) -> str:
            return "WorkflowRequestMiddleware"

        def after_model(self, state: dict[str, Any], runtime: Any) -> dict[str, Any] | None:
            del runtime
            return self._maybe_execute_textual_workflow_request(state)

        async def aafter_model(self, state: dict[str, Any], runtime: Any) -> dict[str, Any] | None:
            del runtime
            return self._maybe_execute_textual_workflow_request(state)

        def _maybe_execute_textual_workflow_request(self, state: dict[str, Any]) -> dict[str, Any] | None:
            if workflow_executor is None:
                return None
            messages = state.get("messages", [])
            if any(_is_workflow_tool_message(message, ToolMessage) for message in messages):
                return None
            workflow_request = _workflow_request_from_messages(messages)
            if workflow_request is None:
                return None
            try:
                result = workflow_executor(
                    str(workflow_request.get("method") or "POST"),
                    str(workflow_request.get("url") or ""),
                    workflow_request.get("body") if isinstance(workflow_request.get("body"), dict) else {},
                )
            except Exception as exc:
                return {
                    "messages": [
                        ToolMessage(
                            content=json.dumps(
                                {"error": {"code": "workflow_error", "message": str(exc)}},
                                ensure_ascii=False,
                            ),
                            tool_call_id="workflow_request_middleware",
                            name="workflow_api_call",
                        )
                    ]
                }
            return {
                "messages": [
                    ToolMessage(
                        content=json.dumps(result, ensure_ascii=False),
                        tool_call_id="workflow_request_middleware",
                        name="workflow_api_call",
                    )
                ]
            }

    class WorkflowCompletionMiddleware(AgentMiddleware):
        @property
        def name(self) -> str:
            return "WorkflowCompletionMiddleware"

        @hook_config(can_jump_to=["end"])
        def before_model(self, state: dict[str, Any], runtime: Any) -> dict[str, Any] | None:
            del runtime
            return self._maybe_finish_after_workflow(state)

        @hook_config(can_jump_to=["end"])
        async def abefore_model(self, state: dict[str, Any], runtime: Any) -> dict[str, Any] | None:
            del runtime
            return self._maybe_finish_after_workflow(state)

        def _maybe_finish_after_workflow(self, state: dict[str, Any]) -> dict[str, Any] | None:
            messages = state.get("messages", [])
            if not any(_is_workflow_tool_message(message, ToolMessage) for message in messages):
                return None
            return {
                "jump_to": "end",
                "messages": [
                    AIMessage(
                        content=json.dumps(
                            {
                                "frames": [
                                    {
                                        "ok": True,
                                        "status": "waiting_assistant_completion",
                                        "completion_state": 1,
                                        "completion_reason": "workflow_result_available",
                                        "output": {},
                                    }
                                ]
                            },
                            ensure_ascii=False,
                        )
                    )
                ],
            }

    middleware: list[Any] = []
    if harness is not None:
        middleware.append(HarnessContextMiddleware())
        middleware.append(HarnessSkillFileMiddleware())
    if workflow_executor is not None:
        middleware.append(WorkflowRequestMiddleware())
    middleware.extend(
        [
            WorkflowCompletionMiddleware(),
            ModelCallLimitMiddleware(
                run_limit=max(4, min(max_iterations, 8)),
                exit_behavior="error",
            ),
        ]
    )
    return middleware


def _is_workflow_tool_message(message: Any, tool_message_type: type[Any] | None = None) -> bool:
    if tool_message_type is not None and not isinstance(message, tool_message_type):
        return False
    return str(getattr(message, "name", "") or "") == "workflow_api_call"


def _tool_call_name(request: Any) -> str:
    tool_call = getattr(request, "tool_call", {}) or {}
    if isinstance(tool_call, dict):
        return str(tool_call.get("name") or "")
    return ""


def _tool_call_id(request: Any) -> str:
    tool_call = getattr(request, "tool_call", {}) or {}
    if isinstance(tool_call, dict):
        return str(tool_call.get("id") or "harness_tool_call")
    return "harness_tool_call"


def _tool_call_args(request: Any) -> dict[str, Any]:
    tool_call = getattr(request, "tool_call", {}) or {}
    if isinstance(tool_call, dict) and isinstance(tool_call.get("args"), dict):
        return dict(tool_call["args"])
    return {}


def _harness_virtual_files(harness: PromptHarness) -> dict[str, str]:
    files: dict[str, str] = {}
    for skill_name in harness.skills.names():
        skill = harness.skills.get(skill_name)
        if skill is None:
            continue
        skill_dir_name = skill.path.parent.name
        files[f"/skills/{skill_dir_name}/SKILL.md"] = skill.body
        for reference in skill.references:
            try:
                relative_reference = reference.path.relative_to(skill.path.parent).as_posix()
            except ValueError:
                relative_reference = reference.path.name
            files[f"/skills/{skill_dir_name}/{relative_reference}"] = reference.body
    return files


def _normalize_virtual_file_path(path: str) -> str:
    if not path:
        return ""
    normalized = path.replace("\\", "/").strip()
    if not normalized.startswith("/"):
        normalized = "/" + normalized
    while "//" in normalized:
        normalized = normalized.replace("//", "/")
    return normalized


def _workflow_request_from_messages(messages: Any) -> dict[str, Any] | None:
    if not isinstance(messages, list):
        return None
    for message in reversed(messages):
        content = _message_content_text(
            message.get("content") if isinstance(message, dict) else getattr(message, "content", "")
        ).strip()
        if not content:
            continue
        request_payload = _workflow_request_from_content(content)
        if request_payload is not None:
            return request_payload
    return None


def _workflow_request_from_content(content: str) -> dict[str, Any] | None:
    try:
        payload = _loads_json_object(content)
    except Exception:
        return None
    candidates: list[Any] = [payload]
    if isinstance(payload.get("workflow_request"), dict):
        candidates.append(payload["workflow_request"])
    if isinstance(payload.get("current_task"), dict):
        candidates.append(payload["current_task"].get("workflow_request"))
    if isinstance(payload.get("frame"), dict):
        candidates.append(payload["frame"].get("workflow_request"))
    raw_frames = payload.get("frames")
    if isinstance(raw_frames, list):
        for frame in raw_frames:
            if isinstance(frame, dict):
                candidates.append(frame.get("workflow_request"))
                current_task = frame.get("current_task")
                if isinstance(current_task, dict):
                    candidates.append(current_task.get("workflow_request"))
    for candidate in candidates:
        if _is_workflow_request_payload(candidate):
            return dict(candidate)
    return None


def _is_workflow_request_payload(candidate: Any) -> bool:
    return (
        isinstance(candidate, dict)
        and isinstance(candidate.get("url"), str)
        and bool(candidate.get("url"))
        and isinstance(candidate.get("body"), dict)
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


def _stream_chunk_mode_data(chunk: Any) -> tuple[str, Any]:
    if isinstance(chunk, tuple):
        if len(chunk) == 3 and isinstance(chunk[1], str):
            return chunk[1], chunk[2]
        if len(chunk) == 2 and isinstance(chunk[0], str):
            return chunk[0], chunk[1]
        if len(chunk) == 2:
            return "messages", chunk
    if isinstance(chunk, dict):
        if "messages" in chunk:
            return "updates", chunk
        if "updates" in chunk:
            return "updates", chunk["updates"]
    return "", chunk


def _stream_message_from_data(data: Any) -> Any | None:
    if isinstance(data, tuple | list) and data:
        return data[0]
    if isinstance(data, dict) and ("content" in data or "tool_calls" in data):
        return data
    if hasattr(data, "content"):
        return data
    return None


def _stream_messages_from_update(update: Any) -> list[Any]:
    messages: list[Any] = []

    def collect(value: Any) -> None:
        if isinstance(value, dict):
            raw_messages = value.get("messages")
            if isinstance(raw_messages, list):
                messages.extend(raw_messages)
            for child in value.values():
                if child is not raw_messages:
                    collect(child)
        elif isinstance(value, list):
            for child in value:
                collect(child)

    collect(update)
    return messages


def _stream_debug_summary(messages: list[Any]) -> dict[str, Any]:
    tail = messages[-6:]
    return {
        "message_count": len(messages),
        "tail": [
            {
                "type": message.__class__.__name__,
                "name": getattr(message, "name", None),
                "content": _truncate(_message_content_text(getattr(message, "content", "")), 160),
                "tool_calls": _message_tool_calls_debug(message),
            }
            for message in tail
        ],
    }


def _message_tool_calls_debug(message: Any) -> list[dict[str, Any]]:
    raw_tool_calls = getattr(message, "tool_calls", None)
    if not isinstance(raw_tool_calls, list):
        return []
    calls: list[dict[str, Any]] = []
    for item in raw_tool_calls:
        if isinstance(item, dict):
            calls.append(
                {
                    "name": str(item.get("name") or ""),
                    "args": item.get("args") if isinstance(item.get("args"), dict) else str(item.get("args") or ""),
                    "id": str(item.get("id") or ""),
                }
            )
    return calls


def _parse_deepagent_response(request: RouterMessageRequest, response: Any) -> DeepAgentRunResult:
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
        title="DeepAgent运行完成",
        summary=f"session={request.sessionId} frames={len(frames)}",
        data={"thread_id": _thread_id(request)},
    )
    task_state_payload = payload.get("task_state")
    task_state = (
        TaskRuntimeState.model_validate(task_state_payload)
        if isinstance(task_state_payload, dict)
        else None
    )
    return DeepAgentRunResult(frames=frames, trace_events=(trace_event,), task_state=task_state)


def _try_parse_deepagent_response(
    request: RouterMessageRequest,
    response: Any,
) -> DeepAgentRunResult | None:
    try:
        return _parse_deepagent_response(request, response)
    except Exception:
        return None


def _result_with_workflow_call(
    context: DeepAgentRunContext,
    *,
    workflow_call: WorkflowCallSnapshot,
    parsed_result: DeepAgentRunResult | None,
) -> DeepAgentRunResult:
    base_frame = parsed_result.frames[-1] if parsed_result is not None and parsed_result.frames else None
    intent_code = workflow_call.intent_code or (base_frame.intent_code if base_frame is not None else None)
    slot_memory = _workflow_slot_memory(workflow_call, base_frame)
    task_id = _workflow_task_id(base_frame)
    title = _workflow_task_title(workflow_call, base_frame)

    running_task = _workflow_task_payload(
        task_id=task_id,
        intent_code=intent_code,
        status="waiting_assistant_completion",
        title=title,
        slot_memory=slot_memory,
        output={},
        workflow_call=workflow_call,
    )
    completed_task = {
        **running_task,
        "status": "completed",
        "output": workflow_call.result.final_output,
    }
    running_task_list = _workflow_task_list(base_frame, running_task)
    completed_task_list = _workflow_task_list(base_frame, completed_task)

    frames: list[AssistantProtocolFrame] = []
    for event in workflow_call.result.events:
        frames.append(
            AssistantProtocolFrame(
                ok=True,
                status="waiting_assistant_completion",
                intent_code=intent_code,
                completion_state=1,
                completion_reason="workflow_node_output",
                details=_workflow_event_details(event, workflow_call),
                output=event.node_output,
                slot_memory=slot_memory,
                task_list=running_task_list,
                current_task=running_task,
                graph=base_frame.graph if base_frame is not None else context.task_state.graph,
            )
        )

    frames.append(
        AssistantProtocolFrame(
            ok=True,
            status="completed",
            intent_code=intent_code,
            completion_state=2,
            completion_reason="workflow_done",
            output=workflow_call.result.final_output,
            slot_memory=slot_memory,
            task_list=completed_task_list,
            current_task=completed_task,
            graph=base_frame.graph if base_frame is not None else context.task_state.graph,
        )
    )
    trace_events = [
        AssistantTraceEvent(
            stage="deepagent_langchain_tool_call_started",
            title="LangChain工具调用开始",
            summary=f"workflow_api_call {workflow_call.method} {workflow_call.url}",
            data={
                "thread_id": context.thread_id,
                "method": workflow_call.method,
                "url": workflow_call.url,
                "body_keys": sorted(workflow_call.body),
                "intent_code": intent_code,
                "skill_name": workflow_call.skill_name,
            },
        ),
        AssistantTraceEvent(
            stage="deepagent_langchain_tool_call_completed",
            title="LangChain工具调用完成",
            summary=f"workflow_api_call returned {len(workflow_call.result.events)} node_output event(s)",
            data={
                "thread_id": context.thread_id,
                "method": workflow_call.method,
                "url": workflow_call.url,
                "event_count": len(workflow_call.result.events),
            },
        ),
    ]
    if parsed_result is not None:
        trace_events.extend(parsed_result.trace_events)
    source_task_state = (
        parsed_result.task_state
        if parsed_result is not None and parsed_result.task_state is not None
        else context.task_state
    )
    return DeepAgentRunResult(
        frames=tuple(frames),
        trace_events=tuple(trace_events),
        task_state=_workflow_terminal_task_state(source_task_state, completed_task),
    )


def _workflow_slot_memory(
    workflow_call: WorkflowCallSnapshot,
    base_frame: AssistantProtocolFrame | None,
) -> dict[str, Any]:
    if workflow_call.slot_memory:
        return dict(workflow_call.slot_memory)
    if base_frame is not None and base_frame.slot_memory:
        return dict(base_frame.slot_memory)
    return {}


def _workflow_task_id(base_frame: AssistantProtocolFrame | None) -> str:
    if base_frame is not None and isinstance(base_frame.current_task, dict):
        task_id = str(base_frame.current_task.get("taskId") or "").strip()
        if task_id:
            return task_id
    return "task_001"


def _workflow_task_title(
    workflow_call: WorkflowCallSnapshot,
    base_frame: AssistantProtocolFrame | None,
) -> str:
    if base_frame is not None and isinstance(base_frame.current_task, dict):
        title = str(base_frame.current_task.get("title") or "").strip()
        if title:
            return title
    return workflow_call.skill_name or "workflow_api_call"


def _workflow_task_payload(
    *,
    task_id: str,
    intent_code: str | None,
    status: str,
    title: str,
    slot_memory: dict[str, Any],
    output: Any,
    workflow_call: WorkflowCallSnapshot,
) -> dict[str, Any]:
    return {
        "taskId": task_id,
        "intent_code": intent_code or "",
        "status": status,
        "title": title,
        "slot_memory": slot_memory,
        "output": output,
        "workflow_request": {
            "method": workflow_call.method,
            "url": workflow_call.url,
            "body": workflow_call.body,
        },
    }


def _workflow_task_list(
    base_frame: AssistantProtocolFrame | None,
    task_payload: dict[str, Any],
) -> list[dict[str, Any]]:
    if base_frame is None or not base_frame.task_list:
        return [task_payload]
    task_id = str(task_payload.get("taskId") or "")
    replaced = False
    task_list: list[dict[str, Any]] = []
    for item in base_frame.task_list:
        if isinstance(item, dict) and str(item.get("taskId") or "") == task_id:
            task_list.append(task_payload)
            replaced = True
        else:
            task_list.append(dict(item))
    if not replaced:
        task_list.append(task_payload)
    return task_list


def _workflow_event_details(
    event: WorkflowToolEvent,
    workflow_call: WorkflowCallSnapshot,
) -> dict[str, Any]:
    return {
        "node_id": event.node_id,
        "node_title": event.node_title,
        "timestamp": event.timestamp,
        "workflow_url": workflow_call.url,
    }


def _workflow_terminal_task_state(
    task_state: TaskRuntimeState,
    completed_task: dict[str, Any],
) -> TaskRuntimeState:
    active_tasks = [
        task
        for task in task_state.task_list
        if task.taskId != completed_task.get("taskId") and task.status not in {"completed", "cancelled", "failed"}
    ]
    current_task = active_tasks[0] if active_tasks else None
    return task_state.model_copy(
        update={
            "slot_memory": current_task.slot_memory if current_task is not None else {},
            "task_list": active_tasks,
            "current_task": current_task,
            "active_context": {},
            "context_leases": [],
        },
        deep=True,
    )


def _loads_json_object(content: str) -> dict[str, Any]:
    try:
        payload = json.loads(content)
    except json.JSONDecodeError as first_error:
        fenced = _extract_fenced_json(content)
        if fenced is not None:
            return json.loads(fenced)
        braced = _extract_braced_json(content)
        if braced is not None:
            return json.loads(braced)
        raise first_error
    if not isinstance(payload, dict):
        raise DeepAgentRuntimeError("deepagent final response must be a JSON object")
    return payload


def _extract_fenced_json(content: str) -> str | None:
    match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", content, flags=re.DOTALL | re.IGNORECASE)
    if match is None:
        return None
    return match.group(1)


def _extract_braced_json(content: str) -> str | None:
    start = content.find("{")
    end = content.rfind("}")
    if start < 0 or end <= start:
        return None
    return content[start : end + 1]


def _result_with_workflow_final_output(
    result: DeepAgentRunResult,
    *,
    final_output: Any,
) -> DeepAgentRunResult:
    if final_output is None or not result.frames:
        return result
    frames = list(result.frames)
    last = frames[-1]
    frames[-1] = last.model_copy(
        update={
            "output": final_output,
            "completion_reason": "workflow_done",
        }
    )
    return DeepAgentRunResult(
        frames=tuple(frames),
        trace_events=result.trace_events,
        task_state=result.task_state,
    )


def _assistant_protocol_frame_from_payload(payload: Any) -> AssistantProtocolFrame:
    if isinstance(payload, dict) and "status" not in payload and _assistant_turn_output(payload) is not None:
        return AssistantProtocolFrame(
            ok=True,
            status="completed",
            completion_state=2,
            completion_reason="deepagent_done",
            output=_assistant_turn_output(payload),
        )
    return AssistantProtocolFrame.model_validate(payload)


def _assistant_turn_output(payload: dict[str, Any]) -> Any:
    if "output" in payload:
        return payload["output"]
    content = payload.get("content")
    if isinstance(content, dict) and "output" in content:
        return content["output"]
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


def _last_message_content(response: Any) -> str:
    if isinstance(response, dict):
        messages = response.get("messages")
        if isinstance(messages, list) and messages:
            for message in reversed(messages):
                content = message.get("content") if isinstance(message, dict) else getattr(message, "content", "")
                text = _message_content_text(content).strip()
                if text:
                    return text
        if "content" in response:
            return _message_content_text(response["content"])
    return str(response)


def _message_content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                text = item.get("text") or item.get("content")
                if text:
                    parts.append(str(text))
        return "\n".join(parts)
    return str(content or "")


def _deepagent_system_prompt(harness: PromptHarness) -> str:
    return "\n\n".join(
        part
        for part in [
            _agent_context_text(harness),
            "你是 DeepAgent 版本的 intent router harness。必须按受控顺序执行，不要自由改写流程。",
            "执行顺序固定为：1) 先根据用户输入做意图识别；2) 单意图只加载命中的对应 skill 和必要 reference，多意图加载每个命中任务对应的 skill/reference；3) 根据 skill/reference 提取参数和补齐槽位；4) 槽位齐全且 executionMode=execute 时触发 LangChain 工具调用 workflow_api_call；5) 返回工具结果对应的 Assistant Protocol。",
            "如果用户一次表达多个意图或多个任务，必须使用 DeepAgent 原生 write_todos 做任务规划。任务顺序必须与用户表达顺序一致，输出 Assistant Protocol 时必须包含 task_list，并用 current_task 表示当前正在推进的任务。",
            "多任务场景必须串行推进：一次只推进一个 current_task。不要并行触发多个资金类 workflow_api_call。当前任务缺槽时先追问当前任务；当前任务完成后再推进下一个任务。",
            "如果意图不明确、没有命中 skill、或必填槽位缺失，不要调用任何工具，应返回 waiting_user_input 或 failed/unsupported 的 Assistant Protocol JSON。",
            "必须保持多用户会话隔离；只能处理当前请求给出的 sessionId/custID。",
            "需要执行子工作流时，必须触发 LangChain 工具调用 workflow_api_call(method, url, body)。不要把 tool_use、workflow_api_call 或 workflow_request 写成普通 JSON/Markdown 文本。",
            "workflow_api_call 的 url 必须是 reference 中给出的完整 http(s) 地址，不要输出相对路径，不要让模型控制 headers。",
            "工具调用完成后，必须最终只返回 Assistant Protocol JSON，不要返回 Markdown。可以返回 {\"frames\":[...],\"task_state\":{...}}。",
            "如果 workflow_api_call 返回 final_output，必须把完整 final_output 原样作为 AssistantProtocolFrame.output；不要提取 final_output.output 字段。",
        ]
        if part.strip()
    )


def _deepagent_user_payload(context: DeepAgentRunContext) -> str:
    return json.dumps(
        {
            "request": context.request.model_dump(mode="json"),
            "task_state": context.task_state.model_dump(mode="json"),
            "thread_id": context.thread_id,
            "available_skill_names": sorted(context.skills),
            "config_variables": context.config_variables,
            "assistant_protocol_contract": {
                "message_event": "AssistantProtocolFrame JSON",
                "terminal_statuses": ["completed", "failed", "cancelled"],
                "multi_task": {
                    "task_list": "When one user message contains multiple intents or tasks, include all tasks in expression order.",
                    "current_task": "Exactly one active task should be current at a time.",
                    "execution": "Use DeepAgent write_todos for planning and execute workflow tools serially.",
                },
            },
        },
        ensure_ascii=False,
    )


def _single_failed_result(
    request: RouterMessageRequest | TaskCompletionRequest,
    *,
    code: str,
    message: str,
    trace_events: list[AssistantTraceEvent] | None = None,
) -> AssistantServiceResult:
    return AssistantServiceResult(
        frames=[
            AssistantProtocolFrame(
                ok=False,
                status="failed",
                completion_state=2,
                completion_reason=code,
                output={"error": {"code": code, "message": message}},
                errorCode=code,
            )
        ],
        trace_events=trace_events or [],
    )


def _thread_id(request: RouterMessageRequest) -> str:
    return f"{request.custID}:{request.sessionId}"


def _config_variable_map(request: RouterMessageRequest) -> dict[str, Any]:
    return {item.name: item.value for item in request.config_variables}


def _slot_memory_from_workflow_body(body: dict[str, Any]) -> dict[str, Any]:
    config_variables = body.get("config_variables")
    if not isinstance(config_variables, list):
        return {}
    for item in config_variables:
        if not isinstance(item, dict):
            continue
        if str(item.get("name") or "") != "slots_data":
            continue
        raw_value = item.get("value")
        if isinstance(raw_value, dict):
            return dict(raw_value)
        if not isinstance(raw_value, str):
            return {}
        try:
            payload = json.loads(raw_value)
        except json.JSONDecodeError:
            return {}
        return payload if isinstance(payload, dict) else {}
    return {}


def _workflow_session_id(body: dict[str, Any]) -> str:
    direct = str(body.get("session_id") or "").strip()
    if direct:
        return direct
    config_variables = body.get("config_variables")
    if isinstance(config_variables, list):
        for item in config_variables:
            if not isinstance(item, dict):
                continue
            if str(item.get("name") or "") == "sessionID":
                return str(item.get("value") or "").strip()
    return ""


def _truncate(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return f"{value[:limit].rstrip()}..."


def _agent_context_text(harness: PromptHarness) -> str:
    return "\n\n".join(context.body for context in harness.agent_contexts)


def _harness_context_block(harness: PromptHarness) -> str:
    sections = ["## Intent Router Harness Loaded Context"]
    for skill_name in harness.skills.names():
        skill = harness.skills.get(skill_name)
        if skill is None:
            continue
        sections.append(
            "\n".join(
                [
                    f"### Skill: {skill.name}",
                    f"- description: {skill.description}",
                    f"- intent_codes: {list(skill.intent_codes)}",
                    f"- required_slots: {list(skill.required_slots)}",
                    f"- skill_file: /skills/{skill.path.parent.name}/SKILL.md",
                ]
            )
        )
        for reference in skill.references:
            sections.append(
                "\n".join(
                    [
                        f"#### Reference: {skill.name}/{reference.id}",
                        f"- purpose: {reference.purpose}",
                        "```markdown",
                        reference.body.strip(),
                        "```",
                    ]
                )
            )
    if len(sections) == 1:
        return ""
    sections.append(
        "Use this middleware-loaded context before reading files. "
        "Only call read_file when the loaded context is insufficient."
    )
    return "\n\n".join(sections)


def _skill_bodies(harness: PromptHarness) -> dict[str, str]:
    return {
        name: skill.body
        for name in harness.skills.names()
        if (skill := harness.skills.get(name)) is not None
    }


def _reference_bodies(harness: PromptHarness) -> dict[str, dict[str, str]]:
    result: dict[str, dict[str, str]] = {}
    for name in harness.skills.names():
        skill = harness.skills.get(name)
        if skill is None:
            continue
        result[skill.name] = {reference.id: reference.body for reference in skill.references}
    return result
