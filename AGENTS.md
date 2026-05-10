# Repository Guidelines

## Project Structure & Module Organization

This is a Python 3.11+ package using a `src/` layout. The runtime is exclusively `harness_v2` — a thin enterprise shell over the deepagent SDK.

### Core package (`src/intent_router_harness/harness_v2/`)

| Module | Purpose |
| ------ | ------- |
| `agent.py` | `build_agent()` — zero-invasion deepagent `create_deep_agent()` wrapper |
| `api.py` | FastAPI app factory — `/api/v1/message`, `/api/v1/task/completion`, health |
| `config.py` | `HarnessConfig` — TOML spec loading |
| `errors.py` | Error hierarchy (`HarnessError`, `SessionBusyError`, `WorkflowExecutionError`, etc.) |
| `middleware.py` | 6 enterprise middleware: TaskProgress, CompletionGate, SkillLifecycle, SkillFile, WorkflowGateway, ProtocolOutput |
| `protocol.py` | `AssistantProtocolFrame`, `MessageRequest`, `TaskCompletionRequest`, `TraceEvent` |
| `session.py` | `SessionManager` — multi-user isolation + concurrency locks |
| `skill_registry.py` | `SkillRegistry` — filesystem-based skill index, metadata scan, on-demand body loading |
| `workflow.py` | `workflow_api_call` StructuredTool + SSE parsing |

### Supporting directories

- `tests/unit/`: Unit tests for harness_v2 modules (no network required).
- `tests/integration/`: Integration tests (ASGI endpoints, E2E flows).
- `docs/architecture/`: High-level design documents.
- `docs/development/`: Deployment and development guides.
- `examples/`: Sample harness specs, mock servers, E2E scripts.
- `skills/`: Sample business skills and references.

## Build, Test, and Development Commands

```bash
make install          # Install with test dependencies
make test             # Run full pytest suite
make lint             # Run ruff linter
make format           # Run ruff formatter
make serve            # Start ASGI server
make mock-workflow    # Start mock workflow server for E2E
make e2e              # Run E2E test with mock workflow
make clean            # Remove build artifacts
```

## Coding Style & Naming Conventions

Follow the existing Python style: 4-space indentation, type annotations for public interfaces, `from __future__ import annotations`, Pydantic models for request/response contracts, and small functions with explicit error types. Use `snake_case` for functions, variables, files, and test names; use `PascalCase` for classes and Pydantic models. Keep imports grouped as standard library, third-party, then local package imports.

Ruff is configured in `pyproject.toml` with line-length 120 and rules: `E`, `W`, `F`, `I`, `UP`, `B`, `SIM`, `TID`. Prefer inline `# noqa: RULE` for individual exceptions; reserve `per-file-ignores` for categorical policy (e.g., `tests/**`).

## Framework Boundary Rules

Framework code must stay business-agnostic. Do not hard-code business intent codes, slot names, slot aliases, sample utterances, prompt examples, follow-up wording, API semantics, keyword matching, or regex fallback in `src/intent_router_harness/`. Business behavior belongs in `skills/` and exposed `references/`; the runtime only scans skill metadata, loads the current task's skill content, validates protocol shape, advances task state, and releases context.

## Testing Guidelines

Use pytest. Place unit tests in `tests/unit/` and integration tests in `tests/integration/`. Filenames follow `test_harness_v2_<module>.py` convention. Prefer focused tests that exercise public behavior. For changes affecting stream behavior, cover both SSE and non-stream responses when practical.

## Commit & Pull Request Guidelines

Use Conventional Commits: `type(scope): description`. Pull requests should include a short description, affected modules or endpoints, test commands run, and any changes to examples or environment variables.

## Security & Configuration Tips

Copy `.env.example` to a local env file when needed and do not commit secrets. Keep LLM provider credentials, base URLs, and model settings outside source files. When adding skills or regression fixtures, avoid embedding private customer data.
