# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "yfinance>=0.2.50",
#   "pandas",
#   "numpy",
#   "scipy",
#   "statsmodels",
#   "openpyxl",
#   "pypdf",
# ]
# ///
"""wm.py — wealth-manager analytics engine.

Subcommands: prices, analyze, factors, regimes, build, holdings, ingest,
report, plan, review, fees, compare.
JSON to stdout by default; --pretty for a human table.
"""
from __future__ import annotations

import argparse
import csv
import io
import urllib.request
import zipfile
import os
import hashlib
import json
import math
import re
import sqlite3

import time
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
if os.environ.get("WEALTH_CACHE"):
    CACHE = Path(os.environ["WEALTH_CACHE"]).expanduser()
else:
    CACHE = Path(os.environ.get("XDG_CACHE_HOME", str(Path.home() / ".cache"))).expanduser() / "wealth-harness"
TRADING_DAYS = 252
CORR_CLUSTER = 0.7
FF_SETS = {3: "F-F_Research_Data_Factors_daily",
           5: "F-F_Research_Data_5_Factors_2x3_daily"}

# --------------------------------------------------------------------------
# data loading
# --------------------------------------------------------------------------
def _fetch_one(ticker: str) -> pd.Series:
    """One ticker's full adjusted-close history, sequential, retrying the cache lock.

    Always the full history so the on-disk cache is independent of --years:
    a later, longer window must not silently reuse a short cached series.
    """
    import yfinance as yf

    last = None
    for attempt in range(3):
        try:
            df = yf.download(ticker, period="max", interval="1d",
                             auto_adjust=True, progress=False, threads=False,
                             actions=False)
            if df is None or df.empty:
                raise ValueError("no data returned")
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)
            s = df["Close"].dropna()
            s.index = pd.to_datetime(s.index).tz_localize(None).normalize()
            s.name = ticker
            return s
        except sqlite3.OperationalError as exc:  # yfinance tz cache
            last = exc
            if "database is locked" not in str(exc).lower():
                break
            time.sleep(1.0 + attempt)
        except Exception as exc:  # noqa: BLE001
            last = exc
            time.sleep(0.5)
    raise RuntimeError(f"{ticker}: {last}")

def _cache_path(name: str) -> Path:
    CACHE.mkdir(parents=True, exist_ok=True)
    return CACHE / name

def _fresh(path: Path) -> bool:
    return path.exists() and date.fromtimestamp(path.stat().st_mtime) == date.today()

def _read_cache(path: Path) -> pd.DataFrame:
    return pd.read_parquet(path) if path.suffix == ".parquet" else \
        pd.read_csv(path, index_col=0, parse_dates=True)

def _write_cache(df: pd.DataFrame, path: Path) -> None:
    try:
        df.to_parquet(path)
    except Exception:  # noqa: BLE001 — pyarrow missing: fall back to csv
        df.to_csv(path.with_suffix(".csv"))

def _load_prices(tickers, years: int = 5, use_cache: bool = True, currency=None,
                 fx_loader=None):
    """Adjusted closes on a common date window. Returns (DataFrame, warnings)."""
    if years <= 0:
        raise ValueError("years must be positive")
    series, warnings = [], []
    for t in dict.fromkeys(tickers):
        if not re.fullmatch(r"[A-Za-z0-9^=._-]{1,32}", t) or '..' in t:
            raise ValueError("invalid ticker syntax")
        t = t.upper()
        pq, csv = _cache_path(f"px_{t}.parquet"), _cache_path(f"px_{t}.csv")
        hit = pq if _fresh(pq) else (csv if _fresh(csv) else None)
        if use_cache and hit is not None:
            try:
                series.append(_read_cache(hit)[t])
                continue
            except Exception:  # noqa: BLE001
                pass
        try:
            s = _fetch_one(t)
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"dropped {t}: {exc}")
            continue
        _write_cache(s.to_frame(), pq)
        series.append(s)
    if not series:
        return pd.DataFrame(), warnings
    px = pd.concat(series, axis=1, sort=False).sort_index()
    first_dates = {s.name: str(s.dropna().index.min().date()) for s in series}
    cutoff = px.index.max() - pd.Timedelta(days=int(round(365.25 * years)))
    px = px.loc[px.index >= cutoff]
    coverage = {t: {"first": str(px[t].dropna().index.min().date()) if px[t].notna().any() else None,
                    "last": str(px[t].dropna().index.max().date()) if px[t].notna().any() else None,
                    "available_observations": int(px[t].notna().sum())} for t in px.columns}
    if (px.index.dayofweek>=5).any():
        px=px.loc[px.index.dayofweek<5]
        warnings.append('seven-day markets sampled at weekday closes for consistent 252-day annualization; weekend moves enter the next weekday return')
    n_before = len(px.columns)
    px = px.dropna(axis=1, how="all").dropna(how="any")  # common window
    if len(px.columns) < n_before:
        warnings.append("dropped columns with no data in the requested window")
    if len(px) < 30:
        warnings.append(f"only {len(px)} common trading days — stats are noisy")
    px.attrs["first_dates"] = first_dates
    px.attrs.update(source='Yahoo Finance via yfinance adjusted daily closes', data_kind='historical',
                    retrieved=date.today().isoformat(), coverage=coverage)
    if len(px) and (pd.Timestamp.today().normalize()-px.index.max()).days > 7:
        warnings.append("price sample ends more than seven calendar days ago")
    return to_currency(px, currency, warnings, fx_loader=fx_loader), warnings

def _fx_series(frm: str, to: str):
    """Daily FX rate multiplying `frm` into `to`, cached like a price series."""
    for pair, invert in ((f"{frm}{to}=X", False), (f"{to}{frm}=X", True)):
        pq, csv = _cache_path(f"px_{pair}.parquet"), _cache_path(f"px_{pair}.csv")
        hit = pq if _fresh(pq) else (csv if _fresh(csv) else None)
        try:
            s = _read_cache(hit)[pair] if hit is not None else _fetch_one(pair)
        except Exception:  # noqa: BLE001 — try the inverse pair
            continue
        if hit is None:
            _write_cache(s.to_frame(), pq)
        return 1.0 / s if invert else s
    return None

def to_currency(px: pd.DataFrame, ccy: str | None, warnings: list, meta=None,
                fx_loader=None) -> pd.DataFrame:
    """Convert known quote currencies; missing or stale FX is a hard error.

    Without a requested currency, homogeneous native currencies are required.
    At most four calendar days of backward-looking FX carry are allowed for
    weekends/holidays. This is a disclosed data-quality rule, not an FX model.
    """
    if px.empty:
        return px
    meta = _ticker_meta(list(px.columns)) if meta is None else meta
    raw_units = {t: str((meta.get(t) or {}).get('currency') or '') for t in px.columns}
    if any(v in ('GBp','GBX','ZAc','ILA') for v in raw_units.values()):
        raise ValueError('quote subunits require an explicit unit conversion before FX')
    native = {t: str((meta.get(t) or {}).get('currency') or '').upper()
              for t in px.columns}
    unknown = [t for t, v in native.items() if not v]
    if unknown:
        raise ValueError('native currency unavailable: ' + ', '.join(unknown))
    if ccy is None:
        if len(set(native.values())) != 1:
            raise ValueError('mixed native currencies: specify --currency')
        ccy = next(iter(native.values()))
    ccy = ccy.upper()
    if not re.fullmatch(r'[A-Z]{3}', ccy):
        raise ValueError('currency must be a three-letter code')
    load, out, converted = fx_loader or _fx_series, px.copy(), []
    for t, frm in native.items():
        if frm == ccy:
            continue
        fx = load(frm, ccy)
        if fx is None or not len(fx):
            raise ValueError(f'{t}: no {frm}->{ccy} rate; no conversion performed')
        fx = fx.sort_index()
        if fx.index.has_duplicates or not np.isfinite(fx.to_numpy(float)).all() or (fx <= 0).any():
            raise ValueError(f'{t}: invalid FX series')
        rate = fx.reindex(out.index, method='ffill', tolerance=pd.Timedelta(days=4))
        if rate.isna().any():
            raise ValueError(f'{t}: FX coverage missing or over four days stale; no future fill permitted')
        out[t] = out[t] * rate
        converted.append(t)
    out.attrs.update(px.attrs)
    out.attrs.update(currency=ccy, fx_converted=converted, quote_currencies=native,
                     fx_method='prior available close, maximum four calendar days')
    return out

def _load_factors(model: int = 3):
    """Daily official French-library CSV over HTTPS; decimal returns."""
    if model not in FF_SETS:
        raise ValueError('factor model must be 3 or 5')
    name = FF_SETS[model]
    pq, csv_path = _cache_path(f"ff{model}_daily.parquet"), _cache_path(f"ff{model}_daily.csv")
    hit = pq if _fresh(pq) else (csv_path if _fresh(csv_path) else None)
    if hit is not None:
        return _read_cache(hit)
    url = f"https://mba.tuck.dartmouth.edu/pages/faculty/ken.french/ftp/{name}_CSV.zip"
    req = urllib.request.Request(url, headers={'User-Agent':'wealth-manager-research/2.0'})
    with urllib.request.urlopen(req, timeout=20) as response:
        content = response.read(10_000_001)
    if len(content) > 10_000_000:
        raise ValueError('factor archive exceeds size limit')
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        members = [x for x in archive.infolist() if x.filename.lower().endswith('.csv')]
        if len(members) != 1:
            raise ValueError('factor archive must contain exactly one CSV')
        member = members[0]
        if member.file_size > 20_000_000:
            raise ValueError('factor CSV exceeds size limit')
        lines = archive.read(member).decode('utf-8-sig').splitlines()
    header = next((x for x in lines if 'Mkt-RF' in x and 'RF' in x), None)
    if header is None:
        raise ValueError('factor CSV header missing')
    daily = [x for x in lines if re.match(r'^\s*\d{8},',x)]
    if not daily:
        raise ValueError('no daily observations in factor file')
    ff = pd.read_csv(io.StringIO(header+'\n'+'\n'.join(daily)),index_col=0)
    ff.index = pd.to_datetime(ff.index.astype(str),format='%Y%m%d')
    ff.columns = [c.strip() for c in ff.columns]
    required = {'Mkt-RF', 'SMB', 'HML', 'RF'} | ({'RMW', 'CMA'} if model == 5 else set())
    if not required.issubset(ff.columns):
        raise ValueError('factor CSV is missing required columns')
    ff = ff.astype(float).replace([-99.99,-999.],np.nan)/100.
    if ff.index.has_duplicates or not ff.index.is_monotonic_increasing:
        raise ValueError('invalid factor dates')
    _write_cache(ff,pq)
    return ff

def _ticker_meta(tickers) -> dict:
    """Daily-cache quote currencies. Ambiguous feed expense units are not guessed.

    A verified decimal expense ratio may be supplied to analyze_frame via meta.
    Live Yahoo raw expense fields are retained for checking against the issuer.
    """
    path = _cache_path('meta_v2.json')
    try:
        cache = json.loads(path.read_text()) if _fresh(path) else {}
    except (OSError, ValueError):
        cache = {}
    out = {}
    for t in tickers:
        if t in cache:
            out[t] = cache[t]
            continue
        info = {}
        try:
            import yfinance as yf
            info = yf.Ticker(t).get_info() or {}
        except Exception:
            pass
        raw = {k: info[k] for k in ('netExpenseRatio', 'annualReportExpenseRatio', 'expenseRatio')
               if info.get(k) is not None}
        fee = None
        for k in ('netExpenseRatio', 'annualReportExpenseRatio', 'expenseRatio'):
            if raw.get(k) is not None:
                try:
                    fee = float(raw[k])
                except (TypeError, ValueError):
                    fee = None
                break
        # The feed mixes units on the same field: 0.0945 means 0.0945%, 0.0009
        # means 0.09%. Nothing real charges more than 2%, so anything above that
        # is being quoted in percent and is divided down. A value that survives
        # neither reading is dropped rather than guessed at.
        if fee is not None:
            if not np.isfinite(fee) or fee < 0:
                fee = None
            elif fee > 0.02:
                fee = fee / 100.0
                if fee >= 1.0:
                    fee = None
        # A single common share is not a fund: it has no expense ratio at all, which
        # is a known zero, not a missing number. Treating it as unknown would drag
        # fee coverage down for every portfolio that holds a stock.
        kind = str(info.get('quoteType') or '').upper()
        if fee is None and kind == 'EQUITY':
            fee, status = 0.0, 'not-a-fund'
        else:
            status = 'provider' if fee is not None else 'unavailable'
        out[t] = cache[t] = {'expense_ratio': fee, 'currency': info.get('currency'),
                            'raw_fee_fields': raw, 'quote_type': info.get('quoteType'),
                            'fee_status': status,
                            'source': 'Yahoo Finance via yfinance (provider-reported, '
                                      'not issuer-verified)',
                            'retrieved': date.today().isoformat()}
    try:
        path.write_text(json.dumps(cache), encoding='utf-8')
    except OSError:
        pass
    return out

RF_FFILL_MAX_DAYS = 90   # French publishes with a lag; carry the last rate no further

def _rf_daily(index: pd.DatetimeIndex, warnings: list, currency: str | None):
    if currency is None:
        warnings.append('reporting currency not declared; Sharpe and alpha omitted')
        return None
    if currency != 'USD':
        # There is no published daily short rate for most home currencies. Using
        # the USD rate is wrong in level, but omitting Sharpe and alpha entirely
        # leaves the pretty table blank for every non-USD investor. Substitute it
        # and say so; the page does not show these numbers either way.
        warnings.append(f'no matching-currency risk-free series for {currency}; the US daily '
                        f'risk-free rate is substituted, so Sharpe and alpha understate the '
                        f'local cash return and are comparable only against other USD estimates')
    try:
        series = _load_factors(3)['RF']
        last = series.index.max()
        rf = series.reindex(index)
        if rf.isna().any() or not np.isfinite(rf.to_numpy(float)).all():
            # Ken French publishes with a lag, so a live window routinely ends a
            # few weeks past the last factor date. That tail is carried forward
            # rather than throwing away Sharpe and alpha; a genuine hole inside
            # the published range still disqualifies the series.
            interior = rf.loc[index <= last]
            if interior.isna().any() or not np.isfinite(interior.to_numpy(float)).all():
                raise ValueError('risk-free dates do not cover every return observation')
            tail = index[index > last]
            if len(tail) == 0:
                raise ValueError('risk-free dates do not cover every return observation')
            gap = int((tail.max() - last).days)
            if gap > RF_FFILL_MAX_DAYS:
                raise ValueError(f'risk-free series ends {gap} days before the window '
                                 f'({last.date()}); beyond the {RF_FFILL_MAX_DAYS}-day carry-forward limit')
            rf = rf.ffill()
            if rf.isna().any():
                raise ValueError('risk-free dates do not cover every return observation')
            warnings.append(f'risk-free series ends {last.date()}; the last published rate is carried '
                            f'forward over the final {len(tail)} observation(s) ({gap} days) so Sharpe '
                            f'and alpha use a stale short rate at the end of the window')
        return rf
    except Exception as exc:
        warnings.append(f'risk-free series unavailable; Sharpe and alpha omitted ({exc})')
        return None

def _rf_annual(index: pd.DatetimeIndex, warnings: list):
    """Arithmetic annualization of USD daily RF; unavailable stays None."""
    rf = _rf_daily(index, warnings, 'USD')
    return float(rf.mean() * TRADING_DAYS) if rf is not None else None

# --------------------------------------------------------------------------
# math — small pure functions
# --------------------------------------------------------------------------
def daily_returns(px: pd.DataFrame) -> pd.DataFrame:
    if not isinstance(px.index, pd.DatetimeIndex) or px.index.has_duplicates or not px.index.is_monotonic_increasing:
        raise ValueError('prices require unique, increasing dates')
    if px.columns.has_duplicates or len(px) < 3 or not len(px.columns):
        raise ValueError('prices require unique assets and at least three observations')
    a = px.to_numpy(float)
    if not np.isfinite(a).all() or (a <= 0).any():
        raise ValueError('prices must be positive, finite and complete on the common sample')
    r = px.pct_change(fill_method=None).iloc[1:]
    r.attrs.update(px.attrs)
    r.attrs['price_start'] = str(px.index[0])
    return r

def ann_return(r: pd.Series) -> float:
    if len(r) == 0:
        return float("nan")
    total = float((1.0 + r).prod())
    if total <= 0:
        return float("nan")
    return total ** (TRADING_DAYS / len(r)) - 1.0

def ann_vol(r: pd.Series) -> float:
    return float(r.std(ddof=1) * np.sqrt(TRADING_DAYS))

def max_drawdown(r: pd.Series) -> float:
    if not len(r):
        return float('nan')
    curve = (1.0 + r).cumprod()
    return float((curve / curve.cummax().clip(lower=1.0) - 1.0).min())

def beta_alpha_r2(r: pd.Series, rb: pd.Series, rf_daily=0.0):
    """OLS with intercept; alpha is the daily intercept times 252, not a forecast."""
    if not r.index.equals(rb.index):
        raise ValueError('regression return dates must match')
    if isinstance(rf_daily, pd.Series) and not r.index.equals(rf_daily.index):
        raise ValueError('risk-free dates must match')
    x, y = (rb - rf_daily).to_numpy(float), (r - rf_daily).to_numpy(float)
    if len(x) < 3 or not (np.isfinite(x).all() and np.isfinite(y).all()):
        return (float('nan'),) * 3
    vx, vy = float(np.var(x, ddof=1)), float(np.var(y, ddof=1))
    if vx < 1e-20:
        return (float('nan'),) * 3
    beta = float(np.cov(y, x, ddof=1)[0, 1] / vx)
    alpha_d = float(y.mean() - beta * x.mean())
    resid = y - (alpha_d + beta * x)
    r2 = float(1 - np.var(resid, ddof=1) / vy) if vy > 1e-20 else float('nan')
    return beta, alpha_d * TRADING_DAYS, r2

def sharpe(r: pd.Series, rf_ann) -> float:
    """Annualized mean daily excess return / daily excess-return sample deviation.

    A Series is daily RF; a scalar is an arithmetic annual RF assumption.
    Square-root annualization is conventional, not a serial-correlation correction.
    """
    if rf_ann is None:
        return float('nan')
    if isinstance(rf_ann, pd.Series):
        if not r.index.equals(rf_ann.index):
            raise ValueError('risk-free dates must match')
        excess = r - rf_ann
    else:
        excess = r - float(rf_ann) / TRADING_DAYS
    vol = float(excess.std(ddof=1))
    return float('nan') if vol < 1e-15 else float(excess.mean() / vol * np.sqrt(TRADING_DAYS))

def effective_bets(w: np.ndarray) -> float:
    s = float(np.sum(np.asarray(w, float) ** 2))
    return float("nan") if s == 0 else 1.0 / s

def effective_bets_corr(w, cov) -> float:
    """Correlation-aware bets: participation ratio of the eigenvalues of
    diag(w) Σ diag(w). Two perfectly correlated names at 50/50 give 1, not 2."""
    w = np.asarray(w, float)
    m = np.asarray(cov, float) * np.outer(w, w)
    lam = np.clip(np.linalg.eigvalsh((m + m.T) / 2.0), 0.0, None)
    s, ss = float(lam.sum()), float(np.sum(lam ** 2))
    return float("nan") if ss <= 0 else s * s / ss

def normalized_weights(weights, columns) -> pd.Series:
    if weights is None or not len(weights):
        raise ValueError('at least one weight is required')
    w = pd.Series(dict(weights), dtype=float)
    if not np.isfinite(w.to_numpy()).all() or (w < 0).any() or w.sum() <= 0:
        raise ValueError('weights must be finite, nonnegative and have a positive sum')
    missing = [k for k in w.index if k not in columns]
    if missing:
        raise ValueError('allocated holdings missing from prices: ' + ', '.join(missing))
    w = w[w > 0]
    return w / w.sum()

def portfolio_returns(rets: pd.DataFrame, w, rebalance: str = "annual") -> pd.Series:
    """Daily portfolio returns with drifting weights.

    "annual" (default) resets to the target weights on the first trading day of
    each calendar year, "daily" is the constant-mix rebalance-every-day case,
    "none" is buy-and-hold over the whole window.
    """
    w = normalized_weights(w, rets.columns)
    r = rets[list(w.index)]
    if not np.isfinite(r.to_numpy(float)).all() or (r < -1).any().any():
        raise ValueError("invalid long-only return series")
    if rebalance == "daily":
        out = (r * w).sum(axis=1)
        out.attrs.update(rets.attrs)
        return out
    if rebalance not in ("annual", "none"):
        raise ValueError(f"unknown rebalance: {rebalance}")
    segs = [r] if rebalance == "none" else [g for _, g in r.groupby(r.index.year)]
    legs = []
    for seg in segs:
        value = (1.0 + seg).cumprod().mul(w, axis=1).sum(axis=1)  # 1.0 at segment start
        legs.append(value / value.shift(1).fillna(1.0) - 1.0)
    out = pd.concat(legs) if legs else pd.Series(dtype=float,index=r.index)
    out.attrs.update(rets.attrs)
    return out

def pca_first_share(corr: pd.DataFrame) -> float:
    a = corr.to_numpy(float)
    if not np.isfinite(a).all():
        return float('nan')
    if corr.shape[0] < 2:
        return 1.0
    vals = np.linalg.eigvalsh(a)
    return float(vals.max() / vals.sum()) if vals.sum() > 0 else float('nan')

def corr_clusters(corr: pd.DataFrame, threshold: float = CORR_CLUSTER):
    """Connected components of the corr > threshold graph, size >= 2."""
    names = list(corr.columns)
    parent = {n: n for n in names}

    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    for i, a in enumerate(names):
        for b in names[i + 1:]:
            if float(corr.loc[a, b]) > threshold:
                ra, rb = find(a), find(b)
                if ra != rb:
                    parent[ra] = rb
    groups: dict = {}
    for n in names:
        groups.setdefault(find(n), []).append(n)
    return sorted([sorted(g) for g in groups.values() if len(g) > 1])

def _round(x, nd=4):
    if x is None:
        return None
    x = float(x)
    return None if not np.isfinite(x) else round(x, nd)

# --------------------------------------------------------------------------
# analysis
# --------------------------------------------------------------------------
def _window(index: pd.DatetimeIndex) -> dict:
    return {"start": str(index.min().date()), "end": str(index.max().date()),
            "n_days": int(len(index))}

def cluster_detail(corr: pd.DataFrame, weights=None, threshold: float = CORR_CLUSTER):
    """Connected high-correlation groups, not independent economic risk factors."""
    out = []
    for members in corr_clusters(corr, threshold):
        vals = corr.loc[members, members].to_numpy(float)[np.triu_indices(len(members), k=1)]
        out.append({'members': members, 'avg_corr': _round(vals.mean(), 3),
                    'min_corr': _round(vals.min(), 3), 'threshold': threshold,
                    'all_pairs_above_threshold': bool((vals > threshold).all()),
                    'weight': _round(sum(float(weights.get(m, 0)) for m in members))
                    if weights is not None else None})
    return out

def pair_diagnostics(rets: pd.DataFrame) -> list:
    """Sensitivity checks, not probability estimates or statistical break tests."""
    out = []
    names = list(rets.columns)
    for i, a in enumerate(names):
        for b in names[i+1:]:
            x, y = rets[a], rets[b]
            nz = (x != 0) & (y != 0)
            constant = x.std() < 1e-15 or y.std() < 1e-15
            out.append({'pair': [a, b], 'n_observations': len(x),
                        'pearson': None if constant else _round(x.corr(y), 4),
                        'spearman': None if constant else _round(x.corr(y, method='spearman'), 4),
                        'same_sign_fraction': _round((np.sign(x[nz]) == np.sign(y[nz])).mean())
                        if nz.any() else None,
                        'same_sign_observations': int(nz.sum())})
    return out


def price_fingerprint(px: pd.DataFrame) -> str:
    return hashlib.sha256((str(px.attrs.get('currency'))+'\n'+px.to_csv(float_format='%.12g')).encode()).hexdigest()

