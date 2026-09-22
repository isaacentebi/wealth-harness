"""Order brokers beyond Alpaca: the IBKR Client Portal gateway and place-it-yourself (manual) tickets.

Every IBKR test runs against :class:`FakeGateway`, an in-memory transport for
``https://localhost:5000/v1/api``; manual tickets get injected reference prices
and FX.  The real transports are replaced for the whole module, so nothing here
can reach a broker, a gateway or a market-data provider.
"""
from __future__ import annotations

import json
from datetime import timedelta
from decimal import Decimal
from urllib.parse import parse_qs, urlsplit

import pytest

from test_execution import NOW, FakeAlpaca, PAPER_ENV, db, no_network  # noqa: F401 - fixtures and the network guard
from wealth.execution import tickets
from wealth.execution.brokers import alpaca_orders, base, ibkr_gateway, manual
from wealth.execution.brokers.base import BrokerError, OrderBroker
from wealth.store import WealthStore

PAPER_ACCOUNT, LIVE_ACCOUNT = "DU1234567", "U7654321"
# Wealth names an IBKR account ibkr-<last four> (the Flex connector and the gateway adapter agree), so the paper login
# is ibkr-4567 and the live one ibkr-4321.
PAPER_LEDGER_ID, LIVE_LEDGER_ID = "ibkr-4567", "ibkr-4321"
IBKR_FACTS = {"facts": [{"key": f"account.{i}", "value": {"institution": "Interactive Brokers"}}
                        for i in (PAPER_LEDGER_ID, LIVE_LEDGER_ID)]}


