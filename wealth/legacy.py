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
                 fx_loader=None, align: bool = True):
    """Adjusted closes, by default on a common date window. Returns (DataFrame, warnings).

    ``align=False`` keeps each asset's own history inside the requested span
    (leading gaps stay NaN) for inception-aware estimators; nothing is filled.
    """
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
    px = px.dropna(axis=1, how="all")
    if align:
        px = px.dropna(how="any")  # common window
    else:
        px = px.dropna(how="all")
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
    """Daily-cache quote currencies. Provider expense ratios are never unit-guessed.

    Yahoo's expense fields mix percent and decimal quoting on the same field
    (``0.03`` may mean 0.03% or 3%), so no reading of them is safe: a guess is a
    100x error one way or the other. The raw fields are kept for checking
    against the issuer, ``expense_ratio`` stays unknown with
    ``fee_status='ambiguous-units'``, and a verified decimal may be supplied to
    analyze_frame via meta instead. A common share is a known zero, not unknown.
    """
    path = _cache_path('meta_v3.json')
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
        kind = str(info.get('quoteType') or '').upper()
        if kind == 'EQUITY' and not raw:
            fee, status = 0.0, 'not-a-fund'
        elif raw:
            fee, status = None, 'ambiguous-units'
        else:
            fee, status = None, 'unavailable'
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
        # The only built-in short-rate series is the US one-month T-bill in the
        # Ken French library. Substituting it for another currency's cash rate
        # is wrong in level and in dynamics, so the statistics are withheld.
        warnings.append(f'no matching-currency risk-free series for {currency}; Sharpe and alpha '
                        f'omitted (supply an explicit {currency} risk-free assumption to compute them)')
        return None
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
                  meta=None, rebalance: str = 'annual', risk_free=None,
                  risk_free_label: str | None = None) -> dict:
    """Descriptive statistics on one common sample.

    ``risk_free`` may be a daily decimal series on the return dates (the caller
    then owns its provenance, described by ``risk_free_label``); otherwise the
    Ken French USD series is used for USD samples unless the price attrs say
    ``risk_free_policy='omit'``. Other currencies get no Sharpe or alpha.
    """
    warnings = list(warnings or [])
    meta = meta or {}
    if (px.index.dayofweek>=5).any():
        raise ValueError('252-day analytics requires weekday close observations; explicitly resample seven-day market data first')
    rets = daily_returns(px)
    currency = px.attrs.get('currency')
    if not currency:
        declared = {meta.get(t,{}).get('currency') for t in px.columns}
        currency = next(iter(declared)) if len(declared) == 1 and None not in declared else None
    if isinstance(risk_free, pd.Series):
        rf = risk_free.reindex(rets.index)
        if rf.isna().any() or not np.isfinite(rf.to_numpy(float)).all():
            raise ValueError('supplied risk-free series must cover every return date')
        rf_label = risk_free_label or 'caller-supplied daily risk-free series'
    else:
        rf = None if px.attrs.get('risk_free_policy') == 'omit' else _rf_daily(rets.index, warnings, currency)
        rf_label = 'Ken French daily USD RF (one-month T-bill)' if rf is not None else None
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
    out = {'schema_version': 3, 'window': _window(rets.index),
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
                           'risk_free': rf_label,
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
                 n_holdings=len(w),
                 weight_concentration_equivalent=_round(effective_bets(w.to_numpy()),2),
                 covariance_participation_ratio=_round(effective_bets_corr(w.to_numpy(), rets[w.index].cov().to_numpy()),2),
                 pca_first_pc_share=_round(pca_first_share(corr),3),
                 expense_ratio=_round(contribution,6) if coverage >= 1-1e-10 else None,
                 fee_known_weight=_round(coverage,6), fee_known_contribution=_round(contribution,6),
                 fee_missing=[k for k in w.index if k not in known])
        out['portfolio'] = p
        if coverage < 1-1e-10:
            warnings.append('complete weighted expense ratio unavailable; fee coverage is partial')
        ambiguous = sorted(k for k in w.index if (meta.get(k) or {}).get('fee_status') == 'ambiguous-units')
        if ambiguous:
            warnings.append('provider expense ratios have ambiguous percent/decimal units and were not used for '
                            + ', '.join(ambiguous) + '; supply issuer-verified decimal expense ratios')
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
        return {'window': None, 'model': model, 'assets': {}, 'warnings': warnings}
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
    return {'window': _window(data.index), 'n_days': len(data), 'model': model,
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

def _price_dates(rets: pd.DataFrame) -> pd.DatetimeIndex:
    """Close dates behind a return frame: the first price date plus each return date."""
    first = rets.attrs.get('price_start')
    if first is None:
        return pd.DatetimeIndex(rets.index)
    return pd.DatetimeIndex([pd.Timestamp(first)]).append(pd.DatetimeIndex(rets.index))

def regime_anchor(rets: pd.DataFrame, start):
    """The first observed close on or after ``start``: the window's base price."""
    closes = _price_dates(rets)
    eligible = closes[closes >= pd.Timestamp(start)]
    return eligible[0] if len(eligible) else None

def slice_regime(rets: pd.DataFrame, start, end) -> pd.DataFrame:
    """Returns measured close-to-close within [start, end].

    The window's base is the first observed close on or after ``start`` and
    its last close is the last one on or before ``end``, so the return *into*
    the start date is excluded. This is the same convention as a price-based
    ``P_end / P_start - 1`` historical stress window.
    """
    anchor = regime_anchor(rets, start)
    hi = pd.Timestamp(end) if end else rets.index.max()
    if anchor is None:
        return rets.iloc[0:0]
    return rets.loc[(rets.index > anchor) & (rets.index <= hi)]

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
        row = {"regime": label, "start": str(regime_anchor(rets, start).date()),
               "window_convention": "close on the first observed date on or after the requested start "
                                    "to the last close on or before the requested end",
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
               "largest_change_date": None, "largest_change": None,
               "lookback_observations": lookback, "structural_break_test": False}
        d = s.diff(lookback).dropna()
        if not d.empty:
            i = d.abs().idxmax()
            rec["largest_change_date"] = str(pd.Timestamp(i).date())
            rec["largest_change"] = _round(d.loc[i], 3)
        out.append(rec)
    return sorted(out, key=lambda r: -abs(r["largest_change"] or 0.0))

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
SEC_UA = os.environ.get("WEALTH_SEC_USER_AGENT") or os.environ.get("SEC_USER_AGENT", "")
_SEC_LAST = [0.0]
_NAME_NOISE = re.compile(
    r"\b(INC|CORP|CORPORATION|CO|COMPANY|COMPANIES|LTD|LIMITED|PLC|SA|NV|AG|GROUP|"
    r"HOLDING|HOLDINGS|CLASS|CL|COM|COMMON|ORD|ORDINARY|SHS|SHARES|STOCK|NEW|THE|"
    r"LLC|LP|TR|TRUST|ADR|ADS|SPONSORED|REIT|PAR)\b")

def _sec_get(url: str, as_json: bool = False):
    if not SEC_UA or "@" not in SEC_UA:
        raise ValueError("set WEALTH_SEC_USER_AGENT to your app name and contact email before EDGAR access")
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
        print(_view('_table')(["symbol", "type", "value", "weight"],
                     [[p["symbol"] or (p["name"] or "?")[:24], p["type"],
                       f"{p['value']:,.0f}", f"{100 * p['weight']:.1f}%"]
                      for p in out["positions"]]))
        if out["unresolved"]:
            print(_view('_table')(["unresolved", "in total", "why"],
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

def solve_weights(rets: pd.DataFrame, method: str, max_w: float = 1.0,
                  cov: np.ndarray | None = None) -> pd.Series:
    """Long-only capped weights. ``cov`` (annualized) overrides the default
    Ledoit-Wolf estimate on the complete common sample, e.g. an inception-aware
    pairwise estimate; ``rets`` may then contain leading gaps."""
    names = list(rets.columns)
    n = len(names)
    if cov is None and (len(rets) < 2 or not np.isfinite(rets.to_numpy(float)).all()):
        raise ValueError("construction requires complete finite returns")
    _cap(np.ones(n), max_w)  # enforce feasibility even for a single asset
    if n == 1:
        return pd.Series([1.0], index=names)
    supplied = cov is not None
    if not supplied:
        cov = shrink_covariance(rets)[0] * TRADING_DAYS
    else:
        cov = np.asarray(cov, float)
        if cov.shape != (n, n) or not np.isfinite(cov).all():
            raise ValueError("supplied covariance must be a finite square matrix over the assets")
    if method == "equal":
        w = _cap(np.full(n, 1.0 / n), max_w)
    elif method == "invvol":
        # Sample volatility (unshrunk) unless a covariance estimate was supplied.
        vol = (np.sqrt(np.clip(np.diag(cov), 0.0, None)) if supplied
               else np.array(rets.std(ddof=1).to_numpy(), dtype=float))
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
# presentation lives in wealth/render.py
# --------------------------------------------------------------------------
def _view(name: str):
    """Resolve a presentation function without a circular module import.

    The ``tools/wm.py`` launcher executes the engine and renderer sources in one
    shared namespace, so the name is already a global there; the installed
    package imports :mod:`wealth.render` on first use.
    """
    found = globals().get(name)
    if found is not None:
        return found
    from . import render
    return getattr(render, name)

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
                       rebalance='annual', meta=None, risk_free=None, risk_free_label=None) -> dict:
    a = analyze_frame(px,bench,current,meta=meta,rebalance=rebalance,
                      risk_free=risk_free,risk_free_label=risk_free_label)
    b = analyze_frame(px,bench,proposed,meta=meta,rebalance=rebalance,
                      risk_free=risk_free,risk_free_label=risk_free_label)
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
        _view('_write_card')(_view('card_compare')(result,px,fragment=args.fragment),args.card)
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
        _view('_write_card')(_view('card_analyze')(res, px, _weights_arg(args, tickers),
                                 fragment=args.fragment), args.card)
    _view('print_analyze')(res) if args.pretty else print(json.dumps(res))

def cmd_regimes(args):
    tickers = [t.upper() for t in args.tickers]
    universe = tickers + ([args.bench] if args.bench not in tickers else [])
    px, warnings = _cli_prices(args, universe)
    if px.empty:
        raise SystemExit(json.dumps({"error": "no price data", "warnings": warnings}))
    res = regimes_frame(px, args.bench, _weights_arg(args, tickers), args.window,
                        warnings, args.rebalance)
    if args.card:
        _view('_write_card')(_view('card_regimes')(res, px, _weights_arg(args, tickers),
                                 fragment=args.fragment), args.card)
    _view('print_regimes')(res) if args.pretty else print(json.dumps(res))

def cmd_factors(args):
    px, warnings = _cli_prices(args, [t.upper() for t in args.tickers])
    if px.empty:
        raise SystemExit(json.dumps({"error": "no price data", "warnings": warnings}))
    res = factor_regression(px, args.model, warnings)
    if args.card:
        _view('_write_card')(_view('card_factors')(res, fragment=args.fragment), args.card)
    _view('print_factors')(res) if args.pretty else print(json.dumps(res))

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
        _view('_write_card')(_view('card_build')(out, px, fragment=args.fragment), args.card)
    if args.pretty:
        print(f"{args.method}  max weight {args.max_weight:.0%}")
        print(_view('_table')(["asset", "weight"],
                     [[k, f"{100 * v:.1f}%"] for k, v in w.items()]))
        print()
        _view('print_analyze')(stats)
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
        print(_view('_table')(["asset", "ticker", "weight"],
                     [[h["name"][:34], h["ticker"] or "—", f"{100 * h['weight']:.1f}%"]
                      for h in res["holdings"]]))
        if res.get("weights"):
            print(f"\nInvestable weights from unverified name matches "
                  f"(coverage {100 * float(res.get('coverage') or 0):.0f}% of shown value):")
            print(_view('_table')(["ticker", "weight"],
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
    path = _view('write_report')(portfolio,px,out,fragment=args.fragment)
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

def cli_entry(argv=None):
    """Run the CLI, reporting expected failures as one JSON line on stderr."""
    try:
        main(argv)
    except (ValueError, KeyError, OSError, RuntimeError, TypeError) as exc:
        import sys
        print(json.dumps({'error':str(exc),'error_type':type(exc).__name__}),file=sys.stderr)
        raise SystemExit(2)

if __name__ == "__main__":
    cli_entry()
