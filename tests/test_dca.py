from __future__ import annotations

from decimal import Decimal

import pytest

from wealth import dca
from wealth.store import WealthStore


PLAN = {"id": "core", "currency": "MXN", "cadence": "monthly", "start_date": "2026-01-15", "account_id": "gbm",
        "source_account_id": "bank", "legs": [{"instrument_id": "VOO-SIC", "amount": 5000}]}


def buy(entry_id, day, amount, currency="MXN", instrument="VOO-SIC"):
    return {"id": entry_id, "account_id": "gbm", "kind": "buy", "date": day, "instrument_id": instrument,
            "quantity": "1", "amount": str(-amount), "currency": currency, "confidence": "reported",
            "source": {"kind": "document", "ref": "GBM"}}


def ledger(*entries, fx=()):
    return {"accounts": [{"id": "gbm", "institution": "GBM", "type": "brokerage", "currency": "MXN"}],
            "instruments": [], "entries": list(entries), "fx": list(fx)}


def test_schedule_clamps_month_ends_and_supports_weekly():
    assert dca.schedule(dict(PLAN, start_date="2026-01-31"), "2026-04-30") == [
        "2026-01-31", "2026-02-28", "2026-03-31", "2026-04-30"]
    assert dca.schedule(dict(PLAN, day_of_month=10), "2026-03-31") == ["2026-02-10", "2026-03-10"]
    assert dca.schedule(dict(PLAN, cadence="weekly", end_date="2026-02-01"), "2026-12-31") == [
        "2026-01-15", "2026-01-22", "2026-01-29"]
    with pytest.raises(ValueError, match="cadence"):
        dca.validate_plan(dict(PLAN, cadence="daily"))


def test_adherence_states_and_cumulative_variance():
    data = ledger(
        buy("early", "2026-01-03", 5000),   # before the plan window: ignored
        buy("jan", "2026-01-15", 5000),
        buy("feb", "2026-02-25", 5000),     # due 02-15, tolerance 5 days: late
        buy("apr", "2026-04-15", 2000),     # below 95% of plan: partial
    )
    report = dca.adherence(data, PLAN, "2026-04-18")["result"]
    states = [(i["due"], i["state"]) for i in report["installments"]]
    assert states == [("2026-01-15", "on_time"), ("2026-02-15", "late"), ("2026-03-15", "skipped"),
                      ("2026-04-15", "partial")]
    assert report["counts"] == {"on_time": 1, "late": 1, "skipped": 1, "partial": 1}
    assert (report["planned_to_date"], report["invested_to_date"], report["cumulative_variance"]) == (
        "20000.00", "12000.00", "-8000.00")
    assert report["next_due"] == "2026-05-15" and report["installments"][-1]["open"] is True
    assert report["on_time_rate"] == "0.2500"
    pending = dca.adherence(data, PLAN, "2026-03-17")["result"]["installments"][-1]
    assert pending["state"] == "pending"


def test_adherence_with_foreign_currency_buy_and_missing_fx_is_unknown():
    data = ledger(buy("jan", "2026-01-15", 300, currency="USD"))
    report = dca.adherence(data, PLAN, "2026-01-31")
    assert report["status"] == "partial"
    assert report["result"]["installments"][0]["state"] == "unknown"
    assert report["result"]["invested_to_date"] is None
    data["fx"] = [{"date": "2026-01-15", "base": "USD", "quote": "MXN", "rate": "17", "source": "Banxico"}]
    report = dca.adherence(data, PLAN, "2026-01-31")["result"]
    assert report["installments"][0]["state"] == "on_time" and report["installments"][0]["invested"] == "5100.00"


def test_backtest_is_historical_deterministic_and_fails_closed_on_gaps():
    prices = {"VOO-SIC": {"2026-01-15": 100, "2026-02-16": 80, "2026-03-16": 120}}
    plan = dict(PLAN, end_date="2026-03-15")
    first = dca.backtest(plan, prices, "2026-01-15", "2026-03-16")
    second = dca.backtest(plan, prices, "2026-01-15", "2026-03-16")
    assert first == second
    result = first["result"]
    assert result["historical"] is True and "not a forecast" in first["assumptions"][-1]
    units = Decimal(50) + Decimal(5000) / 80 + Decimal(5000) / 120
    assert result["dca_end_value"] == str((units * 120).quantize(Decimal("0.01")))
    assert result["lump_sum_end_value"] == "18000.00"
    assert result["invested"] == "15000.00"
    assert [b["executed"] for b in result["legs"][0]["buys"]] == ["2026-01-15", "2026-02-16", "2026-03-16"]
    gap = dca.backtest(plan, {"VOO-SIC": {"2026-01-15": 100, "2026-03-16": 120}}, "2026-01-15", "2026-03-16")
    assert gap["status"] == "partial" and gap["result"]["dca_end_value"] is None
    assert gap["missing"][0]["key"] == "prices.VOO-SIC@2026-02-15"


def test_suggestion_is_a_bounded_range_not_an_instruction():
    out = dca.suggest("12116.67", "MXN", target_weights={"VOO-SIC": 0.6, "NAFTRAC": 0.4})
    assert out["result"]["range"] == {"low": "6000.00", "high": "9600.00"}
    assert out["result"]["split"]["VOO-SIC"] == {"low": "3600.00", "high": "5760.00"}
    assert any("not an instruction" in a for a in out["assumptions"])
    capped = dca.suggest(12116.67, "MXN", constraints={"max_amount": 8000, "min_amount": 7000})
    assert capped["result"]["range"] == {"low": "7000.00", "high": "8000.00"}
    impossible = dca.suggest(1000, "MXN", constraints={"min_amount": 5000})
    assert impossible["status"] == "partial" and impossible["warnings"]
    assert dca.suggest(None, "MXN")["status"] == "needs_input"
    assert dca.suggest(-500, "MXN")["result"]["range"] is None


def test_plan_is_saved_as_a_fact_and_used_from_context(tmp_path):
    fact = dca.plan_fact(PLAN, "2026-01-10")
    with WealthStore(tmp_path / "w.sqlite3") as store:
        store.create_client("c", "C")
        receipt = store.remember("c", [fact])
        assert receipt["written"][0]["key"] == "planning.dca"
        store.remember("c", [dca.plan_fact(dict(PLAN, id="bonds", legs=[{"instrument_id": "CETES", "amount": 1000}]),
                                           "2026-01-10")])
        value = {f["key"]: f["value"] for f in store.snapshot("c")["facts"]}["planning.dca"]
    assert [p["id"] for p in value["plans"]] == ["core", "bonds"]
    context = {"planning.dca": value}
    schedule = dca.run("dca", {"view": "schedule", "plan_id": "core", "through": "2026-02-28"}, context)
    assert schedule["result"]["due_dates"] == ["2026-01-15", "2026-02-15"]
    assert dca.run("dca", {"view": "adherence", "plan_id": "nope", "as_of": "2026-02-28"}, context)["status"] == "needs_input"
