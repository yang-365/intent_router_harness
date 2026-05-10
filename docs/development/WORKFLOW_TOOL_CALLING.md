# Workflow 子工作流调用规范

本文定义 Router 调用子工作流的通用接口规范。该规范适用于转账、缴费、查询等通过 workflow `use_as_tool` 形式接入的子工作流。

## 1. 调用边界

整体链路：

```text
前端 -> Router /api/v1/message
Router -> LLM planner 只提取 slots
Router -> workflow use_as_tool
workflow -> Router SSE node_output
Router -> 前端 SSE output
```

关键约束：

- 模型只负责识别意图和填写 `slot_memory`。
- 除 slots 外，workflow 所需参数全部由前端透传或 Router 会话字段提供。
- 前端透传参数不进入模型 prompt。
- workflow 调用方式由 skill reference 定义。
- Router 不展开、不校验、不理解 `node_output` 内部业务结构。
- Router 返回给前端时，直接令 `output = workflow_event.additional_kwargs.node_output`。

## 2. 前端调用 Router

前端仍调用现有 Router 接口：

```http
POST /api/v1/message
Content-Type: application/json
Accept: text/event-stream
```

请求示例：

```json
{
  "sessionId": "1635501196813426",
  "custID": "1631102265490929",
  "txt": "给陈广荣转500元",
  "stream": true,
  "executionMode": "execute",
  "config_variables": [
    { "name": "sessionID", "value": "1635501196813426" },
    { "name": "currentDisplay", "value": "" },
    { "name": "agentSessionID", "value": "1635501196813426" }
  ]
}
```

字段说明：

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `sessionId` | 是 | Router 会话 ID，也用于 workflow `session_id`。 |
| `custID` | 是 | 客户 ID，会透传给 workflow。 |
| `txt` | 是 | 用户输入，进入模型用于提槽，也透传给 workflow。 |
| `stream` | 否 | 建议传 `true`，返回 SSE。 |
| `executionMode` | 否 | `router_only` 只规划；`execute` 槽齐后调用 workflow。 |
| `config_variables` | 否 | 前端请求变量，提槽和 workflow request reference 可以引用。 |

`executionMode` 语义：

| 值 | 行为 |
| --- | --- |
| `router_only` | 只做意图识别、提槽和任务规划；槽齐返回 `ready_for_dispatch`，不调用 workflow。 |
| `execute` | 槽齐后调用对应 workflow，并把 workflow `node_output` 流式返回。 |

## 3. Skill 与 Reference 分层

建议每个场景按两层拆分，API 调用前后的通用处理放在 hooks 中：

```text
SKILL.md                      意图路由/场景边界，模型在意图识别阶段使用。
references/slot_filling.md    提槽业务规则，模型在提槽阶段默认加载。
references/workflow_request.md 子工作流接口和参数组装说明，模型在提槽阶段默认加载。
hooks/hooks.json              Deep-Agents-style hook 注册表，模型不可见。
hooks/<hook-name>/*.py        workflow 工具调用生命周期处理逻辑，模型不可见。
tools/<tool-name>/*.py        workflow/API 执行工具，模型不可见。
```

`SKILL.md` 的 references 示例：

```json
[
  { "id": "slot_filling", "path": "references/slot_filling.md", "purpose": "转账提槽规则" },
  { "id": "workflow_request", "path": "references/workflow_request.md", "purpose": "转账子工作流接口与参数组装说明" }
]
```

加载规则：

- 意图识别阶段只使用 skill metadata 和轻量正文。
- 提槽阶段自动加载 `slot_filling` 和 `workflow_request` reference。
- hooks 永远不进入模型 prompt，只供 Router 执行阶段读取。

## 4. Workflow Tool Hooks

hooks 和 skills 平级，通过 `hooks/hooks.json` 注册。Router 只提供两个 workflow 工具调用事件：`before_workflow_tool_call` 和 `after_workflow_tool_call`。事件发生时，Router 将 JSON payload 通过 stdin 传给 command，command 从 stdout 返回 JSON。

