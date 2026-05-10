# 当前服务架构

## 模块

| 模块 | 职责 |
| --- | --- |
| `asgi.py` / `server.py` | HTTP 入口、SSE 输出、本地验证 UI |
| `service.py` | 应用服务边界，加载配置并委托助手协议服务 |
| `assistant_service.py` | session 读取、任务运行态保存、协议帧生成、任务完成推进 |
| `planner.py` | 两阶段 LLM：意图拆分和当前任务提槽 |
| `skills.py` | 加载 `SKILL.md` 元数据、正文和 reference |
| `session_store.py` | `sessionId` 到 `custID` 绑定、空闲过期和任务运行态存储 |

## LLM 调用

- 无活跃任务：两次 LLM。
  - 第一次只做意图识别和任务拆分。
  - 第二次只对当前任务提槽。
- 有活跃等待任务：一次 LLM，只做当前任务提槽。
- 任务完成回调：不调用 LLM。

## 上下文

- `sessionId`、`agentSessionID`、`custID` 等系统标识不会进入 LLM prompt。
- 意图识别只使用 skill 摘要。
- 提槽只加载当前任务对应 skill 正文。
- task 结束后释放 skill/reference lease。
