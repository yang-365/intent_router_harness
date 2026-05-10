"""Tests for harness_v2.session module — multi-user isolation."""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

import pytest

from intent_router_harness.harness_v2.errors import SessionBusyError
from intent_router_harness.harness_v2.session import SessionManager, SessionMeta


class TestSessionManager:
    def test_thread_id_format(self):
        mgr = SessionManager()
        tid = mgr.thread_id("user1", "sess1")
        assert tid == "user1:sess1"

    def test_acquire_creates_session(self):
        mgr = SessionManager()
        with mgr.acquire("u1", "s1") as meta:
            assert meta.cust_id == "u1"
            assert meta.session_id == "s1"
            assert meta.thread_id == "u1:s1"

    def test_concurrent_same_session_rejected(self):
        mgr = SessionManager()
        with mgr.acquire("u1", "s1"):
            with pytest.raises(SessionBusyError, match="already has an active"):
                with mgr.acquire("u1", "s1"):
                    pass

    def test_concurrent_different_sessions_allowed(self):
        mgr = SessionManager()
        with mgr.acquire("u1", "s1"):
            with mgr.acquire("u1", "s2") as meta2:
                assert meta2.session_id == "s2"

    def test_concurrent_different_users_allowed(self):
        mgr = SessionManager()
        with mgr.acquire("u1", "s1"):
            with mgr.acquire("u2", "s1") as meta2:
                assert meta2.cust_id == "u2"

    def test_lock_released_after_exit(self):
        mgr = SessionManager()
        with mgr.acquire("u1", "s1"):
            pass
        with mgr.acquire("u1", "s1") as meta:
            assert meta.session_id == "s1"

    def test_lock_released_after_exception(self):
        mgr = SessionManager()
        with pytest.raises(ValueError):
            with mgr.acquire("u1", "s1"):
                raise ValueError("boom")
        with mgr.acquire("u1", "s1") as meta:
            assert meta.session_id == "s1"

    def test_expired_session_recreated(self):
        mgr = SessionManager(idle_timeout=timedelta(seconds=0))
        with mgr.acquire("u1", "s1") as meta1:
            first_created = meta1.created_at
        with mgr.acquire("u1", "s1") as meta2:
            assert meta2.created_at >= first_created

    def test_get_meta_returns_none_for_unknown(self):
        mgr = SessionManager()
        assert mgr.get_meta("u1", "unknown") is None

    def test_get_meta_returns_existing(self):
        mgr = SessionManager()
        with mgr.acquire("u1", "s1"):
            pass
        meta = mgr.get_meta("u1", "s1")
        assert meta is not None
        assert meta.session_id == "s1"


class TestSessionMeta:
    def test_touch_updates_last_active(self):
        meta = SessionMeta(thread_id="u:s", cust_id="u", session_id="s")
        old_active = meta.last_active_at
        meta.touch()
        assert meta.last_active_at >= old_active

    def test_is_expired(self):
        meta = SessionMeta(thread_id="u:s", cust_id="u", session_id="s")
        assert not meta.is_expired(timedelta(hours=1))
        assert meta.is_expired(timedelta(seconds=0))
