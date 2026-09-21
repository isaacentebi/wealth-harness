import json
import threading
from urllib.request import Request, urlopen
from urllib.error import HTTPError

import pytest
from wealth import web
from wealth.agent import build_command, build_prompt


def test_local_http_chat_memory_context_and_request_boundary(tmp_path, monkeypatch):
    calls = []
    def turn(message, **kwargs):
        calls.append((message, kwargs))
        return "A real response boundary."
    monkeypatch.setattr(web, "run_turn", turn)
    chat = web.Chat(tmp_path / "clients.sqlite3", "client", web_search=True)
    server = web.create_server(chat, 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    def send(token, origin=None):
        headers = {"Content-Type": "application/json", "X-Wealth-Token": token}
        if origin:
            headers["Origin"] = origin
        return urlopen(Request(base + "/api/chat", data=b'{"message":"hello"}', headers=headers), timeout=5)
    try:
        state = json.load(urlopen(base + "/api/state", timeout=5))
        assert state["welcome"] and state["capabilities"]["web_search"]
        with pytest.raises(HTTPError) as denied:
            send("wrong")
        assert denied.value.code == 403
        with pytest.raises(HTTPError) as denied:
            send(state["csrf_token"], "https://unrelated.example")
        assert denied.value.code == 403
        assert not calls
        assert json.load(send(state["csrf_token"]))["answer"] == "A real response boundary."
        assert calls[0][1]["profile_empty"] is True
        assert calls[0][1]["web_search"] is True
        assert calls[0][1]["history"][0][0] == "assistant"
        assert len(json.load(urlopen(base + "/api/state", timeout=5))["messages"]) == 2
        chat.lock.acquire()
        try:
            with pytest.raises(HTTPError) as busy:
                send(state["csrf_token"])
            assert busy.value.code == 409
        finally:
            chat.lock.release()
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_live_search_defaults_on_and_prompt_agrees(tmp_path):
    assert 'web_search="live"' in build_command("sol", tmp_path / "db")
    assert 'web_search="live"' in build_command("sol", tmp_path / "db", web_search=True)
    assert "Web search: on" in build_prompt("question", "client", web_search=True)
    assert "Web search: off" in build_prompt("question", "client", web_search=False)


def test_reasoning_selection_reaches_model_and_rejects_unknown(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(web, "run_turn", lambda text, **kwargs: calls.append(kwargs) or "Answer")
    chat = web.Chat(tmp_path / "reasoning.sqlite3", "client")
    assert chat.state()["reasoning"] == "low"
    chat.ask("hello", "high")
    assert calls[-1]["reasoning"] == "high"
    assert chat.state()["reasoning"] == "high"
    assert 'model_reasoning_effort="low"' in build_command("sol", tmp_path / "db")
    assert 'model_reasoning_effort="high"' in build_command("sol", tmp_path / "db", reasoning="high")
    with pytest.raises(ValueError):
        chat.ask("hello", "invalid")
    assert len(calls) == 1
