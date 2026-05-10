"""TOML spec loading — reads existing harness spec format for backward compat."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class HarnessConfig:
    """Resolved configuration for the minimal harness.

    Reads from the same TOML format as v1 so existing spec files work unchanged.
    """

    # Identity
    name: str = "intent-router-harness"
    version: str = "0.1.0"
    description: str = ""

    # DeepAgent SDK parameters
    model: str | None = None
    system_prompt: str = ""
    skill_sources: list[str] = field(default_factory=lambda: ["/skills/"])
    memory_sources: list[str] = field(default_factory=list)
    max_iterations: int = 20

    # Enterprise parameters
    workflow_allowed_urls: list[str] = field(default_factory=list)
    session_idle_timeout_seconds: int = 1800

    # Backend
    backend: str = "state"
    backend_root: str | None = None

    # Paths resolved relative to spec file
    agent_paths: list[str] = field(default_factory=list)
    skill_roots: list[str] = field(default_factory=list)
    hook_roots: list[str] = field(default_factory=list)
    tool_roots: list[str] = field(default_factory=list)


def load_config(spec_path: str | Path) -> HarnessConfig:
    """Load a harness config from the existing TOML spec format."""
    spec_file = Path(spec_path)
    raw = tomllib.loads(spec_file.read_text(encoding="utf-8"))
    base_dir = spec_file.parent

    agent_paths = [
        str((base_dir / p).resolve()) for p in raw.get("agent_paths", [])
    ]
    system_prompt = _load_system_prompt(agent_paths)

    deepagent = raw.get("deepagent", {})
    workflow = raw.get("workflow", {})
    session = raw.get("session", {})

    return HarnessConfig(
        name=raw.get("name", "intent-router-harness"),
        version=raw.get("version", "0.1.0"),
        description=raw.get("description", ""),
        model=deepagent.get("model"),
        system_prompt=system_prompt,
        skill_sources=deepagent.get("skills", ["/skills/"]),
        memory_sources=deepagent.get("memory", []),
        max_iterations=deepagent.get("max_iterations", 20),
        workflow_allowed_urls=workflow.get("allowed_urls", []),
        session_idle_timeout_seconds=session.get("idle_timeout_seconds", 1800),
        agent_paths=agent_paths,
        skill_roots=[str((base_dir / p).resolve()) for p in raw.get("skill_roots", [])],
        hook_roots=[str((base_dir / p).resolve()) for p in raw.get("hook_roots", [])],
        tool_roots=[str((base_dir / p).resolve()) for p in raw.get("tool_roots", [])],
    )


def _load_system_prompt(agent_paths: list[str]) -> str:
    """Concatenate all agent.md files into a single system prompt."""
    parts: list[str] = []
    for path_str in agent_paths:
        path = Path(path_str)
        if path.is_file():
            parts.append(path.read_text(encoding="utf-8").strip())
    return "\n\n".join(parts)
