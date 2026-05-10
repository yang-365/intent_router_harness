from __future__ import annotations

import argparse
import json
import sys
from typing import Any
from urllib import request


def main() -> None:
    parser = argparse.ArgumentParser(description="Check DeepAgent harness transfer E2E over /api/v1/message.")
    parser.add_argument("--url", default="http://127.0.0.1:8766/api/v1/message")
    parser.add_argument("--session-id", default="e2e_deepagent_transfer_001")
    parser.add_argument("--cust-id", default="C_E2E_001")
    parser.add_argument("--text", default="给陈广荣转500元")
    parser.add_argument("--expected-output", default="mock-transfer-done")
    args = parser.parse_args()

    events = _post_sse(
        args.url,
        {
            "sessionId": args.session_id,
            "custID": args.cust_id,
            "txt": args.text,
            "stream": True,
            "executionMode": "execute",
            "debugTrace": True,
            "config_variables": [
                {"name": "custID", "value": args.cust_id},
                {"name": "sessionID", "value": args.session_id},
                {"name": "currentDisplay", "value": ""},
                {"name": "agentSessionID", "value": args.session_id},
            ],
        },
    )
    traces = [data for name, data in events if name == "trace" and isinstance(data, dict)]
    messages = [data for name, data in events if name == "message" and isinstance(data, dict)]
    final = messages[-1] if messages else {}

    _assert(any(item.get("stage") == "deepagent_langchain_tool_call_completed" for item in traces), "missing tool-call trace")
    _assert(any(item.get("completion_reason") == "workflow_node_output" for item in messages), "missing workflow node frame")
    _assert(final.get("status") == "completed", f"final status is not completed: {final}")
    _assert(final.get("completion_reason") == "workflow_done", f"final reason is not workflow_done: {final}")
    output = final.get("output")
    _assert(isinstance(output, dict) and output.get("output") == args.expected_output, f"unexpected final output: {output}")

    print(
        json.dumps(
            {
                "ok": True,
                "trace_count": len(traces),
                "message_count": len(messages),
                "final": final,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


def _post_sse(url: str, payload: dict[str, Any]) -> list[tuple[str, Any]]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Accept": "text/event-stream",
            "Content-Type": "application/json",
        },
    )
    with request.urlopen(req, timeout=180) as response:
        raw = response.read().decode("utf-8")
    return _parse_sse(raw)


def _parse_sse(raw: str) -> list[tuple[str, Any]]:
    events: list[tuple[str, Any]] = []
    for block in raw.split("\n\n"):
        event_name = "message"
        data_lines: list[str] = []
        for line in block.splitlines():
            if line.startswith("event:"):
                event_name = line.removeprefix("event:").strip()
            elif line.startswith("data:"):
                data_lines.append(line.removeprefix("data:").strip())
        if not data_lines:
            continue
        data_text = "\n".join(data_lines)
        data: Any = data_text
        if data_text != "[DONE]":
            data = json.loads(data_text)
        events.append((event_name, data))
    return events


def _assert(condition: bool, message: str) -> None:
    if not condition:
        raise SystemExit(message)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"deepagent e2e check failed: {exc}", file=sys.stderr)
        raise
