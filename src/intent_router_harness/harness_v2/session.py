"""Multi-user session isolation — the core enterprise capability deepagent lacks."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from threading import Lock
from typing import Any

from intent_router_harness.harness_v2.errors import SessionBusyError, SessionExpiredError


@dataclass
class SessionMeta:
    """Enterprise-layer metadata that deepagent checkpointer does not manage."""

    thread_id: str
    cust_id: str
    session_id: str
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    last_active_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def is_expired(self, idle_timeout: timedelta) -> bool:
        return (datetime.now(timezone.utc) - self.last_active_at) > idle_timeout

    def touch(self) -> None:
        self.last_active_at = datetime.now(timezone.utc)


class SessionManager:
    """Per-(custID, sessionId) isolation + concurrency lock.

    Deepagent's checkpointer handles actual agent state persistence keyed
    by ``thread_id``.  This class adds:

    1. Mapping from frontend (custID, sessionId) → deepagent thread_id
    2. Per-session concurrency lock (one active run at a time)
    3. Session idle-timeout expiration
    4. User binding enforcement (custID must match)
    """

    def __init__(self, idle_timeout: timedelta = timedelta(minutes=30)) -> None:
        self._idle_timeout = idle_timeout
        self._active_runs: dict[str, bool] = {}
        self._mu = Lock()
        self._sessions: dict[str, SessionMeta] = {}

    def thread_id(self, cust_id: str, session_id: str) -> str:
        """Map (custID, sessionId) → deepagent thread_id."""
        return f"{cust_id}:{session_id}"

    @contextmanager
    def acquire(self, cust_id: str, session_id: str):
        """Acquire a per-session run lock.  Yields ``SessionMeta``."""
        key = self.thread_id(cust_id, session_id)
        with self._mu:
            if self._active_runs.get(key):
                raise SessionBusyError(f"session {session_id} already has an active agent run")
            self._active_runs[key] = True
        try:
            meta = self._ensure_session(cust_id, session_id)
            if meta.is_expired(self._idle_timeout):
                self._expire(key)
                meta = self._ensure_session(cust_id, session_id)
            meta.touch()
            yield meta
        finally:
            with self._mu:
                self._active_runs.pop(key, None)

    def get_meta(self, cust_id: str, session_id: str) -> SessionMeta | None:
        """Return session metadata without locking."""
        key = self.thread_id(cust_id, session_id)
        return self._sessions.get(key)

    def _ensure_session(self, cust_id: str, session_id: str) -> SessionMeta:
        key = self.thread_id(cust_id, session_id)
        if key not in self._sessions:
            self._sessions[key] = SessionMeta(
                thread_id=key,
                cust_id=cust_id,
                session_id=session_id,
            )
        return self._sessions[key]

    def _expire(self, key: str) -> None:
        self._sessions.pop(key, None)