class FakeGateway:
    """The Client Portal Web API in memory: a transport, never a socket."""

    def __init__(self, account=PAPER_ACCOUNT, prices=None, cash="10000", replies=None):
        self.account = account
        self.prices = {"VTI": "250.00", "BND": "72.00", **(prices or {})}
        self.conids = {s: str(1000 + i) for i, s in enumerate(sorted(self.prices))}
        self.cash = cash
        self.positions = []
        self.orders: dict[str, dict] = {}
        self.replies = list(replies or [])  # message-id lists the next order answers with, one question each
        self.pending: dict[str, dict] = {}
        self.calls: list[dict] = []
        self.subscribed: set[str] = set()
        self.next_id = 100
        self.availability = "RpB"  # field 6509: real-time

    def __call__(self, method, url, headers, body, timeout):
        parts = urlsplit(url)
        assert parts.scheme == "https" and parts.hostname == "localhost" and parts.port == 5000, url
        assert ibkr_gateway.allowed(method, parts.path), (method, parts.path)
        path = parts.path[len("/v1/api"):]
        query = {k: v[0] for k, v in parse_qs(parts.query).items()}
        payload = json.loads(body) if body else None
        self.calls.append({"method": method, "path": path, "query": query, "body": payload})
        acct = self.account
        if path == "/iserver/auth/status":
            return self._json({"authenticated": True, "connected": True, "competing": False})
        if path == "/iserver/accounts":
            return self._json({"accounts": [acct], "selectedAccount": acct})
        if path == "/portfolio/accounts":
            return self._json([{"id": acct}])
        if path == f"/portfolio/{acct}/ledger":
            return self._json({"USD": {"cashbalance": float(self.cash), "settledcash": float(self.cash)}})
        if path == f"/portfolio/{acct}/positions/0":
            return self._json(self.positions)
        if path == "/iserver/secdef/search":
            symbol = query["symbol"].replace(" ", ".")
            if symbol not in self.conids:
                return self._json([])
            return self._json([{"conid": self.conids[symbol], "symbol": query["symbol"], "description": "ARCA",
                                "sections": [{"secType": "STK"}]}])
        if path == "/iserver/marketdata/snapshot":
            conid = query["conids"]
            if conid not in self.subscribed:  # the first snapshot only subscribes
                self.subscribed.add(conid)
                return self._json([{"conid": int(conid)}])
            symbol = next(s for s, c in self.conids.items() if c == conid)
            return self._json([{"conid": int(conid), "31": self.prices[symbol], "6509": self.availability,
                                "_updated": NOW.timestamp() * 1000}])
        if method == "GET" and path == "/iserver/account/orders":
            return self._json({"orders": [dict(o) for o in self.orders.values()], "snapshot": True})
        if method == "POST" and path == f"/iserver/account/{acct}/orders":
            order = payload["orders"][0]
            if self.replies:
                reply_id = f"reply-{len(self.pending) + 1}"
                ids = self.replies.pop(0)
                self.pending[reply_id] = order
                return self._json([{"id": reply_id, "message": [f"Notice {i}: please confirm." for i in ids],
                                    "isSuppressed": False, "messageIds": ids}])
            return self._json(self._place(order))
        if method == "POST" and path.startswith("/iserver/reply/"):
            reply_id = path.rsplit("/", 1)[1]
            order = self.pending.pop(reply_id)
            if not payload.get("confirmed"):
                return self._json([{"order_status": "cancelled"}])
            if self.replies:
                return self(method, url.replace(f"/iserver/reply/{reply_id}", f"/iserver/account/{acct}/orders"),
                            headers, json.dumps({"orders": [order]}).encode(), timeout)
            return self._json(self._place(order))
        if method == "GET" and path.startswith("/iserver/account/order/status/"):
            order = self.orders.get(path.rsplit("/", 1)[1])
            return self._json({"order_id": int(order["orderId"]), "order_status": order["status"],
                               "cum_fill": order["filledQuantity"], "average_price": order["avgPrice"]}) \
                if order else (404, b'{"error": "not found"}')
        if method == "DELETE" and path.startswith(f"/iserver/account/{acct}/order/"):
            order = self.orders.get(path.rsplit("/", 1)[1])
            if not order:
                return 404, b'{"error": "not found"}'
            order["status"] = "Cancelled"
            return self._json({"msg": "Request was submitted", "order_id": int(order["orderId"])})
        return 404, json.dumps({"error": f"unexpected {method} {path}"}).encode()

    def _place(self, order):
        self.next_id += 1
        order_id = str(self.next_id)
        symbol = next(s for s, c in self.conids.items() if c == str(order["conid"]))
        self.orders[order_id] = {"orderId": int(order_id), "conid": order["conid"], "ticker": symbol.replace(".", " "),
                                 "side": order["side"], "status": "Submitted", "order_ref": order["cOID"],
                                 "filledQuantity": 0, "avgPrice": None, "totalSize": order["quantity"],
                                 "price": order.get("price"), "orderType": order["orderType"]}
        return [{"order_id": order_id, "order_status": "Submitted", "encrypt_message": "1"}]

    def fill(self, order_id, price=None):
        order = self.orders[str(order_id)]
        order.update(status="Filled", filledQuantity=order["totalSize"], avgPrice=price or order["price"])

    def order_posts(self):
        return [c for c in self.calls if c["method"] == "POST" and c["path"].endswith("/orders")]

    @staticmethod
    def _json(value):
        return 200, json.dumps(value).encode()


@pytest.fixture
def gateway(monkeypatch):
    fake = FakeGateway()
    monkeypatch.setattr(ibkr_gateway, "default_transport", fake)
    monkeypatch.delenv("WEALTH_TRADING_LIVE", raising=False)
    return fake


def ibkr_ticket(db, orders, *, snapshot=IBKR_FACTS, environ=None, now=NOW, account=None):
    orders = [{"account_id": account or PAPER_LEDGER_ID, **o} for o in orders]
    with WealthStore(db) as store:
        return tickets.create_ticket(store, "ana", {"orders": orders, "rationale": "Invest the surplus at IBKR."},
                                     snapshot=snapshot, environ=environ, now=now)["result"]["ticket"]


def nonce_of(db, ticket_id, now=NOW):
    with WealthStore(db) as store:
        return next(t["nonce"] for t in tickets.list_tickets(store, "ana", include_nonce=True, now=now)
                    if t["id"] == ticket_id)


