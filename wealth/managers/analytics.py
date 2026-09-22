"""Manager profile over quarters (turnover, concentration, splits, sectors), comparison and backtest."""
from __future__ import annotations

import json
import math
import statistics
from typing import Any, Callable, Iterable, Mapping

from .._common import envelope
from .edgar import (
    Edgar, MIN_CONFIDENCE, ManagerDataError, OPTIONS_NOTE, SEC_UA_ENV, SUBMISSIONS_URL, TransportError,
    UserAgentRequired, _DEFAULT, _date, _r, _today, normalize_cik,
)
from .positions import (
    _apply_mapping, _client, _lag, _load_quarters, _meta, _needs, _norm_name, _pct, _quarter_view, _sources,
    _ticker_index, map_cusips,
)
from .sleeve import target_weights


# ------------------------------------------------------------------ profile

SECTORS = (
    ((3570, 3579), "Technology"), ((3660, 3699), "Technology"), ((3670, 3679), "Technology"),
    ((7370, 7379), "Technology"), ((3820, 3829), "Technology"),
    ((2830, 2836), "Health care"), ((3840, 3851), "Health care"), ((8000, 8099), "Health care"),
    ((6321, 6324), "Health care"),
    ((6000, 6499), "Financials"), ((6700, 6797), "Financials"), ((6798, 6798), "Real estate"),
    ((6500, 6599), "Real estate"), ((4900, 4991), "Utilities"), ((4800, 4899), "Communication"),
    ((7800, 7999), "Communication"), ((2700, 2799), "Communication"),
    ((1300, 1399), "Energy"), ((2900, 2999), "Energy"),
    ((1000, 1299), "Materials"), ((1400, 1499), "Materials"), ((2800, 2829), "Materials"),
    ((2840, 2899), "Materials"), ((3300, 3399), "Materials"),
    ((2000, 2199), "Consumer staples"), ((5400, 5499), "Consumer staples"),
    ((3710, 3716), "Consumer discretionary"), ((5000, 5399), "Consumer discretionary"),
    ((5500, 5999), "Consumer discretionary"), ((7000, 7299), "Consumer discretionary"),
    ((3720, 3729), "Industrials"), ((3760, 3769), "Industrials"), ((3600, 3659), "Industrials"),
    ((3400, 3569), "Industrials"), ((3580, 3599), "Industrials"), ((4000, 4799), "Industrials"),
    ((1500, 1799), "Industrials"), ((8700, 8799), "Industrials"),
)


def sector_for_sic(sic: Any) -> str | None:
    """A broad sector from a SEC SIC code (first matching range); None when unknown."""
    try:
        code = int(str(sic))
    except (TypeError, ValueError):
        return None
    for (low, high), name in SECTORS:
        if low <= code <= high:
            return name
    if 2000 <= code <= 3999:
        return "Industrials"
    return "Other"


def _sectors(client: Edgar, tickers: Iterable[str]) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """Ticker -> {sector, sic, sic_description, state_of_incorporation} from issuer submissions."""
    index = _ticker_index(client)
    out: dict[str, dict[str, Any]] = {}
    missing = []
    for ticker in sorted(set(t for t in tickers if t)):
        entry = index.by_ticker.get(ticker.upper()) if index else None
        if entry is None:
            missing.append(ticker)
            continue
        try:
            sub = client.json(SUBMISSIONS_URL.format(cik=entry["cik"]), "issuer")
        except (TransportError, ManagerDataError):
            missing.append(ticker)
            continue
        out[ticker] = {"sector": sector_for_sic(sub.get("sic")), "sic": sub.get("sic"),
                       "sic_description": sub.get("sicDescription"),
                       "state_of_incorporation": sub.get("stateOfIncorporation")}
    return out, missing


_SPLITS = (2, 3, 4, 5, 8, 10, 15, 20, 25, 40, 50)


_CLEAN_SPLITS = (2, 3, 4, 5, 10)
SPLIT_VALUE_TOLERANCE = 0.25   # offline fallback: a clean ratio with the position value within +/-25%

# ticker -> [(ex-date ISO, share ratio new/old), ...], or None when the history is not available (offline).
SplitLookup = Callable[[str], "list[tuple[str, float]] | None"]


def _split_ratio(old_shares: float, new_shares: float, old_price: float | None, new_price: float | None) -> float | None:
    """A share-count change that matches a common split ratio with the price moving inversely.

    Only a candidate: doubling a position while the price halves looks the same, so ``_split_evidence`` decides.
    """
    if not old_shares or not new_shares or not old_price or not new_price:
        return None
    ratio = new_shares / old_shares
    for k in _SPLITS:
        for r in (k, 1 / k):
            if abs(ratio / r - 1) < 0.005 and _inverse_price(old_price, new_price, r):
                return r
    return None


def _maybe_split(old_shares: float, new_shares: float, old_price: float | None, new_price: float | None) -> bool:
    """Worth checking a split history: shares within 25% of a split ratio (a split plus a trade) with the
    price moving inversely."""
    if not old_shares or not new_shares or not old_price or not new_price:
        return False
    ratio = new_shares / old_shares
    return any(abs(ratio / r - 1) < 0.25 and _inverse_price(old_price, new_price, r)
               for k in (1.5,) + _SPLITS for r in (k, 1 / k))


def _inverse_price(old_price: float | None, new_price: float | None, ratio: float) -> bool:
    return bool(old_price and new_price) and 0.6 < (new_price * ratio / old_price) < 1.6


