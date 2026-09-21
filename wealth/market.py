"""Integrated historical market analytics and portfolio construction.

This module is a pure adapter around :mod:`wealth.legacy`: it reads only caller
supplied data (or the legacy yfinance adapter when explicitly requested), never
writes client state, and returns the shared domain-module envelope.
"""
from __future__ import annotations

import csv
from datetime import date
from pathlib import Path
import re
from typing import Any

import numpy as np
import pandas as pd

from . import legacy


_TASKS = {"analyze", "stress", "compare", "construct", "factors"}
_METHODS = {"equal", "invvol", "minvar", "riskparity", "hrp", "cvar", "black_litterman"}
_CASH_PREFIX = "CASH::"


def _envelope(status: str, result: dict | None = None, *, missing=(), warnings=(),
              sources=(), assumptions=()) -> dict:
    return {"status": status, "result": result or {}, "missing": list(missing),
            "warnings": list(warnings), "sources": list(sources),
            "assumptions": list(assumptions)}


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a nonempty string")
    return value.strip()


def _currency(value: Any, field: str = "currency") -> str:
    value = _text(value, field)
    if not re.fullmatch(r"[A-Z]{3}", value):
        raise ValueError(f"{field} must be three uppercase letters")
    return value


def _number(value: Any, field: str, *, minimum: float | None = None) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{field} must be a finite number")
    try:
        out = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be a finite number") from exc
    if not np.isfinite(out) or (minimum is not None and out < minimum):
        raise ValueError(f"{field} must be a finite number >= {minimum}")
    return out


def _weights(raw: Any, field: str) -> dict[str, float]:
    if not isinstance(raw, dict) or not raw:
        raise ValueError(f"{field} must be a nonempty object")
    out: dict[str, float] = {}
    for symbol, value in raw.items():
        name = _text(symbol, f"{field} symbol").upper()
        if name in out:
            raise ValueError(f"{field} contains duplicate symbol {name}")
        out[name] = _number(value, f"{field}.{symbol}", minimum=0.0)
    total = sum(out.values())
    if total <= 0:
        raise ValueError(f"{field} must have a positive sum")
    return {symbol: value / total for symbol, value in out.items() if value > 0}


def _validate_cash_symbols(weights: dict[str, float], currency: str, field: str) -> None:
    expected = f"{_CASH_PREFIX}{currency}"
    mismatched = [symbol for symbol in weights if symbol.startswith(_CASH_PREFIX) and symbol != expected]
    if mismatched:
        raise ValueError(f"{field} cash symbols must match portfolio currency {currency}: " +
                         ", ".join(sorted(mismatched)))


def _stored_portfolio(inputs: dict, context: dict) -> tuple[dict | None, str | None, bool, list[str]]:
    """Return value weights without dropping cash or incomplete positions."""
    supplied = inputs.get("household")
    source_name = "inputs.household"
    if supplied is None:
        supplied = context.get("household")
        source_name = "context.household"
    if supplied is None:
        supplied = context.get("portfolio.snapshot")
        source_name = "context.portfolio.snapshot"
    if supplied is None:
        return None, None, True, []
    if not isinstance(supplied, dict):
        raise ValueError(f"{source_name} must be an object")
    currency = _currency(supplied.get("currency"), f"{source_name}.currency")
    positions = supplied.get("positions")
    if not isinstance(positions, list):
        raise ValueError(f"{source_name}.positions must be a list")
    account_ids = inputs.get("account_ids")
    if account_ids is not None:
        if not isinstance(account_ids, list) or not account_ids:
            raise ValueError("account_ids must be a nonempty list")
        selected = {_text(x, "account_ids[]") for x in account_ids}
        positions = [p for p in positions if isinstance(p, dict) and p.get("account_id") in selected]
        scope = "accounts:" + ",".join(sorted(selected))
    else:
        scope = str(supplied.get("scope") or "all stored positions")
    values: dict[str, float] = {}
    missing: list[str] = []
    for index, position in enumerate(positions):
        if not isinstance(position, dict):
            raise ValueError(f"{source_name}.positions[{index}] must be an object")
        if position.get("currency") != currency:
            raise ValueError(f"{source_name}.positions[{index}] requires explicit FX into {currency}")
        if "value" not in position:
            missing.append(f"{source_name}.positions[{index}].value")
            continue
        value = _number(position["value"], f"{source_name}.positions[{index}].value", minimum=0.0)
        is_cash = str(position.get("asset_class", "")).lower() == "cash"
        symbol = f"{_CASH_PREFIX}{currency}" if is_cash else position.get("symbol")
        if not symbol:
            missing.append(f"{source_name}.positions[{index}].symbol")
            continue
        symbol = str(symbol).upper()
        if not is_cash and symbol.startswith(_CASH_PREFIX):
            raise ValueError(f"{source_name}.positions[{index}] uses the reserved cash symbol prefix without asset_class cash")
        if symbol.startswith(_CASH_PREFIX) and symbol != f"{_CASH_PREFIX}{currency}":
            raise ValueError(f"{source_name}.positions[{index}] cash symbol must match portfolio currency {currency}")
        values[symbol] = values.get(symbol, 0.0) + value
    total = sum(values.values())
    if total <= 0 and not missing:
        missing.append(f"{source_name}.positions positive total value")
    portfolio = None if total <= 0 else {
        "weights": {symbol: value / total for symbol, value in values.items() if value > 0},
        "currency": currency, "scope": scope, "total_value": total,
        "complete": supplied.get("complete") is True,
        "source": source_name,
    }
    return portfolio, source_name, supplied.get("complete") is True, missing


