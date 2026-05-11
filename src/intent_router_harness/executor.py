"""Workflow executor backed by LLM function calling.

The executor is a stateless, single-pass component that sits between the
planner and the HTTP workflow layer.  It receives the planner's slot-filling
result, collects **all** execution-relevant references from the skill, and
presents them to the LLM alongside a generic tool definition.  The model
autonomously assembles call parameters based on the reference descriptions.

No reference id is hard-coded.  The skill author controls what the executor
sees by declaring references in the SKILL.md frontmatter; the executor simply
loads all references that belong to the matched skill, excluding those already
consumed by the planner (``slot_filling``, ``slot_rules``).

Design constraints
------------------
* **Stateless** – no mutable instance state; all context flows through method
  arguments.  Safe to share across concurrent requests.
* **Memory-friendly** – SSE events are processed in a streaming fashion; only
  the *last* output is retained across iterations.
* **Single LLM call** – the executor makes exactly one ``chat`` call with
  ``tool_choice`` forced so that the model must invoke the tool.
* **Reference-driven** – any skill reference can drive the tool definition;
  the executor itself is business-agnostic.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Protocol

from intent_router_harness.contracts import (
    AssistantProtocolFrame,
    PlannedTask,
    RouterMessageRequest,
    TaskRuntimeState,
)
from intent_router_harness.llm import LLMClient, LLMRequestError
from intent_router_harness.skills import SkillDocument, SkillLibrary, SkillReference
from intent_router_harness.workflow import (
    WorkflowHTTPRequest,
    WorkflowToolClient,
    WorkflowToolError,
    WorkflowToolSpec,
    render_workflow_response_mapping,
    workflow_event_output,
)
from intent_router_harness.workflow_hooks import (
    WorkflowHook,
    WorkflowHookError,
    run_first_workflow_hook,
    run_workflow_hooks,
)

logger = logging.getLogger(__name__)

TOOL_NAME = "workflow_api_call"
TERMINAL_TASK_STATUSES = {"completed", "cancelled", "failed"}


class ExecutorError(RuntimeError):
    """Raised when the executor cannot complete a workflow invocation."""


# ---------------------------------------------------------------------------
# Result container
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class ExecutorResult:
    """Immutable result returned by a workflow executor invocation."""

    frames: tuple[AssistantProtocolFrame, ...]
    task_state: TaskRuntimeState


# ---------------------------------------------------------------------------
# Executor protocol
# ---------------------------------------------------------------------------

class WorkflowExecutor(Protocol):
    """Boundary protocol for workflow execution."""

    def execute(
        self,
        request: RouterMessageRequest,
        task_state: TaskRuntimeState,
    ) -> ExecutorResult:
        """Execute the current task's workflow if conditions are met."""


# ---------------------------------------------------------------------------
# LLM-backed implementation
# ---------------------------------------------------------------------------

