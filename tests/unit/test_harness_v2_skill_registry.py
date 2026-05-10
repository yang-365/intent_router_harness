"""Tests for harness_v2.skill_registry — progressive skill loading."""

from __future__ import annotations

from pathlib import Path

import pytest

from intent_router_harness.harness_v2.skill_registry import (
    SkillMeta,
    SkillReferenceMeta,
    SkillRegistry,
    _parse_skill_metadata,
    _split_frontmatter,
)


@pytest.fixture()
def skill_tree(tmp_path: Path) -> Path:
    """Create a minimal skill tree for testing."""
    root = tmp_path / "skills"
    root.mkdir()

    # transfer-routing skill
    transfer = root / "transfer-routing"
    transfer.mkdir()
    (transfer / "SKILL.md").write_text(
        '---\n'
        'name: transfer-routing\n'
        'description: 掌银转账意图识别技能\n'
        'intent_codes: ["AG_TRANS"]\n'
        'required_slots: ["payee_name", "amount"]\n'
        'references: [{"id": "slot_filling", "path": "references/slot_filling.md", "purpose": "转账提槽规则"}, '
        '{"id": "workflow_request", "path": "references/workflow_request.md", "purpose": "API调用说明"}]\n'
        '---\n'
        '\n# 掌银转账意图识别\n\n这是意图边界内容。\n',
        encoding="utf-8",
    )
    refs = transfer / "references"
    refs.mkdir()
    (refs / "slot_filling.md").write_text(
        "# 转账提槽规则\n\n- payee_name: 收款人\n- amount: 金额\n",
        encoding="utf-8",
    )
    (refs / "workflow_request.md").write_text(
        "# 转账API调用\n\nmethod: POST\nurl: http://example.com/transfer\n",
        encoding="utf-8",
    )

    # bill-payment skill
    bill = root / "bill-payment"
    bill.mkdir()
    (bill / "SKILL.md").write_text(
        '---\n'
        'name: bill-payment\n'
        'description: 缴费意图识别\n'
        'intent_codes: ["AG_PAY_BILL"]\n'
        'required_slots: ["payment_item", "amount"]\n'
        '---\n'
        '\n# 缴费规则\n\n这是缴费意图边界。\n',
        encoding="utf-8",
    )

    return root


