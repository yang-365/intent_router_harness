# intent_router_harness

A standalone assistant-protocol router for intent recognition, serial business
task queues, skill-constrained slot filling, and task completion callbacks.

The project owns its own specs, skills, regression data, tests, and local
service — it does not import or patch any production router.

## Key Features

- **Dual runtime**: Classic two-phase LLM pipeline or DeepAgent (LangGraph)
  harness runtime, selectable per spec.
- **Assistant Protocol**: Structured frame-based protocol with SSE streaming
  support, trace events, and task-level state management.
- **Workflow tool integration**: HTTP workflow API calls with URL whitelist
  validation, SSE parsing, before/after lifecycle hooks, and comprehensive
  error classification.
- **Multi-task planning**: Serial task queue with `task_list` / `current_task`
  tracking, slot memory isolation per task.
- **Regression suite**: JSON-driven test fixtures for assistant protocol
  validation with transcript-level assertion.

## Project Layout

```
intent_router_harness/
├── src/intent_router_harness/    # Core package
│   ├── deepagent/                # DeepAgent runtime subpackage
│   │   ├── errors.py             #   Error hierarchy
│   │   ├── helpers.py            #   Shared helpers & protocol builders
│   │   ├── middleware.py          #   LangGraph middleware controls
│   │   ├── runner.py             #   NativeDeepAgentRunner
│   │   └── service.py            #   DeepAgentAssistantProtocolService
│   ├── serving/                  # HTTP serving layer
│   │   ├── asgi.py               #   FastAPI ASGI application
│   │   ├── server.py             #   Stdlib HTTP server
│   │   └── debug_ui.py           #   Browser validation UI
│   ├── contracts.py              # Pydantic request/response models
│   ├── runtime.py                # PromptHarness & spec loading
│   ├── service.py                # IntentRouterHarnessService
│   ├── session_store.py          # In-memory session management
│   ├── skills.py                 # Skill document loading
│   ├── workflow.py               # Workflow SSE parsing & HTTP client
│   └── _version.py               # Single source of truth for version
├── tests/
│   ├── unit/                     # Unit tests (no network)
│   └── integration/              # Integration tests (ASGI, E2E)
├── docs/
│   ├── architecture/             # High-level design & protocol docs
│   ├── development/              # Developer setup, deployment, E2E testing
│   └── product/                  # Product requirements & regression specs
├── examples/                     # Sample specs, mock servers, clients
├── skills/                       # Sample business skills & references
├── regressions/                  # Structured regression suites
├── hooks/                        # Workflow lifecycle hooks
├── tools/                        # Command-backed runtime tools
├── k8s/                          # Kubernetes manifests
├── Makefile                      # Standard development targets
├── pyproject.toml                # Build config, dependencies, ruff/pytest
└── .env.example                  # Environment variable template
```

## Quick Start

```bash
# Install with test dependencies
pip install -e '.[test]'

# Run full test suite
make test
# or: python -m pytest -q

# Start the HTTP service
make serve SPEC=examples/finance-router-harness.toml

# Start the ASGI service (FastAPI + uvicorn)
make serve-asgi
```

## Development Commands

| Command            | Description                                       |
| ------------------ | ------------------------------------------------- |
| `make install`     | Install package with test dependencies             |
| `make test`        | Run pytest suite                                   |
| `make lint`        | Run ruff linter                                    |
| `make format`      | Run ruff formatter                                 |
| `make serve`       | Start stdlib HTTP server                           |
| `make serve-asgi`  | Start FastAPI ASGI application                     |
| `make mock-workflow`| Start mock workflow server for E2E testing        |
| `make e2e`         | Run end-to-end test with mock workflow             |
| `make show-suite`  | Display regression suite summary                   |
| `make clean`       | Remove build artifacts                             |

## HTTP API

| Method | Path                       | Description                          |
| ------ | -------------------------- | ------------------------------------ |
| GET    | `/healthz`                 | Liveness check                       |
| GET    | `/readyz`                  | Readiness check with LLM status      |
| GET    | `/` or `/validator`        | Browser validation UI                |
| POST   | `/api/v1/message`          | Assistant protocol message entrypoint|
| POST   | `/api/v1/task/completion`  | Task completion callback             |

```bash
curl -s http://127.0.0.1:8765/api/v1/message \
  -H 'Content-Type: application/json' \
  -d '{
    "sessionId": "demo_001",
    "custID": "C0001",
    "txt": "给小明转账200元",
    "stream": true,
    "executionMode": "router_only"
  }'
```

## Configuration

Copy `.env.example` to `.env` and fill in provider credentials:

```bash
cp .env.example .env
# Edit .env with your LLM provider settings
```

See `examples/` for sample harness spec files.

## License

See [LICENSE](LICENSE) for details.
