"""Tests for harness_v2.workflow module — SSE parsing and raw fallback."""

from __future__ import annotations

from intent_router_harness.harness_v2.workflow import _parse_sse_event, _raw_output


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


class TestRawOutput:
    def test_json_payload(self):
        result = _raw_output('{"status": "ok", "data": [1, 2]}')
        assert result["output"] == {"status": "ok", "data": [1, 2]}

    def test_plain_text_payload(self):
        result = _raw_output("some plain text response")
        assert result["output"] == "some plain text response"

    def test_empty_payload(self):
        result = _raw_output("")
        assert result["output"] == ""

    def test_whitespace_only(self):
        result = _raw_output("   \n  ")
        assert result["output"] == ""

    def test_html_payload(self):
        html = "<html><body>Error 502</body></html>"
        result = _raw_output(html)
        assert result["output"] == html