class TestSkillRegistry:
    def test_from_roots_scans_skills(self, skill_tree: Path):
        registry = SkillRegistry.from_roots([skill_tree])
        assert len(registry) == 2
        assert "transfer-routing" in registry.names()
        assert "bill-payment" in registry.names()

    def test_get_meta(self, skill_tree: Path):
        registry = SkillRegistry.from_roots([skill_tree])
        meta = registry.get_meta("transfer-routing")
        assert meta is not None
        assert meta.name == "transfer-routing"
        assert meta.description == "掌银转账意图识别技能"
        assert meta.intent_codes == ("AG_TRANS",)
        assert meta.required_slots == ("payee_name", "amount")
        assert len(meta.references) == 2

    def test_find_by_intent(self, skill_tree: Path):
        registry = SkillRegistry.from_roots([skill_tree])
        meta = registry.find_by_intent("AG_TRANS")
        assert meta is not None
        assert meta.name == "transfer-routing"

    def test_find_by_intent_not_found(self, skill_tree: Path):
        registry = SkillRegistry.from_roots([skill_tree])
        assert registry.find_by_intent("UNKNOWN") is None

    def test_all_metadata_summary(self, skill_tree: Path):
        registry = SkillRegistry.from_roots([skill_tree])
        summary = registry.all_metadata_summary()
        assert "## Available Skills (Intent Recognition)" in summary
        assert "transfer-routing" in summary
        assert "bill-payment" in summary
        assert "AG_TRANS" in summary
        assert "AG_PAY_BILL" in summary

    def test_load_skill_body(self, skill_tree: Path):
        registry = SkillRegistry.from_roots([skill_tree])
        body = registry.load_skill_body("transfer-routing")
        assert body is not None
        assert "掌银转账意图识别" in body
        assert "意图边界内容" in body
        # Frontmatter should be stripped
        assert "---" not in body

    def test_load_skill_body_not_found(self, skill_tree: Path):
        registry = SkillRegistry.from_roots([skill_tree])
        assert registry.load_skill_body("nonexistent") is None

    def test_load_reference(self, skill_tree: Path):
        registry = SkillRegistry.from_roots([skill_tree])
        body = registry.load_reference("transfer-routing", "slot_filling")
        assert body is not None
        assert "收款人" in body
        assert "金额" in body

    def test_load_reference_workflow(self, skill_tree: Path):
        registry = SkillRegistry.from_roots([skill_tree])
        body = registry.load_reference("transfer-routing", "workflow_request")
        assert body is not None
        assert "POST" in body

    def test_load_reference_not_found(self, skill_tree: Path):
        registry = SkillRegistry.from_roots([skill_tree])
        assert registry.load_reference("transfer-routing", "nonexistent") is None
        assert registry.load_reference("nonexistent", "slot_filling") is None

    def test_virtual_file_listing(self, skill_tree: Path):
        registry = SkillRegistry.from_roots([skill_tree])
        listing = registry.virtual_file_listing("transfer-routing")
        assert "/skills/transfer-routing/SKILL.md" in listing
        assert "/skills/transfer-routing/references/slot_filling.md" in listing
        assert "/skills/transfer-routing/references/workflow_request.md" in listing

    def test_missing_root_skipped(self, tmp_path: Path):
        registry = SkillRegistry.from_roots([tmp_path / "nonexistent"])
        assert len(registry) == 0

    def test_dir_without_skill_md_skipped(self, tmp_path: Path):
        root = tmp_path / "skills"
        root.mkdir()
        (root / "empty-skill").mkdir()
        registry = SkillRegistry.from_roots([root])
        assert len(registry) == 0

    def test_duplicate_intent_raises(self, tmp_path: Path):
        root = tmp_path / "skills"
        root.mkdir()
        for name in ["skill-a", "skill-b"]:
            d = root / name
            d.mkdir()
            (d / "SKILL.md").write_text(
                f'---\nname: {name}\ndescription: test\nintent_codes: ["SAME_INTENT"]\n---\n',
                encoding="utf-8",
            )
        with pytest.raises(ValueError, match="SAME_INTENT"):
            SkillRegistry.from_roots([root])


class TestSplitFrontmatter:
    def test_with_frontmatter(self):
        content = "---\nname: test\n---\n# Body"
        metadata, body = _split_frontmatter(content)
        assert metadata["name"] == "test"
        assert "# Body" in body

    def test_without_frontmatter(self):
        content = "# Just body"
        metadata, body = _split_frontmatter(content)
        assert metadata == {}
        assert body == content

    def test_unclosed_frontmatter(self):
        content = "---\nname: test\n# Body"
        metadata, body = _split_frontmatter(content)
        assert metadata == {}


class TestParseSkillMetadata:
    def test_parse_real_skill(self, skill_tree: Path):
        meta = _parse_skill_metadata(skill_tree / "transfer-routing" / "SKILL.md")
        assert meta is not None
        assert meta.name == "transfer-routing"
        assert meta.intent_codes == ("AG_TRANS",)
        assert len(meta.references) == 2
        assert meta.references[0].id == "slot_filling"
        assert meta.references[1].id == "workflow_request"

    def test_reference_outside_skill_dir_rejected(self, tmp_path: Path):
        root = tmp_path / "skills"
        root.mkdir()
        d = root / "bad-ref"
        d.mkdir()
        (d / "SKILL.md").write_text(
            '---\nname: bad-ref\ndescription: test\n'
            'references: [{"id": "escape", "path": "../../etc/passwd", "purpose": "bad"}]\n'
            '---\n',
            encoding="utf-8",
        )
        meta = _parse_skill_metadata(d / "SKILL.md")
        assert meta is not None
        assert len(meta.references) == 0
