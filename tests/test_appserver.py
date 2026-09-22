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
    events = _turn(fake, model="gpt-5.6-sol")
    agent._await_reaper(events[-1].data["thread_id"])  # the process is reaped after the answer is out
    (record,) = _records(fake)
    exec_command = agent.build_command("gpt-5.6-sol", fake / "w.sqlite3", turn_env={"WEALTH_TURN_FILE": "f"})
    as_command = appserver.build_command("gpt-5.6-sol", fake / "w.sqlite3", turn_env={"WEALTH_TURN_FILE": "f"})
    assert as_command[as_command.index("app-server") + 1:][4:] == exec_command[exec_command.index("--json") + 1:-1]
    assert "-C" not in exec_command and "project_root_markers=[]" in _overrides(record["argv"])
    # config/read asked for every layer, and the process ran in a scratch directory that is gone now.
    assert record["config_read"] == {"cwd": record["cwd"], "includeLayers": True}
    assert Path(record["cwd"]).name.startswith(appserver.SCRATCH_PREFIX) and not Path(record["cwd"]).exists()
    assert record["thread"]["params"]["cwd"] == record["cwd"]
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
        "threadId": thread, "cwd": two["cwd"], "sandbox": "read-only", "approvalPolicy": "never",
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

    tries = []
    real = agent._stream_appserver
    monkeypatch.setattr(agent, "_stream_appserver", lambda *a, **k: tries.append(1) or real(*a, **k))
    monkeypatch.setattr(agent, "_stream_process", fake_exec)
    events = _turn(fake)
    assert events[-1].text == "From exec." and not [e for e in events if e.type == "delta"]
    assert calls and calls[0][:2] == ["codex", "exec"] and calls[0] and len(tries) == 1
    assert appserver.unavailable_reason()
    if mode == "leaky":
        assert "node_repl" in appserver.unavailable_reason()
    _turn(fake)
    assert len(calls) == 2 and len(tries) == 1  # the second turn never tried app-server again
    # Not for ever: after RETRY_SECONDS the next turn tries app-server again (and, still failing, uses exec).
    reason, since = appserver._unavailable
    monkeypatch.setattr(appserver, "_unavailable", (reason, since - appserver.RETRY_SECONDS - 1))
    assert appserver.use_appserver() and appserver.unavailable_reason() is None
    _turn(fake)
    assert len(calls) == 3 and len(tries) == 2
    assert appserver.unavailable_reason()  # marked again, from now


def test_planted_base_url_is_refused_before_any_thread(fake, monkeypatch):
    """adv3-sec: config/read passed a planted openai_base_url, and the login token went there."""
    monkeypatch.setenv("FAKE_APPSERVER_MODE", "base_url")
    monkeypatch.setattr(agent, "_stream_process", lambda *a, **k: pytest.fail("forced app-server must not fall back"))
    with pytest.raises(AgentError) as caught:
        _turn(fake)
    assert "system config layer" in caught.value.detail
    assert _records(fake) == []  # no thread/start, so no request ever carried the token


