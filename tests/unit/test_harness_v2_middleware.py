"""Tests for harness_v2.middleware module — enterprise runtime constraints."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, AsyncMock, patch

import pytest

from intent_router_harness.harness_v2.errors import WorkflowUrlNotAllowedError


class TestBuildHarnessMiddleware:
    """Test that build_harness_middleware returns the correct middleware stack."""

    @pytest.fixture(autouse=True)
    def _mock_langchain(self, monkeypatch):
        """Mock langchain imports so tests run without deepagents installed."""
        self.mock_middleware_module = MagicMock()
        self.mock_types_module = MagicMock()
        self.mock_messages_module = MagicMock()

        class FakeAgentMiddleware:
            pass

        class FakeSystemMessage:
            def __init__(self, content=""):
                self.content = content

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

        self.mock_middleware_module.AgentMiddleware = FakeAgentMiddleware
        self.mock_types_module.hook_config = fake_hook_config
        self.mock_messages_module.AIMessage = FakeAIMessage
        self.mock_messages_module.SystemMessage = FakeSystemMessage
        self.mock_messages_module.ToolMessage = FakeToolMessage

        self.FakeAgentMiddleware = FakeAgentMiddleware
        self.FakeSystemMessage = FakeSystemMessage
        self.FakeAIMessage = FakeAIMessage
        self.FakeToolMessage = FakeToolMessage

        import sys
        monkeypatch.setitem(sys.modules, "langchain.agents.middleware", self.mock_middleware_module)
        monkeypatch.setitem(sys.modules, "langchain.agents.middleware.types", self.mock_types_module)
        monkeypatch.setitem(sys.modules, "langchain_core.messages", self.mock_messages_module)

    def test_returns_five_middleware(self):
        from intent_router_harness.harness_v2.middleware import build_harness_middleware
        mw_list = build_harness_middleware(allowed_urls=["http://ok.com"])
        assert len(mw_list) == 5

    def test_middleware_names(self):
        from intent_router_harness.harness_v2.middleware import build_harness_middleware
        mw_list = build_harness_middleware()
        names = [mw.name for mw in mw_list]
        assert "TaskProgressMiddleware" in names
        assert "CompletionGateMiddleware" in names
        assert "SkillLifecycleMiddleware" in names
        assert "WorkflowGatewayMiddleware" in names
        assert "ProtocolOutputMiddleware" in names


class TestWorkflowGatewayUrlValidation:
    """Test URL whitelist enforcement without deepagent SDK."""

    @pytest.fixture(autouse=True)
    def _mock_langchain(self, monkeypatch):
        class FakeAgentMiddleware:
            pass

        class FakeSystemMessage:
            def __init__(self, content=""):
                self.content = content

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

    def test_blocked_url_raises(self):
        from intent_router_harness.harness_v2.middleware import build_harness_middleware
        mw_list = build_harness_middleware(allowed_urls=["http://ok.com"])
        gateway = [m for m in mw_list if m.name == "WorkflowGatewayMiddleware"][0]

        request = MagicMock()
        request.tool_call = {"name": "workflow_api_call", "args": {"url": "http://evil.com/hack"}}
        handler = MagicMock()

        with pytest.raises(WorkflowUrlNotAllowedError):
            gateway.wrap_tool_call(request, handler)
        handler.assert_not_called()

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
    """Test that CompletionGateMiddleware terminates after workflow results."""

    @pytest.fixture(autouse=True)
    def _mock_langchain(self, monkeypatch):
        class FakeAgentMiddleware:
            pass

        class FakeToolMessage:
            def __init__(self, content="", name="", tool_call_id=""):
                self.content = content
                self.name = name
                self.tool_call_id = tool_call_id

        class FakeSystemMessage:
            def __init__(self, content=""):
                self.content = content

        class FakeAIMessage:
            def __init__(self, content=""):
                self.content = content

        def fake_hook_config(**kwargs):
            def decorator(func):
                return func
            return decorator

        mock_mw = MagicMock()
        mock_types = MagicMock()
        mock_msg = MagicMock()
        mock_mw.AgentMiddleware = FakeAgentMiddleware
        mock_types.hook_config = fake_hook_config
        mock_msg.AIMessage = FakeAIMessage
        mock_msg.SystemMessage = FakeSystemMessage
        mock_msg.ToolMessage = FakeToolMessage

        self.FakeToolMessage = FakeToolMessage
        self.FakeAIMessage = FakeAIMessage

        import sys
        monkeypatch.setitem(sys.modules, "langchain.agents.middleware", mock_mw)
        monkeypatch.setitem(sys.modules, "langchain.agents.middleware.types", mock_types)
        monkeypatch.setitem(sys.modules, "langchain_core.messages", mock_msg)

    def test_gate_triggers_on_workflow_result(self):
        from intent_router_harness.harness_v2.middleware import build_harness_middleware
        mw_list = build_harness_middleware()
        gate = [m for m in mw_list if m.name == "CompletionGateMiddleware"][0]

        state = {
            "messages": [
                self.FakeToolMessage(content='{"ok": true}', name="workflow_api_call", tool_call_id="tc1"),
            ]
        }
        result = gate.before_model(state, None)
        assert result is not None
        assert result["jump_to"] == "end"

    def test_gate_no_op_without_workflow(self):
        from intent_router_harness.harness_v2.middleware import build_harness_middleware
        mw_list = build_harness_middleware()
        gate = [m for m in mw_list if m.name == "CompletionGateMiddleware"][0]

        state = {"messages": [self.FakeAIMessage(content="hello")]}
        result = gate.before_model(state, None)
        assert result is None


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
