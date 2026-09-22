"""Quarterly review and fee audit: the report a private bank sends every quarter.

* :func:`quarterly` builds a deterministic, structured quarterly report from
  the ledger, the canonical situation, the accepted IPS, the decision journal
  and the fact history.  Every section carries ``status``, ``missing``,
  ``sources`` and ``assumptions``; a ``narrative_inputs`` block holds the
  compact facts a model may turn into a one-page letter (prose only: it never
  writes a number it was not given).
* :func:`audit` (also exported as :data:`fees_audit`) is the all-in annual
  cost audit: fund expense ratios, broker commissions plus IVA, AFORE
  comisión, advisory/wrap fees and the cost of idle cash, with the annual
  cost in money and basis points, the top three sources, neutral cheaper
  equivalents and the 10/20-year compounding cost of the fee gap as a range.

Rules applied everywhere here:

* Unknown is never zero.  A missing price, rate, expense ratio, commission or
  tax parameter leaves that figure ``None`` and names it in ``missing``.
* A rate is used only with a source.  A bare expense ratio is read as a
  decimal (0.0007 = 0.07%) only below 3%; anything else without an explicit
  ``unit`` (decimal | percent | bps) is ambiguous and stays unknown, the same
  convention as :mod:`wealth.market`.
* AFORE commissions come from the dated CONSAR table below; an unknown AFORE
  or a year that is not in the table fails closed.
* The period is inclusive: the opening value is the end of the day before
  ``period_start`` and the closing value the end of ``period_end``.
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
import json
from typing import Any, Callable, Iterable, Mapping, Sequence

from . import cashflow, dca as dca_module, policy
from .ledger.derive import FxTable, active_entries, price_table, realized_gains, replay
from .ledger.model import fold, money
from .ledger.performance import performance, performance_by_account
from .situation.model import UNDERLYING, add_months, build as build_situation

ZERO = Decimal(0)
CENT = Decimal("0.01")
TASKS = ("quarterly_review", "fee_audit")

# Account types that are not part of the investment portfolio the IPS governs.
_NON_PORTFOLIO_TYPES = frozenset({"checking", "savings", "bank", "cash", "debit", "credit_card", "loan", "mortgage",
                                  "line_of_credit", "afore"})
_SPENDING_TYPES = frozenset({"checking", "savings", "bank", "cash", "credit_card", "debit"})
_TAX_ADVANTAGED_TYPES = frozenset({"afore", "ppr", "retirement", "ira", "401k", "roth_ira", "traditional_ira",
                                   "pension"})
_ART129_VENUES = frozenset({"bmv", "biva", "sic"})

CONSAR_2026_URL = ("https://www.gob.mx/consar/articulos/junta-de-gobierno-de-la-consar-autoriza-comisiones-de-las-"
                   "afore-para-2026-413436")
# Commission on balance (decimal) authorised by the CONSAR Junta de Gobierno, dated per year.
AFORE_COMMISSIONS: dict[int, dict[str, Any]] = {
    2026: {
        "status": "verified", "checked_on": "2026-09-21",
        "source": {"title": "CONSAR, Junta de Gobierno autoriza comisiones de las Afore para 2026 (21-11-2025)",
                   "url": CONSAR_2026_URL},
        "rates": {"azteca": "0.0054", "banamex": "0.0054", "coppel": "0.0054", "inbursa": "0.0054",
                  "invercap": "0.0054", "principal": "0.0054", "profuturo": "0.0054", "sura": "0.0054",
                  "xxi banorte": "0.0054", "pensionissste": "0.0052"},
        "aliases": {"citibanamex": "banamex", "afore banamex": "banamex", "xxi": "xxi banorte", "banorte": "xxi banorte",
                    "pension issste": "pensionissste", "afore sura": "sura", "afore coppel": "coppel",
                    "afore azteca": "azteca", "profuturo gnp": "profuturo", "afore inbursa": "inbursa",
                    "afore invercap": "invercap", "afore principal": "principal", "afore xxi banorte": "xxi banorte"},
        "system_average": "0.00538",
    },
}

_RULES = {
    "decomposition": ("Net worth change = net contributions (earned income, spending and other money in or out) + "
                      "growth (dividends and interest, fees and withholding, price and FX change)."),
    "benchmark": ("Benchmark period returns are buy-and-hold from the opening date: the IPS benchmark weights each "
                  "sleeve's index by its target; the global reference is 60% equity index / 40% bond index."),
    "ranking": ("Next-quarter candidates rank by tier (1 reserve shortfall; 2 a deadline inside the next quarter; "
                "3 policy or goal off course; 4 efficiency), then by money at stake (unknown last), then by kind."),
}


# ------------------------------------------------------------------ helpers


def _d(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = Decimal(str(value))
    except Exception:
        return None
    return result if result.is_finite() else None


def _ratio(value: Decimal | float | None, places: str = "0.000001") -> str | None:
    return None if value is None else format(Decimal(str(value)).quantize(Decimal(places)), "f")


def _bps(cost: Decimal | None, base: Decimal | None) -> str | None:
    if cost is None or not base:
        return None
    return format((cost / base * 10000).quantize(Decimal("0.1")), "f")


def _date(value: Any, field: str) -> date:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be an ISO date (YYYY-MM-DD)")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{field} must be an ISO date (YYYY-MM-DD)") from exc
    if parsed.isoformat() != value:
        raise ValueError(f"{field} must be an ISO date (YYYY-MM-DD)")
    return parsed


def _day_before(day: str) -> str:
    return (date.fromisoformat(day) - timedelta(days=1)).isoformat()


def _miss(key: str, detail: str, reason: str = "missing") -> dict[str, str]:
    return {"key": key, "reason": reason, "detail": detail}


def _unique(items: Iterable[Mapping[str, Any]]) -> list[dict]:
    seen, result = set(), []
    for item in items:
        if item["key"] not in seen:
            seen.add(item["key"])
            result.append(dict(item))
    return result


def _section(data: Any, *, missing: Iterable = (), sources: Iterable = (), assumptions: Iterable = (),
             warnings: Iterable = (), status: str | None = None) -> dict[str, Any]:
    missing = _unique(missing)
    return {"status": status or ("partial" if missing else "ready"), "data": data, "missing": missing,
            "sources": list(sources), "assumptions": list(assumptions), "warnings": list(warnings)}


def _sum(values: Iterable[Decimal | None]) -> Decimal | None:
    total = ZERO
    for value in values:
        if value is None:
            return None
        total += value
    return total


def _brief(value: Any, limit: int = 160) -> Any:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return text if len(text) <= limit else text[: limit - 1] + "…"


class _Book:
    """Ledger lookups shared by the sections (one FX table, one price provider)."""

    def __init__(self, ledger: Mapping[str, Any], currency: str, prices: Mapping[str, Any] | None,
                 max_price_age_days: int = 5, max_fx_age_days: int = 5):
        self.ledger = ledger
        self.currency = currency
        self.price_series = prices or {}
        self.prices = price_table(self.price_series, max_price_age_days)
        self.fx = FxTable(ledger.get("fx", []), max_fx_age_days)
        self.instruments = {i["id"]: i for i in ledger.get("instruments", [])}
        self.accounts = {a["id"]: a for a in ledger.get("accounts", [])}

    def account_type(self, account_id: str) -> str:
        return str((self.accounts.get(account_id) or {}).get("type") or "other")

    def days(self, *, portfolio: bool = False) -> list[str]:
        """Sorted entry dates (investment accounts only with ``portfolio``)."""
        return sorted(e["date"] for e in self.ledger.get("entries") or [] if isinstance(e.get("date"), str)
                      and (not portfolio or self.account_type(e.get("account_id")) not in _NON_PORTFOLIO_TYPES))

    def known_at(self, day: str, *, portfolio: bool = False) -> bool:
        """Whether the ledger holds anything on or before ``day``: before that, balances are unknown, not zero."""
        days = self.days(portfolio=portfolio)
        return bool(days) and days[0] <= day

    def active_in(self, start: str, end: str, *, portfolio: bool = False) -> bool:
        """Whether the ledger has any entry inside the window (a quarter with no statement says nothing)."""
        return any(start <= d <= end for d in self.days(portfolio=portfolio))

    def convert(self, amount: Decimal | None, ccy: str | None, day: str) -> Decimal | None:
        return self.fx.convert(amount, ccy, self.currency, day)

    def positions(self, day: str) -> tuple[list[dict], list[dict]]:
        """Cash balances and positions at the end of ``day`` in the reporting currency."""
        state, _ = replay(self.ledger, day)
        rows, missing = [], []
        for (account, ccy), amount in sorted(state.cash.items()):
            if amount == 0:
                continue
            value = self.convert(amount, ccy, day)
            if value is None:
                missing.append(_miss(f"fx.{ccy}/{self.currency}@{day}", f"Cannot value {ccy} cash in {account}."))
            rows.append({"account_id": account, "instrument_id": f"cash:{ccy}", "kind": "cash", "native": amount,
                         "native_currency": ccy, "value": value, "lots": []})
        for (account, instrument), lots in sorted(state.lots.items()):
            quantity = sum((lot.quantity for lot in lots), ZERO)
            if quantity == 0:
                continue
            meta = self.instruments.get(instrument, {})
            price = self.prices(instrument, day)
            if price is None:
                missing.append(_miss(f"prices.{instrument}@{day}", f"No price for {instrument} on or shortly before {day}."))
                value = None
            else:
                value = self.convert(quantity * price, meta.get("currency"), day)
                if value is None:
                    missing.append(_miss(f"fx.{meta.get('currency')}/{self.currency}@{day}",
                                         f"Cannot convert {instrument} into {self.currency}."))
            rows.append({"account_id": account, "instrument_id": instrument, "kind": "position", "quantity": quantity,
                         "price": price, "native_currency": meta.get("currency"), "value": value, "lots": lots})
        return rows, missing


# ------------------------------------------------------------------ net worth and performance


def _market_change(book: _Book, opening: str, end: str) -> tuple[Decimal | None, list[dict]]:
    """Price (and FX) change of the holdings over (opening, end], measured on its own from the ledger.

    The valuation change of every position minus what moved into positions:
    trades at their gross value (fees are costs, not price), and securities
    transferred in or opened in the period at that day's price.  Cash, income,
    costs and external flows are not part of it, so the net-worth identity
    ``end = start + contributions + income + costs + market + residual`` is a
    real check: the residual is what none of them explains (cash FX moves,
    unmatched entries), not zero by construction.
    """
    start_rows, missing = book.positions(opening)
    end_rows, more = book.positions(end)
    missing = list(missing) + list(more)

    def held(rows: list[dict]) -> Decimal | None:
        return _sum(r["value"] for r in rows if r["kind"] == "position")

    moved: Decimal | None = ZERO
    entries, _ = active_entries(book.ledger)
    for entry in entries:
        if not (opening < entry["date"] <= end) or not entry.get("instrument_id") or entry.get("quantity") is None:
            continue
        kind = entry["kind"]
        if kind in {"buy", "sell"} and entry.get("amount") is not None:
            # buy: amount = -(gross + fee); sell: amount = gross - fee.  Value into the position: -amount - fee.
            into = -_d(entry["amount"]) - (_d(entry.get("fee")) or ZERO)
            value = book.convert(into, entry.get("currency"), entry["date"])
            if value is None:
                missing.append(_miss(f"fx.{entry.get('currency')}/{book.currency}@{entry['date']}",
                                     f"Cannot convert the {entry['instrument_id']} trade on {entry['date']}."))
        elif kind in {"transfer", "opening_balance"}:
            price = book.prices(entry["instrument_id"], entry["date"])
            meta = book.instruments.get(entry["instrument_id"], {})
            value = None if price is None else book.convert(_d(entry["quantity"]) * price, meta.get("currency"),
                                                            entry["date"])
            if value is None:
                missing.append(_miss(f"prices.{entry['instrument_id']}@{entry['date']}",
                                     f"Cannot value the {entry['instrument_id']} moved in on {entry['date']}."))
        else:
            continue
        moved = None if value is None or moved is None else moved + value
    start_value, end_value = held(start_rows), held(end_rows)
    if None in (start_value, end_value, moved):
        return None, missing
    return end_value - start_value - moved, missing


def _net_worth(book: _Book, start: str, end: str, sit: Mapping[str, Any] | None,
               perf: Mapping[str, Any]) -> dict:
    total = perf["result"]["total"]
    missing = list(perf["missing"])
    buckets: dict[str, Decimal | None] = {"earned_income": ZERO, "spending": ZERO, "other_flows": ZERO,
                                          "opening_balances": ZERO}
    names = {"income": "earned_income", "expense": "spending", "opening_balance": "opening_balances"}
    for flow in total["flows"]:
        bucket = names.get(flow["kind"], "other_flows")
        amount = _d(flow["amount"])
        buckets[bucket] = None if amount is None or buckets[bucket] is None else buckets[bucket] + amount
    opening, closing = _d(total["start_value"]), _d(total["end_value"])
    contributions = _d(total["decomposition"]["net_flows"])
    income = _d(total["decomposition"]["investment_income"])
    costs = _d(total["decomposition"]["fees_and_withholding"])
    market, market_missing = _market_change(book, _day_before(start), end)
    missing += market_missing
    # Before the first statement nothing is known: the opening value is unknown, never zero.  A period with
    # no entries at all says nothing about flows or the closing value either.
    if not book.known_at(_day_before(start)):
        opening = None
        missing.append(_miss(f"ledger.start@{_day_before(start)}",
                             f"No statement or entry on or before {_day_before(start)}: the value at the start of "
                             "the period is unknown.", "no_data"))
    if not book.active_in(start, end):
        closing = contributions = market = None
        buckets = {k: None for k in buckets}
        missing.append(_miss(f"ledger.period@{start}..{end}",
                             f"No statement or entry between {start} and {end}: this period's flows and closing value "
                             "are unknown.", "no_data"))
    growth = None if None in (income, costs, market) else income + costs + market
    residual = None if None in (opening, closing, contributions, growth) else closing - (opening + contributions + growth)
    data = {
        "currency": book.currency, "opening_date": _day_before(start), "closing_date": end,
        "start": money(opening), "end": money(closing),
        "change": money(None if opening is None or closing is None else closing - opening),
        "contributions": {"net": money(contributions), "earned_income": money(buckets["earned_income"]),
                          "spending": money(buckets["spending"]), "other_flows": money(buckets["other_flows"]),
                          "opening_balances_recorded": money(buckets["opening_balances"])},
        "growth": {"total": money(growth), "investment_income": money(income), "fees_and_withholding": money(costs),
                   "market": money(market)},
        "identity": {"start": money(opening), "contributions": money(contributions), "growth": money(growth),
                     "end": money(closing), "residual": money(residual)},
    }
    assumptions = [_RULES["decomposition"],
                   "Scope: every account in the ledger; card and loan balances count as negative cash.",
                   "Earned income and spending are the ledger's income and expense entries; transfers between the "
                   "person's own accounts are internal and cancel out.",
                   "Opening balances posted inside the period are shown apart: they record money already owned, "
                   "not new saving."]
    warnings = []
    if residual is not None and abs(residual) >= Decimal("1"):
        warnings.append(f"{money(residual)} {book.currency} of the change is not explained by contributions, income, "
                        "costs or market moves (for example currency moves on cash, or entries that do not match).")
    if sit is not None:
        nw = sit.get("net_worth") or {}
        # Outside the ledger means no ledger entries for that account, whatever fed the picture (a synced
        # connector account is a statement source but lives in the ledger).
        in_ledger = {e.get("account_id") for e in book.ledger.get("entries") or []}
        outside = [a["label"] for a in sit.get("accounts") or []
                   if a.get("id") not in in_ledger and a.get("value") is not None]
        outside += [c.get("name") or c.get("id") for c in sit.get("cash") or [] if c.get("counted") and c.get("value") is not None]
        data["situation_end"] = {"total": nw.get("total"), "currency": nw.get("currency"), "complete": nw.get("complete"),
                                 "stated_outside_ledger": outside}
        if outside:
            warnings.append("The picture also holds stated balances outside the ledger; they are in situation_end, "
                            "not in the decomposition.")
    if buckets.get("opening_balances"):
        warnings.append("Opening balances were recorded inside the period; the change partly reflects accounts added "
                        "to the ledger, not saving.")
    return _section(data, missing=missing, sources=perf["sources"], assumptions=assumptions, warnings=warnings)


def _series_return(spec: Any, start: str, end: str, max_age: int, label: str) -> tuple[Decimal | None, str | None, list]:
    if not isinstance(spec, Mapping):
        return None, None, [_miss(f"benchmarks.{label}", f"No {label} series or rate supplied.")]
    name = spec.get("name") or label
    if spec.get("annual_rate") is not None:
        rate = _d(spec["annual_rate"])
        if rate is None or not spec.get("source"):
            return None, name, [_miss(f"benchmarks.{label}.source", "A rate needs a numeric annual_rate and a source.")]
        days = (date.fromisoformat(end) - date.fromisoformat(start)).days
        return Decimal(str((1 + float(rate)) ** (days / 365.0) - 1)), name, []
    series = spec.get("series")
    if not isinstance(series, (list, Mapping)):
        return None, name, [_miss(f"benchmarks.{label}", "A benchmark needs series [{date, value}] or annual_rate.")]
    provider = price_table({"b": series}, max_age)
    first, last = provider("b", start), provider("b", end)
    missing = [_miss(f"benchmarks.{label}@{day}", f"No {name} level on or shortly before {day}.")
               for day, level in ((start, first), (end, last)) if level is None]
    return (None if missing else last / first - 1), name, missing


def _performance(book: _Book, start: str, end: str, perf: Mapping[str, Any], portfolio: Mapping[str, Any] | None,
                 ips: Mapping[str, Any] | None, benchmarks: Mapping[str, Any], max_age: int) -> dict:
    opening = _day_before(start)
    missing = list(perf["missing"])
    if portfolio is None:
        missing.append(_miss("accounts.investment", "No investment account in the ledger, so no portfolio return."))
        total = {"twr": {"period": None, "annualized": None}, "xirr_annual": None, "start_value": None,
                 "end_value": None, "scope": []}
    else:
        total = portfolio["result"]
        missing += portfolio["missing"]
    twr = _d(total["twr"]["period"])
    accounts = {}
    for account_id, row in perf["result"]["accounts"].items():
        if row["start_value"] in (None, "0.00") and row["end_value"] in (None, "0.00") and not row["flows"]:
            continue
        accounts[account_id] = {"start_value": row["start_value"], "end_value": row["end_value"],
                                "twr_period": row["twr"]["period"], "xirr_annual": row["xirr_annual"],
                                "net_flows": row["decomposition"]["net_flows"],
                                "gain": row["decomposition"]["total_gain"]}
    compared = {}
    sleeves = ((ips or {}).get("allocation") or {}).get("sleeves") or []
    ips_specs = benchmarks.get("ips") if isinstance(benchmarks.get("ips"), Mapping) else {}
    if sleeves:
        parts, names, ips_missing = [], [], []
        for sleeve in sleeves:
            if not sleeve.get("target"):
                continue
            ret, name, gaps = _series_return(ips_specs.get(sleeve["id"]), opening, end, max_age, f"ips.{sleeve['id']}")
            ips_missing += gaps
            parts.append(None if ret is None else Decimal(str(sleeve["target"])) * ret)
            names.append(f"{sleeve['target']:.0%} {name}")
        value = None if ips_missing else _sum(parts)
        missing += ips_missing
        compared["ips_benchmark"] = {"name": " + ".join(names), "period_return": _ratio(value),
                                     "excess_twr": _ratio(None if value is None or twr is None else twr - value)}
    else:
        missing.append(_miss("policy.ips", "No accepted IPS, so there is no policy benchmark."))
    reference = benchmarks.get("reference_60_40") if isinstance(benchmarks.get("reference_60_40"), Mapping) else {}
    equity, eq_name, eq_missing = _series_return(reference.get("equity"), opening, end, max_age, "reference_60_40.equity")
    bonds, bd_name, bd_missing = _series_return(reference.get("bonds"), opening, end, max_age, "reference_60_40.bonds")
    ref = None if equity is None or bonds is None else Decimal("0.6") * equity + Decimal("0.4") * bonds
    missing += eq_missing + bd_missing
    compared["global_60_40"] = {"name": f"60% {eq_name or 'equity index'} / 40% {bd_name or 'bond index'}",
                                "period_return": _ratio(ref),
                                "excess_twr": _ratio(None if ref is None or twr is None else twr - ref)}
    data = {"currency": book.currency, "period": {"start": start, "end": end, "opening_date": opening,
                                                  "days": (date.fromisoformat(end) - date.fromisoformat(opening)).days},
            "total": {"scope": total["scope"], "twr_period": total["twr"]["period"],
                      "twr_annualized": total["twr"]["annualized"], "xirr_annual": total["xirr_annual"],
                      "start_value": total["start_value"], "end_value": total["end_value"]},
            "accounts": accounts, "benchmarks": compared,
            "labels": {"twr": "time-weighted (the portfolio's return, flows removed)",
                       "xirr": "money-weighted (your return, timing of your deposits included), annual rate"}}
    return _section(data, missing=missing, sources=perf["sources"],
                    assumptions=perf["assumptions"] + [
                        "The portfolio total covers investment accounts (brokerage, PPR, retirement); bank and card "
                        "accounts and the AFORE are reported per account only.", _RULES["benchmark"],
                                                       "A quarter is not annualized for TWR; XIRR is an annual rate."])


# ------------------------------------------------------------------ allocation


def _sleeve_of(book: _Book, row: Mapping[str, Any], sleeves: list[dict], sleeve_map: Mapping[str, str]) -> str | None:
    if row["kind"] == "cash":
        return next((s["id"] for s in sleeves if s.get("asset") == "cash"), "cash")
    if row["instrument_id"] in sleeve_map:
        return sleeve_map[row["instrument_id"]]
    meta = book.instruments.get(row["instrument_id"], {})
    asset = policy._asset_of({"asset_class": meta.get("asset_class"), "symbol": meta.get("symbol")})
    if asset is None:
        return None
    if not sleeves:
        return asset
    return next((s["id"] for s in sleeves if s.get("asset") == asset), None)


def _weights(book: _Book, day: str, sleeves: list[dict], sleeve_map: Mapping[str, str]) -> tuple[dict, Decimal, list, list]:
    rows, missing = book.positions(day)
    values: dict[str, Decimal] = {}
    total, unclassified = ZERO, []
    for row in rows:
        if book.account_type(row["account_id"]) in _NON_PORTFOLIO_TYPES:
            continue
        if row["value"] is None:
            continue
        if row["kind"] == "cash" and row["value"] < 0:
            continue
        sleeve = _sleeve_of(book, row, sleeves, sleeve_map)
        if sleeve is None:
            unclassified.append(row["instrument_id"])
            missing.append(_miss(f"sleeve_map.{row['instrument_id']}",
                                 f"{row['instrument_id']} has no asset class; map it to a sleeve."))
            continue
        values[sleeve] = values.get(sleeve, ZERO) + row["value"]
        total += row["value"]
    return values, total, missing, unclassified


def _allocation(book: _Book, start: str, end: str, ips: Mapping[str, Any] | None,
                sleeve_map: Mapping[str, str]) -> dict:
    sleeves = ((ips or {}).get("allocation") or {}).get("sleeves") or []
    values, total, missing, unclassified = _weights(book, end, sleeves, sleeve_map)
    before, before_total, _, before_unclassified = _weights(book, _day_before(start), sleeves, sleeve_map)
    # An unclassified holding is as unknown as an unpriced one: weights without it would mislead, so there is
    # no portfolio value, no band judgement and no drift until it is mapped to a sleeve.
    incomplete = any(m["key"].startswith(("prices.", "fx.")) for m in missing) or bool(unclassified)
    if before_unclassified:
        before, before_total = {}, ZERO
    rows = []
    if sleeves:
        for sleeve in sleeves:
            value = values.get(sleeve["id"], ZERO)
            weight = value / total if total and not unclassified else None
            prior = before.get(sleeve["id"], ZERO) / before_total if before_total else None
            drift = None if weight is None else weight - Decimal(str(sleeve["target"]))
            rows.append({"sleeve": sleeve["id"], "name": sleeve.get("name"), "value": money(value),
                         "weight": _ratio(weight, "0.0001"), "target": sleeve["target"], "min": sleeve["min"],
                         "max": sleeve["max"], "drift": _ratio(drift, "0.0001"),
                         "drift_at_start": _ratio(None if prior is None else prior - Decimal(str(sleeve["target"])), "0.0001"),
                         "outside_band": None if weight is None or incomplete else policy._outside(sleeve, float(weight))})
    else:
        missing.append(_miss("policy.ips", "No accepted IPS: weights are shown by asset class without targets or bands."))
        for name, value in sorted(values.items()):
            rows.append({"sleeve": name, "value": money(value), "weight": _ratio(value / total if total else None, "0.0001")})
    data = {"currency": book.currency, "as_of": end, "portfolio_value": money(total) if not incomplete else None,
            "known_value": money(total), "sleeves": rows, "unclassified": sorted(set(unclassified)),
            "policy": {"decision_id": (ips or {}).get("decision_id"), "profile": ((ips or {}).get("risk") or {}).get("profile")}}
    return _section(data, missing=missing, assumptions=[
        "Portfolio = investment accounts (brokerage, PPR, retirement); bank accounts, cards, loans and the AFORE are "
        "outside the IPS allocation. Cash inside investment accounts is the cash sleeve.",
        "Bands are the IPS ranges: a sleeve of 20% or more may drift +/-5 points; a smaller sleeve +/-25% of its target."])


# ------------------------------------------------------------------ cash flow


def _prior_period(start: str, end: str) -> tuple[str, str]:
    """The window before: the same number of months; for a quarter to date, the same stretch of the
    quarter before (1 Jul-31 Aug compares with 1 Apr-31 May), so the comparison is like for like."""
    first, last = date.fromisoformat(start), date.fromisoformat(end)
    if _to_date(first, last):
        return add_months(first, -3).isoformat(), add_months(last, -3).isoformat()
    whole_months = first.day == 1 and (last + timedelta(days=1)).day == 1
    if whole_months:
        months = (last.year - first.year) * 12 + last.month - first.month + 1
        return add_months(first, -months).isoformat(), _day_before(start)
    # Any other window: the same number of days, ending the day before (15 Jan-14 Apr compares with the
    # 90 days before it, never a four-month window).
    days = (last - first).days + 1
    return (first - timedelta(days=days)).isoformat(), _day_before(start)


def _to_date(first: date, last: date) -> bool:
    """A period that starts on a quarter's first day and ends before that quarter closes."""
    return first.day == 1 and first.month % 3 == 1 and last < _quarter_end(first)


