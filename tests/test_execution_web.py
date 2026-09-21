"""The only submit path: the order card's tap on the local web page (token, local origin, nonce, expiry).

Uses the in-memory FakeAlpaca from test_execution; nothing reaches the network.
"""
from __future__ import annotations

import json
import re
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from test_execution import PAPER_ENV, FakeAlpaca, no_network  # noqa: F401 - the autouse fixture guards this module too
from wealth import web
from wealth.execution import tickets
from wealth.execution.brokers import alpaca_orders
from wealth.service import WealthService
from wealth.store import WealthStore

PAGE = Path(web.__file__).with_name("chat.html").read_text(encoding="utf-8")


@pytest.fixture
def fake(monkeypatch):
    broker = FakeAlpaca()
    monkeypatch.setattr(alpaca_orders, "default_transport", broker)
    for key, value in PAPER_ENV.items():
        monkeypatch.setenv(key, value)
    return broker


@contextmanager
def serving(tmp_path):
    chat = web.Chat(tmp_path / "clients.sqlite3", "ana")
    server = web.create_server(chat, 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield chat, f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def call(base, path, *, token=None, body=None, origin=None, host=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-Wealth-Token"] = token
    if origin:
        headers["Origin"] = origin
    if host:
        headers["Host"] = host
    data = json.dumps(body).encode() if body is not None else (b"{}" if path.endswith("/confirm") else None)
    request = Request(base + path, data=data, headers=headers, method="POST" if data is not None or
                      path.endswith("/cancel") else "GET")
    try:
        with urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read() or b"{}")
    except HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


def propose(chat, orders=None, **kwargs):
    report = WealthService(chat.db).run("order_ticket", {"orders": orders or [{"symbol": "VTI", "side": "buy", "qty": 1}],
                                                         "rationale": "Buy one share."}, client_id=chat.client_id)
    return report["result"]["ticket"]["id"]


def test_confirm_needs_token_local_origin_and_the_cards_nonce(tmp_path, fake):
    with serving(tmp_path) as (chat, base):
        ticket_id = propose(chat)
        assert call(base, "/api/orders")[0] == 403  # the card's data, nonce included, needs the session token
        status, listed = call(base, "/api/orders", token=chat.token)
        card = next(t for t in listed["tickets"] if t["id"] == ticket_id)
        assert status == 200 and listed["mode"] == "paper" and card["mode"] == "paper" and card["nonce"]
        nonce = card["nonce"]
        path = f"/api/orders/{ticket_id}/confirm"
        assert call(base, path, body={"nonce": nonce})[0] == 403
        assert call(base, path, token="wrong", body={"nonce": nonce})[0] == 403
        assert call(base, path, token=chat.token, body={"nonce": nonce}, origin="https://evil.example")[0] == 403
        assert call(base, path, token=chat.token, body={"nonce": nonce}, host="evil.example")[0] == 403
        status, denied = call(base, path, token=chat.token, body={"nonce": "00000000"})
        assert status == 403 and denied["kind"] == "nonce"
        assert fake.posts() == []
        status, placed = call(base, path, token=chat.token, body={"nonce": nonce})
        assert status == 200 and placed["ticket"]["status"] == "submitted"
        assert placed["ticket"]["lines"][0]["state"] == "sent" and len(fake.posts()) == 1
        status, again = call(base, path, token=chat.token, body={"nonce": nonce})
        assert status == 409 and len(fake.posts()) == 1


def test_an_expired_ticket_cannot_be_placed(tmp_path, fake):
    with serving(tmp_path) as (chat, base):
        with WealthStore(chat.db) as store:
            old = tickets.create_ticket(store, "ana", {"orders": [{"symbol": "VTI", "side": "buy", "qty": 1}],
                                                       "rationale": "x"}, snapshot={"facts": []},
                                        now=datetime.now(timezone.utc) - timedelta(minutes=11))
            nonce = store.auxiliary("ana", "execution")["tickets"][old["result"]["ticket"]["id"]]["nonce"]
        status, body = call(base, f"/api/orders/{old['result']['ticket']['id']}/confirm", token=chat.token,
                            body={"nonce": nonce})
        assert status == 410 and body["kind"] == "expired" and fake.posts() == []


def test_a_yes_in_chat_never_places_an_order(tmp_path, fake, monkeypatch):
    monkeypatch.setattr(web, "run_turn", lambda message, **kwargs: "Listo, la orden quedó enviada.")
    with serving(tmp_path) as (chat, base):
        propose(chat)
        for message in ("sí, envíala", "yes, place it", "confirm"):
            request = Request(base + "/api/chat", data=json.dumps({"message": message}).encode(),
                              headers={"Content-Type": "application/json", "X-Wealth-Token": chat.token})
            urlopen(request, timeout=5).read()
        assert fake.posts() == []
        with WealthStore(chat.db) as store:
            assert [t["status"] for t in tickets.list_tickets(store, "ana")] == ["pending"]


def test_cancel_route_discards_or_cancels(tmp_path, fake):
    with serving(tmp_path) as (chat, base):
        pending = propose(chat)
        assert call(base, f"/api/orders/{pending}/cancel")[0] == 403
        status, body = call(base, f"/api/orders/{pending}/cancel", token=chat.token)
        assert status == 200 and body["ticket"]["status"] == "discarded" and fake.posts() == []
        placed = propose(chat, [{"symbol": "BND", "side": "buy", "qty": 1}])
        nonce = next(t["nonce"] for t in call(base, "/api/orders", token=chat.token)[1]["tickets"] if t["id"] == placed)
        order_id = call(base, f"/api/orders/{placed}/confirm", token=chat.token,
                        body={"nonce": nonce})[1]["ticket"]["lines"][0]["broker_order_id"]
        status, body = call(base, f"/api/orders/{order_id}/cancel", token=chat.token)
        assert status == 200 and body["ticket"]["lines"][0]["state"] == "canceled"
        assert call(base, "/api/orders/not-ours-0000/cancel", token=chat.token)[0] == 404


def test_the_page_polls_status_and_fills_are_posted(tmp_path, fake):
    with serving(tmp_path) as (chat, base):
        ticket_id = propose(chat)
        nonce = call(base, "/api/orders", token=chat.token)[1]["tickets"][0]["nonce"]
        order_id = call(base, f"/api/orders/{ticket_id}/confirm", token=chat.token,
                        body={"nonce": nonce})[1]["ticket"]["lines"][0]["broker_order_id"]
        fake.fill(order_id, price="250.50")
        card = call(base, "/api/orders", token=chat.token)[1]["tickets"][0]
        assert card["lines"][0]["state"] == "filled" and "nonce" not in card
        with WealthStore(chat.db) as store:
            assert [e["kind"] for e in store.ledger("ana")["entries"]] == ["buy"]


# -- the card ---------------------------------------------------------------------------------

def _section():
    return PAGE[PAGE.index("// ------------------------------------------------------------------ order cards"):
                PAGE.index("// ------------------------------------------------------------------ memory line")]


def test_the_order_card_renders_with_dom_only():
    section = _section()
    for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function",
                 "setAttribute('on", ".src =", "href"):
        assert sink not in section, sink
    # Data reaches the page only as text nodes or textContent.
    assert "el('p', 'total'" in section and "document.createTextNode" in section
    card = section[section.index("function orderCard("):section.index("function showOrder(")]
    assert card.count("'primary'") == 1 and "'text-control', O.cancel" in card
    assert "O.live : O.paper" in card  # every card says PAPER or LIVE
    # A place tap sends the card's code; nothing else in the page posts to confirm.
    assert PAGE.count("/confirm") == 0 and "`/api/orders/${encodeURIComponent(target)}/${action}`" in section
    assert "nonce: ticket.nonce" in section and "placeOrders(ticket" in card
    # No celebration: no motion or emoji in the card.
    assert not re.search(r"animation|confetti|[\U0001F300-\U0001FAFF]", section)
    css = PAGE[PAGE.index("/* Order card"):PAGE.index("/* Engine-drawn views")]
    assert [line.split("{")[0].strip() for line in css.splitlines() if "var(--vermilion)" in line] == []
    assert "var(--cobalt)" in css.split(".order .line .state.working")[1].split("}")[0]


def test_the_order_card_strings_exist_in_both_languages():
    blocks = re.findall(r"        orders: \{\n(.*?)\n        \},\n", PAGE, re.S)
    assert len(blocks) == 2
    keys = [set(re.findall(r"(?:^|[{,])\s*(\w+):", block, re.M)) for block in blocks]
    assert keys[0] == keys[1]
    en, es = blocks
    assert "'Place orders'" in en and "'Cancel'" in en and "'Enviar órdenes'" in es and "'Cancelar'" in es
    for word in ("'Enviada'", "'Ejecutada'", "'Rechazada'"):
        assert word in es
    assert "Paper · " in en and "Live · " in en and "Paper · " in es and "Live · " in es
    used = set(re.findall(r"\bO\.(\w+)", _section()))
    assert used <= keys[0], used - keys[0]
