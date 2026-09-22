"""The picture: deterministic figures for the charts the You page draws."""
from __future__ import annotations

import math
import re
from datetime import date, timedelta
from typing import Any, Callable, Iterable

from .. import finmath
from ..situation.text import _clean_symbol
from .helpers import _as_date, _dict_value, _facts_by_key, _humanize, _num


# ---------------------------------------------------------------- the picture: what the You page draws
#
# Deterministic figures for the page's charts, taken from the canonical picture (``situation``), the
# ledger, cached market prices and the goal and DCA facts.  Unknown stays ``None`` (with a reason code),
# never zero; weights are fractions of 1; amounts are in the reporting currency unless a row says
# otherwise.  Only what a chart draws is sent.

PICTURE_TOP = 5                 # ranked holdings shown
PICTURE_POINTS = 26             # at most this many points on a payoff line
PICTURE_WEEKS = 56              # weekly valuation points (a year and a bit)
PICTURE_DOTS = 12               # DCA installments shown as dots
PICTURE_PRICE_BUDGET = 1.0      # seconds the page waits for market data before using the cache

_ASSET_CLASS = {
    "cash": "cash", "money_market": "cash",
    "equity": "equity", "stock": "equity", "stocks": "equity", "common_stock": "equity", "adr": "equity",
    "fund": "fund", "etf": "fund", "mutual_fund": "fund", "index_fund": "fund",
    "fixed_income": "fixed_income", "bond": "fixed_income", "bonds": "fixed_income", "bond_fund": "fixed_income",
    "government_bond": "fixed_income", "treasury": "fixed_income",
    "real_estate": "real_estate", "reit": "real_estate", "fibra": "real_estate",
    "retirement": "retirement", "afore": "retirement", "pension": "retirement", "ppr": "retirement",
    "crypto": "crypto",
}
_CLASS_ORDER = ("equity", "fund", "fixed_income", "cash", "real_estate", "retirement", "crypto", "unclassified")
_SPENDING_LABELS = {
    "housing": ("Housing", "Vivienda"), "utilities": ("Utilities", "Servicios"), "telecom": ("Phone and internet", "Teléfono e internet"),
    "groceries": ("Groceries", "Súper"), "transport": ("Transport", "Transporte"), "education": ("Education", "Educación"),
    "health": ("Health", "Salud"), "insurance": ("Insurance", "Seguros"), "taxes": ("Taxes", "Impuestos"),
    "childcare": ("Childcare", "Cuidado infantil"), "convenience": ("Convenience stores", "Tiendas de conveniencia"),
    "dining": ("Eating out", "Restaurantes"), "food_delivery": ("Food delivery", "Comida a domicilio"),
    "subscriptions": ("Subscriptions", "Suscripciones"), "shopping": ("Shopping", "Compras"), "travel": ("Travel", "Viajes"),
    "entertainment": ("Entertainment", "Entretenimiento"), "personal_care": ("Personal care", "Cuidado personal"),
    "gifts_donations": ("Gifts and donations", "Regalos y donativos"), "bank_fees": ("Bank fees", "Comisiones"),
    "cash_withdrawal": ("Cash withdrawals", "Retiros en efectivo"), "other": ("Other", "Otros"),
}
_DEBT_KINDS = {"auto": ("Car loan", "Crédito automotriz"), "mortgage": ("Mortgage", "Hipoteca"),
               "card": ("Credit card", "Tarjeta de crédito"), "personal": ("Personal loan", "Préstamo personal"),
               "student": ("Student loan", "Crédito educativo"), "other": ("Loan", "Préstamo")}
_ACCOUNT_KINDS = {"checking": ("Checking", "Cuenta de cheques"), "savings": ("Savings", "Ahorro"),
                  "bank": ("Bank", "Banco"), "cash": ("Cash", "Efectivo"), "brokerage": ("Brokerage", "Inversión"),
                  "taxable": ("Brokerage", "Inversión"), "retirement": ("Retirement", "Retiro"), "afore": ("AFORE", "AFORE"),
                  "ppr": ("PPR", "PPR"), "ira": ("IRA", "IRA"), "401k": ("401(k)", "401(k)"),
                  "credit_card": ("Credit card", "Tarjeta de crédito"), "fund": ("Fund", "Fondo"), "other": ("Other", "Otra")}


def _r(value: Any, places: int = 2) -> float | None:
    number = _num(value)
    return None if number is None else round(number, places)


def _pair(pair: tuple[str, str]) -> dict:
    return {"en": pair[0], "es": pair[1]}


