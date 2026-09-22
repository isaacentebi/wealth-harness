"""The streaming Codex runtime: one turn over ``codex app-server`` (JSON-RPC on stdio).

``codex exec --json`` reports each message whole, so an answer appears all at
once. ``codex app-server`` also sends ``item/agentMessage/delta`` notifications,
so the chat can show the answer as it is written. This module drives one turn
over that protocol and yields the same items ``agent._stream_process`` does,
so ``agent.stream_turn`` parses both runtimes with one parser:

- ``("line", json)``: an event in the ``codex exec --json`` shape
  (``thread.started``, ``item.started``/``item.completed`` with
  ``agent_message``/``reasoning``/``web_search``/``mcp_tool_call`` items,
  ``turn.completed``, ``turn.failed``, ``error``), translated from the
  app-server's notifications;
- ``("delta", text, item_id)``: a chunk of an answer as it is written;
- ``("exit", code, stderr)``: the end of the turn.

Security posture, kept equal to the exec path:

- The same ``-c`` overrides as ``codex exec`` (``agent._config_overrides``): the
  read-only sandbox, the disabled features, the Wealth instructions file, the
  Wealth MCP server and its env (the per-turn consent file, the web-search
  taint flag), web search, reasoning and service tier.
- ``codex app-server`` has no ``--ignore-user-config``, and its ``-c`` overrides
  merge into the user's ``config.toml`` rather than replace it (their other MCP
  servers, plugins, notify hooks and base URL would load). So it runs with a
  Wealth-owned ``CODEX_HOME`` (``codex_home()``): an empty ``config.toml`` and a
  symlink to the user's ``auth.json``. Codex writes ``auth.json`` in place, so a
  token refresh goes through the link to the real file; auth "still uses
  ``CODEX_HOME``" exactly as with ``--ignore-user-config``. Anything else Codex
  would load from a home (AGENTS.md, skills, hooks, rules) is removed each turn,
  and the process runs in a fresh empty directory (``scratch_dir()``) so no
  project ``.codex`` loads. The exec runtime uses the same home and scratch
  directory (``agent._stream_process(..., ISOLATED)``).
- ``config/read`` with ``includeLayers`` must show nothing but Wealth's own ``-c``
  flags, and no base URL, provider, hooks, skills or projects (``config_problem``).
- The same scrubbed environment (``agent.child_env``), plus that ``CODEX_HOME``.
- One process per turn, in its own process group: stop, timeout or an
  abandoned turn kills Codex and every descendant (``agent._terminate``; Codex
  puts the MCP server in a process group of its own). The MCP server
  reads the turn's consent evidence when its thread starts, and Codex keeps a
  loaded thread's MCP servers running, so a process kept warm across turns would
  keep the first turn's evidence; a new process per turn keeps it exact.
  Starting one and completing the handshake takes about 45 ms, which is all a
  warm process would save; the MCP server's own start is per thread either way.
- Approval requests are declined and any other request from the server gets a
  JSON-RPC error: nothing is approved on the person's behalf.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import queue
import shutil
import stat
import subprocess
import tempfile
import threading
import time
from typing import Any, Iterator, Mapping, Sequence

from . import agent as _agent

CODEX_COMMAND: tuple[str, ...] = ("codex",)
"""The Codex executable; tests point it at a fake app-server."""

RUNTIMES = ("exec", "appserver")
RUNTIME_ENV = "WEALTH_RUNTIME"
HOME_ENV = "WEALTH_CODEX_HOME"
HANDSHAKE_SECONDS = 30.0
CLOSE_SECONDS = 5.0
CLIENT_INFO = {"name": "wealth", "title": "Wealth", "version": "1"}

RETRY_SECONDS = 300.0
"""How long the auto runtime stays on exec after app-server failed before it tries app-server again."""

_unavailable_lock = threading.Lock()
_unavailable: tuple[str, float] | None = None  # why app-server last failed here, and when (monotonic)


class RuntimeUnavailable(RuntimeError):
    """app-server could not start a turn (not supported, or failed before the thread existed)."""


def runtime_setting() -> str:
    """``exec``, ``appserver`` or ``auto`` (the default: app-server when it works, else exec)."""
    value = os.environ.get(RUNTIME_ENV, "").strip().lower()
    return value if value in RUNTIMES else "auto"


def mark_unavailable(reason: str) -> None:
    global _unavailable
    with _unavailable_lock:
        _unavailable = (reason or "unavailable", time.monotonic())


def unavailable_reason() -> str | None:
    """Why app-server failed, while that still holds (``RETRY_SECONDS``); None once it may be tried again."""
    with _unavailable_lock:
        entry = _unavailable
    if entry is None or time.monotonic() - entry[1] >= RETRY_SECONDS:
        return None
    return entry[0]


def reset() -> None:
    """Forget an earlier failure (tests; a restart does the same)."""
    global _unavailable
    with _unavailable_lock:
        _unavailable = None


def use_appserver() -> bool:
    setting = runtime_setting()
    if setting == "exec":
        return False
    if setting == "appserver":
        return True
    return unavailable_reason() is None


# --------------------------------------------------------------------------- private CODEX_HOME


def _user_codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex").expanduser().absolute()


HOME_MARKER = ".wealth-codex-home"
_CONFIG_TEXT = "# Wealth's private Codex home: no user config applies here.\n"
# What Codex loads from its home besides config.toml: global instructions, skills, hooks and exec-policy rules.
# Wealth writes none of them; they are removed before every turn in case something planted them.
_STRIPPED = ("AGENTS.md", "AGENTS.override.md", "skills", "hooks", "hooks.json", "rules")


def _configured_home() -> Path:
    configured = os.environ.get(HOME_ENV)
    if configured:
        home = Path(configured).expanduser()
    else:
        base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share").expanduser()
        home = base / "wealth-harness" / "codex-home"
    if not home.is_absolute():
        raise RuntimeUnavailable(f"the Wealth CODEX_HOME must be an absolute path, not {home}")
    return home


def _within(path: Path, root: Path) -> bool:
    """Whether ``path`` is ``root`` or inside it: resolved, case-folded, and by inode for what exists.

    APFS is case-insensitive by default, so ``~/.CODEX`` is ``~/.codex``: comparing ``(st_dev, st_ino)`` of
    ``path`` and each existing parent with ``root`` catches that and any other alias, and the case-folded
    comparison catches it before the directory exists.
    """
    path, root = path.resolve(), root.resolve()
    if path == root or root in path.parents:
        return True
    folded, folded_root = [p.casefold() for p in path.parts], [p.casefold() for p in root.parts]
    if folded[:len(folded_root)] == folded_root:
        return True
    try:
        target = os.stat(root)
    except OSError:
        return False
    for candidate in (path, *path.parents):
        try:
            info = os.stat(candidate)
        except OSError:
            continue
        if (info.st_dev, info.st_ino) == (target.st_dev, target.st_ino):
            return True
    return False


def _check_parents(home: Path) -> None:
    """Outside ``$HOME``, every existing component of ``home`` must be safe from other users.

    Owned by the current user or root and not group- or world-writable, so nobody else can swap a
    component for a symlink between this check and Codex's use of the path. A root-owned sticky
    directory (``/tmp``) is accepted: nobody else can rename or remove what is ours inside it.
    """
    user_home = Path.home().resolve()
    if home == user_home or user_home in home.parents:
        return
    uid = os.getuid() if hasattr(os, "getuid") else None
    for part in (*reversed(home.parents), home):
        try:
            info = os.lstat(part)
        except FileNotFoundError:
            return  # this one and the rest are created below, 0700
        except OSError as exc:
            raise RuntimeUnavailable(f"cannot check {part}: {exc.strerror}") from None
        if stat.S_ISLNK(info.st_mode):
            raise RuntimeUnavailable(f"{part} changed while it was checked")
        if uid is None:
            continue
        sticky_root = info.st_uid == 0 and bool(info.st_mode & stat.S_ISVTX)
        if info.st_uid not in (uid, 0) or (info.st_mode & 0o022 and not sticky_root):
            raise RuntimeUnavailable(f"{part} can be changed by other users; choose a private WEALTH_CODEX_HOME")


def _is_wealth_home(home: Path) -> bool:
    """An existing directory Wealth may use: it has the marker, is a home made before the marker, or is empty."""
    if (home / HOME_MARKER).is_file():
        return True
    try:
        if (home / "config.toml").read_text(encoding="utf-8") == _CONFIG_TEXT:
            return True
    except (OSError, UnicodeDecodeError):
        pass
    try:
        return not any(home.iterdir())
    except OSError:
        return False


def _strip(home: Path) -> None:
    """Remove ``_STRIPPED`` from ``home`` (never following a symlink out of it)."""
    for name in _STRIPPED:
        path = home / name
        try:
            info = path.lstat()
        except FileNotFoundError:
            continue
        # A Codex starting at the same moment (the memory step) may recreate skills/ with its bundled skills;
        # that is Codex's own content, so a directory that reappears is not an error.
        if stat.S_ISDIR(info.st_mode):
            shutil.rmtree(path, ignore_errors=True)
        else:
            path.unlink(missing_ok=True)


def codex_home(*, require_login: bool = True) -> Path:
    """The Wealth-owned CODEX_HOME, resolved (created 0700; raises RuntimeUnavailable if unusable).

    ``$WEALTH_CODEX_HOME``, else ``$XDG_DATA_HOME/wealth-harness/codex-home`` (default
    ``~/.local/share``), next to the default database. It holds an empty
    ``config.toml`` (rewritten each time: nothing from the user's config applies),
    ``auth.json`` as a symlink to the user's own, and the sessions of both runtimes.

    The path is resolved once (Codex gets the resolved path) and every check runs before anything is
    written: it must not be, contain or be inside the user's own Codex home (``_within``); outside
    ``$HOME`` its parents must be private (``_check_parents``); an existing directory must be one Wealth
    made (``_is_wealth_home``). Each call then removes any AGENTS.md, skills, hooks and rules there.
    ``require_login=False`` (exec signed in with ``CODEX_API_KEY``) allows a user home without ``auth.json``.
    """
    home = _configured_home().resolve()
    user = _user_codex_home()
    if _within(home, user) or _within(user, home):
        raise RuntimeUnavailable("the Wealth CODEX_HOME must not be, contain or be inside the user's own")
    _check_parents(home)
    auth = user / "auth.json"
    if require_login and not auth.is_file():
        raise RuntimeUnavailable("no auth.json to share (Codex signed out, or keyring credentials)")
    uid = os.getuid() if hasattr(os, "getuid") else None

    def check_directory() -> None:
        info = home.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise RuntimeUnavailable(f"{home} must be a real directory")
        if uid is not None and info.st_uid != uid:
            raise RuntimeUnavailable(f"{home} is not owned by the current user")

    link = home / "auth.json"
    if os.path.lexists(home):
        check_directory()
        if not _is_wealth_home(home):
            raise RuntimeUnavailable(f"{home} is not empty and is not Wealth's Codex home")
        if os.path.lexists(link) and not link.is_symlink():
            # Only a Codex that replaced the link with a file could put one here; its tokens may be newer than
            # the user's, so leave it for the person to look at instead of deleting it.
            raise RuntimeUnavailable(f"{link} is a file, not the link to the Codex login")
    # Every check passed; from here on Wealth writes.
    for part in (*reversed(home.parents), home):
        if not os.path.lexists(part):
            try:
                part.mkdir(mode=0o700)
            except FileExistsError:
                pass
    check_directory()
    if home.lstat().st_mode & 0o077:
        home.chmod(0o700)
    if not (home / HOME_MARKER).is_file():
        _agent._write_private(home / HOME_MARKER, "Wealth's private Codex home.\n")
    _strip(home)
    _agent._write_private(home / "config.toml", _CONFIG_TEXT)
    if link.is_symlink() and Path(os.readlink(link)) != auth:
        link.unlink()
    if not link.is_symlink() and auth.is_file():
        try:
            link.symlink_to(auth)
        except FileExistsError:  # another turn made it a moment ago
            if not (link.is_symlink() and Path(os.readlink(link)) == auth):
                raise RuntimeUnavailable(f"{link} is not the link to the Codex login") from None
    return home


SCRATCH_PREFIX = "wealth-codex-cwd-"


def scratch_dir() -> Path:
    """A fresh, empty 0700 working directory for one Codex process: no project ``.codex``, skills or AGENTS.md."""
    return Path(tempfile.mkdtemp(prefix=SCRATCH_PREFIX)).resolve()


def remove_scratch(path: Path | None) -> None:
    if path is not None and path.name.startswith(SCRATCH_PREFIX):
        shutil.rmtree(path, ignore_errors=True)


# --------------------------------------------------------------------------- command


def build_command(model: str, db_path: str | Path, *, web_search: bool = True, reasoning: str = "low",
                  instructions: Path | None = None, tools=None, turn_env: Mapping[str, str] | None = None) -> list[str]:
    """``codex app-server`` with exactly the overrides ``codex exec`` gets, and the model."""
    if reasoning not in _agent.REASONING_LEVELS:
        raise ValueError("reasoning must be low, medium, or high")
    overrides = _agent._config_overrides(db_path, web_search=web_search, reasoning=reasoning,
                                         instructions=instructions, tools=tools, turn_env=turn_env)
    return [*CODEX_COMMAND, "app-server",
            "-c", f"model={_agent._toml(_agent.resolve_model(model))}",
            "-c", 'approval_policy="never"',
            *overrides]


CHATGPT_BASE_URLS = (None, "https://chatgpt.com/backend-api/", "https://chatgpt.com/backend-api")
"""Codex's own default (config/read reports it even when nothing sets it)."""
_ABSENT_KEYS = ("openai_base_url", "hooks", "skills", "projects")


def _empty(value: object) -> bool:
    return value is None or value == {} or value == []


def layers_problem(layers: object) -> str | None:
    """Every config layer but Wealth's own ``-c`` flags (``sessionFlags``) must be empty.

    The private home's ``config.toml`` is empty, so a non-empty layer is something else speaking: a
    system or managed config, MDM, or a project ``.codex/config.toml`` (reported even while untrusted).
    """
    if not isinstance(layers, list) or not layers:
        return "config/read returned no config layers"
    for layer in layers:
        if not isinstance(layer, dict):
            return "config/read returned a malformed layer"
        name = layer.get("name") if isinstance(layer.get("name"), dict) else {}
        kind = name.get("type")
        if kind == "sessionFlags":
            continue
        if not _empty(layer.get("config")):
            where = name.get("file") or name.get("domain") or name.get("id") or ""
            return f"the {kind or 'unknown'} config layer {where}".rstrip() + " is not empty"
    return None


def config_problem(config: object, layers: object) -> str | None:
    """Why the app-server's effective config differs from the exec posture, or None when it matches.

    Only Wealth's own flags may set anything (``layers_problem``). Only the Wealth MCP server may be
    enabled, the sandbox must be read-only, approvals never asked, every feature Wealth disables off, no
    notify hook, no other model provider or base URL (where the login token would be sent), and no
    hooks, skills, trusted projects or ``experimental_*`` endpoints.
    """
    if not isinstance(config, dict):
        return "config/read returned no config"
    problem = layers_problem(layers)
    if problem:
        return problem
    for key in _ABSENT_KEYS:
        if not _empty(config.get(key)):
            return f"{key} is set"
    if config.get("chatgpt_base_url") not in CHATGPT_BASE_URLS:
        return "chatgpt_base_url is not Codex's default"
    if not _empty(config.get("model_providers")):
        return "a model provider is configured"
    if config.get("model_provider") not in (None, "openai"):
        return f"model_provider is {config.get('model_provider')!r}"
    for key, value in config.items():
        if key.startswith("experimental_") and not _empty(value) and value is not False:
            return f"{key} is set"
    servers = config.get("mcp_servers") or {}
    if not isinstance(servers, dict):
        return "config/read returned malformed mcp_servers"
    enabled = sorted(name for name, spec in servers.items()
                     if not (isinstance(spec, dict) and spec.get("enabled") is False))
    if enabled != ["wealth"]:
        return f"MCP servers other than Wealth are enabled: {', '.join(n for n in enabled if n != 'wealth') or 'none'}"
    if config.get("sandbox_mode") != "read-only":
        return f"sandbox_mode is {config.get('sandbox_mode')!r}, not read-only"
    if config.get("approval_policy") not in (None, "never"):
        return f"approval_policy is {config.get('approval_policy')!r}"
    features = config.get("features") or {}
    for name in _agent.DISABLED_FEATURES:
        value = features.get(name) if isinstance(features, dict) else None
        if isinstance(value, dict):
            value = value.get("enabled")
        if value not in (None, False):
            return f"feature {name} is on"
    if config.get("notify"):
        return "a notify hook is configured"
    return None


# --------------------------------------------------------------------------- translation

_ITEM_TYPES = {"agentMessage": "agent_message", "reasoning": "reasoning", "webSearch": "web_search",
               "mcpToolCall": "mcp_tool_call"}
_STATUS = {"inProgress": "in_progress", "completed": "completed", "failed": "failed"}


def translate_item(item: Mapping[str, Any]) -> dict[str, Any] | None:
    """An app-server ThreadItem in the ``codex exec --json`` item shape the turn parser reads."""
    kind = _ITEM_TYPES.get(str(item.get("type")))
    if kind is None:
        return None
    out: dict[str, Any] = {"id": item.get("id"), "type": kind}
    if kind == "agent_message":
        out["text"] = item.get("text") if isinstance(item.get("text"), str) else ""
        out["phase"] = item.get("phase")
    elif kind == "web_search":
        out["query"] = item.get("query")
    elif kind == "mcp_tool_call":
        out["server"] = item.get("server")
        out["tool"] = item.get("tool")
        out["arguments"] = item.get("arguments")
        status = item.get("status")
        out["status"] = _STATUS.get(status, status) if isinstance(status, str) else None
        result = item.get("result")
        if isinstance(result, dict):
            out["result"] = {"content": result.get("content") or [],
                             "structured_content": result.get("structuredContent")}
        else:
            out["result"] = None
        error = item.get("error")
        out["error"] = error if error else None
    return out


def _line(event: Mapping[str, Any]) -> tuple[str, str]:
    return ("line", json.dumps(event, ensure_ascii=False))


# --------------------------------------------------------------------------- one turn


class _Connection:
    """JSON-RPC over the child's stdio: newline-delimited JSON messages."""

    def __init__(self, process: subprocess.Popen[str]):
        self.process = process
        self.inbox: queue.Queue[dict[str, Any] | None] = queue.Queue()
        self.next_id = 0
        self._write_lock = threading.Lock()

    def send(self, message: Mapping[str, Any]) -> None:
        with self._write_lock:
            try:
                assert self.process.stdin is not None
                self.process.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
                self.process.stdin.flush()
            except (BrokenPipeError, OSError, ValueError):
                pass

    def request(self, method: str, params: Mapping[str, Any] | None) -> int:
        self.next_id += 1
        self.send({"id": self.next_id, "method": method, "params": params})
        return self.next_id

    def answer_server_request(self, message: Mapping[str, Any]) -> None:
        """Decline approvals; refuse anything else. Nothing is granted on the person's behalf."""
        method = str(message.get("method"))
        if method in {"item/commandExecution/requestApproval", "item/fileChange/requestApproval"}:
            self.send({"id": message["id"], "result": {"decision": "decline"}})
        elif method in {"execCommandApproval", "applyPatchApproval"}:
            self.send({"id": message["id"], "result": {"decision": "abort"}})
        elif method == "mcpServer/elicitation/request":
            self.send({"id": message["id"], "result": {"action": "decline", "content": None, "_meta": None}})
        else:
            self.send({"id": message["id"], "error": {"code": -32601, "message": f"{method} is not supported"}})

    def read(self) -> None:
        assert self.process.stdout is not None
        try:
            for raw in self.process.stdout:
                if not raw.strip():
                    continue
                try:
                    message = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if isinstance(message, dict):
                    self.inbox.put(message)
        except (OSError, ValueError):
            pass
        finally:
            self.inbox.put(None)


def _error_text(value: object) -> str:
    if isinstance(value, dict):
        for key in ("message", "additionalDetails"):
            if isinstance(value.get(key), str) and value[key].strip():
                return value[key]
    return str(value or "")


def stream(command: Sequence[str], prompt: str, timeout: float, control=None, *,
           resume_thread: str | None = None, ephemeral: bool = False) -> Iterator[tuple]:
    """Run one turn on a fresh ``codex app-server``; yields like ``agent._stream_process`` plus ``delta`` items.

    Codex runs with the private home (``codex_home``) in a fresh empty working directory
    (``scratch_dir``, removed afterwards), so no project ``.codex`` skills or config load.
    Raises ``RuntimeUnavailable`` when app-server cannot start the turn before
    any thread exists (the caller then uses exec), ``AgentError`` on timeout or
    cancellation. A resume that fails ends with ``("exit", 1, reason)`` and no
    activity, so the caller starts a fresh thread as it does for exec.
    """
    AgentError = _agent.AgentError
    home = codex_home()
    env = _agent.child_env()
    env["CODEX_HOME"] = str(home)
    cwd = scratch_dir()
    try:
        process = subprocess.Popen(
            list(command), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            encoding="utf-8", errors="replace", cwd=str(cwd), env=env, start_new_session=True,
        )
    except FileNotFoundError as exc:
        remove_scratch(cwd)
        raise AgentError(None, "not_installed") from exc
    except OSError as exc:
        remove_scratch(cwd)
        raise RuntimeUnavailable(_agent.safe_diagnostic(str(exc))) from exc
    if control is not None:
        control.attach(process)
    connection = _Connection(process)
    stderr_parts: list[str] = []

    def read_stderr() -> None:
        assert process.stderr is not None
        size = 0
        try:
            for line in process.stderr:
                if size < 65_536:
                    stderr_parts.append(line)
                    size += len(line)
        except (OSError, ValueError):
            pass

    workers = [threading.Thread(target=connection.read, daemon=True),
               threading.Thread(target=read_stderr, daemon=True)]
    for worker in workers:
        worker.start()
    deadline = time.monotonic() + timeout
    handshake_deadline = time.monotonic() + min(timeout, HANDSHAKE_SECONDS)

    def next_message(until: float) -> dict[str, Any] | None:
        """The next message, or None at EOF; raises on cancel and timeout."""
        while True:
            if control is not None and control.cancelled:
                _agent._terminate(process)
                raise AgentError(None, "cancelled")
            remaining = min(until, deadline) - time.monotonic()
            if remaining <= 0:
                if until < deadline:
                    raise RuntimeUnavailable("app-server did not answer the handshake in time")
                _agent._terminate(process)
                raise AgentError(f"Codex did not finish within {timeout:g} seconds.", "timeout")
            try:
                message = connection.inbox.get(timeout=min(remaining, 0.25))
            except queue.Empty:
                continue
            if message is not None and "method" in message and "id" in message:
                connection.answer_server_request(message)
                continue
            return message

    def response(request_id: int, until: float) -> dict[str, Any]:
        """The response to ``request_id``; notifications before it are dropped (none carry the turn yet)."""
        while True:
            message = next_message(until)
            if message is None:
                raise RuntimeUnavailable(_agent.safe_diagnostic("".join(stderr_parts)) or "app-server exited")
            if message.get("id") == request_id and "method" not in message:
                return message

    try:
        reply = response(connection.request("initialize", {"clientInfo": CLIENT_INFO, "capabilities": {
            "experimentalApi": False, "requestAttestation": False}}), handshake_deadline)
        if "error" in reply:
            raise RuntimeUnavailable(_error_text(reply["error"]) or "initialize failed")
        connection.send({"method": "initialized"})
        # Defense in depth: the effective config must be what the overrides say before any thread starts.
        reply = response(connection.request("config/read", {"cwd": str(cwd), "includeLayers": True}),
                         handshake_deadline)
        result = reply.get("result") if isinstance(reply.get("result"), dict) else {}
        problem = config_problem(result.get("config"), result.get("layers")) if "error" not in reply else \
            "config/read failed: " + _error_text(reply["error"])
        if problem:
            raise RuntimeUnavailable(problem)
        thread_params: dict[str, Any] = {"cwd": str(cwd), "sandbox": "read-only", "approvalPolicy": "never"}
        if resume_thread is not None:
            reply = response(connection.request("thread/resume", {"threadId": resume_thread, **thread_params,
                                                                  "excludeTurns": True}), handshake_deadline)
            if "error" in reply:
                # Unknown here (an exec session, or one this home no longer has): the caller starts fresh.
                yield ("exit", 1, "could not resume: " + _error_text(reply["error"]))
                return
        else:
            reply = response(connection.request("thread/start", {**thread_params, "ephemeral": bool(ephemeral)}),
                             handshake_deadline)
            if "error" in reply:
                raise RuntimeUnavailable(_error_text(reply["error"]) or "thread/start failed")
        result = reply.get("result") or {}
        sandbox = (result.get("sandbox") or {}).get("type")
        if sandbox not in (None, "readOnly"):
            raise RuntimeUnavailable(f"app-server reported sandbox {sandbox!r}, not read-only")
        thread_id = str((result.get("thread") or {}).get("id") or "")
        if not thread_id:
            raise RuntimeUnavailable("app-server returned no thread id")
        yield _line({"type": "thread.started", "thread_id": thread_id})
        turn_request = connection.request("turn/start", {
            "threadId": thread_id, "input": [{"type": "text", "text": prompt, "text_elements": []}]})
        commentary: set[str] = set()
        finished = False
        while not finished:
            message = next_message(deadline)
            if message is None:
                break  # the process ended before the turn did; the parser sees no turn.completed
            method = message.get("method")
            params = message.get("params") if isinstance(message.get("params"), dict) else {}
            if method is None:
                if message.get("id") == turn_request and "error" in message:
                    yield _line({"type": "turn.failed", "error": {"message": _error_text(message["error"])}})
                    finished = True
                continue
            if params.get("threadId") not in (None, thread_id):
                continue  # another thread's event (none are started here)
            if method in {"item/started", "item/completed"} and isinstance(params.get("item"), dict):
                raw = params["item"]
                if raw.get("type") == "agentMessage" and raw.get("phase") == "commentary":
                    commentary.add(str(raw.get("id")))
                item = translate_item(raw)
                if item is not None:
                    kind = "item.started" if method == "item/started" else "item.completed"
                    yield _line({"type": kind, "item": item})
            elif method == "item/agentMessage/delta":
                delta, item_id = params.get("delta"), str(params.get("itemId") or "")
                if isinstance(delta, str) and delta and item_id not in commentary:
                    yield ("delta", delta, item_id)
            elif method == "error":
                if not params.get("willRetry"):
                    yield _line({"type": "error", "message": _error_text(params.get("error"))})
            elif method == "turn/completed":
                turn = params.get("turn") if isinstance(params.get("turn"), dict) else {}
                status = turn.get("status")
                if status == "completed":
                    yield _line({"type": "turn.completed"})
                else:
                    text = _error_text(turn.get("error")) or f"Codex turn {status or 'ended'}."
                    yield _line({"type": "turn.failed", "error": {"message": text}})
                finished = True
        # The turn is over: closing stdin lets app-server flush the session and exit on its own.
        try:
            assert process.stdin is not None
            process.stdin.close()
        except (OSError, ValueError):
            pass
        try:
            process.wait(timeout=max(0.1, min(CLOSE_SECONDS, deadline - time.monotonic())))
        except subprocess.TimeoutExpired:
            pass
        workers[1].join(timeout=1)
        if control is not None and control.cancelled:
            raise AgentError(None, "cancelled")
        code = 0 if finished else (process.returncode if process.returncode not in (None, 0) else 1)
        yield ("exit", code, "".join(stderr_parts))
    finally:
        try:
            _agent._terminate(process)
        finally:
            remove_scratch(cwd)