def test_stop_reaches_a_grandchild_in_its_own_process_group(fake, monkeypatch):
    """adv3-sec: Codex puts the MCP server in a process group of its own; killpg alone missed it."""
    monkeypatch.setenv("FAKE_APPSERVER_MODE", "grandchild")
    control = TurnControl()
    seen = []

    def run():
        try:
            for event in agent.stream_turn("hola", client_id="c1", db_path=fake / "w.sqlite3", brief="",
                                           timeout=30, control=control):
                seen.append(event)
        except AgentError:
            pass

    worker = threading.Thread(target=run)
    worker.start()
    deadline = time.monotonic() + 10
    while not any(e.type == "delta" for e in seen) and time.monotonic() < deadline:
        time.sleep(0.02)
    (record,) = _records(fake)
    grandchild = record["grandchild"]
    assert os.getpgid(grandchild) != os.getpgid(record["pid"])
    control.cancel()
    worker.join(10)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            os.kill(grandchild, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    with pytest.raises(ProcessLookupError):
        os.kill(grandchild, 0)


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
            "approval_policy": "never", "features": {name: False for name in agent.DISABLED_FEATURES},
            "openai_base_url": None, "chatgpt_base_url": "https://chatgpt.com/backend-api/", "model_providers": {},
            "model_provider": None, "hooks": None, "skills": None, "projects": None,
            "experimental_realtime_ws_base_url": None}
    layers = [{"name": {"type": "sessionFlags"}, "config": {"sandbox_mode": "read-only"}},
              {"name": {"type": "user", "file": "/w/config.toml"}, "config": {}},
              {"name": {"type": "system", "file": "/etc/codex/config.toml"}, "config": {}}]

    def problem(config=None, with_layers=None):
        return appserver.config_problem(good if config is None else {**good, **config},
                                        layers if with_layers is None else with_layers)

    assert problem() is None
    assert "pencil" in problem({"mcp_servers": {"wealth": {}, "pencil": {}}})
    assert "sandbox" in problem({"sandbox_mode": "danger-full-access"})
    assert "multi_agent_v2" in problem({"features": {**good["features"], "multi_agent_v2": {"enabled": True}}})
    assert "notify" in problem({"notify": ["say"]})
    # Where the login token would go, and what else a planted config could bring.
    assert "openai_base_url" in problem({"openai_base_url": "http://127.0.0.1:1/v1"})
    assert "chatgpt_base_url" in problem({"chatgpt_base_url": "https://evil.example/backend-api/"})
    assert "model provider" in problem({"model_providers": {"evil": {"base_url": "http://x"}}})
    assert "model_provider" in problem({"model_provider": "evil"})
    for key in ("hooks", "skills", "projects"):
        assert key in problem({key: {"x": 1}})
    assert "experimental_realtime_ws_base_url" in problem({"experimental_realtime_ws_base_url": "wss://x"})
    # Only Wealth's own -c flags may set anything: any other non-empty layer (system, MDM, project) refuses.
    assert "no config layers" in problem(with_layers=[])
    assert "system config layer /etc/codex/config.toml" in problem(
        with_layers=[layers[0], layers[1], {**layers[2], "config": {"model": "x"}}])
    assert "project" in problem(with_layers=[*layers, {"name": {"type": "project", "dotCodexFolder": "/p/.codex"},
                                                       "config": {"notify": ["x"]}, "disabledReason": "untrusted"}])
    assert "mdm" in problem(with_layers=[*layers, {"name": {"type": "mdm", "domain": "d", "key": "k"},
                                                   "config": {"openai_base_url": "http://x"}}])


def _fake_user_home(tmp_path, monkeypatch):
    """A fake $HOME with its own ~/.codex (config and login); CODEX_HOME unset, so that is the user's home."""
    home = tmp_path / "home"
    user = home / ".codex"
    user.mkdir(parents=True)
    (user / "auth.json").write_text('{"OPENAI_API_KEY": "sk-fake"}')
    (user / "config.toml").write_text('model = "mine"\nnotify = ["/bin/echo"]\n')
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    return home, user


@pytest.mark.parametrize("variant", ["{u}", "{u}/", "{u}/../.codex", "{h}/.CODEX", "{u}/sessions", "{u}/../.codex/x",
                                     "{h}", "{h}/.CoDeX/../.codex"])
def test_the_users_own_codex_home_is_refused_in_any_spelling_before_any_write(tmp_path, monkeypatch, variant):
    """adv3-sec: the guard compared text, so ~/.codex/../.codex and ~/.CODEX rewrote the user's config.toml."""
    home, user = _fake_user_home(tmp_path, monkeypatch)
    before = sorted(p.relative_to(home) for p in home.rglob("*"))
    monkeypatch.setenv("WEALTH_CODEX_HOME", variant.format(u=user, h=home))
    with pytest.raises(appserver.RuntimeUnavailable, match="user's own"):
        appserver.codex_home()
    assert (user / "config.toml").read_text() == 'model = "mine"\nnotify = ["/bin/echo"]\n'
    assert not (user / "auth.json").is_symlink()
    assert sorted(p.relative_to(home) for p in home.rglob("*")) == before  # nothing written anywhere