def confirm(db, ticket_id, *, now=NOW, environ=None, **kwargs):
    nonce = kwargs.pop("nonce", None) or nonce_of(db, ticket_id, now)
    with WealthStore(db) as store:
        return tickets.confirm(store, "ana", ticket_id, nonce=nonce, snapshot=kwargs.pop("snapshot", IBKR_FACTS),
                               environ=environ, now=now, **kwargs)


# -- the interface ------------------------------------------------------------------------------

def test_every_adapter_satisfies_the_order_broker_protocol(monkeypatch):
    monkeypatch.setattr(alpaca_orders, "default_transport", FakeAlpaca())
    alpaca = alpaca_orders.broker("paper", environ=PAPER_ENV)
    assert isinstance(alpaca, OrderBroker) and alpaca.name == "alpaca" and alpaca.submits
    assert isinstance(ibkr_gateway.broker(environ={}), OrderBroker)
    by_hand = manual.ManualBroker(institution="GBM", quote=lambda *a: None, fx=lambda *a: None)
    assert isinstance(by_hand, OrderBroker) and not by_hand.submits and by_hand.mode == "manual"
    with pytest.raises(BrokerError):
        by_hand.submit({"symbol": "VOO"})


def test_the_institution_picks_the_adapter():
    assert base.kind_for_institution("Alpaca Securities") == "alpaca"
    assert base.kind_for_institution("Interactive Brokers LLC") == "ibkr"
    assert base.kind_for_institution("IBKR") == "ibkr"
    for other in ("GBM", "Vest", "Charles Schwab", "Fidelity", None):
        assert base.kind_for_institution(other) == "manual"
    assert base.institution_profile("GBM+")["market"] == "MX"
    assert base.institution_profile("gbm-4321")["label"] == "GBM"
    assert base.institution_profile("Vest")["market"] == "US"


def test_a_ticket_holds_one_account_and_an_unknown_account_stays_with_the_default_broker(db, gateway, monkeypatch):
    with WealthStore(db) as store, pytest.raises(ValueError, match="one account"):
        tickets.create_ticket(store, "ana", {"rationale": "x", "orders": [
            {"symbol": "VTI", "side": "buy", "qty": 1, "account_id": PAPER_LEDGER_ID},
            {"symbol": "BND", "side": "buy", "qty": 1, "account_id": "gbm-1"}]}, snapshot=IBKR_FACTS, now=NOW)
    monkeypatch.setattr(alpaca_orders, "default_transport", FakeAlpaca())
    for key, value in PAPER_ENV.items():
        monkeypatch.setenv(key, value)
    with WealthStore(db) as store:
        view = tickets.create_ticket(store, "ana", {"rationale": "x", "orders": [
            {"symbol": "VTI", "side": "buy", "qty": 1, "account_id": "brk"}]}, snapshot={"facts": []},
            now=NOW)["result"]["ticket"]
    assert view["broker"] == "alpaca" and "account_unmatched" in {n["code"] for n in view["notices"]}
    assert gateway.calls == []


# -- IBKR ------------------------------------------------------------------------------------------

