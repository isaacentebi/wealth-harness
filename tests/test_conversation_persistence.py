"""The chat conversation survives a server restart and travels with export and forget."""
from __future__ import annotations

import sqlite3

import pytest

from wealth import agent, web
from wealth.agent import TurnEvent
from wealth.store import WealthStore


def _stream(thread_ids):
    """A fake Codex stream that records the thread it resumed and answers on a fixed thread."""

    def fake_stream(message, **kwargs):
        thread_ids.append(kwargs.get("thread_id"))
        yield TurnEvent("thread", data={"thread_id": "0199-thread-a"})
        yield TurnEvent("memory", data={"keys": ["goals"]})
        yield TurnEvent("answer", f"Answer to {message}", {"thread_id": "0199-thread-a"})

    return fake_stream


def _stub(monkeypatch, thread_ids):
    monkeypatch.setattr(web, "run_turn", agent.run_turn)
    fake = _stream(thread_ids)
    monkeypatch.setattr(web, "stream_turn", fake)
    monkeypatch.setattr(agent, "stream_turn", fake)  # the real runtime: memory is saved after the reply
    monkeypatch.setattr(web, "remember_exchange", lambda *a, **k: ["income.salary"])


def _ask(chat, message):
    turn = chat.start(message)
    turn.wait(10)
    chat.memory_thread and chat.memory_thread.join(10)
    return turn


def test_restart_shows_the_same_messages_and_continues_the_same_thread(tmp_path, monkeypatch):
    threads: list = []
    _stub(monkeypatch, threads)
    db = tmp_path / "w.sqlite3"
    first = web.Chat(db, "personal")
    _ask(first, "hola, gano 50 mil")  # a bare "hola" skips the memory step
    assert first.thread_id == "0199-thread-a"
    before = first.state()["messages"]
    assert [m["role"] for m in before] == ["user", "assistant"]
    # The memory receipts arrived after the reply was saved, and they persist too.
    assert before[1]["memory"] == [{"key": "goals"}, {"key": "income.salary"}]

    restarted = web.Chat(db, "personal")  # a new server process on the same database
    assert restarted.state()["messages"] == before
    assert restarted.thread_id == "0199-thread-a"
    assert restarted.conversation_id == first.conversation_id
    _ask(restarted, "y ahora?")
    assert threads == [None, "0199-thread-a"]  # the second turn resumed the stored thread
    assert [m["content"] for m in web.Chat(db, "personal").messages] == [
        "hola, gano 50 mil", "Answer to hola, gano 50 mil", "y ahora?", "Answer to y ahora?"]


def test_new_conversation_keeps_history_and_starts_fresh(tmp_path, monkeypatch):
    threads: list = []
    _stub(monkeypatch, threads)
    db = tmp_path / "w.sqlite3"
    chat = web.Chat(db, "personal")
    _ask(chat, "hola")
    old = chat.conversation_id
    chat.reset()
    assert chat.messages == [] and chat.thread_id is None and chat.conversation_id != old
    restarted = web.Chat(db, "personal")
    assert restarted.messages == [] and restarted.thread_id is None
    assert restarted.conversation_id == chat.conversation_id
    exported = web.WealthService(db).inspect("personal", detail="export")["conversations"]
    assert [c["id"] for c in exported] == [old, chat.conversation_id]
    assert [m["content"] for m in exported[0]["messages"]] == ["hola", "Answer to hola"]
    assert exported[0]["thread_id"] == "0199-thread-a" and exported[1]["messages"] == []


def test_reveal_turns_store_only_the_reply(tmp_path, monkeypatch):
    threads: list = []
    _stub(monkeypatch, threads)
    db = tmp_path / "w.sqlite3"
    chat = web.Chat(db, "personal")
    turn = chat.start("Setup just finished. This request comes from Wealth", internal=True)
    turn.wait(10)
    stored = web.Chat(db, "personal").messages
    assert [m["role"] for m in stored] == ["assistant"]


def test_stored_messages_are_redacted(tmp_path, monkeypatch):
    threads: list = []
    _stub(monkeypatch, threads)
    db = tmp_path / "w.sqlite3"
    chat = web.Chat(db, "personal")
    secret = ("Mi CLABE es 012180015555555555, RFC GODE561231GR8, CURP GODE561231HDFRRN09, "
              "SSN 123-45-6789 y tarjeta 4111 1111 1111 1111. Gano $85,000 al mes.")
    _ask(chat, secret)
    content = web.Chat(db, "personal").messages[0]["content"]
    for raw in ("012180015555555555", "GODE561231GR8", "GODE561231HDFRRN09", "123-45-6789", "4111 1111 1111 1111"):
        assert raw not in content
    assert "****5555" in content and "****1111" in content and "$85,000" in content
    with sqlite3.connect(db) as connection:
        stored = " ".join(row[0] for row in connection.execute("SELECT content FROM conversation_messages"))
    assert "012180015555555555" not in stored and "123-45-6789" not in stored


def test_only_the_last_hundred_messages_load(tmp_path):
    db = tmp_path / "w.sqlite3"
    with WealthStore(db) as store:
        store.create_client("personal", "personal")
        conv = store.start_conversation("personal")
        store.append_messages("personal", conv, [
            {"id": f"m{i}", "role": "user" if i % 2 == 0 else "assistant", "content": str(i)} for i in range(130)
        ], thread_id="t1")
    chat = web.Chat(db, "personal")
    assert len(chat.messages) == 100 and chat.messages[0]["content"] == "30" and chat.messages[-1]["content"] == "129"
    assert chat.thread_id == "t1"


def test_messages_are_append_only_and_forget_deletes_them(tmp_path):
    db = tmp_path / "w.sqlite3"
    with WealthStore(db) as store:
        store.create_client("a", "A")
        store.create_client("b", "B")
        conv = store.start_conversation("a")
        store.append_messages("a", conv, [{"id": "m1", "role": "user", "content": "hola"}])
        store.append_messages("b", store.start_conversation("b"), [{"id": "m2", "role": "user", "content": "hi"}])
        store.set_message_memory("a", "m1", [{"key": "goals"}])  # the one column that is filled in later
        assert store.conversation("a")["messages"][0]["memory"] == [{"key": "goals"}]
    with sqlite3.connect(db) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute("UPDATE conversation_messages SET content = 'x'")
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute("DELETE FROM conversation_messages")
    web.WealthService(db).forget("a", "a")
    with sqlite3.connect(db) as connection:
        assert connection.execute("SELECT client_id FROM conversation_messages").fetchall() == [("b",)]
        assert connection.execute("SELECT client_id FROM conversations").fetchall() == [("b",)]


def test_an_existing_database_gains_the_conversation_tables(tmp_path):
    db = tmp_path / "w.sqlite3"
    with WealthStore(db) as store:
        store.create_client("a", "A")
    with sqlite3.connect(db) as connection:
        connection.executescript(
            "DROP TRIGGER conversation_messages_append_only_update; "
            "DROP TRIGGER conversation_messages_append_only_delete; "
            "DROP TABLE conversation_messages; DROP TABLE conversations;")
    with WealthStore(db) as store:
        assert store.conversation("a") == {"id": None, "thread_id": None, "messages": []}
        assert store.export_client("a")["conversations"] == []