def test_the_default_home_is_used_and_resolved(tmp_path, monkeypatch):
    home, user = _fake_user_home(tmp_path, monkeypatch)
    monkeypatch.delenv("WEALTH_CODEX_HOME", raising=False)
    made = appserver.codex_home()
    assert made == (home / ".local/share/wealth-harness/codex-home").resolve()
    assert os.readlink(made / "auth.json") == str(user / "auth.json") and (made / appserver.HOME_MARKER).is_file()
    # Through a symlink, Codex gets the resolved path.
    (tmp_path / "alias").symlink_to(made)
    monkeypatch.setenv("WEALTH_CODEX_HOME", str(tmp_path / "alias"))
    assert appserver.codex_home() == made


def test_what_codex_would_load_from_the_private_home_is_removed_each_time(fake, monkeypatch):
    """adv3-sec: AGENTS.md and skills in the Wealth home reached the model."""
    home = appserver.codex_home()
    outside = fake / "keep"
    outside.mkdir()
    (outside / "SKILL.md").write_text("mine")
    (home / "AGENTS.md").write_text("PLANTED")
    (home / "AGENTS.override.md").write_text("PLANTED")
    (home / "skills" / "planted").mkdir(parents=True)
    (home / "skills" / "planted" / "SKILL.md").write_text("PLANTED")
    (home / "hooks").symlink_to(outside)  # a link is removed, never followed
    (home / "rules").mkdir()
    (home / "rules" / "default.rules").write_text("PLANTED")
    (home / "sessions").mkdir()
    (home / "sessions" / "keep.jsonl").write_text("{}")
    _turn(fake)
    for name in ("AGENTS.md", "AGENTS.override.md", "skills", "hooks", "rules"):
        assert not os.path.lexists(home / name), name
    assert (outside / "SKILL.md").read_text() == "mine" and (home / "sessions" / "keep.jsonl").exists()


def test_a_directory_that_is_not_wealths_is_never_used(fake, monkeypatch):
    other = fake / "someones-project"
    other.mkdir()
    (other / "AGENTS.md").write_text("theirs")
    monkeypatch.setenv("WEALTH_CODEX_HOME", str(other))
    with pytest.raises(appserver.RuntimeUnavailable, match="not Wealth's"):
        appserver.codex_home()
    assert (other / "AGENTS.md").read_text() == "theirs" and sorted(os.listdir(other)) == ["AGENTS.md"]
    # A home made before the marker existed (its config.toml is Wealth's) is still accepted, and marked.
    legacy = fake / "legacy"
    legacy.mkdir(mode=0o700)
    (legacy / "config.toml").write_text(appserver._CONFIG_TEXT)
    monkeypatch.setenv("WEALTH_CODEX_HOME", str(legacy))
    assert appserver.codex_home() == legacy.resolve() and (legacy / appserver.HOME_MARKER).is_file()


def test_a_home_outside_home_needs_private_parents(fake, monkeypatch):
    """adv3-sec: a WEALTH_CODEX_HOME under a directory others can write could be swapped for a link."""
    shared = fake / "shared"
    shared.mkdir()
    shared.chmod(0o777)
    monkeypatch.setenv("WEALTH_CODEX_HOME", str(shared / "codex-home"))
    try:
        with pytest.raises(appserver.RuntimeUnavailable, match="other users"):
            appserver.codex_home()
        assert not (shared / "codex-home").exists()
        monkeypatch.setenv("XDG_DATA_HOME", str(shared))
        monkeypatch.delenv("WEALTH_CODEX_HOME")
        with pytest.raises(appserver.RuntimeUnavailable, match="other users"):
            appserver.codex_home()
        assert list(shared.iterdir()) == []
    finally:
        shared.chmod(0o700)
    monkeypatch.setenv("WEALTH_CODEX_HOME", "relative/home")
    with pytest.raises(appserver.RuntimeUnavailable, match="absolute"):
        appserver.codex_home()


_REPORT = """
import json, os, sys
print(json.dumps({"codex_home": os.environ.get("CODEX_HOME"), "cwd": os.getcwd(), "listing": sorted(os.listdir("."))}))
"""


