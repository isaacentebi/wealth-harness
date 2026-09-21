"""Ledger maths regressions: value in transit, splits in transit, same-day order, Dietz without a base."""
from __future__ import annotations

from decimal import Decimal

import pytest

from wealth import ledger as L
from wealth.ledger.model import LedgerInputError, normalize_transaction

_A = {"id": "a", "institution": "IB", "type": "taxable", "currency": "USD", "owners": [{"person_id": "x", "share": 1}]}
_B = dict(_A, id="b", institution="Schwab")
_X = {"id": "X", "symbol": "X", "currency": "USD", "asset_class": "equity", "venue": "us"}


def _ledger(entries):
    return {"accounts": [_A, _B], "instruments": [_X], "fx": [], "entries": entries}


def test_cash_in_transit_between_own_accounts_stays_in_the_portfolio_value():
    # Repro perf1: 1,000 leaves a on 03-30 and reaches b on 04-02.  The portfolio still holds
    # 1,000 on 03-31 and nothing was earned or lost: end value 1,000, TWR 0 (it was -100 %).
    ledger = _ledger([
        {"id": "d", "account_id": "a", "kind": "deposit", "date": "2025-01-01", "amount": "1000", "currency": "USD"},
        {"id": "t1", "account_id": "a", "kind": "transfer", "date": "2025-03-30", "amount": "-1000", "currency": "USD"},
        {"id": "t2", "account_id": "b", "kind": "transfer", "date": "2025-04-02", "amount": "1000", "currency": "USD"},
    ])
    prices = L.price_table({})
    linked = L.performance(ledger, "2025-01-01", "2025-03-31", "USD", prices)
    assert linked["result"]["end_value"] == "1000.00"
    assert linked["result"]["twr"]["period"] == "0.000000"
    assert linked["result"]["decomposition"]["total_gain"] == "0.00"
    # Modified Dietz with the 03-31 month end mid-transfer loses nothing either.
    dietz = L.performance(ledger, "2025-01-01", "2025-04-30", "USD", prices, method="modified_dietz")
    assert dietz["result"]["twr"]["period"] == "0.000000"
    assert L.valuation_series(ledger, ["2025-03-31"], "USD", prices)["points"][0]["value"] == Decimal(1000)
    # Account a alone: the money left it on 03-30 as a flow, so its value is 0 and its return 0.
    alone = L.performance(ledger, "2025-01-01", "2025-03-31", "USD", prices, account_ids=["a"])
    assert alone["result"]["end_value"] == "0.00" and alone["result"]["twr"]["period"] == "0.000000"
    # Account balances (statement reconciliation) are unchanged: a holds 0 on 03-31.
    check = L.reconcile(dict(ledger, assertions=[{"id": "x", "account_id": "a", "date": "2025-03-31",
                                                  "currency": "USD", "balance": "0"}]))
    assert check["result"]["breaks"] == []


def test_money_received_before_it_is_sent_is_not_counted_twice():
    # The incoming leg posts on 03-29, the outgoing leg on 03-31: on 03-30 the portfolio holds 1,000, not 2,000.
    ledger = _ledger([
        {"id": "d", "account_id": "a", "kind": "deposit", "date": "2025-01-01", "amount": "1000", "currency": "USD"},
        {"id": "t2", "account_id": "b", "kind": "transfer", "date": "2025-03-29", "amount": "1000", "currency": "USD"},
        {"id": "t1", "account_id": "a", "kind": "transfer", "date": "2025-03-31", "amount": "-1000", "currency": "USD"},
    ])
    points = L.valuation_series(ledger, ["2025-03-30", "2025-04-01"], "USD", L.price_table({}))["points"]
    assert [p["value"] for p in points] == [Decimal(1000), Decimal(1000)]


def test_modified_dietz_links_a_month_with_no_base_instead_of_dropping_its_change():
    # 10 X at 100 (1,000) on 01-31; X doubles by 02-02, all sold for 2,000 and withdrawn that day.
    # Dietz base for February = 1,000 - 2,000 x 26/28 < 0, so the month used to be skipped (TWR 0).
    # Linked on the flow date: 2,000 / 1,000 - 1 = +100 %.
    ledger = _ledger([
        {"id": "d", "account_id": "a", "kind": "deposit", "date": "2025-01-30", "amount": "1000", "currency": "USD"},
        {"id": "b1", "account_id": "a", "kind": "buy", "date": "2025-01-30", "instrument_id": "X", "quantity": "10",
         "amount": "-1000", "currency": "USD"},
        {"id": "s1", "account_id": "a", "kind": "sell", "date": "2025-02-02", "instrument_id": "X", "quantity": "10",
         "amount": "2000", "currency": "USD"},
        {"id": "w", "account_id": "a", "kind": "withdrawal", "date": "2025-02-02", "amount": "-2000", "currency": "USD"},
    ])
    prices = L.price_table({"X": {"2025-01-30": 100, "2025-01-31": 100, "2025-02-02": 200}})
    result = L.performance(ledger, "2025-01-31", "2025-02-28", "USD", prices, method="modified_dietz")
    assert result["result"]["twr"]["period"] == "1.000000"
    assert any("linked on its flow dates" in w for w in result["warnings"])


