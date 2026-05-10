# Protocol Compatibility Matrix: Classic vs DeepAgent Runtime

Both runtimes serve the same external interface (`POST /api/v1/message`) and
emit the same SSE event types (`trace`, `message`, `done`). The table below
clarifies which `AssistantProtocolFrame` fields are emitted by each runtime
and whether the semantics are identical.

## Frame Field Matrix

| Field               | Classic Runtime                          | DeepAgent Runtime                        | Identical? |
| ------------------- | ---------------------------------------- | ---------------------------------------- | ---------- |
| `ok`                | Always present                           | Always present                           | Yes        |
| `status`            | All `AssistantStatus` values             | All `AssistantStatus` values             | Yes        |
| `intent_code`       | From planner recognition                 | From workflow URL → skill lookup         | Yes (*)    |
| `completion_state`  | 0 = running, 1 = partial, 2 = terminal  | Same semantics                           | Yes        |
| `completion_reason` | Planner-defined strings                  | Planner + `workflow_node_output`, `workflow_done`, `deepagent_done` | Superset   |
| `stage`             | Planner stage name                       | Not emitted (trace only)                 | No         |
| `details`           | Planner-driven details                   | Workflow node metadata when available     | Different  |
| `output`            | Planner output or workflow `node_output` | Workflow `node_output` (opaque passthrough) | Yes      |
| `slot_memory`       | From planner slot extraction             | From workflow body `slots_data` or model | Yes        |
| `message`           | Model-generated follow-up text           | Model-generated text or `null`           | Yes        |
| `task_list`         | Planner multi-task list                  | DeepAgent `write_todos` mapped list      | Shape-compatible |
| `current_task`      | Planner current task dict                | DeepAgent current task dict              | Shape-compatible |
| `errorCode`         | Planner or runtime error code            | Runtime error code (same vocabulary)     | Yes        |
| `graph`             | Planner execution graph                  | Passthrough from task state              | Yes        |
| `actions`           | Planner recommended actions              | Empty (not yet implemented)              | Subset     |

(*) Both runtimes derive `intent_code` from skill metadata. Classic uses
planner output directly; DeepAgent infers it by matching the workflow URL
back to the skill that declares it.

## Trace Stage Matrix

| Trace Stage                             | Classic | DeepAgent | Notes                          |
| --------------------------------------- | ------- | --------- | ------------------------------ |
| `request_received`                      | Yes     | Yes       | Identical shape                |
| `session_loaded`                        | Yes     | Yes       | Identical shape                |
| `task_runtime_loaded`                   | Yes     | Yes       | Identical shape                |
| `skill_progressive_disclosure`          | Yes     | No        | Classic planner-specific       |
| `reference_loaded`                      | Yes     | No        | Classic planner-specific       |
| `deepagent_runtime_start`               | No      | Yes       | DeepAgent-specific             |
| `deepagent_run_completed`               | No      | Yes       | DeepAgent-specific             |
| `deepagent_runtime_failed`              | No      | Yes       | DeepAgent-specific             |
| `deepagent_langchain_tool_call_started` | No      | Yes       | DeepAgent-specific             |
| `deepagent_langchain_tool_call_completed` | No    | Yes       | DeepAgent-specific             |
| `assistant_protocol_frames`             | Yes     | Yes       | Identical shape                |
| `workflow_tool_call_started`            | Yes     | No        | Classic workflow dispatch       |
| `workflow_tool_call_completed`          | Yes     | No        | Classic workflow dispatch       |

## SSE Event Compatibility

Both runtimes produce identical SSE event shapes:

```
event: trace
data: {"stage": "...", "title": "...", "summary": "...", "data": {...}}

event: message
data: {"ok": true, "status": "...", "completion_state": ..., ...}

event: done
data: [DONE]
```

## Session and Concurrency

| Behavior                              | Classic          | DeepAgent        |
| ------------------------------------- | ---------------- | ---------------- |
| Session store                         | In-memory        | In-memory        |
| Thread isolation (`custID:sessionId`) | Yes              | Yes              |
| Same-session run lock                 | No               | Yes (non-reentrant) |
| Concurrent different sessions         | Yes              | Yes              |
| Session idle timeout                  | Configurable     | Configurable     |
| User ownership enforcement            | Yes              | Yes              |

## Workflow Error Classification

| Error Category             | Code                        | Description                                          |
| -------------------------- | --------------------------- | ---------------------------------------------------- |
| URL not in whitelist       | `workflow_url_not_allowed`  | Workflow URL failed `allowed_urls` check              |
| HTTP non-2xx               | `workflow_http_error`       | Upstream workflow returned non-success status         |
| SSE parse failure          | `workflow_sse_parse_error`  | Response is not valid SSE or JSON                     |
| Missing `node_output`      | `workflow_no_node_output`   | SSE events lack `additional_kwargs.node_output`       |
| Timeout                    | `workflow_timeout`          | Workflow call exceeded configured timeout             |
| Hook rejection             | `workflow_hook_rejected`    | A before/after hook rejected the call                 |
| Tool runtime error         | `workflow_tool_error`       | Command tool process exited with non-zero             |
| Session run in progress    | `session_run_in_progress`   | Same session already has an active agent run          |
| DeepAgent runtime failure  | `deepagent_runtime_error`   | DeepAgent invocation crashed or returned invalid data |
| Model call limit exceeded  | `model_call_limit_exceeded` | Single request exceeded max model call iterations     |
