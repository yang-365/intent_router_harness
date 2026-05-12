# Test Plan: Todo Content Skill Name Prefix Format

## What Changed
- Todo content format changed to `<skill-name> 任务描述` (e.g. `transfer-routing 给张三转账100`)
- Middleware matches first token against `registry.names()` to load skill
- Frontend title strips the skill name prefix for clean display
- Prompt strengthened with explicit correct/incorrect format examples

## Primary Flow: Multi-intent message triggers correct todo format

### Setup
1. Start mock workflow server on port 9877
2. Load .env and start harness server on port 8765
3. Use a **new** session ID (not `my_session`) to avoid stale state

### Test: Send "给张三转账635元，然后缴费"

**Steps:**
1. POST to `/api/v1/message` with `debugTrace=true`, new session ID
2. Parse SSE response for trace events

**Assertions (ordered by importance):**

| # | Assertion | Pass Criteria | Fail Criteria |
|---|-----------|---------------|---------------|
| 1 | **Todo content has skill name prefix** | `task_planned` trace's raw todos or agent logs show content starting with a registered skill name (e.g. `transfer-routing 给张三转账635元`) | Content is plain text without skill name prefix (e.g. `给张三转账635元`) |
| 2 | **skill_loaded fires AFTER task_planned** | In trace event sequence: `task_planned` appears before `skill_loaded` | `skill_loaded` appears before `task_planned`, or `skill_loaded` never appears |
| 3 | **Correct skill loaded** | `skill_loaded` event shows `skill_name: transfer-routing` (not `bill-payment-routing`) | Wrong skill loaded or no skill loaded |
| 4 | **Frontend title is clean** | `task_list` in trace/response shows title WITHOUT skill name prefix (e.g. `给张三转账635元`) | Title still contains `transfer-routing` prefix |
| 5 | **Two todos created** | `task_planned` shows exactly 2 tasks | More or fewer tasks |
| 6 | **First todo is in_progress** | First task has `status: in_progress` | Different status |

### Verification Method
- Shell-based: curl POST with SSE parsing, extract trace events as JSON
- Check harness server logs for raw `write_todos` output
- No browser interaction needed — all verification via trace events and server logs

### Edge Case: Single intent
If time permits, send a single-intent message like "给张三转账100" to verify:
- Still creates one todo with skill name prefix
- skill_loaded fires after task_planned
