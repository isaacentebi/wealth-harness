"""Valuation, time-weighted and money-weighted returns over the ledger.

Conventions (stated in every result):

* A value is the end-of-day value in the reporting currency.
* External flows (deposits, withdrawals, income paid in, spending paid out,
  loan payments, transfers from/to accounts outside the scope, in-kind
  transfers and opening balances) happen at the *end* of their day.
  Dividends, interest, fees, withholding, trades, splits and FX conversions are
  internal and therefore part of the return.
* ``linked`` TWR revalues the scope on every flow date and chains the
  sub-period returns (exact TWR).  ``modified_dietz`` needs values only at
  calendar-month ends and weights flows by the days they were invested.
* XIRR solves the annual rate that discounts all flows plus the starting and
  ending values to zero.
* A missing price or rate leaves a gap: the value on that date is unknown,
  and any return that needs it is ``None`` with the gap listed.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from typing import Any, Callable, Mapping, Sequence

from .derive import (DEFAULT_FX_AGE_DAYS, FxTable, PriceProvider, _d, _identity, _sources, _unique,
                     envelope, replay)
from .model import money, out


_ZERO = Decimal(0)
_EXTERNAL = frozenset({"deposit", "withdrawal", "income", "expense", "loan_payment", "opening_balance", "transfer"})
_INCOME = frozenset({"dividend", "interest"})


def _ratio(value: Decimal | float | None) -> str | None:
    if value is None:
        return None
    return format(Decimal(str(value)).quantize(Decimal("0.000001")), "f")


def _scope(ledger: Mapping[str, Any], account_ids: Sequence[str] | None) -> set[str]:
    known = {a["id"] for a in ledger.get("accounts", [])}
    if account_ids is None:
        return known
    unknown = set(account_ids) - known
    if unknown:
        raise ValueError(f"unknown account_ids: {sorted(unknown)}")
    return set(account_ids)


def _value(snapshot: Mapping[str, Any], scope: set[str], day: str, currency: str, prices: PriceProvider,
           fx: FxTable, instruments: Mapping[str, Mapping[str, Any]]) -> tuple[Decimal | None, list[str]]:
    total, gaps = _ZERO, []
    for (account, ccy), amount in snapshot["cash"].items():
        if account not in scope or amount == 0:
            continue
        converted = fx.convert(amount, ccy, currency, day)
        if converted is None:
            gaps.append(f"fx {ccy}/{currency} on {day}")
        else:
            total += converted
    for (account, instrument), quantity in snapshot["quantities"].items():
        if account not in scope or quantity == 0:
            continue
        price = prices(instrument, day)
        ccy = instruments.get(instrument, {}).get("currency")
        if price is None:
            gaps.append(f"price {instrument} on {day}")
            continue
        converted = fx.convert(quantity * price, ccy, currency, day)
        if converted is None:
            gaps.append(f"fx {ccy}/{currency} on {day}")
        else:
            total += converted
    return (None if gaps else total), gaps


def external_flows(ledger: Mapping[str, Any], start: str, end: str, currency: str, prices: PriceProvider, *,
                   account_ids: Sequence[str] | None = None, include_inferred: bool = False,
                   fx_max_age_days: int = DEFAULT_FX_AGE_DAYS) -> dict[str, Any]:
    """Money entering (+) or leaving (-) the scope in (start, end], in ``currency``."""

    scope = _scope(ledger, account_ids)
    state, meta = replay(ledger, end, include_inferred=include_inferred)
    fx = FxTable(ledger.get("fx", []), fx_max_age_days)
    instruments = {i["id"]: i for i in ledger.get("instruments", [])}
    partner = {}
    for pair in meta["transfers"]["pairs"]:
        partner[pair["out"]], partner[pair["in"]] = pair["in"], pair["out"]
    by_id = {e["id"]: e for e in meta["entries"]}
    flows, income, costs, gaps = [], [], [], []
    for entry in meta["entries"]:
        if not (start < entry["date"] <= end):
            continue
        account = entry["account_id"]
        kind = entry["kind"]
        if kind in _INCOME or kind in {"fee", "tax_withheld"}:
            if account in scope:
                converted = fx.convert(_d(entry["amount"]), entry["currency"], currency, entry["date"])
                if converted is None:
                    gaps.append(f"fx {entry['currency']}/{currency} on {entry['date']}")
                (income if kind in _INCOME else costs).append({"entry_id": entry["id"], "kind": kind, "amount": converted})
            continue
        legs = []
        if kind == "loan_payment" and entry.get("counterparty_account_id") and entry.get("principal") is not None:
            legs.append((account, _d(entry["amount"]), entry["currency"]))
            legs.append((entry["counterparty_account_id"], _d(entry["principal"]), entry["currency"]))
        elif kind in _EXTERNAL:
            if kind == "transfer" and entry["id"] in partner and by_id[partner[entry["id"]]]["account_id"] in scope \
                    and account in scope:
                continue
            if entry.get("instrument_id") and entry.get("quantity") is not None:
                price = prices(entry["instrument_id"], entry["date"])
                ccy = instruments.get(entry["instrument_id"], {}).get("currency")
                value = None if price is None else _d(entry["quantity"]) * price
                if value is None:
                    gaps.append(f"price {entry['instrument_id']} on {entry['date']}")
                legs.append((account, value, ccy))
                if entry.get("amount") is not None:
                    legs.append((account, _d(entry["amount"]), entry["currency"]))
            else:
                legs.append((account, _d(entry.get("amount")), entry.get("currency")))
        for leg_account, amount, ccy in legs:
            if leg_account not in scope:
                continue
            converted = fx.convert(amount, ccy, currency, entry["date"]) if amount is not None else None
            if amount is not None and converted is None:
                gaps.append(f"fx {ccy}/{currency} on {entry['date']}")
            flows.append({"date": entry["date"], "entry_id": entry["id"], "kind": kind, "amount": converted})
    return {"flows": flows, "income": income, "costs": costs, "gaps": sorted(set(gaps)), "meta": meta}


def valuation_series(ledger: Mapping[str, Any], dates: Sequence[str], currency: str, prices: PriceProvider, *,
                     account_ids: Sequence[str] | None = None, include_inferred: bool = False,
                     fx_max_age_days: int = DEFAULT_FX_AGE_DAYS) -> dict[str, Any]:
    """End-of-day value on each date; unknown values are ``None`` with the gap named."""

    scope = _scope(ledger, account_ids)
    days = sorted(set(dates))
    state, meta = replay(ledger, days[-1] if days else None, include_inferred=include_inferred, checkpoints=days)
    fx = FxTable(ledger.get("fx", []), fx_max_age_days)
    instruments = {i["id"]: i for i in ledger.get("instruments", [])}
    points = []
    for day in days:
        value, gaps = _value(state.snapshots[day], scope, day, currency, prices, fx, instruments)
        points.append({"date": day, "value": value, "gaps": gaps})
    return {"points": points, "meta": meta}


def xirr(cashflows: Sequence[tuple[str, Decimal]]) -> float | None:
    """Annual money-weighted return; investor view (contributions negative).

    Deterministic bracket scan plus bisection.  Returns ``None`` when there is
    no sign change (no unique economically meaningful rate).
    """

    if not cashflows or all(c >= 0 for _, c in cashflows) or all(c <= 0 for _, c in cashflows):
        return None
    origin = min(date.fromisoformat(d) for d, _ in cashflows)
    points = [((date.fromisoformat(d) - origin).days / 365.0, float(c)) for d, c in cashflows]

    def npv(rate: float) -> float:
        return sum(c / (1.0 + rate) ** t for t, c in points)

    grid = [-0.99, -0.9, -0.75, -0.5, -0.25, -0.1, 0.0, 0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0, 100.0]
    values = [npv(r) for r in grid]
    for low, high, f_low, f_high in zip(grid, grid[1:], values, values[1:]):
        if f_low == 0:
            return low
        if (f_low < 0) != (f_high < 0):
            for _ in range(200):
                mid = (low + high) / 2
                f_mid = npv(mid)
                if (f_mid < 0) == (f_low < 0):
                    low, f_low = mid, f_mid
                else:
                    high = mid
            return (low + high) / 2
    return None


def _month_ends(start: str, end: str) -> list[str]:
    result, current = [], date.fromisoformat(start)
    last = date.fromisoformat(end)
    while True:
        next_month = (current.replace(day=1) + timedelta(days=32)).replace(day=1)
        month_end = next_month - timedelta(days=1)
        if month_end >= last:
            break
        if month_end > date.fromisoformat(start):
            result.append(month_end.isoformat())
        current = next_month
    return result


def _annualize(total: Decimal | None, days: int) -> Decimal | None:
    if total is None or days < 365 or total <= -1:
        return None
    return Decimal(str((1 + float(total)) ** (365.0 / days) - 1))


def performance(ledger: Mapping[str, Any], start: str, end: str, currency: str, prices: PriceProvider, *,
                account_ids: Sequence[str] | None = None, method: str = "linked",
                benchmark: Callable[[str], Decimal | None] | None = None, benchmark_name: str | None = None,
                include_inferred: bool = False, fx_max_age_days: int = DEFAULT_FX_AGE_DAYS) -> dict[str, Any]:
    """TWR, XIRR, benchmark comparison and gain decomposition for one scope."""

    if method not in {"linked", "modified_dietz"}:
        raise ValueError("method must be linked or modified_dietz")
    if end <= start:
        raise ValueError("end must be after start")
    flow_data = external_flows(ledger, start, end, currency, prices, account_ids=account_ids,
                               include_inferred=include_inferred, fx_max_age_days=fx_max_age_days)
    flows = flow_data["flows"]
    by_day: dict[str, Decimal | None] = {}
    for flow in flows:
        if flow["amount"] is None or by_day.get(flow["date"], _ZERO) is None:
            by_day[flow["date"]] = None
        else:
            by_day[flow["date"]] = by_day.get(flow["date"], _ZERO) + flow["amount"]
    needed = {start, end, *by_day}
    if method == "modified_dietz":
        needed.update(_month_ends(start, end))
    series = valuation_series(ledger, sorted(needed), currency, prices, account_ids=account_ids,
                              include_inferred=include_inferred, fx_max_age_days=fx_max_age_days)
    values = {p["date"]: p["value"] for p in series["points"]}
    gaps = sorted({g for p in series["points"] for g in p["gaps"]} | set(flow_data["gaps"]))
    missing = [{"key": gap, "reason": "missing", "detail": f"Needed to value the scope: {gap}."} for gap in gaps]
    warnings = list(series["meta"]["notes"])
    v_start, v_end = values[start], values[end]
    twr: Decimal | None = None
    if method == "linked":
        chain, previous, ok = Decimal(1), v_start, True
        for day in sorted(d for d in by_day if d != start) + ([end] if end not in by_day else []):
            value, flow = values.get(day), by_day.get(day, _ZERO)
            if value is None or flow is None or previous is None:
                ok = False
                break
            if previous > 0:
                chain *= (value - flow) / previous
            elif value - flow != 0:
                warnings.append(f"Value changed on {day} with no invested base; that change is excluded from TWR.")
            previous = value
        twr = chain - 1 if ok else None
    else:
        boundaries = [start, *_month_ends(start, end), end]
        chain, ok = Decimal(1), True
        for p0, p1 in zip(boundaries, boundaries[1:]):
            b0, b1 = values.get(p0), values.get(p1)
            inside = [f for f in flows if p0 < f["date"] <= p1]
            if b0 is None or b1 is None or any(f["amount"] is None for f in inside):
                ok = False
                break
            span = (date.fromisoformat(p1) - date.fromisoformat(p0)).days
            net = sum((f["amount"] for f in inside), _ZERO)
            weighted = sum((f["amount"] * Decimal((date.fromisoformat(p1) - date.fromisoformat(f["date"])).days) / Decimal(span)
                            for f in inside), _ZERO)
            base = b0 + weighted
            if base <= 0:
                if b1 - b0 - net != 0:
                    warnings.append(f"Period {p0}..{p1} has no invested base; its change is excluded from TWR.")
                continue
            chain *= 1 + (b1 - b0 - net) / base
        twr = chain - 1 if ok else None
    irr = None
    if v_start is not None and v_end is not None and all(f["amount"] is not None for f in flows):
        cashflows = ([(start, -v_start)] if v_start else []) + [(f["date"], -f["amount"]) for f in flows] + [(end, v_end)]
        irr = xirr(cashflows)
        if irr is None:
            warnings.append("Money-weighted return has no unique solution for these flows.")
    days = (date.fromisoformat(end) - date.fromisoformat(start)).days
    net_flows = None if any(f["amount"] is None for f in flows) else sum((f["amount"] for f in flows), _ZERO)
    total_gain = None if None in (v_start, v_end, net_flows) else v_end - v_start - net_flows
    income = None if any(i["amount"] is None for i in flow_data["income"]) else sum((i["amount"] for i in flow_data["income"]), _ZERO)
    costs = None if any(c["amount"] is None for c in flow_data["costs"]) else sum((c["amount"] for c in flow_data["costs"]), _ZERO)
    market = None if None in (total_gain, income, costs) else total_gain - income - costs
    contributions = None if net_flows is None else sum((f["amount"] for f in flows if f["amount"] > 0), _ZERO)
    withdrawals = None if net_flows is None else sum((f["amount"] for f in flows if f["amount"] < 0), _ZERO)
    bench = None
    if benchmark is not None:
        b_start, b_end = benchmark(start), benchmark(end)
        bench_missing = [d for d, v in ((start, b_start), (end, b_end)) if v is None]
        pme = None
        if not bench_missing and v_start is not None and net_flows is not None:
            units, pme_ok = (v_start / b_start) if v_start else _ZERO, True
            for flow in flows:
                level = benchmark(flow["date"])
                if level is None:
                    bench_missing.append(flow["date"])
                    pme_ok = False
                    break
                units += flow["amount"] / level
            pme = units * b_end if pme_ok else None
        for day in bench_missing:
            missing.append({"key": f"benchmark@{day}", "reason": "missing", "detail": f"No benchmark level on or shortly before {day}."})
        bench_return = None if b_start is None or b_end is None else b_end / b_start - 1
        bench = {
            "name": benchmark_name, "period_return": _ratio(bench_return),
            "annualized_return": _ratio(_annualize(bench_return, days)),
            "excess_twr": _ratio(None if twr is None or bench_return is None else twr - bench_return),
            "same_flows_ending_value": money(pme),
            "ending_value_minus_same_flows": money(None if pme is None or v_end is None else v_end - pme),
        }
    result = {
        "scope": sorted(_scope(ledger, account_ids)), "currency": currency, "start": start, "end": end,
        "method": method, "start_value": money(v_start), "end_value": money(v_end),
        "twr": {"period": _ratio(twr), "annualized": _ratio(_annualize(twr, days))},
        "xirr_annual": _ratio(irr),
        "decomposition": {
            "contributions": money(contributions), "withdrawals": money(withdrawals), "net_flows": money(net_flows),
            "total_gain": money(total_gain), "investment_income": money(income),
            "fees_and_withholding": money(costs), "price_and_fx_change": money(market),
        },
        "flows": [{"date": f["date"], "entry_id": f["entry_id"], "kind": f["kind"], "amount": money(f["amount"])} for f in flows],
        "benchmark": bench,
    }
    missing = _unique(missing)
    status = "partial" if missing or twr is None else "ready"
    return envelope(status, result, missing=missing, warnings=warnings, sources=_sources(series["meta"]), assumptions=[
        "Flows occur at the end of their day; dividends, interest, fees and withholding are part of the return.",
        "Transfers between accounts inside the scope are internal; transfers to or from accounts outside it are flows.",
        "Returns under one year are not annualized. Historical results, not a forecast.",
        f"Values use the latest price and FX rate on or up to their provider's allowed age before each date (FX {fx_max_age_days} days).",
        *(["The benchmark comparison applies the same external flows to the benchmark series (public-market equivalent)."] if benchmark else []),
    ])


def performance_by_account(ledger: Mapping[str, Any], start: str, end: str, currency: str, prices: PriceProvider,
                           **kwargs: Any) -> dict[str, Any]:
    """Run :func:`performance` for every account and for the whole ledger."""

    accounts = [a["id"] for a in ledger.get("accounts", [])]
    per = {account: performance(ledger, start, end, currency, prices, account_ids=[account], **kwargs)
           for account in accounts}
    total = performance(ledger, start, end, currency, prices, **kwargs)
    missing = _unique([m for r in [total, *per.values()] for m in r["missing"]])
    return envelope("partial" if missing or total["status"] != "ready" else "ready",
                    {"total": total["result"], "accounts": {k: v["result"] for k, v in per.items()}},
                    missing=missing, warnings=total["warnings"], sources=total["sources"],
                    assumptions=total["assumptions"])


def exposure_groups(ledger: Mapping[str, Any], as_of: str, currency: str, prices: PriceProvider, *,
                    by: str = "underlying", account_ids: Sequence[str] | None = None,
                    include_inferred: bool = False, fx_max_age_days: int = DEFAULT_FX_AGE_DAYS) -> dict[str, Any]:
    """Group holdings by economic ``underlying`` (exposure) or by ``venue`` (tax/estate).

    VOO bought through the SIC in MXN and VOO bought in USD at a US broker are
    one underlying but two venues.
    """

    if by not in {"underlying", "venue", "issuer_domicile"}:
        raise ValueError("by must be underlying, venue, or issuer_domicile")
    scope = _scope(ledger, account_ids)
    state, meta = replay(ledger, as_of, include_inferred=include_inferred)
    fx = FxTable(ledger.get("fx", []), fx_max_age_days)
    instruments = {i["id"]: i for i in ledger.get("instruments", [])}
    groups: dict[str, dict[str, Any]] = {}
    missing = []
    field = {"underlying": "underlying_symbol", "venue": "venue", "issuer_domicile": "issuer_domicile"}[by]
    for (account, instrument), lots in sorted(state.lots.items()):
        if account not in scope:
            continue
        quantity = sum((l.quantity for l in lots), _ZERO)
        identity = _identity(instruments.get(instrument, {}))
        name = identity[field] or "unknown"
        group = groups.setdefault(name, {"value": _ZERO, "complete": True, "holdings": []})
        price = prices(instrument, as_of)
        ccy = instruments.get(instrument, {}).get("currency")
        value = None if price is None else fx.convert(quantity * price, ccy, currency, as_of)
        if value is None:
            group["complete"] = False
            missing.append({"key": f"{'price' if price is None else 'fx'}.{instrument}@{as_of}", "reason": "missing",
                            "detail": f"Cannot value {instrument} in {account} in {currency} on {as_of}."})
        else:
            group["value"] += value
        group["holdings"].append({"account_id": account, "instrument_id": instrument, "quantity": out(quantity),
                                  "value": money(value), **identity})
    result = {name: {"value": money(g["value"]) if g["complete"] else None,
                     "known_value": money(g["value"]), "holdings": g["holdings"]}
              for name, g in sorted(groups.items())}
    missing = _unique(missing)
    return envelope("partial" if missing else "ready", {"by": by, "currency": currency, "as_of": as_of, "groups": result},
                    missing=missing, warnings=meta["notes"], sources=_sources(meta), assumptions=[
                        "Exposure groups use underlying_symbol; tax and estate views use venue. Cash is excluded.",
                    ])
