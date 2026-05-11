from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any


TRANSFER_AGENT_ID = "workflow-agent-1-1b14f16b"
PAYEE_LIST_AGENT_ID = "workflow-agent-payee"
BILL_PAYMENT_AGENT_ID = "workflow-agent-bill"


class MockWorkflowHandler(BaseHTTPRequestHandler):
    """Small SSE workflow server for local end-to-end testing."""

    server_version = "MockWorkflowServer/0.1"

    def do_POST(self) -> None:
        body = self._read_json_body()
        record = {"path": self.path, "body": body}
        print("MOCK_WORKFLOW_REQUEST", json.dumps(record, ensure_ascii=False), flush=True)
        outputs = _outputs_for_path(self.path, body)
        print(
            "MOCK_WORKFLOW_RESPONSE",
            json.dumps({"path": self.path, "event_count": len(outputs), "outputs": outputs}, ensure_ascii=False),
            flush=True,
        )
        self._write_sse(outputs)

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _read_json_body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0") or 0)
        raw = self.rfile.read(length).decode("utf-8")
        if not raw:
            return {}
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return {"raw": raw}
        return payload if isinstance(payload, dict) else {"payload": payload}

    def _write_sse(self, outputs: list[Any]) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        for index, output in enumerate(outputs):
            payload = {
                "content": "",
                "additional_kwargs": {
                    "node_id": f"mock_{index}",
                    "node_title": f"Mock节点{index}",
                    "node_output": output,
                    "timestamp": "2026-05-08 00:00:00",
                },
                "response_metadata": {},
                "type": "ai",
                "name": None,
                "id": None,
                "tool_calls": [],
                "invalid_tool_calls": [],
                "usage_metadata": None,
            }
            frame = f"event:message\ndata:{json.dumps(payload, ensure_ascii=False)}\n\n"
            self.wfile.write(frame.encode("utf-8"))
            self.wfile.flush()
        self.wfile.write(b"event:done\ndata:[DONE]\n\n")
        self.wfile.flush()


def _outputs_for_path(path: str, body: dict[str, Any]) -> list[Any]:
    if TRANSFER_AGENT_ID in path:
        return [
            {"mock": "transfer-start", "received": body},
            {"mock": "transfer-check", "result": "transfer_ready"},
            {"output": "mock-transfer-done", "exception": None},
        ]
    if PAYEE_LIST_AGENT_ID in path:
        return [{"mock": "payee-list", "payees": ["陈广荣", "王阳明"]}]
    if BILL_PAYMENT_AGENT_ID in path:
        return [{"mock": "bill-payment", "status": "accepted"}]
    return [{"mock": "generic", "path": path, "received": body}]


def main() -> None:
    parser = argparse.ArgumentParser(description="Serve mock workflow use_as_tool endpoints.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9876)
    args = parser.parse_args()

    server = HTTPServer((args.host, args.port), MockWorkflowHandler)
    print(f"mock workflow serving on http://{args.host}:{args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nmock workflow stopped", flush=True)
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