def _clean_multiple(old_shares: float, new_shares: float, ratio: float) -> bool:
    """New shares are exactly the old shares times the ratio (a reverse split may round down a fraction)."""
    expected = old_shares * ratio
    return abs(new_shares - expected) <= max(1.0, 1e-4 * expected)


def _line_price(line: Mapping[str, Any] | None) -> float | None:
    return line["value"] / float(line["shares"]) if line and line.get("shares") else None


def _split_evidence(old: Mapping[str, Any], new: Mapping[str, Any],
                    peers: list[tuple[Mapping[str, Any], Mapping[str, Any]]],
                    history: list[tuple[str, float]] | None, start: str, end: str
                    ) -> tuple[float | None, str, float | None]:
    """(split ratio or None, basis, uncorroborated candidate ratio).

    With the ticker's split history, the splits dated in (start, end] adjust the shares (so a trade on top of
    a split is measured on split-adjusted shares) and no split there means a trade.  Without it (offline), a
    split is accepted when (a) at least two of the issuer's lines (this one, other share classes, options on
    its CUSIPs), and most of them, show the same share ratio with this line's price moving inversely; or (b)
    the share ratio is a clean 2, 3, 4, 5 or 10 (or its inverse) and the value moved at most
    SPLIT_VALUE_TOLERANCE.  Anything else is a trade.
    """
    if history is not None:
        factor = 1.0
        for day, ratio in history:
            if start < day <= end and ratio and ratio > 0:
                factor *= ratio
        return (factor, "split history", None) if abs(factor - 1) > 1e-9 else (None, "", None)
    old_shares, new_shares = float(old["shares"]), float(new["shares"])
    old_price, new_price = _line_price(old), _line_price(new)
    candidate = _split_ratio(old_shares, new_shares, old_price, new_price)
    lines = [candidate] + [_split_ratio(float(a["shares"]), float(b["shares"]), _line_price(a), _line_price(b))
                           for a, b in peers]
    agreed = [r for r in lines if r]
    if agreed:
        ratio = max(set(agreed), key=agreed.count)
        n = agreed.count(ratio)
        if n >= 2 and n * 2 > len(lines) and _inverse_price(old_price, new_price, ratio):
            return ratio, f"no split history; {n} of the issuer's {len(lines)} lines show the same split", None
    if candidate is None:
        return None, "", None
    value_change = new["value"] / old["value"] - 1 if old["value"] else None
    clean = any(abs(candidate - r) < 1e-9 for k in _CLEAN_SPLITS for r in (k, 1 / k))
    if (clean and _clean_multiple(old_shares, new_shares, candidate) and value_change is not None
            and abs(value_change) <= SPLIT_VALUE_TOLERANCE):
        return candidate, "no split history; clean share ratio with the value within 25%", None
    return None, "", candidate


def yahoo_split_history(ticker: str) -> list[tuple[str, float]]:
    """A ticker's split history from Yahoo via yfinance (the library prices.py wraps); raises on failure."""
    import pandas as pd
    import yfinance as yf

    from ..prices import provider_symbol

    symbol, reason = provider_symbol(ticker, venue="us")
    if symbol is None:
        raise LookupError(f"{ticker}: {reason}")
    series = yf.Ticker(symbol).splits
    return [(pd.Timestamp(day).date().isoformat(), float(ratio)) for day, ratio in series.items()
            if math.isfinite(float(ratio)) and float(ratio) > 0]


def split_lookup(client: Edgar, fetch: Callable[[str], list[tuple[str, float]]] | None = None,
                 *, limit: int = 40) -> SplitLookup | None:
    """A cached :data:`SplitLookup`, or None when offline (``WEALTH_OFFLINE`` or a recorded-snapshot client).

    Histories are kept in the client's disk cache for a week; a failed lookup answers None, so the offline
    rules apply to that ticker.  At most ``limit`` tickers are fetched per profile.
    """
    from ..prices import offline_mode

    if client.offline or offline_mode():
        return None
    fetch = fetch or yahoo_split_history
    fetched = [0]

    def lookup(ticker: str) -> list[tuple[str, float]] | None:
        cache_key = "splits:" + ticker.upper()
        if client.memory.get(cache_key) == "null":   # failed earlier in this session
            return None
        cached = client._cached("splits", cache_key)
        if cached is not None:
            return [(d, float(r)) for d, r in json.loads(cached)]
        if fetched[0] >= limit:
            return None
        fetched[0] += 1
        try:
            history = [(str(d), float(r)) for d, r in fetch(ticker)]
        except Exception as exc:  # noqa: BLE001 - no history means the offline rules, never a failed profile
            client.warnings.append(f"Split history for {ticker} was not available ({type(exc).__name__}).")
            client.memory[cache_key] = "null"
            return None
        client._store("splits", cache_key, json.dumps(history))
        return history

    return lookup


def _quarter_number(period: str) -> int:
    d = _date(period)
    return d.year * 4 + (d.month - 1) // 3 if d else 0


def _same_security(old: Mapping[str, Any], new: Mapping[str, Any]) -> str | None:
    """How two lines with different CUSIPs are known to be the same security (FIGI, then a confident ticker)."""
    if old.get("figi") and old.get("figi") == new.get("figi"):
        return "figi"
    if (old.get("ticker") and old.get("ticker") == new.get("ticker")
            and (old.get("ticker_confidence") or 0) >= MIN_CONFIDENCE
            and (new.get("ticker_confidence") or 0) >= MIN_CONFIDENCE):
        return "ticker"
    return None


