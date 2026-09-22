"""Chat latency: the next turn never waits on a memory save, answers leave at turn.completed,
discovery stays compact, and the fast tier is opt-in."""
from __future__ import annotations

import asyncio
import json
import threading
import time

import pytest

from wealth import agent, web
from wealth.agent import TurnEvent


def _fake_runtime(monkeypatch, remember):
    def fake_stream(message, **kwargs):
        assert kwargs["defer_memory"] is True
        yield TurnEvent("answer", f"Answer to {message}")

    monkeypatch.setattr(agent, "stream_turn", fake_stream)
    monkeypatch.setattr(web, "stream_turn", fake_stream)
    monkeypatch.setattr(web, "remember_exchange", remember)


def _wait_for(condition, seconds=5.0):
    deadline = time.monotonic() + seconds
    while not condition():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.01)


def test_second_turn_starts_while_the_first_memory_step_is_still_running(tmp_path, monkeypatch):
    release = threading.Event()
    log: list[tuple[str, str]] = []

    def slow_remember(message, answer, **kwargs):
        log.append(("remember-start", message))
        assert release.wait(10)
        log.append(("remember-end", message))
        return ["income.salary"]

    _fake_runtime(monkeypatch, slow_remember)
    chat = web.Chat(tmp_path / "w.sqlite3", "personal")
    first = chat.start("gano 85 mil al mes")
    _wait_for(lambda: ("remember-start", "gano 85 mil al mes") in log)
    assert first.answer == "Answer to gano 85 mil al mes" and not first.finished  # saving, not blocking

    second = chat.start("gasto 30 mil")
    _wait_for(lambda: second.answer is not None)  # answered while the first save is still held
    assert ("remember-end", "gano 85 mil al mes") not in log
    assert ("remember-start", "gasto 30 mil") not in log  # saves are serial: the second waits its turn

    release.set()
    chat.memory_thread.join(10)
    assert log == [("remember-start", "gano 85 mil al mes"), ("remember-end", "gano 85 mil al mes"),
                   ("remember-start", "gasto 30 mil"), ("remember-end", "gasto 30 mil")]
    assert first.finished and second.finished
    assert chat.messages[1]["memory"] == [{"key": "income.salary"}]


def test_a_bare_greeting_or_thanks_skips_the_memory_step(tmp_path, monkeypatch):
    calls: list[str] = []
    _fake_runtime(monkeypatch, lambda message, answer, **kwargs: calls.append(message) or [])
    chat = web.Chat(tmp_path / "w.sqlite3", "personal")
    for message in ("hola", "¡Muchas gracias!", "gano 40 mil", "ok"):
        chat.start(message).wait(5)
        chat.memory_thread and chat.memory_thread.join(5)
    assert calls == ["gano 40 mil", "ok"]  # "ok" can accept advice, so it is never skipped


@pytest.mark.parametrize("message, small", [
    ("hola", True), ("Hola, buenos días", True), ("thanks!", True), ("¿qué tal?", True),
    ("hola, gano 50 mil", False), ("gracias, ya pagué 3000", False), ("sí", False), ("no", False),
    ("ok", False), ("hola, me casé", False), ("", False),
])
def test_small_talk_heuristic_is_conservative(message, small):
    assert web.is_small_talk(message) is small


def _proc_lines(*events):
    return [("line", json.dumps(event)) for event in events]


def test_answer_is_yielded_at_turn_completed_and_the_process_is_reaped_later(tmp_path, monkeypatch):
    exit_allowed = threading.Event()
    drained = threading.Event()

    def fake(command, prompt, timeout, control=None, cwd=None):
        try:
            yield from _proc_lines({"type": "thread.started", "thread_id": "0199-early-answer"},
                                   {"type": "item.completed", "item": {"type": "agent_message", "text": "Listo."}},
                                   {"type": "turn.completed"})
            assert exit_allowed.wait(10)  # Codex is still shutting down
            yield ("exit", 0, "")
        finally:
            drained.set()

    monkeypatch.setattr(agent, "_stream_process", fake)
    started = time.monotonic()
    events = list(agent.stream_turn("q", client_id="c", db_path=tmp_path / "w.sqlite3"))
    assert time.monotonic() - started < 2
    assert events[-1].type == "answer" and events[-1].text == "Listo."
    assert not drained.is_set()  # the process is still being reaped in the background
    exit_allowed.set()
    assert drained.wait(5)


