"""Runnable fictional end-to-end wealth journey; offline and analysis-only."""
from __future__ import annotations

import argparse
from datetime import date, timedelta
import json
import math
from pathlib import Path
import tempfile

from wealth.service import WealthService


CLIENT_ID = "fictional-complete-demo"


def _iso(day: date) -> str:
    return day.isoformat()


def _household(today: date) -> dict:
    return {
        "currency": "USD", "as_of": _iso(today), "complete": True,
        "people": [{"id": "ana", "name": "Ana Example"}],
        "accounts": [{"id": "taxable", "owner_id": "ana", "tax_unit": "single",
                      "type": "taxable", "currency": "USD"}],
        "positions": [
            {"id": "p-acme", "account_id": "taxable", "instrument_id": "ACME", "symbol": "ACME",
             "quantity": 10, "value": 900, "currency": "USD", "asset_class": "equity"},
            {"id": "p-bond", "account_id": "taxable", "instrument_id": "BOND", "symbol": "BOND",
             "quantity": 10, "value": 1000, "currency": "USD", "asset_class": "fixed_income"},
            {"id": "p-cash", "account_id": "taxable", "instrument_id": "USD-CASH", "symbol": "USD",
             "quantity": 600, "value": 600, "currency": "USD", "asset_class": "cash"},
        ],
        "lots": [{"id": "lot-acme", "account_id": "taxable", "instrument_id": "ACME",
                  "quantity": 10, "acquired_on": "2023-01-03", "cost_basis": 1200, "currency": "USD"}],
        "liabilities": [], "external_assets": [], "income_exposures": [], "fx": [], "fund_holdings": [],
    }


def _prices(today: date) -> dict:
    days = []
    cursor = today
    while len(days) < 120:
        if cursor.weekday() < 5:
            days.append(cursor)
        cursor -= timedelta(days=1)
    days.reverse()
    rows = []
    for index, day in enumerate(days):
        rows.append({
            "date": _iso(day),
            "ACME": round(80 * math.exp(0.0012 * index + 0.025 * math.sin(index / 7)), 6),
            "BOND": round(98 * math.exp(0.00025 * index + 0.004 * math.cos(index / 11)), 6),
        })
    return {"currency": "USD", "source": "fictional offline adjusted-close series", "rows": rows}


def _research_inputs(today: date) -> dict:
    source = {"id": "fictional-filing", "title": "Fictional ACME annual report",
              "url": "https://example.com/fictional-acme-report", "as_of": _iso(today), "kind": "filing"}
    return {
        "symbol": "ACME", "as_of": _iso(today), "sources": [source],
        "business_facts": [{"label": "business", "value": "Fictional industrial software", "source_ids": [source["id"]]}],
        "statements": [
            {"period_end": _iso(today), "period_type": "FY", "currency": "USD", "source_ids": [source["id"]], "metrics": {
                "revenue": 120, "gross_profit": 72, "operating_income": 18, "net_income": 12,
                "total_assets": 200, "shareholders_equity": 80, "cash": 20, "total_debt": 50,
                "ebitda": 25, "current_assets": 60, "current_liabilities": 30,
                "operating_cash_flow": 24, "capital_expenditure": -6,
            }},
            {"period_end": _iso(today.replace(year=today.year - 1)), "period_type": "FY", "currency": "USD",
             "source_ids": [source["id"]], "metrics": {"revenue": 100}},
        ],
        "thesis_evidence": {
            "bullish": [{"claim": "Recurring revenue expanded in the fictional filing.", "source_ids": [source["id"]]}],
            "bearish": [{"claim": "Debt remains material relative to EBITDA.", "source_ids": [source["id"]]}],
            "disconfirming": [{"claim": "Weak renewal data would undermine the thesis.", "source_ids": [source["id"]]}],
        },
    }


def _mx_household(today: date) -> dict:
    return {
        "currency": "MXN", "as_of": _iso(today), "complete": True,
        "people": [{"id": "ana"}],
        "accounts": [{"id": "mx-taxable", "owner_id": "ana", "type": "taxable", "currency": "MXN"}],
        "positions": [{"id": "mx-position", "account_id": "mx-taxable", "instrument_id": "BMV-X",
                       "symbol": "BMV-X", "quantity": 100, "value": 12000, "currency": "MXN"}],
        "lots": [{"id": "mx-lot", "account_id": "mx-taxable", "instrument_id": "BMV-X",
                  "quantity": 100, "acquired_on": "2023-01-03", "cost_basis": 18000, "currency": "MXN"}],
    }