def _rates(sit: dict) -> dict[str, float]:
    """Currency -> rate into the reporting currency, from the rates the picture itself used."""
    currency = sit.get("currency")
    rates = {currency: 1.0} if currency else {}
    for row in sit.get("fx") or []:
        base, _, quote = str(row.get("pair") or "").partition("/")
        rate = _num(row.get("rate"))
        if not rate:
            continue
        if quote == currency:
            rates.setdefault(base, rate)
        elif base == currency:
            rates.setdefault(quote, 1 / rate)
    return rates


def _asset_class(raw: Any, instrument: Any = None) -> str:
    if str(instrument or "").upper().startswith("CASH:"):
        return "cash"
    return _ASSET_CLASS.get(str(raw or "").strip().lower(), "unclassified")


# Government paper as people say it: "S UDIBONO 351122" -> "Udibono 2035", "BI CETES 261015" -> "Cetes 2026",
# "M BONO 291201" -> "Bono 2029". Tickers (VOO, NAFTRAC) pass through untouched.
_GOV_SERIES = re.compile(r"^[A-Z]{1,2}\s+(?=(?:UDIBONO|BONO|CETES|BONDES[A-Z]?)\b)")
_GOV_MATURITY = re.compile(r"\b(CETES|BONDES[A-Z]?)\s+(\d{2})\d{4}\b")


def _instrument_label(symbol: Any) -> str:
    text = _GOV_SERIES.sub("", str(symbol or "").strip())
    text = _GOV_MATURITY.sub(lambda m: f"{m.group(1)} 20{m.group(2)}", text)
    text = re.sub(r"\bBONDES([A-Z])?\b", lambda m: "Bondes" + (f" {m.group(1)}" if m.group(1) else ""), text)
    return _clean_symbol(text)


def _holding_rows(sit: dict, facts: dict[str, dict]) -> list[dict]:
    """Every counted asset as {class, currency, native, value, venue, domicile, underlying}, in the reporting currency.

    Built from the same rows the net worth adds up, so the class and currency splits sum to the assets.
    Part of an account that cannot be split (a rate is missing) is kept as unclassified, never dropped.
    """
    from ..situation.model import underlying_of

    rates = _rates(sit)
    rows: list[dict] = []

    def add(klass: str, currency: Any, native: Any, *, symbol: Any = None, venue: Any = None,
            domicile: Any = None, underlying: Any = None) -> float | None:
        amount = _num(native)
        rate = rates.get(currency) if isinstance(currency, str) else None
        if amount is None or rate is None:
            return None
        value = amount * rate
        rows.append({"class": klass, "currency": currency, "native": amount, "value": value, "symbol": symbol,
                     "venue": venue, "domicile": domicile,
                     "underlying": underlying_of(underlying or symbol) or (str(underlying or symbol) if symbol else None)})
        return value

    for row in sit.get("cash") or []:
        if row.get("counted") and row.get("value") is not None:
            rows.append({"class": "cash", "currency": row.get("currency"), "native": _num(row.get("amount")),
                         "value": _num(row["value"]), "symbol": None, "venue": None, "domicile": None, "underlying": None})
    for row in sit.get("investments") or []:
        if row.get("counted") and row.get("value") is not None:
            kind = str(row.get("kind") or "").lower()
            rows.append({"class": _asset_class(kind) if kind in _ASSET_CLASS else "unclassified",
                         "currency": row.get("currency"), "native": _num(row.get("amount")), "value": _num(row["value"]),
                         "symbol": None, "venue": None, "domicile": None, "underlying": None})
    household = _dict_value(facts.get("household"))
    for account in sit.get("accounts") or []:
        total = _num(account.get("value"))
        if total is None or account.get("superseded_by") or account.get("stale"):
            continue
        if account.get("source") == "statement":
            positions = (_dict_value(facts.get(account.get("key") or "")).get("positions")) or []
        elif account.get("source") == "ledger":
            positions = account.get("priced_positions") or []
        else:
            positions = [p for p in household.get("positions") or []
                         if isinstance(p, dict) and p.get("account_id") == account.get("id")]
        placed = 0.0
        for p in positions:
            if not isinstance(p, dict):
                continue
            value = add(_asset_class(p.get("asset_class"), p.get("instrument_id")), p.get("currency"), p.get("value"),
                        symbol=p.get("symbol") or p.get("instrument_id"), venue=p.get("venue"),
                        domicile=p.get("issuer_domicile"), underlying=p.get("underlying_symbol"))
            placed += value or 0.0
        rest = total - placed
        if abs(rest) >= 0.01:
            kind = str(account.get("type") or "").lower()
            klass = _asset_class(kind) if not positions and kind in _ASSET_CLASS else "unclassified"
            rows.append({"class": klass, "currency": account.get("currency") or sit.get("currency"), "native": None,
                         "value": rest, "symbol": None, "venue": None, "domicile": None, "underlying": None})
    return rows


