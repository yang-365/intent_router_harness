"""FastAPI routes — same /api/v1/* wire format as v1 for full compatibility."""

from __future__ import annotations

import json
import logging
import os
from datetime import timedelta
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, StreamingResponse

from intent_router_harness.harness_v2.config import HarnessConfig, load_config
from intent_router_harness.harness_v2.errors import HarnessError, SessionBusyError
from intent_router_harness.harness_v2.protocol import (
    AssistantProtocolFrame,
    AssistantStatus,
    MessageRequest,
    TaskCompletionRequest,
    TraceEvent,
    emit_trace,
    trace_collector_init,
)
from intent_router_harness.harness_v2.session import SessionManager

logger = logging.getLogger(__name__)

SSE_HEADERS = {
    "Cache-Control": "no-cache, no-transform",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}


class HarnessApp:
    """Application container — holds config, agent, and session manager."""

    def __init__(
        self,
        config: HarnessConfig,
        agent: Any | None = None,
        session_mgr: SessionManager | None = None,
        skill_lifecycle: Any | None = None,
    ) -> None:
        self.config = config
        self._agent = agent
        self._skill_lifecycle = skill_lifecycle
        self.session_mgr = session_mgr or SessionManager(
            idle_timeout=timedelta(seconds=config.session_idle_timeout_seconds),
        )

    @property
    def agent(self) -> Any:
        if self._agent is None:
            from intent_router_harness.harness_v2.agent import build_agent

            self._agent, self._skill_lifecycle = build_agent(self.config)
        return self._agent

    @property
    def skill_lifecycle(self) -> Any | None:
        # Ensure agent is built so _skill_lifecycle is populated
        _ = self.agent
        return self._skill_lifecycle


def create_app(
    spec_path: str | Path | None = None,
    *,
    config: HarnessConfig | None = None,
    harness_app: HarnessApp | None = None,
) -> FastAPI:
    """Create the ASGI application with v1-compatible routes."""
    if harness_app is None:
        default_spec = os.environ.get("HARNESS_SPEC_PATH", "examples/deepagent-finance-router-harness.toml")
        resolved_config = config or load_config(spec_path or default_spec)
        harness_app = HarnessApp(resolved_config)

    app = FastAPI(title="intent_router_harness", version="2.0.0")
    app.state.harness = harness_app

    # ------------------------------------------------------------------
    # Health
    # ------------------------------------------------------------------

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    def readyz() -> dict[str, Any]:
        return {
            "ready": True,
            "service": "intent_router_harness",
            "version": "2.0.0",
        }

    # ------------------------------------------------------------------
    # Index
    # ------------------------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        from intent_router_harness.harness_v2.debug_ui import validator_html

        return validator_html()

    # ------------------------------------------------------------------
    # /api/v1/message — same wire format
    # ------------------------------------------------------------------

    @app.post("/api/v1/message")
    def message(request: MessageRequest):
        ha = app.state.harness
        try:
            with ha.session_mgr.acquire(request.custID, request.sessionId) as meta:
                thread_id = meta.thread_id
                # Initialise per-request trace collector so middleware
                # events are captured alongside the high-level API traces.
                mw_trace_buf = trace_collector_init()

                emit_trace(
                    "request_received",
                    "请求进入Router",
                    f"session={request.sessionId}，executionMode={request.executionMode}",
                    session_id=request.sessionId,
                    cust_id=request.custID,
                    execution_mode=request.executionMode,
                    txt=request.txt,
                )
                user_input = _build_user_input(request)
                emit_trace(
                    "deepagent_runtime_start",
                    "DeepAgent运行开始",
                    f"thread_id={thread_id}",
                    thread_id=thread_id,
                )
                result = _invoke_agent(ha.agent, user_input, thread_id)
                frames = _extract_frames(result)
                todos = result.get("todos")
                _inject_todos_into_frames(frames, todos)
                if todos:
                    task_list, current_task = _todos_to_task_list(todos)
                    emit_trace(
                        "task_planned",
                        "任务规划 (write_todos)",
                        f"共 {len(task_list)} 个任务",
                        task_list=task_list,
                        current_task=current_task,
                        raw_todos=todos,
                    )
                emit_trace(
                    "assistant_protocol_frames",
                    "SSE业务帧生成",
                    f"生成 {len(frames)} 个 message frame",
                    frame_count=len(frames),
                    frames=[f.protocol_dump() for f in frames],
                )
                # Collect all trace events (API-level + middleware-level)
                trace_events: list[TraceEvent] = list(mw_trace_buf)
                payloads = [f.protocol_dump() for f in frames]
                if request.stream:
                    trace_payloads = (
                        [e.model_dump(mode="json") for e in trace_events]
                        if request.debugTrace
                        else []
                    )
                    return _sse_response(payloads, trace_payloads=trace_payloads)
                return payloads[-1] if payloads else {}
        except SessionBusyError as exc:
            frame = AssistantProtocolFrame(
                ok=False,
                status="failed",
                completion_state=0,
                completion_reason="session_run_in_progress",
                errorCode="session_run_in_progress",
                message=str(exc),
            )
            payload = frame.protocol_dump()
            if request.stream:
                return _sse_response([payload])
            return payload
        except HarnessError as exc:
            frame = AssistantProtocolFrame(
                ok=False,
                status="failed",
                completion_state=0,
                completion_reason=exc.code,
                errorCode=exc.code,
                message=str(exc),
            )
            payload = frame.protocol_dump()
            if request.stream:
                return _sse_response([payload])
            return payload

    # ------------------------------------------------------------------
    # /api/v1/task/completion — same wire format
    # ------------------------------------------------------------------

    @app.post("/api/v1/task/completion")
    def task_completion(request: TaskCompletionRequest):
        ha = app.state.harness
        todo_status = "completed" if request.completionSignal == 1 else "failed"

        # Unload current skill context
        if ha.skill_lifecycle is not None:
            ha.skill_lifecycle.unload_skill()

        # Update AgentState.todos via checkpointer
        thread_id = ha.session_mgr.thread_id(request.custID, request.sessionId)
        updated_todos = _advance_todos(ha.agent, thread_id, todo_status)

        # Build response frame
        task_list, current_task = _todos_to_task_list(updated_todos) if updated_todos else ([], None)
        has_remaining = updated_todos is not None and any(
            t.get("status") in ("pending", "in_progress") for t in updated_todos
        )
        frame_status: AssistantStatus = todo_status if not has_remaining else "waiting_assistant_completion"  # type: ignore[assignment]
        frame = AssistantProtocolFrame(
            ok=request.completionSignal == 1,
            status=frame_status,
            completion_state=1 if has_remaining else 2,
            completion_reason="task_completion_received",
            output={"taskId": request.taskId, "completionSignal": request.completionSignal},
            task_list=task_list,
            current_task=current_task,
        )
        payload = frame.protocol_dump()
        if request.stream:
            trace_payloads: list[dict[str, Any]] = []
            if request.debugTrace:
                trace_payloads = [
                    TraceEvent(
                        stage="task_completion",
                        title="任务完成回调",
                        summary=f"taskId={request.taskId} signal={request.completionSignal}",
                        data={
                            "taskId": request.taskId,
                            "completionSignal": request.completionSignal,
                            "todos_remaining": len([
                                t for t in (updated_todos or [])
                                if t.get("status") == "pending"
                            ]),
                        },
                    ).model_dump(mode="json")
                ]
            return _sse_response([payload], trace_payloads=trace_payloads)
        return payload

    return app


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------


