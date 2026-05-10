"""Enterprise middleware — strong runtime constraints over deepagent SDK.

Seven middleware classes handle everything that deepagent SDK does not provide:

1. TaskProgressMiddleware — enforce serial task execution, slot validation
2. FrontendContextMiddleware — inject recommendTask / currentDisplay into LLM context
3. CompletionGateMiddleware — tasks complete only via frontend /completion call
4. SkillLifecycleMiddleware — progressive skill load/unload per session context
5. SkillFileMiddleware — intercept read_file for virtual skill/reference paths
6. WorkflowGatewayMiddleware — URL whitelist + before/after hooks
7. ProtocolOutputMiddleware — guarantee output conforms to AssistantProtocolFrame

All business logic lives in skill files — middleware is business-agnostic.
"""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)


def build_harness_middleware(
    *,
    allowed_urls: list[str] | None = None,
    workflow_hooks: list[Any] | None = None,
    skill_registry: Any | None = None,
) -> list[Any]:
    """Build the ordered list of harness middleware for ``create_deep_agent(middleware=...)``.

    Args:
        allowed_urls: URL prefixes for workflow whitelist.
        workflow_hooks: Before/after hooks for workflow tool calls.
        skill_registry: A ``SkillRegistry`` instance for progressive skill loading.

    Returns:
        Middleware instances compatible with deepagent's ``AgentMiddleware``.
    """
    try:
        from langchain.agents.middleware import AgentMiddleware
        from langchain.agents.middleware.types import hook_config
        from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
    except ImportError as exc:
        raise RuntimeError(
            "langchain middleware is required; install the 'deepagent' extra"
        ) from exc

    # ------------------------------------------------------------------
    # 1. TaskProgressMiddleware
    # ------------------------------------------------------------------

    class TaskProgressMiddleware(AgentMiddleware):
        """Enforce serial task execution invariants.

        Rules injected into system prompt (business-agnostic):
        - One current_task at a time
        - Missing required slots → must ask user
        - Never self-promote tasks to 'completed'
        - Multi-task: execute in expression order
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
    # 2. FrontendContextMiddleware
    # ------------------------------------------------------------------

    class FrontendContextMiddleware(AgentMiddleware):
        """Inject frontend context (recommendTask, currentDisplay) into system prompt.

        Reads these fields from the latest HumanMessage JSON and, when non-empty,
        appends a context block to the system message so the LLM can use them
        for intent recognition and task planning.
        """

        @property
        def name(self) -> str:
            return "FrontendContextMiddleware"

        def wrap_model_call(self, request: Any, handler: Any) -> Any:
            return handler(self._inject_frontend_context(request))

        async def awrap_model_call(self, request: Any, handler: Any) -> Any:
            return await handler(self._inject_frontend_context(request))

        def _inject_frontend_context(self, request: Any) -> Any:
            existing = request.system_message
            existing_text = existing.text if existing is not None else ""
            if "## Frontend Context" in existing_text:
                return request

            messages = getattr(request, "messages", None) or []
            recommend_task, current_display = self._extract_context(messages)
            if not recommend_task and not current_display:
                return request

            sections: list[str] = []
            if current_display:
                sections.append(
                    "当前掌银页面展示卡片 (currentDisplay):\n"
                    f"{json.dumps(current_display, ensure_ascii=False)}"
                )
            if recommend_task:
                sections.append(
                    "推荐任务 (recommendTask):\n"
                    f"{json.dumps(recommend_task, ensure_ascii=False)}"
                )
            sections.append("请结合以上前端上下文进行意图识别和任务规划。")

            context_block = "\n\n## Frontend Context\n" + "\n\n".join(sections)
            system_message = SystemMessage(
                content=f"{existing_text}{context_block}" if existing_text else context_block.strip()
            )
            return request.override(system_message=system_message)

        @staticmethod
        def _extract_context(
            messages: list[Any],
        ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
            """Return (recommendTask, currentDisplay) from the latest HumanMessage."""
            for msg in reversed(messages):
                if not isinstance(msg, HumanMessage):
                    continue
                content = msg.content if isinstance(msg.content, str) else ""
                if not content.strip().startswith("{"):
                    continue
                try:
                    payload = json.loads(content)
                except (json.JSONDecodeError, TypeError):
                    continue
                if isinstance(payload, dict):
                    rt = payload.get("recommendTask") or []
                    cd = payload.get("currentDisplay") or []
                    return (
                        rt if isinstance(rt, list) else [],
                        cd if isinstance(cd, list) else [],
                    )
            return [], []

    # ------------------------------------------------------------------
    # 3. CompletionGateMiddleware
    # ------------------------------------------------------------------

    class CompletionGateMiddleware(AgentMiddleware):
        """Prevent agent from self-completing tasks.

        After a workflow_api_call tool returns, auto-terminate the loop
        with status=waiting_assistant_completion.  The transition to
        'completed' only happens via frontend /api/v1/task/completion.
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
    # 4. SkillLifecycleMiddleware — progressive load/unload
    # ------------------------------------------------------------------

    class SkillLifecycleMiddleware(AgentMiddleware):
        """Progressive skill body loading/unloading per session context.

        Skill name/description injection for intent recognition is handled
        natively by deepagent SDK (via ``create_deep_agent(skills=...)``).

        This middleware adds progressive body management:

        Body loading:
            - When agent identifies intent (intent_code in JSON output) →
              inject the matched skill's SKILL.md body into context
            - Reference summaries (id + purpose) listed so agent knows
              what's available via read_file

        Reference on-demand:
            - Agent uses read_file to load specific references
              (slot_filling.md, workflow_request.md)
            - SkillFileMiddleware intercepts these reads

        Unload: when task completes (completion_state=1 or agent moves
        to next task), the skill body is NOT re-injected.
        """

        def __init__(self, registry: Any | None = None) -> None:
            self._registry = registry
            self._loaded_skill: str | None = None

        @property
        def name(self) -> str:
            return "SkillLifecycleMiddleware"

        def wrap_model_call(self, request: Any, handler: Any) -> Any:
            return handler(self._inject_skill_body(request))

        async def awrap_model_call(self, request: Any, handler: Any) -> Any:
            return await handler(self._inject_skill_body(request))

        def _inject_skill_body(self, request: Any) -> Any:
            if self._registry is None:
                return request

            messages = getattr(request, "messages", None) or []
            target_skill = self._detect_target_skill(messages)

            # First model call (no prior AI messages): eagerly inject all
            # skill bodies + references so the LLM has full context to
            # identify intent AND call workflow_api_call in a single pass.
            has_ai_history = any(isinstance(m, AIMessage) for m in messages)
            if not has_ai_history and not self._loaded_skill:
                return self._inject_all_skills(request)

            if not target_skill or target_skill == self._loaded_skill:
                return request

            if self._loaded_skill:
                logger.info(
                    "SkillLifecycle: unloading skill=%s, loading skill=%s",
                    self._loaded_skill,
                    target_skill,
                )
            else:
                logger.info("SkillLifecycle: loading skill=%s", target_skill)
            self._loaded_skill = target_skill
            return self._inject_single_skill(request, target_skill)

        def _inject_all_skills(self, request: Any) -> Any:
            """Inject all skill bodies + references for first-turn context."""
            skill_blocks: list[str] = []
            for name in self._registry.names():
                block = self._build_skill_block(name)
                if block:
                    skill_blocks.append(block)
            if not skill_blocks:
                return request
            logger.info("SkillLifecycle: eagerly loading all %d skills", len(skill_blocks))
            combined = "\n".join(skill_blocks)
            existing = request.system_message
            existing_text = existing.text if existing is not None else ""
            system_message = SystemMessage(
                content=f"{existing_text}{combined}" if existing_text else combined.strip()
            )
            return request.override(system_message=system_message)

        def _inject_single_skill(self, request: Any, skill_name: str) -> Any:
            """Inject a single skill body + references."""
            skill_block = self._build_skill_block(skill_name)
            if not skill_block:
                return request
            existing = request.system_message
            existing_text = existing.text if existing is not None else ""
            system_message = SystemMessage(
                content=f"{existing_text}{skill_block}" if existing_text else skill_block.strip()
            )
            return request.override(system_message=system_message)

        def _build_skill_block(self, skill_name: str) -> str | None:
            """Build the full skill context block with body + references."""
            skill_body = self._registry.load_skill_body(skill_name)
            if not skill_body:
                return None
            meta = self._registry.get_meta(skill_name)
            ref_sections = ""
            if meta and meta.references:
                ref_parts: list[str] = []
                for ref in meta.references:
                    ref_body = self._registry.load_reference(skill_name, ref.id)
                    if ref_body:
                        ref_parts.append(
                            f"\n### Reference: {ref.id} — {ref.purpose}\n\n{ref_body}"
                        )
                if ref_parts:
                    ref_sections = "\n".join(ref_parts)
            return (
                f"\n\n## Loaded Skill: {skill_name}\n\n"
                f"{skill_body}"
                f"{ref_sections}"
            )

        def _detect_target_skill(self, messages: list[Any]) -> str | None:
            """Scan recent messages for intent_code or skill name signals.

            Business-agnostic: reads structured JSON from agent output,
            does not hard-code any intent codes.
            """
            for msg in reversed(messages):
                content = getattr(msg, "content", "")
                if not isinstance(content, str) or not content.strip().startswith("{"):
                    continue
                try:
                    payload = json.loads(content)
                except json.JSONDecodeError:
                    continue
                skill_name = self._extract_skill_from_payload(payload)
                if skill_name:
                    return skill_name
            return None

        def _extract_skill_from_payload(self, payload: dict[str, Any]) -> str | None:
            """Extract skill name from protocol JSON — business-agnostic."""
            # Check frames[].intent_code
            frames = payload.get("frames", [])
            if isinstance(frames, list):
                for frame in frames:
                    if not isinstance(frame, dict):
                        continue
                    intent_code = frame.get("intent_code")
                    if intent_code:
                        meta = self._registry.find_by_intent(str(intent_code))
                        if meta:
                            return meta.name
                    # Check current_task.intent_code
                    current_task = frame.get("current_task")
                    if isinstance(current_task, dict):
                        task_intent = current_task.get("intent_code")
                        if task_intent:
                            meta = self._registry.find_by_intent(str(task_intent))
                            if meta:
                                return meta.name

            # Check top-level intent_code
            top_intent = payload.get("intent_code")
            if top_intent:
                meta = self._registry.find_by_intent(str(top_intent))
                if meta:
                    return meta.name

            # Check current_task at top level
            current_task = payload.get("current_task")
            if isinstance(current_task, dict):
                task_intent = current_task.get("intent_code")
                if task_intent:
                    meta = self._registry.find_by_intent(str(task_intent))
                    if meta:
                        return meta.name

            return None

    # ------------------------------------------------------------------
    # 5. SkillFileMiddleware — virtual file reads
    # ------------------------------------------------------------------

    class SkillFileMiddleware(AgentMiddleware):
        """Intercept read_file tool calls for virtual skill/reference paths.

        When the agent calls read_file("/skills/<dir>/SKILL.md") or
        read_file("/skills/<dir>/references/slot_filling.md"), this
        middleware loads the content from the registry instead of the
        filesystem — keeping skill files as virtual overlays.
        """

        def __init__(self, registry: Any | None = None) -> None:
            self._registry = registry

        @property
        def name(self) -> str:
            return "SkillFileMiddleware"

        def wrap_tool_call(self, request: Any, handler: Any) -> Any:
            result = self._maybe_read_skill_file(request)
            if result is not None:
                return result
            return handler(request)

        async def awrap_tool_call(self, request: Any, handler: Any) -> Any:
            result = self._maybe_read_skill_file(request)
            if result is not None:
                return result
            return await handler(request)

        def _maybe_read_skill_file(self, request: Any) -> Any | None:
            if self._registry is None:
                return None
            tool_name = _tool_name(request)
            if tool_name != "read_file":
                return None
            args = _tool_args(request)
            raw_path = str(
                args.get("file_path")
                or args.get("path")
                or args.get("filename")
                or args.get("name")
                or ""
            ).strip()
            normalized = _normalize_virtual_path(raw_path)
            if not normalized.startswith("/skills/"):
                return None

            content = self._resolve_virtual_path(normalized)
            if content is not None:
                return ToolMessage(
                    content=content,
                    tool_call_id=_tool_call_id(request),
                    name="read_file",
                )

            # Path is under /skills/ but not found — list available files
            all_paths: list[str] = []
            for skill_name in self._registry.names():
                listing = self._registry.virtual_file_listing(skill_name)
                all_paths.extend(listing.keys())

            return ToolMessage(
                content=(
                    f"File not found: {normalized}\n"
                    "Available skill files:\n"
                    + "\n".join(f"- {p}" for p in sorted(all_paths))
                ),
                tool_call_id=_tool_call_id(request),
                name="read_file",
            )

        def _resolve_virtual_path(self, path: str) -> str | None:
            """Resolve a virtual path like /skills/transfer-routing/SKILL.md."""
            parts = path.strip("/").split("/")
            if len(parts) < 3:
                return None
            # parts: ["skills", "<dir>", "SKILL.md"] or
            #         ["skills", "<dir>", "references", "<file>.md"]
            skill_dir_name = parts[1]
            file_part = "/".join(parts[2:])

            # Find skill by directory name
            for name in self._registry.names():
                meta = self._registry.get_meta(name)
                if meta is None:
                    continue
                if meta.skill_dir.name != skill_dir_name:
                    continue

                if file_part == "SKILL.md":
                    return self._registry.load_skill_body(name)

                # Check references
                for ref in meta.references:
                    if ref.relative_path == file_part:
                        return self._registry.load_reference(name, ref.id)

            return None

    # ------------------------------------------------------------------
    # 6. WorkflowGatewayMiddleware
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
            if _tool_name(request) == "workflow_api_call":
                rejection = self._check_url(request)
                if rejection is not None:
                    return rejection
                self._run_before_hooks(request)
                result = handler(request)
                self._run_after_hooks(request, result)
                return result
            return handler(request)

        async def awrap_tool_call(self, request: Any, handler: Any) -> Any:
            if _tool_name(request) == "workflow_api_call":
                rejection = self._check_url(request)
                if rejection is not None:
                    return rejection
                self._run_before_hooks(request)
                result = await handler(request)
                self._run_after_hooks(request, result)
                return result
            return await handler(request)

        def _check_url(self, request: Any) -> Any | None:
            """Return a ToolMessage rejection if the URL is not allowed, else None."""
            if not self._allowed:
                return None
            args = _tool_args(request)
            url = str(args.get("url", ""))
            if any(url.startswith(prefix) for prefix in self._allowed):
                return None
            allowed_list = sorted(self._allowed)
            logger.warning("workflow URL rejected: %s (allowed: %s)", url, allowed_list)
            return ToolMessage(
                content=(
                    f"Error: URL '{url}' is not in the allowed list.\n"
                    f"Allowed URLs: {allowed_list}\n"
                    "Please use one of the allowed URLs from the skill's workflow_request reference."
                ),
                tool_call_id=_tool_call_id(request),
                name="workflow_api_call",
            )

        def _run_before_hooks(self, request: Any) -> None:
            for hook in self._hooks:
                if hasattr(hook, "before"):
                    hook.before(_tool_args(request))

        def _run_after_hooks(self, request: Any, result: Any) -> None:
            for hook in self._hooks:
                if hasattr(hook, "after"):
                    try:
                        hook.after(_tool_args(request), result)
                    except Exception:
                        logger.warning("after-hook failed (non-fatal)", exc_info=True)

    # ------------------------------------------------------------------
    # 7. ProtocolOutputMiddleware
    # ------------------------------------------------------------------

    class ProtocolOutputMiddleware(AgentMiddleware):
        """Validate and normalize agent output to AssistantProtocolFrame format."""

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
        FrontendContextMiddleware(),
        CompletionGateMiddleware(),
        SkillLifecycleMiddleware(registry=skill_registry),
        SkillFileMiddleware(registry=skill_registry),
        WorkflowGatewayMiddleware(
            urls=allowed_urls,
            hooks=workflow_hooks,
        ),
        ProtocolOutputMiddleware(),
    ]
    return middleware


