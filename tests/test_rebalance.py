"""Tax-aware rebalancing and asset location (wealth.rebalance)."""
from __future__ import annotations

from copy import deepcopy
from decimal import Decimal

import pytest

from wealth import rebalance
from wealth.catalog import _REBAL_MX_HOUSEHOLD, _REBAL_US_HOUSEHOLD
from wealth.service import WealthService

US_TARGETS = {"by": "asset_class", "sleeves": [{"name": "equity", "weight": 0.6, "buy": ["VTI"]},
                                               {"name": "bond", "weight": 0.4, "buy": ["BND"]}]}
US_JC = {"jurisdiction": "US", "trade_date": "2026-09-18",
         "accounts": {"brk": {"commission_rate": 0}, "ira": {"commission_rate": 0}, "roth": {"commission_rate": 0}}}
RATES = {"rates": {"ordinary": 0.32, "long_term": 0.15}}

MX_TARGETS = {"by": "asset_class", "map": {"CETETRC": "mx_fixed_income"},
              "sleeves": [{"name": "equity", "weight": 0.7, "buy": ["CSPXN"]},
                          {"name": "mx_fixed_income", "weight": 0.3, "buy": ["CETETRC"]}]}
MX_JC = {"jurisdiction": "MX", "trade_date": "2026-09-18",
         "accounts": {"gbm": {"platform": "gbm_trading_mx"}, "ibkr": {"platform": "ibkr", "commission_rate": "0.0005"},
                      "bank": {"purpose": "reserve"}},
         "instruments": {"AAPL": {"sic_listed": True}, "XYZ": {"sic_listed": False}}}


def us(**kwargs):
    kwargs.setdefault("tax_inputs", deepcopy(RATES))
    household = kwargs.pop("household", deepcopy(_REBAL_US_HOUSEHOLD))
    targets = kwargs.pop("targets", deepcopy(US_TARGETS))
    return rebalance.plan(household, targets, jurisdiction_context=kwargs.pop("jurisdiction_context", deepcopy(US_JC)), **kwargs)


def mx(**kwargs):
    kwargs.setdefault("tax_inputs", {"marginal_rate": 0.30})
    household = kwargs.pop("household", deepcopy(_REBAL_MX_HOUSEHOLD))
    return rebalance.plan(household, deepcopy(MX_TARGETS), jurisdiction_context=kwargs.pop("jurisdiction_context", deepcopy(MX_JC)), **kwargs)


def trades(report, side=None):
    return [t for t in report["result"]["trades"] if side is None or t["side"] == side]


def find(report, account, instrument, side):
    return next(t for t in trades(report, side) if (t["account_id"], t["instrument_id"]) == (account, instrument))


# ---------------------------------------------------------------- US


def test_us_harvests_the_loss_lot_first_then_sells_in_the_ira_and_buys_bonds():
    report = us()
    assert report["status"] == "ready", report["missing"]
    sells = trades(report, "sell")
    assert [(t["account_id"], t["instrument_id"]) for t in sells] == [("brk", "VTI"), ("ira", "VTI")]
    taxable = sells[0]
    assert [lot["lot_id"] for lot in taxable["lots"]] == ["lot-loss"]
    assert taxable["lots"][0]["character"] == "short_term"
    assert Decimal(taxable["realized_gain_or_loss"]["amount"]) == Decimal("-2000")
    assert Decimal(taxable["estimated_tax"]["amount"]) == Decimal("-640")  # 2000 x 32%, an estimated reduction
    assert sells[1]["estimated_tax"]["amount"] == "0.00"
    assert "traditional_ira" in sells[1]["reason"]
    # Proceeds stay where they were raised and buy bonds there (IRA bonds by location preference).
    assert {t["account_id"] for t in trades(report, "buy")} == {"brk", "ira"}
    assert all(t["instrument_id"] == "BND" for t in trades(report, "buy"))
    check = report["result"]["tax_engine_check"]
    assert check["wash_sale_disallowed_loss"] == "0.00" and check["realized"]["short_term"] == "-2000.00"
    assert any("before 2026-10-19" in w for w in report["warnings"])
    summary = report["result"]["summary"]
    assert summary["total_estimated_tax"] == "-640.00" and summary["total_estimated_cost"] == "0.00"
    assert summary["residual_drift"]["sleeves_out_of_range"] == []
    assert summary["before"]["sleeves"]["equity"]["in_range"] is False
    assert summary["after"]["sleeves"]["equity"]["weight"] == "0.6000"


