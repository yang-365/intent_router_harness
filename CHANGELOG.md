# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0] - 2026-05-10

### Added

- DeepAgent harness runtime with `agent_runtime = "deepagent"` configuration.
- `/api/v1/message` assistant protocol with SSE streaming (trace, message, done events).
- In-memory session store with user binding, idle timeout, and ownership enforcement.
- Non-reentrant per-session run lock for DeepAgent runtime.
- Workflow tool calling via `workflow_api_call` LangChain tool (method/url/body, no headers to model).
- Workflow URL whitelist enforcement (`[workflow].allowed_urls`).
- Workflow error classification: `WorkflowUrlNotAllowedError`, `WorkflowTimeoutError`, `WorkflowHttpError`, `WorkflowSseParseError`, `WorkflowNoNodeOutputError`.
- Workflow hook lifecycle: before/after events with error isolation.
- DeepAgent native skill file loading with `/skills/` virtual path mapping.
- Multi-intent task planning with `write_todos` and `task_list`/`current_task` protocol fields.
- Protocol compatibility matrix documentation (classic vs deepagent runtime).
- Mock workflow server and E2E validation script.
- Browser-based validator UI at `/validator`.
- Kubernetes deployment manifest.
- Concurrency tests, context isolation tests, and ASGI streaming consistency tests.

### Classic Runtime

- Spec-driven intent recognition and multi-intent splitting.
- Skill progressive disclosure with slot filling.
- Assistant protocol regression validation suite.
