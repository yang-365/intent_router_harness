# Design

## Runtime Flow

The router uses fixed business stages instead of configurable prompt rendering layers.

1. Intent recognition reads `agent.md` plus every skill's `name`, `description`, and `intent_codes`.
2. The recognizer outputs a serial `task_list`; every task has exactly one `intent_code`.
3. The service selects one `current_task` from the queue.
4. Slot filling loads only the `SKILL.md` body for `current_task.intent_code`.
5. The slot filler may update only `current_task.slot_memory`.
6. The service computes task readiness from the current skill's `required_slots`.
7. `/api/v1/task/completion` advances the queue after downstream completion.

## Skill Contract

Each dispatchable skill declares one business intent:

```yaml
name: transfer-routing
description: 识别并处理转账/汇款意图 AG_TRANS 的补槽与交接规则。
intent_codes: ["AG_TRANS"]
required_slots: ["payee_name", "amount"]
```

Skill bodies define intent boundaries, slot semantics, normalization rules, and handoff behavior. They do not define prompt rendering stages.

## State Boundaries

- `SessionState` is only the `sessionId` to `custID` binding and idle timeout boundary.
- `TaskRuntimeState` owns `task_list`, `current_task`, `slot_memory`, and lightweight context leases.
- Skill and reference Markdown bodies are loaded into prompts only and are never stored in session state.
- Completed, cancelled, or failed tasks release their context leases and are removed from runtime state.
