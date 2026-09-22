"""Positions from 13F rows, quarter diffs, CUSIP to ticker mapping, manager search and holdings."""
from __future__ import annotations

import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import date
from difflib import SequenceMatcher, get_close_matches
from typing import Any, Iterable, Mapping

from .._common import envelope
from .edgar import (
    ALIASES, AMENDMENT_POLICY, BROWSE_URL, COMPANY_TICKERS_URL, DOCS, ENTITY_SEARCH_URL, EXCLUDES, Edgar,
    FULL_TEXT_URL, MIN_CONFIDENCE, ManagerDataError, OPTIONS_NOTE, SEC_UA_ENV, TransportError,
    UserAgentRequired, WHAT_IS_13F, _by_period, _date, _documents, _r, _resolve_quarter, _today, filings_13f,
    normalize_cik, normalize_period, parse_primary_doc,
)


# ------------------------------------------------------------------ positions


def positions_from_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate rows by CUSIP into long equity, options and other (principal amounts).

    A line whose merged value is zero is not a position (it holds nothing at quarter end) and is left out;
    ``zero_value_lines`` counts them.
    """
    merged: dict[tuple, dict[str, Any]] = {}
    for row in rows:
        key = (row["cusip"], row.get("put_call") or "", row.get("amount_type") or "SH")
        item = merged.get(key)
        if item is None:
            merged[key] = {"issuer": row["issuer"], "class": row["class"], "cusip": row["cusip"],
                           "put_call": row.get("put_call"), "amount_type": row.get("amount_type") or "SH",
                           "shares": 0.0, "value": 0}
            item = merged[key]
        item["shares"] += row["shares"]
        item["value"] += row["value"]
        if row.get("figi") and not item.get("figi"):
            item["figi"] = row["figi"]
    zero = [k for k, item in merged.items() if not item["value"]]
    for k in zero:
        del merged[k]
    equity = [p for p in merged.values() if not p["put_call"] and p["amount_type"] == "SH"]
    options = [p for p in merged.values() if p["put_call"]]
    other = [p for p in merged.values() if not p["put_call"] and p["amount_type"] != "SH"]
    total = sum(p["value"] for p in merged.values())
    equity_total = sum(p["value"] for p in equity)
    for group in (equity, options, other):
        group.sort(key=lambda p: (-p["value"], p["cusip"]))
        for p in group:
            p["shares"] = int(p["shares"]) if float(p["shares"]).is_integer() else p["shares"]
            p["weight_of_reported"] = _r(p["value"] / total) if total else None
    for p in equity:
        p["weight"] = _r(p["value"] / equity_total) if equity_total else None
    return {"equity": equity, "options": options, "other": other, "total": total, "equity_total": equity_total,
            "calls": sum(p["value"] for p in options if p["put_call"] == "call"),
            "puts": sum(p["value"] for p in options if p["put_call"] == "put"),
            "other_total": sum(p["value"] for p in other), "zero_value_lines": len(zero)}


def diff(previous: list[dict[str, Any]], current: list[dict[str, Any]]) -> dict[str, Any]:
    """Quarter-over-quarter change of long-equity positions, keyed by CUSIP."""
    before = {p["cusip"]: p for p in previous}
    after = {p["cusip"]: p for p in current}
    out: dict[str, list[dict[str, Any]]] = {"new": [], "exited": [], "increased": [], "decreased": []}
    unchanged = 0
    for cusip in sorted(set(before) | set(after)):
        old, new = before.get(cusip), after.get(cusip)
        ref = new or old
        row = {"cusip": cusip, "issuer": ref["issuer"], "ticker": ref.get("ticker"),
               "shares": new["shares"] if new else 0, "previous_shares": old["shares"] if old else 0,
               "weight": new.get("weight", 0) if new else 0, "previous_weight": old.get("weight", 0) if old else 0,
               "value": new["value"] if new else 0, "previous_value": old["value"] if old else 0}
        row["share_change"] = row["shares"] - row["previous_shares"]
        row["share_change_pct"] = _r(row["share_change"] / row["previous_shares"], 4) if row["previous_shares"] else None
        row["weight_change"] = _r((row["weight"] or 0) - (row["previous_weight"] or 0))
        if old is None:
            out["new"].append(row)
        elif new is None:
            out["exited"].append(row)
        elif row["share_change"] > 0:
            out["increased"].append(row)
        elif row["share_change"] < 0:
            out["decreased"].append(row)
        else:
            unchanged += 1
    for rows in out.values():
        rows.sort(key=lambda r: -abs(r["weight_change"] or 0))
    return {**out, "unchanged": unchanged,
            "counts": {k: len(v) for k, v in out.items()} | {"unchanged": unchanged},
            "note": "Share changes compare quarter-end snapshots; trades within the quarter are invisible, and a "
                    "stock split shows as an increase in shares."}


# ------------------------------------------------------------------ CUSIP -> ticker

_NAME_NOISE = re.compile(
    r"\b(INC|INCORPORATED|CORP|CORPORATION|CO|COMPANY|LTD|LIMITED|PLC|LLC|L ?P|HLDGS?|HOLDINGS?|GROUP|GRP|THE|NEW|"
    r"DEL|DE|CL|CLASS|COM|COMMON|SHS|SH|ORD|N ?V|S ?A|AG|SE|SPONSORED|ADR|ADS|REG|STK)\b")


def _norm_name(name: Any) -> str:
    text = re.sub(r"[^A-Z0-9 ]", " ", str(name or "").upper().replace("&", " AND "))
    text = _NAME_NOISE.sub(" ", text)
    return " ".join(text.split())


def _class_letter(title: str) -> str | None:
    match = re.search(r"\bCL(?:ASS)?\s+([A-C])\b", title.upper())
    return match.group(1) if match else None


class _TickerIndex:
    def __init__(self, data: Any):
        self.by_name: dict[str, list[dict[str, Any]]] = {}
        self.by_ticker: dict[str, dict[str, Any]] = {}
        rows = data.values() if isinstance(data, Mapping) else data or []
        for row in rows:
            if not isinstance(row, Mapping) or not row.get("ticker"):
                continue
            entry = {"cik": str(row.get("cik_str") or row.get("cik") or "").zfill(10), "ticker": str(row["ticker"]),
                     "title": str(row.get("title") or "")}
            self.by_name.setdefault(_norm_name(entry["title"]), []).append(entry)
            self.by_ticker.setdefault(entry["ticker"].upper(), entry)

    def match(self, issuer: str, title_class: str) -> tuple[dict[str, Any] | None, float]:
        key = _norm_name(issuer)
        if not key:
            return None, 0.0
        score, entries = 1.0, self.by_name.get(key)
        if entries is None:
            close = get_close_matches(key, list(self.by_name), n=3, cutoff=0.75)
            if not close:
                return None, 0.0
            best = max(close, key=lambda c: SequenceMatcher(None, key, c).ratio())
            score, entries = SequenceMatcher(None, key, best).ratio(), self.by_name[best]
        letter = _class_letter(title_class)
        chosen = entries[0]
        if letter and len(entries) > 1:
            chosen = next((e for e in entries if re.search(rf"[.-]?{letter}$", e["ticker"])), chosen)
        return chosen, score


def _ticker_index(client: Edgar) -> _TickerIndex | None:
    try:
        return _TickerIndex(client.json(COMPANY_TICKERS_URL, "tickers"))
    except (TransportError, ManagerDataError) as exc:
        client.warnings.append(f"SEC company_tickers.json was not available ({exc}); no name matching.")
        return None


def map_cusips(client: Edgar, items: Iterable[Mapping[str, Any]],
               overrides: Mapping[str, str] | None = None) -> dict[str, dict[str, Any]]:
    """CUSIP -> {ticker, confidence, method}.

    Supplied mappings are 1.0; OpenFIGI CUSIP matches 0.99 for a US equity listing and 0.7 otherwise
    (a foreign line or a bond, which a US broker may not offer); SEC name matches at
    most 0.9 (0.9 x name similarity), and below 0.6 the name match is dropped.
    """
    wanted: dict[str, Mapping[str, Any]] = {}
    for item in items:
        wanted.setdefault(str(item["cusip"]).upper(), item)
    supplied = {str(k).upper(): str(v).upper() for k, v in (overrides or {}).items()}
    out: dict[str, dict[str, Any]] = {}
    for cusip in wanted:
        if cusip in supplied:
            out[cusip] = {"ticker": supplied[cusip], "confidence": 1.0, "method": "supplied"}
    todo = [c for c in wanted if c not in out]
    figi = client.figi(todo) if todo else {}
    for cusip in todo:
        hit = figi.get(cusip)
        if hit:
            us_equity = hit.get("us_equity", True)
            out[cusip] = {"ticker": hit["ticker"], "confidence": 0.99 if us_equity else 0.7,
                          "method": "openfigi" if us_equity else "openfigi_not_us_equity",
                          "security_type": hit.get("security_type")}
    rest = [c for c in wanted if c not in out]
    index = _ticker_index(client) if rest else None
    for cusip in rest:
        item = wanted[cusip]
        entry, score = index.match(str(item.get("issuer") or ""), str(item.get("class") or "")) if index else (None, 0.0)
        confidence = round(0.9 * score, 2)
        if entry and confidence >= 0.6:
            out[cusip] = {"ticker": entry["ticker"], "confidence": confidence, "method": "name_match",
                          "matched_name": entry["title"]}
        else:
            out[cusip] = {"ticker": None, "confidence": 0.0, "method": "unmapped"}
    return out


def _apply_mapping(groups: Iterable[list[dict[str, Any]]], mapping: Mapping[str, Mapping[str, Any]]) -> None:
    for group in groups:
        for p in group:
            m = mapping.get(p["cusip"]) or {"ticker": None, "confidence": 0.0, "method": "unmapped"}
            p["ticker"], p["ticker_confidence"], p["mapping_method"] = m["ticker"], m["confidence"], m["method"]
            if m.get("security_type"):
                p["security_type"] = m["security_type"]


# ------------------------------------------------------------------ quarters


def _load_quarters(client: Edgar, cik: str, count: int, *, period: str | None = None,
                   submissions: Mapping[str, Any] | None = None) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    """(submissions, resolved quarters newest first, 13F filing rows).  Notice-only quarters are kept."""
    sub, rows = filings_13f(client, cik, periods_wanted=count + 1, submissions=submissions)
    grouped = _by_period(rows)
    periods = sorted(grouped, reverse=True)
    if period is not None:
        if period not in grouped:
            raise ManagerDataError(f"CIK {cik} has no 13F for the quarter ending {period}; filed quarters: "
                                   f"{', '.join(periods[:8]) or 'none'}")
        periods = [p for p in periods if p <= period]
    out = []
    for p in periods:
        if len([q for q in out if not q["notice"]]) >= count:
            break
        out.append(_resolve_quarter(client, cik, p, grouped[p]))
    return sub, out, rows


def _lag(quarter: Mapping[str, Any], today: date) -> dict[str, Any]:
    period, filed = _date(quarter["period"]), _date(quarter["filing_date"])
    return {"period_end": quarter["period"], "filed": quarter["filing_date"],
            "days_since_period_end": (today - period).days if period else None,
            "days_since_filing": (today - filed).days if filed else None,
            "filing_delay_days": (filed - period).days if period and filed else None,
            "deadline": "45 days after the quarter ends"}


def _meta(result: dict[str, Any]) -> dict[str, Any]:
    result.update(what_is_13f=WHAT_IS_13F, excludes=EXCLUDES, options_note=OPTIONS_NOTE,
                  amendment_policy=AMENDMENT_POLICY)
    return result


def _sources(client: Edgar, extra: Iterable[str] = ()) -> list[dict[str, str]]:
    urls = list(dict.fromkeys([*client.fetched, *extra]))
    out = [{"title": "SEC EDGAR" if "sec.gov" in u else "OpenFIGI" if "openfigi" in u else "source", "url": u}
           for u in urls]
    out += [{"title": "SEC Form 13F FAQ", "url": DOCS["form_13f_faq"]},
            {"title": "SEC EDGAR APIs and fair access", "url": DOCS["fair_access"]}]
    return out


UA_FIX = (f"One-line fix, done once by the person (not by you): add {SEC_UA_ENV}=\"Their Name their@email.com\" "
          "to the wealth MCP server's env (OpenClaw: rerun integrations/openclaw/install.sh; Claude Desktop or Claude "
          "Code: the \"env\" block of the wealth server in the MCP config), then restart the host. The SEC requires "
          "a contact on every automated request; nothing else is sent.")


def _needs(detail: str, missing: str, client: Edgar | None = None) -> dict[str, Any]:
    result = _meta({"fix": UA_FIX} if missing == SEC_UA_ENV else {})
    return envelope("needs_input", result, missing=[missing], warnings=[detail],
                    sources=_sources(client) if client else [])


def _client(client: Edgar | None) -> Edgar:
    return client if client is not None else Edgar()


# ------------------------------------------------------------------ find


def find(name: str, *, client: Edgar | None = None, limit: int = 8) -> dict[str, Any]:
    """Search EDGAR for a manager by name; candidates carry CIK and latest 13F filing."""
    if not isinstance(name, str) or not name.strip():
        raise ValueError("name must be a manager or firm name")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 20:
        raise ValueError("limit must be 1-20")
    client = _client(client)
    query = " ".join(name.split())
    terms = [query] + [firm for person, firm in ALIASES.items() if person in query.lower() and firm.lower()
                       not in query.lower()]
    order: dict[str, dict[str, Any]] = {}
    warnings: list[str] = []
    try:
        for term in terms:
            q = urllib.parse.quote(term.lower())
            try:
                hits = client.json(FULL_TEXT_URL.format(q=urllib.parse.quote('"' + term.lower() + '"')), "search")
                for hit in (hits.get("hits") or {}).get("hits") or []:
                    source = hit.get("_source") or {}
                    for cik, label in zip(source.get("ciks") or [], source.get("display_names") or []):
                        order.setdefault(str(cik).zfill(10), {"name": re.sub(r"\s*\(CIK \d+\)\s*$", "", label).strip(),
                                                              "via": "13F full-text search"})
            except TransportError:
                warnings.append(f"EDGAR full-text search for {term!r} was not available.")
            try:
                hits = client.json(ENTITY_SEARCH_URL.format(q=q), "search")
                for hit in (hits.get("hits") or {}).get("hits") or []:
                    entity = str((hit.get("_source") or {}).get("entity") or "")
                    order.setdefault(str(hit.get("_id")).zfill(10), {"name": re.sub(r"\s*\([^)]*\)\s*$", "", entity).strip(),
                                                                     "via": "EDGAR company search"})
            except TransportError:
                warnings.append(f"EDGAR company search for {term!r} was not available.")
    except UserAgentRequired as exc:
        return _needs(str(exc), SEC_UA_ENV)
    wanted_terms = [_norm_name(t) for t in terms]  # an alias ranks by the firm's name as well

    def score(item: tuple[str, dict[str, Any]]) -> float:
        name = _norm_name(item[1]["name"])
        best = 0.0
        for wanted in wanted_terms:
            tokens = set(wanted.split())
            best = max(best, SequenceMatcher(None, wanted, name).ratio()
                       + (1.0 if tokens and tokens <= set(name.split()) else 0.0))
        return best

    ranked = sorted(order.items(), key=score, reverse=True)  # stable: search relevance breaks ties
    candidates = []
    queue = ranked[:limit]
    seen_ciks = {cik for cik, _ in queue}
    while queue:
        cik, meta = queue.pop(0)
        try:
            normalize_cik(cik)
            sub, rows = filings_13f(client, cik)
        except (TransportError, ManagerDataError, ValueError):
            warnings.append(f"Submissions for CIK {cik} could not be read.")
            continue
        except UserAgentRequired as exc:
            return _needs(str(exc), SEC_UA_ENV)
        latest = rows[0] if rows else None
        latest_hr = next((r for r in rows if r["form"].startswith("13F-HR")), None)
        item = {"cik": cik, "name": sub.get("name") or meta["name"], "files_13f": bool(latest_hr),
                "latest_13f": {k: latest[k] for k in ("form", "filing_date", "period", "accession")} if latest else None,
                "latest_holdings_report": {k: latest_hr[k] for k in ("form", "filing_date", "period", "accession")}
                if latest_hr else None,
                "found_via": meta["via"], "edgar_url": BROWSE_URL.format(cik=cik)}
        if latest and latest["form"].startswith("13F-NT"):
            try:
                primary, _, _ = _documents(client, cik, latest["accession"])
                doc = parse_primary_doc(primary) if primary else {}
                item["notice"] = {"reported_by": doc.get("other_managers") or [],
                                  "note": doc.get("additional_information") or
                                  "The latest quarter's holdings are reported in another manager's 13F."}
                for other in item["notice"]["reported_by"]:
                    if other.get("cik") and other["cik"] not in seen_ciks:
                        seen_ciks.add(other["cik"])
                        queue.append((other["cik"], {"name": other.get("name") or other["cik"],
                                                     "via": f"named in {item['name']}'s 13F notice"}))
            except (TransportError, ManagerDataError):
                item["notice"] = {"reported_by": [], "note": "The latest filing is a 13F notice (holdings reported by "
                                                             "another manager)."}
        candidates.append(item)
    candidates.sort(key=lambda c: not c["files_13f"])  # stable: relevance order within each group
    filers = [c for c in candidates if c["files_13f"]]
    if not candidates:
        return envelope("needs_input", {"query": query, "candidates": []},
                        missing=["a different spelling of the firm name, or its CIK"],
                        warnings=warnings + [f"EDGAR found no filer matching {query!r}."], sources=_sources(client))
    if not filers:
        warnings.append(f"None of the EDGAR filers matching {query!r} files Form 13F. The manager may be under the "
                        "US$100 million threshold, file under a parent or a differently named entity, or not be a "
                        "US-reporting institutional manager.")
    for c in candidates:
        if c.get("notice"):
            names = ", ".join(f"{m.get('name')} (CIK {m.get('cik')})" for m in c["notice"]["reported_by"] if m.get("name"))
            warnings.append(f"{c['name']}'s latest 13F is a notice: its holdings are reported by "
                            f"{names or 'another manager'}.")
    result = {"query": query, "search_terms": terms, "candidates": candidates,
              "note": "Only managers with at least US$100 million in 13(f) securities file 13F; a person is found "
                      "through their firm."}
    return envelope("ready" if filers else "partial", result, warnings=warnings + client.warnings,
                    sources=_sources(client, [DOCS["edgar_apis"]]))


# ------------------------------------------------------------------ holdings


def _quarter_view(quarter: Mapping[str, Any]) -> dict[str, Any]:
    book = positions_from_rows(quarter["rows"])
    return {**quarter, "book": book}


def holdings(cik: Any, period: Any = None, *, client: Edgar | None = None, tickers: Mapping[str, str] | None = None,
             include_options: bool = False, as_of: Any = None) -> dict[str, Any]:
    """The chosen (default latest) quarter's holdings, amendments applied, with the prior-quarter diff."""
    cik = normalize_cik(cik)
    wanted = normalize_period(period)
    client = _client(client)
    today = _today(as_of)
    try:
        sub, quarters, rows = _load_quarters(client, cik, 2, period=wanted)
    except UserAgentRequired as exc:
        return _needs(str(exc), SEC_UA_ENV)
    except TransportError as exc:
        return _needs(f"EDGAR could not be reached: {exc}", "EDGAR access (network)", client)
    name = sub.get("name")
    if not rows:
        return envelope("needs_input", _meta({"manager": {"cik": cik, "name": name}}),
                        missing=["a manager that files Form 13F"],
                        warnings=[f"{name or 'CIK ' + cik} has no 13F filings on EDGAR. Only managers with at least "
                                  "US$100 million in 13(f) securities file them; the holdings may be reported by a "
                                  "parent or affiliate under another CIK."], sources=_sources(client))
    if not quarters:
        return _needs("No quarter with 13F holdings was found.", "a quarter with 13F holdings", client)
    current = quarters[0]
    if current["notice"] is not None:
        reported_by = current["notice"]["reported_by"]
        older = next((q for q in quarters[1:] if not q["notice"]), None)
        names = ", ".join(f"{m.get('name')} (CIK {m.get('cik')})" for m in reported_by if m.get("name"))
        return envelope("needs_input", _meta({
            "manager": {"cik": cik, "name": name}, "period": current["period"],
            "filing": {k: current[k] for k in ("accession", "form", "filing_date", "url", "report_type")},
            "notice": current["notice"],
            "latest_holdings_period": older["period"] if older else None}),
            missing=["cik of the manager whose 13F includes these holdings" + (f": {names}" if names else "")],
            warnings=[f"{name}'s 13F for {current['period']} is a notice (13F-NT): the holdings are reported in "
                      f"{names or 'another manager’s'} 13F. Ask for that CIK, or pass period="
                      f"{older['period'] if older else 'an earlier quarter'} for this filer's last own report."],
            sources=_sources(client))
    view = _quarter_view(current)
    prev_quarter = next((q for q in quarters[1:] if not q["notice"]), None)
    skipped_prev = None
    if prev_quarter is not None and prev_quarter.get("incomplete"):
        skipped_prev, prev_quarter = prev_quarter["period"], None
    prev_view = _quarter_view(prev_quarter) if prev_quarter else None
    book = view["book"]
    items = book["equity"] + book["options"] + (prev_view["book"]["equity"] if prev_view else [])
    try:
        mapping = map_cusips(client, items, tickers)
    except UserAgentRequired as exc:
        return _needs(str(exc), SEC_UA_ENV)
    _apply_mapping([book["equity"], book["options"], book["other"]] + ([prev_view["book"]["equity"]] if prev_view else []),
                   mapping)
    warnings = list(current["warnings"])
    unmapped = [p for p in book["equity"] if not p["ticker"]]
    low = [p for p in book["equity"] if p["ticker"] and p["ticker_confidence"] < MIN_CONFIDENCE]
    if unmapped:
        warnings.append(f"{len(unmapped)} holdings ({_pct(sum(p['weight'] or 0 for p in unmapped))} of long equity) "
                        "have no ticker; pass tickers {cusip: ticker} to map them.")
    if low:
        warnings.append("Tickers matched by name only, below 0.8 confidence: " +
                        ", ".join(f"{p['issuer']} -> {p['ticker']} ({p['ticker_confidence']})" for p in low) + ".")
    total_check = None
    reported = current.get("summary_value_dollars")
    if reported is not None:
        # Compared with the summary pages of the filings actually applied (original plus NEW HOLDINGS, or the
        # latest RESTATEMENT plus later NEW HOLDINGS), each converted from its own unit.
        total_check = {"summary_page": int(round(reported)), "parsed": book["total"],
                       "matches": abs(reported - book["total"]) <= max(1000.0, 0.001 * reported)}
        if not total_check["matches"]:
            warnings.append("The parsed total differs from the summary page total of the filings applied; the "
                            "information table may be incomplete.")
    result = _meta({
        "manager": {"cik": cik, "name": name or current.get("manager")},
        "period": current["period"],
        "filing": {k: current[k] for k in ("accession", "form", "filing_date", "url", "report_type")},
        "amendments": current["amendments"],
        "value_unit": "dollars" if current["value_unit"] == ["dollars"] else
                      "thousands in the filing, converted to dollars" if current["value_unit"] == ["thousands"] else
                      "mixed (original and amendment in different units), all converted to dollars",
        "currency": "USD",
        "total_reported_value": book["total"], "long_equity_value": book["equity_total"],
        "options_value": {"call": book["calls"], "put": book["puts"]}, "other_value": book["other_total"],
        "positions": book["equity"],
        "options": book["options"], "other": book["other"],
        "include_options_in_weights": False,
        "counts": {"positions": len(book["equity"]), "options": len(book["options"]), "other": len(book["other"]),
                   "unmapped": len(unmapped)},
        "unmapped": [{"cusip": p["cusip"], "issuer": p["issuer"], "weight": p["weight"]} for p in unmapped],
        "previous_period": prev_view["period"] if prev_view else None,
        "changes": diff(prev_view["book"]["equity"], book["equity"]) if prev_view else None,
        "lag": _lag(current, today),
        "summary_total_check": total_check,
    })
    if include_options:
        combined = book["equity_total"] + book["calls"] + book["puts"]
        result["include_options_in_weights"] = True
        for p in book["equity"] + book["options"]:
            p["weight_with_options"] = _r(p["value"] / combined) if combined else None
        warnings.append("Weights with options treat each option as its underlying stock value, which overstates "
                        "the money at stake in the option.")
    if skipped_prev:
        warnings.append(f"The earlier quarter {skipped_prev} is incomplete (a NEW HOLDINGS amendment without its "
                        "original), so there is no quarter-over-quarter comparison.")
    elif prev_view is None:
        warnings.append("No earlier quarter is available, so there is no quarter-over-quarter comparison.")
    status = "partial" if (unmapped or low or current.get("incomplete") or any(current.get("unit_flags") or [])
                           or any("look like" in w for w in warnings)) else "ready"
    return envelope(status, result, warnings=warnings + client.warnings, sources=_sources(client),
                    assumptions=["Values are the filing's quarter-end market values in US dollars.",
                                 "Long-equity weights exclude options and principal-amount (debt) rows."])


def _pct(value: float | None) -> str:
    if value is None:
        return "unknown"
    return f"{value * 100:.0f}%" if abs(value) >= 0.095 else f"{value * 100:.1f}%"