class LLMWorkflowExecutor:
    """Executor that uses LLM function calling to build workflow payloads.

    All instance attributes are immutable references injected at construction
    time.  No per-request state is stored on the instance.
    """

    def __init__(
        self,
        *,
        llm_client: LLMClient,
        skill_library: SkillLibrary,
        workflow_client: WorkflowToolClient,
        workflow_tools: dict[str, WorkflowToolSpec],
        workflow_hooks: tuple[WorkflowHook, ...] = (),
    ) -> None:
        self._llm = llm_client
        self._skills = skill_library
        self._workflow_client = workflow_client
        self._workflow_tools = dict(workflow_tools)
        self._hooks = tuple(workflow_hooks)

    # -- public entry point -------------------------------------------------

    def execute(
        self,
        request: RouterMessageRequest,
        task_state: TaskRuntimeState,
    ) -> ExecutorResult:
        current_task = task_state.current_task
        if (
            current_task is None
            or request.executionMode != "execute"
            or current_task.status != "ready_for_dispatch"
        ):
            return ExecutorResult(frames=(), task_state=task_state)

        spec = self._workflow_tools.get(current_task.intent_code)
        if spec is None:
            return ExecutorResult(frames=(), task_state=task_state)

        skill = self._find_skill(current_task.intent_code)
        if skill is None:
            return ExecutorResult(frames=(), task_state=task_state)

        exec_refs = _collect_executor_references(skill)
        if not exec_refs:
            return ExecutorResult(frames=(), task_state=task_state)

        ref_body = _merge_reference_bodies(exec_refs)

        # -- LLM function call to assemble the payload ----------------------
        try:
            workflow_request = self._call_llm_for_tool(
                request=request,
                current_task=current_task,
                ref_body=ref_body,
            )
        except (ExecutorError, LLMRequestError) as exc:
            logger.exception(
                "executor.function_call.failed session_id=%s task_id=%s error=%s",
                request.sessionId,
                current_task.taskId,
                exc,
            )
            return self._failed_result(
                request, task_state, current_task, spec,
                error_summary={"code": "executor_function_call_error", "message": str(exc)},
            )

        # -- before hooks (URL allowlist, etc.) -----------------------------
        try:
            run_workflow_hooks(
                self._hooks,
                event="before_workflow_tool_call",
                payload={
                    "request": {
                        "method": workflow_request.method,
                        "url": workflow_request.url,
                        "body": workflow_request.body,
                    },
                    "allowed_urls": list(spec.allowed_urls),
                },
            )
        except (WorkflowToolError, WorkflowHookError) as exc:
            logger.exception(
                "executor.before_hook.failed session_id=%s error=%s",
                request.sessionId,
                exc,
            )
            return self._failed_result(
                request, task_state, current_task, spec,
                error_summary={"code": "workflow_error", "message": str(exc)},
            )

        logger.info(
            "executor.workflow.start session_id=%s task_id=%s intent_code=%s method=%s url=%s",
            request.sessionId,
            current_task.taskId,
            current_task.intent_code,
            workflow_request.method,
            workflow_request.url,
        )

        # -- execute workflow HTTP call -------------------------------------
        running_task = current_task.model_copy(
            update={"status": "waiting_assistant_completion"}, deep=True,
        )
        running_task_list = [
            running_task if t.taskId == running_task.taskId else t
            for t in task_state.task_list
        ]

        try:
            workflow_result = self._workflow_client.run_workflow(
                spec, request_payload=workflow_request,
            )
        except WorkflowToolError as exc:
            logger.exception(
                "executor.workflow.failed session_id=%s task_id=%s error=%s",
                request.sessionId,
                current_task.taskId,
                exc,
            )
            return self._failed_result(
                request, task_state, current_task, spec,
                error_summary={"code": "workflow_error", "message": str(exc)},
            )

        # -- process SSE events through hooks → frames ----------------------
        frames: list[AssistantProtocolFrame] = []
        last_output: Any = None

        for event in workflow_result.events:
            node_mapping = render_workflow_response_mapping(
                spec,
                phase="message",
                request=request,
                task=running_task,
                event=event,
                last_output=last_output,
            )
            hook_node = run_first_workflow_hook(
                self._hooks,
                event="after_workflow_tool_call",
                payload={
                    "phase": "message",
                    "url": workflow_request.url,
                    "response_type": "sse",
                    "event": {
                        "node_id": event.node_id,
                        "node_title": event.node_title,
                        "node_output": event.node_output,
                    },
                    "request": {
                        "method": workflow_request.method,
                        "url": workflow_request.url,
                        "body": workflow_request.body,
                    },
                },
            )
            if hook_node is not None:
                node_mapping = {**node_mapping, **hook_node}

            output = node_mapping.get("output", workflow_event_output(event))
            frames.append(
                AssistantProtocolFrame(
                    ok=True,
                    status=str(node_mapping.get("status") or "waiting_assistant_completion"),
                    intent_code=current_task.intent_code,
                    completion_state=0,
                    completion_reason=str(node_mapping.get("completion_reason") or "workflow_node_output"),
                    output=output,
                    slot_memory=current_task.slot_memory,
                    task_list=[t.model_dump(mode="json") for t in running_task_list],
                    current_task=running_task.model_dump(mode="json"),
                    graph=task_state.graph,
                ),
            )
            last_output = output

        # -- done frame -----------------------------------------------------
        completed_task = current_task.model_copy(
            update={"status": "completed"}, deep=True,
        )
        completed_task_list = [
            completed_task if t.taskId == completed_task.taskId else t
            for t in task_state.task_list
        ]

        done_mapping = render_workflow_response_mapping(
            spec,
            phase="done",
            request=request,
            task=completed_task,
            last_output=last_output,
        )
        hook_done = run_first_workflow_hook(
            self._hooks,
            event="after_workflow_tool_call",
            payload={
                "phase": "done",
                "url": workflow_request.url,
                "response_type": "sse",
                "last": {"output": last_output},
                "request": {
                    "method": workflow_request.method,
                    "url": workflow_request.url,
                    "body": workflow_request.body,
                },
            },
        )
        if hook_done is not None:
            done_mapping = {**done_mapping, **hook_done}

        frames.append(
            AssistantProtocolFrame(
                ok=True,
                status=str(done_mapping.get("status") or "completed"),
                intent_code=current_task.intent_code,
                completion_state=2,
                completion_reason=str(done_mapping.get("completion_reason") or "workflow_done"),
                output=done_mapping.get("output") if "output" in done_mapping else (
                    workflow_result.final_output
                ),
                slot_memory=current_task.slot_memory,
                task_list=[t.model_dump(mode="json") for t in completed_task_list],
                current_task=completed_task.model_dump(mode="json"),
                graph=task_state.graph,
            ),
        )

        logger.info(
            "executor.workflow.done session_id=%s task_id=%s intent_code=%s event_count=%d",
            request.sessionId,
            current_task.taskId,
            current_task.intent_code,
            len(workflow_result.events),
        )

        updated_task_state = _terminal_task_state(task_state, completed_task)
        return ExecutorResult(frames=tuple(frames), task_state=updated_task_state)

    # -- LLM function call --------------------------------------------------

    def _call_llm_for_tool(
        self,
        *,
        request: RouterMessageRequest,
        current_task: PlannedTask,
        ref_body: str,
    ) -> WorkflowHTTPRequest:
        tool_def = _build_tool_definition(ref_body)
        config_vars = {
            cv.get("name", cv.get("key", "")): cv.get("value", "")
            for cv in (
                [v.model_dump() for v in request.config_variables]
                if request.config_variables
                else []
            )
        }
        messages: list[dict[str, str]] = [
            {
                "role": "system",
                "content": (
                    "你是 workflow 执行器。根据提槽结果和系统变量，调用工具执行业务。"
                    "严格按照工具定义描述组装参数，不要添加未定义的字段。"
                ),
            },
            {
                "role": "user",
                "content": "\n".join([
                    f"slot_memory: {json.dumps(current_task.slot_memory, ensure_ascii=False)}",
                    f"config_variables: {json.dumps(config_vars, ensure_ascii=False)}",
                    f"用户输入: {request.txt}",
                    "请调用工具。",
                ]),
            },
        ]

        logger.info(
            "executor.llm.start session_id=%s task_id=%s intent_code=%s",
            request.sessionId,
            current_task.taskId,
            current_task.intent_code,
        )

        response = self._llm.chat(
            messages,
            tools=[tool_def],
            tool_choice={"type": "function", "function": {"name": TOOL_NAME}},
        )

        return _parse_tool_call_response(response)

    # -- helpers ------------------------------------------------------------

    def _find_skill(self, intent_code: str) -> SkillDocument | None:
        for name in self._skills.names():
            skill = self._skills.get(name)
            if skill is not None and intent_code in skill.intent_codes:
                return skill
        return None

    def _failed_result(
        self,
        request: RouterMessageRequest,
        task_state: TaskRuntimeState,
        current_task: PlannedTask,
        spec: WorkflowToolSpec,
        *,
        error_summary: dict[str, Any],
    ) -> ExecutorResult:
        try:
            error_mapping = render_workflow_response_mapping(
                spec,
                phase="error",
                request=request,
                task=current_task,
                error_summary=error_summary,
            )
            hook_error = run_first_workflow_hook(
                self._hooks,
                event="after_workflow_tool_call",
                payload={
                    "phase": "error",
                    "url": "",
                    "response_type": "sse",
                    "error": error_summary,
                },
            )
            if hook_error is not None:
                error_mapping = {**error_mapping, **hook_error}
        except (WorkflowToolError, WorkflowHookError):
            error_mapping = {
                "status": "failed",
                "completion_reason": "workflow_error",
                "output": {"error": error_summary},
            }

        failed_task = current_task.model_copy(update={"status": "failed"}, deep=True)
        failed_frame = AssistantProtocolFrame(
            ok=False,
            status=str(error_mapping.get("status") or "failed"),
            intent_code=current_task.intent_code,
            completion_state=2,
            completion_reason=str(error_mapping.get("completion_reason") or "workflow_error"),
            output=error_mapping.get("output") or {"error": error_summary},
            slot_memory=current_task.slot_memory,
            task_list=[
                (failed_task if t.taskId == failed_task.taskId else t).model_dump(mode="json")
                for t in task_state.task_list
            ],
            current_task=failed_task.model_dump(mode="json"),
            graph=task_state.graph,
        )
        updated = _terminal_task_state(task_state, failed_task)
        return ExecutorResult(frames=(failed_frame,), task_state=updated)


