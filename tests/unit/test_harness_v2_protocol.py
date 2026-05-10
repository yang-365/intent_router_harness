"""Tests for harness_v2.protocol module — wire format compatibility."""

from __future__ import annotations

import pytest

from intent_router_harness.harness_v2.protocol import (
    AssistantProtocolFrame,
    MessageRequest,
    TaskCompletionRequest,
    TraceEvent,
)


class TestMessageRequest:
    def test_valid_request(self):
        req = MessageRequest(sessionId="s1", txt="hello", custID="c1")
        assert req.sessionId == "s1"
        assert req.txt == "hello"
        assert req.custID == "c1"
        assert req.stream is False
        assert req.executionMode == "execute"

    def test_blank_session_id_rejected(self):
        with pytest.raises(ValueError, match="must not be blank"):
            MessageRequest(sessionId="  ", txt="hello", custID="c1")

    def test_blank_txt_rejected(self):
        with pytest.raises(ValueError, match="must not be blank"):
            MessageRequest(sessionId="s1", txt="", custID="c1")

    def test_extra_fields_allowed(self):
        req = MessageRequest(sessionId="s1", txt="hi", custID="c1", custom_field="val")
        assert req.model_extra.get("custom_field") == "val"


class TestTaskCompletionRequest:
    def test_valid_completion(self):
        req = TaskCompletionRequest(
            sessionId="s1", custID="c1", taskId="t1", completionSignal=1,
        )
        assert req.completionSignal == 1

    def test_invalid_signal(self):
        with pytest.raises(ValueError):
            TaskCompletionRequest(
                sessionId="s1", custID="c1", taskId="t1", completionSignal=3,
            )


class TestAssistantProtocolFrame:
    def test_protocol_dump_excludes_none(self):
        frame = AssistantProtocolFrame(
            ok=True,
            status="running",
            completion_state=0,
            completion_reason="test",
        )
        dump = frame.protocol_dump()
        assert "intent_code" not in dump
        assert "errorCode" not in dump
        assert dump["ok"] is True
        assert dump["status"] == "running"

    def test_all_fields_present(self):
        frame = AssistantProtocolFrame(
            ok=False,
            status="failed",
            intent_code="TRANSFER",
            completion_state=0,
            completion_reason="error",
            errorCode="bad_request",
            message="something went wrong",
            output={"key": "val"},
            slot_memory={"amount": 100},
            task_list=[{"taskId": "t1"}],
            current_task={"taskId": "t1"},
        )
        dump = frame.protocol_dump()
        assert dump["intent_code"] == "TRANSFER"
        assert dump["errorCode"] == "bad_request"
        assert dump["slot_memory"]["amount"] == 100

    def test_v1_compatible_field_names(self):
        """Ensure field names match v1 contracts exactly."""
        frame = AssistantProtocolFrame(status="running", completion_state=0, completion_reason="x")
        dump = frame.protocol_dump()
        expected_keys = {"ok", "status", "completion_state", "completion_reason", "output", "slot_memory", "task_list"}
        assert expected_keys.issubset(dump.keys())


class TestTraceEvent:
    def test_defaults(self):
        event = TraceEvent(stage="test")
        assert event.title == ""
        assert event.summary == ""
        assert event.data == {}
