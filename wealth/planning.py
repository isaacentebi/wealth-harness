"""Deterministic, assumption-explicit financial planning calculators.

The module deliberately does not choose capital-market assumptions or invent
securities.  Results are conditional simulations, not forecasts or advice.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date
import math
import random
import re
from typing import Any


_CURRENCY = re.compile(r"[A-Z]{3}")
_DAY = 365.2425


def _number(value: Any, field: str, *, minimum: float | None = None) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field} must be a finite number")
    number = float(value)
    if not math.isfinite(number) or (minimum is not None and number < minimum):
        suffix = f" greater than or equal to {minimum}" if minimum is not None else ""
        raise ValueError(f"{field} must be a finite number{suffix}")
    return number


def _integer(value: Any, field: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{field} must be an integer greater than or equal to {minimum}")
    return value


def _date(value: Any, field: str) -> date:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be an ISO date")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{field} must be an ISO date") from exc
    if parsed.isoformat() != value:
        raise ValueError(f"{field} must be an ISO date")
    return parsed


def _currency(value: Any, field: str) -> str:
    if not isinstance(value, str) or _CURRENCY.fullmatch(value) is None:
        raise ValueError(f"{field} must be three uppercase letters")
    return value


def _rate(value: Any, field: str, *, allow_negative: bool = False) -> float:
    rate = _number(value, field)
    if rate <= -1 or (not allow_negative and rate < 0):
        raise ValueError(f"{field} must be {'greater than -1' if allow_negative else 'between 0 and 1'}")
    if not allow_negative and rate > 1:
        raise ValueError(f"{field} must be between 0 and 1")
    return rate


def _money(value: float, currency: str) -> dict[str, Any]:
    return {"currency": currency, "amount": round(value, 2)}


def _pct(value: float) -> float:
    return round(100 * value, 4)


def _percentile(values: list[float], percentile: float) -> float:
    ordered = sorted(values)
    if not ordered:
        raise ValueError("cannot calculate a percentile of an empty list")
    position = (len(ordered) - 1) * percentile
    low = math.floor(position)
    high = math.ceil(position)
    if low == high:
        return ordered[low]
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def _envelope(status: str, *, result: dict[str, Any] | None = None,
              missing: list[str] | None = None, warnings: list[str] | None = None,
              assumptions: list[str] | None = None) -> dict[str, Any]:
    return {
        "status": status,
        "result": result or {},
        "missing": missing or [],
        "warnings": warnings or [],
        "sources": [],
        "assumptions": assumptions or [],
    }


def _required(data: dict[str, Any], fields: list[str]) -> list[str]:
    return [field for field in fields if field not in data]


def _return_model(raw: Any, field: str) -> tuple[str, list[float] | tuple[float, float]]:
    if not isinstance(raw, dict):
        raise ValueError(f"{field} must be an object")
    kind = raw.get("type")
    if kind == "bootstrap":
        values = raw.get("historical_annual_returns")
        if not isinstance(values, list) or not values:
            raise ValueError(f"{field}.historical_annual_returns must be a nonempty list")
        returns = [_rate(value, f"{field}.historical_annual_returns[{index}]", allow_negative=True)
                   for index, value in enumerate(values)]
        return kind, returns
    if kind == "parametric":
        mean = _rate(raw.get("annual_return"), f"{field}.annual_return", allow_negative=True)
        volatility = _number(raw.get("annual_volatility"), f"{field}.annual_volatility", minimum=0)
        return kind, (mean, volatility)
    raise ValueError(f"{field}.type must be 'bootstrap' or 'parametric'")


def _draw_returns(model: tuple[str, list[float] | tuple[float, float]], years: int,
                  rng: random.Random) -> list[float]:
    kind, parameters = model
    if kind == "bootstrap":
        history = parameters
        assert isinstance(history, list)
        return [rng.choice(history) for _ in range(years)]
    mean, volatility = parameters
    assert isinstance(mean, float) and isinstance(volatility, float)
    if volatility == 0:
        return [mean] * years
    # Convert arithmetic mean/volatility to lognormal parameters.  This keeps
    # every gross return nonnegative without an arbitrary clipping rule.
    variance = volatility * volatility
    sigma2 = math.log1p(variance / ((1 + mean) ** 2))
    mu = math.log1p(mean) - sigma2 / 2
    sigma = math.sqrt(sigma2)
    return [math.exp(rng.normalvariate(mu, sigma)) - 1 for _ in range(years)]


def _growth(wealth: float, annual_return: float, years: float, fee: float, tax_drag: float) -> float:
    if wealth <= 0 or years <= 0:
        return max(wealth, 0.0)
    try:
        result = wealth * ((1 + annual_return) ** years) * ((1 - fee) ** years) * ((1 - tax_drag) ** years)
    except OverflowError as exc:
        raise ValueError("supplied assumptions produce nonfinite wealth") from exc
    if not math.isfinite(result):
        raise ValueError("supplied assumptions produce nonfinite wealth")
    return max(0.0, result)


def _add_years(day: date, years: int) -> date:
    try:
        return day.replace(year=day.year + years)
    except ValueError:  # February 29
        return day.replace(year=day.year + years, day=28)


def _accrue(wealth: float, projection_start: date, interval_start: date, interval_end: date,
            returns: list[float], fee: float, tax_drag: float) -> float:
    """Accrue through projection-year boundaries using each year's return once."""
    if interval_end <= interval_start:
        return max(wealth, 0.0)
    for index, annual_return in enumerate(returns):
        period_start = _add_years(projection_start, index)
        period_end = _add_years(projection_start, index + 1)
        overlap_start = max(interval_start, period_start)
        overlap_end = min(interval_end, period_end)
        if overlap_end > overlap_start:
            fraction = (overlap_end - overlap_start).days / (period_end - period_start).days
            wealth = _growth(wealth, annual_return, fraction,
                             fee, tax_drag)
        if period_end >= interval_end:
            break
    return wealth


