"""Integrated historical market analytics and portfolio construction.

This module is a pure adapter around :mod:`wealth.legacy`: it reads only caller
supplied data (or the legacy yfinance/Ken French adapters when explicitly or
documentedly requested), never writes client state, and returns the shared
domain-module envelope.

Documented defaults (each is also stated in the result's ``assumptions``):

* Benchmark: an explicit ``benchmark`` wins; otherwise USD uses VTI (a broad US
  equity market proxy). Other currencies have no default and omit beta/alpha.
* Risk-free: an explicit ``risk_free`` wins; otherwise live USD prices use the
  Ken French daily RF (one-month T-bill). Caller-supplied prices never trigger
  a hidden fetch, and other currencies have no built-in series: both omit
  Sharpe/alpha with a stated reason and cash earns 0% unless ``risk_free`` is
  ``"ken_french"`` (USD) or ``{"annual_rate", "source"}``.
* Weights must sum to one; ``weights_residual`` = ``cash`` or ``normalize``
  is the only way a partial allocation is completed.
* Covariance: the common (all assets observed) window with Ledoit-Wolf
  shrinkage; ``covariance="pairwise"`` opts into an inception-aware estimate.
"""
from __future__ import annotations

import csv
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date
import math
import re
from pathlib import Path
import threading
from typing import Any, Callable

import numpy as np
import pandas as pd

from . import legacy
from ._common import currency as _iso_currency
from ._common import envelope as _envelope
from ._common import historical_cvar
from ._common import iso_date as _iso_date
from ._common import number as _number
from ._common import text as _text


_TASKS = {"analyze", "stress", "compare", "construct", "factors", "sic_premium"}
_METHODS = {"equal", "invvol", "minvar", "riskparity", "hrp", "cvar", "black_litterman"}
_CASH_PREFIX = "CASH::"
_WEIGHT_TOLERANCE = 1e-6
_ROUNDING_TOLERANCE = 0.01  # hand-rounded weights (0.9998) are rescaled with a note; larger gaps need weights_residual
# The exact portfolio shape a needs_input names, so one retry can supply it.
_PORTFOLIO_SHAPE = ("weights {SYMBOL: fraction} summing to 1, with currency, e.g. {\"currency\": \"MXN\", \"weights\": "
                    "{\"IVV\": 0.6, \"CASH::MXN\": 0.4}}; or household {currency, positions: [{symbol, value, currency, "
                    "asset_class}]}; or client_id with saved statements or synced accounts (their holdings are used)")
_DEFAULT_BENCHMARKS = {
    "USD": ("VTI", "Vanguard Total Stock Market ETF, a broad US equity market proxy"),
}
_MIN_PAIR_OVERLAP = 60
_TRUNCATION_SLACK_DAYS = 21  # one trading month, matching the 30 calendar days of slack in the date test


def _currency(value: Any, field: str = "currency") -> str:
    return _iso_currency(_text(value, field), field)


# --------------------------------------------------------------------------
# weights and portfolio scope
# --------------------------------------------------------------------------
def _weights(raw: Any, field: str) -> dict[str, float]:
    """Validated positive raw weights, *not* rescaled."""
    if not isinstance(raw, dict) or not raw:
        raise ValueError(f"{field} must be a nonempty object")
    out: dict[str, float] = {}
    for symbol, value in raw.items():
        name = _text(symbol, f"{field} symbol").upper()
        if name in out:
            raise ValueError(f"{field} contains duplicate symbol {name}")
        out[name] = _number(value, f"{field}.{symbol}", minimum=0.0)
    if sum(out.values()) <= 0:
        raise ValueError(f"{field} must have a positive sum")
    return {symbol: value for symbol, value in out.items() if value > 0}


def _complete_weights(raw: Any, field: str, currency: str, policy: Any,
                      warnings: list[str]) -> tuple[dict[str, float] | None, list[str]]:
    """Weights that sum to one, or a ``missing`` entry explaining why not.

    A partial allocation is never rescaled silently: the caller must say whether
    the remainder is cash (``weights_residual='cash'``) or whether proportional
    rescaling is intended (``weights_residual='normalize'``).
    """
    weights = _weights(raw, field)
    if policy is not None and policy not in {"cash", "normalize"}:
        raise ValueError("weights_residual must be 'cash' or 'normalize'")
    total = sum(weights.values())
    if abs(total - 1.0) <= _WEIGHT_TOLERANCE:
        return {k: v / total for k, v in weights.items()}, []
    if abs(total - 1.0) < _ROUNDING_TOLERANCE and policy is None:
        warnings.append(f"{field} summed to {total:.6g}, within rounding of 1; rescaled proportionally to 1.")
        return {k: v / total for k, v in weights.items()}, []
    if policy == "normalize":
        warnings.append(f"{field} summed to {total:.6g}; rescaled proportionally to 1 at the caller's "
                        "explicit request (weights_residual='normalize').")
        return {k: v / total for k, v in weights.items()}, []
    if policy == "cash":
        if total > 1.0:
            raise ValueError(f"{field} sum to {total:.6g}; a residual above 100% cannot be completed with cash")
        cash = f"{_CASH_PREFIX}{currency}"
        completed = dict(weights)
        completed[cash] = completed.get(cash, 0.0) + 1.0 - total
        warnings.append(f"{field} summed to {total:.6g}; the unallocated {1.0 - total:.4%} is labelled "
                        f"{cash} at the caller's explicit request (weights_residual='cash').")
        return completed, []
    return None, [f"{field} summing to 1 (received {total:.6g}), or weights_residual='cash'|'normalize'"]


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
        "source": source_name, "warnings": [],
    }
    return portfolio, source_name, supplied.get("complete") is True, missing


def _portfolio(inputs: dict, context: dict, key: str = "weights") -> tuple[dict | None, dict | None, list[str]]:
    weights, info, missing = _raw_portfolio(inputs, context, key)
    if weights and info is not None:
        info = dict(info)
        info["warnings"] = list(info.get("warnings", []))
        info["assumptions"] = list(info.get("assumptions", []))
        weights = _combine_sic(weights, inputs, info.get("source") or key,
                               info["assumptions"], info["warnings"])
        if info.get("weights") is not None:
            info["weights"] = weights
    return weights, info, missing


def _raw_portfolio(inputs: dict, context: dict, key: str = "weights") -> tuple[dict | None, dict | None, list[str]]:
    if key in inputs:
        currency = _currency(inputs.get("currency"))
        warnings: list[str] = []
        requested, missing = _complete_weights(inputs[key], key, currency,
                                               inputs.get("weights_residual"), warnings)
        if requested is None:
            return None, None, missing
        _validate_cash_symbols(requested, currency, key)
        return requested, {
            "currency": currency, "scope": str(inputs.get("scope") or "request weights"),
            "complete": True, "source": f"inputs.{key}", "total_value": None,
            "warnings": warnings,
        }, []
    stored, _, _, missing = _stored_portfolio(inputs, context)
    return (stored or {}).get("weights"), stored, missing


# --------------------------------------------------------------------------
# prices, risk-free, benchmark
# --------------------------------------------------------------------------
@dataclass
class _Prices:
    px: pd.DataFrame | None
    warnings: list = field(default_factory=list)
    sources: list = field(default_factory=list)
    missing: list = field(default_factory=list)
    assumptions: list = field(default_factory=list)
    rf: pd.Series | None = None          # daily decimal on px.index[1:]
    rf_label: str | None = None
    dropped_optional: list = field(default_factory=list)


def _inline_prices(spec: dict, tickers: list[str], optional: set[str]) -> pd.DataFrame:
    rows = spec.get("rows")
    if not isinstance(rows, list) or len(rows) < 3:
        raise ValueError("prices.rows must contain at least three observations")
    frame = pd.DataFrame(rows)
    if "date" not in frame:
        raise ValueError("prices.rows require a date field")
    frame.index = pd.to_datetime(frame.pop("date"), errors="raise")
    missing = sorted(set(tickers) - set(frame.columns) - optional)
    if missing:
        raise ValueError("prices.rows missing requested assets: " + ", ".join(missing))
    return frame[[t for t in tickers if t in frame.columns]].astype(float)


def _csv_prices(path: str, tickers: list[str], optional: set[str]) -> pd.DataFrame:
    price_path = Path(path).expanduser()
    with price_path.open(newline="", encoding="utf-8-sig") as handle:
        header = next(csv.reader(handle), [])
    if len(header) < 2 or len(set(header[1:])) != len(header[1:]):
        raise ValueError("price_csv has duplicate or missing asset columns")
    frame = pd.read_csv(price_path, index_col=0, parse_dates=True)
    missing = sorted(set(tickers) - set(frame.columns) - optional)
    if missing:
        raise ValueError("price_csv missing requested assets: " + ", ".join(missing))
    return frame[[t for t in tickers if t in frame.columns]]


def _risk_free(inputs: dict, return_index: pd.DatetimeIndex, currency: str,
               warnings: list[str], live: bool) -> tuple[pd.Series | None, str | None, dict | None]:
    """Daily decimal risk-free series on ``return_index``, its label and source.

    ``risk_free`` may be ``"none"``, ``"ken_french"`` (USD one-month T-bill,
    fetched), or ``{"annual_rate", "source"}``. Unstated, live USD prices use
    Ken French; caller-supplied prices stay offline and omit it with a warning.
    """
    spec = inputs.get("risk_free")
    if spec == "none":
        warnings.append("risk_free='none': Sharpe and alpha are omitted and cash earns 0%.")
        return None, None, None
    if isinstance(spec, dict):
        annual = _number(spec.get("annual_rate"), "risk_free.annual_rate")
        if annual <= -1:
            raise ValueError("risk_free.annual_rate must be greater than -1")
        source = _text(spec.get("source"), "risk_free.source")
        if "currency" in spec and _currency(spec["currency"], "risk_free.currency") != currency:
            raise ValueError("risk_free.currency must match the portfolio currency")
        daily = (1.0 + annual) ** (1.0 / legacy.TRADING_DAYS) - 1.0
        label = (f"constant {annual:.4%} annual {currency} risk-free assumption compounded daily "
                 f"({source})")
        return (pd.Series(daily, index=return_index), label,
                {"kind": "risk_free_assumption", "ref": source, "currency": currency,
                 "annual_rate": annual})
    if spec not in (None, "ken_french"):
        raise ValueError("risk_free must be 'none', 'ken_french', or an object with annual_rate and source")
    if spec == "ken_french" and currency != "USD":
        raise ValueError("risk_free='ken_french' is the US one-month T-bill and applies only to USD portfolios")
    if spec is None and currency == "USD" and not live:
        warnings.append("Supplied prices keep this run offline, so no risk-free series was fetched: Sharpe and "
                        "alpha are omitted and cash earns 0%. Pass risk_free='ken_french' (USD T-bill, fetched) "
                        "or risk_free.annual_rate with a source.")
        return None, None, None
    if currency != "USD":
        warnings.append(f"No built-in {currency} risk-free series: Sharpe and alpha are omitted and cash earns 0%. "
                        f"Supply risk_free.annual_rate with a source to compute them.")
        return None, None, None
    rf = legacy._rf_daily(return_index, warnings, "USD")
    if rf is None:
        return None, None, None
    label = "Ken French daily USD RF (one-month T-bill)"
    return rf, label, {"kind": "risk_free", "ref": "Ken French US daily RF (one-month T-bill)",
                       "currency": "USD"}


