from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from intent_router_harness.contracts import TaskRuntimeState
from intent_router_harness.session_store import InMemorySessionStore, SessionOwnershipError


def test_in_memory_session_store_preserves_task_state_for_session() -> None:
    store = InMemorySessionStore()
    store.load("s1", user_binding_id="C0001")
    store.save_task_state("s1", TaskRuntimeState(slot_memory={"amount": 500}))

    loaded = store.load_existing("s1", user_binding_id="C0001")

    assert loaded.session.session_id == "s1"
    assert loaded.session.user_binding_id == "C0001"
    assert loaded.task_state.slot_memory == {"amount": 500}


def test_in_memory_session_store_rejects_cross_user_reuse() -> None:
    store = InMemorySessionStore()
    store.load("s1", user_binding_id="C0001")

    with pytest.raises(SessionOwnershipError):
        store.load("s1", user_binding_id="C0002")


def test_in_memory_session_store_expires_idle_session() -> None:
    now = [0]

    def clock() -> datetime:
        return datetime.fromtimestamp(now[0], timezone.utc)

    store = InMemorySessionStore(idle_timeout=timedelta(seconds=1), clock=clock)
    store.load("s1", user_binding_id="C0001")
    store.save_task_state("s1", TaskRuntimeState(slot_memory={"amount": 500}))

    now[0] = 2
    loaded = store.load("s1", user_binding_id="C0001")

    assert loaded.expired is True
    assert loaded.created is True
    assert loaded.task_state.slot_memory == {}