def analyze_frame(px: pd.DataFrame, bench: str, weights=None, warnings=None,
                  meta=None, rebalance: str = 'annual') -> dict:
    warnings = list(warnings or [])
    meta = meta or {}
    if (px.index.dayofweek>=5).any():
        raise ValueError('252-day analytics requires weekday close observations; explicitly resample seven-day market data first')
    rets = daily_returns(px)
    currency = px.attrs.get('currency')
    if not currency:
        declared = {meta.get(t,{}).get('currency') for t in px.columns}
        currency = next(iter(declared)) if len(declared) == 1 and None not in declared else None
    rf = None if px.attrs.get('risk_free_policy') == 'omit' else _rf_daily(rets.index, warnings, currency)
    rf_ann = float(rf.mean() * TRADING_DAYS) if rf is not None else None
    has_bench = bench in rets.columns
    if not has_bench:
        warnings.append(f'benchmark {bench} unavailable; beta and alpha omitted')
    rb = rets[bench] if has_bench else None
    if len(rets) < 126:
        warnings.append('fewer than 126 return observations; estimates are particularly unstable')
    if len(rets.index) > 1 and rets.index.to_series().diff().dt.days.max() > 7:
        warnings.append('common sample contains gaps longer than seven days; 252-day annualization may mislead')
    first_dates = px.attrs.get('first_dates', {})
    def stats(r):
        b, a, r2 = beta_alpha_r2(r, rb, rf if rf is not None else 0.0) if has_bench else (None, None, None)
        return {'ann_return': _round(ann_return(r)), 'ann_vol': _round(ann_vol(r)),
                'max_drawdown': _round(max_drawdown(r)), 'sharpe': _round(sharpe(r, rf), 3),
                'beta': _round(b, 3), 'alpha': _round(a) if rf is not None else None,
                'r2': _round(r2, 3)}
    assets = {}
    for t in rets.columns:
        m = meta.get(t) or {}
        fee = m.get('expense_ratio')
        if fee is not None and (not np.isfinite(fee) or not 0 <= fee < 1):
            raise ValueError(f'{t}: expense ratio must be a verified decimal between zero and one')
        assets[t] = dict(stats(rets[t]), first_date=first_dates.get(t),
                         expense_ratio=_round(fee, 6), currency=m.get('currency'),
                         fee_source=m.get('source'), fee_as_of=m.get('retrieved'))
    w = normalized_weights(weights, rets.columns) if weights is not None else None
    holdings = list(w.index) if w is not None else ([t for t in rets.columns if t != bench] or list(rets.columns))
    corr = rets[holdings].corr()
    out = {'schema_version': 2, 'window': _window(rets.index),
           'window_start': str(rets.index.min().date()), 'window_end': str(rets.index.max().date()),
           'n_days': len(rets), 'bench': bench if has_bench else None,
           'rf_annual': _round(rf_ann), 'rebalance': rebalance, 'currency': currency,
           'fx_converted': px.attrs.get('fx_converted', []), 'tickers': list(rets.columns),
           'assets': assets,
           'correlation': {a: {b: _round(corr.loc[a,b],3) for b in corr.columns} for a in corr.index},
           'pair_diagnostics': pair_diagnostics(rets[holdings]),
           'clusters': cluster_detail(corr, w.to_dict() if w is not None else None),
           'portfolio': None, 'warnings': warnings,
           'provenance': {'source': px.attrs.get('source', 'caller-supplied prices'),
                          'data_kind': px.attrs.get('data_kind', 'unspecified'),
                          'retrieved': px.attrs.get('retrieved'),
                          'price_sha256': price_fingerprint(px),
                          'price_start': str(px.index.min().date()),
                          'price_end': str(px.index.max().date()),
                          'coverage': px.attrs.get('coverage')},
           'methodology': {'returns': 'simple returns from supplied adjusted closes',
                           'sample': 'common observations; no price backfill',
                           'annualization': 252, 'cagr': 'compounded return annualized by observation count',
                           'beta': 'OLS slope; descriptive exposure, not a downside forecast',
                           'alpha': 'OLS daily excess-return intercept times 252',
                           'risk_free': 'Ken French daily USD RF' if rf is not None else None,
                           'backtest': 'hypothetical; no cash flows, taxes or trading frictions',
                           'regression': 'excess returns' if rf is not None else 'raw returns; alpha omitted',
                           'concentration': 'inverse weight HHI, not independent bets',
                           'risk_dimensions': 'covariance participation ratio, basis dependent; not a literal bet count'}}
    if w is not None:
        pr = portfolio_returns(rets, w, rebalance)
        known = {k: assets[k]['expense_ratio'] for k in w.index if assets[k]['expense_ratio'] is not None}
        coverage = sum(float(w[k]) for k in known)
        contribution = sum(float(w[k])*known[k] for k in known)
        p = dict(stats(pr), weights={k: float(v) for k,v in w.items()},
                 n_holdings=len(w), effective_bets=_round(effective_bets(w.to_numpy()),2),
                 effective_bets_corr=_round(effective_bets_corr(w.to_numpy(), rets[w.index].cov().to_numpy()),2),
                 pca_first_pc_share=_round(pca_first_share(corr),3),
                 expense_ratio=_round(contribution,6) if coverage >= 1-1e-10 else None,
                 fee_known_weight=_round(coverage,6), fee_known_contribution=_round(contribution,6),
                 fee_missing=[k for k in w.index if k not in known])
        p['weight_concentration_equivalent'] = p['effective_bets']
        p['covariance_participation_ratio'] = p['effective_bets_corr']
        out['portfolio'] = p
        if coverage < 1-1e-10:
            warnings.append('complete weighted expense ratio unavailable; fee coverage is partial')
    return out

def factor_regression(px: pd.DataFrame, model: int, warnings: list) -> dict:
    import statsmodels.api as sm
    if px.attrs.get('currency') != 'USD':
        raise ValueError('US Fama-French factors require explicitly USD-denominated returns')
    rets = daily_returns(px)
    try:
        ff = _load_factors(model)
    except Exception as exc:
        warnings.append(f'Fama-French {model}-factor download failed: {exc}')
        return {'window': None, 'window_start': None, 'window_end': None,
                'model': model, 'assets': {}, 'warnings': warnings}
    cols = [c for c in ff.columns if c != 'RF']
    data = rets.join(ff, how='inner').dropna()
    if len(data) < 126:
        raise ValueError('factor regression needs at least 126 common daily observations')
    X = sm.add_constant(data[cols], has_constant='add')
    if np.linalg.matrix_rank(X.to_numpy()) < X.shape[1]:
        raise ValueError('factor design matrix is rank deficient')
    out = {}
    for t in rets.columns:
        res = sm.OLS(data[t] - data['RF'], X).fit(cov_type='HAC', cov_kwds={'maxlags': 5})
        ci = res.conf_int()
        loadings = {c: {'beta': _round(res.params[c],3), 't': _round(res.tvalues[c],2),
                        'ci95': [_round(ci.loc[c,0],3), _round(ci.loc[c,1],3)]} for c in cols}
        out[t] = {'alpha_annual': _round(res.params['const']*TRADING_DAYS),
                  'alpha_t': _round(res.tvalues['const'],2), 'r2': _round(res.rsquared,3),
                  'loadings': loadings,
                  'tilts': {c: loadings[c]['beta'] for c in cols
                            if loadings[c]['t'] is not None and abs(loadings[c]['t']) > 2}}
    warnings.append('US factor model; correlations and coefficients are not causal or evidence of manager skill; no multiple-testing correction')
    return {'window': _window(data.index), 'window_start': str(data.index.min().date()),
            'window_end': str(data.index.max().date()), 'n_days': len(data), 'model': model,
            'currency': 'USD', 'factors': cols, 'assets': out, 'warnings': warnings,
            'standard_errors': 'HAC, 5 lags', 'screen': '|t| > 2 is an exploratory screen, not a truth label'}

# --------------------------------------------------------------------------
# regimes
# --------------------------------------------------------------------------
REGIMES = [
    ("2018 Q4 selloff", "2018-10-01", "2018-12-24"),
    ("2020 COVID crash", "2020-02-19", "2020-03-23"),
    ("2020-21 zero-rate rally", "2020-03-24", "2021-12-31"),
    ("2022 hiking cycle", "2022-01-01", "2022-10-12"),
    ("2023-24 AI / mega-cap rally", "2023-01-01", "2024-12-31"),
    ("2025-present", "2025-01-01", None),
]
HOLDING_PERIODS = [("1y", 1), ("2y", 2), ("3y", 3), ("5y", 5)]

def total_return(r: pd.Series) -> float:
    return float((1.0 + r).prod() - 1.0) if len(r) else float("nan")

def slice_regime(rets: pd.DataFrame, start, end) -> pd.DataFrame:
    lo = pd.Timestamp(start)
    hi = pd.Timestamp(end) if end else rets.index.max()
    return rets.loc[(rets.index >= lo) & (rets.index <= hi)]

def _leg_stats(r: pd.Series, rb, rf_d=0.0) -> dict:
    b = beta_alpha_r2(r, rb, rf_d)[0] if rb is not None and len(r) > 2 else None
    return {"total_return": _round(total_return(r)), "ann_vol": _round(ann_vol(r)),
            "max_drawdown": _round(max_drawdown(r)), "beta": _round(b, 3)}

def regime_table(rets: pd.DataFrame, bench: str, w: pd.Series | None,
                 min_days: int = 10, rebalance: str = "annual") -> list:
    rb_all = rets[bench] if bench in rets.columns else None
    rows = []
    for label, start, end in REGIMES:
        seg = slice_regime(rets, start, end)
        if len(seg) < min_days:
            continue
        rb = seg[bench] if rb_all is not None else None
        row = {"regime": label, "start": str(seg.index.min().date()),
               "end": str(seg.index.max().date()), "n_days": int(len(seg)),
               "assets": {t: _leg_stats(seg[t], rb) for t in seg.columns},
               "requested_start": start, "requested_end": end,
               "partial_window": bool(rets.index.min() > pd.Timestamp(start) + pd.Timedelta(days=7)
                                      or (end and rets.index.max() < pd.Timestamp(end) - pd.Timedelta(days=7))),
               "portfolio_method": "target weights restarted at each window boundary",
               "portfolio": None}
        if w is not None and len(w):
            pr = portfolio_returns(seg, w, rebalance)
            row["portfolio"] = _leg_stats(pr, rb)
        rows.append(row)
    return rows

def rolling_pair_corr(rets: pd.DataFrame, window: int = 126) -> pd.DataFrame:
    """Rolling correlation for every unordered pair, columns 'A|B'."""
    if window < 3:
        raise ValueError("rolling correlation window must be at least three observations")
    cols = list(rets.columns)
    out = {}
    for i, a in enumerate(cols):
        for b in cols[i + 1:]:
            out[f"{a}|{b}"] = rets[a].rolling(window).corr(rets[b])
    return pd.DataFrame(out).dropna(how="all")

def correlation_breaks(rc: pd.DataFrame, lookback: int = 63) -> list:
    """Per pair: current/min/max rolling corr and the date it moved most."""
    out = []
    if lookback < 1:
        raise ValueError("lookback must be positive")
    for pair in rc.columns:
        s = rc[pair].dropna()
        if s.empty:
            continue
        rec = {"pair": pair.split("|"), "current": _round(s.iloc[-1], 3),
               "min": _round(s.min(), 3), "max": _round(s.max(), 3),
               "min_date": str(s.idxmin().date()), "max_date": str(s.idxmax().date()),
               "broke_on": None, "broke_change": None}
        d = s.diff(lookback).dropna()
        if not d.empty:
            i = d.abs().idxmax()
            rec["broke_on"] = str(pd.Timestamp(i).date())
            rec["broke_change"] = _round(d.loc[i], 3)
        rec.update(largest_change_date=rec['broke_on'], largest_change=rec['broke_change'],
                   lookback_observations=lookback, structural_break_test=False)
        out.append(rec)
    return sorted(out, key=lambda r: -abs(r["broke_change"] or 0.0))

def holding_periods(rets: pd.DataFrame, bench: str, w: pd.Series | None,
                    rebalance: str = "annual") -> list:
    end = rets.index.max()
    rows = []
    for label, yrs in HOLDING_PERIODS:
        seg = rets.loc[rets.index >= end - pd.Timedelta(days=int(365.25 * yrs))]
        if (rets.index.min() > end - pd.Timedelta(days=int(365.25 * yrs)) + pd.Timedelta(days=7)
                or len(seg) < int(TRADING_DAYS * yrs * 0.9)):
            continue  # not enough history — no fake precision
        row = {"period": label, "start": str(seg.index.min().date()),
               "end": str(end.date()), "portfolio": None, "bench": None}
        if w is not None and len(w):
            pr = portfolio_returns(seg, w, rebalance)
            row["portfolio"] = {"total_return": _round(total_return(pr)),
                                "max_drawdown": _round(max_drawdown(pr))}
        if bench in seg.columns:
            row["bench"] = {"total_return": _round(total_return(seg[bench])),
                            "max_drawdown": _round(max_drawdown(seg[bench]))}
        rows.append(row)
    return rows

def regimes_frame(px: pd.DataFrame, bench: str, weights=None, window: int = 126,
                  warnings=None, rebalance: str = "annual") -> dict:
    warnings = [] if warnings is None else warnings
    if (px.index.dayofweek >= 5).any():
        raise ValueError('252-day analysis requires weekday-close observations; explicitly resample calendar-daily data')
    rets = daily_returns(px)
    w = None
    if weights:
        w = normalized_weights(weights, rets.columns)
    holdings = list(w.index) if w is not None else ([t for t in rets.columns if t != bench] or list(rets.columns))
    rc = rolling_pair_corr(rets[holdings], window)
    avg = rc.mean(axis=1).dropna() if len(rc.columns) else pd.Series(dtype=float)
    if bench not in rets.columns:
        warnings.append(f"benchmark {bench} unavailable — beta omitted")
    return {
        "window": _window(rets.index),
        "window_start": str(rets.index.min().date()),
        "window_end": str(rets.index.max().date()),
        "n_days": int(len(rets)), "bench": bench if bench in rets.columns else None,
        "corr_window": int(window),
        "rebalance": rebalance,
        "currency": px.attrs.get("currency"),
        "fx_converted": px.attrs.get("fx_converted", []),
        "regimes": regime_table(rets, bench, w, rebalance=rebalance),
        "correlation_breaks": correlation_breaks(rc),
        "avg_pairwise_corr": {"current": _round(avg.iloc[-1], 3) if len(avg) else None,
                              "mean": _round(avg.mean(), 3) if len(avg) else None,
                              "min": _round(avg.min(), 3) if len(avg) else None,
                              "max": _round(avg.max(), 3) if len(avg) else None},
        "holding_periods": holding_periods(rets, bench, w, rebalance),
        "warnings": warnings,
    }

# --------------------------------------------------------------------------
# 13F holdings (SEC EDGAR — free, no key; be polite: one request a second)
# --------------------------------------------------------------------------
SEC_UA = os.environ.get("SEC_USER_AGENT", "")
_SEC_LAST = [0.0]
_NAME_NOISE = re.compile(
    r"\b(INC|CORP|CORPORATION|CO|COMPANY|COMPANIES|LTD|LIMITED|PLC|SA|NV|AG|GROUP|"
    r"HOLDING|HOLDINGS|CLASS|CL|COM|COMMON|ORD|ORDINARY|SHS|SHARES|STOCK|NEW|THE|"
    r"LLC|LP|TR|TRUST|ADR|ADS|SPONSORED|REIT|PAR)\b")

