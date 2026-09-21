"""Execution hardening (QA round 2): daily-limit races, interrupted submissions, fill paging, stale prices,
ticker near-misses, and a fill posted before the first connector sync.

Everything runs on the in-memory fakes: no network, no real or paper orders.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
import threading
import time

import pytest

from test_execution import (LIVE_ENV, NOW, PAPER_ENV, FakeAlpaca, confirm, db, fake, make_ticket,  # noqa: F401
                            no_network, nonce_of, paper)
from wealth.execution import tickets
from wealth.execution.brokers import alpaca_orders
from wealth.store import WealthStore


def _live(monkeypatch):
    for key, value in LIVE_ENV.items():
        monkeypatch.setenv(key, value)


def _state(db):
    with WealthStore(db) as store:
        return store.auxiliary("ana", "execution")


# -- 1. the live daily limit holds under concurrent confirmations ----------------------------------

def test_concurrent_live_confirms_cannot_exceed_the_daily_limit(db, monkeypatch):
    _live(monkeypatch)
    broker = FakeAlpaca(prices={s: "90.00" for s in ("AAA", "BBB", "CCC", "DDD", "EEE", "FFF")}, buying_power="100000")
    lock, slow = threading.Lock(), {"on": False}

    def transport(method, url, headers, body, timeout):
        if slow["on"] and url.endswith("/v2/account"):
            time.sleep(0.3)  # both confirmations pass their checks before either claims its ticket
        with lock:
            return broker(method, url, headers, body, timeout)

    monkeypatch.setattr(alpaca_orders, "default_transport", transport)
    # Each ticket is USD 2,713.50 (3 x 10 x 90.45): one fits under USD 5,000, two do not.
    first = make_ticket(db, [{"symbol": s, "side": "buy", "qty": 10} for s in ("AAA", "BBB", "CCC")])
    second = make_ticket(db, [{"symbol": s, "side": "buy", "qty": 10} for s in ("DDD", "EEE", "FFF")])
    ids = [first["result"]["ticket"]["id"], second["result"]["ticket"]["id"]]
    with WealthStore(db) as store:  # one listing issues both cards' codes (showing a card again replaces its code)
        issued = {t["id"]: t["nonce"] for t in tickets.list_tickets(store, "ana", include_nonce=True, now=NOW)}
    nonces = [issued[i] for i in ids]
    slow["on"] = True
    outcome: dict[str, str] = {}

    def go(ticket_id, nonce):
        try:
            outcome[ticket_id] = confirm(db, ticket_id, nonce, typed="LIVE")["status"]
        except tickets.ConfirmError as exc:
            outcome[ticket_id] = exc.kind

    threads = [threading.Thread(target=go, args=pair) for pair in zip(ids, nonces)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(outcome.values()) == ["blocked", "submitted"]
    posted = sum(Decimal(c["body"]["qty"]) * Decimal(c["body"]["limit_price"]) for c in broker.posts())
    assert len(broker.posts()) == 3 and posted <= Decimal(5000)
    state = _state(db)
    winner = next(i for i in ids if outcome[i] == "submitted")
    loser = next(i for i in ids if outcome[i] == "blocked")
    assert set(state["live_reserved"]) == {winner} and state["live_reserved"][winner]["notional"] == "2713.5"
    assert state["tickets"][loser]["status"] == "pending"  # nothing sent; it can be confirmed another day
    with WealthStore(db) as store:
        assert tickets.execution_status(store, "ana", now=NOW)["live_used_today_usd"] == "2713.5"


def test_a_live_order_that_never_reaches_the_broker_gives_its_reservation_back(db, monkeypatch):
    _live(monkeypatch)
    broker = FakeAlpaca()

    def transport(method, url, headers, body, timeout):
        if method == "POST":
            return 403, json.dumps({"message": "insufficient buying power"}).encode()
        return broker(method, url, headers, body, timeout)

    monkeypatch.setattr(alpaca_orders, "default_transport", transport)
    ticket_id = make_ticket(db, [{"symbol": "VTI", "side": "buy", "qty": 3}])["result"]["ticket"]["id"]
    placed = confirm(db, ticket_id, nonce_of(db, ticket_id), typed="LIVE")
    assert placed["lines"][0]["state"] == "failed"
    assert _state(db)["live_reserved"][ticket_id]["notional"] == "0"
    with WealthStore(db) as store:
        assert tickets.execution_status(store, "ana", now=NOW)["live_used_today_usd"] == "0"


# -- 2. a crash mid-submit is reconciled by client_order_id ---------------------------------------

def test_refresh_reconciles_a_ticket_interrupted_mid_submit(db, paper, monkeypatch):
    posts = {"n": 0}

    def transport(method, url, headers, body, timeout):
        if method == "POST":
            posts["n"] += 1
            if posts["n"] == 2:
                raise MemoryError("the process dies mid-submit")
        return paper(method, url, headers, body, timeout)

    monkeypatch.setattr(alpaca_orders, "default_transport", transport)
    ticket_id = make_ticket(db, [{"symbol": "VTI", "side": "buy", "qty": 1},
                                 {"symbol": "BND", "side": "buy", "qty": 1}])["result"]["ticket"]["id"]
    with pytest.raises(MemoryError):
        confirm(db, ticket_id, nonce_of(db, ticket_id))
    assert _state(db)["tickets"][ticket_id]["status"] == "submitting"
    order_id = next(iter(paper.orders))
    paper.fill(order_id, price="250.00")
    with WealthStore(db) as store:
        view = next(t for t in tickets.refresh(store, "ana", now=NOW) if t["id"] == ticket_id)
        entries = store.ledger("ana")["entries"]
    assert view["status"] == "done"
    assert [(l["symbol"], l["state"]) for l in view["lines"]] == [("VTI", "filled"), ("BND", "failed")]
    assert view["lines"][0]["broker_order_id"] == order_id
    assert [(e["kind"], e["instrument_id"], e["quantity"]) for e in entries] == [("buy", "VTI", "1")]


def test_a_stale_submitting_ticket_is_reconciled_only_after_its_lease(db, paper):
    ticket_id = make_ticket(db, [{"symbol": "VTI", "side": "buy", "qty": 1}])["result"]["ticket"]["id"]
    placed = confirm(db, ticket_id, nonce_of(db, ticket_id))
    order_id = placed["lines"][0]["broker_order_id"]

    def back_to_submitting(state):  # as if the process was killed right after the POST
        items = dict(state["tickets"])
        line = {k: v for k, v in items[ticket_id]["lines"][0].items() if k not in ("broker_order_id", "state")}
        items[ticket_id] = {**items[ticket_id], "status": "submitting", "lines": [{**line, "state": "proposed"}]}
        return {**state, "tickets": items}

    with WealthStore(db) as store:
        store.update_auxiliary("ana", "execution", back_to_submitting)
        tickets.refresh(store, "ana", now=NOW)  # within the lease: someone may still be submitting it
        assert store.auxiliary("ana", "execution")["tickets"][ticket_id]["status"] == "submitting"
        later = NOW + tickets.SUBMIT_LEASE + timedelta(seconds=1)
        view = next(t for t in tickets.refresh(store, "ana", now=later) if t["id"] == ticket_id)
    assert view["status"] == "submitted" and view["lines"][0]["broker_order_id"] == order_id


# -- 3. fills are paged, read errors are retried, the ticket stays open until all are posted -----------

def test_fills_are_read_across_pages_and_a_read_error_keeps_the_ticket_open(db, paper):
    for i in range(150):  # other trading on the same account the same day
        paper.activities.append({"id": f"20260921140000{i:03d}::00000000-0000-4000-8000-{i:012d}",
                                 "activity_type": "FILL", "order_id": f"other-{i}", "symbol": "BND", "side": "buy",
                                 "qty": "1", "price": "72", "transaction_time": "2026-09-21T14:00:00Z",
                                 "type": "fill"})
    ticket_id = make_ticket(db, [{"symbol": "VTI", "side": "buy", "qty": 1}])["result"]["ticket"]["id"]
    order_id = confirm(db, ticket_id, nonce_of(db, ticket_id))["lines"][0]["broker_order_id"]
    paper.fill(order_id, price="250.00")
    paper.fail_fills = True
    with WealthStore(db) as store:
        view = next(t for t in tickets.refresh(store, "ana", now=NOW) if t["id"] == ticket_id)
        assert view["status"] == "submitted"  # filled at the broker but not yet in the ledger: not done
        assert store.auxiliary("ana", "execution")["tickets"][ticket_id]["lines"][0]["fills_error"]
        assert store.ledger("ana")["entries"] == []
    paper.fail_fills = False
    with WealthStore(db) as store:
        view = next(t for t in tickets.refresh(store, "ana", now=NOW) if t["id"] == ticket_id)
        entries = store.ledger("ana")["entries"]
    assert view["status"] == "done" and [(e["instrument_id"], e["quantity"]) for e in entries] == [("VTI", "1")]
    pages = [c["query"].get("page_token") for c in paper.calls if c["path"] == "/v2/account/activities/FILL"]
    assert len([p for p in pages if p]) >= 1  # it followed page_token past the first 100


def test_a_fill_page_that_does_not_advance_is_an_error_not_an_empty_list(fake):
    fake.activities = [{"id": f"x{i:04d}", "order_id": "o"} for i in range(150)]
    api = alpaca_orders.AlpacaOrders(alpaca_orders.AlpacaKeys("k", "s", mode="paper", source="env"),
                                     transport=lambda m, u, h, b, t: fake._json(200, fake.activities[:100]))
    with pytest.raises(alpaca_orders.BrokerError) as stuck:
        api.all_fills()
    assert stuck.value.retryable


# -- 4. a stale last trade is no price ----------------------------------------------------------

def test_a_stale_last_trade_blocks_the_order_with_a_plain_reason(db, paper):
    paper.trade_time = "2026-09-11T19:59:59Z"  # ten days old (a halted symbol)
    ticket = make_ticket(db, [{"symbol": "VTI", "side": "buy", "qty": 1}])["result"]["ticket"]
    stale = [n for n in ticket["notices"] if n["code"] == "price_stale"]
    assert ticket["blocked"] and len(stale) == 1 and "2026-09-11" in stale[0]["message"]
    assert not any(n["code"] == "price_unknown" for n in ticket["notices"])  # one plain reason, not two
    assert "limit_price" not in ticket["lines"][0]
    with pytest.raises(tickets.ConfirmError) as blocked:
        confirm(db, ticket["id"], nonce_of(db, ticket["id"]))
    assert blocked.value.kind == "blocked" and paper.posts() == []

    paper.trade_time = "2026-09-21T14:40:00Z"  # 20 minutes old while the market is open
    assert any(n["code"] == "price_stale" for n in make_ticket(db, [{"symbol": "VTI", "side": "buy", "qty": 1}])
               ["result"]["ticket"]["notices"])


def test_trade_age_follows_market_hours_and_is_configurable(db, paper, monkeypatch):
    monday_pre_open = datetime(2026, 9, 21, 12, 0, tzinfo=timezone.utc)  # 08:00 in New York
    paper.is_open = False
    paper.trade_time = "2026-09-18T19:59:00Z"  # Friday's close
    ticket = make_ticket(db, [{"symbol": "VTI", "side": "buy", "qty": 1}], now=monday_pre_open)["result"]["ticket"]
    assert not any(n["code"] == "price_stale" for n in ticket["notices"])
    paper.trade_time = "2026-09-17T19:59:00Z"  # Thursday's close: more than one trading day
    ticket = make_ticket(db, [{"symbol": "VTI", "side": "buy", "qty": 1}], now=monday_pre_open)["result"]["ticket"]
    assert any(n["code"] == "price_stale" for n in ticket["notices"])
    monkeypatch.setenv("WEALTH_TRADING_MAX_TRADE_AGE_DAYS", "2")
    ticket = make_ticket(db, [{"symbol": "VTI", "side": "buy", "qty": 1}], now=monday_pre_open)["result"]["ticket"]
    assert not any(n["code"] == "price_stale" for n in ticket["notices"])
    paper.is_open, paper.trade_time = True, "2026-09-21T14:30:00Z"
    monkeypatch.setenv("WEALTH_TRADING_MAX_TRADE_AGE_MINUTES", "60")
    assert not any(n["code"] == "price_stale" for n in make_ticket(db, [{"symbol": "VTI", "side": "buy", "qty": 1}])
                   ["result"]["ticket"]["notices"])


# -- 11. near-miss tickers and every schema error at once -----------------------------------------

def test_near_miss_tickers_map_to_alpacas_form(db, paper):
    paper.prices["BRK.B"] = "480.00"
    paper.assets["BRK.B"] = {"symbol": "BRK.B", "tradable": True, "fractionable": True, "status": "active"}
    for raw in ("BRK-B", "BRK/B", "brk.b", "BRK B"):
        ticket = make_ticket(db, [{"symbol": raw, "side": "BUY", "qty": 1, "type": "LIMIT"}])["result"]["ticket"]
        assert ticket["lines"][0]["symbol"] == "BRK.B" and not ticket["blocked"], raw
    assert tickets.order_symbol("VOD.L") is None and tickets.order_symbol("WALMEX.MX") is None  # other listings
    notional = make_ticket(db, [{"symbol": "VTI", "side": "buy", "notional": "$500"}])["result"]["ticket"]
    assert notional["lines"][0]["notional"] == "500"


def test_every_schema_error_comes_back_at_once(db, paper):
    with pytest.raises(ValueError) as bad:
        make_ticket(db, [{"symbol": "VOD.L", "side": "hold", "qty": "ten", "type": "stop"},
                         {"ticker": "VTI", "side": "buy"}], source="rebalance_plan")
    message = str(bad.value)
    for part in ("orders[0].symbol", "orders[0].side", "orders[0].type", "orders[0].qty", "orders[1] has unknown",
                 "orders[1] needs exactly one of qty or notional", "source must be one of"):
        assert part in message, part


# -- 5. a fill posted before the first connector sync -------------------------------------------

def _connector_then_orders(tmp_path, monkeypatch, orders_first):
    import test_connector_alpaca as TC
    from wealth.connectors import alpaca
    from wealth.connectors.alpaca import AlpacaConnector
    from wealth.service import WealthService

    for key, value in PAPER_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("WEALTH_ALPACA_KEY_ID", TC.KEY_ID)
    monkeypatch.setenv("WEALTH_ALPACA_SECRET", TC.SECRET)
    monkeypatch.setattr(AlpacaConnector, "today", property(lambda self: TC.TODAY))
    connector = TC.FakeAlpaca()
    monkeypatch.setattr(alpaca, "default_transport", connector.transport)
    monkeypatch.setattr(alpaca, "default_sleep", lambda seconds: None)
    broker = FakeAlpaca(prices={"VOO": "550.00"}, buying_power="2234.56")
    broker.account["account_number"] = "912765432"
    monkeypatch.setattr(alpaca_orders, "default_transport", broker)
    path = tmp_path / ("orders.sqlite3" if orders_first else "sync.sqlite3")
    service = WealthService(path)
    service.create("ana", "Ana")
    now = datetime(2026, 9, 18, 15, 0, tzinfo=timezone.utc)

    def sync():
        proposal = service.ingest("ana", "connector", {"name": "alpaca", "since": "2026-01-01"})
        return service.ingest("ana", "confirm", {"proposal_id": proposal["result"]["proposal_id"]})

    if not orders_first:
        sync()
    with WealthStore(path) as store:
        ticket = tickets.create_ticket(store, "ana", {"orders": [{"symbol": "VOO", "side": "buy", "qty": "0.5"}],
                                                      "rationale": "x"}, snapshot={"facts": []}, now=now)
        ticket_id = ticket["result"]["ticket"]["id"]
        nonce = next(t["nonce"] for t in tickets.list_tickets(store, "ana", include_nonce=True, now=now)
                     if t["id"] == ticket_id)
        order_id = tickets.confirm(store, "ana", ticket_id, nonce=nonce, snapshot={"facts": []},
                                   now=now)["lines"][0]["broker_order_id"]
        activity = broker.fill(order_id, qty="0.5", price="550", when="2026-09-18T15:05:00Z")
        tickets.refresh(store, "ana", now=now)
    # the connector now sees the fill and the account after it
    connector.data["activities"].append({**activity, "cum_qty": "0.5", "leaves_qty": "0"})
    connector.data["positions"][0] = {**connector.data["positions"][0], "qty": "11", "market_value": "6050",
                                      "cost_basis": "5315"}
    connector.data["account"] = {**connector.data["account"], "cash": "1959.56"}
    sync()

    def view(day):
        result = service.run("ledger", {"view": "holdings", "as_of": day}, client_id="ana")["result"]
        return ({p["instrument_id"]: p["quantity"] for p in result["positions"]},
                {c["currency"]: c["balance"] for c in result["cash"]})

    with WealthStore(path) as store:
        entries = store.ledger("ana")["entries"]
    history = sorted((e["kind"], str(e.get("instrument_id")), str(e.get("quantity")), str(e.get("amount")), e["date"])
                     for e in entries)
    return history, view("2026-09-18"), view("2026-01-02")


def test_a_fill_posted_before_the_first_sync_does_not_suppress_its_history(tmp_path, monkeypatch):
    orders_first = _connector_then_orders(tmp_path, monkeypatch, True)
    sync_first = _connector_then_orders(tmp_path, monkeypatch, False)
    assert orders_first == sync_first
    history, _, start_of_year = orders_first
    assert start_of_year[0] == {"ASML": "2", "TSLA": "1.5", "VOO": "5"}  # opening balances were posted
    assert sum(1 for row in history if row[0] == "buy" and row[1] == "VOO" and row[2] == "0.5") == 1  # once
