"""Guarded trade execution: tickets, checks, confirmation, audit, fills and cancel.

Every test runs against :class:`FakeAlpaca`, an in-memory transport.  The real
transport and the OS keychain are replaced for the whole module, so nothing here
can reach Alpaca or read a real credential.
"""
from __future__ import annotations

import asyncio
import json
import re
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from urllib.parse import parse_qs, urlsplit

import pytest

from wealth import service as service_module
from wealth.connectors import _rest
from wealth.execution import tickets
from wealth.execution.brokers import alpaca_orders
from wealth.execution.brokers.alpaca_orders import AlpacaKeys, AlpacaOrders, BrokerError
from wealth.service import OPERATIONS, WealthService
from wealth.store import WealthStore

NOW = datetime(2026, 9, 21, 15, 0, tzinfo=timezone.utc)  # a Monday, 11:00 in New York
PAPER_KEY, PAPER_SECRET = "PKFAKEPAPERKEY0001", "fake-paper-secret-value-0001"
LIVE_KEY, LIVE_SECRET = "AKFAKELIVEKEY00001", "fake-live-secret-value-00001"
PAPER_ENV = {"WEALTH_ALPACA_PAPER_KEY_ID": PAPER_KEY, "WEALTH_ALPACA_PAPER_SECRET": PAPER_SECRET}
LIVE_ENV = {"WEALTH_ALPACA_KEY_ID": LIVE_KEY, "WEALTH_ALPACA_SECRET": LIVE_SECRET, "WEALTH_TRADING_LIVE": "alpaca"}
ACCOUNT_NUMBER = "PA3K7Q9981234"


