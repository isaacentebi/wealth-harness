"""tax harvest_report -> order ticket: lot ids, per-line estimated saving and the wash-sale date, on the fake broker.

The ticket is only proposed here; nothing is posted to the (fake) broker.
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from test_execution import NOW, PAPER_ENV, FakeAlpaca, db, no_network  # noqa: F401 - fixtures and the network guard
from test_tax import SALE, _multi_household, _return_facts, _sized, _us, _us_facts
from wealth.execution import tickets
from wealth.execution.brokers import alpaca_orders
from wealth.store import WealthStore


def _report(**extra):
    household = _multi_household(
        _sized("vti-1", "VTI", "2025-12-01", 30000, 100),   # short-term loss 5,000 at 250
        _sized("bnd-1", "BND", "2024-01-02", 9200, 100),    # long-term loss 2,000 at 72
        _sized("bnd-2", "BND", "2024-02-02", 6000, 100),    # long-term gain: not a candidate
        _sized("xyz-1", "XYZ", "2024-01-02", 5000, 100),    # long-term loss 4,000 beyond the $3,000 limit
    )
    inputs = {"us_tax_facts": _us_facts(SALE, short_term_gains=1000), "us_return_facts": _return_facts(), **extra}
    packet = _us(household, prices={"VTI": 250, "BND": 72, "XYZ": 10}, mode="harvest_report", **inputs)
    return packet["result"]


def test_harvest_report_drafts_one_ticket_with_lot_ids_savings_and_repurchase_date():
    report = _report()
    assert len(report["order_tickets"]) == 1
    draft = report["order_tickets"][0]
    orders = {o["symbol"]: o for o in draft["inputs"]["orders"]}
    assert all(o["side"] == "sell" and o["type"] == "limit" for o in orders.values())
    lots = {lot["lot_id"]: lot for o in orders.values() for lot in o["lots"]}
    assert "bnd-2" not in lots  # a gain lot is never harvested
    steps = {s["through_lot_id"]: s["marginal_tax_reduction"] for s in report["cumulative_plan"]}
    for lot_id, lot in lots.items():
        assert lot["estimated_tax_saving"] == steps[lot_id] and float(lot["estimated_tax_saving"]) > 0
        assert lot["repurchase_not_before"] == "2026-04-20"  # sale 2026-03-20 + 31 days
    assert draft["estimated_tax_saving"] == f"{sum(float(v['estimated_tax_saving']) for v in lots.values()):.2f}"
    excluded = {e["lot_id"]: e["reason"] for e in report["order_ticket_exclusions"]}
    assert set(lots) | set(excluded) == {"vti-1", "bnd-1", "xyz-1"}
    assert all(reason == "carryforward_only" for reason in excluded.values())
    assert "2026-04-20" in draft["inputs"]["rationale"]


def test_harvest_draft_leaves_out_a_washed_lot_and_has_no_saving_without_return_facts():
    purchase = [{"account_id": "spouse-ira", "instrument_id": "VTI", "trade_date": "2026-03-10", "quantity": 100}]
    report = _report(purchases=purchase)
    assert {"lot_id": "vti-1", "reason": "wash_sale"} in [
        {k: e[k] for k in ("lot_id", "reason")} for e in report["order_ticket_exclusions"]]
    blind = _report(us_return_facts=None)
    lots = [lot for d in blind["order_tickets"] for o in d["inputs"]["orders"] for lot in o["lots"]]
    assert lots and all(lot["estimated_tax_saving"] is None for lot in lots)
    assert blind["order_tickets"][0]["estimated_tax_saving"] is None
    assert "cannot be estimated" in blind["order_tickets"][0]["inputs"]["rationale"]


@pytest.fixture
def paper_with_positions(monkeypatch):
    broker = FakeAlpaca(positions=[{"symbol": "VTI", "qty": "100", "qty_available": "100"},
                                   {"symbol": "BND", "qty": "200", "qty_available": "200"},
                                   {"symbol": "XYZ", "qty": "100", "qty_available": "100"}])
    monkeypatch.setattr(alpaca_orders, "default_transport", broker)
    for key, value in PAPER_ENV.items():
        monkeypatch.setenv(key, value)
    return broker


def test_the_draft_becomes_a_ticket_whose_lines_carry_the_lots(db, paper_with_positions):
    draft = _report()["order_tickets"][0]
    with WealthStore(db) as store:
        made = tickets.create_ticket(store, "ana", draft["inputs"], snapshot={"facts": []}, now=NOW)
    view = made["result"]["ticket"]
    assert view["status"] == "pending" and view["mode"] == "paper"
    lines = {line["symbol"]: line for line in view["lines"]}
    for order in draft["inputs"]["orders"]:
        line = lines[order["symbol"]]
        assert [lot["lot_id"] for lot in line["lots"]] == [lot["lot_id"] for lot in order["lots"]]
        assert Decimal(line["estimated_tax_saving"]) == sum(Decimal(lot["estimated_tax_saving"]) for lot in order["lots"])
        assert line["repurchase_not_before"] == "2026-04-20"
    assert not paper_with_positions.posts()  # proposed only: the person confirms on the card
    with WealthStore(db) as store:
        stored = tickets.ticket_status(store, "ana", view["id"])
    assert stored["lines"][0]["lots"]


def test_lots_are_validated():
    base = {"symbol": "VTI", "side": "sell", "qty": 10}
    with pytest.raises(ValueError, match="only a sell relieves tax lots"):
        tickets.create_ticket(None, None, {"orders": [{**base, "side": "buy", "lots": [{"lot_id": "a"}]}],
                                           "rationale": "x"}, snapshot=None)
    with pytest.raises(ValueError, match="add up to 4, not the order qty 10"):
        tickets.create_ticket(None, None, {"orders": [{**base, "lots": [{"lot_id": "a", "quantity": 4}]}],
                                           "rationale": "x"}, snapshot=None)
    with pytest.raises(ValueError, match="lists a lot twice"):
        tickets.create_ticket(None, None, {"orders": [{**base, "lots": [{"lot_id": "a"}, {"lot_id": "a"}]}],
                                           "rationale": "x"}, snapshot=None)
    preview = tickets.create_ticket(None, None, {"orders": [{**base, "lots": [
        {"lot_id": "a", "quantity": 6, "estimated_tax_saving": "12.5"}, {"lot_id": "b", "quantity": 4}]}],
        "rationale": "x"}, snapshot=None)
    line = preview["result"]["ticket"]["lines"][0]
    assert line["lots"][0]["estimated_tax_saving"] == "12.5" and "estimated_tax_saving" not in line  # b unknown