def _common_window(px: pd.DataFrame, requested_years: int | None, warnings: list[str]) -> dict:
    """Describe (and warn about) how asset histories truncate the common sample."""
    coverage = px.attrs.get("coverage") or {}
    first = {symbol: (coverage.get(symbol) or {}).get("first") for symbol in px.columns}
    start = px.index.min()
    limited = sorted(s for s, d in first.items() if d and pd.Timestamp(d) >= start)
    detail = {"start": str(start.date()), "end": str(px.index.max().date()), "n_prices": int(len(px)),
              "asset_first_available": {k: v for k, v in first.items() if v}}
    if requested_years:
        requested_start = px.index.max() - pd.Timedelta(days=int(round(365.25 * requested_years)))
        short_days = (start - requested_start).days
        detail["requested_years"] = requested_years
        # 252 x years observations is a full sample by the annualization convention, even though 252 business
        # days span only about 353 calendar days; allow the same month of slack in observations as in days.
        full_sample = len(px) >= legacy.TRADING_DAYS * requested_years - _TRUNCATION_SLACK_DAYS
        if short_days > 30 and not full_sample:
            detail["truncated_by"] = limited
            warnings.insert(0, f"TRUNCATED SAMPLE: the common window starts {start.date()}, about "
                               f"{short_days / 365.25:.1f} years short of the requested {requested_years}y, "
                               f"because {', '.join(limited) or 'some assets'} lack earlier history; every "
                               "statistic uses this shorter window.")
    return detail


def _price_frame(inputs: dict, tickers: list[str], currency: str, *, need_rf: bool = False,
                 optional: set[str] | None = None, align: bool = True) -> _Prices:
    optional = set(optional or ())
    cash = [ticker for ticker in tickers if ticker.startswith(_CASH_PREFIX)]
    market = [ticker for ticker in tickers if ticker not in cash]
    out = _Prices(None)
    source: dict
    requested_years = None
    if "prices" in inputs:
        spec = inputs["prices"]
        if not isinstance(spec, dict):
            raise ValueError("prices must be an object")
        declared = _currency(spec.get("currency"), "prices.currency")
        if declared != currency:
            raise ValueError("prices.currency must match portfolio currency; no implicit FX")
        source_ref = spec.get("source")
        if not source_ref:
            out.missing = ["prices.source"]
            return out
        px = (_inline_prices(spec, market, optional) if market
              else pd.DataFrame(index=pd.to_datetime([r["date"] for r in spec["rows"]])))
        source = {"kind": "supplied_rows", "ref": str(source_ref)}
        if declared != SIC_CURRENCY:
            sic_cols = [t for t in market if is_sic_symbol(t)]
            if sic_cols:
                out.warnings.append(f"{', '.join(sic_cols)} quote in {SIC_CURRENCY} on BMV/SIC; the supplied rows are "
                                    f"taken as already converted into {declared} (no implicit FX is applied).")
    elif "price_csv" in inputs:
        source_ref = inputs.get("price_source")
        if not source_ref:
            out.missing = ["price_source"]
            return out
        px = _csv_prices(_text(inputs["price_csv"], "price_csv"), market, optional) if market else pd.DataFrame()
        source = {"kind": "supplied_csv", "ref": str(source_ref),
                  "path": str(Path(inputs["price_csv"]).expanduser())}
    else:
        if not market:
            out.missing = ["prices or at least one non-cash asset"]
            return out
        years = inputs.get("years", 5)
        if isinstance(years, bool) or not isinstance(years, int) or years <= 0:
            raise ValueError("years must be a positive integer")
        requested_years = years
        sic_notes: list[str] = []
        if any(is_sic_symbol(t) for t in market):
            with _sic_quote_currencies(sic_notes):
                px, out.warnings = legacy._load_prices(market, years=years, currency=currency, align=align)
        else:
            px, out.warnings = legacy._load_prices(market, years=years, currency=currency, align=align)
        out.warnings.extend(sic_notes)
        source = {"kind": "live", "ref": "Yahoo Finance via yfinance adjusted daily closes",
                  "retrieved": px.attrs.get("retrieved")}
        absent = sorted(set(market) - set(px.columns))
        required_absent = [s for s in absent if s not in optional]
        if required_absent:
            out.sources = [source]
            out.missing = [f"prices for {symbol}" for symbol in required_absent]
            return out
    out.dropped_optional = sorted(set(market) & optional - set(px.columns))
    market = [t for t in market if t in px.columns]
    px.index = pd.DatetimeIndex(px.index).tz_localize(None)
    px = px.sort_index()
    if align and market and px[market].isna().any().any():
        before = len(px)
        attrs = dict(px.attrs)
        px = px.dropna(subset=market, how="any")
        px.attrs.update(attrs)
        out.warnings.insert(0, f"TRUNCATED SAMPLE: {before - len(px)} supplied price rows with a missing "
                               "asset were dropped so every statistic uses one common window "
                               f"({px.index.min().date()} to {px.index.max().date()}).")
    if len(px.index) < 3:
        raise ValueError("prices require at least three observations")
    px = px[market].copy() if market else pd.DataFrame(index=px.index)
    if align:
        if market:
            legacy.daily_returns(px)
    else:
        empty = [symbol for symbol in market if px[symbol].notna().sum() < 2]
        if empty:
            raise ValueError("prices have fewer than two observations for: " + ", ".join(empty))
        values = px.to_numpy(float)
        if (values[np.isfinite(values)] <= 0).any() or np.isinf(values).any():
            raise ValueError("prices must be positive and finite where observed")
    return_index = px.index[1:]
    rf, rf_label, rf_source = (None, None, None)
    if need_rf or cash:
        rf, rf_label, rf_source = _risk_free(inputs, return_index, currency, out.warnings,
                                             live=source["kind"] == "live")
    for symbol in cash:
        if rf is not None:
            px[symbol] = np.r_[1.0, np.cumprod(1.0 + rf.to_numpy(float))]
        else:
            px[symbol] = 1.0
    px = px[[t for t in tickers if t in px.columns]]
    source["currency"] = currency
    source["window"] = {"start": str(px.index.min().date()), "end": str(px.index.max().date()),
                        "n_prices": int(len(px))}
    if source["kind"] == "live" and align:
        source["common_window"] = _common_window(px, requested_years, out.warnings)
    px.attrs.update(currency=currency, source=source["ref"], data_kind=source["kind"],
                    retrieved=source.get("retrieved"),
                    risk_free_policy="supplied" if rf is not None else "omit",
                    first_dates=px.attrs.get("first_dates") or
                    {symbol: str(px[symbol].first_valid_index().date()) for symbol in px.columns})
    out.assumptions.append("Supplied prices are adjusted closes already expressed in the declared currency."
                           if source["kind"].startswith("supplied") else
                           "Live prices use the legacy yfinance adjusted-close adapter.")
    sic_live = [t for t in market if is_sic_symbol(t)] if source["kind"] == "live" else []
    if sic_live:
        source["quote_currencies"] = {t: SIC_CURRENCY for t in sic_live}
        out.assumptions.append(f"{', '.join(sic_live)} are {SIC_SUFFIX} (BMV/SIC) listings quoted in {SIC_CURRENCY}" +
                               ("." if currency == SIC_CURRENCY else
                                f", converted to {currency} at the prior available {SIC_CURRENCY}->{currency} "
                                "daily close (maximum four calendar days)."))
    if cash:
        out.assumptions.append(f"Stored cash compounds at the {rf_label}." if rf is not None else
                               "Stored cash is a constant-price, zero-return allocation because no "
                               f"{currency} risk-free series is available.")
    out.px, out.rf, out.rf_label = px, rf, rf_label
    out.sources = [source] + ([rf_source] if rf_source else [])
    return out


def _benchmark(inputs: dict, currency: str, warnings: list[str],
               assumptions: list[str]) -> tuple[str | None, str]:
    explicit = inputs.get("benchmark")
    if explicit:
        return _text(explicit, "benchmark").upper(), "explicit"
    default = _DEFAULT_BENCHMARKS.get(currency)
    if default is None:
        warnings.append(f"No benchmark supplied and no documented {currency} default; beta and alpha are "
                        "omitted. Pass benchmark explicitly.")
        return None, "none"
    assumptions.append(f"Benchmark defaulted to {default[0]} ({default[1]}), the documented {currency} "
                       "default; pass benchmark to override.")
    return default[0], "currency_default"


def _metadata(px: pd.DataFrame, currency: str, inputs: dict, warnings: list[str]) -> dict:
    meta: dict[str, dict] = {symbol: {"currency": currency} for symbol in px.columns}
    quoted = px.attrs.get("quote_currencies") or {}
    for symbol in meta:
        native = quote_currency(symbol, quoted.get(symbol))
        if native:
            meta[symbol]["quote_currency"] = native
    raw = inputs.get("expense_ratios")
    if raw is None:
        return meta
    if not isinstance(raw, dict):
        raise ValueError("expense_ratios must be an object of decimal annual ratios")
    source = _text(inputs.get("expense_ratio_source"), "expense_ratio_source")
    for symbol, value in raw.items():
        name = _text(symbol, "expense_ratios symbol").upper()
        ratio = _number(value, f"expense_ratios.{symbol}", minimum=0.0)
        if ratio >= 1:
            raise ValueError(f"expense_ratios.{symbol} must be a decimal below one (0.0003 = 0.03%)")
        if ratio > 0.03:
            warnings.append(f"expense_ratios.{name} = {ratio} is above 3%; confirm it is a decimal "
                            "(0.0003 = 0.03%), not a percentage.")
        if name in meta:
            meta[name].update(expense_ratio=ratio, source=source, fee_status="caller-verified")
    for symbol in meta:
        if symbol.startswith(_CASH_PREFIX):
            meta[symbol].update(expense_ratio=0.0, fee_status="cash")
    return meta


# --------------------------------------------------------------------------
# SIC (Sistema Internacional de Cotizaciones, Mexico) listings
# --------------------------------------------------------------------------
SIC_SUFFIX = ".MX"
SIC_CURRENCY = "MXN"
_META_LOCK = threading.RLock()


def is_sic_symbol(symbol: Any) -> bool:
    """True for Yahoo-style BMV/SIC symbols (``AAPL.MX``), which quote in MXN."""
    return isinstance(symbol, str) and symbol.strip().upper().endswith(SIC_SUFFIX) \
        and len(symbol.strip()) > len(SIC_SUFFIX)


def quote_currency(symbol: str, provider_currency: str | None = None) -> str | None:
    """A symbol's native quote currency.

    The ``.MX`` exchange suffix (BMV/SIC) is MXN by construction and wins over
    provider metadata; every other symbol keeps the provider's currency (or
    ``None`` when unknown -- unknown is never assumed).
    """
    if is_sic_symbol(symbol):
        return SIC_CURRENCY
    return str(provider_currency).upper() if provider_currency else None


def _sic_underlyings(raw: Any) -> dict[str, str]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValueError("sic_underlyings must be an object mapping SIC symbols to home listings")
    out: dict[str, str] = {}
    for key, value in raw.items():
        name = _text(key, "sic_underlyings symbol").upper()
        if not is_sic_symbol(name):
            raise ValueError(f"sic_underlyings key {name} must be a {SIC_SUFFIX} symbol")
        target = _text(value, f"sic_underlyings.{key}").upper()
        if is_sic_symbol(target):
            raise ValueError(f"sic_underlyings.{key} must be a home-market listing, not a SIC symbol")
        out[name] = target
    return out


def sic_underlying(symbol: str, mapping: dict[str, str] | None = None) -> str:
    """Home-market underlying of a SIC symbol: explicit mapping, else the suffix stripped."""
    name = _text(symbol, "symbol").upper()
    if not is_sic_symbol(name):
        return name
    explicit = _sic_underlyings(mapping)
    return explicit.get(name) or name[: -len(SIC_SUFFIX)]