def _quarter_end(day: date) -> date:
    quarter = (day.month - 1) // 3 + 1
    return (date(day.year + 1, 1, 1) if quarter == 4 else date(day.year, 3 * quarter + 1, 1)) - timedelta(days=1)


def _coverage(book: _Book, start: str, end: str) -> str:
    dates = [e["date"] for e in book.ledger.get("entries", []) if book.account_type(e["account_id"]) in _SPENDING_TYPES]
    if not dates or min(dates) > end or max(dates) < start:
        return "none"  # nothing inside the window: income and spending are unknown, not zero
    return "full" if min(dates) <= start else "partial"


def _flow_window(book: _Book, start: str, end: str) -> tuple[dict | None, list, list]:
    coverage = _coverage(book, start, end)
    if coverage == "none":
        return None, [_miss(f"ledger.bank@{start}..{end}", "No bank or card activity in the ledger for this window.")], []
    spending = cashflow.spending_report(book.ledger, start, end, book.currency)
    income = cashflow.income_report(book.ledger, start, end, book.currency)
    by_category: dict[str, Decimal] = {}
    for month in spending["result"]["months"].values():
        for category, amount in month["by_category"].items():
            by_category[category] = by_category.get(category, ZERO) + Decimal(amount)
    income_total = sum((Decimal(v) for m in income["result"]["months"].values() for v in m.values()), ZERO)
    spend_total = Decimal(spending["result"]["total"])
    rate = None if income_total <= 0 else (income_total - spend_total) / income_total
    warnings = [] if coverage == "full" else [f"The ledger's bank activity starts inside {start}..{end}; totals cover part of it."]
    return {"start": start, "end": end, "coverage": coverage, "income": money(income_total),
            "spending": money(spend_total), "net": money(income_total - spend_total),
            "savings_rate": _ratio(rate, "0.0001"), "by_category": by_category,
            "essential": money(sum((Decimal(m["essential"]) for m in spending["result"]["months"].values()), ZERO)),
            "discretionary": money(sum((Decimal(m["discretionary"]) for m in spending["result"]["months"].values()), ZERO))
            }, spending["missing"] + income["missing"], warnings + spending["warnings"]


