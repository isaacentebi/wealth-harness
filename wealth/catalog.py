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

_PROACTIVE_FACTS: list[dict[str, Any]] = [
    {"key": "client.profile", "value": {"name": "Lucía", "residence": {"country": "MX"}, "tax_residence": ["MX"],
                                        "language": "es", "timezone": "America/Mexico_City"}},
    {"key": "income.salary", "value": {"amount": 42000, "currency": "MXN", "frequency": "monthly", "net": True,
                                       "kind": "salary"}},
    {"key": "spending.monthly", "value": {"essential": 22000, "discretionary": 8000, "currency": "MXN"}},
    {"key": "cash.nu", "value": {"amount": 40000, "currency": "MXN", "purpose": "reserve"}},
    {"key": "reserve", "value": {"target_months": 6}},
]
_MX_SOURCE = {"kind": "document", "ref": "estado de cuenta"}
_PROACTIVE_LEDGER: dict[str, Any] = {
    "accounts": [{"id": "nomina", "institution": "BBVA", "type": "checking", "currency": "MXN", "owners": _OWNER}],
    "instruments": [],
    "entries": [
        {"id": "p0", "account_id": "nomina", "kind": "opening_balance", "date": "2026-08-31", "amount": "15000",
         "currency": "MXN", "confidence": "reported", "source": _MX_SOURCE},
        *({"id": f"p{i}", "account_id": "nomina", "kind": "income", "subtype": "salary", "date": day, "amount": "21000",
           "currency": "MXN", "description": "PAGO DE NOMINA", "confidence": "reported", "source": _MX_SOURCE}
          for i, day in enumerate(["2026-09-15", "2026-09-30", "2026-10-15", "2026-10-30", "2026-11-15", "2026-11-30"], 1)),
        *({"id": f"r{i}", "account_id": "nomina", "kind": "expense", "date": day, "amount": "-26000",
           "currency": "MXN", "description": "RENTA DEPARTAMENTO", "confidence": "reported", "source": _MX_SOURCE}
          for i, day in enumerate(["2026-09-02", "2026-10-02", "2026-11-02", "2026-12-02"], 1)),
        {"id": "ag", "account_id": "nomina", "kind": "income", "date": "2026-12-15", "amount": "42000",
         "currency": "MXN", "description": "PAGO AGUINALDO", "confidence": "reported", "source": _MX_SOURCE},
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


_MX_OWNER = [{"person_id": "ana", "share": "1"}]


def _mx_entry(i: int, account: str, kind: str, day: str, amount: str | None = None, currency: str = "MXN",
              **extra: Any) -> dict[str, Any]:
    row = {"id": f"mx{i:03d}", "account_id": account, "kind": kind, "date": day, "currency": currency,
           "confidence": "reported", "source": _MX_SOURCE, **extra}
    if amount is not None:
        row["amount"] = amount
    return row


def _mx_entries() -> list[dict[str, Any]]:
    rows = [
        _mx_entry(1, "bbva", "opening_balance", "2025-12-31", "120000"),
        _mx_entry(2, "gbm", "opening_balance", "2025-12-31", "20000"),
        _mx_entry(3, "gbm", "opening_balance", "2025-12-31", instrument_id="CSPXN", quantity="30",
                  cost_basis="285000", acquired_on="2025-06-02"),
        _mx_entry(4, "gbm", "opening_balance", "2025-12-31", instrument_id="CETES", quantity="20000",
                  cost_basis="196000", acquired_on="2025-12-01"),
        _mx_entry(5, "ibkr", "opening_balance", "2025-12-31", instrument_id="VOO", quantity="20",
                  cost_basis="11000", acquired_on="2025-03-03", currency="USD"),
        _mx_entry(6, "ibkr", "opening_balance", "2025-12-31", "1000", currency="USD"),
        _mx_entry(7, "afore", "opening_balance", "2025-12-31", "450000"),
    ]
    i = 8
    for month in ("01", "02", "03", "04", "05", "06"):
        q2 = month in ("04", "05", "06")
        rows += [
            _mx_entry(i, "bbva", "income", f"2026-{month}-01", "60000", subtype="salary", description="PAGO DE NOMINA ACME"),
            _mx_entry(i + 1, "bbva", "expense", f"2026-{month}-01", "-18000", description="RENTA DEPARTAMENTO"),
            _mx_entry(i + 2, "bbva", "expense", f"2026-{month}-01", "-9000" if q2 else "-8000", description="WALMART SUPERCENTER"),
            _mx_entry(i + 3, "bbva", "expense", f"2026-{month}-01", "-6000" if q2 else "-4000", description="RESTAURANTE EL CARDENAL"),
        ]
        i += 4
    for month, price in (("04", "10000"), ("05", "10300")):
        rows += [
            _mx_entry(i, "bbva", "transfer", f"2026-{month}-01", "-10050", description="SPEI A GBM", transfer_group=f"t{month}"),
            _mx_entry(i + 1, "gbm", "transfer", f"2026-{month}-01", "10050", description="SPEI DE BBVA", transfer_group=f"t{month}"),
            _mx_entry(i + 2, "gbm", "buy", f"2026-{month}-01", f"-{price}", instrument_id="CSPXN", quantity="1"),
            _mx_entry(i + 3, "gbm", "fee", f"2026-{month}-01", "-25", description="COMISION COMPRA CSPXN"),
            _mx_entry(i + 4, "gbm", "fee", f"2026-{month}-01", "-4", description="IVA COMISION"),
        ]
        i += 5
    rows += [
        _mx_entry(i, "gbm", "sell", "2026-05-14", "50000", instrument_id="CETES", quantity="5000"),
        _mx_entry(i + 1, "ibkr", "dividend", "2026-06-15", "30", currency="USD", instrument_id="VOO"),
        _mx_entry(i + 2, "ibkr", "tax_withheld", "2026-06-15", "-3", currency="USD", instrument_id="VOO"),
    ]
    return rows


_MX_REVIEW_LEDGER: dict[str, Any] = {
    "accounts": [
        {"id": "bbva", "institution": "BBVA", "type": "checking", "currency": "MXN", "owners": _MX_OWNER},
        {"id": "gbm", "institution": "GBM", "type": "brokerage", "currency": "MXN", "owners": _MX_OWNER},
        {"id": "ibkr", "institution": "Interactive Brokers", "type": "brokerage", "currency": "USD", "owners": _MX_OWNER},
        {"id": "afore", "institution": "Afore XXI Banorte", "type": "afore", "currency": "MXN", "owners": _MX_OWNER},
    ],
    "instruments": [
        {"id": "CSPXN", "symbol": "CSPXN", "currency": "MXN", "listing_currency": "MXN", "asset_class": "fund",
         "underlying_symbol": "CSPX", "venue": "sic", "issuer_domicile": "IE"},
        {"id": "CETES", "symbol": "CETES", "currency": "MXN", "listing_currency": "MXN", "asset_class": "fixed_income",
         "underlying_symbol": "CETES", "venue": "bmv", "issuer_domicile": "MX"},
        {"id": "VOO", "symbol": "VOO", "currency": "USD", "listing_currency": "USD", "asset_class": "fund",
         "underlying_symbol": "VOO", "venue": "us", "issuer_domicile": "US"},
    ],
    "entries": _mx_entries(),
    "fx": [{"date": d, "base": "USD", "quote": "MXN", "rate": r, "source": "Banxico FIX (fictional example)"}
           for d, r in (("2025-12-31", "18.40"), ("2026-03-31", "18.60"), ("2026-04-30", "18.50"),
                        ("2026-05-14", "18.30"), ("2026-05-31", "18.20"), ("2026-06-15", "18.10"),
                        ("2026-06-30", "18.00"), ("2025-03-03", "20.30"), ("2025-06-02", "19.40"),
                        ("2025-12-01", "18.30"))],
    "assertions": [], "labels": [], "category_rules": [],
}


_MX_REVIEW_PRICES = {
    "CSPXN": [{"date": "2025-12-31", "price": "9800"}, {"date": "2026-03-31", "price": "10000"},
              {"date": "2026-04-30", "price": "10300"}, {"date": "2026-05-31", "price": "10100"},
              {"date": "2026-06-30", "price": "10500"}],
    "CETES": [{"date": "2025-12-31", "price": "9.80"}, {"date": "2026-03-31", "price": "9.90"},
              {"date": "2026-04-30", "price": "9.97"}, {"date": "2026-05-31", "price": "10.04"},
              {"date": "2026-06-30", "price": "10.11"}],
    "VOO": [{"date": "2025-12-31", "price": "470"}, {"date": "2026-03-31", "price": "480"},
            {"date": "2026-04-30", "price": "490"}, {"date": "2026-05-31", "price": "495"},
            {"date": "2026-06-30", "price": "505"}],
}


_MX_REVIEW_IPS = {"currency": "MXN", "decision_id": "ips-example", "risk": {"profile": "balanced"},
                  "allocation": {"model": "balanced", "sleeves": [
                      {"id": "global_equity", "name": "Global equity", "asset": "equity", "target": 0.6, "min": 0.55, "max": 0.65},
                      {"id": "mx_fixed_income", "name": "Mexican government fixed income", "asset": "fixed_income",
                       "target": 0.35, "min": 0.3, "max": 0.4},
                      {"id": "cash", "name": "Cash (MXN)", "asset": "cash", "target": 0.05, "min": 0.0375, "max": 0.0625}]}}


_MX_EQUITY_INDEX = {"name": "MSCI ACWI in MXN (fictional levels)",
                    "series": [{"date": "2026-03-31", "value": "100"}, {"date": "2026-06-30", "value": "104.2"}]}


_MX_FEE_OPTIONS: dict[str, Any] = {
    "instruments": {
        "CSPXN": {"expense_ratio": {"value": "0.07", "unit": "percent",
                                    "source": "iShares Core S&P 500 UCITS factsheet (fictional example)"}},
        "VOO": {"expense_ratio": {"value": "0.0003", "source": "Vanguard VOO prospectus (fictional example)"}},
        "CETES": {"expense_ratio": {"value": "0", "source": "Government security held directly: no management fee"}},
    },
    "alternatives": [{"symbol": "CSPX", "underlying_symbol": "CSPX", "issuer_domicile": "IE",
                      "expense_ratio": {"value": "0.07", "unit": "percent",
                                        "source": "iShares CSPX factsheet (fictional example)"}}],
    "cash_reference_rate": {"low": "7.0", "high": "7.5", "unit": "percent", "source": "CETES 28 days (fictional example)"},
}


_MX_REVIEW_FACTS = [
    {"key": "client.profile", "value": {"name": "Ana", "language": "es", "residence": {"country": "MX"},
                                        "tax_residence": ["MX"], "us_person": False, "birth_year": 1988}},
    {"key": "income.salary", "value": {"amount": 60000, "currency": "MXN", "frequency": "monthly", "kind": "salary"}},
    {"key": "spending.monthly", "value": {"essential": 30000, "total": 33000, "currency": "MXN"}},
    {"key": "cash.nu", "value": {"amount": 90000, "currency": "MXN", "purpose": "reserve", "institution": "Nu"}},
    {"key": "reserve", "value": {"target_months": 6}},
    {"key": "goals", "value": [{"id": "retiro", "name": "Retiro", "target_amount": 8000000, "currency": "MXN",
                                "target_date": "2055-01-01", "monthly_contribution": 10000}]},
    {"key": "thread.voo", "value": {"kind": "advice", "text": "Revisar si conviene pasar VOO a CSPX por situs sucesorio.",
                                    "status": "open", "created": "2026-05-20"}},
    {"key": "planning.dca", "value": {"plans": [{"id": "sp500", "currency": "MXN", "cadence": "monthly",
                                                 "start_date": "2026-04-01", "account_id": "gbm",
                                                 "legs": [{"instrument_id": "CSPXN", "amount": "10000"}]}]}},
]


_MX_REVIEW_EXAMPLE: dict[str, Any] = {
    "period_start": "2026-04-01", "period_end": "2026-06-30", "currency": "MXN",
    "ledger": _MX_REVIEW_LEDGER, "prices": _MX_REVIEW_PRICES, "facts": _MX_REVIEW_FACTS, "ips": _MX_REVIEW_IPS,
    "benchmarks": {"ips": {"global_equity": _MX_EQUITY_INDEX,
                           "mx_fixed_income": {"name": "CETES 364 days", "annual_rate": "0.075",
                                               "source": "Banxico auction yield (fictional example)"},
                           "cash": {"name": "CETES 28 days", "annual_rate": "0.07",
                                    "source": "Banxico auction yield (fictional example)"}},
                   "reference_60_40": {"equity": _MX_EQUITY_INDEX,
                                       "bonds": {"name": "Global aggregate bonds in MXN (fictional levels)",
                                                 "series": [{"date": "2026-03-31", "value": "100"},
                                                            {"date": "2026-06-30", "value": "100.9"}]}}},
    "tax": {"jurisdiction": "MX", "mx_marginal_rate": "0.30"}, "sic_listed": {"VOO": True},
    "goal_accounts": {"retiro": ["gbm", "ibkr"]},
    "ppr": {"tax_year": 2026, "accumulable_income_mxn": "720000", "contributed_mxn": "20000", "marginal_rate": "0.30"},
    "fees": _MX_FEE_OPTIONS,
}


_GUARD_FACTS: list[dict[str, Any]] = [
    {"key": "client.profile", "value": {"residence": {"country": "MX"}, "birth_year": 1988, "dependents": 2,
                                        "dependent_ages": [4, 7], "us_person": False}},
    {"key": "income.salary", "value": {"amount": 70000, "currency": "MXN", "frequency": "monthly", "net": True,
                                       "kind": "salary"}},
    {"key": "spending.monthly", "value": {"essential": 30000, "total": 42000, "currency": "MXN"}},
    {"key": "cash.nu", "value": {"amount": 200000, "currency": "MXN", "purpose": "reserve"}},
    {"key": "reserve", "value": {"target_months": 6}},
    {"key": "investment.gbm", "value": {"amount": 500000, "currency": "MXN", "institution": "GBM", "kind": "brokerage"}},
    {"key": "goals", "value": [{"id": "retiro", "name": "Retiro", "target_amount": 9000000, "currency": "MXN",
                                "target_date": "2053-01-01", "monthly_contribution": 8000},
                               {"id": "auto", "name": "Coche", "target_amount": 250000, "currency": "MXN",
                                "target_date": "2027-12-01"}]},
    {"key": "preference.risk", "value": {"drop_reaction": "hold", "experience": "some"}},
]


# ------------------------------------------------------------------ tax pack fixtures (fictional, 2025)

def _tp_entry(i: int, prefix: str, account: str, kind: str, day: str, amount: str | None = None,
              currency: str = "MXN", **extra: Any) -> dict[str, Any]:
    row = {"id": f"{prefix}{i:03d}", "account_id": account, "kind": kind, "date": day, "currency": currency,
           "confidence": "reported", "source": {"kind": "document", "ref": "estado de cuenta (fictional example)"},
           **extra}
    if amount is not None:
        row["amount"] = amount
    return row


_TP_INPC = {"2023-04": "128.363", "2024-06": "134.594", "2025-01": "137.949", "2025-02": "138.343",
            "2025-03": "138.726", "2025-08": "140.726", "2025-12": "142.645",
            "source": "INEGI INPC (fictional example values)"}

_TP_MX_LEDGER: dict[str, Any] = {
    "accounts": [
        {"id": "gbm", "institution": "GBM", "type": "brokerage", "currency": "MXN", "country": "MX", "owners": _MX_OWNER},
        {"id": "bbva", "institution": "BBVA", "type": "savings", "currency": "MXN", "country": "MX", "owners": _MX_OWNER},
    ],
    "instruments": [
        {"id": "AMXB", "symbol": "AMXB", "currency": "MXN", "asset_class": "equity", "venue": "bmv", "issuer_domicile": "MX"},
        {"id": "NAFTRAC", "symbol": "NAFTRAC", "currency": "MXN", "asset_class": "fund", "venue": "bmv",
         "issuer_domicile": "MX"},
        {"id": "CETES", "symbol": "CETES", "currency": "MXN", "asset_class": "fixed_income", "venue": "bmv",
         "issuer_domicile": "MX"},
    ],
    "entries": [
        _tp_entry(1, "mx", "gbm", "opening_balance", "2024-12-31", "200000"),
        _tp_entry(2, "mx", "gbm", "opening_balance", "2024-12-31", instrument_id="AMXB", quantity="2000",
                  cost_basis="30000", acquired_on="2023-04-03"),
        _tp_entry(3, "mx", "gbm", "opening_balance", "2024-12-31", instrument_id="NAFTRAC", quantity="500",
                  cost_basis="27000", acquired_on="2024-06-03"),
        _tp_entry(4, "mx", "bbva", "opening_balance", "2024-12-31", "80000"),
        _tp_entry(5, "mx", "gbm", "buy", "2025-01-06", "-98000", instrument_id="CETES", quantity="10000"),
        _tp_entry(6, "mx", "gbm", "buy", "2025-02-10", "-16000", instrument_id="AMXB", quantity="1000"),
        _tp_entry(7, "mx", "gbm", "sell", "2025-04-07", "25000", instrument_id="NAFTRAC", quantity="500"),
        _tp_entry(8, "mx", "gbm", "sell", "2025-07-07", "102500", instrument_id="CETES", quantity="10000"),
        _tp_entry(9, "mx", "gbm", "tax_withheld", "2025-07-07", "-400", instrument_id="CETES"),
        _tp_entry(10, "mx", "gbm", "dividend", "2025-07-21", "1200", instrument_id="AMXB"),
        _tp_entry(11, "mx", "gbm", "tax_withheld", "2025-07-21", "-120", instrument_id="AMXB"),
        _tp_entry(12, "mx", "gbm", "sell", "2025-09-15", "30000", instrument_id="AMXB", quantity="1500"),
        _tp_entry(13, "mx", "bbva", "interest", "2025-12-31", "1800"),
        _tp_entry(14, "mx", "bbva", "tax_withheld", "2025-12-31", "-300"),
    ],
    "fx": [], "assertions": [], "labels": [], "category_rules": [],
}

_TP_MX_FACTS = [
    {"key": "client.profile", "value": {"name": "Ana", "language": "es", "residence": {"country": "MX"},
                                        "tax_residence": ["MX"], "us_person": False, "birth_year": 1988}},
    {"key": "tax.2025", "value": {"mx": {
        "article_129_loss_carryforwards": [{"origin_year": 2023, "available_updated_mxn": 1500,
                                            "updated_through": "2025-12"}],
        "total_income_mxn": 900000, "accumulable_income_mxn": 860000, "marginal_rate": 0.3,
        "deductions": {"medical_mxn": 18000, "insurance_premiums_mxn": 12000, "ppr_mxn": 30000, "art185_mxn": 0},
        "inpc": _TP_INPC}}},
    {"key": "constancia.gbm_2025", "value": {
        "tax_year": 2025, "institution": "GBM", "account_id": "gbm", "currency": "MXN", "issued_on": "2026-02-13",
        "enajenacion": {"gain": 5417.0, "loss": 2830.0, "net": 2587.0},
        "intereses": {"nominal": 4500.0, "real": 2400.0, "real_loss": 0, "isr_withheld": 400.0},
        "dividendos": {"domestic_gross": 1200.0, "isr_withheld": 120.0}}},
]

_TP_MX_EXAMPLE: dict[str, Any] = {
    "tax_year": 2025, "as_of": "2026-03-01", "facts": _TP_MX_FACTS, "ledger": _TP_MX_LEDGER,
    "parameters": {"uma_annual_mxn": {"value": "41273.52", "source": "INEGI, UMA 2025 (DOF 10-01-2025)"}},
}

_TP_US_LEDGER: dict[str, Any] = {
    "accounts": [
        {"id": "schwab", "institution": "Charles Schwab", "type": "brokerage", "currency": "USD", "country": "US",
         "owners": [{"person_id": "sam", "share": "1"}]},
        {"id": "schwab_roth", "institution": "Charles Schwab", "type": "roth_ira", "currency": "USD", "country": "US",
         "owners": [{"person_id": "sam", "share": "1"}]},
    ],
    "instruments": [
        {"id": "VTI", "symbol": "VTI", "currency": "USD", "asset_class": "fund", "venue": "us", "issuer_domicile": "US"},
        {"id": "SCHD", "symbol": "SCHD", "currency": "USD", "asset_class": "fund", "venue": "us", "issuer_domicile": "US"},
    ],
    "entries": [
        _tp_entry(1, "us", "schwab", "opening_balance", "2024-12-31", "40000", currency="USD"),
        _tp_entry(2, "us", "schwab", "opening_balance", "2024-12-31", instrument_id="SCHD", quantity="50",
                  cost_basis="3500", acquired_on="2023-05-01", currency="USD"),
        _tp_entry(3, "us", "schwab", "buy", "2025-01-15", "-6000", currency="USD", instrument_id="VTI", quantity="20"),
        _tp_entry(4, "us", "schwab", "sell", "2025-03-10", "5600", currency="USD", instrument_id="VTI", quantity="20"),
        _tp_entry(5, "us", "schwab", "buy", "2025-03-25", "-5700", currency="USD", instrument_id="VTI", quantity="20"),
        _tp_entry(6, "us", "schwab", "dividend", "2025-06-25", "30", currency="USD", instrument_id="SCHD"),
        _tp_entry(7, "us", "schwab", "sell", "2025-08-01", "4200", currency="USD", instrument_id="SCHD", quantity="50"),
        _tp_entry(8, "us", "schwab", "dividend", "2025-09-30", "75", currency="USD", instrument_id="VTI"),
        _tp_entry(9, "us", "schwab", "sell", "2025-11-03", "6500", currency="USD", instrument_id="VTI", quantity="20"),
        _tp_entry(10, "us", "schwab", "interest", "2025-12-31", "40", currency="USD"),
        _tp_entry(11, "us", "schwab_roth", "deposit", "2025-04-01", "6000", currency="USD"),
        _tp_entry(12, "us", "schwab", "buy", "2026-01-20", "-3000", currency="USD", instrument_id="SCHD", quantity="40"),
    ],
    "fx": [], "assertions": [], "labels": [], "category_rules": [],
}

_TP_US_EXAMPLE: dict[str, Any] = {
    "tax_year": 2025, "as_of": "2026-02-20", "ledger": _TP_US_LEDGER,
    "facts": [
        {"key": "client.profile", "value": {"name": "Sam", "language": "en", "residence": {"country": "US"},
                                            "tax_residence": ["US"], "us_person": True, "birth_year": 1985}},
        {"key": "tax.2025", "value": {"us": {"filing_status": "single",
                                             "capital_loss_carryover": {"short_term": 0, "long_term": 1200}}}},
        {"key": "constancia.schwab_2025", "value": {
            "tax_year": 2025, "institution": "Charles Schwab", "account_id": "schwab", "currency": "USD",
            "form_1099_b": {"short_term_gain": 400.0, "long_term_gain": 700.0, "wash_sale_disallowed": 400.0},
            "form_1099_div": {"ordinary": 105.0, "qualified": 90.0, "capital_gain_distributions": 0,
                              "foreign_tax_paid": 4.0},
            "form_1099_int": {"interest": 40.0}}},
    ],
    "parameters": {"us_ira_limit": {"value": 7000, "source": "IRS Notice 2024-80 (2025 IRA limit)"}},
}

_TP_XB_LEDGER: dict[str, Any] = {
    "accounts": [
        {"id": "bbva", "institution": "BBVA", "type": "checking", "currency": "MXN", "country": "MX",
         "owners": [{"person_id": "lee", "share": "1"}]},
        {"id": "gbm", "institution": "GBM", "type": "brokerage", "currency": "MXN", "country": "MX",
         "owners": [{"person_id": "lee", "share": "1"}]},
    ],
    "instruments": [
        {"id": "CSPXN", "symbol": "CSPXN", "currency": "MXN", "asset_class": "fund", "venue": "sic",
         "issuer_domicile": "IE", "underlying_symbol": "CSPX"},
    ],
    "entries": [
        _tp_entry(1, "xb", "bbva", "opening_balance", "2024-12-31", "300000"),
        _tp_entry(2, "xb", "gbm", "opening_balance", "2024-12-31", "60000"),
        _tp_entry(3, "xb", "bbva", "income", "2025-06-01", "60000", subtype="salary", description="NOMINA"),
        _tp_entry(4, "xb", "bbva", "expense", "2025-07-01", "-20000", description="RENTA"),
        _tp_entry(5, "xb", "gbm", "buy", "2025-02-03", "-50000", instrument_id="CSPXN", quantity="5"),
        _tp_entry(6, "xb", "bbva", "interest", "2025-12-31", "1500"),
        _tp_entry(7, "xb", "bbva", "tax_withheld", "2025-12-31", "-250"),
    ],
    "fx": [{"date": d, "base": "USD", "quote": "MXN", "rate": r, "source": "Banxico FIX (fictional example)"}
           for d, r in (("2025-12-31", "18.00"),)],
    "assertions": [], "labels": [], "category_rules": [],
}

_TP_XB_EXAMPLE: dict[str, Any] = {
    "tax_year": 2025, "as_of": "2026-03-10", "ledger": _TP_XB_LEDGER,
    "facts": [
        {"key": "client.profile", "value": {"name": "Lee", "language": "en", "residence": {"country": "MX"},
                                            "tax_residence": ["MX"], "citizenship": ["US"], "us_person": True,
                                            "birth_year": 1990}},
        {"key": "tax.2025", "value": {"us": {"filing_status": "single",
                                             "capital_loss_carryover": {"short_term": 0, "long_term": 0}},
                                      "mx": {"article_129_loss_carryforwards": []}}},
    ],
}


_MANAGER_SNAPSHOT = ('snapshot: "example" (the fictional offline managers CIK 0000000001 and 0000000002) or '
                     "recorded pages {url: body}; omit for live EDGAR")


_MANAGER_NETWORK = ("Live EDGAR needs WEALTH_SEC_USER_AGENT ('Name email@domain', SEC fair access) and is cached on "
                    "disk; OpenFIGI maps CUSIPs (WEALTH_OPENFIGI_API_KEY optional), SEC names are the fallback.")


_MANAGER_IPS: dict[str, Any] = {
    "currency": "USD",
    "allocation": {"model": "growth", "sleeves": [
        {"id": "equity", "name": "Equity", "asset": "equity", "target": 0.8, "min": 0.75, "max": 0.85},
        {"id": "fixed_income", "name": "Fixed income", "asset": "fixed_income", "target": 0.2, "min": 0.15, "max": 0.25}]},
    "constraints": {"concentration": {"limit": 0.1}, "leverage": {"allowed": False}},
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
            "fund_holdings[] {instrument_id, as_of, source, holdings, asset_class?} (asset_class labels holdings and the unreported residual)",
            "income_exposures[].employer_instrument_id (employer stock: concentration with look-through and salary)",
            "auto_lookthrough (default true: funds without holdings use saved research.<SYMBOL> fund packets)",
            "live_lookthrough (default true: a Yahoo top-holdings pull only when market data is online)",
        ],
        "notes": "A stored portfolio.snapshot converts foreign positions only through its own fx list [{from, to, rate, as_of, source}]. "
                 "Result adds top_underlying (direct + via funds), overlap_matrix (fund positions), employer_concentration "
                 "and lookthrough_sources (where each fund's holdings came from, or why none were found).",
        "example": {"household": _HOUSEHOLD, "targets": {"asset_class": {"equity": 0.6}}, "evaluation_date": "2026-09-20"},
    },
    "analyze": {
        "purpose": "Describe historical return, drawdown, volatility, beta, correlation, and concentration on one common sample, "
                   "plus tail_risk: historical and parametric 95% VaR/CVaR (1 day, 1 month) with each position's contribution.",
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
        "purpose": "Apply explicit shocks or dated historical windows to supplied holdings; scenarios are arithmetic, not forecasts. "
                   "Partial shocks reach the other holdings by beta; fx_shocks reach the reporting-currency value.",
        "required": ["household or weights", "scenarios: [{name, shocks {SYMBOL: return}, factor?, fx_shocks? {USDMXN: change}} "
                     "or {name, start, end}]"],
        "optional": [*_MARKET_COMMON, "beta_prices {source, rows, currencies?} (history for betas only; the service "
                     "fills it from the price cache)", "betas {SYMBOL: beta} with beta_source",
                     "asset_classes {SYMBOL: equity|bond|reit|commodity|gold|crypto|cash} (else household asset_class)",
                     "asset_currencies {SYMBOL: USD} (else positions[].native_currency)"],
        "notes": "Holdings without a shock move by beta to the factor (default: the first shocked symbol): a history "
                 "beta from at least 60 overlapping daily returns, else betas, else a stated asset-class default when "
                 "the factor is an equity index; otherwise the return is null with a reason. Each row's propagation "
                 "says which. With fx_shocks, shocks are in each holding's own currency.",
        "example": {"currency": "USD", "weights": {"SPY": 0.7, "BND": 0.3}, "scenarios": [{"name": "equity shock", "shocks": {"SPY": -0.2, "BND": 0.02}}]},
        "variants": {
            "partial_shock_by_beta": {"currency": "USD", "weights": {"VTI": 0.6, "BND": 0.4},
                                      "asset_classes": {"VTI": "equity", "BND": "bond"},
                                      "scenarios": [{"name": "S&P 500 -25%", "shocks": {"SPY": -0.25}}]},
            "peso_household_fx_shock": {"household": {**_HOUSEHOLD, "currency": "MXN", "positions": [
                {"id": "pos-1", "account_id": "a1", "instrument_id": "SPY", "symbol": "SPY", "quantity": 10,
                 "value": 120000, "currency": "MXN", "native_currency": "USD", "asset_class": "equity"},
                {"id": "pos-2", "account_id": "a1", "instrument_id": "MXN", "symbol": "MXN", "value": 30000,
                 "currency": "MXN", "asset_class": "cash"}]},
                "scenarios": [{"name": "US selloff, peso weaker", "shocks": {"SPY": -0.25},
                               "fx_shocks": {"USDMXN": 0.15}}]},
        },
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
    "debt": {
        "purpose": "The debt engine: full amortization (cards, loans, mortgages, Infonavit/Fovissste in VSM/UMA), prepay vs "
                   "invest after tax, refinance / balance transfer / consolidation offers, and avalanche vs snowball vs hybrid.",
        "required": ["mode: amortize | prepay_vs_invest | refinance | strategies",
                     "liabilities [{id, kind, balance, annual_rate (the tasa, decimal), monthly_payment | remaining_term_months "
                     "| minimum_payment, currency}], or debt (one id or object); default: the client's stored liability.<id> facts",
                     "prepay_vs_invest: extra_monthly and/or lump_sum", "refinance: offer", "strategies: monthly_amount"],
        "optional": ["debts: stored liability ids to include", "as_of", "currency",
                     "payment_frequency weekly|biweekly|semimonthly|monthly|quarterly|semiannual|annual (the payment is "
                     "converted to its monthly equivalent)", "maturity (a date; later this month counts as 1 month)",
                     "card: credit_limit, cat (Banxico CAT, compared with the tasa; alone it is used as an estimate), iva_on_interest "
                     "(default 16% for MXN cards and consumer credit, 0 for home credit), minimum_payment {percent_of_balance, "
                     "plus_interest (true), floor | percent_of_limit + credit_limit}",
                     "Infonavit/Fovissste: kind infonavit|fovissste, denomination VSM|UMA|MXN, balance_units (or balance in "
                     "pesos), unit_value_mxn (monthly; UMA defaults to INEGI 2026 x 30.4), monthly_payment_units or "
                     "monthly_payment, unit_growth_annual (default 4%, an estimate), update_month, months_paid | "
                     "origination_date (unknown: listed in missing and the projection is partial, without the 30-year "
                     "liberation), liberation_eligible (the 30-year liberation applies to VSM/UMA credits under the "
                     "post-1997 regime with no payment omissions; denomination MXN amortizes by contract)",
                     "amortize: monthly_rows (default 12, or \"all\")",
                     "prepay_vs_invest: horizon_months (default: payoff at the current payment), jurisdiction MX|US, "
                     "marginal_rate, federal_marginal_rate (T-bills are state-exempt), itemizes (US mortgage interest), "
                     "capital_gains_rate (US, default 15%), standard_deduction + other_itemized_deductions (US "
                     "itemizers: only interest above the standard deduction saves tax), student_loan_deduction (US, "
                     "IRC 221 $2,500 cap; false above the income phase-out), investment_channel "
                     "mx_intermediary|foreign_broker and sic_listed (MX: 10% Art. 129 on the real gain for BMV/SIC "
                     "securities; other securities through a foreign broker at the marginal rate), account "
                     "taxable|tax_free, mx_mortgage {casa_habitacion, "
                     "financial_system_lender, credit_udis | credit_amount_mxn + udi_value_at_origination + udi_source, "
                     "within_global_cap} (LISR Art. 151 fr. IV, as mx_deductions), inflation (MX real interest, default 4%), "
                     "expected_return {conservative, base, source}, risk_free {rate, source} (default: a saved "
                     "cash_reference_rate, else the latest CETES 28-day (MXN) or 13-week T-bill (USD) auction rate, "
                     "dated and sourced; see reference_rates), investment (a name), "
                     "reserve {months, target_months} (default: the saved picture)",
                     "refinance: offer {kind refinance|balance_transfer|consolidation, annual_rate (after any promo; with a promo and "
                     "no rate given, the current rate is assumed and said), "
                     "promo_rate + promo_months, fee, fee_percent, fees_financed, term_months | monthly_payment, "
                     "deferred_interest} (no fee given: 0, stated as an assumption); the result adds same_payment "
                     "(your current payment on the new rate) and a verdict over both",
                     "strategies: order (a custom order to compare), quick_win_months (hybrid, default 3)"],
        "scope": "Fixed rates and on-time payments; interest accrues at annual_rate / 12 plus IVA where it applies. "
                 "A payment derived from a term includes IVA where it applies. Anything unknown is listed in missing and "
                 "shown as a range, never taken as zero. prepay_vs_invest waits for the full reserve target, except a "
                 "debt costing 20%+ a year with IVA, which waits only for one month of essentials. Views: balance "
                 "series, payoff tickets and comparisons.",
        "example": {"mode": "amortize", "as_of": "2026-09-21", "liabilities": [
            {"id": "tarjeta", "name": "Tarjeta BBVA", "kind": "card", "balance": 30000, "annual_rate": 0.45, "cat": 0.60,
             "monthly_payment": 2500, "currency": "MXN",
             "minimum_payment": {"percent_of_balance": 0.015, "plus_interest": True, "percent_of_limit": 0.0125,
                                 "credit_limit": 60000}}]},
        "variants": {
            "mx_card_cat_only": {"mode": "amortize", "as_of": "2026-09-21", "liabilities": [
                {"id": "tarjeta", "kind": "card", "balance": 30000, "cat": 0.60, "monthly_payment": 2500, "currency": "MXN"}]},
            "us_mortgage_prepay_vs_vti": {
                "mode": "prepay_vs_invest", "as_of": "2026-09-21",
                "debt": {"id": "mortgage", "kind": "mortgage", "balance": 350000, "annual_rate": 0.065,
                         "monthly_payment": 2400, "currency": "USD"},
                "extra_monthly": 500, "jurisdiction": "US", "marginal_rate": 0.24, "itemizes": False,
                "capital_gains_rate": 0.15, "investment": "VTI",
                "expected_return": {"conservative": 0.04, "base": 0.065, "source": "Wealth planning range for VTI (fictional)"},
                "risk_free": {"rate": 0.04, "source": "3-month T-bill (fictional example)"},
                "reserve": {"months": 6, "target_months": 6}},
            "mx_card_prepay_vs_cetes": {
                "mode": "prepay_vs_invest", "as_of": "2026-09-21",
                "debt": {"id": "tarjeta", "kind": "card", "balance": 30000, "annual_rate": 0.45, "monthly_payment": 2500,
                         "currency": "MXN"},
                "extra_monthly": 3000, "marginal_rate": 0.30, "reserve": {"months": 4, "target_months": 3}},
            "car_loan_refinance": {
                "mode": "refinance", "as_of": "2026-09-21",
                "liabilities": [{"id": "car", "kind": "auto", "balance": 22000, "annual_rate": 0.089, "monthly_payment": 520,
                                 "currency": "USD"}],
                "offer": {"kind": "refinance", "annual_rate": 0.059, "fee": 350, "term_months": 48}},
            "card_balance_transfer": {
                "mode": "refinance", "as_of": "2026-09-21",
                "liabilities": [{"id": "card", "kind": "card", "balance": 8000, "annual_rate": 0.2499, "monthly_payment": 300,
                                 "currency": "USD"}],
                "offer": {"kind": "balance_transfer", "annual_rate": 0.2699, "promo_rate": 0, "promo_months": 15,
                          "fee_percent": 0.03}},
            "infonavit_uma": {"mode": "amortize", "as_of": "2026-09-21", "liabilities": [
                {"id": "infonavit", "kind": "infonavit", "denomination": "UMA", "balance_units": 180, "annual_rate": 0.09,
                 "monthly_payment_units": 1.9, "months_paid": 60}]},
            "strategies": {"mode": "strategies", "as_of": "2026-09-21", "monthly_amount": 9000, "liabilities": [
                {"id": "tarjeta", "kind": "card", "balance": 30000, "annual_rate": 0.45, "monthly_payment": 2500, "currency": "MXN"},
                {"id": "car", "kind": "auto", "balance": 85000, "annual_rate": 0.14, "monthly_payment": 3200, "currency": "MXN"},
                {"id": "personal", "kind": "personal", "balance": 6000, "annual_rate": 0.32, "monthly_payment": 800,
                 "currency": "MXN"}]},
        },
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
        "notes": "Result rows report wash_sale {disallowed_loss, replacements}. Not state tax or a filing position. "
                 "harvest_report also returns order_tickets [{account_id, estimated_tax_saving, repurchase_not_before, "
                 "inputs}]: pass inputs to order_ticket (sell lines carry lots [{lot_id, quantity, estimated_tax_saving, "
                 "repurchase_not_before}]); the person confirms on the card. order_ticket_exclusions says why a loss lot "
                 "was left out (wash_sale, carryforward_only, not_tradable_here).",
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
    "tax_pack": {
        "purpose": "The annual tax pack for the contador or CPA, built from the ledger and saved facts. Mexico: Art. 129 "
                   "sales per broker with INPC-updated average cost, the net result, the 10% and carryforwards; interest "
                   "nominal and real per institution with ISR withheld; domestic and foreign dividends; foreign securities "
                   "at a foreign broker; Art. 151/185 deductions with the CFDI checklist; aguinaldo/PTU exemptions only "
                   "when stated. US: Form 8949 lots with wash sales (code W), Schedule D totals and carryovers, "
                   "1099-DIV/INT (qualified vs ordinary), foreign tax paid (Form 1116 inputs), IRA/Roth contributions vs "
                   "the limit and RMDs, FBAR/Form 8938 flags with thresholds and sources. Always: pendientes (what is "
                   "missing) and key deadlines. A saved constancia or 1099 is the source of truth: our figure, the "
                   "document and the difference.",
        "required": ["client_id with a posted ledger (or ledger + facts [{key, value}])"],
        "optional": ["tax_year (default: the last completed year)",
                     "jurisdiction MX | US | \"MX,US\" (default: tax.<year>.jurisdiction, else the profile's tax "
                     "residence, plus US for a US person)",
                     "inpc {\"YYYY-MM\": value, source} (default tax.<year>.mx.inpc)",
                     "prices {instrument_id: [{date, price}]} (month-end values for FBAR/8938)",
                     "parameters {key: {value, source}} for unverified dated parameters (UMA, IRA limit)",
                     "language es|en", "exports [\"csv\", \"html\"] (adds result.csv {file: text} and result.html)",
                     "as_of"],
        "notes": "Reads client.profile, tax.<year> (stated tax facts: carryforwards, deductions, income, filing status, "
                 "IRA contributions) and constancia.<id> (documents: enajenacion, intereses, dividendos, form_1099_b, "
                 "form_1099_div, form_1099_int, form_5498; an uploaded PDF confirmed through wealth_ingest saves "
                 "them with source.kind=document). Result: sections {id: {status, title {es, en}, summary, table "
                 "{columns, rows}, reconciliation [{item, ours, document, difference, source_of_truth}], missing, "
                 "warnings, sources, assumptions}}, section_order, pendientes, deadlines. An empty figure is unknown, "
                 "never zero. CLI: `wealth tax-pack --client ID --year YYYY --out DIR` writes JSON, one CSV per section "
                 "and a printable bilingual HTML; the web serves /api/tax-pack?year=.",
        "example": _TP_MX_EXAMPLE,
        "variants": {"us_schwab_wash_sale": _TP_US_EXAMPLE, "us_person_in_mexico": _TP_XB_EXAMPLE},
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
        "optional": ["deductions.outside_global_cap_mxn", "parameters {key: {value, source}}",
                     "mortgage (LISR Art. 151 fr. IV, inside the global cap; keep it out of general_mxn) {casa_habitacion: true, "
                     "financial_system_lender: true, real_interest_paid_mxn (constancia) | nominal_interest_paid_mxn + "
                     "inflation_adjustment_mxn, credit_udis | credit_amount_mxn + udi_value_at_origination + udi_source, "
                     "constancia_deductible_real_interest_mxn?}"],
        "example": {"tax_year": 2026, "total_income_mxn": 1000000, "accumulable_income_mxn": 900000,
                    "deductions": {"general_mxn": 200000, "retirement_151v_mxn": 50000, "art185_mxn": 0},
                    "proposed_ppr_contribution_mxn": 100000, "taxable_income_before_mxn": 700000},
        "variants": {"mortgage": {"tax_year": 2026, "total_income_mxn": 1000000, "accumulable_income_mxn": 900000,
                                  "deductions": {"general_mxn": 60000, "retirement_151v_mxn": 0, "art185_mxn": 0},
                                  "mortgage": {"casa_habitacion": True, "financial_system_lender": True,
                                               "real_interest_paid_mxn": 90000, "credit_udis": 400000}}},
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
    "today": {
        "purpose": "Proactive nudges: at most 3 ranked items for today (money at risk > deadline within 14 days > "
                   "opportunity > info, one per kind) plus upcoming dates, from the saved picture, ledger and the "
                   "MX/US/life calendar. Each item: id, kind, severity act|consider|fyi, title/why/next_step in en "
                   "and es, due, data, sources, fingerprint. Unknown inputs never fire a nudge; they are listed in "
                   "result.unknown.",
        "required": ["client_id (or facts [{key, value}] and an optional ledger for a run without a client)"],
        "optional": ["as_of (default today in the client's timezone)", "jurisdiction: MX|US|'MX,US' (default "
                     "stated tax residence, plus US for US persons)", "timezone (IANA)",
                     "dismiss: [item id] (hidden until its trigger changes)",
                     "snooze: [{id, until: YYYY-MM-DD} | {id, days}]", "restore: [item id]"],
        "example": {"as_of": "2026-12-18", "facts": _PROACTIVE_FACTS, "ledger": _PROACTIVE_LEDGER},
        "notes": "The idle_yield item prices cash above the reserve target and goals at a reference rate: a saved "
                 "cash_reference_rate {low, high, unit, source, as_of?, currency?} fact, else the latest CETES 28-day "
                 "auction rate for Mexico residents (13-week T-bill for US filers' USD cash), with its date and source "
                 "(marked stale after 30 days; see reference_rates). A saved cash_yield (what the cash earns) "
                 "makes it exact; without it the figure is an upper bound. data.offer holds ladder task inputs for a "
                 "four-rung CETES ladder.",
        "variants": {"us_person_in_mexico": {"as_of": "2026-09-08", "jurisdiction": "MX,US", "facts": _PROACTIVE_FACTS}},
    },
    "weekly": {
        "purpose": "The weekly letter: the week in at most 5 lines of data (cash flow, new merchants, today's items, "
                   "the next date) for the model to phrase. Replaces a stream of notifications.",
        "required": ["client_id (or facts [{key, value}] and an optional ledger)"],
        "optional": ["as_of", "jurisdiction", "timezone"],
        "example": {"as_of": "2026-12-18", "facts": _PROACTIVE_FACTS, "ledger": _PROACTIVE_LEDGER},
    },
    "reference_rates": {
        "purpose": "Today's cash reference rates, dated and sourced: CETES 28 days (Banxico weekly primary auction) "
                   "and the 13-week US Treasury bill (high investment rate at auction), plus other CETES and bill "
                   "tenors when cached. With client_id a saved cash_reference_rate in that currency comes first.",
        "required": ["nothing (client_id optional)"],
        "optional": ["currency: MXN|USD (default both)", "as_of (default today)"],
        "notes": "Each rate has rate (decimal), percent, as_of (auction date, never after the as_of asked about), "
                 "source, origin saved_fact|fetched|builtin|unavailable and stale (older than 30 days). Fetches refresh at most daily in the "
                 "background; a turn never waits for the network, and builtin is Wealth's dated fallback.",
        "example": {"currency": "MXN"},
    },
    "quarterly_review": {
        "purpose":"The quarterly report a private bank sends: net worth start to end decomposed into contributions "
                   "and growth, TWR and XIRR per account against the IPS benchmark and a global 60/40, allocation and "
                   "drift against the IPS bands, cash flow and savings rate vs the prior quarter, goal progress and "
                   "pace, the decision journal with what happened since, DCA adherence, realised gains and estimated "
                   "MX/US tax (period and YTD), fees paid, the timeline of changed facts, open threads, and two "
                   "ranked decisions for next quarter. Every section has status, missing, sources and assumptions; "
                   "narrative_inputs are the only numbers a letter may use.",
        "required": ["period_start, period_end (inclusive; the opening value is the end of the day before)",
                     "client_id with a posted ledger (or ledger)", "prices {instrument_id: [{date, price}]}"],
        "optional": ["currency (default: the picture's)", "ips (default: the accepted policy.ips)",
                     "benchmarks {ips: {sleeve_id: {name, series [{date, value}]} | {name, annual_rate, source}}, "
                     "reference_60_40: {equity, bonds}}",
                     "tax {jurisdiction: MX|US, mx_marginal_rate, us_rates {short_term, long_term}}",
                     "sic_listed {instrument_id: true|false} (MX regime of foreign-venue sales)",
                     "fees {instruments {id: {expense_ratio {value, unit: decimal|percent|bps, source}}}, alternatives, "
                     "cash_reference_rate {low, high, unit, source}, cash_yield, advisory, afore, accounts, return_range}",
                     "goal_accounts {goal_id: [account_id]}", "goal_return_assumption (0.05)",
                     "dca_plans (default planning.dca)",
                     "ppr {tax_year, accumulable_income_mxn, contributed_mxn, marginal_rate}",
                     "sleeve_map {instrument_id: sleeve_id}", "facts [{key, value}] to run without a profile",
                     "max_price_age_days (5)", "max_fx_age_days (5)"],
        "notes": "Result: sections {net_worth, performance, allocation, cash_flow, goals, decisions, dca, taxes, fees, "
                 "changes, threads, next_quarter} each {status, data, missing, sources, assumptions, warnings}, and "
                 "narrative_inputs. net_worth.data.identity satisfies start + contributions + growth = end.",
        "example": _MX_REVIEW_EXAMPLE,
    },
    "fee_audit": {
        "purpose": "All-in annual cost audit: fund expense ratios (sourced only; ambiguous units stay unknown), broker "
                   "commissions plus IVA from the ledger, AFORE comisión (CONSAR, dated; fails closed), advisory/wrap "
                   "fees and idle-cash drag as a range. Annual cost in money and basis points, top 3 sources, neutral "
                   "cheaper equivalents (same index lower TER; for Mexico residents the Irish UCITS route and estate "
                   "situs) and the 10/20-year compounding cost of the fee gap as a range.",
        "required": ["holdings [{account_id, instrument_id, value}] in currency, or client_id/ledger + prices",
                     "instruments {id: {expense_ratio {value, unit?, source}}} for fund costs"],
        "optional": ["as_of (default today)", "currency", "accounts [{id, type, afore_name?}]",
                     "window_start (commissions window; default one year)", "residence (MX|US)",
                     "advisory [{name, rate {value, unit, source}, account_ids?}]",
                     "cash_reference_rate {low, high, unit, source}", "cash_yield",
                     "alternatives [{symbol, underlying_symbol|index, issuer_domicile, expense_ratio {value, unit, source}}]",
                     "afore [{name, balance}]", "return_range [low, high] (0.04, 0.07)"],
        "example": {"as_of": "2026-06-30", "currency": "MXN", "residence": "MX", "ledger": _MX_REVIEW_LEDGER,
                    "prices": _MX_REVIEW_PRICES, "window_start": "2026-01-01", **_MX_FEE_OPTIONS},
        "variants": {"holdings_only": {
            "as_of": "2026-06-30", "currency": "USD", "residence": "US",
            "holdings": [{"account_id": "brk", "instrument_id": "FUNDX", "value": 100000},
                         {"account_id": "brk", "instrument_id": "cash:USD", "kind": "cash", "value": 5000}],
            "instruments": {"FUNDX": {"symbol": "FUNDX", "index": "US total market",
                                      "expense_ratio": {"value": "0.45", "unit": "percent",
                                                        "source": "fund prospectus (fictional)"}}},
            "accounts": [{"id": "brk", "type": "taxable"}],
            "alternatives": [{"symbol": "VTI", "underlying_symbol": "VTI",
                              "expense_ratio": {"value": "3", "unit": "bps", "source": "Vanguard VTI (fictional example)"}}],
            "advisory": [{"name": "Advisor wrap", "rate": {"value": "1", "unit": "percent",
                                                          "source": "advisory agreement (fictional)"}}],
            "cash_reference_rate": {"low": "4.0", "high": "4.5", "unit": "percent",
                                    "source": "3-month T-bill (fictional example)"},
        }},
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
                     "conversion_target_rate (default 0.12), conversion_sweep_rates (default [0.10, 0.12, 0.22, 0.24]), "
                     "terminal_rates {tax_deferred, taxable_gain}, social_security_annual_benefit_usd + "
                     "social_security_start_age (taxed under IRC 86), threshold_inflation (default 0.025), "
                     "medicare_enrollees, medicare_start_age (65), magi_prior_two_years_usd [t-2, t-1], irmaa: false}",
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
    "order_ticket": {
        "purpose": "Prepare an order ticket when the person asks to act. The account's institution picks the broker: "
                   "Alpaca (paper unless the person enabled live trading), Interactive Brokers through the gateway the "
                   "person runs (paper when logged into a DU... account), or, for any broker without an API (GBM, "
                   "Vest, Schwab, Fidelity...), a place-it-yourself card with the exact symbol and listing (SIC or "
                   "BMV), fees and FX that the person marks 'Ya la puse' once placed. Wealth stores the exact orders with pre-trade checks (tradable/fractionable, cash buying "
                   "power, the accepted IPS, live per-order and daily limits, duplicates, market hours, a limit price "
                   "collared around the last trade, estimated tax and cost) and returns a ticket id and a summary. It "
                   "never places an order: the person confirms its card in the Wealth web app (wealth-chat). inputs {ticket_id} "
                   "alone reads a stored ticket's state instead. inputs {ticket_id, placed: true} records that the "
                   "person placed a place-it-yourself ticket at their broker (only when they say so; outside the "
                   "Wealth app it returns needs_person with a code: show it and, on their yes, call again with "
                   "confirm: true and confirmation_code inside inputs); it never sends anything. When the person "
                   "names a broker or account, orders[].account_id is required: with several brokerage accounts "
                   "and none named the result is needs_input listing them.",
        "required": ["orders [{symbol, side: buy|sell, qty | notional (USD)}] (a rebalance trade's instrument_id, "
                     "quantity and estimated_amount are accepted)",
                     "rationale (one or two sentences the person reads)", "client_id (a ticket belongs to a person)"],
        "optional": ["source: rebalance|manager_mirror|user_request (default user_request)",
                     "orders[].type: limit (default) | market (paper only)", "orders[].limit_price (default: last "
                     "trade +/- half the collar)", "orders[].time_in_force: day (default) | gtc (whole shares)",
                     "orders[].account_id (one account per ticket; it picks the broker; required when the person "
                     "names a broker), estimated_tax, "
                     "estimated_cost, asset_class, sleeve, domicile, tags",
                     "orders[].exchange: SIC | BMV | BIVA for a Mexican broker (default: SIC for foreign shares, BMV "
                     "for known Mexican issuers)",
                     "orders[].lots (sells only) [{lot_id, quantity?, estimated_tax_saving?, repurchase_not_before?, "
                     "character?, account_id?}] (from tax harvest_report order_tickets)"],
        "notes": "Result: ticket {id, broker alpaca|ibkr|manual, broker_label, mode paper|live|manual, status, total, lines [{side, symbol, qty, limit_price, "
                 "estimated_amount, state}], notices [{code, status: warn|violation|block|unknown, message}]} and "
                 "summary. Tell the person to confirm on the card; never say an order was placed or filled until "
                 "its line state says so. Without client_id the result is a preview that cannot be confirmed.",
        "example": {"source": "user_request", "rationale": "Invest this month's USD 500 in the total-market fund.",
                    "orders": [{"symbol": "VTI", "side": "buy", "notional": 500}]},
    },
    "speculation_check": {
        "purpose": "Check a speculative idea (options, leverage, crypto, a single stock) against the play-money "
                   "policy: a cap of 5% of liquid net worth (0% while the reserve is short or any debt costs over "
                   "15%), a single-position loss limit and a drawdown stop. Returns allow | allow_with_warning | "
                   "decline_to_recommend with plain reasons (en/es) and the mechanics-and-risks card; never a trade "
                   "call, and action=explain always explains.",
        "required": ["proposal {action: buy|sell|explain, instrument: options|crypto|margin|cfd|future|perp|"
                     "leveraged_etf|short|stock|fintech_yield, amount?, currency?, leverage?, side?: long|short, "
                     "position_effect?: open|close, option_type?, covered?, strike?, contracts?, premium? (per "
                     "share), entry_price?, negative_balance_protection?, sleeve?: {value, peak}}",
                     "client_id (or facts [{key, value}])"],
        "optional": ["policy {cap_share (<= 0.10), max_position_loss_share, drawdown_stop} (else preference.speculation, "
                     "else defaults)", "ips (else the accepted policy.ips)",
                     "context {now: ISO date-time, timezone: IANA, last_move: {size, at}, udi_value} for the cool-off "
                     "flag", "as_of",
                     "proposal.legs [{type: call|put, side, strike, premium, contracts, cash_secured?} | {type: "
                     "stock|crypto, side, entry_price, quantity|amount} | {type: leveraged, side, entry_price, margin, "
                     "leverage, negative_balance_protection?}] for a multi-leg payoff",
                     "proposal.spot, proposal.grid [prices]", "proposal.fx_rate (1 proposal currency in the picture's)"],
        "notes": "A sell of an option writes it (sell to open) unless position_effect is close; writing is sized by "
                 "the capital it puts at risk (an uncovered put: strike x 100 x contracts less premium; an "
                 "uncovered call has no ceiling). With legs, or strike, contracts and premium (entry_price and "
                 "leverage for leveraged or crypto), result.payoff has the P&L grid at expiry, max_loss / "
                 "max_gain (null with *_unbounded), breakevens, capital_at_risk and sizing {share_of_net_worth, "
                 "share_of_speculation_budget} (null with a reason when unknown), plus an en/es explanation; "
                 "action=explain returns the payoff without a verdict.",
        "example": {"as_of": "2026-09-21", "facts": _GUARD_FACTS,
                    "proposal": {"action": "buy", "instrument": "options", "amount": 3000, "currency": "MXN",
                                 "sleeve": {"value": 10000, "peak": 12000}},
                    "context": {"now": "2026-09-21T15:10:00-06:00", "timezone": "America/Mexico_City"}},
        "variants": {
            "card_debt_blocks_play_money": {"as_of": "2026-09-21", "facts": _GUARD_FACTS + [
                {"key": "liability.tdc", "value": {"kind": "card", "balance": 40000, "currency": "MXN",
                                                   "annual_rate": 0.45, "payment": 5000, "payment_frequency": "monthly"}}],
                "proposal": {"action": "buy", "instrument": "crypto", "amount": 5000, "currency": "MXN"}},
            "explain_mechanics": {"proposal": {"action": "explain", "instrument": "cfd"}},
            "write_uncovered_puts": {"as_of": "2026-09-21", "facts": _GUARD_FACTS,
                                     "proposal": {"action": "sell", "instrument": "options", "symbol": "AAPL",
                                                  "option_type": "put", "covered": False, "strike": 180,
                                                  "contracts": 2, "premium": 3.5, "currency": "MXN",
                                                  "sleeve": {"value": 0, "peak": 0}}},
            "vertical_spread_payoff": {"as_of": "2026-09-21", "facts": _GUARD_FACTS,
                                       "proposal": {"action": "explain", "instrument": "options", "symbol": "SPY",
                                                    "currency": "MXN", "spot": 650, "legs": [
                                                        {"type": "call", "side": "long", "strike": 650,
                                                         "premium": 12, "contracts": 1},
                                                        {"type": "call", "side": "short", "strike": 670,
                                                         "premium": 5, "contracts": 1}]}},
            "leveraged_crypto_payoff": {"as_of": "2026-09-21", "facts": _GUARD_FACTS,
                                        "proposal": {"action": "buy", "instrument": "perp", "symbol": "BTC",
                                                     "amount": 2000, "leverage": 5, "entry_price": 60000,
                                                     "negative_balance_protection": True, "currency": "MXN",
                                                     "sleeve": {"value": 0, "peak": 0}}},
        },
    },
    "panic_check": {
        "purpose": "Circuit breaker for 'sell everything' after a fall above 10%: what is actually at risk for the "
                   "goals, what followed similar S&P 500 drawdowns (with a stress request for this portfolio), the "
                   "tax cost of selling and a suggested cool-off. It never blocks the person's choice.",
        "required": ["request {action: sell_all|sell, share? (for sell), drawdown (0.18 = 18% below the peak)}",
                     "client_id (or facts)"],
        "optional": ["request.portfolio_value", "request.holdings [{symbol, value, cost_basis, account_type?, "
                     "acquired_on?, listed_in_mx?}] for the tax cost", "request.history (stress-task scenario rows)",
                     "request.jurisdiction", "context {now, timezone, last_move}", "as_of"],
        "example": {"as_of": "2026-09-21", "facts": _GUARD_FACTS,
                    "request": {"action": "sell_all", "drawdown": 0.18, "portfolio_value": 500000,
                                "holdings": [{"symbol": "CSPX", "value": 400000, "cost_basis": 330000},
                                             {"symbol": "AFORE", "value": 100000, "account_type": "afore"}]},
                    "context": {"now": "2026-09-21T23:40:00-06:00", "timezone": "America/Mexico_City",
                                "last_move": {"size": -0.06, "at": "2026-09-21T23:05:00-06:00"}}},
    },
    "scam_check": {
        "purpose": "Screen a message or a transfer for scam patterns (guaranteed or high monthly returns, urgency, "
                   "requests for codes or passwords, 'safe account', pay-to-withdraw, advance-fee loans, fake "
                   "CONDUSEF/SAT/IRS contact, pig-butchering scripts, a large transfer to a new payee). Deterministic; "
                   "returns risk low|medium|high, reasons (en/es) and the official places to verify (CONDUSEF SIPRES, "
                   "CNBV padron, FINRA BrokerCheck, SEC Investor.gov, FTC, IC3).",
        "required": ["item: message text, or {text?, amount?, currency?, payee?, new_payee?: bool}"],
        "optional": ["jurisdiction: MX|US (else residence; unknown lists both)", "typical_transfer",
                     "client_id (liquid assets size 'large')"],
        "example": {"item": "Hola, soy asesor de inversiones. Te ofrezco 8% mensual garantizado en nuestra plataforma, "
                            "solo hoy. Deposita en USDT y compárteme el código que te llegó por SMS.",
                    "jurisdiction": "MX"},
        "variants": {
            "large_transfer_new_payee": {"item": {"text": "Transfer to new beneficiary", "amount": 9000, "currency": "USD",
                                                  "payee": "Global Asset Partners", "new_payee": True},
                                         "jurisdiction": "US"},
        },
    },
    "protection_review": {
        "purpose": "Protection review: life-insurance need as a needs-minus-resources range (only with dependants), "
                   "disability income gap, health cover (Mexico: gastos medicos mayores deductible and coaseguro vs "
                   "the reserve; US: out-of-pocket maximum and HSA eligibility) and an estate checklist with status "
                   "(testamento and Mes del Testamento, beneficiaries, poder notarial, marital regime, US-situs "
                   "exposure via estate). Refers to a licensed agent and a notario/attorney; never drafts.",
        "required": ["client_id (or facts [{key, value}])"],
        "optional": ["policies [{kind: life|disability|gmm|health, cover_amount?, monthly_benefit?, deductible?, "
                     "coinsurance?, coinsurance_cap?, out_of_pocket_max?, coverage?: self|family, hdhp?, currency?}] "
                     "(omitted = unknown, [] = none)",
                     "life {years?, replacement_ratio?, education?, final_expenses?}",
                     "disability {gross_monthly_income?, other_monthly_benefit?}",
                     "estate {will: yes|no|unknown, will_date?, beneficiaries: {afore, insurance, accounts, retirement}, "
                     "power_of_attorney, advance_directive, married, marital_regime: sociedad_conyugal|"
                     "separacion_de_bienes, us_situs: estate-task inputs}", "jurisdiction", "as_of"],
        "example": {"as_of": "2026-09-21", "facts": _GUARD_FACTS[:-2] + [
                        {"key": "goals", "value": [{"id": "uni", "name": "Universidad", "action": "education",
                                                    "target_amount": 900000, "currency": "MXN",
                                                    "target_date": "2040-08-01"}]}],
                    "policies": [{"kind": "life", "cover_amount": 1000000, "currency": "MXN"},
                                 {"kind": "gmm", "deductible": 20000, "coinsurance": 0.1, "coinsurance_cap": 40000},
                                 {"kind": "disability", "monthly_benefit": 20000}],
                    "estate": {"will": "no", "beneficiaries": {"afore": "yes", "insurance": "yes", "accounts": "unknown"},
                               "power_of_attorney": "no", "married": True, "marital_regime": "sociedad_conyugal",
                               "us_situs": {"year": 2026, "decedent": {"us_citizen": False, "green_card": False,
                                                                       "us_domiciled": False},
                                            "assets": [{"id": "voo", "type": "us_domiciled_fund", "value_usd": 90000}]}}},
        "variants": {
            "us_hsa": {"as_of": "2026-09-21", "facts": [
                {"key": "client.profile", "value": {"residence": {"country": "US"}, "birth_year": 1968, "dependents": 0}},
                {"key": "income.salary", "value": {"amount": 9000, "currency": "USD", "frequency": "monthly", "net": False}},
                {"key": "cash.bank", "value": {"amount": 20000, "currency": "USD", "purpose": "reserve"}}],
                "policies": [{"kind": "health", "coverage": "self", "hdhp": True, "deductible": 2000,
                              "out_of_pocket_max": 6000},
                             {"kind": "disability", "monthly_benefit": 4000}]},
        },
    },
    "life_event": {
        "purpose": "Route a life event (marriage, birth_or_adoption, divorce, death_in_family, job_change, layoff, "
                   "relocation, inheritance, home_purchase, retirement) to an ordered checklist of reviews: facts to "
                   "update, tasks to run, referrals and dated nudges, in English and Mexican Spanish, filtered by "
                   "residence.",
        "required": ["kind"],
        "optional": ["date (YYYY-MM-DD; default today)", "details", "jurisdiction: MX|US (else residence; unknown "
                     "lists both)", "client_id"],
        "example": {"kind": "birth_or_adoption", "date": "2026-09-21", "jurisdiction": "MX"},
    },
    "estate_register": {
        "purpose": "Estate and beneficiary register: for each account, policy and property, what happens at the "
                   "person's death (beneficiary designation that skips the juicio sucesorio or probate, trust, "
                   "survivorship on a US joint account, will, or intestate succession), who receives it and an "
                   "estimated amount per heir; gaps ranked by amount at risk (no beneficiary, shares not 100%, a "
                   "minor named directly without guardian or trust, predeceased or ex-spouse beneficiary, old "
                   "designation or one before a marriage or child, AFORE beneficiaries, ERISA spousal consent, "
                   "US-situs over US$60,000 for a non-resident alien, no will or a will older than a marriage or "
                   "child, no guardian), questions for what is unknown, a 0-100 completeness score and two views. "
                   "Mexico: LIC Art. 56 bank beneficiaries, LMV Art. 201, AFORE designacion (LSS Art. 193), "
                   "mancomunada vs beneficiary, Mes del Testamento; US: TOD/POD, SECURE Act 10-year rule, ERISA. "
                   "An estimate, not legal advice.",
        "required": ["client_id (or facts [{key, value}]: cash.<id>, investment.<id>, insurance.<id>, "
                     "property.<id>, estate.designation.<slug> {account, beneficiaries, designation_date, titling, "
                     "...}, estate.will, estate.guardianship, estate.family {marital_regime, spouse_assets, ...})"],
        "optional": ["review_years (default 5)", "us_situs: estate-task inputs (else US-domiciled holdings)",
                     "as_of"],
        "example": {"as_of": "2026-09-22", "facts": [
            {"key": "client.profile", "value": {"residence": {"country": "MX"}, "birth_year": 1986, "dependents": 2,
                                                "dependent_ages": [5, 9], "us_person": False, "language": "es"}},
            {"key": "cash.bbva", "value": {"amount": 85000, "currency": "MXN", "institution": "BBVA",
                                           "purpose": "reserve"}},
            {"key": "estate.designation.cash-bbva", "value": {
                "account": "cash.bbva", "designation_date": "2023-03-10",
                "beneficiaries": [{"name": "Laura", "relationship": "spouse", "share": 1}]}},
            {"key": "investment.gbm", "value": {"amount": 217000, "currency": "MXN", "institution": "GBM",
                                                "kind": "brokerage"}},
            {"key": "estate.designation.investment-gbm", "value": {"account": "investment.gbm", "beneficiaries": []}},
            {"key": "investment.afore", "value": {"amount": 410000, "currency": "MXN",
                                                  "institution": "Afore XXI Banorte", "kind": "afore"}},
            {"key": "investment.cetes", "value": {"amount": 120000, "currency": "MXN", "institution": "Cetesdirecto"}},
            {"key": "estate.designation.investment-cetes", "value": {
                "account": "investment.cetes",
                "beneficiaries": [{"name": "Laura", "share": 0.6}, {"name": "Sofía", "relationship": "child",
                                                                    "share": 0.3}]}},
            {"key": "insurance.vida", "value": {"kind": "life", "coverage": 1500000, "currency": "MXN",
                                                "insurer": "GNP"}},
            {"key": "estate.designation.insurance-vida", "value": {
                "account": "insurance.vida", "designation_date": "2014-05-01",
                "beneficiaries": [{"name": "Laura", "relationship": "spouse"}]}},
            {"key": "property.depa", "value": {"kind": "home", "value": 3200000, "currency": "MXN"}},
            {"key": "estate.will", "value": {"exists": False}},
            {"key": "estate.family", "value": {"marital_status": "married", "marriage_date": "2015-06-20",
                                               "spouse": "Laura", "marital_regime": "separacion_de_bienes",
                                               "children": [{"name": "Sofía", "birth_year": 2017},
                                                            {"name": "Mateo", "birth_year": 2021}],
                                               "parents_living": 2}}]},
        "variants": {
            "us_401k_consent_and_ira": {"as_of": "2026-09-22", "facts": [
                {"key": "client.profile", "value": {"residence": {"country": "US"}, "birth_year": 1980,
                                                    "us_person": True, "dependents": 0}},
                {"key": "investment.k401", "value": {"amount": 380000, "currency": "USD", "institution": "Fidelity",
                                                     "kind": "retirement", "plan_type": "401k"}},
                {"key": "estate.designation.investment-k401", "value": {
                    "account": "investment.k401", "designation_date": "2012-02-01",
                    "beneficiaries": [{"name": "Tom", "relationship": "sibling", "share": 1}]}},
                {"key": "investment.ira", "value": {"amount": 95000, "currency": "USD", "institution": "Vanguard",
                                                    "kind": "retirement", "plan_type": "roth_ira"}},
                {"key": "estate.designation.investment-ira", "value": {
                    "account": "investment.ira", "designation_date": "2024-01-15",
                    "beneficiaries": [{"name": "Dana", "relationship": "spouse", "share": 0.5},
                                      {"name": "Tom", "relationship": "sibling", "share": 0.5}]}},
                {"key": "investment.brokerage", "value": {"amount": 60000, "currency": "USD",
                                                          "institution": "Schwab", "kind": "brokerage"}},
                {"key": "estate.designation.investment-brokerage", "value": {
                    "account": "investment.brokerage", "titling": "joint", "co_owners": ["Dana"]}},
                {"key": "estate.will", "value": {"exists": True, "date": "2019-05-01",
                                                 "heirs": [{"name": "Dana", "relationship": "spouse"}]}},
                {"key": "estate.family", "value": {"marital_status": "married", "marriage_date": "2020-10-10",
                                                   "spouse": "Dana", "children": [],
                                                   "marital_regime": "separate_property"}}]},
            "mx_resident_us_etfs": {"as_of": "2026-09-22", "facts": [
                {"key": "client.profile", "value": {"residence": {"country": "MX"}, "us_person": False,
                                                    "reporting_currency": "USD"}},
                {"key": "investment.ibkr", "value": {"amount": 150000, "currency": "USD", "institution": "IBKR",
                                                     "kind": "brokerage"}},
                {"key": "estate.designation.investment-ibkr", "value": {
                    "account": "investment.ibkr", "country": "US",
                    "beneficiaries": [{"name": "Andrés", "relationship": "child", "share": 1}]}},
                {"key": "estate.will", "value": {"exists": True, "date": "2024-09-15"}}],
                "us_situs": {"year": 2026, "decedent": {"us_citizen": False, "green_card": False,
                                                        "us_domiciled": False},
                             "assets": [{"id": "voo", "type": "us_domiciled_fund", "value_usd": 150000}]}},
        },
    },
    "manager_search": {
        "purpose": "Find a fund manager's SEC filer (CIK) by firm or person name, with its latest 13F filing. "
                   "Says so when a match does not file 13F or its latest 13F is a notice pointing to another filer.",
        "required": ["name (firm or person, e.g. 'Situational Awareness', 'Gavin Baker')"],
        "optional": ["limit (1-20, default 8)", _MANAGER_SNAPSHOT],
        "notes": _MANAGER_NETWORK,
        "example": {"name": "example", "snapshot": "example"},
    },
    "manager_holdings": {
        "purpose": "A manager's 13F holdings for the latest (or a chosen) quarter: issuer, class, CUSIP, ticker with "
                   "mapping confidence, shares, value, weight; options and principal-amount rows apart; amendments "
                   "applied; changes vs the prior quarter; the reporting lag and what a 13F leaves out.",
        "required": ["cik (from manager_search)"],
        "optional": ["period (YYYY-MM-DD quarter end or 2026Q2)", "include_options (add weights that count options at "
                     "underlying value)", "tickers {cusip: ticker} overrides", "as_of (for the lag)", _MANAGER_SNAPSHOT],
        "notes": "Result: positions, options, other, changes {new, exited, increased, decreased}, lag, excludes, "
                 "what_is_13f {en, es}, amendments. " + _MANAGER_NETWORK,
        "example": {"cik": "0000000001", "snapshot": "example", "as_of": "2026-09-21"},
        "variants": {"earlier_quarter_with_options": {"cik": "0000000001", "period": "2026Q1", "include_options": True,
                                                      "snapshot": "example", "as_of": "2026-09-21"}},
    },
    "manager_profile": {
        "purpose": "How a manager invests, read from consecutive 13F filings: turnover (quarterly and annualised "
                   "estimate), holding period, concentration and its trend, conviction and position sizing, sector "
                   "drift, options usage, and a plain-language character in English and Spanish.",
        "required": ["cik"],
        "optional": ["quarters (2-40, default 8)", "sectors (default true; issuer SIC codes from EDGAR)",
                     "tickers {cusip: ticker}", _MANAGER_SNAPSHOT],
        "notes": "Turnover = min(buys, sells) / average portfolio value from quarter-end share changes; an estimate, "
                 "since trades inside a quarter are invisible. " + _MANAGER_NETWORK,
        "example": {"cik": "0000000001", "quarters": 4, "snapshot": "example", "as_of": "2026-09-21"},
    },
    "manager_compare": {
        "purpose": "Two to six managers' 13F profiles side by side (turnover, holding period, concentration, "
                   "initial position size, options share, sector drift, character).",
        "required": ["ciks: [cik, ...] (2-6)"],
        "optional": ["quarters (default 8)", "sectors", "tickers", _MANAGER_SNAPSHOT],
        "example": {"ciks": ["0000000001", "0000000002"], "quarters": 4, "snapshot": "example", "as_of": "2026-09-21"},
    },
    "manager_mirror": {
        "purpose": "Mirror a manager's long-equity 13F weights in a sleeve of the person's money: drop unmapped "
                   "tickers, optional top-N and minimum weight, cap single names at the policy's concentration limit "
                   "and redistribute, check each buy against the IPS, flag Mexico SIC availability, US estate situs "
                   "and whole-share cost, and hand off to rebalance for a trade list. Never places orders.",
        "required": ["holdings (a manager_holdings result) or cik", "sleeve_amount", "currency"],
        "optional": ["period", "ips (else the accepted policy.ips)", "residence: MX|US (else client.profile)",
                     "constraints {top_n, min_weight, max_weight, min_confidence (0.8), portfolio_value, prices "
                     "{TICKER: USD}, usdmxn, sic_listed {TICKER: bool}, funding, portfolio (policy_check shape), "
                     "household + jurisdiction_context (+ cash_flows, tax_inputs, rebalance_constraints) for trades}",
                     "backtest {quarters?, prices|price_csv+price_source|years, benchmark?}: historical, lagged copy "
                     "of each 13F from its filing date", "tickers", _MANAGER_SNAPSHOT],
        "notes": "Result: targets [{ticker, weight, amount, capped, mexico?}], cap, dropped, expected_trades, "
                 "policy_check, speculation (satellite-sleeve assessment), plan (rebalance envelope or null), "
                 "tracking_caveats (always), backtest?. execution_ready is always false.",
        "example": {"cik": "0000000001", "snapshot": "example", "as_of": "2026-09-21",
                    "sleeve_amount": 50000, "currency": "USD", "ips": _MANAGER_IPS,
                    "constraints": {"top_n": 5, "portfolio_value": 500000}},
        "variants": {
            "mexico_resident": {"cik": "0000000001", "snapshot": "example", "as_of": "2026-09-21",
                                "sleeve_amount": 200000, "currency": "MXN", "residence": "MX", "ips": _MANAGER_IPS,
                                "constraints": {"usdmxn": 18.5, "sic_listed": {"NVDA": True, "TSM": True, "META": True}}},
            "with_backtest": {"cik": "0000000002", "snapshot": "example", "as_of": "2026-09-21",
                              "sleeve_amount": 20000, "currency": "USD", "ips": _MANAGER_IPS,
                              "constraints": {"portfolio_value": 200000},
                              "backtest": {"quarters": 4, "benchmark": "SPY", "prices": {
                                  "currency": "USD", "source": "example closes (fictional)", "rows": [
                                      {"date": "2026-02-12", "MSFT": 470, "V": 335, "JPM": 298, "KO": 70, "AMZN": 228, "SPY": 690},
                                      {"date": "2026-05-13", "MSFT": 455, "V": 332, "JPM": 292, "KO": 72, "AMZN": 210, "SPY": 675},
                                      {"date": "2026-08-12", "MSFT": 472, "V": 344, "JPM": 306, "KO": 71, "AMZN": 226, "SPY": 705},
                                      {"date": "2026-09-18", "MSFT": 480, "V": 350, "JPM": 310, "KO": 72, "AMZN": 231, "SPY": 712}]}}},
        },
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
    "alpaca": {
        "purpose": "Pull an Alpaca brokerage account (Trading API v2: account, positions with average cost, open "
                   "orders, fills, dividends, NRA withholding, interest, fees, cash transfers, portfolio history) into "
                   "a reconciled ingest proposal. Read-only: only GET on read endpoints; it cannot trade or move money.",
        "action": "ingest action=connector",
        "required": ["name: alpaca",
                     "key id and secret in the OS keychain (service wealth-alpaca, accounts key_id and secret) or "
                     "WEALTH_ALPACA_KEY_ID/WEALTH_ALPACA_SECRET; never an input"],
        "optional": ["owner_id", "paper: true for the paper host (or WEALTH_ALPACA_PAPER=1)",
                     "since: YYYY-MM-DD (default 365 days ago)",
                     "sic_listed: [symbols] or {symbol: true|false} (Mexican SIC listing; unknown otherwise)"],
        "example": {"name": "alpaca", "since": "2026-01-01"},
        "status": "ingest action=connector_status (optional name): whether keys are available and the last sync",
        "resync": "Lines carry Alpaca activity ids, so a re-sync posts only new lines; result.changes lists what moved.",
        "limits": "Alpaca reports average entry price, not tax lots (lots are marked unavailable), and no ISIN.",
    },
    "cuenca": {
        "purpose": "Pull a Cuenca account (MXN): balance and apartados as balance checks, SPEI and internal transfers, "
                   "deposits, card purchases (merchant, categorised like statement lines), ATM withdrawals and "
                   "commissions into a reconciled ingest proposal. Read-only: only GET; it cannot transfer or pay.",
        "action": "ingest action=connector",
        "required": ["name: cuenca",
                     "API key and secret issued by Cuenca, in the OS keychain (service wealth-cuenca, accounts api_key "
                     "and api_secret) or WEALTH_CUENCA_API_KEY/WEALTH_CUENCA_API_SECRET; never an input"],
        "optional": ["owner_id", "since: YYYY-MM-DD (default 365 days ago)"],
        "example": {"name": "cuenca", "since": "2026-08-01"},
        "status": "ingest action=connector_status (optional name): whether keys are available and the last sync",
        "resync": "SPEI lines carry their clave de rastreo and others their Cuenca id, so a re-sync posts only new lines.",
        "limits": "Cuenca does not publish whether individual app users can get API keys; without one, upload the "
                  "monthly estado de cuenta with ingest action=file.",
    },
}
# Providers without an API for individuals: statements come in through ingest action=file.
STATEMENT_ONLY: dict[str, dict[str, Any]] = {
    "vest": {
        "why": "Vest (vest.investments) offers no API for individual users; its app provides monthly statements, "
               "trade confirmations and 1042-S/1099 tax reports, with no documented export layout.",
        "action": "ingest action=file with preset=vest (a generic US-broker CSV layout, labelled as such), or upload "
                  "the monthly statement PDF.",
    },
}


__all__ = ["CATALOG", "CONNECTORS", "STATEMENT_ONLY"]
