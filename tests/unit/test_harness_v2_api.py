"""Tests for harness_v2.api module — wire format compatibility."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from intent_router_harness.harness_v2.api import HarnessApp, create_app, _extract_frames, _build_user_input
from intent_router_harness.harness_v2.config import HarnessConfig
from intent_router_harness.harness_v2.protocol import AssistantProtocolFrame, MessageRequest
from intent_router_harness.harness_v2.session import SessionManager


def _patch_invoke_agent(monkeypatch, mock_agent):
    """Patch _invoke_agent to bypass langchain_core import."""
    def fake_invoke(agent, user_input, thread_id):
        return agent.invoke(
            {"messages": [{"content": user_input}]},
            config={"configurable": {"thread_id": thread_id}},
        )
    monkeypatch.setattr(
        "intent_router_harness.harness_v2.api._invoke_agent", fake_invoke,
    )


class TestBuildUserInput:
    def test_contains_required_fields(self):
        req = MessageRequest(sessionId="s1", txt="hello", custID="c1")
        result = json.loads(_build_user_input(req))
        assert result["txt"] == "hello"
        assert result["sessionId"] == "s1"
        assert result["custID"] == "c1"
        assert result["executionMode"] == "execute"

    def test_preserves_execution_mode(self):
        req = MessageRequest(sessionId="s1", txt="hi", custID="c1", executionMode="plan")
        result = json.loads(_build_user_input(req))
        assert result["executionMode"] == "plan"


class TestExtractFrames:
    def test_protocol_json_output(self):
        msg = MagicMock()
        msg.content = json.dumps({
            "frames": [
                {
                    "ok": True,
                    "status": "waiting_user_input",
                    "completion_state": 0,
                    "completion_reason": "slot_filling",
                    "message": "请输入金额",
                    "slot_memory": {"payee": "张三"},
                }
            ]
        })
        result = {"messages": [msg]}
        frames = _extract_frames(result)
        assert len(frames) == 1
        assert frames[0].status == "waiting_user_input"
        assert frames[0].slot_memory == {"payee": "张三"}
        assert frames[0].message == "请输入金额"

    def test_natural_text_output(self):
        msg = MagicMock()
        msg.content = "你好，请问有什么可以帮你的？"
        result = {"messages": [msg]}
        frames = _extract_frames(result)
        assert len(frames) == 1
        assert frames[0].status == "running"
        assert frames[0].message == "你好，请问有什么可以帮你的？"

    def test_empty_messages(self):
        frames = _extract_frames({"messages": []})
        assert len(frames) == 1
        assert frames[0].ok is False
        assert frames[0].status == "failed"

    def test_single_frame_without_frames_key(self):
        msg = MagicMock()
        msg.content = json.dumps({
            "status": "running",
            "completion_state": 0,
            "completion_reason": "planning",
            "message": "processing",
        })
        result = {"messages": [msg]}
        frames = _extract_frames(result)
        assert len(frames) == 1
        assert frames[0].status == "running"


class TestApiRoutes:
    """Test API routes with mocked agent."""

    @pytest.fixture()
    def mock_app(self, monkeypatch):
        config = HarnessConfig(name="test")
        mock_agent = MagicMock()

        ai_msg = MagicMock()
        ai_msg.content = json.dumps({
            "frames": [
                {
                    "ok": True,
                    "status": "waiting_user_input",
                    "completion_state": 0,
                    "completion_reason": "slot_filling",
                    "message": "请告诉我收款人",
                }
            ]
        })
        mock_agent.invoke.return_value = {"messages": [ai_msg]}

        _patch_invoke_agent(monkeypatch, mock_agent)
        harness_app = HarnessApp(config, agent=mock_agent)
        return create_app(harness_app=harness_app)

    def test_healthz(self, mock_app):
        client = TestClient(mock_app)
        resp = client.get("/healthz")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

    def test_readyz(self, mock_app):
        client = TestClient(mock_app)
        resp = client.get("/readyz")
        assert resp.status_code == 200
        assert resp.json()["ready"] is True

    def test_message_non_stream(self, mock_app):
        client = TestClient(mock_app)
        resp = client.post("/api/v1/message", json={
            "sessionId": "s1",
            "txt": "转账给张三",
            "custID": "c1",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["status"] == "waiting_user_input"
        assert data["message"] == "请告诉我收款人"

    def test_message_stream(self, mock_app):
        client = TestClient(mock_app)
        resp = client.post("/api/v1/message", json={
            "sessionId": "s1",
            "txt": "转账",
            "custID": "c1",
            "stream": True,
        })
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        body = resp.text
        assert "event: message\n" in body
        assert "event: done\n" in body
        assert "[DONE]" in body

    def test_message_stream_with_trace(self, mock_app):
        client = TestClient(mock_app)
        resp = client.post("/api/v1/message", json={
            "sessionId": "s1",
            "txt": "查账单",
            "custID": "c1",
            "stream": True,
            "debugTrace": True,
        })
        assert resp.status_code == 200
        body = resp.text
        assert "event: trace\n" in body
        assert "event: message\n" in body
        assert "event: done\n" in body

    def test_task_completion_success(self, mock_app):
        client = TestClient(mock_app)
        resp = client.post("/api/v1/task/completion", json={
            "sessionId": "s1",
            "custID": "c1",
            "taskId": "t1",
            "completionSignal": 1,
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["status"] == "completed"
        assert data["completion_state"] == 2
        assert data["output"]["taskId"] == "t1"

    def test_task_completion_failure(self, mock_app):
        client = TestClient(mock_app)
        resp = client.post("/api/v1/task/completion", json={
            "sessionId": "s1",
            "custID": "c1",
            "taskId": "t1",
            "completionSignal": 2,
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is False
        assert data["status"] == "failed"

    def test_concurrent_session_rejected(self, mock_app):
        """Verify same session concurrent run returns error frame."""
        harness_app = mock_app.state.harness
        # Manually lock the session
        key = harness_app.session_mgr.thread_id("c1", "s-locked")
        with harness_app.session_mgr._mu:
            harness_app.session_mgr._active_runs[key] = True

        client = TestClient(mock_app)
        resp = client.post("/api/v1/message", json={
            "sessionId": "s-locked",
            "txt": "test",
            "custID": "c1",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is False
        assert data["errorCode"] == "session_run_in_progress"

        # Cleanup
        with harness_app.session_mgr._mu:
            harness_app.session_mgr._active_runs.pop(key, None)