def _cash_flow(book: _Book, start: str, end: str) -> dict:
    current, missing, warnings = _flow_window(book, start, end)
    prior_start, prior_end = _prior_period(start, end)
    prior, prior_missing, _ = _flow_window(book, prior_start, prior_end)
    if current is None:
        return _section({"currency": book.currency, "current": None, "prior": None}, missing=missing, status="needs_input")
    categories = sorted(current.pop("by_category").items(), key=lambda kv: (-kv[1], kv[0]))
    prior_categories = prior.pop("by_category") if prior else {}
    top = [{"category": c, "amount": money(v),
            "prior": money(prior_categories.get(c, ZERO)) if prior else None,
            "change": money(v - prior_categories.get(c, ZERO)) if prior else None,
            "share": _ratio(v / Decimal(current["spending"]) if Decimal(current["spending"]) else None, "0.0001")}
           for c, v in categories[:5]]
    change = None
    if prior:
        change = {k: money(Decimal(current[k]) - Decimal(prior[k])) for k in ("income", "spending", "net")}
        change["savings_rate"] = _ratio(None if current["savings_rate"] is None or prior["savings_rate"] is None
                                        else Decimal(current["savings_rate"]) - Decimal(prior["savings_rate"]), "0.0001")
    else:
        missing = missing + [_miss(f"ledger.bank@{prior_start}..{prior_end}",
                                   "The prior quarter is not in the ledger, so there is no comparison.")]
    return _section({"currency": book.currency, "current": current, "prior": prior, "change": change,
                     "top_categories": top}, missing=missing + prior_missing, warnings=warnings, assumptions=[
        "Income and spending come from categorised bank and card entries; transfers, investments and loan payments "
        "are excluded.", "Savings rate = (income - spending) / income for the window.",
        f"The prior window is {prior_start}..{prior_end}."])


# ------------------------------------------------------------------ goals


def _months_left(today: date, when: date) -> int:
    """Months of contributions left before ``when``, counting days: 1 Sep to 30 Sep is 1, never 0 ("due").

    The smallest n with today + n months on or after the date; 0 only once the date has come.
    """
    if when <= today:
        return 0
    months = (when.year - today.year) * 12 + when.month - today.month
    return months + 1 if add_months(today, months) < when else months


def _goals(book: _Book, end: str, sit: Mapping[str, Any] | None, goal_accounts: Mapping[str, Any],
           assumed_return: Decimal) -> dict:
    if sit is None:
        return _section({"goals": []}, missing=[_miss("goals", "No saved picture, so no goals.")], status="needs_input")
    rows, missing = [], []
    positions, pos_missing = (book.positions(end) if goal_accounts else ([], []))
    today = date.fromisoformat(end)
    for goal in sit.get("goals") or []:
        if goal.get("status") != "active" or goal.get("action") == "pay_off":
            continue
        target, monthly = _d(goal.get("target_amount")), _d(goal.get("monthly_contribution"))
        funded, basis = None, None
        accounts = goal_accounts.get(goal["id"])
        if accounts:
            values = [r["value"] for r in positions if r["account_id"] in accounts and not (r["kind"] == "cash" and (r["value"] or 0) < 0)]
            funded, basis = _sum(values), f"value of accounts {', '.join(accounts)} on {end}"
            if funded is None:
                missing += pos_missing
            elif goal.get("currency") not in (None, book.currency):
                funded = None
                missing.append(_miss(f"goals.{goal['id']}.currency", "Goal currency differs from the reporting currency."))
        else:
            amount, keys, fx_gap = policy._funded(goal, sit)
            if keys and fx_gap is None:
                funded, basis = _d(round(amount, 2)), "cash earmarked goal:" + goal["id"]
            elif fx_gap:
                missing.append(_miss(fx_gap, f"Cannot convert earmarked cash for {goal['name']}."))
            else:
                missing.append(_miss(f"goal_accounts.{goal['id']}",
                                     f"Nothing is earmarked for {goal['name']}; name its accounts to measure progress."))
        when = date.fromisoformat(goal["target_date"]) if goal.get("target_date") else None
        months = _months_left(today, when) if when else None
        row = {"id": goal["id"], "name": goal["name"], "currency": goal.get("currency"), "target": money(target),
               "target_date": goal.get("target_date"), "months_left": months, "funded": money(funded),
               "funded_basis": basis, "funded_pct": _ratio(funded / target if funded is not None and target else None, "0.0001"),
               "monthly_contribution": money(monthly), "required_monthly": None, "required_return": None,
               "status": "unknown"}
        if target is None:
            missing.append(_miss(f"goals.{goal['id']}.target_amount", f"{goal['name']} has no target amount."))
        elif months is None:
            missing.append(_miss(f"goals.{goal['id']}.target_date", f"{goal['name']} has no target date."))
        elif funded is not None:
            if funded >= target:
                row["status"] = "reached"
            elif months <= 0:
                row["status"] = "due_short"
            else:
                m = (1 + float(assumed_return)) ** (1 / 12) - 1
                growth = (1 + m) ** months
                needed = (float(target) - float(funded) * growth) * m / (growth - 1) if m else (float(target) - float(funded)) / months
                row["required_monthly"] = money(Decimal(str(max(needed, 0.0))))
                if monthly is not None:
                    solved = policy.required_return(float(target), float(funded), float(monthly), months)
                    row["required_return"] = _ratio(solved["rate"]) if solved.get("rate") is not None else None
                    row["status"] = "on_track" if monthly >= Decimal(str(max(needed, 0.0))) - CENT else "behind"
                    row["gap_monthly"] = money(max(Decimal(str(needed)) - monthly, ZERO))
                else:
                    missing.append(_miss(f"goals.{goal['id']}.monthly_contribution",
                                         f"{goal['name']} has no monthly contribution, so pace cannot be judged."))
        rows.append(row)
    return _section({"goals": rows, "assumed_return": _ratio(assumed_return, "0.0001")}, missing=missing, assumptions=[
        f"On track = the stated monthly contribution is at least the monthly amount that reaches the target at "
        f"{float(assumed_return):.1%} a year, compounding monthly (a planning assumption, not a forecast).",
        "Funded = the value of the goal's named accounts, or cash saved with purpose goal:<id>; with neither it is unknown."])


# ------------------------------------------------------------------ decision journal


