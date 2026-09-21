"""Read model for the "My profile" dashboard, plus the fact edits it can request.

Everything here is pure with respect to storage: ``profile_view`` only reads
through ``WealthService.inspect``; ``fact_action`` and ``form_facts`` return fact
payloads that the web layer passes to ``WealthService.remember`` with the
revision the page was rendered at.  Unknown values stay ``None``; they are never zero.
The code tolerates store shape differences (missing fields, legacy values).
"""
from __future__ import annotations

import math
import re
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Iterable

FORM_FIELDS = ("income", "spending", "savings", "investments", "debts", "dependents",
               "tax_residence", "currencies", "goals", "risk")
# client.profile field written by the "Complete your profile" form for each amount field.
_FORM_PROFILE_FIELDS = {"income": "monthly_income", "spending": "monthly_spending", "savings": "savings",
                        "investments": "investments", "debts": "debts"}
_MARKET_VALUED = ("household", "portfolio.snapshot")
_CURRENCY = re.compile(r"^[A-Z]{3}$")


# ---------------------------------------------------------------- small helpers

def _as_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str) and value:
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return None
    return None


def _today(today: Any) -> date:
    parsed = _as_date(today)
    return parsed or datetime.now(timezone.utc).date()


def _num(value: Any) -> float | None:
    """A finite number from a number or numeric string; ``None`` when unknown."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value) if math.isfinite(value) else None
    if isinstance(value, Decimal):
        return float(value) if value.is_finite() else None
    if isinstance(value, str):
        text = value.strip().replace(",", "")
        if not text:
            return None
        try:
            parsed = Decimal(text)
        except InvalidOperation:
            return None
        return float(parsed) if parsed.is_finite() else None
    return None


def _ratio(numerator: float | None, denominator: float | None) -> float | None:
    if numerator is None or not denominator:
        return None
    return round(numerator / denominator, 6)


def _humanize(name: str) -> str:
    words = re.sub(r"[._]+", " ", str(name)).strip().split()
    if not words:
        return str(name)
    words = [w.upper() if w.lower() in _ACRONYMS else w for w in words]
    return " ".join([words[0][:1].upper() + words[0][1:], *words[1:]])


def _stale(fact: dict, today: date) -> bool:
    if isinstance(fact.get("stale"), bool) and today == datetime.now(timezone.utc).date():
        return fact["stale"]  # computed by the service
    expires = _as_date(fact.get("expires_on"))
    return bool(expires and expires < today)


def _snapshot(service: Any, client_id: str) -> dict:
    snapshot = service.inspect(client_id) if hasattr(service, "inspect") else service.client("inspect", client_id)
    if not isinstance(snapshot, dict):
        raise ValueError("client snapshot has an unexpected shape")
    facts = [f for f in snapshot.get("facts") or [] if isinstance(f, dict) and isinstance(f.get("key"), str)]
    decisions = [d for d in snapshot.get("decisions") or [] if isinstance(d, dict)]
    client = snapshot.get("client") if isinstance(snapshot.get("client"), dict) else {}
    return {"client": client, "facts": facts, "decisions": decisions}


def _history(service: Any, client_id: str, key: str) -> list[dict]:
    try:
        result = service.inspect(client_id, detail="history", key=key)
    except Exception:  # history is optional context for the dashboard
        return []
    rows = result.get("history") if isinstance(result, dict) else None
    return [row for row in rows or [] if isinstance(row, dict)]


def _facts_by_key(snapshot: dict) -> dict[str, dict]:
    return {fact["key"]: fact for fact in snapshot["facts"]}


def _dict_value(fact: dict | None) -> dict:
    value = fact.get("value") if fact else None
    return value if isinstance(value, dict) else {}


# ---------------------------------------------------------------- classification

_FIELD_KINDS = (
    ("debts", r"debt|loan|mortgage|liabilit|credit|owe"),
    ("income", r"income|salary|wage|earn|pension|dividend|bonus|payroll"),
    ("spending", r"spend|expense|essential|rent|budget|cost|outgo"),
    ("goals", r"goal"),
    ("preferences", r"prefer|risk|tolerance|horizon|style"),
    ("constraints", r"constraint|restrict|avoid|exclu"),
    ("holdings", r"saving|invest|cash|capital|portfolio|asset|account|holding|pool|reserve|brokerage|retirement"),
)


def _field_kind(name: str) -> str:
    lowered = name.lower()
    for kind, pattern in _FIELD_KINDS:
        if re.search(pattern, lowered):
            return kind
    return "profile"


def classify_key(key: str) -> str:
    if key == "goals" or key.startswith("goal."):
        return "goals"
    if key.startswith(("preference.", "preferences.")):
        return "preferences"
    if key.startswith(("constraint.", "constraints.")):
        return "constraints"
    if key in _MARKET_VALUED or key.startswith(("account.", "lot.", "holding.", "portfolio.")):
        return "holdings"
    if key.startswith(("income.", "salary.")) or key == "planning.income":
        return "income"
    if key.startswith(("spending.", "expense.", "expenses.", "budget.")):
        return "spending"
    if key.startswith(("debt.", "debts.", "liability.", "loan.", "mortgage.")):
        return "debts"
    if key.startswith(("client.", "tax.", "profile.", "person.", "household.")):
        return "profile"
    if key.startswith(("analysis.", "research.", "thesis.", "planning.", "monitor.", "performance.")):
        return "analysis"
    return _field_kind(key)


_EXPLODED_KEYS = ("client.profile", "plan.resources")
# plan.resources plumbing that is not a fact about the person; shown only in the detail sheet.
_HIDDEN_FIELDS = {"plan.resources": {"currency", "reserve_outside_pool", "debt_payments_from_pool",
                                     "outside_sources", "reserve_funding"},
                  "client.profile": {"reporting_currency", "locale", "language"}}
# Human sections, in display order, and the old kind each one absorbs.
GROUPS = ("money_in", "money_out", "own", "owe", "goals", "invest", "about")
_KIND_GROUP = {"income": "money_in", "spending": "money_out", "holdings": "own", "debts": "owe",
               "goals": "goals", "preferences": "invest", "constraints": "invest", "profile": "about"}
_FIELD_GROUP = {"income": "money_in", "spending": "money_out", "savings": "own", "investments": "own",
                "debts": "owe", "goals": "goals", "risk": "invest", "dependents": "about",
                "tax_residence": "about", "currencies": "about"}
_TOP_ITEMS = 3
_PLANNING_FIELDS = {"available_capital", "cash_available", "reserve_months"}
_ACRONYMS = {"esg", "etf", "fx", "usd", "mxn", "ira", "afore", "cetes", "ipc", "sp"}


def _editor(value: Any) -> str | None:
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "text"
    if isinstance(value, dict) and set(value) <= {"amount", "currency", "period", "note"} and "amount" in value:
        return "amount"
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return "list"
    return None


def _summary(key: str, value: Any) -> dict:
    if key == "household" and isinstance(value, dict):
        return {"type": "household", "accounts": len(value.get("accounts") or []),
                "positions": len(value.get("positions") or [])}
    if isinstance(value, list):
        return {"type": "list", "items": len(value)}
    return {"type": "object", "fields": len(value) if isinstance(value, dict) else 0}


def _source_label(kind: Any) -> str:
    return {"user": "you_said", "document": "document", "web": "web"}.get(kind, "derived")


def _entry(fact: dict, today: date, *, field: str | None = None, value: Any = None, label: str) -> dict:
    """Only what a memory row renders; everything else comes from ``fact_detail`` on tap."""
    parent = fact.get("value")
    sibling = parent.get("currency") if field and isinstance(parent, dict) else None
    money_hint = sibling if isinstance(sibling, str) and _CURRENCY.match(sibling) and field != "currency" \
        and isinstance(value, (int, float)) and not isinstance(value, bool) \
        and not re.search(r"months|count|dependents|years", field or "") else None
    source = fact.get("source") if isinstance(fact.get("source"), dict) else {}
    editor = "goal" if fact["key"] == "goals" and field else _editor(value)
    entry = {
        "id": f"{fact['key']}#{field}" if field else fact["key"],
        "key": fact["key"], "field": field, "label": label,
        "value": value if editor else _summary(fact["key"], value),
        "editor": editor,
        "source": _source_label(source.get("kind")), "observed_on": source.get("observed_on"),
    }
    if money_hint:
        entry["currency"] = money_hint
    if _stale(fact, today):
        entry["stale"] = True
        entry["can_confirm"] = fact["key"] not in _MARKET_VALUED and not fact["key"].startswith(("analysis.", "research."))
    if fact.get("confidence") == "inferred":
        entry["unconfirmed"] = True
    return entry


def _headline(group: str, entries: list[dict]) -> dict:
    """A group total that always equals the sum of the rows it heads."""
    if group == "goals":
        return {"count": sum(1 for e in entries if e["editor"] == "goal")}
    if group in {"money_in", "money_out", "owe", "own"}:
        amounts = []
        for e in entries:
            v = e.get("value")
            if e["editor"] == "amount" or e.get("derived"):
                amount = _num(v.get("amount")) if isinstance(v, dict) else None
                if amount is None:
                    return {"count": len(entries)}  # an unknown row means no honest total
                amounts.append((amount, v.get("currency"), v.get("period")))
            elif e["editor"] == "number" and e.get("currency"):
                amounts.append((float(v), e["currency"], None))
            else:
                return {"count": len(entries)}
        if amounts and len({(c, p) for _, c, p in amounts}) == 1:
            _, currency, period = amounts[0]
            head = {"amount": round(sum(a for a, _, _ in amounts), 2), "currency": currency}
            if period:
                head["period"] = period
            return head
    return {"count": len(entries)}


def _household_rows(fact: dict, overview: dict, today: date) -> tuple[list[dict], list[dict]]:
    """Accounts (assets) and liabilities from the statement, one row each, read-only."""
    source = fact.get("source") if isinstance(fact.get("source"), dict) else {}
    currency = overview.get("currency")

    def row(kind: str, name: Any, amount: Any, index: int) -> dict:
        entry = {"id": f"household#{kind}{index}", "key": "household", "field": None, "label": str(name),
                 "value": {"amount": amount, "currency": currency}, "editor": None, "derived": True,
                 "source": _source_label(source.get("kind")), "observed_on": source.get("observed_on")}
        if overview.get("stale"):
            entry["stale"] = True
            entry["can_confirm"] = False
        return entry

    accounts = [row("account", r.get("name") or r.get("id"), r.get("value"), i)
                for i, r in enumerate((overview.get("allocations") or {}).get("account") or [])]
    debts = [row("debt", r.get("name"), r.get("value"), i) for i, r in enumerate(overview.get("liabilities") or [])]
    return accounts, debts


def memory_groups(snapshot: dict, today: date, overview: dict | None = None,
                  goals: dict | None = None, missing: Iterable[str] = ()) -> list[dict]:
    buckets: dict[str, list[dict]] = {group: [] for group in GROUPS}
    funding = {g["id"]: g for g in (goals or {}).get("items", [])}
    ov = overview or {}
    for fact in snapshot["facts"]:
        value = fact.get("value")
        if value is None:  # a retracted fact
            continue
        key = fact["key"]
        if key in _EXPLODED_KEYS and isinstance(value, dict):
            hidden = _HIDDEN_FIELDS.get(key, set())
            for name, item in value.items():
                if item is None or name in hidden:
                    continue
                # Planning resources fund goals; they are not assets and must not sit beside holdings.
                group = "goals" if key == "plan.resources" and name in _PLANNING_FIELDS \
                    else _KIND_GROUP[_field_kind(name)]
                buckets[group].append(_entry(fact, today, field=name, value=item, label=_humanize(name)))
        elif key == "goals" and isinstance(value, list):
            for index, goal in enumerate(value):
                if not isinstance(goal, dict):
                    continue
                goal_id = str(goal.get("id") or index)
                entry = _entry(fact, today, field=goal_id, value=goal, label=str(goal.get("name") or goal_id))
                entry["funded_ratio"] = (funding.get(goal_id) or {}).get("funded_ratio")
                buckets["goals"].append(entry)
        elif key == "household" and ov.get("status") in {"ready", "partial"}:
            accounts, debts = _household_rows(fact, ov, today)
            buckets["own"].extend(accounts)
            buckets["owe"].extend(debts)
        else:
            kind = classify_key(key)
            if kind == "analysis":  # saved tool output, not a fact about the person
                continue
            label = key.split(".", 1)[1] if key.startswith(("preference.", "constraint.")) else key
            buckets[_KIND_GROUP[kind]].append(_entry(fact, today, value=value, label=_humanize(label)))
    missing_by_group: dict[str, list[str]] = {}
    for name in missing:
        missing_by_group.setdefault(_FIELD_GROUP[name], []).append(name)
    out = []
    for group in GROUPS:
        entries = buckets[group]
        if group == "goals":  # goals first, then the resources that fund them
            entries.sort(key=lambda e: (e["editor"] != "goal", not e.get("stale")))
        elif not any(e.get("derived") for e in entries):  # statement rows keep value order
            entries.sort(key=lambda e: (not e.get("stale"), e["label"].lower()))
        if not entries and not missing_by_group.get(group):
            continue
        out.append({"id": group, "headline": _headline(group, entries), "count": len(entries),
                    "entries": entries, "top": _TOP_ITEMS, "missing": missing_by_group.get(group, [])})
    return out


# ---------------------------------------------------------------- completeness

def completeness(snapshot: dict) -> dict:
    facts = {k: f for k, f in _facts_by_key(snapshot).items() if f.get("value") is not None}
    profile = _dict_value(facts.get("client.profile"))
    resources = _dict_value(facts.get("plan.resources"))
    household = _dict_value(facts.get("household"))

    def has(*names: str) -> bool:
        return any(profile.get(name) not in (None, "", []) for name in names)

    def prefixed(*prefixes: str) -> bool:
        return any(key.startswith(prefixes) for key in facts)

    goals = facts.get("goals", {}).get("value") if "goals" in facts else None
    unknown_sections = household.get("unknown_sections") or []
    residences = [r for person in household.get("people") or [] if isinstance(person, dict)
                  for r in person.get("tax_residencies") or []]
    checks = {
        "income": has("monthly_income", "income", "annual_income", "salary") or prefixed("income.") or "planning.income" in facts,
        "spending": has("monthly_spending", "spending", "expenses", "monthly_expenses")
        or resources.get("monthly_essentials") is not None or prefixed("spending.", "expense"),
        "savings": has("savings", "cash", "emergency_fund") or resources.get("cash_available") is not None,
        "investments": has("investments", "portfolio") or bool(household.get("positions")) or "portfolio.snapshot" in facts,
        "debts": has("debts", "debt", "liabilities") or prefixed("debt.", "liability.")
        or ("liabilities" in household and "liabilities" not in unknown_sections),
        "dependents": has("dependents", "children"),
        "tax_residence": has("tax_residence", "tax_residences", "tax_residency") or bool(residences) or "tax.profile" in facts,
        "currencies": has("currencies", "reporting_currency", "currency"),
        "goals": isinstance(goals, list),  # an explicit empty list means "no goals", which is known
        "risk": prefixed("preference.risk") or has("risk_tolerance", "risk_preference", "risk"),
    }
    return {"known": [n for n in FORM_FIELDS if checks[n]], "missing": [n for n in FORM_FIELDS if not checks[n]]}


def fact_detail(service: Any, client_id: str, key: str, today: Any = None) -> dict:
    """Everything the row hides, fetched when the person taps a fact."""
    today = _today(today)
    snapshot = _snapshot(service, client_id)
    fact = next((f for f in snapshot["facts"] if f["key"] == key and f.get("value") is not None), None)
    if fact is None:
        raise LookupError("no current fact with that key")
    source = fact.get("source") if isinstance(fact.get("source"), dict) else {}
    return {
        "key": key, "value": fact["value"],
        "source": {"label": _source_label(source.get("kind")), "ref": source.get("ref"),
                   "observed_on": source.get("observed_on")},
        "confidence": fact.get("confidence"), "recorded_at": fact.get("recorded_at"),
        "review_on": fact.get("expires_on"), "stale": _stale(fact, today),
        "revisions": len(_history(service, client_id, key)) or 1,
    }


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
        from .household import run as household_run
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


# ---------------------------------------------------------------- performance math

def _series(rows: Any) -> list[tuple[date, float]]:
    out = {}
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        when, value = _as_date(row.get("date") or row.get("as_of")), _num(row.get("value", row.get("amount")))
        if when is not None and value is not None:
            out[when] = out.get(when, 0.0) + value if "amount" in row and "value" not in row else value
    return sorted(out.items())


def time_weighted_return(valuations: list[tuple[date, float]], flows: list[tuple[date, float]]) -> float | None:
    """Chain-linked TWR over consecutive valuations.

    Valuations are end-of-day and include that day's flows, so a flow on a
    valuation date belongs to the period ending there.  Within each sub-period
    flows are day-weighted (Modified Dietz); with a valuation on every flow date
    this is exact daily-valuation TWR.  Flows are positive when money enters.
    """
    points = sorted(valuations)
    if len(points) < 2:
        return None
    growth = 1.0
    for (start, v0), (end, v1) in zip(points, points[1:]):
        days = (end - start).days
        if days <= 0:
            return None
        period_flows = [(d, a) for d, a in flows if start < d <= end]
        net = sum(a for _, a in period_flows)
        weighted = sum(a * (end - d).days / days for d, a in period_flows)
        denominator = v0 + weighted
        if denominator <= 0:
            if v0 == 0 and v1 == 0 and net == 0:
                continue
            return None
        growth *= 1 + (v1 - v0 - net) / denominator
    return growth - 1


def xirr(cashflows: list[tuple[date, float]]) -> float | None:
    """Annualized money-weighted return (investor view: deposits negative).

    Brackets a sign change on a grid expanding outward from 0 %, then bisects.
    Returns ``None`` when no root exists in (-99.99 %, 1e6 %).
    """
    flows = sorted((d, a) for d, a in cashflows if a)
    if len(flows) < 2 or all(a > 0 for _, a in flows) or all(a < 0 for _, a in flows):
        return None
    origin = flows[0][0]
    years = [((d - origin).days / 365.0, a) for d, a in flows]

    def npv(rate: float) -> float:
        return sum(a / (1 + rate) ** t for t, a in years)

    grid = [0.0]
    step = 0.01
    while step < 1e4:
        grid.extend([step, -min(step, 0.9999)])
        step *= 1.6
    grid.append(-0.9999)
    grid = sorted(set(grid), key=abs)
    # Walk outward: check neighbouring grid points on each side of zero.
    positives = sorted(r for r in grid if r >= 0)
    negatives = sorted((r for r in grid if r <= 0), reverse=True)
    candidates = []
    for side in (positives, negatives):
        for lo, hi in zip(side, side[1:]):
            try:
                f_lo, f_hi = npv(lo), npv(hi)
            except (OverflowError, ZeroDivisionError):
                break
            if f_lo == 0:
                return lo
            if f_lo * f_hi < 0:
                candidates.append((min(lo, hi), max(lo, hi)))
                break
    if not candidates:
        return None
    lo, hi = min(candidates, key=lambda pair: min(abs(pair[0]), abs(pair[1])))
    f_lo = npv(lo)
    for _ in range(200):
        mid = (lo + hi) / 2
        f_mid = npv(mid)
        if abs(f_mid) < 1e-9 or hi - lo < 1e-12:
            return mid
        if f_lo * f_mid < 0:
            hi = mid
        else:
            lo, f_lo = mid, f_mid
    return (lo + hi) / 2


def _annualize(total: float | None, days: int) -> float | None:
    if total is None or days < 365 or total <= -1:
        return None
    return (1 + total) ** (365.0 / days) - 1


def _value_on(series: list[tuple[date, float]], when: date) -> float | None:
    prior = [v for d, v in series if d <= when]
    return prior[-1] if prior else None


def performance(snapshot: dict, household_history: list[dict], today: date) -> dict:
    facts = _facts_by_key(snapshot)
    fact = next((facts[k] for k in ("performance.history", "portfolio.history") if k in facts
                 and isinstance(facts[k].get("value"), dict)), None)
    if fact is None:
        snapshots = {row.get("value", {}).get("as_of") for row in household_history
                     if isinstance(row.get("value"), dict) and row["value"].get("as_of")}
        return {"status": "insufficient", "reason": "flows_unknown" if len(snapshots) >= 2 else "no_history",
                "statements": len(snapshots)}
    value = fact["value"]
    valuations = [(d, v) for d, v in _series(value.get("valuations")) if d <= today]
    if "cash_flows" not in value and "flows" not in value:
        return {"status": "insufficient", "reason": "flows_unknown", "statements": len(valuations)}
    flows = _series([{"date": r.get("date"), "amount": r.get("amount")}
                     for r in (value.get("cash_flows", value.get("flows")) or []) if isinstance(r, dict)])
    if len(valuations) < 2 or (valuations[-1][0] - valuations[0][0]).days < 28:
        return {"status": "insufficient", "reason": "short_history", "statements": len(valuations)}
    start, end = valuations[0][0], valuations[-1][0]
    days = (end - start).days
    in_range = [(d, a) for d, a in flows if start < d <= end]
    twr = time_weighted_return(valuations, in_range)
    irr = xirr([(start, -valuations[0][1]), *[(d, -a) for d, a in in_range], (end, valuations[-1][1])])
    index, growth = [], 1.0
    for i, (d, v) in enumerate(valuations):
        if i:
            step = time_weighted_return(valuations[i - 1:i + 1], in_range)
            growth *= 1 + step if step is not None else 1
        index.append({"date": d.isoformat(), "value": round(v, 2), "index": round(100 * growth, 4)})
    benchmark = None
    raw_bench = value.get("benchmark")
    if isinstance(raw_bench, dict):
        bench = _series(raw_bench.get("values"))
        b0, b1 = _value_on(bench, start), _value_on(bench, end)
        if b0 and b1 is not None:
            bench_total = b1 / b0 - 1
            benchmark = {
                "name": str(raw_bench.get("name") or "Benchmark"),
                "return": round(bench_total, 6), "annualized": _round(_annualize(bench_total, days)),
                "series": [{"date": d.isoformat(), "index": round(100 * v / b0, 4)} for d, v in bench if start <= d <= end],
                "excess": round(twr - bench_total, 6) if twr is not None else None,
            }
    return {
        "status": "ready" if twr is not None else "insufficient",
        "reason": None if twr is not None else "invalid_history",
        "currency": value.get("currency"), "start": start.isoformat(), "end": end.isoformat(), "days": days,
        "twr": _round(twr), "twr_annualized": _round(_annualize(twr, days)),
        "irr_annualized": _round(irr),
        "irr_period": _round((1 + irr) ** (days / 365.0) - 1 if irr is not None else None),
        "net_flows": round(sum(a for _, a in in_range), 2), "flow_count": len(in_range),
        "series": index, "benchmark": benchmark,
        "stale": _stale(fact, today),
    }


def _round(value: float | None, digits: int = 6) -> float | None:
    return round(value, digits) if value is not None else None


# ---------------------------------------------------------------- goals

def goals_view(snapshot: dict, today: date) -> dict:
    facts = _facts_by_key(snapshot)
    fact = facts.get("goals")
    goals = fact.get("value") if fact else None
    if not isinstance(goals, list):
        return {"status": "unknown", "items": []}
    plan, plan_status = None, "unavailable"
    try:
        from .workflows import prepare
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
        due = _as_date(goal.get("due"))
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

def upcoming(snapshot: dict, today: date, horizon_days: int = 60) -> list[dict]:
    """Dated things to act on that the rows do not already show.

    Stale facts ask "Still true?" on their own row and goals show their date,
    so neither is repeated here.
    """
    items = []
    for fact in snapshot["facts"]:
        review = _as_date(fact.get("expires_on"))
        if fact.get("value") is None or review is None or classify_key(fact["key"]) == "analysis":
            continue
        if today <= review <= today + timedelta(days=horizon_days):
            items.append({"type": "review", "date": review.isoformat(), "key": fact["key"],
                          "label": _humanize(fact["key"])})
    for decision in snapshot["decisions"]:
        status = decision.get("status")
        if status == "proposed" or (status == "accepted" and decision.get("needs_review")):
            items.append({"type": "decision" if status == "proposed" else "decision_review",
                          "date": str(decision.get("created_at") or "")[:10] or None,
                          "label": str(decision.get("title") or "Decision")})
    order = {"decision": 0, "decision_review": 0, "review": 1}
    return sorted(items, key=lambda i: (order[i["type"]], i["date"] or "9999"))


# ---------------------------------------------------------------- entry point

def profile_view(service: Any, client_id: str, today: Any = None) -> dict:
    """Assemble the JSON-able dashboard model: only what the page renders."""
    today = _today(today)
    snapshot = _snapshot(service, client_id)
    facts = _facts_by_key(snapshot)
    profile = _dict_value(facts.get("client.profile"))
    household = _dict_value(facts.get("household"))
    ov = overview(snapshot, today)
    goals = goals_view(snapshot, today)
    known = completeness(snapshot)
    groups = memory_groups(snapshot, today, ov, goals, known["missing"])
    reporting = next((c for c in (profile.get("reporting_currency"), household.get("currency"),
                                  _dict_value(facts.get("plan.resources")).get("currency"))
                      if isinstance(c, str) and _CURRENCY.match(c)), None)
    locale = next((profile.get(k) for k in ("locale", "language") if isinstance(profile.get(k), str)), None)
    return {
        "version": 2, "today": today.isoformat(),
        "client": {"display_name": snapshot["client"].get("display_name"),
                   "revision": snapshot["client"].get("revision")},
        "reporting_currency": reporting, "locale": locale,
        "overview": ov,
        "performance": performance(snapshot, _history(service, client_id, "household"), today),
        "memory": groups,
        "completeness": known,
        "upcoming": upcoming(snapshot, today),
    }


# ---------------------------------------------------------------- writes (returned, not performed)
#
# Each write omits expires_on so the store sets the review date from
# store.REVIEW_DAYS; the web layer passes the revision the page was rendered at
# as expected_revision so an edit never overwrites a change it did not see.

def _user_fact(key: str, value: Any, ref: str, today: date, *, merge: bool = False) -> dict:
    fact = {"key": key, "value": value, "source": {"kind": "user", "ref": ref, "observed_on": today.isoformat()},
            "confidence": "confirmed"}
    if merge:
        fact["merge"] = True
    return fact


def _coerce(editor: str | None, value: Any) -> Any:
    if editor == "text":
        if not isinstance(value, str) or not value.strip() or len(value) > 2000:
            raise ValueError("enter text of 1–2,000 characters")
        return value.strip()
    if editor == "number":
        number = _num(value)
        if number is None:
            raise ValueError("enter a number")
        return int(number) if number.is_integer() else number
    if editor == "bool":
        if not isinstance(value, bool):
            raise ValueError("choose yes or no")
        return value
    if editor == "list":
        items = value.split(",") if isinstance(value, str) else value
        if not isinstance(items, list) or not all(isinstance(i, str) for i in items):
            raise ValueError("enter a comma-separated list")
        cleaned = [i.strip() for i in items if i.strip()]
        if not cleaned or len(cleaned) > 30:
            raise ValueError("enter one to thirty items")
        return cleaned
    if editor == "amount":
        return _amount(value)
    raise ValueError("this value is structured; update it in the chat")


def _amount(value: Any) -> dict:
    if not isinstance(value, dict):
        raise ValueError("enter an amount and currency")
    amount = _num(value.get("amount"))
    currency = str(value.get("currency") or "").upper()
    if amount is None or amount < 0:
        raise ValueError("enter a non-negative amount")
    if not _CURRENCY.match(currency):
        raise ValueError("choose a three-letter currency")
    result = {"amount": int(amount) if amount.is_integer() else amount, "currency": currency}
    if value.get("period") in {"month", "year"}:
        result["period"] = value["period"]
    return result


def _goal_patch(goal: dict, value: Any) -> dict:
    if not isinstance(value, dict):
        raise ValueError("goal edits must be an object")
    updated = dict(goal)
    if "name" in value:
        updated["name"] = _coerce("text", value["name"])
    if "target_amount" in value:
        if value["target_amount"] in (None, ""):
            updated.pop("target_amount", None)  # unknown, not zero
        else:
            number = _num(value["target_amount"])
            if number is None or number < 0:
                raise ValueError("enter a non-negative target")
            updated["target_amount"] = int(number) if number.is_integer() else number
    if value.get("currency"):
        currency = str(value["currency"]).upper()
        if not _CURRENCY.match(currency):
            raise ValueError("choose a three-letter currency")
        updated["currency"] = currency
    if "due" in value:
        if value["due"] in (None, ""):
            updated.pop("due", None)
        elif _as_date(value["due"]) is None:
            raise ValueError("enter a date as YYYY-MM-DD")
        else:
            updated["due"] = _as_date(value["due"]).isoformat()
    return updated


def fact_action(snapshot: dict, key: str, action: str, *, field: str | None = None,
                value: Any = None, today: Any = None) -> list[dict]:
    """Return the facts to remember for an Edit / Confirm / Delete request.

    * confirm: rewrites the current value as a user-confirmed observation dated
      today, which restarts its review period (and confirms an inferred fact).
    * edit / delete of a field: a ``merge`` patch (``null`` removes the field).
    * edit / delete of one goal: the full goals list with that goal changed.
    * delete of a whole key: a ``null`` retraction; history is kept.

    Raises ``LookupError`` for an unknown key/field, ``ValueError`` otherwise.
    """
    today = _today(today)
    if action not in {"edit", "confirm", "delete"}:
        raise ValueError("action must be edit, confirm, or delete")
    fact = next((f for f in (snapshot or {}).get("facts") or []
                 if isinstance(f, dict) and f.get("key") == key), None)
    if fact is None or fact.get("value") is None:
        raise LookupError("no current fact with that key")
    current = fact["value"]

    if action == "confirm":
        if key in _MARKET_VALUED or key.startswith(("analysis.", "research.")):
            raise ValueError("market values need a fresh statement or analysis, not a confirmation")
        return [_user_fact(key, current, "profile page: confirmed still true", today)]
    if field is None:
        if action == "delete":
            return [_user_fact(key, None, "profile page: removed", today)]
        if key == "goals" or key in _MARKET_VALUED:
            raise ValueError("this value is structured; update it in the chat")
        return [_user_fact(key, _coerce(_editor(current), value), "profile page edit", today)]
    if key == "goals":
        if not isinstance(current, list):
            raise ValueError("goals has an unexpected shape")
        position = next((i for i, g in enumerate(current)
                         if isinstance(g, dict) and str(g.get("id", i)) == field), None)
        if position is None:
            raise LookupError("no goal with that id")
        goals = list(current)
        if action == "delete":
            goals.pop(position)
            return [_user_fact(key, goals, "profile page: goal removed", today)]
        goals[position] = _goal_patch(current[position], value)
        return [_user_fact(key, goals, "profile page: goal edited", today)]
    if not isinstance(current, dict) or field not in current:
        raise LookupError("no such field on this fact")
    if action == "delete":
        return [_user_fact(key, {field: None}, "profile page: field removed", today, merge=True)]
    new_value = _coerce(_editor(current[field]), value)
    if isinstance(new_value, list):  # the store merges only id-keyed lists; replace the object
        return [_user_fact(key, {**current, field: new_value}, "profile page edit", today)]
    return [_user_fact(key, {field: new_value}, "profile page edit", today, merge=True)]


def form_facts(snapshot: dict, form: dict, today: Any = None) -> list[dict]:
    """Turn "Complete your profile" answers into canonical facts.

    Blank answers are skipped (unknown stays unknown, never zero).  Profile
    fields are written as a ``merge`` patch into ``client.profile``; a new goal
    is appended to ``goals`` without dropping existing goals.  Returns an empty
    list when nothing was supplied.
    """
    today = _today(today)
    if not isinstance(form, dict):
        raise ValueError("form must be an object")
    unknown = set(form) - set(FORM_FIELDS)
    if unknown:
        raise ValueError(f"unknown form fields: {sorted(unknown)}")
    facts = {f["key"]: f for f in (snapshot or {}).get("facts") or [] if isinstance(f, dict) and "key" in f}
    patch: dict[str, Any] = {}
    for field, target in _FORM_PROFILE_FIELDS.items():
        raw = form.get(field)
        if raw in (None, "") or (isinstance(raw, dict) and raw.get("amount") in (None, "")):
            continue
        patch[target] = _amount(raw)
    if form.get("dependents") not in (None, ""):
        number = _num(form["dependents"])
        if number is None or number < 0 or not number.is_integer() or number > 50:
            raise ValueError("dependents must be a whole number")
        patch["dependents"] = int(number)
    for field in ("tax_residence", "currencies"):
        if form.get(field) not in (None, "", []):
            items = _coerce("list", form[field])
            if field == "currencies":
                items = [i.upper() for i in items]
                if not all(_CURRENCY.match(i) for i in items):
                    raise ValueError("currencies must be three-letter codes")
            patch[field] = items
    out = []
    ref = "profile page form"
    if patch:
        current = _dict_value(facts.get("client.profile"))
        if any(isinstance(v, list) and isinstance(current.get(k), list) for k, v in patch.items()):
            # String lists cannot be merge-patched; replace the whole object (needs expected_revision).
            out.append(_user_fact("client.profile", {**current, **patch}, ref, today))
        else:
            out.append(_user_fact("client.profile", patch, ref, today, merge=True))
    if form.get("risk") not in (None, ""):
        out.append(_user_fact("preference.risk", _coerce("text", form["risk"]), ref, today, merge=True))
    new_goal = form.get("goals")
    if isinstance(new_goal, dict) and str(new_goal.get("name") or "").strip():
        goals_fact = facts.get("goals")
        goals = list(goals_fact["value"]) if goals_fact and isinstance(goals_fact.get("value"), list) else []
        base_id = re.sub(r"[^a-z0-9]+", "-", str(new_goal["name"]).lower()).strip("-")[:40] or "goal"
        existing = {str(g.get("id")) for g in goals if isinstance(g, dict)}
        goal_id, n = base_id, 2
        while goal_id in existing:
            goal_id, n = f"{base_id}-{n}", n + 1
        goal = _goal_patch({"id": goal_id}, {k: v for k, v in new_goal.items()
                                             if k in {"name", "target_amount", "currency", "due"}})
        if all(isinstance(g, dict) and "id" in g for g in goals):
            out.append(_user_fact("goals", [goal], ref, today, merge=True))  # merged by id
        else:
            out.append(_user_fact("goals", [*goals, goal], ref, today))
    elif new_goal not in (None, "", {}) and not isinstance(new_goal, dict):
        raise ValueError("goals must be an object with a name")
    return out


__all__ = ["profile_view", "fact_detail", "fact_action", "form_facts", "time_weighted_return", "xirr", "classify_key",
           "memory_groups", "completeness", "overview", "performance", "goals_view", "upcoming"]