def _portfolio(inputs: dict, context: dict, key: str = "weights") -> tuple[dict | None, dict | None, list[str]]:
    if key in inputs:
        currency = _currency(inputs.get("currency"))
        requested = _weights(inputs[key], key)
        _validate_cash_symbols(requested, currency, key)
        return requested, {
            "currency": currency, "scope": str(inputs.get("scope") or "request weights"),
            "complete": True, "source": f"inputs.{key}", "total_value": None,
        }, []
    stored, _, _, missing = _stored_portfolio(inputs, context)
    return (stored or {}).get("weights"), stored, missing


def _inline_prices(spec: dict, tickers: list[str], currency: str) -> pd.DataFrame:
    rows = spec.get("rows")
    if not isinstance(rows, list) or len(rows) < 3:
        raise ValueError("prices.rows must contain at least three observations")
    frame = pd.DataFrame(rows)
    if "date" not in frame:
        raise ValueError("prices.rows require a date field")
    frame.index = pd.to_datetime(frame.pop("date"), errors="raise")
    missing = sorted(set(tickers) - set(frame.columns))
    if missing:
        raise ValueError("prices.rows missing requested assets: " + ", ".join(missing))
    return frame[tickers].astype(float)


def _csv_prices(path: str, tickers: list[str]) -> pd.DataFrame:
    price_path = Path(path).expanduser()
    with price_path.open(newline="", encoding="utf-8-sig") as handle:
        header = next(csv.reader(handle), [])
    if len(header) < 2 or len(set(header[1:])) != len(header[1:]):
        raise ValueError("price_csv has duplicate or missing asset columns")
    frame = pd.read_csv(price_path, index_col=0, parse_dates=True)
    missing = sorted(set(tickers) - set(frame.columns))
    if missing:
        raise ValueError("price_csv missing requested assets: " + ", ".join(missing))
    return frame[tickers]


def _price_frame(inputs: dict, tickers: list[str], currency: str) -> tuple[pd.DataFrame | None, list, list, list]:
    cash = [ticker for ticker in tickers if ticker.startswith(_CASH_PREFIX)]
    market = [ticker for ticker in tickers if ticker not in cash]
    warnings: list[str] = []
    assumptions: list[str] = []
    source: dict
    if "prices" in inputs:
        spec = inputs["prices"]
        if not isinstance(spec, dict):
            raise ValueError("prices must be an object")
        declared = _currency(spec.get("currency"), "prices.currency")
        if declared != currency:
            raise ValueError("prices.currency must match portfolio currency; no implicit FX")
        source_ref = spec.get("source")
        if not source_ref:
            return None, [], [], ["prices.source"]
        px = _inline_prices(spec, market, currency) if market else pd.DataFrame(index=pd.to_datetime([r["date"] for r in spec["rows"]]))
        source = {"kind": "supplied_rows", "ref": str(source_ref)}
    elif "price_csv" in inputs:
        source_ref = inputs.get("price_source")
        if not source_ref:
            return None, [], [], ["price_source"]
        px = _csv_prices(_text(inputs["price_csv"], "price_csv"), market) if market else pd.DataFrame()
        source = {"kind": "supplied_csv", "ref": str(source_ref),
                  "path": str(Path(inputs["price_csv"]).expanduser())}
    else:
        if not market:
            return None, [], [], ["prices or at least one non-cash asset"]
        years = inputs.get("years", 5)
        if isinstance(years, bool) or not isinstance(years, int) or years <= 0:
            raise ValueError("years must be a positive integer")
        px, warnings = legacy._load_prices(market, years=years, currency=currency)
        source = {"kind": "live", "ref": "Yahoo Finance via yfinance adjusted daily closes",
                  "retrieved": px.attrs.get("retrieved")}
        absent = sorted(set(market) - set(px.columns))
        if absent:
            return None, warnings, [source], [f"prices for {symbol}" for symbol in absent]
    if len(px.index) < 3:
        raise ValueError("prices require at least three observations")
    px.index = pd.DatetimeIndex(px.index).tz_localize(None)
    px = px.sort_index()
    for symbol in cash:
        px[symbol] = 1.0
    px = px[tickers]
    legacy.daily_returns(px)
    source["currency"] = currency
    source["window"] = {"start": str(px.index.min().date()), "end": str(px.index.max().date()),
                        "n_prices": int(len(px))}
    px.attrs.update(currency=currency, source=source["ref"], data_kind=source["kind"],
                    retrieved=source.get("retrieved"), risk_free_policy="omit",
                    first_dates={symbol: str(px.index.min().date()) for symbol in px.columns})
    assumptions.append("Supplied prices are adjusted closes already expressed in the declared currency." if source["kind"].startswith("supplied") else "Live prices use the legacy yfinance adjusted-close adapter and its common-date window.")
    if cash:
        assumptions.append("Stored cash is preserved as a constant-price, zero-return allocation.")
    return px, warnings, [source], []


