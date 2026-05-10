"""LangGraph middleware for enterprise harness runtime controls."""

from __future__ import annotations

import json
import logging
from typing import Any

from intent_router_harness.deepagent.errors import DeepAgentRuntimeError
from intent_router_harness.deepagent.helpers import (
    _harness_context_block,
    _harness_virtual_files,
    _is_workflow_tool_message,
    _normalize_virtual_file_path,
    _tool_call_args,
    _tool_call_id,
    _tool_call_name,
    _workflow_request_from_messages,
)
from intent_router_harness.runtime import PromptHarness

logger = logging.getLogger(__name__)


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