def _bars(groups: dict[str, float], total: float | None, order: Iterable[str] = ()) -> list[dict]:
    rank = {k: i for i, k in enumerate(order)}
    items = sorted(groups.items(), key=lambda kv: (rank.get(kv[0], len(rank)), -kv[1]))
    return [{"id": k, "value": round(v, 2), "weight": round(v / total, 4) if total else None}
            for k, v in items if abs(v) >= 0.005]


def _allocation(sit: dict, facts: dict[str, dict]) -> dict:
    rows = _holding_rows(sit, facts)
    total = sum(r["value"] for r in rows)
    if not rows or total <= 0:
        return {"status": "unknown", "total": None, "asset_class": [], "currency": [], "top": [], "overlaps": [],
                "venue": None, "domicile": None}
    by_class: dict[str, float] = {}
    by_currency: dict[str, float] = {}
    native: dict[str, float | None] = {}
    for row in rows:
        by_class[row["class"]] = by_class.get(row["class"], 0.0) + row["value"]
        cur = row["currency"] or "?"
        by_currency[cur] = by_currency.get(cur, 0.0) + row["value"]
        # The amount in its own currency is shown only when every part of it is known.
        known = native.get(cur, 0.0)
        native[cur] = None if known is None or row["native"] is None else known + row["native"]
    currency_rows = _bars(by_currency, total, (sit.get("currency"),))
    for row in currency_rows:
        amount = native.get(row["id"])
        row["native"] = round(amount, 2) if amount is not None and row["id"] != sit.get("currency") else None
    groups: dict[str, dict] = {}
    for row in rows:
        if row["class"] == "cash" or not row["underlying"]:
            continue
        group = groups.setdefault(row["underlying"], {"name": _instrument_label(row["underlying"]), "value": 0.0, "symbols": []})
        group["value"] += row["value"]
        symbol = _instrument_label(row["symbol"] or row["underlying"])
        if symbol not in group["symbols"]:
            group["symbols"].append(symbol)
    top = sorted(groups.values(), key=lambda g: (-g["value"], g["name"]))[:PICTURE_TOP]
    overlaps = [{"name": _instrument_label(g["underlying"]), "symbols": [_instrument_label(s) for s in g["symbols"]], "value": _r(g["value"]),
                 "weight": round(_num(g["value"]) / total, 4)}
                for g in (sit.get("holdings") or {}).get("overlaps") or [] if _num(g.get("value")) is not None]
    profile = sit.get("profile") or {}
    mexican = "MX" in ((profile.get("tax_residence") or []) + [(profile.get("residence") or {}).get("country")])
    venue = domicile = None
    securities = [r for r in rows if r["class"] != "cash" and r["symbol"]]
    if mexican and securities:
        venue_groups: dict[str, float] = {}
        domicile_groups: dict[str, float] = {}
        for row in securities:
            v = str(row["venue"] or "").lower()
            key = "sic" if v == "sic" else "bmv" if v in {"bmv", "biva"} else "abroad" if v == "us" else "unknown"
            venue_groups[key] = venue_groups.get(key, 0.0) + row["value"]
            d = str(row["domicile"] or "").upper()
            dkey = "us" if d == "US" else "non_us" if d in {"IE", "MX", "LU", "CA", "GB"} else "unknown"
            domicile_groups[dkey] = domicile_groups.get(dkey, 0.0) + row["value"]
        base = sum(r["value"] for r in securities)
        venue = _bars(venue_groups, base, ("sic", "bmv", "abroad", "unknown"))
        domicile = _bars(domicile_groups, base, ("us", "non_us", "unknown"))
    return {"status": "ready", "total": round(total, 2),
            "asset_class": _bars(by_class, total, _CLASS_ORDER), "currency": currency_rows,
            "top": [{"name": g["name"], "symbols": g["symbols"], "value": round(g["value"], 2),
                     "weight": round(g["value"] / total, 4)} for g in top],
            "overlaps": overlaps, "venue": venue, "domicile": domicile}


