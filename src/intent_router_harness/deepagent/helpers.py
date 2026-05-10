"""Internal helpers shared across the deepagent subpackage."""

from __future__ import annotations

import json
import re
from typing import Any

from intent_router_harness.contracts import (
    AssistantProtocolFrame,
    AssistantTraceEvent,
    RouterMessageRequest,
    TaskRuntimeState,
)
from intent_router_harness.deepagent.errors import DeepAgentRuntimeError
from intent_router_harness.runtime import PromptHarness
from intent_router_harness.trace import emit_trace
from intent_router_harness.workflow import WorkflowToolEvent


def _append_trace(trace_events: list[AssistantTraceEvent], event: AssistantTraceEvent) -> None:
    trace_events.append(event)
    emit_trace(event)


def _is_workflow_tool_message(message: Any, tool_message_type: type[Any] | None = None) -> bool:
    if tool_message_type is not None and not isinstance(message, tool_message_type):
        return False
    return str(getattr(message, "name", "") or "") == "workflow_api_call"


def _tool_call_name(request: Any) -> str:
    tool_call = getattr(request, "tool_call", {}) or {}
    if isinstance(tool_call, dict):
        return str(tool_call.get("name") or "")
    return ""


def _tool_call_id(request: Any) -> str:
    tool_call = getattr(request, "tool_call", {}) or {}
    if isinstance(tool_call, dict):
        return str(tool_call.get("id") or "harness_tool_call")
    return "harness_tool_call"


def _tool_call_args(request: Any) -> dict[str, Any]:
    tool_call = getattr(request, "tool_call", {}) or {}
    if isinstance(tool_call, dict) and isinstance(tool_call.get("args"), dict):
        return dict(tool_call["args"])
    return {}


def _harness_virtual_files(harness: PromptHarness) -> dict[str, str]:
    files: dict[str, str] = {}
    for skill_name in harness.skills.names():
        skill = harness.skills.get(skill_name)
        if skill is None:
            continue
        skill_dir_name = skill.path.parent.name
        files[f"/skills/{skill_dir_name}/SKILL.md"] = skill.body
        for reference in skill.references:
            try:
                relative_reference = reference.path.relative_to(skill.path.parent).as_posix()
            except ValueError:
                relative_reference = reference.path.name
            files[f"/skills/{skill_dir_name}/{relative_reference}"] = reference.body
    return files


def _normalize_virtual_file_path(path: str) -> str:
    if not path:
        return ""
    normalized = path.replace("\\", "/").strip()
    if not normalized.startswith("/"):
        normalized = "/" + normalized
    while "//" in normalized:
        normalized = normalized.replace("//", "/")
    return normalized


def _workflow_request_from_messages(messages: Any) -> dict[str, Any] | None:
    if not isinstance(messages, list):
        return None
    for message in reversed(messages):
        content = _message_content_text(
            message.get("content") if isinstance(message, dict) else getattr(message, "content", "")
        ).strip()
        if not content:
            continue
        request_payload = _workflow_request_from_content(content)
        if request_payload is not None:
            return request_payload
    return None


def _workflow_request_from_content(content: str) -> dict[str, Any] | None:
    try:
        payload = _loads_json_object(content)
    except Exception:
        return None
    candidates: list[Any] = [payload]
    if isinstance(payload.get("workflow_request"), dict):
        candidates.append(payload["workflow_request"])
    if isinstance(payload.get("current_task"), dict):
        candidates.append(payload["current_task"].get("workflow_request"))
    if isinstance(payload.get("frame"), dict):
        candidates.append(payload["frame"].get("workflow_request"))
    raw_frames = payload.get("frames")
    if isinstance(raw_frames, list):
        for frame in raw_frames:
            if isinstance(frame, dict):
                candidates.append(frame.get("workflow_request"))
                current_task = frame.get("current_task")
                if isinstance(current_task, dict):
                    candidates.append(current_task.get("workflow_request"))
    for candidate in candidates:
        if _is_workflow_request_payload(candidate):
            return dict(candidate)
    return None


