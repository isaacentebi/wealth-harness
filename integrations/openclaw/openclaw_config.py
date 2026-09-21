"""Add or remove Wealth's entries in an OpenClaw config (stdlib only; used by install.sh).

    python openclaw_config.py json   --home H --db D --uploads U --views V   # print the two JSON values
    python openclaw_config.py merge  --config C --home H --db D --uploads U --views V [--no-mcp] [--dry-run]
    python openclaw_config.py remove --config C [--dry-run]

``merge`` and ``remove`` edit only ``mcp.servers.wealth`` and ``skills.entries.wealth``,
back the file up first (``<config>.wealth-backup-<timestamp>``) and keep every other
entry.  They handle strict JSON only: OpenClaw's config is JSON5, so a file with
comments or trailing commas is left untouched (exit 3) and the snippet is printed
for a manual edit.  install.sh prefers ``openclaw mcp set`` / ``openclaw config set``
when the openclaw CLI is on PATH, which understand JSON5.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

SERVER = "wealth"
SKILL = "wealth"
JSON5_ONLY = 3


def server_entry(home: str, db: str, uploads: str) -> dict:
    """The stdio MCP server definition for ``mcp.servers.wealth``."""
    return {"command": "uv", "args": ["--directory", home, "run", "wealth-mcp"],
            "env": {"WEALTH_DB": db, "WEALTH_UPLOAD_DIR": uploads}}


def skill_env(home: str, db: str, uploads: str, views: str) -> dict:
    """``skills.entries.wealth.env``: what the skill's shell commands expect."""
    return {"WEALTH_HOME": home, "WEALTH_DB": db, "WEALTH_UPLOAD_DIR": uploads, "WEALTH_VIEW_DIR": views}


def snippet(home: str, db: str, uploads: str, views: str, mcp: bool = True) -> str:
    config: dict = {}
    if mcp:
        config["mcp"] = {"servers": {SERVER: server_entry(home, db, uploads)}}
    config["skills"] = {"entries": {SKILL: {"enabled": True, "env": skill_env(home, db, uploads, views)}}}
    return json.dumps(config, indent=2)


def _load(path: Path) -> dict:
    if not path.exists():
        return {}
    text = path.read_text(encoding="utf-8")
    if not text.strip():
        return {}
    value = json.loads(text)  # JSONDecodeError for JSON5 syntax
    if not isinstance(value, dict):
        raise ValueError("the config is not a JSON object")
    return value


def _backup(path: Path) -> Path | None:
    if not path.exists():
        return None
    target = path.with_name(f"{path.name}.wealth-backup-{time.strftime('%Y%m%d%H%M%S')}")
    n = 1
    while target.exists():
        n += 1
        target = path.with_name(f"{path.name}.wealth-backup-{time.strftime('%Y%m%d%H%M%S')}-{n}")
    shutil.copy2(path, target)
    os.chmod(target, 0o600)
    return target


def _write(path: Path, config: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(config, handle, indent=2)
            handle.write("\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _section(config: dict, *keys: str) -> dict:
    node = config
    for key in keys:
        child = node.get(key)
        if child is None:
            child = node[key] = {}
        if not isinstance(child, dict):
            raise ValueError(f"{'.'.join(keys)} is not an object in the config")
        node = child
    return node


def merge(config: dict, home: str, db: str, uploads: str, views: str, mcp: bool = True) -> dict:
    """A copy of ``config`` with Wealth's entries set and everything else kept."""
    out = json.loads(json.dumps(config))
    if mcp:
        _section(out, "mcp", "servers")[SERVER] = server_entry(home, db, uploads)
    entry = _section(out, "skills", "entries", SKILL)
    entry.setdefault("enabled", True)
    env = entry.get("env")
    entry["env"] = {**(env if isinstance(env, dict) else {}), **skill_env(home, db, uploads, views)}
    return out


def remove(config: dict) -> dict:
    out = json.loads(json.dumps(config))
    servers = (out.get("mcp") or {}).get("servers")
    if isinstance(servers, dict):
        servers.pop(SERVER, None)
    entries = (out.get("skills") or {}).get("entries")
    if isinstance(entries, dict):
        entries.pop(SKILL, None)
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=("json", "snippet", "merge", "remove"))
    parser.add_argument("--config")
    parser.add_argument("--home")
    parser.add_argument("--db")
    parser.add_argument("--uploads")
    parser.add_argument("--views")
    parser.add_argument("--no-mcp", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    values = (args.home, args.db, args.uploads, args.views)
    if args.command in ("json", "snippet", "merge") and not all(values):
        parser.error("--home, --db, --uploads and --views are required")
    if args.command == "json":
        print(json.dumps(server_entry(args.home, args.db, args.uploads)))
        print(json.dumps(skill_env(*values)))
        return 0
    if args.command == "snippet":
        print(snippet(*values, mcp=not args.no_mcp))
        return 0
    if not args.config:
        parser.error("--config is required")
    path = Path(args.config).expanduser()
    try:
        config = _load(path)
    except (json.JSONDecodeError, ValueError) as exc:
        print(f"{path.name} is not strict JSON ({exc}); left untouched. Add this by hand, or install the "
              f"openclaw CLI and rerun:", file=sys.stderr)
        if args.command == "merge":
            print(snippet(*values, mcp=not args.no_mcp), file=sys.stderr)
        return JSON5_ONLY
    updated = merge(config, *values, mcp=not args.no_mcp) if args.command == "merge" else remove(config)
    if updated == config:
        print(f"{path.name}: already up to date")
        return 0
    if args.dry_run:
        print(f"[dry-run] would back up and update {path}")
        return 0
    backup = _backup(path)
    _write(path, updated)
    print(f"updated {path}" + (f" (backup: {backup.name})" if backup else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