def run_demo(db_path: str | Path) -> dict:
    """Run every calculator through ``WealthService`` and return compact proof."""
    today = date.today()
    expiry = _iso(today + timedelta(days=30))
    service = WealthService(db_path)
    service.create(CLIENT_ID, "Ana Example (fictional)")

    imported = service.run(
        "import", {"format": "json", "data": _household(today), "source": "fictional custodian export"},
        CLIENT_ID, save_as="household", expires_on=expiry,
    )
    revision = imported["saved"]["client_revision"]
    source = {"kind": "user", "ref": "fictional demo conversation", "observed_on": _iso(today)}
    remembered = service.remember(CLIENT_ID, [
        {"key": "goals", "value": [{"id": "home", "name": "Fictional home deposit", "currency": "USD",
          "due": _iso(today + timedelta(days=900)), "target_amount": 50000,
          "funded_outside_pool": 0, "protect_now": True}], "source": source,
         "confidence": "confirmed", "expires_on": expiry},
        {"key": "preference.risk", "value": {"maximum_equity_weight": 0.65, "note": "fictional preference"},
         "source": source, "confidence": "confirmed", "expires_on": expiry},
        {"key": "thesis.ACME", "value": {"summary": "Recurring revenue may be durable.",
          "reconsider_if": "Renewal data weakens."}, "source": source, "confidence": "confirmed", "expires_on": expiry},
    ], revision, "fictional-initial-memory")
    revision = remembered["client"]["revision"]

    exposure = service.run("exposure", {}, CLIENT_ID)
    prices = _prices(today)
    market_analysis = service.run("analyze", {"benchmark": "ACME", "prices": prices}, CLIENT_ID)
    construction = service.run("construct", {"method": "equal", "max_weight": 0.7, "prices": prices}, CLIENT_ID)

    research_inputs = _research_inputs(today)
    research = service.run("research", research_inputs, CLIENT_ID)
    valuation = service.run("value", {
        "symbol": "ACME", "as_of": _iso(today), "sources": research_inputs["sources"],
        "scenarios": [{
            "name": "fictional base", "kind": "dcf_fcff", "currency": "USD", "valuation_date": _iso(today),
            "forecast": [{"period": 1, "free_cash_flow": 10}, {"period": 2, "free_cash_flow": 11}],
            "discount_rate": 0.10, "terminal_growth": 0.02, "net_debt": 20,
            "shares_outstanding": 10, "current_price": 90, "current_price_currency": "USD", "source_ids": ["fictional-filing"],
        }],
    }, CLIENT_ID)

    projection = service.run("project", {
        "currency": "USD", "as_of": _iso(today), "end_date": _iso(today.replace(year=today.year + 10)),
        "initial_wealth": 250000, "simulations": 100, "seed": 17,
        "return_model": {"type": "bootstrap", "historical_annual_returns": [-0.12, 0.04, 0.09, 0.16]},
        "annual_inflation": 0.025, "annual_fee": 0.006, "annual_tax_drag": 0.008,
        "cashflows": [{"id": "home", "date": _iso(today + timedelta(days=900)), "amount": 50000,
                       "type": "withdrawal", "inflation_indexed": True, "hard_goal": True}],
        "recurring_cashflows": [],
    }, CLIENT_ID)
    income = service.run("income", {
        "currency": "USD", "initial_wealth": 250000, "years": 10, "annual_income_need": 12000,
        "annual_need_growth": 0.025, "annual_dividend_yield": 0.025, "annual_fee": 0.006,
        "annual_tax_drag": 0.008, "simulations": 100, "seed": 17,
        "return_model": {"type": "parametric", "annual_return": 0.06, "annual_volatility": 0.12},
    }, CLIENT_ID)
    ladder = service.run("ladder", {
        "currency": "USD", "as_of": _iso(today), "reserve": {"currency": "USD", "amount": 10000},
        "liabilities": [
            {"id": "tuition-1", "date": _iso(today + timedelta(days=365)), "amount": 20000, "currency": "USD"},
            {"id": "tuition-2", "date": _iso(today + timedelta(days=730)), "amount": 20000, "currency": "USD"},
        ],
        "assets": [{"id": "fictional-bond", "currency": "USD", "cashflows": [
            {"date": _iso(today + timedelta(days=300)), "amount": 12000},
            {"date": _iso(today + timedelta(days=665)), "amount": 18000},
        ]}],
    }, CLIENT_ID)

    sale_date = today - timedelta(days=40)
    us_tax = service.run("tax", {
        "jurisdiction": "US", "sale_date": _iso(sale_date), "as_of": _iso(today), "mode": "rebalance",
        "sales": [{"lot_id": "lot-acme", "quantity": 10}],
        "prices": [{"instrument_id": "ACME", "price": 90, "currency": "USD",
                    "as_of": _iso(sale_date), "source": "fictional broker quote"}],
        "filing_status": "single",
        "us_tax_facts": {
            "as_of": _iso(sale_date), "complete": True,
            "short_term_gains": 0, "short_term_losses": 0,
            "long_term_gains": 1000, "long_term_losses": 0,
            "short_term_loss_carryover": 0, "long_term_loss_carryover": 0,
            "ordinary_income_loss_deduction_available": 3000,
        },
        "rates": {"ordinary": 0.37, "long_term": 0.15},
        "wash_sale_coverage": {
            "accounts_complete": True, "identity_mapping_complete": True,
            "automatic_reinvestment_reviewed": True, "spouse_accounts_reviewed": True,
            "controlled_accounts_reviewed": True,
            "purchases_from": _iso(sale_date - timedelta(days=30)), "purchases_through": _iso(today),
        },
    }, CLIENT_ID)
    mx_tax = service.run("tax", {
        "jurisdiction": "MX_ARTICLE_129", "household": _mx_household(today), "as_of": _iso(today), "sale_date": _iso(today),
        "article_129_sales": [{"lot_id": "mx-lot", "quantity": 100, "proceeds_mxn": 12000,
                               "article_129_adjusted_basis_mxn": 18000,
                               "basis_source": "fictional broker Article 129 statement",
                               "proceeds_source": "fictional broker execution statement",
                               "article_129_eligibility": {
                                   "individual_taxpayer": True, "eligible_security": True,
                                   "eligible_venue": True, "eligible_acquisition": True,
                                   "no_exclusion_applies": True,
                                   "source": "fictional broker tax statement and taxpayer records",
                                   "security_scope": "BMV-X", "venue": "Bolsa Mexicana de Valores",
                                   "acquisition_scope": "mx-lot",
                               }}],
        "article_129_realized_gain_or_loss_mxn": 10000,
        "article_129_loss_carryforwards": [{"origin_year": today.year - 1,
                                             "available_updated_mxn": 2000,
                                             "updated_through": f"{today.year - 1}-12"}],
    }, CLIENT_ID)

    recalled = service.recall(CLIENT_ID, "home goal risk ACME thesis")
    current = service.inspect(CLIENT_ID)
    evidence_ids = [fact["id"] for fact in current["facts"] if fact["key"] in {"household", "goals", "preference.risk"}]
    proposal = service.propose(CLIENT_ID, "Protect the fictional home goal",
                               "Keep the dated goal visible before considering the modeled allocation.",
                               revision, evidence_ids, ["Refresh assumptions before changing allocation"])
    service.resolve(CLIENT_ID, proposal["id"], "accepted", revision)
    corrected_goal = {
        "key": "goals", "value": [{"id": "home", "name": "Fictional home deposit", "currency": "USD",
        "due": _iso(today + timedelta(days=900)), "target_amount": 65000,
        "funded_outside_pool": 0, "protect_now": True}], "source": source,
        "confidence": "confirmed", "expires_on": expiry,
    }
    correction = service.remember(CLIENT_ID, [corrected_goal], revision, "fictional-goal-correction")
    corrected_revision = correction["client"]["revision"]
    invalidated = service.inspect(CLIENT_ID)["decisions"][0]
    rule = {"id": "decision-review", "kind": "review"}
    first_monitor = service.run("monitor", {"rules": [rule]}, CLIENT_ID)
    repeated_monitor = service.run("monitor", {"rules": [rule]}, CLIENT_ID)

    return {
        "notice": "Fictional demonstration. Supplied offline data only; no network, trades, transfers, or messages.",
        "client": {"id": CLIENT_ID, "revision": corrected_revision},
        "statuses": {
            "import": imported["status"], "exposure": exposure["status"],
            "market_analyze": market_analysis["status"], "market_construct": construction["status"],
            "research": research["status"], "value": valuation["status"],
            "project": projection["status"], "income": income["status"], "ladder": ladder["status"],
            "us_tax": us_tax["status"], "mx_tax": mx_tax["status"],
        },
        "outcomes": {
            "known_nav": exposure["result"]["known_nav"],
            "constructed_weights": construction["result"]["weights"],
            "research_revenue_growth": research["result"]["financial_metrics"]["revenue_growth"]["value"],
            "valuation_per_share": valuation["result"]["scenarios"][0]["implied_value_per_share"],
            "project_all_withdrawals_fulfilled_probability_percent": projection["result"]["all_withdrawals_fulfilled_probability_percent"],
            "income_deficit_probability_percent": income["result"]["strategies"]["total_return_sales"]["probability_of_any_income_deficit_percent"],
            "ladder_total_gap": ladder["result"]["cumulative_on_due_date_gap"],
            "us_incremental_tax": us_tax["result"]["incremental_tax_estimate"]["incremental_tax"],
            "mx_incremental_tax": mx_tax["result"]["incremental_tax_estimate"]["incremental_tax"],
        },
        "memory": {
            "recalled_keys": sorted({item["key"] for item in recalled["matches"] if item["type"] == "fact"}),
            "goal_history_revisions": len(service.inspect(CLIENT_ID, "history", "goals")["history"]),
            "accepted_decision_needs_review_after_correction": invalidated["needs_review"],
            "first_monitor_events": len(first_monitor["result"]["events"]),
            "unchanged_repeat_events": len(repeated_monitor["result"]["events"]),
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, help="SQLite path; omit for a temporary database")
    args = parser.parse_args(argv)
    if args.db:
        result = run_demo(args.db)
    else:
        with tempfile.TemporaryDirectory(prefix="wealth-complete-demo-") as directory:
            result = run_demo(Path(directory) / "demo.sqlite3")
    print(json.dumps(result, indent=2, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