def test_us_recent_identical_purchase_blocks_the_loss_lot_and_uses_advantaged_then_long_term():
    report = us(tax_inputs={**RATES, "purchases": [{"account_id": "roth", "instrument_id": "VTI", "trade_date": "2026-09-01", "quantity": 1}]})
    blocked = report["result"]["blocked"]
    assert blocked[0]["lot_id"] == "lot-loss" and "wash sale" in blocked[0]["reason"]
    sells = trades(report, "sell")
    assert [(t["account_id"]) for t in sells] == ["ira", "roth", "brk"]
    taxable = find(report, "brk", "VTI", "sell")
    assert [lot["lot_id"] for lot in taxable["lots"]] == ["lot-lt"]
    assert taxable["lots"][0]["character"] == "long_term"
    gain = Decimal(taxable["realized_gain_or_loss"]["amount"])
    assert gain > 0 and Decimal(taxable["estimated_tax"]["amount"]) == gain * Decimal("0.15")
    assert report["result"]["tax_engine_check"]["wash_sale_disallowed_loss"] == "0.00"
    assert not any("before 2026-10-19" in w for w in report["warnings"])


def test_us_identical_group_and_household_lot_also_block_the_loss():
    household = deepcopy(_REBAL_US_HOUSEHOLD)
    household["positions"].append({"id": "roth-voo", "account_id": "roth", "instrument_id": "VOO", "symbol": "VOO",
                                   "quantity": 2, "value": 1000, "currency": "USD", "asset_class": "equity"})
    household["lots"].append({"id": "roth-voo-lot", "account_id": "roth", "instrument_id": "VOO", "quantity": 2,
                              "cost_basis": 1000, "acquired_on": "2026-09-10", "currency": "USD"})
    report = us(household=household, tax_inputs={**RATES, "substantially_identical_groups": [{"id": "sp", "instrument_ids": ["VTI", "VOO"]}]})
    assert any(b.get("lot_id") == "lot-loss" and "VOO" in b["reason"] for b in report["result"]["blocked"])


def test_us_advantaged_first_when_harvesting_is_not_preferred():
    report = us(constraints={"harvest_losses_first": False})
    assert trades(report, "sell")[0]["account_id"] == "ira"


def test_us_missing_rates_leave_tax_unknown_and_listed():
    report = us(tax_inputs={})
    assert report["status"] == "partial"
    taxable = find(report, "brk", "VTI", "sell")
    assert taxable["estimated_tax"] is None
    assert report["result"]["summary"]["total_estimated_tax"] is None
    assert any(m.startswith("tax_inputs.rates") for m in report["missing"])


def test_us_bracket_engine_prices_the_plan():
    tax_inputs = {"filing_status": "single",
                  "us_tax_facts": {"short_term_gains": 0, "short_term_losses": 0, "long_term_gains": 0, "long_term_losses": 0,
                                   "short_term_loss_carryover": 0, "long_term_loss_carryover": 0,
                                   "ordinary_income_loss_deduction_available": 3000, "complete": True, "as_of": "2026-09-18"},
                  "us_return_facts": {"tax_year": 2026, "complete": True, "ordinary_taxable_income": 150000, "qualified_dividends": 2000,
                                      "magi_excluding_capital_gains": 170000, "net_investment_income_excluding_capital_gains": 5000}}
    report = us(tax_inputs=tax_inputs)
    estimate = report["result"]["tax_engine_check"]["incremental_tax_estimate"]
    assert Decimal(estimate["incremental_tax"]) < 0
    assert report["result"]["summary"]["total_estimated_tax"] == estimate["incremental_tax"]


def test_us_missing_lots_block_taxable_sales_and_are_reported():
    household = deepcopy(_REBAL_US_HOUSEHOLD)
    household["lots"] = []
    report = us(household=household)
    assert not any(t["account_id"] == "brk" for t in trades(report, "sell"))
    assert any("household.lots for brk/VTI" in m for m in report["missing"])
    assert report["status"] == "partial"


def test_unknown_commission_is_missing_not_zero():
    jc = deepcopy(US_JC)
    del jc["accounts"]["ira"]
    report = us(jurisdiction_context=jc)
    ira = find(report, "ira", "VTI", "sell")
    assert ira["estimated_cost"] is None and "commission" in ira["cost_note"]
    assert "jurisdiction_context.accounts.ira.commission_rate" in report["missing"]
    assert report["result"]["summary"]["total_estimated_cost"] is None


# ---------------------------------------------------------------- Mexico