def _accounts(sit: dict) -> list[dict]:
    """One row per place money sits: name, kind, value and how fresh the figure is."""
    meta = sit.get("meta") or {}
    out = []
    shown = [a.get("institution") for a in sit.get("accounts") or [] if not a.get("superseded_by")]
    for account in sit.get("accounts") or []:
        if account.get("superseded_by"):
            continue
        kind = str(account.get("type") or "other").lower()
        institution = account.get("institution")
        name = institution or str(account.get("label") or "").partition(" · ")[0]
        if institution and shown.count(institution) > 1 and account.get("currency"):
            name = f"{institution} ({account['currency']})"
        out.append({"name": name, "kind": _pair(_ACCOUNT_KINDS.get(kind, _ACCOUNT_KINDS["other"])),
                    "currency": account.get("currency"), "value": _r(account.get("value")),
                    "as_of": account.get("as_of"), "source": "prices" if account.get("valued_by") == "prices" else account.get("source"),
                    "unknown": "prices" if account.get("value") is None else None})
    for row in (*(sit.get("cash") or []), *(sit.get("investments") or [])):
        if not row.get("counted"):
            continue
        kind = str(row.get("kind") or ("cash" if row["key"].startswith("cash.") else "other")).lower()
        out.append({"name": row.get("institution") or row.get("name") or _humanize(row["id"]),
                    "kind": _pair(_ACCOUNT_KINDS.get(kind, _ACCOUNT_KINDS["other"])), "currency": row.get("currency"),
                    "value": _r(row.get("value")), "as_of": (meta.get(row["key"]) or {}).get("observed_on"),
                    "source": "said", "unknown": "balance" if row.get("balance_unknown") else
                    "fx" if row.get("value") is None else None})
    out.sort(key=lambda r: (r["value"] is None, -(r["value"] or 0), str(r["name"])))
    return out


def _worth(sit: dict, overview_view: dict) -> dict:
    nw = sit.get("net_worth") or {}
    unknown = list(nw.get("unknown_balances") or []) + list(nw.get("unvalued_accounts") or [])
    total = _num(nw.get("total"))
    return {"total": total, "known_total": _num(nw.get("known_total")) if total is None else None,
            "currency": nw.get("currency"), "liquid": _num(nw.get("liquid")), "illiquid": _num(nw.get("illiquid")),
            "debts": _num(nw.get("liabilities")), "assets": _num(nw.get("assets")),
            "without": unknown, "unconverted": len(nw.get("unconverted") or []),
            "as_of": overview_view.get("as_of") or sit.get("as_of")}


def _flow(sit: dict) -> dict:
    """Income split into essentials, other spending, debt payments, commitments and what is left (one baseline).

    The segments always add up to the income; a negative ``unallocated`` means commitments exceed it.
    """
    cf, spending, commitments = sit.get("cash_flow") or {}, sit.get("spending") or {}, sit.get("commitments") or {}
    income, spent = _num(cf.get("income")), _num(spending.get("monthly"))
    names = {r["id"]: r.get("name") or r.get("lender") or _pair(_DEBT_KINDS.get(r.get("kind"), _DEBT_KINDS["other"]))
             for r in sit.get("liabilities") or []}
    debt_unknown = [names.get(i, i) for i in cf.get("debt_payments_unknown") or []]
    base = {"currency": sit.get("currency"), "income": income, "source": spending.get("source"),
            "months": spending.get("ledger_months"), "debt_unknown": debt_unknown,
            "commitments_unconverted": [c.get("name") for c in commitments.get("unconverted") or []]}
    missing = [k for k, v in (("income", income), ("spending", spent)) if v is None]
    if missing:
        return {**base, "status": "unknown", "missing": missing, "segments": [], "savings_rate": None}
    essential = _num(spending.get("essential"))
    segments = []
    if essential is not None and 0 <= essential <= spent:
        segments += [{"id": "essentials", "value": round(essential, 2)}, {"id": "other", "value": round(spent - essential, 2)}]
    else:
        segments.append({"id": "spending", "value": round(spent, 2)})
    debt = _num(cf.get("debt_payments_known")) or 0.0
    if debt:
        segments.append({"id": "debt", "value": round(debt, 2)})
    # The model's commitments exactly, so this bar and every sentence about the month agree.
    committed_items = list(commitments.get("items") or [])
    committed = sum(_num(c.get("monthly")) or 0.0 for c in committed_items)
    if committed:
        segments.append({"id": "committed", "value": round(committed, 2)})
    left = income - sum(s["value"] for s in segments)
    segments.append({"id": "unallocated", "value": round(left, 2)})
    saved = income - spent - debt
    # When the model cannot say what is left each month (an unknown payment, or one that may already sit inside
    # the stated spending), the savings rate is unknown too, never a figure built on a guess.
    known = cf.get("surplus") is not None or "surplus" not in cf
    return {**base, "status": "ready", "missing": [], "segments": segments,
            "committed": [{"name": c.get("name"), "kind": c.get("kind"), "value": _r(c.get("monthly"))} for c in committed_items],
            "savings_rate": round(saved / income, 4) if income > 0 and known else None}


