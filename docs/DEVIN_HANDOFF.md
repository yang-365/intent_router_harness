# Devin 交接文档：DeepAgent 企业级 Harness Runtime

本文用于把当前分支的后续工作交给 Devin。第一节是可以直接复制给 Devin 的完整提示词；后续章节是项目上下文、已完成范围、验证方式和下一步建议。

## 1. 给 Devin 的完整提示词

```text
你将接手 intent_router_harness 项目的 DeepAgent 企业级 Harness Runtime 后续开发。

仓库信息：
- upstream: https://github.com/corelli359/intent_router_harness.git
- origin: https://github.com/yang-365/intent_router_harness.git
- 当前工作分支: codex/deepagent-harness-runtime
- 当前已推送提交: fa0f085 Add DeepAgent harness runtime
- 当前分支已推送到 origin/codex/deepagent-harness-runtime
- 工作目录建议: /Users/hongyang/Code/intent_router_harness

核心目标：
把原有 intent router 从多段固定 planner 逐步重构为“围绕 DeepAgent 的企业级 harness”：
1. 前端接口协议尽量保持原来的 /api/v1/message，不破坏现有 UI 和调用方。
2. 底层使用 DeepAgent 原生能力：skill progressive disclosure、任务规划、LangChain tool call、middleware/hook。
3. 对客银行场景要求稳定、低迭代、多用户隔离、高并发可控。
4. 强校验和稳定性不要依赖 prompt 软约束，要通过 hook/middleware 或工具执行层保证。
5. 业务知识不能硬编码在 src/intent_router_harness/，必须放在 skills/、references/、hooks/、tools/ 或示例配置中。

当前已经完成的能力：
- 新增 DeepAgent runtime，配置项 agent_runtime = "deepagent"。
- 保留原 /api/v1/message 和 SSE event: trace/message/done。
- 新增 src/intent_router_harness/deepagent_service.py。
- 支持内存 session store，不使用 file session store，不引入 Redis/PostgreSQL。
- 支持多用户隔离：thread_id = "{custID}:{sessionId}"。
- 支持同一 session 非重入 run lock，不同 session 可并发。
- 支持 DeepAgent 原生 skill 文件加载，把本地 skills 映射到虚拟 /skills/。
- 支持 workflow_api_call LangChain tool call，模型只能传 method/url/body，不开放 headers。
- workflow URL 白名单在通用配置 [workflow].allowed_urls 中，不放 reference。
- workflow 响应 SSE 中每个 additional_kwargs.node_output 作为整体映射到 Router output。
- workflow done 后最终帧为 completed / workflow_done，output 为最后一个 node_output。
- 增加 mock workflow server 和 DeepAgent E2E 检查脚本。
- 增加 UI 验证台关键步骤 trace：请求进入、session 读取、DeepAgent 开始、LangChain 工具调用开始/完成、协议帧生成等。

重要设计约束：
- 不要把掌银转账、缴费等业务 intent code、slot 名、示例话术、接口路径等硬编码到 src/intent_router_harness/。
- config_variables 是前端请求报文的一部分，写提示词时可以引用其中变量。
- headers 不给模型看，也不允许模型生成；workflow tool 内部固定处理。
- skill reference 中 workflow_request.md 应写完整 URL，不再使用 base_url。
- request 调用方式由 workflow_request.md 自然语言描述，提槽逻辑由 slot_filling.md 单独描述。
- response 固定解析逻辑适合 JSON/JSONPath/hook；不需要模型参与的解析不要丢给模型。
- hook 与 skills 平级，hooks.json 指向一组 hook 目录；每个 hook 一个目录，目录内有有意义的 Python 脚本和配置文件。
- tools 也独立成目录，当前有 workflow-api-call 工具。
- 当前用户希望最终形成企业级通用 harness 范式：一个大的 agent loop，一些强校验通过 hook/middleware 完成。

你接手后的第一步：
1. 查看当前分支状态：
   git status --short --branch
   git log -1 --oneline
2. 阅读：
   docs/DEEPAGENT_HARNESS_RUNTIME.md
   docs/WORKFLOW_E2E_TESTING.md
   docs/WORKFLOW_TOOL_CALLING.md
   src/intent_router_harness/deepagent_service.py
   examples/deepagent-finance-router-harness.toml
   examples/mock_workflow_server.py
   examples/deepagent_e2e_check.py
   tests/test_deepagent_service.py
3. 运行完整测试：
   .venv/bin/python -m pytest -q
4. 端到端验证 DeepAgent 版 harness：
   终端 A:
   .venv/bin/python examples/mock_workflow_server.py --host 127.0.0.1 --port 9876

   终端 B:
   .venv/bin/intent-router-harness serve examples/deepagent-finance-router-harness.toml --port 8766

   终端 C:
   .venv/bin/python examples/deepagent_e2e_check.py --url http://127.0.0.1:8766/api/v1/message

   浏览器 UI:
   http://127.0.0.1:8766/validator
   输入“给陈广荣转500元”，executionMode 选 execute，确认 trace 中有 deepagent_langchain_tool_call_completed，最终 output.output 是 mock-transfer-done。

需要优先继续推进的方向：
1. 收敛 DeepAgent runtime 与原 classic runtime 的边界，让 /api/v1/message 的协议兼容性更明确。
2. 对 hook/middleware 做企业级稳定性治理：URL 白名单、workflow before/after 事件、错误分类、超时、取消、审计 trace。
3. 完善多意图/多任务规划：使用 DeepAgent 原生 write_todos，协议中 task_list/current_task 要稳定可测。
4. 完善模型调用上下文隔离：每个阶段输入输出清晰，避免不同用户、不同 session、不同 task 之间串上下文。
5. 强化高并发测试：不同 session 并发通过，同一 session 并发拒绝或排队策略明确。
6. 补齐 ASGI/stdlib 两种服务入口下 DeepAgent streaming 的一致性测试。
7. 使用真实 LLM 环境变量时不要提交密钥。环境变量使用占位写法：
   ROUTER_LLM_API_BASE_URL=...
   ROUTER_LLM_API_KEY=...
   ROUTER_LLM_MODEL=...
   ROUTER_LLM_TIMEOUT_SECONDS=120
8. 如果要开 PR，PR 描述必须包含：架构摘要、接口兼容性、验证命令、UI E2E 结果、mock workflow 说明、风险和后续工作。

当前验收基线：
- .venv/bin/python -m pytest -q 通过，最近结果为 79 passed, 1 warning。
- examples/deepagent_e2e_check.py 通过。
- UI /validator 转账场景通过，能看到 workflow_node_output 和 workflow_done。
- 当前分支工作区在提交 fa0f085 后是干净的。

注意事项：
- 不要用真实 API key 写入源码、文档、测试或提交历史。
- 不要删除 classic runtime，除非用户明确确认。
- 不要引入 Redis/PostgreSQL/file session store；当前用户倾向 memory + 前端 ingress 会话保持。
- 不要把 headers 暴露给模型。
- 不要把 workflow base_url 重新引入 reference；当前要求 skill 里的 URL 是完整地址。
- 如果发现 upstream 又更新，先 fetch/rebase 或 merge，并完整跑测试和 UI E2E。
```

