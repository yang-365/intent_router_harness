# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [2.0.0] - 2026-05-10

### Added

- **SkillRegistry** — filesystem-based skill index with metadata-only startup scan, on-demand body loading, and virtual file resolution.
- **SkillLifecycleMiddleware** — progressive skill body loading on intent detection, unloading on task transition. Name/description injection handled by deepagent SDK natively.
- **SkillFileMiddleware** — intercept `read_file` tool calls for virtual `/skills/` paths.
- Six enterprise middleware classes: TaskProgress, CompletionGate, SkillLifecycle, SkillFile, WorkflowGateway, ProtocolOutput.
- Multi-user session isolation with concurrency locks (`SessionManager`).
- `/api/v1/message` and `/api/v1/task/completion` protocol-compatible endpoints.
- Workflow tool (`workflow_api_call`) via deepagent `StructuredTool`.
- TOML spec-driven configuration (`HarnessConfig`).

### Removed

- Classic runtime modules: `assistant_service.py`, `planner.py`, `llm.py`, `skills.py`, `service_factory.py`, `tool_runtime.py`, `deepagent/` subpackage, `serving/` subpackage.
- v1 session store, contracts, regression framework, workflow hooks, debug UI.
- All v1-only documentation and test files.

## [0.1.0] - 2026-05-10

### Added

- Initial release with classic runtime and DeepAgent experimental runtime.