def _decisions(book: _Book, start: str, end: str, snapshot: Mapping[str, Any]) -> dict:
    entries, _ = active_entries(book.ledger)
    rows, carried, missing = [], [], []
    for decision in snapshot.get("decisions") or []:
        created = str(decision.get("created_at") or "")[:10]
        updated = str(decision.get("updated_at") or "")[:10]
        in_period = start <= created <= end or (decision.get("status") != "proposed" and start <= updated <= end)
        if not in_period:
            if decision.get("status") == "proposed" and created < start:
                carried.append({"id": decision["id"], "title": decision["title"], "proposed_on": created})
            continue
        decided_on = updated if decision.get("status") in {"accepted", "dismissed"} else None
        since = decided_on or created
        trades = [e for e in entries if since <= e["date"] <= end and e["kind"] in {"buy", "sell"}]
        invested, unknown = ZERO, False
        for trade in trades:
            value = book.convert(-Decimal(trade["amount"]), trade["currency"], trade["date"])
            if value is None:
                unknown = True
                missing.append(_miss(f"fx.{trade['currency']}/{book.currency}@{trade['date']}",
                                     f"Cannot convert trade {trade['id']}."))
            else:
                invested += value
        rows.append({
            "id": decision["id"], "what": decision["title"], "why": decision["rationale"],
            "status": decision.get("status"), "proposed_on": created, "decided_on": decided_on,
            "needs_review": decision.get("needs_review", False), "review_reasons": decision.get("review_reasons") or [],
            "what_happened": {"since": since, "trades": len(trades),
                              "instruments": sorted({t.get("instrument_id") for t in trades if t.get("instrument_id")}),
                              "net_invested": None if unknown else money(invested),
                              "days_since": (date.fromisoformat(end) - date.fromisoformat(since)).days},
        })
    rows.sort(key=lambda r: (r["proposed_on"], r["id"]))
    counts: dict[str, int] = {}
    for row in rows:
        counts[row["status"]] = counts.get(row["status"], 0) + 1
    return _section({"decisions": rows, "counts": counts, "still_open_from_before": carried}, missing=missing,
                    sources=[{"title": "Decision journal (store decisions and their status events)"}], assumptions=[
        "What happened = the ledger's buys and sells from the decision date to the period end (net invested is buys "
        "minus sales, fees included); the ledger does not say which trade a decision caused."])


# ------------------------------------------------------------------ DCA


def _plans(snapshot: Mapping[str, Any], supplied: Any) -> list:
    raw = supplied
    if raw is None:
        fact = next((f for f in snapshot.get("facts") or [] if f.get("key") == dca_module.FACT_KEY
                     and f.get("status", "active") == "active"), None)
        raw = fact.get("value") if fact else None
    if isinstance(raw, Mapping) and isinstance(raw.get("plans"), list):
        return list(raw["plans"])
    if isinstance(raw, list):
        return list(raw)
    return [raw] if isinstance(raw, Mapping) and raw.get("id") else []


def _dca(book: _Book, start: str, end: str, plans: list) -> dict:
    if not plans:
        return _section({"plans": []}, status="ready", assumptions=["No DCA plan is saved; nothing to measure."])
    rows, missing, warnings = [], [], []
    for plan in plans:
        try:
            report = dca_module.adherence(book.ledger, plan, end)
        except ValueError as exc:
            warnings.append(f"DCA plan {plan.get('id') if isinstance(plan, Mapping) else '?'} is not valid: {exc}")
            continue
        due = [i for i in report["result"]["installments"] if start <= i["due"] <= end]
        counts: dict[str, int] = {}
        for item in due:
            counts[item["state"]] = counts.get(item["state"], 0) + 1
        judged = sum(v for k, v in counts.items() if k not in {"pending", "unknown"})
        planned = sum((Decimal(i["planned"]) for i in due), ZERO)
        invested = _sum(_d(i["invested"]) for i in due)
        rows.append({"plan_id": report["result"]["plan_id"], "currency": report["result"]["currency"],
                     "installments": len(due), "counts": counts,
                     "on_time_rate": _ratio(Decimal(counts.get("on_time", 0)) / judged if judged else None, "0.0001"),
                     "planned": money(planned), "invested": money(invested),
                     "shortfall": money(None if invested is None else max(planned - invested, ZERO)),
                     "missed": [i["due"] for i in due if i["state"] in {"skipped", "partial"}],
                     "next_due": report["result"]["next_due"]})
        missing += report["missing"]
    return _section({"plans": rows}, missing=missing, warnings=warnings,
                    assumptions=["Installments due inside the period only; see the dca task for the full history."])


# ------------------------------------------------------------------ realised gains and tax


def _jurisdiction(tax: Mapping[str, Any], sit: Mapping[str, Any] | None) -> tuple[str | None, str]:
    if tax.get("jurisdiction") in {"MX", "US"}:
        return tax["jurisdiction"], "stated in the request"
    profile = (sit or {}).get("profile") or {}
    if profile.get("tax_residence"):
        return profile["tax_residence"][0], "stated tax residence"
    if (profile.get("residence") or {}).get("country"):
        return profile["residence"]["country"], "country of residence (tax residence assumed there)"
    return None, "unknown"


def _tax_estimate(sales: list[dict], jurisdiction: str | None, tax: Mapping[str, Any], sic_listed: Mapping[str, Any],
                  label: str) -> tuple[dict, list]:
    missing = []
    if jurisdiction == "MX":
        regimes: dict[str, Decimal | None] = {"art129_10pct": ZERO, "progressive": ZERO}
        unclassified = []
        for sale in sales:
            venue = sale.get("venue")
            listed = sic_listed.get(sale["instrument_id"])
            regime = "art129_10pct" if venue in _ART129_VENUES or listed is True else \
                "progressive" if listed is False else None
            if regime is None:
                unclassified.append(sale["instrument_id"])
                continue
            gain = _d(sale.get("gain_mxn"))
            regimes[regime] = None if gain is None or regimes[regime] is None else regimes[regime] + gain
        for instrument in sorted(set(unclassified)):
            missing.append(_miss(f"sic_listed.{instrument}",
                                 f"{instrument} was sold outside a Mexican venue; say whether it is listed in the SIC "
                                 "(10% definitive) or not (progressive)."))
        art129 = regimes["art129_10pct"]
        rate = _d(tax.get("mx_marginal_rate"))
        progressive = regimes["progressive"]
        if progressive and rate is None:
            missing.append(_miss("tax.mx_marginal_rate", "Gains outside the SIC are taxed at the progressive rate."))
        tax_129 = None if art129 is None else max(art129, ZERO) * Decimal("0.10")
        tax_prog = None if progressive is None else (ZERO if progressive <= 0 else None if rate is None else progressive * rate)
        total = None if unclassified else _sum([tax_129, tax_prog])
        for key, value in (("art129", art129), ("progressive", progressive)):
            if value is None:
                missing.append(_miss(f"fx.MXN.{label}.{key}", "A sale lacks an MXN rate or a known basis."))
        return {"currency": "MXN", "gains": {"art129_10pct": money(art129), "progressive": money(progressive)},
                "estimated_tax": {"art129_10pct": money(tax_129), "progressive": money(tax_prog), "total": money(total)}}, missing
    if jurisdiction == "US":
        terms: dict[str, Decimal | None] = {"short": ZERO, "long": ZERO}
        for sale in sales:
            term = sale.get("holding")
            gain = _d(sale.get("gain_usd"))
            if term not in terms:
                missing.append(_miss(f"lots.{sale['lot_id']}.acquired_on", "Holding period unknown."))
                terms = {k: None for k in terms}
                break
            terms[term] = None if gain is None or terms[term] is None else terms[term] + gain
        rates = tax.get("us_rates") if isinstance(tax.get("us_rates"), Mapping) else {}
        estimate = {}
        for term, key in (("short", "short_term"), ("long", "long_term")):
            rate, gain = _d(rates.get(key)), terms[term]
            if gain is not None and gain > 0 and rate is None:
                missing.append(_miss(f"tax.us_rates.{key}", f"The {key.replace('_', '-')} rate is needed for the estimate."))
            estimate[key] = None if gain is None else ZERO if gain <= 0 else None if rate is None else gain * rate
        return {"currency": "USD", "gains": {"short_term": money(terms["short"]), "long_term": money(terms["long"])},
                "estimated_tax": {**{k: money(v) for k, v in estimate.items()}, "total": money(_sum(estimate.values()))}}, missing
    return {"gains": None, "estimated_tax": None}, [_miss("tax.jurisdiction", "Tax residence (MX or US) is unknown.")]


def _taxes(book: _Book, start: str, end: str, jurisdiction: str | None, basis: str, tax: Mapping[str, Any],
           sic_listed: Mapping[str, Any]) -> dict:
    report = realized_gains(book.ledger, as_of=end)
    sales = report["result"]["sales"]
    period = [s for s in sales if start <= s["date"] <= end]
    ytd = [s for s in sales if s["date"][:4] == end[:4] and s["date"] <= end]
    period_est, period_missing = _tax_estimate(period, jurisdiction, tax, sic_listed, "period")
    ytd_est, ytd_missing = _tax_estimate(ytd, jurisdiction, tax, sic_listed, "ytd")
    # No sales is only "no tax" when the ledger covered the holdings the whole window; before the first
    # statement a sale could have happened unseen, so the estimate is unknown, not zero.
    for window_start, rows, est, gaps in ((start, period, period_est, period_missing),
                                          (end[:4] + "-01-01", ytd, ytd_est, ytd_missing)):
        if not rows and est.get("estimated_tax") and not (book.known_at(_day_before(window_start), portfolio=True)
                                                          and book.active_in(window_start, end, portfolio=True)):
            est["estimated_tax"] = {k: None for k in est["estimated_tax"]}
            est["gains"] = {k: None for k in (est.get("gains") or {})}
            gaps.append(_miss(f"ledger.trades@{window_start}..{end}",
                              f"The investment statements do not cover {window_start}..{end}, so sales in that "
                              "window are unknown; the estimate is unknown, not zero.", "no_data"))
    state, _ = replay(book.ledger, end)
    income: dict[str, Decimal | None] = {}
    missing = list(report["missing"])
    for item in state.income:
        if not start <= item["date"] <= end:
            continue
        value = book.convert(item["amount"], item["currency"], item["date"])
        if value is None:
            missing.append(_miss(f"fx.{item['currency']}/{book.currency}@{item['date']}", f"Cannot convert {item['kind']}."))
        prior = income.get(item["kind"], ZERO)
        income[item["kind"]] = None if value is None or prior is None else prior + value
    assumptions = ["Nominal gains per lot (FIFO) at trade-date FX. Estimates, not a return: the tax task applies "
                   "Mexican average cost with INPC update and US wash sales."]
    if jurisdiction == "MX":
        assumptions.append("Mexico: gains on BMV/BIVA/SIC-listed securities at 10% definitive (LISR Art. 129), netted "
                           "within the window; other foreign securities at the stated marginal rate (Title IV Ch. IV).")
    elif jurisdiction == "US":
        assumptions.append("US: short- and long-term gains netted separately at the stated rates; state tax, NIIT and "
                           "loss carryovers are not applied here.")
    return _section({"jurisdiction": jurisdiction, "jurisdiction_basis": basis,
                     "period": {"sales": len(period), **period_est},
                     "year_to_date": {"sales": len(ytd), **ytd_est},
                     "investment_income_period": {k: money(v) for k, v in sorted(income.items())},
                     "currency": book.currency},
                    missing=missing + period_missing + ytd_missing, sources=report["sources"], assumptions=assumptions,
                    warnings=report["warnings"])


# ------------------------------------------------------------------ fee audit


