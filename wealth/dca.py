"""Dollar-cost-averaging plans: schedule, adherence, historical backtest, sizing.

A plan is remembered as a fact (``planning.dca``; a list of plans merged by
``id``) and the person's choice to follow it is recorded as a decision citing
that fact.  Adherence compares the schedule with actual ledger buys.  The
backtest replays the plan over a supplied historical price series and is
labelled as history, never as a forecast.  The sizing helper returns a range
for discussion, not an instruction.
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from datetime import date, timedelta
from decimal import Decimal
from typing import Any, Mapping

from .ledger.derive import FxTable, active_entries, envelope
from .ledger.model import LedgerInputError, currency as _currency, dec, iso, money, out, text
from .finmath import add_months, xirr


FACT_KEY = "planning.dca"
CADENCES = {"weekly": 7, "biweekly": 14, "monthly": None, "quarterly": None}
_ZERO = Decimal(0)
HISTORICAL_LABEL = ("Historical backtest over the supplied prices: it describes what would have happened, "
                    "not what will happen. Fractional units, no taxes, uninvested cash earns nothing unless cash_rate is given.")


def validate_plan(raw: Any) -> dict[str, Any]:
    """Validate and normalize a DCA plan (JSON-safe, decimal strings)."""

    if not isinstance(raw, Mapping):
        raise ValueError("plan must be an object")
    allowed = {"id", "name", "currency", "cadence", "start_date", "end_date", "day_of_month", "account_id",
               "source_account_id", "legs", "tolerance_days", "amount_tolerance"}
    unknown = set(raw) - allowed
    if unknown:
        raise ValueError(f"plan has unknown fields {sorted(unknown)!r}; allowed {sorted(allowed)!r}")
    try:
        plan = {
            "id": text(raw.get("id"), "plan.id"),
            "currency": _currency(raw.get("currency"), "plan.currency"),
            "cadence": raw.get("cadence"),
            "start_date": iso(raw.get("start_date"), "plan.start_date"),
            "account_id": text(raw.get("account_id"), "plan.account_id"),
            "tolerance_days": int(raw.get("tolerance_days", 5)),
            "amount_tolerance": out(dec(raw.get("amount_tolerance", "0.05"), "plan.amount_tolerance", nonnegative=True)),
        }
        if plan["cadence"] not in CADENCES:
            raise ValueError(f"plan.cadence must be one of {sorted(CADENCES)}")
        if not 0 <= plan["tolerance_days"] <= 31:
            raise ValueError("plan.tolerance_days must be between 0 and 31")
        for name in ("name", "source_account_id"):
            if raw.get(name) is not None:
                plan[name] = text(raw[name], f"plan.{name}")
        if raw.get("end_date") is not None:
            plan["end_date"] = iso(raw["end_date"], "plan.end_date")
            if plan["end_date"] < plan["start_date"]:
                raise ValueError("plan.end_date precedes start_date")
        if plan["cadence"] in {"monthly", "quarterly"}:
            day = raw.get("day_of_month", date.fromisoformat(plan["start_date"]).day)
            if isinstance(day, bool) or not isinstance(day, int) or not 1 <= day <= 31:
                raise ValueError("plan.day_of_month must be an integer 1-31")
            plan["day_of_month"] = day
        legs = raw.get("legs")
        if not isinstance(legs, list) or not legs:
            raise ValueError("plan.legs must be a nonempty list of {instrument_id, amount}")
        seen, plan["legs"] = set(), []
        for index, leg in enumerate(legs):
            if not isinstance(leg, Mapping):
                raise ValueError(f"plan.legs[{index}] must be an object")
            instrument = text(leg.get("instrument_id"), f"plan.legs[{index}].instrument_id")
            if instrument in seen:
                raise ValueError(f"plan.legs repeats {instrument}")
            seen.add(instrument)
            plan["legs"].append({"instrument_id": instrument,
                                 "amount": out(dec(leg.get("amount"), f"plan.legs[{index}].amount", positive=True))})
    except LedgerInputError as exc:
        raise ValueError(str(exc)) from exc
    return plan


def plan_fact(plan: Mapping[str, Any], observed_on: str, *, source_ref: str = "conversation") -> dict[str, Any]:
    """The fact to ``remember`` for a plan the person stated (merged by plan id)."""

    return {"key": FACT_KEY, "value": {"plans": [validate_plan(plan)]}, "merge": True,
            "source": {"kind": "user", "ref": source_ref, "observed_on": observed_on}, "confidence": "confirmed"}


def _add_months(start: date, months: int, day: int) -> date:
    return add_months(start, months, day)


def schedule(plan: Mapping[str, Any], through: str, *, extra: int = 0) -> list[str]:
    """Due dates from the start through ``through`` (and ``extra`` more after it)."""

    plan = validate_plan(plan)
    start = date.fromisoformat(plan["start_date"])
    last = date.fromisoformat(min(through, plan.get("end_date", through)))
    dates, index, beyond = [], 0, 0
    while True:
        if plan["cadence"] in {"weekly", "biweekly"}:
            due = start + timedelta(days=CADENCES[plan["cadence"]] * index)
        else:
            step = 1 if plan["cadence"] == "monthly" else 3
            due = _add_months(start.replace(day=1), step * index, plan["day_of_month"])
            if index == 0 and due < start:
                index += 1
                continue
        if plan.get("end_date") and due.isoformat() > plan["end_date"]:
            break
        if due > last:
            if beyond >= extra:
                break
            beyond += 1
        dates.append(due.isoformat())
        index += 1
    return dates


def adherence(ledger: Mapping[str, Any], plan: Mapping[str, Any], as_of: str, *, include_inferred: bool = False,
              fx_max_age_days: int = 5) -> dict[str, Any]:
    """Compare scheduled installments with actual buys in the plan's account.

    Each installment owns the buys from ``tolerance_days`` before its due date
    until the same point before the next due date.  States: ``on_time``,
    ``late`` (after due + tolerance), ``partial`` (below the amount
    tolerance), ``skipped`` (nothing by due + tolerance), ``pending`` (due
    date not yet past its tolerance), ``unknown`` (a buy's amount could not be
    converted into the plan currency).
    """

    plan = validate_plan(plan)
    dues = schedule(plan, as_of, extra=1)
    tolerance = timedelta(days=plan["tolerance_days"])
    amount_tolerance = Decimal(plan["amount_tolerance"])
    today = date.fromisoformat(as_of)
    entries, notes = active_entries(ledger, include_inferred=include_inferred)
    fx = FxTable(ledger.get("fx", []), fx_max_age_days)
    installments, missing = [], []
    counts: dict[str, int] = {}
    planned_to_date = invested_to_date = _ZERO
    unknown_amounts = False
    for leg in plan["legs"]:
        buys = [e for e in entries if e["kind"] == "buy" and e["account_id"] == plan["account_id"]
                and e["instrument_id"] == leg["instrument_id"] and e["date"] <= as_of]
        planned = Decimal(leg["amount"])
        for index, due_text in enumerate(dues):
            due = date.fromisoformat(due_text)
            if due > today:
                break
            window_start = due - tolerance
            if index + 1 < len(dues):
                window_end = date.fromisoformat(dues[index + 1]) - tolerance
            elif plan["cadence"] in {"weekly", "biweekly"}:
                window_end = due + timedelta(days=CADENCES[plan["cadence"]]) - tolerance
            else:
                window_end = _add_months(due.replace(day=1), 1 if plan["cadence"] == "monthly" else 3,
                                         plan["day_of_month"]) - tolerance
            mine = [b for b in buys if window_start.isoformat() <= b["date"] < window_end.isoformat()]
            total, unknown = _ZERO, False
            for buy in mine:
                converted = fx.convert(-Decimal(buy["amount"]), buy["currency"], plan["currency"], buy["date"])
                if converted is None:
                    unknown = True
                    missing.append({"key": f"fx.{buy['currency']}/{plan['currency']}@{buy['date']}", "reason": "missing",
                                    "detail": f"Buy {buy['id']} cannot be converted into {plan['currency']}."})
                else:
                    total += converted
            first = min((b["date"] for b in mine), default=None)
            late = first is not None and first > (due + tolerance).isoformat()
            if unknown:
                state = "unknown"
                unknown_amounts = True
            elif total == 0:
                state = "skipped" if today > due + tolerance else "pending"
            elif total < planned * (1 - amount_tolerance):
                state = "partial"
            else:
                state = "late" if late else "on_time"
            counts[state] = counts.get(state, 0) + 1
            planned_to_date += planned
            invested_to_date += total
            installments.append({
                "instrument_id": leg["instrument_id"], "due": due_text, "planned": money(planned),
                "invested": None if unknown else money(total), "state": state, "late": late,
                "over_plan": not unknown and total > planned * (1 + amount_tolerance),
                "window": [window_start.isoformat(), (window_end - timedelta(days=1)).isoformat()],
                "open": today < window_end, "buy_ids": [b["id"] for b in mine],
            })
    evaluated = sum(v for k, v in counts.items() if k not in {"pending", "unknown"})
    next_due = next((d for d in dues if d > as_of), None)
    status = "partial" if missing else "ready"
    return envelope(status, {
        "plan_id": plan["id"], "as_of": as_of, "currency": plan["currency"], "installments": installments,
        "counts": counts, "on_time_rate": None if not evaluated else format(
            (Decimal(counts.get("on_time", 0)) / evaluated).quantize(Decimal("0.0001")), "f"),
        "planned_to_date": money(planned_to_date),
        "invested_to_date": None if unknown_amounts else money(invested_to_date),
        "cumulative_variance": None if unknown_amounts else money(invested_to_date - planned_to_date),
        "next_due": next_due,
    }, missing=missing, warnings=notes, assumptions=[
        f"An installment counts buys from {plan['tolerance_days']} days before its due date until the next installment's window; "
        f"on time means bought by due + {plan['tolerance_days']} days and within {plan['amount_tolerance']} of the planned amount.",
        "Buy amounts include fees and convert into the plan currency at the trade-date rate.",
    ])


def _series(prices: Mapping[str, Any]) -> dict[str, tuple[list[str], list[Decimal]]]:
    table = {}
    for instrument, rows in prices.items():
        points = rows.items() if isinstance(rows, Mapping) else ((r["date"], r.get("price", r.get("close"))) for r in rows)
        values = {}
        for day, price in points:
            value = Decimal(str(price))
            if not value.is_finite() or value <= 0:
                raise ValueError(f"prices for {instrument} must be positive finite numbers")
            values[iso(day, f"prices.{instrument}.date")] = value
        days = sorted(values)
        table[instrument] = (days, [values[d] for d in days])
    return table


def _on_or_after(series: tuple[list[str], list[Decimal]], day: str, max_age: int) -> tuple[str, Decimal] | None:
    days, values = series
    index = bisect_left(days, day)
    if index < len(days) and (date.fromisoformat(days[index]) - date.fromisoformat(day)).days <= max_age:
        return days[index], values[index]
    return None


def _on_or_before(series: tuple[list[str], list[Decimal]], day: str, max_age: int) -> tuple[str, Decimal] | None:
    days, values = series
    index = bisect_right(days, day) - 1
    if index >= 0 and (date.fromisoformat(day) - date.fromisoformat(days[index])).days <= max_age:
        return days[index], values[index]
    return None


def backtest(plan: Mapping[str, Any], prices: Mapping[str, Any], start: str, end: str, *, fee_bps: Any = 0,
             cash_rate: Any = 0, max_price_age_days: int = 5) -> dict[str, Any]:
    """Replay the plan over historical prices beside investing the same total at ``start``.

    Each installment buys at the first price on or after its due date; the
    lump sum buys at the first price on or after ``start``; both are valued at
    the last price on or before ``end``.  While waiting, DCA cash earns the
    optional ``cash_rate`` (annual, simple).  Deterministic for fixed inputs.
    """

    base = validate_plan(plan)
    plan = validate_plan(dict(base, start_date=max(start, base["start_date"])))
    table = _series(prices)
    fee = Decimal(str(fee_bps)) / Decimal(10000)
    rate = Decimal(str(cash_rate))
    dues = [d for d in schedule(plan, end) if start <= d <= end]
    missing, legs_out = [], []
    total_invested = dca_value = lump_value = cash_interest = _ZERO
    flows: list[tuple[str, Decimal]] = []
    complete = bool(dues)
    for leg in plan["legs"]:
        series = table.get(leg["instrument_id"])
        if series is None:
            missing.append({"key": f"prices.{leg['instrument_id']}", "reason": "missing",
                            "detail": f"No price series for {leg['instrument_id']}."})
            complete = False
            continue
        amount = Decimal(leg["amount"])
        units = _ZERO
        buys = []
        for due in dues:
            found = _on_or_after(series, due, max_price_age_days)
            if found is None:
                missing.append({"key": f"prices.{leg['instrument_id']}@{due}", "reason": "missing",
                                "detail": f"No price within {max_price_age_days} days after {due}."})
                complete = False
                continue
            units += amount * (1 - fee) / found[1]
            waited = (date.fromisoformat(due) - date.fromisoformat(start)).days
            cash_interest += amount * rate * Decimal(waited) / Decimal(365)
            buys.append({"due": due, "executed": found[0], "price": out(found[1])})
            flows.append((found[0], -amount))
        leg_total = amount * len(dues)
        entry = _on_or_after(series, start, max_price_age_days)
        final = _on_or_before(series, end, max_price_age_days)
        if entry is None or final is None:
            missing.append({"key": f"prices.{leg['instrument_id']}@{start if entry is None else end}", "reason": "missing",
                            "detail": "No price near the backtest start or end."})
            complete = False
            continue
        lump_units = leg_total * (1 - fee) / entry[1]
        total_invested += leg_total
        dca_value += units * final[1]
        lump_value += lump_units * final[1]
        legs_out.append({"instrument_id": leg["instrument_id"], "installments": len(buys), "invested": money(leg_total),
                         "dca_units": out(units.quantize(Decimal("0.00000001"))),
                         "lump_units": out(lump_units.quantize(Decimal("0.00000001"))),
                         "dca_average_price": money(amount * len(buys) / units) if units else None,
                         "lump_price": out(entry[1]), "end_price": out(final[1]), "buys": buys})
    dca_value += cash_interest
    result: dict[str, Any] = {
        "historical": True, "label": HISTORICAL_LABEL, "plan_id": plan["id"], "currency": plan["currency"],
        "start": start, "end": end, "installments": len(dues), "legs": legs_out,
    }
    if complete:
        flows.append((end, dca_value))
        lump_flows = [(start, -total_invested), (end, lump_value)]
        dca_irr, lump_irr = xirr(flows), xirr(lump_flows)
        result.update({
            "invested": money(total_invested), "dca_end_value": money(dca_value), "lump_sum_end_value": money(lump_value),
            "dca_return": format((dca_value / total_invested - 1).quantize(Decimal("0.000001")), "f"),
            "lump_sum_return": format((lump_value / total_invested - 1).quantize(Decimal("0.000001")), "f"),
            "dca_minus_lump_sum": money(dca_value - lump_value),
            "dca_xirr": None if dca_irr is None else format(Decimal(str(dca_irr)).quantize(Decimal("0.000001")), "f"),
            "lump_sum_xirr": None if lump_irr is None else format(Decimal(str(lump_irr)).quantize(Decimal("0.000001")), "f"),
            "cash_interest": money(cash_interest),
        })
    else:
        result.update({"invested": None, "dca_end_value": None, "lump_sum_end_value": None})
    return envelope("ready" if complete else "partial", result, missing=missing, warnings=[HISTORICAL_LABEL], assumptions=[
        "Installments execute at the first available price on or after the due date; the lump sum at the first price on or after start.",
        f"Fees: {out(Decimal(str(fee_bps)))} bps per purchase; cash rate {out(rate)} per year (simple) on DCA cash awaiting investment.",
        "Past prices only. This is not a forecast or a recommendation between DCA and lump sum.",
    ])


def suggest(monthly_surplus: Any, currency: str, *, constraints: Mapping[str, Any] | None = None,
            target_weights: Mapping[str, Any] | None = None, round_to: Any = 100) -> dict[str, Any]:
    """Size a monthly DCA range from an investable surplus and policy constraints.

    Returns a low/high range (default 50%-80% of the surplus, clamped by
    ``constraints.min_amount``/``max_amount``/``max_share_of_surplus``) and,
    with ``target_weights``, a per-instrument split.  A range for discussion,
    not an instruction.
    """

    constraints = dict(constraints or {})
    if monthly_surplus is None:
        return envelope("needs_input", {}, missing=[{"key": "investable_surplus", "reason": "missing",
                        "detail": "Run spending view=surplus first; the surplus is unknown."}])
    surplus = Decimal(str(monthly_surplus))
    low_share = Decimal(str(constraints.get("min_share_of_surplus", "0.5")))
    high_share = Decimal(str(constraints.get("max_share_of_surplus", "0.8")))
    if not 0 <= low_share <= high_share <= 1:
        raise ValueError("shares of surplus must satisfy 0 <= min_share_of_surplus <= max_share_of_surplus <= 1")
    step = Decimal(str(round_to))
    warnings = []
    if surplus <= 0:
        return envelope("ready", {"currency": currency, "range": None, "monthly_surplus": money(surplus)},
                        warnings=["No investable surplus: a DCA amount cannot be sized from current cash flow."],
                        assumptions=["A range for discussion, not an instruction."])

    def floor(value: Decimal) -> Decimal:
        return (value / step).to_integral_value(rounding="ROUND_FLOOR") * step if step > 0 else value

    low, high = floor(surplus * low_share), floor(surplus * high_share)
    if constraints.get("max_amount") is not None:
        high = min(high, Decimal(str(constraints["max_amount"])))
        low = min(low, high)
    if constraints.get("min_amount") is not None:
        minimum = Decimal(str(constraints["min_amount"]))
        if minimum > high:
            warnings.append(f"The minimum of {money(minimum)} {currency} exceeds the sized range; the constraint cannot be met from surplus.")
        else:
            low = max(low, minimum)
    split = None
    if target_weights:
        weights = {k: Decimal(str(v)) for k, v in target_weights.items()}
        total = sum(weights.values(), _ZERO)
        if total <= 0 or any(w < 0 for w in weights.values()):
            raise ValueError("target_weights must be nonnegative and sum to more than zero")
        split = {k: {"low": money(low * w / total), "high": money(high * w / total)} for k, w in sorted(weights.items())}
    return envelope("partial" if warnings else "ready", {
        "currency": currency, "monthly_surplus": money(surplus),
        "range": {"low": money(low), "high": money(high)}, "split": split,
        "shares_of_surplus": [out(low_share), out(high_share)],
    }, warnings=warnings, assumptions=[
        f"Range = {out(low_share)}-{out(high_share)} of the monthly investable surplus, rounded down to {out(step)}, within stated constraints.",
        "A range for discussion, not an instruction; the person chooses the amount and records the plan.",
    ])


def _plan_from(inputs: Mapping[str, Any], context: Mapping[str, Any]) -> dict[str, Any] | None:
    if inputs.get("plan") is not None:
        return inputs["plan"]
    stored = context.get(FACT_KEY) or {}
    plans = stored.get("plans", []) if isinstance(stored, Mapping) else []
    wanted = inputs.get("plan_id")
    matches = [p for p in plans if wanted is None or p.get("id") == wanted]
    return matches[0] if len(matches) == 1 else None


def run(task: str, inputs: dict[str, Any], context: dict[str, Any] | None = None) -> dict[str, Any]:
    """``dca`` task: view=schedule|adherence|backtest|suggest."""

    context = context or {}
    if task != "dca":
        raise ValueError("task must be dca")
    view = inputs.get("view", "adherence")
    if view == "suggest":
        return suggest(inputs.get("monthly_surplus"), inputs.get("currency", "MXN"),
                       constraints=inputs.get("constraints", context.get("constraint.dca")),
                       target_weights=inputs.get("target_weights"), round_to=inputs.get("round_to", 100))
    plan = _plan_from(inputs, context)
    if plan is None:
        return envelope("needs_input", {}, missing=[{"key": FACT_KEY, "reason": "missing",
                        "detail": "Supply inputs.plan or plan_id of a remembered plan."}])
    as_of = inputs.get("as_of")
    if view == "schedule":
        through = inputs.get("through") or as_of
        if through is None:
            return envelope("needs_input", {}, missing=[{"key": "through", "reason": "missing", "detail": "Schedule needs through."}])
        return envelope("ready", {"plan": validate_plan(plan), "due_dates": schedule(plan, through)})
    if view == "adherence":
        ledger = inputs.get("ledger", context.get("ledger"))
        if ledger is None or as_of is None:
            return envelope("needs_input", {}, missing=[{"key": k, "reason": "missing", "detail": f"adherence needs {k}."}
                                                        for k, v in (("ledger", ledger), ("as_of", as_of)) if v is None])
        return adherence(ledger, plan, as_of, include_inferred=bool(inputs.get("include_inferred", False)))
    if view == "backtest":
        needed = [k for k in ("prices", "start", "end") if k not in inputs]
        if needed:
            return envelope("needs_input", {}, missing=[{"key": k, "reason": "missing", "detail": f"backtest needs {k}."} for k in needed])
        return backtest(plan, inputs["prices"], inputs["start"], inputs["end"], fee_bps=inputs.get("fee_bps", 0),
                        cash_rate=inputs.get("cash_rate", 0), max_price_age_days=int(inputs.get("max_price_age_days", 5)))
    raise ValueError("view must be schedule, adherence, backtest, or suggest")