def _concentration(equity: list[dict[str, Any]]) -> dict[str, Any]:
    weights = sorted((p["weight"] or 0 for p in equity), reverse=True)
    hhi = sum(w * w for w in weights)
    return {"positions": len(weights), "top5": _r(sum(weights[:5]), 4), "top10": _r(sum(weights[:10]), 4),
            "hhi": _r(hhi, 4), "effective_positions": _r(1 / hhi, 1) if hhi else None,
            "largest": _r(weights[0], 4) if weights else None}


def profile_from_history(quarters: list[Mapping[str, Any]], *, sectors: Mapping[str, Any] | None = None,
                         manager: Mapping[str, Any] | None = None,
                         split_history: SplitLookup | None = None) -> dict[str, Any]:
    """Interpretation from consecutive quarters (oldest first); each has period, filing_date and ``book``.

    ``split_history`` returns a ticker's splits (see :data:`SplitLookup`); it is asked only about lines whose
    share and price changes could be a split.  Without it, or when it returns None, the offline rules apply.
    """
    quarters = [q for q in quarters if not q.get("notice")]
    quarters.sort(key=lambda q: q["period"])
    sectors = sectors or {}
    series = []
    for q in quarters:
        book = q["book"]
        total = book["total"] or 0
        conc = _concentration(book["equity"])
        sector_weights: dict[str, float] = {}
        for p in book["equity"]:
            label = (sectors.get(p.get("ticker")) or {}).get("sector") or "Unknown"
            sector_weights[label] = sector_weights.get(label, 0.0) + (p["weight"] or 0)
        series.append({"period": q["period"], "filing_date": q["filing_date"], "long_equity_value": book["equity_total"],
                       "total_reported_value": total, **conc,
                       "calls_share": _r(book["calls"] / total, 4) if total else None,
                       "puts_share": _r(book["puts"] / total, 4) if total else None,
                       "sectors": {k: _r(v, 4) for k, v in sorted(sector_weights.items(), key=lambda kv: -kv[1])}})
    # A CUSIP that changes (reorganisation, redomicile) is followed through FIGI or a confident ticker; without
    # either, an exit and a new line with the same issuer name become a caveat.
    canon: dict[str, str] = {}
    identifier_changes, caveats = [], []

    def key(line: Mapping[str, Any]) -> str:
        return canon.get(line["cusip"], line["cusip"])

    for i in range(1, len(quarters)):
        before = {key(p): p for p in quarters[i - 1]["book"]["equity"]}
        current = quarters[i]["book"]["equity"]
        gone = [k for k in before if k not in {key(p) for p in current}]
        for p in current:
            if key(p) in before:
                continue
            match = next(((k, how) for k in gone if (how := _same_security(before[k], p))), None)
            if match:
                canon[p["cusip"]] = match[0]
                gone.remove(match[0])
                identifier_changes.append({"period": quarters[i]["period"], "issuer": p["issuer"],
                                           "old_cusip": before[match[0]]["cusip"], "new_cusip": p["cusip"],
                                           "matched_by": match[1]})
            elif any(_norm_name(before[k]["issuer"]) == _norm_name(p["issuer"]) for k in gone):
                caveats.append(f"{p['issuer']} appears under a new CUSIP in {quarters[i]['period']} with no FIGI or "
                               "confident ticker to link it; it is counted as a sale and a new purchase.")
    transitions = []
    entries: dict[str, dict[str, Any]] = {}   # key -> entry info for names that appear inside the window
    splits, possible_splits, fallback_splits = [], [], []
    split_factor: dict[tuple[str, int], float] = {}   # (key, transition index) -> share ratio of a split
    numbers = [_quarter_number(q["period"]) for q in quarters]
    for i in range(1, len(quarters)):
        before = {key(p): p for p in quarters[i - 1]["book"]["equity"]}
        after = {key(p): p for p in quarters[i]["book"]["equity"]}
        options_before = {(key(o), o["put_call"]): o for o in quarters[i - 1]["book"]["options"]}
        options_after = {(key(o), o["put_call"]): o for o in quarters[i]["book"]["options"]}
        buys = sells = new_value = add_value = 0.0
        new_names, adds, trims, exits = [], 0, 0, 0
        for k in sorted(set(before) | set(after)):
            old, new = before.get(k), after.get(k)
            old_shares = float(old["shares"]) if old else 0.0
            new_shares = float(new["shares"]) if new else 0.0
            old_price = old["value"] / old_shares if old and old_shares else None
            new_price = new["value"] / new_shares if new and new_shares else None
            price = new_price or old_price or 0.0
            if old and new:
                # The issuer's other lines: its other share classes and options on any of its CUSIPs.
                name = _norm_name(new["issuer"])
                family = {k} | {c for c in before if c in after and name and _norm_name(after[c]["issuer"]) == name}
                peers = [(before[c], after[c]) for c in sorted(family - {k})]
                peers += [(options_before[o], options_after[o]) for o in sorted(options_before)
                          if o[0] in family and o in options_after]
                history = None
                ticker = new.get("ticker") or old.get("ticker")
                if (split_history is not None and ticker and (new.get("ticker_confidence") or 0) >= MIN_CONFIDENCE
                        and _maybe_split(old_shares, new_shares, old_price, new_price)):
                    history = split_history(ticker)
                ratio, basis, candidate = _split_evidence(old, new, peers, history, quarters[i - 1]["period"],
                                                          quarters[i]["period"])
                if ratio and history is None:
                    fallback_splits.append(f"{new['issuer']} ({quarters[i]['period']}, {_r(ratio, 4):g}-for-1)")
                if ratio:
                    old_shares *= ratio
                    split_factor[(k, i)] = ratio
                    splits.append({"period": quarters[i]["period"], "cusip": new["cusip"], "issuer": new["issuer"],
                                   "ratio": _r(ratio, 4), "basis": basis})
                    if _clean_multiple(old_shares, new_shares, 1.0):
                        continue
                elif candidate:
                    possible_splits.append({"period": quarters[i]["period"], "cusip": new["cusip"],
                                            "issuer": new["issuer"], "ratio": _r(candidate, 4)})
            change = new_shares - old_shares
            if change > 0:
                buys += change * price
                if old:
                    adds += 1
                    add_value += change * price
                else:
                    new_value += change * price
                    new_names.append(new)
                    entries[k] = {"period_index": i, "weight": new["weight"], "shares": new_shares,
                                  "issuer": new["issuer"], "ticker": new.get("ticker")}
            elif change < 0:
                sells += -change * price
                if new:
                    trims += 1
                else:
                    exits += 1
        average = (quarters[i - 1]["book"]["equity_total"] + quarters[i]["book"]["equity_total"]) / 2
        elapsed = max(1, numbers[i] - numbers[i - 1])
        turnover = min(buys, sells) / average if average else None
        transitions.append({
            "from": quarters[i - 1]["period"], "to": quarters[i]["period"], "quarters_elapsed": elapsed,
            "buys_estimate": int(buys), "sells_estimate": int(sells),
            "turnover": _r(turnover, 4) if turnover is not None else None,
            "turnover_per_quarter": _r(turnover / elapsed, 4) if turnover is not None else None,
            "new_positions": len(new_names), "added_to": adds, "trimmed": trims, "exited": exits,
            "buying_into_existing_share": _r(add_value / (add_value + new_value), 4) if add_value + new_value else None,
            "new_position_weights": [_r(p["weight"], 4) for p in sorted(new_names, key=lambda p: -(p["weight"] or 0))],
        })
    # Each transition's turnover is spread over the quarters it spans (a missed filing or a 13F-NT leaves a gap),
    # so the quarterly rate is total turnover / total quarters elapsed.
    counted = [(float(t["turnover"]), t["quarters_elapsed"]) for t in transitions if t["turnover"] is not None]
    quarterly = sum(v for v, _ in counted) / sum(n for _, n in counted) if counted else None
    annual = quarterly * 4 if quarterly is not None else None
    gaps = [t for t in transitions if t["quarters_elapsed"] > 1]
    if gaps:
        caveats.append("Quarters are missing between " + ", ".join(f"{t['from']} and {t['to']}" for t in gaps) +
                       " (a missed filing, a 13F notice or an incomplete quarter); turnover there is spread over the "
                       "quarters elapsed, and trades inside the gap are invisible.")
    if fallback_splits:
        caveats.append("No split history was available for " + ", ".join(fallback_splits) + "; the share change "
                       "is treated as a split from its clean ratio, which a trade of the same size would mimic.")
    if possible_splits:
        caveats.append("Share counts of " + ", ".join(f"{x['issuer']} ({x['period']})" for x in possible_splits) +
                       " changed by a common split ratio, but nothing corroborates a split, so they are counted as "
                       "trades.")
    # holding periods: runs of consecutive quarters per CUSIP
    presence: dict[str, list[int]] = {}
    for i, q in enumerate(quarters):
        for p in q["book"]["equity"]:
            presence.setdefault(key(p), []).append(i)
    runs, completed = [], []
    for idx in presence.values():
        start = prev = idx[0]
        for j in idx[1:] + [None]:
            if j is not None and j == prev + 1:
                prev = j
                continue
            length = numbers[prev] - numbers[start] + 1   # calendar quarters, across any gap in the filings
            runs.append(length)
            if start > 0 and prev < len(quarters) - 1:
                completed.append(length)
            if j is not None:
                start = prev = j
    # build-up of names first bought inside the window
    build = []
    for k, info in entries.items():
        later = [q["book"]["equity"] for q in quarters[info["period_index"] + 1:]]
        path = [next((p for p in book if key(p) == k), None) for book in later]
        held = []
        for p in path:
            if p is None:
                break
            held.append(p)
        if not held:
            continue
        # Entry shares restated in the last held quarter's terms, so a split is not read as buying more.
        adjusted = info["shares"]
        for j in range(info["period_index"] + 1, info["period_index"] + len(held) + 1):
            adjusted *= split_factor.get((k, j), 1.0)
        peak = max(held, key=lambda p: p["weight"] or 0)
        build.append({"issuer": info["issuer"], "ticker": info["ticker"], "entry_weight": _r(info["weight"], 4),
                      "peak_weight": _r(peak["weight"], 4),
                      "quarters_to_peak": held.index(peak) + 1 if (peak["weight"] or 0) > (info["weight"] or 0) else 0,
                      "built_up": float(held[-1]["shares"]) > adjusted * 1.1})
    initial = [w for t in transitions for w in t["new_position_weights"] if w is not None]
    first, last = (series[0], series[-1]) if series else ({}, {})
    drift = None
    if len(series) >= 2:
        labels = set(first["sectors"]) | set(last["sectors"])
        drift = _r(0.5 * sum(abs((last["sectors"].get(k) or 0) - (first["sectors"].get(k) or 0)) for k in labels), 4)
    known = [s for s in series if s["sectors"] and set(s["sectors"]) != {"Unknown"}]
    result = {
        "manager": dict(manager or {}),
        "quarters": len(quarters),
        "span_quarters": numbers[-1] - numbers[0] + 1 if quarters else 0,
        "periods": [q["period"] for q in quarters],
        "turnover": {
            "per_quarter": transitions,
            "average_quarterly": _r(quarterly, 4), "annualised": _r(annual, 4),
            "method": "min(buys, sells) / average long-equity value between two filings, with buys and sells "
                      "estimated as share changes x the quarter-end price (value / shares); the sum over "
                      "transitions / the calendar quarters they span, x 4.",
            "estimate": True,
            "caveat": "An estimate: a 13F shows only quarter-end snapshots, so trades within a quarter, and "
                      "round trips between filings, are invisible and real turnover is usually higher.",
            "caveats": caveats,
            "splits_ignored": splits,
            "possible_splits_counted_as_trades": possible_splits,
            "identifier_changes": identifier_changes,
        },
        "holding_period": {
            "average_quarters": _r(statistics.fmean(runs), 2) if runs else None,
            "median_quarters": _r(float(statistics.median(runs)), 2) if runs else None,
            "completed_average_quarters": _r(statistics.fmean(completed), 2) if completed else None,
            "implied_years": _r(1 / annual, 2) if annual else None,
            "note": f"Counted in calendar quarters inside a {numbers[-1] - numbers[0] + 1 if quarters else 0}-quarter "
                    "window, so positions held before or after it are cut short (censored); a position filed on "
                    "both sides of a missing quarter counts as held through it; implied years = 1 / annual turnover.",
        },
        "concentration": {
            "by_quarter": [{k: s[k] for k in ("period", "positions", "top5", "top10", "hhi", "effective_positions",
                                              "largest")} for s in series],
            "latest": {k: last.get(k) for k in ("positions", "top5", "top10", "hhi", "effective_positions", "largest")},
            "trend": {k: _r((last.get(k) or 0) - (first.get(k) or 0), 4) if len(series) >= 2 else None
                      for k in ("positions", "top10", "effective_positions")},
        },
        "conviction": {
            "new_positions": sum(t["new_positions"] for t in transitions),
            "added_to_existing": sum(t["added_to"] for t in transitions),
            "buying_into_existing_share": _r(
                statistics.fmean([t["buying_into_existing_share"] for t in transitions
                                  if t["buying_into_existing_share"] is not None]), 4)
            if any(t["buying_into_existing_share"] is not None for t in transitions) else None,
            "typical_initial_weight": _r(statistics.median(initial), 4) if initial else None,
            "average_initial_weight": _r(statistics.fmean(initial), 4) if initial else None,
            "build_up": {"names": build,
                         "share_built_up": _r(sum(b["built_up"] for b in build) / len(build), 4) if build else None,
                         "median_peak_to_entry": _r(statistics.median([b["peak_weight"] / b["entry_weight"]
                                                                       for b in build if b["entry_weight"]]), 2)
                         if any(b["entry_weight"] for b in build) else None},
        },
        "sectors": {
            "by_quarter": [{"period": s["period"], "weights": s["sectors"]} for s in series],
            "drift_first_to_last": drift,
            "method": "Issuer SIC code from EDGAR submissions mapped to broad sectors; Unknown when the ticker or "
                      "SIC code is not available.",
            "coverage": _r(1 - (last.get("sectors") or {}).get("Unknown", 0), 4) if last else None,
        },
        "options": {
            "by_quarter": [{"period": s["period"], "calls_share": s["calls_share"], "puts_share": s["puts_share"]}
                           for s in series],
            "average_calls_share": _r(statistics.fmean([s["calls_share"] or 0 for s in series]), 4) if series else None,
            "average_puts_share": _r(statistics.fmean([s["puts_share"] or 0 for s in series]), 4) if series else None,
            "note": OPTIONS_NOTE,
        },
        "style_drift": {
            "top10_change": _r((last.get("top10") or 0) - (first.get("top10") or 0), 4) if len(series) >= 2 else None,
            "positions_change": (last.get("positions") or 0) - (first.get("positions") or 0) if len(series) >= 2 else None,
            "sector_drift": drift,
        },
        "sector_data_available": bool(known),
    }
    result["character"] = character(result)
    return _meta(result)


