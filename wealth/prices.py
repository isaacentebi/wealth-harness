"""Market data: dated, sourced prices and FX with a local cache.

:class:`PriceProvider` is the one place Wealth reads market prices outside the
ad-hoc ``market`` analysis tasks.  It answers three questions:

* ``latest(symbols, as_of)``: the last close on or before ``as_of``;
* ``history(symbols, start, end)``: daily closes over a window;
* ``fx(base, quote, start, end)``: daily exchange rates over a window.

Rules applied everywhere here:

* **Every price is dated and sourced.**  Each value carries its date, currency,
  source and retrieval time; nothing is ever returned without them.
* **Unknown is never zero.**  A symbol that cannot be priced is listed under
  ``missing`` with a reason; no price is ever 0 or a guess.
* **Yahoo symbols.**  SIC and BMV listings quote in MXN on Yahoo under the
  ``.MX`` suffix with the series appended (``AAPL *`` in the SIC ->
  ``AAPL.MX``; ``WALMEX *`` -> ``WALMEX.MX``; ``NAFTRAC ISHRS`` ->
  ``NAFTRAC.MX``; ``GFNORTE O`` -> ``GFNORTEO.MX``).  US symbols use Yahoo's
  share-class dash (``BRK.B`` -> ``BRK-B``).  FX is ``USDMXN=X`` style.
* **Mexican government fixed income is not on Yahoo.**  CETES, UDIBONOS,
  BONDES and Bonos M are priced from a statement or trade price the caller
  passes, accrued at a stated annual rate when one is given (simple interest,
  actual/360, the CETES convention); otherwise they are missing.
* **Cache.**  Values are stored in the Wealth database (``market_prices``),
  keyed by provider symbol, kind (close or adjusted close) and date, with the
  source and retrieval time.  A latest value is reused for 15 minutes while
  markets are open (weekdays 13:30-21:00 UTC, which covers NYSE and BMV hours)
  and 12 hours otherwise.  A past day fetched after it closed is final.
* **Offline.**  With ``WEALTH_OFFLINE=1``, or when a fetch fails or runs past
  its time budget, cached values are served labelled ``origin`` =
  ``cache_fallback`` (and ``stale`` when old), or the symbol is missing.  No
  method raises into the caller because the network is down.
* **Staleness.**  A price dated more than ``stale_after_days`` (5) calendar
  days before the date it is used for is ``stale``; payloads list such prices
  in ``stale_prices`` so a page can say so.

The module also turns a ledger into the inputs the pure engines take
(:func:`ledger_market`, :func:`ledger_series`, :func:`fx_rows`) and builds the
IPS benchmark from ETF proxies (:func:`benchmarks`).  Proxies are named as
proxies wherever they appear.
"""
from __future__ import annotations

import concurrent.futures as futures
import math
import os
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

OFFLINE_ENV = "WEALTH_OFFLINE"
LATEST_TTL_OPEN = timedelta(minutes=15)
LATEST_TTL_CLOSED = timedelta(hours=12)
STALE_AFTER_DAYS = 5
DEFAULT_BUDGET_SECONDS = 1.5
LATEST_LOOKBACK_DAYS = 10
YAHOO = "Yahoo Finance via yfinance"
FX_CARRY_DAYS = 4

# Mexican fixed income that Yahoo does not quote.
_MX_FIXED_INCOME = ("CETES", "CETE", "UDIBONO", "UDIBONOS", "BONDES", "BONDE", "BONOS", "BONO", "MBONO")
_FIXED_INCOME_CLASSES = {"government_bond", "fixed_income", "bond"}
# BMV series that Yahoo leaves out of the symbol: "*" (single series) and ISHRS (iShares trusts).
_DROPPED_SERIES = {"*", "ISHRS", "ISHARES"}
_SUBUNITS = {"GBp": ("GBP", 100), "GBX": ("GBP", 100), "ZAc": ("ZAR", 100), "ILA": ("ILS", 100)}

# ETF proxies for the IPS benchmark and the global 60/40 reference.  Adjusted closes (dividends
# reinvested) in USD, converted to the reporting currency at the daily Yahoo rate.
PROXIES: dict[str, dict[str, str]] = {
    "global_equity": {"symbol": "ACWI", "currency": "USD", "index": "MSCI ACWI",
                      "vehicle": "iShares MSCI ACWI ETF (ACWI)"},
    "us_fixed_income": {"symbol": "AGG", "currency": "USD", "index": "Bloomberg US Aggregate",
                        "vehicle": "iShares Core US Aggregate Bond ETF (AGG)"},
    "global_bonds": {"symbol": "BNDW", "currency": "USD", "index": "Global aggregate bonds",
                     "vehicle": "Vanguard Total World Bond ETF (BNDW)"},
    "usd_cash": {"symbol": "BIL", "currency": "USD", "index": "US T-bills 1-3 months",
                 "vehicle": "SPDR Bloomberg 1-3 Month T-Bill ETF (BIL)"},
}


@dataclass
class Fetched:
    """What a fetcher returns: ``{date: close}`` and the quote currency (None when unknown)."""

    points: dict[str, float]
    currency: str | None
    source: str = YAHOO


# (provider_symbol, start, end, adjusted) -> Fetched; raises on failure.
Fetcher = Callable[[str, date, date, bool], Fetched]


# ------------------------------------------------------------------ symbols


def _clean(symbol: Any) -> str:
    return re.sub(r"\s+", " ", str(symbol or "").strip().upper())