def _spending_months(sit: dict, ledger: dict | None) -> dict | None:
    """Spending per complete ledger month and the three largest categories, when the ledger has two or more months."""
    spending = sit.get("spending") or {}
    period = spending.get("ledger_period")
    if spending.get("source") != "ledger" or not ledger or not period or not sit.get("currency"):
        return None
    from ..cashflow import spending_report

    try:
        report = spending_report(ledger, period[0], period[1], sit["currency"])
    except Exception:  # noqa: BLE001 - an unreadable ledger leaves the chart out, not the page
        return None
    months = (report.get("result") or {}).get("months") or {}
    if len(months) < 2:
        return None
    categories: dict[str, float] = {}
    for month in months.values():
        for name, amount in (month.get("by_category") or {}).items():
            categories[name] = categories.get(name, 0.0) + (_num(amount) or 0.0)
    count = len(months)
    average = sum(_num(m.get("total")) or 0.0 for m in months.values()) / count
    top = sorted(((k, v / count) for k, v in categories.items() if v > 0), key=lambda kv: -kv[1])[:3]
    return {"months": [{"month": k, "total": _r(v.get("total"))} for k, v in sorted(months.items())],
            "average": round(average, 2),
            "top": [{"id": k, "label": _pair(_SPENDING_LABELS.get(k, (_humanize(k), _humanize(k)))), "value": round(v, 2),
                     "share": round(v / average, 4) if average else None} for k, v in top],
            "partial": report.get("status") != "ready"}


def _amortization(balance: float, annual_rate: float, payment: float, limit: int = 600,
                  iva: float = 0.0) -> list[tuple[int, float]]:
    # IVA on interest where the debt engine charges it, so the curve ends at the payoff month the card shows.
    rate, remaining, points, month = annual_rate / 12 * (1 + iva), balance, [(0, balance)], 0
    while remaining > 0.005 and month < limit:
        remaining = remaining * (1 + rate) - payment
        month += 1
        points.append((month, max(remaining, 0.0)))
    if len(points) > PICTURE_POINTS:
        step = math.ceil((len(points) - 1) / (PICTURE_POINTS - 1))
        points = points[::step] + ([points[-1]] if (len(points) - 1) % step else [])
    return points


def _debts(sit: dict) -> list[dict]:
    out = []
    for r in sit.get("liabilities") or []:
        balance = _num(r.get("balance"))
        plan = r.get("payoff") or {}
        name = r.get("name") or r.get("lender")
        row = {"id": r["id"], "name": name, "kind": _pair(_DEBT_KINDS.get(r.get("kind"), _DEBT_KINDS["other"])),
               "lender": r.get("lender") if r.get("lender") != name else None,
               "currency": r.get("currency"), "balance": balance, "value": _num(r.get("value")),
               "rate": _num(r.get("annual_rate")), "payment": _r(r.get("monthly_payment")),
               "status": plan.get("status") or "unknown", "payoff": plan.get("date") if plan.get("status") == "ready" else None,
               "months": plan.get("months") if plan.get("status") == "ready" else None,
               "interest": _num(plan.get("interest")) if plan.get("status") == "ready" else None,
               "missing": ["rate" if m == "annual_rate" else "payment" for m in r.get("missing") or []],
               "points": None}
        if row["status"] == "ready" and balance and row["rate"] is not None and r.get("monthly_payment") is not None:
            row["points"] = [[m, round(b, 2)] for m, b in _amortization(balance, row["rate"], _num(r["monthly_payment"]),
                                                                            iva=_num(r.get("iva_on_interest")) or 0.0)]
        out.append(row)
    out.sort(key=lambda d: -(d["value"] or 0))
    return out


def _goal_funded(goal: dict, sit: dict) -> float | None:
    """Cash earmarked for the goal (purpose goal:<id>), in the goal's currency.

    None when nothing is earmarked or it cannot be converted: with no account tied to the goal the
    progress is not known, and the page says so rather than showing $0.
    """
    total, found = 0.0, False
    for row in sit.get("cash") or []:
        if row.get("purpose") != f"goal:{goal['id']}" or not row.get("counted", True):
            continue
        found = True
        if row.get("currency") == goal.get("currency") and _num(row.get("amount")) is not None:
            total += _num(row["amount"])
        elif goal.get("currency") == sit.get("currency") and _num(row.get("value")) is not None:
            total += _num(row["value"])
        else:
            return None
    return total if found else None


def _dca_plans(snapshot_facts: dict[str, dict]) -> list[dict]:
    from .. import dca as dca_module

    raw = (snapshot_facts.get("planning.dca") or {}).get("value")
    raw = raw.get("plans") if isinstance(raw, dict) else raw
    plans = []
    for item in raw if isinstance(raw, list) else [raw] if isinstance(raw, dict) else []:
        try:
            plans.append(dca_module.validate_plan({k: v for k, v in item.items() if k != "status"}))
        except (ValueError, AttributeError):
            continue
    return plans