def test_mx_sic_at_gbm_sold_at_10_percent_with_commission_and_iva_non_sic_gain_not_realised():
    report = mx(cash_flows=[{"id": "sept", "account_id": "gbm", "amount": 20000}])
    assert report["status"] == "ready", report["missing"]
    sell = find(report, "gbm", "CSPXN", "sell")
    assert sell["tax_regime"] == "article_129"
    assert Decimal(sell["quantity"]) == Decimal(sell["quantity"]).to_integral_value()  # whole shares on Trading MX
    amount = Decimal(sell["estimated_amount"]["amount"])
    assert Decimal(sell["estimated_cost"]["amount"]) == (amount * Decimal("0.0025") * Decimal("1.16")).quantize(Decimal("0.01"))
    gain = Decimal(sell["realized_gain_or_loss"]["amount"])
    assert gain == Decimal(sell["quantity"]) * (Decimal(11000) - Decimal(9000))
    assert Decimal(sell["estimated_tax"]["amount"]) == gain * Decimal("0.10")
    blocked = report["result"]["blocked"]
    assert blocked == [{"account_id": "ibkr", "instrument_id": "XYZ", "reason": blocked[0]["reason"]}]
    assert "progressive" in blocked[0]["reason"]
    assert not any(t["instrument_id"] == "XYZ" for t in trades(report))
    for buy in trades(report, "buy"):
        assert buy["instrument_id"] == "CETETRC" and buy["whole_shares"] is True
        assert Decimal(buy["quantity"]) == Decimal(buy["quantity"]).to_integral_value()
    assert report["result"]["summary"]["residual_drift"]["sleeves_out_of_range"] == []


def test_mx_sic_listed_share_at_foreign_broker_keeps_10_percent():
    report = mx(constraints={"no_sell_instrument_ids": ["CSPXN"]})
    sell = find(report, "ibkr", "AAPL", "sell")
    assert sell["tax_regime"] == "article_129"
    gain = Decimal(sell["realized_gain_or_loss"]["amount"])
    assert Decimal(sell["estimated_tax"]["amount"]) == (gain * Decimal("0.10")).quantize(Decimal("0.01"))
    assert "declare it in April" in sell["reason"]
    assert Decimal(sell["estimated_cost"]["amount"]) == (Decimal(sell["estimated_amount_reporting"]["amount"]) * Decimal("0.0005")).quantize(Decimal("0.01"))


def test_mx_non_sic_gain_is_progressive_when_explicitly_allowed():
    report = mx(constraints={"no_sell_instrument_ids": ["CSPXN", "AAPL"], "avoid_progressive_gains": False})
    sell = find(report, "ibkr", "XYZ", "sell")
    assert sell["tax_regime"] == "progressive"
    gain = Decimal(sell["realized_gain_or_loss"]["amount"])
    assert Decimal(sell["estimated_tax"]["amount"]) == (gain * Decimal("0.30")).quantize(Decimal("0.01"))


def test_mx_unknown_sic_listing_is_missing_and_blocks_gains():
    jc = deepcopy(MX_JC)
    jc["instruments"] = {"XYZ": {"sic_listed": False}}
    report = mx(jurisdiction_context=jc, constraints={"no_sell_instrument_ids": ["CSPXN"]})
    assert any("AAPL.sic_listed" in m for m in report["missing"])
    assert any(b["instrument_id"] == "AAPL" and "SIC listing unknown" in b["reason"] for b in report["result"]["blocked"])


def test_mx_foreign_lot_without_mxn_basis_is_missing():
    household = deepcopy(_REBAL_MX_HOUSEHOLD)
    for lot in household["lots"]:
        lot.pop("cost_basis_mxn", None)
    report = mx(household=household, constraints={"no_sell_instrument_ids": ["CSPXN"]})
    assert any("l-aapl].cost_basis_mxn" in m for m in report["missing"])
    assert not any(t["instrument_id"] == "AAPL" for t in trades(report, "sell"))


# ---------------------------------------------------------------- flows, reserve, missing data