def _combine_sic(weights: dict[str, float] | None, inputs: dict, field: str,
                 assumptions: list[str], warnings: list[str]) -> dict[str, float] | None:
    """Merge ``XXX.MX`` weights into their home underlying when explicitly asked.

    Off by default: without ``combine_sic_listings=true`` weights are returned
    unchanged and only a note flags SIC/home pairs held side by side.
    """
    if not weights:
        return weights
    flag = inputs.get("combine_sic_listings", False)
    if not isinstance(flag, bool):
        raise ValueError("combine_sic_listings must be true or false")
    mapping = _sic_underlyings(inputs.get("sic_underlyings"))
    sic = [s for s in weights if is_sic_symbol(s)]
    if not sic:
        return weights
    if not flag:
        pairs = sorted(f"{s}/{sic_underlying(s, mapping)}" for s in sic
                       if sic_underlying(s, mapping) in weights)
        if pairs:
            note = (f"{field} holds SIC and home listings of the same underlying ({', '.join(pairs)}); they are "
                    "analysed as separate assets. Pass combine_sic_listings=true to treat each pair as one exposure.")
            if note not in warnings:
                warnings.append(note)
        return weights
    merged: dict[str, float] = {}
    moved: list[str] = []
    for symbol, weight in weights.items():
        target = sic_underlying(symbol, mapping) if is_sic_symbol(symbol) else symbol
        if target != symbol:
            moved.append(f"{symbol}->{target}")
        merged[target] = merged.get(target, 0.0) + weight
    line = (f"combine_sic_listings=true: SIC listings in {field} are treated as their home-market underlying "
            f"and merged into one exposure ({', '.join(moved)}); the home listing's price history stands in for "
            "the SIC line, so the SIC premium/discount and MXN trading frictions are ignored.")
    if line not in assumptions:
        assumptions.append(line)
    return merged


def _combined_shocks(shocks: dict, inputs: dict, name: str) -> dict:
    """Re-key explicit stress shocks onto merged underlyings when combining SIC listings."""
    if inputs.get("combine_sic_listings") is not True:
        return shocks
    mapping = _sic_underlyings(inputs.get("sic_underlyings"))
    out: dict = {}
    for symbol, value in shocks.items():
        key = str(symbol).upper()
        target = sic_underlying(key, mapping) if is_sic_symbol(key) else symbol
        if target in out and _number(out[target], f"{name}.{target}") != _number(value, f"{name}.{symbol}"):
            raise ValueError(f"{name}: conflicting shocks for {symbol} and its underlying {target}")
        out[target] = value
    return out


@contextmanager
def _sic_quote_currencies(notes: list[str]):
    """Make the legacy price adapter treat ``.MX`` symbols as MXN-quoted.

    ``legacy.to_currency`` reads native currencies from provider metadata; for
    ``.MX`` symbols the exchange suffix fixes the currency at MXN, so a missing or
    contradictory provider field is replaced (and a contradiction is disclosed).
    """
    with _META_LOCK:
        original = legacy._ticker_meta

        def patched(tickers) -> dict:
            meta = dict(original(tickers) or {})
            for symbol in tickers:
                if not is_sic_symbol(symbol):
                    continue
                entry = dict(meta.get(symbol) or {})
                provider = str(entry.get("currency") or "").upper() or None
                if provider and provider != SIC_CURRENCY:
                    notes.append(f"{symbol}: provider reported quote currency {provider}; the {SIC_SUFFIX} "
                                 f"(BMV/SIC) listing is treated as {SIC_CURRENCY}.")
                entry["currency"] = SIC_CURRENCY
                meta[symbol] = entry
            return meta

        legacy._ticker_meta = patched
        try:
            yield
        finally:
            legacy._ticker_meta = original


def _dated_input(value: Any, as_of: Any, field: str, as_of_field: str) -> tuple[float | None, str | None]:
    if value is None:
        return None, None
    amount = _number(value, field)
    if amount <= 0:
        raise ValueError(f"{field} must be positive")
    if as_of is None:
        return amount, None
    if isinstance(as_of, (date, pd.Timestamp)):
        return amount, pd.Timestamp(as_of).date().isoformat()
    return amount, _iso_date(as_of, as_of_field).isoformat()


def _fetch_quote(symbol: str, expected_ccy: str, field: str,
                 warnings: list[str]) -> tuple[float | None, str | None, dict | None]:
    quote = legacy._latest_quote(symbol, warnings)
    if quote is None:
        return None, None, None
    price, qccy, pdate = quote
    qccy = quote_currency(symbol, qccy)
    if qccy != expected_ccy:
        warnings.append(f"{symbol}: quote currency {qccy} is not {expected_ccy}; {field} left unknown.")
        return None, None, None
    return float(price), pdate, {"kind": "live", "ref": "Yahoo Finance via yfinance adjusted daily closes",
                                 "symbol": symbol, "field": field, "currency": qccy, "as_of": pdate,
                                 "retrieved": date.today().isoformat()}


def _fetch_usdmxn(warnings: list[str]) -> tuple[float | None, str | None, dict | None]:
    try:
        series = legacy._fx_series("USD", SIC_CURRENCY)
    except Exception as exc:  # noqa: BLE001 -- provider failure means unknown, not zero
        warnings.append(f"USDMXN: no usable FX history ({exc})")
        return None, None, None
    if series is None or not len(series):
        warnings.append("USDMXN: no usable FX history")
        return None, None, None
    series = series.dropna().sort_index()
    rate, rdate = float(series.iloc[-1]), str(pd.Timestamp(series.index[-1]).date())
    return rate, rdate, {"kind": "live", "ref": "Yahoo Finance via yfinance USDMXN=X daily close",
                         "field": "usdmxn", "as_of": rdate, "retrieved": date.today().isoformat()}


def sic_premium(sic_symbol: str, home_symbol: str | None = None, *,
                sic_price_mxn: float | None = None, sic_price_as_of: str | None = None,
                home_price: float | None = None, home_price_as_of: str | None = None,
                usdmxn: float | None = None, usdmxn_as_of: str | None = None,
                source: str | None = None, fetch_missing: bool = False,
                sic_underlyings: dict[str, str] | None = None) -> dict:
    """Premium (+) or discount (-) of a SIC MXN price over the home USD price x USDMXN.

    ``premium = sic_price_mxn / (home_price * usdmxn) - 1``. Every input may be
    supplied (with its ``*_as_of`` date) or, only when ``fetch_missing`` is true,
    taken from the existing Yahoo adapter. A missing input is never zero: the
    result is ``needs_input`` naming the missing field(s). Inputs dated on
    different days are computed but warned about.
    """
    sic = _text(sic_symbol, "sic_symbol").upper()
    if not is_sic_symbol(sic):
        raise ValueError(f"sic_symbol must be a {SIC_SUFFIX} (BMV/SIC) symbol, e.g. AAPL.MX")
    home = (_text(home_symbol, "home_symbol").upper() if home_symbol is not None
            else sic_underlying(sic, sic_underlyings))
    if is_sic_symbol(home):
        raise ValueError("home_symbol must be the home-market (USD) listing, not a SIC symbol")
    if not isinstance(fetch_missing, bool):
        raise ValueError("fetch_missing must be true or false")
    warnings: list[str] = []
    sources: list[dict] = []
    values = {
        "sic_price_mxn": _dated_input(sic_price_mxn, sic_price_as_of, "sic_price_mxn", "sic_price_as_of"),
        "home_price": _dated_input(home_price, home_price_as_of, "home_price", "home_price_as_of"),
        "usdmxn": _dated_input(usdmxn, usdmxn_as_of, "usdmxn", "usdmxn_as_of"),
    }
    supplied = [name for name, (value, _) in values.items() if value is not None]
    if supplied:
        if not source:
            return _envelope("needs_input", {"sic_symbol": sic, "home_symbol": home},
                             missing=["price_source (who supplied " + ", ".join(supplied) + ")"])
        sources.append({"kind": "supplied", "ref": _text(source, "price_source"), "fields": supplied,
                        "as_of": {name: values[name][1] for name in supplied}})
    if fetch_missing:
        fetchers = {"sic_price_mxn": lambda: _fetch_quote(sic, SIC_CURRENCY, "sic_price_mxn", warnings),
                    "home_price": lambda: _fetch_quote(home, "USD", "home_price", warnings),
                    "usdmxn": lambda: _fetch_usdmxn(warnings)}
        for name, (value, _) in list(values.items()):
            if value is None:
                fetched, fdate, fsource = fetchers[name]()
                if fetched is not None:
                    values[name] = (fetched, fdate)
                    sources.append(fsource)
    inputs_used = {name: {"value": value, "as_of": as_of} for name, (value, as_of) in values.items()}
    base = {"sic_symbol": sic, "home_symbol": home, "home_currency": "USD",
            "sic_currency": SIC_CURRENCY, "inputs": inputs_used}
    missing = [name for name, (value, _) in values.items() if value is None]
    assumptions = [f"{home} is quoted in USD and {sic} in MXN; one {sic} share represents one {home} share "
                   "(no ADR/share-class ratio).",
                   "premium = sic_price_mxn / (home_price x usdmxn) - 1; positive is a SIC premium, "
                   "negative a discount. No bid/ask, commissions, or taxes."]
    if missing:
        if not fetch_missing:
            warnings.append("Missing inputs were not fetched; pass them explicitly or set fetch_missing=true.")
        return _envelope("needs_input", base, missing=missing, warnings=warnings,
                         sources=sources, assumptions=assumptions)
    sic_px, home_px, fx = (values[k][0] for k in ("sic_price_mxn", "home_price", "usdmxn"))
    implied = home_px * fx
    premium = sic_px / implied - 1.0
    dates = {name: as_of for name, (_, as_of) in values.items()}
    undated = [name for name, as_of in dates.items() if as_of is None]
    if undated:
        warnings.append("Undated input(s): " + ", ".join(undated) + "; the comparison cannot be confirmed "
                        "as same-day.")
    distinct = sorted({d for d in dates.values() if d})
    if len(distinct) > 1:
        warnings.append("DATE MISMATCH: inputs are from different dates (" +
                        ", ".join(f"{k} {v}" for k, v in dates.items() if v) +
                        "); part of the premium may be price or FX movement between those dates.")
    result = {**base,
              "implied_sic_price_mxn": implied,
              "sic_price_usd": sic_px / fx,
              "premium": premium,
              "premium_bps": premium * 1e4,
              "direction": "premium" if premium > 0 else ("discount" if premium < 0 else "parity"),
              "as_of": dates,
              "same_day": len(distinct) == 1 and not undated}
    return _envelope("ready", result, warnings=warnings, sources=sources, assumptions=assumptions)


def _sic_premium_task(inputs: dict, context: dict) -> dict:
    if "sic_symbol" not in inputs:
        return _envelope("needs_input", missing=["sic_symbol"])
    report = sic_premium(
        inputs["sic_symbol"], inputs.get("home_symbol"),
        sic_price_mxn=inputs.get("sic_price_mxn"), sic_price_as_of=inputs.get("sic_price_as_of"),
        home_price=inputs.get("home_price"), home_price_as_of=inputs.get("home_price_as_of"),
        usdmxn=inputs.get("usdmxn"), usdmxn_as_of=inputs.get("usdmxn_as_of"),
        source=inputs.get("price_source"), fetch_missing=inputs.get("fetch_missing", False),
        sic_underlyings=inputs.get("sic_underlyings"))
    report["next_step"] = ("This is the price side only. For the tax side of SIC vs a foreign broker (the 10% "
                           "Art. 129 rate vs progressive income, MXN gains including FX, foreign dividends and "
                           "credit), run mx_foreign.")
    return report