def _metadata(px: pd.DataFrame, currency: str) -> dict:
    return {symbol: {"currency": currency} for symbol in px.columns}


def _analysis(inputs: dict, context: dict) -> dict:
    weights, info, missing = _portfolio(inputs, context)
    if missing:
        return _envelope("needs_input", missing=missing)
    if weights is None:
        return _envelope("needs_input", missing=["weights or household/portfolio.snapshot"])
    currency = info["currency"]
    benchmark = str(inputs.get("benchmark") or next((x for x in weights if not x.startswith(_CASH_PREFIX)), "")).upper()
    tickers = list(weights)
    if benchmark and benchmark not in tickers:
        tickers.append(benchmark)
    px, warnings, sources, price_missing = _price_frame(inputs, tickers, currency)
    if price_missing:
        return _envelope("needs_input", missing=price_missing, warnings=warnings, sources=sources)
    result = legacy.analyze_frame(px, benchmark, weights, warnings, _metadata(px, currency),
                                  str(inputs.get("rebalance", "annual")))
    result["scope"] = info["scope"]
    result["portfolio_total_value"] = info["total_value"]
    result["input_complete"] = info["complete"]
    status = "ready" if info["complete"] else "partial"
    completeness = [] if info["complete"] else ["Stored portfolio is not marked complete; statistics cover only supplied positions."]
    return _envelope(status, result, warnings=[*warnings, *completeness], sources=sources,
                     assumptions=["Historical statistics are descriptive and are not forecasts."])


def _stress(inputs: dict, context: dict) -> dict:
    weights, info, missing = _portfolio(inputs, context)
    if missing:
        return _envelope("needs_input", missing=missing)
    if weights is None:
        return _envelope("needs_input", missing=["weights or household/portfolio.snapshot"])
    scenarios = inputs.get("scenarios")
    if not isinstance(scenarios, list) or not scenarios:
        return _envelope("needs_input", missing=["scenarios"])
    needs_history = any(isinstance(s, dict) and ("start" in s or "end" in s) for s in scenarios)
    sources: list = []
    warnings: list[str] = []
    assumptions = ["Scenario returns are deterministic arithmetic on the supplied portfolio weights; no rebalancing, taxes, or trading costs."]
    px = None
    if needs_history:
        px, warnings, sources, price_missing = _price_frame(inputs, list(weights), info["currency"])
        if price_missing:
            return _envelope("needs_input", missing=price_missing, warnings=warnings, sources=sources)
    rows = []
    for index, scenario in enumerate(scenarios):
        if not isinstance(scenario, dict):
            raise ValueError(f"scenarios[{index}] must be an object")
        name = _text(scenario.get("name"), f"scenarios[{index}].name")
        if "shocks" in scenario:
            shocks = scenario["shocks"]
            if not isinstance(shocks, dict):
                raise ValueError(f"scenarios[{index}].shocks must be an object")
            missing_assets = sorted(set(weights) - set(shocks))
            if missing_assets:
                raise ValueError(f"{name}: shocks missing portfolio assets: {', '.join(missing_assets)}")
            asset_returns = {asset: _number(shocks[asset], f"{name}.{asset}") for asset in weights}
            portfolio_return = sum(weights[a] * asset_returns[a] for a in weights)
            rows.append({"name": name, "kind": "explicit_shock", "asset_returns": asset_returns,
                         "portfolio_return": portfolio_return})
        else:
            start = pd.Timestamp(_text(scenario.get("start"), f"scenarios[{index}].start"))
            end = pd.Timestamp(_text(scenario.get("end"), f"scenarios[{index}].end"))
            if start >= end:
                raise ValueError(f"{name}: start must precede end")
            sample = px.loc[(px.index >= start) & (px.index <= end), list(weights)]
            if len(sample) < 2:
                raise ValueError(f"{name}: fewer than two price observations in window")
            asset_returns = (sample.iloc[-1] / sample.iloc[0] - 1.0).to_dict()
            portfolio_return = sum(weights[a] * float(asset_returns[a]) for a in weights)
            rows.append({"name": name, "kind": "historical_window",
                         "window": {"requested_start": str(start.date()), "requested_end": str(end.date()),
                                    "observed_start": str(sample.index[0].date()), "observed_end": str(sample.index[-1].date()),
                                    "n_prices": len(sample)},
                         "asset_returns": {k: float(v) for k, v in asset_returns.items()},
                         "portfolio_return": portfolio_return})
    result = {"currency": info["currency"], "scope": info["scope"], "weights": weights,
              "scenarios": rows, "input_complete": info["complete"]}
    status = "ready" if info["complete"] else "partial"
    return _envelope(status, result, warnings=warnings, sources=sources, assumptions=assumptions)