def _turnover_word(annual: float | None) -> tuple[str, str] | None:
    if annual is None:
        return None
    if annual < 0.25:
        return "long-term", "de largo plazo"
    if annual < 0.6:
        return "patient", "paciente"
    if annual < 1.2:
        return "active", "activo"
    return "fast-trading", "de rotación muy alta"


def _concentration_word(latest: Mapping[str, Any]) -> tuple[str, str] | None:
    top10, eff = latest.get("top10"), latest.get("effective_positions")
    if top10 is None:
        return None
    if top10 >= 0.6 or (eff is not None and eff < 12):
        return "concentrated", "concentrado"
    if top10 < 0.35:
        return "diversified", "diversificado"
    return "moderately concentrated", "moderadamente concentrado"


def _a(word: str) -> str:
    return "An" if word[:1].lower() in "aeiou" else "A"


def _round_to(value: float, step: int) -> int:
    return int(step * round(value / step))


def character(profile: Mapping[str, Any]) -> dict[str, str]:
    """A one-paragraph read of the numbers, in English and Mexican Spanish."""
    turnover = profile["turnover"]["annualised"]
    latest = profile["concentration"]["latest"]
    words = _turnover_word(turnover)
    conc = _concentration_word(latest)
    if words and conc:
        head_en, head_es = f"{_a(words[0])} {words[0]}, {conc[0]} investor", f"Un inversionista {words[1]} y {conc[1]}"
    elif conc:
        head_en, head_es = f"{_a(conc[0])} {conc[0]} investor", f"Un inversionista {conc[1]}"
    else:
        head_en, head_es = "A manager", "Un administrador"
    facts_en, facts_es = [], []
    if turnover is not None and turnover < 0.05:
        facts_en.append("under 5% turnover a year")
        facts_es.append("rotación menor a 5% al año")
    elif turnover is not None:
        pct = _round_to(turnover * 100, 5)
        facts_en.append(f"about {pct}% turnover a year")
        facts_es.append(f"rotación de alrededor de {pct}% al año")
    if latest.get("positions") is not None:
        facts_en.append(f"{latest['positions']} holdings")
        facts_es.append(f"{latest['positions']} posiciones")
    if latest.get("top10") is not None:
        top = _round_to(latest["top10"] * 100, 5)
        facts_en.append(f"top 10 about {top}%")
        facts_es.append(f"las 10 mayores suman cerca de {top}%")
    en = head_en + (": " + ", ".join(facts_en) if facts_en else "") + "."
    es = head_es + (": " + ", ".join(facts_es) if facts_es else "") + "."
    hp = profile["holding_period"]
    if hp.get("median_quarters") is not None:
        window = profile.get("span_quarters") or profile["quarters"]
        en += f" A position is typically held about {hp['median_quarters']:g} quarters within the " \
              f"{window}-quarter window"
        es += f" Una posición suele mantenerse unos {hp['median_quarters']:g} trimestres dentro de la ventana de " \
              f"{window} trimestres"
        if hp.get("implied_years") and hp["implied_years"] > 30:
            en += " (turnover implies holding for decades)"
            es += " (la rotación implica mantenerlas por décadas)"
        elif hp.get("implied_years"):
            en += f" (turnover implies about {hp['implied_years']:g} years)"
            es += f" (la rotación implica unos {hp['implied_years']:g} años)"
        en += "."
        es += "."
    conv = profile["conviction"]
    if conv.get("typical_initial_weight") is not None:
        en += f" New names typically start near {_pct(conv['typical_initial_weight'])} of the book"
        es += f" Las posiciones nuevas suelen empezar cerca de {_pct(conv['typical_initial_weight'])} del portafolio"
        share = conv["build_up"].get("share_built_up")
        if share is not None:
            en += f", and {_pct(share)} of them were added to in later quarters"
            es += f", y a {_pct(share)} de ellas se les agregó en trimestres siguientes"
        en += "."
        es += "."
    sectors = (profile["sectors"]["by_quarter"] or [{}])[-1].get("weights") or {}
    top_sector = next(((k, v) for k, v in sectors.items() if k != "Unknown"), None)
    if top_sector and top_sector[1] >= 0.4:
        en += f" The book leans to {top_sector[0]} ({_pct(top_sector[1])})."
        es += f" El portafolio se inclina hacia {_SECTOR_ES.get(top_sector[0], top_sector[0])} ({_pct(top_sector[1])})."
    options = profile["options"]
    used = (options.get("average_calls_share") or 0) + (options.get("average_puts_share") or 0)
    if used >= 0.02:
        en += f" Options are a real part of it: about {_pct(used)} of reported value on average, at underlying value."
        es += (f" Las opciones pesan: cerca de {_pct(used)} del valor reportado en promedio, al valor del "
               "subyacente.")
    en += " This is read from quarter-end 13F snapshots, not the manager's full portfolio."
    es += " Esto se lee de fotos trimestrales del 13F, no del portafolio completo del administrador."
    return {"en": en, "es": es}