# ---------------------------------------------------------------------------
# Pure functions (stateless helpers)
# ---------------------------------------------------------------------------

# Reference ids consumed by the planner; excluded from executor context.
_PLANNER_REFERENCE_IDS = frozenset({"slot_filling", "slot_rules"})


def _collect_executor_references(skill: SkillDocument) -> tuple[SkillReference, ...]:
    """Return all skill references that are relevant to the executor.

    Planner-specific references (``slot_filling``, ``slot_rules``) are
    excluded.  Everything else is considered execution context – the skill
    author decides what references the executor sees.
    """
    return tuple(
        ref for ref in skill.references
        if ref.id not in _PLANNER_REFERENCE_IDS
    )


def _merge_reference_bodies(refs: tuple[SkillReference, ...]) -> str:
    """Concatenate reference bodies into a single text block."""
    parts: list[str] = []
    for ref in refs:
        header = f"## {ref.purpose}" if ref.purpose else f"## {ref.id}"
        parts.append(f"{header}\n{ref.body}")
    return "\n\n".join(parts)


def _build_tool_definition(ref_body: str) -> dict[str, Any]:
    """Build an OpenAI function-tool definition from skill reference text.

    The full reference text is embedded in the function description so the
    LLM can read the contract.  The parameter schema is generic (method +
    url + body) and skill-agnostic; the LLM fills concrete values based on
    the reference instructions.
    """
    return {
        "type": "function",
        "function": {
            "name": TOOL_NAME,
            "description": (
                "执行 HTTP 调用。根据以下 Reference 定义组装参数。\n\n"
                + ref_body
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "method": {
                        "type": "string",
                        "description": "HTTP 方法",
                    },
                    "url": {
                        "type": "string",
                        "description": "完整 HTTP(S) URL，按 Reference 输出",
                    },
                    "body": {
                        "type": "object",
                        "description": "请求体 JSON 对象，按 Reference 定义填充",
                    },
                },
                "required": ["method", "url", "body"],
            },
        },
    }