def test_ibkr_happy_path_prices_places_and_reads_status(db, gateway):
    view = ibkr_ticket(db, [{"symbol": "VTI", "side": "buy", "qty": 2}])
    assert view["broker"] == "ibkr" and view["mode"] == "paper" and not view["blocked"], view["notices"]
    assert view["account"]["number"] == "****4567" and PAPER_ACCOUNT not in json.dumps(view)
    line = view["lines"][0]
    assert line["limit_price"] == "251.25" and line["qty"] == "2" and line["exchange"] == "ARCA"
    assert gateway.order_posts() == []  # a ticket only reads

    placed = confirm(db, view["id"])
    body = gateway.order_posts()[0]["body"]["orders"][0]
    assert body == {"acctId": PAPER_ACCOUNT, "conid": int(gateway.conids["VTI"]), "cOID": f"wealth-{view['id']}-0",
                    "side": "BUY", "orderType": "LMT", "tif": "DAY", "quantity": 2.0, "outsideRTH": False,
                    "price": 251.25}
    line = placed["lines"][0]
    assert placed["status"] == "submitted" and line["state"] == "sent" and line["broker_order_id"] == "101"

    gateway.fill("101", price=251.0)
    with WealthStore(db) as store:
        after = next(t for t in tickets.refresh(store, "ana", now=NOW) if t["id"] == view["id"])
        events = store.order_events("ana", view["id"])
    assert after["lines"][0]["state"] == "filled" and after["lines"][0]["filled_avg_price"] == "251"
    assert after["status"] == "done"  # IBKR fills reach the ledger with the next Flex sync, not from here
    assert PAPER_ACCOUNT not in json.dumps([e["payload"] for e in events])  # audit rows mask the account


@pytest.mark.parametrize("message_id", ["o163", "o354", "o403", "o10151", "o10153", "o10331", "o2137", "o10334",
                                        "p6", "p12"])
def test_ibkr_never_confirms_a_reply_within_the_tap(db, gateway, message_id):
    # o163 (price percentage limit) means Wealth's price disagreed with IBKR's; o10334 is the omnibus (wrong)
    # account; p6 and p12 are precautions too.  Every one is answered confirmed=false and shown.
    gateway.replies = [[message_id]]
    view = ibkr_ticket(db, [{"symbol": "VTI", "side": "buy", "qty": 1}])
    placed = confirm(db, view["id"])
    replies = [c for c in gateway.calls if c["path"].startswith("/iserver/reply/")]
    assert [c["body"] for c in replies] == [{"confirmed": False}]
    line = placed["lines"][0]
    assert gateway.orders == {} and line["state"] == "failed"
    assert f"Notice {message_id}" in line["reason"] and "not placed" in line["reason"]
    assert "TWS" not in line["reason"]


@pytest.mark.parametrize("message_id", ["o383", "o451"])
def test_ibkr_tws_precaution_is_blocked_with_a_note_about_presets(db, gateway, message_id):
    gateway.replies = [[message_id]]
    view = ibkr_ticket(db, [{"symbol": "VTI", "side": "buy", "qty": 1}])
    line = confirm(db, view["id"])["lines"][0]
    replies = [c for c in gateway.calls if c["path"].startswith("/iserver/reply/")]
    assert [c["body"] for c in replies] == [{"confirmed": False}] and gateway.orders == {}
    assert line["state"] == "failed" and f"Notice {message_id}" in line["reason"]
    assert "place the order in TWS yourself" in line["reason"] and "presets" in line["reason"]
    assert ibkr_gateway.BENIGN_REPLIES == frozenset()


def test_ibkr_unknown_reply_is_refused_and_shown(db, gateway):
    gateway.replies = [["o354"]]
    view = ibkr_ticket(db, [{"symbol": "VTI", "side": "buy", "qty": 1}])
    placed = confirm(db, view["id"])
    replies = [c for c in gateway.calls if c["path"].startswith("/iserver/reply/")]
    assert [c["body"] for c in replies] == [{"confirmed": False}]
    line = placed["lines"][0]
    assert gateway.orders == {} and line["state"] == "failed"
    assert "Notice o354" in line["reason"] and "not placed" in line["reason"]
    with WealthStore(db) as store:
        blocked = [e for e in store.order_events("ana", view["id"]) if e["event"] == "reply_blocked"]
    assert blocked and blocked[0]["payload"]["body"]["message_ids"] == ["o354"]