## 2. 当前代码状态

当前分支：

```text
codex/deepagent-harness-runtime
```

已推送提交：

```text
fa0f085 Add DeepAgent harness runtime
```

远端：

```text
origin   https://github.com/yang-365/intent_router_harness.git
upstream https://github.com/corelli359/intent_router_harness.git
```

建议 PR 链接：

```text
https://github.com/yang-365/intent_router_harness/pull/new/codex/deepagent-harness-runtime
```

当前分支在最近一次提交后工作区干净。

## 3. 本轮已经完成的主要改动

新增和修改的重点文件：

| 文件 | 作用 |
| --- | --- |
| `src/intent_router_harness/deepagent_service.py` | DeepAgent runtime 主实现，包含 runner、middleware、workflow tool 适配、协议帧映射。 |
| `src/intent_router_harness/service.py` | 按 `agent_runtime` 选择 classic 或 deepagent service。 |
| `src/intent_router_harness/runtime.py` | harness 配置加载扩展，支持 deepagent/session/workflow 等配置。 |
| `src/intent_router_harness/session_store.py` | 内存 session store 与 run lock，支持用户隔离和同 session 非重入。 |
| `src/intent_router_harness/schema.py` | 请求/响应协议字段补充。 |
| `src/intent_router_harness/debug_ui.py` | UI 验证台补齐 config_variables 和关键 trace 展示。 |
| `examples/deepagent-finance-router-harness.toml` | DeepAgent 版 finance harness 示例配置。 |
| `examples/mock_workflow_server.py` | 本地 mock workflow SSE 服务。 |
| `examples/deepagent_e2e_check.py` | 命令行端到端检查脚本。 |
| `docs/DEEPAGENT_HARNESS_RUNTIME.md` | DeepAgent harness runtime 设计和启动说明。 |
| `docs/WORKFLOW_E2E_TESTING.md` | mock workflow 和 UI E2E 测试说明。 |
| `tests/test_deepagent_service.py` | DeepAgent runtime、workflow、middleware、并发、协议映射测试。 |
| `tests/test_session_store.py` | 内存 session store 和 run lock 测试。 |

