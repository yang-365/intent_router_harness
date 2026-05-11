# 运行测试操作手册

本文档覆盖从环境配置到完整业务流程验证的所有步骤。

> **最后更新**: 2026-05-10 | **分支**: `codex/deepagent-harness-runtime` | **测试数**: 94 passed

---

## 目录

1. [环境准备](#1-环境准备)
2. [配置说明](#2-配置说明)
3. [启动服务](#3-启动服务)
4. [启动 Mock 工作流服务](#4-启动-mock-工作流服务)
5. [单元测试](#5-单元测试)
6. [API 手动测试](#6-api-手动测试)
7. [Debug UI 前端操作指南](#7-debug-ui-前端操作指南)
8. [完整业务场景验证](#8-完整业务场景验证)
9. [E2E 自动化验证](#9-e2e-自动化验证)
10. [Kubernetes 部署](#10-kubernetes-部署)
11. [故障排查](#11-故障排查)

---

## 1. 环境准备

### 1.1 系统要求

- Python 3.11+
- pip（或 uv）
- Git

### 1.2 克隆项目

```bash
git clone https://github.com/yang-365/intent_router_harness.git
cd intent_router_harness
git checkout codex/deepagent-harness-runtime
```

### 1.3 创建虚拟环境

```bash
# 创建 .venv 虚拟环境
python -m venv .venv

# 激活虚拟环境
# Linux / macOS:
source .venv/bin/activate
# Windows:
# .venv\Scripts\activate
```

> **重要：** 后续所有命令（安装依赖、运行测试、启动服务等）都应在激活的 `.venv` 环境中执行。

### 1.4 安装依赖

```bash
# 先升级 pip
.venv/bin/python -m pip install --upgrade pip

# 基础安装（含测试依赖，适合运行单元测试）
.venv/bin/python -m pip install -e '.[test]'

# 完整安装（含 deepagent SDK，真实 LLM E2E 测试和生产环境必需）
.venv/bin/python -m pip install -e '.[deepagent,test]'
```

### 1.5 验证安装

```bash
.venv/bin/python -c "from intent_router_harness import create_app, SessionManager, SkillRegistry; print('OK')"
```

如果安装了 deepagent SDK，还可验证：

```bash
.venv/bin/python -c "from deepagents.deep_agent import create_deep_agent; print('deepagent OK')"
```

---

## 2. 配置说明

### 2.1 TOML Spec 文件

项目有两个配置文件，区别在于 Mock 工作流端口：

| 文件 | Mock 端口 | 说明 |
|------|-----------|------|
| `examples/deepagent-finance-router-harness.toml` | 9876 | 默认配置 |
| `examples/deepagent-finance-router-harness-local.toml` | 9877 | 本地开发配置 |

以本地开发配置为例（`examples/deepagent-finance-router-harness-local.toml`）：

```toml
name = "deepagent-finance-router-harness"
version = "0.1.0"
description = "DeepAgent-backed finance harness POC."
agent_runtime = "deepagent"
agent_paths = ["../agent.md"]          # Agent 系统提示词
skill_roots = ["../skills"]            # Skill 目录
hook_roots = ["../hooks"]              # Hook 目录
tool_roots = ["../tools"]              # Tool 目录
max_skill_body_chars = 6000            # Skill body 最大字符数
max_reference_body_chars = 6000        # Reference 最大字符数
max_reference_count = 4                # 最大 reference 加载数

[workflow]
allowed_urls = [                       # 白名单 URL（安全约束）
  "http://127.0.0.1:9877/agent-api/workflow-agent-1-1b14f16b/chatabc/use_as_tool",
  "http://127.0.0.1:9877/agent-api/workflow-agent-payee/chatabc/use_as_tool",
  "http://127.0.0.1:9877/agent-api/workflow-agent-bill/chatabc/use_as_tool",
]

[session]
idle_timeout_seconds = 1800            # Session 30 分钟超时

[deepagent]
model = "openai:Qwen/Qwen3-Coder-30B-A3B-Instruct"  # LLM 模型
max_iterations = 20                    # Agent 最大迭代次数
skills = ["/skills/"]                  # deepagent SDK skill 搜索路径

[[bindings]]
skill = "transfer-routing"             # 转账技能绑定
load = "metadata"                      # 启动时只加载元数据

[[bindings]]
skill = "bill-payment-routing"         # 缴费技能绑定
load = "metadata"
```

### 2.2 环境变量

复制模板并填入真实凭据：

```bash
cp .env.example .env
```

`.env` 文件内容（使用 SiliconFlow 提供的 LLM）：

```bash
# LLM 提供商配置（连接真实 LLM 时必填）
# deepagent SDK 使用 langchain_openai (ChatOpenAI)，读取标准 OpenAI SDK 环境变量：
OPENAI_API_BASE=https://api.siliconflow.cn/v1
OPENAI_API_KEY=sk-your-real-key-here
```

> **重要：** 环境变量必须使用 `OPENAI_API_KEY` 和 `OPENAI_API_BASE`（标准 OpenAI SDK 变量名）。
> LLM 模型在 TOML 配置文件的 `[deepagent].model` 中指定（如 `"openai:Qwen/Qwen3-Coder-30B-A3B-Instruct"`），不需要环境变量。

> **注意：** 单元测试不需要 LLM 凭据。只有 E2E 测试和生产运行需要。

### 2.3 Skill 目录结构

```
skills/
├── transfer-routing/                  # 转账意图 AG_TRANS
│   ├── SKILL.md                       # 意图识别规则
│   │   ├── frontmatter: name + description（deepagent 原生读取，用于意图匹配）
│   │   └── body: 意图边界判定逻辑（Step1~Step4）
│   └── references/
│       ├── slot_filling.md            # 提槽逻辑（payee_name, amount）
│       └── workflow_request.md        # API 调用方式（workflow_api_call 工具参数）
└── bill-payment-routing/              # 缴费意图 AG_PAY_BILL
    └── SKILL.md                       # 意图识别 + 补槽规则（无单独 reference）
```

### 2.4 渐进式 Skill 加载流程

这是 harness 的核心流程之一，严格按以下顺序执行：

```
Phase 1: Metadata 注入（每次请求）
  └─ deepagent 原生读取所有 skill 的 name + description → 用于意图识别

Phase 2: Skill Body + Reference 加载（意图命中后）
  └─ 检测到 intent_code → 加载匹配 skill 的 SKILL.md body + ALL references
  └─ LLM 按 slot_filling.md 中的业务规则进行提槽
  └─ LLM 按 workflow_request.md 中的说明调用 workflow_api_call 工具

Phase 3: 卸载（任务切换或完成）
  └─ 前端调 /completion → 任务完成 → 从上下文卸载当前 skill
  └─ 如有下一个任务 → 加载新 skill 的 body + references
```

---

## 3. 启动服务

### 3.1 前置条件

确保 `.env` 已配置好 LLM 凭据（参见 2.2）。

### 3.2 启动 Harness 服务

```bash
# 方式一：使用 Makefile（默认端口 8765，使用默认 TOML 配置）
make serve

# 方式二：指定本地开发配置（确保已激活 .venv）
.venv/bin/python -m intent_router_harness serve examples/deepagent-finance-router-harness-local.toml --port 8765

# 方式三：带热重载（开发模式）
.venv/bin/python -m intent_router_harness serve examples/deepagent-finance-router-harness-local.toml --port 8765 --reload
```

启动后终端输出：

```
INFO:     Uvicorn running on http://0.0.0.0:8765 (Press CTRL+C to quit)
```

### 3.3 验证服务健康

```bash
# 存活检查
curl -s http://127.0.0.1:8765/healthz
# 返回: {"status":"ok"}

# 就绪检查
curl -s http://127.0.0.1:8765/readyz
# 返回: {"ready":true,"service":"intent_router_harness","version":"2.0.0"}

# 首页（返回 Debug UI 页面）
curl -s http://127.0.0.1:8765/ | head -5
# 返回: <!doctype html>...（Intent Router 验证台 HTML）
```

---

## 4. 启动 Mock 工作流服务

Mock 服务模拟下游工作流 API（转账、收款人列表、缴费），用于本地 E2E 测试。

### 4.1 启动

> **重要：** Mock 端口必须和 TOML 配置文件中 `[workflow].allowed_urls` 的端口一致。

```bash
# 方式一：使用 Makefile（默认端口 9876，对应默认 TOML）
make mock-workflow

# 方式二：指定端口 9877（对应 local TOML，确保已激活 .venv）
.venv/bin/python examples/mock_workflow_server.py --host 127.0.0.1 --port 9877
```

输出：`mock workflow serving on http://127.0.0.1:9877`

### 4.2 验证 Mock 服务

```bash
curl -s -X POST http://127.0.0.1:9877/agent-api/workflow-agent-1-1b14f16b/chatabc/use_as_tool \
  -H 'Content-Type: application/json' \
  -d '{"session_id":"test","txt":"转账测试"}'
```

应返回 SSE 流，包含 `event:message`（3 个节点事件）和 `event:done` 事件。

### 4.3 Mock 支持的工作流端点

| URL 路径中包含 | 模拟业务 | 返回数据 |
|----------------|----------|----------|
| `workflow-agent-1-1b14f16b` | 转账 | 3 个节点事件 → `{"output": "mock-transfer-done"}` |
| `workflow-agent-payee` | 收款人列表 | `{"payees": ["陈广荣", "王阳明"]}` |
| `workflow-agent-bill` | 缴费 | `{"status": "accepted"}` |

Mock 服务控制台会打印每个请求的 `MOCK_WORKFLOW_REQUEST` 日志，方便调试。

### 4.4 Workflow 原始报文回退

当 workflow API 返回的报文和 runtime SSE 解析逻辑不一致时（如无 `node_output`、非 SSE 格式等），`workflow_api_call` 工具不会抛异常，而是将 API 原始报文放到返回结果的 `output` 字段中，协议框架（ok/status/completion_state 等）正常封装。

---

## 5. 单元测试

### 5.1 运行全量测试

```bash
# 简洁输出
make test
# 或
.venv/bin/python -m pytest -q

# 详细输出
make test-verbose
# 或
.venv/bin/python -m pytest -v
```

### 5.2 运行单个测试文件

```bash
.venv/bin/python -m pytest tests/unit/test_harness_v2_api.py -v          # API 路由
.venv/bin/python -m pytest tests/unit/test_harness_v2_config.py -v        # 配置加载
.venv/bin/python -m pytest tests/unit/test_harness_v2_errors.py -v        # 错误层次
.venv/bin/python -m pytest tests/unit/test_harness_v2_middleware.py -v     # 中间件
.venv/bin/python -m pytest tests/unit/test_harness_v2_protocol.py -v      # 协议模型
.venv/bin/python -m pytest tests/unit/test_harness_v2_session.py -v       # Session 管理
.venv/bin/python -m pytest tests/unit/test_harness_v2_skill_registry.py -v # Skill 注册
.venv/bin/python -m pytest tests/unit/test_harness_v2_workflow.py -v       # 工作流工具
```

### 5.3 代码检查

```bash
make lint                  # Ruff 代码检查
make format-check          # 格式检查（不修改文件）
make format                # 自动格式化
```

### 5.4 预期结果

```
94 passed in <1s
```

---

## 6. API 手动测试

以下操作假设 Harness 服务（端口 8765）和 Mock 工作流服务均已启动。

### 6.1 基本消息请求（非流式）

```bash
curl -s http://127.0.0.1:8765/api/v1/message \
  -H 'Content-Type: application/json' \
  -d '{
    "sessionId": "test_001",
    "custID": "C0001",
    "txt": "你好",
    "stream": false,
    "executionMode": "router_only"
  }' | python -m json.tool
```

**预期响应：**

```json
{
  "ok": true,
  "status": "running",
  "completion_state": 0,
  "completion_reason": "natural_response",
  "message": "你好！有什么可以帮助您的？..."
}
```

### 6.2 流式请求（SSE）

```bash
curl -s --no-buffer http://127.0.0.1:8765/api/v1/message \
  -H 'Content-Type: application/json' \
  -H 'Accept: text/event-stream' \
  -d '{
    "sessionId": "test_002",
    "custID": "C0001",
    "txt": "给张三转账500元",
    "stream": true,
    "debugTrace": true,
    "executionMode": "execute",
    "config_variables": [
      {"name": "custID", "value": "C0001"},
      {"name": "sessionID", "value": "test_002"},
      {"name": "currentDisplay", "value": ""},
      {"name": "agentSessionID", "value": "test_002"}
    ]
  }'
```

**预期 SSE 流格式：**

```
event:trace
data:{"stage":"request_received","title":"请求进入Router",...}

event:trace
data:{"stage":"deepagent_runtime_start","title":"DeepAgent运行开始",...}

event:trace
data:{"stage":"task_constraints_injected","title":"任务约束注入",...}

event:trace
data:{"stage":"skill_metadata_injected","title":"Skill元数据注入",...}

event:trace
data:{"stage":"intent_recognized","title":"意图识别",...}

event:trace
data:{"stage":"skill_loaded","title":"Skill加载",...}

event:trace
data:{"stage":"skill_reference_loaded","title":"Skill Reference加载",...}

event:trace
data:{"stage":"slots_extracted","title":"槽位提取",...}

event:trace
data:{"stage":"workflow_call","title":"工作流调用",...}

event:message
data:{"ok":true,"status":"waiting_assistant_completion","completion_state":1,...}

event:trace
data:{"stage":"assistant_protocol_frames","title":"SSE业务帧生成",...}

event:done
data:[DONE]
```

**Trace 事件说明（关键环节）：**

| trace stage | 含义 | 所在 middleware |
|-------------|------|----------------|
| `request_received` | 请求进入 Router | API 层 |
| `deepagent_runtime_start` | DeepAgent 开始执行 | API 层 |
| `task_constraints_injected` | 串行任务约束注入 | TaskProgressMiddleware |
| `frontend_context_injected` | 推荐任务/页面卡片注入 | FrontendContextMiddleware |
| `skill_metadata_injected` | Skill 元数据摘要注入 | SkillLifecycleMiddleware |
| `intent_recognized` | 意图识别完成 | SkillLifecycleMiddleware |
| `skill_loaded` | Skill body 加载 | SkillLifecycleMiddleware |
| `skill_reference_loaded` | Skill reference 文件加载 | SkillLifecycleMiddleware |
| `slots_extracted` | 槽位提取完成 | SkillLifecycleMiddleware |
| `workflow_url_allowed` | 工作流 URL 白名单通过 | WorkflowGatewayMiddleware |
| `workflow_call` | 工作流 API 调用 | WorkflowGatewayMiddleware |
| `workflow_result` | 工作流返回结果 | WorkflowGatewayMiddleware |
| `completion_gate_active` | CompletionGate 拦截 | CompletionGateMiddleware |
| `protocol_validated` | 协议校验通过 | ProtocolOutputMiddleware |
| `skill_unloaded` | Skill 从上下文卸载 | SkillLifecycleMiddleware |
| `assistant_protocol_frames` | SSE 业务帧生成 | API 层 |

### 6.3 任务完成回调

```bash
curl -s http://127.0.0.1:8765/api/v1/task/completion \
  -H 'Content-Type: application/json' \
  -d '{
    "sessionId": "test_002",
    "custID": "C0001",
    "taskId": "task_001",
    "completionSignal": 1,
    "stream": false
  }' | python -m json.tool
```

**预期响应：**

```json
{
  "ok": true,
  "status": "completed",
  "completion_state": 2,
  "completion_reason": "task_completion_received",
  "output": {
    "taskId": "task_001",
    "completionSignal": 1
  }
}
```

**completionSignal 含义：**

| 值 | 含义 | 返回 status |
|----|------|------------|
| 1 | 成功完成 | `completed` |
| 2 | 失败 | `failed` |

### 6.4 带前端上下文的请求

```bash
curl -s --no-buffer http://127.0.0.1:8765/api/v1/message \
  -H 'Content-Type: application/json' \
  -d '{
    "sessionId": "test_ctx",
    "custID": "C0001",
    "txt": "帮我处理一下",
    "stream": true,
    "debugTrace": true,
    "executionMode": "execute",
    "recommendTask": [
      {"intent_code": "AG_TRANS", "description": "给陈广荣转500元"},
      {"intent_code": "AG_PAY_BILL", "description": "缴水电费200元"}
    ],
    "currentDisplay": [
      {"type": "transfer_card", "payee_name": "陈广荣", "amount": 500}
    ]
  }'
```

**观察要点：**
- [ ] trace 中出现 `frontend_context_injected`（含 `recommendTask=2项, currentDisplay=1项`）
- [ ] LLM 利用推荐任务信息辅助意图识别和任务规划
- [ ] 若 `recommendTask` 和 `currentDisplay` 为空数组或不传，则不会注入该 trace

### 6.5 并发 Session 拒绝测试

同一 session 同时发两个请求，第二个应被拒绝：

```bash
# 终端1：发送长请求（流式，会持续一段时间）
curl -s --no-buffer http://127.0.0.1:8765/api/v1/message \
  -H 'Content-Type: application/json' \
  -d '{"sessionId":"lock_test","custID":"C0001","txt":"给小明转账100元","stream":true,"executionMode":"execute"}' &

# 等待0.5秒后发送同 session 请求
sleep 0.5 && curl -s http://127.0.0.1:8765/api/v1/message \
  -H 'Content-Type: application/json' \
  -d '{"sessionId":"lock_test","custID":"C0001","txt":"你好","stream":false}'
```

**预期：** 第二个请求返回：

```json
{
  "ok": false,
  "status": "failed",
  "completion_state": 0,
  "completion_reason": "session_run_in_progress",
  "errorCode": "session_run_in_progress",
  "message": "..."
}
```

---

## 7. Debug UI 前端操作指南

Harness 服务启动后，浏览器打开 `http://127.0.0.1:8765/` 即可进入 **Intent Router 验证台**。

### 7.1 界面布局

验证台界面包含以下区域：

- **顶部**：标题栏 "Intent Router 验证台"
- **左侧控制面板**：
  - `sessionId` 输入框（默认随机生成）
  - `custID` 输入框（默认 `C0001`）
  - `executionMode` 选择（`execute` / `router_only`）
  - `stream` 开关
  - `debugTrace` 开关
  - `recommendTask JSON` 输入区（复选框启用 + textarea）
  - `currentDisplay JSON` 输入区（复选框启用 + textarea）
  - 消息输入框 + 发送按钮
- **右侧结果面板**：
  - Trace 事件列表（当 debugTrace 开启时）
  - 响应 JSON 显示

### 7.2 基本测试步骤（问候）

1. 打开浏览器，访问 `http://127.0.0.1:8765/`
2. 确认 `sessionId` 和 `custID` 已填写
3. 选择 `executionMode` = `router_only`
4. 勾选 `stream` 和 `debugTrace`
5. 在消息输入框输入：`你好`
6. 点击 **发送** 按钮

**预期结果：**
- 右侧 Trace 面板显示 `request_received` → `deepagent_runtime_start` → `assistant_protocol_frames`
- 响应 JSON 中 `completion_state: 0`，`status: "running"`
- `message` 中包含自然语言问候回复

### 7.3 转账意图测试（execute 模式）

1. **新建 session**（刷新页面或清空 sessionId）
2. 选择 `executionMode` = `execute`
3. 勾选 `stream` 和 `debugTrace`
4. 输入：`给陈广荣转500元`
5. 点击 **发送**

**预期结果：**
- Trace 面板依次显示：
  - `request_received` → `deepagent_runtime_start`
  - `task_constraints_injected` — 任务约束已注入
  - `skill_metadata_injected` — Skill 元数据已注入
  - `intent_recognized` — 识别到 `AG_TRANS`
  - `skill_loaded` — 加载了 `transfer-routing` skill body
  - `skill_reference_loaded` — 加载了 `slot_filling.md` 和 `workflow_request.md`
  - `slots_extracted` — 提取到 `payee_name=陈广荣, amount=500`
  - `workflow_call` — 调用了转账工作流 API
  - `workflow_result` — Mock 服务返回结果
  - `completion_gate_active` — CompletionGate 拦截，等待前端确认
  - `assistant_protocol_frames`
- 响应 JSON 中：
  - `completion_state: 1` — 等待前端调用 completion 确认
  - `status: "waiting_assistant_completion"`
  - `intent_code: "AG_TRANS"`
- Mock 工作流服务控制台打印 `MOCK_WORKFLOW_REQUEST`

6. **模拟完成任务**：用 curl 或另一个窗口调用 completion 接口

```bash
curl -s http://127.0.0.1:8765/api/v1/task/completion \
  -H 'Content-Type: application/json' \
  -d '{"sessionId":"<同上的sessionId>","custID":"C0001","taskId":"task_001","completionSignal":1,"stream":false}'
```

### 7.4 多轮补槽测试

1. **新建 session**
2. `executionMode` = `router_only`
3. 输入：`我要转账500元`（缺少收款人）
4. **发送**

**预期结果：**
- `intent_code: "AG_TRANS"`
- `slot_memory` 中有 `amount: 500`，缺少 `payee_name`
- `status: "waiting_user_input"` — LLM 追问收款人
- `message` 中包含"请提供收款人"类似提示

5. 在**同一 session** 中输入：`转给张三`
6. **发送**

**预期结果：**
- `slot_memory` 同时包含 `payee_name: "张三"` 和 `amount: 500`
- `status: "ready_for_dispatch"`（槽位齐全）

### 7.5 带推荐任务测试

1. **新建 session**
2. 勾选 `recommendTask JSON` 复选框，填入：
```json
[
  {"intent_code": "AG_TRANS", "description": "给陈广荣转500元"},
  {"intent_code": "AG_PAY_BILL", "description": "缴水电费200元"}
]
```
3. 勾选 `currentDisplay JSON` 复选框，填入：
```json
[{"type": "transfer_card", "payee_name": "陈广荣", "amount": 500}]
```
4. 输入：`帮我处理一下推荐的任务`
5. **发送**

**预期结果：**
- Trace 中出现 `frontend_context_injected`
- LLM 利用推荐任务信息规划多个任务

---

## 8. 完整业务场景验证

以下场景覆盖核心业务流程和 harness 的两个关键流程（任务规划执行 + Skill 加载卸载）。

### 场景 1：单任务转账（槽位齐全一步完成）

**步骤：**

1. 发送请求：
```bash
curl -s --no-buffer http://127.0.0.1:8765/api/v1/message \
  -H 'Content-Type: application/json' \
  -d '{
    "sessionId": "s1",
    "custID": "C0001",
    "txt": "给陈广荣转500元",
    "stream": true,
    "debugTrace": true,
    "executionMode": "execute",
    "config_variables": [
      {"name": "custID", "value": "C0001"},
      {"name": "sessionID", "value": "s1"},
      {"name": "currentDisplay", "value": ""},
      {"name": "agentSessionID", "value": "s1"}
    ]
  }'
```

**观察要点：**
- [ ] 响应包含 `intent_code: "AG_TRANS"`
- [ ] `slot_memory` 包含 `payee_name: "陈广荣"` 和 `amount: 500`
- [ ] trace 中出现完整链路：`skill_metadata_injected` → `intent_recognized` → `skill_loaded` → `skill_reference_loaded` → `slots_extracted` → `workflow_call` → `workflow_result` → `completion_gate_active`
- [ ] Mock 工作流服务控制台打印 `MOCK_WORKFLOW_REQUEST`
- [ ] `completion_state: 1`（等待前端 completion）
- [ ] `status: "waiting_assistant_completion"`

2. 调用任务完成回调：
```bash
curl -s http://127.0.0.1:8765/api/v1/task/completion \
  -H 'Content-Type: application/json' \
  -d '{"sessionId":"s1","custID":"C0001","taskId":"task_001","completionSignal":1,"stream":false}'
```

**观察要点：**
- [ ] 返回 `status: "completed"`，`completion_state: 2`
- [ ] `completion_reason: "task_completion_received"`

---

### 场景 2：转账补槽（多轮对话）

**步骤：**

1. 第一轮 — 缺少收款人：
```bash
curl -s --no-buffer http://127.0.0.1:8765/api/v1/message \
  -H 'Content-Type: application/json' \
  -d '{
    "sessionId": "s2",
    "custID": "C0001",
    "txt": "我要转账500元",
    "stream": true,
    "debugTrace": true,
    "executionMode": "router_only"
  }'
```

**观察要点：**
- [ ] `intent_code: "AG_TRANS"`
- [ ] `slot_memory` 中 `amount: 500`，`payee_name` 缺失
- [ ] `status: "waiting_user_input"`
- [ ] `message` 中追问收款人
- [ ] trace 中出现 `skill_loaded` 和 `skill_reference_loaded`（加载了提槽规则）
- [ ] 但未出现 `workflow_call`（槽位不全，不调用工作流）

2. 第二轮 — 补充收款人：
```bash
curl -s --no-buffer http://127.0.0.1:8765/api/v1/message \
  -H 'Content-Type: application/json' \
  -d '{
    "sessionId": "s2",
    "custID": "C0001",
    "txt": "转给张三",
    "stream": true,
    "debugTrace": true,
    "executionMode": "router_only"
  }'
```

**观察要点：**
- [ ] `slot_memory` 同时包含 `payee_name: "张三"` 和 `amount: 500`
- [ ] `status: "ready_for_dispatch"`（槽位齐全）

---

### 场景 3：缴费意图（AG_PAY_BILL）

**步骤：**

1. 发送缴费请求：
```bash
curl -s --no-buffer http://127.0.0.1:8765/api/v1/message \
  -H 'Content-Type: application/json' \
  -d '{
    "sessionId": "s3",
    "custID": "C0001",
    "txt": "帮我缴水电费200元",
    "stream": true,
    "debugTrace": true,
    "executionMode": "router_only"
  }'
```

**观察要点：**
- [ ] `intent_code: "AG_PAY_BILL"`
- [ ] `slot_memory` 包含 `payment_item: "水电费"` 和 `amount: 200`
- [ ] 不应出现 `AG_TRANS` 意图
- [ ] `status: "ready_for_dispatch"`（两个必填槽位齐全）

---

### 场景 4：多意图多任务推进（核心全链路）

> **这是 harness 最核心的测试场景，验证两个关键流程的完整衔接：**
> - **任务流程**：意图识别 → 任务规划 → 串行执行 → workflow 调用 → completion → 下一个任务
> - **Skill 流程**：metadata → 意图命中 → skill body + reference 加载 → 提槽 → workflow 调用 → 卸载

**步骤：**

1. 一次发送包含两个意图的消息：
```bash
curl -s --no-buffer http://127.0.0.1:8765/api/v1/message \
  -H 'Content-Type: application/json' \
  -d '{
    "sessionId": "s4",
    "custID": "C0001",
    "txt": "给陈广荣转300元，另外帮我充100元话费",
    "stream": true,
    "debugTrace": true,
    "executionMode": "execute",
    "config_variables": [
      {"name": "custID", "value": "C0001"},
      {"name": "sessionID", "value": "s4"},
      {"name": "currentDisplay", "value": ""},
      {"name": "agentSessionID", "value": "s4"}
    ]
  }'
```

**观察要点（第一个任务：转账）：**
- [ ] `task_list` 应包含 2 个任务
- [ ] 第一个任务 `intent_code: "AG_TRANS"`，slot_memory 包含 `payee_name: "陈广荣"` 和 `amount: 300`
- [ ] 第二个任务 `intent_code: "AG_PAY_BILL"`，slot_memory 包含 `payment_item: "话费"` 和 `amount: 100`
- [ ] `current_task` 指向第一个任务（串行处理）
- [ ] trace 中可见：`skill_loaded`（加载了 transfer-routing）→ `workflow_call`（调用转账 API）
- [ ] `completion_state: 1`（等待前端 completion）

2. 完成第一个任务：
```bash
curl -s http://127.0.0.1:8765/api/v1/task/completion \
  -H 'Content-Type: application/json' \
  -d '{"sessionId":"s4","custID":"C0001","taskId":"task_001","completionSignal":1,"stream":false}'
```

**观察要点：**
- [ ] 返回 `status: "completed"`，`completion_state: 2`

3. 发送"继续"推进到下一个任务：
```bash
curl -s --no-buffer http://127.0.0.1:8765/api/v1/message \
  -H 'Content-Type: application/json' \
  -d '{
    "sessionId": "s4",
    "custID": "C0001",
    "txt": "继续",
    "stream": true,
    "debugTrace": true,
    "executionMode": "execute",
    "config_variables": [
      {"name": "custID", "value": "C0001"},
      {"name": "sessionID", "value": "s4"},
      {"name": "currentDisplay", "value": ""},
      {"name": "agentSessionID", "value": "s4"}
    ]
  }'
```

**观察要点（第二个任务：缴费）：**
- [ ] `current_task` 切换为缴费任务（`AG_PAY_BILL`）
- [ ] 第一个任务标记为 completed
- [ ] trace 中可见：`skill_unloaded`（卸载 transfer-routing）→ `skill_loaded`（加载 bill-payment-routing）
- [ ] 如果缴费 skill 有 workflow_request reference，则触发 `workflow_call`
- [ ] `completion_state: 1`（等待前端 completion）

4. 完成第二个任务：
```bash
curl -s http://127.0.0.1:8765/api/v1/task/completion \
  -H 'Content-Type: application/json' \
  -d '{"sessionId":"s4","custID":"C0001","taskId":"task_002","completionSignal":1,"stream":false}'
```

**观察要点：**
- [ ] 返回 `status: "completed"`，`completion_state: 2`
- [ ] 所有任务已完成

---

### 场景 5：不支持的转账类型

1. 外币转账：
```bash
curl -s http://127.0.0.1:8765/api/v1/message \
  -H 'Content-Type: application/json' \
  -d '{"sessionId":"s5","custID":"C0001","txt":"帮我转500美元给张三","stream":false,"executionMode":"router_only"}'
```

**观察要点：**
- [ ] 不应创建 AG_TRANS 任务
- [ ] `message` 应包含"仅支持人民币"相关提示

2. 非农行卡转出：
```bash
curl -s http://127.0.0.1:8765/api/v1/message \
  -H 'Content-Type: application/json' \
  -d '{"sessionId":"s5b","custID":"C0001","txt":"从我的招行卡转3000给李四","stream":false,"executionMode":"router_only"}'
```

**观察要点：**
- [ ] `message` 应包含"非农行卡转出"相关提示

---

### 场景 6：Session 隔离验证

1. Session A 发送转账请求：
```bash
curl -s http://127.0.0.1:8765/api/v1/message \
  -H 'Content-Type: application/json' \
  -d '{"sessionId":"iso_a","custID":"user_A","txt":"给张三转100元","stream":false,"executionMode":"router_only"}'
```

2. Session B 发送缴费请求：
```bash
curl -s http://127.0.0.1:8765/api/v1/message \
  -H 'Content-Type: application/json' \
  -d '{"sessionId":"iso_b","custID":"user_B","txt":"充话费50元","stream":false,"executionMode":"router_only"}'
```

3. Session A 补充信息：
```bash
curl -s http://127.0.0.1:8765/api/v1/message \
  -H 'Content-Type: application/json' \
  -d '{"sessionId":"iso_a","custID":"user_A","txt":"收款人是李四","stream":false,"executionMode":"router_only"}'
```

**观察要点：**
- [ ] Session A 的 slot_memory 应更新 payee_name，保留之前的 amount
- [ ] Session B 的 slot_memory 不受 Session A 影响
- [ ] 两个 session 的 intent_code 分别独立

---

### 场景 7：非业务意图过滤

```bash
# 闲聊
curl -s http://127.0.0.1:8765/api/v1/message \
  -H 'Content-Type: application/json' \
  -d '{"sessionId":"s7a","custID":"C0001","txt":"你好，今天天气怎么样","stream":false,"executionMode":"router_only"}'

# 查询类
curl -s http://127.0.0.1:8765/api/v1/message \
  -H 'Content-Type: application/json' \
  -d '{"sessionId":"s7b","custID":"C0001","txt":"查一下我的余额","stream":false,"executionMode":"router_only"}'
```

**观察要点：**
- [ ] 不应生成任何 AG_TRANS 或 AG_PAY_BILL 任务
- [ ] 应以自然语言回复
- [ ] `completion_state: 0`，`status: "running"`

---

### 场景 8：任务失败回调

```bash
# 先发送一个转账意图使 session 有任务
curl -s http://127.0.0.1:8765/api/v1/message \
  -H 'Content-Type: application/json' \
  -d '{"sessionId":"s8","custID":"C0001","txt":"给张三转100元","stream":false,"executionMode":"execute"}'

# 用 completionSignal=2 标记任务失败
curl -s http://127.0.0.1:8765/api/v1/task/completion \
  -H 'Content-Type: application/json' \
  -d '{"sessionId":"s8","custID":"C0001","taskId":"task_001","completionSignal":2,"stream":false}'
```

**预期响应：**

```json
{
  "ok": false,
  "status": "failed",
  "completion_state": 2,
  "completion_reason": "task_completion_received",
  "output": {"taskId": "task_001", "completionSignal": 2}
}
```

---

## 9. E2E 自动化验证

同时启动 Harness + Mock 工作流后，运行 E2E 脚本：

```bash
# 终端 1：启动 Mock 工作流（端口与 TOML 匹配）
.venv/bin/python examples/mock_workflow_server.py --host 127.0.0.1 --port 9877

# 终端 2：启动 Harness（使用 local TOML，需要真实 LLM 配置）
.venv/bin/python -m intent_router_harness serve examples/deepagent-finance-router-harness-local.toml --port 8765

# 终端 3：运行 E2E 检查
make e2e
# 或
.venv/bin/python examples/deepagent_e2e_check.py --url http://127.0.0.1:8765/api/v1/message
```

> **注意：** E2E 脚本需要真实 LLM 配置（`.env` 中的 API key），因为它依赖 deepagent SDK 完成完整的意图识别、提槽和工作流调用流程。

### 9.1 完整 E2E 验证清单

| # | 验证项 | 预期 | 方式 |
|---|--------|------|------|
| 1 | 健康检查 | `/healthz` → `{"status":"ok"}` | curl |
| 2 | 就绪检查 | `/readyz` → `{"ready":true}` | curl |
| 3 | Debug UI | `/` → HTML 页面 | 浏览器 |
| 4 | 问候（非意图） | `completion_state=0`, 自然语言回复 | curl/UI |
| 5 | 转账意图识别 | `AG_TRANS`, skill 加载 trace | curl/UI |
| 6 | 提槽 + workflow 调用 | slot_memory 齐全 → workflow_call trace | curl/UI |
| 7 | Completion 成功 | signal=1 → `completion_state=2` | curl |
| 8 | Completion 失败 | signal=2 → `status=failed` | curl |
| 9 | 多意图多任务 | task_list=2 → 串行推进 | curl/UI |
| 10 | Skill 卸载 + 加载 | 切换任务时 skill_unloaded → skill_loaded | curl/UI |
| 11 | 并发拒绝 | 同 session 第二请求被拒 | curl |
| 12 | Session 隔离 | 不同 session 数据互不影响 | curl |
| 13 | 单元测试 | 94 passed | `make test` |
| 14 | Lint | clean | `make lint` |

---

## 10. Kubernetes 部署

### 10.1 准备代码包

```bash
cd intent_router_harness
tar -czf app.tar.gz -C . src/ examples/ skills/ agent.md pyproject.toml
```

### 10.2 创建 ConfigMap

```bash
kubectl create namespace intent --dry-run=client -o yaml | kubectl apply -f -
kubectl -n intent create configmap intent-router-harness-code \
  --from-file=app.tar.gz=app.tar.gz \
  --dry-run=client -o yaml | kubectl apply -f -
```

### 10.3 创建 Secret（LLM 凭据）

```bash
kubectl -n intent create secret generic intent-router-harness-env \
  --from-literal=OPENAI_API_BASE=https://api.siliconflow.cn/v1 \
  --from-literal=OPENAI_API_KEY=sk-your-real-key \
  --dry-run=client -o yaml | kubectl apply -f -
```

### 10.4 部署

```bash
kubectl apply -f k8s/intent-router-harness.yaml
```

### 10.5 验证部署

```bash
# 检查 Pod 状态
kubectl -n intent get pods -l app=intent-router-harness

# 查看日志
kubectl -n intent logs -l app=intent-router-harness -f

# 端口转发测试
kubectl -n intent port-forward svc/intent-router-harness 8765:8765

# 健康检查
curl -s http://127.0.0.1:8765/healthz
```

### 10.6 通过 Ingress 访问

部署后通过域名访问：

```
http://intent-router.kkrrc-359.top/intent-router-harness/api/v1/message
```

---

## 11. 故障排查

### 常见问题

| 问题 | 可能原因 | 解决方案 |
|------|----------|----------|
| `ImportError: langchain_core` | 未安装 deepagent 依赖 | `pip install -e '.[deepagent,test]'` |
| `session_run_in_progress` | 同一 session 并发请求 | 等待前一请求完成，或换 sessionId |
| Mock 服务无响应 | Mock 服务未启动 | 确认 Mock 端口与 TOML 配置一致 |
| 工作流 URL 被拒绝 | URL 不在白名单 | 检查 TOML `[workflow].allowed_urls` 端口是否匹配 |
| Session 过期 | 超过 idle_timeout | 使用新 sessionId 或调大 `[session].idle_timeout_seconds` |
| `Missing credentials` / LLM 调用失败 | 环境变量名错误或 API key 无效 | 必须使用 `OPENAI_API_KEY` 和 `OPENAI_API_BASE`，不是 `ROUTER_LLM_*` |
| LLM 返回 "没有可用技能" | Skill 未被 deepagent 加载 | 检查 TOML 的 `skill_roots` 路径和 `[[bindings]]` 配置 |
| Skill reference 未加载 | reference 文件路径错误 | 检查 skill frontmatter 中 `references` 字段的 `path` |
| `completion_state` 一直是 0 | executionMode 为 router_only | 切换为 `execute` 模式触发 workflow 调用 |
| output 为空或原始报文 | workflow 返回格式不匹配 | 正常行为 — 不匹配时 output 放原始报文 |

### 日志级别

```bash
# 调试模式（查看详细 middleware 日志）
INTENT_ROUTER_HARNESS_LOG_LEVEL=DEBUG .venv/bin/python -m intent_router_harness serve ...
```

### 查看完整 SSE 流

```bash
# 使用 curl --no-buffer 实时查看 SSE（含 trace 事件）
curl -s --no-buffer http://127.0.0.1:8765/api/v1/message \
  -H 'Content-Type: application/json' \
  -d '{
    "sessionId": "debug",
    "custID": "C0001",
    "txt": "测试",
    "stream": true,
    "debugTrace": true,
    "executionMode": "router_only"
  }'
```

### 7 个 Middleware 执行链参考

```
请求 → TaskProgressMiddleware (串行任务约束)
     → FrontendContextMiddleware (推荐任务/页面卡片)
     → CompletionGateMiddleware (防止自完成, workflow 结果拦截)
     → SkillLifecycleMiddleware (渐进式 skill 加载/卸载)
     → SkillFileMiddleware (虚拟文件路径拦截)
     → WorkflowGatewayMiddleware (URL 白名单 + hooks)
     → ProtocolOutputMiddleware (协议输出校验)
     → deepagent SDK (LLM 调用)
     → 响应沿链返回
```