def _expand_flows(data: dict[str, Any], start: date, end: date, inflation: float) -> list[dict[str, Any]]:
    flows: list[dict[str, Any]] = []
    raw_flows = data.get("cashflows", [])
    if not isinstance(raw_flows, list):
        raise ValueError("cashflows must be a list")
    for index, raw in enumerate(raw_flows):
        if not isinstance(raw, dict):
            raise ValueError(f"cashflows[{index}] must be an object")
        flow_type = raw.get("type")
        if flow_type not in {"contribution", "withdrawal"}:
            raise ValueError(f"cashflows[{index}].type must be contribution or withdrawal")
        when = _date(raw.get("date"), f"cashflows[{index}].date")
        if not start < when <= end:
            raise ValueError(f"cashflows[{index}].date must be after as_of and on or before end_date")
        amount = _number(raw.get("amount"), f"cashflows[{index}].amount", minimum=0)
        indexed = raw.get("inflation_indexed")
        if not isinstance(indexed, bool):
            raise ValueError(f"cashflows[{index}].inflation_indexed must be a boolean")
        if indexed:
            amount *= (1 + inflation) ** ((when - start).days / _DAY)
        hard = raw.get("hard_goal")
        if not isinstance(hard, bool):
            raise ValueError(f"cashflows[{index}].hard_goal must be a boolean")
        identifier = str(raw.get("id", f"cashflow-{index}"))
        flows.append({"id": identifier, "goal_id": str(raw.get("_goal_id", identifier)), "date": when,
                      "amount": amount, "type": flow_type, "hard": hard})

    recurring = data.get("recurring_cashflows", [])
    if not isinstance(recurring, list):
        raise ValueError("recurring_cashflows must be a list")
    for index, raw in enumerate(recurring):
        if not isinstance(raw, dict):
            raise ValueError(f"recurring_cashflows[{index}] must be an object")
        frequency = raw.get("frequency")
        if frequency not in {"annual", "monthly"}:
            raise ValueError(f"recurring_cashflows[{index}].frequency must be annual or monthly")
        first = _date(raw.get("start"), f"recurring_cashflows[{index}].start")
        last = _date(raw.get("end"), f"recurring_cashflows[{index}].end")
        if first <= start or last > end or last < first:
            raise ValueError(f"recurring_cashflows[{index}] dates must fall within the projection horizon")
        raw_copy = dict(raw)
        cursor = first
        occurrence = 0
        while cursor <= last:
            raw_copy["date"] = cursor.isoformat()
            base_id = str(raw.get("id", f"recurring-{index}"))
            raw_copy["id"] = f"{base_id}:{occurrence}"
            raw_copy["_goal_id"] = base_id
            child = {"cashflows": [raw_copy]}
            flows.extend(_expand_flows(child, start, end, inflation))
            occurrence += 1
            if frequency == "annual":
                cursor = _add_years(first, occurrence)
            else:
                month_index = first.month - 1 + occurrence
                year = first.year + month_index // 12
                month = month_index % 12 + 1
                # Preserve day where possible, otherwise use month end.
                day = first.day
                while True:
                    try:
                        cursor = date(year, month, day)
                        break
                    except ValueError:
                        day -= 1
    return sorted(flows, key=lambda item: (item["date"], item["type"] != "contribution", item["id"]))


