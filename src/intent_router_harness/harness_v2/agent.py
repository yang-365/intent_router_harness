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


def _resolve_model(model_spec: str | None) -> Any:
    """Resolve a model string to a BaseChatModel instance.

    For OpenAI-compatible providers, disables the Responses API which
    many third-party providers (SiliconFlow, DeepSeek, etc.) do not support.

    Args:
        model_spec: Model string like ``"openai:model-name"`` or ``None``.

    Returns:
        A configured ``BaseChatModel`` instance, or the string as-is if
        no special handling is needed.
    """
    if not model_spec:
        return model_spec
    try:
        from langchain.chat_models import init_chat_model
    except ImportError:
        return model_spec
    if model_spec.startswith("openai:"):
        return init_chat_model(model_spec, use_responses_api=False)
    return model_spec


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

    resolved_model = _resolve_model(config.model)

    agent = create_deep_agent(
        model=resolved_model,
        tools=[workflow_tool],
        system_prompt=system_prompt,
        middleware=harness_mw,
        # deepagent natively loads skill name/description for intent recognition;
        # SkillLifecycleMiddleware handles progressive body loading/unloading.
        skills=config.skill_sources if config.skill_sources else None,
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