def _sec_get(url: str, as_json: bool = False):
    if not SEC_UA or "@" not in SEC_UA:
        raise ValueError("set SEC_USER_AGENT to your app name and contact email before EDGAR access")
    import urllib.request

    gap = 1.0 - (time.time() - _SEC_LAST[0])
    if gap > 0:
        time.sleep(gap)
    req = urllib.request.Request(url, headers={"User-Agent": SEC_UA,
                                               "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read()
    _SEC_LAST[0] = time.time()
    return json.loads(raw) if as_json else raw.decode("utf-8", "replace")

def _norm_issuer(name: str) -> str:
    raw = re.sub(r"/[A-Z]{2,3}/?", " ", str(name).upper())  # EDGAR's "/DE/" suffix
    bare = re.sub(r"[^A-Z0-9 ]", " ", raw.replace("'", ""))
    out = _NAME_NOISE.sub(" ", bare)
    return " ".join(out.split())

def _sec_name_tickers() -> dict:
    """{normalised company name: ticker} from EDGAR's own ticker file."""
    try:
        data = _sec_get("https://www.sec.gov/files/company_tickers.json", True)
    except Exception:  # noqa: BLE001
        return {}
    out: dict = {}
    for v in data.values():
        tick = str(v.get("ticker") or "").upper()
        key = _norm_issuer(v.get("title"))
        if tick and key and len(tick) < len(out.get(key, "~" * 9)):
            out[key] = tick  # share classes: the shortest ticker is the main line
    return out

def _resolve_filer(query: str):
    """(cik10, name) for a 13F filer given a CIK or a company name."""
    import urllib.parse

    q = str(query).strip()
    if q.isdigit():
        return q.zfill(10), None
    atom = _sec_get("https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany"
                    f"&company={urllib.parse.quote(q)}&type=13F-HR&dateb="
                    "&owner=include&count=10&output=atom")
    ciks = re.findall(r"<CIK>(\d+)</CIK>", atom, re.I)
    names = re.findall(r"<conformed-name>([^<]+)</conformed-name>", atom, re.I)
    if not ciks:
        raise RuntimeError(f"no 13F filer on EDGAR matching {query!r}")
    if len(set(ciks)) != 1:
        cands = []
        for c in list(dict.fromkeys(ciks))[:6]:  # the search feed carries no names
            try:
                n = _sec_get(f"https://data.sec.gov/submissions/CIK{c.zfill(10)}.json",
                             True).get("name", "?")
            except Exception:  # noqa: BLE001
                n = "?"
            cands.append(f"{n} (CIK {c.zfill(10)})")
        raise ValueError("ambiguous filer name; supply the intended CIK. Candidates: "
                         + "; ".join(cands))
    return ciks[0].zfill(10), (names[0].strip() if names else None)

def _latest_13f(cik: str) -> dict:
    sub = _sec_get(f"https://data.sec.gov/submissions/CIK{cik}.json", True)
    rec = sub["filings"]["recent"]
    for i, form in enumerate(rec["form"]):
        if str(form).upper().startswith("13F-HR"):
            if str(form).upper() == '13F-HR/A':
                raise ValueError('latest filing is an amendment; reconcile cover page and original before using holdings')
            return {"fund": sub.get("name"), "period": rec["reportDate"][i],
                    "filed": rec["filingDate"][i],
                    "accession": rec["accessionNumber"][i].replace("-", "")}
    raise RuntimeError(f"CIK {cik} has filed no 13F-HR")

def _information_table(cik: str, accession: str) -> str:
    base = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accession}"
    idx = _sec_get(base + "/index.json", True)
    for item in idx["directory"]["item"]:
        name = item["name"]
        if name.lower().endswith(".xml") and "primary_doc" not in name.lower():
            text = _sec_get(f"{base}/{name}")
            if "infoTable" in text:
                return text
    raise RuntimeError("filing has no information table")

def parse_13f_table(xml_text: str) -> list:
    """Preserve security class, put/call and amount type; never net options into stock."""
    import xml.etree.ElementTree as ET
    rows = {}
    for el in ET.fromstring(xml_text.strip()).iter():
        if el.tag.split('}')[-1] != 'infoTable':
            continue
        f = {c.tag.split('}')[-1]:(c.text or '').strip() for c in el.iter()}
        cusip, issuer = f.get('cusip','').upper(), f.get('nameOfIssuer','')
        kind, share_class, amount_type = f.get('putCall','').upper(), f.get('titleOfClass',''), f.get('sshPrnamtType','SH')
        key = (cusip or issuer, share_class, kind, amount_type)
        rec = rows.setdefault(key, {'name':issuer,'cusip':cusip,'share_class':share_class,
                                    'put_call':kind or None,'amount_type':amount_type,
                                    'value':0.,'shares':0.})
        value, shares = float(f.get('value') or 0), float(f.get('sshPrnamt') or 0)
        if not np.isfinite(value) or not np.isfinite(shares) or min(value,shares)<0:
            raise ValueError('13F values and shares must be finite and nonnegative')
        rec['value'] += value
        rec['shares'] += shares
    return sorted(rows.values(),key=lambda r:-r['value'])

def holdings_from_table(rows: list, name_tickers=None, top=None):
    """Issuer-name matches are suggestions, NEVER verified backtest weights.

    The second return value deliberately remains an empty mapping. Supply a
    separately verified CUSIP/share-class mapping to construct a portfolio.
    """
    if top is not None and top < 1:
        raise ValueError('top must be positive')
    name_tickers = name_tickers or {}
    total = sum(r['value'] for r in rows)
    if not np.isfinite(total) or total <= 0:
        raise ValueError('13F table has no positive reported value')
    out = []
    for r in rows:
        suggestion = name_tickers.get(_norm_issuer(r['name'])) if not r.get('put_call') else None
        out.append({'name':r['name'],'cusip':r['cusip'],'ticker':None,
                    'ticker_suggestion':suggestion,'mapping_status':'unverified',
                    'share_class':r.get('share_class'), 'put_call':r.get('put_call'),
                    'amount_type':r.get('amount_type'),
                    'weight':_round(r['value']/total,6),'value_usd':_round(r['value'],2),
                    'shares':_round(r.get('shares'),2)})
    warnings = ['13F is a dated, incomplete disclosure, not current net portfolio exposure',
                'issuer-name ticker suggestions are unverified; no investable weights produced',
                'option-reported values describe underlying securities, not option market premiums']
    return out[:top] if top else out, {}, warnings

def suggested_weights(holdings: list) -> tuple[dict, float]:
    """Opt-in only: the name-matched subset, renormalised to sum to one.

    These are issuer-name guesses, not verified CUSIP/share-class mappings, so
    the caller gets the coverage back alongside them and must say it out loud.
    Options and unmapped positions are excluded, never silently substituted.
    """
    mapped = {}
    covered = 0.0
    for h in holdings:
        ticker = h.get('ticker') or h.get('ticker_suggestion')
        if not ticker or h.get('put_call'):
            continue
        mapped[ticker] = mapped.get(ticker, 0.0) + float(h['weight'])
        covered += float(h['weight'])
    if covered <= 0:
        return {}, 0.0
    return ({t: _round(v / covered, 6) for t, v in mapped.items()}, _round(covered, 6))

def fund_holdings(query: str, top: int = 15, use_cache: bool = True,
                  weights_from_suggestions: bool = False) -> dict:
    cik, name = _resolve_filer(query)
    meta = _latest_13f(cik)
    path = _cache_path(f"13f_v2_{cik}_{meta['accession']}.json")
    if use_cache and path.exists():
        rows = json.loads(path.read_text())
    else:
        rows = parse_13f_table(_information_table(cik, meta["accession"]))
        path.write_text(json.dumps(rows))
    warnings = []
    if meta["filed"] < "2023-01-03":  # pre-2023 tables report value in thousands
        for r in rows:
            r["value"] *= 1000.0
        warnings.append("pre-2023 filing: values scaled from thousands to dollars")
    holdings, weights, warn = holdings_from_table(rows, _sec_name_tickers(), top)
    coverage = None
    if weights_from_suggestions:
        weights, coverage = suggested_weights(holdings)
        if not weights:
            raise ValueError('no issuer-name ticker suggestions to build weights from')
        warn = [w for w in warn if 'no investable weights produced' not in w]
        warn = warn + [f'--weights-from-suggestions: weights are the renormalised '
                       f'name-matched subset covering {100 * coverage:.0f}% of the '
                       f'reported value of the positions shown. Mappings are unverified '
                       f'issuer-name guesses; share classes, options, non-13F assets, '
                       f'shorts and cash are not represented. State this caveat to the user.']
    out = {"fund": meta.get("fund") or name or str(query), "cik": cik,
            "period": meta["period"], "filed": meta["filed"],
            "total_value_usd": _round(sum(r["value"] for r in rows), 2),
            "n_positions": len(rows), "holdings": holdings, "weights": weights,
            "as_of": date.today().isoformat(),
            "report_age_days": (date.today()-date.fromisoformat(meta['period'])).days,
            "displayed_reported_value_fraction": _round(sum(h['weight'] for h in holdings),6),
            "verified_mapped_value_fraction": 0.0,
            "warnings": warnings + warn}
    if weights_from_suggestions:
        out["coverage"] = coverage
        out["weights_basis"] = "unverified issuer-name suggestions, renormalised"
    return out

# --------------------------------------------------------------------------
# statement ingestion — files to positions; layout is judgment, arithmetic ours
# --------------------------------------------------------------------------
CASH_SYMBOLS = {"CASH", "SPAXX", "FDRXX", "VMFXX", "VMMXX", "SWVXX", "SPRXX",
                "FZFXX", "SNVXX", "QACDS", "FGZXX"}
SUBUNIT_CCY = {"GBP": "GBP", "GBX": "GBP", "ILA": "ILS", "ZAC": "ZAR"}
_MM_NAME = re.compile(r"\b(cash|money\s*market|pending activity|core position|"
                      r"sweep|bank deposit)\b", re.I)
_TOTAL_NAME = re.compile(r"^(account|portfolio|grand|ending)?\s*total\b", re.I)
_OPTION_SYM = re.compile(r"^[A-Z]{1,6}\d{6}[CP]\d{8}$")
_OPTION_NAME = re.compile(r"\b(call|put)\b", re.I)
_CUSIP = re.compile(r"^(?=.*\d)[0-9A-Z]{9}$")
_BOND_NAME = re.compile(r"\b(bond|note|treasury|certificate|debenture|cd\b)", re.I)
_SYM_KEYS = ("symbol", "ticker", "cusip", "securityid", "security")
_DESC_KEYS = ("description", "investmentname", "securityname", "name", "investment")
_QTY_KEYS = ("quantity", "sharesheld", "shares", "qty", "units", "position")
_PX_KEYS = ("price", "lastprice", "shareprice", "closeprice", "currentprice", "nav")
_VAL_KEYS = ("valueinbase", "inbase", "marketvalue", "currentvalue",
             "positionvalue", "totalvalue", "endingvalue", "value", "balance")
_NUM_EXCLUDE = ("change", "%", "gain", "loss", "cost", "basis", "accrued",
                "income", "yield", "unrealized", "realized", "pnl")
_ASOF = re.compile(r"as of[^\d]*(\d{1,2})[/-](\d{1,2})[/-](\d{2,4})", re.I)


def normalize_symbol(raw) -> str:
    """Statement symbol -> yfinance form: BRK/B -> BRK-B, '*' and '$' out.
    Dot suffixes are left alone: .L/.MX/.DE are exchanges, not share classes."""
    s = re.sub(r"[\s$*]+", "", str(raw or "")).upper().lstrip("+-")
    s = re.sub(r"/([A-Z]{1,2})$", r"-\1", s)  # share-class separator
    return s if re.fullmatch(r"[A-Z0-9^=._-]{1,32}", s) else ""


def _parse_amount(text):
    """'($1,234.56)' -> -1234.56; unparseable -> None (never a guess)."""
    if text is None or isinstance(text, bool):
        return None
    if isinstance(text, (int, float)):
        return float(text) if np.isfinite(text) else None
    s = str(text).strip()
    if not s:
        return None
    neg = (s.startswith("(") and s.endswith(")")) or s.endswith("-")
    s = s.strip("()").rstrip("-").replace("$", "").replace(",", "").replace("%", "").strip()
    try:
        v = float(s)
    except ValueError:
        return None
    return -abs(v) if neg else v


def _pick_col(headers, keywords, exclude=()):
    """First keyword, searched across all headers, wins; excluded substrings disqualify."""
    for kw in keywords:
        for i, h in enumerate(headers):
            norm = re.sub(r"[^a-z]", "", str(h).lower())
            if norm and kw in norm and not any(b in norm for b in exclude):
                return i
    return None


def _classify(sym: str, name: str) -> str:
    n = name or ""
    if sym in CASH_SYMBOLS or (not sym and _MM_NAME.search(n)):
        return "cash"
    if _OPTION_SYM.match(sym.replace(" ", "")) or _OPTION_NAME.search(n):
        return "option"
    if _BOND_NAME.search(n) and "%" in n:
        return "fixed_income"
    if sym and not _CUSIP.match(sym):
        return "security"
    return "unknown"


def _extract_table(lines):
    """-> (headers, rows, preamble, layout_hint). Sectioned reports (IBKR-style
    NAME,Header/NAME,Data) first, else the first row that maps as a table header."""
    if any(len(r) > 1 and str(r[1]).strip().lower() in ("header", "data")
           for r in lines):
        headers, rows = None, []
        for row in lines:
            tag = str(row[1]).strip().lower() if len(row) > 1 else ""
            sec = str(row[0]).strip().lower() if row else ""
            if tag == "header" and sec in ("positions", "open positions"):
                headers = [str(c).strip() for c in row[2:]]
            elif tag == "data" and headers and sec in ("positions", "open positions"):
                rows.append([row[2 + i] if 2 + i < len(row) else ""
                             for i in range(len(headers))])
        if not headers or not rows:
            raise ValueError("sectioned report found but its positions block is empty")
        return headers, rows, [], "sectioned-report"
    for i, row in enumerate(lines[:80]):
        h = [str(c).strip() for c in row]
        if (_pick_col(h, _SYM_KEYS, ("description", "name", "type", "sector")) is not None
                and (_pick_col(h, _VAL_KEYS, _NUM_EXCLUDE) is not None
                     or _pick_col(h, _QTY_KEYS, _NUM_EXCLUDE) is not None)):
            preamble = [" ".join(str(c) for c in r).strip()[:200]
                        for r in lines[:i] if any(str(c).strip() for c in r)]
            return h, lines[i + 1:], preamble, "flat-table"
    raise ValueError("no positions table found; need a symbol/ticker column "
                     "plus a quantity or value column")


def _rows_to_records(headers, rows):
    sym_i = _pick_col(headers, _SYM_KEYS, ("description", "name", "type", "sector"))
    desc_i = _pick_col(headers, _DESC_KEYS, ("account",))
    qty_i = _pick_col(headers, _QTY_KEYS, _NUM_EXCLUDE + ("type", "category"))
    px_i = _pick_col(headers, _PX_KEYS, _NUM_EXCLUDE + ("average", "acquisition"))
    val_i = _pick_col(headers, _VAL_KEYS, _NUM_EXCLUDE)
    ccy_i = _pick_col(headers, ("currency", "ccy"))
    records, skipped = [], 0
    for row in rows:
        def get(i):
            return str(row[i]).strip() if i is not None and i < len(row) else ""
        sym_raw, name = get(sym_i), get(desc_i)
        if not sym_raw and not name:
            skipped += 1
            continue
        if _TOTAL_NAME.search(sym_raw) or (not sym_raw and _TOTAL_NAME.search(name)):
            skipped += 1
            continue
        qty, val = _parse_amount(get(qty_i)), _parse_amount(get(val_i))
        if qty is None and val is None:
            skipped += 1
            continue
        records.append({"symbol": sym_raw, "name": name, "quantity": qty,
                        "price": _parse_amount(get(px_i)), "value": val,
                        "currency": get(ccy_i).upper() or None})
    return records, skipped


def _read_csv_rows(path: Path):
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    return [r for r in csv.reader(io.StringIO(text))
            if any(c.strip() for c in r)]


def _read_xlsx(path: Path):
    try:
        import openpyxl
    except ImportError:
        raise ValueError("xlsx input needs openpyxl; export the statement as CSV")
    try:
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    except Exception as exc:
        raise ValueError(f"cannot read workbook: {exc}")
    try:
        rows = []
        for row in wb.active.iter_rows(values_only=True):
            vals = ["" if c is None else str(c) for c in row]
            if any(v.strip() for v in vals):
                rows.append(vals)
    finally:
        wb.close()
    return rows


def _statement_date(preamble):
    for line in preamble:
        m = _ASOF.search(line)
        if m:
            mm, dd, yy = int(m.group(1)), int(m.group(2)), int(m.group(3))
            yy += 2000 if yy < 100 else 0
            try:
                return date(yy, mm, dd).isoformat()
            except ValueError:
                return None
    return None


def _convert_value(amount, frm, to, warnings, label):
    frm = (frm or to).upper()
    if frm == to:
        return amount
    fx = _fx_series(frm, to)
    if fx is None or not len(fx):
        warnings.append(f"{label}: no {frm}->{to} rate; value left out of totals")
        return None
    lag = (pd.Timestamp.today().normalize() - fx.index[-1]).days
    if lag > 7:
        warnings.append(f"{label}: {frm}->{to} rate is {lag} days old")
    return amount * float(fx.iloc[-1])


def _latest_quote(symbol, warnings):
    """Last adjusted close plus its quote currency; None if unverifiable."""
    try:
        s = _fetch_one(symbol)
        px, pdate = float(s.iloc[-1]), str(s.index[-1].date())
    except Exception as exc:
        warnings.append(f"{symbol}: no usable price history ({exc})")
        return None
    meta = (_ticker_meta([symbol]) or {}).get(symbol) or {}
    qccy = str(meta.get("currency") or "").upper()
    if not qccy:
        warnings.append(f"{symbol}: quote currency unavailable; not priced")
        return None
    if qccy in SUBUNIT_CCY:
        px, qccy = px / 100.0, SUBUNIT_CCY[qccy]
    lag = (pd.Timestamp.today().normalize() - s.index[-1]).days
    if lag > 7:
        warnings.append(f"{symbol}: last price is {lag} days old")
    return px, qccy, pdate


def ingest_positions(records, currency: str, warnings=None) -> dict:
    """Normalize parsed/agent-read rows into positions, weights and a
    securities-only `analysis_ready` map. records: dicts with optional
    symbol/name/quantity/price/value/currency. Never guesses a value."""
    warnings = list(warnings or [])
    if not records:
        raise ValueError("no positions parsed")
    merged, order = {}, []
    for rec in records:
        sym = normalize_symbol(rec.get("symbol"))
        name = str(rec.get("name") or "").strip()
        hint = str(rec.get("type") or "").lower()
        typ = hint if hint in ("security", "cash", "option", "fixed_income",
                               "unknown") else _classify(sym, name)
        key = sym or ("CASH" if typ == "cash" else "NAME:" + name[:48].upper())
        if key not in merged:
            merged[key] = {"symbol": sym, "name": name, "type": typ,
                           "quantity": 0.0, "value": 0.0, "currency": None,
                           "price": None, "has_qty": False, "has_value": False}
            order.append(key)
        m = merged[key]
        qty, val = _parse_amount(rec.get("quantity")), _parse_amount(rec.get("value"))
        if qty is not None:
            m["quantity"] += qty; m["has_qty"] = True
        if val is not None:
            m["value"] += val; m["has_value"] = True
        pr = _parse_amount(rec.get("price"))
        if pr is not None:
            m["price"] = pr
        rc = str(rec.get("currency") or "").upper() or None
        if rc and m["currency"] and m["currency"] != rc:
            warnings.append(f"{key}: conflicting row currencies; using {rc}")
        m["currency"] = rc or m["currency"]
        if name and not m["name"]:
            m["name"] = name
    positions, unresolved = [], []
    for key in order:
        m = merged[key]
        label = m["symbol"] or m["name"] or key
        value, src, pdate = None, "stated", None
        if m["has_value"]:
            value = _convert_value(m["value"], m["currency"], currency, warnings, label)
        elif m["has_qty"] and m["price"] is not None:
            value = _convert_value(m["quantity"] * m["price"], m["currency"],
                                   currency, warnings, label)
            src = "stated-price"
        elif m["has_qty"] and m["type"] == "security" and m["symbol"]:
            quote = _latest_quote(m["symbol"], warnings)
            if quote:
                px, qccy, pdate = quote
                value = _convert_value(m["quantity"] * px, qccy, currency, warnings, label)
                src = "priced"
        elif m["has_qty"] and m["type"] == "cash":
            value = _convert_value(m["quantity"], m["currency"], currency, warnings, label)
            src = "par"  # money-market units are face value in the fund's currency
        if value is None:
            unresolved.append({"label": label, "symbol": m["symbol"] or None,
                               "name": m["name"] or None, "type": m["type"],
                               "value": None, "included_in_total": False,
                               "reason": "options are not priced by this engine"
                               if m["type"] == "option" else
                               "no stated value; could not verify a price"})
            continue
        pos = {"symbol": m["symbol"] or None, "name": m["name"] or None,
               "type": m["type"],
               "quantity": _round(m["quantity"], 6) if m["has_qty"] else None,
               "price": _round(m["price"], 4),
               "value": _round(value, 2), "value_source": src,
               "price_date": pdate}
        positions.append(pos)
        flagged = (m["type"] in ("option", "fixed_income", "unknown")
                   or (not m["symbol"] and m["type"] != "cash"))
        if flagged:
            reason = {"option": "option position; a different bet, excluded from analysis",
                      "fixed_income": "unmapped fixed income; identify by name",
                      "unknown": "symbol missing or unmapped; identify by name",
                      "cash": "cash row", "security": "symbol missing; identify by name"}[m["type"]]
            unresolved.append({"label": label, "symbol": m["symbol"] or None,
                               "name": m["name"] or None, "type": m["type"],
                               "value": pos["value"], "included_in_total": True,
                               "reason": reason})
    total = sum(p["value"] for p in positions)
    if total <= 0:
        raise ValueError("parsed positions sum to a non-positive total")
    for p in positions:
        p["weight"] = _round(p["value"] / total, 6)
    secs = [p for p in positions if p["type"] == "security" and p["symbol"]
            and p["value"] > 0]
    sec_sum = sum(p["value"] for p in secs)
    ready = {p["symbol"]: _round(p["value"] / sec_sum, 6) for p in secs} if sec_sum > 0 else {}
    cash = sum(p["value"] for p in positions if p["type"] == "cash")
    unident = sum(p["value"] for p in positions if p["type"] in ("unknown", "fixed_income"))
    return {"schema_version": 1, "command": "ingest", "currency": currency,
            "as_of": date.today().isoformat(), "total_value": _round(total, 2),
            "positions": positions, "analysis_ready": ready,
            "securities_value": _round(sec_sum, 2),
            "analysis_coverage": _round(sec_sum / total, 6),
            "cash_value": _round(cash, 2), "cash_weight": _round(cash / total, 6),
            "unidentified_value": _round(unident, 2), "unresolved": unresolved,
            "warnings": warnings,
            "notes": ["stated values are taken at face value in the declared currency",
                      "analysis_ready renormalizes priced securities only; cash, "
                      "options and unmapped names are excluded",
                      "weights are shares of the parsed total, not a recommendation"]}


def _pdf_payload(path: Path) -> dict:
    try:
        import pypdf
    except ImportError:
        raise ValueError("PDF input needs pypdf; read the statement yourself "
                         "and pass --positions JSON instead")
    try:
        reader = pypdf.PdfReader(str(path))
        text = "\n".join((pg.extract_text() or "") for pg in reader.pages[:40])
    except Exception as exc:
        raise ValueError(f"cannot extract PDF text ({exc}); read it directly "
                         "and pass --positions JSON")
    return {"schema_version": 1, "command": "ingest", "needs_agent": True,
            "source": {"file": path.name, "format": "pdf",
                       "pages": len(reader.pages)},
            "reason": "a PDF statement has no reliable column structure; "
                      "structuring it is judgment, so the engine does not guess",
            "extracted_text": text[:12000],
            "positions_schema": [{"symbol": "ticker as listed or null",
                                  "name": "name as printed",
                                  "quantity": 0, "value": 0,
                                  "currency": "ISO code or null"}],
            "next": "re-run: wm.py ingest --positions <file>.json --currency <CCY>"}


def cmd_ingest(args):
    import sys
    warnings, source, skipped = [], {}, 0
    if args.positions:
        if args.file:
            raise ValueError("give a statement file or --positions, not both")
        raw = (sys.stdin.read() if args.positions == "-"
               else Path(args.positions).read_text(encoding="utf-8-sig"))
        doc = json.loads(raw)
        records = doc.get("positions") if isinstance(doc, dict) else doc
        if not isinstance(records, list):
            raise ValueError('--positions expects a JSON list or {"positions": [...]}')
        source = {"format": "positions-json",
                  "file": None if args.positions == "-" else Path(args.positions).name}
    else:
        if not args.file:
            raise ValueError("supply a statement file or --positions JSON")
        path = Path(args.file)
        if not path.exists():
            raise ValueError(f"no such file: {path}")
        suffix = path.suffix.lower()
        if suffix == ".pdf":
            print(json.dumps(_pdf_payload(path), allow_nan=False))
            return
        lines = (_read_xlsx(path) if suffix in (".xlsx", ".xlsm")
                 else _read_csv_rows(path))
        headers, rows, preamble, hint = _extract_table(lines)
        records, skipped = _rows_to_records(headers, rows)
        source = {"file": path.name, "format": suffix.lstrip(".") or "csv",
                  "layout_hint": hint, "preamble": preamble[:4],
                  "as_of": _statement_date(preamble)}
        if source["as_of"]:
            age = (date.today() - date.fromisoformat(source["as_of"])).days
            if age > 45:
                warnings.append(f"statement is dated {source['as_of']} ({age} days "
                                "ago); positions may have moved")
    out = ingest_positions(records, args.currency, warnings)
    out["source"] = source
    out["skipped_rows"] = skipped
    if args.pretty:
        print(f"{source.get('file') or 'positions JSON'}  ->  "
              f"{len(out['positions'])} positions, "
              f"{out['currency']} {out['total_value']:,.0f} total")
        print(_table(["symbol", "type", "value", "weight"],
                     [[p["symbol"] or (p["name"] or "?")[:24], p["type"],
                       f"{p['value']:,.0f}", f"{100 * p['weight']:.1f}%"]
                      for p in out["positions"]]))
        if out["unresolved"]:
            print(_table(["unresolved", "in total", "why"],
                         [[u["label"][:30], "yes" if u["included_in_total"] else "no",
                           u["reason"][:52]] for u in out["unresolved"]]))
        for w in out["warnings"]:
            print(f"! {w}")
        return
    print(json.dumps(out, allow_nan=False))


# --------------------------------------------------------------------------
# portfolio construction
# --------------------------------------------------------------------------
def _cap(w: np.ndarray, max_w: float) -> np.ndarray:
    w = np.asarray(w, float)
    n = len(w)
    if not n or not np.isfinite(w).all() or (w < 0).any() or w.sum() <= 0:
        raise ValueError('invalid construction weights')
    if not np.isfinite(max_w) or not 0 < max_w <= 1 or n*max_w < 1-1e-10:
        raise ValueError(f'infeasible maximum weight {max_w} for {n} assets')
    if abs(n*max_w - 1) < 1e-10:
        return np.full(n, 1/n)
    w = w / w.sum()
    for _ in range(n+1):
        over = w > max_w+1e-12
        if not over.any():
            return w
        excess = float((w[over]-max_w).sum())
        w[over] = max_w
        free = w < max_w-1e-12
        basis = w[free]
        w[free] += excess * (basis/basis.sum() if basis.sum() > 0 else np.full(free.sum(), 1/free.sum()))
    raise ValueError('weight cap could not be satisfied')

def _optimize(objective, n: int, max_w: float) -> np.ndarray:
    from scipy.optimize import minimize
    initial = _cap(np.ones(n), max_w)
    res = minimize(objective, initial, method='SLSQP', bounds=[(0.,max_w)]*n,
                   constraints=({'type':'eq', 'fun':lambda w:w.sum()-1},),
                   options={'maxiter':500,'ftol':1e-12})
    if (not res.success or not np.isfinite(res.x).all() or abs(res.x.sum()-1)>1e-7
            or res.x.min() < -1e-8 or res.x.max() > max_w+1e-7):
        raise ValueError(f'optimizer did not produce feasible weights: {res.message}')
    w = np.clip(res.x,0,None)
    return w/w.sum()

def shrink_covariance(rets: pd.DataFrame) -> tuple[np.ndarray, float]:
    """Ledoit-Wolf scaled-identity estimator, no extra runtime dependency.

    Maximum-likelihood sample covariance (denominator n), centered returns.
    Checked against sklearn.covariance.LedoitWolf in the regression suite.
    """
    x = rets.to_numpy(float)
    if len(x)<2 or x.shape[1]<1 or not np.isfinite(x).all():
        raise ValueError('covariance requires finite observations')
    x = x-x.mean(axis=0)
    n,p = x.shape
    empirical = x.T@x/n
    mu = np.trace(empirical)/p
    target = mu*np.eye(p)
    delta = float(np.sum((empirical-target)**2))
    beta = float(np.sum(np.sum(x*x,axis=1)**2)/n**2-np.sum(empirical**2)/n)
    shrink = float(np.clip(beta/delta,0.,1.)) if delta>0 else 0.
    return (1-shrink)*empirical+shrink*target, shrink

def solve_weights(rets: pd.DataFrame, method: str, max_w: float = 1.0) -> pd.Series:
    names = list(rets.columns)
    n = len(names)
    if len(rets) < 2 or not np.isfinite(rets.to_numpy(float)).all():
        raise ValueError("construction requires complete finite returns")
    _cap(np.ones(n), max_w)  # enforce feasibility even for a single asset
    if n == 1:
        return pd.Series([1.0], index=names)
    cov = shrink_covariance(rets)[0] * TRADING_DAYS
    if method == "equal":
        w = _cap(np.full(n, 1.0 / n), max_w)
    elif method == "invvol":
        vol = np.array(rets.std(ddof=1).to_numpy(), dtype=float)
        vol[vol <= 0] = np.nan
        inv = np.nan_to_num(1.0 / vol)
        w = _cap(inv, max_w) if inv.sum() > 0 else _cap(np.full(n, 1.0), max_w)
    elif method == "minvar":
        w = _optimize(lambda w: float(w @ cov @ w), n, max_w)
    elif method == "riskparity":
        def obj(w):
            pv = float(np.sqrt(max(w @ cov @ w, 1e-18)))
            rc = w * (cov @ w) / pv
            return float(np.sum((rc - pv / n) ** 2))
        w = _optimize(obj, n, max_w)
    else:
        raise ValueError(f"unknown method: {method}")
    return pd.Series(w / w.sum(), index=names)

def build_weights(rets: pd.DataFrame, method: str, max_w: float,
                  sleeves=None, sleeve_weights=None) -> pd.Series:
    if not sleeves:
        return solve_weights(rets, method, max_w)
    sw = sleeve_weights if sleeve_weights is not None else {k:1/len(sleeves) for k in sleeves}
    if set(sw) != set(sleeves):
        raise ValueError('sleeve weights must identify exactly the supplied sleeves')
    shares = normalized_weights(sw, list(sleeves))
    seen, parts = set(), []
    for name, members in sleeves.items():
        if not members or len(set(members)) != len(members):
            raise ValueError(f'{name}: empty sleeve or duplicate members')
        if seen.intersection(members):
            raise ValueError('an asset cannot appear in multiple sleeves')
        seen.update(members)
        missing = set(members)-set(rets.columns)
        if missing:
            raise ValueError(f'{name}: missing sleeve members {sorted(missing)}')
        share = float(shares.get(name,0))
        if share:
            inner = solve_weights(rets[members], method, min(1.,max_w/share))
            parts.append(inner*share)
    if set(rets.columns) != seen:
        raise ValueError('every construction asset must belong to exactly one sleeve')
    w = pd.concat(parts).reindex([c for c in rets.columns if c in set().union(*(set(p.index) for p in parts))])
    if w.max() > max_w+1e-7 or abs(w.sum()-1)>1e-7:
        raise ValueError('global portfolio cap not satisfied')
    return w

# --------------------------------------------------------------------------
# report + cards — the report as a spec sheet
# --------------------------------------------------------------------------
# non-breaking space inside the index name: "S&P 500" must never wrap to "500"
BENCH_NAMES = {"SPY": "the S&P 500", "VOO": "the S&P 500",
               "IVV": "the S&P 500", "QQQ": "the Nasdaq 100",
               "VTI": "the whole US market", "ACWI": "world stocks",
               "VT": "world stocks", "IWM": "small caps"}
YEAR_WORDS = {1: "One year", 2: "Two years", 3: "Three years", 4: "Four years",
              5: "Five years", 7: "Seven years", 10: "Ten years"}
SLEEVE_WORDS = {"theme": "Theme", "ballast": "Core", "cash": "Cash",
                "core": "Core", "growth": "Growth", "income": "Income"}
FACTOR_WORDS = {"Mkt-RF": "The market", "SMB": "Smaller companies",
                "HML": "Cheap over pricey", "RMW": "Profitable companies",
                "CMA": "Careful spenders"}
VB_W = 640          # every chart is drawn in a 640-unit-wide viewBox
MAX_PTS = 600       # path points after min/max-preserving downsampling
HOVER_PTS = 160     # readout rows: a separate, uniform grid, to keep files small
SEP = "‖"      # readout row separator inside a data- attribute
MINUS = "−"    # real minus sign: hyphens read as dashes at tabular sizes
DOT = " · "   # middle dot with a thin space either side
GLYPH = 6.9    # measured advance of one mono glyph at label size, in viewBox units
MONO_EM = 0.6   # one mono glyph, as a fraction of its font size
LB_MAX = 24     # the largest viewBox size a chart label takes (the phone breakpoint)
LB_ADV = MONO_EM * LB_MAX   # so a grid laid out to this never crowds on a phone
FEE_COVERAGE_FULL = 0.90   # above this the blended fee is quoted without a caveat
GRID_MAX = 8    # more holdings than this and the grid becomes a ranked list

# v3 bento grid. Twelve columns, 960px, 16px gutters, 24px page margin.
# A card declares its span; the span sets both the grid track it occupies and the
# size a chart's labels are drawn at. Every chart is still authored in a 640-unit
# viewBox, so a narrow card scales that viewBox down harder and needs bigger
# label units to land back on 11 device px. The table is the inverse of the scale:
#   rendered px = viewBox px x (card content width / 640)
# Content widths at the 960 container are 870 / 483 / 406 / 329 for spans 12/7/6/5.
GRID_N = 12
SPANS = (12, 7, 6, 5, 3)
AX_VB = {12: 11, 7: 16, 6: 19, 5: 24, 3: 24}    # mono ticks/readouts, viewBox px
BD_VB = {12: 12, 7: 17, 6: 20, 5: 26, 3: 26}    # sans band/series labels
# Chart heights, viewBox units. Chosen so the two cards of a row land within a
# few px of each other; the grid stretches away whatever is left.
PLOT_H = {12: 256, 7: 256, 6: 284, 5: 336, 3: 256}
# Which card each section sits in, on the desktop grid.
SPAN = {"What you own": 12, "Growth of 100": 7, "Falls from peak": 5,
        "Moves with the market": 6, "Correlation over time": 6,
        "Move together": 12, "Details": 12, "Return by period": 12,
        "What is driving these": 12}
NUMS = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight",
        "nine", "ten", "eleven", "twelve", "thirteen", "fourteen", "fifteen",
        "sixteen", "seventeen", "eighteen", "nineteen", "twenty"]
