"""Mirror a manager's 13F book as a sleeve: target weights, policy checks and the trade handoff."""
from __future__ import annotations

import json
import math
import re
from typing import Any, Mapping

from .._common import envelope
from .edgar import DEFAULT_CONCENTRATION, DOCS, MIN_CONFIDENCE, TRACKING_CAVEATS, _r
from .positions import _pct


# ------------------------------------------------------------------ mirror


def _unwrap_holdings(holdings: Any) -> dict[str, Any]:
    if isinstance(holdings, Mapping) and "status" in holdings and isinstance(holdings.get("result"), Mapping):
        holdings = holdings["result"]
    if not isinstance(holdings, Mapping) or not isinstance(holdings.get("positions"), list):
        raise ValueError("holdings must be a manager_holdings result (with positions)")
    return dict(holdings)


def target_weights(positions: list[Mapping[str, Any]], *, top_n: int | None = None, min_weight: float = 0.0,
                   cap: float | None = None, min_confidence: float = MIN_CONFIDENCE) -> dict[str, Any]:
    """Long-equity weights -> sleeve targets: drop unmapped, top-N, minimum weight, cap and redistribute pro rata."""
    dropped: dict[str, list[dict[str, Any]]] = {"unmapped": [], "below_min_weight": [], "outside_top_n": []}
    kept: dict[str, float] = {}
    names: dict[str, str] = {}
    ranked = sorted((p for p in positions if (p.get("weight") or 0) > 0), key=lambda p: -(p.get("weight") or 0))
    for p in ranked:
        ticker = p.get("ticker")
        row = {"cusip": p.get("cusip"), "issuer": p.get("issuer"), "ticker": ticker, "weight": p.get("weight")}
        if not ticker or (p.get("ticker_confidence") or 0) < min_confidence:
            dropped["unmapped"].append({**row, "confidence": p.get("ticker_confidence")})
            continue
        if p["weight"] < min_weight:
            dropped["below_min_weight"].append(row)
            continue
        if top_n is not None and len(kept) >= top_n and ticker not in kept:
            dropped["outside_top_n"].append(row)
            continue
        kept[ticker] = kept.get(ticker, 0.0) + float(p["weight"])
        names.setdefault(ticker, p.get("issuer") or ticker)
    total = sum(kept.values())
    if not total:
        return {"weights": {}, "dropped": dropped, "unallocated": 1.0, "capped": [], "names": names,
                "coverage_of_manager": 0.0}
    weights = {t: w / total for t, w in kept.items()}
    uncapped = dict(weights)
    capped: list[str] = []
    unallocated = 0.0
    if cap is not None:
        if cap <= 0 or cap > 1:
            raise ValueError("the single-name cap must be between 0 and 1")
        for _ in range(len(weights) + 1):
            over = [t for t, w in weights.items() if w > cap + 1e-12]
            if not over:
                break
            excess = sum(weights[t] - cap for t in over)
            for t in over:
                weights[t] = cap
                if t not in capped:
                    capped.append(t)
            free = {t: w for t, w in weights.items() if t not in capped}
            base = sum(free.values())
            if base <= 0:
                unallocated += excess
                break
            for t, w in free.items():
                weights[t] = w + excess * w / base
    return {"weights": {t: _r(w) for t, w in sorted(weights.items(), key=lambda kv: -kv[1])},
            "uncapped_weights": {t: _r(w) for t, w in uncapped.items()},
            "dropped": dropped, "unallocated": _r(unallocated), "capped": capped, "names": names,
            "coverage_of_manager": _r(total)}


def _residence(situation: Any) -> str | None:
    if isinstance(situation, str):
        return situation.upper()
    if not isinstance(situation, Mapping):
        return None
    profile_ = situation.get("profile") if isinstance(situation.get("profile"), Mapping) else situation
    residence = profile_.get("residence")
    if isinstance(residence, Mapping):
        return str(residence.get("country") or "").upper() or None
    if isinstance(residence, str):
        return residence.upper()
    country = profile_.get("country")
    return str(country).upper() if country else None


