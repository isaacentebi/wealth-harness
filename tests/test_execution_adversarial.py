"""Regressions for the adversarial review of order execution (adv4-exec probes A-H and the review's findings).

Fake transports only: FakeGateway / FakeAlpaca in memory, injected reference prices; no socket, keychain or order.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from test_execution import NOW, PAPER_ENV, FakeAlpaca, db, no_network  # noqa: F401 - fixtures and network guard
from test_execution_brokers import (PAPER_ACCOUNT, FakeGateway, _seed_gbm, confirm,  # noqa: F401
                                    gateway, gbm_ticket, ibkr_ticket, quotes)
from wealth import consent
from wealth.execution import tickets
from wealth.execution.brokers import alpaca_orders, base, ibkr_gateway, manual
from wealth.store import WealthStore


def codes(view):
    return {n["code"]: n for n in view["notices"]}


def _post(db, batch):
    with WealthStore(db) as store:
        store.post_ledger("ana", batch)


# -- 1. IBKR replies (see also test_execution_brokers: every reply id is refused) ------------------------------

def test_o163_is_no_longer_benign():
    assert "o163" not in ibkr_gateway.BENIGN_REPLIES and not ibkr_gateway.BENIGN_REPLIES


# -- 2. IBKR price freshness ---------------------------------------------------------------------------------

def test_a_previous_close_is_stale_while_the_market_is_open(db, gateway):
    gateway.prices["VTI"] = "C250.00"
    view = ibkr_ticket(db, [{"symbol": "VTI", "side": "buy", "qty": 1}])  # NOW: Monday 11:00 New York
    notice = codes(view)["price_stale"]
    assert view["blocked"] and "prior session" in notice["message"]
    assert "limit_price" not in view["lines"][0]


def test_a_previous_close_before_the_open_is_the_prior_sessions_close(db, gateway):
    gateway.prices["VTI"] = "C250.00"
    before_open = datetime(2026, 9, 22, 12, 0, tzinfo=timezone.utc)  # Tuesday 08:00 New York
    view = ibkr_ticket(db, [{"symbol": "VTI", "side": "buy", "qty": 1}], now=before_open)
    assert "price_stale" not in codes(view) and view["lines"][0]["limit_price"] == "251.25"
    broker = ibkr_gateway.broker(environ={})
    broker.use_clock(lambda: before_open)
    trade = broker.latest_trade("VTI")
    assert trade["fresh"] and trade["at"].startswith("2026-09-21T20:00")  # Monday's 16:00 New York close


@pytest.mark.parametrize("availability", ["DpB", "YpB", "NpB", "ZpB", ""])
def test_delayed_frozen_or_unlabelled_data_is_not_fresh_while_open(db, gateway, availability):
    gateway.availability = availability
    view = ibkr_ticket(db, [{"symbol": "VTI", "side": "buy", "qty": 1}])
    assert view["blocked"] and "price_stale" in codes(view)


def test_the_snapshot_asks_for_availability_and_quotes(db, gateway):
    ibkr_ticket(db, [{"symbol": "VTI", "side": "buy", "qty": 1}])
    snap = next(c for c in gateway.calls if c["path"] == "/iserver/marketdata/snapshot")
    assert set(snap["query"]["fields"].split(",")) >= {"31", "6509", "84", "86"}


def test_price_moved_compares_the_fresh_price_with_the_one_the_card_showed(db, gateway):
    # A limit the person gave stays put, so only the displayed price shows the market moved.
    view = ibkr_ticket(db, [{"symbol": "VTI", "side": "buy", "qty": 1, "limit_price": "251.00"}])
    assert view["lines"][0]["last_price"] == "250" and not view["blocked"]
    gateway.prices["VTI"] = "256.00"
    with pytest.raises(tickets.ConfirmError) as moved:
        confirm(db, view["id"])
    assert moved.value.kind == "price_moved" and gateway.order_posts() == []
    notice = codes(moved.value.ticket)["price_moved"]
    assert "last_price" in notice["params"]["fields"] and "250 -> 256" in notice["message"]


# -- 3. routing ----------------------------------------------------------------------------------------------

@pytest.mark.parametrize("text, kind", [
    ("GBM (not Interactive Brokers)", "manual"), ("Interactive Brokers", "ibkr"), ("Interactive Brokers, LLC.", "ibkr"),
    ("IBKR", "ibkr"), ("my ibkr and alpaca accounts", "manual"), ("Alpaca Securities LLC", "alpaca"),
    ("alpaca-ish broker", "manual"), ("Not Alpaca", "manual"), (None, "manual"),
])
def test_institutions_match_the_alias_table_exactly(text, kind):
    assert base.kind_for_institution(text) == kind


def test_a_crafted_fact_alone_never_reaches_a_live_broker(db, gateway):
    # probe A: a model-written fact says a GBM account is at Interactive Brokers.
    facts = {"facts": [{"key": "account.gbm-4321", "value": {"institution": "Interactive Brokers"}}]}
    with WealthStore(db) as store:
        view = tickets.create_ticket(store, "ana", {"orders": [
            {"symbol": "VTI", "side": "buy", "qty": 1, "account_id": "gbm-4321"}], "rationale": "x"},
            snapshot=facts, now=NOW)["result"]["ticket"]
    notice = codes(view)["account_mismatch"]
    assert view["blocked"] and notice["status"] == "block" and notice["params"]["connected"] == "ibkr-4567"
    with pytest.raises(tickets.ConfirmError):
        confirm(db, view["id"], snapshot=facts)
    assert gateway.order_posts() == []


def test_the_brokers_own_account_must_match_the_named_ledger_account(db, gateway):
    _post(db, {"batch_id": "ibkr-flex", "source": {"kind": "document", "ref": "flex.xml", "observed_on": "2026-09-01"},
               "accounts": [{"id": "ibkr-9999", "institution": "Interactive Brokers", "type": "brokerage",
                             "currency": "USD", "owners": [{"person_id": "self", "share": "1"}]}]})
    view = ibkr_ticket(db, [{"symbol": "VTI", "side": "buy", "qty": 1}], account="ibkr-9999", snapshot={"facts": []})
    assert view["blocked"] and "account_mismatch" in codes(view)
    ok = ibkr_ticket(db, [{"symbol": "VTI", "side": "buy", "qty": 1}], account="ibkr-4567", snapshot={"facts": []})
    assert not ok["blocked"], ok["notices"]  # the id's form is a hint the gateway's own account confirms


def test_an_unknown_account_blocks_a_live_ticket(db, monkeypatch):
    monkeypatch.setattr(alpaca_orders, "default_transport", FakeAlpaca())
    for key, value in PAPER_ENV.items():
        monkeypatch.setenv(key, value)
    with WealthStore(db) as store:
        paper = tickets.create_ticket(store, "ana", {"orders": [
            {"symbol": "VTI", "side": "buy", "qty": 1, "account_id": "cuenta-9"}], "rationale": "x"},
            snapshot={"facts": []}, now=NOW)["result"]["ticket"]
    assert codes(paper)["account_unmatched"]["status"] == "warn"
    live = {"WEALTH_ALPACA_KEY_ID": "AKFAKELIVEKEY00001", "WEALTH_ALPACA_SECRET": "fake-live-secret-value-00001",
            "WEALTH_TRADING_LIVE": "alpaca"}
    with WealthStore(db) as store:
        view = tickets.create_ticket(store, "ana", {"orders": [
            {"symbol": "VTI", "side": "buy", "qty": 1, "account_id": "cuenta-9"}], "rationale": "x"},
            snapshot={"facts": []}, environ=live, now=NOW)["result"]["ticket"]
    assert view["mode"] == "live" and codes(view)["account_unmatched"]["status"] == "block" and view["blocked"]


def test_a_named_broker_routes_an_order_without_an_account(db, quotes, gateway):
    _seed_gbm(db)
    with WealthStore(db) as store:
        view = tickets.create_ticket(store, "ana", {"orders": [{"symbol": "VOO", "side": "buy", "qty": 1}],
                                                    "rationale": "compra 1 VOO en GBM"},
                                     snapshot={"facts": []}, now=NOW)["result"]["ticket"]
    assert view["broker"] == "manual" and view["broker_label"] == "GBM" and view["account"]["id"] == "gbm-1"
    assert gateway.calls == []


def test_the_single_brokerage_account_is_used_and_several_need_input(db, quotes, gateway):
    _seed_gbm(db)
    with WealthStore(db) as store:
        view = tickets.create_ticket(store, "ana", {"orders": [{"symbol": "VOO", "side": "buy", "qty": 1}],
                                                    "rationale": "Add to the S&P 500."},
                                     snapshot={"facts": []}, now=NOW)["result"]["ticket"]
    assert view["broker"] == "manual" and view["account"]["id"] == "gbm-1"
    _post(db, {"batch_id": "vest", "source": {"kind": "document", "ref": "vest.pdf", "observed_on": "2026-09-01"},
               "accounts": [{"id": "vest-1", "institution": "Vest", "type": "brokerage", "currency": "USD",
                             "owners": [{"person_id": "self", "share": "1"}]}]})
    with WealthStore(db) as store:
        report = tickets.create_ticket(store, "ana", {"orders": [{"symbol": "VOO", "side": "buy", "qty": 1}],
                                                      "rationale": "Add to the S&P 500."},
                                       snapshot={"facts": []}, now=NOW)
        stored = store.auxiliary("ana", "execution").get("tickets") or {}
    assert report["status"] == "needs_input"
    assert {a["id"] for a in report["result"]["accounts"]} == {"gbm-1", "vest-1"}
    assert report["missing"][0]["key"] == "orders[].account_id" and len(stored) == 1  # nothing new stored
    with WealthStore(db) as store:
        both = tickets.create_ticket(store, "ana", {"orders": [{"symbol": "VOO", "side": "buy", "qty": 1}],
                                                    "rationale": "GBM or Vest, whichever"},
                                     snapshot={"facts": []}, now=NOW)
    assert both["status"] == "needs_input"


# -- 4. IBKR order listing ------------------------------------------------------------------------------------

class FreshSessionGateway(FakeGateway):
    """The first order listing of a gateway session is empty and not a snapshot; a POST's answer can be lost."""

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.fresh = False
        self.lose_post = False
        self.duplicate = False
        self.drop_post = False

    def __call__(self, method, url, headers, body, timeout):
        path = url.split("/v1/api", 1)[1].split("?", 1)[0]
        if method == "GET" and path == "/iserver/account/orders" and self.fresh:
            self.fresh = False
            self.calls.append({"method": method, "path": path, "preflight": True})
            return 200, json.dumps({"orders": [], "snapshot": False}).encode()
        if method == "POST" and path.endswith("/orders") and self.duplicate:
            super().__call__(method, url, headers, body, timeout)
            self.duplicate = False
            error = "Order ID/cOID duplicated: local order ID is already registered."
            return 200, json.dumps([{"error": error}]).encode()
        if method == "POST" and path.endswith("/orders") and self.drop_post:
            self.drop_post = False  # the request never reached IBKR
            raise base.BrokerError("Could not reach the IBKR gateway (TimeoutError).", retryable=True)
        if method == "POST" and path.endswith("/orders") and self.lose_post:
            super().__call__(method, url, headers, body, timeout)
            self.lose_post = False
            self.fresh = True
            raise base.BrokerError("Could not reach the IBKR gateway (TimeoutError).", retryable=True)
        return super().__call__(method, url, headers, body, timeout)


