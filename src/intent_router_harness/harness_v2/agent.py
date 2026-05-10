"""Agent builder — one-time construction of the deepagent compiled graph."""

from __future__ import annotations

import logging
from typing import Any

from intent_router_harness.harness_v2.config import HarnessConfig
from intent_router_harness.harness_v2.middleware import build_harness_middleware
from intent_router_harness.harness_v2.skill_registry import SkillRegistry
from intent_router_harness.harness_v2.workflow import create_workflow_tool

logger = logging.getLogger(__name__)

# Protocol frame format instructions appended to every system prompt so the
# agent outputs structured JSON that ProtocolOutputMiddleware can validate.
_PROTOCOL_SUFFIX = """

## Output Format (Assistant Protocol)

你的每次回复都必须是一个 JSON 对象，格式如下：

```json
{
  "frames": [
    {
      "ok": true,
      "status": "<status>",
      "completion_state": <0|1|2>,
      "completion_reason": "<reason>",
      "message": "<给用户的文字回复>",
      "output": {},
      "slot_memory": {},
      "task_list": [],
      "current_task": null
    }
  ]
}
```

status 取值：
- "running" — 正在处理中
- "waiting_user_input" — 需要用户提供信息（缺少必填参数时使用）
- "ready_for_dispatch" — 已收集完参数，准备执行 workflow
- "waiting_assistant_completion" — workflow 已执行，等待前端确认
- "failed" — 处理失败

completion_state 取值：
- 0 — 未完成
- 1 — 单步完成（等待前端确认）
- 2 — 全部完成

重要规则：
- 只在 workflow_api_call 返回结果后才设置 completion_state=1
- 不要自行设置 completion_state=2，这由前端 /completion 接口触发
- message 字段是给用户看的文字，务必用自然语言
"""


def build_agent(
    config: HarnessConfig,
    *,
    workflow_hooks: list[Any] | None = None,
    skill_registry: SkillRegistry | None = None,
) -> Any:
    """Construct a compiled deepagent graph from harness config.

    Args:
        config: Resolved harness configuration.
        workflow_hooks: Optional before/after hooks for workflow tool calls.
        skill_registry: Pre-built skill registry for progressive loading.

    Returns:
        A compiled LangGraph ``StateGraph`` ready for ``.invoke()`` / ``.astream()``.
    """
    try:
        from deepagents import create_deep_agent
        from deepagents.backends import FilesystemBackend, StateBackend
        from langchain.agents.middleware import ModelCallLimitMiddleware
        from langgraph.checkpoint.memory import MemorySaver
    except ImportError as exc:
        raise RuntimeError(
            "deepagents SDK is required; install the 'deepagent' extra"
        ) from exc

    backend: Any
    if config.backend == "filesystem" and config.backend_root:
        backend = FilesystemBackend(root_dir=config.backend_root)
    else:
        backend = StateBackend()

    system_prompt = config.system_prompt + _PROTOCOL_SUFFIX if config.system_prompt else _PROTOCOL_SUFFIX.strip()

    if skill_registry is None:
        skill_registry = SkillRegistry.from_roots(config.skill_roots)

    harness_mw = build_harness_middleware(
        allowed_urls=config.workflow_allowed_urls,
        workflow_hooks=workflow_hooks,
        skill_registry=skill_registry,
    )
    harness_mw.append(
        ModelCallLimitMiddleware(
            run_limit=max(4, min(config.max_iterations, 30)),
            exit_behavior="error",
        )
    )

    workflow_tool = create_workflow_tool()

    agent = create_deep_agent(
        model=config.model,
        tools=[workflow_tool],
        system_prompt=system_prompt,
        middleware=harness_mw,
        # Skills are managed by SkillLifecycleMiddleware, not deepagent native
        skills=None,
        memory=config.memory_sources if config.memory_sources else None,
        backend=backend,
        checkpointer=MemorySaver(),
    )
    logger.info(
        "agent built model=%s skills=%s memory=%s middleware_count=%d",
        config.model,
        config.skill_sources,
        config.memory_sources,
        len(harness_mw),
    )
    return agent
