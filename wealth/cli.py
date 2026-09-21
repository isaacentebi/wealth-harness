"""JSON CLI: wealth <operation> --input request.json (or stdin)."""
from __future__ import annotations

import argparse
import json
import math
import sqlite3
import sys
import time
from pathlib import Path

from .cli_text import COMMANDS as TEXT_COMMANDS
from .cli_text import main as text_main
from .service import OPERATIONS, WealthService, dispatch


def _reject_constant(value: str):
    raise ValueError(f"non-finite JSON value: {value}")


def _destructive(operation: str, payload: object) -> bool:
    return operation == "forget" or (
        operation == "client" and isinstance(payload, dict) and payload.get("action") == "forget")


def _confirm_forget(payload: dict) -> None:
    """Require a person at a terminal to type the client id and DELETE.

    Refuses whenever stdin is not an interactive terminal, so an agent or a
    script with shell access cannot delete a profile non-interactively.
    """

    if not sys.stdin.isatty():
        raise PermissionError("forget requires an interactive terminal; it cannot be run from a script or agent")
    client_id = payload.get("client_id")
    if not isinstance(client_id, str) or not client_id:
        raise ValueError("forget requires client_id")
    expected = f"{client_id} DELETE"
    print(f"This permanently deletes client {client_id!r} and all of its memory.\n"
          f"Type '{expected}' to confirm: ", end="", file=sys.stderr, flush=True)
    try:
        answer = input()
    except EOFError:
        answer = ""
    if answer.strip() != expected:
        raise PermissionError("forget was not confirmed; nothing was deleted")


def _command(argv: list[str]) -> str | None:
    """The first positional argument, skipping the value of a leading --db."""
    skip = False
    for arg in argv:
        if skip:
            skip = False
        elif arg == "--db":
            skip = True
        elif not arg.startswith("-"):
            return arg
    return None


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)
    if _command(argv) in TEXT_COMMANDS:  # text-channel helpers, see docs/openclaw.md
        return text_main(argv)
    parser = argparse.ArgumentParser(description="Wealth JSON CLI: one JSON object in (stdin or --input), JSON out")
    parser.add_argument("operation", choices=(*OPERATIONS, "watch"),
                        help=" | ".join((*OPERATIONS, "watch")) + " "
                             "(text channels: onboarding | today | view, see docs/openclaw.md)")
    parser.add_argument("--db", help="SQLite path (default WEALTH_DB or user data directory)")
    parser.add_argument("--input", default="-", help="JSON argument file; '-' reads stdin")
    parser.add_argument("--client", help="explicit client identifier for wealth watch or forget")
    parser.add_argument("--interval", type=float, default=300,
                        help="foreground watch interval in seconds (default: 300)")
    parser.add_argument("--once", action="store_true",
                        help="evaluate monitoring once and exit")
    args = parser.parse_args(argv)
    try:
        if args.operation == "watch":
            if not args.client:
                raise ValueError("watch requires --client")
            if not math.isfinite(args.interval) or args.interval <= 0:
                raise ValueError("watch --interval must be positive")
            service = WealthService(args.db)
            try:
                while True:
                    result = service.run("monitor", client_id=args.client)
                    for event in result.get("result", {}).get("events", []):
                        print(json.dumps(event, ensure_ascii=False, allow_nan=False), flush=True)
                    if args.once:
                        break
                    time.sleep(args.interval)
            except KeyboardInterrupt:
                pass
            return 0
        if args.operation == "forget" and not sys.stdin.isatty():
            _confirm_forget({})  # refuses before reading any piped payload
        if args.operation == "forget" and args.input == "-":
            if not args.client:
                raise ValueError("forget requires --client (or --input FILE)")
            payload = {"client_id": args.client, "confirm_client_id": args.client}
        elif args.input == "-" and args.operation == "context" and sys.stdin.isatty():
            payload = {}
        else:
            raw = sys.stdin.read() if args.input == "-" else Path(args.input).read_text(encoding="utf-8")
            payload = {} if args.operation == "context" and not raw.strip() else json.loads(raw, parse_constant=_reject_constant)
        if _destructive(args.operation, payload):
            _confirm_forget(payload if args.operation == "forget" else {"client_id": payload.get("client_id")})
        result = dispatch(args.operation, payload, args.db)
        print(json.dumps(result, ensure_ascii=False, allow_nan=False, indent=2))
        return 0
    except (ValueError, TypeError, KeyError, OSError, sqlite3.Error) as exc:
        print(json.dumps({"error": str(exc), "error_type": type(exc).__name__}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