def test_flows_only_matches_the_no_sell_alternative_and_drifts_more():
    flows = [{"id": "sept", "account_id": "brk", "amount": 5000}]
    full = us(cash_flows=flows)
    only = us(cash_flows=flows, constraints={"mode": "flows_only"})
    assert trades(only, "sell") == []
    assert only["result"]["trades"] == full["result"]["alternatives"]["no_sell"]["trades"]
    assert full["result"]["alternatives"]["no_sell"]["total_estimated_tax"] == "0.00"
    drift_full = Decimal(full["result"]["summary"]["residual_drift"]["max_abs"])
    drift_flows = Decimal(only["result"]["summary"]["residual_drift"]["max_abs"])
    assert drift_flows == Decimal(full["result"]["alternatives"]["no_sell"]["residual_drift"]["max_abs"])
    assert drift_full < drift_flows
    # New cash was deployed first: the flow buy exists in both plans.
    assert find(only, "brk", "BND", "buy")["reason"].startswith("bond is below target; deploys new cash")


def test_flows_inside_the_range_trigger_no_sales():
    household = deepcopy(_REBAL_US_HOUSEHOLD)
    for position in household["positions"]:
        if position["id"] == "ira-bnd":
            position.update(quantity=380, value=26600)
    report = us(household=household, cash_flows=[{"id": "sept", "account_id": "brk", "amount": 1000}])
    assert trades(report, "sell") == []
    assert trades(report, "buy")


def test_reserve_and_goal_money_is_never_touched():
    household = deepcopy(_REBAL_US_HOUSEHOLD)
    household["accounts"].append({"id": "house", "owner_id": "p1", "type": "taxable", "currency": "USD", "purpose": "goal:house"})
    household["positions"].append({"id": "house-vti", "account_id": "house", "instrument_id": "VTI", "symbol": "VTI",
                                   "quantity": 50, "value": 15000, "currency": "USD", "asset_class": "equity"})
    household["positions"].append({"id": "house-cash", "account_id": "house", "instrument_id": "cash:USD", "symbol": "CASH",
                                   "quantity": 9000, "value": 9000, "currency": "USD", "asset_class": "cash"})
    report = us(household=household, cash_flows=[{"id": "reserve-top-up", "account_id": "brk", "amount": 3000, "purpose": "reserve"}])
    touched = {t["account_id"] for t in trades(report)}
    assert "bank" not in touched and "house" not in touched
    protected = {p["position_id"] for p in report["result"]["protected"]}
    assert protected == {"bank-cash", "house-vti", "house-cash"}
    assert report["result"]["cash_flows_used"] == []
    assert any("reserve or goal money" in w for w in report["warnings"])
    assert report["result"]["summary"]["before"]["total"] == "43000.00"  # protected money is outside the plan


def test_protected_ids_and_bank_cash_stay_outside_the_plan():
    report = us(constraints={"protected_account_ids": ["ira"]})
    assert not any(t["account_id"] == "ira" for t in trades(report))
    assert {p["account_id"] for p in report["result"]["protected"]} == {"bank", "ira"}
    household = deepcopy(_REBAL_US_HOUSEHOLD)
    household["accounts"][3].pop("purpose")
    plain = us(household=household)
    assert any("bank cash is not deployed" in o["reason"] for o in plain["result"]["outside_plan"])
    assert not any(t["account_id"] == "bank" for t in trades(plain))


def test_missing_buy_price_is_reported_and_whole_shares_and_minimums_apply():
    targets = deepcopy(US_TARGETS)
    targets["sleeves"][1]["buy"] = ["AGG", "BND"]
    report = us(targets=targets, constraints={"min_trade_amount": 1000}, cash_flows=[{"id": "tiny", "account_id": "brk", "amount": 100}])
    assert "jurisdiction_context.instruments.AGG.price (buy candidate for bond)" in report["missing"]
    for trade in trades(report):
        assert Decimal(trade["quantity"]) == Decimal(trade["quantity"]).to_integral_value()
        assert Decimal(trade["estimated_amount_reporting"]["amount"]) >= 1000


def test_unassigned_positions_and_missing_fx_are_outside_the_plan():
    household = deepcopy(_REBAL_US_HOUSEHOLD)
    household["positions"].append({"id": "gold", "account_id": "brk", "instrument_id": "GLD", "symbol": "GLD",
                                   "quantity": 1, "value": 200, "currency": "USD", "asset_class": "commodity"})
    household["positions"].append({"id": "eur", "account_id": "brk", "instrument_id": "SXR8", "symbol": "SXR8",
                                   "quantity": 1, "value": 500, "currency": "EUR", "asset_class": "equity"})
    report = us(household=household)
    reasons = {o["position_id"]: o["reason"] for o in report["result"]["outside_plan"]}
    assert "not assigned" in reasons["gold"] and reasons["eur"] == "no usable FX rate"
    assert any(m.startswith("fx.EUR/USD") for m in report["missing"])


