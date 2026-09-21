"""Deterministic, assumption-explicit financial planning calculators.

The module deliberately does not choose capital-market assumptions or invent
securities.  Results are conditional simulations, not forecasts or advice.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date
import math
import random
from typing import Any

from ._common import currency as iso_currency
from ._common import envelope, iso_date, money
from ._common import number as common_number


_DAY = 365.2425
_TIMINGS = {"start", "end"}
_BASES = {"nominal", "real"}


_date = iso_date
_money = money


def _currency(value: Any, field: str) -> str:
    return iso_currency(value, field)


def _number(value: Any, field: str, *, minimum: float | None = None) -> float:
    if isinstance(value, str):
        raise ValueError(f"{field} must be a finite number")
    return common_number(value, field, minimum=minimum)


def _integer(value: Any, field: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{field} must be an integer greater than or equal to {minimum}")
    return value


def _rate(value: Any, field: str, *, allow_negative: bool = False) -> float:
    rate = _number(value, field)
    if rate <= -1 or (not allow_negative and rate < 0):
        raise ValueError(f"{field} must be {'greater than -1' if allow_negative else 'between 0 and 1'}")
    if not allow_negative and rate > 1:
        raise ValueError(f"{field} must be between 0 and 1")
    return rate


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


def _percentiles(values: list[float], currency: str, scale: float = 1.0) -> dict[str, Any]:
    return {label: _money(_percentile(values, q) * scale, currency)
            for label, q in (("p10", .10), ("p50", .50), ("p90", .90))}


def _envelope(status: str, *, result: dict[str, Any] | None = None,
              missing: list[str] | None = None, warnings: list[str] | None = None,
              assumptions: list[str] | None = None) -> dict[str, Any]:
    return envelope(status, result, missing=missing or (), warnings=warnings or (),
                    assumptions=assumptions or ())


def _required(data: dict[str, Any], fields: list[str]) -> list[str]:
    return [field for field in fields if field not in data]


@dataclass(frozen=True)
class _ReturnModel:
    """A seeded annual-return generator in a stated basis (nominal or real)."""

    kind: str
    basis: str
    history: tuple[float, ...] = ()
    block_length: int = 1
    mean: float = 0.0
    volatility: float = 0.0
    degrees_of_freedom: float = 0.0

    def describe(self) -> str:
        if self.kind == "bootstrap":
            return f"iid bootstrap of {len(self.history)} {self.basis} annual returns"
        if self.kind == "block_bootstrap":
            return (f"circular block bootstrap (block length {self.block_length}) of "
                    f"{len(self.history)} {self.basis} annual returns; preserves serial dependence within blocks")
        if self.kind == "student_t":
            return (f"Student-t {self.basis} annual returns, mean {self.mean}, volatility {self.volatility}, "
                    f"{self.degrees_of_freedom} degrees of freedom (variance-matched), floored at -99%")
        return (f"lognormal {self.basis} annual returns with arithmetic mean {self.mean} and "
                f"volatility {self.volatility}")


def _return_model(raw: Any, field: str, warnings: list[str]) -> _ReturnModel:
    if not isinstance(raw, dict):
        raise ValueError(f"{field} must be an object")
    kind = raw.get("type")
    basis = raw.get("basis")
    if basis is None:
        basis = "nominal"
        warnings.append(f"{field}.basis was not stated; returns are treated as NOMINAL. State "
                        "'nominal' or 'real' so inflation is neither ignored nor double counted.")
    if basis not in _BASES:
        raise ValueError(f"{field}.basis must be 'nominal' or 'real'")
    if kind in {"bootstrap", "block_bootstrap"}:
        values = raw.get("historical_annual_returns")
        if not isinstance(values, list) or not values:
            raise ValueError(f"{field}.historical_annual_returns must be a nonempty list")
        history = tuple(_rate(value, f"{field}.historical_annual_returns[{index}]", allow_negative=True)
                        for index, value in enumerate(values))
        if kind == "bootstrap":
            return _ReturnModel(kind, basis, history)
        block = _integer(raw.get("block_length"), f"{field}.block_length", minimum=1)
        if block > len(history):
            raise ValueError(f"{field}.block_length cannot exceed the history length")
        return _ReturnModel(kind, basis, history, block_length=block)
    if kind in {"parametric", "student_t"}:
        mean = _rate(raw.get("annual_return"), f"{field}.annual_return", allow_negative=True)
        volatility = _number(raw.get("annual_volatility"), f"{field}.annual_volatility", minimum=0)
        if kind == "parametric":
            return _ReturnModel(kind, basis, mean=mean, volatility=volatility)
        df = _number(raw.get("degrees_of_freedom"), f"{field}.degrees_of_freedom")
        if df <= 2:
            raise ValueError(f"{field}.degrees_of_freedom must be greater than 2 (finite variance)")
        return _ReturnModel(kind, basis, mean=mean, volatility=volatility, degrees_of_freedom=df)
    raise ValueError(f"{field}.type must be 'bootstrap', 'block_bootstrap', 'parametric' or 'student_t'")


def _draw_returns(model: _ReturnModel, years: int, rng: random.Random) -> tuple[list[float], int]:
    """Annual returns in the model's basis, plus the count of floored draws."""
    if model.kind == "bootstrap":
        return [rng.choice(model.history) for _ in range(years)], 0
    if model.kind == "block_bootstrap":
        out: list[float] = []
        size = len(model.history)
        while len(out) < years:
            start = rng.randrange(size)
            out.extend(model.history[(start + i) % size] for i in range(model.block_length))
        return out[:years], 0
    if model.volatility == 0:
        return [model.mean] * years, 0
    if model.kind == "student_t":
        df = model.degrees_of_freedom
        scale = model.volatility * math.sqrt((df - 2) / df)
        draws, floored = [], 0
        for _ in range(years):
            chi2 = rng.gammavariate(df / 2, 2)
            value = model.mean + scale * rng.normalvariate(0.0, 1.0) / math.sqrt(chi2 / df)
            if value < -0.99:
                value, floored = -0.99, floored + 1
            draws.append(value)
        return draws, floored
    # Convert arithmetic mean/volatility to lognormal parameters.  This keeps
    # every gross return nonnegative without an arbitrary clipping rule.
    variance = model.volatility * model.volatility
    sigma2 = math.log1p(variance / ((1 + model.mean) ** 2))
    mu = math.log1p(model.mean) - sigma2 / 2
    sigma = math.sqrt(sigma2)
    return [math.exp(rng.normalvariate(mu, sigma)) - 1 for _ in range(years)], 0


