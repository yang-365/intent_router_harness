"""Agent builder — one-time construction of the deepagent compiled graph."""

from __future__ import annotations

import logging
import os
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
      "slot_memory": {}
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
- 任务规划使用 write_todos 工具管理，不要在 JSON 输出中包含 task_list 或 current_task
"""


def _resolve_model(model_spec: str | None) -> Any:
    """Resolve a model string to a ``BaseChatModel`` instance.

    Reads ``ROUTER_LLM_*`` environment variables to configure the LLM
    provider.  These take precedence over TOML config where applicable:

    * ``ROUTER_LLM_API_BASE_URL`` — LLM API base URL
    * ``ROUTER_LLM_API_KEY`` — LLM API key
    * ``ROUTER_LLM_MODEL`` — model name (overrides TOML ``[deepagent].model``)
    * ``ROUTER_LLM_TEMPERATURE`` — sampling temperature (default: not set)
    * ``ROUTER_LLM_TIMEOUT_SECONDS`` — request timeout in seconds
    * ``ROUTER_LLM_ENABLE_THINKING`` — enable thinking/reasoning mode

    For OpenAI-compatible providers the Responses API is disabled because
    most third-party providers (SiliconFlow, DashScope, DeepSeek …) do not
    support it.
    """
    env_base_url = os.environ.get("ROUTER_LLM_API_BASE_URL")
    env_api_key = os.environ.get("ROUTER_LLM_API_KEY")
    env_model = os.environ.get("ROUTER_LLM_MODEL")
    env_temperature = os.environ.get("ROUTER_LLM_TEMPERATURE")
    env_timeout = os.environ.get("ROUTER_LLM_TIMEOUT_SECONDS")
    env_thinking = os.environ.get("ROUTER_LLM_ENABLE_THINKING")

    # Model name: env var overrides TOML spec.
    effective_model = env_model or (model_spec.split(":", 1)[1] if model_spec and ":" in model_spec else None)
    if not effective_model and not model_spec:
        return model_spec

    try:
        from langchain_openai import ChatOpenAI
    except ImportError:
        return model_spec

    kwargs: dict[str, Any] = {
        "model": effective_model or model_spec,
        "use_responses_api": False,
    }
    if env_base_url:
        kwargs["base_url"] = env_base_url
    if env_api_key:
        kwargs["api_key"] = env_api_key
    if env_temperature is not None:
        kwargs["temperature"] = float(env_temperature)
    if env_timeout is not None:
        kwargs["request_timeout"] = float(env_timeout)
    if env_thinking is not None and env_thinking.lower() in ("true", "1", "yes"):
        kwargs["model_kwargs"] = {"enable_thinking": True}

    logger.info(
        "resolving LLM model=%s base_url=%s thinking=%s",
        kwargs.get("model"),
        kwargs.get("base_url", "(default)"),
        env_thinking,
    )
    return ChatOpenAI(**kwargs)


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
    elif config.project_root:
        # Auto-use FilesystemBackend when project root is detected so that
        # deepagent's SkillsMiddleware can read SKILL.md files from disk.
        backend = FilesystemBackend(root_dir=config.project_root)
        logger.info("using FilesystemBackend root=%s", config.project_root)
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