_SECTOR_ES = {"Technology": "tecnología", "Health care": "salud", "Financials": "finanzas", "Real estate": "bienes raíces",
              "Utilities": "servicios públicos", "Communication": "comunicaciones", "Energy": "energía",
              "Materials": "materiales", "Consumer staples": "consumo básico",
              "Consumer discretionary": "consumo discrecional", "Industrials": "industria", "Other": "otros"}


def _history(client: Edgar, cik: str, quarters: int, tickers: Mapping[str, str] | None,
             with_sectors: bool) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any], list[str],
                                          dict[str, list[str]]]:
    """(submissions, quarter views, sectors, warnings, flags).

    ``flags`` names the quarters that make a profile partial: ``unit_suspect`` (values in the other unit,
    rescaled when every table in the quarter points the same way, else left out) and ``incomplete`` (a NEW
    HOLDINGS amendment without its original, left out).
    """
    sub, loaded, rows = _load_quarters(client, cik, quarters)
    warnings = [w for q in loaded for w in q["warnings"]]
    flags: dict[str, list[str]] = {"unit_suspect": [], "incomplete": []}
    usable = []
    for q in loaded:
        if q["notice"]:
            continue
        if q.get("incomplete"):
            flags["incomplete"].append(q["period"])
            continue
        marks = q.get("unit_flags") or []
        if any(marks):
            flags["unit_suspect"].append(q["period"])
            if len(set(marks)) == 1:
                factor = 1000 if marks[0] == "thousands_as_dollars" else 0.001
                q = {**q, "rows": [{**r, "value": int(round(r["value"] * factor))} for r in q["rows"]],
                     "rescaled": factor}
                warnings.append(f"Values for {q['period']} were rescaled "
                                f"{'x1000 (read as thousands)' if factor == 1000 else '/1000 (read as dollars)'} "
                                "to match the other quarters before computing turnover.")
            else:
                warnings.append(f"{q['period']} mixes tables in different units and is left out of the profile.")
                continue
        usable.append(q)
    views = [_quarter_view(q) for q in usable]
    notices = [q["period"] for q in loaded if q["notice"]]
    if notices:
        warnings.append(f"Quarters {', '.join(notices)} are 13F notices (holdings reported by another manager) and "
                        "are left out.")
    items = [p for v in views for p in v["book"]["equity"] + v["book"]["options"]]
    mapping = map_cusips(client, items, tickers)
    for v in views:
        _apply_mapping([v["book"]["equity"], v["book"]["options"], v["book"]["other"]], mapping)
    sectors: dict[str, Any] = {}
    if with_sectors:
        sectors, missing = _sectors(client, {p.get("ticker") for v in views for p in v["book"]["equity"]
                                             if (p.get("ticker_confidence") or 0) >= MIN_CONFIDENCE})
        if missing:
            warnings.append(f"No sector for {len(missing)} tickers ({', '.join(missing[:8])}"
                            f"{'…' if len(missing) > 8 else ''}); they count as Unknown.")
    return sub, views, sectors, warnings, flags


