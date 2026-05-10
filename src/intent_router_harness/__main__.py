"""CLI entry point for intent_router_harness.

Phase 2: classic serve and llm-smoke commands removed.
Use ``serve-v2`` for the deepagent-backed harness runtime.
"""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

from intent_router_harness.regression import load_regression_suite


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="intent_router_harness")
    subparsers = parser.add_subparsers(dest="command", required=True)

    suite_parser = subparsers.add_parser("show-suite", help="Load and summarize a regression suite")
    suite_parser.add_argument("suite", type=Path)

    v2_parser = subparsers.add_parser("serve-v2", help="Run the harness v2 ASGI service")
    v2_parser.add_argument("spec", type=Path, nargs="?", default=None)
    v2_parser.add_argument("--host", default="0.0.0.0")
    v2_parser.add_argument("--port", type=int, default=8765)
    v2_parser.add_argument("--reload", action="store_true")

    args = parser.parse_args(argv)
    if args.command == "show-suite":
        suite = load_regression_suite(args.suite)
        print(f"{suite.version}: {len(suite.cases)} cases from {suite.source_document}")
        print(" ".join(sorted(suite.case_ids())))
        return 0

    if args.command == "serve-v2":
        import uvicorn

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
