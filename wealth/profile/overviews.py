"""Overview figures, goals and upcoming dates for the profile page."""
from __future__ import annotations

from datetime import date, timedelta
from typing import Callable, Iterable

from .helpers import _as_date, _facts_by_key, _humanize, _num, _ratio, _stale
from .classify import _LABELS, account_label, classify_key


# ---------------------------------------------------------------- overview

def _converter(household: dict) -> Callable[[float, str], float | None]:
    reporting = household.get("currency")
    rates: dict[tuple[str, str], float] = {}
    for item in household.get("fx") or []:
        if not isinstance(item, dict):
            continue
        rate = _num(item.get("rate"))
        if rate and rate > 0 and item.get("from") and item.get("to"):
            rates[(item["from"], item["to"])] = rate
            rates.setdefault((item["to"], item["from"]), 1 / rate)

    def convert(value: float, currency: str) -> float | None:
        if currency == reporting:
            return value
        rate = rates.get((currency, reporting))
        return value * rate if rate else None
    return convert


def _rows(items: Iterable[dict], labels: dict[str, str] | None = None) -> list[dict]:
    rows = []
    for item in items:
        value = _num(item.get("value"))
        rows.append({"name": (labels or {}).get(item.get("name"), item.get("name")),
                     "id": item.get("name"), "value": round(value, 2) if value is not None else None,
                     "weight": _num(item.get("weight_of_known_assets"))})
    return rows