def profile(cik: Any, quarters: int = 8, *, client: Edgar | None = None, tickers: Mapping[str, str] | None = None,
            sectors: bool = True, as_of: Any = None, splits: Any = _DEFAULT) -> dict[str, Any]:
    """How a manager invests, from up to ``quarters`` consecutive 13F filings.

    ``splits`` is a :data:`SplitLookup`; by default Yahoo split histories via :func:`split_lookup` (none when
    offline), ``None`` for the offline rules only.
    """
    cik = normalize_cik(cik)
    if isinstance(quarters, bool) or not isinstance(quarters, int) or not 2 <= quarters <= 40:
        raise ValueError("quarters must be 2-40")
    client = _client(client)
    try:
        sub, views, sector_map, warnings, flags = _history(client, cik, quarters, tickers, sectors)
    except UserAgentRequired as exc:
        return _needs(str(exc), SEC_UA_ENV)
    except TransportError as exc:
        return _needs(f"EDGAR could not be reached: {exc}", "EDGAR access (network)", client)
    except ManagerDataError as exc:
        return _needs(str(exc), "a manager with 13F holdings", client)
    if not views:
        return envelope("needs_input", _meta({"manager": {"cik": cik, "name": sub.get("name")}}),
                        missing=["a manager that files Form 13F holdings"],
                        warnings=warnings + [f"{sub.get('name') or cik} has no 13F holdings reports on EDGAR."],
                        sources=_sources(client))
    lookup = split_lookup(client) if splits is _DEFAULT else splits
    result = profile_from_history(views, sectors=sector_map, manager={"cik": cik, "name": sub.get("name")},
                                  split_history=lookup)
    result["lag"] = _lag(views[0], _today(as_of))
    if len(views) < 2:
        warnings.append("Only one quarter is available: turnover, holding period and drift need at least two.")
    if len(views) < quarters:
        warnings.append(f"{len(views)} of the {quarters} quarters asked for are available.")
    warnings += result["turnover"]["caveats"]
    status = ("ready" if len(views) >= 2 and result["sector_data_available"]
              and not flags["unit_suspect"] and not flags["incomplete"] else "partial")
    return envelope(status, result, warnings=warnings + client.warnings, sources=_sources(client),
                    assumptions=["Turnover and holding period are estimates from quarter-end snapshots."])