def test_ibkr_live_account_needs_the_opt_in_and_keeps_the_limits(db, gateway):
    gateway.account = LIVE_ACCOUNT
    view = ibkr_ticket(db, [{"symbol": "VTI", "side": "buy", "qty": 2}], account=LIVE_LEDGER_ID)
    assert view["mode"] == "live" and view["blocked"]
    assert "live_disabled" in {n["code"] for n in view["notices"]}
    with pytest.raises(tickets.ConfirmError) as off:
        confirm(db, view["id"])
    assert off.value.kind == "live_disabled" and gateway.order_posts() == []

    live = {"WEALTH_TRADING_LIVE": "1"}
    big = ibkr_ticket(db, [{"symbol": "VTI", "side": "buy", "qty": 5}], account=LIVE_LEDGER_ID,
                      environ=live)  # about USD 1,256
    assert big["mode"] == "live" and "live_order_limit" in {n["code"] for n in big["notices"]}
    market = ibkr_ticket(db, [{"symbol": "BND", "side": "buy", "qty": 1, "type": "market"}], account=LIVE_LEDGER_ID,
                         environ=live)
    assert "live_needs_limit" in {n["code"] for n in market["notices"]}

    ok = ibkr_ticket(db, [{"symbol": "VTI", "side": "buy", "qty": 1}], account=LIVE_LEDGER_ID,
                     environ={"WEALTH_TRADING_LIVE": "ibkr"})
    assert not ok["blocked"] and ok["needs_typed"]
    with pytest.raises(tickets.ConfirmError) as typed:
        confirm(db, ok["id"], environ=live)
    assert typed.value.kind == "typed"
    placed = confirm(db, ok["id"], environ=live, typed="EN VIVO")
    assert placed["lines"][0]["state"] == "sent"
    with WealthStore(db) as store:
        assert tickets.execution_status(store, "ana", environ=live, now=NOW)["live_used_today_usd"] == "251.25"
    # "1" opts in IBKR only: Alpaca stays paper
    assert tickets.trading_mode(live) == "paper"


def test_ibkr_paper_ticket_is_not_placed_after_the_gateway_switches_to_a_live_login(db, gateway):
    view = ibkr_ticket(db, [{"symbol": "VTI", "side": "buy", "qty": 1}])
    gateway.account = LIVE_ACCOUNT
    with pytest.raises(tickets.ConfirmError) as changed:
        confirm(db, view["id"], environ={"WEALTH_TRADING_LIVE": "ibkr"})
    assert changed.value.kind == "account_changed" and gateway.order_posts() == []


def test_ibkr_price_moved_sends_nothing_until_a_fresh_tap(db, gateway):
    view = ibkr_ticket(db, [{"symbol": "VTI", "side": "buy", "notional": 1000}])
    assert view["lines"][0]["qty"] == "3"
    gateway.prices["VTI"] = "240.00"  # 4% lower: the collared limit moves more than the 1% collar
    with pytest.raises(tickets.ConfirmError) as moved:
        confirm(db, view["id"])
    assert moved.value.kind == "price_moved" and gateway.order_posts() == []
    fresh = moved.value.ticket
    assert fresh["lines"][0]["limit_price"] == "241.2" and fresh["lines"][0]["qty"] == "4"
    placed = confirm(db, view["id"], nonce=fresh["nonce"])
    assert placed["lines"][0]["state"] == "sent" and gateway.order_posts()[0]["body"]["orders"][0]["quantity"] == 4.0


def test_ibkr_cancel_reaches_one_order(db, gateway):
    view = ibkr_ticket(db, [{"symbol": "VTI", "side": "buy", "qty": 1}])
    order_id = confirm(db, view["id"])["lines"][0]["broker_order_id"]
    with WealthStore(db) as store:
        after = tickets.cancel(store, "ana", order_id, now=NOW)
    deletes = [c for c in gateway.calls if c["method"] == "DELETE"]
    assert [c["path"] for c in deletes] == [f"/iserver/account/{PAPER_ACCOUNT}/order/{order_id}"]
    assert after["lines"][0]["state"] == "canceled" and after["status"] == "done"


def test_ibkr_sells_are_checked_against_gateway_positions(db, gateway):
    gateway.positions = [{"contractDesc": "VTI", "position": 1.0, "mktValue": 250.0}]
    view = ibkr_ticket(db, [{"symbol": "VTI", "side": "sell", "qty": 2}])
    assert "sell_exceeds_position" in {n["code"] for n in view["notices"]} and view["blocked"]