这里不再配置 `match`。是否拦截、是否透传、如何处理，都由对应 hook command 的代码决定。

```toml
hook_roots = ["../hooks"]
tool_roots = ["../tools"]
```

```json
{
  "hooks": [
    "restrict-api-url-to-allowlist",
    "forward-workflow-node-output"
  ]
}
```

当前内置两个 hook：

| Hook | 事件 | 作用 |
| --- | --- | --- |
| `restrict-api-url-to-allowlist/enforce_url_allowlist.py` | `before_workflow_tool_call` | 在 workflow 工具发起请求前，校验完整 URL 必须命中 `workflow.allowed_urls`。 |
| `forward-workflow-node-output/forward_node_output.py` | `after_workflow_tool_call` | 在 workflow 工具返回后，把 `additional_kwargs.node_output` 作为 Router `output` 直接透传。 |

## 5. Workflow URL 白名单

Workflow URL 白名单是 Router 通用配置，不放在 skill reference 里。白名单由 `restrict-api-url-to-allowlist` hook 执行校验。示例：

```toml
[workflow]
allowed_urls = [
  "http://127.0.0.1:9876/agent-api/workflow-agent-1-1b14f16b/chatabc/use_as_tool",
  "http://127.0.0.1:9876/agent-api/workflow-agent-payee/chatabc/use_as_tool",
  "http://127.0.0.1:9876/agent-api/workflow-agent-bill/chatabc/use_as_tool",
]
```

字段说明：

| 字段 | 说明 |
| --- | --- |
| `workflow.allowed_urls` | 模型输出的 `workflow_request.url` 白名单，必须是完整 HTTP(S) URL 精确匹配。只要 URL 命中白名单，Router 就允许访问。 |

注意：

- 白名单是全局配置，不绑定 intent_code。
- Router 将 workflow request 交给 `before_workflow_tool_call` hook；hook 校验 URL 必须命中 `allowed_urls`。
- 当前 workflow 工具使用 `tools/workflow-api-call/execute_workflow_api.py` 执行 POST + SSE JSON headers 调用子工作流。
- URL 必须是完整 `http://` 或 `https://` 地址，禁止 `..` 路径。

## 6. Router 调用 Workflow

当 planner 输出当前任务：

```json
{
  "intent_code": "AG_TRANS",
  "status": "ready_for_dispatch",
  "slot_memory": {
    "payee_name": "陈广荣",
    "amount": "500"
  },
  "workflow_request": {
    "method": "POST",
    "url": "http://127.0.0.1:9876/agent-api/workflow-agent-1-1b14f16b/chatabc/use_as_tool",
    "body": {
      "session_id": "1635501196813426",
      "txt": "给陈广荣转500元",
      "stream": true,
      "config_variables": [
        { "name": "custID", "value": "1631102265490929" },
        { "name": "sessionID", "value": "1635501196813426" },
        { "name": "currentDisplay", "value": "" },
        { "name": "agentSessionID", "value": "1635501196813426" },
        { "name": "slots_data", "value": "{\"payee_name\":\"陈广荣\",\"amount\":500}" }
      ]
    }
  }
}
```

且请求为：

```json
{
  "executionMode": "execute"
}
```

Router 根据模型输出的 `workflow_request` 和通用配置中的 `workflow.allowed_urls` 白名单执行内部请求：

```http
POST http://127.0.0.1:9876/agent-api/workflow-agent-1-1b14f16b/chatabc/use_as_tool
Accept: text/event-stream
Cache-Control: no-cache
Content-Type: application/json
```

请求体来自 `workflow_request.body`：

