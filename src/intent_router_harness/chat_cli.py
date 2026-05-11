"""Interactive chat CLI for debugging intent_router_harness conversations.

Usage::

    python -m intent_router_harness chat
    python -m intent_router_harness chat --url http://127.0.0.1:8765
    python -m intent_router_harness chat --session-id my_session --cust-id C0001

Features:
- Multi-turn conversation with persistent session
- Real-time SSE trace event display (color-coded by stage)
- Automatic task completion via /complete command
- Shows every middleware step in the call chain
"""

from __future__ import annotations

import contextlib
import json
import sys
from typing import Any
from urllib import request

# ---------------------------------------------------------------------------
# ANSI color helpers
# ---------------------------------------------------------------------------

_RESET = "\033[0m"
_BOLD = "\033[1m"
_DIM = "\033[2m"
_RED = "\033[31m"
_GREEN = "\033[32m"
_YELLOW = "\033[33m"
_BLUE = "\033[34m"
_MAGENTA = "\033[35m"
_CYAN = "\033[36m"
_WHITE = "\033[37m"
_GRAY = "\033[90m"
_BG_GREEN = "\033[42m"
_BG_RED = "\033[41m"
_BG_YELLOW = "\033[43m"
_BG_BLUE = "\033[44m"

# Map trace stages to colors for visual grouping
_STAGE_COLORS: dict[str, str] = {
    "request_received": _BLUE,
    "deepagent_runtime_start": _BLUE,
    "task_constraints_injected": _GRAY,
    "skill_metadata_injected": _CYAN,
    "skill_loaded": _GREEN,
    "skill_reference_loaded": _GREEN,
    "skill_file_read": _CYAN,
    "intent_recognized": _MAGENTA,
    "slots_extracted": _MAGENTA,
    "task_planned": _YELLOW,
    "task_added": _GREEN,
    "task_removed": _RED,
    "task_status_changed": _YELLOW,
    "task_slots_updated": _YELLOW,
    "current_task_switched": _YELLOW,
    "workflow_call_started": _CYAN,
    "workflow_call_completed": _GREEN,
    "workflow_url_rejected": _RED,
    "completion_gate_triggered": _YELLOW,
    "assistant_protocol_frames": _BLUE,
    "protocol_frame_output": _BLUE,
    "task_completion": _GREEN,
}


def _color(text: str, color: str) -> str:
    return f"{color}{text}{_RESET}"


def _print_separator(char: str = "─", width: int = 72) -> None:
    print(_color(char * width, _GRAY))


def _print_header(text: str) -> None:
    print(f"\n{_BOLD}{_BG_BLUE}{_WHITE} {text} {_RESET}")


# ---------------------------------------------------------------------------
# SSE client
# ---------------------------------------------------------------------------


def _post_sse(url: str, payload: dict[str, Any]) -> list[tuple[str, Any]]:
    """POST JSON and parse SSE response."""
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Accept": "text/event-stream",
            "Content-Type": "application/json",
        },
    )
    with request.urlopen(req, timeout=300) as response:
        raw = response.read().decode("utf-8")
    return _parse_sse(raw)


def _parse_sse(raw: str) -> list[tuple[str, Any]]:
    events: list[tuple[str, Any]] = []
    for block in raw.split("\n\n"):
        event_name = "message"
        data_lines: list[str] = []
        for line in block.splitlines():
            if line.startswith("event:"):
                event_name = line.removeprefix("event:").strip()
            elif line.startswith("data:"):
                data_lines.append(line.removeprefix("data:").strip())
        if not data_lines:
            continue
        data_text = "\n".join(data_lines)
        data: Any = data_text
        if data_text != "[DONE]":
            with contextlib.suppress(json.JSONDecodeError):
                data = json.loads(data_text)
        events.append((event_name, data))
    return events


# ---------------------------------------------------------------------------
# Display helpers
# ---------------------------------------------------------------------------


def _display_trace(trace: dict[str, Any]) -> None:
    """Print a single trace event with color coding."""
    stage = trace.get("stage", "")
    title = trace.get("title", "")
    summary = trace.get("summary", "")
    data = trace.get("data", {})
    color = _STAGE_COLORS.get(stage, _GRAY)

    print(f"  {_color('▸', color)} {_color(stage, color + _BOLD)}")
    if title:
        print(f"    {_color(title, _WHITE + _BOLD)}: {summary}")

    # Show important data fields inline
    if isinstance(data, dict):
        skip_keys = {"session_id", "cust_id", "execution_mode", "thread_id"}
        interesting = {k: v for k, v in data.items() if k not in skip_keys and v}
        if interesting:
            for key, value in interesting.items():
                if isinstance(value, (dict, list)):
                    val_str = json.dumps(value, ensure_ascii=False, indent=2)
                    if len(val_str) > 200:
                        val_str = val_str[:200] + "..."
                    print(f"    {_color(key, _DIM)}: {val_str}")
                else:
                    print(f"    {_color(key, _DIM)}: {value}")


