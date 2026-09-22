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
  ``CODEX_HOME``" exactly as with ``--ignore-user-config``. Sessions live in
  that private home, so an exec thread cannot be resumed here (and the other
  way round): the turn then starts fresh with the recent conversation, as
  after any failed resume.
- The same scrubbed environment (``agent.child_env``), plus that ``CODEX_HOME``.
- One process per turn, in its own process group: stop, timeout or an
  abandoned turn kills Codex and its MCP children together. The MCP server
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
import stat
import subprocess
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

_unavailable_lock = threading.Lock()
_unavailable: str | None = None  # why app-server failed in this process; the auto runtime then stays on exec


class RuntimeUnavailable(RuntimeError):
    """app-server could not start a turn (not supported, or failed before the thread existed)."""


def runtime_setting() -> str:
    """``exec``, ``appserver`` or ``auto`` (the default: app-server when it works, else exec)."""
    value = os.environ.get(RUNTIME_ENV, "").strip().lower()
    return value if value in RUNTIMES else "auto"


def mark_unavailable(reason: str) -> None:
    global _unavailable
    with _unavailable_lock:
        _unavailable = reason or "unavailable"


def unavailable_reason() -> str | None:
    return _unavailable


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
    return _unavailable is None


# --------------------------------------------------------------------------- private CODEX_HOME


def _user_codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex").expanduser().absolute()


def codex_home() -> Path:
    """The Wealth-owned CODEX_HOME for app-server turns (created 0700; raises RuntimeUnavailable if unusable).

    ``$WEALTH_CODEX_HOME``, else ``$XDG_DATA_HOME/wealth-harness/codex-home`` (default
    ``~/.local/share``), next to the default database. It holds an empty
    ``config.toml`` (rewritten each time: nothing from the user's config applies),
    ``auth.json`` as a symlink to the user's own, and this runtime's sessions.
    """
    configured = os.environ.get(HOME_ENV)
    if configured:
        home = Path(configured).expanduser()
    else:
        base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share")
        home = base / "wealth-harness" / "codex-home"
    if home.absolute() == _user_codex_home():
        raise RuntimeUnavailable("the Wealth CODEX_HOME must not be the user's own")
    auth = _user_codex_home() / "auth.json"
    if not auth.is_file():
        raise RuntimeUnavailable("no auth.json to share (Codex signed out, or keyring credentials)")
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = home.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise RuntimeUnavailable(f"{home} must be a real directory")
    if hasattr(os, "getuid") and info.st_uid != os.getuid():
        raise RuntimeUnavailable(f"{home} is not owned by the current user")
    if info.st_mode & 0o077:
        home.chmod(0o700)
    _agent._write_private(home / "config.toml", "# Wealth's private Codex home: no user config applies here.\n")
    link = home / "auth.json"
    if link.is_symlink():
        if Path(os.readlink(link)) != auth:
            link.unlink()
    elif link.exists():
        # Only a Codex that replaced the link with a file could put one here; its tokens may be newer than the
        # user's, so leave it for the person to look at instead of deleting it.
        raise RuntimeUnavailable(f"{link} is a file, not the link to the Codex login")
    if not link.is_symlink():
        try:
            link.symlink_to(auth)
        except FileExistsError:  # another turn made it a moment ago
            if not (link.is_symlink() and Path(os.readlink(link)) == auth):
                raise RuntimeUnavailable(f"{link} is not the link to the Codex login") from None
    return home


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


def config_problem(config: object) -> str | None:
    """Why the app-server's effective config differs from the exec posture, or None when it matches.

    Only the Wealth MCP server may be enabled, the sandbox must be read-only,
    approvals never asked, every feature Wealth disables off and no notify hook.
    """
    if not isinstance(config, dict):
        return "config/read returned no config"
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


def stream(command: Sequence[str], prompt: str, timeout: float, control=None, cwd: str | Path | None = None, *,
           resume_thread: str | None = None, ephemeral: bool = False) -> Iterator[tuple]:
    """Run one turn on a fresh ``codex app-server``; yields like ``agent._stream_process`` plus ``delta`` items.

    Raises ``RuntimeUnavailable`` when app-server cannot start the turn before
    any thread exists (the caller then uses exec), ``AgentError`` on timeout or
    cancellation. A resume that fails ends with ``("exit", 1, reason)`` and no
    activity, so the caller starts a fresh thread as it does for exec.
    """
    AgentError = _agent.AgentError
    home = codex_home()
    env = _agent.child_env()
    env["CODEX_HOME"] = str(home)
    try:
        process = subprocess.Popen(
            list(command), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            encoding="utf-8", errors="replace", cwd=str(cwd) if cwd else None, env=env, start_new_session=True,
        )
    except FileNotFoundError as exc:
        raise AgentError(None, "not_installed") from exc
    except OSError as exc:
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
        reply = response(connection.request("config/read", {"cwd": str(cwd) if cwd else None}), handshake_deadline)
        problem = config_problem((reply.get("result") or {}).get("config")) if "error" not in reply else \
            "config/read failed: " + _error_text(reply["error"])
        if problem:
            raise RuntimeUnavailable(problem)
        thread_params: dict[str, Any] = {"cwd": str(cwd) if cwd else None, "sandbox": "read-only",
                                         "approvalPolicy": "never"}
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
        _agent._terminate(process)