def _is_workflow_request_payload(candidate: Any) -> bool:
    return (
        isinstance(candidate, dict)
        and isinstance(candidate.get("url"), str)
        and bool(candidate.get("url"))
        and isinstance(candidate.get("body"), dict)
    )


def _loads_json_object(content: str) -> dict[str, Any]:
    try:
        payload = json.loads(content)
    except json.JSONDecodeError as first_error:
        fenced = _extract_fenced_json(content)
        if fenced is not None:
            return json.loads(fenced)
        braced = _extract_braced_json(content)
        if braced is not None:
            return json.loads(braced)
        raise first_error
    if not isinstance(payload, dict):
        raise DeepAgentRuntimeError("deepagent final response must be a JSON object")
    return payload


def _extract_fenced_json(content: str) -> str | None:
    match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", content, flags=re.DOTALL | re.IGNORECASE)
    if match is None:
        return None
    return match.group(1)


def _extract_braced_json(content: str) -> str | None:
    start = content.find("{")
    end = content.rfind("}")
    if start < 0 or end <= start:
        return None
    return content[start : end + 1]


def _assistant_protocol_frame_from_payload(payload: Any) -> AssistantProtocolFrame:
    if isinstance(payload, dict) and "status" not in payload and _assistant_turn_output(payload) is not None:
        return AssistantProtocolFrame(
            ok=True,
            status="completed",
            completion_state=2,
            completion_reason="deepagent_done",
            output=_assistant_turn_output(payload),
        )
    return AssistantProtocolFrame.model_validate(payload)


def _assistant_turn_output(payload: dict[str, Any]) -> Any:
    if "output" in payload:
        return payload["output"]
    content = payload.get("content")
    if isinstance(content, dict) and "output" in content:
        return content["output"]
    return None


def _last_message_content(response: Any) -> str:
    if isinstance(response, dict):
        messages = response.get("messages")
        if isinstance(messages, list) and messages:
            for message in reversed(messages):
                content = message.get("content") if isinstance(message, dict) else getattr(message, "content", "")
                text = _message_content_text(content).strip()
                if text:
                    return text
        if "content" in response:
            return _message_content_text(response["content"])
    return str(response)


def _message_content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                text = item.get("text") or item.get("content")
                if text:
                    parts.append(str(text))
        return "\n".join(parts)
    return str(content or "")


