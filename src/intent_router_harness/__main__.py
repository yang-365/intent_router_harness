"""CLI entry point for intent_router_harness.

Usage::

    python -m intent_router_harness serve examples/deepagent-finance-router-harness.toml
    python -m intent_router_harness chat --url http://127.0.0.1:8765
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    """Run the harness CLI."""
    parser = argparse.ArgumentParser(prog="intent_router_harness")
    subparsers = parser.add_subparsers(dest="command", required=True)

    serve_parser = subparsers.add_parser("serve", help="Run the harness v2 ASGI service")
    serve_parser.add_argument("spec", type=Path, nargs="?", default=None)
    serve_parser.add_argument("--host", default="0.0.0.0")
    serve_parser.add_argument("--port", type=int, default=8765)
    serve_parser.add_argument("--reload", action="store_true")
    serve_parser.add_argument("--env-file", type=Path, default=None, help="Path to .env file (default: auto-detect .env in cwd)")

    chat_parser = subparsers.add_parser("chat", help="Interactive chat CLI for debugging conversations")
    chat_parser.add_argument("--url", default="http://127.0.0.1:8765", help="Harness base URL")
    chat_parser.add_argument("--session-id", default="cli_debug_001", help="Session ID")
    chat_parser.add_argument("--cust-id", default="C0001", help="Customer ID")
    chat_parser.add_argument("--mode", default="execute", help="Execution mode (execute/plan)")

    args = parser.parse_args(argv)
    if args.command == "chat":
        from intent_router_harness.chat_cli import run_chat

        run_chat(
            base_url=args.url,
            session_id=args.session_id,
            cust_id=args.cust_id,
            execution_mode=args.mode,
        )
        return 0

    if args.command == "serve":
        import os

        import uvicorn
        from dotenv import load_dotenv

        env_file = args.env_file or Path.cwd() / ".env"
        if env_file.exists():
            load_dotenv(env_file, override=True)

        if args.spec:
            os.environ["HARNESS_SPEC_PATH"] = str(args.spec)
        uvicorn.run(
            "intent_router_harness.harness_v2.api:create_app",
            factory=True,
            host=args.host,
            port=args.port,
            reload=args.reload,
        )
        return 0

    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