def _simulate_project(data: dict[str, Any]) -> dict[str, Any]:
    required = _required(data, ["currency", "as_of", "end_date", "initial_wealth", "simulations",
                                "seed", "return_model", "annual_inflation", "annual_fee", "annual_tax_drag",
                                "cashflows", "recurring_cashflows"])
    if required:
        return _envelope("needs_input", missing=required)
    currency = _currency(data["currency"], "currency")
    start = _date(data["as_of"], "as_of")
    end = _date(data["end_date"], "end_date")
    if end <= start:
        raise ValueError("end_date must be after as_of")
    initial = _number(data["initial_wealth"], "initial_wealth", minimum=0)
    simulations = _integer(data["simulations"], "simulations", minimum=1)
    seed = _integer(data["seed"], "seed", minimum=0)
    model = _return_model(data["return_model"], "return_model")
    inflation = _rate(data["annual_inflation"], "annual_inflation")
    fee = _rate(data["annual_fee"], "annual_fee")
    tax_drag = _rate(data["annual_tax_drag"], "annual_tax_drag")
    flows = _expand_flows(data, start, end, inflation)
    horizon_years = max(1, math.ceil((end - start).days / _DAY))
    rng = random.Random(seed)
    terminals: list[float] = []
    all_withdrawals_fulfilled = 0
    positive_terminal_wealth = 0
    goal_success: defaultdict[str, int] = defaultdict(int)
    hard_goal_ids = sorted({flow["goal_id"] for flow in flows
                            if flow["type"] == "withdrawal" and flow["hard"]})
    for _ in range(simulations):
        returns = _draw_returns(model, horizon_years, rng)
        wealth = initial
        previous = start
        all_path_withdrawals_funded = True
        path_goals = {goal_id: True for goal_id in hard_goal_ids}
        for flow in flows:
            wealth = _accrue(wealth, start, previous, flow["date"], returns, fee, tax_drag)
            if flow["type"] == "contribution":
                wealth += flow["amount"]
            else:
                funded = wealth + 1e-9 >= flow["amount"]
                wealth = max(0.0, wealth - flow["amount"])
                if not funded:
                    all_path_withdrawals_funded = False
                if flow["hard"] and not funded:
                    goal_id = flow["goal_id"]
                    path_goals[goal_id] = False
            previous = flow["date"]
        wealth = _accrue(wealth, start, previous, end, returns, fee, tax_drag)
        terminals.append(wealth)
        all_withdrawals_fulfilled += int(all_path_withdrawals_funded)
        positive_terminal_wealth += int(wealth > 1e-9)
        for goal_id, funded in path_goals.items():
            goal_success[goal_id] += int(funded)
    result = {
        "currency": currency,
        "horizon": {"as_of": start.isoformat(), "end_date": end.isoformat()},
        "simulations": simulations,
        "all_withdrawals_fulfilled_probability_percent": _pct(
            all_withdrawals_fulfilled / simulations
        ),
        "positive_terminal_wealth_probability_percent": _pct(
            positive_terminal_wealth / simulations
        ),
        "terminal_wealth_percentiles": {
            "p10": _money(_percentile(terminals, .10), currency),
            "p50": _money(_percentile(terminals, .50), currency),
            "p90": _money(_percentile(terminals, .90), currency),
        },
        "hard_goal_funded_probability_percent": {
            goal_id: _pct(goal_success[goal_id] / simulations) for goal_id in hard_goal_ids
        },
    }
    assumptions = [
        "Results are conditional on the supplied return model, cash flows, inflation, fees, tax drag, and horizon.",
        "Cash flows occur on their stated dates; contributions are applied before withdrawals that share a date.",
        "All-withdrawals-fulfilled and positive-terminal-wealth probabilities are separate; a plan may spend exactly to zero after meeting every withdrawal.",
        "A missed hard goal remains failed even if later contributions restore positive wealth.",
    ]
    return _envelope("ready", result=result, assumptions=assumptions)