def _truncate(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return f"{value[:limit].rstrip()}..."


def _agent_context_text(harness: PromptHarness) -> str:
    return "\n\n".join(context.body for context in harness.agent_contexts)


def _harness_context_block(harness: PromptHarness) -> str:
    sections = ["## Intent Router Harness Loaded Context"]
    for skill_name in harness.skills.names():
        skill = harness.skills.get(skill_name)
        if skill is None:
            continue
        sections.append(
            "\n".join(
                [
                    f"### Skill: {skill.name}",
                    f"- description: {skill.description}",
                    f"- intent_codes: {list(skill.intent_codes)}",
                    f"- required_slots: {list(skill.required_slots)}",
                    f"- skill_file: /skills/{skill.path.parent.name}/SKILL.md",
                ]
            )
        )
        for reference in skill.references:
            sections.append(
                "\n".join(
                    [
                        f"#### Reference: {skill.name}/{reference.id}",
                        f"- purpose: {reference.purpose}",
                        "```markdown",
                        reference.body.strip(),
                        "```",
                    ]
                )
            )
    if len(sections) == 1:
        return ""
    sections.append(
        "Use this middleware-loaded context before reading files. "
        "Only call read_file when the loaded context is insufficient."
    )
    return "\n\n".join(sections)


def _skill_bodies(harness: PromptHarness) -> dict[str, str]:
    return {
        name: skill.body
        for name in harness.skills.names()
        if (skill := harness.skills.get(name)) is not None
    }


def _reference_bodies(harness: PromptHarness) -> dict[str, dict[str, str]]:
    result: dict[str, dict[str, str]] = {}
    for name in harness.skills.names():
        skill = harness.skills.get(name)
        if skill is None:
            continue
        result[skill.name] = {reference.id: reference.body for reference in skill.references}
    return result


def _thread_id(request: RouterMessageRequest) -> str:
    return f"{request.custID}:{request.sessionId}"


def _config_variable_map(request: RouterMessageRequest) -> dict[str, Any]:
    return {item.name: item.value for item in request.config_variables}


def _slot_memory_from_workflow_body(body: dict[str, Any]) -> dict[str, Any]:
    config_variables = body.get("config_variables")
    if not isinstance(config_variables, list):
        return {}
    for item in config_variables:
        if not isinstance(item, dict):
            continue
        if str(item.get("name") or "") != "slots_data":
            continue
        raw_value = item.get("value")
        if isinstance(raw_value, dict):
            return dict(raw_value)
        if not isinstance(raw_value, str):
            return {}
        try:
            payload = json.loads(raw_value)
        except json.JSONDecodeError:
            return {}
        return payload if isinstance(payload, dict) else {}
    return {}


def _workflow_session_id(body: dict[str, Any]) -> str:
    direct = str(body.get("session_id") or "").strip()
    if direct:
        return direct
    config_variables = body.get("config_variables")
    if isinstance(config_variables, list):
        for item in config_variables:
            if not isinstance(item, dict):
                continue
            if str(item.get("name") or "") == "sessionID":
                return str(item.get("value") or "").strip()
    return ""


def _workflow_slot_memory(
    workflow_call: Any,
    base_frame: AssistantProtocolFrame | None,
) -> dict[str, Any]:
    if workflow_call.slot_memory:
        return dict(workflow_call.slot_memory)
    if base_frame is not None and base_frame.slot_memory:
        return dict(base_frame.slot_memory)
    return {}


def _workflow_task_id(base_frame: AssistantProtocolFrame | None) -> str:
    if base_frame is not None and isinstance(base_frame.current_task, dict):
        task_id = str(base_frame.current_task.get("taskId") or "").strip()
        if task_id:
            return task_id
    return "task_001"


def _workflow_task_title(
    workflow_call: Any,
    base_frame: AssistantProtocolFrame | None,
) -> str:
    if base_frame is not None and isinstance(base_frame.current_task, dict):
        title = str(base_frame.current_task.get("title") or "").strip()
        if title:
            return title
    return workflow_call.skill_name or "workflow_api_call"


def _workflow_task_payload(
    *,
    task_id: str,
    intent_code: str | None,
    status: str,
    title: str,
    slot_memory: dict[str, Any],
    output: Any,
    workflow_call: Any,
) -> dict[str, Any]:
    return {
        "taskId": task_id,
        "intent_code": intent_code or "",
        "status": status,
        "title": title,
        "slot_memory": slot_memory,
        "output": output,
        "workflow_request": {
            "method": workflow_call.method,
            "url": workflow_call.url,
            "body": workflow_call.body,
        },
    }


def _workflow_task_list(
    base_frame: AssistantProtocolFrame | None,
    task_payload: dict[str, Any],
) -> list[dict[str, Any]]:
    if base_frame is None or not base_frame.task_list:
        return [task_payload]
    task_id = str(task_payload.get("taskId") or "")
    replaced = False
    task_list: list[dict[str, Any]] = []
    for item in base_frame.task_list:
        if isinstance(item, dict) and str(item.get("taskId") or "") == task_id:
            task_list.append(task_payload)
            replaced = True
        else:
            task_list.append(dict(item))
    if not replaced:
        task_list.append(task_payload)
    return task_list


def _workflow_event_details(
    event: WorkflowToolEvent,
    workflow_call: Any,
) -> dict[str, Any]:
    return {
        "node_id": event.node_id,
        "node_title": event.node_title,
        "timestamp": event.timestamp,
        "workflow_url": workflow_call.url,
    }


def _workflow_terminal_task_state(
    task_state: TaskRuntimeState,
    completed_task: dict[str, Any],
) -> TaskRuntimeState:
    active_tasks = [
        task
        for task in task_state.task_list
        if task.taskId != completed_task.get("taskId") and task.status not in {"completed", "cancelled", "failed"}
    ]
    current_task = active_tasks[0] if active_tasks else None
    return task_state.model_copy(
        update={
            "slot_memory": current_task.slot_memory if current_task is not None else {},
            "task_list": active_tasks,
            "current_task": current_task,
            "active_context": {},
            "context_leases": [],
        },
        deep=True,
    )


# ---------------------------------------------------------------------------
# Streaming helpers
# ---------------------------------------------------------------------------


def _stream_chunk_mode_data(chunk: Any) -> tuple[str, Any]:
    if isinstance(chunk, tuple):
        if len(chunk) == 3 and isinstance(chunk[1], str):
            return chunk[1], chunk[2]
        if len(chunk) == 2 and isinstance(chunk[0], str):
            return chunk[0], chunk[1]
        if len(chunk) == 2:
            return "messages", chunk
    if isinstance(chunk, dict):
        if "messages" in chunk:
            return "updates", chunk
        if "updates" in chunk:
            return "updates", chunk["updates"]
    return "", chunk


def _stream_message_from_data(data: Any) -> Any | None:
    if isinstance(data, tuple | list) and data:
        return data[0]
    if isinstance(data, dict) and ("content" in data or "tool_calls" in data):
        return data
    if hasattr(data, "content"):
        return data
    return None


def _stream_messages_from_update(update: Any) -> list[Any]:
    messages: list[Any] = []

    def collect(value: Any) -> None:
        if isinstance(value, dict):
            raw_messages = value.get("messages")
            if isinstance(raw_messages, list):
                messages.extend(raw_messages)
            for child in value.values():
                if child is not raw_messages:
                    collect(child)
        elif isinstance(value, list):
            for child in value:
                collect(child)

    collect(update)
    return messages


def _stream_debug_summary(messages: list[Any]) -> dict[str, Any]:
    tail = messages[-6:]
    return {
        "message_count": len(messages),
        "tail": [
            {
                "type": message.__class__.__name__,
                "name": getattr(message, "name", None),
                "content": _truncate(_message_content_text(getattr(message, "content", "")), 160),
                "tool_calls": _message_tool_calls_debug(message),
            }
            for message in tail
        ],
    }


def _message_tool_calls_debug(message: Any) -> list[dict[str, Any]]:
    raw_tool_calls = getattr(message, "tool_calls", None)
    if not isinstance(raw_tool_calls, list):
        return []
    calls: list[dict[str, Any]] = []
    for item in raw_tool_calls:
        if isinstance(item, dict):
            calls.append(
                {
                    "name": str(item.get("name") or ""),
                    "args": item.get("args") if isinstance(item.get("args"), dict) else str(item.get("args") or ""),
                    "id": str(item.get("id") or ""),
                }
            )
    return calls


# ---------------------------------------------------------------------------
# DeepAgent user payload builder
# ---------------------------------------------------------------------------


def _deepagent_user_payload(context: Any) -> str:
    return json.dumps(
        {
            "request": context.request.model_dump(mode="json"),
            "task_state": context.task_state.model_dump(mode="json"),
            "thread_id": context.thread_id,
            "available_skill_names": sorted(context.skills),
            "config_variables": context.config_variables,
            "assistant_protocol_contract": {
                "message_event": "AssistantProtocolFrame JSON",
                "terminal_statuses": ["completed", "failed", "cancelled"],
                "multi_task": {
                    "task_list": "When one user message contains multiple intents or tasks, include all tasks in expression order.",
                    "current_task": "Exactly one active task should be current at a time.",
                    "execution": "Use DeepAgent write_todos for planning and execute workflow tools serially.",
                },
            },
        },
        ensure_ascii=False,
    )


# ---------------------------------------------------------------------------
# Result builders
# ---------------------------------------------------------------------------


def _result_with_workflow_call(
    context: Any,
    *,
    workflow_call: Any,
    parsed_result: Any | None,
) -> Any:
    from intent_router_harness.deepagent.service import DeepAgentRunResult

    base_frame = parsed_result.frames[-1] if parsed_result is not None and parsed_result.frames else None
    intent_code = workflow_call.intent_code or (base_frame.intent_code if base_frame is not None else None)
    slot_memory = _workflow_slot_memory(workflow_call, base_frame)
    task_id = _workflow_task_id(base_frame)
    title = _workflow_task_title(workflow_call, base_frame)

    running_task = _workflow_task_payload(
        task_id=task_id,
        intent_code=intent_code,
        status="waiting_assistant_completion",
        title=title,
        slot_memory=slot_memory,
        output={},
        workflow_call=workflow_call,
    )
    completed_task = {
        **running_task,
        "status": "completed",
        "output": workflow_call.result.final_output,
    }
    running_task_list = _workflow_task_list(base_frame, running_task)
    completed_task_list = _workflow_task_list(base_frame, completed_task)

    frames: list[AssistantProtocolFrame] = []
    for event in workflow_call.result.events:
        frames.append(
            AssistantProtocolFrame(
                ok=True,
                status="waiting_assistant_completion",
                intent_code=intent_code,
                completion_state=1,
                completion_reason="workflow_node_output",
                details=_workflow_event_details(event, workflow_call),
                output=event.node_output,
                slot_memory=slot_memory,
                task_list=running_task_list,
                current_task=running_task,
                graph=base_frame.graph if base_frame is not None else context.task_state.graph,
            )
        )

    frames.append(
        AssistantProtocolFrame(
            ok=True,
            status="completed",
            intent_code=intent_code,
            completion_state=2,
            completion_reason="workflow_done",
            output=workflow_call.result.final_output,
            slot_memory=slot_memory,
            task_list=completed_task_list,
            current_task=completed_task,
            graph=base_frame.graph if base_frame is not None else context.task_state.graph,
        )
    )
    trace_events = [
        AssistantTraceEvent(
            stage="deepagent_langchain_tool_call_started",
            title="LangChain\u5de5\u5177\u8c03\u7528\u5f00\u59cb",
            summary=f"workflow_api_call {workflow_call.method} {workflow_call.url}",
            data={
                "thread_id": context.thread_id,
                "method": workflow_call.method,
                "url": workflow_call.url,
                "body_keys": sorted(workflow_call.body),
                "intent_code": intent_code,
                "skill_name": workflow_call.skill_name,
            },
        ),
        AssistantTraceEvent(
            stage="deepagent_langchain_tool_call_completed",
            title="LangChain\u5de5\u5177\u8c03\u7528\u5b8c\u6210",
            summary=f"workflow_api_call returned {len(workflow_call.result.events)} node_output event(s)",
            data={
                "thread_id": context.thread_id,
                "method": workflow_call.method,
                "url": workflow_call.url,
                "event_count": len(workflow_call.result.events),
            },
        ),
    ]
    if parsed_result is not None:
        trace_events.extend(parsed_result.trace_events)
    source_task_state = (
        parsed_result.task_state
        if parsed_result is not None and parsed_result.task_state is not None
        else context.task_state
    )
    return DeepAgentRunResult(
        frames=tuple(frames),
        trace_events=tuple(trace_events),
        task_state=_workflow_terminal_task_state(source_task_state, completed_task),
    )


def _result_with_workflow_final_output(
    result: Any,
    *,
    final_output: Any,
) -> Any:
    from intent_router_harness.deepagent.service import DeepAgentRunResult

    if final_output is None or not result.frames:
        return result
    frames = list(result.frames)
    last = frames[-1]
    frames[-1] = last.model_copy(
        update={
            "output": final_output,
            "completion_reason": "workflow_done",
        }
    )
    return DeepAgentRunResult(
        frames=tuple(frames),
        trace_events=result.trace_events,
        task_state=result.task_state,
    )