@pytest.fixture
def session_gateway(monkeypatch):
    fake = FreshSessionGateway()
    monkeypatch.setattr(ibkr_gateway, "default_transport", fake)
    monkeypatch.delenv("WEALTH_TRADING_LIVE", raising=False)
    return fake


def test_a_lost_answer_and_a_fresh_session_do_not_report_a_live_order_as_not_placed(db, session_gateway):
    view = ibkr_ticket(db, [{"symbol": "VTI", "side": "buy", "qty": 1}])
    session_gateway.lose_post = True
    confirm(db, view["id"])
    session_gateway.fresh = True
    with WealthStore(db) as store:
        later = next(t for t in tickets.refresh(store, "ana", now=NOW + timedelta(minutes=5)) if t["id"] == view["id"])
    line = later["lines"][0]
    assert line["state"] == "sent" and line["broker_order_id"] == "101"


def test_one_empty_listing_is_unknown_and_two_a_lease_apart_are_not_placed(db, session_gateway):
    view = ibkr_ticket(db, [{"symbol": "VTI", "side": "buy", "qty": 1}])
    session_gateway.drop_post = True  # IBKR really has no such order
    assert confirm(db, view["id"])["lines"][0]["state"] == "unknown"
    with WealthStore(db) as store:
        first = next(t for t in tickets.refresh(store, "ana", now=NOW + timedelta(minutes=1)) if t["id"] == view["id"])
        assert first["lines"][0]["state"] == "unknown" and first["status"] == "submitted"
        soon = next(t for t in tickets.refresh(store, "ana", now=NOW + timedelta(minutes=2)) if t["id"] == view["id"])
        assert soon["lines"][0]["state"] == "unknown"  # a second miss inside the lease is still not enough
        final = next(t for t in tickets.refresh(store, "ana", now=NOW + timedelta(minutes=4)) if t["id"] == view["id"])
    assert final["lines"][0]["state"] == "failed" and final["status"] == "done"