def overview(snapshot: dict, today: date) -> dict:
    fact = _facts_by_key(snapshot).get("household")
    if not fact or not isinstance(fact.get("value"), dict):
        return {"status": "empty"}
    household = fact["value"]
    base = {"as_of": household.get("as_of"), "currency": household.get("currency")}
    if _stale(fact, today):
        base["stale"] = True
    if fact.get("confidence") == "inferred":
        base["unconfirmed"] = True
    try:
        from ..household import run as household_run
        report = household_run("exposure", {}, {"household": household})
    except Exception as exc:  # malformed or legacy household
        return {**base, "status": "invalid"}
    if report.get("status") not in {"ready", "partial"}:
        return {**base, "status": "invalid"}
    result = report.get("result") or {}
    exposures = result.get("exposures") or {}
    currency = result.get("currency") or household.get("currency")
    known_assets = _num(result.get("known_assets"))
    liquid = _num(result.get("liquid_capital"))
    people = {p.get("id"): p.get("name") or p.get("id") for p in household.get("people") or [] if isinstance(p, dict)}
    accounts = {a.get("id"): a for a in household.get("accounts") or [] if isinstance(a, dict)}
    account_labels = {aid: str(a.get("name") or f"{aid} · {a.get('type', '')}".strip(" ·")) for aid, a in accounts.items()}
    account_labels["external"] = "Outside accounts"
    convert = _converter(household)

    # Symbols for instruments, including fund constituents.
    symbols: dict[str, str] = {}
    fresh_funds: set[str] = set()
    as_of = _as_date(household.get("as_of"))
    for fund in household.get("fund_holdings") or []:
        if not isinstance(fund, dict):
            continue
        fund_date = _as_date(fund.get("as_of"))
        if as_of and fund_date and (as_of - fund_date).days <= 90:
            fresh_funds.add(fund.get("instrument_id"))
        for holding in fund.get("holdings") or []:
            if isinstance(holding, dict) and holding.get("symbol"):
                symbols.setdefault(holding.get("instrument_id"), holding["symbol"])

    positions, by_currency = [], {}
    direct: dict[str, float] = {}
    direct_total = through_funds_total = 0.0
    for position in household.get("positions") or []:
        if not isinstance(position, dict):
            continue
        native = _num(position.get("value"))
        pcur = position.get("currency")
        value = convert(native, pcur) if native is not None and pcur else None
        symbols.setdefault(position.get("instrument_id"), position.get("symbol") or position.get("instrument_id"))
        if pcur:
            bucket = by_currency.setdefault(pcur, {"native": 0.0, "value": 0.0, "unconverted": 0})
            bucket["native"] += native or 0.0
            if value is None:
                bucket["unconverted"] += 1
            else:
                bucket["value"] += value
        if value is None:
            continue
        account = accounts.get(position.get("account_id"), {})
        if position.get("instrument_id") in fresh_funds:
            through_funds_total += value
        else:
            direct_total += value
            direct[position.get("instrument_id")] = direct.get(position.get("instrument_id"), 0.0) + value
        positions.append({
            "symbol": position.get("symbol") or position.get("instrument_id"),
            "account": account_labels.get(position.get("account_id"), position.get("account_id")),
            "value": round(value, 2), "weight": _ratio(value, known_assets),
            "native": {"amount": round(native, 2), "currency": pcur} if pcur != currency else None,
        })
    for item in household.get("external_assets") or []:
        if not isinstance(item, dict) or _num(item.get("value")) is None or not item.get("currency"):
            continue
        native = _num(item["value"])
        value = convert(native, item["currency"])
        bucket = by_currency.setdefault(item["currency"], {"native": 0.0, "value": 0.0, "unconverted": 0})
        bucket["native"] += native
        if value is None:
            bucket["unconverted"] += 1
        else:
            bucket["value"] += value
            direct_total += value
    positions.sort(key=lambda row: -row["value"])
    liabilities = []
    for item in household.get("liabilities") or []:
        if not isinstance(item, dict):
            continue
        native = _num(item.get("value"))
        value = convert(native, item.get("currency")) if native is not None and item.get("currency") else None
        liabilities.append({"name": item.get("name") or _humanize(item.get("id") or "debt"),
                            "value": round(value, 2) if value is not None else None})

    currency_rows = [{"name": cur, "id": cur, "value": round(b["value"], 2), "weight": _ratio(b["value"], known_assets),
                      "native": {"amount": round(b["native"], 2), "currency": cur} if cur != currency else None}
                     for cur, b in sorted(by_currency.items(), key=lambda kv: -kv[1]["value"])]

    lookthrough = []
    for row in exposures.get("instrument") or []:
        total = _num(row.get("value")) or 0.0
        instrument = str(row.get("name"))
        own = direct.get(instrument, 0.0)
        via = max(0.0, total - own)
        if instrument.startswith("unknown:"):
            label = "unidentified"
        elif instrument.startswith("external:"):
            continue
        else:
            label = symbols.get(instrument, instrument)
        if via > 0:  # only holdings reached through a fund need explaining
            lookthrough.append({"instrument": label, "direct": round(own, 2), "via_funds": round(via, 2),
                                "total": round(total, 2)})
    lookthrough.sort(key=lambda r: -r["total"])

    return {
        **base, "status": report["status"], "currency": currency,
        "net_worth": _num(result.get("known_nav")), "known_assets": known_assets,
        "known_liabilities": _num(result.get("known_liabilities")),
        "liquid": liquid,
        "allocations": {
            "asset_class": _rows(exposures.get("asset_class") or []),
            "currency": currency_rows,
            "account": _rows(exposures.get("account") or [], account_labels),
            "owner": _rows(exposures.get("person") or [], {k: str(v) for k, v in people.items()}),
        },
        "liabilities": liabilities,
        "top_positions": positions[:5],
        "lookthrough": {
            "direct": round(direct_total, 2), "through_funds": round(through_funds_total, 2),
            "rows": lookthrough[:6],
        },
    }