# --------------------------------------------------------------------------
# descriptive tasks
# --------------------------------------------------------------------------
# --------------------------------------------------------------------------
# tail risk of the current book: historical and parametric VaR / CVaR
# --------------------------------------------------------------------------
VAR_CONFIDENCE = 0.95
VAR_HORIZONS = {"1d": 1, "1m": 21}  # trading days
# Fewest daily returns before the historical estimate is given: a year for one day (about 13 tail days),
# two years for one month (overlapping 21-day windows, about 24 independent months).
HISTORICAL_MIN_RETURNS = {"1d": 250, "1m": 500}
PARAMETRIC_MIN_RETURNS = 60
_Z95, _PHI_Z95 = 1.6448536269514722, 0.10313564037537128  # standard normal quantile and density at it


def tail_risk(px: pd.DataFrame, weights: dict[str, float], total_value: float | None,
              currency: str) -> dict:
    """Historical and parametric 95% VaR / CVaR, one day and one month, with each position's contribution.

    Losses are positive shares of the portfolio's value; amounts need the portfolio's total value. The
    current weights are applied to the whole price sample, rebalanced daily.
    """
    confidence = VAR_CONFIDENCE
    names = [w for w in weights if w in px.columns]
    w = np.array([weights[n] for n in names], dtype=float)
    rets = legacy.daily_returns(px[names])
    matrix = rets.to_numpy(float)
    port = matrix @ w
    n = int(len(port))
    out: dict[str, Any] = {
        "confidence": confidence, "currency": currency, "portfolio_value": total_value,
        "n_daily_returns": n, "window": {"start": str(rets.index.min().date()), "end": str(rets.index.max().date())},
        "convention": "losses are positive shares of today's portfolio value; the 1-month horizon is 21 trading days",
        "historical": {}, "parametric": {}, "by_position": [],
        "assumptions": ["VaR and CVaR apply today's weights to the whole price sample, rebalanced daily; they "
                        "describe that sample's tails, not a forecast, and a worse day than any in it can happen."],
    }

    def amount(share: float | None) -> float | None:
        return None if share is None or total_value is None else round(share * total_value, 2)

    def pack(var: float | None, cvar: float | None, **extra: Any) -> dict:
        return {"var": None if var is None else round(var, 6), "cvar": None if cvar is None else round(cvar, 6),
                "var_amount": amount(var), "cvar_amount": amount(cvar), **extra}

    tail_mask = None
    for label, days in VAR_HORIZONS.items():
        need = HISTORICAL_MIN_RETURNS[label]
        if n < need:
            out["historical"][label] = pack(None, None, reason=f"needs at least {need} daily returns; the sample "
                                                                 f"has {n}")
            continue
        if days == 1:
            horizon = port
        else:
            growth = pd.Series(1.0 + port).rolling(days).apply(np.prod, raw=True).dropna().to_numpy()
            horizon = growth - 1.0
        losses = -horizon
        var = float(np.quantile(losses, confidence))
        cvar = historical_cvar(losses, confidence)
        extra = {"n_windows": int(len(losses))}
        if days > 1:
            extra["windows"] = "overlapping 21-day windows (neighbours share days, so the tail is thinner than it looks)"
        else:
            tail_mask = losses >= var
        out["historical"][label] = pack(var, cvar, **extra)
    if total_value is None:
        out["amounts_reason"] = "the portfolio's total value is unknown, so only shares are given"
    if n < PARAMETRIC_MIN_RETURNS:
        for label in VAR_HORIZONS:
            out["parametric"][label] = pack(None, None, reason=f"needs at least {PARAMETRIC_MIN_RETURNS} daily "
                                                                f"returns; the sample has {n}")
        return out
    mu_i = matrix.mean(axis=0)
    cov = np.cov(matrix, rowvar=False, ddof=1).reshape(len(names), len(names))
    mu, sigma = float(w @ mu_i), float(math.sqrt(max(w @ cov @ w, 0.0)))
    tail_z = _PHI_Z95 / (1 - confidence)
    marginal = cov @ w / sigma if sigma > 0 else np.zeros(len(names))
    for label, days in VAR_HORIZONS.items():
        m, s = mu * days, sigma * math.sqrt(days)
        out["parametric"][label] = pack(_Z95 * s - m, tail_z * s - m,
                                        mean=round(m, 6), volatility=round(s, 6))
    out["assumptions"].append("Parametric figures assume normal, independent daily returns (one month = 21 days, "
                              "volatility scaled by the square root of time); real tails are usually fatter.")
    for i, name in enumerate(names):
        param = {label: round(float(w[i] * (_Z95 * marginal[i] * math.sqrt(d) - mu_i[i] * d)), 6)
                 for label, d in VAR_HORIZONS.items()}
        hist = None if tail_mask is None else round(float(-(w[i] * matrix[tail_mask, i]).mean()), 6)
        total_hist = out["historical"]["1d"]["cvar"]
        out["by_position"].append({
            "symbol": name, "weight": round(float(w[i]), 6),
            "historical_cvar_1d": hist, "historical_cvar_1d_amount": amount(hist),
            "share_of_historical_cvar_1d": None if hist is None or not total_hist else round(hist / total_hist, 4),
            "parametric_var_1d": param["1d"], "parametric_var_1m": param["1m"],
            "parametric_var_1d_amount": amount(param["1d"]),
            "share_of_parametric_var_1d": (round(param["1d"] / out["parametric"]["1d"]["var"], 4)
                                           if out["parametric"]["1d"]["var"] else None),
        })
    out["contribution_method"] = ("historical: each position's average loss on the sample's worst 5% of days "
                                  "(they add up to the 1-day CVaR); parametric: Euler allocation of normal VaR "
                                  "(they add up to the parametric VaR)")
    if tail_mask is None:
        out["contribution_reason"] = "historical contributions need the 1-day historical estimate"
    return out


def _analysis(inputs: dict, context: dict) -> dict:
    weights, info, missing = _portfolio(inputs, context)
    if missing:
        return _envelope("needs_input", missing=missing)
    if weights is None:
        return _envelope("needs_input", missing=[_PORTFOLIO_SHAPE])
    currency = info["currency"]
    warnings = list(info.get("warnings", []))
    assumptions = ["Historical statistics are descriptive and are not forecasts.", *info.get("assumptions", [])]
    benchmark, basis = _benchmark(inputs, currency, warnings, assumptions)
    tickers = list(weights)
    if benchmark and benchmark not in tickers:
        tickers.append(benchmark)
    optional = {benchmark} if basis == "currency_default" and benchmark not in weights else set()
    prices = _price_frame(inputs, tickers, currency, need_rf=True, optional=optional)
    warnings.extend(prices.warnings)
    if prices.missing:
        return _envelope("needs_input", missing=prices.missing, warnings=warnings, sources=prices.sources)
    if benchmark in prices.dropped_optional:
        warnings.append(f"Default benchmark {benchmark} is not in the supplied prices; beta and alpha are omitted.")
        benchmark, basis = None, "none"
    result = legacy.analyze_frame(prices.px, benchmark or "", weights, warnings,
                                  _metadata(prices.px, currency, inputs, warnings),
                                  str(inputs.get("rebalance", "annual")),
                                  risk_free=prices.rf, risk_free_label=prices.rf_label)
    warnings = result["warnings"]
    result["tail_risk"] = tail_risk(prices.px, weights, info.get("total_value"), currency)
    assumptions.extend(result["tail_risk"]["assumptions"])
    result["benchmark"] = {"symbol": benchmark, "basis": basis}
    result["scope"] = info["scope"]
    result["portfolio_total_value"] = info["total_value"]
    result["input_complete"] = info["complete"]
    status = "ready" if info["complete"] else "partial"
    if not info["complete"]:
        warnings.append("Stored portfolio is not marked complete; statistics cover only supplied positions.")
    return _envelope(status, result, warnings=warnings, sources=prices.sources,
                     assumptions=[*assumptions, *prices.assumptions])


# --------------------------------------------------------------------------
# stress: partial shocks propagate by beta; FX shocks reach the reporting value
# --------------------------------------------------------------------------
_BETA_MIN_RETURNS = _MIN_PAIR_OVERLAP
# Beta to a broad equity index, used only when there is no price history. Round conventions, not estimates:
# they are stated in the result and replaced by a history beta whenever prices allow one.
ASSET_CLASS_BETAS = {
    "equity": 1.0, "stock": 1.0, "equity_fund": 1.0, "etf_equity": 1.0, "reit": 0.8, "real_estate": 0.8,
    "bond": 0.1, "fixed_income": 0.1, "bond_fund": 0.1, "commodity": 0.2, "gold": 0.0, "crypto": 1.5,
    "cash": 0.0, "money_market": 0.0,
}
_EQUITY_CLASSES = {"equity", "stock", "equity_fund", "etf_equity", "index", "equity_index"}
# Broad equity index funds treated as an equity factor when the factor's asset class is not given.
_EQUITY_INDEX_PROXIES = {"SPY", "VOO", "IVV", "VTI", "ITOT", "SCHB", "ACWI", "VT", "QQQ", "IWM", "VEA", "EFA",
                         "VWO", "EEM", "^GSPC", "^IXIC", "NAFTRAC.MX", "^MXX"}
_FX_PAIR = re.compile(r"^[A-Z]{3}[A-Z]{3}$")


def _price_rows_frame(spec: Any, field: str) -> tuple[pd.DataFrame, dict]:
    """``{rows: [{date, SYMBOL: price|null}], source, currency?, currencies?}`` as a frame (gaps kept)."""
    if not isinstance(spec, dict):
        raise ValueError(f"{field} must be an object")
    rows = spec.get("rows")
    if not isinstance(rows, list) or len(rows) < 3:
        raise ValueError(f"{field}.rows must contain at least three observations")
    frame = pd.DataFrame(rows)
    if "date" not in frame:
        raise ValueError(f"{field}.rows require a date field")
    frame.index = pd.to_datetime(frame.pop("date"), errors="raise")
    frame.columns = [str(c).upper() for c in frame.columns]
    frame = frame.sort_index().apply(pd.to_numeric, errors="coerce")
    frame = frame.where(frame > 0)
    source = {"kind": "supplied_rows" if field == "prices" else field, "ref": str(spec.get("source") or field),
              "window": {"start": str(frame.index.min().date()), "end": str(frame.index.max().date()),
                         "n_prices": int(len(frame))}}
    if spec.get("currency"):
        source["currency"] = spec["currency"]
    return frame, source