def test_the_listing_is_read_at_least_twice_until_it_is_a_snapshot(session_gateway):
    session_gateway.fresh = True
    broker = ibkr_gateway.broker(environ={})
    broker._sleep = lambda seconds: None
    assert broker.order_by_client_id("wealth-t0000000000000000-0") is None
    listings = [c for c in session_gateway.calls if c["path"] == "/iserver/account/orders"]
    assert len(listings) == 2


def test_a_listing_that_never_completes_is_an_error_not_an_empty_list(monkeypatch):
    class Never(FakeGateway):
        def __call__(self, method, url, headers, body, timeout):
            if method == "GET" and "/iserver/account/orders" in url:
                self.calls.append({"path": "/iserver/account/orders"})
                return 200, json.dumps({"orders": [], "snapshot": False}).encode()
            return super().__call__(method, url, headers, body, timeout)

    fake = Never()
    monkeypatch.setattr(ibkr_gateway, "default_transport", fake)
    broker = ibkr_gateway.broker(environ={})
    broker._sleep = lambda seconds: None
    with pytest.raises(base.BrokerError) as error:
        broker.order_by_client_id("wealth-t0000000000000000-0")
    assert error.value.retryable and len(fake.calls) - 2 == ibkr_gateway.ORDER_LIST_ATTEMPTS


