from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class SkillBinding(BaseModel):
    """Rule that declares a configured skill."""

    skill: str
    intent_codes: list[str] = Field(default_factory=list)
    load: Literal["metadata", "body"] = "body"


class WorkflowConfig(BaseModel):
    """Global workflow tool configuration."""

    allowed_urls: list[str] = Field(default_factory=list)


class DeepAgentConfig(BaseModel):
    """Configuration for the optional DeepAgent-backed runtime."""

    model: str | None = None
    skills: list[str] = Field(default_factory=lambda: ["/skills/"])
    memory: list[str] = Field(default_factory=list)
    max_iterations: int = Field(default=20, gt=0)


class SessionConfig(BaseModel):
    """Session lifecycle configuration for the in-memory runtime."""

    idle_timeout_seconds: int = Field(default=1800, gt=0)


class HarnessSpec(BaseModel):
    """Top-level spec for a standalone intent router harness."""

    name: str = "intent-router-harness"
    version: str = "0.1.0"
    description: str = ""
    enabled: bool = True
    agent_runtime: Literal["deepagent"] = "deepagent"
    agent_paths: list[str] = Field(default_factory=list)
    skill_roots: list[str] = Field(default_factory=list)
    hook_roots: list[str] = Field(default_factory=list)
    tool_roots: list[str] = Field(default_factory=list)
    max_skill_body_chars: int = Field(default=6000, gt=0)
    max_reference_body_chars: int = Field(default=6000, gt=0)
    max_reference_count: int = Field(default=4, gt=0)
    session: SessionConfig = Field(default_factory=SessionConfig)
    workflow: WorkflowConfig = Field(default_factory=WorkflowConfig)
    deepagent: DeepAgentConfig = Field(default_factory=DeepAgentConfig)
    bindings: list[SkillBinding] = Field(default_factory=list)


class EvalCase(BaseModel):
    """Portable eval case for harness-driven experiments."""

    id: str
    variables: dict[str, Any] = Field(default_factory=dict)
    intent_codes: list[str] = Field(default_factory=list)
    expected: dict[str, Any] | None = None
    tags: list[str] = Field(default_factory=list)


class Variant(BaseModel):
    """One named spec variant in a harness experiment."""

    name: str
    spec_file: str


class ExperimentSpec(BaseModel):
    """Minimal experiment manifest for comparing harness variants."""

    name: str
    variants: list[Variant] = Field(default_factory=list)
    cases: list[EvalCase] = Field(default_factory=list)