def _compare(inputs: dict, context: dict) -> dict:
    current, info, missing = _portfolio(inputs, context, "current_weights")
    if missing:
        return _envelope("needs_input", missing=missing)
    if current is None:
        return _envelope("needs_input", missing=["current_weights or household/portfolio.snapshot"])
    if "proposed_weights" not in inputs:
        return _envelope("needs_input", missing=["proposed_weights"])
    proposed = _weights(inputs["proposed_weights"], "proposed_weights")
    _validate_cash_symbols(proposed, info["currency"], "proposed_weights")
    all_names = list(dict.fromkeys([*current, *proposed]))
    benchmark = str(inputs.get("benchmark") or next((x for x in all_names if not x.startswith(_CASH_PREFIX)), "")).upper()
    if benchmark and benchmark not in all_names:
        all_names.append(benchmark)
    px, warnings, sources, price_missing = _price_frame(inputs, all_names, info["currency"])
    if price_missing:
        return _envelope("needs_input", missing=price_missing, warnings=warnings, sources=sources)
    result = legacy.compare_portfolios(px, current, proposed, benchmark,
                                       str(inputs.get("rebalance", "annual")),
                                       _metadata(px, info["currency"]))
    result.update(currency=info["currency"], scope=info["scope"], input_complete=info["complete"])
    status = "ready" if info["complete"] else "partial"
    return _envelope(status, result, warnings=warnings, sources=sources,
                     assumptions=["Both portfolios use the identical historical sample, currency, and rebalance convention."])


def _factors(inputs: dict, context: dict) -> dict:
    weights, info, missing = _portfolio(inputs, context)
    if missing:
        return _envelope("needs_input", missing=missing)
    if weights is None:
        tickers = inputs.get("tickers")
        if not isinstance(tickers, list) or not tickers:
            return _envelope("needs_input", missing=["tickers or household/portfolio.snapshot"])
        names = [_text(x, "tickers[]").upper() for x in tickers]
        if len(set(names)) != len(names):
            raise ValueError("tickers must be unique")
        info = {"currency": _currency(inputs.get("currency")),
                "scope": str(inputs.get("scope") or "requested universe"),
                "complete": True}
    else:
        names = list(weights)
    if info["currency"] != "USD":
        raise ValueError("factor analysis supports USD prices and US Ken French factors only")
    cash = [name for name in names if name.startswith(_CASH_PREFIX)]
    names = [name for name in names if not name.startswith(_CASH_PREFIX)]
    if not names:
        return _envelope("needs_input", missing=["at least one non-cash asset for factor analysis"])
    px, warnings, sources, price_missing = _price_frame(inputs, names, "USD")
    if price_missing:
        return _envelope("needs_input", missing=price_missing, warnings=warnings, sources=sources)
    model = inputs.get("model", 3)
    if isinstance(model, bool) or model not in (3, 5):
        raise ValueError("model must be 3 or 5")
    result = legacy.factor_regression(px, model, warnings)
    result.update(currency="USD", scope=info["scope"], input_complete=info["complete"])
    factor_source = {"kind": "factor_data", "ref": f"Ken French US daily {model}-factor library",
                     "currency": "USD", "window": result["window"]}
    sources.append(factor_source)
    if not result.get("assets"):
        factor_source["status"] = "unavailable"
        return _envelope("needs_input", result,
                         missing=["factor data coverage for requested assets"],
                         warnings=warnings, sources=sources,
                         assumptions=["No factor inference is returned without common provider coverage."])
    if cash:
        warnings.append("Stored cash was retained in scope but has no market-factor regression; it is listed as excluded cash.")
        result["excluded_cash"] = cash
    status = "ready" if info["complete"] else "partial"
    return _envelope(status, result, warnings=warnings, sources=sources,
                     assumptions=["Factor loadings are in-sample regressions on US daily factors, not forecasts or causal exposures."])


def _hrp(rets: pd.DataFrame, max_weight: float) -> pd.Series:
    from scipy.cluster.hierarchy import leaves_list, linkage
    from scipy.spatial.distance import squareform

    if rets.shape[1] == 1:
        legacy._cap(np.ones(1), max_weight)
        return pd.Series([1.0], index=rets.columns)
    cov = pd.DataFrame(legacy.shrink_covariance(rets)[0], index=rets.columns, columns=rets.columns)
    corr = rets.corr().clip(-1.0, 1.0)
    distance = np.sqrt(np.maximum((1.0 - corr.to_numpy()) / 2.0, 0.0))
    order = leaves_list(linkage(squareform(distance, checks=False), method="single")).tolist()
    ordered = [rets.columns[i] for i in order]
    allocation = pd.Series(1.0, index=ordered)

    def variance(names: list[str]) -> float:
        block = cov.loc[names, names].to_numpy()
        diag = np.diag(block)
        inv = np.divide(1.0, diag, out=np.zeros_like(diag), where=diag > 0)
        ivp = inv / inv.sum() if inv.sum() else np.full(len(names), 1.0 / len(names))
        return float(ivp @ block @ ivp)

    clusters = [ordered]
    while clusters:
        next_clusters = []
        for cluster in clusters:
            if len(cluster) <= 1:
                continue
            split = len(cluster) // 2
            left, right = cluster[:split], cluster[split:]
            left_var, right_var = variance(left), variance(right)
            alpha = 0.5 if left_var + right_var <= 0 else 1.0 - left_var / (left_var + right_var)
            allocation[left] *= alpha
            allocation[right] *= 1.0 - alpha
            next_clusters.extend([left, right])
        clusters = next_clusters
    capped = legacy._cap(allocation.reindex(rets.columns).to_numpy(), max_weight)
    return pd.Series(capped, index=rets.columns)