class _BetaContext:
    """Where betas come from: explicit ``betas``, price history (``beta_prices``, ``prices``, ``price_csv`` or
    the history a historical window loaded), else asset-class defaults for an equity factor."""

    def __init__(self, inputs: dict, context: dict, weights: dict, px: pd.DataFrame | None, info: dict):
        self.warnings: list[str] = []
        self.assumptions: list[str] = []
        self.sources: list[dict] = []
        self.frames: list[tuple[pd.DataFrame, dict]] = []
        self.attributes = _asset_attributes(inputs, context, weights)
        for key in ("beta_prices", "prices"):
            if key in inputs:
                frame, source = _price_rows_frame(inputs[key], key)
                currencies = inputs[key].get("currencies") if isinstance(inputs[key].get("currencies"), dict) else {}
                source["currencies"] = {str(k).upper(): v for k, v in currencies.items()}
                self.frames.append((frame, source))
        if "price_csv" in inputs and inputs.get("price_source"):
            frame = pd.read_csv(Path(str(inputs["price_csv"])).expanduser(), index_col=0, parse_dates=True)
            frame.columns = [str(c).upper() for c in frame.columns]
            self.frames.append((frame.sort_index(), {"kind": "supplied_csv", "ref": str(inputs["price_source"])}))
        if px is not None:
            self.frames.append((px, {"kind": "stress_history", "ref": str(px.attrs.get("source") or "loaded prices")}))
        explicit = inputs.get("betas")
        self.explicit: dict[str, float] = {}
        if explicit is not None:
            if not isinstance(explicit, dict):
                raise ValueError("betas must be an object of {SYMBOL: beta}")
            self.explicit_source = _text(inputs.get("beta_source"), "beta_source")
            self.explicit = {str(k).upper(): _number(v, f"betas.{k}") for k, v in explicit.items()}
        self._cache: dict[tuple[str, str], dict] = {}
        self.used_frames: list[dict] = []

    def history_beta(self, asset: str, factor: str) -> dict:
        key = (asset, factor)
        if key in self._cache:
            return self._cache[key]
        best = {"beta": None, "reason": f"no price history with both {asset} and {factor}"}
        for frame, source in self.frames:
            if asset not in frame.columns or factor not in frame.columns:
                continue
            rets = frame[[asset, factor]].astype(float).pct_change(fill_method=None).dropna(how="any")
            if len(rets) < _BETA_MIN_RETURNS:
                best = {"beta": None, "reason": f"only {len(rets)} overlapping daily returns of {asset} and {factor} "
                                                f"(at least {_BETA_MIN_RETURNS} needed)"}
                continue
            variance = float(rets[factor].var())
            if not variance > 0:
                continue
            beta = float(rets[asset].cov(rets[factor]) / variance)
            corr = float(rets[asset].corr(rets[factor]))
            best = {"beta": round(beta, 4), "basis": "history", "n_returns": int(len(rets)),
                    "window": {"start": str(rets.index.min().date()), "end": str(rets.index.max().date())},
                    "r_squared": round(corr * corr, 4), "source": source.get("ref")}
            if source not in self.sources:
                self.sources.append({k: v for k, v in source.items() if k != "currencies"})
            break
        self._cache[key] = best
        return best

    def factor_is_equity(self, factor: str) -> bool:
        cls = (self.attributes.get(factor) or {}).get("asset_class")
        return (cls in _EQUITY_CLASSES) if cls else factor in _EQUITY_INDEX_PROXIES

    def native_currency(self, symbol: str, reporting: str) -> str | None:
        attrs = self.attributes.get(symbol) or {}
        if attrs.get("currency"):
            return attrs["currency"]
        if symbol.startswith(_CASH_PREFIX):
            return symbol[len(_CASH_PREFIX):]
        if is_sic_symbol(symbol):
            return None  # a SIC line trades in MXN but its value follows its home-market currency
        for _, source in self.frames:
            found = (source.get("currencies") or {}).get(symbol)
            if found:
                return str(found).upper()
        return None


def _asset_attributes(inputs: dict, context: dict, weights: dict) -> dict[str, dict]:
    """Asset class and native (quote) currency per symbol: household positions, then explicit inputs."""
    out: dict[str, dict] = {}
    household = inputs.get("household") or context.get("household") or context.get("portfolio.snapshot")
    if isinstance(household, dict) and "weights" not in inputs:
        for position in household.get("positions") or []:
            if not isinstance(position, dict) or not position.get("symbol"):
                continue
            is_cash = str(position.get("asset_class", "")).lower() == "cash"
            symbol = f"{_CASH_PREFIX}{household.get('currency')}" if is_cash else str(position["symbol"]).upper()
            entry = out.setdefault(symbol, {})
            if position.get("asset_class"):
                entry["asset_class"] = str(position["asset_class"]).lower()
            native = position.get("native_currency") or position.get("quote_currency") or position.get("listing_currency")
            if native:
                entry["currency"] = _currency(native, f"positions[{symbol}].native_currency")
    for key, field, parse in (("asset_classes", "asset_class", lambda v, f: _text(v, f).lower()),
                              ("asset_currencies", "currency", _currency)):
        raw = inputs.get(key)
        if raw is None:
            continue
        if not isinstance(raw, dict):
            raise ValueError(f"{key} must be an object of {{SYMBOL: value}}")
        for symbol, value in raw.items():
            out.setdefault(str(symbol).upper(), {})[field] = parse(value, f"{key}.{symbol}")
    return out


def _fx_factor(native: str, reporting: str, fx_shocks: dict[str, float]) -> tuple[float | None, str | None]:
    """Multiplier on (1 + local return) from the FX shocks, and the pair used; (1, None) when none applies."""
    if native == reporting:
        return 1.0, None
    direct, inverse = native + reporting, reporting + native
    if direct in fx_shocks:
        return 1.0 + fx_shocks[direct], direct
    if inverse in fx_shocks:
        return 1.0 / (1.0 + fx_shocks[inverse]), inverse
    return None, None


def _shock_scenario(name: str, scenario: dict, shocks: dict[str, float], weights: dict[str, float],
                    info: dict, ctx: _BetaContext) -> tuple[dict, list[str]]:
    """One explicit-shock scenario: given shocks, beta-propagated returns for the rest, then FX."""
    reporting = info["currency"]
    fx_raw = scenario.get("fx_shocks") or {}
    if not isinstance(fx_raw, dict):
        raise ValueError(f"{name}: fx_shocks must be an object like {{'USDMXN': 0.15}}")
    fx_shocks: dict[str, float] = {}
    for pair, value in fx_raw.items():
        code = str(pair).upper().replace("/", "")
        if not _FX_PAIR.match(code) or code[:3] == code[3:]:
            raise ValueError(f"{name}: fx_shocks keys are currency pairs like USDMXN (MXN per USD)")
        fx_shocks[code] = _number(value, f"{name}.fx_shocks.{pair}", minimum=-0.99)
    missing: list[str] = []
    factor = scenario.get("factor")
    factor = str(factor).upper() if factor else next(iter(shocks), None)
    if factor is not None and factor not in shocks:
        raise ValueError(f"{name}: factor {factor} needs a shock in shocks")
    local: dict[str, float | None] = {}
    how: dict[str, dict] = {}
    for asset in weights:
        if asset in shocks:
            local[asset], how[asset] = shocks[asset], {"basis": "shocked"}
        elif asset.startswith(_CASH_PREFIX):
            local[asset], how[asset] = 0.0, {"basis": "cash", "beta": 0.0}
        elif factor is None:  # an FX-only scenario: local prices held, only the translation moves
            local[asset], how[asset] = 0.0, {"basis": "held", "beta": None}
        elif asset in ctx.explicit:
            beta = ctx.explicit[asset]
            local[asset], how[asset] = beta * shocks[factor], {"basis": "supplied", "beta": beta,
                                                               "source": ctx.explicit_source}
        else:
            found = ctx.history_beta(asset, factor)
            if found["beta"] is not None:
                local[asset], how[asset] = found["beta"] * shocks[factor], dict(found)
            else:
                cls = (ctx.attributes.get(asset) or {}).get("asset_class")
                default = ASSET_CLASS_BETAS.get(cls) if cls else None
                if default is not None and ctx.factor_is_equity(factor):
                    local[asset] = default * shocks[factor]
                    how[asset] = {"basis": "asset_class_default", "beta": default, "asset_class": cls,
                                  "history": found["reason"]}
                else:
                    why = (f"{found['reason']}, and " +
                           (f"asset class {cls!r} has no default beta" if cls and default is None else
                            "its asset class is unknown" if not cls else
                            f"class defaults are betas to an equity index and {factor} is not one"))
                    local[asset], how[asset] = None, {"basis": "unknown", "beta": None, "reason": why}
                    missing.append(f"{name}: a beta for {asset} to {factor} (price history, betas, or asset_classes)")
        if local[asset] is not None and local[asset] < -1.0:
            how[asset]["clipped_from"] = round(local[asset], 6)
            local[asset] = -1.0  # a long holding cannot lose more than all of it
    returns: dict[str, float | None] = dict(local)
    fx_detail: dict[str, Any] | None = None
    if fx_shocks:
        applied, unknown = {}, []
        for asset, value in local.items():
            native = ctx.native_currency(asset, reporting)
            if native is None:
                unknown.append(asset)
                returns[asset] = None
                continue
            multiplier, pair = _fx_factor(native, reporting, fx_shocks)
            if multiplier is None:
                returns[asset] = value  # no shock given for this currency pair: the rate is held fixed
                applied[asset] = {"currency": native, "pair": None}
                continue
            applied[asset] = {"currency": native, "pair": pair}
            returns[asset] = None if value is None else (1.0 + value) * multiplier - 1.0
        missing += [f"{name}: the currency {a} is priced in (asset_currencies or positions[].native_currency)"
                    for a in unknown]
        fx_detail = {"shocks": fx_shocks, "reporting_currency": reporting, "assets": applied,
                     "unknown_currency": unknown,
                     "note": "Shocks and betas are in each asset's own currency; the FX shock converts them into "
                             f"{reporting}." + (" No price shock was given, so local prices are held and only "
                                                "the translation moves." if factor is None else "")}
    unresolved = sorted(a for a, v in returns.items() if v is None)
    portfolio_return = None if unresolved else sum(weights[a] * returns[a] for a in weights)
    row: dict[str, Any] = {"name": name, "kind": "explicit_shock", "asset_returns": returns,
                           "portfolio_return": portfolio_return}
    if portfolio_return is None:
        row["portfolio_return_reason"] = f"unknown return for {', '.join(unresolved)}"
    total = info.get("total_value")
    if total is not None:
        row["portfolio_change"] = None if portfolio_return is None else round(total * portfolio_return, 2)
    if factor is not None and any(h["basis"] not in ("shocked", "cash") for h in how.values()):
        row["propagation"] = {"factor": factor, "factor_shock": shocks[factor], "assets": how,
                              "method": "return = beta x factor shock (linear, no idiosyncratic move), floored "
                                        "at -100%"}
        if any(h["basis"] == "asset_class_default" for h in how.values()):
            line = ("Assets without enough price history take a round asset-class beta to an equity index "
                    f"({', '.join(f'{k} {v:g}' for k, v in sorted(ASSET_CLASS_BETAS.items()))}); a convention, "
                    "not an estimate.")
            if line not in ctx.assumptions:
                ctx.assumptions.append(line)
        if any(h["basis"] == "history" for h in how.values()):
            line = ("History betas are OLS slopes of daily returns on the shocked factor's daily returns; a large "
                    "shock can move assets further than their everyday beta suggests.")
            if line not in ctx.assumptions:
                ctx.assumptions.append(line)
    if fx_detail is not None:
        row["local_returns"] = local
        row["fx"] = fx_detail
    return row, missing


def stress_price_symbols(inputs: dict, context: dict) -> list[str]:
    """Symbols a partial-shock stress needs betas for (holdings and factors); [] when betas are not needed."""
    scenarios = inputs.get("scenarios")
    if not isinstance(scenarios, list) or any(k in inputs for k in ("prices", "price_csv", "beta_prices")):
        return []
    try:
        weights, _, _ = _portfolio(inputs, context)
    except ValueError:
        return []
    if not weights:
        return []
    needed: set[str] = set()
    for scenario in scenarios:
        shocks = scenario.get("shocks") if isinstance(scenario, dict) else None
        if not isinstance(shocks, dict) or not shocks:
            continue
        keys = [str(k).upper() for k in shocks]
        if set(weights) - set(keys):
            needed |= set(weights) | {str(scenario.get("factor") or keys[0]).upper()}
    return sorted(s for s in needed if not s.startswith(_CASH_PREFIX))


_BROAD_SHOCK_ALIASES = ("equity_shock", "market_shock")


