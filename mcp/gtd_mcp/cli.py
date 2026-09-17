"""`gtd-mcp serve` (streamable-HTTP) and `gtd-mcp stdio`."""
from __future__ import annotations

import argparse
import os

from .config import load_config
from .server import build_app, run_stdio


def _apply_overrides(args: argparse.Namespace) -> None:
    if args.vault:
        os.environ["GTD_VAULT_DIR"] = args.vault
    if args.today:
        os.environ["GTD_MCP_FIXED_TODAY"] = args.today


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="gtd-mcp")
    sub = parser.add_subparsers(dest="command", required=True)

    def add_common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--vault", help="Override GTD_VAULT_DIR (a local clone or scratch directory)")
        p.add_argument("--today", help="Fix 'today' for every call (YYYY-MM-DD), for tests")

    serve_p = sub.add_parser("serve", help="Run the streamable-HTTP server plus /health and /hooks/gitlab")
    add_common(serve_p)
    serve_p.add_argument("--host")
    serve_p.add_argument("--port", type=int)

    stdio_p = sub.add_parser("stdio", help="Run over stdio, e.g. from a local Claude Code session")
    add_common(stdio_p)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    _apply_overrides(args)

    config = load_config()
    if args.command == "serve":
        import uvicorn

        uvicorn.run(build_app(config), host=args.host or config.host, port=args.port or config.port)
    else:
        run_stdio(config)
    return 0
