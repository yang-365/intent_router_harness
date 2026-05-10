"""Tests for runtime + v2 SkillRegistry integration."""

from __future__ import annotations

from pathlib import Path

import pytest

from intent_router_harness import load_prompt_harness
from intent_router_harness.harness_v2.skill_registry import SkillRegistry


def test_runtime_loads_agent_context_and_skill_roots(tmp_path: Path) -> None:
    agent_path = tmp_path / "agent.md"
    agent_path.write_text("根路由约束。", encoding="utf-8")
    skills_root = tmp_path / "skills"
    skill_dir = skills_root / "transfer-routing"
    reference_dir = skill_dir / "references"
    reference_dir.mkdir(parents=True)
    (reference_dir / "ref_001.md").write_text("详细转账槽位规则。", encoding="utf-8")
    (skill_dir / "SKILL.md").write_text(
        "\n".join(
            [
                "---",
                "name: transfer-routing",
                "description: 转账路由规则",
                'intent_codes: ["AG_TRANS"]',
                'required_slots: ["payee_name", "amount"]',
                'references: [{"id": "ref_001", "path": "references/ref_001.md", "purpose": "Transfer slot detail"}]',
                "---",
                "# 转账路由规则",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    spec_path = tmp_path / "harness.toml"
    spec_path.write_text(
        "\n".join(
            [
                'name = "runtime-test"',
                'version = "2026.05"',
                f'agent_paths = ["{agent_path.as_posix()}"]',
                f'skill_roots = ["{skills_root.as_posix()}"]',
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    harness = load_prompt_harness(spec_path)
    assert harness is not None
    assert harness.agent_contexts[0].body == "根路由约束。"
    assert len(harness.skill_roots) > 0

    registry = SkillRegistry.from_roots(list(harness.skill_roots))
    meta = registry.get_meta("transfer-routing")
    assert meta is not None
    assert meta.intent_codes == ("AG_TRANS",)
    assert meta.required_slots == ("payee_name", "amount")
    assert len(meta.references) == 1

    ref_body = registry.load_reference("transfer-routing", "ref_001")
    assert ref_body is not None
    assert "详细转账槽位规则" in ref_body


def test_registry_rejects_skill_with_multiple_intents(tmp_path: Path) -> None:
    skills_root = tmp_path / "skills"
    skill_dir = skills_root / "bad-routing"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "\n".join(
            [
                "---",
                "name: bad-routing",
                "description: bad",
                'intent_codes: ["AG_TRANSFER", "AG_BALANCE"]',
                "---",
                "# Bad Routing",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="multiple intent_codes"):
        SkillRegistry.from_roots([skills_root])


def test_finance_skills_declare_required_slots() -> None:
    harness = load_prompt_harness(Path("examples/finance-router-harness.toml"))
    assert harness is not None

    registry = SkillRegistry.from_roots(list(harness.skill_roots))
    transfer = registry.get_meta("transfer-routing")
    bill = registry.get_meta("bill-payment-routing")

    assert transfer is not None
    assert transfer.required_slots == ("payee_name", "amount")
    assert bill is not None
    assert bill.required_slots == ("payment_item", "amount")