TENS = ["", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy",
        "eighty", "ninety"]

# Regime band labels: (full, abbreviated). Small tracked caps, above the plot.
REGIME_CAPS = {
    "2018 Q4 selloff": ("2018 selloff", "2018"),
    "2020 COVID crash": ("COVID crash", "COVID"),
    "2020-21 zero-rate rally": ("Zero rates 20–21", "Zero rates"),
    "2022 hiking cycle": ("2022 hikes", "Hikes"),
    "2023-24 AI / mega-cap rally": ("AI rally 23–24", "AI rally"),
    "2025-present": ("2025 onward", "2025→"),
}
# Short prose forms of the regime names, for a bar chart's label gutter.
REGIME_SHORT = {
    "2018 Q4 selloff": "2018 selloff",
    "2020 COVID crash": "COVID crash",
    "2020-21 zero-rate rally": "Zero rates 20–21",
    "2022 hiking cycle": "2022 hikes",
    "2023-24 AI / mega-cap rally": "AI rally 23–24",
    "2025-present": "2025 onward",
}
# Sleeve families: theme runs warm (the accent), core runs ink grey, cash is paper
# grey. Colour is never decoration here — it only says which sleeve a holding is in.
SLEEVE_TONES = {"theme": ("th1", "th2"), "growth": ("th1", "th2"),
                "ballast": ("co1", "co2", "co3"), "core": ("co1", "co2", "co3"),
                "income": ("co1", "co2", "co3"), "bonds": ("ca1", "co3"),
                "cash": ("ca1", "co3")}
TONES = ("th1", "co1", "co2", "co3", "ca1", "th2")

# Lucide/Feather paths inherited from the supplied prototype. Upstream notices
# are retained below and in THIRD_PARTY_LICENSES.md. 24×24, drawn at 16px
# with a 1.5 stroke in currentColor and a non-scaling stroke so that 1.5 is
# 1.5 device-independent px at any rendered size.
ICONS = {
    "trending-down": '<path d="M16 17h6v-6"/><path d="m22 17-8.5-8.5-5 5L2 7"/>',
    "git-fork": '<circle cx="12" cy="18" r="3"/><circle cx="6" cy="6" r="3"/>'
                '<circle cx="18" cy="6" r="3"/>'
                '<path d="M18 9v2c0 .6-.4 1-1 1H7c-.6 0-1-.4-1-1V9"/>'
                '<path d="M12 12v3"/>',
    "activity": '<path d="M22 12h-2.48a2 2 0 0 0-1.93 1.46l-2.35 8.36a.25.25 0 0 1'
                '-.48 0L9.24 2.18a.25.25 0 0 0-.48 0l-2.35 8.36A2 2 0 0 1 4.49 12H2"/>',
    "receipt": '<path d="M12 17V7"/>'
               '<path d="M16 8h-6a2 2 0 0 0 0 4h4a2 2 0 0 1 0 4H8"/>'
               '<path d="M4 3a1 1 0 0 1 1-1 1.3 1.3 0 0 1 .7.2l.933.6a1.3 1.3 0 0 0 '
               '1.4 0l.934-.6a1.3 1.3 0 0 1 1.4 0l.933.6a1.3 1.3 0 0 0 1.4 0l.933-.6a'
               '1.3 1.3 0 0 1 1.4 0l.934.6a1.3 1.3 0 0 0 1.4 0l.933-.6A1.3 1.3 0 0 1 '
               '19 2a1 1 0 0 1 1 1v18a1 1 0 0 1-1 1 1.3 1.3 0 0 1-.7-.2l-.933-.6a1.3 '
               '1.3 0 0 0-1.4 0l-.934.6a1.3 1.3 0 0 1-1.4 0l-.933-.6a1.3 1.3 0 0 0'
               '-1.4 0l-.933.6a1.3 1.3 0 0 1-1.4 0l-.934-.6a1.3 1.3 0 0 0-1.4 0l-.933'
               '.6a1.3 1.3 0 0 1-.7.2 1 1 0 0 1-1-1z"/>',
    "layers": '<path d="M12.83 2.18a2 2 0 0 0-1.66 0L2.6 6.08a1 1 0 0 0 0 1.83l8.58 '
              '3.91a2 2 0 0 0 1.66 0l8.58-3.9a1 1 0 0 0 0-1.83z"/>'
              '<path d="M2 12a1 1 0 0 0 .58.91l8.6 3.91a2 2 0 0 0 1.65 0l8.58-3.9A1 1 '
              '0 0 0 22 12"/>'
              '<path d="M2 17a1 1 0 0 0 .58.91l8.6 3.91a2 2 0 0 0 1.65 0l8.58-3.9A1 1 '
              '0 0 0 22 17"/>',
    "line-chart": '<path d="M3 3v16a2 2 0 0 0 2 2h16"/><path d="m19 9-5 5-4-4-3 3"/>',
    "arrow-down-to-line": '<path d="M12 17V3"/><path d="m6 11 6 6 6-6"/>'
                          '<path d="M19 21H5"/>',
    "waves": '<path d="M2 12q2.5 2 5 0t5 0 5 0 5 0"/>'
             '<path d="M2 19q2.5 2 5 0t5 0 5 0 5 0"/>'
             '<path d="M2 5q2.5 2 5 0t5 0 5 0 5 0"/>',
    "grid-2x2": '<path d="M12 3v18"/><path d="M3 12h18"/>'
                '<rect x="3" y="3" width="18" height="18" rx="2"/>',
    "split": '<path d="M16 3h5v5"/><path d="M8 3H3v5"/>'
             '<path d="M12 22v-8.3a4 4 0 0 0-1.172-2.872L3 3"/><path d="m15 9 6-6"/>',
    "list": '<path d="M3 5h.01"/><path d="M3 12h.01"/><path d="M3 19h.01"/>'
            '<path d="M8 5h13"/><path d="M8 12h13"/><path d="M8 19h13"/>',
}

# Optical centring, in viewBox units. Lucide draws on a 24-unit grid but several
# glyphs are not centred on it: measured with getBBox() in the browser, any glyph
# whose geometric bounding box misses (12,12) by more than ICON_TOL units is
# nudged so its visual centre lands there. Re-measure with getBBox() in a browser after touching ICONS.
ICON_TOL = 1.0
ICON_NUDGE = {"split": (0.0, -0.5)}   # its stem runs to y=22, the arrow stops at 3

def icon(name: str, slot: str = "") -> str:
    """16px Lucide glyph. Always beside a label — never alone, never decorative."""
    body = ICONS[name]
    dx, dy = ICON_NUDGE.get(name, (0.0, 0.0))
    if dx or dy:
        body = f'<g transform="translate({dx:g} {dy:g})">{body}</g>'
    al = f' data-align="{slot}"' if slot else ""
    return (f'<svg class="wm-ic" data-icon="{name}"{al} viewBox="0 0 24 24" width="16" '
            f'height="16" fill="none" stroke="currentColor" stroke-width="1.5" '
            f'stroke-linecap="round" stroke-linejoin="round" '
            f'vector-effect="non-scaling-stroke" aria-hidden="true">'
            f'{body}</svg>')

ICON_LICENSE_NOTICE = 'ISC License\nCopyright (c) 2026 Lucide Icons and Contributors\nPermission to use, copy, modify, and/or distribute this software for any purpose with or without fee is hereby granted, provided that the above copyright notice and this permission notice appear in all copies.\nTHE SOFTWARE IS PROVIDED "AS IS" AND THE AUTHOR DISCLAIMS ALL WARRANTIES WITH REGARD TO THIS SOFTWARE INCLUDING ALL IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS. IN NO EVENT SHALL THE AUTHOR BE LIABLE FOR ANY SPECIAL, DIRECT, INDIRECT, OR CONSEQUENTIAL DAMAGES OR ANY DAMAGES WHATSOEVER RESULTING FROM LOSS OF USE, DATA OR PROFITS, WHETHER IN AN ACTION OF CONTRACT, NEGLIGENCE OR OTHER TORTIOUS ACTION, ARISING OUT OF OR IN CONNECTION WITH THE USE OR PERFORMANCE OF THIS SOFTWARE.\n\nThe MIT License (MIT)\nCopyright (c) 2013-2023 Cole Bemis\nPermission is hereby granted, free of charge, to any person obtaining a copy of this software and associated documentation files (the "Software"), to deal in the Software without restriction, including without limitation the rights to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of the Software, and to permit persons to whom the Software is furnished to do so, subject to the following conditions:\nThe above copyright notice and this permission notice shall be included in all copies or substantial portions of the Software.\nTHE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.'

FONT_LINK = ('<link rel="preconnect" href="https://fonts.googleapis.com">'
             '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>'
             '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?'
             'family=Geist:wght@400;500;600&family=Geist+Mono:wght@400;500'
             '&display=swap">')  # standalone only; fragments inherit the host and stay request-free

# Per-span chart label sizes live in two custom properties so a breakpoint can
# restate four numbers instead of twelve selectors.
def _fsz(ax: int, bd: int, spans=(7, 6, 5), hm=False) -> str:
    sel = ",".join([".wm"] + [f".wm .wm-s{s}" for s in spans]
                   + ([".wm .wm-hm"] if hm else []))
    return f"{sel}{{--wm-fa:{ax}px;--wm-fb:{bd}px}}"

# Below 900px every card is full width, so one pair of label sizes covers the page.
# The steps are the inverse of the card's scale: the narrower the page, the harder
# the 640-unit viewBox is squeezed, and the larger a label must be authored.
_STEPS = ((899, 12, 13), (759, 14, 15), (599, 18, 19), (479, 24, 26), (374, 27, 29))
# What each step changes beyond the two label sizes: at 900 the twelve columns
# collapse (key figures stay two across), at 600 the page margin and the masthead
# come down and the spec table pins its first column.
_EXTRA = {
    899: (".wm .wm-grid>*{grid-column:span 12}.wm .wm-kf{grid-column:span 6}"
          ".wm .wm-panes{display:block}.wm .wm-hmw{max-width:none;padding-top:0}"
          ".wm .wm-hmp{border-left:0;padding-left:0;margin-top:20px;padding-top:20px;"
          "border-top:1px solid var(--wm-edge)}"),
    599: ("\u002ewm{padding:32px 16px 40px}.wm h1{font-size:30px;line-height:34px}"
          ".wm .wm-lede{font-size:17px;line-height:26px}"
          ".wm .wm-scroll td:first-child,.wm .wm-scroll th:first-child"
          "{position:sticky;left:0;background:var(--wm-panel)}"
          ".wm .wm-slv{flex-wrap:wrap;gap:4px 20px}"
          ".wm .wm-slv>div{flex:0 0 auto!important}.wm .wm-slv b{padding-right:0}"),
}

def _responsive() -> str:
    """The same ladder twice: viewport for a page, container for a chat fragment."""
    out = []
    for kind in ("@media", "@container"):
        for bp, ax, bd in _STEPS:
            # the capped heatmap only rejoins the ladder once the card is
            # narrower than the cap, which is what happens under 600px
            out.append(f"{kind} (max-width:{bp}px){{{_EXTRA.get(bp, '')}"
                       f"{_fsz(ax, bd, hm=bp <= 599)}}}")
    return "".join(out)

WM_CSS = """
.wm{--wm-paper:#f7f6f2;--wm-panel:#ffffff;--wm-ink:#141414;--wm-muted:#6b6b6b;--wm-edge:#d9d7d0;--wm-grid:#d9d7d0;--wm-chip:rgba(20,20,20,.06);--wm-accent:#ff4f00;--wm-text-accent:#b63800;--wm-text-bench:#656565;--wm-bench:#8c8c8c;--wm-band:#8c8c8c;--wm-th1:#ff4f00;--wm-th2:#ff8a4c;--wm-co1:#1f1f1f;--wm-co2:#5a5a5a;--wm-co3:#9a9a9a;--wm-ca1:#c9c6bd;--wm-h0:#f2f1ed;--wm-h1:#d6d5d1;--wm-h2:#aaa9a6;--wm-h3:#7d7c7a;--wm-h4:#51504f;--wm-h5:#242423;--wm-ht0:#141414;--wm-ht1:#141414;--wm-ht2:#141414;--wm-ht3:#ffffff;--wm-ht4:#ffffff;--wm-ht5:#ffffff;--wm-fa:11px;--wm-fb:12px;--wm-sans:"Geist",Inter,-apple-system,BlinkMacSystemFont,"Helvetica Neue",Helvetica,sans-serif;--wm-mono:"Geist Mono","JetBrains Mono",ui-monospace,Menlo,monospace;color:var(--wm-ink);background:var(--wm-paper);font-family:var(--wm-sans);font-size:14px;line-height:22px;font-weight:400;letter-spacing:0;font-variant-numeric:tabular-nums;-webkit-font-smoothing:antialiased;max-width:960px;margin:0 auto;padding:40px 24px 48px;box-sizing:border-box;container-type:inline-size}
@media (prefers-color-scheme:dark){.wm{--wm-paper:#111111;--wm-panel:#1a1a1a;--wm-ink:#f2f0ea;--wm-muted:#9a9a9a;--wm-edge:#2a2a2a;--wm-grid:#2f2f2f;--wm-chip:rgba(242,240,234,.08);--wm-accent:#ff6a26;--wm-text-accent:#ff9565;--wm-text-bench:#b8b8b8;--wm-bench:#8c8c8c;--wm-band:#9a9a9a;--wm-th1:#ff6a26;--wm-th2:#ff9b64;--wm-co1:#dcd8cf;--wm-co2:#9b978e;--wm-co3:#6b6861;--wm-ca1:#3a3832;--wm-h0:#242423;--wm-h1:#3d3c3a;--wm-h2:#656462;--wm-h3:#8d8b86;--wm-h4:#b5b3ad;--wm-h5:#dddbd4;--wm-ht0:#f2f0ea;--wm-ht1:#f2f0ea;--wm-ht2:#f2f0ea;--wm-ht3:#1a1a1a;--wm-ht4:#1a1a1a;--wm-ht5:#1a1a1a}}
.wm *{box-sizing:border-box;letter-spacing:0}
.wm .wm-mast{margin:0 0 24px}
.wm h1{font:600 40px/44px var(--wm-sans);letter-spacing:-.03em;margin:0 0 8px;overflow-wrap:break-word}
.wm .wm-meta{font-family:var(--wm-mono);font-weight:400;font-size:11px;line-height:16px;color:var(--wm-muted);margin:0}
.wm .wm-lede{font:400 18px/28px var(--wm-sans);letter-spacing:-.01em;color:var(--wm-ink);margin:16px 0 0;max-width:34em;text-wrap:pretty}
.wm .wm-warn{font-size:14px;line-height:22px;color:var(--wm-text-accent);border-left:2px solid var(--wm-accent);padding-left:12px;margin:16px 0 0}
.wm .wm-grid{display:grid;grid-template-columns:repeat(12,1fr);gap:16px;align-items:stretch}
.wm .wm-s12{grid-column:span 12}
.wm .wm-s7{grid-column:span 7;--wm-fa:16px;--wm-fb:17px}
.wm .wm-s6{grid-column:span 6;--wm-fa:19px;--wm-fb:20px}
.wm .wm-s5{grid-column:span 5;--wm-fa:24px;--wm-fb:26px}
.wm .wm-s3{grid-column:span 3}
.wm .wm-hm{--wm-fa:14px;--wm-fb:15px}
.wm .wm-panes{display:flex;gap:20px;align-items:flex-start}
.wm .wm-hmw{flex:0 1 560px;min-width:0;padding-top:32px}
.wm .wm-hmp{flex:1 1 auto;min-width:0;align-self:stretch;border-left:1px solid var(--wm-edge);padding-left:20px}
.wm .wm-hph{font:500 12px/16px var(--wm-sans);color:var(--wm-muted);margin:0}
.wm .wm-pl{list-style:none;margin:12px 0 0;padding:0}
.wm .wm-plr{display:flex;align-items:center;gap:10px;padding:5px 0;font-family:var(--wm-mono);font-weight:400;font-size:12px;line-height:18px}
.wm .wm-pln{color:var(--wm-ink);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.wm .wm-plb{flex:1 1 auto;min-width:20px;height:4px;background:var(--wm-chip)}
.wm .wm-plb i{display:block;height:4px}
.wm .wm-plv{flex:0 0 4ch;text-align:right;color:var(--wm-muted)}
.wm .wm-card{background:var(--wm-panel);border:1px solid var(--wm-edge);border-radius:12px;padding:20px;display:flex;flex-direction:column;min-width:0;overflow:hidden}
.wm .wm-chip{width:28px;height:28px;flex:0 0 28px;border-radius:8px;background:var(--wm-chip);display:flex;align-items:center;justify-content:center;color:var(--wm-ink)}
.wm .wm-kk{font:500 12px/16px var(--wm-sans);color:var(--wm-muted);margin:12px 0 0;min-height:32px;display:flex;align-items:flex-end}
.wm .wm-kv{font-family:var(--wm-mono);font-weight:500;font-size:32px;line-height:36px;letter-spacing:-.02em;margin:4px 0 0;white-space:nowrap}
.wm .wm-kc{font:400 12px/16px var(--wm-sans);color:var(--wm-muted);margin:auto 0 0;padding-top:8px;overflow-wrap:break-word}
.wm .wm-ch{display:grid;grid-template-columns:24px 1fr auto;align-items:baseline;column-gap:10px;min-height:24px}
.wm .wm-hi{display:flex;align-items:center;width:24px;height:24px;color:var(--wm-muted)}
.wm .wm-hi::before{content:"\\200b";width:0;overflow:hidden;font:600 15px/24px var(--wm-sans)}
.wm .wm-ch h2{font:600 15px/24px var(--wm-sans);letter-spacing:-.01em;margin:0;min-width:0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.wm .wm-ro{font-family:var(--wm-mono);font-weight:400;font-size:11px;line-height:24px;color:var(--wm-muted);text-align:right;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;min-width:0;justify-self:end}
.wm .wm-body{margin-top:16px;flex:1 1 auto;display:flex;flex-direction:column;justify-content:flex-end;min-width:0}
.wm .wm-body.wm-top{justify-content:flex-start}
.wm .wm-foot{font:400 12px/18px var(--wm-sans);color:var(--wm-muted);margin:12px 0 0}
.wm .wm-ic{width:16px;height:16px;flex:0 0 16px;display:block;color:inherit}
.wm .wm-ic,.wm .wm-ic *{vector-effect:non-scaling-stroke}
.wm svg{display:block;width:100%;height:auto;overflow:visible;touch-action:pan-y}
.wm .wm-ax,.wm .wm-hl,.wm .wm-hd{font-family:var(--wm-mono);font-weight:400;font-size:var(--wm-fa)}
.wm .wm-bd,.wm .wm-bl{font-family:var(--wm-sans);font-weight:500;font-size:var(--wm-fb)}
.wm .wm-ax,.wm .wm-hl,.wm .wm-bd,.wm .wm-bl{fill:var(--wm-muted)}
.wm .wm-hd{fill:var(--wm-ink);font-weight:500}
.wm .wm-slv{display:flex;gap:2px;margin:0 0 8px}
.wm .wm-slv>div{display:flex;align-items:baseline;gap:8px;white-space:nowrap;overflow:hidden;min-width:0}
.wm .wm-slv span{font:500 12px/16px var(--wm-sans);color:var(--wm-muted)}
.wm .wm-slv b{font-family:var(--wm-mono);font-weight:400;font-size:12px;line-height:16px;color:var(--wm-ink);padding-right:12px}
.wm .wm-strip{display:flex;gap:2px;height:28px}
.wm .wm-seg{min-width:2px;transition:opacity .12s ease;cursor:default}
.wm .wm-alloc.wm-dim .wm-seg,.wm .wm-alloc.wm-dim .wm-tag{opacity:.32}
.wm .wm-alloc .wm-seg.wm-on,.wm .wm-alloc .wm-tag.wm-on{opacity:1}
.wm .wm-tags{list-style:none;margin:12px 0 0;padding:0;display:flex;flex-wrap:wrap;gap:6px 20px}
.wm .wm-tag{display:flex;align-items:center;gap:8px;font-family:var(--wm-mono);font-weight:400;font-size:12px;line-height:18px;color:var(--wm-ink);transition:opacity .12s ease}
.wm .wm-sw{width:10px;height:10px;flex:0 0 10px}
.wm .wm-tag b{font-weight:400;color:var(--wm-muted)}
.wm .wm-pairs{list-style:none;margin:0;padding:0}
.wm .wm-pair{display:flex;align-items:baseline;gap:12px;padding:7px 10px;margin-bottom:2px;font-family:var(--wm-mono);font-weight:400;font-size:12px;line-height:18px}
.wm .wm-pair b{font-weight:400;margin-left:auto}
.wm .wm-scroll{overflow-x:auto;flex:1 1 auto}
.wm .wm-scroll table{height:100%}
.wm table{border-collapse:collapse;width:100%}
.wm th,.wm td{text-align:right;padding:9px 14px 9px 0;border-bottom:1px solid var(--wm-edge);white-space:nowrap}
.wm th:last-child,.wm td:last-child{padding-right:0}
.wm th:first-child,.wm td:first-child{text-align:left}
.wm th{font:500 12px/16px var(--wm-sans);color:var(--wm-muted)}
.wm td{font-family:var(--wm-mono);font-weight:400;font-size:13px;line-height:18px}
.wm tbody tr:last-child td{border-bottom:0}
.wm tr.wm-me td{color:var(--wm-ink);font-weight:500}
.wm tr.wm-bench td{color:var(--wm-muted)}
""" + _responsive() + "\n"