def _nominal(returns: list[float], basis: str, inflation: float) -> list[float]:
    """Express draws in nominal terms; real draws are compounded with inflation once."""
    if basis == "nominal":
        return returns
    return [(1 + r) * (1 + inflation) - 1 for r in returns]


def _growth(wealth: float, annual_return: float, years: float, fee: float, tax_drag: float) -> float:
    """Grow for ``years`` (possibly fractional): return, then the AUM fee, then tax drag.

    Tax drag is a return drag in percentage points per year charged only on a
    positive net gain and never larger than that gain: loss periods are not
    taxed, and the drag cannot turn a small gain into a loss.
    """
    if wealth <= 0 or years <= 0:
        return max(wealth, 0.0)
    try:
        factor = ((1 + annual_return) ** years) * ((1 - fee) ** years)
        gain = factor - 1.0
        if gain > 0:
            factor -= min(tax_drag * years, gain)
        result = wealth * factor
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


def _years_between(start: date, when: date) -> float:
    """Projection years from ``start``: whole anniversaries plus the elapsed share
    of the current anniversary year. Returns, inflation indexing and deflation
    all use this one clock, so a real return and indexed flows stay consistent."""
    whole = when.year - start.year
    if _add_years(start, whole) > when:
        whole -= 1
    anchor, following = _add_years(start, whole), _add_years(start, whole + 1)
    return whole + (when - anchor).days / (following - anchor).days


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
            amount *= (1 + inflation) ** _years_between(start, when)
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
    warnings: list[str] = []
    currency = _currency(data["currency"], "currency")
    start = _date(data["as_of"], "as_of")
    end = _date(data["end_date"], "end_date")
    if end <= start:
        raise ValueError("end_date must be after as_of")
    initial = _number(data["initial_wealth"], "initial_wealth", minimum=0)
    simulations = _integer(data["simulations"], "simulations", minimum=1)
    seed = _integer(data["seed"], "seed", minimum=0)
    model = _return_model(data["return_model"], "return_model", warnings)
    inflation = _rate(data["annual_inflation"], "annual_inflation")
    fee = _rate(data["annual_fee"], "annual_fee")
    tax_drag = _rate(data["annual_tax_drag"], "annual_tax_drag")
    flows = _expand_flows(data, start, end, inflation)
    horizon_years = max(1, math.ceil((end - start).days / _DAY))
    deflator = (1 + inflation) ** _years_between(start, end)
    rng = random.Random(seed)
    terminals: list[float] = []
    floored = 0
    all_withdrawals_fulfilled = 0
    positive_terminal_wealth = 0
    goal_success: defaultdict[str, int] = defaultdict(int)
    hard_goal_ids = sorted({flow["goal_id"] for flow in flows
                            if flow["type"] == "withdrawal" and flow["hard"]})
    for _ in range(simulations):
        drawn, clipped = _draw_returns(model, horizon_years, rng)
        floored += clipped
        returns = _nominal(drawn, model.basis, inflation)
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
    if floored:
        warnings.append(f"{floored} Student-t annual draws fell below -99% and were floored there.")
    result = {
        "currency": currency,
        "horizon": {"as_of": start.isoformat(), "end_date": end.isoformat()},
        "simulations": simulations,
        "return_model": {"description": model.describe(), "basis": model.basis},
        "all_withdrawals_fulfilled_probability_percent": _pct(
            all_withdrawals_fulfilled / simulations
        ),
        "positive_terminal_wealth_probability_percent": _pct(
            positive_terminal_wealth / simulations
        ),
        "terminal_wealth_percentiles": _percentiles(terminals, currency),
        "real_terminal_wealth_percentiles": _percentiles(terminals, currency, 1 / deflator),
        "real_value_basis": f"{start.isoformat()} money, deflated at {inflation} annual inflation",
        "hard_goal_funded_probability_percent": {
            goal_id: _pct(goal_success[goal_id] / simulations) for goal_id in hard_goal_ids
        },
    }
    assumptions = [
        "Results are conditional on the supplied return model, cash flows, inflation, fees, tax drag, and horizon.",
        f"Return model: {model.describe()}.",
        ("Real returns are converted to nominal once with (1 + real) x (1 + inflation) - 1; inflation-indexed "
         "cash flows are grown at the same inflation, so inflation is counted exactly once."
         if model.basis == "real" else
         "Returns are nominal; inflation-indexed cash flows grow at the stated inflation and real outcomes "
         "are nominal outcomes deflated by the same inflation."),
        "terminal_wealth_percentiles are nominal; real_terminal_wealth_percentiles are in as_of money.",
        "Cash flows occur on their stated dates, so there is no separate start- or end-of-year timing choice; contributions are applied before withdrawals that share a date.",
        "The AUM fee is charged every period; tax drag is percentage points of return charged only on positive net gains and capped at the gain, so loss periods are untaxed.",
        "All-withdrawals-fulfilled and positive-terminal-wealth probabilities are separate; a plan may spend exactly to zero after meeting every withdrawal.",
        "A missed hard goal remains failed even if later contributions restore positive wealth.",
    ]
    return _envelope("ready", result=result, warnings=warnings, assumptions=assumptions)