def test_ibkr_gateway_down_is_unknown_and_blocks(db, monkeypatch):
    def down(*_):
        raise BrokerError("Could not reach the IBKR gateway on localhost (ConnectionRefusedError).", retryable=True)

    monkeypatch.setattr(ibkr_gateway, "default_transport", down)
    view = ibkr_ticket(db, [{"symbol": "VTI", "side": "buy", "qty": 1}])
    notices = {n["code"]: n for n in view["notices"]}
    assert view["blocked"] and "gateway" in notices["broker_unavailable"]["message"]


def test_ibkr_transport_and_tls_rules():
    tls = ibkr_gateway.TlsPolicy.from_env({"WEALTH_IBKR_GATEWAY_CERT_SHA256": "AB:" * 31 + "CD"})
    assert tls.pin == "ab" * 31 + "cd" and tls.describe() == "pinned"
    assert ibkr_gateway.TlsPolicy.from_env({"WEALTH_IBKR_GATEWAY_INSECURE_LOCALHOST": "1"}).describe() \
        == "localhost-unverified"
    assert ibkr_gateway.TlsPolicy.from_env({}).describe() == "system-trust"
    with pytest.raises(BrokerError, match="localhost"):
        ibkr_gateway.https_transport("GET", "https://example.com:5000/v1/api/iserver/accounts", {}, None, 1, tls=tls)
    with pytest.raises(BrokerError, match="Refusing"):
        ibkr_gateway.https_transport("POST", "https://localhost:5000/v1/api/iserver/account/DU1/transfer", {}, None, 1,
                                     tls=tls)
    assert not ibkr_gateway.allowed("DELETE", "/v1/api/iserver/account/DU1234567/orders")
    assert ibkr_gateway.allowed("POST", "/v1/api/iserver/reply/abc-1")
    broker = ibkr_gateway.broker(environ={"WEALTH_IBKR_GATEWAY_PORT": "5055"})
    assert broker.base == "https://localhost:5055/v1/api"


# -- manual (place it yourself) ---------------------------------------------------------------------

GBM_ACCOUNT = {"id": "gbm-1", "institution": "GBM", "type": "brokerage", "currency": "MXN", "country": "MX",
               "owners": [{"person_id": "self", "share": "1"}]}


def _seed_gbm(db, cash="50000"):
    with WealthStore(db) as store:
        store.post_ledger("ana", {
            "batch_id": "gbm-statement-aug", "source": {"kind": "document", "ref": "gbm-aug.pdf",
                                                        "observed_on": "2026-09-01"},
            "accounts": [GBM_ACCOUNT],
            "instruments": [{"id": "WALMEX", "symbol": "WALMEX", "currency": "MXN", "venue": "bmv"}],
            "transactions": [
                {"kind": "deposit", "account_id": "gbm-1", "date": "2026-08-01", "amount": cash, "currency": "MXN",
                 "external_id": "GBM-D1"},
                {"kind": "buy", "account_id": "gbm-1", "date": "2026-08-02", "instrument_id": "WALMEX",
                 "quantity": "100", "price": "60", "amount": "-6000", "currency": "MXN", "external_id": "GBM-B1"}]})


@pytest.fixture
def quotes(monkeypatch):
    prices = {("VOO", "SIC"): "9500.00", ("WALMEX", "BMV"): "61.00"}

    def quote(db_path):
        return lambda symbol, listing, currency: (
            {"price": prices[(symbol, listing)], "date": "2026-09-18", "currency": "MXN", "source": "fake"}
            if (symbol, listing) in prices else None)

    monkeypatch.setattr(manual, "default_quote", quote)
    monkeypatch.setattr(manual, "default_fx", lambda db_path: lambda base_ccy, quote_ccy: Decimal("19"))
    return prices