def is_mx_fixed_income(symbol: Any, asset_class: Any = None, currency: Any = None) -> bool:
    """CETES, UDIBONOS, BONDES, Bonos M: by symbol, by asset class, or a peso bond."""
    head = re.split(r"[\s_-]", _clean(symbol))[0] if symbol else ""
    if head.startswith(_MX_FIXED_INCOME):
        return True
    cls = str(asset_class or "").lower()
    if cls in {"cetes", "udibonos", "bondes", "mbonos"}:
        return True
    return cls in _FIXED_INCOME_CLASSES and str(currency or "MXN").upper() == "MXN"


def provider_symbol(symbol: Any, *, venue: Any = None, listing: Any = None, currency: Any = None,
                    asset_class: Any = None) -> tuple[str | None, str | None]:
    """``(yahoo_symbol, None)`` or ``(None, reason)`` for one holding.

    SIC and BMV listings (venue ``sic``/``bmv``/``biva``, listing SIC/BMV/BIVA, a
    ``" *"``-style series, or an MXN listing currency) map to ``<emisora><series>.MX``.
    """
    raw = _clean(symbol)
    if not raw:
        return None, "no symbol"
    if raw.startswith("CASH") or str(asset_class or "").lower() in {"cash", "money_market"}:
        return None, "cash is not priced"
    if is_mx_fixed_income(raw, asset_class, currency):
        return None, "Mexican government fixed income is not quoted on Yahoo; it needs a statement price or a stated rate"
    if raw.endswith(".MX") or raw.endswith("=X") or raw.startswith("^"):
        return raw, None
    venue = str(venue or "").lower()
    listing = str(listing or "").upper()
    ccy = str(currency or "").upper()
    head, _, series = raw.partition(" ")
    series = series.replace(" ", "")
    head = head.rstrip("*")
    mexican = (venue in {"sic", "bmv", "biva"} or listing in {"SIC", "BMV", "BIVA"} or raw.endswith("*")
               or (series != "" and ccy in {"", "MXN"}) or (ccy == "MXN" and venue not in {"us", "other"}))
    if mexican:
        suffix = "" if series in _DROPPED_SERIES or series.strip("*") == "" else series
        base = re.sub(r"[^A-Z0-9&-]", "", head.replace(".", "").replace("/", "")) + re.sub(r"[^A-Z0-9]", "", suffix)
        return (base + ".MX", None) if base else (None, "unreadable symbol")
    if series:
        return None, f"cannot tell the market of {raw!r}; save its venue (us, sic, bmv)"
    us = re.sub(r"[/.]([A-Z]{1,2})$", r"-\1", head)
    if not re.fullmatch(r"[A-Z0-9^=._-]{1,20}", us):
        return None, "unreadable symbol"
    return us, None


def fx_symbol(base: str, quote: str) -> str:
    return f"{base.upper()}{quote.upper()}=X"


# ------------------------------------------------------------------ default fetcher (network)


def yahoo_fetcher(symbol: str, start: date, end: date, adjusted: bool) -> Fetched:
    """Daily closes from Yahoo via yfinance (the same library as :mod:`wealth.legacy`)."""
    import sqlite3

    import pandas as pd
    import yfinance as yf

    from . import legacy
    from .market import quote_currency

    last: Exception | None = None
    frame = None
    for attempt in range(2):
        try:
            frame = yf.download(symbol, start=start.isoformat(), end=(end + timedelta(days=1)).isoformat(),
                                interval="1d", auto_adjust=adjusted, progress=False, threads=False,
                                actions=False, timeout=8)
            break
        except sqlite3.OperationalError as exc:  # yfinance's own tz cache lock
            last = exc
            time.sleep(0.5 + attempt)
    if frame is None:
        raise RuntimeError(f"{symbol}: {last}")
    if frame.empty:
        raise LookupError(f"{symbol}: no data returned")
    if isinstance(frame.columns, pd.MultiIndex):
        frame.columns = frame.columns.get_level_values(0)
    closes = frame["Close"].dropna()
    points = {pd.Timestamp(day).date().isoformat(): float(value) for day, value in closes.items()
              if math.isfinite(float(value)) and float(value) > 0}
    if symbol.endswith("=X"):
        currency = symbol[3:6] if len(symbol) == 8 else None
    else:
        meta = (legacy._ticker_meta([symbol]) or {}).get(symbol) or {}
        currency = quote_currency(symbol, meta.get("currency"))
    if currency in _SUBUNITS:
        currency, factor = _SUBUNITS[currency]
        points = {d: v / factor for d, v in points.items()}
    source = f"{YAHOO} {'adjusted close (dividends reinvested)' if adjusted else 'daily close'} of {symbol}"
    return Fetched(points, currency.upper() if currency else None, source)


# ------------------------------------------------------------------ helpers


def _iso(value: Any) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def _dec(value: Any) -> Decimal | None:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return result if result.is_finite() and result > 0 else None


def market_open(now: datetime) -> bool:
    """Weekdays 13:30-21:00 UTC: NYSE (9:30-16:00 New York) and BMV (7:30-15:00 Mexico City) overlap it."""
    now = now.astimezone(timezone.utc)
    minutes = now.hour * 60 + now.minute
    return now.weekday() < 5 and 13 * 60 + 30 <= minutes < 21 * 60


def offline_mode() -> bool:
    return os.environ.get(OFFLINE_ENV, "").strip().lower() in {"1", "true", "yes", "on"}