def test_a_split_while_shares_are_in_transit_applies_to_them():
    # Repro lots1: 10 shares (basis 1,000) leave a on 06-01, a 2:1 split on 06-03, they reach b
    # on 06-05: 20 shares, basis still 1,000 (it was 10 shares).
    entries = [
        {"id": "d", "account_id": "a", "kind": "deposit", "date": "2025-01-02", "amount": "1000", "currency": "USD"},
        {"id": "b1", "account_id": "a", "kind": "buy", "date": "2025-01-02", "instrument_id": "X", "quantity": "10",
         "amount": "-1000", "currency": "USD"},
        {"id": "t1", "account_id": "a", "kind": "transfer", "date": "2025-06-01", "instrument_id": "X", "quantity": "-10"},
        {"id": "s", "account_id": "b", "kind": "split", "date": "2025-06-03", "instrument_id": "X", "ratio": "2"},
        {"id": "t2", "account_id": "b", "kind": "transfer", "date": "2025-06-05", "instrument_id": "X", "quantity": "10"},
    ]
    lots = L.holdings(_ledger(entries), "2025-06-10")["result"]["lots"]
    assert [(l["account_id"], l["quantity"], l["cost_basis"], l["acquired_on"]) for l in lots] == \
        [("b", "20", "1000.00", "2025-01-02")]
    # The same split posted by both brokers is applied once.
    both = entries[:3] + [{"id": "s0", "account_id": "a", "kind": "split", "date": "2025-06-03",
                           "instrument_id": "X", "ratio": "2"}] + entries[3:]
    assert [l["quantity"] for l in L.holdings(_ledger(both), "2025-06-10")["result"]["lots"]] == ["20"]
    # In transit on 06-04 the 20 post-split shares at 55 are worth 1,100 to the whole portfolio.
    prices = L.price_table({"X": {"2025-06-04": 55}})
    assert L.valuation_series(_ledger(entries), ["2025-06-04"], "USD", prices)["points"][0]["value"] == Decimal(1100)


def test_same_day_buy_is_applied_before_the_sell_so_no_phantom_lot_appears():
    # Repro lots1: a day trade posted sell-first.  Buy 10 for 1,000, sell 10 for 1,100: gain 100, nothing left
    # (it was an unknown-basis sale and a 10-share phantom lot).
    ledger = _ledger([
        {"id": "d", "account_id": "a", "kind": "deposit", "date": "2025-01-02", "amount": "5000", "currency": "USD"},
        {"id": "s1", "account_id": "a", "kind": "sell", "date": "2025-01-03", "instrument_id": "X", "quantity": "10",
         "amount": "1100", "currency": "USD"},
        {"id": "b1", "account_id": "a", "kind": "buy", "date": "2025-01-03", "instrument_id": "X", "quantity": "10",
         "amount": "-1000", "currency": "USD"},
    ])
    assert [(s["quantity"], s["gain"]) for s in L.realized_gains(ledger)["result"]["sales"]] == [("10", "100.00")]
    held = L.holdings(ledger, "2025-01-04")["result"]
    assert held["lots"] == [] and held["breaks"] == []


def test_execution_times_order_same_day_trades_when_every_trade_has_one():
    # With times, the sell at 09:30 precedes the buy at 15:00: a short sale, reported, not reordered.
    entries = [
        {"id": "b1", "account_id": "a", "kind": "buy", "date": "2025-01-03", "instrument_id": "X", "quantity": "10",
         "amount": "-1000", "currency": "USD", "executed_at": "2025-01-03T15:00:00"},
        {"id": "s1", "account_id": "a", "kind": "sell", "date": "2025-01-03", "instrument_id": "X", "quantity": "10",
         "amount": "1100", "currency": "USD", "executed_at": "2025-01-03T09:30:00"},
    ]
    held = L.holdings(_ledger(entries), "2025-01-04")["result"]
    assert [b["kind"] for b in held["breaks"]] == ["oversold"]
    assert [l["quantity"] for l in held["lots"]] == ["10"]
    source = {"kind": "document", "ref": "x.pdf", "observed_on": "2025-01-04"}
    base = {"kind": "buy", "account_id": "a", "date": "2025-01-03", "instrument_id": "X", "quantity": "1",
            "amount": "-10", "currency": "USD"}
    entry, _ = normalize_transaction(dict(base, executed_at="2025-01-03T15:00:00"), 0, source=source, confidence=None)
    assert entry["executed_at"] == "2025-01-03T15:00:00"
    with pytest.raises(LedgerInputError):
        normalize_transaction(dict(base, executed_at="2025-01-04T09:00:00"), 0, source=source, confidence=None)
