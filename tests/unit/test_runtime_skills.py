from __future__ import annotations

from pathlib import Path

import pytest

from intent_router_harness import load_prompt_harness


def test_runtime_loads_agent_context_and_skills(tmp_path: Path) -> None:
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
    skill = harness.skills.get("transfer-routing")
    assert skill is not None
    assert skill.intent_codes == ("AG_TRANS",)
    assert skill.required_slots == ("payee_name", "amount")
    assert skill.references[0].body == "详细转账槽位规则。"


def test_runtime_rejects_skill_with_multiple_intents(tmp_path: Path) -> None:
    skills_root = tmp_path / "skills"
    skill_dir = skills_root / "bad-routing"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "\n".join(
            [
                "---",
                "name: bad-routing",
                'intent_codes: ["AG_TRANSFER", "AG_BALANCE"]',
                "---",
                "# Bad Routing",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    spec_path = tmp_path / "harness.toml"
    spec_path.write_text(
        "\n".join(
            [
                'name = "invalid-skill-test"',
                'version = "2026.05"',
                f'skill_roots = ["{skills_root.as_posix()}"]',
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="multiple intent_codes"):
        load_prompt_harness(spec_path)


def test_finance_skills_declare_required_slots() -> None:
    harness = load_prompt_harness(Path("examples/finance-router-harness.toml"))
    assert harness is not None

    transfer = harness.skills.get("transfer-routing")
    bill = harness.skills.get("bill-payment-routing")

    assert transfer is not None
    assert transfer.required_slots == ("payee_name", "amount")
    assert bill is not None
    assert bill.required_slots == ("payment_item", "amount")