def _cvar(rets: pd.DataFrame, max_weight: float, confidence: float,
          min_return: float | None) -> pd.Series:
    from scipy.optimize import linprog

    if not 0.5 <= confidence < 1.0:
        raise ValueError("confidence must be at least 0.5 and below 1")
    n_obs, n_assets = rets.shape
    legacy._cap(np.ones(n_assets), max_weight)
    # Variables: weights, VaR threshold alpha (free), nonnegative tail excesses.
    objective = np.r_[np.zeros(n_assets), 1.0,
                      np.full(n_obs, 1.0 / ((1.0 - confidence) * n_obs))]
    losses = -rets.to_numpy(float)
    aub = np.c_[losses, -np.ones(n_obs), -np.eye(n_obs)]
    bub = np.zeros(n_obs)
    if min_return is not None:
        target_daily = (1.0 + min_return) ** (1.0 / legacy.TRADING_DAYS) - 1.0
        aub = np.vstack([aub, np.r_[-rets.mean().to_numpy(), 0.0, np.zeros(n_obs)]])
        bub = np.r_[bub, -target_daily]
    result = linprog(objective, A_ub=aub, b_ub=bub,
                     A_eq=np.r_[np.ones(n_assets), 0.0, np.zeros(n_obs)][None, :],
                     b_eq=[1.0], bounds=[(0.0, max_weight)] * n_assets + [(None, None)] + [(0.0, None)] * n_obs,
                     method="highs")
    if not result.success:
        raise ValueError(f"CVaR optimizer did not produce feasible weights: {result.message}")
    weights = result.x[:n_assets]
    return pd.Series(weights / weights.sum(), index=rets.columns)


def _black_litterman(rets: pd.DataFrame, max_weight: float, inputs: dict) -> tuple[pd.Series, dict]:
    names = list(rets.columns)
    prior_raw = inputs.get("prior_returns")
    if not isinstance(prior_raw, dict) or set(prior_raw) != set(names):
        raise ValueError("prior_returns must identify exactly every construction symbol")
    prior = np.array([_number(prior_raw[name], f"prior_returns.{name}") for name in names])
    views = inputs.get("views")
    if not isinstance(views, list) or not views:
        raise ValueError("views must be a nonempty list")
    tau = _number(inputs.get("tau"), "tau", minimum=0.0)
    risk_aversion = _number(inputs.get("risk_aversion"), "risk_aversion", minimum=0.0)
    if tau <= 0 or risk_aversion <= 0:
        raise ValueError("tau and risk_aversion must be greater than zero")
    annual_cov = legacy.shrink_covariance(rets)[0] * legacy.TRADING_DAYS
    tau_cov = tau * annual_cov
    loadings, expected, omega, view_records = [], [], [], []
    for index, view in enumerate(views):
        if not isinstance(view, dict) or not isinstance(view.get("weights"), dict):
            raise ValueError(f"views[{index}] requires a weights object")
        unknown = set(view["weights"]) - set(names)
        if unknown:
            raise ValueError(f"views[{index}] has unknown symbols: {', '.join(sorted(unknown))}")
        row = np.array([_number(view["weights"].get(name, 0.0),
                                f"views[{index}].weights.{name}") for name in names])
        if np.linalg.norm(row) <= 1e-12:
            raise ValueError(f"views[{index}] has degenerate zero loadings")
        confidence = _number(view.get("confidence"), f"views[{index}].confidence")
        if not 0 < confidence < 1:
            raise ValueError(f"views[{index}].confidence must be strictly between zero and one")
        variance = float(row @ tau_cov @ row)
        if not np.isfinite(variance) or variance <= 1e-18:
            raise ValueError(f"views[{index}] has zero or degenerate variance")
        q = _number(view.get("expected_return"), f"views[{index}].expected_return")
        uncertainty = variance * (1.0 - confidence) / confidence
        loadings.append(row)
        expected.append(q)
        omega.append(uncertainty)
        view_records.append({"weights": {name: float(row[i]) for i, name in enumerate(names) if row[i] != 0},
                             "expected_return": q, "confidence": confidence,
                             "prior_view_return": float(row @ prior),
                             "uncertainty_variance": uncertainty})
    p = np.vstack(loadings)
    q = np.asarray(expected)
    system = p @ tau_cov @ p.T + np.diag(omega)
    try:
        adjustment = tau_cov @ p.T @ np.linalg.solve(system, q - p @ prior)
    except np.linalg.LinAlgError as exc:
        raise ValueError("Black-Litterman view system is singular") from exc
    posterior = prior + adjustment
    weights = legacy._optimize(
        lambda w: float(-(posterior @ w) + 0.5 * risk_aversion * (w @ annual_cov @ w)),
        len(names), max_weight,
    )
    return pd.Series(weights, index=names), {
        "prior_returns": {name: float(prior[i]) for i, name in enumerate(names)},
        "posterior_returns": {name: float(posterior[i]) for i, name in enumerate(names)},
        "views": view_records, "tau": tau, "risk_aversion": risk_aversion,
    }


