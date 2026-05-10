# DeepAgent Harness Runtime 设计说明

本文说明新增的 DeepAgent 运行时方向：Router 不再把意图识别、提槽、工具调用、响应处理拆成多段固定 planner，而是围绕一个 DeepAgent 主循环组织能力。强校验、审计、协议映射继续放在 harness 层完成。

## 1. 目标

新的通用范式：

```text
前端 /api/v1/message
  -> Harness 会话隔离和请求校验
  -> DeepAgent 一个主循环
       -> 原生 skill progressive disclosure
       -> 原生任务规划 / 子任务拆解
       -> 工具调用 workflow_api_call
       -> hook 强校验
  -> Assistant Protocol 帧
```

对客银行场景的受控执行顺序固定为：

```text
意图识别
  -> 命中并加载对应 skill/reference
  -> 根据 skill/reference 提取参数和补齐槽位
  -> 槽位齐全且 executionMode=execute 时触发 LangChain workflow_api_call
  -> Router 返回工具 final_output
```

多意图 / 多任务场景使用 DeepAgent 原生任务规划能力：

```text
识别到多个意图或多个任务
  -> 使用 DeepAgent write_todos 生成任务计划
  -> Assistant Protocol 返回 task_list，顺序与用户表达一致
  -> current_task 指向当前推进任务
  -> 每个任务独立加载对应 skill/reference 并提槽
  -> 资金类 workflow_api_call 串行触发，不并行执行
```

约束：

- 意图不明确、不支持、未命中 skill：不调用工具。
- 必填槽位缺失：只返回 `waiting_user_input` 或追问，不调用工具。
- `router_only`：只到 `ready_for_dispatch`，不调用工具。
- `execute`：槽位齐全后必须触发 LangChain tool call，而不是输出 tool JSON 文本。
- workflow 完成后，Router 以完整 `final_output` 作为最终 `output`。
- 一次用户输入包含多个任务时，必须先规划 `task_list`，一次只推进一个 `current_task`。

核心原则：

- 业务逻辑继续放在 `skills/` 和 skill references 中。
- 工具怎么调用，由 reference 里的自然语言说明驱动模型生成 `method/url/body`。
- Header 不开放给模型，workflow 工具内部固定处理。
- URL 安全由通用白名单和 hook 保证。
- Router 不理解 workflow `node_output` 的业务含义，只负责透传或协议映射。
- 多用户隔离由 `custID + sessionId` 组成 thread/session 边界。

## 2. 当前落地范围

本次新增的是 DeepAgent runtime 的第一版适配层：

| 能力 | 当前状态 |
| --- | --- |
| 配置选择运行时 | `agent_runtime = "classic"` 或 `"deepagent"` |
| DeepAgent 原生 skill 文件 | Router 加载本地 `skills/`，Native runner 将 skill 目录种到 DeepAgent 文件系统 `/skills/`，由 DeepAgent 原生 skill middleware 按需读取 |
| 单主循环入口 | `NativeDeepAgentRunner.run_message(...)` 调用 `create_deep_agent(...).astream(...)`，消费 `messages/updates` 流 |
| 多任务规划 | 使用 DeepAgent 内置 `write_todos`/todo state 做多意图任务拆解，Router 协议使用 `task_list/current_task` 承载 |
| workflow 工具 | 通过 LangChain `StructuredTool` 暴露 `workflow_api_call(method, url, body)`，不暴露 headers |
| hook/middleware 强校验 | workflow 工具内部触发 `before_workflow_tool_call`；DeepAgent middleware 在 workflow 工具完成后直接结束 agent loop |
| 多用户隔离 | `thread_id = "{custID}:{sessionId}"`，session store 按 `custID` 绑定 |
| 高并发保护 | 不同 session 可并发；同一用户同一 session 使用非阻塞 run lock |
| 结果协议 | DeepAgent 最终必须返回 Assistant Protocol JSON |

后续可以继续把当前 command hook 适配为 DeepAgent/LangGraph middleware，但业务边界已经按这个方向收拢。

## 2.1 与官方 CLI 示例的关系

官方 `langchain-ai/deepagents` CLI 的核心模式值得沿用：