def parse_rate(raw: Any, field: str, source: Any = None) -> tuple[Decimal | None, str | None, dict | None]:
    """An annual cost rate as a decimal, its source, or why it is unknown.

    ``raw`` is ``{value, unit: decimal|percent|bps, source}`` or a bare number
    with a separate ``source``.  A bare number is a decimal only below 0.03;
    anything else without a unit is ambiguous (0.2 could be 0.2% or 20%).
    """
    unit = None
    if isinstance(raw, Mapping):
        unit, source, raw = raw.get("unit"), raw.get("source", source), raw.get("value")
    if raw is None:
        return None, None, _miss(field, "No rate supplied.")
    value = _d(raw)
    if value is None or value < 0:
        raise ValueError(f"{field} must be a nonnegative number")
    if not isinstance(source, str) or not source.strip():
        return None, None, _miss(f"{field}.source", "A rate is used only with its source (fund factsheet, contract).",
                                 "unsourced")
    if unit is None:
        if value >= Decimal("0.03"):
            return None, None, _miss(f"{field}.unit", f"{raw} is ambiguous (decimal or percent); give unit "
                                                      "decimal|percent|bps.", "ambiguous")
        unit = "decimal"
    scale = {"decimal": Decimal(1), "percent": Decimal("0.01"), "bps": Decimal("0.0001")}.get(unit)
    if scale is None:
        raise ValueError(f"{field}.unit must be decimal, percent or bps")
    rate = value * scale
    if rate >= 1:
        raise ValueError(f"{field} is 100% or more a year")
    return rate, source.strip(), None


def afore_rate(name: Any, year: int) -> tuple[Decimal | None, dict | None, dict | None]:
    """CONSAR commission for an AFORE and year; fails closed when either is unknown."""
    table = AFORE_COMMISSIONS.get(year)
    if table is None or table.get("status") != "verified":
        return None, None, _miss(f"afore.commission@{year}", f"No verified CONSAR commission table for {year}.")
    key = fold(str(name or ""))
    key = table["aliases"].get(key, key)
    if key not in table["rates"]:
        return None, None, _miss("afore.name", f"AFORE {name!r} is not in the CONSAR {year} table; name the AFORE.")
    return Decimal(table["rates"][key]), {**table["source"], "checked_on": table["checked_on"]}, None


def _compound_gap(base: Decimal, gap: Decimal, returns: tuple[Decimal, Decimal], years: int) -> dict:
    out = []
    for r in returns:
        with_fee = float(base) * (1 + float(r) - float(gap)) ** years
        without = float(base) * (1 + float(r)) ** years
        out.append(Decimal(str(without - with_fee)))
    return {"low": money(min(out)), "high": money(max(out))}


def _index_of(meta: Mapping[str, Any]) -> str | None:
    for field in ("underlying_symbol", "symbol"):
        symbol = str(meta.get(field) or "").upper().split(".")[0]
        if symbol in UNDERLYING:
            return UNDERLYING[symbol]
    return meta.get("index")


def audit(holdings: Sequence[Mapping[str, Any]], instruments: Mapping[str, Mapping[str, Any]],
          accounts: Sequence[Mapping[str, Any]], ledger: Mapping[str, Any] | None = None, *, currency: str,
          as_of: str, window_start: str | None = None, residence: str | None = None,
          advisory: Sequence[Mapping[str, Any]] = (), cash_reference_rate: Mapping[str, Any] | None = None,
          cash_yield: Any = None, alternatives: Sequence[Mapping[str, Any]] = (),
          return_range: Sequence[Any] = ("0.04", "0.07"), afore: Sequence[Mapping[str, Any]] = ()) -> dict:
    """All-in annual cost of the portfolio (see module docstring).

    ``holdings``: ``[{account_id, instrument_id, value, kind?: position|cash}]`` in ``currency`` (value None =
    unknown).  ``instruments``: ``{id: {symbol, underlying_symbol?, issuer_domicile?, venue?, expense_ratio?:
    {value, unit?, source}, expense_ratio_source?}}``.  ``accounts``: ``[{id, type, afore_name?, institution?}]``.
    ``ledger`` supplies commission and IVA fee entries between ``window_start`` (default one year before
    ``as_of``) and ``as_of``.
    """
    _date(as_of, "as_of")
    window_start = window_start or (date.fromisoformat(as_of) - timedelta(days=364)).isoformat()
    _date(window_start, "window_start")
    window_days = (date.fromisoformat(as_of) - date.fromisoformat(window_start)).days + 1
    annualize = Decimal(365) / Decimal(window_days)
    account_meta = {a["id"]: a for a in accounts if isinstance(a, Mapping) and a.get("id")}
    fx = FxTable((ledger or {}).get("fx", []), 5)
    missing: list[dict] = []
    sources: list[dict] = []
    components: list[dict] = []
    assessed = ZERO
    assessed_known = True

    # fund expense ratios
    fund_cost, fund_complete, fund_rows = ZERO, True, []
    idle_cash = ZERO
    portfolio: list[Mapping[str, Any]] = []
    for index, holding in enumerate(holdings):
        value = _d(holding.get("value"))
        account_type = str((account_meta.get(holding.get("account_id")) or {}).get("type") or "")
        if account_type in _NON_PORTFOLIO_TYPES:
            continue
        portfolio.append(holding)
        if value is None:
            assessed_known = False
            missing.append(_miss(f"holdings[{index}].value", f"{holding.get('instrument_id')} has no value."))
            continue
        if value <= 0:
            continue
        assessed += value
        instrument = str(holding.get("instrument_id") or "")
        if holding.get("kind") == "cash" or instrument.startswith("cash:"):
            idle_cash += value
            continue
        meta = instruments.get(instrument) or {}
        rate, source, problem = parse_rate(meta.get("expense_ratio"), f"instruments.{instrument}.expense_ratio",
                                           meta.get("expense_ratio_source"))
        if problem:
            problem["detail"] = f"{meta.get('symbol') or instrument}: {problem['detail']}"
            missing.append(problem)
            fund_complete = False
        else:
            sources.append({"title": f"Expense ratio {meta.get('symbol') or instrument}", "ref": source})
        cost = None if rate is None else value * rate
        if cost is not None:
            fund_cost += cost
        fund_rows.append({"account_id": holding.get("account_id"), "instrument_id": instrument,
                          "symbol": meta.get("symbol") or instrument, "value": money(value),
                          "expense_ratio": _ratio(rate), "annual_cost": money(cost), "source": source})
    components.append({"id": "fund_expenses", "name": "Fund expense ratios", "annual_low": money(fund_cost),
                       "annual_high": money(fund_cost), "complete": fund_complete, "detail": fund_rows})

    # broker commissions and IVA from the ledger
    commission = iva = ZERO
    trading_rows, trading_complete = [], ledger is not None
    if ledger is None:
        missing.append(_miss("ledger", "No ledger, so commissions and IVA paid are unknown."))
    else:
        entries, _ = active_entries(ledger)
        for entry in entries:
            if not window_start <= entry["date"] <= as_of:
                continue
            if str((account_meta.get(entry["account_id"]) or {}).get("type") or "") in _SPENDING_TYPES:
                continue  # bank and card fees are spending (cash flow), not investment costs
            text = fold(" ".join(str(entry.get(k) or "") for k in ("description", "subtype", "memo", "category")))
            if entry["kind"] == "fee" and any(w in text for w in ("advis", "asesor", "management", "wrap", "gestion")):
                continue  # counted under advisory
            if entry["kind"] == "fee":
                amount = -Decimal(entry["amount"])
                ccy = entry["currency"]
            elif entry["kind"] in {"buy", "sell"} and entry.get("fee") is not None:
                amount = Decimal(entry["fee"])
                ccy = entry["currency"]
            else:
                continue
            value = fx.convert(amount, ccy, currency, entry["date"])
            if value is None:
                trading_complete = False
                missing.append(_miss(f"fx.{ccy}/{currency}@{entry['date']}", f"Cannot convert fee {entry['id']}."))
                continue
            is_iva = "iva" in text.split() or "vat" in text.split()
            if is_iva:
                iva += value
            else:
                commission += value
            trading_rows.append({"entry_id": entry["id"], "date": entry["date"], "kind": "iva" if is_iva else "commission",
                                 "amount": money(value)})
    trading_annual = (commission + iva) * annualize
    components.append({"id": "trading", "name": "Broker commissions + IVA", "paid_in_window": money(commission + iva),
                       "commission": money(commission), "iva": money(iva), "window": [window_start, as_of],
                       "annual_low": money(trading_annual) if ledger is not None else None,
                       "annual_high": money(trading_annual) if ledger is not None else None,
                       "complete": trading_complete, "detail": trading_rows})

    # AFORE comisión
    year = int(as_of[:4])
    afore_rows, afore_cost, afore_complete = [], ZERO, True
    afore_items = list(afore)
    listed = {a.get("account_id") for a in afore_items}
    for account in accounts:
        if str(account.get("type") or "") == "afore" and account.get("id") not in listed:
            value = _sum(_d(h.get("value")) for h in holdings if h.get("account_id") == account["id"])
            afore_items.append({"account_id": account["id"], "name": account.get("afore_name") or account.get("institution"),
                                "balance": value})
    for item in afore_items:
        balance = _d(item.get("balance"))
        rate, source, problem = afore_rate(item.get("name"), year)
        if problem:
            missing.append(problem)
            afore_complete = False
        elif source not in sources:
            sources.append(source)
        if balance is None:
            missing.append(_miss(f"afore.{item.get('account_id') or item.get('name')}.balance", "AFORE balance unknown."))
            afore_complete = False
        cost = None if rate is None or balance is None else balance * rate
        if balance is not None:
            assessed += balance
        if cost is not None:
            afore_cost += cost
        afore_rows.append({"account_id": item.get("account_id"), "afore": item.get("name"), "balance": money(balance),
                           "commission": _ratio(rate), "annual_cost": money(cost)})
    if afore_rows:
        components.append({"id": "afore", "name": "AFORE comisión", "annual_low": money(afore_cost),
                           "annual_high": money(afore_cost), "complete": afore_complete, "detail": afore_rows})

    # advisory / wrap
    advisory_rows, advisory_cost, advisory_complete = [], ZERO, True
    for index, item in enumerate(advisory):
        rate, source, problem = parse_rate(item.get("rate"), f"advisory[{index}].rate", item.get("source"))
        scope = item.get("account_ids")
        base = _sum(_d(h.get("value")) for h in portfolio if scope is None or h.get("account_id") in scope)
        if problem:
            missing.append(problem)
            advisory_complete = False
        cost = None if rate is None or base is None else base * rate
        if cost is not None:
            advisory_cost += cost
        advisory_rows.append({"name": item.get("name"), "account_ids": scope, "rate": _ratio(rate), "base": money(base),
                              "annual_cost": money(cost), "source": source})
    if ledger is not None:
        for entry in active_entries(ledger)[0]:
            text = fold(" ".join(str(entry.get(k) or "") for k in ("description", "subtype", "memo", "category")))
            if entry["kind"] == "fee" and window_start <= entry["date"] <= as_of and \
                    any(w in text for w in ("advis", "asesor", "management", "wrap", "gestion")) and not advisory:
                value = fx.convert(-Decimal(entry["amount"]), entry["currency"], currency, entry["date"])
                if value is None:
                    advisory_complete = False
                    continue
                advisory_cost += value * annualize
                advisory_rows.append({"entry_id": entry["id"], "date": entry["date"], "paid": money(value)})
    if advisory_rows:
        components.append({"id": "advisory", "name": "Advisory / wrap fees", "annual_low": money(advisory_cost),
                           "annual_high": money(advisory_cost), "complete": advisory_complete, "detail": advisory_rows})

    # cash drag
    drag_low = drag_high = None
    rate_warnings: list[str] = []
    if idle_cash > 0:
        if cash_reference_rate is None:
            missing.append(_miss("cash_reference_rate", "A reference rate {low, high, source} (e.g. CETES 28 days) is "
                                                        "needed to price idle cash."))
        else:
            low, _, p1 = parse_rate({"value": cash_reference_rate.get("low"), "unit": cash_reference_rate.get("unit"),
                                     "source": cash_reference_rate.get("source")}, "cash_reference_rate.low")
            high, _, p2 = parse_rate({"value": cash_reference_rate.get("high", cash_reference_rate.get("low")),
                                      "unit": cash_reference_rate.get("unit"), "source": cash_reference_rate.get("source")},
                                     "cash_reference_rate.high")
            earned = _d(cash_yield)
            if p1 or p2:
                missing += [p for p in (p1, p2) if p]
            else:
                sources.append({"title": "Cash reference rate", "ref": cash_reference_rate.get("source"),
                                **({"date": cash_reference_rate["as_of"]} if cash_reference_rate.get("as_of") else {})})
                if cash_reference_rate.get("stale"):
                    from .rates import REFERENCE_RATE_STALE_DAYS
                    rate_warnings.append(f"The cash reference rate is from {cash_reference_rate.get('as_of')}, over "
                                         f"{REFERENCE_RATE_STALE_DAYS} days old; the idle-cash cost uses it until a "
                                         "newer rate is known.")
                if earned is None:
                    drag_low, drag_high = ZERO, idle_cash * max(low, high)
                else:
                    drag_low, drag_high = idle_cash * max(min(low, high) - earned, ZERO), idle_cash * max(max(low, high) - earned, ZERO)
        components.append({"id": "cash_drag", "name": "Idle cash vs reference rate", "idle_cash": money(idle_cash),
                           "annual_low": money(drag_low), "annual_high": money(drag_high),
                           "complete": drag_low is not None,
                           "note": "A range: low if the cash already earns the reference rate, high if it earns nothing."
                           if cash_yield is None else "Reference rate minus the stated cash yield."})

    known_low = sum((_d(c["annual_low"]) or ZERO for c in components), ZERO)
    known_high = sum((_d(c["annual_high"]) or ZERO for c in components), ZERO)
    complete = all(c["complete"] for c in components) and assessed_known
    ranked = sorted((c for c in components if c.get("annual_high") is not None),
                    key=lambda c: (-(_d(c["annual_low"]) + _d(c["annual_high"])) / 2, c["id"]))
    top = [{"id": c["id"], "name": c["name"], "annual_low": c["annual_low"], "annual_high": c["annual_high"],
            "bps_low": _bps(_d(c["annual_low"]), assessed), "bps_high": _bps(_d(c["annual_high"]), assessed)}
           for c in ranked if (_d(c["annual_high"]) or ZERO) > 0][:3]

    # cheaper equivalents (neutral)
    suggestions, gap = [], ZERO
    alt_rows = []
    for index, alt in enumerate(alternatives):
        rate, source, problem = parse_rate(alt.get("expense_ratio"), f"alternatives[{index}].expense_ratio",
                                           alt.get("expense_ratio_source"))
        alt_rows.append({**{k: alt.get(k) for k in ("symbol", "name", "issuer_domicile", "venue", "sic_listed")},
                         "index": _index_of(alt), "rate": rate, "source": source, "problem": problem})
    for row in fund_rows:
        meta = instruments.get(row["instrument_id"]) or {}
        index = _index_of(meta)
        current = _d(row["expense_ratio"])
        value = _d(row["value"])
        best = None
        for alt in alt_rows:
            if alt["index"] is None or alt["index"] != index or alt["symbol"] == row["symbol"] or alt["rate"] is None:
                continue
            if current is not None and alt["rate"] < current and (best is None or alt["rate"] < best["rate"]):
                best = alt
        if best is not None:
            saving = value * (current - best["rate"])
            gap += saving
            suggestions.append({"holding": row["symbol"], "index": index, "equivalent": best["symbol"],
                                "current_expense_ratio": row["expense_ratio"], "equivalent_expense_ratio": _ratio(best["rate"]),
                                "annual_difference": money(saving), "source": best["source"],
                                "note": "Same index, lower expense ratio; check tracking difference, spreads and tax "
                                        "on selling before changing."})
        domicile = (meta.get("issuer_domicile") or policy._domicile({"symbol": meta.get("symbol")}) or "").upper()
        if residence == "MX" and domicile == "US" and index in policy.UCITS_EQUIVALENT:
            ucits = policy.UCITS_EQUIVALENT[index]
            alt = next((a for a in alt_rows if (a["symbol"] or "").upper().startswith(ucits)), None)
            difference = None if alt is None or alt["rate"] is None or current is None else value * (current - alt["rate"])
            suggestions.append({
                "holding": row["symbol"], "index": index, "equivalent": ucits + " (Irish UCITS; SIC listing e.g. "
                                                                         + ucits + "N)" if ucits == "CSPX" else ucits + " (Irish UCITS)",
                "current_expense_ratio": row["expense_ratio"],
                "equivalent_expense_ratio": _ratio(alt["rate"]) if alt else None,
                "annual_difference": money(difference),
                "note": ("Same exposure through an Irish-domiciled UCITS: not US-situs for US estate tax (a non-US "
                         "person's US-situs assets above US$60k face rates up to 40%), accumulating share classes pay "
                         "no dividend until sale, and SIC-listed trades keep the 10% Art. 129 rate. Weigh the SIC "
                         "premium, whole-share lots and the tax on selling the current holding."),
                "kind": "estate_situs"})
            if alt is None or alt["rate"] is None:
                missing.append(_miss(f"alternatives.{ucits}.expense_ratio",
                                     f"Supply {ucits}'s expense ratio with its source to size the cost difference."))
    for alt in alt_rows:
        if alt["problem"] and alt["problem"]["reason"] != "missing":
            missing.append(alt["problem"])

    returns = tuple(Decimal(str(r)) for r in return_range)
    if len(returns) != 2:
        raise ValueError("return_range must be [low, high]")
    compounding = {"return_range": [_ratio(r, "0.0001") for r in returns], "fee_gap": None, "total_fees": None}
    if assessed > 0 and suggestions and gap > 0:
        g = gap / assessed
        compounding["fee_gap"] = {"annual": money(gap), "bps": _bps(gap, assessed),
                                  "10y": _compound_gap(assessed, g, returns, 10), "20y": _compound_gap(assessed, g, returns, 20)}
    if assessed > 0:
        low10, high10 = (_compound_gap(assessed, known_low / assessed, returns, 10),
                         _compound_gap(assessed, known_high / assessed, returns, 10))
        low20, high20 = (_compound_gap(assessed, known_low / assessed, returns, 20),
                         _compound_gap(assessed, known_high / assessed, returns, 20))
        compounding["total_fees"] = {"10y": {"low": low10["low"], "high": high10["high"]},
                                     "20y": {"low": low20["low"], "high": high20["high"]}, "complete": complete}
    if compounding["fee_gap"] is None:
        compounding["fee_gap_note"] = ("No same-exposure alternative with a sourced lower expense ratio was supplied, "
                                       "so the fee gap is unknown, not zero.")
    notes = []
    if afore_rows and year in AFORE_COMMISSIONS:
        notes.append("AFORE commissions for 2026 are 0.54% at nine AFOREs and 0.52% at PensionISSSTE (CONSAR); "
                     "compare AFOREs on net return (IRN) rather than commission.")
    result = {
        "currency": currency, "as_of": as_of, "assessed_value": money(assessed) if assessed_known else None,
        "known_assessed_value": money(assessed),
        "annual_cost": {"low": money(known_low), "high": money(known_high), "bps_low": _bps(known_low, assessed),
                        "bps_high": _bps(known_high, assessed), "complete": complete,
                        "note": None if complete else "Known components only: the total is a floor, not the full cost."},
        "components": components, "top_sources": top, "cheaper_equivalents": suggestions,
        "compounding": compounding, "notes": notes,
    }
    missing = _unique(missing)
    status = "partial" if missing else "ready"
    return {"status": status, "result": result, "missing": missing, "warnings": rate_warnings,
            "sources": sources, "assumptions": [
                "Annual cost = value x rate for expense ratios, AFORE and advisory rates (advisory on the investment "
                "accounts it names, else all of them); commissions and IVA are the investment accounts' ledger fee "
                f"lines in {window_start}..{as_of} scaled to a year. Bank and card fees belong to cash flow.",
                "Rates are used only with a source; a bare expense ratio is a decimal below 3%, anything else needs a unit.",
                "Compounding cost = the portfolio grown for 10 or 20 years without the fee minus with it, at the low and "
                "high return assumptions; a range, not a forecast. Contributions are not included.",
                "Equivalents are listed to compare, not recommendations to trade."]}