def test_resuming_waits_for_the_previous_process_of_that_thread(tmp_path, monkeypatch):
    order: list[str] = []
    exit_allowed = threading.Event()

    def fake(command, prompt, timeout, control=None, cwd=None):
        resumed = "resume" in command
        order.append("resume" if resumed else "first")
        yield from _proc_lines({"type": "thread.started", "thread_id": "0199-reap-thread"},
                               {"type": "item.completed", "item": {"type": "agent_message", "text": "ok"}},
                               {"type": "turn.completed"})
        if not resumed:
            assert exit_allowed.wait(10)
            order.append("first-exited")
        yield ("exit", 0, "")

    monkeypatch.setattr(agent, "_stream_process", fake)
    list(agent.stream_turn("a", client_id="c", db_path=tmp_path / "w.sqlite3"))
    threading.Timer(0.2, exit_allowed.set).start()
    list(agent.stream_turn("b", client_id="c", db_path=tmp_path / "w.sqlite3", thread_id="0199-reap-thread"))
    assert order == ["first", "first-exited", "resume"]


def test_the_turn_builds_the_situation_once(tmp_path, monkeypatch):
    from wealth.service import WealthService
    builds = []
    original = WealthService.situation

    def counting(self, *args, **kwargs):
        builds.append(1)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(WealthService, "situation", counting)
    service = WealthService(tmp_path / "w.sqlite3")
    service.create("ana", "Ana")
    brief, revision, views = agent.situation_context(service.db_path, "ana", "hola")
    assert len(builds) == 1 and isinstance(brief, str) and isinstance(views, list)


def test_service_tier_is_opt_in(tmp_path, monkeypatch):
    monkeypatch.delenv("WEALTH_SERVICE_TIER", raising=False)
    agent.set_service_tier(None)
    assert not any(part.startswith("service_tier=") for part in agent.build_command("sol", tmp_path / "db"))
    monkeypatch.setenv("WEALTH_SERVICE_TIER", "fast")
    assert 'service_tier="fast"' in agent.build_command("sol", tmp_path / "db")
    monkeypatch.setenv("WEALTH_SERVICE_TIER", "turbo")  # unknown values keep the default
    assert agent.service_tier() is None
    monkeypatch.delenv("WEALTH_SERVICE_TIER")
    try:
        agent.set_service_tier("fast")
        assert 'service_tier="fast"' in agent.build_command("sol", tmp_path / "db", resume_thread="0199-abcdefgh")
        with pytest.raises(ValueError):
            agent.set_service_tier("turbo")
    finally:
        agent.set_service_tier(None)


def test_chat_instructions_name_the_tools_and_inline_the_mexico_facts():
    text = agent.conversation_instructions().read_text(encoding="utf-8")
    assert "mcp__wealth__wealth_run" in text and "web__run" in text
    assert "no file, shell or resource tool" in text and "Promise.all" in text
    assert "mexico-investing-facts" not in text
    flat = " ".join(text.split())
    assert "Art. 129" in flat and "US$60k" in flat and "contested" in flat and "CSPX" in flat


def test_discovery_is_compact_by_default_in_a_conversation_turn(tmp_path):
    pytest.importorskip("mcp")
    from wealth import server as server_module
    from wealth.service import WealthService
    WealthService(tmp_path / "w.sqlite3").create("ana", "Ana")
    chat_tools = agent.WEALTH_TOOLS - {"wealth_remember"}
    chat = server_module.build_server(str(tmp_path / "w.sqlite3"), tools=chat_tools)
    host = server_module.build_server(str(tmp_path / "w.sqlite3"))

    def call(server, arguments):
        return asyncio.run(server.call_tool("wealth_context", arguments)).structured_content

    schema = call(chat, {"intent": "order_ticket"})
    assert list(schema["tasks"]) == ["order_ticket"] and "example" in schema["tasks"]["order_ticket"]
    assert "fact_contract" not in schema and "connectors" not in schema and len(json.dumps(schema)) < 4_000
    assert "fact_contract" in call(chat, {"intent": "order_ticket", "detail": "full"})
    # Without wealth_remember the fact contract is dead weight; a host that saves facts reads it on purpose
    # (intent=remember), and a task read carries a one-line pointer instead of ~16k characters.
    assert "fact_contract" not in call(chat, {"client_id": "ana", "intent": "plan"})
    assert "fact_contract" not in call(chat, {"client_id": "ana", "intent": "remember"})
    plan = call(host, {"client_id": "ana", "intent": "plan"})
    assert "fact_contract" not in plan and "intent=remember" in plan["fact_contract_pointer"]
    assert "fact_contract" in call(host, {"client_id": "ana", "intent": "remember"})
    assert "fact_contract" in call(host, {"client_id": "ana", "intent": "plan", "detail": "full"})
    description = {t.name: t for t in asyncio.run(chat.list_tools())}["wealth_run"].description
    assert "order_ticket {orders" in description and "research {symbol" in description
