"""Browser chat server: streaming turns, cancellation, errors, uploads, headers."""
from __future__ import annotations

import http.client
import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from wealth import agent, web
from wealth.agent import AgentError, TurnEvent
from wealth.store import StoreError


@contextmanager
def serving(chat):
    server = web.create_server(chat, 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", server
    finally:
        turn = chat.turn
        if turn is not None and not turn.finished:
            turn.control.cancel()
        server.shutdown()
        server.server_close()
        thread.join()


def _post(base, path, token, body=b"{}", content_type="application/json", headers=None):
    request = Request(base + path, data=body, method="POST",
                      headers={"Content-Type": content_type, "X-Wealth-Token": token, **(headers or {})})
    return urlopen(request, timeout=10)


def _events(base, turn_id, token):
    request = Request(f"{base}/api/turns/{turn_id}/events", headers={"X-Wealth-Token": token})
    with urlopen(request, timeout=10) as response:
        assert response.headers.get_content_type() == "text/event-stream"
        events = []
        for block in response.read().decode().split("\n\n"):
            data = [line[6:] for line in block.splitlines() if line.startswith("data: ")]
            if data:
                events.append(json.loads(data[0]))
        return events


def _streaming(monkeypatch, fn):
    """Route Chat through a fake event stream instead of Codex."""

    monkeypatch.setattr(web, "run_turn", agent.run_turn)
    monkeypatch.setattr(web, "stream_turn", fn)


def test_sse_stream_carries_progress_memory_and_answer(tmp_path, monkeypatch):
    seen = {}

    def fake_stream(message, **kwargs):
        seen.update(kwargs)
        yield TurnEvent("thread", data={"thread_id": "0199a1b2-c3d4-7e5f-8a9b"})
        yield TurnEvent("progress", "Checking your saved profile")
        yield TurnEvent("progress", "Checking your saved profile")
        yield TurnEvent("memory", data={"keys": ["goals", "preference.style"]})
        yield TurnEvent("answer", "Here is the answer.", {"thread_id": "0199a1b2-c3d4-7e5f-8a9b"})

    _streaming(monkeypatch, fake_stream)
    chat = web.Chat(tmp_path / "w.sqlite3", "personal")
    with serving(chat) as (base, _):
        state = json.load(urlopen(base + "/api/state", timeout=5))
        assert state["display_name"] == "Personal"
        assert len(state["starters"]) == 3
        assert "I can help" not in state["welcome"]
        body = json.dumps({"message": "hi", "reasoning": "medium", "timezone": "America/Mexico_City"}).encode()
        turn = json.load(_post(base, "/api/turns", state["csrf_token"], body))["turn"]
        events = _events(base, turn["id"], state["csrf_token"])
        types = [e["type"] for e in events]
        assert types == ["progress", "memory", "answer", "done"]
        assert events[1]["labels"] == ["Goals", "Preference: style"]
        assert events[2]["message"]["memory"] == ["Goals", "Preference: style"]
        # Replaying from an offset resumes after a reload.
        assert [e["type"] for e in _events(base, turn["id"], state["csrf_token"] )][-1] == "done"
        after = json.load(urlopen(base + "/api/state", timeout=5))
        assert [m["role"] for m in after["messages"]] == ["user", "assistant"]
        assert after["turn"] is None and after["starters"] == []
    assert seen["timezone_name"] == "America/Mexico_City"
    assert seen["reasoning"] == "medium"
    assert seen["profile"] == {"fresh": [], "stale": [], "inferred": []}
    assert chat.thread_id == "0199a1b2-c3d4-7e5f-8a9b"


def test_events_endpoint_requires_token_and_local_host(tmp_path, monkeypatch):
    _streaming(monkeypatch, lambda message, **kw: iter([TurnEvent("answer", "ok")]))
    chat = web.Chat(tmp_path / "w.sqlite3", "personal")
    with serving(chat) as (base, server):
        token = chat.token
        turn = json.load(_post(base, "/api/turns", token, b'{"message":"hi"}'))["turn"]
        with pytest.raises(HTTPError) as denied:
            urlopen(Request(f"{base}/api/turns/{turn['id']}/events"), timeout=5)
        assert denied.value.code == 403
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        connection.request("GET", "/api/state", headers={"Host": f"[::1]:{server.server_port}"})
        assert connection.getresponse().status == 403  # not bound to ::1
        connection.close()


def test_stop_cancels_running_turn_and_keeps_chat_usable(tmp_path, monkeypatch):
    started = threading.Event()

    def slow_stream(message, *, control, **kwargs):
        yield TurnEvent("progress", "Thinking it through")
        started.set()
        for _ in range(200):
            if control.cancelled:
                raise AgentError(None, "cancelled")
            time.sleep(0.02)
        yield TurnEvent("answer", "late")

    _streaming(monkeypatch, slow_stream)
    chat = web.Chat(tmp_path / "w.sqlite3", "personal")
    with serving(chat) as (base, _):
        turn = json.load(_post(base, "/api/turns", chat.token, b'{"message":"hi"}'))["turn"]
        assert started.wait(5)
        pending = json.load(urlopen(base + "/api/state", timeout=5))["turn"]
        assert pending["status"] == "running" and pending["message"] == "hi"
        assert pending["progress"] == "Thinking it through"
        with pytest.raises(HTTPError) as busy:
            _post(base, "/api/turns", chat.token, b'{"message":"again"}')
        assert busy.value.code == 409
        assert _post(base, f"/api/turns/{turn['id']}/cancel", chat.token).status == 202
        events = _events(base, turn["id"], chat.token)
        assert events[-2]["type"] == "error" and events[-2]["kind"] == "cancelled"
        assert events[-1] == {"seq": events[-1]["seq"], "type": "done", "status": "cancelled"}
        state = json.load(urlopen(base + "/api/state", timeout=5))
        assert state["messages"] == [] and state["turn"]["status"] == "cancelled"
        assert not chat.lock.locked()


@pytest.mark.parametrize("raised, status, kind", [
    (AgentError(None, "not_logged_in", "Not logged in"), 502, "not_logged_in"),
    (AgentError("slow", "timeout"), 502, "timeout"),
    (StoreError("broken"), 503, "storage"),
    (sqlite3.OperationalError("database is locked"), 503, "storage"),
    (KeyError("boom"), 500, "other"),
])
def test_blocking_endpoint_maps_every_failure_to_json(tmp_path, monkeypatch, raised, status, kind):
    def failing(message, **kwargs):
        raise raised

    monkeypatch.setattr(web, "run_turn", failing)
    chat = web.Chat(tmp_path / "w.sqlite3", "personal")
    with serving(chat) as (base, _):
        with pytest.raises(HTTPError) as failure:
            _post(base, "/api/chat", chat.token, b'{"message":"hi"}')
        assert failure.value.code == status
        payload = json.load(failure.value)
        assert payload["kind"] == kind and payload["error"]
        assert "boom" not in payload["error"]
        assert not chat.lock.locked()


def test_reset_clears_conversation_but_not_memory(tmp_path, monkeypatch):
    monkeypatch.setattr(web, "run_turn", lambda message, **kw: "Answer")
    chat = web.Chat(tmp_path / "w.sqlite3", "personal")
    chat.ask("hello")
    chat.thread_id = "0199a1b2-c3d4-7e5f"
    web.WealthService(chat.db).remember("personal", [{
        "key": "preference.style", "value": "calm",
        "source": {"kind": "user", "ref": "conversation", "observed_on": "2026-09-21"},
        "confidence": "confirmed"}], 0)
    with serving(chat) as (base, _):
        state = json.load(_post(base, "/api/reset", chat.token))
    assert state["messages"] == [] and chat.thread_id is None
    assert state["welcome"] == ""  # returning client: no onboarding welcome
    assert len(state["starters"]) == 3
    assert web.WealthService(chat.db).inspect("personal")["facts"]


def test_uploads_are_typed_bounded_and_reach_the_turn(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(web, "run_turn", lambda message, **kw: calls.append((message, kw)) or "Seen")
    chat = web.Chat(tmp_path / "w.sqlite3", "personal")
    with serving(chat) as (base, server):
        pdf = b"%PDF-1.7\n" + b"x" * 100
        upload = json.load(_post(base, "/api/upload", chat.token, pdf, "application/pdf",
                                 {"X-Filename": "..%2F..%2Fetc%2Fpasswd%20statement.pdf"}))["upload"]
        assert upload["name"] == "passwd statement.pdf"
        stored = chat.uploads.get(upload["id"])
        assert stored["path"].startswith(str(tmp_path / "uploads" / "personal"))
        for body, kind in ((b"MZ\x90\x00", "application/x-msdownload"), (b"not a pdf", "application/pdf")):
            with pytest.raises(HTTPError) as rejected:
                _post(base, "/api/upload", chat.token, body, kind, {"X-Filename": "x"})
            assert rejected.value.code == 400
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        connection.putrequest("POST", "/api/upload")
        for name, value in (("Content-Type", "application/pdf"), ("X-Wealth-Token", chat.token),
                            ("Content-Length", str(web.MAX_UPLOAD_BYTES + 1))):
            connection.putheader(name, value)
        connection.endheaders()
        assert connection.getresponse().status == 413
        connection.close()
        body = json.dumps({"message": "", "attachments": [upload["id"]]}).encode()
        assert json.load(_post(base, "/api/chat", chat.token, body))["answer"] == "Seen"
        with pytest.raises(HTTPError) as missing:
            _post(base, "/api/chat", chat.token, json.dumps({"message": "x", "attachments": ["../../x"]}).encode())
        assert missing.value.code == 400
    message, kwargs = calls[0]
    assert message == "I’ve attached a file."
    assert kwargs["attachments"][0]["path"] == stored["path"]
    assert chat.messages[0]["attachments"][0]["name"] == "passwd statement.pdf"
    assert "path" not in chat.messages[0]["attachments"][0]


def test_security_headers_and_socket_timeout(tmp_path):
    chat = web.Chat(tmp_path / "w.sqlite3", "personal")
    with serving(chat) as (base, server):
        response = urlopen(base + "/", timeout=5)
        assert response.headers["Referrer-Policy"] == "no-referrer"
        assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]
        assert server.RequestHandlerClass.timeout == 30
    with pytest.raises(ValueError):
        web.create_server(chat, 0, "0.0.0.0")


def test_page_never_injects_html_from_model_text():
    from pathlib import Path
    page = Path(web.__file__).with_name("chat.html").read_text(encoding="utf-8")
    for sink in ("innerHTML =", "innerHTML=", "outerHTML", "insertAdjacentHTML", "document.write"):
        assert sink not in page
    assert "event.isComposing" in page and "(pointer: coarse)" in page
    assert "prefers-color-scheme: dark" in page


def test_friendly_names_and_memory_labels():
    assert web.friendly_name("my-profile") == "My profile"
    assert web.friendly_name("fictional-demo", "Fictional Wealth Demo") == "Fictional Wealth Demo"
    assert web.memory_label("plan.resources") == "Planning resources"
    assert web.memory_label("constraint.no_leverage") == "Constraint: no leverage"
