"""Plan maths regressions: US netting in rebalance, Mexican foreign-securities totals, required return."""
from __future__ import annotations

import math
from copy import deepcopy
from decimal import Decimal

import pytest

from wealth import mexico, policy, rebalance
from wealth.catalog import _REBAL_US_HOUSEHOLD


def test_rebalance_nets_short_and_long_term_results_before_applying_rates():
    # Repro reb1: the plan sells a 2,000 short-term loss lot and a 900 long-term gain lot.
    # Per-trade tax was -2,000 x 32% + 900 x 15% = -505.  US netting: ST -2,000 + LT +900 = net ST loss
    # -1,100, deductible (under 3,000) at the ordinary 32%: -1,100 x 0.32 = -352.
    household = deepcopy(_REBAL_US_HOUSEHOLD)
    household["accounts"] = [a for a in household["accounts"] if a["id"] == "brk"]
    household["positions"] = [p for p in household["positions"] if p["account_id"] == "brk"]
    targets = {"by": "asset_class", "sleeves": [{"name": "equity", "weight": 0.5, "buy": ["VTI"]},
                                                {"name": "bond", "weight": 0.5, "buy": ["BND"]}]}
    context = {"jurisdiction": "US", "trade_date": "2026-09-18", "accounts": {"brk": {"commission_rate": 0}},
               "instruments": {"BND": {"price": 70, "currency": "USD"}}}
    result = rebalance.plan(household, targets, jurisdiction_context=context,
                            tax_inputs={"rates": {"ordinary": 0.32, "long_term": 0.15}})["result"]
    sell = next(t for t in result["trades"] if t["side"] == "sell")
    assert sorted((l["lot_id"], l["gain_or_loss"]) for l in sell["lots"]) == [("lot-loss", "-2000.00"), ("lot-lt", "900.00")]
    assert result["summary"]["total_estimated_tax"] == "-352.00"
    assert sell["estimated_tax"]["amount"] == "-352.00"
    per_trade = sum(Decimal(t["estimated_tax"]["amount"]) for t in result["trades"] if t["side"] == "sell")
    assert per_trade == Decimal("-352.00")
    assert result["tax_engine_check"]["incremental_tax_estimate"]["incremental_tax"] == "-352.00"


A129_NONE = {"article_129_realized_gain_or_loss_mxn": 0, "article_129_loss_carryforwards": []}
SIC_SALE = {"id": "VOO", "currency": "USD", "proceeds": 1500, "fx_sale": 18, "cost": 1000, "fx_acquisition": 20,
            "acquired_on": "2024-01-10", "sold_on": "2026-03-01", "sic_listed": True, "cost_update_factor": 1}


def test_foreign_securities_with_only_sic_sales_names_its_totals_and_computes_the_known_tax():
    # Repro mx1: 1,500 x 18 - 1,000 x 20 = 7,000 MXN SIC gain; 10% = 700.  The progressive total read 0.00
    # beside it and the net tax was unknown without income inputs although nothing was progressive.
    # Without the year's other Art. 129 results and carryforwards the Art. 129 tax is not estimated;
    # only the scenario-only figure is shown.
    unknown = mexico.foreign_securities({"tax_year": 2026, "sales": [SIC_SALE]})
    assert unknown["status"] == "partial"
    assert unknown["result"]["totals"]["article_129_tax_before_other_results_mxn"] == "700.00"
    assert unknown["result"]["totals"]["article_129_tax_mxn"] is None
    assert unknown["result"]["net_estimated_mexican_tax_mxn"] is None
    assert "article_129_realized_gain_or_loss_mxn" in unknown["missing"] and "article_129_loss_carryforwards" in unknown["missing"]
    report = mexico.foreign_securities({"tax_year": 2026, "sales": [SIC_SALE], **A129_NONE})
    totals, result = report["result"]["totals"], report["result"]
    assert totals["article_129_net_gain_or_loss_mxn"] == "7000.00" and totals["article_129_tax_mxn"] == "700.00"
    assert totals["progressive_net_gain_or_loss_mxn"] is None and "net_gain_or_loss_mxn" not in totals
    assert result["net_estimated_mexican_tax_mxn"] == "700.00" and result["known_tax_mxn"] == "700.00"
    assert report["status"] == "ready" and "taxable_income_before_mxn or marginal_rate" not in report["missing"]


def test_foreign_securities_shows_the_known_tax_when_the_progressive_part_is_unknown():
    # Add a non-SIC sale with the same numbers (7,000 progressive) and no income inputs:
    # the whole estimate is unknown, the known 700 is still reported and the unknown part named.
    report = mexico.foreign_securities({"tax_year": 2026, "sales": [SIC_SALE, dict(SIC_SALE, id="X", sic_listed=False)], **A129_NONE})
    result = report["result"]
    assert result["totals"]["progressive_net_gain_or_loss_mxn"] == "7000.00"
    assert result["net_estimated_mexican_tax_mxn"] is None
    assert result["known_tax_mxn"] == "700.00" and result["unknown_tax_components"] == ["progressive_isr_change_mxn"]
    # With a 30% marginal rate: 700 + 7,000 x 0.30 = 2,800.
    report = mexico.foreign_securities({"tax_year": 2026, "marginal_rate": "0.3", **A129_NONE,
                                        "sales": [SIC_SALE, dict(SIC_SALE, id="X", sic_listed=False)]})
    assert report["result"]["net_estimated_mexican_tax_mxn"] == "2800.00"


@pytest.mark.parametrize("target,funded,monthly,months", [
    (12000, 0, 1000, 12),      # contributions alone reach it exactly
    (500, 1000, 0, 12),        # already above target
    (10, 1000, 0, 12),         # far above target (it read -99 %)
    (12000, -1000, 2000, 12),  # 23,000 at 0 %
])
def test_required_return_for_a_target_already_met_is_zero_never_negative(target, funded, monthly, months):
    solved = policy.required_return(target, funded, monthly, months)
    assert solved["status"] == "met" and solved["rate"] == 0.0 and math.copysign(1, solved["rate"]) == 1


def test_required_return_still_solves_positive_rates():
    # 1,000 to 1,100 in 12 months: 10 % a year.
    assert policy.required_return(1100, 1000, 0, 12) == {"status": "ready", "rate": 0.1}
    assert policy.required_return(10 ** 6, 0, 100, 12)["status"] == "unrealistic"