def situation_overview(sit: dict) -> dict:
    """Net worth and holdings from the canonical model: stated balances, statements and debts."""
    nw = sit["net_worth"]
    if nw["total"] is None:
        return {"status": "empty"}
    currency = nw["currency"]
    dated = [a["as_of"] for a in sit["accounts"] if a.get("as_of")]
    accounts = [{"name": r.get("institution") or r.get("name") or r["id"], "id": r["key"], "value": r["value"]}
                for r in sit["cash"] if r["counted"] and r["value"] is not None]
    accounts += [{"name": a["label"], "id": a["key"], "value": a["value"]} for a in sit["accounts"]
                 if a["value"] is not None and not a.get("superseded_by")]
    accounts += [{"name": r.get("institution") or r.get("name") or r["id"], "id": r["key"], "value": r["value"]}
                 for r in sit["investments"] if r["counted"] and r["value"] is not None]
    assets = _num(nw["assets"]) or 0.0
    rows = [{**r, "weight": _ratio(_num(r["value"]), assets)} for r in sorted(accounts, key=lambda r: -(r["value"] or 0))]
    by_currency = {c: v for c, v in nw["by_currency"].items()}
    currency_rows = [{"name": c, "id": c, "value": None if c != currency else v,
                      "weight": None, "native": {"amount": v, "currency": c} if c != currency else None}
                     for c, v in sorted(by_currency.items())]
    view = {
        "status": "ready" if nw["complete"] else "partial", "source": "model",
        "as_of": max(dated) if dated else sit["as_of"], "currency": currency,
        "net_worth": _num(nw["total"]), "known_assets": _num(nw["assets"]),
        "known_liabilities": _num(nw["liabilities"]), "liquid": _num(nw["liquid"]),
        "allocations": {"asset_class": [], "currency": currency_rows, "account": rows, "owner": []},
        "liabilities": [{"name": r.get("name") or r["kind"], "value": r["value"]} for r in sit["liabilities"]],
        "top_positions": [], "lookthrough": {"direct": _num(nw["assets"]), "through_funds": 0.0, "rows": []},
    }
    if any(sit["meta"].get(k, {}).get("stale") for k in (a["key"] for a in sit["accounts"] if a.get("key"))):
        view["stale"] = True
    return view


def differences(sit: dict) -> list[dict]:
    """What the person said vs what a statement shows: one quiet line each, nothing overwritten."""
    return [{"institution": d["institution"], "stated": d["stated"], "stated_approximate": d["stated_approximate"],
             "statement": d["statement"], "as_of": d["as_of"]}
            for d in sit["differences"] if d.get("statement")]


# ---------------------------------------------------------------- goals

def goals_view(snapshot: dict, today: date) -> dict:
    facts = _facts_by_key(snapshot)
    fact = facts.get("goals")
    goals = fact.get("value") if fact else None
    if not isinstance(goals, list):
        return {"status": "unknown", "items": []}
    plan, plan_status = None, "unavailable"
    try:
        from ..workflows import prepare
        packet = prepare({"client": snapshot["client"], "facts": snapshot["facts"], "decisions": []}, "plan", today)
        calc = packet.get("calculations") if isinstance(packet, dict) else None
        if isinstance(calc, dict) and calc.get("goal_requirements") is not None:
            plan, plan_status = calc, "ready"
        elif isinstance(packet, dict) and packet.get("missing"):
            # Funding stays unknown until every goal and planning resource is complete.
            plan_status = "incomplete"
    except Exception:
        plan = None
    shortfall = _num(((plan or {}).get("funding_shortfall") or {}).get("amount")) if plan else None
    items = []
    for index, goal in enumerate(goals):
        if not isinstance(goal, dict):
            continue
        currency = goal.get("currency") if isinstance(goal.get("currency"), str) else None
        target = _num(goal.get("target_amount", goal.get("target")))
        outside = _num(goal.get("funded_outside_pool"))
        explicit = next((_num(goal.get(k)) for k in ("reserved_amount", "funded_amount", "current_amount")
                         if _num(goal.get(k)) is not None), None)
        reserved, basis = None, None
        if explicit is not None:
            reserved, basis = explicit, "stated"
        elif plan is not None and target is not None and outside is not None:
            if goal.get("protect_now") is True and shortfall == 0:
                reserved, basis = target, "plan_reserved"
            elif goal.get("protect_now") is False:
                reserved, basis = outside, "outside_pool"
        due = _as_date(goal.get("due") or goal.get("target_date"))
        items.append({
            "id": str(goal.get("id") or index), "name": goal.get("name") or goal.get("id"),
            "target": {"amount": target, "currency": currency} if target is not None else None,
            "due": due.isoformat() if due else None,
            "timing": None if due else (goal.get("timing") or goal.get("when") or goal.get("horizon")),
            "reserved": {"amount": round(reserved, 2), "currency": currency} if reserved is not None else None,
            "reserved_basis": basis,
            "funded_ratio": _ratio(reserved, target) if reserved is not None else None,
            "protect_now": goal.get("protect_now") if isinstance(goal.get("protect_now"), bool) else None,
            "days_left": (due - today).days if due else None,
        })
    items.sort(key=lambda g: (g["due"] is None, g["due"] or "", str(g["name"])))
    return {"status": "stale" if _stale(fact, today) else "ready", "review_on": fact.get("expires_on"),
            "plan_status": plan_status,
            "plan_shortfall": {"amount": shortfall, "currency": plan.get("currency")} if plan and shortfall else None,
            "items": items}


