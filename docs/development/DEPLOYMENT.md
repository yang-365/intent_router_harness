# Deployment

## Local

```bash
python -m intent_router_harness serve examples/deepagent-finance-router-harness.toml --port 8765
```

Health check:

```bash
curl -s http://127.0.0.1:8765/healthz
```

## Configuration

Environment variables (see `.env.example`):

```bash
ROUTER_LLM_API_BASE_URL=...
ROUTER_LLM_API_KEY=...
ROUTER_LLM_MODEL=...
```

## Kubernetes

See `k8s/intent-router-harness.yaml` and the full guide in
[OPERATIONS_MANUAL.md](OPERATIONS_MANUAL.md#9-kubernetes-部署).

## Current Limits

- Session storage is in-memory; use one replica or replace with PostgresSaver for multi-instance.
- Authentication, authorization, rate limiting, and persistent audit logging are not yet implemented.