def test_a_duplicate_coid_means_the_order_exists(db, session_gateway):
    view = ibkr_ticket(db, [{"symbol": "VTI", "side": "buy", "qty": 1}])
    session_gateway.duplicate = True
    line = confirm(db, view["id"])["lines"][0]
    assert line["state"] == "sent" and line["broker_order_id"] == "101"


# -- 5. manual reconciliation ---------------------------------------------------------------------------------

def _statement(db, rows, batch="gbm-statement-sep"):
    _post(db, {"batch_id": batch, "source": {"kind": "document", "ref": f"{batch}.pdf", "observed_on": "2026-10-01"},
               "instruments": [{"id": "VOO-SIC", "symbol": "VOO", "currency": "MXN", "venue": "sic"}],
               "transactions": [{"kind": "buy", "account_id": "gbm-1", "date": day, "instrument_id": "VOO-SIC",
                                 "quantity": qty, "price": "9530", "amount": str(-int(qty) * 9530),
                                 "currency": "MXN", "external_id": ext} for ext, day, qty in rows]})


def _placed_gbm(db, qty):
    _seed_gbm(db)
    view = gbm_ticket(db, [{"symbol": "VOO", "side": "buy", "qty": qty, "exchange": "SIC"}])
    confirm(db, view["id"], snapshot={"facts": []})
    return view["id"]


def _status(db, ticket_id, when):
    with WealthStore(db) as store:
        tickets.refresh(store, "ana", now=when)
        return tickets.ticket_status(store, "ana", ticket_id, now=when)


def test_a_much_larger_trade_does_not_fill_the_line(db, quotes):
    ticket_id = _placed_gbm(db, 2)
    _statement(db, [("GBM-B9", "2026-09-21", "200")])
    view = _status(db, ticket_id, NOW + timedelta(days=3))
    line = view["lines"][0]
    assert line["state"] == "awaiting" and "filled_qty" not in line and "not counted" in line["reason"]
    assert view["manual"]["verified"] is False


def test_an_exact_trade_is_preferred_over_pieces(db, quotes):
    ticket_id = _placed_gbm(db, 2)
    _statement(db, [("GBM-P1", "2026-09-21", "1"), ("GBM-X2", "2026-09-22", "2")])
    view = _status(db, ticket_id, NOW + timedelta(days=3))
    line = view["lines"][0]
    assert line["state"] == "filled" and line["confirmed_by"] == ["GBM-X2"] and view["manual"]["verified"] is True