def _taxes(raw: Any, tax_drag: float) -> dict[str, float] | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError("taxes must be an object")
    if tax_drag:
        raise ValueError("annual_tax_drag must be 0 when explicit taxes are modeled; both would double count tax")
    return {
        "dividend": _rate(raw.get("dividend_tax_rate"), "taxes.dividend_tax_rate"),
        "gains": _rate(raw.get("capital_gains_tax_rate"), "taxes.capital_gains_tax_rate"),
        "basis": _number(raw.get("initial_cost_basis"), "taxes.initial_cost_basis", minimum=0),
        "total_return_yield": _rate(raw.get("total_return_dividend_yield"),
                                    "taxes.total_return_dividend_yield"),
    }


def _income_path(initial: float, returns: list[float], need: float, need_growth: float,
                 dividend_yield: float, fee: float, tax_drag: float, timing: str,
                 taxes: dict[str, float] | None) -> dict[str, float | bool]:
    """One withdrawal path.

    Without ``taxes`` the source of cash is economically irrelevant, so the path
    is a single total-return account with tax drag on positive returns. With
    ``taxes`` dividends are taxed when paid (spent or reinvested), share sales
    realize gains against an average cost basis, and sales are grossed up so
    the net cash covers the need.
    """
    shares, cash = initial, 0.0
    basis = taxes["basis"] if taxes else initial
    total_deficit = sales = dividends_used = taxes_paid = 0.0
    depleted = False

    def spend(amount: float) -> float:
        nonlocal shares, cash, basis, sales, dividends_used, taxes_paid
        from_cash = min(cash, amount)
        cash -= from_cash
        dividends_used += from_cash
        remaining = amount - from_cash
        if remaining <= 0 or shares <= 0:
            return max(remaining, 0.0)
        gain_fraction = max(0.0, 1.0 - basis / shares) if taxes else 0.0
        rate = taxes["gains"] * gain_fraction if taxes else 0.0
        if rate >= 1.0:
            return remaining
        gross = min(shares, remaining / (1.0 - rate))
        net = gross * (1.0 - rate)
        taxes_paid += gross - net
        basis *= 1.0 - gross / shares
        shares -= gross
        sales += gross
        return max(0.0, remaining - net)

    def reinvest() -> None:
        nonlocal shares, cash, basis
        shares += cash
        basis += cash
        cash = 0.0

    for year, total_return in enumerate(returns):
        annual_need = need * ((1 + need_growth) ** year)
        deficit = 0.0
        if timing == "start":
            deficit = spend(annual_need)
            reinvest()
        opening = shares
        grown = opening * (1 + total_return) * (1 - fee)
        if taxes is None:
            gain = grown - opening
            shares = grown - (min(tax_drag * opening, gain) if gain > 0 else 0.0)
        else:
            # The dividend is carved out of total value, not a second return source.
            dividend = min(opening * dividend_yield * (1 - fee), max(grown, 0.0))
            shares = grown - dividend
            tax = taxes["dividend"] * dividend
            taxes_paid += tax
            cash += dividend - tax
        shares = max(0.0, shares)
        if timing == "end":
            deficit = spend(annual_need)
            reinvest()
        total_deficit += deficit
        depleted = depleted or deficit > 1e-9
    terminal = shares + cash
    deferred = taxes["gains"] * max(0.0, shares - basis) if taxes else 0.0
    return {"terminal": terminal, "after_liquidation": terminal - deferred,
            "deficit": total_deficit, "sales": sales, "dividends_used": dividends_used,
            "taxes_paid": taxes_paid, "depleted": depleted}


