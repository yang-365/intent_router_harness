# Workflow 端到端操作手册

本文说明如何在本地启动 Router、启动 mock workflow，并通过 UI 或命令行完成转账场景端到端验证。

## 1. 项目结构

```text
src/intent_router_harness/   # Router 框架代码
skills/                      # 业务 skill 和 reference
hooks/                       # workflow before/after hook
tools/                       # runtime 工具，例如 workflow-api-call
examples/                    # 示例配置、mock workflow、调试 client
docs/                        # 文档
tests/                       # 自动化测试
regressions/                 # 协议回归数据
```

关键文件：

```text
examples/finance-router-harness.toml
examples/mock_workflow_server.py
examples/router_message_client.py
skills/transfer-routing/SKILL.md
skills/transfer-routing/references/slot_filling.md
skills/transfer-routing/references/workflow_request.md
docs/WORKFLOW_E2E_TESTING.md
docs/WORKFLOW_TOOL_CALLING.md
```

## 2. 准备虚拟环境

在项目根目录执行：

```bash
cd /Users/hongyang/Code/intent_router_harness

python -m venv .venv
.venv/bin/python -m pip install -e '.[test]'
```

如果 `.venv` 已经存在，可以直接执行：

```bash
.venv/bin/python -m pip install -e '.[test]'
```

## 3. 配置大模型

创建或修改 `.env.local`：

```bash
ROUTER_LLM_API_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
ROUTER_LLM_API_KEY=你的key
ROUTER_LLM_MODEL=qwen3.6-flash-2026-04-16
ROUTER_LLM_TEMPERATURE=0
ROUTER_LLM_TIMEOUT_SECONDS=30
ROUTER_LLM_ENABLE_THINKING=false
```

也可以使用其他 OpenAI-compatible API，例如：

```bash
ROUTER_LLM_API_BASE_URL=https://api.siliconflow.cn/v1
ROUTER_LLM_API_KEY=你的key
ROUTER_LLM_MODEL=Qwen/Qwen3-Coder-30B-A3B-Instruct
ROUTER_LLM_TIMEOUT_SECONDS=120
ROUTER_LLM_TEMPERATURE=0
```

测试 LLM 连通性：

```bash
.venv/bin/python -m intent_router_harness llm-smoke --env-file .env.local
```

预期能看到类似 `OK` 的返回。

## 4. 启动 Mock Workflow

打开终端 A：

```bash
cd /Users/hongyang/Code/intent_router_harness

.venv/bin/python examples/mock_workflow_server.py --host 127.0.0.1 --port 9876
```

成功后输出：

```text
mock workflow serving on http://127.0.0.1:9876
```

mock workflow 支持以下 endpoint：

```text
/agent-api/workflow-agent-1-1b14f16b/chatabc/use_as_tool
/agent-api/workflow-agent-payee/chatabc/use_as_tool
/agent-api/workflow-agent-bill/chatabc/use_as_tool
```

转账场景使用第一个 endpoint。

## 5. 启动 Router

打开终端 B：

```bash
cd /Users/hongyang/Code/intent_router_harness

.venv/bin/python -m intent_router_harness serve examples/finance-router-harness.toml --port 8765
```

成功后输出：

```text
intent_router_harness serving examples/finance-router-harness.toml on http://127.0.0.1:8765
```

健康检查：

```bash
curl -s http://127.0.0.1:8765/healthz
```

预期返回：

```json
{"status": "ok"}
```

## 6. 前端 UI 测试

浏览器打开：

```text
http://127.0.0.1:8765/validator
```

页面操作：

1. 执行模式选择 `execute`。
2. 用户输入填写 `给陈广荣转500元`。
3. 请求中的 `config_variables` 至少包含：

```json
[
  { "name": "custID", "value": "C0001" },
  { "name": "sessionID", "value": "ui-transfer-001" },
  { "name": "currentDisplay", "value": "validator_page" },
  { "name": "agentSessionID", "value": "ui-transfer-001" }
]
```

4. 点击发送。

预期 UI/SSE 中出现：

```text
intent_recognized
router_ready_for_dispatch
workflow_node_output
workflow_node_output
workflow_node_output
workflow_done
completed
```

终端 A 的 mock workflow 应打印：

```text
MOCK_WORKFLOW_REQUEST ...
```

请求体类似：

```json
{
  "session_id": "ui-transfer-001",
  "txt": "给陈广荣转500元",
  "stream": true,
  "config_variables": [
    { "name": "custID", "value": "C0001" },
    { "name": "sessionID", "value": "ui-transfer-001" },
    { "name": "currentDisplay", "value": "validator_page" },
    { "name": "agentSessionID", "value": "ui-transfer-001" },
    { "name": "slots_data", "value": "{\"payee_name\":\"陈广荣\",\"amount\":500}" }
  ]
}
```

## 7. 命令行端到端测试

可以直接通过 `curl` 调 Router：

```bash
curl -sN -X POST http://127.0.0.1:8765/api/v1/message \
  -H 'Accept: text/event-stream' \
  -H 'Content-Type: application/json' \
  -d '{
    "sessionId": "e2e-transfer-001",
    "custID": "C0001",
    "txt": "给陈广荣转500元",
    "stream": true,
    "executionMode": "execute",
    "debugTrace": false,
    "config_variables": [
      { "name": "custID", "value": "C0001" },
      { "name": "sessionID", "value": "e2e-transfer-001" },
      { "name": "currentDisplay", "value": "validator_page" },
      { "name": "agentSessionID", "value": "e2e-transfer-001" }
    ]
  }'
```

成功输出中应包含：

```text
"completion_reason": "workflow_node_output"
"completion_reason": "workflow_done"
"status": "completed"
"output": {"output": "mock-transfer-done", "exception": null}
event: done
data: [DONE]
```

也可以使用项目自带 client：

```bash
.venv/bin/python examples/router_message_client.py \
  --base-url http://127.0.0.1:8765 \
  --execution-mode execute \
  --session-id e2e-transfer-002 \
  --cust-id C0001 \
  --current-display validator_page \
  --txt '给陈广荣转500元'
```

## 8. 自动化测试

运行全量测试：

```bash
.venv/bin/python -m pytest -q
```

当前验证结果：

```text
50 passed
```

如果测试环境禁止绑定本地端口，HTTP/ASGI 相关测试可能失败，需要在允许监听 `127.0.0.1` 临时端口的环境中运行。

## 9. 常见问题

### 端口占用

查看端口占用：

```bash
lsof -nP -iTCP:9876 -sTCP:LISTEN
lsof -nP -iTCP:8765 -sTCP:LISTEN
```

换端口启动 Router：

```bash
.venv/bin/python -m intent_router_harness serve examples/finance-router-harness.toml --port 8770
```

注意：mock workflow 默认端口 `9876` 写在 `workflow_request.md` 和 `finance-router-harness.toml` 白名单里。如果要改 mock 端口，需要同步修改这两个地方。

### LLM 未输出 workflow_request

重点检查：

- `executionMode` 是否为 `execute`
- `workflow_request.md` 是否加载
- `config_variables` 是否包含 `custID/sessionID/currentDisplay/agentSessionID`
- `debugTrace` 中是否出现 `Reference正文加载 workflow_request`

### Workflow 被白名单拦截

检查 `examples/finance-router-harness.toml`：

```toml
[workflow]
allowed_urls = [
  "http://127.0.0.1:9876/agent-api/workflow-agent-1-1b14f16b/chatabc/use_as_tool",
]
```

模型输出的 `workflow_request.url` 必须和白名单完全一致。
