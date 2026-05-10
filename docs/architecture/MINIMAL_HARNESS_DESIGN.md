# 极简 Harness 架构设计方案

> 目标：把 deepagent SDK 当成黑盒 — 不改它一行代码，harness 只做 deepagent 不提供的企业层薄壳。

## 1. 设计原则

| 原则 | 说明 |
|------|------|
| **零侵入** | deepagent SDK 作为纯依赖使用，不 fork、不 monkey-patch |
| **极简壳** | harness 只做 deepagent 不提供的能力，不重复造轮子 |
| **前端透传** | 不做 auth，直接绑定前端传的 sessionId / custID 参数 |
| **Middleware 优先** | 除了多用户隔离，其余逻辑全部用 deepagent 原生 middleware 实现 |

## 2. 当前代码量分析

### 现有 harness 代码分布 (~8600 行 src/)

| 模块 | 行数 | 极简方案去向 |
|------|------|-------------|
| `deepagent/helpers.py` | 671 | **大幅删减** — 60+ 函数中大部分是重复实现 deepagent 已有能力 |
| `deepagent/runner.py` | 534 | **精简** — `NativeDeepAgentRunner` 保留薄壳，去掉手动流解析 |
| `deepagent/service.py` | 268 | **保留** — 多用户隔离 + 并发锁是核心增值 |
| `deepagent/middleware.py` | 222 | **精简** — 4 个 middleware 可以合并为 2 个更精简的 |
| `deepagent/errors.py` | 58 | **保留** — 错误层次对企业运维有价值 |
| `serving/asgi.py` | 261 | **保留** — HTTP 入口必须有 |
| `serving/server.py` | 255 | **保留** — stdlib 服务器作为备选 |
| `serving/debug_ui.py` | 1083 | **保留** — 开发调试工具 |
| `contracts.py` | 202 | **精简** — Frame 模型可以简化 |
| `service.py` | 308 | **精简** — 去掉 classic runtime 分支后更薄 |
| `session_store.py` | 162 | **保留** — 多用户 session 管理 |
| `runtime.py` | 137 | **精简** — spec 加载保留，skill 加载交给 deepagent |
| `skills.py` | 267 | **删除** — 完全交给 deepagent `SkillsMiddleware` |
| `workflow.py` | 290 | **精简** — SSE 解析保留，HTTP client 简化 |
| `llm.py` | 209 | **删除** — 模型构建交给 deepagent `resolve_model` |
| `assistant_service.py` | ~500 | **删除** — classic runtime 不再需要 |
| `assistant_protocol.py` | ~200 | **保留** — 协议解析逻辑 |
| `planner.py` | ~200 | **删除** — classic runtime 的 LLM planner |

### 极简方案预估代码量

| 模块 | 预估行数 | 职责 |
|------|---------|------|
| `session.py` | ~120 | `SessionManager` (隔离 + 并发锁 + session 元数据) |
| `api.py` | ~150 | FastAPI 路由 (接参数 → 调 deepagent → 返 SSE/JSON) |
| `config.py` | ~80 | TOML spec 加载 + 环境变量 |
| `middleware.py` | ~300 | 5 个 harness middleware (任务推进 + 完成门控 + skill 生命周期 + workflow 网关 + 协议输出) |
| `protocol.py` | ~60 | `AssistantProtocolFrame` Pydantic 模型 |
| `workflow.py` | ~100 | Workflow tool 注册 + SSE 解析（复用现有） |
| `errors.py` | ~30 | 错误层次 |
| **合计** | **~640** | **对比现在 ~5000 行核心逻辑（去掉 debug_ui/server 后）** |

## 3. 架构图