def _adherence(plan: dict, ledger: dict | None, today: date) -> dict | None:
    from .. import dca as dca_module

    if not ledger or not ledger.get("entries"):
        return None
    try:
        report = dca_module.adherence(ledger, plan, today.isoformat())
    except Exception:  # noqa: BLE001 - a plan the ledger cannot check shows no dots
        return None
    result = report.get("result") or {}
    installments = [{"due": i["due"], "state": i["state"]} for i in result.get("installments") or []]
    return {"installments": installments[-PICTURE_DOTS:], "on_time_rate": _num(result.get("on_time_rate")),
            "planned": _num(result.get("planned_to_date")), "invested": _num(result.get("invested_to_date")),
            "currency": result.get("currency"), "next": result.get("next_due")}


def _goals_picture(sit: dict, facts: dict[str, dict], ledger: dict | None, today: date) -> list[dict]:
    plans = _dca_plans(facts)
    sit_plans = {d.get("id"): d for d in sit.get("dca") or []}
    attached: set[str] = set()
    invest_goals = [g for g in sit.get("goals") or [] if g.get("action") == "invest" and g.get("status") == "active"]
    out = []
    for goal in sit.get("goals") or []:
        if goal.get("status") != "active":
            continue
        target, monthly, months = _num(goal.get("target_amount")), _num(goal.get("monthly_contribution")), goal.get("months_left")
        # The situation's own funding (a stated amount, or the accounts linked to the goal); the earmarked-cash
        # fallback serves an older situation that has not worked it out.
        funded = (_num(goal["funded"]) if "funded" in goal else _goal_funded(goal, sit)) if target is not None else None
        projected = funded + monthly * months if None not in (funded, monthly, months) and months >= 0 else None
        if target is not None and funded is not None and funded >= target:
            status = "funded"
        elif projected is not None and target is not None:
            status = "on_track" if projected >= target else "behind"
        else:
            status = "unknown"
        row = {"id": goal["id"], "name": goal.get("name"), "currency": goal.get("currency"), "target": target,
               "funded": funded if target is not None else None,
               "ratio": round(min(funded / target, 1.0), 4) if funded is not None and target else None,
               "due": goal.get("target_date"), "months": months, "monthly": monthly, "status": status,
               "projected": round(projected, 2) if projected is not None else None,
               "needed": round((target - funded) / months, 2) if status == "behind" and months else None,
               "plan": None}
        if goal.get("action") == "invest":
            match = [p for p in plans if p["id"] not in attached and p["currency"] == goal.get("currency")
                     and (len(invest_goals) == 1 or _num((sit_plans.get(p["id"]) or {}).get("monthly")) == monthly)]
            if match:
                plan = match[0]
                attached.add(plan["id"])
                row["plan"] = {"name": plan.get("name") or plan["id"], "monthly": _num((sit_plans.get(plan["id"]) or {}).get("monthly")),
                               "adherence": _adherence(plan, ledger, today)}
        out.append(row)
    for plan in plans:
        if plan["id"] in attached or plan["id"] not in sit_plans:
            continue
        out.append({"id": "plan:" + plan["id"], "name": plan.get("name") or plan["id"], "currency": plan["currency"],
                    "target": None, "funded": None, "ratio": None, "due": None, "months": None,
                    "monthly": _num(sit_plans[plan["id"]].get("monthly")), "status": "unknown", "projected": None,
                    "needed": None, "plan": {"name": plan.get("name") or plan["id"],
                                             "monthly": _num(sit_plans[plan["id"]].get("monthly")),
                                             "adherence": _adherence(plan, ledger, today)}})
    return out


def _reserve(sit: dict) -> dict:
    reserve = sit.get("reserve") or {}
    return {"months": _num(reserve.get("months")), "target_months": _num(reserve.get("target_months")),
            "amount": _num(reserve.get("amount")), "target_amount": _num(reserve.get("target_amount")),
            "gap": _num(reserve.get("gap")), "essential": _num((sit.get("spending") or {}).get("essential_for_reserve")),
            "currency": sit.get("currency")}


def _weekly(start: date, end: date) -> list[date]:
    days = [end - timedelta(days=7 * k) for k in range(PICTURE_WEEKS)]
    return sorted(d for d in days if d >= start)


def _lookup(series: list[dict]) -> Callable[[date], float | None]:
    points = sorted((d, v) for d, v in ((_as_date(p.get("date")), _num(p.get("value"))) for p in series) if d and v)

    def at(day: date) -> float | None:
        prior = [v for d, v in points if d <= day and (day - d).days <= 5]
        return prior[-1] if prior else None
    return at


