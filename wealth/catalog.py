"""Small, JSON-safe discovery catalog for :meth:`WealthService.run` tasks.

Examples are task ``inputs`` objects. A host passes them inside
``wealth_run(task=..., inputs=...)`` and may add ``client_id`` when remembered
context should be used. Fields described as stored alternatives are not required
inside ``inputs`` when an eligible fact is already present for that client.
Ledger tasks read the client's stored transaction ledger; the inline ``ledger``
in their examples only makes the example runnable without a client.
Examples are fictional dated fixtures, not current evidence or client defaults.
Every example and variant is executed offline by ``tests/test_catalog.py``.
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

_SOURCE = {"kind": "document", "ref": "bank and broker statements"}
_OWNER = [{"person_id": "p1", "share": "1"}]
_LEDGER: dict[str, Any] = {
    "accounts": [
        {"id": "bank", "institution": "Bank", "type": "checking", "currency": "USD", "owners": _OWNER},
        {"id": "brk", "institution": "Broker", "type": "brokerage", "currency": "USD", "owners": _OWNER},
    ],
    "instruments": [{"id": "VTI", "symbol": "VTI", "currency": "USD", "listing_currency": "USD",
                     "asset_class": "fund", "underlying_symbol": "VTI"}],
    "entries": [
        {"id": "e1", "account_id": "bank", "kind": "opening_balance", "date": "2026-07-01", "amount": "3000",
         "currency": "USD", "confidence": "reported", "source": _SOURCE},
        {"id": "e2", "account_id": "bank", "kind": "expense", "date": "2026-07-01", "amount": "-2000",
         "currency": "USD", "description": "RENT JULY", "confidence": "reported", "source": _SOURCE},
        {"id": "e3", "account_id": "bank", "kind": "income", "subtype": "salary", "date": "2026-07-15",
         "amount": "5000", "currency": "USD", "description": "PAYROLL ACME", "confidence": "reported", "source": _SOURCE},
        {"id": "e4", "account_id": "bank", "kind": "transfer", "date": "2026-07-20", "amount": "-1000",
         "currency": "USD", "description": "TO BROKER", "confidence": "reported", "source": _SOURCE},
        {"id": "e5", "account_id": "brk", "kind": "transfer", "date": "2026-07-20", "amount": "1000",
         "currency": "USD", "description": "FROM BANK", "confidence": "reported", "source": _SOURCE},
        {"id": "e6", "account_id": "brk", "kind": "buy", "date": "2026-07-21", "instrument_id": "VTI",
         "quantity": "4", "amount": "-1000", "currency": "USD", "confidence": "reported", "source": _SOURCE},
    ],
    "fx": [], "assertions": [], "labels": [], "category_rules": [],
}

_PRICE_SOURCE = ["prices {currency, source, rows:[{date, SYMBOL: price}]}", "price_csv + price_source",
                 "years (live Yahoo prices, default 5)"]
_MARKET_COMMON = [
    "currency", "scope", "account_ids", "rebalance",
    "risk_free: none|ken_french (USD, fetched)|{annual_rate, source}; unstated: Ken French for live USD, else omitted",
    "weights_residual: cash|normalize (only way weights not summing to 1 are completed)",
    "expense_ratios {SYMBOL: decimal (0.0003 = 0.03%)} with expense_ratio_source",
    "combine_sic_listings: true merges SYMBOL.MX with its US listing into one exposure; sic_underlyings {\"BRKB.MX\": \"BRK-B\"}",
    *_PRICE_SOURCE,
]
_BENCHMARK = "benchmark (default VTI for USD; no default otherwise, so beta/alpha are omitted)"

_MX_TAX_HOUSEHOLD = {
    "currency": "MXN", "as_of": "2026-09-20", "complete": True, "people": [{"id": "p1"}],
    "accounts": [{"id": "a1", "owner_id": "p1", "tax_unit": "individual", "type": "taxable", "currency": "MXN"}],
    "positions": [{"id": "pos-1", "account_id": "a1", "instrument_id": "NAFTRAC", "symbol": "NAFTRAC",
                   "quantity": 10, "value": 900, "currency": "MXN"}],
    "lots": [{"id": "lot-1", "account_id": "a1", "instrument_id": "NAFTRAC", "quantity": 10, "cost_basis": 1000,
              "acquired_on": "2024-01-01", "currency": "MXN"}],
    "liabilities": [], "external_assets": [], "income_exposures": [], "fx": [], "fund_holdings": [],
}
_US_TAX_HOUSEHOLD = {
    "currency": "USD", "as_of": "2026-04-15", "complete": True, "people": [{"id": "p1"}],
    "accounts": [{"id": "taxable", "owner_id": "p1", "tax_unit": "single", "type": "taxable", "currency": "USD"}],
    "positions": [{"id": "pos-loss", "account_id": "taxable", "instrument_id": "LOSS", "symbol": "LOSS",
                   "quantity": 100, "value": 1000, "currency": "USD"}],
    "lots": [{"id": "lot-loss", "account_id": "taxable", "instrument_id": "LOSS", "quantity": 100,
              "acquired_on": "2024-01-01", "cost_basis": 2000, "currency": "USD"}],
    "liabilities": [], "external_assets": [], "income_exposures": [], "fx": [], "fund_holdings": [],
}
_US_TAX_COMMON = {
    "jurisdiction": "US", "sale_date": "2026-02-28", "as_of": "2026-04-15", "household": _US_TAX_HOUSEHOLD,
    "prices": [{"instrument_id": "LOSS", "price": 10, "currency": "USD", "as_of": "2026-02-28", "source": "broker sale-date quote"}],
    "purchases": [], "substantially_identical_groups": [],
    "wash_sale_coverage": {"accounts_complete": True, "identity_mapping_complete": True, "automatic_reinvestment_reviewed": True,
                           "spouse_accounts_reviewed": True, "controlled_accounts_reviewed": True,
                           "purchases_from": "2026-01-01", "purchases_through": "2026-04-15"},
    "us_tax_facts": {"short_term_gains": 0, "short_term_losses": 0, "long_term_gains": 0, "long_term_losses": 0,
                     "short_term_loss_carryover": 0, "long_term_loss_carryover": 0,
                     "ordinary_income_loss_deduction_available": 3000, "complete": True, "as_of": "2026-02-28"},
    "us_return_facts": {"tax_year": 2026, "complete": True, "ordinary_taxable_income": 150000, "qualified_dividends": 2000,
                        "magi_excluding_capital_gains": 170000, "net_investment_income_excluding_capital_gains": 5000},
    "filing_status": "single",
}

_REBAL_US_HOUSEHOLD: dict[str, Any] = {
    "currency": "USD", "as_of": "2026-09-18", "complete": True, "people": [{"id": "p1"}],
    "accounts": [{"id": "brk", "owner_id": "p1", "type": "taxable", "currency": "USD"},
                 {"id": "ira", "owner_id": "p1", "type": "traditional_ira", "currency": "USD"},
                 {"id": "roth", "owner_id": "p1", "type": "roth_ira", "currency": "USD"},
                 {"id": "bank", "owner_id": "p1", "type": "checking", "currency": "USD", "purpose": "reserve"}],
    "positions": [
        {"id": "brk-vti", "account_id": "brk", "instrument_id": "VTI", "symbol": "VTI", "quantity": 100, "value": 30000, "currency": "USD", "asset_class": "equity"},
        {"id": "brk-cash", "account_id": "brk", "instrument_id": "cash:USD", "symbol": "CASH", "quantity": 500, "value": 500, "currency": "USD", "asset_class": "cash"},
        {"id": "ira-bnd", "account_id": "ira", "instrument_id": "BND", "symbol": "BND", "quantity": 50, "value": 3500, "currency": "USD", "asset_class": "bond"},
        {"id": "ira-vti", "account_id": "ira", "instrument_id": "VTI", "symbol": "VTI", "quantity": 20, "value": 6000, "currency": "USD", "asset_class": "equity"},
        {"id": "roth-vti", "account_id": "roth", "instrument_id": "VTI", "symbol": "VTI", "quantity": 10, "value": 3000, "currency": "USD", "asset_class": "equity"},
        {"id": "bank-cash", "account_id": "bank", "instrument_id": "cash:USD", "symbol": "CASH", "quantity": 20000, "value": 20000, "currency": "USD", "asset_class": "cash"},
    ],
    "lots": [
        {"id": "lot-lt", "account_id": "brk", "instrument_id": "VTI", "quantity": 60, "cost_basis": 12000, "acquired_on": "2021-01-04", "currency": "USD"},
        {"id": "lot-loss", "account_id": "brk", "instrument_id": "VTI", "quantity": 40, "cost_basis": 14000, "acquired_on": "2026-06-01", "currency": "USD"},
    ],
    "liabilities": [], "external_assets": [], "income_exposures": [], "fx": [], "fund_holdings": [],
}
_REBAL_MX_HOUSEHOLD: dict[str, Any] = {
    "currency": "MXN", "as_of": "2026-09-18", "complete": True, "people": [{"id": "p1"}],
    "accounts": [{"id": "gbm", "owner_id": "p1", "type": "taxable", "currency": "MXN"},
                 {"id": "ibkr", "owner_id": "p1", "type": "taxable", "currency": "USD"},
                 {"id": "bank", "owner_id": "p1", "type": "checking", "currency": "MXN"}],
    "positions": [
        {"id": "g-cspx", "account_id": "gbm", "instrument_id": "CSPXN", "symbol": "CSPXN", "quantity": 20, "value": 220000, "currency": "MXN", "asset_class": "equity", "venue": "sic", "issuer_domicile": "IE"},
        {"id": "g-cete", "account_id": "gbm", "instrument_id": "CETETRC", "symbol": "CETETRC", "quantity": 1000, "value": 11000, "currency": "MXN", "asset_class": "fixed_income", "venue": "bmv", "issuer_domicile": "MX"},
        {"id": "g-cash", "account_id": "gbm", "instrument_id": "cash:MXN", "symbol": "CASH", "quantity": 5000, "value": 5000, "currency": "MXN", "asset_class": "cash"},
        {"id": "i-aapl", "account_id": "ibkr", "instrument_id": "AAPL", "symbol": "AAPL", "quantity": 50, "value": 11500, "currency": "USD", "asset_class": "equity", "venue": "us", "issuer_domicile": "US"},
        {"id": "i-xyz", "account_id": "ibkr", "instrument_id": "XYZ", "symbol": "XYZ", "quantity": 100, "value": 5000, "currency": "USD", "asset_class": "equity", "venue": "us", "issuer_domicile": "US"},
        {"id": "b-cash", "account_id": "bank", "instrument_id": "cash:MXN", "symbol": "CASH", "quantity": 80000, "value": 80000, "currency": "MXN", "asset_class": "cash"},
    ],
    "lots": [
        {"id": "l-cspx", "account_id": "gbm", "instrument_id": "CSPXN", "quantity": 20, "cost_basis": 180000, "acquired_on": "2023-03-01", "currency": "MXN"},
        {"id": "l-cete", "account_id": "gbm", "instrument_id": "CETETRC", "quantity": 1000, "cost_basis": 10000, "acquired_on": "2025-03-01", "currency": "MXN"},
        {"id": "l-aapl", "account_id": "ibkr", "instrument_id": "AAPL", "quantity": 50, "cost_basis": 5000, "cost_basis_mxn": 100000, "acquired_on": "2020-01-10", "currency": "USD"},
        {"id": "l-xyz", "account_id": "ibkr", "instrument_id": "XYZ", "quantity": 100, "cost_basis": 2000, "cost_basis_mxn": 40000, "acquired_on": "2021-01-10", "currency": "USD"},
    ],
    "liabilities": [], "external_assets": [], "income_exposures": [],
    "fx": [{"from": "USD", "to": "MXN", "rate": 18.5, "as_of": "2026-09-18", "source": "Banxico FIX (fictional example)"}],
    "fund_holdings": [],
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
        "optional": [
            "targets", "evaluation_date (default today; not before household.as_of)",
            "max_household_age_days (31)", "max_fx_age_days (7)", "max_fund_age_days (90)", "liquidity_horizon_days (30)",
            "household records: ownership {form, owners:[{person_id, share}]} or owner_id",
            "positions: listed, liquid, restricted, lockup_until, redemption {frequency, notice_days, gate}",
            "fx [{from, to, rate, as_of, source, direction_verified}] where 1 from = rate to",
        ],
        "notes": "A stored portfolio.snapshot converts foreign positions only through its own fx list [{from, to, rate, as_of, source}].",
        "example": {"household": _HOUSEHOLD, "targets": {"asset_class": {"equity": 0.6}}, "evaluation_date": "2026-09-20"},
    },
    "analyze": {
        "purpose": "Describe historical return, drawdown, volatility, beta, correlation, and concentration on one common sample.",
        "required": ["household or weights (or stored household/portfolio.snapshot)", "a price source"],
        "optional": [_BENCHMARK, "years", *_MARKET_COMMON],
        "example": {"currency": "USD", "weights": {"SPY": 0.6, "BND": 0.4}, "benchmark": "SPY", "years": 5},
        "variants": {
            "partial_weights_with_fees": {"currency": "USD", "weights": {"VTI": 0.5, "BND": 0.3}, "weights_residual": "cash",
                                          "risk_free": {"annual_rate": 0.04, "source": "3-month T-bill assumption"},
                                          "expense_ratios": {"VTI": 0.0003, "BND": 0.0003},
                                          "expense_ratio_source": "issuer fact sheets 2026-09"},
        },
    },
    "stress": {
        "purpose": "Apply explicit shocks or dated historical windows to supplied holdings; scenarios are arithmetic, not forecasts.",
        "required": ["household or weights", "scenarios: [{name, shocks {SYMBOL: return}} or {name, start, end}]"],
        "optional": _MARKET_COMMON,
        "example": {"currency": "USD", "weights": {"SPY": 0.7, "BND": 0.3}, "scenarios": [{"name": "equity shock", "shocks": {"SPY": -0.2, "BND": 0.02}}]},
    },
    "compare": {
        "purpose": "Compare current and proposed weights on the same price sample and convention.",
        "required": ["currency", "current_weights (or stored household)", "proposed_weights", "a price source"],
        "optional": [_BENCHMARK, "years", *_MARKET_COMMON],
        "example": {"currency": "USD", "current_weights": {"SPY": 0.8, "BND": 0.2}, "proposed_weights": {"SPY": 0.6, "BND": 0.4}, "benchmark": "SPY"},
    },
    "construct": {
        "purpose": "Construct constrained weights beside an equal-weight baseline; advanced methods remain assumption-sensitive.",
        "required": ["household, or tickers plus currency", "a price source"],
        "optional": [
            "method: equal|invvol|minvar|riskparity|hrp|cvar|black_litterman", "max_weight", "confidence",
            "cvar: min_annual_return (in-sample CAGR floor) or min_annual_arithmetic_return (252 x mean daily return)",
            "black_litterman: prior_returns, or market_weights (every symbol) + market_weights_source; views[] {weights, expected_return, confidence}; tau; risk_aversion",
            "covariance: common_window (default, Ledoit-Wolf) | pairwise (inception-aware; no validation)",
            "validation {train_days, test_days, transaction_cost_bps, belief_schedule (black_litterman), static_baseline_weights}",
            "risk_free", "weights_residual", "combine_sic_listings", *_PRICE_SOURCE,
        ],
        "example": {"currency": "USD", "tickers": ["SPY", "BND", "GLD"], "method": "hrp", "max_weight": 0.6, "years": 5},
        "variants": {
            "black_litterman_with_walk_forward": {
                "currency": "USD", "tickers": ["SPY", "BND", "GLD"], "years": 5,
                "method": "black_litterman", "max_weight": 0.6,
                "prior_returns": {"SPY": 0.06, "BND": 0.03, "GLD": 0.03},
                "views": [{"weights": {"SPY": 1, "BND": -1}, "expected_return": 0.03, "confidence": 0.65}],
                "tau": 0.05, "risk_aversion": 3,
                "validation": {"train_days": 252, "test_days": 63, "transaction_cost_bps": 10,
                               "static_baseline_weights": {"SPY": 0.6, "BND": 0.4},
                               "belief_schedule": [{"effective_on": "2020-01-01", "prior_returns": {"SPY": 0.06, "BND": 0.03, "GLD": 0.03}, "views": [{"weights": {"SPY": 1, "BND": -1}, "expected_return": 0.03, "confidence": 0.65}]}]},
            },
            "cvar_return_floor": {"currency": "USD", "tickers": ["SPY", "BND", "GLD"], "years": 5, "method": "cvar",
                                  "max_weight": 0.7, "min_annual_return": 0.01},
            "market_implied_prior": {"currency": "USD", "tickers": ["SPY", "BND", "GLD"], "years": 5, "method": "black_litterman",
                                     "market_weights": {"SPY": 0.6, "BND": 0.35, "GLD": 0.05},
                                     "market_weights_source": "approximate global market caps 2026-09",
                                     "views": [], "tau": 0.05, "risk_aversion": 3},
        },
    },
    "factors": {
        "purpose": "Estimate historical Fama–French factor exposures for non-cash holdings.",
        "required": ["household, or tickers plus currency", "a price source"],
        "optional": ["model: 3|5", "years", "prices", "price_csv", "price_source"],
        "example": {"currency": "USD", "tickers": ["SPY", "IWM"], "model": 3, "years": 5},
    },
    "sic_premium": {
        "purpose": "Compare a SIC listing's MXN price with its home-market price x USDMXN (premium or discount, with each input's date).",
        "required": ["sic_symbol (ends in .MX)", "sic_price_mxn, home_price, usdmxn with price_source, or fetch_missing=true"],
        "optional": ["home_symbol (default: symbol without .MX)", "sic_price_as_of", "home_price_as_of", "usdmxn_as_of",
                     "sic_underlyings", "fetch_missing (live Yahoo)"],
        "example": {"sic_symbol": "AAPL.MX", "home_symbol": "AAPL", "sic_price_mxn": 3960, "sic_price_as_of": "2026-09-18",
                    "home_price": 200, "home_price_as_of": "2026-09-18", "usdmxn": 20, "usdmxn_as_of": "2026-09-18",
                    "price_source": "broker screen"},
    },
    "research": {
        "purpose": "Normalize dated company or fund evidence, calculate source-backed metrics, and compare with a prior case.",
        "required": ["symbol", "sources/evidence or explicit live_fetch=true"],
        "optional": ["as_of", "entity_type: company|fund", "live_fetch", "sources", "business_facts", "fund_facts",
                     "statements [{period_end, period_type: FY|Q|TTM, currency, source_ids, metrics}] (period_type needed for growth, ROE/ROA, leverage)",
                     "fund_holdings", "thesis_evidence", "max_fund_age_days (90)", "max_fx_age_days (7)"],
        "example": {
            "symbol": "ACME", "entity_type": "company", "as_of": "2026-09-20",
            "sources": [{"id": "10k", "title": "FY2025 annual report", "url": "https://example.com/10k", "as_of": "2026-02-20", "kind": "filing"}],
            "business_facts": [{"label": "segments", "value": ["software", "services"], "source_ids": ["10k"]}],
            "statements": [
                {"period_end": "2025-12-31", "period_type": "FY", "currency": "USD", "source_ids": ["10k"],
                 "metrics": {"revenue": 1200, "net_income": 150, "total_equity": 900, "total_assets": 2000}},
                {"period_end": "2024-12-31", "period_type": "FY", "currency": "USD", "source_ids": ["10k"],
                 "metrics": {"revenue": 1100, "net_income": 130, "total_equity": 820, "total_assets": 1900}},
            ],
            "thesis_evidence": {
                "bullish": [{"claim": "Revenue grew about 9% in FY2025.", "source_ids": ["10k"]}],
                "bearish": [{"claim": "Assets grew faster than equity.", "source_ids": ["10k"]}],
                "disconfirming": [{"claim": "A revenue decline in FY2026 would refute the growth case.", "source_ids": ["10k"]}],
            },
        },
    },
    "value": {
        "purpose": "Calculate explicit DCF or multiples scenarios from dated, cited assumptions; never a target-price forecast.",
        "required": ["symbol", "sources", "scenarios (dcf_fcff: forecast [{period, free_cash_flow}], discount_rate, terminal_growth, net_debt, shares_outstanding; multiples: basis, metric_name, metric_value, multiple, shares_outstanding)"],
        "optional": ["as_of",
                     "dcf_fcff: first_period_end + stub_fcf_basis (full_period|remaining_stub), mid_year_convention",
                     "bridge: minority_interest, preferred_equity, lease_liabilities or leases_included_in_net_debt=true, non_operating_assets",
                     "dilution: options [{count, strike}] (treasury stock method), rsus",
                     "current_price with current_price_currency"],
        "example": {
            "symbol": "ACME", "as_of": "2026-09-20",
            "sources": [{"id": "filing", "title": "Annual filing", "url": "https://example.com/filing", "as_of": "2026-09-20", "kind": "filing"}],
            "scenarios": [{"kind": "multiples", "name": "base", "currency": "USD", "valuation_date": "2026-09-20", "basis": "equity_value", "metric_name": "earnings", "metric_value": 100, "multiple": 15, "shares_outstanding": 100, "source_ids": ["filing"]}],
        },
        "variants": {
            "dcf_with_bridge": {
                "symbol": "ACME", "as_of": "2026-09-20",
                "sources": [{"id": "filing", "title": "Annual filing", "url": "https://example.com/filing", "as_of": "2026-09-20", "kind": "filing"}],
                "scenarios": [{"kind": "dcf_fcff", "name": "base", "currency": "USD", "valuation_date": "2026-09-20",
                               "forecast": [{"period": 1, "free_cash_flow": 10}, {"period": 2, "free_cash_flow": 11}],
                               "discount_rate": "0.10", "terminal_growth": "0.02", "net_debt": 20, "mid_year_convention": True,
                               "minority_interest": 2, "leases_included_in_net_debt": True, "non_operating_assets": 5,
                               "shares_outstanding": 10, "options": [{"count": 1, "strike": 5}], "rsus": 0.5,
                               "current_price": 9, "current_price_currency": "USD", "source_ids": ["filing"]}],
            },
        },
    },
    "plan": {
        "purpose": "Reserve capital for essentials, selected debt payments, and protected dated goals without double-counting outside funds.",
        "required": ["plan.resources and goals, unless both are stored for the client"],
        "derived": "With client_id and no stored plan.resources object, inputs come from the saved picture: liquid cash and investments, spending.monthly, reserve.target_months and dated goals.",
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
        "scope": "With client_id and no calendar-shaped income.schedule, the 12 months are derived from income.<id>, spending.monthly and debt payments.",
        "example": {"income.schedule": {"currency": "USD", "monthly_need": 3000, "months": [{"month": "2026-10", "expected_cash_received": 2500, "committed_outflow": 300}]}},
    },
    "debt_payoff": {
        "purpose": "Payoff dates and interest for a monthly debt budget: avalanche (highest rate first) against a chosen order.",
        "required": ["monthly_amount: the whole monthly budget for these debts, minimums included"],
        "optional": ["liabilities [{id, balance, annual_rate (decimal), monthly_payment (minimum), currency}]; default: the client's stored liability.<id> facts",
                     "order: debt ids in the order extra money goes (default smallest balance first)", "currency", "as_of"],
        "scope": "Fixed rates, monthly accrual, no new borrowing. A debt without rate or payment is listed as missing, never guessed.",
        "example": {"monthly_amount": 12000, "as_of": "2026-09-21", "liabilities": [
            {"id": "car", "balance": 60000, "annual_rate": 0.13, "monthly_payment": 3500, "currency": "MXN"},
            {"id": "card", "balance": 18000, "annual_rate": 0.42, "monthly_payment": 1200, "currency": "MXN"}]},
    },
    "project": {
        "purpose": "Project dated contributions and withdrawals under explicit returns, fees, tax drag, and inflation.",
        "required": ["currency", "as_of", "end_date", "initial_wealth", "simulations", "seed", "return_model", "annual_inflation", "annual_fee", "annual_tax_drag", "cashflows", "recurring_cashflows"],
        "optional": [
            "return_model.type: parametric {annual_return, annual_volatility} | student_t (+ degrees_of_freedom > 2) | bootstrap {historical_annual_returns} | block_bootstrap (+ block_length)",
            "return_model.basis: nominal|real (unstated = nominal with a warning; real is compounded with annual_inflation once)",
            "annual_tax_drag: percentage points charged only in positive-return years, capped at the gain",
            "cashflows[]: id,date,amount,type,inflation_indexed,hard_goal", "recurring_cashflows[]: id,start,end,amount,type,frequency,inflation_indexed,hard_goal",
        ],
        "example": {"currency": "USD", "as_of": "2026-09-20", "end_date": "2028-09-20", "initial_wealth": 100000, "simulations": 1000, "seed": 7, "return_model": {"type": "parametric", "basis": "nominal", "annual_return": 0.05, "annual_volatility": 0.12}, "annual_inflation": 0.03, "annual_fee": 0.001, "annual_tax_drag": 0.01, "cashflows": [{"id": "home", "date": "2027-09-20", "amount": 30000, "type": "withdrawal", "inflation_indexed": False, "hard_goal": True}], "recurring_cashflows": [{"id": "savings", "start": "2026-10-20", "end": "2028-08-20", "amount": 1000, "type": "contribution", "frequency": "monthly", "inflation_indexed": False, "hard_goal": False}]},
        "variants": {"block_bootstrap_real": {"currency": "USD", "as_of": "2026-09-20", "end_date": "2031-09-20", "initial_wealth": 100000, "simulations": 500, "seed": 7, "return_model": {"type": "block_bootstrap", "basis": "real", "block_length": 2, "historical_annual_returns": [-0.2, 0.1, 0.15, 0.04, 0.08]}, "annual_inflation": 0.03, "annual_fee": 0.001, "annual_tax_drag": 0, "cashflows": [], "recurring_cashflows": []}},
    },
    "income": {
        "purpose": "Compare dividend-cash and total-return withdrawals without double-counting yield, including sequence risk.",
        "required": ["currency", "initial_wealth", "years", "annual_income_need", "annual_need_growth", "annual_dividend_yield", "annual_fee", "annual_tax_drag", "simulations", "seed", "return_model"],
        "optional": ["spending_timing: start|end (default start)", "annual_inflation (required for a real basis; adds real outcomes)",
                     "taxes {dividend_tax_rate, capital_gains_tax_rate, initial_cost_basis, total_return_dividend_yield} (requires annual_tax_drag 0; without taxes both strategies are identical)",
                     "return_model as in project"],
        "example": {"currency": "USD", "initial_wealth": 1000000, "years": 20, "annual_income_need": 50000, "annual_need_growth": 0.03, "annual_dividend_yield": 0.025, "annual_fee": 0.001, "annual_tax_drag": 0, "simulations": 1000, "seed": 7, "spending_timing": "start", "annual_inflation": 0.03, "return_model": {"type": "bootstrap", "basis": "nominal", "historical_annual_returns": [-0.2, 0.1, 0.15, 0.04]}, "taxes": {"dividend_tax_rate": 0.15, "capital_gains_tax_rate": 0.15, "initial_cost_basis": 600000, "total_return_dividend_yield": 0.01}},
    },
    "ladder": {
        "purpose": "Match reserves and dated asset cash flows to dated liabilities, reporting gaps without inventing instruments.",
        "required": ["currency", "as_of", "reserve", "liabilities", "assets"],
        "optional": ["modeled_fx", "arrears_policy: carry_forward|drop_after_miss (default carry_forward)"],
        "example": {"currency": "USD", "as_of": "2026-09-20", "reserve": {"currency": "USD", "amount": 20000}, "liabilities": [{"id": "tuition", "date": "2027-06-01", "amount": 30000, "currency": "USD"}], "assets": [{"id": "bond", "currency": "USD", "cashflows": [{"date": "2027-05-01", "amount": 15000}]}]},
    },
    "tax": {
        "purpose": "Screen explicit taxable-security scenarios: US federal (brackets, LTCG stacking, NIIT, wash sales, lots) or Mexico Article 129.",
        "required": ["jurisdiction: US|MX_ARTICLE_129", "household with accounts and lots", "jurisdiction-specific sale and coverage inputs"],
        "optional": [
            "US mode: harvest (default) | rebalance (sales) | lot_selection (target {instrument_id, proceeds|quantity, account_id?}, methods fifo|lifo|hifo|specific_id|tax_min, specific_lots) | harvest_report",
            "US: sale_date, as_of, prices, purchases[] {account_id, instrument_id, trade_date, quantity, lot_id?, tax_treatment?, related_party?}, substantially_identical_groups, wash_sale_coverage, wash_sale_excluded_account_ids, us_tax_facts, filing_status",
            "US tax: us_return_facts {tax_year, complete, ordinary_taxable_income, qualified_dividends, magi_excluding_capital_gains, net_investment_income_excluding_capital_gains} for the 2025/2026 bracket engine; rates {ordinary, long_term, state?, niit?} is a flat marginal override",
            "lots[].holding_period_start (tacked holding period)",
            "MX: article_129_sales [{lot_id, quantity, sale_date, proceeds_mxn, article_129_adjusted_basis_mxn, basis_source, proceeds_source, article_129_eligibility}], article_129_realized_gain_or_loss_mxn, article_129_loss_carryforwards",
        ],
        "notes": "Result rows report wash_sale {disallowed_loss, replacements}. Not state tax or a filing position.",
        "example": {
            "jurisdiction": "MX_ARTICLE_129", "as_of": "2026-09-20", "sale_date": "2026-09-18",
            "household": _MX_TAX_HOUSEHOLD,
            "article_129_sales": [{"lot_id": "lot-1", "quantity": 10, "proceeds_mxn": 900, "article_129_adjusted_basis_mxn": 1000, "basis_source": "broker calculation", "proceeds_source": "scenario quote", "article_129_eligibility": {"individual_taxpayer": True, "eligible_security": True, "eligible_venue": True, "eligible_acquisition": True, "no_exclusion_applies": True, "source": "broker and tax review", "security_scope": "NAFTRAC", "venue": "BMV", "acquisition_scope": "lot-1"}}],
            "article_129_realized_gain_or_loss_mxn": 0,
            "article_129_loss_carryforwards": [],
        },
        "variants": {
            "US": {**_US_TAX_COMMON, "mode": "rebalance", "sales": [{"lot_id": "lot-loss", "quantity": 100}]},
            "US_lot_selection": {**_US_TAX_COMMON, "mode": "lot_selection", "target": {"instrument_id": "LOSS", "quantity": 50},
                                 "methods": ["fifo", "hifo", "tax_min"]},
            "US_marginal_override": {**{k: v for k, v in _US_TAX_COMMON.items() if k != "us_return_facts"}, "mode": "harvest",
                                     "rates": {"ordinary": 0.37, "long_term": 0.15}},
        },
    },
    "mx_holdings": {
        "purpose": "Classify Mexico-resident holdings by tax regime and liquidity (CETES, UDIBONOS, FIBRAs, SIC, AFORE, PPR, funds).",
        "required": ["holdings [{id, type, value_mxn | value+currency+fx_to_mxn+fx_source | udis+udi_value+udi_date+udi_source}] or a household whose positions carry mx_instrument_type"],
        "optional": ["us_person (adds PFIC and estate notes)"],
        "example": {"us_person": False, "holdings": [
            {"id": "c1", "type": "cetes", "value_mxn": 100000},
            {"id": "a1", "type": "afore_rcv", "value_mxn": 300000},
            {"id": "s1", "type": "sic_foreign_listing", "value": 1000, "currency": "USD", "fx_to_mxn": "18.5", "fx_source": "Banxico FIX 2026-09-18"},
        ]},
    },
    "mx_interest": {
        "purpose": "Real vs nominal interest (LISR Arts. 133-134), retention credit and annual ISR effect.",
        "required": ["tax_year", "accounts [{id, nominal_interest_mxn, average_daily_balance_mxn, days, inflation_factor | inpc_first_month+inpc_last_month, retention_withheld_mxn}]", "taxable_income_before_mxn or marginal_rate"],
        "optional": ["udi_adjustment_mxn", "constancia_real_interest_mxn (reconciliation)", "parameters {key: {value, source}} for unverified years"],
        "example": {"tax_year": 2026, "taxable_income_before_mxn": 500000, "accounts": [
            {"id": "cetes", "institution": "Cetesdirecto", "nominal_interest_mxn": 10000, "average_daily_balance_mxn": 100000, "days": 365,
             "inpc_first_month": "100", "inpc_last_month": "104", "retention_withheld_mxn": 900, "constancia_real_interest_mxn": 6000}]},
    },
    "mx_deductions": {
        "purpose": "Art. 151 personal deductions: global cap, PPR/Art. 185 room, and the ISR saving of an extra contribution.",
        "required": ["tax_year", "total_income_mxn", "accumulable_income_mxn", "deductions {general_mxn, retirement_151v_mxn, art185_mxn}", "proposed_ppr_contribution_mxn and/or proposed_art185_mxn", "taxable_income_before_mxn or marginal_rate"],
        "optional": ["deductions.outside_global_cap_mxn", "parameters {key: {value, source}}"],
        "example": {"tax_year": 2026, "total_income_mxn": 1000000, "accumulable_income_mxn": 900000,
                    "deductions": {"general_mxn": 200000, "retirement_151v_mxn": 50000, "art185_mxn": 0},
                    "proposed_ppr_contribution_mxn": 100000, "taxable_income_before_mxn": 700000},
    },
    "mx_foreign": {
        "purpose": "Foreign securities at a foreign broker (e.g. IBKR, GBM Trading USA): SIC-listed securities keep the 10% Art. 129 rate (criterio 37/ISR/N); others are progressive income. MXN gains including FX, foreign dividends and credit.",
        "required": ["tax_year", "sales [{id, currency, proceeds, fx_sale, cost, fx_acquisition, acquired_on, sold_on, sic_listed}] and/or dividends [{id, currency, gross, withheld, fx, paid_on, source_country}]", "taxable_income_before_mxn or marginal_rate"],
        "optional": ["sales[].security_type (share | equity_etf | other_etf)", "sales[].cost_update_factor (INPC)", "dividends[].w8ben_on_file"],
        "example": {"tax_year": 2026, "taxable_income_before_mxn": 700000,
                    "sales": [{"id": "aapl", "currency": "USD", "proceeds": 12000, "fx_sale": "18", "cost": 10000, "fx_acquisition": "20", "acquired_on": "2020-01-10", "sold_on": "2026-05-01", "sic_listed": True}],
                    "dividends": [{"id": "d1", "currency": "USD", "gross": 1000, "withheld": 100, "fx": "18", "paid_on": "2026-03-31", "source_country": "US", "w8ben_on_file": True}]},
    },
    "mx_calendar": {
        "purpose": "Dated Mexico (and optional US-person) tax deadlines for a year, ready to use as monitor rules.",
        "required": ["tax_year"],
        "optional": ["as_of (adds days_until)", "business_or_professional_income", "rental_income", "foreign_dividends", "us_person"],
        "example": {"tax_year": 2026, "as_of": "2026-09-21", "business_or_professional_income": False, "rental_income": False,
                    "foreign_dividends": True, "us_person": False},
    },
    "estate": {
        "purpose": "US estate-tax exposure for a Mexico resident: US-situs assets (including via the SIC), credit, and a tax range.",
        "required": ["year", "decedent {us_citizen, green_card, us_domiciled}", "assets [{id, type, value_usd, custody?: sic|mx_broker|us_broker|foreign_broker}]"],
        "optional": ["estimated_deductions_usd", "US persons: adjusted_taxable_gifts_usd (required), marital_deduction_usd"],
        "example": {"year": 2026, "decedent": {"us_citizen": False, "green_card": False, "us_domiciled": False},
                    "assets": [{"id": "voo", "type": "us_domiciled_fund", "value_usd": 300000, "custody": "us_broker"},
                               {"id": "aapl-sic", "type": "us_stock", "value_usd": 200000, "custody": "sic"},
                               {"id": "cspx", "type": "non_us_domiciled_fund", "value_usd": 400000}],
                    "estimated_deductions_usd": 0},
    },
    "policy_draft": {
        "purpose": "Draft the person's Investment Policy Statement from the saved picture: objectives and required "
                   "return per goal, ability vs willingness to take risk, reserve and goal buckets, a strategic "
                   "allocation with ranges, constraints, rebalancing bands and review cadence; every number carries "
                   "its rule. propose=true records it as a decision; the person's accept stores it as policy.ips.",
        "required": ["client_id (or facts [{key, value}] to draft without a profile)"],
        "optional": ["propose (true: record the draft as a decision for the person to accept or dismiss)",
                     "overrides {profile (only stricter), concentration_limit, reserve_months, "
                     "review_cadence: annual|semiannual|quarterly} for an amendment", "as_of"],
        "example": {"as_of": "2026-09-21", "facts": [
            {"key": "client.profile", "value": {"residence": {"country": "MX"}, "birth_year": 1990, "us_person": False}},
            {"key": "income.salary", "value": {"amount": 60000, "currency": "MXN", "frequency": "monthly", "kind": "salary"}},
            {"key": "spending.monthly", "value": {"essential": 25000, "total": 35000, "currency": "MXN"}},
            {"key": "cash.nu", "value": {"amount": 180000, "currency": "MXN", "purpose": "reserve"}},
            {"key": "goals", "value": [{"id": "retiro", "name": "Retiro", "target_amount": 8000000, "currency": "MXN",
                                        "target_date": "2055-01-01", "monthly_contribution": 6000}]},
            {"key": "preference.risk", "value": {"drop_reaction": "hold", "experience": "some"}},
            {"key": "onboarding", "value": {"steps": {"debts": "done"}, "started_at": "2026-09-01T00:00:00Z"}},
        ]},
    },
    "policy_check": {
        "purpose": "Check a proposed trade or target allocation against the accepted IPS: allocation bands, "
                   "single-holding concentration, reserve untouched, near-goal buckets, leverage and exclusions, "
                   "and (Mexico residents) the preference for non-US-situs funds. pass/warn/violation per rule.",
        "required": ["proposal {kind: trade, action: buy|sell, symbol, amount, funding?: reserve|surplus|sale|"
                     "cash:<id>|goal:<id>, asset_class?, sleeve?, domicile?, instrument?, leverage?, tags?} or "
                     "{kind: allocation, target: {sleeve id: share}}", "client_id with an accepted policy.ips (or ips)"],
        "optional": ["portfolio {currency, positions: [{symbol, value, asset_class?, sleeve?}]} for band and "
                     "concentration effects of a trade", "as_of"],
        "example": {
            "proposal": {"kind": "trade", "action": "buy", "symbol": "VOO", "amount": 20000, "currency": "MXN",
                         "funding": "surplus", "tags": []},
            "portfolio": {"currency": "MXN", "positions": [{"symbol": "CSPX", "value": 300000},
                                                           {"symbol": "CETES", "value": 175000},
                                                           {"symbol": "CASH", "value": 25000, "asset_class": "cash"}]},
            "ips": {"currency": "MXN", "allocation": {"model": "balanced", "sleeves": [
                {"id": "global_equity", "name": "Global equity", "asset": "equity", "target": 0.6, "min": 0.55, "max": 0.65},
                {"id": "mx_fixed_income", "name": "Mexican government fixed income", "asset": "fixed_income",
                 "target": 0.35, "min": 0.3, "max": 0.4},
                {"id": "cash", "name": "Cash (MXN)", "asset": "cash", "target": 0.05, "min": 0.0375, "max": 0.0625}]},
                "constraints": {"concentration": {"limit": 0.1}, "leverage": {"allowed": False},
                                "estate_situs": {"prefer": "non_us_domiciled"}},
                "liquidity": {"reserve": {"sources": ["nu"]}}},
        },
    },
    "ledger": {
        "purpose": "Read the transaction ledger: holdings and lots, household document, statement reconciliation, realized gains, investment income, transfers, exposure groups.",
        "required": ["client_id with a posted ledger (wealth_ingest confirm)"],
        "optional": ["view: holdings (default)|household|reconcile|realized|income|transfers|groups", "as_of (default today)",
                     "household/groups: currency, prices {instrument_id: [{date, price}]}", "household: default_owner_id, complete",
                     "realized/income: year", "groups: by (underlying), account_ids", "lot_method: fifo", "include_inferred", "max_fx_age_days (5)"],
        "example": {"view": "holdings", "as_of": "2026-08-31", "ledger": _LEDGER},
        "variants": {"household": {"view": "household", "as_of": "2026-08-31", "currency": "USD",
                                   "prices": {"VTI": [{"date": "2026-08-31", "price": 260}]}, "ledger": _LEDGER}},
    },
    "performance": {
        "purpose": "Time-weighted and money-weighted (XIRR) return from the ledger, net of external flows, optionally against a benchmark.",
        "required": ["client_id with a posted ledger", "start", "currency", "prices {instrument_id: [{date, price}]}"],
        "optional": ["end (default today)", "method: linked (default)|modified_dietz", "account_ids", "by_account",
                     "benchmark {name, series: [{date, value}]}", "include_inferred", "max_price_age_days (5)", "max_fx_age_days (5)"],
        "example": {"start": "2026-07-01", "end": "2026-08-31", "currency": "USD", "ledger": _LEDGER,
                    "prices": {"VTI": [{"date": "2026-07-21", "price": 250}, {"date": "2026-08-31", "price": 260}]}},
    },
    "spending": {
        "purpose": "Spending, income, recurring charges and investable surplus from the ledger with explainable categories.",
        "required": ["client_id with a posted ledger", "start, end, currency (monthly|income|surplus)"],
        "optional": ["view: monthly (default)|income|recurring|surplus|categories", "surplus: plan.resources, reserve_balance, reserve_account_ids, reserve_fill_months (12)",
                     "include_inferred", "max_fx_age_days (5)"],
        "example": {"view": "monthly", "start": "2026-07-01", "end": "2026-07-31", "currency": "USD", "ledger": _LEDGER},
    },
    "dca": {
        "purpose": "Dollar-cost-averaging plans: due dates, adherence to actual ledger buys, historical backtest, or a sizing range.",
        "required": ["plan {id, currency, cadence: weekly|biweekly|monthly|quarterly, start_date, account_id, legs [{instrument_id, amount}]} or plan_id of stored planning.dca"],
        "optional": ["view: adherence (default; needs as_of and a ledger)|schedule (through)|backtest (prices, start, end, fee_bps, cash_rate)|suggest (monthly_surplus, currency, constraints, target_weights)"],
        "example": {"view": "schedule", "through": "2026-12-31",
                    "plan": {"id": "core", "currency": "USD", "cadence": "monthly", "start_date": "2026-07-21", "account_id": "brk",
                             "source_account_id": "bank", "legs": [{"instrument_id": "VTI", "amount": 1000}]}},
        "variants": {"adherence": {"view": "adherence", "as_of": "2026-08-31", "ledger": _LEDGER,
                                   "plan": {"id": "core", "currency": "USD", "cadence": "monthly", "start_date": "2026-07-21", "account_id": "brk",
                                            "legs": [{"instrument_id": "VTI", "amount": 1000}]}}},
    },
    "rebalance": {
        "purpose": "Tax-aware trade list from the current allocation to sleeve targets: cash and contributions first, then sells chosen by tax cost (US lots and wash sales; Mexico SIC 10% vs progressive, commission + IVA), whole shares, reserve untouched, plus a no-sell alternative.",
        "required": ["household (or ledger + prices {instrument_id: [{date, price}]} + currency + as_of; a client's stored ledger works with prices)",
                     "targets {by: asset_class|underlying|instrument, sleeves: [{name, weight, min?, max?, band?, kind?, buy: [instrument_id]}], map? {instrument_id: sleeve}, band? (default 0.05)}",
                     "jurisdiction_context {jurisdiction: US|MX, trade_date?, currency?}"],
        "optional": [
            "jurisdiction_context.accounts {id: {platform: gbm_trading_mx|gbm_trading_usa|ibkr|mx_broker|foreign_broker|us_broker, commission_rate, vat_rate, fractional, min_trade_amount, purpose: reserve|goal:<id>}} (commission is unknown, not zero, unless the platform publishes it)",
            "jurisdiction_context.instruments {id: {price, currency, price_as_of?, sleeve?, venue: sic|bmv|biva|us|other, sic_listed, security_type: share|equity_etf|other_etf, issuer_domicile}} (buy candidates need a price)",
            "cash_flows [{id, account_id, amount, currency?, date?}] (contributions; deployed before any sale)",
            "tax_inputs US: rates {ordinary, long_term, state?, niit?} or filing_status + us_tax_facts + us_return_facts (bracket engine), purchases [{account_id, instrument_id, trade_date, quantity}], substantially_identical_groups",
            "tax_inputs MX: marginal_rate or taxable_income_before_mxn (progressive gains); lots[].cost_basis_mxn for non-MXN lots",
            "constraints {mode: full|flows_only, min_trade_amount, harvest_losses_first (true), avoid_progressive_gains (true), sell_to: target|band_edge, protected_account_ids, protected_position_ids, no_sell_instrument_ids, deployable_cash_account_ids}",
        ],
        "notes": "Result: trades [{account_id, instrument_id, side, quantity, estimated_amount, estimated_tax, estimated_cost, lots?, tax_regime?, reason}], summary {before, after, total_estimated_tax, total_estimated_cost, residual_drift}, alternatives.no_sell, blocked, skipped, protected, outside_plan, tax_engine_check. Bank cash and reserve/goal money are never deployed. Never an order.",
        "example": {
            "household": _REBAL_US_HOUSEHOLD,
            "targets": {"by": "asset_class", "sleeves": [{"name": "equity", "weight": 0.6, "buy": ["VTI"]},
                                                         {"name": "bond", "weight": 0.4, "buy": ["BND"]}]},
            "jurisdiction_context": {"jurisdiction": "US", "trade_date": "2026-09-18",
                                     "accounts": {"brk": {"commission_rate": 0}, "ira": {"commission_rate": 0}, "roth": {"commission_rate": 0}}},
            "cash_flows": [{"id": "sept", "account_id": "brk", "amount": 1000}],
            "tax_inputs": {"rates": {"ordinary": 0.32, "long_term": 0.15},
                           "purchases": [{"account_id": "roth", "instrument_id": "VTI", "trade_date": "2026-09-01", "quantity": 1}]},
        },
        "variants": {
            "mexico_gbm_sic_and_ibkr": {
                "household": _REBAL_MX_HOUSEHOLD,
                "targets": {"by": "asset_class", "map": {"CETETRC": "mx_fixed_income"},
                            "sleeves": [{"name": "equity", "weight": 0.7, "buy": ["CSPXN"]},
                                        {"name": "mx_fixed_income", "weight": 0.3, "buy": ["CETETRC"]}]},
                "jurisdiction_context": {"jurisdiction": "MX", "trade_date": "2026-09-18",
                                         "accounts": {"gbm": {"platform": "gbm_trading_mx"},
                                                      "ibkr": {"platform": "ibkr", "commission_rate": "0.0005"},
                                                      "bank": {"purpose": "reserve"}},
                                         "instruments": {"AAPL": {"sic_listed": True}, "XYZ": {"sic_listed": False}}},
                "cash_flows": [{"id": "sept", "account_id": "gbm", "amount": 20000}],
                "tax_inputs": {"marginal_rate": 0.30},
            },
            "flows_only": {
                "household": _REBAL_US_HOUSEHOLD,
                "targets": {"by": "asset_class", "sleeves": [{"name": "equity", "weight": 0.6, "buy": ["VTI"]},
                                                             {"name": "bond", "weight": 0.4, "buy": ["BND"]}]},
                "jurisdiction_context": {"jurisdiction": "US", "trade_date": "2026-09-18",
                                         "accounts": {"brk": {"commission_rate": 0}, "ira": {"commission_rate": 0}, "roth": {"commission_rate": 0}}},
                "cash_flows": [{"id": "sept", "account_id": "brk", "amount": 5000}],
                "constraints": {"mode": "flows_only", "min_trade_amount": 100},
            },
            "from_ledger": {
                "ledger": _LEDGER, "as_of": "2026-08-31", "currency": "USD",
                "prices": {"VTI": [{"date": "2026-08-31", "price": 260}]},
                "targets": {"by": "asset_class", "map": {"VTI": "equity", "BND": "bond"},
                            "sleeves": [{"name": "equity", "weight": 0.8, "buy": ["VTI"]},
                                        {"name": "bond", "weight": 0.2, "buy": ["BND"]}]},
                "jurisdiction_context": {"jurisdiction": "US", "trade_date": "2026-08-31",
                                         "accounts": {"brk": {"commission_rate": 0, "fractional": True}},
                                         "instruments": {"BND": {"price": 72, "currency": "USD", "price_as_of": "2026-08-31"}}},
                "tax_inputs": {"rates": {"ordinary": 0.24, "long_term": 0.15}},
            },
        },
    },
    "asset_location": {
        "purpose": "Where each sleeve should live: current placement vs suggested (US account types; Mexico SIC via a Mexican broker vs foreign broker, Irish UCITS vs US-situs, local fixed income, PPR pointer) with the annual tax-drag difference as a range. Never forces trades.",
        "required": ["jurisdiction: US|MX", "accounts [{id, type, platform?, value?}]",
                     "sleeves [{name, kind?, holdings: [{account_id, value, instrument_id?, sic_listed?, issuer_domicile?|us_situs?, distributing?}]}]"],
        "optional": ["currency (default USD for US, MXN for MX)", "sleeves[].kind: bond|reit|cash|broad_equity|intl_equity|high_growth|mx_fixed_income|mx_equity|other (default from the name)",
                     "sleeves[].yield, sleeves[].expected_return",
                     "assumptions US {ordinary_rate: [low, high], qualified_rate: [low, high]}; MX {marginal_rate, equity_yield, price_return: [low, high], ucits_us_dividend_withholding}"],
        "notes": "Result: sleeves with current vs suggested placement and reasons, estimated_annual_tax_drag {current, suggested, difference} as low/high ranges, related_tasks (mx_deductions for PPR, estate for US-situs). forces_trades is always false.",
        "example": {"jurisdiction": "US", "currency": "USD",
                    "accounts": [{"id": "brk", "type": "taxable"}, {"id": "ira", "type": "traditional_ira"}, {"id": "roth", "type": "roth_ira"}],
                    "sleeves": [{"name": "us_equity", "holdings": [{"account_id": "ira", "value": 50000}, {"account_id": "brk", "value": 20000}]},
                                {"name": "bonds", "holdings": [{"account_id": "brk", "value": 40000}]},
                                {"name": "small_cap_growth", "kind": "high_growth",
                                 "holdings": [{"account_id": "brk", "value": 10000}, {"account_id": "roth", "value": 10000}]}]},
        "variants": {
            "mexico": {"jurisdiction": "MX", "currency": "MXN",
                       "accounts": [{"id": "gbm", "type": "taxable", "platform": "gbm_trading_mx"},
                                    {"id": "ibkr", "type": "taxable", "platform": "ibkr"}, {"id": "ppr", "type": "ppr"}],
                       "sleeves": [{"name": "us_equity", "holdings": [
                                       {"account_id": "ibkr", "instrument_id": "VOO", "value": 500000, "sic_listed": True, "issuer_domicile": "US", "distributing": True},
                                       {"account_id": "gbm", "instrument_id": "CSPXN", "value": 300000, "sic_listed": True, "issuer_domicile": "IE", "distributing": False}]},
                                   {"name": "cetes", "kind": "mx_fixed_income", "holdings": [{"account_id": "gbm", "value": 200000}]}]},
        },
    },
    "retirement_mx": {
        "purpose": "Mexico retirement in real MXN: IMSS regime (Ley 73 if first cotizacion before 1997-07-01), Ley 73 pension "
                   "(Art. 167 table, cesantia 60-65, 1.11 decree factor, minimum pension) with Modalidad 40 cost, payback and IRR, "
                   "or the Ley 97 AFORE projection (2020-reform contribution schedule, fees, weeks by year, programmed withdrawal, "
                   "pension garantizada), voluntary contributions, and the gap to target spending.",
        "required": ["first_cotizacion_date (or regime: ley73|ley97)", "birth_year (or age, or stored client.profile)", "weeks_cotizadas",
                     "ley73 {average_daily_salary_mxn | salary_history [{weeks, daily_salary_mxn}] most recent first, dependants {spouse, children_under_16, dependent_parents}}",
                     "or ley97 {sbc_daily_mxn, afore_balance_mxn, average_career_sbc_daily_mxn}", "target_monthly_spending_mxn"],
        "optional": ["as_of", "retirement_age (60-65; default 65)", "longevity_ages", "other_monthly_income_mxn",
                     "ley73.modalidad40 {daily_salary_mxn | salary_uma_multiple, years, start_year, longevity_ages}",
                     "ley97 {real_return [low, base, high], fee, salary_real_growth, weeks_per_year, voluntary_monthly_mxn, cuota_social_daily_mxn, annuity_real_rate}",
                     "voluntary {short_term_monthly_mxn, long_term_monthly_mxn, accumulable_income_mxn}",
                     "parameters {key: {value, source}} (e.g. pension_garantizada_inpc_factor, cuota_social_daily_mxn)"],
        "notes": "Unverified parameters (INPC update of the pension garantizada, cuota social, other years' salario minimo) are never "
                 "used; the affected piece is omitted and listed under missing. result.parameters_used carries each source and status.",
        "example": {"as_of": "2026-09-21", "birth_year": 1966, "first_cotizacion_date": "1990-03-01", "weeks_cotizadas": 1300,
                    "retirement_age": 65, "target_monthly_spending_mxn": 40000,
                    "ley73": {"average_daily_salary_mxn": 600, "dependants": {"spouse": True, "children_under_16": 0},
                              "modalidad40": {"salary_uma_multiple": 10, "years": 5}}},
        "variants": {
            "ley97_with_voluntary": {
                "as_of": "2026-09-21", "birth_year": 1990, "first_cotizacion_date": "2012-01-01", "weeks_cotizadas": 600,
                "retirement_age": 65, "target_monthly_spending_mxn": 30000,
                "ley97": {"sbc_daily_mxn": 1000, "afore_balance_mxn": 400000, "average_career_sbc_daily_mxn": 700,
                          "real_return": [0.02, 0.035, 0.05]},
                "voluntary": {"short_term_monthly_mxn": 1000, "long_term_monthly_mxn": 2000, "accumulable_income_mxn": 400000},
                "parameters": {"pension_garantizada_inpc_factor": {"value": 1.33, "source": "fictional example ratio; use INEGI INPC"}}},
        },
    },
    "retirement_us": {
        "purpose": "US retirement in today's dollars: Social Security claim ages 62-70 from a supplied PIA with breakevens and a "
                   "spousal note, 2026 contribution limits (401(k), catch-ups, Roth catch-up wage rule, IRA, HSA), RMD age and "
                   "amount, and withdrawal ordering (taxable-first vs proportional vs bracket-filling Roth conversions) on the "
                   "federal bracket engine.",
        "required": ["at least one of social_security {pia_monthly_usd, birth_year}, contributions {tax_year, birth_year, "
                     "prior_year_fica_wages_usd, hdhp_coverage: none|self|family}, rmd {birth_year, age, prior_year_end_balance_usd}, "
                     "withdrawals {balances {taxable, taxable_basis, tax_deferred, roth}, annual_spending_usd, start_age, years, "
                     "filing_status, real_return, birth_year}"],
        "optional": ["birth_year (top level, shared by sections; or stored client.profile)",
                     "social_security {real_discount_rate, longevity_ages, spouse {birth_year, own_pia_monthly_usd}}",
                     "withdrawals {tax_year (bracket table, default 2026), other_ordinary_income_usd, deduction_usd, "
                     "conversion_target_rate (default 0.12), terminal_rates {tax_deferred, taxable_gain}}",
                     "parameters {key: {value, source}}"],
        "example": {"birth_year": 1964,
                    "social_security": {"pia_monthly_usd": 2400, "spouse": {"birth_year": 1966, "own_pia_monthly_usd": 600}},
                    "contributions": {"tax_year": 2026, "prior_year_fica_wages_usd": 180000, "hdhp_coverage": "family"},
                    "rmd": {"age": 62, "prior_year_end_balance_usd": 500000},
                    "withdrawals": {"balances": {"taxable": 400000, "taxable_basis": 250000, "tax_deferred": 800000, "roth": 150000},
                                    "annual_spending_usd": 70000, "other_ordinary_income_usd": 0, "start_age": 62, "years": 30,
                                    "filing_status": "married_filing_jointly", "real_return": 0.03}},
    },
    "retirement_readiness": {
        "purpose": "Retirement readiness in any currency: required nest egg over a safe-withdrawal range, projected savings over "
                   "a return range, the gap with the extra monthly contribution to close it, and the Monte Carlo probability "
                   "from the planning income simulator.",
        "required": ["currency", "current_age (or birth_year / stored client.profile)", "retirement_age", "plan_to_age",
                     "target_annual_spending (real)", "guaranteed_annual_income (pension, Social Security; 0 if none)",
                     "current_savings", "annual_contribution", "real_return (number or [low, base, high])"],
        "optional": ["withdrawal_rates (default [0.03, 0.035, 0.04])",
                     "return_model (planning schema, e.g. {type: parametric, basis: real, annual_return, annual_volatility}) + annual_inflation",
                     "simulations (2000)", "seed (7)", "annual_fee"],
        "example": {"currency": "MXN", "current_age": 45, "retirement_age": 65, "plan_to_age": 95, "target_annual_spending": 480000,
                    "guaranteed_annual_income": 190000, "current_savings": 1500000, "annual_contribution": 120000,
                    "real_return": [0.02, 0.035, 0.05],
                    "return_model": {"type": "parametric", "basis": "real", "annual_return": 0.035, "annual_volatility": 0.1},
                    "annual_inflation": 0.04, "simulations": 500, "seed": 7},
    },
    "monitor": {
        "purpose": "Evaluate opt-in review, expiry, drift, goal, threshold, or thesis rules and return only state changes to the caller.",
        "required": ["client_id on wealth_run", "rules or stored monitor.rules"],
        "optional": ["rules[].enabled", "acknowledge: [rule ids] (accept a thesis change as the new baseline)",
                     "timezone (IANA; default client.profile.timezone, else local)"],
        "example": {"rules": [{"id": "goal-freshness", "kind": "expiry", "keys": ["goals", "plan.resources"]}],
                    "timezone": "America/Mexico_City"},
    },
}


# Read-only account connectors used through ``ingest`` (not ``run`` tasks).  A pull
# returns the same held proposal as a statement upload; saving still needs the
# person's yes (``ingest action=confirm``).  Credentials are never inputs.
CONNECTORS: dict[str, dict[str, Any]] = {
    "ibkr_flex": {
        "purpose": "Pull an Interactive Brokers Activity Flex Query (positions with lots and cost basis, trades, "
                   "dividends, withholding, interest, fees, deposits/withdrawals, FX conversions, splits, conversion "
                   "rates, NAV) into a reconciled ingest proposal. Read-only: it cannot trade or move money.",
        "action": "ingest action=connector",
        "required": ["name: ibkr_flex", "query_id (numeric Activity Flex Query id; not secret)",
                     "token in the OS keychain (service wealth-ibkr-flex) or WEALTH_IBKR_FLEX_TOKEN; never an input"],
        "optional": ["owner_id", "sic_listed: [symbols] or {symbol: true|false} (Mexican SIC listing; unknown otherwise)"],
        "example": {"name": "ibkr_flex", "query_id": "987654"},
        "status": "ingest action=connector_status (optional name): whether a token is available and the last sync",
        "resync": "Transactions carry IBKR trade/transaction ids, so a re-sync posts only new lines; "
                  "result.changes lists what moved since the last confirmed sync.",
    },
}


__all__ = ["CATALOG", "CONNECTORS"]
