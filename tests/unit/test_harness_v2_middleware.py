"""Tests for harness_v2.middleware module — enterprise runtime constraints."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from intent_router_harness.harness_v2.skill_registry import SkillRegistry

# ---------------------------------------------------------------------------
# Shared mock fixtures for langchain imports
# ---------------------------------------------------------------------------

class FakeAgentMiddleware:
    pass

class FakeSystemMessage:
    def __init__(self, content=""):
        self.content = content
        self.text = content

class FakeAIMessage:
    def __init__(self, content=""):
        self.content = content

class FakeToolMessage:
    def __init__(self, content="", name="", tool_call_id=""):
        self.content = content
        self.name = name
        self.tool_call_id = tool_call_id

def fake_hook_config(**kwargs):
    def decorator(func):
        return func
    return decorator


@pytest.fixture(autouse=True)
def _mock_langchain(monkeypatch):
    """Mock langchain imports so tests run without deepagents installed."""
    mock_mw = MagicMock()
    mock_types = MagicMock()
    mock_msg = MagicMock()
    mock_mw.AgentMiddleware = FakeAgentMiddleware
    mock_types.hook_config = fake_hook_config
    mock_msg.AIMessage = FakeAIMessage
    mock_msg.SystemMessage = FakeSystemMessage
    mock_msg.ToolMessage = FakeToolMessage

    import sys
    monkeypatch.setitem(sys.modules, "langchain.agents.middleware", mock_mw)
    monkeypatch.setitem(sys.modules, "langchain.agents.middleware.types", mock_types)
    monkeypatch.setitem(sys.modules, "langchain_core.messages", mock_msg)


@pytest.fixture()
def skill_tree(tmp_path: Path) -> Path:
    root = tmp_path / "skills"
    root.mkdir()
    transfer = root / "transfer-routing"
    transfer.mkdir()
    (transfer / "SKILL.md").write_text(
        '---\n'
        'name: transfer-routing\n'
        'description: 转账意图\n'
        'intent_codes: ["AG_TRANS"]\n'
        'required_slots: ["payee_name", "amount"]\n'
        'references: [{"id": "slot_filling", "path": "references/slot_filling.md", "purpose": "提槽"}]\n'
        '---\n\n# 转账边界\n\n转账规则内容。\n',
        encoding="utf-8",
    )
    refs = transfer / "references"
    refs.mkdir()
    (refs / "slot_filling.md").write_text("# 提槽规则\n\npayee_name 和 amount\n", encoding="utf-8")
    return root


class TestBuildHarnessMiddleware:
    def test_returns_seven_middleware(self):
        from intent_router_harness.harness_v2.middleware import build_harness_middleware
        mw_list = build_harness_middleware(allowed_urls=["http://ok.com"])
        assert len(mw_list) == 7

    def test_middleware_names(self):
        from intent_router_harness.harness_v2.middleware import build_harness_middleware
        mw_list = build_harness_middleware()
        names = [mw.name for mw in mw_list]
        assert "TaskProgressMiddleware" in names
        assert "FrontendContextMiddleware" in names
        assert "CompletionGateMiddleware" in names
        assert "SkillLifecycleMiddleware" in names
        assert "SkillFileMiddleware" in names
        assert "WorkflowGatewayMiddleware" in names
        assert "ProtocolOutputMiddleware" in names


class TestWorkflowGatewayUrlValidation:
    def test_allowed_url_passes(self):
        from intent_router_harness.harness_v2.middleware import build_harness_middleware
        mw_list = build_harness_middleware(allowed_urls=["http://ok.com"])
        gateway = [m for m in mw_list if m.name == "WorkflowGatewayMiddleware"][0]

        request = MagicMock()
        request.tool_call = {"name": "workflow_api_call", "args": {"url": "http://ok.com/endpoint"}}
        handler = MagicMock(return_value="result")

        result = gateway.wrap_tool_call(request, handler)
        handler.assert_called_once()
        assert result == "result"

    def test_blocked_url_returns_tool_message(self):
        from intent_router_harness.harness_v2.middleware import build_harness_middleware
        mw_list = build_harness_middleware(allowed_urls=["http://ok.com"])
        gateway = [m for m in mw_list if m.name == "WorkflowGatewayMiddleware"][0]

        request = MagicMock()
        request.tool_call = {"name": "workflow_api_call", "args": {"url": "http://evil.com/hack"}}
        handler = MagicMock()

        result = gateway.wrap_tool_call(request, handler)
        handler.assert_not_called()
        assert "not in the allowed list" in result.content
        assert "http://ok.com" in result.content

    def test_non_workflow_tool_passes_through(self):
        from intent_router_harness.harness_v2.middleware import build_harness_middleware
        mw_list = build_harness_middleware(allowed_urls=["http://ok.com"])
        gateway = [m for m in mw_list if m.name == "WorkflowGatewayMiddleware"][0]

        request = MagicMock()
        request.tool_call = {"name": "read_file", "args": {"path": "/foo"}}
        handler = MagicMock(return_value="file_content")

        result = gateway.wrap_tool_call(request, handler)
        assert result == "file_content"


class TestCompletionGate:
    def test_gate_triggers_on_workflow_result(self):
        from intent_router_harness.harness_v2.middleware import build_harness_middleware
        mw_list = build_harness_middleware()
        gate = [m for m in mw_list if m.name == "CompletionGateMiddleware"][0]

        state = {
            "messages": [
                FakeToolMessage(content='{"ok": true}', name="workflow_api_call", tool_call_id="tc1"),
            ]
        }
        result = gate.before_model(state, None)
        assert result is not None
        assert result["jump_to"] == "end"

    def test_gate_no_op_without_workflow(self):
        from intent_router_harness.harness_v2.middleware import build_harness_middleware
        mw_list = build_harness_middleware()
        gate = [m for m in mw_list if m.name == "CompletionGateMiddleware"][0]

        state = {"messages": [FakeAIMessage(content="hello")]}
        result = gate.before_model(state, None)
        assert result is None


class TestSkillLifecycleMiddleware:
    def test_eager_load_on_first_call(self, skill_tree):
        """On first model call (no AI history), eagerly inject all skill bodies."""
        registry = SkillRegistry.from_roots([skill_tree])
        from intent_router_harness.harness_v2.middleware import build_harness_middleware
        mw_list = build_harness_middleware(skill_registry=registry)
        lifecycle = [m for m in mw_list if m.name == "SkillLifecycleMiddleware"][0]

        request = MagicMock()
        request.system_message = FakeSystemMessage(content="You are a helpful assistant.")
        request.messages = []

        handler = MagicMock(return_value="response")
        lifecycle.wrap_model_call(request, handler)

        # On first call, all skills should be eagerly loaded via override
        request.override.assert_called_once()
        # Handler should be called with the overridden request
        handler.assert_called_once()

    def test_detects_intent_from_ai_message(self, skill_tree):
        registry = SkillRegistry.from_roots([skill_tree])
        from intent_router_harness.harness_v2.middleware import build_harness_middleware
        mw_list = build_harness_middleware(skill_registry=registry)
        lifecycle = [m for m in mw_list if m.name == "SkillLifecycleMiddleware"][0]

        ai_msg = FakeAIMessage(content=json.dumps({
            "frames": [{"intent_code": "AG_TRANS", "status": "running"}]
        }))

        request = MagicMock()
        request.system_message = FakeSystemMessage(content="")
        request.messages = [ai_msg]
        request.override.return_value = request

        lifecycle.wrap_model_call(request, lambda r: "ok")

        # Skill body should have been injected
        call_args = request.override.call_args
        assert call_args is not None
        sm = call_args[1].get("system_message") or (call_args[0][0] if call_args[0] else None)
        if sm:
            assert "transfer-routing" in sm.content or "转账" in sm.content


class TestSkillFileMiddleware:
    def test_intercepts_skill_read(self, skill_tree):
        registry = SkillRegistry.from_roots([skill_tree])
        from intent_router_harness.harness_v2.middleware import build_harness_middleware
        mw_list = build_harness_middleware(skill_registry=registry)
        skill_file_mw = [m for m in mw_list if m.name == "SkillFileMiddleware"][0]

        request = MagicMock()
        request.tool_call = {
            "name": "read_file",
            "id": "tc1",
            "args": {"file_path": "/skills/transfer-routing/references/slot_filling.md"},
        }
        handler = MagicMock()

        result = skill_file_mw.wrap_tool_call(request, handler)
        handler.assert_not_called()
        assert result is not None
        assert "payee_name" in result.content

    def test_intercepts_skill_md_read(self, skill_tree):
        registry = SkillRegistry.from_roots([skill_tree])
        from intent_router_harness.harness_v2.middleware import build_harness_middleware
        mw_list = build_harness_middleware(skill_registry=registry)
        skill_file_mw = [m for m in mw_list if m.name == "SkillFileMiddleware"][0]

        request = MagicMock()
        request.tool_call = {
            "name": "read_file",
            "id": "tc2",
            "args": {"file_path": "/skills/transfer-routing/SKILL.md"},
        }
        handler = MagicMock()

        result = skill_file_mw.wrap_tool_call(request, handler)
        handler.assert_not_called()
        assert "转账" in result.content

    def test_not_found_lists_available(self, skill_tree):
        registry = SkillRegistry.from_roots([skill_tree])
        from intent_router_harness.harness_v2.middleware import build_harness_middleware
        mw_list = build_harness_middleware(skill_registry=registry)
        skill_file_mw = [m for m in mw_list if m.name == "SkillFileMiddleware"][0]

        request = MagicMock()
        request.tool_call = {
            "name": "read_file",
            "id": "tc3",
            "args": {"file_path": "/skills/nonexistent/SKILL.md"},
        }
        handler = MagicMock()

        result = skill_file_mw.wrap_tool_call(request, handler)
        handler.assert_not_called()
        assert "not found" in result.content.lower() or "File not found" in result.content
        assert "/skills/transfer-routing/SKILL.md" in result.content

    def test_non_skill_path_passes_through(self, skill_tree):
        registry = SkillRegistry.from_roots([skill_tree])
        from intent_router_harness.harness_v2.middleware import build_harness_middleware
        mw_list = build_harness_middleware(skill_registry=registry)
        skill_file_mw = [m for m in mw_list if m.name == "SkillFileMiddleware"][0]

        request = MagicMock()
        request.tool_call = {
            "name": "read_file",
            "id": "tc4",
            "args": {"file_path": "/home/user/file.txt"},
        }
        handler = MagicMock(return_value="real_content")

        result = skill_file_mw.wrap_tool_call(request, handler)
        handler.assert_called_once()
        assert result == "real_content"

    def test_non_read_tool_passes_through(self, skill_tree):
        registry = SkillRegistry.from_roots([skill_tree])
        from intent_router_harness.harness_v2.middleware import build_harness_middleware
        mw_list = build_harness_middleware(skill_registry=registry)
        skill_file_mw = [m for m in mw_list if m.name == "SkillFileMiddleware"][0]

        request = MagicMock()
        request.tool_call = {"name": "write_file", "id": "tc5", "args": {"path": "/skills/x"}}
        handler = MagicMock(return_value="ok")

        result = skill_file_mw.wrap_tool_call(request, handler)
        handler.assert_called_once()
        assert result == "ok"


class TestFillFrameDefaults:
    def test_fills_missing_keys(self):
        from intent_router_harness.harness_v2.middleware import _fill_frame_defaults
        frame: dict = {"status": "running"}
        _fill_frame_defaults(frame)
        assert frame["ok"] is True
        assert frame["completion_state"] == 0
        assert frame["output"] == {}
        assert frame["task_list"] == []

    def test_preserves_existing_keys(self):
        from intent_router_harness.harness_v2.middleware import _fill_frame_defaults
        frame: dict = {"ok": False, "completion_state": 2, "status": "completed"}
        _fill_frame_defaults(frame)
        assert frame["ok"] is False
        assert frame["completion_state"] == 2