def _income_path(initial: float, returns: list[float], need: float, need_growth: float,
                 dividend_yield: float, fee: float, tax_drag: float, strategy: str) -> dict[str, float | bool]:
    wealth = initial
    total_deficit = 0.0
    sales = 0.0
    dividends_used = 0.0
    depleted = False
    for year, total_return in enumerate(returns):
        annual_need = need * ((1 + need_growth) ** year)
        opening = wealth
        if strategy == "dividend_cash":
            total_value = opening * (1 + total_return) * (1 - fee) * (1 - tax_drag)
            # The dividend is cash carved out of total portfolio value, not a
            # second return source.  Cap it at total value for pathological
            # caller-supplied combinations of yield and negative total return.
            dividend = min(opening * dividend_yield * (1 - fee) * (1 - tax_drag), total_value)
            used = min(dividend, annual_need)
            dividends_used += used
            cash_need = annual_need - used
            # Unused distributions are reinvested, so value after spending the
            # used dividend equals total value less that spending.
            wealth = total_value - used
        else:
            wealth = opening * (1 + total_return) * (1 - fee) * (1 - tax_drag)
            cash_need = annual_need
        sold = min(wealth, cash_need)
        sales += sold
        wealth -= sold
        deficit = cash_need - sold
        total_deficit += deficit
        depleted = depleted or deficit > 1e-9
        wealth = max(0.0, wealth)
    return {"terminal": wealth, "deficit": total_deficit, "sales": sales,
            "dividends_used": dividends_used, "depleted": depleted}


def _simulate_income(data: dict[str, Any]) -> dict[str, Any]:
    required = _required(data, ["currency", "initial_wealth", "years", "annual_income_need",
                                "annual_need_growth", "annual_dividend_yield", "annual_fee",
                                "annual_tax_drag", "simulations", "seed", "return_model"])
    if required:
        return _envelope("needs_input", missing=required)
    currency = _currency(data["currency"], "currency")
    initial = _number(data["initial_wealth"], "initial_wealth", minimum=0)
    years = _integer(data["years"], "years", minimum=1)
    need = _number(data["annual_income_need"], "annual_income_need", minimum=0)
    growth = _rate(data["annual_need_growth"], "annual_need_growth")
    dividend_yield = _rate(data["annual_dividend_yield"], "annual_dividend_yield")
    fee = _rate(data["annual_fee"], "annual_fee")
    tax_drag = _rate(data["annual_tax_drag"], "annual_tax_drag")
    simulations = _integer(data["simulations"], "simulations", minimum=1)
    seed = _integer(data["seed"], "seed", minimum=0)
    model = _return_model(data["return_model"], "return_model")
    rng = random.Random(seed)
    paths: dict[str, list[dict[str, float | bool]]] = {"dividend_cash": [], "total_return_sales": []}
    early_bad: list[float] = []
    late_bad: list[float] = []
    for _ in range(simulations):
        returns = _draw_returns(model, years, rng)
        for strategy in paths:
            paths[strategy].append(_income_path(initial, returns, need, growth, dividend_yield,
                                                fee, tax_drag, strategy))
        early_bad.append(float(_income_path(initial, sorted(returns), need, growth, dividend_yield,
                                            fee, tax_drag, "total_return_sales")["terminal"]))
        late_bad.append(float(_income_path(initial, sorted(returns, reverse=True), need, growth,
                                           dividend_yield, fee, tax_drag, "total_return_sales")["terminal"]))
    comparison: dict[str, Any] = {}
    for strategy, records in paths.items():
        terminals = [float(record["terminal"]) for record in records]
        comparison[strategy] = {
            "probability_of_any_income_deficit_percent": _pct(sum(bool(r["depleted"]) for r in records) / simulations),
            "mean_cumulative_deficit": _money(sum(float(r["deficit"]) for r in records) / simulations, currency),
            "mean_sales": _money(sum(float(r["sales"]) for r in records) / simulations, currency),
            "mean_dividends_used": _money(sum(float(r["dividends_used"]) for r in records) / simulations, currency),
            "terminal_wealth_percentiles": {
                "p10": _money(_percentile(terminals, .1), currency),
                "p50": _money(_percentile(terminals, .5), currency),
                "p90": _money(_percentile(terminals, .9), currency),
            },
        }
    result = {
        "currency": currency,
        "strategies": comparison,
        "sequence_risk": {
            "median_terminal_wealth_with_low_returns_first": _money(_percentile(early_bad, .5), currency),
            "median_terminal_wealth_with_high_returns_first": _money(_percentile(late_bad, .5), currency),
            "method": "The same simulated annual returns are reordered low-first and high-first while withdrawals continue.",
        },
    }
    assumptions = [
        "Dividend yield is included within total return; it is not added as a second return source.",
        "Unused dividends are reinvested and income deficits persist even if a later year recovers.",
        "Results are conditional on the supplied return, yield, income-growth, fee, tax-drag, and horizon inputs.",
    ]
    return _envelope("ready", result=result, assumptions=assumptions)


