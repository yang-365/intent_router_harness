# Repository Guidelines

## Project Structure & Module Organization

This is a Python 3.11+ package using a `src/` layout.

### Core package (`src/intent_router_harness/`)

| Module / Subpackage | Purpose |
| ------------------- | ------- |
| `deepagent/`        | DeepAgent harness runtime — error hierarchy (`errors.py`), LangGraph middleware (`middleware.py`), `NativeDeepAgentRunner` (`runner.py`), `DeepAgentAssistantProtocolService` (`service.py`), shared helpers (`helpers.py`) |
| `serving/`          | HTTP serving layer — FastAPI ASGI app (`asgi.py`), stdlib HTTP server (`server.py`), browser validation UI (`debug_ui.py`) |
| `contracts.py`      | Pydantic request/response models for the assistant protocol |
| `runtime.py`        | `PromptHarness`, spec loading, skill binding |
| `service.py`        | `IntentRouterHarnessService` — top-level service orchestration |
| `session_store.py`  | In-memory session management |
| `skills.py`         | Skill document loading & library |
| `workflow.py`       | Workflow SSE parsing, HTTP client, tool specs |
| `llm.py`            | LLM client configuration |
| `_version.py`       | Single source of truth for package version |

Backward-compatible shim modules (`asgi.py`, `server.py`, `debug_ui.py`, `deepagent_service.py`) re-export from the new subpackages so existing import paths continue to work.

### Supporting directories

- `tests/unit/`: Unit tests (no network required).
- `tests/integration/`: Integration tests (ASGI endpoints, E2E flows).
- `docs/architecture/`: High-level design, protocol compatibility, runtime design.
- `docs/development/`: Deployment, E2E testing, workflow tool calling, handoff notes.
- `docs/product/`: Product requirements, regression specs, user-facing docs.
- `examples/`: Sample harness specs, mock servers, local clients.
- `skills/`: Sample business skills and references.
- `regressions/`: Structured regression suites (JSON fixtures).
- `hooks/`: Workflow tool lifecycle hooks.
- `tools/`: Command-backed runtime tools.
- `k8s/`: Kubernetes deployment manifests.

## Build, Test, and Development Commands

```bash
make install          # Install with test dependencies
make test             # Run full pytest suite
make lint             # Run ruff linter
make format           # Run ruff formatter
make serve            # Start stdlib HTTP server
make serve-asgi       # Start FastAPI ASGI application
make mock-workflow    # Start mock workflow server for E2E
make e2e              # Run E2E test with mock workflow
make show-suite       # Display regression suite summary
make clean            # Remove build artifacts
```

After installation, the console script `intent-router-harness` replaces `PYTHONPATH=src python -m intent_router_harness`.

## Coding Style & Naming Conventions

Follow the existing Python style: 4-space indentation, type annotations for public interfaces, `from __future__ import annotations`, Pydantic models for request/response contracts, and small functions with explicit error types. Use `snake_case` for functions, variables, files, and test names; use `PascalCase` for classes and Pydantic models. Keep imports grouped as standard library, third-party, then local package imports.

Ruff is configured in `pyproject.toml` with line-length 120 and rules: `E`, `W`, `F`, `I`, `UP`, `B`, `SIM`, `TID`. Prefer inline `# noqa: RULE` for individual exceptions; reserve `per-file-ignores` for categorical policy (e.g., `tests/**`).

## Framework Boundary Rules

Framework code must stay business-agnostic. Do not hard-code business intent codes, slot names, slot aliases, sample utterances, prompt examples, follow-up wording, API semantics, keyword matching, or regex fallback in `src/intent_router_harness/`. Business behavior belongs in `skills/` and exposed `references/`; the runtime only scans skill metadata, loads the current task's skill content, validates protocol shape, advances task state, and releases context.

## Testing Guidelines

Use pytest. Place unit tests in `tests/unit/` and integration tests in `tests/integration/`. Use filenames like `test_service.py` and functions like `test_service_renders_prompt_response`. Prefer focused tests that exercise public behavior: prompt rendering, HTTP/ASGI endpoints, assistant protocol parsing, and regression validation. For changes affecting stream behavior, cover both SSE and non-stream responses when practical.

## Commit & Pull Request Guidelines

Use concise imperative commit messages such as `Add assistant protocol regression validation`. Pull requests should include a short description, affected modules or endpoints, test commands run, and any changes to examples, regression fixtures, environment variables, or deployment manifests.

## Security & Configuration Tips

Copy `.env.example` to a local env file when needed and do not commit secrets. Keep LLM provider credentials, base URLs, and model settings outside source files. When adding skills or regression fixtures, avoid embedding private customer data.