def test_exec_runs_with_the_private_home_in_an_empty_scratch_directory(fake, monkeypatch):
    """adv3-sec: exec used ~/.codex (AGENTS.md, skills) and the project root (.codex/skills) as its cwd."""
    home = appserver.codex_home()
    (home / "AGENTS.md").write_text("PLANTED")
    items = list(agent._stream_process([sys.executable, "-c", _REPORT], "", 20, None, agent.ISOLATED))
    seen = json.loads(items[0][1])
    assert items[-1][:2] == ("exit", 0)
    assert seen["codex_home"] == str(home) and not (home / "AGENTS.md").exists()
    assert Path(seen["cwd"]).name.startswith(appserver.SCRATCH_PREFIX) and seen["listing"] == []
    assert not Path(seen["cwd"]).exists()
    assert os.readlink(home / "auth.json") == str(fake / "user-codex" / "auth.json")
    # Signed out: fails closed as not logged in, instead of falling back to the user's own home.
    (fake / "user-codex" / "auth.json").unlink()
    with pytest.raises(AgentError) as caught:
        list(agent._stream_process([sys.executable, "-c", _REPORT], "", 20, None, agent.ISOLATED))
    assert caught.value.kind == "not_logged_in"
    monkeypatch.setenv("CODEX_API_KEY", "sk-fake")  # API-key sign-in needs no auth.json
    items = list(agent._stream_process([sys.executable, "-c", _REPORT], "", 20, None, agent.ISOLATED))
    assert json.loads(items[0][1])["codex_home"] == str(home)


def test_every_exec_turn_and_the_memory_step_are_isolated(fake, monkeypatch):
    monkeypatch.setenv("WEALTH_RUNTIME", "exec")
    cwds = []

    def fake_exec(command, prompt, timeout, control=None, cwd=None):
        cwds.append(cwd)
        assert "-C" not in command
        yield ("line", json.dumps({"type": "item.completed", "item": {
            "type": "mcp_tool_call", "server": "wealth", "tool": "wealth_remember", "status": "completed",
            "arguments": {}, "result": {"structured_content": {"written": [{"key": "goals"}]}}}}))
        for line in _exec_lines():
            yield ("line", line)
        yield ("exit", 0, "")

    monkeypatch.setattr(agent, "_stream_process", fake_exec)
    _turn(fake)
    assert agent.remember_exchange("hola", "ok", client_id="c1", db_path=fake / "w.sqlite3") == ["goals"]
    assert cwds == [agent.ISOLATED, agent.ISOLATED]


_SPAWNER = """
import subprocess, sys, time
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True)
print(child.pid, flush=True)
time.sleep(60)
"""


def test_stop_reaches_an_exec_grandchild_in_its_own_process_group():
    control = TurnControl()
    stream = agent._stream_process([sys.executable, "-c", _SPAWNER], "", 30, control)
    kind, line = next(stream)
    grandchild = int(line)
    assert os.getpgid(grandchild) != os.getpgid(os.getpid())
    control.cancel()
    with pytest.raises(AgentError):
        list(stream)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            os.kill(grandchild, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    with pytest.raises(ProcessLookupError):
        os.kill(grandchild, 0)


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
    assert [i[0] for i in agent._stream_appserver(["codex"], "p", 1, None, resume_thread=None)] == \
        ["line", "delta", "exit"]
    assert agent.parse_events("\n".join(evals_run._captured.lines)).tools == ("web.search",)


def test_a_keyring_login_gets_an_actionable_message_and_a_shared_home_opt_in(tmp_path, monkeypatch):
    from wealth import appserver
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.setenv(appserver.HOME_ENV, str(tmp_path / "wealth-home"))
    (tmp_path / ".codex").mkdir()
    with pytest.raises(appserver.RuntimeUnavailable, match="keyring"):
        appserver.codex_home()
    monkeypatch.setenv(appserver.SHARED_HOME_ENV, "1")
    assert appserver.shared_home() and appserver.runtime_setting() == "exec"