fees_audit = audit


# ------------------------------------------------------------------ next decisions


def _next_candidates(end: str, sections: Mapping[str, dict], sit: Mapping[str, Any] | None, book: _Book,
                     jurisdiction: str | None, tax: Mapping[str, Any], ppr: Mapping[str, Any] | None,
                     sic_listed: Mapping[str, Any]) -> tuple[list[dict], list[dict]]:
    next_start = date.fromisoformat(end) + timedelta(days=1)
    next_end = add_months(next_start, 3) - timedelta(days=1)
    year_end_next = next_start <= date(next_start.year, 12, 31) <= next_end
    candidates, missing = [], []

    reserve = (sit or {}).get("reserve") or {}
    gap = _d(reserve.get("gap"))
    if gap is not None and gap > 0:
        candidates.append({"kind": "reserve", "tier": 1, "value": gap,
                           "title": "Top up the emergency reserve",
                           "data": {"held": reserve.get("amount"), "target": reserve.get("target_amount"),
                                    "months": reserve.get("months"), "target_months": reserve.get("target_months"),
                                    "gap": reserve.get("gap"), "currency": reserve.get("currency")}})
    alloc = sections["allocation"]["data"]
    portfolio = _d(alloc.get("portfolio_value"))
    outside = [s for s in alloc.get("sleeves") or [] if s.get("outside_band")]
    if outside:
        # Money moved = half the total absolute drift across all sleeves (every sale funds a purchase).
        moved = None if portfolio is None else sum((abs(Decimal(s["drift"])) for s in alloc["sleeves"]
                                                    if s.get("drift") is not None), ZERO) * portfolio / 2
        candidates.append({"kind": "drift", "tier": 3, "value": moved,
                           "title": "Rebalance to the IPS ranges (new contributions first)",
                           "data": {"outside": [{k: s.get(k) for k in ("sleeve", "name", "weight", "target", "min",
                                                                       "max", "drift")} for s in outside],
                                    "amount_to_move": money(moved), "portfolio_value": alloc.get("portfolio_value"),
                                    "currency": book.currency}})

    # harvest: unrealised losses in taxable accounts
    rows, _ = book.positions(end)
    losses: dict[str, Decimal] = {}
    for row in rows:
        if row["kind"] != "position" or row["price"] is None or book.account_type(row["account_id"]) in _TAX_ADVANTAGED_TYPES:
            continue
        for lot in row["lots"]:
            if lot.basis is None or lot.currency != row["native_currency"]:
                continue
            value = book.convert(lot.quantity * row["price"], row["native_currency"], end)
            basis = book.convert(lot.basis, lot.currency, lot.acquired_on) if lot.acquired_on else None
            if value is None or basis is None:
                missing.append(_miss(f"fx.{lot.currency}/{book.currency}@{lot.acquired_on}",
                                     f"Cannot state lot {lot.id} of {row['instrument_id']} in {book.currency} at its purchase date."))
                continue
            pnl = value - basis
            if pnl < 0:
                losses[row["instrument_id"]] = losses.get(row["instrument_id"], ZERO) + pnl
    if losses:
        total_loss = -sum(losses.values(), ZERO)
        rate = None
        if jurisdiction == "MX":
            art129 = all(book.instruments.get(i, {}).get("venue") in _ART129_VENUES or sic_listed.get(i) is True for i in losses)
            rate = Decimal("0.10") if art129 else _d(tax.get("mx_marginal_rate"))
        elif jurisdiction == "US":
            rates = tax.get("us_rates") if isinstance(tax.get("us_rates"), Mapping) else {}
            rate = _d(rates.get("long_term"))
        value = None if rate is None else total_loss * rate
        tier = 2 if (jurisdiction == "US" and year_end_next) else 3 if jurisdiction == "MX" else 4
        candidates.append({"kind": "harvest", "tier": tier, "value": value,
                           "title": "Realise losses to offset gains",
                           "data": {"basis": f"Value on {end} minus cost at the purchase-date rate, in {book.currency}.",
                                    "unrealised_losses": {k: money(-v) for k, v in sorted(losses.items())},
                                    "total_loss": money(total_loss), "rate": _ratio(rate, "0.0001"),
                                    "tax_value_up_to": money(value), "currency": book.currency,
                                    "note": ("US wash-sale rules apply to repurchases within 30 days." if jurisdiction == "US"
                                             else "Losses offset gains of the same regime; confirm carry-forward with a contador.")}})
    if jurisdiction == "MX" and ppr is not None:
        from .mexico import PARAMETERS
        year = int(ppr.get("tax_year") or end[:4])
        entry = PARAMETERS["uma_annual_mxn"].get(year) or {}
        income, contributed = _d(ppr.get("accumulable_income_mxn")), _d(ppr.get("contributed_mxn"))
        if entry.get("status") != "verified":
            missing.append(_miss(f"parameters.uma_annual_mxn@{year}", "The annual UMA is not verified for that year."))
        elif income is None or contributed is None:
            missing.append(_miss("ppr.accumulable_income_mxn|contributed_mxn",
                                 "PPR room needs accumulable income and contributions already made this year."))
        else:
            cap = min(income * Decimal("0.10"), Decimal(entry["value"]) * 5)
            room = max(cap - contributed, ZERO)
            rate = _d(ppr.get("marginal_rate"))
            deadline = date(year, 12, 31)
            if room > 0:
                candidates.append({"kind": "ppr_headroom", "tier": 2 if next_start <= deadline <= next_end else 4,
                                   "value": None if rate is None else room * rate,
                                   "title": "Use the remaining PPR deduction room",
                                   "data": {"tax_year": year, "cap_mxn": money(cap), "contributed_mxn": money(contributed),
                                            "room_mxn": money(room), "marginal_rate": _ratio(rate, "0.0001"),
                                            "estimated_isr_saving_mxn": money(None if rate is None else room * rate),
                                            "deadline": deadline.isoformat(),
                                            "rule": "LISR Art. 151 fr. V: 10% of accumulable income, at most five annual UMAs."}})
    for goal in (sections["goals"]["data"] or {}).get("goals") or []:
        if goal["status"] == "behind":
            gap_m = _d(goal.get("gap_monthly"))
            candidates.append({"kind": "goal_pace", "tier": 3, "value": None if gap_m is None else gap_m * 12,
                               "title": f"Raise the contribution to {goal['name']} or move its date",
                               "data": {k: goal.get(k) for k in ("id", "name", "target", "target_date", "funded",
                                                                 "funded_pct", "monthly_contribution", "required_monthly",
                                                                 "gap_monthly", "currency")}})
    fees = sections["fees"]["data"] or {}
    gap_block = ((fees.get("audit") or {}).get("compounding") or {}).get("fee_gap")
    if gap_block:
        candidates.append({"kind": "fees", "tier": 4, "value": _d(gap_block["annual"]),
                           "title": "Compare lower-cost equivalents", "data": gap_block})
    for plan in (sections["dca"]["data"] or {}).get("plans") or []:
        if plan.get("missed"):
            candidates.append({"kind": "dca", "tier": 4, "value": _d(plan.get("shortfall")),
                               "title": f"Catch up the {plan['plan_id']} plan",
                               "data": {k: plan.get(k) for k in ("plan_id", "missed", "shortfall", "on_time_rate", "next_due")}})
    order = ["reserve", "ppr_headroom", "harvest", "drift", "goal_pace", "fees", "dca"]
    candidates.sort(key=lambda c: (c["tier"], c["value"] is None, -(c["value"] or ZERO), order.index(c["kind"])))
    for rank, candidate in enumerate(candidates, 1):
        candidate["rank"] = rank
        candidate["value"] = money(candidate["value"])
    return candidates, missing


