"""Minimal harness v2 — thin enterprise shell over deepagent SDK.

Modules:
    config    — TOML spec loading
    session   — Multi-user session isolation + concurrency locks
    protocol  — AssistantProtocolFrame (same wire format as v1)
    errors    — Error hierarchy
    middleware — 6 enterprise middleware classes
    skill_registry — Filesystem-based skill index with progressive loading
    workflow  — Workflow tool factory + SSE parsing
    agent     — build_agent() via create_deep_agent
    api       — FastAPI routes (/api/v1/message, /api/v1/task/completion)
"""

from intent_router_harness.harness_v2.api import HarnessApp, create_app
from intent_router_harness.harness_v2.config import HarnessConfig, load_config
from intent_router_harness.harness_v2.errors import (
    HarnessError,
    ProtocolOutputError,
    SessionBusyError,
    SessionExpiredError,
    WorkflowExecutionError,
    WorkflowUrlNotAllowedError,
)
from intent_router_harness.harness_v2.protocol import (
    AssistantProtocolFrame,
    MessageRequest,
    TaskCompletionRequest,
    TraceEvent,
)
from intent_router_harness.harness_v2.session import SessionManager, SessionMeta
from intent_router_harness.harness_v2.skill_registry import SkillMeta, SkillRegistry

__all__ = [
    "AssistantProtocolFrame",
    "HarnessApp",
    "HarnessConfig",
    "HarnessError",
    "MessageRequest",
    "ProtocolOutputError",
    "SessionBusyError",
    "SessionExpiredError",
    "SessionManager",
    "SessionMeta",
    "SkillMeta",
    "SkillRegistry",
    "TaskCompletionRequest",
    "TraceEvent",
    "WorkflowExecutionError",
    "WorkflowUrlNotAllowedError",
    "create_app",
    "load_config",
]