WM_JS = """
(function(){
 document.querySelectorAll('.wm').forEach(function(root){
  if(root.dataset.wmReady)return;root.dataset.wmReady='1';
  function set(f,t){if(!f)return;var r=f.querySelector('.wm-ro'),live=f.querySelector('.wm-live');if(r)r.textContent=t;if(live)live.textContent=t;}
  root.querySelectorAll('[data-wm-toggle]').forEach(function(btn){btn.addEventListener('click',function(){
   var id=btn.dataset.wmToggle;
   root.querySelectorAll('[data-wm-toggle]').forEach(function(b){b.setAttribute('aria-pressed',String(b===btn));});
   root.querySelectorAll('[data-wm-view]').forEach(function(v){v.hidden=v.dataset.wmView!==id;
    if(!v.hidden){var sv=v.querySelector('svg'),f=v.closest('.wm-card');set(f,sv&&sv.getAttribute('aria-valuetext')||'');}
   });
  });});
  root.querySelectorAll('svg[data-wm-rows]').forEach(function(sv){
   var rows=sv.getAttribute('data-wm-rows').split('‖'),g=sv.getAttribute('data-wm-x').split(','),
       x0=+g[0],x1=+g[1],vb=sv.viewBox.baseVal.width,f=sv.closest('.wm-card'),cr=sv.querySelector('.wm-cross'),
       n=rows.length,pos=(sv.getAttribute('data-wm-pos')||'').split(',').map(Number),current=n-1,base=f&&f.querySelector('.wm-ro')?f.querySelector('.wm-ro').textContent:'';
   if(n<2||x1<=x0)return;if(pos.length!==n)pos=rows.map(function(_,i){return x0+(x1-x0)*i/(n-1);});
   sv.setAttribute('tabindex','0');sv.setAttribute('role','slider');sv.setAttribute('aria-label','Sampled historical observation; use left and right arrow keys');
   sv.setAttribute('aria-valuemin','0');sv.setAttribute('aria-valuemax',String(n-1));
   function at(i){current=Math.max(0,Math.min(n-1,i));set(f,rows[current]);
    sv.setAttribute('aria-valuenow',String(current));sv.setAttribute('aria-valuetext',rows[current]);
    if(cr){var cx=pos[current];cr.setAttribute('x1',cx);cr.setAttribute('x2',cx);cr.style.opacity=1;}}
   function move(e){var p=e.touches?e.touches[0]:e,b=sv.getBoundingClientRect();if(!b.width)return;
    var x=(p.clientX-b.left)/b.width*vb,best=0;for(var j=1;j<n;j++)if(Math.abs(pos[j]-x)<Math.abs(pos[best]-x))best=j;at(best);}
   sv.addEventListener('mousemove',move);sv.addEventListener('touchmove',move,{passive:true});sv.addEventListener('touchstart',move,{passive:true});
   sv.addEventListener('focus',function(){at(current);});
   sv.addEventListener('keydown',function(e){var d={ArrowLeft:-1,ArrowDown:-1,ArrowRight:1,ArrowUp:1};
    if(e.key in d){e.preventDefault();at(current+d[e.key]);}else if(e.key==='Home'||e.key==='End'){e.preventDefault();at(e.key==='Home'?0:n-1);}});
   sv.addEventListener('mouseleave',function(){if(document.activeElement!==sv){set(f,base);if(cr)cr.style.opacity=0;}});
   sv.setAttribute('aria-valuenow',String(current));sv.setAttribute('aria-valuetext',rows[current]);
  });
  root.querySelectorAll('[data-wm-r]').forEach(function(el){var f=el.closest('.wm-card'),a=el.closest('.wm-alloc'),k=el.getAttribute('data-wm-k'),
    base=f&&f.querySelector('.wm-ro')?f.querySelector('.wm-ro').textContent:'';
   function on(){set(f,el.getAttribute('data-wm-r'));if(a&&k){a.classList.add('wm-dim');a.querySelectorAll('[data-wm-k]').forEach(function(m){if(m.dataset.wmK===k)m.classList.add('wm-on');});}}
   function off(){set(f,base);if(a){a.classList.remove('wm-dim');a.querySelectorAll('.wm-on').forEach(function(m){m.classList.remove('wm-on');});}}
   el.addEventListener('mouseenter',on);el.addEventListener('touchstart',on,{passive:true});el.addEventListener('mouseleave',off);el.addEventListener('touchend',off,{passive:true});
  });
 });
})();
"""

# Small local enhancements; no third-party JavaScript, fonts or icon requests.
WM_CSS += """
.wm [hidden]{display:none!important}
.wm :focus-visible{outline:2px solid var(--wm-ink);outline-offset:4px}
.wm details{margin-top:20px;min-width:0}
.wm summary{font:500 14px/22px var(--wm-sans);cursor:pointer;padding:8px 0}
.wm .wm-evidence{font:400 12px/19px var(--wm-sans);color:var(--wm-muted);margin:18px 0 0;overflow-wrap:anywhere}
.wm .wm-evidence p{margin:6px 0}
.wm .wm-toggle{display:inline-flex;gap:4px;padding:4px;border:1px solid var(--wm-edge);border-radius:10px;margin:0 0 18px;max-width:100%}
.wm .wm-toggle button{font:500 13px/20px var(--wm-sans);border:0;background:transparent;color:var(--wm-muted);padding:8px 14px;cursor:pointer;border-radius:6px}
.wm .wm-toggle button[aria-pressed=true]{background:var(--wm-ink);color:var(--wm-panel)}
.wm .wm-summary{display:grid;grid-template-columns:1fr 1fr;gap:20px;margin-bottom:18px}
.wm .wm-summary span{display:block;font:500 12px/16px var(--wm-sans);color:var(--wm-muted)}
.wm .wm-summary strong{display:block;font:500 32px/36px var(--wm-mono);letter-spacing:-.02em;margin-top:4px;white-space:nowrap}
.wm .wm-live{display:block;font:400 12px/20px var(--wm-mono);color:var(--wm-muted);min-height:20px;margin-top:8px;overflow-wrap:anywhere}
.wm .wm-note{font:400 14px/22px var(--wm-sans);margin:10px 0 18px}
.wm .wm-badge{font:500 11px/18px var(--wm-mono);padding:4px 8px;border:1px solid var(--wm-edge);display:inline-block;margin-bottom:16px;border-radius:4px}
.wm .wm-a11y{position:absolute;width:1px;height:1px;overflow:hidden;clip-path:inset(50%)}
.wm .wm-compare .wm-ch{grid-template-columns:24px 1fr}
.wm .wm-compare .wm-ro{display:none}
@media (prefers-reduced-motion:reduce){.wm *{transition:none!important}}
@media (max-width:374px){.wm .wm-summary strong{font-size:26px;line-height:32px}.wm .wm-card{padding:16px}.wm .wm-toggle button{padding:8px 12px}}
@container (max-width:374px){.wm .wm-summary strong{font-size:26px;line-height:32px}.wm .wm-card{padding:16px}.wm .wm-toggle button{padding:8px 12px}}
"""

def _esc(s) -> str:
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))

def _ticks(lo: float, hi: float, n: int = 4):
    """3-5 round numbers spanning [lo, hi]."""
    if not (np.isfinite(lo) and np.isfinite(hi)) or hi <= lo:
        return [lo]
    raw = (hi - lo) / max(n, 1)
    mag = 10.0 ** np.floor(np.log10(raw))
    step = float(next((m for m in (1, 2, 2.5, 5, 10) if m * mag >= raw), 10) * mag)
    out, v = [], float(np.ceil(lo / step) * step)
    while v <= hi + step * 1e-6:
        out.append(round(v, 10))
        v += step
    return out or [lo, hi]

def _thin(n: int, target: int = HOVER_PTS):
    """Evenly spaced indices, always including the first and last point."""
    return list(range(n)) if n <= target else sorted(
        {int(round(i * (n - 1) / (target - 1))) for i in range(target)})