def _broad_shock(scenario: dict, weights: dict, name: str, assumptions: list[str]) -> dict:
    """``{equity_shock: -0.3}`` (or market_shock) as ``shocks {<broad equity proxy>: -0.3}``, the rest by beta.

    The proxy is a broad index fund the portfolio holds (the largest), else SPY; it is the factor.
    """
    alias = next((a for a in _BROAD_SHOCK_ALIASES if a in scenario), None)
    if alias is None:
        return scenario
    others = [a for a in _BROAD_SHOCK_ALIASES if a in scenario and a != alias]
    if others:
        raise ValueError(f"{name}: give one of {' or '.join(_BROAD_SHOCK_ALIASES)}, not both")
    value = scenario[alias]
    held = sorted((s for s in weights if str(s).upper() in _EQUITY_INDEX_PROXIES), key=lambda s: -weights[s])
    proxy = str(held[0]).upper() if held else "SPY"
    shocks = dict(scenario.get("shocks") or {})
    if proxy in {str(k).upper() for k in shocks}:
        raise ValueError(f"{name}: {alias} and shocks both set {proxy}; give one")
    shocks[proxy] = value
    out = {k: v for k, v in scenario.items() if k != alias}
    out["shocks"] = shocks
    out.setdefault("factor", proxy)
    note = (f"{name}: {alias} {value:+.0%} is applied to {proxy} as the broad equity market; every other holding "
            "moves by its beta to it." if isinstance(value, (int, float)) and not isinstance(value, bool) else None)
    if note and note not in assumptions:
        assumptions.append(note)
    return out


def _stress(inputs: dict, context: dict) -> dict:
    weights, info, missing = _portfolio(inputs, context)
    if missing:
        return _envelope("needs_input", missing=missing)
    if weights is None:
        return _envelope("needs_input", missing=[_PORTFOLIO_SHAPE])
    scenarios = inputs.get("scenarios")
    if not isinstance(scenarios, list) or not scenarios:
        return _envelope("needs_input", missing=["scenarios"])
    needs_history = any(isinstance(s, dict) and ("start" in s or "end" in s) for s in scenarios)
    sources: list = []
    warnings: list[str] = list(info.get("warnings", []))
    assumptions = ["Scenario returns are deterministic arithmetic on the supplied portfolio weights; no rebalancing, taxes, or trading costs.",
                   "Historical windows run close-to-close from the first observed close on or after start to the last close on or before end (the same convention as the regime table).",
                   *info.get("assumptions", [])]
    px = None
    if needs_history:
        prices = _price_frame(inputs, list(weights), info["currency"])
        warnings.extend(prices.warnings)
        sources = prices.sources
        if prices.missing:
            return _envelope("needs_input", missing=prices.missing, warnings=warnings, sources=sources)
        px = prices.px
        assumptions.extend(prices.assumptions)
    rows = []
    beta_ctx: _BetaContext | None = None
    missing_inputs: list[str] = []
    for index, scenario in enumerate(scenarios):
        if not isinstance(scenario, dict):
            raise ValueError(f"scenarios[{index}] must be an object")
        name = _text(scenario.get("name") or f"scenario {index + 1}", f"scenarios[{index}].name")
        scenario = _broad_shock(scenario, weights, name, assumptions)
        if "shocks" in scenario or "fx_shocks" in scenario:
            shocks = scenario.get("shocks") or {}
            if not isinstance(shocks, dict):
                raise ValueError(f"scenarios[{index}].shocks must be an object")
            shocks = {str(k).upper(): _number(v, f"{name}.{k}", minimum=-1.0)
                      for k, v in _combined_shocks(shocks, inputs, name).items()}
            if beta_ctx is None:
                beta_ctx = _BetaContext(inputs, context, weights, px, info)
            row, row_missing = _shock_scenario(name, scenario, shocks, weights, info, beta_ctx)
            rows.append(row)
            missing_inputs.extend(m for m in row_missing if m not in missing_inputs)
        else:
            if "start" not in scenario:
                raise ValueError(f"scenarios[{index}] needs shocks {{SYMBOL: return}} (e.g. {{\"name\": \"crash\", "
                                 f"\"shocks\": {{\"SPY\": -0.3}}}}; other holdings follow by beta) or a dated "
                                 f"window {{name, start, end}}; unknown fields: {sorted(set(scenario) - {'name'})}")
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
              "scenarios": rows, "input_complete": info["complete"],
              "portfolio_total_value": info.get("total_value")}
    if beta_ctx is not None:
        warnings.extend(w for w in beta_ctx.warnings if w not in warnings)
        assumptions.extend(a for a in beta_ctx.assumptions if a not in assumptions)
        sources = sources + [s for s in beta_ctx.sources if s not in sources]
    unresolved = [r["name"] for r in rows if r.get("portfolio_return") is None]
    status = "ready" if info["complete"] and not unresolved else "partial"
    return _envelope(status, result, warnings=warnings, sources=sources, assumptions=assumptions,
                     missing=missing_inputs)


def _compare(inputs: dict, context: dict) -> dict:
    current, info, missing = _portfolio(inputs, context, "current_weights")
    if missing:
        return _envelope("needs_input", missing=missing)
    if current is None:
        return _envelope("needs_input", missing=["current_" + _PORTFOLIO_SHAPE])
    if "proposed_weights" not in inputs:
        return _envelope("needs_input", missing=["proposed_weights"])
    warnings = list(info.get("warnings", []))
    proposed, proposed_missing = _complete_weights(inputs["proposed_weights"], "proposed_weights",
                                                   info["currency"], inputs.get("weights_residual"), warnings)
    if proposed is None:
        return _envelope("needs_input", missing=proposed_missing, warnings=warnings)
    _validate_cash_symbols(proposed, info["currency"], "proposed_weights")
    assumptions = ["Both portfolios use the identical historical sample, currency, and rebalance convention.",
                   *info.get("assumptions", [])]
    proposed = _combine_sic(proposed, inputs, "proposed_weights", assumptions, warnings)
    benchmark, basis = _benchmark(inputs, info["currency"], warnings, assumptions)
    all_names = list(dict.fromkeys([*current, *proposed]))
    if benchmark and benchmark not in all_names:
        all_names.append(benchmark)
    optional = {benchmark} if basis == "currency_default" and benchmark not in {*current, *proposed} else set()
    prices = _price_frame(inputs, all_names, info["currency"], need_rf=True, optional=optional)
    warnings.extend(prices.warnings)
    if prices.missing:
        return _envelope("needs_input", missing=prices.missing, warnings=warnings, sources=prices.sources)
    if benchmark in prices.dropped_optional:
        warnings.append(f"Default benchmark {benchmark} is not in the supplied prices; beta and alpha are omitted.")
        benchmark, basis = None, "none"
    result = legacy.compare_portfolios(prices.px, current, proposed, benchmark or "",
                                       str(inputs.get("rebalance", "annual")),
                                       _metadata(prices.px, info["currency"], inputs, warnings),
                                       risk_free=prices.rf, risk_free_label=prices.rf_label)
    result.update(currency=info["currency"], scope=info["scope"], input_complete=info["complete"],
                  benchmark={"symbol": benchmark, "basis": basis})
    for leg in ("current", "proposed"):
        warnings.extend(w for w in result[leg].get("warnings", []) if w not in warnings)
    status = "ready" if info["complete"] else "partial"
    return _envelope(status, result, warnings=warnings, sources=prices.sources,
                     assumptions=[*assumptions, *prices.assumptions])


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
    prices = _price_frame(inputs, names, "USD")
    warnings, sources = list(info.get("warnings", [])) + prices.warnings, prices.sources
    if prices.missing:
        return _envelope("needs_input", missing=prices.missing, warnings=warnings, sources=sources)
    model = inputs.get("model", 3)
    if isinstance(model, bool) or model not in (3, 5):
        raise ValueError("model must be 3 or 5")
    result = legacy.factor_regression(prices.px, model, warnings)
    result.update(currency="USD", scope=info["scope"], input_complete=info["complete"])
    factor_assumptions = list(info.get("assumptions", []))
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
                     assumptions=["Factor loadings are in-sample regressions on US daily factors, not forecasts or causal exposures.",
                                  *factor_assumptions])


# --------------------------------------------------------------------------
# covariance estimators
# --------------------------------------------------------------------------
def _shrunk_cov(rets: pd.DataFrame) -> np.ndarray:
    return legacy.shrink_covariance(rets)[0] * legacy.TRADING_DAYS


def _pairwise_cov(rets: pd.DataFrame) -> tuple[np.ndarray, dict]:
    """Pairwise-complete annualized covariance with eigenvalue-clipped PSD repair.

    Each variance uses the asset's full observed history in the window and each
    covariance uses the dates both assets were observed. The resulting matrix
    need not be positive semidefinite; its correlation matrix is repaired by
    clipping eigenvalues at a small floor and restoring the unit diagonal, which
    leaves each asset's own variance unchanged. No shrinkage is applied.
    """
    observed = rets.notna().astype(int)
    overlap = observed.T @ observed
    min_overlap = int(overlap.to_numpy().min())
    if min_overlap < _MIN_PAIR_OVERLAP:
        raise ValueError(f"pairwise covariance needs at least {_MIN_PAIR_OVERLAP} overlapping returns for every "
                         f"pair; the thinnest pair has {min_overlap}")
    raw = rets.cov(min_periods=_MIN_PAIR_OVERLAP).to_numpy(float) * legacy.TRADING_DAYS
    vol = np.sqrt(np.clip(np.diag(raw), 1e-18, None))
    corr = raw / np.outer(vol, vol)
    corr = (corr + corr.T) / 2.0
    values, vectors = np.linalg.eigh(corr)
    floor = 1e-8
    repaired = values.min() < floor
    clipped = vectors @ np.diag(np.clip(values, floor, None)) @ vectors.T
    scale = np.sqrt(np.diag(clipped))
    clipped = clipped / np.outer(scale, scale)
    cov = clipped * np.outer(vol, vol)
    first = {c: str(rets[c].first_valid_index().date()) for c in rets.columns}
    return cov, {"estimator": "pairwise-complete sample covariance (no shrinkage)",
                 "psd_repair": "eigenvalue clipping of the correlation matrix at 1e-8, unit diagonal restored",
                 "psd_repair_applied": bool(repaired),
                 "min_eigenvalue_before_repair": float(values.min()),
                 "minimum_pair_overlap_returns": min_overlap,
                 "asset_first_return": first,
                 "label": "INCEPTION-AWARE: assets contribute different sample lengths; statistics are not from one common window"}


# --------------------------------------------------------------------------
# construction methods
# --------------------------------------------------------------------------
def _hrp(rets: pd.DataFrame, max_weight: float, cov: np.ndarray | None = None) -> pd.Series:
    from scipy.cluster.hierarchy import leaves_list, linkage
    from scipy.spatial.distance import squareform

    if rets.shape[1] == 1:
        legacy._cap(np.ones(1), max_weight)
        return pd.Series([1.0], index=rets.columns)
    cov_matrix = _shrunk_cov(rets) if cov is None else np.asarray(cov, float)
    cov = pd.DataFrame(cov_matrix, index=rets.columns, columns=rets.columns)
    vol = np.sqrt(np.clip(np.diag(cov_matrix), 1e-18, None))
    corr = pd.DataFrame(np.clip(cov_matrix / np.outer(vol, vol), -1.0, 1.0),
                        index=rets.columns, columns=rets.columns)
    distance = np.sqrt(np.maximum((1.0 - corr.to_numpy()) / 2.0, 0.0))
    np.fill_diagonal(distance, 0.0)
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


def _cvar_lp(returns: np.ndarray, max_weight: float, confidence: float,
             mean_floor_daily: float | None) -> np.ndarray:
    from scipy.optimize import linprog

    n_obs, n_assets = returns.shape
    # Variables: weights, VaR threshold alpha (free), nonnegative tail excesses.
    objective = np.r_[np.zeros(n_assets), 1.0,
                      np.full(n_obs, 1.0 / ((1.0 - confidence) * n_obs))]
    aub = np.c_[-returns, -np.ones(n_obs), -np.eye(n_obs)]
    bub = np.zeros(n_obs)
    if mean_floor_daily is not None:
        aub = np.vstack([aub, np.r_[-returns.mean(axis=0), 0.0, np.zeros(n_obs)]])
        bub = np.r_[bub, -mean_floor_daily]
    result = linprog(objective, A_ub=aub, b_ub=bub,
                     A_eq=np.r_[np.ones(n_assets), 0.0, np.zeros(n_obs)][None, :],
                     b_eq=[1.0], bounds=[(0.0, max_weight)] * n_assets + [(None, None)] + [(0.0, None)] * n_obs,
                     method="highs")
    if not result.success:
        raise ValueError(f"CVaR optimizer did not produce feasible weights: {result.message}")
    weights = np.clip(result.x[:n_assets], 0.0, None)
    return weights / weights.sum()