# ------------------------------------------------------------------ changes and threads


def _changes(start: str, end: str, snapshot: Mapping[str, Any], history: Mapping[str, list] | None) -> dict:
    rows = []
    if history:
        for key, revisions in sorted(history.items()):
            ordered = sorted(revisions, key=lambda f: (f.get("revision") or 0, f.get("recorded_at") or ""))
            for index, fact in enumerate(ordered):
                day = str(fact.get("valid_from") or fact.get("recorded_at") or "")[:10]
                if not start <= day <= end:
                    continue
                prior = ordered[index - 1] if index else None
                action = "forget" if fact.get("status") == "forgotten" else "add" if prior is None or \
                    prior.get("status") in {"forgotten"} else "update"
                rows.append({"date": day, "key": key, "action": action,
                             "from": _brief(prior.get("value")) if prior else None, "to": _brief(fact.get("value")),
                             "valid_from": fact.get("valid_from"), "source_kind": (fact.get("source") or {}).get("kind")})
        basis = "full revision history"
    else:
        for fact in snapshot.get("facts") or []:
            day = str(fact.get("valid_from") or fact.get("recorded_at") or "")[:10]
            if start <= day <= end:
                rows.append({"date": day, "key": fact["key"], "action": "changed", "from": None,
                             "to": _brief(fact.get("value")), "valid_from": fact.get("valid_from"),
                             "source_kind": (fact.get("source") or {}).get("kind")})
        basis = "current facts only (earlier values not supplied)"
    rows.sort(key=lambda r: (r["date"], r["key"]))
    return _section({"timeline": rows, "basis": basis}, assumptions=[
        "A change is a fact revision whose valid-from date (else its recording date) falls inside the period; values "
        "are shortened for the report."])


def _threads(end: str, sit: Mapping[str, Any] | None) -> dict:
    rows = []
    for thread in ((sit or {}).get("threads") or {}).get("open") or []:
        created = thread.get("created")
        age = None
        try:
            age = (date.fromisoformat(end) - date.fromisoformat(str(created)[:10])).days
        except (TypeError, ValueError):
            pass
        rows.append({"id": thread["id"], "kind": thread.get("kind"), "text": thread["text"], "created": created,
                     "age_days": age})
    return _section({"open": rows})


# ------------------------------------------------------------------ the report


def _holdings_for_audit(book: _Book, day: str) -> tuple[list[dict], list[dict]]:
    rows, missing = book.positions(day)
    return [{"account_id": r["account_id"], "instrument_id": r["instrument_id"], "kind": r["kind"],
             "value": r["value"]} for r in rows], missing


def _instrument_meta(book: _Book, supplied: Mapping[str, Any]) -> dict:
    merged = {k: dict(v) for k, v in book.instruments.items()}
    for key, value in (supplied or {}).items():
        if not isinstance(value, Mapping):
            raise ValueError(f"instruments.{key} must be an object")
        merged[key] = {**merged.get(key, {}), **value}
    return merged


def _account_meta(book: _Book, supplied: Any) -> list[dict]:
    merged = {k: dict(v) for k, v in book.accounts.items()}
    for item in supplied or []:
        if not isinstance(item, Mapping) or not item.get("id"):
            raise ValueError("accounts must be [{id, type?, afore_name?, platform?}]")
        merged[item["id"]] = {**merged.get(item["id"], {}), **item}
    return list(merged.values())