def test_a_partial_fill_is_partial_and_after_the_window_partial_unconfirmed(db, quotes):
    ticket_id = _placed_gbm(db, 4)
    _statement(db, [("GBM-P1", "2026-09-21", "1")])
    view = _status(db, ticket_id, NOW + timedelta(days=3))
    assert view["lines"][0]["state"] == "partial" and view["lines"][0]["filled_qty"] == "1"
    late = _status(db, ticket_id, NOW + timedelta(days=tickets.MANUAL_CONFIRM_DAYS + 1))
    assert late["lines"][0]["state"] == "partial_unconfirmed" and late["status"] == "done"
    assert late["manual"]["verified"] is False


# -- 6. placing a manual ticket from an MCP host --------------------------------------------------------------

@pytest.mark.parametrize("text", ["ya la puse", "Ya lo compré", "listo, ya la puse", "I placed it", "I've placed it",
                                  "ya quedó puesta la orden", "Done, placed"])
def test_the_person_saying_they_placed_it(text):
    assert consent.says_placed(text)


@pytest.mark.parametrize("text", ["todavía no la pongo", "no la puse", "¿ya la puse?", "la pongo mañana",
                                  "ya la puse pero a otro precio", "sí", "ok", "I haven't placed it yet", ""])
def test_not_saying_they_placed_it(text):
    assert not consent.says_placed(text)


def test_the_summary_outside_the_app_does_not_say_tap(db, quotes):
    _seed_gbm(db)
    with WealthStore(db) as store:
        report = tickets.create_ticket(store, "ana", {"orders": [{"account_id": "gbm-1", "symbol": "VOO", "side": "buy",
                                                                  "qty": 1, "exchange": "SIC"}], "rationale": "x"},
                                       snapshot={"facts": []}, environ={}, now=NOW)
    text = report["result"]["summary"] + report["result"]["next_step"]
    assert "tap" not in text.lower() and '"placed": true' in text
    assert "Compra 1 VOO" not in report["result"]["summary"] and "Buy 1 VOO on the SIC" in report["result"]["summary"]
    with WealthStore(db) as store:
        app = tickets.create_ticket(store, "ana", {"orders": [{"account_id": "gbm-1", "symbol": "VOO", "side": "buy",
                                                               "qty": 1, "exchange": "SIC"}], "rationale": "x"},
                                    snapshot={"facts": []}, environ={"WEALTH_BEHAVIOR_IN_HOST": "1"}, now=NOW)
    assert "Ya la puse" in app["result"]["summary"]


mcp = pytest.importorskip("mcp")


def _call(server, name, arguments):
    from mcp.server.mcpserver.exceptions import ToolError  # noqa: F401 - raised through

    result = asyncio.run(server.call_tool(name, arguments))
    content = getattr(result, "structured_content", None)
    if content is None and isinstance(result, tuple):
        content = result[1]
    return content if content is not None else result


def _manual_ticket(db):
    _seed_gbm(db)
    return gbm_ticket(db, [{"symbol": "VOO", "side": "buy", "qty": 2, "exchange": "SIC"}])["id"]


def test_a_foreign_host_records_a_placed_ticket_through_the_two_step_code(db, quotes, gateway):
    from mcp.server.mcpserver.exceptions import ToolError

    from wealth.server import build_server

    ticket_id = _manual_ticket(db)
    host = build_server(str(db), environ={})
    args = {"task": "order_ticket", "client_id": "ana", "inputs": {"ticket_id": ticket_id, "placed": True}}
    first = _call(host, "wealth_run", args)
    assert first["status"] == "needs_person" and "Buy 2 VOO on the SIC" in first["result"]["summary"]
    assert "confirmation_code" in first["result"]["next_step"]
    with WealthStore(db) as store:
        assert tickets.ticket_status(store, "ana", ticket_id)["status"] != "placed"
    with pytest.raises(ToolError, match="ConsentRequired"):
        _call(host, "wealth_run", {**args, "inputs": {**args["inputs"], "confirm": True, "confirmation_code": "AAA-AAA"}})
    code = _call(host, "wealth_run", args)["result"]["confirmation_code"]
    done = _call(host, "wealth_run", {**args, "inputs": {**args["inputs"], "confirm": True,
                                                         "confirmation_code": code}})
    ticket = done["result"]["ticket"]
    assert ticket["status"] == "placed" and ticket["lines"][0]["state"] == "awaiting"
    assert ticket["manual"]["placed_via"] == "mcp" and ticket["manual"]["verified"] is False
    assert gateway.calls == []  # nothing ever reaches a broker
    with WealthStore(db) as store:
        events = [e for e in store.order_events("ana", ticket_id) if e["event"] == "placed_manually"]
    assert events and events[0]["payload"]["via"] == "mcp"


