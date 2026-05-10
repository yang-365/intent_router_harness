# Workflow 端到端测试说明

本文说明如何在本地用 mock 子工作流跑通 Router 的 workflow 调用链路。测试目标是验证：

- 前端调用 Router `/api/v1/message`。
- Router 识别转账意图并提取 slots。
- 提槽阶段默认加载 `transfer-routing/references/slot_filling.md`。
- `executionMode=execute` 时 Router 调用 mock workflow。
- mock workflow 的每个 `additional_kwargs.node_output` 被整体映射到 Router SSE 的 `output`。
- 最终返回 `completed / workflow_done`。

## 1. 准备环境

在项目根目录使用 `.venv`：

```bash
python -m venv .venv
.venv/bin/python -m pip install -e '.[test]'
```

本地 LLM 配置继续放在 `.env.local`，例如：

```bash
BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
API_KEY=...
MODEL=qwen3.6-flash-2026-04-16
ROUTER_LLM_ENABLE_THINKING=false
```

## 2. 启动 mock 子工作流

终端 A：

```bash
.venv/bin/python examples/mock_workflow_server.py --host 127.0.0.1 --port 9876
```

启动成功后会看到：

```text
mock workflow serving on http://127.0.0.1:9876
```

mock 当前内置了几个子工作流 endpoint：

| workflow agent id | 用途 |
| --- | --- |
| `workflow-agent-1-1b14f16b` | 转账 workflow，URL 由模型按 `transfer-routing/references/workflow_request.md` 输出，并由 harness 通用配置中的 `workflow.allowed_urls` 白名单校验。 |
| `workflow-agent-payee` | 收款人列表示例 workflow。 |
| `workflow-agent-bill` | 缴费示例 workflow。 |

收到调用后，mock 会在终端打印 `MOCK_WORKFLOW_REQUEST ...`，便于检查 Router 组装的请求体。

## 3. 启动 Router

终端 B：

```bash
.venv/bin/intent-router-harness serve examples/finance-router-harness.toml --port 8766
```

健康检查：

```bash
curl -s http://127.0.0.1:8766/healthz
```

预期返回：

```json
{"status":"ok"}
```

## 4. UI 验证转账场景

浏览器打开：

```text
http://localhost:8766/validator
```

页面操作：

1. 执行模式选择 `execute`。
2. 用户输入填写 `给陈广荣转500元`。
3. 点击 `发送消息`。

预期 UI 结果：

- trace 中出现 `Skill渐进式加载`，当前任务加载 `skill=transfer-routing`。
- trace 中出现 `Reference正文加载`，加载 `slot_filling` 和 `workflow_request`。
- 业务帧先返回 `router_ready_for_dispatch`。
- 随后返回多个 `workflow_node_output`。
- 最终返回 `workflow_done`，状态为 `completed`。
- 最近业务帧中的 `output` 为 mock workflow 最后一个 `node_output`：

```json
{
  "output": "mock-transfer-done",
  "exception": null
}
```

终端 A 的 mock 日志应包含类似请求：

```json
{
  "path": "/agent-api/workflow-agent-1-1b14f16b/chatabc/use_as_tool",
  "body": {
    "session_id": "ui_xxx",
    "txt": "给陈广荣转500元",
    "stream": true,
    "config_variables": [
      { "name": "custID", "value": "C0001" },
      { "name": "sessionID", "value": "ui_xxx" },
      { "name": "currentDisplay", "value": "validator_page" },
      { "name": "agentSessionID", "value": "ui_xxx" },
      { "name": "slots_data", "value": "{\"payee_name\": \"陈广荣\", \"amount\": 500}" }
    ]
  }
}
```

`slots_data` 中的 `amount` 可能是字符串 `"500"` 或数字 `500`，取决于模型当轮输出；该字段由模型按 `workflow_request.md` 组装，Router 负责校验 URL 白名单并使用内置 workflow SSE 请求头。

## 5. 命令行快速验证

也可以直接用 curl 调 Router：

```bash
curl -N -X POST http://127.0.0.1:8766/api/v1/message \
  -H 'Accept: text/event-stream' \
  -H 'Content-Type: application/json' \
  -d '{
    "sessionId": "e2e-transfer-001",
    "custID": "C0001",
    "txt": "给陈广荣转500元",
    "stream": true,
    "executionMode": "execute",
    "debugTrace": true,
    "config_variables": [
      { "name": "custID", "value": "C0001" },
      { "name": "sessionID", "value": "e2e-transfer-001" },
      { "name": "currentDisplay", "value": "validator_page" },
      { "name": "agentSessionID", "value": "e2e-transfer-001" }
    ]
  }'
```

输出中应能看到：

```text
"completion_reason":"workflow_node_output"
"completion_reason":"workflow_done"
"output":{"output":"mock-transfer-done","exception":null}
event: done
data: [DONE]
```

## 6. 运行自动化测试

```bash
.venv/bin/python -m pytest -q
```

如果测试环境禁止绑定本地端口，需要在允许本地网络监听的环境中运行。