已有目录结构约定：

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

## 4. DeepAgent Runtime 架构摘要

目标链路：

```text
前端 /api/v1/message
  -> Harness 请求校验 / 会话隔离 / run lock
  -> DeepAgent 单主循环
       -> skill progressive disclosure
       -> 多意图任务规划
       -> slot_filling reference 提槽
       -> workflow_request reference 组装 method/url/body
       -> LangChain workflow_api_call tool
       -> before/after hook 或 middleware 强校验
  -> Assistant Protocol SSE/JSON
```

对客银行场景的受控顺序：

```text
先意图识别
  -> 加载对应 skill
  -> 根据 skill reference 提取参数
  -> 槽位齐全且 executionMode=execute 时触发 tool call
  -> 返回 workflow 的最终 output
```

外部接口保持：

```http
POST /api/v1/message
Accept: text/event-stream
Content-Type: application/json
```

SSE event 保持：

```text
event: trace
event: message
event: done
```

workflow event 映射规则：

```text
Router.output = workflow_event.data.additional_kwargs.node_output
```

不展开、不理解、不校验 `node_output` 内部业务结构。

## 5. 已验证结果

最近一次本地验证：

```bash
.venv/bin/python -m pytest -q
```

结果：

```text
79 passed, 1 warning
```

DeepAgent E2E：

```bash
.venv/bin/python examples/deepagent_e2e_check.py --url http://127.0.0.1:8774/api/v1/message --session-id e2e_limit_fix_001
```

结果：

```text
final.status = completed
final.completion_reason = workflow_done
final.output = {"output": "mock-transfer-done", "exception": null}
slot_memory = {"payee_name": "陈广荣", "amount": 500}
```

UI E2E：

```text
http://127.0.0.1:8774/validator
```

转账输入：

```text
给陈广荣转500元
```

页面已确认出现关键 trace：

```text
请求进入Router
Session生命周期读取
任务运行态读取
DeepAgent运行开始
LangChain工具调用开始
LangChain工具调用完成
DeepAgent运行完成
SSE业务帧生成
```

最终页面结果：

```text
workflow_node_output x 3
workflow_done
output.output = mock-transfer-done
```

## 6. 本地启动和端到端测试手册

安装依赖：

```bash
.venv/bin/python -m pip install -e '.[test,deepagent]'
```

启动 mock workflow：

```bash
.venv/bin/python examples/mock_workflow_server.py --host 127.0.0.1 --port 9876
```

启动 DeepAgent harness：

```bash
.venv/bin/intent-router-harness serve examples/deepagent-finance-router-harness.toml --port 8766
```

健康检查：

```bash
curl -s http://127.0.0.1:8766/healthz
```

命令行 E2E：

```bash
.venv/bin/python examples/deepagent_e2e_check.py --url http://127.0.0.1:8766/api/v1/message
```

UI E2E：

```text
http://127.0.0.1:8766/validator
```

UI 操作：

1. executionMode 选择 `execute`。
2. 输入 `给陈广荣转500元`。
3. 发送消息。
4. 检查 trace 中存在 `deepagent_langchain_tool_call_completed`。
5. 检查最终业务帧为 `completed / workflow_done`。
6. 检查 `output.output = mock-transfer-done`。

## 7. 关键实现细节

DeepAgent runtime 中比较关键的 middleware/控制点：

| 控制点 | 职责 |
| --- | --- |
| `HarnessContextMiddleware` | 在模型调用前注入 harness 已加载 skill metadata 和 reference 正文。 |
| `HarnessSkillFileMiddleware` | 拦截 DeepAgent `read_file('/skills/...')`，返回 harness 虚拟文件内容。 |
| `WorkflowRequestMiddleware` | 兜底处理模型输出 workflow_request JSON 但未触发真正 tool call 的情况。 |
| `WorkflowCompletionMiddleware` | 检测到 `workflow_api_call` ToolMessage 后直接结束 agent loop，避免二次总结和循环。 |
| `ModelCallLimitMiddleware` | 限制单次对客请求真实模型调用次数。 |
| `_deepagent_graph_recursion_limit(...)` | 将 LangGraph 图步数上限与模型调用次数上限分离，为 tool node 留执行空间。 |