def _display_message_frame(frame: dict[str, Any]) -> None:
    """Print a protocol message frame."""
    ok = frame.get("ok", True)
    status = frame.get("status", "")
    cs = frame.get("completion_state", 0)
    reason = frame.get("completion_reason", "")

    ok_badge = _color(" OK ", _BG_GREEN + _WHITE + _BOLD) if ok else _color(" FAIL ", _BG_RED + _WHITE + _BOLD)
    status_color = {
        "running": _BLUE,
        "waiting_user_input": _YELLOW,
        "ready_for_dispatch": _CYAN,
        "waiting_assistant_completion": _MAGENTA,
        "completed": _GREEN,
        "failed": _RED,
    }.get(status, _WHITE)

    print(f"\n  {ok_badge} {_color(status, status_color + _BOLD)}  completion_state={cs}  reason={reason}")

    # Message
    message = frame.get("message", "")
    if message:
        print(f"\n  {_color('💬 Message:', _WHITE + _BOLD)}")
        print(f"  {message}")

    # Output
    output = frame.get("output", {})
    if output:
        print(f"\n  {_color('📦 Output:', _WHITE + _BOLD)}")
        print(f"  {json.dumps(output, ensure_ascii=False, indent=2)}")

    # Slot memory
    slot_memory = frame.get("slot_memory", {})
    if slot_memory:
        print(f"\n  {_color('🔑 Slot Memory:', _WHITE + _BOLD)}")
        for k, v in slot_memory.items():
            print(f"    {k} = {v}")

    # Task list
    task_list = frame.get("task_list", [])
    if task_list:
        print(f"\n  {_color('📋 Task List:', _WHITE + _BOLD)}")
        for i, task in enumerate(task_list):
            tid = task.get("taskId", "")
            ts = task.get("status", "")
            tt = task.get("title") or task.get("intent_code") or ""
            print(f"    #{i + 1} [{ts}] {tid} — {tt}")

    # Current task
    current_task = frame.get("current_task")
    if current_task:
        print(f"\n  {_color('▶ Current Task:', _WHITE + _BOLD)} {current_task.get('taskId', '')} [{current_task.get('status', '')}]")

    # Intent
    intent_code = frame.get("intent_code")
    if intent_code:
        print(f"  {_color('🎯 Intent:', _WHITE + _BOLD)} {intent_code}")

    # Actions
    actions = frame.get("actions", [])
    if actions:
        print(f"\n  {_color('⚡ Actions:', _WHITE + _BOLD)}")
        print(f"  {json.dumps(actions, ensure_ascii=False, indent=2)}")


def _display_response(events: list[tuple[str, Any]]) -> dict[str, Any] | None:
    """Display all SSE events and return the last message frame."""
    traces = [data for name, data in events if name == "trace" and isinstance(data, dict)]
    messages = [data for name, data in events if name == "message" and isinstance(data, dict)]

    if traces:
        _print_header("Trace Events")
        _print_separator()
        for trace in traces:
            _display_trace(trace)
        _print_separator()

    last_frame = None
    if messages:
        _print_header("Response")
        for msg in messages:
            _display_message_frame(msg)
            last_frame = msg

    print()
    return last_frame


# ---------------------------------------------------------------------------
# Interactive chat loop
# ---------------------------------------------------------------------------