def _build_user_input(request: MessageRequest) -> str:
    """Pack frontend parameters into a JSON string for the agent."""
    return json.dumps(
        {
            "txt": request.txt,
            "sessionId": request.sessionId,
            "custID": request.custID,
            "executionMode": request.executionMode,
            "config_variables": request.config_variables,
            "recommendTask": request.recommendTask,
            "currentDisplay": request.currentDisplay,
        },
        ensure_ascii=False,
    )


def _advance_todos(
    agent: Any,
    thread_id: str,
    status: str,
) -> list[dict[str, Any]] | None:
    """Mark the current in_progress todo as completed/failed and advance.

    Steps:
    1. Read current todos from agent state via checkpointer
    2. Mark the first ``in_progress`` todo with *status*
    3. Mark the next ``pending`` todo as ``in_progress``
    4. If all todos are done, clear the list
    5. Write back via ``agent.update_state()``

    Returns the updated todos list, or ``None`` if no todos exist.
    """
    try:
        config = {"configurable": {"thread_id": thread_id}}
        state = agent.get_state(config)
        todos: list[dict[str, Any]] = list(state.values.get("todos") or [])
    except Exception:
        logger.debug("could not read agent state for thread=%s", thread_id, exc_info=True)
        return None

    if not todos:
        return None

    # Mark current in_progress todo
    for todo in todos:
        if todo.get("status") == "in_progress":
            todo["status"] = status
            break

    # Advance next pending todo to in_progress
    for todo in todos:
        if todo.get("status") == "pending":
            todo["status"] = "in_progress"
            break

    # If all todos are done, clear the list
    all_done = all(t.get("status") in ("completed", "failed") for t in todos)
    updated = [] if all_done else todos

    try:
        agent.update_state(config, {"todos": updated})
    except Exception:
        logger.debug("could not update agent state for thread=%s", thread_id, exc_info=True)

    return todos  # Return pre-clear list for response frame


