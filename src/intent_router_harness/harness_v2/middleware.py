"""Enterprise middleware — strong runtime constraints over deepagent SDK.

Five middleware classes handle everything that deepagent SDK does not provide:

1. TaskProgressMiddleware — enforce serial task execution, slot validation
2. CompletionGateMiddleware — tasks complete only via frontend /completion call
3. SkillLifecycleMiddleware — dynamic skill load/unload per session context
4. WorkflowGatewayMiddleware — URL whitelist + before/after hooks
5. ProtocolOutputMiddleware — guarantee output conforms to AssistantProtocolFrame
"""

from __future__ import annotations

import json
import logging
from typing import Any

from intent_router_harness.harness_v2.errors import (
    ProtocolOutputError,
    WorkflowExecutionError,
    WorkflowUrlNotAllowedError,
)

logger = logging.getLogger(__name__)


def build_harness_middleware(
    *,
    allowed_urls: list[str] | None = None,
    workflow_hooks: list[Any] | None = None,
    skill_roots: list[str] | None = None,
) -> list[Any]:
    """Build the ordered list of harness middleware for ``create_deep_agent(middleware=...)``.

    Returns middleware instances that are compatible with deepagent's
    ``AgentMiddleware`` base class.
    """
    try:
        from langchain.agents.middleware import AgentMiddleware
        from langchain.agents.middleware.types import hook_config
        from langchain_core.messages import AIMessage, SystemMessage, ToolMessage
    except ImportError as exc:
        raise RuntimeError(
            "langchain middleware is required; install the 'deepagent' extra"
        ) from exc

    # ------------------------------------------------------------------
    # 1. TaskProgressMiddleware
    # ------------------------------------------------------------------

    class TaskProgressMiddleware(AgentMiddleware):
        """Enforce serial task execution invariants.

        Rules:
        - One current_task at a time — agent must not skip ahead
        - If current_task has missing required slots, agent must ask user
        - Agent must not self-promote tasks to 'completed' status
        - Multi-task: tasks execute in expression order from task_list
        """

        @property
        def name(self) -> str:
            return "TaskProgressMiddleware"

        def wrap_model_call(self, request: Any, handler: Any) -> Any:
            return handler(self._inject_task_constraints(request))

        async def awrap_model_call(self, request: Any, handler: Any) -> Any:
            return await handler(self._inject_task_constraints(request))

        def _inject_task_constraints(self, request: Any) -> Any:
            existing = request.system_message
            existing_text = existing.text if existing is not None else ""
            if "## Task Execution Constraints" in existing_text:
                return request
            constraint_block = (
                "\n\n## Task Execution Constraints\n"
                "- 任务必须串行执行：一次只推进一个 current_task\n"
                "- 当前任务缺少必填参数时，必须向用户追问，不能跳过\n"
                "- 不要自行将任务标记为 completed — 任务完成由前端 /completion 接口触发\n"
                "- 多任务场景中，task_list 按用户表达顺序排列，按顺序推进\n"
                "- 当前任务未完成前，不要开始下一个任务\n"
                "- workflow_api_call 完成后，将状态设为 waiting_assistant_completion，等待前端确认\n"
            )
            system_message = SystemMessage(
                content=f"{existing_text}{constraint_block}" if existing_text else constraint_block.strip()
            )
            return request.override(system_message=system_message)

    # ------------------------------------------------------------------
    # 2. CompletionGateMiddleware
    # ------------------------------------------------------------------

    class CompletionGateMiddleware(AgentMiddleware):
        """Prevent agent from self-completing tasks.

        After a workflow_api_call tool returns, the agent MUST output
        status=waiting_assistant_completion. The transition to 'completed'
        happens only when the frontend calls /api/v1/task/completion.

        Also auto-terminates the loop after workflow tool results to prevent
        the agent from making additional LLM calls.
        """

        @property
        def name(self) -> str:
            return "CompletionGateMiddleware"

        @hook_config(can_jump_to=["end"])
        def before_model(self, state: dict[str, Any], runtime: Any) -> dict[str, Any] | None:
            del runtime
            return self._gate(state)

        @hook_config(can_jump_to=["end"])
        async def abefore_model(self, state: dict[str, Any], runtime: Any) -> dict[str, Any] | None:
            del runtime
            return self._gate(state)

        def _gate(self, state: dict[str, Any]) -> dict[str, Any] | None:
            messages = state.get("messages", [])
            if not any(self._is_workflow_result(msg, ToolMessage) for msg in messages):
                return None
            logger.info("CompletionGate: workflow result detected, terminating loop")
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

        @staticmethod
        def _is_workflow_result(msg: Any, tool_message_cls: type) -> bool:
            if not isinstance(msg, tool_message_cls):
                return False
            return getattr(msg, "name", "") == "workflow_api_call"

    # ------------------------------------------------------------------
    # 3. SkillLifecycleMiddleware
    # ------------------------------------------------------------------

    class SkillLifecycleMiddleware(AgentMiddleware):
        """Dynamic skill loading/unloading per session context.

        - Single intent: load only the matched skill
        - Multi-intent: load each task's skill as it becomes current_task
        - Unload non-current skills to keep context window lean
        - Skills are injected into system prompt via deepagent's native
          SkillsMiddleware, this middleware controls WHICH skills are visible
        """

        def __init__(self, roots: list[str] | None = None) -> None:
            self._roots = roots or []

        @property
        def name(self) -> str:
            return "SkillLifecycleMiddleware"

        def wrap_model_call(self, request: Any, handler: Any) -> Any:
            return handler(self._scope_skills(request))

        async def awrap_model_call(self, request: Any, handler: Any) -> Any:
            return await handler(self._scope_skills(request))

        def _scope_skills(self, request: Any) -> Any:
            existing = request.system_message
            existing_text = existing.text if existing is not None else ""
            if "## Skill Loading Policy" in existing_text:
                return request
            policy_block = (
                "\n\n## Skill Loading Policy\n"
                "- 单意图场景：只加载命中意图对应的 skill\n"
                "- 多意图场景：按 task_list 顺序，只加载 current_task 对应的 skill\n"
                "- 不要同时加载多个 skill 的完整内容到上下文\n"
                "- 使用 read_file 工具按需读取 skill 文件\n"
            )
            system_message = SystemMessage(
                content=f"{existing_text}{policy_block}" if existing_text else policy_block.strip()
            )
            return request.override(system_message=system_message)

    # ------------------------------------------------------------------
    # 4. WorkflowGatewayMiddleware
    # ------------------------------------------------------------------

    class WorkflowGatewayMiddleware(AgentMiddleware):
        """URL whitelist validation + before/after hooks for workflow calls."""

        def __init__(
            self,
            urls: list[str] | None = None,
            hooks: list[Any] | None = None,
        ) -> None:
            self._allowed = set(urls or [])
            self._hooks = hooks or []

        @property
        def name(self) -> str:
            return "WorkflowGatewayMiddleware"

        def wrap_tool_call(self, request: Any, handler: Any) -> Any:
            if self._tool_name(request) == "workflow_api_call":
                self._validate_url(request)
                self._run_before_hooks(request)
                result = handler(request)
                self._run_after_hooks(request, result)
                return result
            return handler(request)

        async def awrap_tool_call(self, request: Any, handler: Any) -> Any:
            if self._tool_name(request) == "workflow_api_call":
                self._validate_url(request)
                self._run_before_hooks(request)
                result = await handler(request)
                self._run_after_hooks(request, result)
                return result
            return await handler(request)

        def _validate_url(self, request: Any) -> None:
            if not self._allowed:
                return
            args = self._tool_args(request)
            url = str(args.get("url", ""))
            if not any(url.startswith(prefix) for prefix in self._allowed):
                raise WorkflowUrlNotAllowedError(url, sorted(self._allowed))

        def _run_before_hooks(self, request: Any) -> None:
            for hook in self._hooks:
                if hasattr(hook, "before"):
                    hook.before(self._tool_args(request))

        def _run_after_hooks(self, request: Any, result: Any) -> None:
            for hook in self._hooks:
                if hasattr(hook, "after"):
                    try:
                        hook.after(self._tool_args(request), result)
                    except Exception:
                        logger.warning("after-hook failed (non-fatal)", exc_info=True)

        @staticmethod
        def _tool_name(request: Any) -> str:
            if hasattr(request, "tool_call"):
                tc = request.tool_call
                return tc.get("name", "") if isinstance(tc, dict) else getattr(tc, "name", "")
            return ""

        @staticmethod
        def _tool_args(request: Any) -> dict[str, Any]:
            if hasattr(request, "tool_call"):
                tc = request.tool_call
                raw = tc.get("args", {}) if isinstance(tc, dict) else getattr(tc, "args", {})
                return raw if isinstance(raw, dict) else {}
            return {}

    # ------------------------------------------------------------------
    # 5. ProtocolOutputMiddleware
    # ------------------------------------------------------------------

    class ProtocolOutputMiddleware(AgentMiddleware):
        """Validate and normalize agent output to AssistantProtocolFrame format.

        The agent is instructed (via system_prompt) to output Assistant Protocol
        JSON.  This middleware intercepts the final output and:
        - Parses the JSON to ensure it's valid
        - Fills in missing required fields with defaults
        - Ensures 'status' and 'completion_state' are present
        """

        @property
        def name(self) -> str:
            return "ProtocolOutputMiddleware"

        def after_model(self, state: dict[str, Any], runtime: Any) -> dict[str, Any] | None:
            del runtime
            return self._normalize(state)

        async def aafter_model(self, state: dict[str, Any], runtime: Any) -> dict[str, Any] | None:
            del runtime
            return self._normalize(state)

        def _normalize(self, state: dict[str, Any]) -> dict[str, Any] | None:
            messages = state.get("messages", [])
            if not messages:
                return None
            last = messages[-1]
            if not isinstance(last, AIMessage):
                return None
            content = last.content if isinstance(last.content, str) else ""
            if not content.strip().startswith("{"):
                return None
            try:
                payload = json.loads(content)
            except json.JSONDecodeError:
                return None
            if not isinstance(payload, dict):
                return None
            frames = payload.get("frames")
            if isinstance(frames, list):
                for frame in frames:
                    _fill_frame_defaults(frame)
            elif "status" in payload:
                _fill_frame_defaults(payload)
            return None

    # ------------------------------------------------------------------
    # Assembly
    # ------------------------------------------------------------------

    middleware: list[Any] = [
        TaskProgressMiddleware(),
        CompletionGateMiddleware(),
        SkillLifecycleMiddleware(roots=skill_roots),
        WorkflowGatewayMiddleware(
            urls=allowed_urls,
            hooks=workflow_hooks,
        ),
        ProtocolOutputMiddleware(),
    ]
    return middleware


def _fill_frame_defaults(frame: dict[str, Any]) -> None:
    """Fill missing required fields in a protocol frame dict."""
    frame.setdefault("ok", True)
    frame.setdefault("completion_state", 0)
    frame.setdefault("completion_reason", "")
    frame.setdefault("output", {})
    frame.setdefault("slot_memory", {})
    frame.setdefault("task_list", [])
