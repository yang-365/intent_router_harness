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

Execution order for the two critical harness flows:

  Task flow:  intent identification → task planning → serial execution →
              workflow call → wait for frontend /completion → next task

  Skill flow: metadata summary (first call) → intent detected →
              load skill body + references → slot extraction (guided by
              slot_filling.md) → workflow call (guided by workflow_request.md)
              → result → unload skill
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from intent_router_harness.harness_v2.protocol import emit_trace, emit_trace_once

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
                "- 使用 write_todos 工具规划任务：每个用户业务意图对应一个 todo 项\n"
                "- todo 的 content 必须以 `[intent_code]` 开头，后跟任务描述。\n"
                "  例如：`[AG_TRANS] 给张三转账3000元`、`[AG_PAY_BILL] 缴电费200元`\n"
                "  intent_code 必须来自 Available Skills 中列出的 intent_codes\n"
                "- todo 只记录业务意图级别的任务，"
                "不要将意图识别、提槽、workflow 调用等内部执行步骤拆成 todo\n"
                "- 单个意图时也需要创建一个 todo 项，标记为 in_progress\n"
                "- 任务必须串行执行：一次只将一个 todo 标记为 in_progress\n"
                "- 当前任务缺少必填参数时，必须向用户追问，不能跳过\n"
                "- 不要自行将任务标记为 completed — 任务完成由前端 /completion 接口触发\n"
                "- 多任务场景中，按用户表达顺序在 write_todos 中排列，按顺序推进\n"
                "- 当前任务未完成前，不要开始下一个任务\n"
                "- workflow_api_call 完成后，将状态设为 waiting_assistant_completion，等待前端确认\n"
                "- write_todos 和 workflow_api_call 不能在同一轮并行调用，必须分步执行\n"
            )
            emit_trace_once(
                "task_constraints_injected",
                "任务执行约束注入",
                "串行执行、缺槽追问、前端确认完成",
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

            emit_trace_once(
                "frontend_context_injected",
                "前端上下文注入",
                f"recommendTask={len(recommend_task)}项, currentDisplay={len(current_display)}项",
                recommend_task=recommend_task,
                current_display=current_display,
            )
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
            workflow_msg = None
            for msg in reversed(messages):
                if self._is_workflow_result(msg, ToolMessage):
                    workflow_msg = msg
                    break
            if workflow_msg is None:
                return None
            logger.info("CompletionGate: workflow result detected, terminating loop")
            output = self._extract_workflow_output(workflow_msg)
            emit_trace(
                "completion_gate_triggered",
                "任务完成门控触发",
                "检测到workflow返回结果，终止agent循环，等待前端确认",
            )
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
                                        "output": output,
                                    }
                                ]
                            },
                            ensure_ascii=False,
                        )
                    )
                ],
            }

        @staticmethod
        def _extract_workflow_output(msg: Any) -> Any:
            """Extract usable output from a workflow ToolMessage.

            Tries to parse the ToolMessage content as JSON and return
            the most meaningful result: node_output > output > raw parsed > raw string.
            """
            content = getattr(msg, "content", "")
            if not content:
                return {}
            try:
                parsed = json.loads(content)
            except (json.JSONDecodeError, TypeError):
                return {"raw": content}
            if isinstance(parsed, dict):
                if "node_output" in parsed:
                    return parsed["node_output"]
                if "output" in parsed:
                    return parsed["output"]
            return parsed

        @staticmethod
        def _is_workflow_result(msg: Any, tool_message_cls: type) -> bool:
            if not isinstance(msg, tool_message_cls):
                return False
            return getattr(msg, "name", "") == "workflow_api_call"

    # ------------------------------------------------------------------
    # 4. SkillLifecycleMiddleware — progressive load/unload
    # ------------------------------------------------------------------

    class SkillLifecycleMiddleware(AgentMiddleware):
        """Todo-driven skill loading/unloading.

        Loading and unloading are driven exclusively by ``AgentState.todos``:

          1. ``before_model``: check todos for in_progress item.  If found
             and no skill loaded, parse ``[INTENT_CODE]`` prefix from the
             todo's content and resolve via ``SkillRegistry.find_by_intent``.
          2. ``wrap_model_call``: if ``_loaded_skill`` is set, inject
             skill body + references; otherwise inject metadata only.
          3. ``unload_skill()``: called by ``/completion`` endpoint
             when a task is marked done — clears ``_loaded_skill``.

        No message scanning is performed.  The only trigger for skill
        loading is the ``[INTENT_CODE]`` prefix written by the LLM in
        ``write_todos`` content.
        """

        def __init__(self, registry: Any | None = None) -> None:
            self._registry = registry
            self._loaded_skill: str | None = None
            self._has_active_todo = False

        @property
        def name(self) -> str:
            return "SkillLifecycleMiddleware"

        def before_model(self, state: dict[str, Any], runtime: Any) -> dict[str, Any] | None:
            del runtime
            todos = state.get("todos") or []
            self._has_active_todo = any(t.get("status") == "in_progress" for t in todos)
            self._try_load_skill(todos)
            return None

        async def abefore_model(self, state: dict[str, Any], runtime: Any) -> dict[str, Any] | None:
            del runtime
            todos = state.get("todos") or []
            self._has_active_todo = any(t.get("status") == "in_progress" for t in todos)
            self._try_load_skill(todos)
            return None

        def _try_load_skill(self, todos: list[dict[str, Any]]) -> None:
            """Detect and load skill from [INTENT_CODE] prefix in in_progress todo."""
            if not self._has_active_todo or self._loaded_skill or not self._registry:
                return
            target = self._detect_skill_from_todos(todos)
            if target:
                logger.info("SkillLifecycle: loading skill=%s (before_model)", target)
                self._loaded_skill = target

        def wrap_model_call(self, request: Any, handler: Any) -> Any:
            return handler(self._prepare_skill_context(request))

        async def awrap_model_call(self, request: Any, handler: Any) -> Any:
            return await handler(self._prepare_skill_context(request))

        def unload_skill(self) -> None:
            """Explicitly unload current skill — called by /completion."""
            if self._loaded_skill:
                logger.info("SkillLifecycle: unloading skill=%s", self._loaded_skill)
                emit_trace(
                    "skill_unloaded",
                    "技能卸载",
                    f"卸载技能: {self._loaded_skill}",
                    skill_name=self._loaded_skill,
                )
                self._loaded_skill = None

        def _prepare_skill_context(self, request: Any) -> Any:
            """Inject skill context based on todo state."""
            if self._registry is None:
                return request

            # Skill loaded (by before_model) → inject body + references
            if self._loaded_skill:
                return self._inject_skill_with_references(request, self._loaded_skill)

            # No skill loaded → metadata only (for intent recognition + todo planning)
            return self._inject_metadata_summary(request)

        def _inject_metadata_summary(self, request: Any) -> Any:
            """Inject lightweight skill metadata for intent recognition only."""
            summary = self._registry.all_metadata_summary()
            if not summary:
                return request
            existing = request.system_message
            existing_text = existing.text if existing is not None else ""
            if "## Available Skills" in existing_text:
                return request
            emit_trace_once(
                "skill_metadata_injected",
                "技能元数据注入",
                f"注入 {len(self._registry.names())} 个技能摘要用于意图识别",
                skill_count=len(self._registry.names()),
                skill_names=list(self._registry.names()),
            )
            instruction = (
                "\n\n## Skill Loading Protocol\n"
                "不要手动调用 read_file 读取 SKILL.md 或 reference 文件 — "
                "系统会在你识别意图后自动将对应技能的完整内容和参考文件注入到上下文中。\n"
                "你只需根据上面的技能摘要识别用户意图并输出 intent_code，"
                "系统会自动加载对应技能的提槽规则和 workflow 地址。\n"
                "不要编造 workflow URL，必须使用系统注入的 reference 中的完整地址。"
            )
            system_message = SystemMessage(
                content=f"{existing_text}\n\n{summary}{instruction}" if existing_text else f"{summary}{instruction}"
            )
            return request.override(system_message=system_message)

        def _inject_skill_with_references(self, request: Any, skill_name: str) -> Any:
            """Inject skill body + ALL reference files inline.

            This is the critical step: references (slot_filling.md,
            workflow_request.md) are injected BEFORE the model runs so
            that slot extraction follows the business rules in references.
            """
            skill_block = self._build_skill_block(skill_name)
            if not skill_block:
                return request
            existing = request.system_message
            existing_text = existing.text if existing is not None else ""
            # Remove any previous skill block to avoid stacking
            if "\n\n## Loaded Skill:" in existing_text:
                idx = existing_text.index("\n\n## Loaded Skill:")
                existing_text = existing_text[:idx]
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

            emit_trace(
                "skill_loaded",
                "技能加载",
                f"加载技能: {skill_name}",
                skill_name=skill_name,
            )

            ref_sections = ""
            if meta and meta.references:
                ref_parts: list[str] = []
                loaded_refs: list[str] = []
                for ref in meta.references:
                    ref_body = self._registry.load_reference(skill_name, ref.id)
                    if ref_body:
                        ref_parts.append(
                            f"\n### Reference: {ref.id} — {ref.purpose}\n\n{ref_body}"
                        )
                        loaded_refs.append(f"{ref.id}({ref.purpose})")
                if ref_parts:
                    ref_sections = "\n".join(ref_parts)
                    emit_trace(
                        "skill_reference_loaded",
                        "技能参考文件加载",
                        f"加载 {len(ref_parts)} 个参考文件: {', '.join(loaded_refs)}",
                        skill_name=skill_name,
                        references=loaded_refs,
                    )
            return (
                f"\n\n## Loaded Skill: {skill_name}\n\n"
                f"{skill_body}"
                f"{ref_sections}"
            )

        _INTENT_PREFIX_RE = re.compile(r"^\[([A-Z][A-Z0-9_]+)\]\s*")

        def _detect_skill_from_todos(self, todos: list[dict[str, Any]]) -> str | None:
            """Parse [intent_code] prefix from the in_progress todo's content."""
            for todo in todos:
                if todo.get("status") != "in_progress":
                    continue
                content = todo.get("content", "")
                match = self._INTENT_PREFIX_RE.match(content)
                if match:
                    intent_code = match.group(1)
                    meta = self._registry.find_by_intent(intent_code)
                    if meta:
                        return meta.name
                # Fallback: scan todo content for any known intent_code
                for code in self._registry.intent_codes():
                    if code in content:
                        meta = self._registry.find_by_intent(code)
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
                emit_trace(
                    "skill_file_read",
                    "技能文件读取",
                    f"读取: {normalized}",
                    path=normalized,
                )
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
                args = _tool_args(request)
                emit_trace(
                    "workflow_call_started",
                    "Workflow API调用开始",
                    f"method={args.get('method', 'POST')} url={args.get('url', '')}",
                    method=args.get("method", "POST"),
                    url=args.get("url", ""),
                    body=args.get("body", {}),
                )
                self._run_before_hooks(request)
                result = handler(request)
                self._run_after_hooks(request, result)
                result_content = getattr(result, "content", "") if result else ""
                emit_trace(
                    "workflow_call_completed",
                    "Workflow API调用完成",
                    f"返回结果长度: {len(result_content)} 字符",
                    result_preview=result_content[:500] if result_content else "",
                )
                return result
            return handler(request)

        async def awrap_tool_call(self, request: Any, handler: Any) -> Any:
            if _tool_name(request) == "workflow_api_call":
                rejection = self._check_url(request)
                if rejection is not None:
                    return rejection
                args = _tool_args(request)
                emit_trace(
                    "workflow_call_started",
                    "Workflow API调用开始",
                    f"method={args.get('method', 'POST')} url={args.get('url', '')}",
                    method=args.get("method", "POST"),
                    url=args.get("url", ""),
                    body=args.get("body", {}),
                )
                self._run_before_hooks(request)
                result = await handler(request)
                self._run_after_hooks(request, result)
                result_content = getattr(result, "content", "") if result else ""
                emit_trace(
                    "workflow_call_completed",
                    "Workflow API调用完成",
                    f"返回结果长度: {len(result_content)} 字符",
                    result_preview=result_content[:500] if result_content else "",
                )
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
            emit_trace(
                "workflow_url_rejected",
                "Workflow URL白名单拒绝",
                f"URL不在白名单: {url}",
                rejected_url=url,
                allowed_urls=allowed_list,
            )
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
                    self._emit_frame_trace(frame)
            elif "status" in payload:
                _fill_frame_defaults(payload)
                self._emit_frame_trace(payload)
            return None

        @staticmethod
        def _emit_frame_trace(frame: dict[str, Any]) -> None:
            """Emit trace events for key fields in a protocol frame."""
            intent_code = frame.get("intent_code")
            if intent_code:
                emit_trace(
                    "intent_recognized",
                    "意图识别",
                    f"识别意图: {intent_code}",
                    intent_code=intent_code,
                )
            slot_memory = frame.get("slot_memory")
            if slot_memory and isinstance(slot_memory, dict) and slot_memory:
                emit_trace(
                    "slots_extracted",
                    "参数提取",
                    f"提取参数: {', '.join(f'{k}={v}' for k, v in slot_memory.items())}",
                    slot_memory=slot_memory,
                )
            status = frame.get("status")
            completion_state = frame.get("completion_state", 0)
            if status:
                emit_trace(
                    "protocol_frame_output",
                    "协议帧输出",
                    f"status={status}, completion_state={completion_state}",
                    status=status,
                    completion_state=completion_state,
                    message=frame.get("message", ""),
                )

    # ------------------------------------------------------------------
    # Assembly
    # ------------------------------------------------------------------

    skill_lifecycle = SkillLifecycleMiddleware(registry=skill_registry)
    middleware: list[Any] = [
        TaskProgressMiddleware(),
        FrontendContextMiddleware(),
        CompletionGateMiddleware(),
        skill_lifecycle,
        SkillFileMiddleware(registry=skill_registry),
        WorkflowGatewayMiddleware(
            urls=allowed_urls,
            hooks=workflow_hooks,
        ),
        ProtocolOutputMiddleware(),
    ]
    return middleware, skill_lifecycle


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
