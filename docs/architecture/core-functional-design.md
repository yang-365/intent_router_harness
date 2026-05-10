# 核心功能设计

## 目标

服务面向多人在线的助手协议路由，不启动 subagent。它需要稳定完成意图识别、多任务拆分、当前任务提槽、任务交接和任务完成推进。

## 主流程

```text
用户消息
  -> 意图识别 / 多意图拆分
  -> 服务端生成或读取 task_list
  -> 服务端选择 current_task
  -> 按 current_task.intent_code 加载一个 skill 正文
  -> 当前任务提槽
  -> 服务端按 required_slots 计算状态
  -> waiting_user_input 或 ready_for_dispatch
```

## 多任务规则

- 一个 task 只能承载一个 intent。
- 多个 task 在同一 session 中可以并存，但处理必须串行。
- 非当前任务不能被提槽阶段修改。
- 当前任务完成后，由 `/api/v1/task/completion` 推进到下一个任务。

## 渐进式加载

- 意图识别阶段只使用 skill 的 `name`、`description`、`intent_codes`。
- 提槽阶段只加载当前任务对应的 `SKILL.md` 正文。
- Reference 只能由当前已加载 skill 暴露并按需加载。
- session 只保存轻量 lease，不保存 Markdown 正文。

## 状态计算

LLM 不决定最终任务状态。服务端根据当前 skill 的 `required_slots` 判断：

- 缺槽：`waiting_user_input`
- 槽齐：`ready_for_dispatch`
- 终态：`completed`、`cancelled`、`failed`
