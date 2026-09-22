"""The streaming runtime over ``codex app-server``, driven against a fake app-server process (no model)."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import threading
import time

import pytest

from wealth import agent, appserver, web
from wealth.agent import AgentError, TurnControl

FAKE = Path(__file__).parent / "fixtures" / "fake_appserver.py"
ANSWER_CHUNKS = ["Hola ", "**Ana**", ".\n\n", "- uno\n", "- dos"]


@pytest.fixture
def fake(tmp_path, monkeypatch):
    """Point the runtime at the fake app-server, with a private Wealth CODEX_HOME and a stand-in login."""
    user_home = tmp_path / "user-codex"
    user_home.mkdir()
    (user_home / "auth.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("CODEX_HOME", str(user_home))
    monkeypatch.setenv("WEALTH_CODEX_HOME", str(tmp_path / "wealth-codex"))
    monkeypatch.setenv("WEALTH_RUNTIME", "appserver")
    monkeypatch.setenv("FAKE_APPSERVER_LOG", str(tmp_path / "fake.log"))
    monkeypatch.setenv("FAKE_APPSERVER_THREADS", str(tmp_path / "threads"))
    monkeypatch.setenv("FAKE_APPSERVER_MODE", "normal")
    monkeypatch.setattr(appserver, "CODEX_COMMAND", (sys.executable, str(FAKE)))
    monkeypatch.setattr(agent, "REAP_WAIT_SECONDS", 5.0)
    appserver.reset()
    yield tmp_path
    appserver.reset()


def _records(tmp_path):
    path = tmp_path / "fake.log"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []


def _turn(tmp_path, message="hola", **kwargs):
    kwargs.setdefault("client_id", "c1")
    kwargs.setdefault("db_path", tmp_path / "w.sqlite3")
    kwargs.setdefault("brief", "")
    kwargs.setdefault("timeout", 20)
    return list(agent.stream_turn(message, **kwargs))


def _overrides(command):
    return [command[i + 1] for i, part in enumerate(command[:-1]) if part == "-c"]


def test_deltas_arrive_in_order_and_add_up_to_the_answer(fake):
    events = _turn(fake)
    kinds = [e.type for e in events]
    deltas = [e for e in events if e.type == "delta"]
    assert [d.text for d in deltas] == ANSWER_CHUNKS
    assert {d.data["item"] for d in deltas} == {"m1"}
    answer = events[-1]
    assert answer.type == "answer" and answer.text == "".join(ANSWER_CHUNKS).strip()
    # The thread comes first, the tool step before the text, the answer last.
    assert kinds[0] == "thread" and kinds.index("progress") < kinds.index("delta")
    assert "Checking your saved profile" in [e.text for e in events if e.type == "progress"]
    assert answer.data["thread_id"] == events[0].data["thread_id"]


def test_commentary_is_not_streamed_as_the_answer(fake, monkeypatch):
    monkeypatch.setenv("FAKE_APPSERVER_MODE", "commentary")
    events = _turn(fake)
    assert [e.text for e in events if e.type == "delta"] == ANSWER_CHUNKS
    assert events[-1].text == "".join(ANSWER_CHUNKS).strip()


def test_same_overrides_as_exec_and_a_private_home_and_scrubbed_env(fake, monkeypatch):
    monkeypatch.setenv("ALPACA_API_SECRET", "x")
    monkeypatch.setenv("GITHUB_TOKEN", "x")
    _turn(fake, model="gpt-5.6-sol")
    (record,) = _records(fake)
    exec_command = agent.build_command("gpt-5.6-sol", fake / "w.sqlite3", turn_env={"WEALTH_TURN_FILE": "f"})
    as_command = appserver.build_command("gpt-5.6-sol", fake / "w.sqlite3", turn_env={"WEALTH_TURN_FILE": "f"})
    assert as_command[as_command.index("app-server") + 1:][4:] == exec_command[exec_command.index("--json") + 1:-3]
    overrides = _overrides(record["argv"])
    assert 'sandbox_mode="read-only"' in overrides and 'approval_policy="never"' in overrides
    assert all(f"features.{name}=false" in overrides for name in agent.DISABLED_FEATURES)
    assert 'model="gpt-5.6-sol"' in overrides
    assert record["thread"]["params"]["sandbox"] == "read-only"
    assert record["thread"]["params"]["approvalPolicy"] == "never"
    # Its own CODEX_HOME: an empty config and the user's login through a link.
    home = Path(record["codex_home"])
    assert home == fake / "wealth-codex"
    assert (home / "auth.json").is_symlink() and os.readlink(home / "auth.json") == str(fake / "user-codex" / "auth.json")
    assert "mcp_servers" not in (home / "config.toml").read_text()
    assert oct(home.stat().st_mode & 0o777) == "0o700"
    assert "ALPACA_API_SECRET" not in record["env"] and "GITHUB_TOKEN" not in record["env"]


def test_consent_file_and_web_search_taint_apply_per_turn(fake, monkeypatch):
    db = fake / "w.sqlite3"
    monkeypatch.setenv("FAKE_APPSERVER_MODE", "ingest")  # the first turn reads a statement's text
    first = _turn(fake, "lee mi estado de cuenta", db_path=db, web_search=True)
    thread = first[-1].data["thread_id"]
    monkeypatch.setenv("FAKE_APPSERVER_MODE", "normal")
    second = _turn(fake, "sí, guárdalo", db_path=db, web_search=True, thread_id=thread,
                   history=[("user", "lee mi estado de cuenta"), ("assistant", "ok")])
    assert second[-1].data == {"thread_id": thread, "resumed": True}
    one, two = _records(fake)
    # Each turn's MCP config names that turn's evidence file, and the server saw that turn's own words in it.
    file_one = [o for o in _overrides(one["argv"]) if o.startswith("mcp_servers.wealth.env.WEALTH_TURN_FILE=")]
    file_two = [o for o in _overrides(two["argv"]) if o.startswith("mcp_servers.wealth.env.WEALTH_TURN_FILE=")]
    assert file_one and file_two and file_one != file_two
    assert one["turn_file"]["message"] == "lee mi estado de cuenta"
    assert two["turn_file"]["message"] == "sí, guárdalo" and "lee mi estado" in two["turn_file"]["recent"]
    assert not Path(json.loads(file_one[0].split("=", 1)[1])).exists()  # deleted when its turn ended
    # Search was live in the first turn; the thread read a file, so the resumed turn has it off.
    assert 'web_search="live"' in _overrides(one["argv"])
    assert 'mcp_servers.wealth.env.WEALTH_TURN_WEB_SEARCH="1"' in _overrides(one["argv"])
    assert 'web_search="disabled"' in _overrides(two["argv"])
    assert not any("WEALTH_TURN_WEB_SEARCH" in o for o in _overrides(two["argv"]))
    assert two["thread"] == {"method": "thread/resume", "params": {
        "threadId": thread, "cwd": str(agent.PROJECT_ROOT), "sandbox": "read-only", "approvalPolicy": "never",
        "excludeTurns": True}}


def test_stop_kills_the_process_group(fake, monkeypatch):
    monkeypatch.setenv("FAKE_APPSERVER_MODE", "slow")
    control = TurnControl()
    seen, outcome = [], {}

    def run():
        try:
            for event in agent.stream_turn("hola", client_id="c1", db_path=fake / "w.sqlite3", brief="",
                                           timeout=30, control=control):
                seen.append(event)
        except AgentError as exc:
            outcome["kind"] = exc.kind

    worker = threading.Thread(target=run)
    worker.start()
    deadline = time.monotonic() + 10
    while not any(e.type == "delta" for e in seen) and time.monotonic() < deadline:
        time.sleep(0.02)
    assert [e.text for e in seen if e.type == "delta"] == ["Hola "]
    started = time.monotonic()
    control.cancel()
    worker.join(10)
    assert outcome == {"kind": "cancelled"} and time.monotonic() - started < 5
    (record,) = _records(fake)
    with pytest.raises(ProcessLookupError):
        os.kill(record["pid"], 0)


def test_failed_turn_is_an_error_not_a_partial_answer(fake, monkeypatch):
    monkeypatch.setenv("FAKE_APPSERVER_MODE", "failed")
    with pytest.raises(AgentError) as caught:
        _turn(fake)
    assert "model overloaded" in str(caught.value)


def test_unknown_thread_starts_fresh(fake):
    events = _turn(fake, thread_id="0199a1b2-c3d4-7e5f-8a9b-exec-session")
    assert "notice" in [e.type for e in events]
    assert events[-1].type == "answer" and events[-1].data["resumed"] is False
    assert [r["thread"]["method"] for r in _records(fake)] == ["thread/start"]


def _exec_lines(answer="From exec."):
    return [json.dumps({"type": "thread.started", "thread_id": "0199a1b2-c3d4-7e5f-8a9b-000000000001"}),
            json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": answer}}),
            json.dumps({"type": "turn.completed"})]


@pytest.mark.parametrize("mode", ["broken", "leaky"])
def test_auto_falls_back_to_exec_and_stays_there(fake, monkeypatch, mode):
    monkeypatch.setenv("FAKE_APPSERVER_MODE", mode)
    monkeypatch.delenv("WEALTH_RUNTIME")
    calls = []

    def fake_exec(command, prompt, timeout, control=None, cwd=None):
        calls.append(command)
        for line in _exec_lines():
            yield ("line", line)
        yield ("exit", 0, "")

    monkeypatch.setattr(agent, "_stream_process", fake_exec)
    events = _turn(fake)
    assert events[-1].text == "From exec." and not [e for e in events if e.type == "delta"]
    assert calls and calls[0][:2] == ["codex", "exec"]
    assert appserver.unavailable_reason()
    if mode == "leaky":
        assert "node_repl" in appserver.unavailable_reason()
    _turn(fake)
    assert len(calls) == 2 and len(_records(fake)) <= 1  # the second turn never tried app-server again


def test_forced_appserver_does_not_fall_back(fake, monkeypatch):
    monkeypatch.setenv("FAKE_APPSERVER_MODE", "broken")
    monkeypatch.setattr(agent, "_stream_process", lambda *a, **k: pytest.fail("exec must not run"))
    with pytest.raises(AgentError):
        _turn(fake)


def test_forced_exec_never_starts_appserver(fake, monkeypatch):
    monkeypatch.setenv("WEALTH_RUNTIME", "exec")

    def fake_exec(command, prompt, timeout, control=None, cwd=None):
        for line in _exec_lines():
            yield ("line", line)
        yield ("exit", 0, "")

    monkeypatch.setattr(agent, "_stream_process", fake_exec)
    assert _turn(fake)[-1].text == "From exec."
    assert _records(fake) == []


def test_private_home_refuses_unsafe_layouts(fake, monkeypatch):
    monkeypatch.setenv("WEALTH_CODEX_HOME", str(fake / "user-codex"))
    with pytest.raises(appserver.RuntimeUnavailable):
        appserver.codex_home()  # never the user's own home, whose config.toml it would overwrite
    monkeypatch.setenv("WEALTH_CODEX_HOME", str(fake / "wealth-codex"))
    home = appserver.codex_home()
    (home / "auth.json").unlink()
    (home / "auth.json").write_text("{}")
    with pytest.raises(appserver.RuntimeUnavailable):
        appserver.codex_home()  # a real file there may hold newer tokens: left alone, not replaced
    (home / "auth.json").unlink()
    (fake / "user-codex" / "auth.json").unlink()
    with pytest.raises(appserver.RuntimeUnavailable):
        appserver.codex_home()  # nothing to share (signed out, or keyring credentials)


def test_config_problem_names_what_differs():
    good = {"mcp_servers": {"wealth": {"enabled": True}, "off": {"enabled": False}}, "sandbox_mode": "read-only",
            "approval_policy": "never", "features": {name: False for name in agent.DISABLED_FEATURES}}
    assert appserver.config_problem(good) is None
    assert "pencil" in appserver.config_problem({**good, "mcp_servers": {"wealth": {}, "pencil": {}}})
    assert "sandbox" in appserver.config_problem({**good, "sandbox_mode": "danger-full-access"})
    assert "multi_agent_v2" in appserver.config_problem(
        {**good, "features": {**good["features"], "multi_agent_v2": {"enabled": True}}})
    assert "notify" in appserver.config_problem({**good, "notify": ["say"]})


def test_translation_keeps_the_exec_shape_for_memory_and_views():
    item = appserver.translate_item({
        "type": "mcpToolCall", "id": "t", "server": "wealth", "tool": "wealth_remember", "status": "completed",
        "arguments": {"facts": []}, "error": None,
        "result": {"content": [], "structuredContent": {"written": [{"key": "goals"}]}}})
    parser = agent._TurnParser()
    events = parser.feed(json.dumps({"type": "item.completed", "item": item}))
    assert [(e.type, dict(e.data)) for e in events] == [("memory", {"keys": ["goals"]})]
    assert appserver.translate_item({"type": "commandExecution", "id": "x"}) is None


def test_web_chat_forwards_deltas_before_the_answer(tmp_path, monkeypatch):
    def fake_stream(message, **kwargs):
        yield agent.TurnEvent("progress", "Writing the answer")
        for chunk in ANSWER_CHUNKS:
            yield agent.TurnEvent("delta", chunk, {"item": "m1"})
        yield agent.TurnEvent("answer", "".join(ANSWER_CHUNKS).strip(), {"thread_id": None})

    monkeypatch.setattr(web, "run_turn", agent.run_turn)
    monkeypatch.setattr(web, "stream_turn", fake_stream)
    chat = web.Chat(tmp_path / "w.sqlite3", "personal")
    turn = chat.start("hola")
    assert turn.wait(10)
    types = [e["type"] for e in turn.events]
    deltas = [e for e in turn.events if e["type"] == "delta"]
    assert [d["text"] for d in deltas] == ANSWER_CHUNKS and {d["item"] for d in deltas} == {"m1"}
    assert types.index("progress") < types.index("delta") and max(
        i for i, t in enumerate(types) if t == "delta") < types.index("answer")
    assert chat.messages[-1]["content"] == "".join(ANSWER_CHUNKS).strip()


def test_evals_capture_both_runtimes(monkeypatch):
    from evals import run as evals_run

    def fake_appserver(command, prompt, timeout, control=None, cwd=None, **thread):
        yield ("line", json.dumps({"type": "item.started", "item": {"type": "web_search"}}))
        yield ("delta", "x", "m1")
        yield ("exit", 0, "")

    monkeypatch.setattr(agent, "_stream_appserver", fake_appserver)
    monkeypatch.setattr(agent, "_stream_process", agent._stream_process)
    evals_run._install_capture()
    evals_run._captured.lines = []
    assert [i[0] for i in agent._stream_appserver(["codex"], "p", 1, None, None, resume_thread=None)] == \
        ["line", "delta", "exit"]
    assert agent.parse_events("\n".join(evals_run._captured.lines)).tools == ("web.search",)