```json
{
  "session_id": "1635501196813426",
  "txt": "给陈广荣转500元",
  "stream": true,
  "config_variables": [
    { "name": "custID", "value": "1631102265490929" },
    { "name": "sessionID", "value": "1635501196813426" },
    { "name": "currentDisplay", "value": "" },
    { "name": "agentSessionID", "value": "1635501196813426" },
    { "name": "slots_data", "value": "{\"payee_name\":\"陈广荣\",\"amount\":\"500\"}" }
  ]
}
```

配置：

```bash
ROUTER_WORKFLOW_TIMEOUT_SECONDS=60
```

## 6. Workflow SSE 响应

workflow 返回 SSE：

```text
event:message
data:{...}

event:message
data:{...}

event:done
data:[DONE]
```

单个 message 示例：

```json
{
  "content": "",
  "additional_kwargs": {
    "node_id": "end",
    "node_title": "结束",
    "node_output": {
      "output": "...",
      "exception": null
    },
    "timestamp": "2026-05-08 19:19:04.553"
  },
  "response_metadata": {},
  "type": "ai",
  "name": null,
  "id": null,
  "tool_calls": [],
  "invalid_tool_calls": [],
  "usage_metadata": null
}
```

`forward-workflow-node-output` after hook 读取：

```text
data.additional_kwargs.node_output
```

Router 默认不解析：

```text
node_output.output
node_output.result
node_output.isHandOver
node_output.typIntent
node_output.answer
```

## 7. Router SSE 返回映射

workflow 每个 `event:message` 映射为 Router 的一个 `event: message`。

hook 映射规则：

```text
RouterFrame.output = WorkflowMessage.additional_kwargs.node_output
```

示例：

workflow event：

```json
{
  "additional_kwargs": {
    "node_title": "结束",
    "node_output": {
      "output": "...",
      "exception": null
    }
  }
}
```

Router 返回：

```text
event: message
data: {
  "ok": true,
  "status": "waiting_assistant_completion",
  "intent_code": "AG_TRANS",
  "completion_state": 1,
  "completion_reason": "workflow_node_output",
  "output": {
    "output": "...",
    "exception": null
  },
  "slot_memory": {
    "payee_name": "陈广荣",
    "amount": "500"
  }
}
```

workflow 正常 done 后，Router 追加最终完成帧：

```text
event: message
data: {
  "ok": true,
  "status": "completed",
  "intent_code": "AG_TRANS",
  "completion_state": 2,
  "completion_reason": "workflow_done",
  "output": {
    "output": "...",
    "exception": null
  }
}

event: done
data: [DONE]
```

最终完成帧的 `output` 由 `forward-workflow-node-output` hook 使用最后一个 workflow message 的 `node_output`。

## 8. 异常处理

以下情况视为 workflow 调用失败：

- workflow HTTP 非 2xx。
- workflow SSE 中 `event:message` 的 `data` 不是合法 JSON。
- `forward-workflow-node-output` hook 处理失败，例如 workflow message 缺少 `additional_kwargs.node_output`。
- workflow SSE 未返回 `event:done` / `data:[DONE]`。
- 请求 workflow 发生网络错误或超时。

失败时 Router 返回：

```json
{
  "ok": false,
  "status": "failed",
  "completion_state": 2,
  "completion_reason": "workflow_error",
  "output": {
    "error": {
      "code": "workflow_error",
      "message": "..."
    }
  }
}
```

## 9. 状态流转

`router_only`：

```text
waiting_user_input -> ready_for_dispatch
```

`execute`：

```text
waiting_user_input -> ready_for_dispatch -> waiting_assistant_completion -> completed
```

规则：

- 槽位缺失：返回 `waiting_user_input`。
- 槽位齐全且 `router_only`：返回 `ready_for_dispatch`，不调用 workflow。
- 槽位齐全且 `execute`：调用 workflow。
- workflow 节点输出中间帧：`waiting_assistant_completion` / `workflow_node_output`。
- workflow 正常结束：`completed` / `workflow_done`。
- workflow 失败：`failed` / `workflow_error`。
