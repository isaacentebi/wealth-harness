"""Performance math: time-weighted return, XIRR and the profile performance block."""
from __future__ import annotations

from datetime import date
from typing import Any

from .. import finmath
from .helpers import _as_date, _facts_by_key, _num, _stale


# ---------------------------------------------------------------- performance math

def _series(rows: Any) -> list[tuple[date, float]]:
    out = {}
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        when, value = _as_date(row.get("date") or row.get("as_of")), _num(row.get("value", row.get("amount")))
        if when is not None and value is not None:
            out[when] = out.get(when, 0.0) + value if "amount" in row and "value" not in row else value
    return sorted(out.items())


def time_weighted_return(valuations: list[tuple[date, float]], flows: list[tuple[date, float]]) -> float | None:
    """Chain-linked TWR over consecutive valuations.

    Valuations are end-of-day and include that day's flows, so a flow on a
    valuation date belongs to the period ending there.  Within each sub-period
    flows are day-weighted (Modified Dietz); with a valuation on every flow date
    this is exact daily-valuation TWR.  Flows are positive when money enters.
    """
    points = sorted(valuations)
    if len(points) < 2:
        return None
    growth = 1.0
    for (start, v0), (end, v1) in zip(points, points[1:]):
        days = (end - start).days
        if days <= 0:
            return None
        period_flows = [(d, a) for d, a in flows if start < d <= end]
        net = sum(a for _, a in period_flows)
        weighted = sum(a * (end - d).days / days for d, a in period_flows)
        denominator = v0 + weighted
        if denominator <= 0:
            if v0 == 0 and v1 == 0 and net == 0:
                continue
            return None
        growth *= 1 + (v1 - v0 - net) / denominator
    return growth - 1


def xirr(cashflows: list[tuple[date, float]]) -> float | None:
    """Annualized money-weighted return (investor view: deposits negative).

    One implementation for the whole app: :func:`wealth.finmath.xirr`.
    """
    return finmath.xirr(cashflows)


def _annualize(total: float | None, days: int) -> float | None:
    return finmath.annualize(total, days)


def _value_on(series: list[tuple[date, float]], when: date) -> float | None:
    prior = [v for d, v in series if d <= when]
    return prior[-1] if prior else None


def performance(snapshot: dict, household_history: list[dict], today: date) -> dict:
    facts = _facts_by_key(snapshot)
    fact = next((facts[k] for k in ("performance.history", "portfolio.history") if k in facts
                 and isinstance(facts[k].get("value"), dict)), None)
    if fact is None:
        snapshots = {(row.get("value") or {}).get("as_of") for row in household_history
                     if isinstance(row.get("value"), dict) and row["value"].get("as_of")}
        return {"status": "insufficient", "reason": "flows_unknown" if len(snapshots) >= 2 else "no_history",
                "statements": len(snapshots)}
    value = fact["value"]
    valuations = [(d, v) for d, v in _series(value.get("valuations")) if d <= today]
    if "cash_flows" not in value and "flows" not in value:
        return {"status": "insufficient", "reason": "flows_unknown", "statements": len(valuations)}
    flows = _series([{"date": r.get("date"), "amount": r.get("amount")}
                     for r in (value.get("cash_flows", value.get("flows")) or []) if isinstance(r, dict)])
    if len(valuations) < 2 or (valuations[-1][0] - valuations[0][0]).days < 28:
        return {"status": "insufficient", "reason": "short_history", "statements": len(valuations)}
    start, end = valuations[0][0], valuations[-1][0]
    days = (end - start).days
    in_range = [(d, a) for d, a in flows if start < d <= end]
    twr = time_weighted_return(valuations, in_range)
    irr = xirr([(start, -valuations[0][1]), *[(d, -a) for d, a in in_range], (end, valuations[-1][1])])
    index, growth = [], 1.0
    for i, (d, v) in enumerate(valuations):
        if i:
            step = time_weighted_return(valuations[i - 1:i + 1], in_range)
            growth *= 1 + step if step is not None else 1
        index.append({"date": d.isoformat(), "value": round(v, 2), "index": round(100 * growth, 4)})
    benchmark = None
    raw_bench = value.get("benchmark")
    if isinstance(raw_bench, dict):
        bench = _series(raw_bench.get("values"))
        b0, b1 = _value_on(bench, start), _value_on(bench, end)
        if b0 and b1 is not None:
            bench_total = b1 / b0 - 1
            benchmark = {
                "name": str(raw_bench.get("name") or "Benchmark"),
                "return": round(bench_total, 6), "annualized": _round(_annualize(bench_total, days)),
                "series": [{"date": d.isoformat(), "index": round(100 * v / b0, 4)} for d, v in bench if start <= d <= end],
                "excess": round(twr - bench_total, 6) if twr is not None else None,
            }
    return {
        "status": "ready" if twr is not None else "insufficient",
        "reason": None if twr is not None else "invalid_history",
        "currency": value.get("currency"), "start": start.isoformat(), "end": end.isoformat(), "days": days,
        "twr": _round(twr), "twr_annualized": _round(_annualize(twr, days)),
        "irr_annualized": _round(irr),
        "irr_period": _round((1 + irr) ** (days / 365.0) - 1 if irr is not None else None),
        "net_flows": round(sum(a for _, a in in_range), 2), "flow_count": len(in_range),
        "series": index, "benchmark": benchmark,
        "stale": _stale(fact, today),
    }


def _round(value: float | None, digits: int = 6) -> float | None:
    return round(value, digits) if value is not None else None
