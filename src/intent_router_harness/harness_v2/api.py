"""FastAPI routes — same /api/v1/* wire format as v1 for full compatibility."""

from __future__ import annotations

import json
import logging
from datetime import timedelta
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse

from intent_router_harness.harness_v2.config import HarnessConfig, load_config
from intent_router_harness.harness_v2.errors import HarnessError, SessionBusyError
from intent_router_harness.harness_v2.protocol import (
    AssistantProtocolFrame,
    MessageRequest,
    TaskCompletionRequest,
    TraceEvent,
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
    ) -> None:
        self.config = config
        self._agent = agent
        self.session_mgr = session_mgr or SessionManager(
            idle_timeout=timedelta(seconds=config.session_idle_timeout_seconds),
        )

    @property
    def agent(self) -> Any:
        if self._agent is None:
            from intent_router_harness.harness_v2.agent import build_agent

            self._agent = build_agent(self.config)
        return self._agent


def create_app(
    spec_path: str | Path | None = None,
    *,
    config: HarnessConfig | None = None,
    harness_app: HarnessApp | None = None,
) -> FastAPI:
    """Create the ASGI application with v1-compatible routes."""
    if harness_app is None:
        resolved_config = config or load_config(spec_path or "examples/deepagent-finance-router-harness.toml")
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
        return "<h1>intent_router_harness v2</h1><p>POST /api/v1/message</p>"

    # ------------------------------------------------------------------
    # /api/v1/message — same wire format
    # ------------------------------------------------------------------

    @app.post("/api/v1/message")
    def message(request: MessageRequest):
        ha = app.state.harness
        try:
            with ha.session_mgr.acquire(request.custID, request.sessionId) as meta:
                thread_id = meta.thread_id
                trace_events: list[TraceEvent] = []
                trace_events.append(
                    TraceEvent(
                        stage="request_received",
                        title="请求进入Router",
                        summary=f"session={request.sessionId}，executionMode={request.executionMode}",
                        data={
                            "session_id": request.sessionId,
                            "cust_id": request.custID,
                            "execution_mode": request.executionMode,
                            "txt": request.txt,
                        },
                    )
                )
                user_input = _build_user_input(request)
                trace_events.append(
                    TraceEvent(
                        stage="deepagent_runtime_start",
                        title="DeepAgent运行开始",
                        summary=f"thread_id={thread_id}",
                        data={"thread_id": thread_id},
                    )
                )
                result = _invoke_agent(ha.agent, user_input, thread_id)
                frames = _extract_frames(result)
                trace_events.append(
                    TraceEvent(
                        stage="assistant_protocol_frames",
                        title="SSE业务帧生成",
                        summary=f"生成 {len(frames)} 个 message frame",
                        data={
                            "frame_count": len(frames),
                            "frames": [f.protocol_dump() for f in frames],
                        },
                    )
                )
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
        status = "completed" if request.completionSignal == 1 else "failed"
        frame = AssistantProtocolFrame(
            ok=request.completionSignal == 1,
            status=status,
            completion_state=2,
            completion_reason="task_completion_received",
            output={"taskId": request.taskId, "completionSignal": request.completionSignal},
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
                        data={"taskId": request.taskId, "completionSignal": request.completionSignal},
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
                "status": raw.get("status", "running"),
                "completion_state": raw.get("completion_state", 0),
                "completion_reason": raw.get("completion_reason", ""),
                "message": raw.get("message"),
                "output": raw.get("output", {}),
                "slot_memory": raw.get("slot_memory", {}),
                "task_list": raw.get("task_list", []),
                "current_task": raw.get("current_task"),
                "intent_code": raw.get("intent_code"),
                "stage": raw.get("stage"),
                "details": raw.get("details"),
                "errorCode": raw.get("errorCode"),
                "graph": raw.get("graph"),
                "actions": raw.get("actions", []),
            }))
        return frames if frames else [_fallback_frame("empty frames array")]

    if isinstance(payload, dict) and "status" in payload:
        return [AssistantProtocolFrame(**{
            "ok": payload.get("ok", True),
            "status": payload.get("status", "running"),
            "completion_state": payload.get("completion_state", 0),
            "completion_reason": payload.get("completion_reason", ""),
            "message": payload.get("message"),
            "output": payload.get("output", {}),
            "slot_memory": payload.get("slot_memory", {}),
            "task_list": payload.get("task_list", []),
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