# ---------------------------------------------------------------- upcoming

def fact_labels(sit: dict | None, language: str | None = None) -> dict[str, str]:
    """Human names for canonical fact keys (institution, account type, debt kind), never raw ids."""
    if not sit:
        return {}
    lang = language if language in _LABELS else "en"
    labels = {a["key"]: account_label(a, lang) for a in sit["accounts"] if a.get("key")}
    for row in [*sit["cash"], *sit["investments"]]:
        if row["key"].startswith(("cash.", "investment.")):
            labels[row["key"]] = row.get("institution") or row.get("name") or _humanize(row["id"])
    for row in sit["liabilities"]:
        if row["key"].startswith("liability."):
            labels[row["key"]] = row.get("name") or _LABELS[lang].get(row["kind"], _LABELS[lang].get("other", "Loan"))
    for row in sit["income"]["items"] + sit["income"]["extras"]:
        if row["key"].startswith("income.") and row["key"] != "income.schedule":
            labels[row["key"]] = row.get("name") or _LABELS[lang].get(row.get("kind") or "income", "Income")
    return labels


def upcoming(snapshot: dict, today: date, horizon_days: int = 60, labels: dict[str, str] | None = None) -> list[dict]:
    """Dated things to act on that the rows do not already show.

    Stale facts ask "Still true?" on their own row and goals show their date,
    so neither is repeated here.
    """
    items = []
    keys = {f["key"] for f in snapshot["facts"] if f.get("value") is not None}
    for fact in snapshot["facts"]:
        review = _as_date(fact.get("expires_on"))
        if fact.get("value") is None or review is None or classify_key(fact["key"]) == "analysis":
            continue
        key = fact["key"]
        if key.startswith("account.") and key.endswith(".activity"):
            # A statement's activity is reviewed with its account: one line, under the account's name.
            key = key[: -len(".activity")]
            if key in keys:
                continue
        if today <= review <= today + timedelta(days=horizon_days):
            items.append({"type": "review", "date": review.isoformat(), "key": key,
                          "label": (labels or {}).get(key) or _humanize(key)})
    for decision in snapshot["decisions"]:
        status = decision.get("status")
        if status == "proposed" or (status == "accepted" and decision.get("needs_review")):
            items.append({"type": "decision" if status == "proposed" else "decision_review",
                          "date": str(decision.get("created_at") or "")[:10] or None,
                          "label": str(decision.get("title") or "Decision")})
    order = {"decision": 0, "decision_review": 0, "review": 1}
    return sorted(items, key=lambda i: (order[i["type"]], i["date"] or "9999"))