def _parse_tool_call_response(response: dict[str, Any]) -> WorkflowHTTPRequest:
    """Extract a ``WorkflowHTTPRequest`` from an LLM function-call response."""
    try:
        message = response["choices"][0]["message"]
    except (KeyError, IndexError, TypeError) as exc:
        raise ExecutorError("LLM response missing choices[0].message") from exc

    tool_calls = message.get("tool_calls")
    if not tool_calls:
        raise ExecutorError(
            "LLM did not produce a tool_call; "
            f"content={message.get('content', '')!r}"
        )
    raw_arguments = tool_calls[0].get("function", {}).get("arguments", "")
    try:
        arguments = json.loads(raw_arguments) if isinstance(raw_arguments, str) else raw_arguments
    except json.JSONDecodeError as exc:
        raise ExecutorError(f"tool_call arguments is not valid JSON: {exc}") from exc

    if not isinstance(arguments, dict):
        raise ExecutorError("tool_call arguments must be a JSON object")

    method = str(arguments.get("method") or "").upper()
    url = str(arguments.get("url") or "").strip()
    body = arguments.get("body")
    if not isinstance(body, dict):
        raise ExecutorError("tool_call body must be a JSON object")
    if method not in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
        raise ExecutorError(f"unsupported HTTP method from LLM: {method}")
    if not url:
        raise ExecutorError("tool_call url is empty")

    return WorkflowHTTPRequest(method=method, url=url, body=body)


def _terminal_task_state(
    task_state: TaskRuntimeState,
    terminal_task: PlannedTask,
) -> TaskRuntimeState:
    """Return an updated task state with the terminal task applied.

    Terminal (completed/cancelled/failed) tasks are removed from the runtime
    list so that subsequent calls see a clean slate.
    """
    updated_list = [
        terminal_task if t.taskId == terminal_task.taskId else t
        for t in task_state.task_list
    ]
    remaining = [t for t in updated_list if t.status not in TERMINAL_TASK_STATUSES]
    next_task = remaining[0] if remaining else None
    return task_state.model_copy(
        update={
            "slot_memory": next_task.slot_memory if next_task is not None else {},
            "task_list": remaining,
            "current_task": next_task,
        },
        deep=True,
    )