需要特别注意：

- `max_iterations` 不宜过高，掌银对客场景建议控制在 12 到 20。
- LangGraph `recursion_limit` 不能简单等于模型调用上限，否则模型刚发出 tool call 后 tool node 可能还没执行就耗尽图步数。
- 当前实现已把图步数至少放到 40，真实模型调用次数仍由 middleware 控制。

## 8. 后续建议任务

建议按优先级推进：

1. 完善 DeepAgent runtime 与 classic runtime 的兼容矩阵，明确哪些协议字段完全一致，哪些是 DeepAgent 专属 trace。
2. 增加 ASGI 模式下 DeepAgent streaming E2E 测试。
3. 增加并发压测脚本：不同 `(custID, sessionId)` 并发通过，同一 `(custID, sessionId)` 非重入返回稳定错误。
4. 把 workflow before/after 事件进一步标准化成 hook/middleware command 事件，减少 runtime 内部特殊分支。
5. 让多意图场景真正落到 DeepAgent 原生 `write_todos`，并补充 task_list/current_task 的端到端测试。
6. 补充 workflow 错误分类：URL 不在白名单、HTTP 非 2xx、SSE 非 JSON、缺少 node_output、超时、中断。
7. 补充 UI E2E 自动化脚本或浏览器测试说明，避免只依赖人工点页面。
8. 合并 upstream 最新代码前后都跑完整 pytest、mock workflow E2E、UI E2E。
9. 开 PR 时把 mock workflow 和运行文档纳入 PR 描述，确保评审方能本地复现。

## 9. 风险和边界

当前仍需关注的风险：

- DeepAgent 依赖是可选 extra，部署环境必须明确安装 `.[deepagent]`。
- 内存 session store 依赖 ingress 会话保持；多副本非粘性路由会丢上下文。
- 真实 LLM 可能不稳定触发 tool call，当前有 middleware 兜底，但仍需更多真实模型回归。
- 多意图规划已有系统约束和测试方向，但还需要更完整的真实 E2E 场景。
- hook 目录结构已经按用户要求整理，但是否完全等同 DeepAgent 官方 hook 标准仍需后续继续对齐。

明确不要做的事：

- 不要提交真实密钥。
- 不要把业务规则硬编码进 `src/intent_router_harness/`。
- 不要重新引入 workflow base_url。
- 不要把 headers 暴露给模型。
- 不要引入 Redis、数据库或 file session store，除非用户改变决策。
- 不要删除 classic runtime，除非用户明确要求。

## 10. PR 描述模板

```markdown
## Summary

Adds a DeepAgent-backed enterprise harness runtime while preserving the existing `/api/v1/message` protocol and SSE events. The new runtime uses DeepAgent native skill loading, task planning, LangChain tool calls, and harness middleware/hooks for stability controls.

## Key Changes

- Add `agent_runtime = "deepagent"` configuration path.
- Add native DeepAgent runner and middleware around skill context, virtual skill files, workflow tool execution, workflow completion, and model-call limits.
- Add memory-only session isolation and same-session run lock.
- Add `workflow_api_call(method, url, body)` LangChain tool without exposing headers to the model.
- Add mock workflow server and DeepAgent E2E check script.
- Add documentation for DeepAgent runtime and local workflow E2E testing.

## Compatibility

- Keeps `POST /api/v1/message`.
- Keeps SSE `trace`, `message`, and `done` events.
- Keeps workflow `node_output` as an opaque output value.
- Classic runtime remains available.

## Verification

- `.venv/bin/python -m pytest -q`
- `.venv/bin/python examples/mock_workflow_server.py --host 127.0.0.1 --port 9876`
- `.venv/bin/intent-router-harness serve examples/deepagent-finance-router-harness.toml --port 8766`
- `.venv/bin/python examples/deepagent_e2e_check.py --url http://127.0.0.1:8766/api/v1/message`
- UI: `http://127.0.0.1:8766/validator`, transfer scenario `给陈广荣转500元`

## Notes

- DeepAgent dependencies are installed via `.[deepagent]`.
- Runtime uses in-memory sessions and relies on ingress session affinity for multi-replica deployments.
- No real secrets are included.
```