```
┌─────────────────────────────────────────────────────────┐
│                    前端 / 调用方                          │
│  POST /api/v1/message  {sessionId, custID, txt, ...}    │
└────────────────────────┬────────────────────────────────┘
                         │
┌────────────────────────▼────────────────────────────────┐
│                   api.py (FastAPI)                       │
│  1. 解析请求参数                                          │
│  2. 绑定 sessionId / custID（不做 auth）                  │
│  3. 调 SessionManager 获取/创建 session                   │
│  4. 构建 deepagent invoke 输入                            │
│  5. 调用 compiled_graph.invoke() 或 .astream()           │
│  6. 将 deepagent 输出转成 Assistant Protocol SSE/JSON     │
└────────────────────────┬────────────────────────────────┘
                         │
┌────────────────────────▼────────────────────────────────┐
│               session.py (SessionManager)               │
│  • per-(custID, sessionId) 并发锁                        │
│  • session 元数据：创建时间、过期时间、用户绑定             │
│  • deepagent thread_id = f"{custID}:{sessionId}"        │
│  • 使用 deepagent checkpointer 做实际 state 持久化       │
└────────────────────────┬────────────────────────────────┘
                         │
┌────────────────────────▼────────────────────────────────┐
│            deepagent SDK (不改动，纯依赖)                 │
│                                                         │
│  create_deep_agent(                                     │
│      model = ChatOpenAI(...) 或 "openai:gpt-4o" 等      │
│      tools = [workflow_api_call],                       │
│      system_prompt = harness_system_prompt,             │
│      middleware = [                                      │
│          # SDK 内置:                                     │
│          TodoListMiddleware,     # 多任务规划             │
│          SkillsMiddleware,       # 原生 skill 加载       │
│          FilesystemMiddleware,   # 文件操作              │
│          SummarizationMiddleware, # 上下文压缩           │
│          # Harness 注入:                                 │
│          WorkflowGatewayMiddleware,  # URL 白名单 + 协议 │
│          ProtocolOutputMiddleware,   # 输出 Frame 协议   │
│      ],                                                 │
│      skills = ["/skills/..."],   # 原生 skill 路径      │
│      memory = ["/memory/AGENTS.md"],                    │
│      checkpointer = MemorySaver() 或 PostgresSaver(),   │
│  )                                                      │
└─────────────────────────────────────────────────────────┘
```

## 4. 各模块详细设计

### 4.1 `config.py` — 配置加载

```python
@dataclass
class HarnessConfig:
    """TOML spec 加载后的配置对象。"""
    # deepagent SDK 参数
    model: str                          # "openai:gpt-4o" 或完整 init_chat_model spec
    system_prompt: str                  # agent.md 内容
    skill_sources: list[str]            # ["/skills/base/", "/skills/project/"]
    memory_sources: list[str]           # ["/memory/AGENTS.md"]

    # 企业层参数
    workflow_allowed_urls: list[str]    # URL 白名单
    session_idle_timeout: int = 1800    # 秒
    max_iterations: int = 6            # agent loop 上限

    # Optional
    backend: str = "state"              # "state" | "filesystem"
    backend_root: str | None = None     # filesystem backend 的 root
```

**与现有差异：**
- 去掉 `agent_runtime` 字段（只保留 deepagent 模式）
- 去掉 `llm` 相关配置（不再管 classic runtime 的 LLM 配置）
- skill 配置从 harness 自定义格式改为 deepagent 原生 skill source 路径
- 新增 `backend` 配置，让用户选择 deepagent 的 StateBackend 或 FilesystemBackend

### 4.2 `session.py` — 多用户 Session 管理

```python
class SessionManager:
    """企业层 session 管理 — deepagent SDK 不提供此能力。"""

    def __init__(
        self,
        checkpointer: Checkpointer,
        idle_timeout: timedelta = timedelta(minutes=30),
    ):
        self._checkpointer = checkpointer
        self._idle_timeout = idle_timeout
        self._locks: dict[str, bool] = {}
        self._mu = Lock()
        self._metadata: dict[str, SessionMeta] = {}

    def thread_id(self, cust_id: str, session_id: str) -> str:
        """映射 (custID, sessionId) → deepagent thread_id。"""
        return f"{cust_id}:{session_id}"

    @contextmanager
    def acquire(self, cust_id: str, session_id: str):
        """并发锁：同一 session 同时只允许一个 agent run。"""
        key = self.thread_id(cust_id, session_id)
        with self._mu:
            if self._locks.get(key):
                raise SessionBusyError(f"session {session_id} has an active run")
            self._locks[key] = True
        try:
            yield self._ensure_meta(key)
        finally:
            with self._mu:
                self._locks.pop(key, None)

    def _ensure_meta(self, thread_id: str) -> SessionMeta:
        """创建或返回 session 元数据，检查过期。"""
        ...
```