def compare(ciks: Iterable[Any], quarters: int = 8, *, client: Edgar | None = None,
            tickers: Mapping[str, str] | None = None, sectors: bool = True, as_of: Any = None) -> dict[str, Any]:
    """Profiles side by side."""
    ciks = [normalize_cik(c) for c in ciks]
    if not 2 <= len(ciks) <= 6 or len(set(ciks)) != len(ciks):
        raise ValueError("compare needs 2-6 different CIKs")
    client = _client(client)
    profiles, warnings, missing = [], [], []
    for cik in ciks:
        report = profile(cik, quarters, client=client, tickers=tickers, sectors=sectors, as_of=as_of)
        if report["status"] == "needs_input":
            missing += report["missing"]
            warnings += report["warnings"]
            if SEC_UA_ENV in report["missing"]:
                return report
            continue
        profiles.append(report["result"])
        warnings += [f"{report['result']['manager'].get('name') or cik}: {w}" for w in report["warnings"]]
    rows = []
    for p in profiles:
        conc = p["concentration"]["latest"]
        rows.append({"cik": p["manager"]["cik"], "name": p["manager"].get("name"), "quarters": p["quarters"],
                     "latest_period": p["periods"][-1] if p["periods"] else None,
                     "annual_turnover": p["turnover"]["annualised"],
                     "median_quarters_held": p["holding_period"]["median_quarters"],
                     "implied_holding_years": p["holding_period"]["implied_years"],
                     "positions": conc.get("positions"), "top10": conc.get("top10"),
                     "effective_positions": conc.get("effective_positions"),
                     "typical_initial_weight": p["conviction"]["typical_initial_weight"],
                     "options_share": _r((p["options"]["average_calls_share"] or 0) +
                                         (p["options"]["average_puts_share"] or 0), 4),
                     "sector_drift": p["sectors"]["drift_first_to_last"],
                     "character": p["character"]})
    if len(rows) < 2:
        return envelope("needs_input", _meta({"managers": rows}), missing=missing or ["two managers with 13F holdings"],
                        warnings=warnings, sources=_sources(client))
    result = _meta({"managers": rows, "profiles": profiles,
                    "note": "Figures come from each manager's own 13F window; compare like with like (same quarters)."})
    return envelope("partial" if missing else "ready", result, missing=missing, warnings=warnings + client.warnings,
                    sources=_sources(client))


# ------------------------------------------------------------------ backtest