def _invoke_agent(agent: Any, user_input: str, thread_id: str) -> dict[str, Any]:
    """Run the compiled deepagent graph synchronously."""
    try:
        from langchain_core.messages import HumanMessage
    except ImportError as exc:
        raise RuntimeError("langchain-core is required") from exc

    result = agent.invoke(
        {"messages": [HumanMessage(content=user_input)]},
        config={"configurable": {"thread_id": thread_id}},
    )
    return result


def _todos_to_task_list(todos: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """Convert deepagent write_todos format to protocol task_list + current_task."""
    task_list: list[dict[str, Any]] = []
    current_task: dict[str, Any] | None = None
    for i, todo in enumerate(todos):
        task = {
            "taskId": f"todo_{i}",
            "title": todo.get("content", ""),
            "status": todo.get("status", "pending"),
        }
        task_list.append(task)
        if current_task is None and todo.get("status") == "in_progress":
            current_task = task
    return task_list, current_task


def _inject_todos_into_frames(
    frames: list[AssistantProtocolFrame],
    todos: list[dict[str, Any]] | None,
) -> None:
    """Populate task_list and current_task on frames from AgentState.todos."""
    if not todos:
        return
    task_list, current_task = _todos_to_task_list(todos)
    for frame in frames:
        if not frame.task_list:
            frame.task_list = task_list
        if frame.current_task is None and current_task is not None:
            frame.current_task = current_task


def _extract_frames(result: dict[str, Any]) -> list[AssistantProtocolFrame]:
    """Parse AssistantProtocolFrame(s) from the agent's final AI message."""
    messages = result.get("messages", [])
    if not messages:
        return [_fallback_frame("no messages in agent result")]

    last = messages[-1]
    content = last.content if hasattr(last, "content") and isinstance(last.content, str) else ""

    if not content.strip().startswith("{"):
        return [
            AssistantProtocolFrame(
                ok=True,
                status="running",
                completion_state=0,
                completion_reason="natural_response",
                message=content,
            )
        ]

    try:
        payload = json.loads(content)
    except json.JSONDecodeError:
        return [
            AssistantProtocolFrame(
                ok=True,
                status="running",
                completion_state=0,
                completion_reason="natural_response",
                message=content,
            )
        ]

    if isinstance(payload, dict) and "frames" in payload:
        frames: list[AssistantProtocolFrame] = []
        for raw in payload["frames"]:
            frames.append(AssistantProtocolFrame(**{
                "ok": raw.get("ok", True),
                "status": raw.get("status") or "running",
                "completion_state": raw.get("completion_state") or 0,
                "completion_reason": raw.get("completion_reason") or "",
                "message": raw.get("message"),
                "output": raw.get("output") or {},
                "slot_memory": raw.get("slot_memory") or {},
                "task_list": raw.get("task_list") or [],
                "current_task": raw.get("current_task"),
                "intent_code": raw.get("intent_code"),
                "stage": raw.get("stage"),
                "details": raw.get("details"),
                "errorCode": raw.get("errorCode"),
                "graph": raw.get("graph"),
                "actions": raw.get("actions") or [],
            }))
        return frames if frames else [_fallback_frame("empty frames array")]

    if isinstance(payload, dict) and "status" in payload:
        return [AssistantProtocolFrame(**{
            "ok": payload.get("ok", True),
            "status": payload.get("status") or "running",
            "completion_state": payload.get("completion_state") or 0,
            "completion_reason": payload.get("completion_reason") or "",
            "message": payload.get("message"),
            "output": payload.get("output") or {},
            "slot_memory": payload.get("slot_memory") or {},
            "task_list": payload.get("task_list") or [],
            "current_task": payload.get("current_task"),
            "intent_code": payload.get("intent_code"),
        })]

    return [
        AssistantProtocolFrame(
            ok=True,
            status="running",
            completion_state=0,
            completion_reason="unstructured_response",
            message=content,
        )
    ]


def _fallback_frame(reason: str) -> AssistantProtocolFrame:
    return AssistantProtocolFrame(
        ok=False,
        status="failed",
        completion_state=0,
        completion_reason=reason,
        errorCode="agent_output_error",
    )


def _sse_response(
    payloads: list[dict[str, Any]],
    *,
    trace_payloads: list[dict[str, Any]] | None = None,
) -> StreamingResponse:
    return StreamingResponse(
        _sse_events(payloads, trace_payloads=trace_payloads or []),
        media_type="text/event-stream",
        headers=SSE_HEADERS,
    )


def _sse_events(
    payloads: list[dict[str, Any]],
    *,
    trace_payloads: list[dict[str, Any]],
):
    """Yield SSE events in exactly the same format as v1."""
    for payload in trace_payloads:
        yield "event: trace\n"
        yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"

    for payload in payloads:
        yield "event: message\n"
        yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"

    yield "event: done\n"
    yield "data: [DONE]\n\n"