**关键点：**
- `thread_id` 映射是唯一需要 harness 管理的。deepagent 的 `checkpointer` 按 `thread_id` 自动持久化 agent state
- 并发锁逻辑和现有 `SessionRunLockStore` 基本一致，但合并到一个类
- Session 元数据（创建时间、过期）仍由 harness 管理，因为 deepagent checkpointer 没有过期概念

### 4.3 `middleware.py` — 5 个 Harness Middleware

#### ① TaskProgressMiddleware（任务稳定推进）

```python
class TaskProgressMiddleware(AgentMiddleware):
    """通过 system prompt 注入强约束：串行任务执行、缺槽追问、不跳任务。"""

    def wrap_model_call(self, request, handler):
        return handler(self._inject_task_constraints(request))

    def _inject_task_constraints(self, request):
        # 注入约束：
        # - 一次只推进一个 current_task
        # - 缺少必填参数必须追问
        # - 不能自行标记完成
        # - 多任务按顺序推进
        # - workflow 完成后设为 waiting_assistant_completion
```

#### ② CompletionGateMiddleware（前端完成门控）

```python
class CompletionGateMiddleware(AgentMiddleware):
    """workflow_api_call 返回后自动终止 agent loop，等待前端 /completion 确认。"""

    @hook_config(can_jump_to=["end"])
    def before_model(self, state, runtime):
        if _has_workflow_tool_result(state):
            return {"jump_to": "end", "messages": [
                AIMessage(content=json.dumps({
                    "frames": [{"status": "waiting_assistant_completion", ...}]
                }))
            ]}
```

**关键：** agent 永远不能自行将任务标记为 completed，transition 到 completed 只能通过前端调 `/api/v1/task/completion`。

#### ③ SkillLifecycleMiddleware（Skill 加载/卸载）

```python
class SkillLifecycleMiddleware(AgentMiddleware):
    """控制 skill 的动态加载和卸载。"""

    def wrap_model_call(self, request, handler):
        return handler(self._scope_skills(request))

    def _scope_skills(self, request):
        # 单意图：只加载命中意图对应的 skill
        # 多意图：按 task_list 顺序，只加载 current_task 的 skill
        # 非当前任务的 skill 不注入上下文（节省 context window）
```

#### ④ WorkflowGatewayMiddleware（URL 白名单 + hooks）

```python
class WorkflowGatewayMiddleware(AgentMiddleware):
    """在 workflow_api_call tool 执行前做 URL 白名单校验 + before/after hooks。"""

    def wrap_tool_call(self, request, handler):
        if _tool_name(request) == "workflow_api_call":
            self._validate_url(request)   # 白名单校验
            self._run_before_hooks(request)
            result = handler(request)
            self._run_after_hooks(request, result)  # 非致命
            return result
        return handler(request)
```

#### ⑤ ProtocolOutputMiddleware（协议输出校验）

```python
class ProtocolOutputMiddleware(AgentMiddleware):
    """校验/补全 Agent 输出的 AssistantProtocolFrame JSON。"""

    def after_model(self, state, runtime):
        # 解析最后一条 AI 消息
        # 如果是 protocol JSON，填充缺失字段
        # 如果不是 JSON，允许通过（自然语言回复）
```

**对比现有 v1 的 4 个 middleware：**
- `HarnessContextMiddleware` → **删除**。上下文改为 `system_prompt` 直接拼接
- `HarnessSkillFileMiddleware` → **替换为 SkillLifecycleMiddleware**（更细粒度的加载/卸载控制）
- `WorkflowRequestMiddleware` → **删除**。deepagent 原生通过 tool call 触发 workflow
- `WorkflowCompletionMiddleware` → **替换为 CompletionGateMiddleware**（前端门控更强约束）
- **新增** TaskProgressMiddleware（保证任务稳定串行推进）

### 4.4 `api.py` — FastAPI HTTP 入口