def _fit_method(rets: pd.DataFrame, method: str, max_weight: float,
                inputs: dict) -> tuple[pd.Series, dict | None]:
    if method == "hrp":
        return _hrp(rets, max_weight), None
    if method == "cvar":
        confidence = _number(inputs.get("confidence", 0.95), "confidence")
        target = inputs.get("min_annual_return")
        min_return = None if target is None else _number(target, "min_annual_return")
        if min_return is not None and min_return <= -1:
            raise ValueError("min_annual_return must be greater than -1")
        return _cvar(rets, max_weight, confidence, min_return), None
    if method == "black_litterman":
        return _black_litterman(rets, max_weight, inputs)
    return legacy.solve_weights(rets, method, max_weight), None


def _tail_loss(rets: pd.DataFrame, weights: pd.Series, confidence: float = 0.95) -> float:
    losses = -(rets[weights.index].to_numpy() @ weights.to_numpy())
    threshold = np.quantile(losses, confidence)
    tail = losses[losses >= threshold]
    return float(tail.mean())


def _drift_weights(target: pd.Series, block: pd.DataFrame) -> pd.Series:
    growth = (1.0 + block[target.index]).prod()
    ending = target * growth
    return ending / ending.sum()


def _validation_metrics(returns: pd.Series, confidence: float) -> dict:
    if returns.empty:
        raise ValueError("validation produced no returns")
    loss = -returns.to_numpy(float)
    threshold = np.quantile(loss, confidence)
    tail = loss[loss >= threshold]
    return {
        "total_return": float((1.0 + returns).prod() - 1.0),
        "annualized_return": float(legacy.ann_return(returns)),
        "annualized_volatility": float(legacy.ann_vol(returns)),
        "max_drawdown": float(legacy.max_drawdown(returns)),
        "historical_daily_cvar_loss": float(tail.mean()),
        "n_days": int(len(returns)),
    }


def _dated_beliefs(spec: dict, first_cutoff: date) -> tuple[list[tuple[date, dict]], list[str]]:
    schedule = spec.get("belief_schedule")
    if not isinstance(schedule, list) or not schedule:
        return [], ["validation.belief_schedule is required for Black-Litterman walk-forward validation"]
    dated: list[tuple[date, dict]] = []
    seen: set[date] = set()
    for index, entry in enumerate(schedule):
        if not isinstance(entry, dict):
            raise ValueError(f"validation.belief_schedule[{index}] must be an object")
        raw_date = entry.get("effective_on")
        if not isinstance(raw_date, str):
            raise ValueError(f"validation.belief_schedule[{index}].effective_on must be an ISO date")
        try:
            effective = date.fromisoformat(raw_date)
        except ValueError as exc:
            raise ValueError(f"validation.belief_schedule[{index}].effective_on must be an ISO date") from exc
        if effective.isoformat() != raw_date:
            raise ValueError(f"validation.belief_schedule[{index}].effective_on must be an ISO date")
        if effective in seen:
            raise ValueError("validation.belief_schedule effective_on dates must be unique")
        if not isinstance(entry.get("prior_returns"), dict) or not isinstance(entry.get("views"), list) or not entry["views"]:
            raise ValueError(f"validation.belief_schedule[{index}] requires complete prior_returns and views")
        seen.add(effective)
        dated.append((effective, entry))
    dated.sort(key=lambda item: item[0])
    if not any(effective <= first_cutoff for effective, _ in dated):
        return dated, [f"validation.belief_schedule needs beliefs effective on or before {first_cutoff.isoformat()}"]
    return dated, []


