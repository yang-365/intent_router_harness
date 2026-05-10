from __future__ import annotations

import pytest

from intent_router_harness.contracts import PlannedTask, RouterMessageRequest
from intent_router_harness.workflow import (
    WorkflowHTTPRequest,
    WorkflowToolError,
    WorkflowToolEvent,
    WorkflowToolSpec,
    build_workflow_request_payload,
    parse_workflow_sse,
)


def test_build_workflow_request_payload_accepts_model_request_in_allowed_urls() -> None:
    spec = WorkflowToolSpec(
        intent_code="AG_TRANS",
        allowed_urls=("http://127.0.0.1:9876/agent-api/workflow-agent/chatabc/use_as_tool",),
    )

    payload = build_workflow_request_payload(
        spec,
        request=RouterMessageRequest(
            sessionId="s1",
            custID="C0001",
            txt="给陈广荣转500元",
            config_variables=[{"name": "currentDisplay", "value": "validator_page"}],
        ),
        task=PlannedTask(
            taskId="task_001",
            intent_code="AG_TRANS",
            slot_memory={"payee_name": "陈广荣", "amount": 500},
            workflow_request={
                "method": "POST",
                "url": "http://127.0.0.1:9876/agent-api/workflow-agent/chatabc/use_as_tool",
                "body": {
                    "session_id": "s1",
                    "txt": "给陈广荣转500元",
                    "stream": True,
                    "config_variables": [
                        {"name": "slots_data", "value": '{"payee_name": "陈广荣", "amount": 500}'},
                    ],
                },
            },
        ),
    )

    assert payload == WorkflowHTTPRequest(
        method="POST",
        url="http://127.0.0.1:9876/agent-api/workflow-agent/chatabc/use_as_tool",
        body={
            "session_id": "s1",
            "txt": "给陈广荣转500元",
            "stream": True,
            "config_variables": [
                {"name": "slots_data", "value": '{"payee_name": "陈广荣", "amount": 500}'},
            ],
        },
    )


def test_build_workflow_request_payload_preserves_model_url_for_hooks() -> None:
    spec = WorkflowToolSpec(
        intent_code="AG_TRANS",
        allowed_urls=("http://127.0.0.1:9876/agent-api/workflow-agent/chatabc/use_as_tool",),
    )

    payload = build_workflow_request_payload(
        spec,
        request=RouterMessageRequest(sessionId="s1", custID="C0001", txt="hi"),
        task=PlannedTask(
            taskId="task_001",
            intent_code="AG_TRANS",
            workflow_request={
                "method": "POST",
                "url": "http://127.0.0.1:9876/agent-api/other/chatabc/use_as_tool",
                "body": {},
            },
        ),
    )

    assert payload.url == "http://127.0.0.1:9876/agent-api/other/chatabc/use_as_tool"


def test_parse_workflow_sse_extracts_node_output_as_whole() -> None:
    result = parse_workflow_sse(
        "\n".join(
            [
                "event:message",
                'data:{"additional_kwargs":{"node_id":"start","node_title":"开始","node_output":{"a":1},"timestamp":"t1"}}',
                "",
                "event:message",
                'data:{"additional_kwargs":{"node_id":"end","node_title":"结束","node_output":["opaque",2],"timestamp":"t2"}}',
                "",
                "event:done",
                "data:[DONE]",
                "",
            ]
        )
    )

    assert [event.node_output for event in result.events] == [{"a": 1}, ["opaque", 2]]
    assert result.final_output == ["opaque", 2]


def test_parse_workflow_sse_rejects_non_json_message() -> None:
    with pytest.raises(WorkflowToolError, match="not JSON"):
        parse_workflow_sse(
            "\n".join(
                [
                    "event:message",
                    "data:not-json",
                    "",
                    "event:done",
                    "data:[DONE]",
                    "",
                ]
            )
        )


def test_parse_workflow_sse_rejects_missing_node_output() -> None:
    with pytest.raises(WorkflowToolError, match="node_output"):
        parse_workflow_sse(
            "\n".join(
                [
                    "event:message",
                    'data:{"additional_kwargs":{"node_id":"end"}}',
                    "",
                    "event:done",
                    "data:[DONE]",
                    "",
                ]
            )
        )


def test_parse_workflow_sse_requires_done_event() -> None:
    with pytest.raises(WorkflowToolError, match="without done"):
        parse_workflow_sse(
            "\n".join(
                [
                    "event:message",
                    'data:{"additional_kwargs":{"node_output":{}}}',
                    "",
                ]
            )
        )
