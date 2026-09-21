"""JSON CLI: wealth <operation> --input request.json (or stdin)."""
from __future__ import annotations

import argparse
import json
import math
import sqlite3
import sys
import time
from pathlib import Path

from .service import OPERATIONS, WealthService, dispatch


def _reject_constant(value: str):
    raise ValueError(f"non-finite JSON value: {value}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Client memory and evidence-backed wealth workflows")
    parser.add_argument("operation", choices=(*OPERATIONS, "watch"))
    parser.add_argument("--db", help="SQLite path (default WEALTH_DB or user data directory)")
    parser.add_argument("--input", default="-", help="JSON argument file; '-' reads stdin")
    parser.add_argument("--client", help="explicit client identifier for wealth watch")
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
        if args.input == "-" and (args.operation == "capabilities" or
                                   (args.operation == "context" and sys.stdin.isatty())):
            payload = {}
        else:
            raw = sys.stdin.read() if args.input == "-" else Path(args.input).read_text(encoding="utf-8")
            payload = {} if args.operation == "context" and not raw.strip() else json.loads(raw, parse_constant=_reject_constant)
        result = dispatch(args.operation, payload, args.db)
        print(json.dumps(result, ensure_ascii=False, allow_nan=False, indent=2))
        return 0
    except (ValueError, TypeError, KeyError, OSError, sqlite3.Error) as exc:
        print(json.dumps({"error": str(exc), "error_type": type(exc).__name__}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