class FakeAlpaca:
    """Alpaca's Trading and Market Data endpoints in memory: a transport, never a socket."""

    def __init__(self, *, prices=None, buying_power="10000", is_open=True, positions=None, assets=None):
        self.prices = {"VTI": "250.00", "BND": "72.00", "XYZ": "10.00", "WHOLE": "40.00", **(prices or {})}
        self.assets = {s: {"symbol": s, "tradable": True, "fractionable": True, "status": "active"}
                       for s in self.prices}
        self.assets["WHOLE"]["fractionable"] = False
        self.assets.update(assets or {})
        self.account = {"account_number": ACCOUNT_NUMBER, "status": "ACTIVE", "currency": "USD", "cash": buying_power,
                        "buying_power": str(Decimal(buying_power) * 2), "non_marginable_buying_power": buying_power,
                        "trading_blocked": False, "account_blocked": False, "trade_suspended_by_user": False}
        self.is_open = is_open
        self.positions = positions or []
        self.orders: dict[str, dict] = {}
        self.activities: list[dict] = []
        self.calls: list[dict] = []
        self.lose_next_response = False
        self.trade_time = None  # None: the last trade is fresh (this instant); Alpaca always sends trade.t
        self.fail_fills = False

    def __call__(self, method, url, headers, body, timeout):
        parts = urlsplit(url)
        assert parts.scheme == "https" and parts.hostname in alpaca_orders.HOSTS, url
        payload = json.loads(body) if body else None
        self.calls.append({"method": method, "host": parts.hostname, "path": parts.path,
                           "query": {k: v[0] for k, v in parse_qs(parts.query).items()}, "body": payload,
                           "headers": dict(headers)})
        path, query = parts.path, {k: v[0] for k, v in parse_qs(parts.query).items()}
        if parts.hostname == "data.alpaca.markets":
            symbol = path.split("/")[3]
            traded_at = self.trade_time or max(datetime.now(timezone.utc), NOW).isoformat()
            return self._json(200, {"symbol": symbol, "trade": {"p": float(self.prices[symbol]), "t": traded_at}}) \
                if symbol in self.prices else self._json(404, {"message": "not found"})
        if method == "GET" and path == "/v2/account":
            return self._json(200, self.account)
        if method == "GET" and path == "/v2/clock":
            return self._json(200, {"is_open": self.is_open, "next_open": "2026-09-22T13:30:00Z"})
        if method == "GET" and path.startswith("/v2/assets/"):
            asset = self.assets.get(path.rsplit("/", 1)[1])
            return self._json(200, asset) if asset else self._json(404, {"message": "asset not found"})
        if method == "GET" and path == "/v2/positions":
            return self._json(200, self.positions)
        if method == "GET" and path == "/v2/orders":
            return self._json(200, [o for o in self.orders.values() if o["status"] in ("new", "accepted")])
        if method == "GET" and path == "/v2/orders:by_client_order_id":
            found = next((o for o in self.orders.values() if o["client_order_id"] == query["client_order_id"]), None)
            return self._json(200, found) if found else self._json(404, {"message": "order not found"})
        if method == "POST" and path == "/v2/orders":
            if any(o["client_order_id"] == payload["client_order_id"] for o in self.orders.values()):
                return self._json(422, {"message": "client_order_id must be unique"})
            order = {"id": str(uuid.uuid4()), "status": "accepted", "filled_qty": "0", "filled_avg_price": None,
                     "submitted_at": "2026-09-21T15:00:01Z", **payload}
            self.orders[order["id"]] = order
            if self.lose_next_response:
                self.lose_next_response = False
                raise BrokerError("Could not reach Alpaca (TimeoutError).", retryable=True)
            return self._json(200, order)
        if method == "GET" and path.startswith("/v2/orders/"):
            order = self.orders.get(path.rsplit("/", 1)[1])
            return self._json(200, order) if order else self._json(404, {"message": "order not found"})
        if method == "DELETE" and path.startswith("/v2/orders/"):
            order = self.orders.get(path.rsplit("/", 1)[1])
            if not order:
                return self._json(404, {"message": "order not found"})
            order["status"] = "canceled"
            return 204, b""
        if method == "GET" and path == "/v2/account/activities/FILL":  # paged like Alpaca: page_size, page_token
            if self.fail_fills:
                return self._json(500, {"message": "internal error"})
            rows = sorted(self.activities, key=lambda a: a["id"])
            if query.get("page_token"):
                rows = [a for a in rows if a["id"] > query["page_token"]]
            return self._json(200, rows[: int(query.get("page_size", 100))])
        return self._json(404, {"message": f"unexpected {method} {path}"})

    @staticmethod
    def _json(status, value):
        return status, json.dumps(value).encode()

    def fill(self, order_id, qty=None, price=None, when="2026-09-21T15:05:00.123Z"):
        order = self.orders[order_id]
        qty = qty or order["qty"]
        filled = Decimal(order.get("filled_qty") or "0") + Decimal(qty)
        order["filled_qty"] = str(filled)
        order["filled_avg_price"] = price or order.get("limit_price")
        order["status"] = "filled" if filled >= Decimal(order["qty"]) else "partially_filled"
        activity = {"id": f"20260921150500123::{uuid.uuid4()}", "activity_type": "FILL", "order_id": order_id,
                    "symbol": order["symbol"], "side": order["side"], "qty": str(qty),
                    "price": price or order.get("limit_price"), "transaction_time": when,
                    "type": "fill" if order["status"] == "filled" else "partial_fill"}
        self.activities.append(activity)
        return activity

    def posts(self):
        return [c for c in self.calls if c["method"] == "POST"]


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*_, **__):
        raise AssertionError("tests must never reach a real broker")

    monkeypatch.setattr(alpaca_orders, "default_transport", refuse)
    monkeypatch.setattr(_rest, "keychain_command", lambda *a, **k: None)
    monkeypatch.setattr(alpaca_orders, "default_sleep", lambda seconds: None)
    for name in ("WEALTH_TRADING_LIVE", "WEALTH_ALPACA_KEY_ID", "WEALTH_ALPACA_SECRET", "WEALTH_ALPACA_PAPER",
                 "WEALTH_ALPACA_PAPER_KEY_ID", "WEALTH_ALPACA_PAPER_SECRET", "WEALTH_TRADING_MAX_ORDER_USD",
                 "WEALTH_TRADING_MAX_DAILY_USD", "WEALTH_TRADING_COLLAR"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def fake(monkeypatch):
    broker = FakeAlpaca()
    monkeypatch.setattr(alpaca_orders, "default_transport", broker)
    return broker


@pytest.fixture
def paper(monkeypatch, fake):
    for key, value in PAPER_ENV.items():
        monkeypatch.setenv(key, value)
    return fake


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "clients.sqlite3"
    WealthService(path).create("ana", "Ana")
    return path


IPS = {"currency": "USD", "allocation": {"model": "balanced", "sleeves": [
    {"id": "equity", "name": "Equity", "asset": "equity", "target": 0.6, "min": 0.5, "max": 0.7},
    {"id": "bonds", "name": "Bonds", "asset": "fixed_income", "target": 0.4, "min": 0.3, "max": 0.5}]},
    "constraints": {"leverage": {"allowed": False}, "exclusions": {"symbols": ["XYZ"]}}}


def snapshot_with_ips():
    return {"client": {"id": "ana", "revision": 1}, "decisions": [], "facts": [
        {"id": "f-ips", "key": "policy.ips", "value": IPS, "confidence": "confirmed", "status": "active",
         "expires_on": "2027-09-21", "source": {"kind": "user", "ref": "accepted IPS", "observed_on": "2026-09-01"}}]}


def make_ticket(db, orders, *, snapshot=None, environ=None, now=NOW, source="user_request"):
    with WealthStore(db) as store:
        return tickets.create_ticket(store, "ana", {"orders": orders, "rationale": "Invest the monthly surplus.",
                                                     "source": source},
                                     snapshot=snapshot or {"facts": []}, environ=environ, now=now)


def nonce_of(db, ticket_id):
    with WealthStore(db) as store:
        return next(t["nonce"] for t in tickets.list_tickets(store, "ana", include_nonce=True, now=NOW)
                    if t["id"] == ticket_id)


def confirm(db, ticket_id, nonce, *, now=NOW, environ=None, **kwargs):
    with WealthStore(db) as store:
        return tickets.confirm(store, "ana", ticket_id, nonce=nonce, snapshot={"facts": []} if "snapshot" not in kwargs
                               else kwargs.pop("snapshot"), environ=environ, now=now, **kwargs)


# -- 1. no model path can submit ---------------------------------------------------------------

def test_no_mcp_tool_cli_operation_or_service_method_can_submit(db, paper):
    from wealth.server import build_server

    server = build_server(str(db))
    names = {tool.name for tool in asyncio.run(server.list_tools())}
    assert names == {"wealth_context", "wealth_remember", "wealth_run", "wealth_recall", "wealth_decision",
                     "wealth_ingest", "wealth_inspect", "wealth_resolve_contradiction", "wealth_client"}
    forbidden = re.compile(r"submit|place|confirm_order|execute|trade|order", re.I)
    assert not [name for name in names if forbidden.search(name)]
    # The CLI/service operations read and reconcile only.
    assert not [op for op in OPERATIONS if re.search(r"submit|place|confirm|cancel_order|execute", op)]
    assert {"execution_status", "order_status"} <= set(OPERATIONS)
    service = WealthService(db)
    assert not [name for name in dir(service) if re.search(r"submit|place|confirm_order|execute|cancel_order", name)]
    with pytest.raises(ValueError, match="unknown task"):
        service.run("order_submit", {}, client_id="ana")
    # The model's task refuses anything that looks like a confirmation.
    for extra in ({"nonce": "ABCD1234"}, {"confirm": True}, {"submit": True}, {"mode": "live"}):
        with pytest.raises(ValueError, match="unknown"):
            service.run("order_ticket", {"orders": [{"symbol": "VTI", "side": "buy", "qty": 1}],
                                         "rationale": "x", **extra}, client_id="ana")

    # Through the real MCP boundary: a ticket is proposed, read-only calls only, no nonce leaves.
    async def run_tool():
        result = await server.call_tool("wealth_run", {"task": "order_ticket", "client_id": "ana", "inputs": {
            "orders": [{"symbol": "VTI", "side": "buy", "qty": 1}], "rationale": "Buy one share."}})
        exported = await server.call_tool("wealth_inspect", {"client_id": "ana", "detail": "export"})
        return result, exported

    result, exported = asyncio.run(run_tool())
    text = json.dumps([c.model_dump() for c in result.content]) if hasattr(result, "content") else json.dumps(result)
    ticket_id = re.search(r"t[0-9a-f]{16}", text).group(0)
    nonce = nonce_of(db, ticket_id)
    assert nonce not in text and "Nothing has been sent" in text
    assert nonce not in json.dumps([c.model_dump() for c in exported.content])
    assert paper.posts() == [] and {c["method"] for c in paper.calls} == {"GET"}
    # The service's read op never shows the nonce either and never submits.
    status = service.order_status("ana", ticket_id)
    assert "nonce" not in json.dumps(status) and status["tickets"][0]["status"] == "pending"
    assert service.run("order_ticket", {"ticket_id": ticket_id}, client_id="ana")["result"]["ticket"]["id"] == ticket_id
    assert paper.posts() == []


def test_ticket_stores_orders_checks_and_a_short_summary(db, paper):
    report = make_ticket(db, [{"symbol": "VTI", "side": "buy", "notional": 500, "estimated_tax": 0},
                              {"symbol": "BND", "side": "buy", "qty": 2}])
    ticket = report["result"]["ticket"]
    assert report["status"] == "ready" and ticket["mode"] == "paper" and ticket["status"] == "pending"
    vti, bnd = ticket["lines"]
    # Limit orders by default, collared around the last trade (half the 1% collar through the price).
    assert vti["type"] == "limit" and Decimal(vti["limit_price"]) == Decimal("251.25")
    assert Decimal(vti["qty"]) == (Decimal(500) / Decimal("251.25")).quantize(Decimal("0.000001"), rounding="ROUND_DOWN")
    assert Decimal(bnd["limit_price"]) == Decimal("72.36") and bnd["qty"] == "2"
    assert Decimal(ticket["total"]["amount"]) == Decimal(vti["estimated_amount"]) + Decimal("144.72")
    assert ticket["total"]["estimated_tax"] == "0"
    assert "Nothing has been sent" in report["result"]["summary"] and "nonce" not in json.dumps(report)
    assert [n["code"] for n in ticket["notices"]] == ["no_policy"]  # one quiet line, not one per order
    assert ticket["expires_at"] == "2026-09-21T15:10:00Z"


def test_hard_checks_block_tradable_fractional_buying_power_collar_and_sells(db, paper):
    paper.assets["ODD"] = {"symbol": "ODD", "tradable": False, "fractionable": False, "status": "active"}
    paper.prices["ODD"] = "5.00"
    report = make_ticket(db, [{"symbol": "ODD", "side": "buy", "qty": 1},
                              {"symbol": "WHOLE", "side": "buy", "qty": "1.5"},
                              {"symbol": "VTI", "side": "buy", "qty": 100},
                              {"symbol": "BND", "side": "buy", "qty": 1, "limit_price": "80.00"},
                              {"symbol": "XYZ", "side": "sell", "qty": 3}])
    codes = {(n["code"], n.get("line")) for n in report["result"]["ticket"]["notices"] if n["status"] == "block"}
    assert {("not_tradable", 0), ("not_fractionable", 1), ("collar", 3), ("sell_exceeds_position", 4),
            ("buying_power", None)} <= codes
    assert report["result"]["ticket"]["blocked"] is True


# -- 2. token, nonce and expiry (the web boundary is in test_execution_web.py) -------------------

def test_confirm_needs_the_cards_nonce_an_unexpired_ticket_and_only_once(db, paper):
    ticket_id = make_ticket(db, [{"symbol": "VTI", "side": "buy", "qty": 1}])["result"]["ticket"]["id"]
    nonce = nonce_of(db, ticket_id)
    with pytest.raises(tickets.ConfirmError) as wrong:
        confirm(db, ticket_id, "00000000")
    assert wrong.value.status == 403 and paper.posts() == []
    with pytest.raises(tickets.ConfirmError) as late:
        confirm(db, ticket_id, nonce, now=NOW + timedelta(minutes=10, seconds=1))
    assert late.value.status == 410 and paper.posts() == []
    placed = confirm(db, ticket_id, nonce.lower(), now=NOW + timedelta(minutes=9))
    assert placed["status"] == "submitted" and placed["lines"][0]["state"] == "sent"
    assert len(paper.posts()) == 1
    with pytest.raises(tickets.ConfirmError) as again:
        confirm(db, ticket_id, nonce)
    assert again.value.status == 409 and len(paper.posts()) == 1


def test_repeated_wrong_nonces_void_the_ticket(db, paper):
    ticket_id = make_ticket(db, [{"symbol": "VTI", "side": "buy", "qty": 1}])["result"]["ticket"]["id"]
    nonce = nonce_of(db, ticket_id)
    for _ in range(tickets.MAX_NONCE_FAILURES):
        with pytest.raises(tickets.ConfirmError):
            confirm(db, ticket_id, "BADBAD00")
    with pytest.raises(tickets.ConfirmError) as used:
        confirm(db, ticket_id, nonce)
    assert used.value.kind == "used" and paper.posts() == []


def test_confirm_reruns_checks_on_fresh_data(db, paper):
    ticket_id = make_ticket(db, [{"symbol": "VTI", "side": "buy", "qty": 1}])["result"]["ticket"]["id"]
    nonce = nonce_of(db, ticket_id)
    paper.prices["VTI"] = "240.00"  # the market moved: the stored limit is now 4.7% through it
    paper.account["trading_blocked"] = True
    with pytest.raises(tickets.ConfirmError) as blocked:
        confirm(db, ticket_id, nonce)
    codes = {n["code"] for n in blocked.value.ticket["notices"] if n["status"] == "block"}
    assert blocked.value.status == 409 and {"account_blocked"} <= codes and paper.posts() == []


def test_unknown_facts_block_submission(db, monkeypatch, fake):
    # No keys: checks could not run, so the ticket is shown but cannot be placed.
    report = make_ticket(db, [{"symbol": "VTI", "side": "buy", "qty": 1}])
    assert report["status"] == "partial" and report["result"]["ticket"]["blocked"]
    ticket_id = report["result"]["ticket"]["id"]
    with pytest.raises(tickets.ConfirmError) as blocked:
        confirm(db, ticket_id, nonce_of(db, ticket_id))
    assert blocked.value.kind == "blocked" and fake.calls == []


# -- 3. policy violations block unless overridden, and the override is recorded -------------------

def test_policy_violation_blocks_and_a_recorded_override_is_honoured(db, paper):
    snapshot = snapshot_with_ips()
    report = make_ticket(db, [{"symbol": "XYZ", "side": "buy", "qty": 1}], snapshot=snapshot)
    ticket = report["result"]["ticket"]
    violation = next(n for n in ticket["notices"] if n["status"] == "violation")
    assert violation["code"] == "policy" and "excluded" in violation["message"] and ticket["needs_override"]
    nonce = nonce_of(db, ticket["id"])
    with pytest.raises(tickets.ConfirmError) as blocked:
        confirm(db, ticket["id"], nonce, snapshot=snapshot)
    assert blocked.value.kind == "blocked" and paper.posts() == []
    placed = confirm(db, ticket["id"], nonce, snapshot=snapshot, override=True)
    assert placed["status"] == "submitted" and placed["override"]["codes"] == ["exclusions"]
    with WealthStore(db) as store:
        confirmed = [e for e in store.order_events("ana", ticket["id"]) if e["event"] == "confirm"]
    assert confirmed[0]["payload"]["override"]["codes"] == ["exclusions"]


def test_override_never_lifts_a_hard_block(db, paper):
    ticket_id = make_ticket(db, [{"symbol": "VTI", "side": "buy", "qty": 1000}])["result"]["ticket"]["id"]
    with pytest.raises(tickets.ConfirmError) as blocked:
        confirm(db, ticket_id, nonce_of(db, ticket_id), override=True)
    assert "buying power" in str(blocked.value) and paper.posts() == []


# -- 4. live: opt-in, typed confirmation, limits and URLs ----------------------------------------

def test_paper_is_the_default_and_uses_paper_and_data_urls(db, paper):
    ticket_id = make_ticket(db, [{"symbol": "VTI", "side": "buy", "qty": 1}])["result"]["ticket"]["id"]
    confirm(db, ticket_id, nonce_of(db, ticket_id))
    hosts = {c["host"] for c in paper.calls}
    assert hosts == {"paper-api.alpaca.markets", "data.alpaca.markets"}
    assert {c["host"] for c in paper.calls if c["path"].startswith("/v2/stocks/")} == {"data.alpaca.markets"}
    assert paper.posts()[0]["host"] == "paper-api.alpaca.markets"
    assert paper.posts()[0]["headers"]["APCA-API-KEY-ID"] == PAPER_KEY
    assert alpaca_orders.base_url("paper") == "https://paper-api.alpaca.markets"
    assert alpaca_orders.base_url("live") == "https://api.alpaca.markets"


def test_live_needs_opt_in_typed_confirmation_and_stays_within_limits(db, fake, monkeypatch):
    for key, value in LIVE_ENV.items():
        monkeypatch.setenv(key, value)
    assert tickets.trading_mode() == "live"
    big = make_ticket(db, [{"symbol": "VTI", "side": "buy", "qty": 5}])["result"]["ticket"]
    assert big["mode"] == "live" and any(n["code"] == "live_order_limit" for n in big["notices"])
    market = make_ticket(db, [{"symbol": "BND", "side": "buy", "qty": 1, "type": "market"}])["result"]["ticket"]
    assert any(n["code"] == "live_needs_limit" and n["status"] == "block" for n in market["notices"])

    ticket = make_ticket(db, [{"symbol": "VTI", "side": "buy", "qty": 3}])["result"]["ticket"]
    assert ticket["needs_typed"] and not ticket["blocked"]
    nonce = nonce_of(db, ticket["id"])
    with pytest.raises(tickets.ConfirmError) as typed:
        confirm(db, ticket["id"], nonce)
    assert typed.value.kind == "typed" and fake.posts() == []
    placed = confirm(db, ticket["id"], nonce, typed="en vivo")
    assert placed["status"] == "submitted" and fake.posts()[0]["host"] == "api.alpaca.markets"
    assert fake.posts()[0]["headers"]["APCA-API-KEY-ID"] == LIVE_KEY

    # Second live ticket: no typed phrase needed; the daily limit counts what was already confirmed today.
    for _ in range(8):
        follow = make_ticket(db, [{"symbol": "VTI", "side": "buy", "qty": 3}])["result"]["ticket"]
        if any(n["code"] == "live_daily_limit" for n in follow["notices"]):
            break
        assert not follow["needs_typed"]
        confirm(db, follow["id"], nonce_of(db, follow["id"]))
    assert any(n["code"] == "live_daily_limit" for n in follow["notices"])
    with WealthStore(db) as store:
        used = tickets.execution_status(store, "ana", now=NOW)["live_used_today_usd"]
    assert Decimal(used) <= Decimal(5000)

    # Turning live off stops live tickets from being placed even with the nonce.
    pending = make_ticket(db, [{"symbol": "BND", "side": "buy", "qty": 1}])["result"]["ticket"]
    monkeypatch.delenv("WEALTH_TRADING_LIVE")
    with pytest.raises(tickets.ConfirmError) as off:
        confirm(db, pending["id"], nonce_of(db, pending["id"]))
    assert off.value.kind == "live_disabled"


def test_limits_are_configurable_and_paper_is_not_limited(db, paper, monkeypatch):
    monkeypatch.setenv("WEALTH_TRADING_MAX_ORDER_USD", "100")
    report = make_ticket(db, [{"symbol": "VTI", "side": "buy", "qty": 3}])
    assert not any(n["code"].startswith("live_") for n in report["result"]["ticket"]["notices"])
    assert tickets.limits()["max_order"] == Decimal(100)


def test_keys_follow_the_mode_and_never_print():
    env = {**PAPER_ENV, "WEALTH_ALPACA_KEY_ID": LIVE_KEY, "WEALTH_ALPACA_SECRET": LIVE_SECRET}
    paper_keys = alpaca_orders.load_keys("paper", env, platform="none")
    live_keys = alpaca_orders.load_keys("live", env, platform="none")
    assert paper_keys.headers()["APCA-API-KEY-ID"] == PAPER_KEY and live_keys.headers()["APCA-API-KEY-ID"] == LIVE_KEY
    assert PAPER_SECRET not in repr(paper_keys) and LIVE_SECRET not in str(live_keys)
    # The read connector's variables hold paper keys only when WEALTH_ALPACA_PAPER says so.
    flagged = {"WEALTH_ALPACA_KEY_ID": PAPER_KEY, "WEALTH_ALPACA_SECRET": PAPER_SECRET, "WEALTH_ALPACA_PAPER": "1"}
    assert alpaca_orders.load_keys("paper", flagged, platform="none") is not None
    assert alpaca_orders.load_keys("live", flagged, platform="none") is None


# -- 5. idempotent client_order_id -----------------------------------------------------------

def test_client_order_id_is_deterministic_and_retries_never_duplicate(db, paper):
    ticket_id = make_ticket(db, [{"symbol": "VTI", "side": "buy", "qty": 1},
                                 {"symbol": "BND", "side": "buy", "qty": 1}])["result"]["ticket"]["id"]
    assert tickets.client_order_id(ticket_id, 1) == f"wealth-{ticket_id}-1"
    paper.lose_next_response = True  # Alpaca accepted the first order but the answer was lost
    placed = confirm(db, ticket_id, nonce_of(db, ticket_id))
    assert [l["state"] for l in placed["lines"]] == ["sent", "sent"]
    assert len(paper.orders) == 2
    assert sorted(o["client_order_id"] for o in paper.orders.values()) == [f"wealth-{ticket_id}-0",
                                                                          f"wealth-{ticket_id}-1"]
    # Submitting the same line again resolves to the same order.
    api = AlpacaOrders(AlpacaKeys(PAPER_KEY, PAPER_SECRET, mode="paper", source="env"), transport=paper)
    order = next(iter(paper.orders.values()))
    again = api.submit({k: order[k] for k in ("symbol", "qty", "side", "type", "time_in_force", "limit_price",
                                              "client_order_id")})
    assert again["id"] == order["id"] and len(paper.orders) == 2
    with pytest.raises(ValueError):
        api.submit({"symbol": "VTI", "qty": "1", "side": "buy", "type": "market", "time_in_force": "day"})


# -- 6. redacted, append-only audit -----------------------------------------------------------

def test_audit_rows_are_redacted_and_append_only(db, paper):
    ticket_id = make_ticket(db, [{"symbol": "VTI", "side": "buy", "qty": 1}])["result"]["ticket"]["id"]
    nonce = nonce_of(db, ticket_id)
    confirm(db, ticket_id, nonce)
    with WealthStore(db) as store:
        tickets.refresh(store, "ana", now=NOW)
        events = store.order_events("ana", ticket_id)
    kinds = [e["event"] for e in events]
    assert {"ticket", "checks", "confirm", "request", "response"} <= set(kinds)
    assert {"lookup", "submit", "status"} <= {e["payload"].get("action") for e in events}
    dump = json.dumps(events)
    for secret in (PAPER_KEY, PAPER_SECRET, nonce, ACCOUNT_NUMBER):
        assert secret not in dump
    request = next(e for e in events if e["event"] == "request" and e["payload"]["method"] == "POST")
    assert request["client_order_id"] == f"wealth-{ticket_id}-0" and request["payload"]["body"]["symbol"] == "VTI"
    assert "headers" not in json.dumps(request["payload"])
    assert alpaca_orders.redact({"account_number": ACCOUNT_NUMBER})["account_number"] == "****1234"
    connection = sqlite3.connect(db)
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        connection.execute("UPDATE orders SET event = 'x'")
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        connection.execute("DELETE FROM orders")
    connection.close()
    # Deleting the person removes their audit rows with them; export carries the rows but no nonce.
    ticket2 = make_ticket(db, [{"symbol": "BND", "side": "buy", "qty": 1}])["result"]["ticket"]["id"]
    with WealthStore(db) as store:
        exported = json.dumps(store.export_client("ana"))
        assert nonce_of(db, ticket2) not in exported and '"orders"' in exported
        store.delete_client("ana", "ana")
    assert sqlite3.connect(db).execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 0


def test_existing_databases_gain_the_orders_table(tmp_path):
    path = tmp_path / "old.sqlite3"
    WealthService(path).create("ana", "Ana")
    connection = sqlite3.connect(path)
    connection.executescript("DROP TRIGGER orders_append_only_update; DROP TRIGGER orders_append_only_delete; "
                             "DROP TABLE orders;")
    connection.close()
    with WealthStore(path) as store:
        assert store.order_events("ana") == []
    names = {row[0] for row in sqlite3.connect(path).execute("SELECT name FROM sqlite_master")}
    assert {"orders", "orders_append_only_update", "orders_append_only_delete"} <= names


# -- 7. fills reach the ledger once -----------------------------------------------------------

def test_fills_are_posted_to_the_ledger_with_alpaca_ids_and_deduped(db, paper):
    ticket_id = make_ticket(db, [{"symbol": "VTI", "side": "buy", "qty": 2}])["result"]["ticket"]["id"]
    placed = confirm(db, ticket_id, nonce_of(db, ticket_id))
    order_id = placed["lines"][0]["broker_order_id"]
    first = paper.fill(order_id, qty="1", price="250.10")
    with WealthStore(db) as store:
        view = tickets.refresh(store, "ana", now=NOW)[-1]
        assert view["lines"][0]["state"] == "partial"
        entries = store.ledger("ana")["entries"]
    head, tail = first["id"].split("::")
    external = f"ALPACA-A{head}_{tail.replace('-', '')}"
    assert [(e["kind"], e["external_id"], e["quantity"], e["account_id"]) for e in entries] == \
        [("buy", external, "1", "alpaca-1234")]
    assert Decimal(entries[0]["amount"]) == Decimal("-250.10")

    paper.fill(order_id, qty="1", price="250.20")
    with WealthStore(db) as store:
        view = tickets.refresh(store, "ana", now=NOW)[-1]
        tickets.refresh(store, "ana", now=NOW)  # nothing new the second time
        entries = store.ledger("ana")["entries"]
        posted = [e for e in store.order_events("ana", ticket_id) if e["event"] == "fill_posted"]
    assert view["lines"][0]["state"] == "filled" and view["status"] == "done"
    assert len(entries) == 2 and len(posted) == 2

    # A later sync by the read connector carrying the same Alpaca ids adds nothing.
    with WealthStore(db) as store:
        batch = {"batch_id": "connector-sync", "source": {"kind": "tool", "ref": "alpaca:paper",
                                                           "observed_on": "2026-09-21"},
                 "transactions": [{"kind": "buy", "account_id": "alpaca-1234", "date": e["date"],
                                   "instrument_id": "VTI", "quantity": e["quantity"], "amount": e["amount"],
                                   "currency": "USD", "external_id": e["external_id"]} for e in entries]}
        receipt = store.post_ledger("ana", batch)
    assert receipt["posted"] == [] and len(receipt["duplicates"]) == 2


def test_fills_synced_first_by_the_connector_are_not_posted_twice(db, paper):
    ticket_id = make_ticket(db, [{"symbol": "VTI", "side": "buy", "qty": 1}])["result"]["ticket"]["id"]
    order_id = confirm(db, ticket_id, nonce_of(db, ticket_id))["lines"][0]["broker_order_id"]
    activity = paper.fill(order_id, price="250.00")
    head, tail = activity["id"].split("::")
    with WealthStore(db) as store:
        store.post_ledger("ana", {
            "batch_id": "connector-first", "source": {"kind": "tool", "ref": "alpaca:paper", "observed_on": "2026-09-21"},
            "accounts": [{"id": "alpaca-1234", "institution": "Alpaca", "type": "brokerage", "currency": "USD",
                          "owners": [{"person_id": "self", "share": "1"}]}],
            "instruments": [{"id": "VTI", "symbol": "VTI", "currency": "USD"}],
            "transactions": [{"kind": "buy", "account_id": "alpaca-1234", "date": "2026-09-21", "instrument_id": "VTI",
                              "quantity": "1", "amount": "-250.00", "currency": "USD",
                              "external_id": f"ALPACA-A{head}_{tail.replace('-', '')}"}]})
        tickets.refresh(store, "ana", now=NOW)
        assert len(store.ledger("ana")["entries"]) == 1
        posted = [e for e in store.order_events("ana", ticket_id) if e["event"] == "fill_posted"]
    assert posted[0]["payload"]["duplicates"] == 1 and posted[0]["payload"]["posted"] == 0


# -- 8. cancel -------------------------------------------------------------------------------

def test_cancel_discards_a_pending_ticket_or_cancels_a_placed_order(db, paper):
    pending = make_ticket(db, [{"symbol": "VTI", "side": "buy", "qty": 1}])["result"]["ticket"]["id"]
    nonce = nonce_of(db, pending)
    with WealthStore(db) as store:
        assert tickets.cancel(store, "ana", pending, now=NOW)["status"] == "discarded"
    with pytest.raises(tickets.ConfirmError):
        confirm(db, pending, nonce)
    assert paper.posts() == []

    placed = make_ticket(db, [{"symbol": "BND", "side": "buy", "qty": 1}])["result"]["ticket"]["id"]
    order_id = confirm(db, placed, nonce_of(db, placed))["lines"][0]["broker_order_id"]
    with WealthStore(db) as store:
        view = tickets.cancel(store, "ana", order_id, now=NOW)
        with pytest.raises(tickets.ConfirmError) as final:
            tickets.cancel(store, "ana", order_id, now=NOW)
        with pytest.raises(tickets.ConfirmError) as foreign:
            tickets.cancel(store, "ana", str(uuid.uuid4()), now=NOW)
    assert view["lines"][0]["state"] == "canceled" and view["status"] == "done"
    assert [c["method"] for c in paper.calls if c["path"] == f"/v2/orders/{order_id}"][0] == "DELETE"
    assert final.value.kind == "final" and foreign.value.status == 404


# -- 9. status and the service ops ----------------------------------------------------------------

def test_execution_status_reports_mode_keys_and_limits_without_secrets(db, paper):
    status = WealthService(db).execution_status("ana")
    assert status["mode"] == "paper" and status["credentials"] == {"paper": True, "live": False}
    assert status["limits"]["per_order_usd"] == "1000" and status["limits"]["daily_usd"] == "5000"
    assert PAPER_KEY not in json.dumps(status) and PAPER_SECRET not in json.dumps(status)
    assert service_module.dispatch("execution_status", {"client_id": "ana"}, db)["mode"] == "paper"


def test_duplicates_and_market_hours_are_quiet_warnings(db, paper):
    paper.is_open = False
    make_ticket(db, [{"symbol": "VTI", "side": "buy", "qty": 1}])
    report = make_ticket(db, [{"symbol": "VTI", "side": "buy", "qty": 1}, {"symbol": "VTI", "side": "buy", "qty": 2}])
    notices = {(n["code"], n["status"]) for n in report["result"]["ticket"]["notices"]}
    assert {("market_closed", "warn"), ("duplicate_ticket", "warn"), ("duplicate_line", "block")} <= notices


def test_rebalance_trades_can_become_a_ticket(db, paper):
    trade = {"account_id": "brk", "instrument_id": "BND", "side": "buy", "quantity": 3, "estimated_amount": 216,
             "estimated_tax": 0, "estimated_cost": "0.00", "reason": "Bring bonds back to 40%."}
    ticket = make_ticket(db, [trade], source="rebalance")["result"]["ticket"]
    assert ticket["source"] == "rebalance" and ticket["lines"][0]["qty"] == "3"
    assert ticket["lines"][0]["account"] == "brk" and ticket["total"]["estimated_cost"] == "0"


@pytest.mark.parametrize("method, path", [
    ("DELETE", "/v2/orders"), ("DELETE", "/v2/positions"), ("DELETE", "/v2/positions/VTI"), ("PATCH", "/v2/orders/abcdef12"),
    ("POST", "/v2/account/configurations"), ("POST", "/v2/transfers"), ("POST", "/v2/positions/VTI"),
    ("GET", "/v2/account/configurations"), ("PUT", "/v2/orders"),
])
def test_the_order_client_refuses_anything_outside_its_allowlist(method, path, fake):
    api = AlpacaOrders(AlpacaKeys(PAPER_KEY, PAPER_SECRET, mode="paper", source="env"), transport=fake)
    with pytest.raises(BrokerError, match="Refusing"):
        api._request(method, path)
    with pytest.raises(BrokerError, match="Refusing"):
        alpaca_orders.urllib_transport(method, "https://paper-api.alpaca.markets" + path, {}, None, 1)
    with pytest.raises(BrokerError, match="non-Alpaca"):
        alpaca_orders.urllib_transport("GET", "https://example.com/v2/account", {}, None, 1)
    assert fake.calls == []
    assert alpaca_orders.allowed("POST", "api", "/v2/orders") and alpaca_orders.allowed("DELETE", "api", "/v2/orders/0a1b2c3d-1")


def test_the_client_stays_under_alpacas_rate_limit(fake):
    moments, slept = [0.0], []
    api = AlpacaOrders(AlpacaKeys(PAPER_KEY, PAPER_SECRET, mode="paper", source="env"), transport=fake,
                       clock=lambda: moments[0], sleep=lambda s: (slept.append(s), moments.__setitem__(0, moments[0] + s)))
    for _ in range(alpaca_orders.WINDOW_BUDGET):
        api.clock()
    assert slept == []  # a ticket's requests go out without waiting
    api.clock()
    assert slept == [60.0] and alpaca_orders.WINDOW_BUDGET < alpaca_orders.RATE_LIMIT_PER_MINUTE