def run_chat(
    base_url: str = "http://127.0.0.1:8765",
    session_id: str = "cli_debug_001",
    cust_id: str = "C0001",
    execution_mode: str = "execute",
) -> None:
    """Run the interactive chat loop."""
    print(f"""
{_BOLD}╔══════════════════════════════════════════════════════════╗
║            Intent Router Harness — Chat CLI              ║
╚══════════════════════════════════════════════════════════╝{_RESET}

  {_color('Server:', _DIM)} {base_url}
  {_color('Session:', _DIM)} {session_id}
  {_color('Customer:', _DIM)} {cust_id}
  {_color('Mode:', _DIM)} {execution_mode}

  {_color('Commands:', _BOLD)}
    {_color('/complete', _GREEN)}    — 模拟完成当前任务 (completionSignal=1)
    {_color('/fail', _RED)}        — 模拟任务失败 (completionSignal=2)
    {_color('/session <id>', _CYAN)} — 切换 session ID
    {_color('/mode <mode>', _CYAN)}  — 切换 executionMode (execute/plan)
    {_color('/status', _BLUE)}      — 显示当前会话状态
    {_color('/clear', _YELLOW)}      — 清屏
    {_color('/quit', _GRAY)}        — 退出
""")

    state: dict[str, Any] = {
        "last_frame": None,
        "task_id": None,
        "turn": 0,
    }

    while True:
        try:
            user_input = input(f"{_BOLD}{_GREEN}You ▸ {_RESET}").strip()
        except (EOFError, KeyboardInterrupt):
            print(f"\n{_color('Bye!', _DIM)}")
            break

        if not user_input:
            continue

        # --- Commands ---
        if user_input == "/quit" or user_input == "/exit":
            print(_color("Bye!", _DIM))
            break

        if user_input == "/clear":
            print("\033[2J\033[H", end="")
            continue

        if user_input == "/status":
            print(f"  session_id: {session_id}")
            print(f"  cust_id: {cust_id}")
            print(f"  execution_mode: {execution_mode}")
            print(f"  turns: {state['turn']}")
            if state["last_frame"]:
                print(f"  last_status: {state['last_frame'].get('status')}")
                print(f"  completion_state: {state['last_frame'].get('completion_state')}")
                print(f"  task_id: {state['task_id']}")
            continue

        if user_input.startswith("/session "):
            session_id = user_input[9:].strip()
            state = {"last_frame": None, "task_id": None, "turn": 0}
            print(_color(f"  Session switched to: {session_id}", _CYAN))
            continue

        if user_input.startswith("/mode "):
            execution_mode = user_input[6:].strip()
            print(_color(f"  Execution mode: {execution_mode}", _CYAN))
            continue

        if user_input in ("/complete", "/fail"):
            signal = 1 if user_input == "/complete" else 2
            task_id = state.get("task_id") or "current_task"
            last = state.get("last_frame") or {}
            if last.get("current_task"):
                task_id = last["current_task"].get("taskId", task_id)
            elif last.get("intent_code"):
                task_id = last["intent_code"]

            payload = {
                "sessionId": session_id,
                "custID": cust_id,
                "taskId": task_id,
                "completionSignal": signal,
                "stream": True,
                "debugTrace": True,
            }
            label = "完成" if signal == 1 else "失败"
            print(_color(f"\n  ⟹ POST /api/v1/task/completion (signal={signal}, taskId={task_id}) [{label}]", _YELLOW))
            try:
                events = _post_sse(f"{base_url}/api/v1/task/completion", payload)
                frame = _display_response(events)
                if frame:
                    state["last_frame"] = frame
            except Exception as exc:
                print(_color(f"  Error: {exc}", _RED))
            continue

        # --- Normal message ---
        state["turn"] += 1
        payload = {
            "sessionId": session_id,
            "custID": cust_id,
            "txt": user_input,
            "stream": True,
            "debugTrace": True,
            "executionMode": execution_mode,
            "config_variables": [
                {"name": "custID", "value": cust_id},
                {"name": "sessionID", "value": session_id},
                {"name": "currentDisplay", "value": ""},
                {"name": "agentSessionID", "value": session_id},
            ],
        }

        print(_color(f"\n  ⟹ POST /api/v1/message (turn #{state['turn']})", _BLUE))
        try:
            events = _post_sse(f"{base_url}/api/v1/message", payload)
            frame = _display_response(events)
            if frame:
                state["last_frame"] = frame
                ct = frame.get("current_task")
                if ct:
                    state["task_id"] = ct.get("taskId")
                elif frame.get("intent_code"):
                    state["task_id"] = frame["intent_code"]
                # Hint for next action
                status = frame.get("status", "")
                cs = frame.get("completion_state", 0)
                if status == "waiting_assistant_completion" and cs == 1:
                    print(_color("  💡 Hint: type /complete to confirm task completion, or /fail to reject", _YELLOW))
                elif status == "waiting_user_input":
                    print(_color("  💡 Hint: the agent is waiting for your input", _YELLOW))
        except Exception as exc:
            print(_color(f"  Error: {exc}", _RED))


def main(argv: list[str] | None = None) -> int:
    """CLI entry point for the chat tool."""
    import argparse

    parser = argparse.ArgumentParser(
        prog="intent_router_harness chat",
        description="Interactive chat CLI for debugging harness conversations.",
    )
    parser.add_argument("--url", default="http://127.0.0.1:8765", help="Harness base URL")
    parser.add_argument("--session-id", default="cli_debug_001", help="Session ID")
    parser.add_argument("--cust-id", default="C0001", help="Customer ID")
    parser.add_argument("--mode", default="execute", help="Execution mode (execute/plan)")

    args = parser.parse_args(argv)
    run_chat(
        base_url=args.url,
        session_id=args.session_id,
        cust_id=args.cust_id,
        execution_mode=args.mode,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