def gbm_ticket(db, orders, now=NOW):
    with WealthStore(db) as store:
        return tickets.create_ticket(store, "ana", {"orders": [{"account_id": "gbm-1", **o} for o in orders],
                                                    "rationale": "Add to the S&P 500 through GBM."},
                                     snapshot={"facts": []}, now=now)["result"]["ticket"]


def test_an_order_may_name_the_currency_the_broker_prices_in(db, quotes, gateway):
    """An outside host wrote "currency": "MXN" with "10 mil pesos en GBM" and was refused twice (unknown field)."""
    _seed_gbm(db)
    view = gbm_ticket(db, [{"symbol": "VOO", "side": "buy", "notional": 20000, "currency": "MXN"}])
    assert view["lines"][0]["currency"] == "MXN" and view["lines"][0]["qty"] == "2"
    with pytest.raises(ValueError, match="orders at this broker are in MXN, not USD"):
        gbm_ticket(db, [{"symbol": "VOO", "side": "buy", "notional": 1000, "currency": "USD"}])


def test_manual_ticket_is_a_clean_place_it_yourself_card(db, quotes, gateway):
    _seed_gbm(db)
    view = gbm_ticket(db, [{"symbol": "VOO", "side": "buy", "notional": 20000}])
    assert view["broker"] == "manual" and view["broker_label"] == "GBM" and view["mode"] == "manual"
    line = view["lines"][0]
    assert line["exchange"] == "SIC" and line["listing_note"] == "Compra en el SIC" and line["currency"] == "MXN"
    assert line["limit_price"] == "9547.5" and line["qty"] == "2" and line["estimated_amount"] == "19095"
    assert line["estimated_fee"] == "55.38"  # 0.25% + 16% IVA
    assert line["fx"] == {"pair": "USD/MXN", "rate": "19", "usd_amount": "1005"}
    assert view["total"]["currency"] == "MXN" and view["total"]["estimated_fee"] == "55.38"
    assert not view["blocked"], view["notices"]
    assert "listing_assumed" in {n["code"] for n in view["notices"]}  # VOO was not said to be SIC
    assert "Compra 2 VOO en el SIC · límite MXN 9547.5" in view["manual"]["es"]
    assert "Buy 2 VOO on the SIC" in view["manual"]["en"] and "gbm-1" in view["manual"]["en"]
    assert gateway.calls == []

    bmv = gbm_ticket(db, [{"symbol": "WALMEX.MX", "side": "sell", "qty": 150, "exchange": "BMV"}])
    assert bmv["lines"][0]["exchange"] == "BMV" and "listing_note" not in bmv["lines"][0]
    assert "sell_exceeds_position" in {n["code"] for n in bmv["notices"]}  # the ledger holds 100


def test_manual_ya_la_puse_records_a_pending_trade_that_a_statement_confirms(db, quotes, gateway):
    _seed_gbm(db)
    view = gbm_ticket(db, [{"symbol": "VOO", "side": "buy", "qty": 2, "exchange": "SIC"}])
    placed = confirm(db, view["id"], snapshot={"facts": []})
    assert placed["status"] == "placed" and placed["lines"][0]["state"] == "awaiting"
    assert placed["manual"]["placed_at"] and gateway.calls == []
    with pytest.raises(tickets.ConfirmError) as again:
        confirm(db, view["id"], nonce="ABCDEF12", snapshot={"facts": []})
    assert again.value.kind == "used"

    later = NOW + timedelta(days=3)
    with WealthStore(db) as store:
        still = next(t for t in tickets.refresh(store, "ana", now=later) if t["id"] == view["id"])
        assert still["status"] == "placed"  # nothing in the ledger yet
        store.post_ledger("ana", {
            "batch_id": "gbm-statement-sep", "source": {"kind": "document", "ref": "gbm-sep.pdf",
                                                        "observed_on": "2026-10-01"},
            "instruments": [{"id": "VOO-SIC", "symbol": "VOO", "currency": "MXN", "venue": "sic"}],
            "transactions": [{"kind": "buy", "account_id": "gbm-1", "date": "2026-09-21", "instrument_id": "VOO-SIC",
                              "quantity": "2", "price": "9530", "amount": "-19060", "currency": "MXN",
                              "external_id": "GBM-B2"}]})
        tickets.refresh(store, "ana", now=later)
        done = tickets.ticket_status(store, "ana", view["id"], now=later)
        events = [e["event"] for e in store.order_events("ana", view["id"])]
    line = done["lines"][0]
    assert done["status"] == "done" and line["state"] == "filled"
    assert line["filled_qty"] == "2" and line["filled_avg_price"] == "9530" and line["confirmed_by"] == ["GBM-B2"]
    assert "placed_manually" in events and "reconciled" in events