- 通过 `create_deep_agent(...)` 创建 LangGraph agent。
- 使用 DeepAgent 内置 `TodoListMiddleware/write_todos` 管理复杂任务计划。
- 用 LangGraph `thread_id` 维持一次会话的上下文。
- 工具以 LangChain tool/middleware 形式注册，而不是让模型输出工具 JSON 文本。
- 长任务通过 `astream(..., stream_mode=["messages", "updates"], subgraphs=True, durability="exit")` 消费消息和状态更新。

本项目与官方 CLI 的差别：

| 维度 | 官方 CLI | 企业级 Harness |
| --- | --- | --- |
| 使用场景 | 个人客户端/开发者本地任务 | 掌银对客在线服务 |
| 会话边界 | 本地 thread/session | `custID + sessionId` 强隔离 |
| 并发模型 | 单用户交互为主 | 多用户、高并发、同 session 非重入，依赖 ingress 会话保持 |
| 工具权限 | 用户交互/HITL/本地 allow-list | 服务端 hook、URL 白名单、固定 header |
| 输出 | CLI 文本/终端体验 | Assistant Protocol SSE/JSON |
| 任务规划 | `write_todos` 给用户展示进度 | `write_todos` 做多意图规划，协议用 `task_list/current_task` 承载 |
| 失败处理 | CLI 退出码/终端错误 | 标准 `failed` 帧和错误码 |

因此，本项目不直接复用 CLI shell/server/client 形态，而是参考其 DeepAgent 使用方式，把外层替换成企业服务治理。

当前实现已经采用 CLI 的核心执行形态：同步 HTTP service 入口内部桥接到 DeepAgent 异步流，消费 LangGraph `messages` 和 `updates`，然后再映射回原有 Assistant Protocol。这样前端协议、SSE event 名称和 session 管理保持稳定，同时底层 agent loop 使用 DeepAgent 原生工具调用和任务规划。

为了让掌银对客链路更稳定，runtime 不把稳定性寄托在 prompt 软约束上，而是在 DeepAgent middleware 中做硬控制：

- `HarnessContextMiddleware` 在 model-call 前注入当前 harness 已加载的 skill metadata 与 reference 正文，业务内容仍来自 `skills/` 和 `references/`，不是框架硬编码。
- `HarnessSkillFileMiddleware` 拦截 DeepAgent 的 `read_file`，把 harness 已加载的 `/skills/<skill>/...` 虚拟文件直接返回，避免模型因 reference 路径读不到而反复读文件。
- `WorkflowRequestMiddleware` 兜底处理“模型输出 workflow_request JSON 但没有触发 tool call”的情况，在 middleware 中执行同一个 workflow 工具并写入 `workflow_api_call` ToolMessage。
- `WorkflowCompletionMiddleware` 检测到 `workflow_api_call` 的 ToolMessage 后，直接 `jump_to=end`，不再让模型继续二次总结或循环。
- `ModelCallLimitMiddleware` 对单轮模型调用数设上限，避免对客请求无限迭代。
- LangGraph `recursion_limit` 与模型调用上限分离：图步数至少为 `40`，为 tool node 留出执行空间；真实 LLM 调用次数仍由 `ModelCallLimitMiddleware` 控制，适合掌银对客场景的低迭代要求。
- runner 兜底：如果 LangGraph 在 workflow 已完成后仍抛出递归/循环异常，Router 直接使用已捕获的 workflow 结果生成 Assistant Protocol 帧。

## 3. 配置示例

示例文件：

```text
examples/deepagent-finance-router-harness.toml
```

关键配置：

```toml
agent_runtime = "deepagent"
skill_roots = ["../skills"]
hook_roots = ["../hooks"]
tool_roots = ["../tools"]

[workflow]
allowed_urls = [
  "http://127.0.0.1:9876/agent-api/workflow-agent-1-1b14f16b/chatabc/use_as_tool",
]

[session]
idle_timeout_seconds = 1800

[deepagent]
model = "openai:gpt-4.1-mini"
max_iterations = 20
skills = ["/skills/"]
```

说明：

