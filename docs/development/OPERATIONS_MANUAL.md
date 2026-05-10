# 运行测试操作手册

本文档覆盖从环境配置到完整业务流程验证的所有步骤。

---

## 目录

1. [环境准备](#1-环境准备)
2. [配置说明](#2-配置说明)
3. [启动服务](#3-启动服务)
4. [启动 Mock 工作流服务](#4-启动-mock-工作流服务)
5. [单元测试](#5-单元测试)
6. [API 手动测试](#6-api-手动测试)
7. [完整业务场景验证](#7-完整业务场景验证)
8. [E2E 自动化验证](#8-e2e-自动化验证)
9. [Kubernetes 部署](#9-kubernetes-部署)
10. [故障排查](#10-故障排查)

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

### 1.3 安装依赖

```bash
# 基础安装（含测试依赖）
pip install -e '.[test]'

# 完整安装（含 deepagent SDK，生产环境必需）
pip install -e '.[deepagent,test]'
```

### 1.4 验证安装

```bash
python -c "from intent_router_harness import create_app, SessionManager, SkillRegistry; print('OK')"
```

---

## 2. 配置说明

### 2.1 TOML Spec 文件

核心配置文件：`examples/deepagent-finance-router-harness.toml`

```toml
name = "deepagent-finance-router-harness"
version = "0.1.0"
agent_runtime = "deepagent"
agent_paths = ["../agent.md"]          # Agent 系统提示词
skill_roots = ["../skills"]            # Skill 目录

[workflow]
allowed_urls = [                       # 白名单 URL（安全约束）
  "http://127.0.0.1:9876/agent-api/workflow-agent-1-1b14f16b/chatabc/use_as_tool",
  "http://127.0.0.1:9876/agent-api/workflow-agent-payee/chatabc/use_as_tool",
  "http://127.0.0.1:9876/agent-api/workflow-agent-bill/chatabc/use_as_tool",
]

[session]
idle_timeout_seconds = 1800            # Session 30 分钟超时

[deepagent]
max_iterations = 20                    # Agent 最大迭代次数
skills = ["/skills/"]                  # deepagent SDK skill 搜索路径
```

### 2.2 环境变量

复制模板并填入真实凭据（仅生产/联调需要，纯测试可跳过）：

```bash
cp .env.example .env
```

`.env` 文件内容：

```bash
# LLM 提供商配置（连接真实 LLM 时必填）
ROUTER_LLM_API_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
ROUTER_LLM_API_KEY=sk-your-real-key-here
ROUTER_LLM_MODEL=qwen-plus
ROUTER_LLM_TIMEOUT_SECONDS=120
```

> **注意：** 使用 Mock 工作流服务进行本地测试时，不需要真实 LLM 密钥。

### 2.3 Skill 目录结构

```
skills/
├── transfer-routing/              # 转账意图 AG_TRANS
│   ├── SKILL.md                   # 意图识别规则（name + description 用于匹配）
│   └── references/
│       ├── slot_filling.md        # 提槽逻辑
│       └── workflow_request.md    # API 调用方式
└── bill-payment-routing/          # 缴费意图 AG_PAY_BILL
    └── SKILL.md                   # 意图识别规则
```

**渐进式加载流程：**
1. 启动时扫描所有 SKILL.md 的 `name`/`description`（元数据）
2. 意图识别命中后，按需加载 SKILL.md body
3. 提槽阶段按需加载 references/
4. 任务完成后从上下文卸载

---

## 3. 启动服务

### 3.1 启动 Harness 服务

```bash
# 方式一：使用 Makefile
make serve

# 方式二：直接运行
python -m intent_router_harness serve examples/deepagent-finance-router-harness.toml --port 8765

# 方式三：带热重载（开发模式）
python -m intent_router_harness serve examples/deepagent-finance-router-harness.toml --port 8765 --reload
```

### 3.2 验证服务健康

```bash
# 存活检查
curl -s http://127.0.0.1:8765/healthz
# 返回: {"status":"ok"}

# 就绪检查
curl -s http://127.0.0.1:8765/readyz
# 返回: {"ready":true,"service":"intent_router_harness","version":"2.0.0"}

# 首页
curl -s http://127.0.0.1:8765/
# 返回: <h1>intent_router_harness v2</h1><p>POST /api/v1/message</p>
```

---

## 4. 启动 Mock 工作流服务

Mock 服务模拟下游工作流 API（转账、收款人列表、缴费），用于本地测试。

### 4.1 启动

```bash
# 方式一：使用 Makefile
make mock-workflow

# 方式二：直接运行
python examples/mock_workflow_server.py --host 127.0.0.1 --port 9876
```

输出：`mock workflow serving on http://127.0.0.1:9876`

### 4.2 验证 Mock 服务

```bash
curl -s -X POST http://127.0.0.1:9876/agent-api/workflow-agent-1-1b14f16b/chatabc/use_as_tool \
  -H 'Content-Type: application/json' \
  -d '{"session_id":"test","txt":"转账测试"}'
```

应返回 SSE 流，包含 `event:message` 和 `event:done` 事件。

### 4.3 Mock 支持的工作流端点

| URL 路径中包含 | 模拟业务 | 返回数据 |
|----------------|----------|----------|
| `workflow-agent-1-1b14f16b` | 转账 | 3 个节点事件 → `mock-transfer-done` |
| `workflow-agent-payee` | 收款人列表 | 返回 `["陈广荣", "王阳明"]` |
| `workflow-agent-bill` | 缴费 | 返回 `{"status": "accepted"}` |

---

## 5. 单元测试

### 5.1 运行全量测试

```bash
# 简洁输出
make test
# 或
python -m pytest -q

# 详细输出
make test-verbose
# 或
python -m pytest -v
```

### 5.2 运行单个测试文件

```bash
python -m pytest tests/unit/test_harness_v2_api.py -v          # API 路由
python -m pytest tests/unit/test_harness_v2_config.py -v        # 配置加载
python -m pytest tests/unit/test_harness_v2_errors.py -v        # 错误层次
python -m pytest tests/unit/test_harness_v2_middleware.py -v     # 中间件
python -m pytest tests/unit/test_harness_v2_protocol.py -v      # 协议模型
python -m pytest tests/unit/test_harness_v2_session.py -v        # Session 管理
python -m pytest tests/unit/test_harness_v2_skill_registry.py -v # Skill 注册
python -m pytest tests/unit/test_harness_v2_workflow.py -v       # 工作流工具
```

### 5.3 代码检查

```bash
make lint                  # Ruff 代码检查
make format-check          # 格式检查（不修改文件）
make format                # 自动格式化
```

### 5.4 预期结果

```
88 passed in <1s
```

---

## 6. API 手动测试

以下操作假设 Harness 服务（端口 8765）和 Mock 工作流服务（端口 9876）均已启动。

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
  "message": "..."
}
```

### 6.2 流式请求（SSE）

```bash
curl -s http://127.0.0.1:8765/api/v1/message \
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

event:message
data:{"ok":true,"status":"...","intent_code":"AG_TRANS",...}

event:trace
data:{"stage":"assistant_protocol_frames","title":"SSE业务帧生成",...}

event:done
data:[DONE]
```

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

### 6.4 并发 Session 拒绝测试

同一 session 同时发两个请求，第二个应被拒绝：

```bash
# 终端1：发送长请求
curl -s http://127.0.0.1:8765/api/v1/message \
  -H 'Content-Type: application/json' \
  -d '{"sessionId":"lock_test","custID":"C0001","txt":"给小明转账100元","stream":true,"executionMode":"execute"}' &

# 终端2：立即发送同 session 请求
sleep 0.5 && curl -s http://127.0.0.1:8765/api/v1/message \
  -H 'Content-Type: application/json' \
  -d '{"sessionId":"lock_test","custID":"C0001","txt":"你好","stream":false}'
```

**预期：** 第二个请求返回 `errorCode: "session_run_in_progress"`。

---

## 7. 完整业务场景验证

以下场景覆盖核心业务流程。每个场景描述了：前端输入 → 预期响应 → 观察要点。

### 场景 1：单任务转账（槽位齐全一步完成）

**步骤：**

1. 发送请求：
```bash
curl -s http://127.0.0.1:8765/api/v1/message \
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
- [ ] 如果 executionMode=execute 且槽位齐全，应触发 `workflow_api_call` 工具调用
- [ ] Mock 工作流服务控制台应打印 `MOCK_WORKFLOW_REQUEST`
- [ ] trace 中包含 `deepagent_runtime_start` 和 `assistant_protocol_frames` 阶段
- [ ] 最终 frame 的 `status` 应为 `completed` 或 `ready_for_dispatch`

2. 调用任务完成回调：
```bash
curl -s http://127.0.0.1:8765/api/v1/task/completion \
  -H 'Content-Type: application/json' \
  -d '{"sessionId":"s1","custID":"C0001","taskId":"task_001","completionSignal":1,"stream":false}'
```

**观察要点：**
- [ ] 返回 `status: "completed"`
- [ ] `completion_reason: "task_completion_received"`

---

### 场景 2：转账补槽（多轮对话）

**步骤：**

1. 第一轮 — 缺少收款人：
```bash
curl -s http://127.0.0.1:8765/api/v1/message \
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
- [ ] `status` 应为 `waiting_user_input`
- [ ] `message` 中应追问收款人

2. 第二轮 — 补充收款人：
```bash
curl -s http://127.0.0.1:8765/api/v1/message \
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
- [ ] `status` 应变为 `ready_for_dispatch`（槽位齐全）

---

### 场景 3：缴费意图（AG_PAY_BILL）

**步骤：**

1. 发送缴费请求：
```bash
curl -s http://127.0.0.1:8765/api/v1/message \
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
- [ ] `status` 应为 `ready_for_dispatch`（两个必填槽位齐全）

---

### 场景 4：多意图多任务（转账 + 缴费）

**步骤：**

1. 一次发送包含两个意图的消息：
```bash
curl -s http://127.0.0.1:8765/api/v1/message \
  -H 'Content-Type: application/json' \
  -d '{
    "sessionId": "s4",
    "custID": "C0001",
    "txt": "给陈广荣转300元，另外帮我充100元话费",
    "stream": true,
    "debugTrace": true,
    "executionMode": "router_only"
  }'
```

**观察要点：**
- [ ] `task_list` 应包含 2 个任务
- [ ] 第一个任务 `intent_code: "AG_TRANS"`，slot_memory 包含 `payee_name: "陈广荣"` 和 `amount: 300`
- [ ] 第二个任务 `intent_code: "AG_PAY_BILL"`，slot_memory 包含 `payment_item: "话费"` 和 `amount: 100`
- [ ] `current_task` 应指向第一个任务（串行处理）

2. 完成第一个任务后推进：
```bash
curl -s http://127.0.0.1:8765/api/v1/task/completion \
  -H 'Content-Type: application/json' \
  -d '{"sessionId":"s4","custID":"C0001","taskId":"task_001","completionSignal":1,"stream":false}'
```

3. 发送新消息，验证 current_task 已推进到第二个任务：
```bash
curl -s http://127.0.0.1:8765/api/v1/message \
  -H 'Content-Type: application/json' \
  -d '{
    "sessionId": "s4",
    "custID": "C0001",
    "txt": "继续",
    "stream": true,
    "debugTrace": true,
    "executionMode": "router_only"
  }'
```

**观察要点：**
- [ ] `current_task` 应切换为缴费任务（`AG_PAY_BILL`）
- [ ] 第一个任务应标记为 completed
- [ ] Skill 上下文应卸载转账 skill，加载缴费 skill

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

### 场景 7：非转账意图过滤

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

---

### 场景 8：Session 用户绑定冲突

```bash
# 用户 A 创建 session
curl -s http://127.0.0.1:8765/api/v1/message \
  -H 'Content-Type: application/json' \
  -d '{"sessionId":"bind_test","custID":"user_A","txt":"你好","stream":false,"executionMode":"router_only"}'

# 用户 B 尝试使用同一 session
curl -s http://127.0.0.1:8765/api/v1/message \
  -H 'Content-Type: application/json' \
  -d '{"sessionId":"bind_test","custID":"user_B","txt":"你好","stream":false,"executionMode":"router_only"}'
```

**观察要点：**
- [ ] 第一个请求正常返回
- [ ] 第二个请求返回（取决于实现）：正常执行或 session 绑定冲突

---

## 8. E2E 自动化验证

同时启动 Harness + Mock 工作流后，运行 E2E 脚本：

```bash
# 终端 1：启动 Mock 工作流
make mock-workflow

# 终端 2：启动 Harness（需要真实 LLM 配置）
make serve

# 终端 3：运行 E2E 检查
make e2e
# 或
python examples/deepagent_e2e_check.py --url http://127.0.0.1:8766/api/v1/message
```

**E2E 脚本验证点：**
1. 向 `/api/v1/message` 发送"给陈广荣转500元"
2. 检查 trace 中包含 `deepagent_langchain_tool_call_completed`（工具调用完成）
3. 检查消息中包含 `workflow_node_output`（工作流节点输出）
4. 检查最终 `status=completed`，`completion_reason=workflow_done`
5. 检查最终 `output` 包含 `mock-transfer-done`

**预期输出：**
```json
{
  "ok": true,
  "trace_count": 5,
  "message_count": 3,
  "final": { "status": "completed", "completion_reason": "workflow_done", ... }
}
```

> **注意：** E2E 脚本需要真实 LLM 配置（`.env` 中的 API key），因为它依赖 deepagent SDK 完成完整的意图识别和提槽流程。

---

## 9. Kubernetes 部署

### 9.1 准备代码包

```bash
cd intent_router_harness
tar -czf app.tar.gz -C . src/ examples/ skills/ agent.md pyproject.toml
```

### 9.2 创建 ConfigMap

```bash
kubectl create namespace intent --dry-run=client -o yaml | kubectl apply -f -
kubectl -n intent create configmap intent-router-harness-code \
  --from-file=app.tar.gz=app.tar.gz \
  --dry-run=client -o yaml | kubectl apply -f -
```

### 9.3 创建 Secret（LLM 凭据）

```bash
kubectl -n intent create secret generic intent-router-harness-env \
  --from-literal=ROUTER_LLM_API_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1 \
  --from-literal=ROUTER_LLM_API_KEY=sk-your-real-key \
  --from-literal=ROUTER_LLM_MODEL=qwen-plus \
  --dry-run=client -o yaml | kubectl apply -f -
```

### 9.4 部署

```bash
kubectl apply -f k8s/intent-router-harness.yaml
```

### 9.5 验证部署

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

### 9.6 通过 Ingress 访问

部署后通过域名访问：

```
http://intent-router.kkrrc-359.top/intent-router-harness/api/v1/message
```

---

## 10. 故障排查

### 常见问题

| 问题 | 可能原因 | 解决方案 |
|------|----------|----------|
| `ImportError: langchain_core` | 未安装 deepagent 依赖 | `pip install -e '.[deepagent,test]'` |
| `session_run_in_progress` | 同一 session 并发请求 | 等待前一请求完成，或换 sessionId |
| Mock 服务无响应 | Mock 服务未启动 | `make mock-workflow` |
| 工作流 URL 被拒绝 | URL 不在白名单 | 检查 TOML `[workflow].allowed_urls` |
| Session 过期 | 超过 idle_timeout | 使用新 sessionId 或调大超时 |
| LLM 调用失败 | API key 无效或配额不足 | 检查 `.env` 中的凭据 |

### 日志级别

```bash
# 调试模式
INTENT_ROUTER_HARNESS_LOG_LEVEL=DEBUG python -m intent_router_harness serve ...
```

### 查看完整 SSE 流

```bash
# 使用 curl --no-buffer 实时查看 SSE
curl -s --no-buffer http://127.0.0.1:8765/api/v1/message \
  -H 'Content-Type: application/json' \
  -d '{"sessionId":"debug","custID":"C0001","txt":"测试","stream":true,"debugTrace":true,"executionMode":"router_only"}'
```