def quarterly(context: Mapping[str, Any], period_start: str, period_end: str) -> dict:
    """The quarterly report.  ``context`` keys (all optional except ``ledger``):

    ``ledger``, ``snapshot`` (store snapshot: facts and decisions), ``fact_history`` ({key: revisions}),
    ``prices`` ({instrument_id: [{date, price}]}), ``currency``, ``ips``, ``benchmarks`` ({ips: {sleeve_id:
    {name, series | annual_rate+source}}, reference_60_40: {equity, bonds}}), ``tax`` ({jurisdiction?,
    mx_marginal_rate?, us_rates?: {short_term, long_term}}), ``sic_listed`` ({instrument_id: bool}), ``fees``
    (fee-audit options: instruments, accounts, advisory, cash_reference_rate, cash_yield, alternatives,
    return_range, afore), ``goal_accounts`` ({goal_id: [account_id]}), ``goal_return_assumption`` (0.05),
    ``dca_plans``, ``ppr`` ({tax_year, accumulable_income_mxn, contributed_mxn, marginal_rate?}),
    ``sleeve_map`` ({instrument_id: sleeve_id}), ``max_price_age_days`` (5), ``max_fx_age_days`` (5).
    """
    start_day, end_day = _date(period_start, "period_start"), _date(period_end, "period_end")
    if end_day < start_day:
        raise ValueError("period_end precedes period_start")
    start, end = period_start, period_end
    # A quarter to date (its end before the quarter closes) is labelled partial everywhere it travels.
    to_date = ({"to_date": True, "quarter_end": _quarter_end(start_day).isoformat()}
               if _to_date(start_day, end_day) else {"to_date": False})
    snapshot = context.get("snapshot") or {"client": {"id": None, "revision": None}, "facts": [], "decisions": []}
    ledger = context.get("ledger")
    if not isinstance(ledger, Mapping) or not ledger.get("accounts"):
        return {"status": "needs_input", "result": {}, "missing": [_miss("ledger", "The review is built from the "
                "transaction ledger; post statements first.")], "warnings": [], "sources": [], "assumptions": []}
    sit = build_situation(snapshot, ledger, end) if snapshot.get("facts") else None
    currency = context.get("currency") or (sit or {}).get("currency")
    if currency is None:
        currencies = sorted({a["currency"] for a in ledger["accounts"]})
        currency = currencies[0] if len(currencies) == 1 else None
    if currency is None:
        return {"status": "needs_input", "result": {}, "missing": [_miss("currency", "Name the reporting currency.")],
                "warnings": [], "sources": [], "assumptions": []}
    max_price_age = int(context.get("max_price_age_days", 5))
    book = _Book(ledger, currency, context.get("prices"), max_price_age, int(context.get("max_fx_age_days", 5)))
    ips = context.get("ips")
    if ips is None and snapshot.get("facts"):
        ips = policy.current(snapshot, end)
        if ips:
            ips.pop("_fact_id", None)
    tax = context.get("tax") or {}
    sic_listed = context.get("sic_listed") or {}
    jurisdiction, basis = _jurisdiction(tax, sit)
    opening = _day_before(start)
    perf = performance_by_account(ledger, opening, end, currency, book.prices)
    portfolio_ids = [a["id"] for a in ledger["accounts"] if str(a.get("type")) not in _SPENDING_TYPES | {"afore", "loan", "mortgage"}]
    portfolio = performance(ledger, opening, end, currency, book.prices, account_ids=portfolio_ids) if portfolio_ids else None

    sections: dict[str, dict] = {}
    sections["net_worth"] = _net_worth(book, start, end, sit, perf)
    sections["performance"] = _performance(book, start, end, perf, portfolio, ips, context.get("benchmarks") or {}, max_price_age)
    sections["allocation"] = _allocation(book, start, end, ips, context.get("sleeve_map") or {})
    sections["cash_flow"] = _cash_flow(book, start, end)
    sections["goals"] = _goals(book, end, sit, context.get("goal_accounts") or {},
                               Decimal(str(context.get("goal_return_assumption", "0.05"))))
    sections["decisions"] = _decisions(book, start, end, snapshot)
    sections["dca"] = _dca(book, start, end, _plans(snapshot, context.get("dca_plans")))
    sections["taxes"] = _taxes(book, start, end, jurisdiction, basis, tax, sic_listed)

    fee_options = dict(context.get("fees") or {})
    holdings, holding_missing = _holdings_for_audit(book, end)
    residence = fee_options.pop("residence", None) or jurisdiction
    fee_report = audit(holdings, _instrument_meta(book, fee_options.pop("instruments", {})),
                       _account_meta(book, fee_options.pop("accounts", [])), ledger, currency=currency, as_of=end,
                       window_start=start, residence=residence, **fee_options)
    period_days = (end_day - start_day).days + 1
    trading = next(c for c in fee_report["result"]["components"] if c["id"] == "trading")
    accrued = [(_d(c["annual_low"]), _d(c["annual_high"])) for c in fee_report["result"]["components"]
               if c["id"] in {"fund_expenses", "afore", "advisory"} and c["annual_low"] is not None]
    fraction = Decimal(period_days) / Decimal(365)
    paid = {"commissions": trading["commission"], "iva": trading["iva"], "total": trading["paid_in_window"]}
    fee_missing = list(fee_report["missing"]) + holding_missing
    if not book.active_in(start, end, portfolio=True):
        # No investment statement in the window: commissions paid are unknown, not zero.
        paid = {k: None for k in paid}
        fee_missing.append(_miss(f"ledger.fees@{start}..{end}", "No investment statement in this period, so fees "
                                 "paid are unknown.", "no_data"))
    annual_cost = dict(fee_report["result"]["annual_cost"])
    if not annual_cost.get("complete") and not _d(annual_cost.get("low")) and not _d(annual_cost.get("high")):
        # A floor of zero is no floor: every cost is unknown, so the annual cost is unknown.
        for field in ("low", "high", "bps_low", "bps_high"):
            if field in annual_cost:
                annual_cost[field] = None
        fee_missing.append(_miss("holdings.costs", "No cost of any holding is known yet, so the annual cost is unknown.",
                                 "no_data"))
    sections["fees"] = _section({
        "paid_in_period": paid,
        "accrued_in_period_estimate": {"low": money(sum((lo for lo, _ in accrued), ZERO) * fraction),
                                       "high": money(sum((hi for _, hi in accrued), ZERO) * fraction)},
        "annual_cost": annual_cost, "top_sources": fee_report["result"]["top_sources"],
        "audit": fee_report["result"]}, missing=fee_missing, sources=fee_report["sources"],
        assumptions=fee_report["assumptions"] + ["Commissions and IVA are the ledger's fee lines inside the period; "
                                                 "fund, AFORE and advisory costs accrue daily and are estimated for the period."])
    sections["changes"] = _changes(start, end, snapshot, context.get("fact_history"))
    sections["threads"] = _threads(end, sit)
    candidates, next_missing = _next_candidates(end, sections, sit, book, jurisdiction, tax, context.get("ppr"), sic_listed)
    sections["next_quarter"] = _section({"two_decisions": candidates[:2], "candidates": candidates},
                                        missing=next_missing, assumptions=[_RULES["ranking"]])

    nw, pf = sections["net_worth"]["data"], sections["performance"]["data"]
    cf = sections["cash_flow"]["data"]
    narrative = {
        "rules": "Write prose around these values only. Quote numbers exactly as given; where a value is null say it "
                 "is not known yet and name what would fill it. No forecasts, no new numbers.",
        "period": {"start": start, "end": end, **to_date}, "currency": currency,
        "language": ((sit or {}).get("profile") or {}).get("language"),
        "name": ((sit or {}).get("profile") or {}).get("name"),
        "net_worth": {k: nw[k] for k in ("start", "end", "change")} | {"contributions": nw["contributions"]["net"],
                                                                        "growth": nw["growth"]["total"],
                                                                        "market": nw["growth"]["market"]},
        "performance": {"twr_period": pf["total"]["twr_period"], "xirr_annual": pf["total"]["xirr_annual"],
                        "ips_benchmark": (pf["benchmarks"].get("ips_benchmark") or {}).get("period_return"),
                        "global_60_40": pf["benchmarks"]["global_60_40"]["period_return"]},
        "allocation_outside_band": [s["sleeve"] for s in sections["allocation"]["data"]["sleeves"] if s.get("outside_band")],
        "cash_flow": None if not cf.get("current") else {
            "income": cf["current"]["income"], "spending": cf["current"]["spending"],
            "savings_rate": cf["current"]["savings_rate"],
            "savings_rate_change": (cf.get("change") or {}).get("savings_rate"),
            "top_categories": [(t["category"], t["amount"]) for t in cf["top_categories"][:3]]},
        "goals": [{"name": g["name"], "funded_pct": g["funded_pct"], "status": g["status"]}
                  for g in sections["goals"]["data"]["goals"]],
        "decisions": [{"what": d["what"], "status": d["status"], "trades_since": d["what_happened"]["trades"]}
                      for d in sections["decisions"]["data"]["decisions"]],
        "dca": [{"plan": p["plan_id"], "on_time_rate": p["on_time_rate"], "missed": len(p["missed"])}
                for p in sections["dca"]["data"]["plans"]],
        "taxes": {"jurisdiction": jurisdiction,
                  "period_estimate": (sections["taxes"]["data"]["period"].get("estimated_tax") or {}).get("total"),
                  "ytd_estimate": (sections["taxes"]["data"]["year_to_date"].get("estimated_tax") or {}).get("total")},
        "fees": {"annual_low": annual_cost.get("low"), "annual_high": annual_cost.get("high"),
                 "bps_low": annual_cost.get("bps_low"), "bps_high": annual_cost.get("bps_high"),
                 "complete": annual_cost.get("complete"), "paid_in_period": paid["total"]},
        "changes": len(sections["changes"]["data"]["timeline"]),
        "open_threads": [t["text"][:120] for t in sections["threads"]["data"]["open"][:3]],
        "next_quarter": [{"title": c["title"], "value": c["value"], "kind": c["kind"]} for c in candidates[:2]],
        "unknown": sorted({m["key"] for s in sections.values() for m in s["missing"]})[:20],
    }
    missing = _unique(m for s in sections.values() for m in s["missing"])
    status = "ready" if all(s["status"] == "ready" for s in sections.values()) else "partial"
    evidence = sorted(set((sit or {}).get("evidence", {}).values()))
    return {"status": status,
            "result": {"period": {"start": start, "end": end, "opening_date": opening, **to_date}, "currency": currency,
                       "sections": sections, "narrative_inputs": narrative},
            "missing": missing, "warnings": [w for s in sections.values() for w in s["warnings"]],
            "sources": perf["sources"], "assumptions": [
                "Deterministic figures from the ledger, the saved picture, the accepted IPS and the decision journal; "
                "each section lists its own sources, assumptions and missing inputs."],
            "_evidence": evidence}


# ------------------------------------------------------------------ service entry


_REVIEW_KEYS = {"period_start", "period_end", "currency", "prices", "benchmarks", "ips", "tax", "sic_listed", "fees",
                "goal_accounts", "goal_return_assumption", "dca_plans", "ppr", "sleeve_map", "max_price_age_days",
                "max_fx_age_days", "ledger", "facts", "as_of"}
_AUDIT_KEYS = {"as_of", "currency", "ledger", "prices", "holdings", "instruments", "accounts", "window_start",
               "residence", "advisory", "cash_reference_rate", "cash_yield", "alternatives", "return_range", "afore",
               "max_price_age_days", "max_fx_age_days", "facts"}


def _reference_input(ref: Mapping[str, Any] | None) -> dict | None:
    """A :func:`wealth.rates.reference` result as the fee audit's ``cash_reference_rate`` input."""
    from .rates import available, origin_text
    if not available(ref):
        return None
    return {"low": ref.get("low", ref["rate"]), "high": ref.get("high", ref["rate"]), "unit": "decimal",
            "source": f"{ref['name']} {ref['percent']}% as of {ref['as_of']} ({origin_text(ref)}): {ref['source']}",
            "as_of": ref["as_of"], "stale": ref["stale"], "fact_id": ref.get("fact_id")}


def run_task(task: str, inputs: Mapping[str, Any], snapshot: Mapping[str, Any], ledger: Any, today: str,
             fact_history: Mapping[str, list] | None = None,
             reference_rate: Callable[[str, Mapping[str, Any]], Mapping[str, Any] | None] | None = None) -> dict:
    """Service entry for ``quarterly_review`` and ``fee_audit``.

    ``reference_rate(currency, snapshot)`` (:func:`wealth.rates.reference`) prices idle cash in the fee audit
    when the inputs carry no ``cash_reference_rate``; ``snapshot`` is the one the audit reads (inline ``facts``
    included), and a saved rate it uses joins ``_evidence``."""
    if task not in TASKS:
        raise ValueError(f"review tasks are {', '.join(TASKS)}")
    inputs = dict(inputs)
    allowed = _REVIEW_KEYS if task == "quarterly_review" else _AUDIT_KEYS
    unknown = sorted(set(inputs) - allowed)
    if unknown:
        raise ValueError(f"{task} inputs: unknown {unknown}; allowed {sorted(allowed)}")
    if "facts" in inputs:
        snapshot = policy.snapshot_from_facts(inputs.pop("facts"), inputs.get("as_of") or inputs.get("period_end") or today)
    ledger = inputs.pop("ledger", ledger)
    if task == "quarterly_review":
        if "period_start" not in inputs or "period_end" not in inputs:
            return {"status": "needs_input", "result": {}, "missing": [_miss("period_start|period_end",
                    "Name the quarter, e.g. period_start 2026-07-01 and period_end 2026-09-30.")],
                    "warnings": [], "sources": [], "assumptions": [], "_evidence": []}
        context = {k: v for k, v in inputs.items() if k not in {"period_start", "period_end", "as_of"}}
        context.update(ledger=ledger, snapshot=snapshot, fact_history=fact_history)
        return quarterly(context, inputs["period_start"], inputs["period_end"])
    as_of = inputs.get("as_of") or today
    sit = build_situation(snapshot, ledger, as_of) if snapshot.get("facts") else None
    currency = inputs.get("currency") or (sit or {}).get("currency")
    residence = inputs.get("residence")
    if residence is None:
        residence, _ = _jurisdiction({}, sit)
    if currency is None:
        return {"status": "needs_input", "result": {}, "missing": [_miss("currency", "Name the reporting currency.")],
                "warnings": [], "sources": [], "assumptions": [], "_evidence": []}
    holdings, extra_missing = inputs.get("holdings"), []
    instruments, accounts = inputs.get("instruments") or {}, inputs.get("accounts") or []
    if holdings is None:
        if not isinstance(ledger, Mapping) or not ledger.get("accounts"):
            return {"status": "needs_input", "result": {}, "missing": [_miss("holdings|ledger",
                    "Supply holdings [{account_id, instrument_id, value}] or a ledger with prices.")],
                    "warnings": [], "sources": [], "assumptions": [], "_evidence": []}
        book = _Book(ledger, currency, inputs.get("prices"), int(inputs.get("max_price_age_days", 5)),
                     int(inputs.get("max_fx_age_days", 5)))
        holdings, extra_missing = _holdings_for_audit(book, as_of)
        instruments, accounts = _instrument_meta(book, instruments), _account_meta(book, accounts)
    if not isinstance(holdings, list):
        raise ValueError("holdings must be a list of {account_id, instrument_id, value}")
    cash_reference = inputs.get("cash_reference_rate")
    if cash_reference is None and reference_rate is not None:
        cash_reference = _reference_input(reference_rate(currency, snapshot))
    report = audit(holdings, instruments, accounts, ledger if isinstance(ledger, Mapping) else None,
                   currency=currency, as_of=as_of, window_start=inputs.get("window_start"), residence=residence,
                   advisory=inputs.get("advisory") or (), cash_reference_rate=cash_reference,
                   cash_yield=inputs.get("cash_yield"), alternatives=inputs.get("alternatives") or (),
                   return_range=inputs.get("return_range") or ("0.04", "0.07"), afore=inputs.get("afore") or ())
    if extra_missing:
        report["missing"] = _unique(report["missing"] + extra_missing)
        report["status"] = "partial"
    evidence = set((sit or {}).get("evidence", {}).values())
    if (cash_reference or {}).get("fact_id") and any(s.get("title") == "Cash reference rate" for s in report["sources"]):
        evidence.add(cash_reference["fact_id"])  # the saved rate priced the idle cash
    report["_evidence"] = sorted(evidence)
    return report


__all__ = ["AFORE_COMMISSIONS", "TASKS", "afore_rate", "audit", "fees_audit", "parse_rate", "quarterly", "run_task"]
