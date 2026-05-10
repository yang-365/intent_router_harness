"""Tests for harness_v2.workflow module — SSE parsing."""

from __future__ import annotations

import json

import pytest

from intent_router_harness.harness_v2.workflow import _parse_sse_event


class TestParseSseEvent:
    def test_json_data(self):
        text = 'data: {"node_id": "n1", "node_output": {"result": "ok"}}'
        result = _parse_sse_event(text)
        assert result is not None
        assert result["node_id"] == "n1"
        assert result["node_output"]["result"] == "ok"

    def test_multiline_data(self):
        text = 'data: {"key":\ndata:  "value"}'
        result = _parse_sse_event(text)
        assert result is not None
        assert result["key"] == "value"

    def test_non_json_data(self):
        text = "data: some plain text"
        result = _parse_sse_event(text)
        assert result == {"raw": "some plain text"}

    def test_empty_event(self):
        result = _parse_sse_event("")
        assert result is None

    def test_event_without_data(self):
        result = _parse_sse_event("event: heartbeat")
        assert result is None

    def test_data_with_colon_no_space(self):
        text = 'data:{"x": 1}'
        result = _parse_sse_event(text)
        assert result is not None
        assert result["x"] == 1
