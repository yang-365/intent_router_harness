"""Tests for the service layer — health and regression validation."""

from __future__ import annotations

from pathlib import Path

from intent_router_harness.service import IntentRouterHarnessService


def _write_demo_harness(tmp_path: Path) -> Path:
    skills_root = tmp_path / "skills"
    skill_dir = skills_root / "transfer-routing"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "\n".join(
            [
                "---",
                "name: transfer-routing",
                "description: 转账路由规则",
                'intent_codes: ["transfer"]',
                "---",
                "# 转账路由规则",
                "",
                "将收款人、金额、账号和银行卡尾号视为槽位。",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    spec_path = tmp_path / "intent-router-harness.toml"
    spec_path.write_text(
        "\n".join(
            [
                'name = "finance-router-harness"',
                'version = "2026.04"',
                f'skill_roots = ["{skills_root.as_posix()}"]',
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return spec_path


def test_service_loads_harness_config(tmp_path: Path) -> None:
    service = IntentRouterHarnessService.from_spec(_write_demo_harness(tmp_path))

    health = service.health()
    assert health.name == "finance-router-harness"
    assert health.version == "2026.04"
    assert health.enabled is True