def _cvar(rets: pd.DataFrame, max_weight: float, confidence: float,
          min_cagr: float | None = None, min_arithmetic: float | None = None) -> tuple[pd.Series, dict]:
    """Minimum historical CVaR with an optional, explicitly typed return floor.

    ``min_cagr`` is an in-sample geometric floor on the daily-rebalanced
    constant-mix portfolio: CAGR = exp(252 * mean(log(1 + r_p))) - 1. It is not
    linear in the weights, so the linear arithmetic-mean floor is bisected for
    the lowest level whose minimum-CVaR solution meets the CAGR target. ``min_arithmetic`` is the linear floor on
    252 x the mean daily return.
    """
    if not 0.5 <= confidence < 1.0:
        raise ValueError("confidence must be at least 0.5 and below 1")
    if min_cagr is not None and min_arithmetic is not None:
        raise ValueError("use either min_annual_return (CAGR) or min_annual_arithmetic_return, not both")
    n_assets = rets.shape[1]
    legacy._cap(np.ones(n_assets), max_weight)
    returns = rets.to_numpy(float)
    detail: dict = {"objective": f"minimize historical daily CVaR at confidence {confidence:.3f}",
                    "return_floor": None}
    if min_cagr is None and min_arithmetic is None:
        return pd.Series(_cvar_lp(returns, max_weight, confidence, None), index=rets.columns), detail
    if min_arithmetic is not None:
        floor = min_arithmetic / legacy.TRADING_DAYS
        weights = _cvar_lp(returns, max_weight, confidence, floor)
        iterations = 1
        kind, target = "arithmetic_annual_mean", min_arithmetic
    else:
        from scipy.optimize import linprog

        target_log = math.log1p(min_cagr) / legacy.TRADING_DAYS

        def log_growth(w: np.ndarray) -> float:
            path = returns @ w
            return float(np.mean(np.log1p(path))) if (path > -1).all() else -np.inf

        unreachable = (f"min_annual_return {min_cagr:.4%} (in-sample CAGR) is not attainable by the "
                       "long-only capped minimum-CVaR portfolios on this sample")
        # Bracket the arithmetic floor: the log target is a lower bound (arithmetic
        # mean >= geometric mean); the highest attainable mean is the upper bound.
        top = linprog(-returns.mean(axis=0), A_eq=np.ones((1, n_assets)), b_eq=[1.0],
                      bounds=[(0.0, max_weight)] * n_assets, method="highs")
        high = float(-top.fun) - 1e-12
        weights = _cvar_lp(returns, max_weight, confidence, max(target_log, -1.0))
        iterations = 1
        floor = target_log
        if log_growth(weights) < target_log - 1e-12:
            if high < target_log:
                raise ValueError(unreachable)
            best = _cvar_lp(returns, max_weight, confidence, high)
            if log_growth(best) < target_log - 1e-12:
                raise ValueError(unreachable)
            low = target_log
            # Bisect for the lowest arithmetic floor whose minimum-CVaR solution meets the CAGR target.
            for iterations in range(2, 42):
                middle = (low + high) / 2.0
                candidate = _cvar_lp(returns, max_weight, confidence, middle)
                if log_growth(candidate) >= target_log - 1e-12:
                    high, best = middle, candidate
                else:
                    low = middle
                if high - low < 1e-10:
                    break
            weights, floor = best, high
        kind, target = "in_sample_cagr", min_cagr
    portfolio = returns @ weights
    detail["return_floor"] = {
        "kind": kind, "target": target,
        "binding_arithmetic_daily_floor": floor, "iterations": iterations,
        "realized_in_sample_cagr": float(math.expm1(np.mean(np.log1p(portfolio)) * legacy.TRADING_DAYS)),
        "realized_arithmetic_annual_mean": float(portfolio.mean() * legacy.TRADING_DAYS),
        "portfolio_convention": "daily-rebalanced constant mix on the training sample",
    }
    return pd.Series(weights, index=rets.columns), detail


def _market_prior(names: list[str], inputs: dict, cov: np.ndarray,
                  risk_aversion: float) -> tuple[np.ndarray, dict]:
    raw = inputs.get("market_weights")
    if not isinstance(raw, dict) or {str(k).upper() for k in raw} != set(names):
        raise ValueError("market_weights must identify exactly every construction symbol")
    source = _text(inputs.get("market_weights_source"), "market_weights_source")
    values = {str(k).upper(): _number(v, f"market_weights.{k}", minimum=0.0) for k, v in raw.items()}
    total = sum(values.values())
    if total <= 0:
        raise ValueError("market_weights must have a positive sum")
    w_mkt = np.array([values[name] / total for name in names])
    prior = risk_aversion * cov @ w_mkt
    return prior, {"prior": "market_equilibrium",
                   "market_weights": {name: float(w_mkt[i]) for i, name in enumerate(names)},
                   "market_weights_source": source,
                   "prior_method": "reverse optimization: pi = risk_aversion x Sigma x w_market "
                                   "(market weights normalized over this universe only)"}


def _black_litterman(rets: pd.DataFrame, max_weight: float, inputs: dict,
                     cov: np.ndarray | None = None) -> tuple[pd.Series, dict]:
    names = list(rets.columns)
    tau = _number(inputs.get("tau"), "tau", minimum=0.0)
    risk_aversion = _number(inputs.get("risk_aversion"), "risk_aversion", minimum=0.0)
    if tau <= 0 or risk_aversion <= 0:
        raise ValueError("tau and risk_aversion must be greater than zero")
    annual_cov = _shrunk_cov(rets) if cov is None else np.asarray(cov, float)
    has_explicit, has_market = "prior_returns" in inputs, "market_weights" in inputs
    if has_explicit == has_market:
        raise ValueError("Black-Litterman needs exactly one prior: prior_returns or market_weights")
    if has_explicit:
        prior_raw = inputs.get("prior_returns")
        if not isinstance(prior_raw, dict) or set(prior_raw) != set(names):
            raise ValueError("prior_returns must identify exactly every construction symbol")
        prior = np.array([_number(prior_raw[name], f"prior_returns.{name}") for name in names])
        prior_detail: dict = {"prior": "explicit"}
    else:
        prior, prior_detail = _market_prior(names, inputs, annual_cov, risk_aversion)
    views = inputs.get("views", [])
    if not isinstance(views, list) or (has_explicit and not views):
        raise ValueError("views must be a nonempty list (it may be empty only with a market_weights prior)")
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
    if loadings:
        p = np.vstack(loadings)
        q = np.asarray(expected)
        system = p @ tau_cov @ p.T + np.diag(omega)
        try:
            gain = tau_cov @ p.T @ np.linalg.inv(system)
        except np.linalg.LinAlgError as exc:
            raise ValueError("Black-Litterman view system is singular") from exc
        posterior = prior + gain @ (q - p @ prior)
        # Posterior uncertainty of the mean: M = tau*Sigma - tau*Sigma P' (P tau*Sigma P' + Omega)^-1 P tau*Sigma
        uncertainty_m = tau_cov - gain @ p @ tau_cov
    else:
        posterior = prior.copy()
        uncertainty_m = tau_cov.copy()
    uncertainty_m = (uncertainty_m + uncertainty_m.T) / 2.0
    posterior_cov = annual_cov + uncertainty_m
    weights = legacy._optimize(
        lambda w: float(-(posterior @ w) + 0.5 * risk_aversion * (w @ posterior_cov @ w)),
        len(names), max_weight,
    )
    return pd.Series(weights, index=names), {
        **prior_detail,
        "prior_returns": {name: float(prior[i]) for i, name in enumerate(names)},
        "posterior_returns": {name: float(posterior[i]) for i, name in enumerate(names)},
        "posterior_volatility": {name: float(np.sqrt(posterior_cov[i, i])) for i, name in enumerate(names)},
        "optimizer_covariance": "Sigma + M: return covariance plus posterior uncertainty of the mean",
        "views": view_records, "tau": tau, "risk_aversion": risk_aversion,
    }


def _fit_method(rets: pd.DataFrame, method: str, max_weight: float,
                inputs: dict, cov: np.ndarray | None = None) -> tuple[pd.Series, dict | None]:
    if method == "hrp":
        return _hrp(rets, max_weight, cov), None
    if method == "cvar":
        if cov is not None:
            raise ValueError("cvar optimizes over joint historical scenarios and requires the common window; "
                             "covariance='pairwise' is not applicable")
        confidence = _number(inputs.get("confidence", 0.95), "confidence")
        floors = {}
        for key in ("min_annual_return", "min_annual_arithmetic_return"):
            if inputs.get(key) is not None:
                floors[key] = _number(inputs[key], key)
                if floors[key] <= -1:
                    raise ValueError(f"{key} must be greater than -1")
        return _cvar(rets, max_weight, confidence, floors.get("min_annual_return"),
                     floors.get("min_annual_arithmetic_return"))
    if method == "black_litterman":
        return _black_litterman(rets, max_weight, inputs, cov)
    return legacy.solve_weights(rets, method, max_weight, cov=cov), None


def _tail_loss(rets: pd.DataFrame, weights: pd.Series, confidence: float = 0.95) -> float:
    sample = rets[weights.index].dropna()
    return historical_cvar(-(sample.to_numpy() @ weights.to_numpy()), confidence)


def _drift_weights(target: pd.Series, block: pd.DataFrame) -> pd.Series:
    growth = (1.0 + block[target.index]).prod()
    ending = target * growth
    return ending / ending.sum()


def _validation_metrics(returns: pd.Series, confidence: float) -> dict:
    if returns.empty:
        raise ValueError("validation produced no returns")
    return {
        "total_return": float((1.0 + returns).prod() - 1.0),
        "annualized_return": float(legacy.ann_return(returns)),
        "annualized_volatility": float(legacy.ann_vol(returns)),
        "max_drawdown": float(legacy.max_drawdown(returns)),
        "historical_daily_cvar_loss": historical_cvar(-returns.to_numpy(float), confidence),
        "n_days": int(len(returns)),
    }


# --------------------------------------------------------------------------
# walk-forward validation
# --------------------------------------------------------------------------
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
        has_prior = isinstance(entry.get("prior_returns"), dict) or isinstance(entry.get("market_weights"), dict)
        if not has_prior or not isinstance(entry.get("views"), list):
            raise ValueError(f"validation.belief_schedule[{index}] requires a prior (prior_returns or "
                             "market_weights) and a views list")
        seen.add(effective)
        dated.append((effective, entry))
    dated.sort(key=lambda item: item[0])
    if not any(effective <= first_cutoff for effective, _ in dated):
        return dated, [f"validation.belief_schedule needs beliefs effective on or before {first_cutoff.isoformat()}"]
    return dated, []


