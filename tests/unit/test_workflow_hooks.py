from __future__ import annotations

import pytest

from intent_router_harness.workflow_hooks import (
    WorkflowHookError,
    load_workflow_hooks,
    run_first_workflow_hook,
    run_workflow_hooks,
)


def test_before_workflow_tool_call_hook_rejects_url_outside_allowlist() -> None:
    hooks = load_workflow_hooks(["hooks"])

    with pytest.raises(WorkflowHookError, match="not allowed"):
        run_workflow_hooks(
            hooks,
            event="before_workflow_tool_call",
            payload={
                "request": {
                    "method": "POST",
                    "url": "http://127.0.0.1:9876/not-allowed",
                    "body": {},
                },
                "allowed_urls": [
                    "http://127.0.0.1:9876/agent-api/workflow-agent/chatabc/use_as_tool",
                ],
            },
        )


def test_after_workflow_tool_call_hook_forwards_workflow_node_output() -> None:
    hooks = load_workflow_hooks(["hooks"])

    result = run_first_workflow_hook(
        hooks,
        event="after_workflow_tool_call",
        payload={
            "phase": "message",
            "url": "http://127.0.0.1:9876/agent-api/workflow-agent/chatabc/use_as_tool",
            "response_type": "sse",
            "event": {
                "additional_kwargs": {
                    "node_output": {"output": "ok", "exception": None},
                },
            },
        },
    )

    assert result == {
        "status": "waiting_assistant_completion",
        "completion_reason": "workflow_node_output",
        "output": {"output": "ok", "exception": None},
    }