def _situs(cusip: str | None, security_type: str | None = None, title_class: str | None = None) -> dict[str, str]:
    depositary = re.search(r"\bAD[RS]\b|DEPOSITARY|NY REG(ISTRY)?\b", f"{security_type or ''} {title_class or ''}".upper())
    if depositary:
        return {"estate_situs": "not_us", "basis": "Depositary receipt of a non-US company: generally treated as the "
                                                   "foreign company's shares, not US-situs (the point is debated; "
                                                   "confirm)."}
    if cusip and cusip[:1].isalpha():
        return {"estate_situs": "not_us", "basis": "CINS CUSIP: a non-US issuer; shares of a foreign corporation are "
                                                   "generally not US-situs (confirm for ADRs)."}
    return {"estate_situs": "us", "basis": "US-style CUSIP: treated as a US issuer, so US-situs for estate tax "
                                           "(Canadian issuers also use these CUSIPs; confirm)."}


def _speculation(summary: dict[str, Any], ips: Any, situation: Any) -> dict[str, Any]:
    try:
        from .. import guardrails  # type: ignore[attr-defined]
    except ImportError:
        guardrails = None
    check = getattr(guardrails, "speculation_check", None) if guardrails else None
    fallback = None
    if callable(check) and situation is not None:
        # A mirrored manager book is a basket of single stocks bought with this sleeve's money.
        proposal = {"action": "buy", "instrument": "single_stock", "amount": summary.get("amount"),
                    "currency": summary.get("currency"), "symbol": summary.get("label")}
        try:
            return {"source": "guardrails.speculation_check", "result": check(proposal, situation, ips=ips),
                    "concentrated": (summary["top10"] or 0) >= 0.5 or summary["names"] <= 20}
        except (TypeError, ValueError) as exc:
            fallback = f"guardrails.speculation_check could not be applied ({exc}); built-in assessment used."
    concentrated = (summary["top10"] or 0) >= 0.5 or summary["names"] <= 20 or (summary["largest_manager_weight"] or 0) >= 0.15
    share = summary.get("share_of_portfolio")
    verdict = "satellite" if concentrated else "core_candidate"
    explanation = ("A single manager's concentrated 13F book belongs in a small speculation/satellite sleeve, not the "
                   "core of the portfolio." if concentrated else
                   "The mirrored book is broad, but it is still one manager's lagged snapshot; treat it as a "
                   "satellite unless the policy says otherwise.")
    status = "warn" if concentrated else "pass"
    if share is not None and share > 0.2:
        status = "warn"
        explanation += f" At {_pct(share)} of the portfolio the sleeve is large for a satellite; 5-10% is typical."
    out = {"source": "managers (built-in satellite check; no situation to size against)", "verdict": verdict, "status": status,
           "explanation": explanation, "concentrated": concentrated, "share_of_portfolio": share}
    if fallback:
        out["note"] = fallback
    return out