_POOL = futures.ThreadPoolExecutor(max_workers=4, thread_name_prefix="wealth-prices")


@dataclass
class _Item:
    key: str
    symbol: str
    provider: str | None
    reason: str | None
    currency: str | None
    statement: dict | None = None
    rate: Decimal | None = None
    rate_source: str | None = None


def _item(raw: Any) -> _Item:
    if isinstance(raw, str):
        yahoo, reason = provider_symbol(raw)
        return _Item(raw, raw, yahoo, reason, None)
    if not isinstance(raw, Mapping):
        raise ValueError("a symbol is a string or {symbol, venue?, currency?, asset_class?, id?}")
    symbol = str(raw.get("symbol") or raw.get("id") or "")
    yahoo = raw.get("provider_symbol")
    reason = None
    if not yahoo:
        yahoo, reason = provider_symbol(symbol, venue=raw.get("venue"), listing=raw.get("listing"),
                                        currency=raw.get("currency"), asset_class=raw.get("asset_class"))
    statement = raw.get("statement_price") if isinstance(raw.get("statement_price"), Mapping) else None
    rate = raw.get("annual_rate")
    return _Item(str(raw.get("id") or symbol), symbol, yahoo, reason,
                 str(raw["currency"]).upper() if raw.get("currency") else None, dict(statement) if statement else None,
                 _dec(rate) if rate is not None else None, raw.get("rate_source"))


# ------------------------------------------------------------------ provider