- `skill_roots` 是 harness 加载本地 skill 的位置。
- `[deepagent].skills = ["/skills/"]` 表示让 DeepAgent 从虚拟文件系统的 `/skills/` 扫描 skill。
- Native runner 会把本地每个 skill 目录下的 `SKILL.md`、`references/*.md` 等文件按原目录结构放入 `/skills/<skill-dir>/`。
- `workflow.allowed_urls` 是唯一的 URL 白名单配置，不放在 reference 里。
- `[session].idle_timeout_seconds` 控制内存会话空闲过期时间。
- 对客银行场景建议把 `max_iterations` 控制在 12 到 20 之间。超过预算应返回失败或转人工/兜底话术，而不是让 agent 长时间自循环。

## 4. Skill / Reference 分工

推荐目录：

```text
skills/
  transfer-routing/
    SKILL.md
    references/
      slot_filling.md
      workflow_request.md

hooks/
  hooks.json
  restrict-api-url-to-allowlist/
    url_allowlist_hook.json
    enforce_url_allowlist.py
  forward-workflow-node-output/
    workflow_node_output_hook.json
    forward_node_output.py

tools/
  workflow-api-call/
    workflow_api_tool.json
    execute_workflow_api.py
```

职责：

| 文件 | 职责 |
| --- | --- |
| `SKILL.md` | 场景边界、意图路由、什么时候适用。 |
| `slot_filling.md` | 多轮提槽规则，描述需要哪些槽位、如何追问。 |
| `workflow_request.md` | 子工作流接口调用说明，描述完整 URL、method、body 如何由请求变量和槽位组装。 |
| `hooks/*` | 模型不可见的强校验和响应透传逻辑。 |
| `tools/workflow-api-call` | 真正发起 HTTP/SSE workflow 调用。 |

## 5. 多用户与高并发

运行时做三层隔离：

1. `InMemorySessionStore` 将 `sessionId` 绑定到首次访问的 `custID`。同一个 `sessionId` 被另一个 `custID` 复用会被拒绝。
2. DeepAgent `thread_id` 使用 `{custID}:{sessionId}`，避免不同客户的同名会话串上下文。
3. `SessionRunLockStore` 对同一 `(custID, sessionId)` 做非阻塞锁。同一会话已有运行中请求时，立即返回失败帧 `session_run_in_progress`；不同会话不互相阻塞。

状态存储采用轻依赖策略：

- 热路径使用内存：`InMemorySessionStore`、进程内 run lock、DeepAgent `MemorySaver`。
- 不使用 file session store，不引入 Redis/PostgreSQL 这类额外基础设施。
- 多副本部署依赖前端 ingress 会话保持，按 `sessionId` 或等价会话键把同一会话固定路由到同一实例。
- 实例重启、扩缩容迁移或非粘性路由会导致该实例内存会话丢失；前端或上游应能重新建立会话或重试业务流。

## 6. Workflow 工具边界

模型只能触发 LangChain 工具调用：

```python
workflow_api_call(method: str, url: str, body: dict)
```

模型不应该把 `workflow_api_call`、`tool_use` 或 `workflow_request` 写成普通 JSON/Markdown 文本。只有真正进入 LangChain tool call 后，Router 才会执行 workflow。

不开放：

- `headers`
- 任意 HTTP client 参数
- 任意 base URL 拼接

执行顺序：

```text
DeepAgent 触发 LangChain workflow_api_call tool
  -> before_workflow_tool_call hook 校验 URL 白名单
  -> workflow-api-call command 发起 SSE 请求
  -> parse_workflow_sse 解析 event: message
  -> 每个 workflow node_output 映射成一条原 Assistant Protocol message frame
  -> Router 返回 workflow_done，output 为最后一个 node_output
```

DeepAgent runtime 不改变前端接口协议。外部仍然只使用：

```http
POST /api/v1/message
Accept: text/event-stream
```

SSE event 类型仍然是原来的 `trace`、`message`、`done`。关键步骤通过 `trace.stage` 打印出来，包括：

- `request_received`
- `session_loaded`
- `task_runtime_loaded`
- `deepagent_runtime_start`
- `deepagent_langchain_tool_call_started`
- `deepagent_langchain_tool_call_completed`
- `assistant_protocol_frames`

workflow 每个节点输出仍然通过原 `message` 帧返回：