def _walk_forward(rets: pd.DataFrame, method: str, inner_cap: float, inputs: dict,
                  initial_risky: pd.Series, cash_weights: dict[str, float]) -> tuple[dict | None, list[str]]:
    spec = inputs.get("validation")
    if spec is None:
        return None, []
    if not isinstance(spec, dict):
        raise ValueError("validation must be an object")
    train_days, test_days = spec.get("train_days"), spec.get("test_days")
    if (isinstance(train_days, bool) or not isinstance(train_days, int) or train_days < 2 or
            isinstance(test_days, bool) or not isinstance(test_days, int) or test_days < 1):
        raise ValueError("validation train_days must be an integer >= 2 and test_days an integer >= 1")
    if "transaction_cost_bps" not in spec:
        raise ValueError("validation.transaction_cost_bps is required, including explicit zero")
    cost_bps = _number(spec["transaction_cost_bps"], "validation.transaction_cost_bps", minimum=0.0)
    required = train_days + test_days
    if len(rets) < required:
        return None, [f"validation requires at least {required} return observations; received {len(rets)}"]
    belief_schedule: list[tuple[date, dict]] = []
    if method == "black_litterman":
        belief_schedule, belief_missing = _dated_beliefs(
            spec, rets.index[train_days - 1].date())
        if belief_missing:
            return None, belief_missing
    confidence = _number(inputs.get("confidence", 0.95), "confidence")
    if not 0.5 <= confidence < 1:
        raise ValueError("confidence must be at least 0.5 and below 1")
    cash_weight = sum(cash_weights.values())
    risky_share = 1.0 - cash_weight

    def full_weights(risky_weights: pd.Series) -> pd.Series:
        out = risky_weights * risky_share
        for cash_name, weight in cash_weights.items():
            out.loc[cash_name] = weight
        return out

    full_rets = rets.copy()
    for cash_name in cash_weights:
        full_rets[cash_name] = 0.0
    prior_risky = initial_risky.reindex(rets.columns).fillna(0.0)
    prior_risky = prior_risky / prior_risky.sum()
    prior_method = full_weights(prior_risky)
    prior_equal = prior_method.copy()
    method_returns, equal_returns, blocks = [], [], []
    cursor = train_days
    while cursor + test_days <= len(rets):
        train = rets.iloc[cursor - train_days:cursor]
        test = rets.iloc[cursor:cursor + test_days]
        fit_inputs = inputs
        belief_effective_on = None
        if method == "black_litterman":
            effective, beliefs = max(
                (item for item in belief_schedule if item[0] <= train.index[-1].date()),
                key=lambda item: item[0])
            fit_inputs = dict(inputs)
            fit_inputs.update(prior_returns=beliefs["prior_returns"], views=beliefs["views"])
            belief_effective_on = effective.isoformat()
        method_risky, _ = _fit_method(train, method, inner_cap, fit_inputs)
        equal_risky = legacy.solve_weights(train, "equal", inner_cap)
        method_target, equal_target = full_weights(method_risky), full_weights(equal_risky)
        method_turnover = 0.5 * float((method_target - prior_method).abs().sum())
        equal_turnover = 0.5 * float((equal_target - prior_equal).abs().sum())
        full_test = full_rets.loc[test.index]
        method_gross = legacy.portfolio_returns(full_test, method_target, "none")
        equal_gross = legacy.portfolio_returns(full_test, equal_target, "none")
        method_net, equal_net = method_gross.copy(), equal_gross.copy()
        method_cost = method_turnover * cost_bps / 10_000.0
        equal_cost = equal_turnover * cost_bps / 10_000.0
        method_net.iloc[0] -= method_cost
        equal_net.iloc[0] -= equal_cost
        method_returns.append(method_net)
        equal_returns.append(equal_net)
        blocks.append({
            "train_window": {"start": str(train.index[0].date()), "end": str(train.index[-1].date()),
                             "n_days": len(train)},
            "test_window": {"start": str(test.index[0].date()), "end": str(test.index[-1].date()),
                            "n_days": len(test)},
            "method_weights": {k: float(v) for k, v in method_target.items()},
            "equal_weights": {k: float(v) for k, v in equal_target.items()},
            "method_turnover": method_turnover, "equal_turnover": equal_turnover,
            "method_cost_return": method_cost, "equal_cost_return": equal_cost,
        })
        if belief_effective_on is not None:
            blocks[-1]["belief_effective_on"] = belief_effective_on
        prior_method = _drift_weights(method_target, full_test)
        prior_equal = _drift_weights(equal_target, full_test)
        cursor += test_days
    method_series, equal_series = pd.concat(method_returns), pd.concat(equal_returns)
    return {
        "design": "rolling fixed-length training window followed by the next non-overlapping test block",
        "train_days": train_days, "test_days": test_days,
        "transaction_cost_bps": cost_bps,
        "turnover_convention": "one-way turnover is half the absolute weight change from prior block-end drifted weights; cost is deducted on the next block's first return",
        "method": _validation_metrics(method_series, confidence),
        "equal_weight_baseline": _validation_metrics(equal_series, confidence),
        "blocks": blocks,
        "interpretation": "historical out-of-sample walk-forward evidence; it does not prove future advantage",
    }, []