def test_a_wealth_turn_needs_the_persons_own_words(db, quotes):
    from mcp.server.mcpserver.exceptions import ToolError

    from wealth.server import build_server

    ticket_id = _manual_ticket(db)
    args = {"task": "order_ticket", "client_id": "ana", "inputs": {"ticket_id": ticket_id, "placed": True}}
    with consent.turn_env("chat", "¿cómo la pongo en GBM?") as turn:
        with pytest.raises(ToolError, match="ConsentRequired"):
            _call(build_server(str(db), environ=dict(turn.env)), "wealth_run", args)
    with consent.turn_env("memory", "ya la puse") as turn:
        with pytest.raises(ToolError, match="ConsentRequired"):
            _call(build_server(str(db), environ=dict(turn.env)), "wealth_run", args)
    with consent.turn_env("chat", "Listo, ya la puse") as turn:
        done = _call(build_server(str(db), environ=dict(turn.env)), "wealth_run", args)
    assert done["result"]["ticket"]["status"] == "placed"


def test_placed_never_applies_to_a_ticket_wealth_sends(db, gateway):
    from mcp.server.mcpserver.exceptions import ToolError

    from wealth.server import build_server

    view = ibkr_ticket(db, [{"symbol": "VTI", "side": "buy", "qty": 1}])
    with consent.turn_env("chat", "ya la puse") as turn:
        with pytest.raises(ToolError, match="places itself"):
            _call(build_server(str(db), environ=dict(turn.env)), "wealth_run",
                  {"task": "order_ticket", "client_id": "ana", "inputs": {"ticket_id": view["id"], "placed": True}})
    assert gateway.order_posts() == []
    with WealthStore(db) as store:
        assert tickets.ticket_status(store, "ana", view["id"])["status"] in ("pending", "expired")


def test_the_tool_description_names_the_placed_action():
    from wealth.server import build_server

    tools = asyncio.run(build_server(None, environ={}).list_tools())
    run = next(t for t in tools if t.name == "wealth_run")
    assert "{ticket_id, placed: true}" in run.description and "account_id when the person names a broker" in \
        run.description


# -- 7-9. fingerprint, DF paper accounts, the loopback transport -----------------------------------------------

def test_the_fingerprint_is_keyed_per_profile(db, gateway):
    view = ibkr_ticket(db, [{"symbol": "VTI", "side": "buy", "qty": 1}])
    with WealthStore(db) as store:
        state = store.auxiliary("ana", "execution")
    stored = state["tickets"][view["id"]]["account_fingerprint"]
    assert stored != hashlib.sha256(f"wealth-ibkr:{PAPER_ACCOUNT}".encode()).hexdigest()[:16]
    assert len(state["fingerprint_key"]) == 64 and "fingerprint_key" not in json.dumps(view)
    assert confirm(db, view["id"])["lines"][0]["state"] == "sent"  # the same login still matches
    assert ibkr_gateway.broker(environ={}).fingerprint(None) == "****4567"


def test_a_legacy_unkeyed_fingerprint_is_still_recognised(db, gateway):
    view = ibkr_ticket(db, [{"symbol": "VTI", "side": "buy", "qty": 1}])

    def legacy(current):
        items = dict(current["tickets"])
        item = dict(items[view["id"]])
        item.pop("fingerprint_v")
        item["account_fingerprint"] = hashlib.sha256(f"wealth-ibkr:{PAPER_ACCOUNT}".encode()).hexdigest()[:16]
        items[view["id"]] = item
        return {**current, "tickets": items}

    with WealthStore(db) as store:
        store.update_auxiliary("ana", "execution", legacy)
    assert confirm(db, view["id"])["lines"][0]["state"] == "sent"


def test_df_accounts_are_paper():
    assert ibkr_gateway.is_paper_account("DF1234567") and ibkr_gateway.is_paper_account("DU1234567")
    assert not ibkr_gateway.is_paper_account("U1234567") and not ibkr_gateway.is_paper_account("F1234567")


