"""Small, JSON-safe discovery catalog for :meth:`WealthService.run` tasks.

Examples are task ``inputs`` objects. A host passes them inside
``wealth_run(task=..., inputs=...)`` and may add ``client_id`` when remembered
context should be used. Fields described as stored alternatives are not required
inside ``inputs`` when an eligible fact is already present for that client.
Examples are fictional dated fixtures, not current evidence or client defaults.
The fund fixture omits constituents, so its look-through coverage is partial.
"""

from __future__ import annotations

from typing import Any


_HOUSEHOLD: dict[str, Any] = {
    "currency": "USD",
    "as_of": "2026-09-20",
    "complete": True,
    "people": [{"id": "p1", "name": "Ana"}],
    "accounts": [{"id": "a1", "owner_id": "p1", "type": "taxable", "currency": "USD"}],
    "positions": [
        {"id": "pos-1", "account_id": "a1", "instrument_id": "SPY", "symbol": "SPY",
         "quantity": 10, "value": 6000, "currency": "USD", "asset_class": "fund"}
    ],
    "lots": [], "liabilities": [], "external_assets": [], "income_exposures": [],
    "fx": [], "fund_holdings": [],
}


CATALOG: dict[str, dict[str, Any]] = {
    "import": {
        "purpose": "Validate and reconcile a household from canonical JSON, CSV, or XLSX without inventing accounts, lots, or values.",
        "required": ["household, or format plus data/path"],
        "optional": ["source", "mapping", "defaults", "sheets"],
        "example": {"household": _HOUSEHOLD, "source": "custodian export"},
        "variants": {
            "csv": {
                "format": "csv",
                "data": "kind,identifier,owner,acct_type,ccy\npeople,p1,,,\naccounts,a1,p1,taxable,USD\n",
                "mapping": {"record_type": "kind", "people": {"id": "identifier"}, "accounts": {"id": "identifier", "owner_id": "owner", "type": "acct_type", "currency": "ccy"}},
                "defaults": {"household": {"currency": "USD", "as_of": "2026-09-20", "complete": False}},
                "source": "custodian CSV",
            }
        },
    },
    "exposure": {
        "purpose": "Measure household NAV, liquidity, exposures, fund look-through, overlap, coverage, and target ranges.",
        "required": ["household (unless stored for the client)"],
        "optional": ["targets", "max_fx_age_days", "max_fund_age_days"],
        "example": {"household": _HOUSEHOLD, "targets": {"asset_class": {"equity": 0.6}}},
    },
    "analyze": {
        "purpose": "Describe historical return, drawdown, volatility, beta, correlation, and concentration on one common sample.",
        "required": ["household or weights", "a usable price source"],
        "optional": ["currency", "scope", "account_ids", "benchmark", "years", "rebalance", "prices", "price_csv", "price_source"],
        "example": {"currency": "USD", "weights": {"SPY": 0.6, "BND": 0.4}, "benchmark": "SPY", "years": 5},
    },
    "stress": {
        "purpose": "Apply explicit shocks or dated historical windows to supplied holdings; scenarios are arithmetic, not forecasts.",
        "required": ["household or weights", "scenarios", "a usable price source for historical windows"],
        "optional": ["currency", "scope", "account_ids", "prices", "price_csv", "price_source"],
        "example": {"currency": "USD", "weights": {"SPY": 0.7, "BND": 0.3}, "scenarios": [{"name": "equity shock", "shocks": {"SPY": -0.2, "BND": 0.02}}]},
    },
    "compare": {
        "purpose": "Compare current and proposed weights on the same price sample and convention.",
        "required": ["currency", "current_weights", "proposed_weights", "a usable price source"],
        "optional": ["benchmark", "years", "rebalance", "prices", "price_csv", "price_source"],
        "example": {"currency": "USD", "current_weights": {"SPY": 0.8, "BND": 0.2}, "proposed_weights": {"SPY": 0.6, "BND": 0.4}, "benchmark": "SPY"},
    },
    "construct": {
        "purpose": "Construct constrained weights beside an equal-weight baseline; advanced methods remain assumption-sensitive.",
        "required": ["household, or tickers plus currency", "a usable price source"],
        "optional": ["method: equal|invvol|minvar|riskparity|hrp|cvar|black_litterman", "max_weight", "confidence", "min_annual_return", "prior_returns", "views[]: weights,expected_return,confidence", "tau", "risk_aversion", "validation: train_days,test_days,transaction_cost_bps,belief_schedule", "prices", "price_csv"],
        "example": {"currency": "USD", "tickers": ["SPY", "BND", "GLD"], "method": "hrp", "max_weight": 0.6, "years": 5},
        "variants": {
            "black_litterman_with_walk_forward": {
                "currency": "USD", "tickers": ["SPY", "BND", "GLD"], "years": 5,
                "method": "black_litterman", "max_weight": 0.6,
                "prior_returns": {"SPY": 0.06, "BND": 0.03, "GLD": 0.03},
                "views": [{"weights": {"SPY": 1, "BND": -1}, "expected_return": 0.03, "confidence": 0.65}],
                "tau": 0.05, "risk_aversion": 3,
                "validation": {"train_days": 252, "test_days": 63, "transaction_cost_bps": 10, "belief_schedule": [{"effective_on": "2020-01-01", "prior_returns": {"SPY": 0.06, "BND": 0.03, "GLD": 0.03}, "views": [{"weights": {"SPY": 1, "BND": -1}, "expected_return": 0.03, "confidence": 0.65}]}]},
            }
        },
    },
    "factors": {
        "purpose": "Estimate historical Fama–French factor exposures for non-cash holdings.",
        "required": ["household, or tickers plus currency", "a usable price source"],
        "optional": ["model: 3|5", "years", "prices", "price_csv", "price_source"],
        "example": {"currency": "USD", "tickers": ["SPY", "IWM"], "model": 3, "years": 5},
    },
    "research": {
        "purpose": "Normalize dated company or fund evidence, calculate source-backed metrics, and compare with a prior case.",
        "required": ["symbol", "sources/evidence or explicit live_fetch=true"],
        "optional": ["as_of", "entity_type: company|fund", "live_fetch", "sources", "business_facts", "fund_facts", "statements", "fund_holdings", "thesis_evidence"],
        "example": {"symbol": "MSFT", "entity_type": "company", "live_fetch": True},
    },
    "value": {
        "purpose": "Calculate explicit DCF or multiples scenarios from dated, cited assumptions; never a target-price forecast.",
        "required": ["symbol", "sources", "scenarios"],
        "optional": ["as_of", "scenario current_price"],
        "example": {
            "symbol": "ACME", "as_of": "2026-09-20",
            "sources": [{"id": "filing", "title": "Annual filing", "url": "https://example.com/filing", "as_of": "2026-09-20", "kind": "filing"}],
            "scenarios": [{"kind": "multiples", "name": "base", "currency": "USD", "valuation_date": "2026-09-20", "basis": "equity_value", "metric_name": "earnings", "metric_value": 100, "multiple": 15, "shares_outstanding": 100, "source_ids": ["filing"]}],
        },
    },
    "plan": {
        "purpose": "Reserve capital for essentials, selected debt payments, and protected dated goals without double-counting outside funds.",
        "required": ["plan.resources and goals, unless both are stored for the client"],
        "optional": ["plan.resources.cash_available: explicitly known unrestricted cash within available_capital, not total assets; omit if unknown", "outside_sources", "reserve_funding", "goal outside_funding"],
        "scope": "Available capital may already be invested. Only additional_cash_to_invest answers new cash deployment; null means unknown. Reserve, debt payments and goals must be disjoint; do not also list the emergency reserve as a protected goal.",
        "example": {
            "plan.resources": {"currency": "USD", "available_capital": 100000, "monthly_essentials": 4000, "reserve_months": 6, "reserve_outside_pool": 0, "debt_payments_from_pool": 0},
            "goals": [{"id": "home", "name": "Home", "currency": "USD", "due": "2027-09-20", "target_amount": 30000, "funded_outside_pool": 0, "protect_now": True}],
        },
    },
    "calendar": {
        "purpose": "Compare explicitly expected monthly cash receipts with needs and commitments, including calendar gaps.",
        "required": ["income.schedule, unless stored for the client"],
        "optional": [],
        "example": {"income.schedule": {"currency": "USD", "monthly_need": 3000, "months": [{"month": "2026-10", "expected_cash_received": 2500, "committed_outflow": 300}]}},
    },
    "project": {
        "purpose": "Project dated contributions and withdrawals under explicit parametric or bootstrap returns, fees, tax drag, and inflation.",
        "required": ["currency", "as_of", "end_date", "initial_wealth", "simulations", "seed", "return_model", "annual_inflation", "annual_fee", "annual_tax_drag", "cashflows", "recurring_cashflows"],
        "optional": ["cashflows[]: id,date,amount,type,inflation_indexed,hard_goal", "recurring_cashflows[]: id,start,end,amount,type,frequency,inflation_indexed,hard_goal"],
        "example": {"currency": "USD", "as_of": "2026-09-20", "end_date": "2028-09-20", "initial_wealth": 100000, "simulations": 1000, "seed": 7, "return_model": {"type": "parametric", "annual_return": 0.05, "annual_volatility": 0.12}, "annual_inflation": 0.03, "annual_fee": 0.001, "annual_tax_drag": 0.01, "cashflows": [{"id": "home", "date": "2027-09-20", "amount": 30000, "type": "withdrawal", "inflation_indexed": False, "hard_goal": True}], "recurring_cashflows": [{"id": "savings", "start": "2026-10-20", "end": "2028-08-20", "amount": 1000, "type": "contribution", "frequency": "monthly", "inflation_indexed": False, "hard_goal": False}]},
    },
    "income": {
        "purpose": "Compare dividend-cash and total-return withdrawals without double-counting yield, including sequence risk.",
        "required": ["currency", "initial_wealth", "years", "annual_income_need", "annual_need_growth", "annual_dividend_yield", "annual_fee", "annual_tax_drag", "simulations", "seed", "return_model"],
        "optional": [],
        "example": {"currency": "USD", "initial_wealth": 1000000, "years": 20, "annual_income_need": 50000, "annual_need_growth": 0.03, "annual_dividend_yield": 0.025, "annual_fee": 0.001, "annual_tax_drag": 0.01, "simulations": 1000, "seed": 7, "return_model": {"type": "bootstrap", "historical_annual_returns": [-0.2, 0.1, 0.15, 0.04]}},
    },
    "ladder": {
        "purpose": "Match reserves and dated asset cash flows to dated liabilities, reporting gaps without inventing instruments.",
        "required": ["currency", "as_of", "reserve", "liabilities", "assets"],
        "optional": ["modeled_fx", "arrears_policy: carry_forward|drop_after_miss (default carry_forward)"],
        "example": {"currency": "USD", "as_of": "2026-09-20", "reserve": {"currency": "USD", "amount": 20000}, "liabilities": [{"id": "tuition", "date": "2027-06-01", "amount": 30000, "currency": "USD"}], "assets": [{"id": "bond", "currency": "USD", "cashflows": [{"date": "2027-05-01", "amount": 15000}]}]},
    },
    "tax": {
        "purpose": "Screen explicit taxable-security scenarios for US federal rules or qualifying Mexico Article 129 treatment.",
        "required": ["jurisdiction", "household with accounts and lots", "jurisdiction-specific sale and coverage inputs"],
        "optional": ["US: sale_date,as_of,mode,sales,prices,purchases,substantially_identical_groups,wash_sale_coverage,us_tax_facts,filing_status,rates", "MX: article_129_sales with per-sale eligibility, realized result, loss carryforwards"],
        "example": {
            "jurisdiction": "MX_ARTICLE_129", "as_of": "2026-09-20",
            "household": {"currency": "MXN", "as_of": "2026-09-20", "complete": True, "people": [{"id": "p1"}], "accounts": [{"id": "a1", "owner_id": "p1", "tax_unit": "individual", "type": "taxable", "currency": "MXN"}], "positions": [{"id": "pos-1", "account_id": "a1", "instrument_id": "NAFTRAC", "symbol": "NAFTRAC", "quantity": 10, "value": 900, "currency": "MXN"}], "lots": [{"id": "lot-1", "account_id": "a1", "instrument_id": "NAFTRAC", "quantity": 10, "cost_basis": 1000, "acquired_on": "2024-01-01", "currency": "MXN"}], "liabilities": [], "external_assets": [], "income_exposures": [], "fx": [], "fund_holdings": []},
            "article_129_sales": [{"lot_id": "lot-1", "quantity": 10, "proceeds_mxn": 900, "article_129_adjusted_basis_mxn": 1000, "basis_source": "broker calculation", "proceeds_source": "scenario quote", "article_129_eligibility": {"individual_taxpayer": True, "eligible_security": True, "eligible_venue": True, "eligible_acquisition": True, "no_exclusion_applies": True, "source": "broker and tax review", "security_scope": "NAFTRAC", "venue": "BMV", "acquisition_scope": "lot-1"}}],
            "article_129_realized_gain_or_loss_mxn": 0,
            "article_129_loss_carryforwards": [],
        },
        "variants": {
            "US": {
                "jurisdiction": "US", "sale_date": "2026-02-28", "as_of": "2026-04-15", "mode": "rebalance",
                "household": {"currency": "USD", "as_of": "2026-04-15", "complete": True, "people": [{"id": "p1"}], "accounts": [{"id": "taxable", "owner_id": "p1", "tax_unit": "single", "type": "taxable", "currency": "USD"}], "positions": [{"id": "pos-loss", "account_id": "taxable", "instrument_id": "LOSS", "symbol": "LOSS", "quantity": 100, "value": 1000, "currency": "USD"}], "lots": [{"id": "lot-loss", "account_id": "taxable", "instrument_id": "LOSS", "quantity": 100, "acquired_on": "2024-01-01", "cost_basis": 2000, "currency": "USD"}], "liabilities": [], "external_assets": [], "income_exposures": [], "fx": [], "fund_holdings": []},
                "sales": [{"lot_id": "lot-loss", "quantity": 100}],
                "prices": [{"instrument_id": "LOSS", "price": 10, "currency": "USD", "as_of": "2026-02-28", "source": "broker sale-date quote"}],
                "purchases": [], "substantially_identical_groups": [],
                "wash_sale_coverage": {"accounts_complete": True, "identity_mapping_complete": True, "automatic_reinvestment_reviewed": True, "spouse_accounts_reviewed": True, "controlled_accounts_reviewed": True, "purchases_from": "2026-01-01", "purchases_through": "2026-04-15"},
                "us_tax_facts": {"short_term_gains": 0, "short_term_losses": 0, "long_term_gains": 0, "long_term_losses": 0, "short_term_loss_carryover": 0, "long_term_loss_carryover": 0, "ordinary_income_loss_deduction_available": 3000, "complete": True, "as_of": "2026-02-28"},
                "filing_status": "single", "rates": {"ordinary": 0.37, "long_term": 0.15},
            }
        },
    },
    "monitor": {
        "purpose": "Evaluate opt-in review, expiry, drift, goal, threshold, or thesis rules and return only state changes to the caller.",
        "required": ["client_id on wealth_run", "rules or stored monitor.rules"],
        "optional": ["rules[].enabled"],
        "example": {"rules": [{"id": "goal-freshness", "kind": "expiry", "keys": ["goals", "plan.resources"]}]},
    },
}


__all__ = ["CATALOG"]