```python
app = FastAPI(title="Intent Router Harness")

@app.post("/api/v1/message")
async def handle_message(request: MessageRequest):
    config = load_config()
    session_mgr = get_session_manager()
    agent = get_compiled_agent()

    thread_id = session_mgr.thread_id(request.custID, request.sessionId)

    with session_mgr.acquire(request.custID, request.sessionId):
        if request.stream:
            return StreamingResponse(
                _stream_agent(agent, request, thread_id),
                media_type="text/event-stream",
            )
        result = agent.invoke(
            {"messages": [HumanMessage(content=_build_user_input(request))]},
            config={"configurable": {"thread_id": thread_id}},
        )
        return _extract_protocol_response(result)

async def _stream_agent(agent, request, thread_id):
    """SSE streaming — 直接利用 deepagent 的 astream。"""
    async for chunk in agent.astream(
        {"messages": [HumanMessage(content=_build_user_input(request))]},
        config={"configurable": {"thread_id": thread_id}},
        stream_mode="messages",
    ):
        # chunk 是 (message, metadata) 元组
        # 将 message 转为 SSE event
        yield f"data: {_message_to_sse(chunk)}\n\n"

def _build_user_input(request: MessageRequest) -> str:
    """把前端参数直接打包成 JSON 字符串作为 user message。"""
    return json.dumps({
        "txt": request.txt,
        "sessionId": request.sessionId,
        "custID": request.custID,
        "executionMode": request.executionMode,
        "taskId": request.taskId,
        # ... 其他前端字段直接透传
    }, ensure_ascii=False)
```

**关键变化：**
- 不再有 `IntentRouterHarnessService` → `DeepAgentAssistantProtocolService` → `NativeDeepAgentRunner` 三层嵌套
- 直接 `agent.invoke()` / `agent.astream()`
- 前端参数透传，不做额外处理

### 4.5 `workflow.py` — Workflow Tool

```python
def create_workflow_tool(
    allowed_urls: list[str],
    hooks: list[WorkflowHook] | None = None,
) -> StructuredTool:
    """创建 workflow_api_call LangChain tool。"""

    def workflow_api_call(method: str, url: str, body: dict) -> str:
        # 1. URL 白名单校验（也可以放在 middleware）
        # 2. before hooks
        # 3. HTTP POST + SSE 解析
        # 4. after hooks
        # 5. 返回 JSON 结果
        ...

    return StructuredTool.from_function(
        func=workflow_api_call,
        name="workflow_api_call",
        description="Call a workflow API endpoint via SSE.",
        args_schema=WorkflowApiCallInput,
    )
```

**变化：** 现有的 `_workflow_api_call` 逻辑基本保留，但从 runner 类方法提取为独立工厂函数。

### 4.6 Agent 初始化（一次性）

```python
def build_agent(config: HarnessConfig) -> CompiledStateGraph:
    """一次性构建 deepagent compiled graph。"""
    from deepagents import create_deep_agent
    from deepagents.backends import StateBackend, FilesystemBackend
    from langgraph.checkpoint.memory import MemorySaver

    backend = (
        FilesystemBackend(root_dir=config.backend_root)
        if config.backend == "filesystem"
        else StateBackend()
    )

    return create_deep_agent(
        model=config.model,
        tools=[create_workflow_tool(config.workflow_allowed_urls)],
        system_prompt=config.system_prompt,
        middleware=[
            WorkflowGatewayMiddleware(config.workflow_allowed_urls),
            ProtocolOutputMiddleware(),
        ],
        skills=config.skill_sources,
        memory=config.memory_sources,
        backend=backend,
        checkpointer=MemorySaver(),  # 或 PostgresSaver() for production
    )
```

## 5. Skill 格式迁移

### 当前格式（harness 自定义）

```yaml
# skills/转账/SKILL.md
---
name: transfer
description: "银行转账"
intent_codes: [TRANSFER]
required_slots: [payee, amount]
---
```

加上独立的 `references/` 目录存放 API endpoint 信息。

### 目标格式（deepagent 原生）

```yaml
# skills/transfer/SKILL.md
---
name: transfer
description: "银行转账技能 — 识别收款人和金额，执行转账 workflow"
---

## When to Use
- 用户表达转账意图（"给XX转YY元"、"转账"等）

## Required Information
- payee: 收款人姓名或账号
- amount: 转账金额

## Workflow
当所有必填信息已收集且 executionMode=execute 时，调用：
workflow_api_call(method="POST", url="http://workflow-server/transfer", body={...})

## Reference API
- URL: http://workflow-server/api/v1/transfer
- Method: POST
- Body: {"payee": "...", "amount": ...}
```

**关键区别：**
- 不再有 `intent_codes` / `required_slots` 结构化字段 — 这些信息写在 skill markdown 中，让 deepagent AI 自己理解
- 不再有独立 `references/` 目录 — API 信息直接写在 skill 文档里
- deepagent 的 `SkillsMiddleware` 负责按需加载 skill 内容到 system prompt

