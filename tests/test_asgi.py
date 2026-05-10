from __future__ import annotations

import asyncio
from pathlib import Path

import httpx

from intent_router_harness.asgi import create_app
from intent_router_harness.config import AppSettings
from intent_router_harness.contracts import (
    PlannedTask,
    PlannerOutput,
    RecognitionPlan,
    RouterMessageRequest,
    TaskRuntimeState,
)
from intent_router_harness.service import IntentRouterHarnessService


class StaticPlanner:
    def __init__(self, output: PlannerOutput) -> None:
        self.output = output

    def plan_message(
        self,
        request: RouterMessageRequest,
        task_state: TaskRuntimeState,
    ) -> PlannerOutput:
        return self.output


def _write_minimal_harness(tmp_path: Path) -> Path:
    spec_path = tmp_path / "harness.toml"
    spec_path.write_text(
        "\n".join(
            [
                'name = "asgi-test"',
                'version = "2026.04"',
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return spec_path


def test_asgi_health_ready_and_aux_routes_are_not_exposed(tmp_path: Path) -> None:
    settings = AppSettings(
        spec_path=_write_minimal_harness(tmp_path),
        regression_suite_path=None,
        llm_env_file=None,
    )
    app = create_app(settings)

    async def run() -> tuple[httpx.Response, httpx.Response, httpx.Response]:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            healthz = await client.get("/healthz")
            readyz = await client.get("/readyz")
            render = await client.post(
                "/render",
                json={
                    "variables": {"message": "hello"},
                },
            )
            return healthz, readyz, render

    healthz, readyz, render = asyncio.run(run())

    assert healthz.status_code == 200
    assert healthz.json() == {"status": "ok"}
    assert readyz.status_code == 200
    assert readyz.json()["llm_configured"] is False
    assert render.status_code == 404


def test_asgi_serves_streaming_validator_ui(tmp_path: Path) -> None:
    settings = AppSettings(
        spec_path=_write_minimal_harness(tmp_path),
        regression_suite_path=None,
        llm_env_file=None,
    )
    app = create_app(settings)

    async def run() -> tuple[httpx.Response, httpx.Response]:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            root = await client.get("/")
            validator = await client.get("/validator")
            return root, validator

    root, validator = asyncio.run(run())

    assert root.status_code == 200
    assert root.headers["content-type"].startswith("text/html")
    assert "Intent Router 验证台" in root.text
    assert "ReadableStream" in root.text
    assert "/api/v1/task/completion" in root.text
    assert validator.status_code == 200


def test_asgi_message_stream_uses_assistant_protocol_service(tmp_path: Path) -> None:
    task = PlannedTask(
        taskId="task_transfer",
        intent_code="AG_TRANS",
        status="ready_for_dispatch",
        slot_memory={"payee_name": "小明", "amount": "200"},
        output={"ishandover": True, "handOverReason": "router_only_ready_for_dispatch"},
    )
    service = IntentRouterHarnessService.from_spec(
        _write_minimal_harness(tmp_path),
        message_planner=StaticPlanner(
            PlannerOutput(
                mode="single_task",
                status="ready_for_dispatch",
                completion_state=0,
                completion_reason="router_ready_for_dispatch",
                intent_code="AG_TRANS",
                recognition=RecognitionPlan(intent_code="AG_TRANS"),
                slot_memory={"payee_name": "小明", "amount": "200"},
                task_list=[task],
                current_task=task,
                output={"ishandover": True, "handOverReason": "router_only_ready_for_dispatch"},
            )
        ),
    )
    app = create_app(
        AppSettings(spec_path=_write_minimal_harness(tmp_path), regression_suite_path=None),
        service=service,
    )

    async def run() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            return await client.post(
                "/api/v1/message",
                json={
                    "sessionId": "asgi_session_001",
                    "txt": "给小明转账200",
                    "custID": "C0001",
                    "stream": True,
                    "executionMode": "router_only",
                },
            )

    response = asyncio.run(run())

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.text.count("event: message") == 2
    assert "event: done" in response.text
    assert "router_ready_for_dispatch" in response.text


# ---------------------------------------------------------------------------
# Priority 6: ASGI DeepAgent streaming consistency tests
# ---------------------------------------------------------------------------


def _write_deepagent_harness_for_asgi(tmp_path: Path) -> Path:
    skills_root = tmp_path / "skills"
    skill_dir = skills_root / "transfer-routing"
    references_dir = skill_dir / "references"
    references_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "\n".join(
            [
                "---",
                "name: transfer-routing",
                "description: 转账路由规则",
                'intent_codes: ["AG_TRANS"]',
                'required_slots: ["payee_name", "amount"]',
                'references: [{"id":"slot_filling","path":"references/slot_filling.md","purpose":"转账提槽规则"}]',
                "---",
                "# 转账路由",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    (references_dir / "slot_filling.md").write_text("# 转账提槽规则\n", encoding="utf-8")
    spec_path = tmp_path / "deepagent-harness.toml"
    spec_path.write_text(
        "\n".join(
            [
                'name = "asgi-deepagent-test"',
                'version = "2026.05"',
                'agent_runtime = "deepagent"',
                f'skill_roots = ["{skills_root.as_posix()}"]',
                "[deepagent]",
                'model = "fake:model"',
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return spec_path


class FakeDeepAgentRunnerForASGI:
    def run_message(self, context):
        from intent_router_harness.contracts import AssistantProtocolFrame, TaskRuntimeState
        from intent_router_harness.deepagent_service import DeepAgentRunResult, AssistantTraceEvent

        return DeepAgentRunResult(
            frames=(
                AssistantProtocolFrame(
                    ok=True,
                    status="completed",
                    completion_state=2,
                    completion_reason="deepagent_done",
                    output={"thread_id": context.thread_id},
                ),
            ),
            trace_events=(
                AssistantTraceEvent(
                    stage="deepagent_run_completed",
                    title="DeepAgent运行完成",
                    summary="test",
                ),
            ),
            task_state=TaskRuntimeState(),
        )


def test_asgi_deepagent_stream_with_trace_returns_sse_events(tmp_path: Path) -> None:
    spec_path = _write_deepagent_harness_for_asgi(tmp_path)
    settings = AppSettings(
        spec_path=spec_path,
        regression_suite_path=None,
        llm_env_file=None,
    )
    service = IntentRouterHarnessService.from_spec(
        spec_path,
        deepagent_runner=FakeDeepAgentRunnerForASGI(),
    )
    app = create_app(settings, service=service)

    async def run() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            return await client.post(
                "/api/v1/message",
                json={
                    "sessionId": "asgi_da_001",
                    "txt": "给陈广荣转500元",
                    "custID": "C0001",
                    "stream": True,
                    "debugTrace": True,
                    "executionMode": "execute",
                },
            )

    response = asyncio.run(run())

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert "event: trace" in response.text
    assert "event: message" in response.text
    assert "event: done" in response.text
    assert "deepagent_done" in response.text


def test_asgi_deepagent_non_stream_returns_json(tmp_path: Path) -> None:
    spec_path = _write_deepagent_harness_for_asgi(tmp_path)
    settings = AppSettings(
        spec_path=spec_path,
        regression_suite_path=None,
        llm_env_file=None,
    )
    service = IntentRouterHarnessService.from_spec(
        spec_path,
        deepagent_runner=FakeDeepAgentRunnerForASGI(),
    )
    app = create_app(settings, service=service)

    async def run() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            return await client.post(
                "/api/v1/message",
                json={
                    "sessionId": "asgi_da_002",
                    "txt": "你好",
                    "custID": "C0001",
                    "stream": False,
                    "debugTrace": False,
                },
            )

    response = asyncio.run(run())

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "completed"
    assert data["completion_reason"] == "deepagent_done"


def test_asgi_deepagent_stream_without_trace_has_no_trace_events(tmp_path: Path) -> None:
    spec_path = _write_deepagent_harness_for_asgi(tmp_path)
    settings = AppSettings(
        spec_path=spec_path,
        regression_suite_path=None,
        llm_env_file=None,
    )
    service = IntentRouterHarnessService.from_spec(
        spec_path,
        deepagent_runner=FakeDeepAgentRunnerForASGI(),
    )
    app = create_app(settings, service=service)

    async def run() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            return await client.post(
                "/api/v1/message",
                json={
                    "sessionId": "asgi_da_003",
                    "txt": "你好",
                    "custID": "C0001",
                    "stream": True,
                    "debugTrace": False,
                },
            )

    response = asyncio.run(run())

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert "event: trace" not in response.text
    assert "event: message" in response.text
    assert "event: done" in response.text


def test_asgi_deepagent_concurrent_sessions_return_success(tmp_path: Path) -> None:
    spec_path = _write_deepagent_harness_for_asgi(tmp_path)
    settings = AppSettings(
        spec_path=spec_path,
        regression_suite_path=None,
        llm_env_file=None,
    )
    service = IntentRouterHarnessService.from_spec(
        spec_path,
        deepagent_runner=FakeDeepAgentRunnerForASGI(),
    )
    app = create_app(settings, service=service)

    async def run() -> tuple[httpx.Response, httpx.Response]:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            r1, r2 = await asyncio.gather(
                client.post(
                    "/api/v1/message",
                    json={
                        "sessionId": "concA",
                        "txt": "hi",
                        "custID": "C0001",
                        "stream": False,
                    },
                ),
                client.post(
                    "/api/v1/message",
                    json={
                        "sessionId": "concB",
                        "txt": "hi",
                        "custID": "C0002",
                        "stream": False,
                    },
                ),
            )
            return r1, r2

    r1, r2 = asyncio.run(run())

    assert r1.status_code == 200
    assert r2.status_code == 200
    assert r1.json()["status"] == "completed"
    assert r2.json()["status"] == "completed"
