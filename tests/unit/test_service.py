from __future__ import annotations

from http.client import HTTPConnection
import json
from pathlib import Path
from threading import Thread

from intent_router_harness.server import create_server
from intent_router_harness.service import IntentRouterHarnessService


def _write_demo_harness(tmp_path: Path) -> Path:
    skills_root = tmp_path / "skills"
    skill_dir = skills_root / "transfer-routing"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "\n".join(
            [
                "---",
                "name: transfer-routing",
                "description: 转账路由规则",
                'intent_codes: ["transfer"]',
                "---",
                "# 转账路由规则",
                "",
                "将收款人、金额、账号和银行卡尾号视为槽位。",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    spec_path = tmp_path / "intent-router-harness.toml"
    spec_path.write_text(
        "\n".join(
            [
                'name = "finance-router-harness"',
                'version = "2026.04"',
                f'skill_roots = ["{skills_root.as_posix()}"]',
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return spec_path


def test_service_loads_harness_config(tmp_path: Path) -> None:
    service = IntentRouterHarnessService.from_spec(_write_demo_harness(tmp_path))

    health = service.health()
    skill = service.harness.skills.get("transfer-routing")

    assert health.name == "finance-router-harness"
    assert skill is not None
    assert skill.intent_codes == ("transfer",)
    assert "将收款人" in skill.body


def test_http_server_exposes_only_health_and_business_routes(tmp_path: Path) -> None:
    service = IntentRouterHarnessService.from_spec(_write_demo_harness(tmp_path))
    server = create_server(service, host="127.0.0.1", port=0)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    conn: HTTPConnection | None = None
    try:
        host, port = server.server_address
        conn = HTTPConnection(host, port, timeout=5)
        conn.request("GET", "/healthz")
        healthz_response = conn.getresponse()
        healthz_payload = json.loads(healthz_response.read().decode("utf-8"))

        conn.request("GET", "/readyz")
        readyz_response = conn.getresponse()
        readyz_payload = json.loads(readyz_response.read().decode("utf-8"))

        conn.request("GET", "/validator")
        validator_response = conn.getresponse()
        validator_body = validator_response.read().decode("utf-8")

        conn.request(
            "POST",
            "/render",
            body=json.dumps(
                {
                    "stream": False,
                    "variables": {
                        "message": "transfer 500 to Alice",
                    },
                    "intent_codes": ["transfer"],
                }
            ),
            headers={"Content-Type": "application/json"},
        )
        render_response = conn.getresponse()
        render_payload = json.loads(render_response.read().decode("utf-8"))

        conn.request("GET", "/regression/suite")
        regression_response = conn.getresponse()
        regression_payload = json.loads(regression_response.read().decode("utf-8"))
    finally:
        if conn is not None:
            conn.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert healthz_response.status == 200
    assert healthz_payload == {"status": "ok"}
    assert readyz_response.status == 200
    assert readyz_payload["ready"] is True
    assert validator_response.status == 200
    assert "Intent Router 验证台" in validator_body
    assert "/api/v1/message" in validator_body
    assert render_response.status == 404
    assert render_payload["error"]["code"] == "not_found"
    assert regression_response.status == 404
    assert regression_payload["error"]["code"] == "not_found"