def _construction(inputs: dict, context: dict) -> dict:
    method = str(inputs.get("method", "equal")).lower()
    if method not in _METHODS:
        raise ValueError("method must be one of " + ", ".join(sorted(_METHODS)))
    current, info, missing = _portfolio(inputs, context)
    if missing:
        return _envelope("needs_input", missing=missing)
    if current is None:
        tickers = inputs.get("tickers")
        if not isinstance(tickers, list) or not tickers:
            return _envelope("needs_input", missing=["tickers or household/portfolio.snapshot"])
        names = [_text(x, "tickers[]").upper() for x in tickers]
        if len(set(names)) != len(names):
            raise ValueError("tickers must be unique")
        currency = _currency(inputs.get("currency"))
        current = {name: 1.0 / len(names) for name in names}
        info = {"currency": currency, "scope": str(inputs.get("scope") or "requested universe"),
                "complete": True, "source": "inputs.tickers", "total_value": None}
    _validate_cash_symbols(current, info["currency"], "construction universe")
    cash_weight = sum(weight for symbol, weight in current.items() if symbol.startswith(_CASH_PREFIX))
    risky = [symbol for symbol in current if not symbol.startswith(_CASH_PREFIX)]
    if not risky:
        return _envelope("needs_input", missing=["at least one non-cash construction asset"])
    max_weight = _number(inputs.get("max_weight", 1.0), "max_weight", minimum=0.0)
    if max_weight <= 0 or max_weight > 1:
        raise ValueError("max_weight must be greater than zero and at most one")
    px, warnings, sources, price_missing = _price_frame(inputs, risky, info["currency"])
    if price_missing:
        return _envelope("needs_input", missing=price_missing, warnings=warnings, sources=sources)
    rets = legacy.daily_returns(px[risky])
    risky_share = 1.0 - cash_weight
    if risky_share <= 0:
        return _envelope("needs_input", missing=["positive non-cash portfolio share"])
    inner_cap = min(1.0, max_weight / risky_share)
    inner, method_detail = _fit_method(rets, method, inner_cap, inputs)
    proposed = {symbol: float(weight * risky_share) for symbol, weight in inner.items()}
    for symbol, weight in current.items():
        if symbol.startswith(_CASH_PREFIX) and weight > 0:
            proposed[symbol] = weight
    if max(proposed.values()) > max_weight + 1e-7:
        raise ValueError(f"infeasible maximum weight {max_weight} with preserved cash allocation")
    equal_inner = legacy.solve_weights(rets, "equal", inner_cap)
    equal = {symbol: float(weight * risky_share) for symbol, weight in equal_inner.items()}
    equal.update({symbol: weight for symbol, weight in current.items() if symbol.startswith(_CASH_PREFIX)})
    confidence = _number(inputs.get("confidence", 0.95), "confidence")
    metrics = lambda w: {
        "annualized_volatility": float((rets[list(w)].cov().to_numpy() * legacy.TRADING_DAYS * np.outer(list(w.values()), list(w.values()))).sum() ** 0.5),
        "historical_daily_cvar_loss": _tail_loss(rets, pd.Series(w), confidence),
    }
    risky_proposed = {k: v / risky_share for k, v in proposed.items() if not k.startswith(_CASH_PREFIX)}
    risky_equal = {k: v / risky_share for k, v in equal.items() if not k.startswith(_CASH_PREFIX)}
    current_risky = {k: v / risky_share for k, v in current.items() if not k.startswith(_CASH_PREFIX)}
    validation, validation_missing = _walk_forward(
        rets, method, inner_cap, inputs, pd.Series(current_risky),
        {k: v for k, v in current.items() if k.startswith(_CASH_PREFIX)})
    if validation_missing:
        return _envelope("needs_input", missing=validation_missing, warnings=warnings, sources=sources)
    result = {"method": method, "currency": info["currency"], "scope": info["scope"],
              "weights": proposed, "cash_weight_preserved": cash_weight,
              "constraints": {"long_only": True, "sum": 1.0, "max_weight": max_weight},
              "historical_metrics_on_risky_sleeve": metrics(risky_proposed),
              "equal_weight_baseline": {"weights": equal,
                                          "historical_metrics_on_risky_sleeve": metrics(risky_equal)},
              "window": legacy._window(rets.index), "input_complete": info["complete"]}
    if method_detail is not None:
        result["model"] = method_detail
    if validation is not None:
        result["validation"] = validation
    assumptions = ["Construction is long-only and uses historical daily returns; it is not an expected-return forecast.",
                   "The equal-weight baseline uses the same universe, cap, cash allocation, and price window."]
    if cash_weight:
        assumptions.append("Stored cash weight is preserved exactly and excluded from covariance optimization.")
    if method == "hrp":
        assumptions.append("HRP uses single-linkage clustering, shrinkage covariance, recursive bisection, then the stated hard cap.")
    if method == "cvar":
        assumptions.append(f"CVaR minimizes empirical daily tail loss at confidence {confidence:.3f} via a constrained linear program.")
    if method == "black_litterman":
        assumptions.append("Black-Litterman combines only the explicit annual prior and relative or absolute views; confidence maps to posterior view uncertainty, not a guaranteed probability of correctness.")
        assumptions.append("Long-only capped weights maximize posterior mean-variance utility using the stated positive risk_aversion.")
    if validation is not None:
        assumptions.append("Walk-forward weights use only each preceding training window; block-end drift determines next-block turnover and the explicit cost deduction.")
    status = "ready" if info["complete"] else "partial"
    return _envelope(status, result, warnings=warnings, sources=sources, assumptions=assumptions)


def run(task: str, inputs: dict, context: dict) -> dict:
    """Run one pure market task using explicit request inputs and eligible context."""
    if task not in _TASKS:
        raise ValueError("unknown market task: " + str(task))
    if not isinstance(inputs, dict) or not isinstance(context, dict):
        raise ValueError("inputs and context must be objects")
    return {"analyze": _analysis, "stress": _stress, "compare": _compare,
            "construct": _construction, "factors": _factors}[task](inputs, context)