def test_needs_input_and_validation():
    assert rebalance.plan(None, US_TARGETS, jurisdiction_context=US_JC)["status"] == "needs_input"
    assert rebalance.plan(deepcopy(_REBAL_US_HOUSEHOLD), None, jurisdiction_context=US_JC)["status"] == "needs_input"
    with pytest.raises(ValueError, match="sum to 1"):
        us(targets={"sleeves": [{"name": "equity", "weight": 0.5, "buy": ["VTI"]}]})
    with pytest.raises(ValueError, match="jurisdiction"):
        us(jurisdiction_context={"jurisdiction": "CA"})
    with pytest.raises(ValueError, match="withdrawals"):
        us(cash_flows=[{"account_id": "brk", "amount": -10}])


def test_plan_is_deterministic():
    assert us()["result"] == us()["result"]
    assert mx()["result"] == mx()["result"]


def test_service_runs_rebalance_from_a_ledger_and_rejects_unknown_inputs(tmp_path):
    from wealth.catalog import CATALOG
    service = WealthService(tmp_path / "db.sqlite3")
    report = service.run("rebalance", inputs=CATALOG["rebalance"]["variants"]["from_ledger"])
    assert report["status"] == "ready"
    assert find(report, "brk", "VTI", "sell")["quantity"] == "0.8"  # fractional account
    no_prices = {k: v for k, v in CATALOG["rebalance"]["variants"]["from_ledger"].items() if k != "prices"}
    assert service.run("rebalance", inputs=no_prices)["status"] == "needs_input"
    with pytest.raises(ValueError, match="unknown"):
        service.run("rebalance", inputs={"jurisdiction_context": US_JC, "surprise": 1})


# ---------------------------------------------------------------- asset location


def test_us_location_puts_bonds_in_deferred_growth_in_roth_and_estimates_drag():
    from wealth.catalog import CATALOG
    inputs = CATALOG["asset_location"]["example"]
    report = rebalance.location(inputs["accounts"], deepcopy(inputs["sleeves"]), "US")
    assert report["status"] == "ready"
    rows = {row["sleeve"]: row for row in report["result"]["sleeves"]}
    assert rows["bonds"]["suggested"]["tax_deferred"] == "40000.00" and rows["bonds"]["suggested"]["taxable"] == "0.00"
    assert rows["small_cap_growth"]["suggested"]["tax_exempt"] == "10000.00"
    assert rows["us_equity"]["preferred_location"] == "taxable"
    difference = report["result"]["estimated_annual_tax_drag"]["difference"]
    assert Decimal(difference["low"]) > 0 and Decimal(difference["high"]) > Decimal(difference["low"])
    assert report["result"]["forces_trades"] is False and report["result"]["execution_ready"] is False
    assert any("ordinary rate 0.22-0.37" in a for a in report["assumptions"])


def test_mx_location_prefers_sic_ucits_flags_estate_and_points_to_deductions():
    from wealth.catalog import CATALOG
    inputs = deepcopy(CATALOG["asset_location"]["variants"]["mexico"])
    report = rebalance.location(inputs["accounts"], inputs["sleeves"], "MX")
    placements = {p["instrument_id"]: p for row in report["result"]["sleeves"] for p in row["placements"]}
    voo = placements["VOO"]
    assert voo["us_situs"] is True and voo["suggested_location"] == "mx_broker_sic"
    assert "Irish" in voo["reason"] and Decimal(voo["suggested_annual_drag"]["high"]) < Decimal(voo["annual_drag"]["low"])
    assert placements["CSPXN"]["annual_drag"] == placements["CSPXN"]["suggested_annual_drag"]
    cetes = report["result"]["sleeves"][1]["placements"][0]
    assert cetes["suggested_location"] == "mx_local" and cetes["gain_regime"] == "interest"
    tasks = {pointer["task"] for pointer in report["result"]["related_tasks"]}
    assert tasks == {"mx_deductions", "estate"}
    assert Decimal(report["result"]["estimated_annual_tax_drag"]["difference"]["low"]) > 0


def test_mx_location_reports_missing_platform_listing_and_domicile():
    report = rebalance.location([{"id": "broker", "type": "taxable"}],
                                [{"name": "us_equity", "holdings": [{"account_id": "broker", "instrument_id": "QQQ", "value": 1000}]}], "MX")
    assert report["status"] == "partial"
    assert any("platform" in m for m in report["missing"])
    assert any("issuer_domicile" in m for m in report["missing"])
    assert any("sic_listed" in m for m in report["missing"])