# ---------------------------------------------------------------------------
# Shared helpers (module-level, outside the factory)
# ---------------------------------------------------------------------------


def _tool_name(request: Any) -> str:
    """Extract tool name from a middleware request."""
    if hasattr(request, "tool_call"):
        tc = request.tool_call
        return tc.get("name", "") if isinstance(tc, dict) else getattr(tc, "name", "")
    return ""


def _tool_args(request: Any) -> dict[str, Any]:
    """Extract tool args from a middleware request."""
    if hasattr(request, "tool_call"):
        tc = request.tool_call
        raw = tc.get("args", {}) if isinstance(tc, dict) else getattr(tc, "args", {})
        return raw if isinstance(raw, dict) else {}
    return {}


def _tool_call_id(request: Any) -> str:
    """Extract tool_call_id from a middleware request."""
    if hasattr(request, "tool_call"):
        tc = request.tool_call
        return tc.get("id", "harness_tool_call") if isinstance(tc, dict) else getattr(tc, "id", "harness_tool_call")
    return "harness_tool_call"


def _normalize_virtual_path(path: str) -> str:
    """Normalize a virtual file path."""
    if not path:
        return ""
    normalized = path.replace("\\", "/").strip()
    if not normalized.startswith("/"):
        normalized = "/" + normalized
    while "//" in normalized:
        normalized = normalized.replace("//", "/")
    return normalized


def _fill_frame_defaults(frame: dict[str, Any]) -> None:
    """Fill missing required fields in a protocol frame dict."""
    frame.setdefault("ok", True)
    frame.setdefault("completion_state", 0)
    frame.setdefault("completion_reason", "")
    frame.setdefault("output", {})
    frame.setdefault("slot_memory", {})
    frame.setdefault("task_list", [])
