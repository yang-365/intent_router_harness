"""Tests for harness_v2.config module — TOML spec loading."""

from __future__ import annotations

from pathlib import Path

from intent_router_harness.harness_v2.config import HarnessConfig, load_config


class TestLoadConfig:
    def test_load_minimal_toml(self, tmp_path: Path):
        spec = tmp_path / "test.toml"
        spec.write_text(
            """
name = "test-harness"
version = "1.0.0"

[workflow]
allowed_urls = ["http://localhost:9876/test"]

[session]
idle_timeout_seconds = 600

[deepagent]
model = "openai:gpt-4o"
max_iterations = 10
skills = ["/skills/"]
""",
            encoding="utf-8",
        )
        config = load_config(spec)
        assert config.name == "test-harness"
        assert config.version == "1.0.0"
        assert config.model == "openai:gpt-4o"
        assert config.max_iterations == 10
        assert config.workflow_allowed_urls == ["http://localhost:9876/test"]
        assert config.session_idle_timeout_seconds == 600
        assert config.skill_sources == ["/skills/"]

    def test_load_with_agent_paths(self, tmp_path: Path):
        agent_md = tmp_path / "agent.md"
        agent_md.write_text("You are a helpful assistant.", encoding="utf-8")
        spec = tmp_path / "harness.toml"
        spec.write_text(
            'agent_paths = ["agent.md"]\n',
            encoding="utf-8",
        )
        config = load_config(spec)
        assert "helpful assistant" in config.system_prompt

    def test_defaults_when_sections_missing(self, tmp_path: Path):
        spec = tmp_path / "bare.toml"
        spec.write_text('name = "bare"\n', encoding="utf-8")
        config = load_config(spec)
        assert config.model is None
        assert config.max_iterations == 20
        assert config.session_idle_timeout_seconds == 1800
        assert config.workflow_allowed_urls == []
        assert config.skill_sources == ["/skills/"]


class TestHarnessConfigDefaults:
    def test_default_values(self):
        config = HarnessConfig()
        assert config.name == "intent-router-harness"
        assert config.backend == "state"
        assert config.session_idle_timeout_seconds == 1800