def test_a_manual_trade_no_statement_shows_becomes_unconfirmed_and_can_be_withdrawn(db, quotes):
    _seed_gbm(db)
    first = gbm_ticket(db, [{"symbol": "VOO", "side": "buy", "qty": 1, "exchange": "SIC"}])
    confirm(db, first["id"], snapshot={"facts": []})
    with WealthStore(db) as store:
        late = NOW + timedelta(days=tickets.MANUAL_CONFIRM_DAYS + 1)
        tickets.refresh(store, "ana", now=late)
        view = tickets.ticket_status(store, "ana", first["id"], now=late)
    assert view["status"] == "done" and view["lines"][0]["state"] == "unconfirmed"

    second = gbm_ticket(db, [{"symbol": "WALMEX", "side": "buy", "qty": 10}])
    assert second["lines"][0]["exchange"] == "BMV"  # a known Mexican issuer defaults to the BMV
    confirm(db, second["id"], snapshot={"facts": []})
    with WealthStore(db) as store:
        withdrawn = tickets.cancel(store, "ana", second["id"], now=NOW)
    assert withdrawn["status"] == "done" and withdrawn["lines"][0]["state"] == "canceled"


def test_manual_ticket_without_a_reference_price_is_placed_at_the_brokers_price(db, quotes):
    # The person places it and sees GBM's screen: no price is a quiet line ("precio al momento de colocar"),
    # never a block, and no amount is invented.
    _seed_gbm(db)
    view = gbm_ticket(db, [{"symbol": "IVV", "side": "buy", "qty": 1, "exchange": "SIC"}])
    notices = {n["code"]: n for n in view["notices"]}
    assert not view["blocked"], view["notices"]
    assert notices["price_unknown"]["status"] == "warn"
    assert "precio al momento de colocar" in notices["price_unknown"]["message"]
    line = view["lines"][0]
    assert line["price_note"] == "precio al momento de colocar" and "estimated_amount" not in line
    assert "limit_price" not in line and "precio al momento de colocar" in view["manual"]["es"]
    given = gbm_ticket(db, [{"symbol": "IVV", "side": "buy", "qty": 1, "exchange": "SIC", "limit_price": "11000"}])
    assert not given["blocked"] and given["lines"][0]["estimated_amount"] == "11000"


def test_a_us_broker_without_an_api_is_a_dollar_card(db, quotes, monkeypatch):
    monkeypatch.setattr(manual, "default_quote", lambda db_path: lambda s, listing, ccy: {
        "price": "500", "date": "2026-09-18", "currency": "USD"})
    snapshot = {"facts": [{"key": "account.vest-1", "value": {"institution": "Vest"}}]}
    with WealthStore(db) as store:
        view = tickets.create_ticket(store, "ana", {"rationale": "x", "orders": [
            {"symbol": "VOO", "side": "buy", "qty": 1, "account_id": "vest-1"}]}, snapshot=snapshot,
            now=NOW)["result"]["ticket"]
    line = view["lines"][0]
    assert view["broker_label"] == "Vest" and line["currency"] == "USD" and line["exchange"] == "US"
    assert line["estimated_fee"] == "0" and "fx" not in line
    assert "buying_power_unknown" in {n["code"] for n in view["notices"]} and not view["blocked"]