## 6. 能力对照表

| 能力 | 现有 harness 实现 | 极简方案 |
|------|-------------------|---------|
| **Agent 编排** | `NativeDeepAgentRunner._run_agent_stream()` 手动管理 | `agent.invoke()` / `agent.astream()` 一行搞定 |
| **Skill 加载** | `SkillLibrary` + `SkillDocument` (267 行) | deepagent `SkillsMiddleware`（零代码） |
| **Memory** | `InMemorySessionStore` (162 行) 自管 state | deepagent `checkpointer` + 薄壳 `SessionManager` |
| **Middleware** | 4 个自定义 middleware (222 行) | 2 个 (≤100 行) |
| **LLM 配置** | `llm.py` (209 行) + `_resolve_deepagent_model` | `model="openai:gpt-4o"` 字符串或 `ChatOpenAI(...)` 实例 |
| **多任务规划** | `helpers.py` 手动管理 task_list/current_task | deepagent `TodoListMiddleware`（原生 write_todos） |
| **上下文注入** | `HarnessContextMiddleware` + `_harness_context_block` | `system_prompt` 参数直接拼接 |
| **Skill 文件拦截** | `HarnessSkillFileMiddleware` 虚拟文件系统 | deepagent `FilesystemMiddleware` 原生支持 |
| **Workflow 调用** | `_workflow_api_call` 方法 + 3 层包装 | 独立 `StructuredTool`，直接注册 |
| **流解析** | `_stream_chunk_mode_data` 等 8 个函数 | `agent.astream(stream_mode="messages")` 直接用 |
| **协议输出** | `_parse_deepagent_response` + Frame 组装 | AI 直接输出 JSON，middleware 只校验 |
| **多用户隔离** | `SessionRunLockStore` + `InMemorySessionStore` | `SessionManager`（合并为一个类） |
| **错误分类** | 5 种错误类型 | 保留（对运维有价值） |
| **Classic runtime** | `AssistantProtocolService` + `LLMMessagePlanner` (~700 行) | **删除** |

## 7. 迁移路径

### Phase 1: 最小可用（MVP）

1. 新建 `harness_v2/` 目录（不影响现有代码）
2. 实现 `config.py` + `session.py` + `api.py` + `middleware.py`
3. 迁移 1 个 skill 到 deepagent 原生格式
4. 验证 `/api/v1/message` 端到端可用
5. 对比新旧两个版本的输出

### Phase 2: 完整迁移

1. 迁移所有 skills 到 deepagent 原生格式
2. 添加 workflow hooks 支持
3. 添加 debug_ui
4. 添加 regression 测试框架适配
5. 删除旧代码，v2 取代 v1

### Phase 3: 生产化

1. 替换 `MemorySaver()` 为 `PostgresSaver()` / `RedisSaver()`
2. 添加 Prometheus metrics
3. 添加结构化日志
4. K8s deployment 更新

## 8. 风险评估

| 风险 | 影响 | 缓解 |
|------|------|------|
| deepagent SDK 版本更新可能 break | 中 | pin 精确版本，升级前跑回归测试 |
| skill 格式迁移丢失结构化约束 | 中 | 用 middleware 做 slot 校验；或在 skill markdown 中明确约束 |
| 多任务串行由 AI 自主判断而非代码强制 | 中 | system_prompt 强约束 + middleware 后处理校验 |
| 去掉 classic runtime 后不支持无 deepagent 场景 | 低 | 按需保留为可选依赖 |
| SSE streaming 格式可能和前端不兼容 | 中 | 在 api.py 做格式转换层，对前端保持协议不变 |

## 9. 需要决策的问题

1. **是否完全删除 classic runtime？** 还是保留为 fallback？
2. **Skill 格式**：全部迁移为 deepagent 原生格式？还是写一个 adapter 兼容现有格式？
3. **State 持久化**：MVP 用 `MemorySaver()`（内存），生产用什么？PostgreSQL？Redis？
4. **task_list / current_task 协议**：完全交给 deepagent `write_todos`，还是 harness 做后处理补全？
5. **前端协议兼容**：SSE event 格式是否需要和现有 `/api/v1/message` 完全兼容？