def _simulate_income(data: dict[str, Any]) -> dict[str, Any]:
    required = _required(data, ["currency", "initial_wealth", "years", "annual_income_need",
                                "annual_need_growth", "annual_dividend_yield", "annual_fee",
                                "annual_tax_drag", "simulations", "seed", "return_model"])
    if required:
        return _envelope("needs_input", missing=required)
    warnings: list[str] = []
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
    model = _return_model(data["return_model"], "return_model", warnings)
    timing = data.get("spending_timing", "start")
    if timing not in _TIMINGS:
        raise ValueError("spending_timing must be 'start' or 'end'")
    inflation = None
    if data.get("annual_inflation") is not None:
        inflation = _rate(data["annual_inflation"], "annual_inflation")
    if model.basis == "real" and inflation is None:
        return _envelope("needs_input", missing=["annual_inflation (required to use real-basis returns)"],
                         warnings=warnings)
    taxes = _taxes(data.get("taxes"), tax_drag)
    strategies = {"total_return_sales": taxes["total_return_yield"] if taxes else 0.0}
    if taxes:
        strategies = {"dividend_cash": dividend_yield, **strategies}
    rng = random.Random(seed)
    paths: dict[str, list[dict[str, float | bool]]] = {name: [] for name in strategies}
    early_bad: list[float] = []
    late_bad: list[float] = []
    floored = 0
    for _ in range(simulations):
        drawn, clipped = _draw_returns(model, years, rng)
        floored += clipped
        returns = _nominal(drawn, model.basis, inflation or 0.0)
        for name, strategy_yield in strategies.items():
            paths[name].append(_income_path(initial, returns, need, growth, strategy_yield,
                                            fee, tax_drag, timing, taxes))
        sequence_yield = strategies["total_return_sales"]
        early_bad.append(float(_income_path(initial, sorted(returns), need, growth, sequence_yield,
                                            fee, tax_drag, timing, taxes)["terminal"]))
        late_bad.append(float(_income_path(initial, sorted(returns, reverse=True), need, growth,
                                           sequence_yield, fee, tax_drag, timing, taxes)["terminal"]))
    if floored:
        warnings.append(f"{floored} Student-t annual draws fell below -99% and were floored there.")
    deflator = (1 + inflation) ** years if inflation is not None else None
    comparison: dict[str, Any] = {}
    for strategy, records in paths.items():
        terminals = [float(record["terminal"]) for record in records]
        comparison[strategy] = {
            "portfolio_dividend_yield": strategies[strategy],
            "probability_of_any_income_deficit_percent": _pct(sum(bool(r["depleted"]) for r in records) / simulations),
            "mean_cumulative_deficit": _money(sum(float(r["deficit"]) for r in records) / simulations, currency),
            "mean_sales": _money(sum(float(r["sales"]) for r in records) / simulations, currency),
            "mean_dividends_used": _money(sum(float(r["dividends_used"]) for r in records) / simulations, currency),
            "mean_taxes_paid": _money(sum(float(r["taxes_paid"]) for r in records) / simulations, currency),
            "terminal_wealth_percentiles": _percentiles(terminals, currency),
            "terminal_wealth_after_liquidation_tax_percentiles": _percentiles(
                [float(r["after_liquidation"]) for r in records], currency),
            "real_terminal_wealth_percentiles": (_percentiles(terminals, currency, 1 / deflator)
                                                 if deflator is not None else None),
        }
    result = {
        "currency": currency,
        "spending_timing": timing,
        "return_model": {"description": model.describe(), "basis": model.basis},
        "strategies": comparison,
        "sequence_risk": {
            "median_terminal_wealth_with_low_returns_first": _money(_percentile(early_bad, .5), currency),
            "median_terminal_wealth_with_high_returns_first": _money(_percentile(late_bad, .5), currency),
            "method": "The same simulated annual returns are reordered low-first and high-first while withdrawals continue (total-return strategy).",
        },
    }
    if taxes is None:
        result["strategy_comparison"] = None
        warnings.append("Dividend-cash and total-return withdrawals are identical without taxes: the same total "
                        "return is spent either way. Supply taxes (dividend and capital-gains rates, cost basis, "
                        "and the total-return portfolio's yield) to compare them.")
    else:
        result["strategy_comparison"] = (
            "Both portfolios earn the same pre-tax total return and differ only in dividend yield: dividends are "
            "taxed when paid; share sales realize gains against average cost basis and are grossed up for tax.")
    assumptions = [
        f"Return model: {model.describe()}.",
        f"Spending occurs at the {timing} of each year"
        + (" (conservative default: spent money misses that year's return)." if timing == "start" else "."),
        "Income need grows at annual_need_growth in nominal terms."
        + (f" Real outcomes deflate nominal terminal wealth at {inflation} annual inflation." if inflation is not None
           else " No annual_inflation was supplied, so real outcomes are not reported."),
        "Dividend yield is included within total return; it is not added as a second return source.",
        "Income deficits persist even if a later year recovers.",
    ]
    if model.basis == "real":
        assumptions.append("Real returns are converted to nominal once with (1 + real) x (1 + inflation) - 1.")
    if taxes is None:
        assumptions.append("annual_tax_drag is percentage points of return charged only in positive-return years and capped at the gain.")
    else:
        assumptions.append("Taxes: dividends at dividend_tax_rate when paid; realized gains at capital_gains_tax_rate on "
                           "average cost basis; realized losses earn no tax credit (conservative); terminal wealth is "
                           "shown before and after the deferred tax on unrealized gains.")
    return _envelope("ready", result=result, warnings=warnings, assumptions=assumptions)


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
