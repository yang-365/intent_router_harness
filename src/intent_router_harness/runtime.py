"""Prompt harness runtime — TOML spec loading and asset resolution.

Phase 2: SkillLibrary has been replaced by harness_v2.SkillRegistry.
This module now only resolves paths and loads agent contexts.  Skill
scanning is handled by SkillRegistry at the harness_v2 layer.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
from pathlib import Path
import tomllib

from intent_router_harness.schema import HarnessSpec, SkillBinding

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class AgentContext:
    """Root-level agent instruction loaded for assistant prompts."""

    path: Path
    body: str


class PromptHarness:
    """Loaded router harness assets.

    The harness is a thin config object.  Skill indexing and lifecycle
    management are delegated to ``harness_v2.SkillRegistry``.
    """

    def __init__(
        self,
        *,
        spec: HarnessSpec,
        skill_roots: tuple[str, ...] = (),
        agent_contexts: tuple[AgentContext, ...] = (),
        hook_roots: tuple[str, ...] = (),
        tool_roots: tuple[str, ...] = (),
    ) -> None:
        self.spec = spec
        self.skill_roots = skill_roots
        self.agent_contexts = agent_contexts
        self.hook_roots = hook_roots
        self.tool_roots = tool_roots


def load_prompt_harness(
    spec_path: str | Path,
    *,
    skill_roots: list[str] | None = None,
) -> PromptHarness | None:
    """Load harness config, root agent context, and resolve skill roots.

    Args:
        spec_path: Path to TOML harness spec.
        skill_roots: Additional skill root directories.

    Returns:
        PromptHarness or None if spec is disabled.
    """
    resolved_spec_path = Path(spec_path).expanduser()
    logger.info("loading harness spec path=%s", resolved_spec_path)
    spec = load_harness_spec(resolved_spec_path)
    logger.info(
        "loaded harness spec name=%s version=%s enabled=%s skill_roots=%s bindings=%d",
        spec.name,
        spec.version,
        spec.enabled,
        list(spec.skill_roots),
        len(spec.bindings),
    )
    if not spec.enabled:
        logger.warning("harness spec disabled path=%s name=%s version=%s", resolved_spec_path, spec.name, spec.version)
        return None

    resolved_skill_roots = tuple(
        str(_resolve_relative_path(resolved_spec_path.parent, root))
        for root in spec.skill_roots
    )
    extra_roots = tuple(str(Path(root).expanduser()) for root in (skill_roots or []))
    all_skill_roots = resolved_skill_roots + extra_roots

    hook_roots = tuple(
        str(_resolve_relative_path(resolved_spec_path.parent, root))
        for root in spec.hook_roots
    )
    tool_roots = tuple(
        str(_resolve_relative_path(resolved_spec_path.parent, root))
        for root in spec.tool_roots
    )
    agent_contexts = _load_agent_contexts(resolved_spec_path.parent, spec.agent_paths or ["agent.md"])
    logger.info(
        "initialized prompt harness name=%s version=%s agent_contexts=%s skill_roots=%s",
        spec.name,
        spec.version,
        [str(context.path) for context in agent_contexts],
        list(all_skill_roots),
    )
    return PromptHarness(
        spec=spec,
        skill_roots=all_skill_roots,
        agent_contexts=agent_contexts,
        hook_roots=hook_roots,
        tool_roots=tool_roots,
    )


def load_harness_spec(path: str | Path) -> HarnessSpec:
    """Load and validate one harness spec from TOML."""
    spec_path = Path(path).expanduser()
    logger.info("parsing harness spec toml path=%s", spec_path)
    raw = tomllib.loads(spec_path.read_text(encoding="utf-8"))
    spec = HarnessSpec.model_validate(raw)
    logger.info("validated harness spec path=%s name=%s version=%s", spec_path, spec.name, spec.version)
    return spec


def binding_matches(
    binding: SkillBinding,
    *,
    intent_codes: tuple[str, ...],
) -> bool:
    """Return whether one binding applies to the current context."""
    if binding.intent_codes and not set(binding.intent_codes).intersection(intent_codes):
        return False
    return True


def _resolve_relative_path(base_dir: Path, raw_path: str) -> Path:
    path = Path(raw_path).expanduser()
    if path.is_absolute():
        return path
    return (base_dir / path).resolve()


def _load_agent_contexts(base_dir: Path, raw_paths: list[str]) -> tuple[AgentContext, ...]:
    contexts: list[AgentContext] = []
    for raw_path in raw_paths:
        path = _resolve_relative_path(base_dir, raw_path)
        if not path.is_file():
            logger.warning("skipping missing agent context path=%s", path)
            continue
        contexts.append(AgentContext(path=path, body=path.read_text(encoding="utf-8").strip()))
    return tuple(contexts)