def _bucket(n: int, cols, target: int = MAX_PTS):
    """Min/max-preserving downsample: each bucket keeps every series' extremes.

    Evenly thinning a long series clips the troughs it happens to step over, so
    a drawdown loses its real bottom. Keeping each bucket's high and low keeps
    the shape honest and the line continuous.
    """
    cols = [c for c in cols if c]
    if n <= target:
        return list(range(n))
    if not cols:
        return _thin(n, target)
    nb = max(2, (target - 2) // (2 * len(cols)))   # the two endpoints are always kept
    keep = {0, n - 1}
    for b in range(nb):
        i0, i1 = int(b * n / nb), int((b + 1) * n / nb)
        if i1 <= i0:
            continue
        keep.add(i0)
        for col in cols:
            seg = [(col[i], i) for i in range(i0, i1)
                   if col[i] is not None and np.isfinite(col[i])]
            if seg:
                keep.add(min(seg)[1])
                keep.add(max(seg)[1])
    return sorted(keep)

def _day(ts) -> str:
    return pd.Timestamp(ts).strftime("%d %b %y")

def _date_ticks(dates, sx, x0=None, x1=None):
    """Ticks on calendar boundaries, never on a data index.

    Over three years: 1 January of each year. One to three years: every six
    months. Under a year: the first of each month.
    """
    arr = pd.DatetimeIndex([pd.Timestamp(d) for d in dates])
    d0, d1, n = arr[0], arr[-1], len(arr)
    if n < 2 or d1 <= d0:
        return []
    span = (d1 - d0).days / 365.25
    months, fmt = ((1,), "%Y") if span > 3 else \
                  (((1, 7), "%b %y") if span >= 1 else (tuple(range(1, 13)), "%b"))
    marks = [pd.Timestamp(year=y, month=m, day=1)
             for y in range(d0.year, d1.year + 1) for m in months]
    marks = [t for t in marks if d0 <= t <= d1]
    while len(marks) > 12:                       # never crowd the axis
        marks = marks[::2]
    out = []
    for t in marks:
        k = int(np.searchsorted(arr.values, np.datetime64(t)))
        if k <= 0:
            pos = 0.0
        elif k >= n:
            pos = float(n - 1)
        else:
            a, b = arr[k - 1], arr[k]
            pos = (k - 1) + ((t - a) / (b - a) if b > a else 0.0)
        out.append((sx(pos), t.strftime(fmt)))
    return out

HAIR = 'stroke-width="1" vector-effect="non-scaling-stroke"'

def _gut(lab: int) -> int:
    """Room under the plot for one line of x ticks, drawn at `lab` viewBox units."""
    return int(round(lab * 1.8))

def _base(lab: int) -> int:
    """Baseline of an x tick, below the axis, at `lab` viewBox units."""
    return 4 + int(round(lab * 1.15))

def _frame(x0, x1, y0, y1, yt, yfmt, xlab, lab: int = 11) -> str:
    """Gridlines span the whole column; their value rides on the line, at the right.

    The page has one hard left edge, so nothing may sit to the left of a plot: a
    y tick label goes inside the plot, right-aligned against the column's right
    edge and set just above its own gridline. X ticks hang below the plot,
    left-aligned to their tick, and a label that would overrun the right edge is
    dropped rather than clipped. `lab` is the size those ticks are drawn at, in
    viewBox units - it varies by card span, so the baseline has to follow it.
    """
    drop = max(LB_ADV, MONO_EM * lab)   # the phone is the crowded case: size for it
    out = []
    for v, y in yt:
        out.append(f'<line x1="{x0}" x2="{x1}" y1="{y:.1f}" y2="{y:.1f}" '
                   f'stroke="var(--wm-grid)" {HAIR}/>')
    for x, t in xlab:
        if not (x0 - 1 <= x <= x1 + 1) or x + len(str(t)) * drop > x1 + 1:
            continue
        out.append(f'<line x1="{x:.1f}" x2="{x:.1f}" y1="{y1:.1f}" y2="{y1 + 4:.1f}" '
                   f'stroke="var(--wm-grid)" {HAIR}/>'
                   f'<text x="{x:.1f}" y="{y1 + _base(lab):.1f}" class="wm-ax">'
                   f'{_esc(t)}</text>')
    return "".join(out)

def _ygut(vals, yfmt) -> float:
    """Width of the right-hand tick gutter, in viewBox units.

    Sized for the longest value at the largest size a label is ever authored at
    (the phone step), so the gutter never moves when the page reflows.
    """
    if not vals:
        return 0.0
    return max(len(str(yfmt(v))) for v in vals) * LB_ADV + 8

def _yticks(x, yt, yfmt, lab: int = 11) -> str:
    """The y values, in a gutter of their own to the right of the plot.

    They used to ride inside the plot on the gridline, which put them on top of
    the series on any chart whose line runs to the right edge. They sit outside
    it now, optically centred on their own gridline, so nothing is ever drawn
    across them and no knockout is needed.
    """
    return "".join(
        f'<text x="{x}" y="{y + lab * 0.34:.1f}" text-anchor="end" class="wm-ax">'
        f'{_esc(yfmt(v)).replace("-", MINUS)}</text>' for v, y in yt)

def _cross(x0, y0, y1) -> str:
    return (f'<line class="wm-cross" x1="{x0}" x2="{x0}" y1="{y0}" y2="{y1}" '
            f'stroke="var(--wm-ink)" opacity="0" pointer-events="none" {HAIR}/>')

def _band_strip(bands, sx, n0, y0, y1, lab: int = 12):
    """Regime tint in the plot, its label horizontal in the strip above it.

    A label is abbreviated to fit its band, and dropped outright before it
    would collide with a neighbour - the narrower band loses its label.
    """
    drawn, picked = [], []
    for label, i0, i1 in bands:
        a, b = sx(i0), sx(i1)
        if b - a < 4:
            continue
        drawn.append(f'<rect x="{a:.1f}" y="{y0}" width="{b - a:.1f}" '
                     f'height="{y1 - y0}" fill="var(--wm-band)" opacity="0.05"/>')
        full, short = REGIME_CAPS.get(label, (label, label))
        adv = max(LB_ADV, 0.55 * lab)          # sans is narrower than mono per glyph
        text = next((t for t in (full, short) if len(t) * adv + 8 <= b - a), None)
        if text:
            picked.append({"x": (a + b) / 2, "w": len(text) * adv,
                           "span": b - a, "t": text})
    picked.sort(key=lambda p: p["x"])
    for i, p in enumerate(picked):                      # collision pass
        for q in picked[i + 1:]:
            if not q.get("drop") and not p.get("drop") and \
                    abs(p["x"] - q["x"]) < (p["w"] + q["w"]) / 2 + 14:
                (p if p["span"] < q["span"] else q)["drop"] = True
    drawn += [f'<text x="{p["x"]:.1f}" y="{y0 - max(8, lab * 0.5):.1f}" '
              f'text-anchor="middle" class="wm-bd">{_esc(p["t"])}</text>'
              for p in picked if not p.get("drop")]
    return "".join(drawn)

def _svg(h, body, rows=None, xr=None, positions=None) -> str:
    attrs = (f' data-wm-rows="{_esc(SEP.join(rows))}" data-wm-x="{xr[0]},{xr[1]}"' if rows else '')
    if positions is not None:
        attrs += ' data-wm-pos="'+','.join(f'{v:.3f}' for v in positions)+'"'
    return (f'<svg viewBox="0 0 {VB_W} {h}" width="100%" role="img" aria-label="Historical data chart"{attrs}>'
            '<title>Historical data chart</title>'+body+'</svg>')

# ---- charts ---------------------------------------------------------------
def svg_line(dates, series, height=230, yfmt=None, bands=None, hline=None,
             hlabel=None, ends=False, rows=None, lab=11, blab=12):
    """Multi-series line. series: [{label, values, cls}] - cls 'me' or 'bench'."""
    yfmt = yfmt or (lambda v: f"{v:,.0f}")
    n0 = len(dates)
    cols0 = [s["values"] for s in series]
    flat = [v for col in cols0 for v in col if v is not None and np.isfinite(v)]
    if not flat or n0 < 2:
        return ""
    keep = _bucket(n0, cols0)
    lo, hi = min(flat), max(flat)
    if hline is not None:
        lo, hi = min(lo, hline), max(hi, hline)
    if hi == lo:
        hi = lo + 1.0
    pad = (hi - lo) * 0.08
    # a lane above the plot for the regime captions, sized to the type in it
    strip = int(round(blab * 1.9)) if bands else 0
    # the plot opens on the card's content edge and closes short of the right one:
    # the y values live in a gutter of their own, never on top of the series
    vals = _ticks(lo, hi)
    x0, x1 = 0, VB_W - _ygut(vals, yfmt)
    y0, y1 = 22 + strip, height - _gut(lab)
    # x is always the position in the *original* series, so the time axis stays
    # linear however unevenly the downsampler picked its points.
    elapsed=(pd.to_datetime(dates)-pd.Timestamp(dates[0])).total_seconds().to_numpy(float)
    sx = lambda i: x0+(x1-x0)*np.interp(i,np.arange(n0),elapsed)/elapsed[-1]
    sy = lambda v: y1 - (y1 - y0) * (v - lo + pad) / (hi - lo + 2 * pad)
    out = []
    if bands:
        out.append(_band_strip(bands, sx, n0, y0, y1, blab))
    yt = [(v, sy(v)) for v in vals]
    out.append(_frame(x0, x1, y0, y1, yt, yfmt, _date_ticks(dates, sx), lab))
    if hline is not None:
        out.append(f'<line x1="{x0}" x2="{x1}" y1="{sy(hline):.1f}" y2="{sy(hline):.1f}" '
                   f'stroke="var(--wm-bench)" stroke-dasharray="1 3" {HAIR}/>')
        # the reference only needs naming when no y tick already prints it
        if hlabel and all(abs(sy(v) - sy(hline)) > 5 for v in vals):
            out.append(f'<text x="{VB_W}" y="{sy(hline) + lab * 0.34:.1f}" '
                       f'text-anchor="end" class="wm-ax">{_esc(hlabel)}</text>')
    tips = []
    for s, col in zip(series, cols0):
        me = s.get("cls") == "me"
        pts = [i for i in keep if col[i] is not None and np.isfinite(col[i])]
        if not pts:
            continue
        d = "M" + " L".join(f"{sx(i):.1f} {sy(col[i]):.1f}" for i in pts)
        out.append(f'<path d="{d}" fill="none" stroke-linejoin="round" '
                   f'stroke-linecap="round" '
                   f'stroke="var(--wm-{"accent" if me else "bench"})" '
                   f'stroke-width="{2 if me else 1.3}" '
                   f'stroke-dasharray="{"none" if me else "4 3"}" '
                   f'vector-effect="non-scaling-stroke"/>')
        if ends:
            tips.append((s["label"], me, col))
    # end labels: nudge apart so two lines that finish close together stay legible
    # The right edge belongs to the y ticks now, so a series names itself on its own
    # line, at the point where the series are furthest apart - the one place a label
    # can sit without landing on its neighbour.
    if tips:
        def _at(i):
            vs = [c[i] for _, _, c in tips
                  if c[i] is not None and np.isfinite(c[i])]
            return (max(vs) - min(vs)) if len(vs) > 1 else 0.0
        span = [i for i in keep if 0.18 <= i / (n0 - 1) <= 0.78] or keep
        at = max(span, key=_at) if len(tips) > 1 else span[-1]
        ranked = sorted(tips, key=lambda t: -(t[2][at] if t[2][at] is not None else 0))
        for rank, (label, me, col) in enumerate(ranked):
            if col[at] is None or not np.isfinite(col[at]):
                continue
            up = rank == 0                       # the upper series labels above
            tone = "var(--wm-text-accent)" if me else "var(--wm-text-bench)"
            out.append(f'<text x="{sx(at):.1f}" y="{sy(col[at]) + (-9 if up else 17):.1f}" '
                       f'text-anchor="middle" class="wm-bl" style="fill:{tone}" '
                       f'stroke="var(--wm-panel)" stroke-width="4" '
                       f'paint-order="stroke">{_esc(label)}</text>')
    out.append(_yticks(VB_W, yt, yfmt, lab))
    out.append(_cross(x0, y0, y1))
    hov = _thin(n0)                               # hover rows: their own even grid
    if rows is None:
        rows = [DOT.join([_day(dates[i])] + [f'{s["label"]} {yfmt(col[i])}'
                for s, col in zip(series, cols0)
                if col[i] is not None and np.isfinite(col[i])]) for i in hov]
    else:
        rows = [rows[i] for i in hov]
    return _svg(height, "".join(out), rows, (x0, x1), [sx(i) for i in hov])

def svg_area(dates, values, height=190, yfmt=None, label="down", mark=True, lab=11):
    """Filled area hanging from a zero baseline - the drawdown chart."""
    yfmt = yfmt or (lambda v: f"{100 * v:.0f}%")
    col, n0 = list(values), len(values)
    if n0 < 2:
        return ""
    keep = _bucket(n0, [col])
    lo = min(min(col), 0.0) * 1.12 or -0.01
    vals = [v for v in _ticks(lo, 0.0) if v <= 0]
    x0, x1 = 0, VB_W - _ygut(vals, yfmt)
    y0, y1 = 22, height - _gut(lab)
    elapsed=(pd.to_datetime(dates)-pd.Timestamp(dates[0])).total_seconds().to_numpy(float)
    sx = lambda i: x0+(x1-x0)*np.interp(i,np.arange(n0),elapsed)/elapsed[-1]
    sy = lambda v: y0 + (y1 - y0) * v / lo
    line = " L".join(f"{sx(i):.1f} {sy(col[i]):.1f}" for i in keep)
    yt = [(v, sy(v)) for v in vals]
    body = [_frame(x0, x1, y0, y1, yt, yfmt, _date_ticks(dates, sx), lab),
            f'<path d="M{x0:.1f} {y0} L{line} L{x1:.1f} {y0}Z" '
            f'fill="var(--wm-accent)" opacity="0.1"/>',
            f'<path d="M{line}" fill="none" stroke="var(--wm-accent)" '
            f'stroke-width="1.6" stroke-linejoin="round" vector-effect="non-scaling-stroke"/>']
    if mark:
        # the single worst day, named: a spec sheet states its worst case
        t = int(np.argmin(col))
        tx, ty = sx(t), sy(col[t])
        anchor = "end" if tx > x1 * 0.62 else "start"
        dx = -lab * 0.7 if anchor == "end" else lab * 0.7
        body.append(f'<circle cx="{tx:.1f}" cy="{ty:.1f}" r="{lab * 0.24:.1f}" '
                    f'fill="var(--wm-accent)"/>'
                    f'<text x="{tx + dx:.1f}" y="{ty + lab * 0.36:.1f}" '
                    f'text-anchor="{anchor}" '
                    f'class="wm-ax" style="fill:var(--wm-ink)" '
                    f'stroke="var(--wm-panel)" stroke-width="4" paint-order="stroke">'
                    f'{_esc(yfmt(col[t]).replace("-", MINUS))}{DOT}'
                    f'{_esc(_month(dates[t]))}</text>')
    body.append(_yticks(VB_W, yt, yfmt, lab))
    body.append(_cross(x0, y0, y1))
    rows = [f"{_day(dates[i])}{DOT}{label} {yfmt(col[i])}" for i in _thin(n0)]
    return _svg(height, "".join(body), rows, (x0, x1), [sx(i) for i in _thin(n0)])

def _short(label, cap=7) -> str:
    """A ticker sized for a grid cell: drop the exchange suffix, then cap the stem.

    NAFTRAC.MX is NAFTRAC on the Mexican exchange; inside a correlation grid the
    suffix is noise, and the stem is what the reader recognises.
    """
    stem = re.split(r"[.\-=]", str(label))[0] or str(label)
    return stem if len(stem) <= cap else stem[:max(1, cap - 1)] + "…"

def _corr_num(v) -> str:
    """A correlation cell drops its leading zero: |r| <= 1, so the 0 says nothing.

    Four characters at most, which is what lets an eight-name grid hold its
    numerals at 11px on a 375px screen without scrolling.
    """
    v = float(v)
    body = "1.0" if abs(v) >= 0.995 else f"{abs(v):.2f}"[1:]
    return (MINUS if v < 0 else "") + body

def _pairs(labels, matrix):
    """Finite unordered pairs, greatest positive correlation first; missing is not zero."""
    return sorted(((float(matrix[i][j]),labels[i],labels[j])
                   for i in range(len(labels)) for j in range(i)
                   if matrix[i][j] is not None and np.isfinite(matrix[i][j])),key=lambda t:-t[0])

def html_pairs(labels, matrix, top=24):
    """Past eight holdings a grid is unreadable on a phone: rank the pairs instead.

    One line per pair, strongest first, the row tinted on the same ramp the grid
    uses - so a dark band at the top is the same signal a dark corner was.
    """
    pairs = _pairs(labels, matrix)
    rows = []
    for v, a, b in pairs[:top]:
        k = max(0, min(5, int(round(min(1.0, abs(v)) * 5))))
        rows.append(f'<li class="wm-pair" style="background:var(--wm-h{k});'
                    f'color:var(--wm-ht{k})" data-wm-r="{_esc(a)} &amp; {_esc(b)}'
                    f'{DOT}{v:.2f}"><span>{_esc(a)}{DOT}{_esc(b)}</span>'
                    f'<b>{f"{v:.2f}".replace("-", MINUS)}</b></li>')
    return f'<ul class="wm-pairs">{"".join(rows)}</ul>', len(pairs)

PANE_ROWS = 8      # the pane is a reading aid, not a second table

def html_pane(labels, matrix, clusters=None, top: int = PANE_ROWS) -> str:
    """The grid's numbers, ranked: the same `_pairs` data as a short bar list.

    A correlation grid answers "how does every pair relate"; it does not answer
    "which pair is closest" without the reader scanning it. This pane does, in
    the space a capped grid leaves beside it. Accent marks the pairs the cluster
    finder identified as a connected correlation group.
    """
    pairs = _pairs(labels, matrix)[:top]
    if not pairs:
        return ""
    fam = {m for m in ((clusters or [{}])[0] or {}).get("members", [])}
    names = [f"{_short(a, 8)}{DOT}{_short(b, 8)}" for _, a, b in pairs]
    wide = max(len(n) for n in names)
    rows = []
    for (v, a, b), nm in zip(pairs, names):
        one = a in fam and b in fam
        rows.append(
            f'<li class="wm-plr" data-align="pane-row" data-wm-r="{_esc(a)} &amp; '
            f'{_esc(b)}{DOT}{v:.2f}">'
            f'<span class="wm-pln" style="flex:0 0 {wide}ch">{_esc(nm)}</span>'
            f'<span class="wm-plb"><i style="width:{min(1.0, abs(float(v))):.1%};'
            f'background:var(--wm-{"accent" if one else "ink"})"></i></span>'
            f'<span class="wm-plv">{_corr_num(v)}</span></li>')
    return ('<div class="wm-hmp" data-align="pane">'
            '<div class="wm-hph" data-align="pane-head">Closest pairs</div>'
            f'<ul class="wm-pl">{"".join(rows)}</ul></div>')

def svg_heatmap(labels, matrix, height=None):
    """Annotated grid, one hue in six steps, shaded on |value|."""
    n = len(labels)
    if not n:
        return ''
    full_labels = list(labels)
    top = 24
    # Sized for the phone breakpoint, where a label glyph is widest: every numeral
    # must clear 11px there, so the grid never needs a sideways scroller.
    need = 4 * LB_ADV + 4                      # the widest cell value, "-.02"
    room = VB_W - 8 - need * n
    cap = max(3, min(7, int((room - 10) / LB_ADV)))
    labs = [_short(l, cap) for l in labels]
    # column heads are centred, so shrink only the ones that would actually touch
    cell0 = (VB_W - (max(len(l) for l in labs) * LB_ADV + 10) - 8) / n
    for _ in range(8):
        hit = [j for j in range(n - 1)
               if (len(labs[j]) + len(labs[j + 1])) * LB_ADV / 2 + 4 > cell0]
        if not hit:
            break
        for j in hit:
            k = j if len(labs[j]) >= len(labs[j + 1]) else j + 1
            if len(labs[k]) > 3:
                labs[k] = labs[k][:-1]
    left = max(len(l) for l in labs) * LB_ADV + 12
    cell = min((VB_W - left) / n, 118.0)
    left = VB_W - cell * n                       # the grid closes on the right edge
    mid = lambda k: left + cell * (k + 0.5)
    labels = labs
    out = [f'<text x="{mid(j):.1f}" y="{top - 8}" text-anchor="middle" class="wm-hl">'
           f'{_esc(lab)}</text>' for j, lab in enumerate(labels)]
    for i, lab in enumerate(labels):
        # row names open on the column's left edge, like every other element
        out.append(f'<text x="0" y="{top + cell * (i + 0.5) + 4:.1f}" class="wm-hl">'
                   f'{_esc(lab)}</text>')
        for j in range(n):
            if i == j:
                # an asset against itself is always 1.00 and says nothing:
                # leave a faint empty square rather than a row of dark cells
                out.append(
                    f'<rect x="{left + cell * j + 1.5:.1f}" y="{top + cell * i + 1.5:.1f}" '
                    f'width="{cell - 3:.1f}" height="{cell - 3:.1f}" fill="none" '
                    f'stroke="var(--wm-edge)" {HAIR}/>')
                continue
            raw = matrix[i][j]
            if raw is None or not np.isfinite(raw):
                out.append(f'<text x="{mid(j):.1f}" y="{top+cell*(i+.5)+3.6:.1f}" text-anchor="middle" class="wm-hl" aria-label="unavailable">n/a</text>')
                continue
            v = float(raw)
            b = max(0, min(5, int(round(min(1.0, abs(v)) * 5))))
            out.append(
                f'<rect x="{left + cell * j + 1.5:.1f}" y="{top + cell * i + 1.5:.1f}" '
                f'width="{cell - 3:.1f}" height="{cell - 3:.1f}" '
                f'fill="var(--wm-h{b})" data-wm-r="{_esc(full_labels[i])} &amp; '
                f'{_esc(full_labels[j])}{DOT}{v:.2f}"/>'
                f'<text x="{mid(j):.1f}" y="{top + cell * (i + 0.5) + 3.6:.1f}" '
                f'text-anchor="middle" class="wm-hl" pointer-events="none" '
                f'style="fill:var(--wm-ht{b})">{_corr_num(v)}</text>')
    return _svg(height or int(top + n * cell + 8), "".join(out))

def svg_bars(groups, fmt=None, row_h=30):
    """Horizontal grouped bars around a zero line. groups: [{label, bars:[...]}].

    A group may carry a `head`: a sub-heading on the left edge that opens a run of
    groups, so the row label can stay short enough to fit a phone's label gutter.
    """
    fmt = fmt or (lambda v: f"{100 * v:+.0f}%")
    vals = [b["value"] for g in groups for b in g["bars"] if b["value"] is not None]
    nrow = sum(len(g["bars"]) for g in groups)
    if not vals or not nrow:
        return ""
    lo, hi = min(min(vals), 0.0), max(max(vals), 0.0)
    span = (hi - lo) or 1.0
    # the label gutter is sized to the longest label at the phone breakpoint, then
    # capped: past the cap a label is cut rather than run off the left of the plot
    wide = max(len(str(g["label"])) for g in groups)
    left = min(max(120.0, LB_ADV * wide + 14), 300.0)
    fits = int((left - 14) / LB_ADV)
    right = VB_W - 56
    zero = left + (right - left) * -lo / span
    head_h = row_h * 0.95
    heads = sum(1 for i, g in enumerate(groups)
                if g.get("head") and (i == 0 or g["head"] != groups[i - 1].get("head")))
    bh = row_h * 0.34
    h = int(nrow * row_h + heads * head_h + 24)
    out = [f'<line x1="{zero:.1f}" x2="{zero:.1f}" y1="8" y2="{h - 16}" '
           f'stroke="var(--wm-grid)" {HAIR}/>']
    y, last = 12.0, None
    for g in groups:
        if g.get("head") and g["head"] != last:
            last = g["head"]
            out.append(f'<text x="0" y="{y + head_h - 11:.1f}" class="wm-hd">'
                       f'{_esc(last)}</text>')
            y += head_h
        lab = str(g["label"])
        lab = lab if len(lab) <= fits else lab[:max(1, fits - 1)].rstrip() + "…"
        out.append(f'<text x="0" y="{y + len(g["bars"]) * row_h / 2 - 3:.1f}" '
                   f'class="wm-bl">{_esc(lab)}</text>')
        for b in g["bars"]:
            v, top = b["value"], y
            y += row_h
            if v is None:
                continue
            x = left + (right - left) * (min(v, 0.0) - lo) / span
            w = max((right - left) * abs(v) / span, 1.2)
            tone = {"me": "accent", "bench": "bench"}.get(b.get("cls"), "ink")
            out.append(f'<rect x="{x:.1f}" y="{top:.1f}" width="{w:.1f}" '
                       f'height="{bh:.1f}" fill="var(--wm-{tone})" '
                       f'data-wm-r="{_esc(g["label"])}{DOT}'
                       f'{_esc(b["label"])} {_esc(fmt(v))}"/>'
                       # the value always sits just past the bar's right edge,
                       # so it never lands on the label gutter
                       f'<text x="{x + w + 6:.1f}" y="{top + bh - 1:.1f}" '
                       f'class="wm-hl" style="fill:var(--wm-ink)">'
                       f'{_esc(fmt(v)).replace("-", MINUS)}</text>')
    return _svg(h, "".join(out))

# ---- text helpers ---------------------------------------------------------
def _rolling_beta(pr: pd.Series, rb: pd.Series, window: int = TRADING_DAYS) -> pd.Series:
    cov = pr.rolling(window).cov(rb)
    var = rb.rolling(window).var()
    return (cov / var).dropna()

def _signed(x):
    if x is None:
        return "—"
    return (MINUS if x < 0 else "") + _bare(x)

def _bare(x) -> str:
    """The same number with no sign - for a sentence that already says "fell"."""
    return "—" if x is None else f"{int(abs(100 * float(x)) + 0.5)}%"

def _upper1(s: str) -> str:
    return s[:1].upper() + s[1:]

def _count_word(n) -> str:
    """A sentence never opens on a numeral, so counts are spelled out to ninety-nine."""
    n = int(n)
    if 0 <= n < len(NUMS):
        return NUMS[n]
    if 20 < n < 100:
        return TENS[n // 10] + (f"-{NUMS[n % 10]}" if n % 10 else "")
    return str(n)

def _half_word(x) -> str:
    """2.55 -> "three". A regular person does not own half a bet."""
    whole = int(math.floor(float(x) + 0.5))
    word = _count_word(whole)
    return word

def _bets_note(clusters, n) -> str:
    """Comparison line for the bets figure: name the look-alikes when we know them."""
    c = (clusters or [None])[0]
    if c and c.get("members"):
        return f"{_and_list(c['members'])} move as one"
    return f"Of {_count_word(int(n))} holdings" if n else "After counting look-alikes as one"

def _month(s):
    try:
        return pd.Timestamp(s).strftime("%b %Y")
    except Exception:  # noqa: BLE001
        return str(s)

def _name_of(rets, bench) -> str:
    held = [t for t in rets.columns if t != bench]
    return DOT.join(held) if 0 < len(held) <= 6 else "Your portfolio"

def _headline(portfolio: dict) -> str:
    name = (portfolio.get("name") or "").strip()
    parts = [p for p in name.rstrip("…").split("+") if p]
    held = {t.upper() for t in portfolio.get("weights", {})}
    if not name or (len(parts) > 1 and all(p.upper() in held for p in parts)):
        return "Your portfolio"
    return name

def lede(p: dict, b: dict, bname: str) -> str:
    """One factual sentence pair, built from whatever stats exist.

    Every clause is optional: drop the clause, keep the sentence grammatical.
    """
    out = []
    n = p.get("n_holdings")
    if n:
        s = f"{_upper1(_count_word(n))} holding{'' if int(n) == 1 else 's'}"
        bets = p.get("effective_bets_corr", p.get("covariance_participation_ratio"))
        if bets is not None and int(n) > 1:
            s += f" that behave like {_half_word(bets)}"
        out.append(s + ".")
    clauses = []
    if p.get("beta") is not None:
        clauses.append(f"moves at {float(p['beta']):.2f}\u00d7 the market")
    if p.get("max_drawdown") is not None:
        c = f"its worst fall was {_signed(p['max_drawdown'])}"
        if (b or {}).get("max_drawdown") is not None:
            c += f" against {bname}'s {_signed(b['max_drawdown'])}"
        clauses.append(c)
    if clauses:
        out.append(("It " + " and ".join(clauses) if clauses[0].startswith("moves")
                    else _upper1(clauses[0])) + ".")
    return " ".join(out)

def _keys(p: dict, b: dict, bname: str, clusters=None) -> str:
    """Four key-figure cards: chip, label, value, comparison. One row of the grid."""
    dd, bdd = p.get("max_drawdown"), (b or {}).get("max_drawdown")
    bets = p.get("effective_bets_corr", p.get("covariance_participation_ratio"))
    beta, er = p.get("beta"), p.get("expense_ratio")
    # Fee coverage is rarely exactly complete. Above 90% of the weight the
    # blended number is the honest headline; below it, the same number is shown
    # but labelled with how much of the portfolio it actually speaks for.
    cov = p.get("fee_known_weight")
    cov = float(cov) if cov is not None else None
    if er is None and cov and cov > 0:
        er = float(p.get("fee_known_contribution") or 0.0) / cov
    if er is None or cov == 0:
        er, fee_note = None, "no published fee for these holdings"
    elif cov is not None and cov < FEE_COVERAGE_FULL:
        fee_note = f"published for {100 * cov:.0f}% of the weight"
    elif er == 0:
        fee_note = "none of these are funds"
    else:
        fee_note = "Blended fund fees per year"
    rows = [
        ("trending-down", "Worst fall", _signed(dd),
         f"{_upper1(bname)} fell {_bare(bdd)}" if bdd is not None
         else "peak to trough, over the window"),
        ("git-fork", "Separate bets",
         "\u2014" if bets is None else f"{int(math.floor(float(bets) + 0.5))} of {p.get('n_holdings', '\u2014')}",
         _bets_note(clusters, p.get("n_holdings"))),
        ("activity", "Moves with the market",
         "\u2014" if beta is None else f"{float(beta):.2f}\u00d7",
         f"Per 1% move in {bname}" if beta is not None
         else "measured against the benchmark"),
        ("receipt", "Annual cost",
         "\u2014" if er is None else f"{100 * float(er):.2f}%", fee_note),
    ]
    return "".join(
        f'<div class="wm-card wm-kf wm-s3" data-align="key-card" data-span="3">'
        f'<span class="wm-chip" data-align="key-chip">{icon(ic)}</span>'
        f'<span class="wm-kk" data-align="key-label">{_esc(k)}</span>'
        f'<span class="wm-kv" data-align="key-value">{_esc(v)}</span>'
        f'<span class="wm-kc" data-align="key-cmp">{_esc(c)}</span></div>'
        for ic, k, v, c in rows)

def card_html(ic, title, body, ro="", foot="", span=12, fit=True, cls="") -> str:
    """One bento card: header row, then content, then an optional footline.

    The header is a baseline grid so the readout and the title sit on one
    baseline; the icon rides in a 24px box whose own baseline is the title's, so
    its centre and the title's centre are the same y to the pixel.
    """
    return (f'<section class="wm-card wm-s{span}{" " + cls.strip() if cls else ""}" data-align="card" '
            f'data-span="{span}"><div class="wm-ch" data-align="head">'
            f'<span class="wm-hi" data-align="head-icon">{icon(ic)}</span>'
            f'<h2 data-align="head-title">{_esc(title)}</h2>'
            f'<span class="wm-ro" data-align="head-ro">{_esc(ro)}</span></div>'
            f'<div class="wm-body{"" if fit else " wm-top"}" data-align="body">'
            f'{body}</div>'
            + (f'<p class="wm-foot">{_esc(foot)}</p>' if foot else "") + "</section>")

# A correlation grid that filled twelve columns would draw cells the size of
# buttons; it is capped and left-aligned in its card instead.
CARD_CLS = {"Move together": " wm-hm"}

class Cards:
    """The cards of the grid, in the order they are added. No numbers, no rules."""

    def __init__(self, spans=None):
        self.spans, self.parts = (SPAN if spans is None else spans), []

    def span(self, title) -> int:
        return self.spans.get(title, 12)

    def add(self, block) -> None:
        if not block:
            return
        ic, title, cap, svg, ro, foot = block
        if not svg:
            return
        note = " ".join(t for t in (cap, foot) if t)
        self.parts.append(card_html(ic, title, svg, ro, note, self.span(title),
                                    fit=title != "What you own",
                                    cls=CARD_CLS.get(title, "")))

    def html(self) -> str:
        return "".join(self.parts)

def _order_colors(weights, sleeves):
    """Holdings in sleeve order; one family of tones per sleeve when there are sleeves."""
    order = [t for s in (sleeves or {}).values() for t in s if t in weights]
    order += [t for t in weights if t not in order]
    if not sleeves:
        return order, {t: TONES[i % len(TONES)] for i, t in enumerate(order)}
    color = {}
    for key, members in sleeves.items():
        fam = SLEEVE_TONES.get(key.lower(), TONES)
        for i, t in enumerate([m for m in members if m in weights]):
            color[t] = fam[i % len(fam)]
    for i, t in enumerate(order):
        color.setdefault(t, TONES[i % len(TONES)])
    return order, color

def _and_list(names) -> str:
    names = list(names)
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]

def _cluster_sentence(c) -> str:
    return f"{_and_list(c['members'])}: {float(c['avg_corr']):.2f} average correlation."

def _band_spans(index):
    """Regime windows as (label, i0, i1) index positions inside `index`."""
    out = []
    for label, start, end in REGIMES:
        lo = max(pd.Timestamp(start), index.min())
        hi = min(pd.Timestamp(end) if end else index.max(), index.max())
        i0, i1 = int(index.searchsorted(lo)), int(index.searchsorted(hi))
        if hi > lo and min(i1, len(index) - 1) > i0:
            out.append((label, i0, min(i1, len(index) - 1)))
    return out

# ---- section blocks shared by cards and the report -------------------------
def _block_alloc(weights, order, color, sleeves=None):
    """One stacked bar. Sleeve totals rule the top, holding labels sit beneath."""
    if not weights:
        return None
    total = sum(weights.values()) or 1.0
    groups = [(SLEEVE_WORDS.get(k.lower(), k.title()),
               [t for t in members if t in weights])
              for k, members in (sleeves or {}).items()]
    groups = [(name, ts) for name, ts in groups if ts]
    seen = {t for _, ts in groups for t in ts}
    rest = [t for t in order if t not in seen]
    if groups and rest:
        groups.append(("Other", rest))
    head = ""
    if groups:
        head = '<div class="wm-slv" data-align="sleeves">' + "".join(
            f'<div style="flex:{sum(weights[t] for t in ts) / total:.6f} 1 0">'
            f'<span>{_esc(name)}</span><b>{sum(weights[t] for t in ts) / total:.0%}</b>'
            f'</div>' for name, ts in groups) + "</div>"
    seg = "".join(
        f'<div class="wm-seg" style="flex:{weights[t] / total:.6f} 1 0;'
        f'background:var(--wm-{color[t]})" data-wm-k="{_esc(t)}" '
        f'data-wm-r="{_esc(t)}{DOT}{weights[t] / total:.1%} of your money"></div>'
        for t in order)
    tags = "".join(
        f'<li class="wm-tag" data-align="tag" data-wm-k="{_esc(t)}" '
        f'data-wm-r="{_esc(t)}{DOT}{weights[t] / total:.1%} of your money">'
        f'<span class="wm-sw" style="background:var(--wm-{color[t]})"></span>'
        f'{_esc(t)}<b>{weights[t] / total:.0%}</b></li>' for t in order)
    big = max(order, key=lambda t: weights[t])
    return ("layers", "What you own", "",
            f'<div class="wm-alloc">{head}'
            f'<div class="wm-strip" data-align="strip">{seg}</div>'
            f'<ul class="wm-tags">{tags}</ul></div>',
            f"largest {big} {weights[big] / total:.0%}", "")

def _block_growth(pr, rets, bench, bname, span=7):
    start = pd.Timestamp(rets.attrs.get('price_start', pr.index[0]-pd.offsets.BDay()))
    idx = [start,*pr.index]
    series = [{'label':'Portfolio','values':[100.,*list(100*(1+pr).cumprod())],'cls':'me'}]
    if bench in rets.columns:
        series.append({'label':bname.replace('the ',''),'values':[100.,*list(100*(1+rets[bench]).cumprod())],'cls':'bench'})
    end = DOT.join(f"{x['label']} {x['values'][-1]:,.0f}" for x in series)
    return ('line-chart','Growth of 100','',svg_line(idx,series,height=PLOT_H[span],ends=True,
            lab=AX_VB[span],blab=BD_VB[span]),end,'Hypothetical history; not a forecast.')

def _block_drawdown(pr, span=5):
    curve = (1+pr).cumprod()
    dd = curve/curve.cummax().clip(lower=1.)-1
    start = pd.Timestamp(pr.attrs.get('price_start',pr.index[0]-pd.offsets.BDay()))
    return ('arrow-down-to-line','Falls from peak','',
            svg_area([start,*dd.index],[0.,*dd],height=PLOT_H[span],lab=AX_VB[span]),
            f'worst {_signed(float(dd.min()))}{DOT}{_month(dd.idxmin())}',
            'Measured from initial wealth as well as later peaks.')

def _block_beta(pr, rets, bench, bname, span=6):
    if bench not in rets.columns or len(rets) <= TRADING_DAYS + 5:
        return None
    rb = _rolling_beta(pr, rets[bench])
    if len(rb) < 5:
        return None
    return ("waves", "Moves with the market", "",
            svg_line(list(rb.index),
                     [{"label": "beta", "values": list(rb), "cls": "me"}],
                     height=PLOT_H[span], yfmt=lambda v: f"{v:.1f}×", hline=1.0,
                     hlabel="1.0", lab=AX_VB[span], blab=BD_VB[span]),
            f"now {float(rb.iloc[-1]):.2f}×", "Rolling one year.")

def _block_heatmap(rets, holdings, clusters, span=7):
    if len(holdings) < 2:
        return None
    corr = rets[holdings].corr()
    z = [[float(corr.loc[a, b]) for b in holdings] for a in holdings]
    cap = _cluster_sentence(clusters[0]) if clusters else ""
    pairs = _pairs(holdings, z)
    ro = f"highest {pairs[0][1]} & {pairs[0][2]} {pairs[0][0]:.2f}" if pairs else ""
    if len(holdings) <= GRID_MAX:
        # two panes: the grid keeps its square cells, the space beside it ranks
        # the same pairs instead of sitting empty
        return ("grid-2x2", "Move together", cap,
                f'<div class="wm-panes"><div class="wm-hmw">'
                f'{svg_heatmap(holdings, z)}</div>'
                f'{html_pane(holdings, z, clusters)}</div>', ro, "")
    body, total = html_pairs(holdings, z)
    shown = min(24, total)
    foot = (f"The {shown} closest of {total} pairs." if shown < total else "")
    return ("grid-2x2", "Move together", cap,
            f'<div class="wm-hmw">{body}</div>', ro, foot)

def _block_diversification(rets, holdings, span=6):
    if len(holdings) < 2:
        return None
    rc = rolling_pair_corr(rets[holdings], 126)
    avg = rc.mean(axis=1).dropna() if len(rc.columns) else pd.Series(dtype=float)
    if len(avg) <= 5:
        return None
    bands = _band_spans(avg.index)
    strip = int(round(BD_VB[span] * 1.9)) if bands else 0
    return ("split", "Correlation over time", "",
            svg_line(list(avg.index),
                     [{"label": "together", "values": list(avg), "cls": "me"}],
                     height=PLOT_H[span] + strip, yfmt=lambda v: f"{v:.1f}",
                     bands=bands, lab=AX_VB[span], blab=BD_VB[span]),
            f"now {float(avg.iloc[-1]):.2f}",
            "Average pairwise correlation, six months.")

def _block_details(p, assets, order, bench, bname, w) -> str:
    """The spec table, always open, scrolling inside its own card."""
    pc = lambda v: _pct(v).replace("-", MINUS)
    nm = lambda v: _num(v).replace("-", MINUS)
    rows = []
    if p:
        rows.append("<tr class='wm-me'><td>Your portfolio</td><td>100.0%%</td>"
                    "<td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>" % (
                        pc(p.get("ann_return")), pc(p.get("ann_vol")),
                        pc(p.get("max_drawdown")), nm(p.get("beta"))))
    for t in order + ([bench] if bench in assets else []):
        a, wt = assets.get(t, {}), w.get(t)
        rows.append(("<tr class='wm-bench'>" if t == bench else "<tr>")
                    + "<td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td>"
                    "</tr>" % (_esc(bname.replace("the ", "") if t == bench else t),
                               "—" if wt is None else f"{wt:.1%}",
                               pc(a.get("ann_return")), pc(a.get("ann_vol")),
                               pc(a.get("max_drawdown")), nm(a.get("beta"))))
    table = ('<div class="wm-scroll"><table><thead>'
             "<tr><th>Holding</th><th>Share</th><th>Yearly return</th><th>Swings</th>"
             "<th>Worst fall</th><th>Moves with market</th></tr></thead><tbody>"
             + "".join(rows) + "</tbody></table></div>")
    return card_html("list", "Details", table,
                     f"{len(rows)} rows", "", SPAN["Details"], fit=False)

# ---- page assembly --------------------------------------------------------
def _css(fragment: bool) -> str:
    """Chat hosts need a transparent ground, tight margins and >= 11px type."""
    if not fragment:
        return WM_CSS
    return (WM_CSS.replace("--wm-paper:#f7f6f2", "--wm-paper:transparent")
                  .replace("--wm-paper:#111111", "--wm-paper:transparent")
                  .replace("padding:40px 24px 48px", "padding:2px 0 8px")
                  .replace("padding:32px 16px 40px", "padding:2px 0 8px"))

def wrap(inner: str, title: str, fragment: bool) -> str:
    # A synthetic-data label must be visible before any result, including reports.
    badge = re.search(r'<div class="wm-badge">.*?</div>',inner)
    if badge:
        inner=badge.group(0)+inner[:badge.start()]+inner[badge.end():]
    block = (f'<div class="wm"><!--{ICON_LICENSE_NOTICE}--><style>{_css(fragment)}</style>{inner}'
             f'<script>{WM_JS}</script></div>')
    if fragment:
        return block
    # Only the standalone document may fetch fonts; the fragment stays request-free.
    return ('<!doctype html><html lang="en"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            f"<title>{_esc(title)}</title>{FONT_LINK}"
            "<style>html,body{margin:0;padding:0;background:#f7f6f2}"
            "@media (prefers-color-scheme:dark){html,body{background:#111}}"
            "</style></head><body>" + block + "</body></html>")

# v2.2: the page is descriptive. Methodology, risk-free substitutions and fee
# coverage belong in the JSON and in --pretty, not in the reader's face. The one
# thing that must reach the page is that the picture is not of what was asked
# for: a requested holding is not in it, or a currency conversion failed.
_PAGE_WARN_KEEP = ('unavailable', 'missing', 'dropped', 'no price history',
                   'exchange rate', 'currency conversion', 'benchmark', 'fx ')
# Checked first: these are notes about method and provenance, never about what
# the picture contains, however they happen to be worded.
_PAGE_WARN_DROP = ('risk-free', 'expense', 'fee coverage', 'factor model',
                   'annualization', 'particularly unstable', 'multiple-testing',
                   'recomputed', 'local csv')

def _page_warnings(warnings) -> list:
    """Only the warnings that change what the page is a picture of."""
    out = []
    for w in warnings or []:
        low = str(w).lower()
        if any(k in low for k in _PAGE_WARN_DROP):
            continue
        if any(k in low for k in _PAGE_WARN_KEEP):
            out.append(str(w))
    return out[:1]   # one line, never a stack

def _mast(title, meta, warnings, lede_text="") -> str:
    shown = _page_warnings(warnings)
    warn = (f'<div class="wm-warn">{_esc(DOT.join(shown))}</div>' if shown else "")
    return ('<div class="wm-mast" data-align="masthead">'
            f'<h1 data-align="mast-title">{_esc(title)}</h1>'
            f'<p class="wm-meta" data-align="mast-meta">{_esc(meta)}</p>'
            + (f'<p class="wm-lede">{_esc(lede_text)}</p>' if lede_text else "")
            + warn + "</div>")

def grid(*parts) -> str:
    return '<div class="wm-grid" data-align="grid">' + "".join(parts) + "</div>"

FOOTER = ""

def _meta(res: dict, bname: str, extra=(), hint=True) -> str:
    """Masthead meta: "Sep 2021 - Sep 2026 - vs S&P 500 - MXN". Months, not ISO."""
    bits = []
    if res.get('window_start'):
        start = (res.get('provenance') or {}).get('price_start') or res['window_start']
        bits.append(f"{_month(start)} \u2013 {_month(res['window_end'])}")
    if res.get('bench') and bname:
        bits.append(f"vs {bname.replace('the ','')}")
    bits.extend(x for x in extra if x and x not in bits)
    if res.get('currency') and res['currency'] not in bits:
        bits.append(res['currency'])
    return DOT.join(bits)

def _prep(px, weights, bench, rebalance: str = 'annual'):
    rets = daily_returns(px)
    w = normalized_weights(weights, rets.columns).to_dict() if weights else {}
    pr = portfolio_returns(rets,w,rebalance) if w else None
    holdings = list(w) if w else ([t for t in rets.columns if t != bench] or list(rets.columns))
    return rets,w,pr,holdings

# ---- cards ----------------------------------------------------------------
# A card is one screen, so every section in it takes the full twelve columns.
CARD_SPAN = {k: 12 for k in SPAN}

def _validate_snapshot(res, px):
    expected = (res.get('provenance') or {}).get('price_sha256')
    if expected and expected != price_fingerprint(px):
        raise ValueError('statistics and chart prices are different snapshots; recompute before rendering')


def _evidence(res):
    p = res.get('provenance') or {}
    kind = p.get('data_kind','unspecified')
    badge = '<div class="wm-badge">Synthetic demonstration. Not market data.</div>' if kind=='synthetic' else ''
    source = p.get('source','Source not supplied')
    items = [source, f"Reporting currency: {res.get('currency') or 'not declared'}.",
             f"Rebalancing: {res.get('rebalance','not supplied')}. Daily-close observations; intraday losses are not measured.",
             'Hypothetical holdings chosen today. No taxes, trading costs or investor cash flows are modeled. Past performance is not a forecast.']
    if p.get('price_start'):
        items.append(f"Prices: {p['price_start']} to {p['price_end']}. All holdings use the same sample.")
    items.extend(res.get('warnings') or [])
    return badge+'<details class="wm-evidence"><summary>Sources and assumptions</summary>'+''.join(f'<p>{_esc(t)}</p>' for t in items)+'</details>'


def _accessible_table(headers, rows, caption):
    head=''.join(f'<th scope="col">{_esc(h)}</th>' for h in headers)
    body=''.join('<tr>'+''.join(f'<td>{_esc(c)}</td>' for c in row)+'</tr>' for row in rows)
    return f'<div class="wm-scroll"><table><caption>{_esc(caption)}</caption><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>'

def card_analyze(res: dict, px: pd.DataFrame, weights=None, growth=False,
                 portfolio=None, fragment=False) -> str:
    _validate_snapshot(res,px)
    bench = res.get('bench') or 'SPY'
    bname = BENCH_NAMES.get(bench,bench)
    p = res.get('portfolio') or {}
    rets,w,pr,holdings = _prep(px,p.get('weights') or weights,bench,res.get('rebalance') or 'annual')
    sleeves = (portfolio or {}).get('sleeves')
    order,color = _order_colors(w,sleeves)
    title = _headline(portfolio) if growth and portfolio else ('Portfolio history' if growth else 'How holdings move together')
    mast = _mast(title,_meta(res,bname),res.get('warnings'))
    secs = Cards(CARD_SPAN)
    if growth and pr is not None:
        secs.add(_block_alloc(w,order,color,sleeves))
        secs.add(_block_growth(pr,rets,bench,bname,12))
    else:
        secs.add(_block_heatmap(rets,holdings,res.get('clusters'),12))
    detail = ''
    if p:
        detail = '<details><summary>Allocation and historical metrics</summary>'+grid(_keys(p,{},bname))+_accessible_table(['Holding','Weight'],[[k,f'{v:.1%}'] for k,v in w.items()],'Target allocation')+'</details>'
    pairs = res.get('pair_diagnostics') or []
    if pairs:
        detail += '<details><summary>Correlation values and cross-checks</summary>'+_accessible_table(
            ['Pair','Pearson','Spearman','Same-sign observations'],
            [[" / ".join(x['pair']),_num(x['pearson']),_num(x['spearman']),f"{_pct(x['same_sign_fraction'])} of {x['same_sign_observations']}"] for x in pairs],
            'Same-sign frequency is measured separately; it is not the correlation coefficient.')+'</details>'
    return wrap(mast+grid(secs.html())+detail+_evidence(res),title,fragment)

def card_factors(res: dict, fragment=False) -> str:
    rows = [[t,FACTOR_WORDS.get(f,f),_num(v['beta']),_num(v['t']),str(v.get('ci95'))]
            for t,a in res.get('assets',{}).items() for f,v in a.get('loadings',{}).items()]
    mast = _mast('Historical factor exposure',_meta(res,''),res.get('warnings'))
    body = _accessible_table(['Asset','Factor','Loading','HAC t','95% interval'],rows,'US Fama-French regression; these estimates do not establish cause or investment skill.')
    return wrap(mast+grid(card_html('activity','Factor estimates',body)),'Historical factor exposure',fragment)

def card_regimes(res: dict, px: pd.DataFrame, weights=None, fragment=False) -> str:
    bench = res.get("bench") or "SPY"
    bname = BENCH_NAMES.get(bench, bench)
    short = bname.replace("the ", "")
    rets, w, pr, holdings = _prep(px, weights, bench,
                                  res.get("rebalance") or "annual")
    groups = []
    for r in res.get("regimes", []):
        bars = [{"label": lab, "value": leg["total_return"], "cls": cls}
                for lab, cls, leg in (("Yours", "me", r.get("portfolio")),
                                      (short, "bench", (r.get("assets") or {}).get(bench)))
                if leg]
        if bars:
            groups.append({"label": REGIME_SHORT.get(r["regime"], r["regime"]),
                           "bars": bars})
    mast = _mast(_name_of(rets, bench), _meta(res, bname), res.get("warnings"))
    secs = Cards(CARD_SPAN)
    secs.add(("line-chart", "Return by period", "",
              svg_bars(groups, row_h=26), "", f"Total return by period, next to {bname}."))
    secs.add(_block_diversification(rets, holdings, 12))
    return wrap(mast + grid(secs.html()), "Return by period", fragment)

def card_compare(result: dict, px: pd.DataFrame, fragment=False) -> str:
    a,b = result['current'],result['proposed']
    _validate_snapshot(a,px);_validate_snapshot(b,px)
    rets = daily_returns(px)
    pa,pb = a['portfolio'],b['portfolio']
    ra = portfolio_returns(rets,pa['weights'],a['rebalance'])
    rb = portfolio_returns(rets,pb['weights'],b['rebalance'])
    idx = [px.index[0],*rets.index]
    ga,gb = [1.,*list((1+ra).cumprod())],[1.,*list((1+rb).cumprod())]
    da=np.array(ga)/np.maximum.accumulate(ga)-1;db=np.array(gb)/np.maximum.accumulate(gb)-1
    growth = svg_line(idx,[{'label':'Current','values':list(100*np.array(ga)),'cls':'bench'},
                          {'label':'Proposed','values':list(100*np.array(gb)),'cls':'me'}],height=290,ends=True)
    decline = svg_line(idx,[{'label':'Current','values':list(da),'cls':'bench'},
                           {'label':'Proposed','values':list(db),'cls':'me'}],height=290,ends=True,
                       yfmt=lambda v:f'{100*v:.0f}%')
    buttons = '<div class="wm-toggle" role="group" aria-label="Chart measure"><button type="button" data-wm-toggle="growth" aria-pressed="true">Growth of 100</button><button type="button" data-wm-toggle="decline" aria-pressed="false">Falls from peak</button></div>'
    panels = f'<div data-wm-view="growth">{growth}</div><div data-wm-view="decline" hidden>{decline}</div><output class="wm-live" aria-live="polite"></output>'
    summary = ('<div class="wm-summary">'+''.join(f'<div><span>{name}: largest decline</span><strong>{_signed(p["max_drawdown"])}</strong></div>'
               for name,p in [('Current',pa),('Proposed',pb)])+'</div>')
    body = card_html('line-chart','Historical comparison',buttons+summary+panels,span=12,cls='wm-compare')
    holdings=sorted(set(pa['weights'])|set(pb['weights']))
    table = _accessible_table(['Holding','Current','Proposed'],[[k,_pct(pa['weights'].get(k,0)),_pct(pb['weights'].get(k,0))] for k in holdings],'Target weights')
    monthly = pd.DataFrame({'Current':100*np.array(ga),'Proposed':100*np.array(gb)},index=idx).resample('ME').last()
    values = _accessible_table(['Month','Current','Proposed'],[[str(d.date()),f'{r.Current:.2f}',f'{r.Proposed:.2f}'] for d,r in monthly.iterrows()],
                              'Month-end sampled values; the last month can be partial.')
    mast=_mast('Compare allocations',_meta(a,''),None)
    # This chart compares the two portfolios, not the reference benchmark.
    return wrap(mast+grid(body)+'<details><summary>What changes</summary>'+table+'</details>'+
                '<details><summary>Chart values</summary>'+values+'</details>'+_evidence(a),'Compare allocations',fragment)

def card_build(portfolio: dict, px: pd.DataFrame, fragment=False) -> str:
    return card_analyze(portfolio.get("stats") or {}, px, portfolio.get("weights"),
                        growth=True, portfolio=portfolio, fragment=fragment)

# ---- the full report ------------------------------------------------------
def write_report(portfolio: dict, px: pd.DataFrame, out_path: Path,
                 fragment: bool = False) -> Path:
    bench = portfolio.get("bench") or "SPY"
    bname = BENCH_NAMES.get(bench, bench)
    stats = portfolio.get("stats") or {}
    _validate_snapshot(stats,px)
    if stats.get('portfolio'):
        expected = normalized_weights(portfolio.get('weights'),px.columns)
        observed = normalized_weights(stats['portfolio']['weights'],px.columns)
        if set(expected.index)!=set(observed.index) or not np.allclose(expected.sort_index(),observed.sort_index(),rtol=0,atol=1e-14):
            raise ValueError('report weights do not match saved statistics')
        if portfolio.get('rebalance',stats.get('rebalance')) != stats.get('rebalance'):
            raise ValueError('report rebalancing does not match saved statistics')
    if not (stats.get('provenance') or {}).get('price_sha256'):
        stats = analyze_frame(px,bench,portfolio.get('weights'),meta=portfolio.get('metadata'),rebalance=portfolio.get('rebalance','annual'))
    rets, w, pr, holdings = _prep(px, portfolio.get("weights"), bench,
                                  stats.get("rebalance") or "annual")
    order, color = _order_colors(w, portfolio.get("sleeves"))
    holdings = [t for t in order if t in rets.columns] or holdings
    p = stats.get("portfolio") or {}
    assets = stats.get("assets", {})
    b = assets.get(bench, {})

    warns = list(stats.get("warnings") or []) + list(portfolio.get("warnings") or [])
    mast = _mast(_headline(portfolio),
                 _meta(stats, bname, [portfolio.get("currency")]),
                 warns, lede(p, b, bname))
    keys = _keys(p, b, bname, stats.get("clusters")) if p else ""

    secs = Cards()
    secs.add(_block_alloc(w, order, color, portfolio.get("sleeves")))
    if pr is not None:
        secs.add(_block_growth(pr, rets, bench, bname, SPAN["Growth of 100"]))
        secs.add(_block_drawdown(pr, SPAN["Falls from peak"]))
        secs.add(_block_beta(pr, rets, bench, bname, SPAN["Moves with the market"]))
    # the grid rows of the spec, in order: the two six-column charts pair up, then
    # the heatmap pairs with the spec table
    secs.add(_block_diversification(rets, holdings, SPAN["Correlation over time"]))
    secs.add(_block_heatmap(rets, holdings, stats.get("clusters"),
                            SPAN["Move together"]))
    details = _block_details(p, assets, order, bench, bname, w)

    body = mast + grid(keys, secs.html(), details) + _evidence(stats) + FOOTER
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(wrap(body, _headline(portfolio), fragment), encoding="utf-8")
    return out_path

def _write_card(html: str, path) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(html, encoding="utf-8")

# --------------------------------------------------------------------------
# pretty printing
# --------------------------------------------------------------------------
def _pct(x, nd=1):
    return "—" if x is None else f"{100 * float(x):.{nd}f}%"

def _num(x, nd=2):
    return "—" if x is None else f"{float(x):.{nd}f}"

def _table(headers, rows) -> str:
    widths = [max(len(str(h)), *(len(str(r[i])) for r in rows)) if rows else len(str(h))
              for i, h in enumerate(headers)]
    def line(cells):
        return "  ".join(str(c).ljust(widths[i]) if i == 0 else str(c).rjust(widths[i])
                         for i, c in enumerate(cells))
    return "\n".join([line(headers), "  ".join("-" * w for w in widths)] +
                     [line(r) for r in rows])

def print_analyze(res: dict) -> None:
    print(f"window {res['window_start']} → {res['window_end']}  "
          f"({res['n_days']} days, bench {res['bench']}, rf {_pct(res['rf_annual'])})")
    rows = [[t, _pct(a["ann_return"]), _pct(a["ann_vol"]), _pct(a["max_drawdown"]),
             _num(a["sharpe"]), _num(a["beta"]), _pct(a["alpha"]), _num(a["r2"]),
             _pct(a.get("expense_ratio"), 2)]
            for t, a in res["assets"].items()]
    print()
    print(_table(["asset", "ret", "vol", "maxDD", "sharpe", "beta", "alpha", "R2", "fee"],
                 rows))
    corr = res["correlation"]
    names = list(corr)
    print("\ncorrelation")
    print(_table([""] + names,
                 [[a] + [_num(corr[a][b]) for b in names] for a in names]))
    if res["clusters"]:
        print("\nconnected correlation groups (threshold > 0.7; not independent bets):")
        for c in res["clusters"]:
            w = ("" if c["weight"] is None
                 else f" That is {100 * c['weight']:.0f}% of the book.")
            print(f"  {_cluster_sentence(c)}{w}")
    p = res.get("portfolio")
    if p:
        print("\nportfolio")
        print(_table(["weights"], [[f"{k} {100 * v:.1f}%"] for k, v in p["weights"].items()]))
        print(f"  ret {_pct(p['ann_return'])}  vol {_pct(p['ann_vol'])}  "
              f"maxDD {_pct(p['max_drawdown'])}  sharpe {_num(p['sharpe'])}  "
              f"beta {_num(p['beta'])}")
        print(f"  covariance participation ratio {_num(p.get('effective_bets_corr'))} "
              f"(weights only {_num(p['effective_bets'])}) of {p['n_holdings']}  "
              f"first PC {_pct(p['pca_first_pc_share'], 0)} of variance  "
              f"blended fee {_pct(p.get('expense_ratio'), 2)}")
    for w in res.get("warnings", []):
        print(f"! {w}")

def print_regimes(res: dict) -> None:
    print(f"window {res['window_start']} → {res['window_end']}  "
          f"({res['n_days']} days, bench {res['bench']}, "
          f"corr window {res['corr_window']}d)")
    for reg in res["regimes"]:
        print(f"\n{reg['regime']}  [{reg['start']} → {reg['end']}, {reg['n_days']}d]")
        rows = []
        if reg["portfolio"]:
            p = reg["portfolio"]
            rows.append(["PORTFOLIO", _pct(p["total_return"]), _pct(p["ann_vol"]),
                         _pct(p["max_drawdown"]), _num(p["beta"])])
        for t, a in reg["assets"].items():
            rows.append([t, _pct(a["total_return"]), _pct(a["ann_vol"]),
                         _pct(a["max_drawdown"]), _num(a["beta"])])
        print(_table(["asset", "total", "vol", "maxDD", "beta"], rows))
    ap = res["avg_pairwise_corr"]
    print(f"\navg pairwise correlation: now {_num(ap['current'])}  "
          f"mean {_num(ap['mean'])}  range {_num(ap['min'])}–{_num(ap['max'])}")
    print("\nlargest rolling-correlation changes (not structural break tests)")
    print(_table(["pair", "now", "min", "max", "change date", "Δ"],
                 [["/".join(b["pair"]), _num(b["current"]), _num(b["min"]),
                   _num(b["max"]), b["broke_on"] or "—", _num(b["broke_change"])]
                  for b in res["correlation_breaks"]]))
    if res["holding_periods"]:
        print("\ntrailing holding periods")
        rows = []
        for h in res["holding_periods"]:
            p, b = h["portfolio"] or {}, h["bench"] or {}
            rows.append([h["period"], _pct(p.get("total_return")),
                         _pct(p.get("max_drawdown")), _pct(b.get("total_return")),
                         _pct(b.get("max_drawdown"))])
        print(_table(["period", "port ret", "port maxDD", "bench ret", "bench maxDD"],
                     rows))
    for w in res.get("warnings", []):
        print(f"! {w}")

def print_factors(res: dict) -> None:
    print(f"FF{res['model']} daily  {res.get('window_start')} → {res.get('window_end')}"
          f"  ({res.get('n_days')} days)")
    facs = res.get("factors", [])
    rows = []
    for t, a in res["assets"].items():
        row = [t, _pct(a["alpha_annual"]), _num(a["alpha_t"])]
        for f in facs:
            row.append(f"{_num(a['loadings'][f]['beta'])} ({_num(a['loadings'][f]['t'],1)})")
        row.append(_num(a["r2"]))
        rows.append(row)
    print()
    print(_table(["asset", "alpha", "t", *facs, "R2"], rows))
    print("\ntilts (|t| > 2):")
    for t, a in res["assets"].items():
        tl = ", ".join(f"{k} {v:+.2f}" for k, v in a["tilts"].items()) or "none"
        print(f"  {t}: {tl}")
    for w in res.get("warnings", []):
        print(f"! {w}")

# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------
def _nonnegative(value, name):
    if isinstance(value, bool):
        raise ValueError(f'{name} must be a number, not a boolean')
    v = float(value)
    if not np.isfinite(v) or v < 0:
        raise ValueError(f'{name} must be finite and nonnegative')
    return v


def money_plan(profile: dict) -> dict:
    """Arithmetic reservation of one declared pool; NOT a suitability classifier.

    All outside funding must be disjoint. Protected goals are a user/design
    decision, not inferred here from a universal horizon rule.
    """
    required = ['currency','as_of','available_capital','monthly_essentials',
                'reserve_months','reserve_outside_pool','goals','debts',
                'debt_payments_from_pool']
    for name in ('goals','debts'):
        value = profile.get(name)
        if value is not None and (not isinstance(value,list) or not all(isinstance(x,dict) for x in value)):
            raise ValueError('goals and debts must be lists of objects')
    missing = [k for k in required if profile.get(k) is None]
    ccy = profile.get('currency')
    if ccy is not None and not re.fullmatch(r'[A-Z]{3}',ccy):
        raise ValueError('currency must be a three-letter uppercase code')
    as_of = date.fromisoformat(profile['as_of']) if profile.get('as_of') else None
    if as_of and as_of > date.today():
        raise ValueError('profile as_of cannot be in the future')
    nums = {k:_nonnegative(profile[k],k) for k in required
            if k not in ('currency','as_of','goals','debts') and profile.get(k) is not None}
    reserve_target = (nums['monthly_essentials']*nums['reserve_months']
                      if all(k in nums for k in ('monthly_essentials','reserve_months')) else None)
    reserve_from_pool = max(0.,reserve_target-nums['reserve_outside_pool']) if reserve_target is not None and 'reserve_outside_pool' in nums else None
    goals, protected = [], 0.
    for i,g in enumerate(profile.get('goals') or []):
        need = ['name','currency','due','protect_now','target_amount','funded_outside_pool']
        gm = [k for k in need if g.get(k) is None]
        missing.extend(f'goals[{i}].{k}' for k in gm)
        if gm:
            continue
        date.fromisoformat(g['due'])
        if not isinstance(g['protect_now'],bool):
            raise ValueError('goal protect_now must be true or false')
        target = _nonnegative(g['target_amount'],'goal target')
        funded = _nonnegative(g['funded_outside_pool'],'goal outside funding')
        if funded > target:
            raise ValueError('outside goal funding exceeds its target; identify the surplus separately')
        need_now = target-funded if g['protect_now'] else 0.
        if g['currency'] != ccy and g['protect_now']:
            missing.append(f'goals[{i}].explicit_conversion_to_pool_currency')
        else:
            protected += need_now
        goals.append({'name':g['name'],'currency':g['currency'],'due':g['due'],
                      'protected_from_pool':need_now if g['currency']==ccy else None,
                      'protect_now':g['protect_now']})
    debts = []
    for i,d in enumerate(profile.get('debts') or []):
        dm = [k for k in ('name','currency','balance','apr') if d.get(k) is None]
        missing.extend(f'debts[{i}].{k}' for k in dm)
        if dm:
            continue
        bal,apr = _nonnegative(d['balance'],'debt balance'),_nonnegative(d['apr'],'debt APR')
        debts.append({'name':d['name'],'currency':d['currency'],'balance':bal,'apr':apr,
                      'annual_interest_at_unchanged_balance':bal*apr})
    if not isinstance(profile.get('goals',[]),list) or not isinstance(profile.get('debts',[]),list):
        raise ValueError('goals and debts must be lists; an explicit empty list means none')
    if 'debt_payments_from_pool' in nums and profile.get('debts') is not None:
        complete_same_currency = all(d.get('currency')==ccy and d.get('balance') is not None for d in profile['debts'])
        if complete_same_currency and nums['debt_payments_from_pool']>sum(d['balance'] for d in debts)+1e-8:
            raise ValueError('debt payment exceeds the declared same-currency balances')
    remaining = None
    if not missing:
        remaining = nums['available_capital']-reserve_from_pool-protected-nums['debt_payments_from_pool']
    notes = ['not an investment approval; risk capacity, income stability and household exposures still require judgment',
             'outside reserve and goal funding must be disjoint and excluded from available_capital',
             'debt interest is a simple unchanged-balance illustration, not a payoff schedule']
    if as_of and (date.today()-as_of).days > 90:
        notes.append('profile is over 90 days old; reconfirm material facts before a proposal')
    return {'currency':ccy,'as_of':profile.get('as_of'),'missing':missing,
            'available_capital':nums.get('available_capital'),'reserve_target':reserve_target,
            'reserve_from_pool':reserve_from_pool,'protected_goals_from_pool':protected if not any(k.startswith('goals') for k in missing) else None,
            'debt_payments_from_pool':nums.get('debt_payments_from_pool'),
            'uncommitted_capital':remaining,
            'funding_shortfall':max(0.,-remaining) if remaining is not None else None,
            'goals':goals,'debts':debts,'notes':notes}


def review_weights(values: dict, target: dict, threshold_pp: float, currency: str, as_of: str) -> dict:
    """Current-value drift, not orders or an optimal rebalancing rule."""
    if not re.fullmatch(r'[A-Z]{3}', currency):
        raise ValueError('review requires the common valuation currency')
    when = date.fromisoformat(as_of)
    if when > date.today():
        raise ValueError('valuation date cannot be in the future')
    threshold = _nonnegative(threshold_pp,'threshold_pp')
    if threshold <= 0 or threshold > 100:
        raise ValueError('threshold_pp must be greater than zero and at most 100')
    names = sorted(set(values)|set(target))
    now = normalized_weights(values,names)
    tgt = normalized_weights(target,names)
    rows = [{'asset':n,'current_weight':float(now.get(n,0)), 'target_weight':float(tgt.get(n,0)),
             'drift_pp':100*float(now.get(n,0)-tgt.get(n,0)),
             'outside_agreed_band':bool(abs(100*float(now.get(n,0)-tgt.get(n,0))) > threshold+1e-10)}
            for n in names]
    return {'currency':currency,'as_of':as_of,'threshold_pp':threshold,'holdings':rows,
            'review_needed':any(r['outside_agreed_band'] for r in rows),
            'note':'user-specified band; review costs, taxes, new contributions and changed goals before acting'}


def fee_scenario(initial: float, gross_return: float, annual_fees: list, years: int) -> dict:
    principal = _nonnegative(initial,'initial')
    if not np.isfinite(gross_return) or gross_return <= -1 or not isinstance(years,int) or not 0 <= years <= 100:
        raise ValueError('invalid gross-return assumption or years')
    rows = []
    for fee in annual_fees:
        fee = _nonnegative(fee,'annual fee')
        if fee >= 1:
            raise ValueError('fee must be a decimal below one')
        end = principal*((1+gross_return)*(1-fee))**years
        no_fee = principal*(1+gross_return)**years
        rows.append({'annual_fee':fee,'ending_value':end,'difference_from_no_fee':no_fee-end})
    return {'initial':principal,'gross_return_assumption':gross_return,'years':years,'scenarios':rows,
            'method':'annual growth followed by annual proportional fee; no cash flows, taxes or inflation',
            'note':'hypothetical gross returns, not a forecast; do not apply fees again to net fund returns'}


def compare_portfolios(px: pd.DataFrame, current: dict, proposed: dict, bench: str,
                       rebalance='annual', meta=None) -> dict:
    a = analyze_frame(px,bench,current,meta=meta,rebalance=rebalance)
    b = analyze_frame(px,bench,proposed,meta=meta,rebalance=rebalance)
    return {'current':a,'proposed':b,'same_price_sha256':a['provenance']['price_sha256'],
            'difference':{k:_round(b['portfolio'][k]-a['portfolio'][k],6)
                          if a['portfolio'][k] is not None and b['portfolio'][k] is not None else None
                          for k in ('ann_return','ann_vol','max_drawdown','expense_ratio')},
            'interpretation':'hypothetical comparison on the same sample, currency and rebalance convention; not an out-of-sample strategy test'}


def _local_metadata(args) -> dict:
    path = getattr(args,'metadata_file',None)
    if not path and getattr(args,'price_file',None):
        candidate = Path(args.price_file).with_suffix('.meta.json')
        path = candidate if candidate.exists() else None
    return json.loads(Path(path).read_text(encoding='utf-8')) if path else {}


def _cli_meta(args, columns):
    if getattr(args,'price_file',None):
        return _local_metadata(args).get('assets',{})
    return _ticker_meta(columns)


def _cli_prices(args, tickers=None):
    tickers = list(tickers or args.tickers)
    if len(set(tickers)) != len(tickers):
        raise ValueError('duplicate tickers are not allowed')
    if getattr(args,'price_file',None):
        if not args.currency:
            raise ValueError('--price-file requires --currency for the already-converted CSV')
        md = _local_metadata(args)
        if md.get('currency') and md['currency'] != args.currency:
            raise ValueError('CSV metadata currency does not match --currency; no automatic local conversion')
        with open(args.price_file,newline='',encoding='utf-8-sig') as handle:
            header = next(csv.reader(handle),[])
        if len(header)<2 or len(set(header[1:]))!=len(header[1:]):
            raise ValueError('duplicate or missing CSV asset columns')
        px = pd.read_csv(args.price_file,index_col=0,parse_dates=True)
        if list(px.columns) != list(dict.fromkeys(px.columns)):
            raise ValueError('duplicate CSV columns')
        missing = set(tickers)-set(px.columns)
        if missing:
            raise ValueError('CSV is missing requested assets: '+', '.join(sorted(missing)))
        px = px[tickers]
        daily_returns(px)  # validate before slicing; never silently fill
        if args.years <= 0:
            raise ValueError('years must be positive')
        px = px.loc[px.index >= px.index.max()-pd.Timedelta(days=365.25*args.years)]
        px.attrs.update(currency=args.currency,source=md.get('source','local CSV; adjustment status unverified'),
                        data_kind=md.get('data_kind','user-supplied'),retrieved=md.get('retrieved'),
                        risk_free_policy='omit',first_dates={t:str(px.index.min().date()) for t in px.columns})
        return px, ['local CSV is treated as already in the declared reporting currency']
    return _load_prices(tickers,args.years,currency=args.currency)


def _snapshot(px):
    return {'dates':[str(x.date()) for x in px.index], 'columns':list(px.columns),
            'values':px.to_numpy(float).tolist(), 'attrs':dict(px.attrs)}


def _from_snapshot(snap):
    px = pd.DataFrame(snap['values'],columns=snap['columns'],index=pd.to_datetime(snap['dates']))
    px.attrs.update(snap.get('attrs',{}))
    daily_returns(px)
    return px


def cmd_plan(args):
    print(json.dumps(money_plan(json.loads(Path(args.profile).read_text())),allow_nan=False))


def cmd_review(args):
    d = json.loads(Path(args.profile).read_text())
    print(json.dumps(review_weights(d['values'],d['target'],d['threshold_pp'],d['currency'],d['as_of']),allow_nan=False))


def cmd_fees(args):
    print(json.dumps(fee_scenario(args.initial,args.gross_return,args.fees,args.years),allow_nan=False))


def cmd_compare(args):
    d = json.loads(Path(args.portfolio).read_text())
    names = list(dict.fromkeys([*d['current_weights'],*d['proposed_weights'],args.bench]))
    px,warnings = _cli_prices(args,names)
    result = compare_portfolios(px,d['current_weights'],d['proposed_weights'],args.bench,args.rebalance,_cli_meta(args,list(px.columns)))
    result['warnings'] = warnings
    if args.card:
        _write_card(card_compare(result,px,fragment=args.fragment),args.card)
    print(json.dumps(result,allow_nan=False))

def _json_arg(raw):
    return json.loads(raw) if raw else None

def cmd_prices(args):
    px, warnings = _cli_prices(args)
    if args.pretty:
        print(f"{len(px.columns)} tickers, {len(px)} common days")
        print(px.tail(5).round(2).to_string())
        for w in warnings:
            print(f"! {w}")
        return
    print(json.dumps({
        "window": _window(px.index) if len(px) else None,
        "window_start": str(px.index.min().date()) if len(px) else None,
        "window_end": str(px.index.max().date()) if len(px) else None,
        "n_days": int(len(px)), "tickers": list(px.columns),
        "currency": px.attrs.get('currency'), "source": px.attrs.get('source'),
        "prices": {t: {str(d.date()): _round(v, 6) for d, v in px[t].items()}
                   for t in px.columns},
        "warnings": warnings}))

def _weights_arg(args, tickers):
    if len(set(tickers)) != len(tickers):
        raise ValueError('duplicate tickers are not allowed')
    if not args.weights:
        return None
    vals = [float(x) for x in args.weights]
    if len(vals) != len(tickers):
        raise ValueError('--weights must have one value per ticker')
    return normalized_weights(dict(zip(tickers,vals)),tickers).to_dict()

def cmd_analyze(args):
    tickers = [t.upper() for t in args.tickers]
    universe = tickers + ([args.bench] if args.bench not in tickers else [])
    px, warnings = _cli_prices(args, universe)
    if px.empty:
        raise SystemExit(json.dumps({"error": "no price data", "warnings": warnings}))
    res = analyze_frame(px, args.bench, _weights_arg(args, tickers), warnings,
                        _cli_meta(args, list(px.columns)), args.rebalance)
    if args.card:
        _write_card(card_analyze(res, px, _weights_arg(args, tickers),
                                 fragment=args.fragment), args.card)
    print_analyze(res) if args.pretty else print(json.dumps(res))

def cmd_regimes(args):
    tickers = [t.upper() for t in args.tickers]
    universe = tickers + ([args.bench] if args.bench not in tickers else [])
    px, warnings = _cli_prices(args, universe)
    if px.empty:
        raise SystemExit(json.dumps({"error": "no price data", "warnings": warnings}))
    res = regimes_frame(px, args.bench, _weights_arg(args, tickers), args.window,
                        warnings, args.rebalance)
    if args.card:
        _write_card(card_regimes(res, px, _weights_arg(args, tickers),
                                 fragment=args.fragment), args.card)
    print_regimes(res) if args.pretty else print(json.dumps(res))

def cmd_factors(args):
    px, warnings = _cli_prices(args, [t.upper() for t in args.tickers])
    if px.empty:
        raise SystemExit(json.dumps({"error": "no price data", "warnings": warnings}))
    res = factor_regression(px, args.model, warnings)
    if args.card:
        _write_card(card_factors(res, fragment=args.fragment), args.card)
    print_factors(res) if args.pretty else print(json.dumps(res))

def cmd_build(args):
    tickers = [t.upper() for t in args.tickers]
    sleeves = _json_arg(args.sleeves)
    if sleeves:
        sleeves = {k: [t.upper() for t in v] for k, v in sleeves.items()}
        tickers = sorted({t for v in sleeves.values() for t in v} | set(tickers))
    universe = tickers + ([args.bench] if args.bench not in tickers else [])
    px, warnings = _cli_prices(args, universe)
    if px.empty:
        raise SystemExit(json.dumps({"error": "no price data", "warnings": warnings}))
    if set(tickers)-set(px.columns):
        raise ValueError('construction assets missing from price history')
    held = [t for t in px.columns if t in tickers]
    rets = daily_returns(px[held])
    w = build_weights(rets, args.method, args.max_weight, sleeves,
                      _json_arg(args.sleeve_weights))
    stats = analyze_frame(px, args.bench, w.to_dict(), warnings,
                          _cli_meta(args, list(px.columns)), args.rebalance)
    out = {"name": args.name or ("+".join(held[:4]) + ("…" if len(held) > 4 else "")),
           "created": date.today().isoformat(), "bench": args.bench,
           "years": args.years, "method": args.method, "max_weight": args.max_weight,
           "rebalance": args.rebalance, "currency": px.attrs.get("currency"),
           "fx_converted": px.attrs.get("fx_converted", []),
           "weights": {k: float(v) for k, v in w.items()},
           "price_snapshot": _snapshot(px), "metadata": _cli_meta(args, list(px.columns)),
           "sleeves": sleeves, "sleeve_weights": _json_arg(args.sleeve_weights),
           "stats": stats, "warnings": warnings}
    if args.save:
        p = Path(args.save)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(out, indent=2), encoding="utf-8")
    if args.card:
        _write_card(card_build(out, px, fragment=args.fragment), args.card)
    if args.pretty:
        print(f"{args.method}  max weight {args.max_weight:.0%}")
        print(_table(["asset", "weight"],
                     [[k, f"{100 * v:.1f}%"] for k, v in w.items()]))
        print()
        print_analyze(stats)
        if args.save:
            print(f"\nsaved {args.save}")
    else:
        print(json.dumps(out))