```json
{
  "ok": true,
  "status": "waiting_assistant_completion",
  "intent_code": "AG_TRANS",
  "completion_state": 1,
  "completion_reason": "workflow_node_output",
  "details": {
    "node_id": "end",
    "node_title": "结束",
    "timestamp": "2026-05-08 19:19:04.553"
  },
  "output": {
    "output": "...",
    "exception": null
  },
  "slot_memory": {
    "payee_name": "陈广荣",
    "amount": "500"
  },
  "task_list": [],
  "current_task": {}
}
```

workflow done 后追加最终业务帧：

```json
{
  "ok": true,
  "status": "completed",
  "completion_state": 2,
  "completion_reason": "workflow_done",
  "output": {
    "output": "mock-transfer-done",
    "exception": null
  }
}
```

如果 hook、工具、SSE 解析失败，Router 返回原 Assistant Protocol 失败帧，错误归类为 `workflow_error`：

```json
{
  "ok": false,
  "status": "failed",
  "completion_reason": "workflow_error",
  "errorCode": "workflow_error",
  "output": {
    "error": {
      "code": "workflow_error",
      "message": "..."
    }
  }
}
```

## 7. 启动方式

安装基础依赖：

```bash
.venv/bin/python -m pip install -e '.[test]'
```

安装 DeepAgent 可选依赖：

```bash
.venv/bin/python -m pip install -e '.[test,deepagent]'
```

启动 mock workflow：

```bash
.venv/bin/python examples/mock_workflow_server.py --host 127.0.0.1 --port 9876
```

启动 DeepAgent 版 harness：

```bash
.venv/bin/intent-router-harness serve examples/deepagent-finance-router-harness.toml --port 8766
```

打开 UI：

```text
http://127.0.0.1:8766/validator
```

命令行端到端验收：

```bash
.venv/bin/python examples/deepagent_e2e_check.py --url http://127.0.0.1:8766/api/v1/message
```

该脚本会断言：

- 返回 trace 中存在 `deepagent_langchain_tool_call_completed`。
- 至少存在一条 `workflow_node_output` message 帧。
- 最终 message 帧为 `completed / workflow_done`。
- 最终 `output.output` 等于 mock workflow 的 `mock-transfer-done`。

## 8. 测试

DeepAgent runtime 适配层单测：

```bash
.venv/bin/python -m pytest -q tests/test_deepagent_service.py
```

完整回归：

```bash
.venv/bin/python -m pytest -q
```

当前新增测试覆盖：

- DeepAgent runtime 会把请求、skills、references、config variables 传给 runner。
- `thread_id` 使用 `custID:sessionId`。
- 同一个 `sessionId` 不允许被不同 `custID` 复用。
- 同一用户同一 session 的并发请求会被 run lock 拒绝。
- workflow SSE 每个 `node_output` 会按原 Assistant Protocol 生成独立 `message` 帧。
- workflow 完成后会保留后续未完成任务，当前完成任务不继续占用运行态。
- workflow 工具、hook、SSE 解析错误统一返回 `workflow_error`。
- DeepAgent middleware 会把 `/skills/<skill>/...` 虚拟路径映射到 harness 已加载的 skill/reference 正文。
- LangGraph 图步数上限会为 tool node 留出空间，模型调用次数仍由 middleware 限制。

## 9. 企业级收口状态

当前 DeepAgent harness 的目标是作为企业级通用 harness 的 runtime 形态，而不是个人 CLI：

- 外部接口保持 `/api/v1/message`、`/api/v1/task/completion` 不变。
- 前端 SSE 展示继续使用原 `trace/message/done`。
- session 使用 memory-only，依赖 Ingress sticky session 支撑多副本。
- workflow 强校验通过 hooks 和全局 allowlist 完成。
- 主路径要求模型触发真实 LangChain `workflow_api_call` tool call；若模型误输出 `workflow_request` JSON，middleware 会执行同一个受控工具并注入 `workflow_api_call` ToolMessage，而不是把 JSON 假调用直接返回给前端。

后续增强可以继续做，但不阻断当前企业 harness 主链路：

1. 将现有 command hook 的 before/after 事件进一步收敛到 LangGraph 原生 middleware 生命周期。
2. 增加更完整的压测脚本和慢 workflow 取消策略。
3. 将更多业务 skill/reference 按同一规范迁移和验收。