def test_the_insecure_localhost_transport_connects_to_the_loopback_literal(monkeypatch):
    seen = {}

    class Response:
        status = 200

        def read(self, limit):
            return b"{}"

    class Connection:
        def __init__(self, host, port, timeout=None, context=None):
            seen.setdefault("hosts", []).append(host)
            self.sock = None

        def connect(self):
            if seen["hosts"][-1] == "127.0.0.1" and seen.get("refuse_v4"):
                raise ConnectionRefusedError

        def request(self, method, target, body=None, headers=None):
            seen["headers"] = headers

        def getresponse(self):
            return Response()

        def close(self):
            pass

    monkeypatch.setattr(ibkr_gateway.http.client, "HTTPSConnection", Connection)
    tls = ibkr_gateway.TlsPolicy(insecure_localhost=True)
    ibkr_gateway.https_transport("GET", "https://localhost:5000/v1/api/iserver/accounts", {}, None, 1, tls=tls)
    assert seen["hosts"] == ["127.0.0.1"] and seen["headers"]["Host"] == "localhost:5000"
    seen.clear()
    seen["refuse_v4"] = True
    ibkr_gateway.https_transport("GET", "https://localhost:5000/v1/api/iserver/accounts", {}, None, 1, tls=tls)
    assert seen["hosts"] == ["127.0.0.1", "::1"]


# -- 10. GBM fees, FX date, dollar accounts at GBM --------------------------------------------------------------

def test_gbm_fee_has_the_mxn_20_minimum(db, quotes):
    _seed_gbm(db)
    line = gbm_ticket(db, [{"symbol": "WALMEX", "side": "buy", "qty": 10, "exchange": "BMV"}])["lines"][0]
    assert line["estimated_fee"] == "23.2"  # MXN 20 minimum + 16% IVA
    assert line["fee_basis"] == "hasta 0.25% + IVA, mínimo MXN 20 por operación (estimado)"


def test_the_fx_rate_shows_its_date(db, quotes, monkeypatch):
    monkeypatch.setattr(manual, "default_fx", lambda db_path: lambda b, q: (Decimal("19"), "2026-09-18"))
    _seed_gbm(db)
    line = gbm_ticket(db, [{"symbol": "VOO", "side": "buy", "qty": 1, "exchange": "SIC"}])["lines"][0]
    assert line["fx"]["rate"] == "19" and line["fx"]["as_of"] == "2026-09-18"


def _gbm_usd(db):
    _post(db, {"batch_id": "gbm-usd", "source": {"kind": "document", "ref": "gbm-usa.pdf", "observed_on": "2026-09-01"},
               "accounts": [{"id": "gbm-usa", "institution": "GBM", "name": "GBM Trading USA", "type": "brokerage",
                             "currency": "USD", "country": "MX", "owners": [{"person_id": "self", "share": "1"}]}]})


def test_a_gbm_dollar_account_quotes_in_usd(db, monkeypatch):
    asked = []
    monkeypatch.setattr(manual, "default_quote", lambda db_path: lambda s, listing, ccy: asked.append(
        (s, listing, ccy)) or {"price": "500", "date": "2026-09-18", "currency": "USD"})
    monkeypatch.setattr(manual, "default_fx", lambda db_path: lambda b, q: Decimal("19"))
    _gbm_usd(db)
    with WealthStore(db) as store:
        view = tickets.create_ticket(store, "ana", {"orders": [{"account_id": "gbm-usa", "symbol": "VOO", "side": "buy",
                                                                "qty": 1}], "rationale": "x"},
                                     snapshot={"facts": []}, now=NOW)["result"]["ticket"]
    line = view["lines"][0]
    assert asked == [("VOO", "US", "USD")] and line["currency"] == "USD" and line["exchange"] == "US"
    assert line["estimated_amount"] == "502.5" and "fx" not in line and "listing_note" not in line


def test_a_gbm_dollar_account_without_a_usd_price_says_why(db, monkeypatch):
    monkeypatch.setattr(manual, "default_quote", lambda db_path: lambda s, listing, ccy: {
        "price": "9500", "date": "2026-09-18", "currency": "MXN"})
    monkeypatch.setattr(manual, "default_fx", lambda db_path: lambda b, q: Decimal("19"))
    _gbm_usd(db)
    with WealthStore(db) as store:
        view = tickets.create_ticket(store, "ana", {"orders": [{"account_id": "gbm-usa", "symbol": "VOO", "side": "buy",
                                                                "qty": 1}], "rationale": "x"},
                                     snapshot={"facts": []}, now=NOW)["result"]["ticket"]
    notice = codes(view)["price_unknown"]
    assert notice["status"] == "warn" and "USD" in notice["message"] and "peso" in notice["message"]
    assert "estimated_amount" not in view["lines"][0] and not view["blocked"]