def cmd_holdings(args):
    res = fund_holdings(args.query, args.top,
                        weights_from_suggestions=args.weights_from_suggestions)
    if args.pretty:
        print(f"{res['fund']}  (CIK {res['cik']})  13F for {res['period']}, "
              f"filed {res['filed']}  ·  {res['n_positions']} positions")
        print(_table(["asset", "ticker", "weight"],
                     [[h["name"][:34], h["ticker"] or "—", f"{100 * h['weight']:.1f}%"]
                      for h in res["holdings"]]))
        if res.get("weights"):
            print(f"\nInvestable weights from unverified name matches "
                  f"(coverage {100 * float(res.get('coverage') or 0):.0f}% of shown value):")
            print(_table(["ticker", "weight"],
                         [[t, f"{100 * w:.1f}%"] for t, w in
                          sorted(res["weights"].items(), key=lambda kv: -kv[1])]))
        for w in res["warnings"]:
            print(f"! {w}")
        return
    print(json.dumps(res))

def cmd_report(args):
    portfolio = json.loads(Path(args.portfolio).read_text(encoding='utf-8'))
    bench = portfolio.get('bench') or 'SPY'
    rebalance = portfolio.get('rebalance') or 'annual'
    warnings = []
    if portfolio.get('price_snapshot') and not args.refresh:
        px = _from_snapshot(portfolio['price_snapshot'])
        meta = portfolio.get('metadata',{})
    else:
        args.currency = portfolio.get('currency')
        args.years = int(portfolio.get('years') or 5)
        tickers = list(dict.fromkeys([*portfolio['weights'],bench]))
        px,warnings = _cli_prices(args,tickers)
        meta = _cli_meta(args,list(px.columns))
        warnings.append('report refreshed from a new price sample; all statistics recomputed')
    portfolio['stats'] = analyze_frame(px,bench,portfolio['weights'],warnings,meta,rebalance)
    out = Path(args.out or Path(args.portfolio).with_suffix('.html'))
    path = write_report(portfolio,px,out,fragment=args.fragment)
    print(json.dumps({'out':str(path),'warnings':warnings,
                      'price_sha256':portfolio['stats']['provenance']['price_sha256']}))

