"""JSON CLI: wealth <operation> --input request.json (or stdin)."""
from __future__ import annotations

import argparse
import json
import math
import os
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


def _saves_on_consent(operation: str, payload: object) -> str | None:
    """What an operation that needs the person's yes would do, or None."""
    action = payload.get("action") if isinstance(payload, dict) else None
    if operation == "ingest" and action in {"confirm", "confirm_duplicates"}:
        return "save this statement or proposal"
    if operation == "decision" and action == "accept":
        return "accept this decision"
    if operation == "resolve_contradiction":
        return "save this answer to the contradiction"
    return None


def _confirm_save(what: str) -> None:
    """Require a person at a terminal (or ``--yes``, passed after the person agreed) to save.

    Piped stdin alone is never consent: a script or agent could send it.
    """

    if not sys.stdin.isatty():
        raise PermissionError(f"this would {what}; it needs an interactive terminal, or --yes once the person "
                              "has agreed to exactly this")
    print(f"This will {what}. Type 'yes' to confirm: ", end="", file=sys.stderr, flush=True)
    try:
        with open("/dev/tty", encoding="utf-8") as terminal:  # stdin may already be at EOF after the payload
            answer = terminal.readline()
    except OSError:
        try:
            answer = input()
        except EOFError:
            answer = ""
    if answer.strip().lower() not in {"yes", "y", "si", "sí"}:
        raise PermissionError("not confirmed; nothing was saved")


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


def _tax_pack(argv: list[str]) -> int:
    """``wealth tax-pack``: run the tax_pack task and write JSON, one CSV per section and printable HTML."""
    parser = argparse.ArgumentParser(prog="wealth tax-pack",
                                     description="Write the annual tax pack for a contador or CPA (JSON, CSV, HTML)")
    parser.add_argument("command", choices=("tax-pack",))
    parser.add_argument("--db", help="SQLite path (default WEALTH_DB or user data directory)")
    parser.add_argument("--client", help="client identifier (reads its ledger and saved facts)")
    parser.add_argument("--input", help="JSON file of tax_pack inputs ('-' reads stdin); needed without --client")
    parser.add_argument("--year", type=int, help="tax year (default: the last completed year)")
    parser.add_argument("--jurisdiction", help="MX, US or MX,US (default: from the profile)")
    parser.add_argument("--lang", choices=("es", "en"), help="language of the CSV headers and the page")
    parser.add_argument("--out", default=".", help="folder to write into (default: the current folder)")
    parser.add_argument("--format", action="append", choices=("json", "csv", "html"),
                        help="what to write (repeatable; default all three)")
    args = parser.parse_args(argv)
    try:
        inputs: dict = {}
        if args.input:
            raw = sys.stdin.read() if args.input == "-" else Path(args.input).read_text(encoding="utf-8")
            inputs = json.loads(raw, parse_constant=_reject_constant) if raw.strip() else {}
            if not isinstance(inputs, dict):
                raise ValueError("tax-pack --input must be a JSON object of tax_pack inputs")
        elif not args.client:
            raise ValueError("tax-pack needs --client, or --input with facts and a ledger")
        if args.year is not None:
            inputs["tax_year"] = args.year
        if args.jurisdiction:
            inputs["jurisdiction"] = args.jurisdiction
        if args.lang:
            inputs["language"] = args.lang
        from .taxpack import write_exports
        report = WealthService(args.db).run("tax_pack", inputs=inputs, client_id=args.client)
        files = write_exports(report, args.out, args.lang, args.format or ("json", "csv", "html"))
        result = report.get("result") or {}
        print(json.dumps({"status": report["status"], "tax_year": result.get("tax_year"),
                          "jurisdictions": result.get("jurisdictions"), "files": files,
                          "pendientes": len(result.get("pendientes") or [])}, ensure_ascii=False, indent=2),
              flush=True)
        return 0
    except (ValueError, TypeError, KeyError, OSError, sqlite3.Error) as exc:
        print(json.dumps({"error": str(exc), "error_type": type(exc).__name__}), file=sys.stderr)
        return 2


def main(argv: list[str] | None = None) -> int:
    """Run the CLI; a reader that closes the pipe early (``wealth context | head``) ends it quietly."""
    try:
        return _main(argv)
    except BrokenPipeError:
        _silence_stdout()
        return 1


def _silence_stdout() -> None:
    """Point stdout at /dev/null so the interpreter's final flush cannot raise again."""
    try:
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, sys.stdout.fileno())
    except (OSError, ValueError, AttributeError):
        pass  # stdout is not a real file (tests capture it); nothing left to flush


def _main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)
    if _command(argv) in TEXT_COMMANDS:  # text-channel helpers, see docs/openclaw.md
        return text_main(argv)
    if _command(argv) == "tax-pack":
        return _tax_pack(argv)
    parser = argparse.ArgumentParser(description="Wealth JSON CLI: one JSON object in (stdin or --input), JSON out")
    parser.add_argument("operation", choices=(*OPERATIONS, "watch"),
                        help=" | ".join((*OPERATIONS, "watch", "tax-pack")) + " "
                             "(text channels: onboarding | today | view, see docs/openclaw.md)")
    parser.add_argument("--db", help="SQLite path (default WEALTH_DB or user data directory)")
    parser.add_argument("--input", default="-", help="JSON argument file; '-' reads stdin")
    parser.add_argument("--client", help="explicit client identifier for wealth watch or forget")
    parser.add_argument("--interval", type=float, default=300,
                        help="foreground watch interval in seconds (default: 300)")
    parser.add_argument("--once", action="store_true",
                        help="evaluate monitoring once and exit")
    parser.add_argument("--yes", action="store_true",
                        help="the person has agreed: run ingest confirm, decision accept or resolve_contradiction "
                             "without an interactive terminal")
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
        what = _saves_on_consent(args.operation, payload)
        if what and not args.yes:
            _confirm_save(what)
        result = dispatch(args.operation, payload, args.db)
        print(json.dumps(result, ensure_ascii=False, allow_nan=False, indent=2), flush=True)
        return 0
    except BrokenPipeError:
        raise
    except (ValueError, TypeError, KeyError, OSError, sqlite3.Error) as exc:
        print(json.dumps({"error": str(exc), "error_type": type(exc).__name__}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