@dataclass
class PriceProvider:
    """Cached market data from Yahoo (or an injected ``fetcher``) stored in the Wealth database.

    ``offline`` forces cache-only reads (default: the ``WEALTH_OFFLINE`` environment
    variable).  ``clock`` returns the current UTC time (tests pin it).
    """

    db_path: str | os.PathLike | None
    fetcher: Fetcher | None = None
    offline: bool | None = None
    clock: Callable[[], datetime] = field(default=lambda: datetime.now(timezone.utc))
    stale_after_days: int = STALE_AFTER_DAYS
    _pending: dict[tuple[str, str], futures.Future] = field(default_factory=dict, init=False, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    # -- plumbing

    def _offline(self) -> bool:
        return offline_mode() if self.offline is None else bool(self.offline)

    def _store(self):
        from .store import WealthStore
        return WealthStore(Path(self.db_path) if self.db_path not in (None, ":memory:") else ":memory:")

    def _now(self) -> datetime:
        return self.clock().astimezone(timezone.utc)

    def _ttl(self) -> timedelta:
        return LATEST_TTL_OPEN if market_open(self._now()) else LATEST_TTL_CLOSED

    def _fetch_and_store(self, symbol: str, kind: str, start: date, end: date) -> Fetched:
        """Fetch one window and write it to the cache (called on the worker pool)."""
        fetcher = self.fetcher or yahoo_fetcher
        retrieved = self._now().isoformat(timespec="seconds")
        try:
            got = fetcher(symbol, start, end, kind == "adjclose")
        except Exception as exc:  # noqa: BLE001 - a provider failure means cached or missing, never a crash
            try:
                with self._store() as store:
                    store.put_market([], {"symbol": symbol, "kind": kind, "start": start.isoformat(),
                                          "end": end.isoformat(), "retrieved_at": retrieved, "status": "failed",
                                          "detail": f"{type(exc).__name__}: {exc}"[:300]})
            except Exception:  # noqa: BLE001
                pass
            raise
        rows = [{"symbol": symbol, "kind": kind, "date": day, "value": repr(float(value)), "currency": got.currency,
                 "source": got.source, "retrieved_at": retrieved}
                for day, value in sorted(got.points.items())
                if start.isoformat() <= day <= end.isoformat() and value and math.isfinite(value) and value > 0]
        with self._store() as store:
            store.put_market(rows, {"symbol": symbol, "kind": kind, "start": start.isoformat(),
                                    "end": end.isoformat(), "retrieved_at": retrieved, "status": "ok"})
        return got

    def _submit(self, symbol: str, kind: str, start: date, end: date) -> futures.Future:
        with self._lock:
            key = (symbol, kind)
            running = self._pending.get(key)
            if running is not None and not running.done():
                return running
            job = _POOL.submit(self._fetch_and_store, symbol, kind, start, end)
            self._pending[key] = job
            return job

    def wait(self, timeout: float | None = None) -> None:
        """Wait for background refreshes (tests and the ``prices refresh`` op)."""
        with self._lock:
            jobs = list(self._pending.values())
        futures.wait(jobs, timeout=timeout)

    def _fresh(self, store, symbol: str, kind: str, start: date, end: date, today: date) -> bool:
        """True when a successful fetch already covers [start, end] and needs no refresh."""
        past_end = min(end, today - timedelta(days=1))
        for fetch in store.market_fetches(symbol, kind):
            if fetch["status"] != "ok" or fetch["start"] > start.isoformat():
                continue
            retrieved = datetime.fromisoformat(fetch["retrieved_at"])
            if end >= today:
                # The window reaches today: the fetch must reach today, be within the latest TTL and
                # not straddle an open or a close (an intraday value is refetched once the market closes).
                now = self._now()
                if (fetch["end"] >= today.isoformat() and now - retrieved <= self._ttl()
                        and market_open(retrieved) == market_open(now)):
                    return True
            elif fetch["end"] >= past_end.isoformat() and retrieved.date() > past_end:
                return True  # fetched after the window closed: history is final
        return False

    def _row_out(self, item: _Item, row: Mapping[str, Any], on: date, origin: str) -> dict:
        age = (on - date.fromisoformat(row["date"])).days
        return {"symbol": item.symbol, "provider_symbol": row.get("symbol", item.provider),
                "price": str(Decimal(row["value"])), "currency": row.get("currency"), "date": row["date"],
                "source": row["source"], "retrieved_at": row.get("retrieved_at"), "origin": origin,
                "age_days": age, "stale": age > self.stale_after_days}

    # -- public API

    def latest(self, symbols: Iterable[Any], as_of: Any = None, *, budget: float | None = None) -> dict:
        """The last close on or before ``as_of`` (default today) for each symbol.

        ``symbols`` are strings or ``{id?, symbol, venue?, listing?, currency?, asset_class?,
        statement_price?: {price, date, source?}, annual_rate?, rate_source?}``; results are keyed
        by ``id`` (else the symbol).  ``budget`` (seconds) bounds the wait for the network: past
        it, cached values are used and the refresh finishes in the background (``pending``).
        """
        today = self._now().date()
        on = _iso(as_of) if as_of is not None else today
        items = [_item(s) for s in symbols]
        out: dict[str, dict] = {}
        missing: list[dict] = []
        pending: list[str] = []
        start = on - timedelta(days=LATEST_LOOKBACK_DAYS)
        jobs: dict[str, futures.Future] = {}
        with self._store() as store:
            for item in items:
                if item.provider is None or self._offline():
                    continue
                if not self._fresh(store, item.provider, "close", start, on, today):
                    jobs[item.key] = self._submit(item.provider, "close", start, max(on, start))
        if jobs:
            futures.wait(list(jobs.values()), timeout=budget)
        with self._store() as store:
            for item in items:
                if item.provider is None:
                    found = self._fixed_income(item, on)
                    if found is None:
                        missing.append({"symbol": item.key, "reason": item.reason or "no provider symbol"})
                    else:
                        out[item.key] = found
                    continue
                job = jobs.get(item.key)
                failed = False
                if job is not None and not job.done():
                    pending.append(item.key)
                    failed = True
                elif job is not None and job.exception() is not None:
                    failed = True
                rows = store.market_rows(item.provider, "close", (on - timedelta(days=366)).isoformat(), on.isoformat())
                if not rows:
                    reason = ("offline and nothing cached" if self._offline() else
                              "refresh still running and nothing cached" if item.key in pending else
                              f"no price: {job.exception()}" if job is not None and job.exception() is not None
                              else "no price on or before this date")
                    missing.append({"symbol": item.key, "provider_symbol": item.provider, "reason": reason})
                    continue
                row = rows[-1]
                origin = "cache_fallback" if (failed or self._offline()) else ("live" if job is not None else "cache")
                quote = self._row_out(item, row, on, origin)
                if item.currency and quote["currency"] and quote["currency"] != item.currency:
                    missing.append({"symbol": item.key, "provider_symbol": item.provider,
                                    "reason": f"{item.provider} quotes in {quote['currency']}, not {item.currency}"})
                    continue
                if quote["currency"] is None:
                    quote["currency"] = item.currency
                if quote["currency"] is None:
                    missing.append({"symbol": item.key, "provider_symbol": item.provider,
                                    "reason": "quote currency unknown; not used"})
                    continue
                out[item.key] = quote
        return {"as_of": on.isoformat(), "prices": out, "missing": missing, "pending": pending,
                "offline": self._offline(),
                "stale_prices": [{"symbol": k, "date": v["date"], "age_days": v["age_days"], "source": v["source"]}
                                 for k, v in out.items() if v["stale"]]}

    def _fixed_income(self, item: _Item, on: date) -> dict | None:
        """A statement or trade price, accrued at a stated annual rate when one is given."""
        stated = item.statement or {}
        price, when = _dec(stated.get("price")), stated.get("date")
        if price is None or not when or _iso(when) > on:
            return None
        days = (on - _iso(when)).days
        source = stated.get("source") or "statement price"
        origin = "statement"
        if item.rate is not None and days > 0:
            price = price * (1 + item.rate * Decimal(days) / Decimal(360))
            source = (f"{source} of {when} accrued {days} days at {item.rate} a year "
                      f"(simple, actual/360{'; ' + item.rate_source if item.rate_source else ''})")
            origin, day, age = "accrual", on.isoformat(), 0
        else:
            day, age = _iso(when).isoformat(), days
        return {"symbol": item.symbol, "provider_symbol": None, "price": str(price.quantize(Decimal("0.000001"))),
                "currency": item.currency or "MXN", "date": day, "source": source, "retrieved_at": None,
                "origin": origin, "age_days": age, "stale": age > self.stale_after_days}

    def history(self, symbols: Iterable[Any], start: Any, end: Any, *, adjusted: bool = False,
                budget: float | None = None) -> dict:
        """Daily closes in [start, end] per symbol: ``{series: {key: {points, currency, source, ...}}, missing}``.

        ``adjusted`` reads adjusted closes (dividends reinvested), for benchmarks; holdings are
        valued at plain closes.
        """
        first, last = _iso(start), _iso(end)
        if last < first:
            raise ValueError("end precedes start")
        today = self._now().date()
        kind = "adjclose" if adjusted else "close"
        items = [_item(s) for s in symbols]
        jobs: dict[str, futures.Future] = {}
        with self._store() as store:
            for item in items:
                if item.provider and not self._offline() and not self._fresh(store, item.provider, kind, first,
                                                                             min(last, today), today):
                    jobs[item.key] = self._submit(item.provider, kind, first, min(last, today))
        if jobs:
            futures.wait(list(jobs.values()), timeout=budget)
        series: dict[str, dict] = {}
        missing: list[dict] = []
        with self._store() as store:
            for item in items:
                if item.provider is None:
                    missing.append({"symbol": item.key, "reason": item.reason or "no provider symbol"})
                    continue
                job = jobs.get(item.key)
                failed = job is not None and (not job.done() or job.exception() is not None)
                rows = store.market_rows(item.provider, kind, first.isoformat(), last.isoformat())
                if not rows:
                    missing.append({"symbol": item.key, "provider_symbol": item.provider,
                                    "reason": "offline and nothing cached" if self._offline() else "no data in range"})
                    continue
                currency = rows[-1].get("currency") or item.currency
                if item.currency and currency != item.currency:
                    missing.append({"symbol": item.key, "provider_symbol": item.provider,
                                    "reason": f"{item.provider} quotes in {currency}, not {item.currency}"})
                    continue
                series[item.key] = {
                    "symbol": item.symbol, "provider_symbol": item.provider, "currency": currency,
                    "points": [{"date": r["date"], "price": str(Decimal(r["value"]))} for r in rows],
                    "source": rows[-1]["source"], "retrieved_at": max(r["retrieved_at"] for r in rows),
                    "first": rows[0]["date"], "last": rows[-1]["date"], "adjusted": adjusted,
                    "origin": "cache_fallback" if failed or self._offline() else ("live" if job else "cache"),
                    "stale": (last - date.fromisoformat(rows[-1]["date"])).days > self.stale_after_days}
        return {"start": first.isoformat(), "end": last.isoformat(), "series": series, "missing": missing,
                "offline": self._offline(), "pending": [k for k, j in jobs.items() if not j.done()]}

    def fx(self, base: str, quote: str, start: Any, end: Any, *, budget: float | None = None) -> dict:
        """Daily ``base -> quote`` rates in [start, end]: ``{rates: [{date, rate}], source, missing?}``.

        Tries ``BASEQUOTE=X`` then the inverse pair (inverted).  Same currency is not a lookup.
        """
        base, quote = base.upper(), quote.upper()
        if base == quote:
            raise ValueError("base and quote are the same currency")
        direct = self.history([{"id": "direct", "symbol": fx_symbol(base, quote),
                                "provider_symbol": fx_symbol(base, quote)}], start, end, budget=budget)
        found, invert = direct["series"].get("direct"), False
        if found is None:
            inverse = self.history([{"id": "inverse", "symbol": fx_symbol(quote, base),
                                     "provider_symbol": fx_symbol(quote, base)}], start, end, budget=budget)
            found, invert = inverse["series"].get("inverse"), True
        if found is None:
            return {"base": base, "quote": quote, "rates": [], "source": None,
                    "missing": [{"symbol": f"{base}/{quote}", "reason": (direct["missing"] or [{}])[0].get("reason")}]}
        rates = [{"date": p["date"], "rate": str(Decimal(1) / Decimal(p["price"]) if invert else Decimal(p["price"]))}
                 for p in found["points"]]
        source = found["source"] + (" (inverted)" if invert else "")
        return {"base": base, "quote": quote, "rates": rates, "source": source, "origin": found["origin"],
                "retrieved_at": found["retrieved_at"], "missing": []}

    def refresh(self, symbols: Iterable[Any] | None = None, *, days: int = LATEST_LOOKBACK_DAYS) -> dict:
        """Refetch the latest window now (blocking) for ``symbols`` or every cached close symbol."""
        if self._offline():
            return {"refreshed": [], "failed": [], "offline": True,
                    "detail": f"{OFFLINE_ENV}=1: nothing was fetched; cached values are served as they are."}
        today = self._now().date()
        if symbols is None:
            with self._store() as store:
                providers = sorted({r["symbol"] for r in store.market_summary() if r["kind"] == "close"})
        else:
            providers = []
            for raw in symbols:
                item = _item(raw)
                if item.provider:
                    providers.append(item.provider)
        refreshed, failed = [], []
        for provider in dict.fromkeys(providers):
            try:
                got = self._fetch_and_store(provider, "close", today - timedelta(days=days), today)
                refreshed.append({"provider_symbol": provider, "points": len(got.points), "currency": got.currency})
            except Exception as exc:  # noqa: BLE001
                failed.append({"provider_symbol": provider, "reason": f"{type(exc).__name__}: {exc}"[:200]})
        return {"refreshed": refreshed, "failed": failed, "offline": False}

    def status(self) -> dict:
        """What is cached: one row per provider symbol and kind, with its age."""
        now = self._now()
        with self._store() as store:
            rows = store.market_summary()
        for row in rows:
            row["age_days"] = (now.date() - date.fromisoformat(row["last"])).days
            row["stale"] = row["age_days"] > self.stale_after_days
        return {"offline": self._offline(), "now": now.isoformat(timespec="seconds"), "market_open": market_open(now),
                "ttl_minutes": int(self._ttl().total_seconds() // 60), "stale_after_days": self.stale_after_days,
                "symbols": rows}


# ------------------------------------------------------------------ ledger helpers


def _holdings(ledger: Mapping[str, Any], as_of: str) -> tuple[dict, dict]:
    """``(cash {(account, ccy): amount}, quantities {(account, instrument): quantity})`` at ``as_of``."""
    from .ledger.derive import replay

    state, _ = replay(ledger, as_of)
    quantities = {}
    for key, lots in state.lots.items():
        quantity = sum((lot.quantity for lot in lots), Decimal(0))
        if quantity:
            quantities[key] = quantity
    return {k: v for k, v in state.cash.items() if v}, quantities


def held_quantities(ledger: Mapping[str, Any], as_of: str) -> dict:
    """``{(account_id, instrument_id): quantity}`` held at the end of ``as_of``."""
    return _holdings(ledger, as_of)[1]


def _trade_prices(ledger: Mapping[str, Any]) -> dict[str, dict]:
    """The last trade price per instrument (gross amount / quantity), for instruments Yahoo does not quote."""
    found: dict[str, dict] = {}
    for entry in ledger.get("entries") or []:
        if entry.get("kind") not in {"buy", "sell"} or not entry.get("instrument_id"):
            continue
        amount, quantity, fee = _dec(abs(Decimal(str(entry.get("amount") or 0)))), entry.get("quantity"), entry.get("fee")
        quantity = _dec(abs(Decimal(str(quantity)))) if quantity is not None else None
        if amount is None or quantity is None:
            continue
        gross = amount - (Decimal(str(fee)) if fee else 0) if entry["kind"] == "buy" else amount + (Decimal(str(fee)) if fee else 0)
        if gross <= 0:
            continue
        current = found.get(entry["instrument_id"])
        if current is None or current["date"] <= entry["date"]:
            found[entry["instrument_id"]] = {"price": str(gross / quantity), "date": entry["date"],
                                             "source": f"ledger trade price ({entry['kind']} on {entry['date']})"}
    return found


def instrument_specs(ledger: Mapping[str, Any], ids: Iterable[str] | None = None) -> list[dict]:
    """Provider lookups for ledger instruments (all, or ``ids``)."""
    trades = _trade_prices(ledger)
    wanted = set(ids) if ids is not None else None
    specs = []
    for instrument in ledger.get("instruments") or []:
        if wanted is not None and instrument["id"] not in wanted:
            continue
        spec = {"id": instrument["id"], "symbol": instrument.get("symbol") or instrument["id"],
                "venue": instrument.get("venue"), "listing": instrument.get("listing"),
                "currency": instrument.get("currency"), "asset_class": instrument.get("asset_class")}
        if instrument["id"] in trades:
            spec["statement_price"] = trades[instrument["id"]]
        specs.append(spec)
    return specs


def _fx_latest(provider: PriceProvider, currencies: Iterable[str], reporting: str | None, as_of: date,
               budget: float | None) -> tuple[list[dict], list[dict], list[str]]:
    """Latest rate per currency into ``reporting``: the direct Yahoo pair, else the inverse pair as quoted
    (the FX tables read a pair and its inverse alike)."""
    rows, missing, pending = [], [], []
    wanted = sorted({c for c in currencies if c and reporting and c != reporting})
    if not wanted:
        return rows, missing, pending

    def ask(pairs: list[tuple[str, str]]) -> dict:
        return provider.latest([{"id": f"{b}/{q}", "symbol": fx_symbol(b, q), "provider_symbol": fx_symbol(b, q),
                                 "currency": q} for b, q in pairs], as_of, budget=budget)

    quotes = ask([(c, reporting) for c in wanted])
    retry = [m["symbol"].split("/")[0] for m in quotes["missing"]]
    found = [quotes]
    if retry:
        inverse = ask([(reporting, c) for c in retry])
        found.append(inverse)
        still = {m["symbol"].split("/")[1] for m in inverse["missing"]}
        missing += [{"symbol": m["symbol"], "reason": m["reason"]} for m in quotes["missing"]
                    if m["symbol"].split("/")[0] in still]
    for got in found:
        for pair, quote in got["prices"].items():
            base, quote_ccy = pair.split("/")
            rows.append({"base": base, "quote": quote_ccy, "rate": quote["price"], "date": quote["date"],
                         "source": quote["source"], "stale": quote["stale"], "origin": quote["origin"]})
        pending += got["pending"]
    return rows, missing, pending


def ledger_market(provider: PriceProvider, ledger: Mapping[str, Any] | None, as_of: Any, currency: str | None, *,
                  budget: float | None = DEFAULT_BUDGET_SECONDS, accounts: Iterable[str] | None = None) -> dict | None:
    """Latest prices for what the ledger holds at ``as_of`` plus FX into ``currency``.

    Returns ``{prices: {instrument_id: quote}, fx: [rows], missing, stale_prices, pending, as_of}``
    (the shape :func:`wealth.situation.build` takes as ``market``), or None without holdings.
    """
    if not ledger or not ledger.get("entries"):
        return None
    on = _iso(as_of)
    scope = set(accounts) if accounts is not None else None
    cash, quantities = _holdings(ledger, on.isoformat())
    if scope is not None:
        cash = {k: v for k, v in cash.items() if k[0] in scope}
        quantities = {k: v for k, v in quantities.items() if k[0] in scope}
    ids = sorted({instrument for _, instrument in quantities})
    quotes = provider.latest(instrument_specs(ledger, ids), on, budget=budget) if ids else \
        {"prices": {}, "missing": [], "pending": [], "stale_prices": []}
    currencies = {ccy for _, ccy in cash} | {q["currency"] for q in quotes["prices"].values()}
    fx, fx_missing, fx_pending = _fx_latest(provider, currencies, currency, on, budget)
    return {"as_of": on.isoformat(), "currency": currency, "prices": quotes["prices"], "fx": fx,
            "missing": quotes["missing"] + fx_missing, "pending": quotes["pending"] + fx_pending,
            "stale_prices": quotes["stale_prices"] + [{"symbol": f"{r['base']}/{r['quote']}", "date": r["date"],
                                                       "source": r["source"]} for r in fx if r["stale"]],
            "offline": provider._offline()}


def ledger_series(provider: PriceProvider, ledger: Mapping[str, Any], start: Any, end: Any,
                  budget: float | None = None) -> dict:
    """Daily closes for every ledger instrument over [start, end], as ``{instrument_id: [{date, price}]}``.

    Instruments Yahoo does not quote get their last trade price on or before ``start`` and ``end``
    (dated as traded) so values stay unknown rather than guessed when that price is too old.
    """
    first, last = _iso(start), _iso(end)
    specs = instrument_specs(ledger)
    got = provider.history(specs, first, last, budget=budget)
    series = {key: s["points"] for key, s in got["series"].items()}
    sources = [{"instrument_id": key, "source": s["source"], "first": s["first"], "last": s["last"],
                "retrieved_at": s["retrieved_at"], "origin": s["origin"]} for key, s in got["series"].items()]
    missing = []
    for item in got["missing"]:
        spec = next((s for s in specs if s["id"] == item["symbol"]), None)
        stated = (spec or {}).get("statement_price")
        if stated and stated["date"] <= last.isoformat():
            series[item["symbol"]] = [{"date": stated["date"], "price": stated["price"]}]
            sources.append({"instrument_id": item["symbol"], "source": stated["source"], "first": stated["date"],
                            "last": stated["date"], "retrieved_at": None, "origin": "statement"})
        else:
            missing.append(item)
    return {"prices": series, "sources": sources, "missing": missing, "pending": got["pending"]}


def fx_rows(provider: PriceProvider, currencies: Iterable[str], reporting: str, start: Any, end: Any,
            budget: float | None = None) -> tuple[list[dict], list[dict]]:
    """Ledger-style FX rows ``[{date, base, quote, rate, source}]`` from each currency into ``reporting``."""
    rows, missing = [], []
    for ccy in sorted({c for c in currencies if c and c != reporting}):
        got = provider.fx(ccy, reporting, start, end, budget=budget)
        rows += [{"date": r["date"], "base": ccy, "quote": reporting, "rate": r["rate"], "source": got["source"]}
                 for r in got["rates"]]
        missing += got["missing"]
    return rows, missing


def with_fx(ledger: Mapping[str, Any], rows: Sequence[Mapping[str, Any]]) -> dict:
    """A copy of the ledger with provider FX rows added where the ledger has no rate for that pair and day."""
    have = {(r.get("base"), r.get("quote"), r.get("date")) for r in ledger.get("fx") or []}
    extra = [dict(r) for r in rows if (r["base"], r["quote"], r["date"]) not in have]
    return {**ledger, "fx": list(ledger.get("fx") or []) + extra}


# ------------------------------------------------------------------ benchmarks


def _convert_series(points: list[dict], fx: list[dict] | None) -> list[dict]:
    if fx is None:
        return [{"date": p["date"], "value": p["price"]} for p in points]
    rates = sorted((r["date"], Decimal(r["rate"])) for r in fx)
    out, index = [], -1
    for point in points:
        while index + 1 < len(rates) and rates[index + 1][0] <= point["date"]:
            index += 1
        if index < 0 or (date.fromisoformat(point["date"]) - date.fromisoformat(rates[index][0])).days > FX_CARRY_DAYS:
            continue
        out.append({"date": point["date"], "value": str(Decimal(point["price"]) * rates[index][1])})
    return out


def _proxy_for(sleeve: Mapping[str, Any], currency: str) -> str | None:
    asset, sleeve_id = sleeve.get("asset"), str(sleeve.get("id") or "")
    if asset == "equity":
        return "global_equity"
    if asset == "fixed_income":
        if sleeve_id.startswith("mx_") or currency == "MXN":
            return "cetes"
        return "us_fixed_income" if sleeve_id.startswith("us_") or currency == "USD" else None
    if asset == "cash":
        return "cetes" if currency == "MXN" else "usd_cash" if currency == "USD" else None
    return None


def benchmarks(provider: PriceProvider, ips: Mapping[str, Any] | None, currency: str, start: Any, end: Any, *,
               cetes_rate: Mapping[str, Any] | None = None, budget: float | None = None) -> dict:
    """``{benchmarks, proxies, missing}`` for :func:`wealth.review.quarterly` from ETF/index proxies.

    Global equity is ACWI, US bonds AGG, USD cash BIL and global bonds BNDW (adjusted closes in USD,
    converted at the daily Yahoo rate when the report is in another currency).  Mexican fixed income
    and MXN cash accrue at the published CETES 28-day rate when ``cetes_rate`` ({rate, source}) is
    given; otherwise they are missing.  Every name says "proxy".
    """
    first, last = _iso(start), _iso(end)
    window = first - timedelta(days=LATEST_LOOKBACK_DAYS)
    needed: dict[str, str] = {}
    sleeves = [s for s in ((ips or {}).get("allocation") or {}).get("sleeves") or [] if s.get("target")]
    for sleeve in sleeves:
        proxy = _proxy_for(sleeve, currency)
        if proxy:
            needed[sleeve["id"]] = proxy
    wanted = {p for p in needed.values() if p in PROXIES} | {"global_equity", "global_bonds"}
    got = provider.history([{"id": key, "symbol": PROXIES[key]["symbol"], "provider_symbol": PROXIES[key]["symbol"],
                             "currency": PROXIES[key]["currency"]} for key in sorted(wanted)],
                           window, last, adjusted=True, budget=budget)
    fx_cache: dict[str, list[dict] | None] = {}
    missing = [{"key": f"benchmarks.proxy.{m['symbol']}", "reason": m["reason"]} for m in got["missing"]]
    proxies: list[dict] = []

    def spec(key: str) -> dict | None:
        found = got["series"].get(key)
        if found is None:
            return None
        meta = PROXIES[key]
        fx = None
        if meta["currency"] != currency:
            if meta["currency"] not in fx_cache:
                rates = provider.fx(meta["currency"], currency, window, last, budget=budget)
                fx_cache[meta["currency"]] = rates["rates"] or None
                if not rates["rates"]:
                    missing.append({"key": f"benchmarks.fx.{meta['currency']}/{currency}",
                                    "reason": (rates["missing"] or [{}])[0].get("reason")})
            fx = fx_cache[meta["currency"]]
            if fx is None:
                return None
        series = _convert_series(found["points"], fx)
        name = f"{meta['index']} (proxy: {meta['symbol']} ETF total return in {currency})"
        proxies.append({"proxy": key, "symbol": meta["symbol"], "vehicle": meta["vehicle"], "index": meta["index"],
                        "currency": currency, "converted_from": meta["currency"] if fx else None,
                        "source": found["source"], "first": found["first"], "last": found["last"]})
        return {"name": name, "series": series, "proxy": True, "source": found["source"]}

    ips_specs: dict[str, dict] = {}
    for sleeve in sleeves:
        proxy = needed.get(sleeve["id"])
        if proxy == "cetes":
            rate = _dec((cetes_rate or {}).get("rate"))
            if rate is not None and (cetes_rate or {}).get("source"):
                ips_specs[sleeve["id"]] = {"name": "CETES 28 days (proxy: accrual at the published rate)",
                                           "annual_rate": str(rate), "source": cetes_rate["source"], "proxy": True}
                proxies.append({"proxy": "cetes", "rate": str(rate), "source": cetes_rate["source"]})
            else:
                missing.append({"key": f"benchmarks.ips.{sleeve['id']}",
                                "reason": "CETES 28-day rate unknown: no published rate was supplied, so this sleeve's "
                                          "benchmark is missing (not assumed)."})
        elif proxy:
            made = spec(proxy)
            if made:
                ips_specs[sleeve["id"]] = made
        else:
            missing.append({"key": f"benchmarks.ips.{sleeve['id']}", "reason": "no proxy for this sleeve"})
    result: dict[str, Any] = {}
    if ips_specs:
        result["ips"] = ips_specs
    equity, bonds = spec("global_equity"), spec("global_bonds")
    if equity or bonds:
        result["reference_60_40"] = {k: v for k, v in (("equity", equity), ("bonds", bonds)) if v}
    seen, unique = set(), []
    for proxy in proxies:
        marker = (proxy["proxy"], proxy.get("symbol"))
        if marker not in seen:
            seen.add(marker)
            unique.append(proxy)
    return {"benchmarks": result, "proxies": unique, "missing": missing, "pending": got["pending"]}


# ------------------------------------------------------------------ proactive overlay


def repriced_snapshot(snapshot: Mapping[str, Any], sit: Mapping[str, Any]) -> tuple[dict, dict]:
    """Snapshot and situation copies whose saved positions include ledger accounts valued at provider prices.

    Proactive rules (harvest, drift, concentration) read positions from saved facts; a ledger-only
    account has none.  Each valued ledger account becomes a transient ``account.ledger.<id>`` fact
    (source kind ``tool``, never saved) dated at its oldest price.
    """
    facts = list(snapshot.get("facts") or [])
    meta = dict(sit.get("meta") or {})
    for account in sit.get("accounts") or []:
        detail = account.get("priced_positions")
        if account.get("source") != "ledger" or account.get("value") is None or not detail:
            continue
        key = f"account.ledger.{account['id']}"
        as_of = account.get("as_of") or sit.get("as_of")
        facts.append({"id": f"prices:{account['id']}", "key": key, "confidence": "reported", "status": "active",
                      "source": {"kind": "tool", "ref": "wealth://prices", "observed_on": as_of},
                      "value": {"account": {"id": account["id"], "type": account.get("type"),
                                            "institution": account.get("institution")},
                                "as_of": as_of, "positions": detail}})
        meta[key] = {"stale": False, "inferred": False, "observed_on": as_of}
    return {**snapshot, "facts": facts}, {**sit, "meta": meta}


__all__ = ["Fetched", "PROXIES", "PriceProvider", "benchmarks", "fx_rows", "fx_symbol", "instrument_specs",
           "is_mx_fixed_income", "ledger_market", "ledger_series", "market_open", "offline_mode", "provider_symbol",
           "repriced_snapshot", "with_fx", "yahoo_fetcher"]