def mirror(holdings: Any, sleeve_amount: Any, currency: str, *, ips: Mapping[str, Any] | None = None,
           situation: Any = None, constraints: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Target weights and amounts for a sleeve that mirrors a manager's long-equity 13F book.

    ``constraints``: ``top_n``, ``min_weight``, ``max_weight`` (single-name cap; default the IPS
    concentration limit, else 10%), ``min_confidence`` (0.8), ``portfolio_value`` (cap and sizing
    relative to the whole portfolio), ``prices`` ``{TICKER: USD price}``, ``usdmxn``,
    ``sic_listed`` ``{TICKER: bool}``, ``funding``, ``portfolio`` (policy_check shape), and for a trade
    list ``household`` + ``jurisdiction_context`` (+ ``cash_flows``, ``tax_inputs``,
    ``rebalance_constraints``).  Never places orders.
    """
    book = _unwrap_holdings(holdings)
    constraints = dict(constraints or {})
    known = {"top_n", "min_weight", "max_weight", "min_confidence", "portfolio_value", "prices", "usdmxn", "sic_listed",
             "funding", "portfolio", "household", "jurisdiction_context", "cash_flows", "tax_inputs",
             "rebalance_constraints"}
    unknown = sorted(set(constraints) - known)
    if unknown:
        raise ValueError(f"mirror constraints: unknown {unknown}; expected {sorted(known)}")
    try:
        amount = float(sleeve_amount)
    except (TypeError, ValueError):
        raise ValueError("sleeve_amount must be a positive number") from None
    if not math.isfinite(amount) or amount <= 0:
        raise ValueError("sleeve_amount must be a positive number")
    if not isinstance(currency, str) or not re.fullmatch(r"[A-Z]{3}", currency):
        raise ValueError("currency must be an ISO code such as USD or MXN")
    warnings: list[str] = []
    missing: list[str] = []
    assumptions: list[str] = []
    top_n = constraints.get("top_n")
    if top_n is not None and (isinstance(top_n, bool) or not isinstance(top_n, int) or top_n < 1):
        raise ValueError("top_n must be a positive whole number")
    min_weight = float(constraints.get("min_weight") or 0)
    limit = ((ips or {}).get("constraints") or {}).get("concentration", {}).get("limit") if isinstance(ips, Mapping) else None
    cap_source = "constraints.max_weight" if constraints.get("max_weight") is not None else \
        "policy.ips concentration limit" if limit is not None else "default 10% single-name limit"
    portfolio_limit = float(constraints.get("max_weight") if constraints.get("max_weight") is not None else
                            limit if limit is not None else DEFAULT_CONCENTRATION)
    portfolio_value = constraints.get("portfolio_value")
    if portfolio_value is not None:
        portfolio_value = float(portfolio_value)
        if portfolio_value < amount:
            raise ValueError("portfolio_value must include the sleeve (at least sleeve_amount)")
        cap = min(1.0, portfolio_limit * portfolio_value / amount)
        cap_basis = "whole portfolio"
    else:
        cap = portfolio_limit
        cap_basis = "sleeve (no portfolio_value given, so the limit is applied inside the sleeve)"
    targets = target_weights(book["positions"], top_n=top_n, min_weight=min_weight, cap=cap,
                             min_confidence=float(constraints.get("min_confidence", MIN_CONFIDENCE)))
    if not targets["weights"]:
        return envelope("needs_input", {"tracking_caveats": TRACKING_CAVEATS, "dropped": targets["dropped"]},
                        missing=["tickers for the manager's holdings (pass tickers {cusip: ticker})"],
                        warnings=["No holding has a usable ticker, so nothing can be mirrored."])
    if targets["dropped"]["unmapped"]:
        warnings.append(f"Left out {len(targets['dropped']['unmapped'])} holdings without a confident ticker "
                        f"({_pct(sum(r['weight'] or 0 for r in targets['dropped']['unmapped']))} of the manager's "
                        "long equity); see dropped.unmapped.")
    if targets["unallocated"]:
        warnings.append(f"The {_pct(cap)} single-name cap cannot be met by {len(targets['weights'])} names; "
                        f"{_pct(targets['unallocated'])} of the sleeve stays in cash." +
                        ("" if portfolio_value is not None else " Pass constraints.portfolio_value to apply the "
                         "policy limit to the whole portfolio instead of inside the sleeve."))
    prices = {str(k).upper(): float(v) for k, v in (constraints.get("prices") or {}).items()}
    implied = {p["ticker"]: p["value"] / float(p["shares"]) for p in book["positions"]
               if p.get("ticker") and p.get("shares")}
    resident = _residence(situation)
    usdmxn = constraints.get("usdmxn")
    sic = {str(k).upper(): v for k, v in (constraints.get("sic_listed") or {}).items()}
    rows = []
    cusips = {p.get("ticker"): p.get("cusip") for p in book["positions"]}
    kinds = {p.get("ticker"): (p.get("security_type"), p.get("class")) for p in book["positions"]}
    for ticker, weight in targets["weights"].items():
        row = {"ticker": ticker, "issuer": targets["names"].get(ticker), "cusip": cusips.get(ticker),
               "estate_situs": _situs(cusips.get(ticker), *kinds.get(ticker, (None, None)))["estate_situs"],
               "weight": weight,
               "manager_weight": next((p["weight"] for p in book["positions"] if p.get("ticker") == ticker), None),
               "amount": round(weight * amount, 2), "currency": currency, "capped": ticker in targets["capped"]}
        price = prices.get(ticker)
        row["price"] = price if price is not None else _r(implied.get(ticker), 4)
        row["price_basis"] = "supplied" if price is not None else \
            f"13F quarter-end value / shares ({book.get('period')}); stale" if implied.get(ticker) else None
        if resident == "MX":
            situs = _situs(cusips.get(ticker), *kinds.get(ticker, (None, None)))
            listed = sic.get(ticker)
            row["mexico"] = {"sic_listed": listed if isinstance(listed, bool) else "unknown", **situs}
            if row["price"] is not None and usdmxn:
                share_mxn = float(row["price"]) * float(usdmxn)
                amount_mxn = row["amount"] if currency == "MXN" else row["amount"] * float(usdmxn) if currency == "USD" else None
                row["mexico"]["whole_share_cost_mxn"] = round(share_mxn, 2)
                if amount_mxn is not None:
                    shares = math.floor(amount_mxn / share_mxn)
                    row["mexico"].update(whole_shares=shares, invested_mxn=round(shares * share_mxn, 2),
                                         left_over_mxn=round(amount_mxn - shares * share_mxn, 2),
                                         rounds_to_zero=shares == 0)
        rows.append(row)
    if resident == "MX":
        if not usdmxn:
            missing.append("constraints.usdmxn (to cost whole shares on the SIC)")
        unknown_sic = [r["ticker"] for r in rows if r["mexico"]["sic_listed"] == "unknown"]
        if unknown_sic:
            warnings.append("SIC availability is unknown for " + ", ".join(unknown_sic[:12]) +
                            ("…" if len(unknown_sic) > 12 else "") + ": a name not listed in the SIC is taxed at "
                            "progressive rates instead of 10% and cannot be bought on GBM Trading MX. Pass "
                            "constraints.sic_listed.")
        us_situs = [r["ticker"] for r in rows if r["mexico"]["estate_situs"] == "us"]
        if us_situs:
            warnings.append(f"{len(us_situs)} names are US issuers and therefore US-situs for US estate tax (US$60k "
                            "exemption for non-residents, rates up to 40%), even when bought through the SIC.")
        zero = [r["ticker"] for r in rows if r["mexico"].get("rounds_to_zero")]
        if zero:
            warnings.append("On the SIC (whole shares only) the target amount buys no share of " + ", ".join(zero) +
                            "; raise the sleeve, use top_n, or use a broker with fractional shares.")
        assumptions.append("Mexico facts: docs/mexico-investing-facts.md (verified 2026-09-21).")
    trades_now = len(rows)
    changes = book.get("changes") or {}
    mirrored = set(targets["weights"])
    quarterly = sum(1 for kind in ("new", "exited", "increased", "decreased") for r in changes.get(kind) or []
                    if r.get("ticker") in mirrored)
    weights = list(targets["weights"].values())
    summary = {"label": "13F mirror of " + str((book.get("manager") or {}).get("name") or "a manager"),
               "amount": amount, "currency": currency, "names": len(rows),
               "top10": _r(sum(sorted(weights, reverse=True)[:10]), 4),
               "largest_manager_weight": max((p.get("weight") or 0) for p in book["positions"]) if book["positions"] else None,
               "share_of_portfolio": _r(amount / portfolio_value, 4) if portfolio_value else None}
    result: dict[str, Any] = {
        "manager": book.get("manager"), "period": book.get("period"), "filing": book.get("filing"),
        "lag": book.get("lag"), "sleeve_amount": amount, "currency": currency,
        "targets": rows, "target_weights": targets["weights"], "cash_weight": targets["unallocated"],
        "cap": {"single_name": _r(cap, 4), "portfolio_limit": portfolio_limit, "basis": cap_basis, "source": cap_source,
                "capped": targets["capped"]},
        "dropped": targets["dropped"], "coverage_of_manager": targets["coverage_of_manager"],
        "expected_trades": {"initial": trades_now,
                            "per_quarter_estimate": quarterly if changes else None,
                            "note": "Initial buys to build the sleeve; per quarter, the last 13F's changes among "
                                    "mirrored names (new, exited, added to or trimmed; a proxy for what following it costs)."},
        "speculation": _speculation(summary, ips, situation),
        "tracking_caveats": TRACKING_CAVEATS, "execution_ready": False,
        "scope": "Target weights and amounts for review; never an order.",
    }
    if ips is not None:
        result["policy_check"] = _policy_checks(ips, rows, currency, constraints, situation)
        if result["policy_check"]["verdict"] == "violation":
            warnings.append("Some buys break the investment policy; see policy_check.")
    else:
        missing.append("policy.ips (accepted investment policy) to check the sleeve")
    if constraints.get("household") is not None and constraints.get("jurisdiction_context") is not None:
        result["plan"] = _handoff(rows, targets, constraints, warnings)
        if result["plan"].get("status") != "ready":
            missing += [f"plan: {m}" for m in result["plan"].get("missing") or []]
    else:
        result["plan"] = None
        assumptions.append("No trade list: pass constraints.household and constraints.jurisdiction_context "
                           "to get one from rebalance.")
    status = "partial" if missing or targets["dropped"]["unmapped"] else "ready"
    return envelope(status, result, missing=missing, warnings=warnings,
                    sources=[{"title": "SEC Form 13F FAQ", "url": DOCS["form_13f_faq"]}],
                    assumptions=assumptions + [f"Single-name cap {_pct(cap)} applied to the {cap_basis}; excess "
                                               "weight is redistributed pro rata to the names under the cap."])


def _policy_checks(ips: Mapping[str, Any], rows: list[dict[str, Any]], currency: str,
                   constraints: Mapping[str, Any], situation: Any) -> dict[str, Any]:
    from .. import policy
    checks = []
    for row in rows:
        situs = row.get("estate_situs")
        proposal = {"kind": "trade", "action": "buy", "symbol": row["ticker"], "amount": row["amount"],
                    "currency": currency, "asset_class": "stock", "instrument": "stock", "tags": [],
                    "domicile": {"us": "US", "not_us": "NON-US"}.get(situs)}
        if constraints.get("funding"):
            proposal["funding"] = constraints["funding"]
        try:
            report = policy.check(ips, proposal, constraints.get("portfolio"))
        except ValueError as exc:
            return {"verdict": "warn", "rules": [], "error": str(exc)}
        checks.append({"ticker": row["ticker"], "verdict": report["verdict"],
                       "rules": [r for r in report["rules"] if r["status"] != "pass"]})
    verdict = max((c["verdict"] for c in checks), key=policy.STATUS_ORDER.index, default="pass")
    return {"verdict": verdict, "by_name": checks,
            "note": "Each buy is checked on its own against the supplied portfolio (policy.check)."}


def _handoff(rows: list[dict[str, Any]], targets: Mapping[str, Any], constraints: Mapping[str, Any],
             warnings: list[str]) -> dict[str, Any]:
    from .. import rebalance
    sleeves = [{"name": r["ticker"], "weight": str(round(r["weight"], 6)), "kind": "equity", "buy": [r["ticker"]]}
               for r in rows]
    total = sum(float(s["weight"]) for s in sleeves)
    cash = round(1 - total, 6)
    if cash > 1e-6:
        sleeves.append({"name": "cash", "weight": str(cash), "kind": "cash", "buy": []})
    elif sleeves:
        sleeves[0]["weight"] = str(round(float(sleeves[0]["weight"]) + (1 - total), 6))
    jc = json.loads(json.dumps(constraints["jurisdiction_context"]))
    instruments = jc.setdefault("instruments", {})
    for row in rows:
        entry = instruments.setdefault(row["ticker"], {})
        if row.get("price") is not None and "price" not in entry and row.get("price_basis") == "supplied":
            entry.update(price=row["price"], currency="USD")
        if row.get("mexico") and isinstance(row["mexico"].get("sic_listed"), bool):
            entry.setdefault("sic_listed", row["mexico"]["sic_listed"])
    try:
        return rebalance.plan(constraints["household"], {"by": "instrument", "sleeves": sleeves},
                              jurisdiction_context=jc, cash_flows=constraints.get("cash_flows"),
                              tax_inputs=constraints.get("tax_inputs"),
                              constraints=constraints.get("rebalance_constraints"))
    except ValueError as exc:
        warnings.append(f"The trade list could not be built: {exc}")
        return envelope("needs_input", {}, missing=[str(exc)])
