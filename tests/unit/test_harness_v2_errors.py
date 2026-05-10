"""Tests for harness_v2.errors module."""

from __future__ import annotations

import pytest

from intent_router_harness.harness_v2.errors import (
    HarnessError,
    ProtocolOutputError,
    SessionBusyError,
    SessionExpiredError,
    WorkflowExecutionError,
    WorkflowUrlNotAllowedError,
)


class TestHarnessError:
    def test_base_error_has_code(self):
        err = HarnessError("boom", code="custom_code")
        assert str(err) == "boom"
        assert err.code == "custom_code"

    def test_default_code(self):
        err = HarnessError("oops")
        assert err.code == "harness_error"


class TestSessionBusyError:
    def test_code(self):
        err = SessionBusyError("session busy")
        assert err.code == "session_run_in_progress"
        assert isinstance(err, HarnessError)


class TestSessionExpiredError:
    def test_message(self):
        err = SessionExpiredError("sess-123")
        assert "sess-123" in str(err)
        assert err.code == "session_expired"


class TestWorkflowUrlNotAllowedError:
    def test_attributes(self):
        err = WorkflowUrlNotAllowedError("http://bad.com", ["http://good.com"])
        assert err.url == "http://bad.com"
        assert err.allowed == ["http://good.com"]
        assert err.code == "workflow_url_not_allowed"


class TestWorkflowExecutionError:
    def test_code(self):
        err = WorkflowExecutionError("timeout")
        assert err.code == "workflow_execution_error"


class TestProtocolOutputError:
    def test_code(self):
        err = ProtocolOutputError("bad output")
        assert err.code == "protocol_output_error"
