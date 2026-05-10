# intent_router_harness

A thin enterprise shell over the deepagent SDK — multi-user session isolation,
progressive skill lifecycle, and protocol-compatible assistant API.

## Key Features

- **Zero-invasion deepagent wrapper**: `create_deep_agent()` used as-is; all
  enterprise logic lives in 6 middleware classes.
- **Progressive skill lifecycle**: Metadata-only scan at startup, body loaded
  on-demand per intent, unloaded after task completion.
- **Multi-user session isolation**: Concurrency locks per session, idle timeout,
  user-binding enforcement.
- **Assistant Protocol**: Structured frame-based protocol with SSE streaming,
  trace events, and task-level state management.
- **Workflow tool integration**: HTTP workflow API calls with URL whitelist
  validation, SSE parsing, and error classification.

## Project Layout

```
intent_router_harness/
├── src/intent_router_harness/
│   ├── harness_v2/               # Core runtime
│   │   ├── agent.py              #   build_agent() via create_deep_agent
│   │   ├── api.py                #   FastAPI app factory
│   │   ├── config.py             #   TOML spec loading
│   │   ├── errors.py             #   Error hierarchy
│   │   ├── middleware.py          #   6 enterprise middleware classes
│   │   ├── protocol.py           #   AssistantProtocolFrame, requests
│   │   ├── session.py            #   SessionManager (isolation + locks)
│   │   ├── skill_registry.py     #   Filesystem-based skill index
│   │   └── workflow.py           #   workflow_api_call tool + SSE
│   ├── __init__.py               # Re-exports from harness_v2
│   └── __main__.py               # CLI entry point
├── tests/
│   ├── unit/                     # Unit tests (no network)
│   └── integration/              # Integration tests (ASGI, E2E)
├── docs/
│   ├── architecture/             # Design docs
│   └── development/              # Deployment guide
├── examples/                     # Sample specs, mock servers
├── skills/                       # Sample business skills & references
├── k8s/                          # Kubernetes manifests
├── Makefile
├── pyproject.toml
└── .env.example
```

## Quick Start

```bash
# Install with test dependencies
pip install -e '.[test]'

# Run full test suite
make test

# Start the ASGI service
make serve
```

## Development Commands

| Command            | Description                                       |
| ------------------ | ------------------------------------------------- |
| `make install`     | Install package with test dependencies             |
| `make test`        | Run pytest suite                                   |
| `make lint`        | Run ruff linter                                    |
| `make format`      | Run ruff formatter                                 |
| `make serve`       | Start ASGI server                                  |
| `make mock-workflow`| Start mock workflow server for E2E testing        |
| `make e2e`         | Run end-to-end test with mock workflow             |
| `make clean`       | Remove build artifacts                             |

## HTTP API

| Method | Path                       | Description                          |
| ------ | -------------------------- | ------------------------------------ |
| GET    | `/healthz`                 | Liveness check                       |
| GET    | `/`                        | Service info page                    |
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

## Operations & Testing

See [docs/development/OPERATIONS_MANUAL.md](docs/development/OPERATIONS_MANUAL.md) for the full
operations manual covering configuration, startup, mock services, business scenario testing,
E2E automation, and Kubernetes deployment.

## License

See [LICENSE](LICENSE) for details.
