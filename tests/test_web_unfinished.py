"""Unfinished turns keep their record: Stop keeps the streamed words, a failure keeps the message; the favicon."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from urllib.request import urlopen

from wealth import agent, web
from wealth.agent import AgentError, TurnEvent
from wealth.store import WealthStore

from test_web_streaming import _events, _post, serving

PAGE = (Path(web.__file__).with_name("chat.html")).read_text(encoding="utf-8")


def _streaming(monkeypatch, fn):
    monkeypatch.setattr(web, "run_turn", agent.run_turn)
    monkeypatch.setattr(web, "stream_turn", fn)


def _stoppable(started):
    """Writes part of an answer (with one aside that is started over), then waits to be stopped."""

    def stream(message, *, control, **kwargs):
        yield TurnEvent("thread", data={"thread_id": "0199-thread-stop"})
        if message == "again":
            yield TurnEvent("answer", "The full plan.", {"thread_id": "0199-thread-stop"})
            return
        yield TurnEvent("delta", "Let me check", {"item": "aside"})
        yield TurnEvent("delta", "Pay the card first: ", {"item": "msg"})
        yield TurnEvent("delta", "it costs 45%.", {"item": "msg"})
        started.set()
        for _ in range(250):
            if control.cancelled:
                yield TurnEvent("delta", " never shown", {"item": "msg"})
                raise AgentError(None, "cancelled")
            time.sleep(0.02)
        yield TurnEvent("answer", "late")

    return stream


def test_stop_keeps_the_streamed_answer_marked_stopped_and_it_survives_a_reload(tmp_path, monkeypatch):
    started = threading.Event()
    seen_history = []
    stream = _stoppable(started)

    def recording(message, **kwargs):
        seen_history.append(list(kwargs.get("history") or []))
        yield from stream(message, **kwargs)

    _streaming(monkeypatch, recording)
    db = tmp_path / "w.sqlite3"
    chat = web.Chat(db, "personal")
    with serving(chat) as (base, _):
        turn = json.load(_post(base, "/api/turns", chat.token, b'{"message":"plan my debts"}'))["turn"]
        assert started.wait(5)
        assert _post(base, f"/api/turns/{turn['id']}/cancel", chat.token).status == 202
        events = _events(base, turn["id"], chat.token)
        error = events[-2]
        assert error["type"] == "error" and error["kind"] == "cancelled"
        # The words already on screen come back as the saved message, not "Stopped…" in their place.
        assert error["stopped"]["content"] == "Pay the card first: it costs 45%."
        assert error["stopped"]["status"] == "stopped" and error["stopped"]["role"] == "assistant"
        state = json.load(urlopen(base + "/api/state", timeout=5))
        assert [(m["role"], m["content"], m.get("status")) for m in state["messages"]] == [
            ("user", "plan my debts", None), ("assistant", "Pay the card first: it costs 45%.", "stopped")]
        assert state["turn"] is None  # shown once, from the conversation
        assert chat.thread_id == "0199-thread-stop"

        # Ask again still works, and the next turn sees the stopped exchange as it was.
        again = json.load(_post(base, "/api/turns", chat.token, b'{"message":"again"}'))["turn"]
        assert _events(base, again["id"], chat.token)[-1]["status"] == "done"
        assert seen_history[-1] == [("user", "plan my debts"), ("assistant", "Pay the card first: it costs 45%.")]

    restarted = web.Chat(db, "personal")
    assert [(m["content"], m.get("status")) for m in restarted.state()["messages"]] == [
        ("plan my debts", None), ("Pay the card first: it costs 45%.", "stopped"), ("again", None),
        ("The full plan.", None)]
    assert restarted.thread_id == "0199-thread-stop"


def test_a_failed_turn_is_logged_and_its_message_kept_for_retry(tmp_path, monkeypatch, capsys):
    calls = []

    def failing(message, **kwargs):
        calls.append(list(kwargs.get("history") or []))
        yield TurnEvent("thread", data={"thread_id": "0199-thread-fail"})
        if len(calls) == 1:
            raise AgentError(None, "other", "app-server: turn/failed code=-32603 RFC GODE561231GR8")
        yield TurnEvent("answer", "Answered on retry.", {"thread_id": "0199-thread-fail"})

    _streaming(monkeypatch, failing)
    db = tmp_path / "w.sqlite3"
    chat = web.Chat(db, "personal")
    with serving(chat) as (base, _):
        turn = json.load(_post(base, "/api/turns", chat.token, b'{"message":"my secret plan for 450000"}'))["turn"]
        events = _events(base, turn["id"], chat.token)
        assert events[-2]["type"] == "error" and events[-2]["kind"] == "other"
        assert events[-2]["user_id"]
        log = capsys.readouterr().err
        assert f"turn {turn['id']} failed" in log and "kind=other" in log and "turn/failed code=-32603" in log
        assert "secret plan" not in log and "450000" not in log and "GODE561231GR8" not in log

        state = json.load(urlopen(base + "/api/state", timeout=5))
        assert [(m["role"], m["content"], m.get("status")) for m in state["messages"]] == [
            ("user", "my secret plan for 450000", "failed")]
        assert state["turn"] is None
        # A reload (a new server on the same database) still shows the message, marked failed.
        assert [m.get("status") for m in web.Chat(db, "personal").state()["messages"]] == ["failed"]

        retry = json.load(_post(base, "/api/turns", chat.token, b'{"message":"my secret plan for 450000"}'))["turn"]
        assert _events(base, retry["id"], chat.token)[-1]["status"] == "done"
        assert calls[-1] == []  # the unanswered message is not replayed as part of the exchange
    assert [(m["role"], m.get("status")) for m in web.Chat(db, "personal").messages] == [
        ("user", "failed"), ("user", None), ("assistant", None)]


def test_codex_without_an_answer_is_logged_too(tmp_path, monkeypatch, capsys):
    _streaming(monkeypatch, lambda message, **kw: iter([TurnEvent("progress", "Thinking it through")]))
    chat = web.Chat(tmp_path / "w.sqlite3", "personal")
    turn = chat.start("hello there")
    turn.wait(5)
    log = capsys.readouterr().err
    assert "kind=other" in log and "without an assistant response" in log and "hello there" not in log
    assert [m.get("status") for m in chat.messages] == ["failed"]


def test_an_internal_turn_that_fails_keeps_nothing(tmp_path, monkeypatch):
    def failing(message, **kwargs):
        raise AgentError(None, "timeout")
        yield  # pragma: no cover

    _streaming(monkeypatch, failing)
    chat = web.Chat(tmp_path / "w.sqlite3", "personal")
    turn = chat.start("Setup just finished.", internal=True)
    turn.wait(5)
    assert chat.messages == [] and chat.state()["turn"]["error"]["kind"] == "timeout"


def test_older_databases_gain_the_status_column(tmp_path):
    db = tmp_path / "w.sqlite3"
    with WealthStore(db) as store:
        store.create_client("personal", "Personal")
        conversation = store.start_conversation("personal")
        store.append_messages("personal", conversation, [{"id": "m1", "role": "user", "content": "hola"}])
    connection = sqlite3.connect(db)
    connection.execute("ALTER TABLE conversation_messages DROP COLUMN status")
    connection.commit()
    connection.close()
    with WealthStore(db) as store:
        store.append_messages("personal", conversation,
                              [{"id": "m2", "role": "assistant", "content": "Pay", "status": "stopped"}])
        messages = store.conversation("personal")["messages"]
    assert [(m["id"], m.get("status")) for m in messages] == [("m1", None), ("m2", "stopped")]


def test_favicon_is_a_vermilion_dot_and_never_a_404(tmp_path, monkeypatch):
    _streaming(monkeypatch, lambda message, **kw: iter([TurnEvent("answer", "ok")]))
    chat = web.Chat(tmp_path / "w.sqlite3", "personal")
    with serving(chat) as (base, _):
        for path in ("/favicon.ico", "/favicon.svg"):
            with urlopen(base + path, timeout=5) as response:
                assert response.status == 200
                assert response.headers.get_content_type() == "image/svg+xml"
                body = response.read()
            assert b"#C84335" in body and b"#FBF8F2" in body and b"<script" not in body
    assert '<link rel="icon" type="image/svg+xml" href="/favicon.svg">' in PAGE


def test_page_keeps_stopped_words_offers_retry_after_reload_and_drops_answered_cards():
    # Stop: the saved partial answer is drawn with a quiet Stopped line and Ask again.
    assert "const stopped = event.kind === 'cancelled' && event.stopped;" in PAGE
    assert "stopped: true, retry: retryFor(turn)" in PAGE
    assert "function stoppedLine(lang, retry)" in PAGE and "stopped: 'Detenido'" in PAGE and "stopped: 'Stopped'" in PAGE
    # History: failed and stopped messages come back with their way forward.
    assert "item.status === 'failed'" in PAGE and "item.status === 'stopped'" in PAGE
    assert "unanswered: 'Este mensaje no se respondió.'" in PAGE
    # Each turn's memory step (and its end) asks which setup card is next and removes the answered one.
    assert "turn.memoryEvent = event;\n        refreshSetup();" in PAGE
    assert "finish(turn);\n        refreshSetup();" in PAGE
    assert "if (data.card && data.card.step === open.dataset.step) return;" in PAGE