def backtest_from_history(quarters: list[Mapping[str, Any]], prices: Any, *, rules: Mapping[str, Any] | None = None,
                          benchmark: str | None = None, end: Any = None) -> dict[str, Any]:
    """Replay 'buy the 13F weights on its filing date, hold until the next filing'.

    ``quarters`` oldest first with ``filing_date`` and ``book`` (tickers mapped); ``prices`` a
    pandas DataFrame of daily closes indexed by date.
    """
    import pandas as pd

    rules = dict(rules or {})
    ordered = sorted((q for q in quarters if not q.get("notice")), key=lambda q: q["filing_date"])
    px = prices.sort_index()
    last_date = px.index.max()
    end_date = pd.Timestamp(end) if end is not None else last_date

    def price_at(symbol: str, when: Any) -> float | None:
        if symbol not in px.columns:
            return None
        series = px[symbol].loc[:when].dropna()
        return float(series.iloc[-1]) if len(series) else None

    periods, growth, bench_growth = [], 1.0, 1.0
    for i, q in enumerate(ordered):
        start = pd.Timestamp(q["filing_date"])
        stop = pd.Timestamp(ordered[i + 1]["filing_date"]) if i + 1 < len(ordered) else end_date
        if stop <= start or start > last_date:
            continue
        chosen = target_weights(q["book"]["equity"], top_n=rules.get("top_n"), min_weight=rules.get("min_weight") or 0.0,
                                cap=rules.get("cap"), min_confidence=rules.get("min_confidence", MIN_CONFIDENCE))
        weights = chosen["weights"]
        covered, ret = 0.0, 0.0
        for ticker, w in weights.items():
            p0, p1 = price_at(ticker, start), price_at(ticker, stop)
            if p0 is None or p1 is None or px[ticker].loc[:start].dropna().empty:
                continue
            covered += w
            ret += w * (p1 / p0 - 1)
        if covered <= 0:
            periods.append({"filing_date": q["filing_date"], "period": q["period"], "from": str(start.date()),
                            "to": str(stop.date()), "return": None, "price_coverage": 0.0})
            continue
        # names without prices are left out and the rest scaled up; cash (unallocated) earns nothing
        invested = 1 - (chosen["unallocated"] or 0)
        period_return = ret / covered * invested
        growth *= 1 + period_return
        row = {"filing_date": q["filing_date"], "period": q["period"], "from": str(start.date()), "to": str(stop.date()),
               "return": _r(period_return, 4), "price_coverage": _r(covered / sum(weights.values()), 4)}
        if benchmark:
            b0, b1 = price_at(benchmark, start), price_at(benchmark, stop)
            if b0 and b1:
                row["benchmark_return"] = _r(b1 / b0 - 1, 4)
                bench_growth *= b1 / b0
        periods.append(row)
    used = [p for p in periods if p["return"] is not None]
    span_days = (pd.Timestamp(used[-1]["to"]) - pd.Timestamp(used[0]["from"])).days if used else 0
    annual = (growth ** (365.25 / span_days) - 1) if used and span_days >= 180 else None
    return {
        "label": "Historical and lagged: copies each 13F on the day it was filed and holds it until the next "
                 "filing. Not the manager's return and not a forecast.",
        "historical": True, "lagged": True,
        "periods": periods, "cumulative_return": _r(growth - 1, 4) if used else None,
        "annualised_return": _r(annual, 4),
        "benchmark": benchmark, "benchmark_cumulative_return": _r(bench_growth - 1, 4) if benchmark and used else None,
        "rules": {k: rules.get(k) for k in ("top_n", "min_weight", "cap")},
        "caveats": ["Before taxes, commissions, spreads and FX.", "Prices are adjusted closes; missing names are "
                    "left out and the rest scaled up (see price_coverage)."],
    }


def backtest(cik: Any, *, quarters: int = 8, price_inputs: Mapping[str, Any] | None = None,
             rules: Mapping[str, Any] | None = None, benchmark: str | None = None, client: Edgar | None = None,
             tickers: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Load quarters and prices (market.py: supplied rows, CSV or live) and replay the lagged copy."""
    from .. import market

    cik = normalize_cik(cik)
    client = _client(client)
    try:
        sub, views, _, warnings, _flags = _history(client, cik, quarters, tickers, False)
    except UserAgentRequired as exc:
        return _needs(str(exc), SEC_UA_ENV)
    except TransportError as exc:
        return _needs(f"EDGAR could not be reached: {exc}", "EDGAR access (network)", client)
    if len(views) < 1:
        return _needs("No 13F holdings to replay.", "a manager with 13F holdings", client)
    symbols = sorted({p["ticker"] for v in views for p in v["book"]["equity"] if p.get("ticker")} |
                     ({benchmark} if benchmark else set()))
    inputs = dict(price_inputs or {})
    if "prices" not in inputs and "price_csv" not in inputs and "years" not in inputs:
        first = min(_date(v["filing_date"]) for v in views)
        inputs["years"] = max(1, math.ceil((_today() - first).days / 365.25) + 1)
    loaded = market._price_frame(inputs, symbols, "USD", optional=set(symbols), align=False)
    if loaded.px is None:
        return envelope("needs_input", {}, missing=loaded.missing, warnings=warnings + loaded.warnings)
    result = backtest_from_history(views, loaded.px, rules=rules, benchmark=benchmark)
    result["manager"] = {"cik": cik, "name": sub.get("name")}
    result["price_source"] = loaded.sources
    if loaded.dropped_optional:
        warnings.append("No prices for " + ", ".join(loaded.dropped_optional[:12]) + "; left out of the replay.")
    return envelope("ready" if result["cumulative_return"] is not None else "partial", result,
                    warnings=warnings + loaded.warnings, sources=_sources(client),
                    assumptions=loaded.assumptions)