def main(argv=None):
    ap = argparse.ArgumentParser(prog="wm.py", description="wealth-manager engine")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def card_args(p):
        p.add_argument("--card", metavar="PATH",
                       help="also write a one-screen HTML card of this result")
        p.add_argument("--fragment", action="store_true",
                       help="card/report as a bare <div class=\"wm\"> block")

    def rebalance_arg(p):
        p.add_argument("--rebalance", choices=["annual", "daily", "none"],
                       default="annual",
                       help="reset to target weights yearly (default), daily, or never")

    def common(p, bench=True):
        p.add_argument("tickers", nargs="+")
        p.add_argument("--years", type=int, default=5)
        p.add_argument("--pretty", action="store_true")
        p.add_argument("--currency", metavar="CCY", default=None,
                       help="reporting currency; native is allowed only for homogeneous quotes")
        p.add_argument("--price-file", help="offline CSV of already-converted adjusted closes")
        p.add_argument("--metadata-file", help="optional source/currency/assets metadata JSON")
        if bench:
            p.add_argument("--bench", default="SPY")

    p = sub.add_parser("prices"); common(p, bench=False); p.set_defaults(func=cmd_prices)
    p = sub.add_parser("analyze"); common(p); card_args(p)
    p.add_argument("--weights", nargs="+")
    rebalance_arg(p); p.set_defaults(func=cmd_analyze)
    p = sub.add_parser("regimes"); common(p); card_args(p)
    p.add_argument("--weights", nargs="+")
    p.add_argument("--window", type=int, default=126)
    rebalance_arg(p); p.set_defaults(func=cmd_regimes, years=10)
    p = sub.add_parser("factors"); common(p, bench=False); card_args(p)
    p.add_argument("--model", type=int, choices=[3, 5], default=3)
    p.set_defaults(func=cmd_factors)
    p = sub.add_parser("build"); common(p); card_args(p)
    p.add_argument("--method", choices=["equal", "invvol", "minvar", "riskparity"],
                   default="equal")
    p.add_argument("--max-weight", type=float, default=1.0, help="hard portfolio-level maximum weight, not a recommended limit")
    p.add_argument("--sleeves"); p.add_argument("--sleeve-weights")
    p.add_argument("--name"); p.add_argument("--save")
    rebalance_arg(p); p.set_defaults(func=cmd_build)
    p = sub.add_parser("holdings")
    p.add_argument("query", help="fund name or CIK, e.g. \"Berkshire Hathaway\"")
    p.add_argument("--top", type=int, default=15)
    p.add_argument("--weights-from-suggestions", action="store_true",
                   help="also emit investable weights from the unverified issuer-name "
                        "matches, renormalised over the mapped subset, with a coverage "
                        "fraction and a warning. The caveat must be stated to the user.")
    p.add_argument("--pretty", action="store_true")
    p.set_defaults(func=cmd_holdings)
    p = sub.add_parser("report")
    p.add_argument("portfolio"); p.add_argument("--out")
    p.add_argument("--pretty", action="store_true")
    p.add_argument("--fragment", action="store_true")
    p.add_argument('--refresh', action='store_true', help='discard saved price snapshot and refresh all statistics')
    p.add_argument('--price-file'); p.add_argument('--metadata-file')
    p.set_defaults(func=cmd_report)
    p = sub.add_parser('plan'); p.add_argument('profile'); p.set_defaults(func=cmd_plan)
    p = sub.add_parser('review'); p.add_argument('profile'); p.set_defaults(func=cmd_review)
    p = sub.add_parser('fees')
    p.add_argument('--initial',type=float,required=True)
    p.add_argument('--gross-return',type=float,required=True)
    p.add_argument('--fees',type=float,nargs='+',required=True)
    p.add_argument('--years',type=int,required=True); p.set_defaults(func=cmd_fees)
    p = sub.add_parser('compare')
    p.add_argument('portfolio'); p.add_argument('--bench',default='SPY')
    p.add_argument('--currency',required=True); p.add_argument('--years',type=int,default=5)
    p.add_argument('--price-file'); p.add_argument('--metadata-file')
    card_args(p); rebalance_arg(p); p.set_defaults(func=cmd_compare)
    p = sub.add_parser('ingest')
    p.add_argument('file', nargs='?',
                   help='statement export (csv, xlsx; pdf returns text for the agent)')
    p.add_argument('--positions', metavar='FILE',
                   help="JSON positions the agent read itself; '-' reads stdin")
    p.add_argument('--currency', required=True,
                   help='ISO code that values are reported in')
    p.add_argument('--pretty', action='store_true')
    p.set_defaults(func=cmd_ingest)
    for parser in sub.choices.values():
        parser.add_argument('--overwrite',action='store_true',help='explicitly permit replacing a named output')
    args = ap.parse_args(argv)
    if hasattr(args,'bench'):
        args.bench = args.bench.upper()
    if hasattr(args,'currency') and args.currency:
        args.currency = args.currency.upper()
    paths = [getattr(args,k,None) for k in ('card','save','out')]
    if args.cmd == 'report' and not args.out:
        paths.append(str(Path(args.portfolio).with_suffix('.html')))
    if not args.overwrite and any(Path(x).exists() for x in paths if x):
        raise ValueError('output exists; choose another path or explicitly pass --overwrite')
    args.func(args)

if __name__ == "__main__":
    try:
        main()
    except (ValueError, KeyError, OSError, RuntimeError, TypeError) as exc:
        import sys
        print(json.dumps({'error':str(exc),'error_type':type(exc).__name__}),file=sys.stderr)
        raise SystemExit(2)
