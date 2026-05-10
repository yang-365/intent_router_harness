"""DeepAgent assistant protocol service and supporting types."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from threading import Lock
from typing import Any, Protocol

from intent_router_harness.contracts import (
    AssistantProtocolFrame,
    AssistantServiceResult,
    AssistantTraceEvent,
    RouterMessageRequest,
    TaskCompletionRequest,
    TaskRuntimeState,
)
from intent_router_harness.deepagent.errors import (
    DeepAgentRuntimeError,
    SessionRunInProgressError,
)
from intent_router_harness.deepagent.helpers import (
    _agent_context_text,
    _append_trace,
    _config_variable_map,
    _reference_bodies,
    _skill_bodies,
    _thread_id,
)
from intent_router_harness.runtime import PromptHarness
from intent_router_harness.session_store import InMemorySessionStore, SessionNotFoundError


@dataclass
class DeepAgentRunContext:
    request: RouterMessageRequest
    task_state: TaskRuntimeState
    thread_id: str
    agent_context: str
    skills: dict[str, str]
    references: dict[str, dict[str, str]]
    workflow_allowed_urls: tuple[str, ...] = ()
    config_variables: dict[str, Any] | None = None


@dataclass
class DeepAgentRunResult:
    frames: tuple[AssistantProtocolFrame, ...]
    trace_events: tuple[AssistantTraceEvent, ...] = ()
    task_state: TaskRuntimeState | None = None


class DeepAgentRunner(Protocol):
    def run_message(self, context: DeepAgentRunContext) -> DeepAgentRunResult: ...


class SessionRunLockStore:
    """Non-reentrant per-session lock: one active agent run per session."""

    def __init__(self) -> None:
        self._locks: dict[str, bool] = {}
        self._mu = Lock()

    @contextmanager
    def acquire(self, *, user_id: str, session_id: str):
        key = f"{user_id}:{session_id}"
        with self._mu:
            if self._locks.get(key, False):
                raise SessionRunInProgressError(
                    f"session {session_id} already has an active agent run"
                )
            self._locks[key] = True
        try:
            yield
        finally:
            with self._mu:
                self._locks.pop(key, None)


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
                        title="\u8bf7\u6c42\u8fdb\u5165Router",
                        summary=f"session={request.sessionId}\uff0cexecutionMode={request.executionMode}",
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
                        title="Session\u751f\u547d\u5468\u671f\u8bfb\u53d6",
                        summary=(
                            f"expired={loaded.expired}\uff0c"
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
                        title="\u4efb\u52a1\u8fd0\u884c\u6001\u8bfb\u53d6",
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
                        title="DeepAgent\u8fd0\u884c\u5f00\u59cb",
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
                        title="SSE\u4e1a\u52a1\u5e27\u751f\u6210",
                        summary=f"\u751f\u6210 {len(result.frames)} \u4e2a message frame\uff0c\u6700\u7ec8\u72b6\u6001 {result.frames[-1].status}",
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
                    title="DeepAgent\u8fd0\u884c\u5931\u8d25",
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