def _market_picture(service: Any, ledger: dict | None, sit: dict, today: date) -> tuple[dict | None, dict]:
    """Net worth over time (ledger accounts that can be valued every week) and portfolio vs benchmark.

    An account joins a series only if every weekly point since its first ledger line is valued; the rest are
    named as left out.  Values are end of day at cached (or briefly fetched) provider prices.
    """
    from ..ledger.derive import active_entries, price_table
    from ..ledger.performance import external_flows, valuation_series
    from ..prices import benchmarks, fx_rows, ledger_series, with_fx
    from ..service import current_ledger

    insufficient = {"status": "insufficient", "reason": "no_history"}
    currency = sit.get("currency")
    ledger = current_ledger(ledger) if ledger else None
    if not ledger or not ledger.get("entries") or not currency or not hasattr(service, "price_provider"):
        return None, insufficient
    entries, _ = active_entries(ledger)
    first: dict[str, date] = {}
    for entry in entries:
        day = _as_date(entry.get("date"))
        if day and day <= today:
            first[entry["account_id"]] = min(first.get(entry["account_id"], day), day)
    if not first:
        return None, insufficient
    window = max(min(first.values()), today - timedelta(days=7 * (PICTURE_WEEKS - 1)))
    provider = service.price_provider
    got = ledger_series(provider, ledger, window - timedelta(days=10), today, budget=PICTURE_PRICE_BUDGET)
    currencies = ({a.get("currency") for a in ledger.get("accounts") or []}
                  | {i.get("currency") for i in ledger.get("instruments") or []})
    rows, _ = fx_rows(provider, currencies, currency, window - timedelta(days=10), today, budget=PICTURE_PRICE_BUDGET)
    ledger = with_fx(ledger, rows)
    prices = price_table(got["prices"], max_age_days=5)
    samples = _weekly(window, today)
    kinds = {a["id"]: str(a.get("type") or "").lower() for a in ledger.get("accounts") or []}
    names = [a.get("institution") or a["id"] for a in ledger.get("accounts") or []]
    labels = {a["id"]: (a.get("institution") or a["id"]) + (f" ({a['currency']})" if names.count(a.get("institution") or a["id"]) > 1
                                                           and a.get("currency") else "")
              for a in ledger.get("accounts") or []}
    valued: dict[str, dict[str, float]] = {}
    for account, since in sorted(first.items()):
        if account not in kinds:
            continue
        mine = [d.isoformat() for d in samples if d >= since]
        if not mine:
            continue
        points = valuation_series(ledger, mine, currency, prices, account_ids=[account])["points"]
        if any(p["value"] is None for p in points):
            continue  # a week it cannot be valued: the account stays out and is named
        valued[account] = {p["date"]: float(p["value"]) for p in points}
    # Net worth over time: the earliest start at which the accounts already open hold at least half of the
    # assets, so the line is mostly the person's money and never jumps because an account's data begins.
    history = None
    current = {a["id"]: _num(a.get("value")) for a in sit.get("accounts") or []}
    assets = _num((sit.get("net_worth") or {}).get("assets"))
    for since in sorted({first[a] for a in valued}):
        members = [a for a in valued if first[a] <= since]
        dates = [d.isoformat() for d in samples if d >= since]
        covered = sum(current.get(a) or 0.0 for a in members)
        if not assets or covered / assets < 0.5 or len(dates) < 2 or (today - since).days < 28:
            continue
        history = {"points": [{"d": d, "v": round(sum(valued[a][d] for a in members), 2)} for d in dates],
                   "accounts": sorted(labels[a] for a in members), "coverage": round(covered / assets, 4),
                   "currency": currency}
        break
    invest = {"brokerage", "taxable", "investment", "retirement", "ira", "401k", "ppr", "afore"}
    # A return needs prices: an account that only ever carried a balance (no instrument to price) has none to
    # measure, so it neither joins the line nor is "left out for lack of prices".
    priced = {e["account_id"] for e in entries if e.get("instrument_id")}
    scope = [a for a in valued if kinds.get(a) in invest and a in priced]
    skipped = sorted({labels[a] for a in first if kinds.get(a) in invest and a in priced and a not in scope})
    if not scope:
        return history, {"status": "insufficient", "reason": "prices" if skipped else "no_history", "left_out": skipped}
    # Accounts whose data starts later join as money coming in (opening balances are external flows).
    start = min(first[a] for a in scope)
    samples = [d for d in samples if d >= start]
    if not samples or samples[0] != start:
        samples = [start, *samples]
    end = samples[-1]
    if (end - start).days < 28:
        return history, {"status": "insufficient", "reason": "short_history", "left_out": skipped}
    flow_rows = external_flows(ledger, start.isoformat(), end.isoformat(), currency, prices, account_ids=scope)["flows"]
    if any(f["amount"] is None for f in flow_rows):
        return history, {"status": "insufficient", "reason": "flows_unknown", "left_out": skipped}
    flows: dict[str, float] = {}
    for f in flow_rows:
        flows[f["date"]] = flows.get(f["date"], 0.0) + float(f["amount"])
    every = sorted({*(d.isoformat() for d in samples), *flows})
    values = {p["date"]: p["value"] for p in valuation_series(ledger, every, currency, prices, account_ids=scope)["points"]}
    if any(v is None for v in values.values()) or not values[every[0]]:
        return history, {"status": "insufficient", "reason": "prices", "left_out": skipped}
    # A line that never moves is a balance carried forward, not a return: unknown, never 0%.
    if len({round(float(v), 2) for v in values.values()}) == 1 and not flows:
        return history, {"status": "insufficient", "reason": "prices", "left_out": skipped}
    index, growth = {every[0]: 100.0}, 1.0
    for d0, d1 in zip(every, every[1:]):
        v0 = float(values[d0])
        if v0 <= 0:
            return history, {"status": "insufficient", "reason": "invalid_history", "left_out": skipped}
        growth *= (float(values[d1]) - flows.get(d1, 0.0)) / v0
        index[d1] = 100.0 * growth
    bench, bench_name = None, None
    try:
        made = benchmarks(provider, None, currency, start, end, budget=PICTURE_PRICE_BUDGET)["benchmarks"].get("reference_60_40") or {}
    except Exception:  # noqa: BLE001 - the comparison is optional
        made = {}
    equity, bonds = made.get("equity"), made.get("bonds")
    if equity:
        e_at, b_at = _lookup(equity["series"]), _lookup(bonds["series"]) if bonds else None
        e0, b0 = e_at(start), b_at(start) if b_at else None
        if e0:
            weights = (0.6, 0.4) if b0 else (1.0, 0.0)

            def level(day: date) -> float | None:
                e = e_at(day)
                b = b_at(day) if b0 else None
                if e is None or (b0 and b is None):
                    return None
                return 100.0 * (weights[0] * e / e0 + (weights[1] * b / b0 if b0 else 0.0))
            bench = level
            # Named once: the page already calls this the reference, so the name only says what it is made of.
            bench_name = (_pair(("Global 60/40 (ACWI and BNDW)", "Global 60/40 (ACWI y BNDW)")) if b0
                          else _pair(("Global equity (ACWI)", "Acciones globales (ACWI)")))
    series = [{"d": d.isoformat(), "p": round(index[d.isoformat()], 3),
               "b": _r(bench(d), 3) if bench else None} for d in samples]
    periods = {}
    for key, months in (("3m", 3), ("1y", 12), ("all", None)):
        begin = finmath.add_months(end, -months) if months else start
        if begin < start - timedelta(days=6):
            continue
        s = next((d for d in samples if d >= begin), None)
        if s is None or (end - s).days < 28:
            continue
        s_iso, days = s.isoformat(), (end - s).days
        twr = index[end.isoformat()] / index[s_iso] - 1
        cash = [(s, -float(values[s_iso])), *[(date.fromisoformat(d), -a) for d, a in flows.items() if s_iso < d <= end.isoformat()],
                (end, float(values[end.isoformat()]))]
        irr = finmath.xirr(cash)
        mwr = None if irr is None else (irr if days >= 365 else (1 + irr) ** (days / 365.0) - 1)
        b_s, b_e = (bench(s), bench(end)) if bench else (None, None)
        periods[key] = {"start": s_iso, "days": days, "twr": round(twr, 6), "mwr": _r(mwr, 6),
                        "bench": round(b_e / b_s - 1, 6) if b_s and b_e else None}
    if "all" in periods and "1y" in periods and periods["1y"]["start"] == periods["all"]["start"]:
        del periods["1y"]
    return history, {"status": "ready", "currency": currency, "start": start.isoformat(), "end": end.isoformat(),
                     "accounts": sorted(labels[a] for a in scope), "left_out": skipped,
                     "benchmark": bench_name, "series": series, "periods": periods}


def picture_view(service: Any, client_id: str, sit: dict, snapshot: dict, overview_view: dict, today: date) -> dict:
    """Everything the You page draws, computed once per load (see the section comment above)."""
    ledger = None
    if getattr(service, "db_path", None):
        from ..store import WealthStore

        with WealthStore(service.db_path) as store:
            ledger = store.ledger(client_id)
    facts = _facts_by_key(snapshot)
    try:
        history, returns = _market_picture(service, ledger, sit, today)
    except Exception:  # noqa: BLE001 - market data is optional; the rest of the picture stands without it
        history, returns = None, {"status": "insufficient", "reason": "prices"}
    return {"currency": sit.get("currency"), "worth": _worth(sit, overview_view), "history": history,
            "flow": _flow(sit), "spending": _spending_months(sit, ledger),
            "allocation": _allocation(sit, facts), "accounts": _accounts(sit), "debts": _debts(sit),
            "goals": _goals_picture(sit, facts, ledger, today), "reserve": _reserve(sit), "returns": returns}