def _fx_converter(data: dict[str, Any], reporting: str):
    raw = data.get("modeled_fx", [])
    if not isinstance(raw, list):
        raise ValueError("modeled_fx must be a list")
    rates: dict[tuple[str, str], float] = {}
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ValueError(f"modeled_fx[{index}] must be an object")
        source = _currency(item.get("from"), f"modeled_fx[{index}].from")
        target = _currency(item.get("to"), f"modeled_fx[{index}].to")
        rate = _number(item.get("rate"), f"modeled_fx[{index}].rate", minimum=0)
        if rate == 0:
            raise ValueError(f"modeled_fx[{index}].rate must be positive")
        rates[(source, target)] = rate

    def convert(amount: float, currency: str, field: str) -> float:
        if currency == reporting:
            return amount
        if (currency, reporting) not in rates:
            raise ValueError(f"{field} currency {currency} requires modeled_fx to {reporting}")
        return amount * rates[(currency, reporting)]
    return convert, bool(rates)


def _ladder(data: dict[str, Any]) -> dict[str, Any]:
    required = _required(data, ["currency", "as_of", "reserve", "liabilities", "assets"])
    if required:
        return _envelope("needs_input", missing=required)
    reporting = _currency(data["currency"], "currency")
    as_of = _date(data["as_of"], "as_of")
    arrears_policy = data.get("arrears_policy", "carry_forward")
    if arrears_policy not in {"carry_forward", "drop_after_miss"}:
        raise ValueError("arrears_policy must be carry_forward or drop_after_miss")
    convert, has_fx = _fx_converter(data, reporting)
    reserve_raw = data["reserve"]
    if not isinstance(reserve_raw, dict):
        raise ValueError("reserve must be an object")
    reserve_currency = _currency(reserve_raw.get("currency"), "reserve.currency")
    balance = convert(_number(reserve_raw.get("amount"), "reserve.amount", minimum=0),
                      reserve_currency, "reserve")
    liabilities_raw = data["liabilities"]
    assets_raw = data["assets"]
    if not isinstance(liabilities_raw, list) or not liabilities_raw:
        raise ValueError("liabilities must be a nonempty list")
    if not isinstance(assets_raw, list):
        raise ValueError("assets must be a list")
    liabilities: list[tuple[date, str, float]] = []
    seen: set[str] = set()
    for index, item in enumerate(liabilities_raw):
        if not isinstance(item, dict):
            raise ValueError(f"liabilities[{index}] must be an object")
        identifier = item.get("id")
        if not isinstance(identifier, str) or not identifier or identifier in seen:
            raise ValueError("liability ids must be nonempty and unique")
        seen.add(identifier)
        when = _date(item.get("date"), f"liabilities[{index}].date")
        if when <= as_of:
            raise ValueError(f"liabilities[{index}].date must be after as_of")
        currency = _currency(item.get("currency"), f"liabilities[{index}].currency")
        amount = convert(_number(item.get("amount"), f"liabilities[{index}].amount", minimum=0),
                         currency, f"liabilities[{index}]")
        liabilities.append((when, identifier, amount))
    inflows: list[tuple[date, str, float]] = []
    asset_ids: set[str] = set()
    for asset_index, asset in enumerate(assets_raw):
        if not isinstance(asset, dict) or not isinstance(asset.get("id"), str):
            raise ValueError(f"assets[{asset_index}] must have an id")
        if not asset["id"] or asset["id"] in asset_ids:
            raise ValueError("asset ids must be nonempty and unique")
        asset_ids.add(asset["id"])
        currency = _currency(asset.get("currency"), f"assets[{asset_index}].currency")
        cashflows = asset.get("cashflows")
        if not isinstance(cashflows, list):
            raise ValueError(f"assets[{asset_index}].cashflows must be a list")
        for flow_index, flow in enumerate(cashflows):
            if not isinstance(flow, dict):
                raise ValueError(f"assets[{asset_index}].cashflows[{flow_index}] must be an object")
            when = _date(flow.get("date"), f"assets[{asset_index}].cashflows[{flow_index}].date")
            if when <= as_of:
                raise ValueError("asset cashflow dates must be after as_of")
            amount = convert(_number(flow.get("amount"), "asset cashflow amount", minimum=0),
                             currency, f"assets[{asset_index}]")
            inflows.append((when, asset["id"], amount))
    inflows.sort()
    liabilities.sort()
    pointer = 0
    coverage: list[dict[str, Any]] = []
    outstanding_arrears = 0.0
    cumulative_on_due_date_gap = 0.0
    for when, identifier, amount in liabilities:
        received = 0.0
        received_ids: list[str] = []
        while pointer < len(inflows) and inflows[pointer][0] <= when:
            _, asset_id, inflow = inflows[pointer]
            balance += inflow
            received += inflow
            received_ids.append(asset_id)
            pointer += 1
        arrears_before = outstanding_arrears
        arrears_paid = 0.0
        if arrears_policy == "carry_forward" and outstanding_arrears > 0:
            arrears_paid = min(balance, outstanding_arrears)
            balance -= arrears_paid
            outstanding_arrears -= arrears_paid
        funded = min(balance, amount)
        balance -= funded
        gap = amount - funded
        cumulative_on_due_date_gap += gap
        if arrears_policy == "carry_forward":
            outstanding_arrears += gap
        coverage.append({
            "liability_id": identifier,
            "date": when.isoformat(),
            "amount": _money(amount, reporting),
            "cashflows_received_since_prior_liability": _money(received, reporting),
            "cashflow_asset_ids": sorted(set(received_ids)),
            "arrears_before": _money(arrears_before, reporting),
            "arrears_paid_before_current_liability": _money(arrears_paid, reporting),
            "funded_on_due_date": _money(funded, reporting),
            "gap_on_due_date": _money(gap, reporting),
            "arrears_after": _money(outstanding_arrears, reporting),
            "reserve_after": _money(balance, reporting),
        })
    later_cashflows = sum(item[2] for item in inflows[pointer:])
    later_applied_to_arrears = 0.0
    if arrears_policy == "carry_forward" and outstanding_arrears > 0:
        later_applied_to_arrears = min(later_cashflows, outstanding_arrears)
        outstanding_arrears -= later_applied_to_arrears
    uncommitted_later_cashflows = later_cashflows - later_applied_to_arrears
    result = {
        "currency": reporting,
        "arrears_policy": arrears_policy,
        "coverage": coverage,
        "cumulative_on_due_date_gap": _money(cumulative_on_due_date_gap, reporting),
        "later_cashflows_applied_to_arrears": _money(later_applied_to_arrears, reporting),
        "ending_outstanding_arrears": _money(outstanding_arrears, reporting),
        "ending_reserve_after_liabilities": _money(balance, reporting),
        "uncommitted_later_cashflows": _money(uncommitted_later_cashflows, reporting),
    }
    assumptions = ["Only the supplied reserve and dated asset cashflows are used; no replacement instruments are invented."]
    if arrears_policy == "carry_forward":
        assumptions.append("Unfunded liabilities remain payable, receive priority over later liabilities, and consume later supplied cashflows before any amount is called uncommitted.")
    else:
        assumptions.append("The caller explicitly chose drop_after_miss: an amount unpaid on its due date is recorded as missed but is not treated as a continuing claim on later cashflows.")
    if has_fx:
        assumptions.append("Cross-currency amounts use the caller-provided fixed modeled FX rates.")
    return _envelope("ready", result=result, assumptions=assumptions)


def run(task: str, inputs: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    """Run a planning calculator.

    Tasks and context fallback keys are ``project``/``planning.project``,
    ``income``/``planning.income``, and ``ladder``/``planning.ladder``.
    Request inputs override the context object field by field.
    """
    if not isinstance(task, str) or task not in {"project", "income", "ladder"}:
        raise ValueError("task must be project, income, or ladder")
    if not isinstance(inputs, dict) or not isinstance(context, dict):
        raise ValueError("inputs and context must be objects")
    contextual = context.get(f"planning.{task}", {})
    if contextual is None:
        contextual = {}
    if not isinstance(contextual, dict):
        raise ValueError(f"context planning.{task} must be an object")
    data = {**contextual, **inputs}
    if task == "project":
        return _simulate_project(data)
    if task == "income":
        return _simulate_income(data)
    return _ladder(data)