def _walk_forward(rets: pd.DataFrame, method: str, inner_cap: float, inputs: dict,
                  initial_full: pd.Series, cash_weights: dict[str, float],
                  cash_returns: pd.Series | None, currency: str) -> tuple[dict | None, list[str]]:
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
    risky_share = 1.0 - sum(cash_weights.values())
    universe = [*rets.columns, *cash_weights]

    def full_weights(risky_weights: pd.Series) -> pd.Series:
        out = risky_weights * risky_share
        for cash_name, weight in cash_weights.items():
            out.loc[cash_name] = weight
        return out.reindex(universe).fillna(0.0)

    static = None
    if spec.get("static_baseline_weights") is not None:
        static_warnings: list[str] = []
        static_raw, static_missing = _complete_weights(spec["static_baseline_weights"],
                                                       "validation.static_baseline_weights",
                                                       currency, None, static_warnings)
        if static_raw is None:
            return None, static_missing
        unknown = sorted(set(static_raw) - set(universe))
        if unknown:
            raise ValueError("validation.static_baseline_weights uses symbols outside the construction universe: "
                             + ", ".join(unknown))
        static = pd.Series(static_raw).reindex(universe).fillna(0.0)

    full_rets = rets.copy()
    for cash_name in cash_weights:
        full_rets[cash_name] = cash_returns.reindex(rets.index).to_numpy() if cash_returns is not None else 0.0

    fitters: dict[str, Callable[[pd.DataFrame, dict], pd.Series]] = {
        "method": lambda train, fit_inputs: full_weights(_fit_method(train, method, inner_cap, fit_inputs)[0]),
        "equal_weight": lambda train, _: full_weights(legacy.solve_weights(train, "equal", inner_cap)),
        "inverse_volatility": lambda train, _: full_weights(legacy.solve_weights(train, "invvol", inner_cap)),
    }
    if static is not None:
        fitters["static_baseline"] = lambda train, _: static.copy()
    prior = {name: initial_full.reindex(universe).fillna(0.0) for name in fitters}
    series: dict[str, list[pd.Series]] = {name: [] for name in fitters}
    blocks = []
    cursor = train_days
    while cursor < len(rets):
        train = rets.iloc[cursor - train_days:cursor]
        test = rets.iloc[cursor:cursor + test_days]
        fit_inputs = inputs
        belief_effective_on = None
        if method == "black_litterman":
            effective, beliefs = max(
                (item for item in belief_schedule if item[0] <= train.index[-1].date()),
                key=lambda item: item[0])
            fit_inputs = {k: v for k, v in inputs.items() if k not in {"prior_returns", "market_weights"}}
            fit_inputs.update({k: beliefs[k] for k in ("prior_returns", "market_weights",
                                                        "market_weights_source", "views") if k in beliefs})
            belief_effective_on = effective.isoformat()
        full_test = full_rets.loc[test.index]
        block_strategies = {}
        for name, fit in fitters.items():
            target = fit(train, fit_inputs)
            turnover = 0.5 * float((target - prior[name]).abs().sum())
            cost = turnover * cost_bps / 10_000.0
            net = legacy.portfolio_returns(full_test, target, "none").copy()
            net.iloc[0] -= cost
            series[name].append(net)
            block_strategies[name] = {"weights": {k: float(v) for k, v in target.items()},
                                      "turnover": turnover, "cost_return": cost}
            prior[name] = _drift_weights(target[target > 0], full_test).reindex(universe).fillna(0.0)
        block = {
            "train_window": {"start": str(train.index[0].date()), "end": str(train.index[-1].date()),
                             "n_days": len(train)},
            "test_window": {"start": str(test.index[0].date()), "end": str(test.index[-1].date()),
                            "n_days": len(test)},
            "partial": len(test) < test_days,
            "strategies": block_strategies,
        }
        if belief_effective_on is not None:
            block["belief_effective_on"] = belief_effective_on
        blocks.append(block)
        cursor += test_days
    results = {name: _validation_metrics(pd.concat(parts), confidence) for name, parts in series.items()}
    return {
        "design": "rolling fixed-length training window followed by the next non-overlapping test block; "
                  "a final shorter block covers the remaining observations",
        "train_days": train_days, "test_days": test_days,
        "transaction_cost_bps": cost_bps,
        "turnover_convention": "one-way turnover is half the absolute weight change from prior block-end drifted weights; cost is deducted on the next block's first return",
        "strategies": results,
        "baselines": {
            "equal_weight": "equal weights over the risky sleeve, refit each block under the same cap",
            "inverse_volatility": "inverse sample volatility over each training window, same cap",
            **({"static_baseline": "caller-specified fixed weights (e.g. a 60/40 mix), reset each block"}
               if static is not None else {}),
        },
        "partial_final_block": bool(blocks and blocks[-1]["partial"]),
        "blocks": blocks,
        "interpretation": "historical out-of-sample walk-forward evidence; it does not prove future advantage",
    }, []


# --------------------------------------------------------------------------
# construction
# --------------------------------------------------------------------------
def _construction(inputs: dict, context: dict) -> dict:
    method = str(inputs.get("method", "equal")).lower()
    if method not in _METHODS:
        raise ValueError("method must be one of " + ", ".join(sorted(_METHODS)))
    covariance_mode = inputs.get("covariance", "common_window")
    if covariance_mode not in {"common_window", "pairwise"}:
        raise ValueError("covariance must be 'common_window' or 'pairwise'")
    pairwise = covariance_mode == "pairwise"
    if pairwise and inputs.get("validation") is not None:
        raise ValueError("walk-forward validation requires the common window; remove covariance='pairwise'")
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
                "complete": True, "source": "inputs.tickers", "total_value": None, "warnings": [],
                "assumptions": []}
        current = _combine_sic(current, inputs, "tickers", info["assumptions"], info["warnings"])
    _validate_cash_symbols(current, info["currency"], "construction universe")
    cash_weights = {k: v for k, v in current.items() if k.startswith(_CASH_PREFIX) and v > 0}
    cash_weight = sum(cash_weights.values())
    risky = [symbol for symbol in current if not symbol.startswith(_CASH_PREFIX)]
    if not risky:
        return _envelope("needs_input", missing=["at least one non-cash construction asset"])
    max_weight = _number(inputs.get("max_weight", 1.0), "max_weight", minimum=0.0)
    if max_weight <= 0 or max_weight > 1:
        raise ValueError("max_weight must be greater than zero and at most one")
    needs_cash_returns = bool(cash_weights) and inputs.get("validation") is not None
    prices = _price_frame(inputs, [*risky, *cash_weights] if needs_cash_returns else risky,
                          info["currency"], align=not pairwise)
    warnings = list(info.get("warnings", [])) + prices.warnings
    sources = prices.sources
    if prices.missing:
        return _envelope("needs_input", missing=prices.missing, warnings=warnings, sources=sources)
    px = prices.px
    cov_detail = None
    if pairwise:
        rets = px[risky].pct_change(fill_method=None).iloc[1:]
        rets = rets.dropna(how="all")
        cov, cov_detail = _pairwise_cov(rets)
        warnings.insert(0, "INCEPTION-AWARE COVARIANCE: " + cov_detail["label"] + ".")
    else:
        rets = legacy.daily_returns(px[risky])
        cov = None
    risky_share = 1.0 - cash_weight
    if risky_share <= 0:
        return _envelope("needs_input", missing=["positive non-cash portfolio share"])
    inner_cap = min(1.0, max_weight / risky_share)
    inner, method_detail = _fit_method(rets, method, inner_cap, inputs, cov)
    proposed = {symbol: float(weight * risky_share) for symbol, weight in inner.items()}
    proposed.update(cash_weights)
    if max(proposed.values()) > max_weight + 1e-7:
        raise ValueError(f"infeasible maximum weight {max_weight} with preserved cash allocation")
    equal_inner = legacy.solve_weights(rets, "equal", inner_cap, cov=cov)
    equal = {symbol: float(weight * risky_share) for symbol, weight in equal_inner.items()}
    equal.update(cash_weights)
    confidence = _number(inputs.get("confidence", 0.95), "confidence")
    sigma = cov if cov is not None else rets.cov().to_numpy() * legacy.TRADING_DAYS

    def metrics(w: dict[str, float]) -> dict:
        vector = np.array([w.get(name, 0.0) for name in rets.columns])
        return {"annualized_volatility": float(vector @ sigma @ vector) ** 0.5,
                "historical_daily_cvar_loss": _tail_loss(rets, pd.Series(w), confidence)}

    risky_proposed = {k: v / risky_share for k, v in proposed.items() if not k.startswith(_CASH_PREFIX)}
    risky_equal = {k: v / risky_share for k, v in equal.items() if not k.startswith(_CASH_PREFIX)}
    cash_returns = None
    if needs_cash_returns:
        first_cash = next(iter(cash_weights))
        cash_returns = px[first_cash].pct_change().iloc[1:]
    validation, validation_missing = _walk_forward(
        rets, method, inner_cap, inputs, pd.Series(current), cash_weights, cash_returns, info["currency"])
    if validation_missing:
        return _envelope("needs_input", missing=validation_missing, warnings=warnings, sources=sources)
    window = legacy._window(rets.index)
    result = {"method": method, "currency": info["currency"], "scope": info["scope"],
              "weights": proposed, "cash_weight_preserved": cash_weight,
              "constraints": {"long_only": True, "sum": 1.0, "max_weight": max_weight},
              "covariance": cov_detail or {
                  "estimator": {"equal": "not used by the method",
                                "invvol": "sample volatility (unshrunk)",
                                "cvar": "not used: joint historical scenarios"}.get(
                                    method, "Ledoit-Wolf scaled-identity shrinkage"),
                  "sample": "common window: every asset observed on every date"},
              "historical_metrics_on_risky_sleeve": metrics(risky_proposed),
              "equal_weight_baseline": {"weights": equal,
                                        "historical_metrics_on_risky_sleeve": metrics(risky_equal)},
              "window": window, "input_complete": info["complete"]}
    if pairwise:
        result["historical_metrics_convention"] = ("volatility from the repaired pairwise covariance; CVaR from "
                                                   "dates on which every asset was observed")
    if method_detail is not None:
        result["model"] = method_detail
    if validation is not None:
        result["validation"] = validation
    assumptions = ["Construction is long-only and uses historical daily returns; it is not an expected-return forecast.",
                   "The equal-weight baseline uses the same universe, cap, cash allocation, and price window.",
                   *info.get("assumptions", []),
                   *prices.assumptions]
    if pairwise:
        assumptions.append("covariance='pairwise': each variance and covariance uses every date its assets were "
                           "observed, then the matrix is repaired to be positive semidefinite; it mixes sample "
                           "lengths and is not a common-window estimate.")
    else:
        assumptions.append("Covariance uses only the common window on which every asset has prices; "
                           "pass covariance='pairwise' for an inception-aware estimate.")
    if cash_weight:
        assumptions.append("Stored cash weight is preserved exactly and excluded from covariance optimization.")
    if method == "hrp":
        assumptions.append("HRP uses single-linkage clustering, the stated covariance estimate, recursive bisection, then the stated hard cap.")
    if method == "cvar":
        assumptions.append(f"CVaR minimizes empirical daily tail loss at confidence {confidence:.3f} via a constrained linear program.")
        if inputs.get("min_annual_return") is not None:
            assumptions.append("min_annual_return is an in-sample CAGR floor on the daily-rebalanced mix, enforced by "
                               "bisecting the linear arithmetic-mean floor until realized CAGR meets it.")
        if inputs.get("min_annual_arithmetic_return") is not None:
            assumptions.append("min_annual_arithmetic_return floors 252 x the mean daily return; realized CAGR is "
                               "lower by the variance drag and is reported beside it.")
    if method == "black_litterman":
        assumptions.append("Black-Litterman combines the stated prior (explicit returns or market-cap reverse optimization) "
                           "with relative or absolute views; confidence maps to view uncertainty, not a probability of being right.")
        assumptions.append("Long-only capped weights maximize posterior mean-variance utility with covariance Sigma + M "
                           "(posterior uncertainty of the mean) at the stated positive risk_aversion.")
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
            "construct": _construction, "factors": _factors,
            "sic_premium": _sic_premium_task}[task](inputs, context)
