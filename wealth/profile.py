"""Read model for the "My profile" dashboard, plus the fact edits it can request.

Everything here is pure with respect to storage: ``profile_view`` only reads
through ``WealthService.inspect`` (and ``situation`` when the service has it);
amounts, names and "missing" come from the canonical model
(``wealth.situation.build``), so what the conversation saves is what this page
shows; ``fact_action`` and ``form_facts`` return fact
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

from . import finmath
from .policy import current as current_policy, summary as policy_summary
from .situation import build as build_situation, sentences, summaries
from .situation.model import goal_name
from .situation.text import _clean_symbol

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


# Keys whose rows come from the canonical model (one human row per item, never a raw id).
_MODEL_PREFIXES = ("income.", "cash.", "liability.", "investment.", "account.", "thread.")
_MODEL_KEYS = {"spending.monthly", "reserve", "onboarding"}
_LABELS = {
    "en": {"income": "Income", "take_home": "Take-home pay", "salary": "Salary", "aguinaldo": "Aguinaldo",
           "ptu": "Profit sharing (PTU)", "bonus": "Bonus", "rent": "Rent", "business": "Business", "pension": "Pension",
           "other": "Income", "spending": "Spending", "essential": "Essential spending", "cash": "Cash",
           "reserve": "emergency fund", "investments": "Investments", "auto": "Car loan", "mortgage": "Mortgage",
           "card": "Credit card", "personal": "Personal loan", "student": "Student loan", "loan": "Loan",
           "reserve_target": "Emergency reserve"},
    "es": {"income": "Ingreso", "take_home": "Ingreso neto", "salary": "Sueldo", "aguinaldo": "Aguinaldo",
           "ptu": "PTU", "bonus": "Bono", "rent": "Rentas", "business": "Negocio", "pension": "Pensión",
           "other": "Ingreso", "spending": "Gasto mensual", "essential": "Gasto esencial", "cash": "Efectivo",
           "reserve": "fondo de emergencia", "investments": "Inversiones", "auto": "Crédito del coche",
           "mortgage": "Hipoteca", "card": "Tarjeta de crédito", "personal": "Préstamo personal",
           "student": "Crédito educativo", "loan": "Préstamo", "reserve_target": "Fondo de emergencia"},
}


_ACCOUNT_TYPE_WORDS = {
    "checking": ("checking", "cuenta de cheques"), "savings": ("savings", "ahorro"), "debit": ("debit", "débito"),
    "credit_card": ("credit card", "tarjeta de crédito"), "roth_ira": ("Roth IRA", "Roth IRA"),
    "traditional_ira": ("traditional IRA", "IRA tradicional"), "rollover_ira": ("rollover IRA", "IRA rollover"),
    "sep_ira": ("SEP IRA", "SEP IRA"), "ira": ("IRA", "IRA"), "401k": ("401(k)", "401(k)"), "403b": ("403(b)", "403(b)"),
    "hsa": ("HSA", "HSA"), "529": ("529 plan", "plan 529"), "afore": ("AFORE", "AFORE"), "ppr": ("PPR", "PPR"),
    "joint": ("joint", "mancomunada"), "individual": ("individual", "individual"), "margin": ("margin", "margen"),
    "cash": ("cash", "efectivo"), "loan": ("loan", "préstamo"), "mortgage": ("mortgage", "hipoteca"),
}


def account_label(account: dict, language: str | None) -> str:
    """The model's 'Institution · type' label with the type in the person's words ('BBVA · cuenta de cheques')."""
    label = str(account.get("label") or account.get("institution") or account.get("id") or "")
    kind = str(account.get("type") or "")
    head, sep, tail = label.partition(" · ")
    if not sep or not kind or not tail.startswith(kind):
        return label
    words = _ACCOUNT_TYPE_WORDS.get(kind.lower())
    lang = 1 if _memory_lang(language) == "es" else 0
    human = words[lang] if words else kind.replace("_", " ")
    return f"{head} · {human}{tail[len(kind):]}"


def _model_key(key: str) -> bool:
    return key in _MODEL_KEYS or key.startswith(_MODEL_PREFIXES) and key != "income.schedule"


def _model_row(sit: dict, key: str, row_id: str, label: str, amount: Any, currency: Any, today: date, *,
               period: str | None = None, editable: bool = False) -> dict:
    meta = (sit.get("meta") or {}).get(key, {})
    value = {"amount": amount, "currency": currency}
    if period:
        value["period"] = period
    entry = {"id": row_id, "key": key, "field": None, "label": label, "value": value,
             "editor": "amount" if editable else None,
             "source": _source_label(meta.get("source")), "observed_on": meta.get("observed_on")}
    if not editable:
        entry["derived"] = True
    if meta.get("stale"):
        entry["stale"] = True
        entry["can_confirm"] = editable
    if meta.get("inferred"):
        entry["unconfirmed"] = True
    return entry


def _situation_rows(sit: dict, today: date, language: str | None = None) -> dict[str, list[dict]]:
    """Rows for canonical keys and legacy lists, labelled with names and institutions."""
    lang = _memory_lang(language or sit["profile"].get("language"))
    t = _LABELS[lang]
    rows: dict[str, list[dict]] = {group: [] for group in GROUPS}
    exploded = {"client.profile"}  # its form amounts already have their own rows
    for item in [*sit["income"]["items"], *sit["income"]["extras"]]:
        if item["key"] in exploded:
            continue
        label = item.get("name") or (t["take_home"] if item.get("net") and item.get("kind") in (None, "salary")
                                     else t.get(item.get("kind") or "income", t["income"]))
        period = {"monthly": "month", "annual": "year"}.get(item["frequency"])
        canonical = item["key"] != "income.schedule"
        rows["money_in"].append(_model_row(sit, item["key"], item["key"] if canonical else f"{item['key']}#{item['id']}",
                                           label, item["amount"], item["currency"], today, period=period, editable=canonical))
    spending = sit["spending"]
    if spending["key"] == "spending.monthly" and spending["stated"]:
        stated = spending["stated"]
        name = "total" if stated.get("total") is not None else "essential"
        rows["money_out"].append(_model_row(sit, "spending.monthly", "spending.monthly",
                                            t["spending"] if name == "total" else t["essential"],
                                            stated[name], stated["currency"], today, period="month", editable=True))
    for item in sit["cash"]:
        if item["key"] in exploded or not item["counted"]:
            continue
        label = item.get("institution") or item.get("name") or t["cash"]
        if item.get("purpose") == "reserve":
            label += " · " + t["reserve"]
        canonical = item["key"].startswith("cash.")
        rows["own"].append(_model_row(sit, item["key"], item["key"] if canonical else f"{item['key']}#{item['id']}",
                                      label, item["amount"], item["currency"], today, editable=canonical))
    for account in sit["accounts"]:
        if account["source"] != "statement" or account.get("native") is None:
            continue
        native = account["native"]
        amount, currency = (next(iter(native.values())), next(iter(native))) if len(native) == 1 else (
            account["value"], sit["currency"])
        rows["own"].append(_model_row(sit, account["key"], account["key"], account_label(account, lang), amount, currency, today))
    for item in sit["investments"]:
        if item["key"] in exploded or not item["counted"]:
            continue
        canonical = item["key"].startswith("investment.")
        rows["own"].append(_model_row(sit, item["key"], item["key"] if canonical else f"{item['key']}#{item['id']}",
                                      item.get("institution") or item.get("name") or t["investments"],
                                      item["amount"], item["currency"], today, editable=canonical))
    for item in sit["liabilities"]:
        if item["key"] in exploded or item["source"] == "household":
            continue
        label = item.get("name") if item["kind"] == "other" and item.get("name") else t.get(item["kind"], t["loan"])
        if item.get("lender"):
            label += f" · {item['lender']}"
        canonical = item["key"].startswith("liability.") and item["source"] == "stated"
        row_id = item["key"] if item["key"].startswith("liability.") else f"{item['key']}#{item['id']}"
        rows["owe"].append(_model_row(sit, item["key"], row_id, label, item["balance"], item["currency"], today,
                                      editable=canonical))
    reserve = sit["reserve"]
    if "reserve" in (sit.get("meta") or {}) and reserve["target_months"] is not None:
        meta = sit["meta"]["reserve"]
        rows["goals"].append({"id": "reserve#target_months", "key": "reserve", "field": "target_months",
                              "label": t["reserve_target"], "value": reserve["target_months"], "editor": "number",
                              "source": _source_label(meta.get("source")), "observed_on": meta.get("observed_on")})
    return rows


_FIELD_LABELS = {
    "es": {"name": "Nombre", "birth_year": "Año de nacimiento", "dependents": "Dependientes",
           "dependent_ages": "Edades de tus dependientes", "language": "Idioma", "tax_residence": "Residencia fiscal",
           "citizenship": "Ciudadanía", "us_person": "Persona estadounidense", "currencies": "Monedas",
           "reporting_currency": "Moneda para reportar", "timezone": "Zona horaria", "locale": "Idioma",
           "country": "País", "risk": "Riesgo", "monthly_income": "Ingreso mensual",
           "monthly_spending": "Gasto mensual", "savings": "Ahorros", "investments": "Inversiones", "debts": "Deudas"},
    "en": {"name": "Name", "birth_year": "Birth year", "dependents": "Dependents",
           "dependent_ages": "Dependents’ ages", "language": "Language", "tax_residence": "Tax residence",
           "citizenship": "Citizenship", "us_person": "US person", "currencies": "Currencies",
           "reporting_currency": "Reporting currency", "timezone": "Time zone", "locale": "Language",
           "country": "Country", "risk": "Risk", "monthly_income": "Monthly income",
           "monthly_spending": "Monthly spending", "savings": "Savings", "investments": "Investments", "debts": "Debts"},
}


def memory_groups(snapshot: dict, today: date, overview: dict | None = None,
                  goals: dict | None = None, missing: Iterable[str] = (), sit: dict | None = None,
                  language: str | None = None) -> list[dict]:
    lang = _memory_lang(language or ((sit or {}).get("profile") or {}).get("language"))
    buckets: dict[str, list[dict]] = {group: [] for group in GROUPS}
    funding = {g["id"]: g for g in (goals or {}).get("items", [])}
    ov = overview or {}
    if sit is not None:
        for group, entries in _situation_rows(sit, today, lang).items():
            buckets[group].extend(entries)
    for fact in snapshot["facts"]:
        value = fact.get("value")
        if value is None:  # a retracted fact
            continue
        key = fact["key"]
        if sit is not None and _model_key(key):
            continue
        if key == "income.schedule" and isinstance(value, dict) and isinstance(value.get("items"), list) and sit is not None:
            continue  # its items are rows above
        if key in _EXPLODED_KEYS and isinstance(value, dict):
            hidden = _HIDDEN_FIELDS.get(key, set())
            for name, item in value.items():
                if item is None or name in hidden:
                    continue
                if key == "plan.resources" and isinstance(item, list) and sit is not None:
                    continue  # legacy cash/debts/investments lists: one row per item above
                if key == "client.profile" and isinstance(item, dict) and name == "residence":
                    continue  # stated in sentences; not an amount row
                # Planning resources fund goals; they are not assets and must not sit beside holdings.
                group = "goals" if key == "plan.resources" and name in _PLANNING_FIELDS \
                    else _KIND_GROUP[_field_kind(name)]
                buckets[group].append(_entry(fact, today, field=name, value=item,
                                             label=_FIELD_LABELS[lang].get(name) or _humanize(name)))
        elif key == "goals" and isinstance(value, list):
            for index, goal in enumerate(value):
                if not isinstance(goal, dict):
                    continue
                goal_id = str(goal.get("id") or index)
                entry = _entry(fact, today, field=goal_id, value=goal, label=goal_name(goal))
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

def completeness(snapshot: dict, sit: dict | None = None) -> dict:
    """Known and missing form fields; the canonical model counts whatever the conversation saved."""
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
    if sit is not None:
        profile_model = sit["profile"]
        steps = (profile_model.get("onboarding") or {}).get("steps") or {}
        model = {
            "income": bool(sit["income"]["items"] or sit["income"]["extras"]),
            "spending": sit["spending"]["monthly"] is not None or sit["spending"]["stated"] is not None,
            "savings": bool(sit["cash"]),
            "investments": bool(sit["investments"]) or any(a["source"] != "ledger" for a in sit["accounts"]),
            "debts": bool(sit["liabilities"]) or steps.get("debts") == "done",
            "dependents": profile_model.get("dependents") is not None,
            "tax_residence": bool(profile_model.get("tax_residence")),
            "currencies": bool(sit.get("currency")) and bool(profile_model.get("currencies")),
            "goals": bool(sit["goals"]),
            "risk": bool(profile_model.get("risk")),
        }
        checks = {n: checks[n] or model[n] for n in FORM_FIELDS}
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

    One implementation for the whole app: :func:`wealth.finmath.xirr`.
    """
    return finmath.xirr(cashflows)


def _annualize(total: float | None, days: int) -> float | None:
    return finmath.annualize(total, days)


def _value_on(series: list[tuple[date, float]], when: date) -> float | None:
    prior = [v for d, v in series if d <= when]
    return prior[-1] if prior else None


def performance(snapshot: dict, household_history: list[dict], today: date) -> dict:
    facts = _facts_by_key(snapshot)
    fact = next((facts[k] for k in ("performance.history", "portfolio.history") if k in facts
                 and isinstance(facts[k].get("value"), dict)), None)
    if fact is None:
        snapshots = {(row.get("value") or {}).get("as_of") for row in household_history
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


# ---------------------------------------------------------------- memory: what Wealth knows, as sentences
#
# The page reads like notes about the person: one sentence per fact, grouped by
# life area, each with a quiet origin cue.  Every sentence carries exactly the
# action it supports (edit its amount, forget it, confirm it), resolved here so
# the page never guesses at fact shapes.

MEMORY_TOPICS = ("money_in", "money_out", "own", "owe", "goals", "invest", "about")
_REVIEW_MAX = 3
# Legacy list facts: list name -> (synthetic id prefix the model uses, amount field).
_LEGACY_LISTS = {"plan.resources": {"cash": ("cash", "amount"), "debts": ("debt", "balance"),
                                    "investments": ("investment", "amount")},
                 "income.schedule": {"items": ("item", "amount")}}
_WHOLE_KEY_FORGET_BLOCKED = {"client.profile", "plan.resources", "income.schedule", "goals", "household"}
_COUNT_FIELDS = {"dependents", "birth_year", "target_months", "reserve_months"}


def _legacy_item(key: str, current: Any, ref: str | None) -> tuple[str, int, str]:
    """(list name, index, amount field) for a ``cash:cash0``-style ref into a legacy list fact."""
    name, sep, item_id = (ref or "").partition(":")
    spec = _LEGACY_LISTS.get(key, {}).get(name)
    items = current.get(name) if isinstance(current, dict) else None
    if not sep or not spec or not isinstance(items, list):
        raise LookupError("no such item on this fact")
    for index, item in enumerate(items):
        if isinstance(item, dict) and str(item.get("id") or f"{spec[0]}{index}") == item_id:
            field = next((f for f in (spec[1], "amount", "balance") if f in item), spec[1])
            return name, index, field
    raise LookupError("no such item on this fact")


def _find_goal(goals: Any, ref: str | None) -> dict | None:
    if not isinstance(goals, list) or ref is None:
        return None
    return next((g for i, g in enumerate(goals) if isinstance(g, dict) and str(g.get("id", i)) == ref), None)


def _edit_spec(fact: dict, ref: str | None) -> dict | None:
    """What an inline edit changes: an amount (with currency) or a count, and where it lives."""
    key, value = fact["key"], fact.get("value")
    if key.startswith(("account.", "analysis.", "research.")) or key in _MARKET_VALUED:
        return None  # statement and market values change with a new statement, not by hand
    if key in _LEGACY_LISTS and ref and ":" in ref:
        try:
            name, index, field = _legacy_item(key, value, ref)
        except LookupError:
            return None
        item = value[name][index]
        return {"field": ref, "kind": "amount", "amount": _num(item.get(field)),
                "currency": item.get("currency") or value.get("currency")}
    if key == "goals":
        goal = _find_goal(value, ref)
        if goal is None:
            return None
        if goal.get("monthly_contribution") is not None or (goal.get("amount") is not None and goal.get("frequency") == "monthly"):
            wrap, amount = "monthly_contribution", goal.get("monthly_contribution", goal.get("amount"))
        elif goal.get("target_amount") is not None:
            wrap, amount = "target_amount", goal["target_amount"]
        else:
            return None
        return {"field": ref, "kind": "amount", "wrap": wrap, "amount": _num(amount), "currency": goal.get("currency")}
    if ref and isinstance(value, dict) and ref in value:
        item = value[ref]
        if isinstance(item, dict) and "amount" in item:
            return {"field": ref, "kind": "amount", "amount": _num(item["amount"]), "currency": item.get("currency")}
        if isinstance(item, (int, float)) and not isinstance(item, bool):
            if ref in _COUNT_FIELDS:
                return {"field": ref, "kind": "number", "amount": item, "unit": "months" if "months" in ref else None}
            currency = value.get("currency")
            if isinstance(currency, str) and _CURRENCY.match(currency):
                return {"field": ref, "kind": "amount", "amount": item, "currency": currency}
        return None
    field = _canonical_amount_field(key, value) if ref is None else None
    if field:
        return {"field": None, "kind": "amount", "amount": _num(value.get(field)), "currency": value.get("currency")}
    return None


def _forget_spec(fact: dict, ref: str | None, sentence: dict) -> dict | None:
    key, value = fact["key"], fact.get("value")
    if sentence.get("readonly") or len(sentence.get("keys") or []) > 1:
        return None
    if key == "spending.monthly" and isinstance(value, dict):
        other = "essential" if ref == "total" else "total"
        return {"field": ref if value.get(other) is not None else None}
    if ref is not None:
        return {"field": ref}
    return None if key in _WHOLE_KEY_FORGET_BLOCKED else {"field": None}


def _origin(sentence: dict, institutions: dict[str, str], observed: dict[str, Any]) -> dict:
    key = sentence.get("key")
    if sentence.get("origin") == "transactions" or key is None:
        return {"kind": "transactions"}
    if sentence.get("unconfirmed"):
        return {"kind": "guess"}
    kind = {"user": "said", "document": "statement"}.get(sentence.get("source"), "calculated")
    if sentence.get("origin") == "statement":
        kind = "statement"
    origin = {"kind": kind}
    institution = sentence.get("institution") or institutions.get(key)
    if institution and kind == "statement":
        origin["institution"] = institution
    as_of = sentence.get("as_of") or (observed.get(key) if kind == "statement" else None)
    if as_of and kind == "statement":
        origin["as_of"] = str(as_of)[:10]
    return origin


def _insight_origin(sentence: dict, sit: dict) -> dict:
    """Where a holdings insight (largest position, an overlap) comes from: the statements that hold it.

    One institution is named with its date; holdings spread over several are credited to the
    calculation, never to whichever statement happens to be first.
    """
    holdings = sit.get("holdings") or {}
    text = sentence.get("text") or ""
    overlap = next((o for o in holdings.get("overlaps") or []
                    if o.get("symbols") and all(str(sym) in text for sym in o["symbols"])), None)
    symbols = set((overlap or ((holdings.get("top") or [{}])[0]) or {}).get("symbols") or [])
    places = {(r.get("institution"), r.get("as_of")) for r in holdings.get("saved") or []
              if r.get("symbol") in symbols and r.get("institution")}
    if len({p[0] for p in places}) != 1:
        return {"kind": "calculated"}
    institution = next(iter(places))[0]
    dates = [d for i, d in places if d]
    origin = {"kind": "statement", "institution": institution}
    if dates:
        origin["as_of"] = str(max(dates))[:10]
    return origin


def _held_back(sit: dict, snapshot: dict, language: str) -> list[dict]:
    """Sentences for facts the model keeps out of the numbers (past review, or only a guess).

    The situation never uses them, so it never words them; a relaxed copy of the
    snapshot does, and each sentence is marked stale or unconfirmed so the page
    asks about it instead of stating it.
    """
    stale, inferred = set(sit.get("stale") or []), set(sit.get("inferred") or [])
    held = stale | inferred
    if not held:
        return []
    relaxed = {**snapshot, "facts": [
        {**f, "expires_on": None, "confidence": "reported"} if f.get("key") in held else f
        for f in snapshot.get("facts") or []]}
    try:
        shadow = build_situation(relaxed, None, _as_date(sit.get("as_of")) or _today(None))
    except Exception:  # a partial legacy shape the model cannot read yet; nothing to word
        return []
    shadow["meta"] = {**shadow.get("meta", {}), **{k: v for k, v in (sit.get("meta") or {}).items() if k in held}}
    out = []
    for s in sentences(shadow, language):
        if s["key"] in held and s.get("kind") != "difference" and not s.get("readonly"):
            out.append({**s, "stale": s["key"] in stale, "unconfirmed": s["key"] in inferred})
    return out


def memory_view(sit: dict, snapshot: dict, language: str, missing: Iterable[str] = ()) -> dict:
    """Sentences grouped by life area, the stated-vs-statement cards and at most three check-ins."""
    facts = {f["key"]: f for f in snapshot.get("facts") or [] if f.get("value") is not None}
    meta = sit.get("meta") or {}
    institutions = {a["key"]: a.get("institution") for a in sit["accounts"] if a.get("key") and a.get("institution")}
    observed = {k: m.get("observed_on") for k, m in meta.items()}
    heads = summaries(sit, language)
    items, conflicts, answered = [], [], set()
    records = [c for c in sit.get("contradictions") or [] if c.get("status", "pending") == "pending"]
    direct = sentences(sit, language)
    # A stale statement account is worded by the situation itself; its held-back copy would repeat it.
    worded = {(s["key"], s.get("ref")) for s in direct}
    extra = [s for s in _held_back(sit, snapshot, language) if (s["key"], s.get("ref")) not in worded]
    for index, s in enumerate(direct + extra):
        fact = facts.get(s["key"] or "")
        ref = s.get("ref")
        editable = not s.get("readonly")
        item = {
            "id": f"{s['topic']}-{index}", "topic": s["topic"], "text": s["text"], "emphasis": s["emphasis"],
            "key": s["key"] if fact else None,
            "origin": _insight_origin(s, sit) if s["topic"] == "invest" and s.get("readonly")
            else _origin(s, institutions, observed),
            "since": s.get("since") or _valid_since(meta.get(s["key"] or "", {})), "age_days": s.get("age_days"),
            "unconfirmed": s["unconfirmed"], "stale": s["stale"],
            "edit": _edit_spec(fact, ref) if fact and editable else None,
            "forget": _forget_spec(fact, ref, s) if fact else None,
            "confirm": bool(fact and editable and (s["stale"] or s["unconfirmed"])
                            and not s["key"].startswith("account.") and s["key"] not in _MARKET_VALUED),
        }
        if s.get("kind") == "difference":
            record = next((c for c in records if c["key"] == item["key"]), None)
            conflict = _conflict(item, s, sit, facts, record)
            if conflict:
                conflicts.append(conflict)
                answered.add(item["key"])
            continue
        items.append(item)
    # Contradiction records the sentences did not already word (a document or pattern vs what was said).
    for record in records:
        if record["key"] not in answered:
            card = _record_card(record, language, facts)
            if card:
                conflicts.append(card)
    review = [i for i in items if i["stale"] and (i["confirm"] or i["edit"])][:_REVIEW_MAX]
    in_review = {i["id"] for i in review}
    groups = []
    for topic in MEMORY_TOPICS:
        rows = [i for i in items if i["topic"] == topic and i["id"] not in in_review]
        if rows:  # a group appears only once it has a fact
            groups.append({"id": topic, "summary": heads.get(topic), "facts": rows})
    return {"groups": groups, "conflicts": conflicts, "review": review, "missing": list(missing)}


def _valid_since(meta: dict) -> str | None:
    """'since March 2026' only when the person said when it became true (not merely when they told us)."""
    start, told = meta.get("valid_from"), meta.get("observed_on")
    if meta.get("source") != "user" or not isinstance(start, str) or not isinstance(told, str):
        return None
    return start[:10] if start[:7] < told[:7] else None


_WHERE = {"es": {"document": "tu estado de cuenta", "pattern": "tus movimientos", "connector": "tu cuenta conectada",
                 "web": "una página web", "inference": "mi lectura", "tool": "un cálculo"},
          "en": {"document": "your statement", "pattern": "your transactions", "connector": "your connected account",
                 "web": "a web page", "inference": "my reading", "tool": "a calculation"}}
_TOPIC = {"es": {"income.": "tu ingreso", "cash.": "tu efectivo", "investment.": "lo que tienes invertido",
                 "liability.": "tu deuda", "spending.": "tu gasto al mes"},
          "en": {"income.": "your income", "cash.": "your cash", "investment.": "what you have invested",
                 "liability.": "your debt", "spending.": "your monthly spending"}}


def _plain_amount(value: Any) -> tuple[Any, Any] | None:
    if isinstance(value, dict):
        for field in ("amount", "balance", "total", "value"):
            if _num(value.get(field)) is not None and isinstance(value.get("currency"), str):
                return _num(value[field]), value["currency"]
    return None


def _amount_words(amount: float, currency: str, reporting: str | None) -> str:
    """"$150,000" in the reporting currency, "$8,000 USD" otherwise (the contradiction card's two figures)."""
    whole = round(amount) if abs(amount) >= 1000 else amount
    text = f"${whole:,.0f}" if float(whole).is_integer() else f"${whole:,.2f}"
    return text + (f" {currency}" if currency != reporting else "")


def _record_card(record: dict, language: str, facts: dict) -> dict | None:
    """A stored contradiction (document, pattern, web) worded as one sentence, amounts only."""
    mine, theirs = _plain_amount(record.get("current_value")), _plain_amount(record.get("proposed_value"))
    topic = next((v for k, v in _TOPIC[language].items() if record["key"].startswith(k)), None)
    if not mine or not theirs or not topic or record["key"] not in facts:
        return None
    es = language == "es"
    where = _WHERE[language].get(((record.get("sources") or {}).get("proposed") or {}).get("kind"),
                                 "otra fuente" if es else "another source")
    a, b = _amount_words(*mine, mine[1]), _amount_words(*theirs, mine[1])
    head = f"Me dijiste que {topic} es de " if es else f"You told me {topic} is "
    mid = f"; {where} dice "
    if not es:
        mid = f"; {where} says "
    text = head + a + mid + b + "."
    text = text[:1].upper() + text[1:]
    spans = [[len(head), len(head) + len(a)], [len(head) + len(a) + len(mid), len(head) + len(a) + len(mid) + len(b)]]
    return {"id": f"contradiction-{record['id']}", "text": text, "emphasis": spans, "key": record["key"],
            "contradiction_id": record["id"], "institution": None,
            "as_of": (record.get("valid_from") or {}).get("proposed"),
            "use_statement": None, "edit": _edit_spec(facts[record["key"]], None)}


def _unanswered(fact: dict | None, ref: str | None, as_of: str | None) -> bool:
    """A legacy stated item not yet kept, replaced or changed since the statement's date."""
    if fact is None or not ref or ":" not in ref:
        return fact is not None
    try:
        name, index, _ = _legacy_item(fact["key"], fact.get("value"), ref)
    except LookupError:
        return False
    answered = fact["value"][name][index].get("confirmed_on")
    return not (isinstance(answered, str) and answered >= str(as_of or ""))


def _conflict(item: dict, sentence: dict, sit: dict, facts: dict, record: dict | None = None) -> dict | None:
    """A stated figure a newer statement disagrees with; gone once the person has answered it."""
    diff = next((d for d in sit["differences"] if d.get("statement") and d["key"] == item["key"]
                 and d.get("institution") == sentence.get("institution")), None)
    if diff is None or item["key"] is None:
        return None
    if record is None and item["key"].startswith(("investment.", "cash.")):
        return None  # the store asks about canonical balances itself; no record means nothing to ask
    if record is None and not _unanswered(facts.get(item["key"]), sentence.get("ref"), diff.get("as_of")):
        return None  # the person already kept, replaced or changed this figure
    edit = item["edit"]
    use = None
    if edit and diff.get("statement_value") is not None and diff.get("currency"):
        use = {"field": edit["field"], "amount": round(float(diff["statement_value"])), "currency": diff["currency"],
               "wrap": edit.get("wrap")}
    return {"id": item["id"], "text": item["text"], "emphasis": item["emphasis"], "key": item["key"],
            "contradiction_id": record["id"] if record else None,
            "institution": diff.get("institution"), "as_of": diff.get("as_of"),
            "use_statement": use, "edit": edit}


# ---------------------------------------------------------------- entry point

def _situation(service: Any, client_id: str, snapshot: dict, today: date) -> dict:
    if hasattr(service, "situation"):
        try:
            return service.situation(client_id, today=today)
        except TypeError:  # an older service without the today argument
            return service.situation(client_id)
    return build_situation(snapshot, None, today)


def _memory_lang(language: str | None) -> str:
    return "es" if str(language or "").lower().startswith("es") else "en"


def profile_view(service: Any, client_id: str, today: Any = None, language: str | None = None) -> dict:
    """Assemble the JSON-able dashboard model: only what the page renders.

    ``language`` ("es"/"en", e.g. from ``?lang=``) builds the memory in that
    language only; without it both are included so the page can switch offline.
    """
    from .service import usable_snapshot

    today = _today(today)
    # A fact no reader can handle (e.g. an amount of 1e308 saved before numbers were bounded) is left
    # out of every view and listed for the person to remove, instead of breaking the page.
    snapshot, invalid = usable_snapshot(_snapshot(service, client_id))
    facts = _facts_by_key(snapshot)
    profile = _dict_value(facts.get("client.profile"))
    household = _dict_value(facts.get("household"))
    sit = _situation(service, client_id, snapshot, today)
    seen_invalid = {item["key"] for item in invalid}
    invalid += [item for item in sit.get("invalid_facts") or [] if item["key"] not in seen_invalid]
    if invalid:
        excluded = {item["key"] for item in invalid}
        snapshot = {**snapshot, "facts": [f for f in snapshot["facts"] if f["key"] not in excluded]}
        facts = _facts_by_key(snapshot)
    ov = overview(snapshot, today)
    if ov.get("status") == "empty":
        ov = situation_overview(sit)
    if ov.get("status") != "empty":
        ov["differences"] = differences(sit)
        if sit.get("prices"):
            # Ledger accounts valued at provider prices: each source with its date, and what is too old.
            ov["price_sources"] = (sit.get("net_worth") or {}).get("price_sources") or []
            ov["stale_prices"] = sit.get("stale_prices") or []
    goals = goals_view(snapshot, today)
    known = completeness(snapshot, sit)
    groups = memory_groups(snapshot, today, ov, goals, known["missing"], sit,
                           language or (sit.get("profile") or {}).get("language"))
    reporting = next((c for c in (profile.get("reporting_currency"), household.get("currency"),
                                  _dict_value(facts.get("plan.resources")).get("currency"), sit.get("currency"))
                      if isinstance(c, str) and _CURRENCY.match(c)), None)
    locale = next((profile.get(k) for k in ("locale", "language") if isinstance(profile.get(k), str)), None)
    saved = sit["profile"].get("language") or ("es" if str(locale or "").lower().startswith("es") else "en")
    langs = [_memory_lang(language)] if language else ["en", "es"]
    memory: dict[str, Any] = {"language": _memory_lang(language) if language else saved}
    for lang in langs:
        memory[lang] = memory_view(sit, snapshot, lang, known["missing"])
        memory[lang]["review"] = _invalid_items(invalid, lang) + memory[lang]["review"]
    return {
        "version": 2, "today": today.isoformat(),
        "client": {"display_name": snapshot["client"].get("display_name"),
                   "revision": snapshot["client"].get("revision")},
        "reporting_currency": reporting, "locale": locale,
        "overview": ov,
        "performance": performance(snapshot, _history(service, client_id, "household"), today),
        # The page's charts: net worth split and history, the month's flow, allocation, debts, goals,
        # reserve and returns (see picture_view).
        "picture": picture_view(service, client_id, sit, snapshot, ov, today),
        "groups": groups,
        # What Wealth knows, as sentences grouped by life area (see memory_view).
        "memory": memory,
        "completeness": known,
        "upcoming": upcoming(snapshot, today, labels=fact_labels(sit, language or (sit.get("profile") or {}).get("language"))),
        # The accepted investment policy (profile, sleeves with ranges, reserve, review), or None.
        "policy": policy_summary(current_policy(snapshot, today)),
        # Saved facts left out of every number because they cannot be read; each can be removed.
        "invalid_facts": invalid,
    }


def _invalid_items(invalid: list[dict], language: str) -> list[dict]:
    """Review cards for unreadable facts: named in the person's words, with a remove action."""
    from .store import _label

    items = []
    for index, item in enumerate(invalid):
        label = _label(item["key"], None, language)
        text = (f"La cifra guardada de {label} no se puede usar (es demasiado grande o está mal escrita). "
                "Elimínala y vuelve a escribirla." if language == "es" else
                f"The saved figure for {label} can’t be used (it is too large or malformed). "
                "Remove it and enter it again.")
        items.append({"id": f"invalid-{index}", "topic": "about", "text": text, "emphasis": [], "key": item["key"],
                      "origin": {"kind": "said"}, "since": None, "age_days": None, "unconfirmed": False,
                      "stale": True, "invalid": True, "edit": None, "forget": {"field": None}, "confirm": False})
    return items


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
    if "monthly_contribution" in value:
        number = _num(value["monthly_contribution"])
        if number is None or number < 0:
            raise ValueError("enter a non-negative monthly amount")
        number = int(number) if number.is_integer() else number
        # Older goals kept the monthly figure as amount + frequency; keep their shape.
        if "monthly_contribution" not in goal and goal.get("amount") is not None and goal.get("frequency") == "monthly":
            updated["amount"] = number
        else:
            updated["monthly_contribution"] = number
        updated.pop("approximate", None)
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

    if action == "confirm" and key in _LEGACY_LISTS and field and ":" in field:
        # One legacy item kept as the person said it (e.g. against a statement): mark that item only.
        name, index, _ = _legacy_item(key, current, field)
        items = list(current[name])
        items[index] = {**items[index], "confirmed_on": today.isoformat()}
        return [_user_fact(key, {**current, name: items}, "profile page: confirmed still true", today)]
    if action == "confirm":
        if key in _MARKET_VALUED or key.startswith(("analysis.", "research.")):
            raise ValueError("market values need a fresh statement or analysis, not a confirmation")
        return [_user_fact(key, current, "profile page: confirmed still true", today)]
    if field is None:
        if action == "delete":
            return [_user_fact(key, None, "profile page: removed", today)]
        amount_field = _canonical_amount_field(key, current)
        if amount_field:
            amount = _amount(value)
            patch = {amount_field: amount["amount"], "currency": amount["currency"]}
            if current.get("approximate"):
                patch["approximate"] = False  # the person just gave the exact figure
            return [_user_fact(key, patch, "profile page edit", today, merge=True)]
        if key == "goals" or key in _MARKET_VALUED:
            raise ValueError("this value is structured; update it in the chat")
        return [_user_fact(key, _coerce(_editor(current), value), "profile page edit", today)]
    if key in _LEGACY_LISTS and ":" in field:
        # One item of a legacy list (plan.resources cash/debts/investments, income.schedule items):
        # lists without ids cannot be merge-patched, so the whole object is rewritten.
        name, index, amount_field = _legacy_item(key, current, field)
        items = list(current[name])
        if action == "delete":
            items.pop(index)
            return [_user_fact(key, {**current, name: items}, "profile page: item removed", today)]
        amount = _amount(value)
        item = {k: v for k, v in items[index].items() if k != "approximate"}  # the person just stated it
        items[index] = {**item, amount_field: amount["amount"], "currency": amount["currency"],
                        "confirmed_on": today.isoformat()}
        return [_user_fact(key, {**current, name: items}, "profile page edit", today)]
    if key == "goals":
        if not isinstance(current, list):
            raise ValueError("goals has an unexpected shape")
        position = next((i for i, g in enumerate(current)
                         if isinstance(g, dict) and str(g.get("id", i)) == field), None)
        if position is None:
            raise LookupError("no goal with that id")
        # Older goals had no name; the schema needs one on every rewrite, so give them their read name.
        goals = [{**g, "name": goal_name(g)} if isinstance(g, dict) and not g.get("name") else g for g in current]
        if action == "delete":
            goals.pop(position)
            return [_user_fact(key, goals, "profile page: goal removed", today)]
        goals[position] = _goal_patch(goals[position], value)
        return [_user_fact(key, goals, "profile page: goal edited", today)]
    if not isinstance(current, dict) or field not in current:
        raise LookupError("no such field on this fact")
    if action == "delete":
        return [_user_fact(key, {field: None}, "profile page: field removed", today, merge=True)]
    if isinstance(value, dict) and "amount" in value and _editor(current[field]) == "number":
        # A plain number beside the fact's currency (spending.monthly total, plan.resources essentials).
        amount = _amount(value)
        patch: dict[str, Any] = {field: amount["amount"]}
        if isinstance(current.get("currency"), str):
            patch["currency"] = amount["currency"]
        if current.get("approximate"):
            patch["approximate"] = False
        return [_user_fact(key, patch, "profile page edit", today, merge=True)]
    new_value = _coerce(_editor(current[field]), value)
    if isinstance(new_value, list):  # the store merges only id-keyed lists; replace the object
        return [_user_fact(key, {**current, field: new_value}, "profile page edit", today)]
    return [_user_fact(key, {field: new_value}, "profile page edit", today, merge=True)]


def _canonical_amount_field(key: str, current: Any) -> str | None:
    """The amount field a row edit changes on a canonical fact, or None."""
    if not isinstance(current, dict) or "proposal_id" in current:
        return None
    if key.startswith(("income.", "cash.", "investment.")) and key != "income.schedule" and "amount" in current:
        return "amount"
    if key.startswith("liability.") and "balance" in current:
        return "balance"
    if key == "spending.monthly":
        return "total" if current.get("total") is not None else "essential"
    return None


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


# ---------------------------------------------------------------- today, quarterly review, connections
#
# Payloads for three surfaces: the "Hoy" lines above the chat composer (and the profile rail), the quarterly
# review letter (/review) and the Conexiones section.  Each returns only what its view draws; every number is
# the engine's, and an unknown stays None (drawn as "—").

TODAY_DUE_DAYS = 14          # a due date is shown only when it is this close
SNOOZE_DAYS = 7
_VALUE_IN_TITLE = re.compile(
    r"-?(?:US)?\$\s?\d[\d,]*(?:\.\d+)?(?: ?[A-Z]{3})?"      # $312,712 or $1,000 USD
    r"|\d+(?:[.,]\d+)?\s?%"                                 # 12%
    r"|\d+(?:\.\d+)? (?:of|de) \d+(?:\.\d+)? (?:months|meses)")  # 3.7 of 6 months


def _value_spans(text: str) -> list[list[int]]:
    """Where the title's value sits, so the page sets it in weight (the first money, percent or ratio)."""
    match = _VALUE_IN_TITLE.search(text or "")
    return [[match.start(), match.end()]] if match else []


def today_view(service: Any, client_id: str, *, action: str | None = None, item_id: str | None = None,
               timezone_name: str | None = None) -> dict:
    """At most three lines for today; ``action`` (dismiss | snooze | restore) acknowledges one item first.

    Snooze is always seven days.  Acknowledgements persist per client in the proactive state.
    """
    inputs: dict[str, Any] = {}
    if timezone_name:
        inputs["timezone"] = timezone_name
    if action is not None:
        if action not in {"dismiss", "snooze", "restore"}:
            raise ValueError("Choose dismiss, snooze or restore.")
        if not isinstance(item_id, str) or not 0 < len(item_id) <= 200:
            raise ValueError("Name the item to change.")
        inputs[action] = [{"id": item_id, "days": SNOOZE_DAYS}] if action == "snooze" else [item_id]
    report = service.run("today", inputs, client_id)
    result = report.get("result") or {}
    as_of = _as_date(result.get("as_of")) or datetime.now(timezone.utc).date()
    items = []
    for item in result.get("today") or []:
        due = _as_date(item.get("due"))
        days = (due - as_of).days if due else None
        near = days is not None and 0 <= days <= TODAY_DUE_DAYS
        title = {lang: str((item.get("title") or {}).get(lang) or "") for lang in ("en", "es")}
        items.append({"id": item["id"], "severity": item.get("severity"), "title": title,
                      "emphasis": {lang: _value_spans(text) for lang, text in title.items()},
                      "next_step": {lang: str((item.get("next_step") or {}).get(lang) or "") for lang in ("en", "es")},
                      "due": due.isoformat() if near else None, "days": days if near else None})
    return {"as_of": as_of.isoformat(), "items": items}


# ---- quarterly review

_QUARTER = re.compile(r"^(\d{4})-Q([1-4])$")
REVIEW_QUARTERS = 8


def quarter_bounds(label: str) -> tuple[date, date]:
    match = _QUARTER.match(label or "") if isinstance(label, str) else None
    if not match:
        raise ValueError("Name the quarter as YYYY-Qn, for example 2026-Q2.")
    year, quarter = int(match.group(1)), int(match.group(2))
    start = date(year, 3 * quarter - 2, 1)
    end = (date(year + 1, 1, 1) if quarter == 4 else date(year, 3 * quarter + 1, 1)) - timedelta(days=1)
    return start, end


def _quarter_of(day: date) -> str:
    return f"{day.year}-Q{(day.month - 1) // 3 + 1}"


def review_quarters(first: date | None, today: date) -> list[str]:
    """Completed quarters with ledger history, newest first."""
    labels: list[str] = []
    if first is None:
        return labels
    start = quarter_bounds(_quarter_of(today))[0]
    while len(labels) < REVIEW_QUARTERS:
        label = _quarter_of(start - timedelta(days=1))
        start, end = quarter_bounds(label)
        if end < first:
            break
        labels.append(label)
    return labels


def _dec(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return number if number.is_finite() else None


def _add(*values: Any) -> str | None:
    parts = [_dec(v) for v in values]
    return None if any(p is None for p in parts) else str(sum(parts, Decimal(0)))


def _money_words(value: Any) -> str | None:
    number = _dec(value)
    if number is None:
        return None
    return ("-" if number < 0 else "") + f"${abs(number):,.0f}"


def _pct_words(value: Any, places: int = 1) -> str | None:
    number = _dec(value)
    if number is None:
        return None
    text = f"{abs(number) * 100:.{places}f}"
    if "." in text:  # trailing zeros of the decimals only: 12.50 -> 12.5, 90 stays 90
        text = text.rstrip("0").rstrip(".")
    return ("-" if number < 0 else "") + text + "%"


_NOTES = {
    "price": ("No price for {x} on those dates yet, so this stays unknown.",
              "Aún no hay precio de {x} en esas fechas; por eso no se sabe."),
    "fx": ("An exchange rate for those dates is missing.", "Falta un tipo de cambio de esas fechas."),
    "ips": ("There is no accepted investment policy yet, so there are no bands.",
            "Aún no hay una política de inversión aceptada; por eso no hay rangos."),
    "benchmark": ("No benchmark series is saved for this quarter.", "No hay una serie de referencia guardada para este trimestre."),
    "goal": ("No account is earmarked for this goal yet.", "Aún no hay cuentas asignadas a esta meta."),
    "floor": ("Some costs are unknown, so this is a floor, not the full cost.",
              "Algunos costos no se conocen; esto es un mínimo, no el total."),
    "flows": ("Income and spending for this quarter are incomplete.", "Los ingresos y gastos de este trimestre están incompletos."),
    "other": ("Some inputs are missing for this section.", "Faltan algunos datos para esta sección."),
}


def _note(missing: Iterable[dict], prefer: str | None = None) -> dict | None:
    """One plain line naming what would fill the unknowns in a section."""
    rows = [m for m in missing or [] if isinstance(m, dict)]
    if not rows and prefer is None:
        return None
    kind, names = prefer, []
    for row in rows:
        key = str(row.get("key") or "")
        found = re.match(r"^(?:price |prices\.)([A-Za-z0-9._:-]+?)(?:@| on |$)", key)
        if found:
            kind = kind or "price"
            if found.group(1) not in names:
                names.append(found.group(1))
        elif kind is None:
            kind = ("fx" if key.startswith("fx") else "ips" if key.startswith("policy") else
                    "benchmark" if "benchmark" in key else "goal" if key.startswith("goal") else
                    "floor" if key.startswith(("holdings", "cash_reference", "instruments")) else "other")
    en, es = _NOTES[kind or "other"]
    x = ", ".join(names[:3]) if names else "some holdings"
    return {"en": en.format(x=x), "es": es.format(x=x if names else "algunas posiciones")}


_NEXT = {
    "reserve": (("Top up the emergency reserve", "Completar el fondo de emergencia"),
                ("How do I top up my emergency reserve this quarter? I'm {v} short.",
                 "¿Cómo completo mi fondo de emergencia este trimestre? Me faltan {v}.")),
    "drift": (("Rebalance to your policy ranges, new contributions first",
               "Volver a los rangos de tu política, primero con aportaciones nuevas"),
              ("How do I rebalance to my policy ranges with new contributions first? About {v} is out of place.",
               "¿Cómo vuelvo a los rangos de mi política usando primero aportaciones nuevas? Hay unos {v} fuera de lugar.")),
    "harvest": (("Realise losses to offset gains", "Realizar pérdidas para compensar ganancias"),
                ("Should I realise losses to offset gains this quarter?",
                 "¿Me conviene realizar pérdidas para compensar ganancias este trimestre?")),
    "ppr_headroom": (("Use the PPR deduction room you have left", "Usar el espacio de deducción del PPR que te queda"),
                     ("How much should I put in my PPR before year end?",
                      "¿Cuánto debería aportar a mi PPR antes de fin de año?")),
    "goal_pace": (("Raise the contribution to {name} or move its date", "Subir la aportación a {name} o mover su fecha"),
                  ("{name} is behind pace. Should I contribute more or move the date?",
                   "{name} va atrasada. ¿Aporto más o muevo la fecha?")),
    "fees": (("Compare lower-cost equivalents", "Comparar equivalentes de menor costo"),
             ("Which of my holdings have cheaper equivalents?", "¿Cuáles de mis inversiones tienen equivalentes más baratos?")),
    "dca": (("Catch up the {name} plan", "Ponerte al día con el plan {name}"),
            ("I'm {v} behind on my {name} plan. How do I catch up?",
             "Voy {v} atrasado en mi plan {name}. ¿Cómo me pongo al día?")),
}


# Engine and policy names arrive in English (or in the words the person used); the page shows each in its own
# language.  Pairs read both ways, so a sleeve saved in Spanish still reads in English on the en page.
_LABEL_PAIRS = (
    ("Global equity", "Renta variable global"), ("US equity", "Renta variable de EE. UU."),
    ("Mexican equity", "Renta variable mexicana"), ("International equity", "Renta variable internacional"),
    ("Emerging markets equity", "Renta variable de mercados emergentes"), ("Equity", "Renta variable"),
    ("Mexican government fixed income", "Deuda gubernamental mexicana"),
    ("Mexican fixed income", "Renta fija mexicana"), ("Global fixed income", "Renta fija global"),
    ("US fixed income", "Renta fija de EE. UU."), ("Fixed income", "Renta fija"), ("Bonds", "Bonos"),
    ("Cash (MXN)", "Efectivo (MXN)"), ("Cash (USD)", "Efectivo (USD)"), ("Cash", "Efectivo"),
    ("Real estate", "Bienes raíces"), ("Gold", "Oro"), ("Commodities", "Materias primas"),
    ("Funds", "Fondos"), ("Unclassified", "Sin clasificar"),
)
_REVIEW_LABELS = {text.casefold(): pair for pair in _LABEL_PAIRS for text in pair}
_ASSET_LABELS = {"cash": ("Cash", "Efectivo"), "equity": ("Equity", "Renta variable"),
                 "fixed_income": ("Fixed income", "Renta fija"), "fund": ("Funds", "Fondos"),
                 "real_estate": ("Real estate", "Bienes raíces"), "unknown": ("Unclassified", "Sin clasificar"),
                 "unclassified": ("Unclassified", "Sin clasificar")}
# Instruments a recurring plan buys, named by the index they track (the plan's human name when it has none).
_TRACKS = {**dict.fromkeys(("CSPX", "CSPXN", "VOO", "IVV", "SPY", "VUSA", "VUAA", "SXR8", "IVVPESO"), "S&P 500"),
           **dict.fromkeys(("CNDX", "EQQQ", "QQQ", "QQQM"), "Nasdaq-100"),
           **dict.fromkeys(("VWRA", "VWRL", "VT", "ACWI", "SSAC"), "MSCI ACWI"),
           **dict.fromkeys(("VTI", "ITOT"), "US total market"), **dict.fromkeys(("NAFTRAC",), "S&P/BMV IPC")}
_CADENCE = {"weekly": ("{x} weekly", "{x} semanal"), "biweekly": ("{x} fortnightly", "{x} quincenal"),
            "monthly": ("{x} monthly", "{x} mensual"), "quarterly": ("{x} quarterly", "{x} trimestral")}
# Fixture and provenance notes some inputs carry in a name, e.g. "(fictional levels)": never shown.
_ANNOTATION = re.compile(r"\s*\((?:[^)]*\b(?:fictional|ficticio|ficticia|example|ejemplo|levels|niveles|demo|test|sample)\b[^)]*)\)",
                         re.I)
_INDEX_WORDS = ((r"\b(\d+) days\b", r"\1 días"), (r"\bin (MXN|USD|EUR)\b", r"en \1"),
                (r"^Global aggregate bonds\b", "Bonos globales agregados"), (r"\bequity index\b", "índice accionario"),
                (r"\bbond index\b", "índice de bonos"), (r"^US total market\b", "Mercado total de EE. UU."))


def _both(text: str) -> dict:
    return {"en": text, "es": text}


def _label(name: Any, asset: Any = None) -> dict:
    """A sleeve or asset-class name in both languages; a name only the person uses is shown as they wrote it."""
    text = str(name or "").strip()
    pair = _REVIEW_LABELS.get(text.casefold()) or _ASSET_LABELS.get((text or str(asset or "")).casefold())
    if pair:
        return {"en": pair[0], "es": pair[1]}
    return _both(_humanize(text) if re.fullmatch(r"[a-z0-9_.]+", text) else text)


def _index_label(name: Any) -> dict | None:
    """An index name without fixture notes, with its few common words in the page language."""
    text = _ANNOTATION.sub("", str(name or "")).strip()
    if not text or re.fullmatch(r"(ips|reference_60_40)\.[\w.]+", text):
        return None
    es = text
    for pattern, repl in _INDEX_WORDS:
        es = re.sub(pattern, repl, es)
    en = text
    for pattern, repl in ((r"\b(\d+) días\b", r"\1 days"), (r"\ben (MXN|USD|EUR)\b", r"in \1")):
        en = re.sub(pattern, repl, en)
    return {"en": en, "es": es}


def _benchmark(chosen_key: str | None, sleeves: list[dict], specs: Any) -> tuple[dict | None, list[dict]]:
    """The benchmark as a label plus its parts [{weight, index}], from the policy weights and the index specs.

    With no structured spec (only the engine's free-text name) the parts are empty and the label is generic.
    """
    from . import views as V

    if chosen_key is None:
        return None, []
    specs = specs if isinstance(specs, dict) else {}
    parts: list[dict] = []
    if chosen_key == "ips_benchmark":
        label = {"en": "Your policy benchmark", "es": "Referencia de tu política"}
        ips_specs = specs.get("ips") if isinstance(specs.get("ips"), dict) else {}
        for sleeve in sleeves:
            if not sleeve.get("target"):
                continue
            index = _index_label((ips_specs.get(sleeve.get("sleeve")) or {}).get("name"))
            if index is None:
                return label, []
            parts.append({"weight": V.ratio(sleeve["target"]), "index": index})
    else:
        label = {"en": "Global 60/40 reference", "es": "Referencia global 60/40"}
        ref = specs.get("reference_60_40") if isinstance(specs.get("reference_60_40"), dict) else {}
        for weight, leg in (("0.6", "equity"), ("0.4", "bonds")):
            index = _index_label((ref.get(leg) or {}).get("name"))
            if index is None:
                return label, []
            parts.append({"weight": V.ratio(weight), "index": index})
    return label, parts


def _plan_names(plans: Any, instruments: Iterable[dict]) -> dict[str, dict]:
    """{plan_id: {en, es}}: the plan's own name, else what it buys and how often ("S&P 500 mensual")."""
    underlying = {str(i.get("id")): str(i.get("underlying_symbol") or i.get("symbol") or i.get("id"))
                  for i in instruments or [] if isinstance(i, dict) and i.get("id")}
    rows = plans.get("plans") if isinstance(plans, dict) else plans if isinstance(plans, list) else [plans]
    names: dict[str, dict] = {}
    for plan in rows or []:
        if not isinstance(plan, dict) or not plan.get("id"):
            continue
        if str(plan.get("name") or "").strip():
            names[str(plan["id"])] = _both(str(plan["name"]).strip()[:80])
            continue
        tracked = []
        for leg in plan.get("legs") or []:
            iid = str((leg or {}).get("instrument_id") or "")
            what = _TRACKS.get(iid.upper()) or _TRACKS.get(underlying.get(iid, "").upper()) or iid
            if what and what not in tracked:
                tracked.append(what)
        if not tracked:
            continue
        what = " + ".join(tracked[:3])
        what_es = re.sub(r"^US total market$", "Mercado total de EE. UU.", what)
        en, es = _CADENCE.get(plan.get("cadence"), ("{x}", "{x}"))
        names[str(plan["id"])] = {"en": en.format(x=what), "es": es.format(x=what_es)}
    return names


def _next_item(candidate: dict, currency: str | None, plan_names: dict[str, dict] | None = None) -> dict:
    from . import views as V

    kind = candidate.get("kind")
    data = candidate.get("data") or {}
    name = str(data.get("name") or data.get("goal") or data.get("plan_id") or "").strip()
    if kind == "goal_pace" and not name:
        found = re.search(r"to (.+?) or move", str(candidate.get("title") or ""))
        name = found.group(1) if found else ""
    if kind == "dca" and not name:
        found = re.search(r"up the (.+?) plan", str(candidate.get("title") or ""))
        name = found.group(1) if found else ""
    names = (plan_names or {}).get(name) if kind == "dca" else None
    names = names or {"en": name or "—", "es": name or "—"}
    (title_en, title_es), (ask_en, ask_es) = _NEXT.get(kind, ((str(candidate.get("title") or ""),) * 2,
                                                               ("Let's talk about: {t}", "Hablemos de: {t}")))
    words = {"v": _money_words(candidate.get("value")) or "—", "t": candidate.get("title") or ""}
    en, es = {**words, "name": names["en"]}, {**words, "name": names["es"]}
    return {"kind": kind, "title": {"en": title_en.format(**en), "es": title_es.format(**es)},
            "value": V.money(candidate.get("value"), currency),
            "prompt": {"en": ask_en.format(**en), "es": ask_es.format(**es)}}


def _quarter_label(label: str, partial: bool = False) -> dict:
    """How the page names a quarter: "T3 2026" / "Q3 2026", and "T3 2026 · en curso" / "Q3 2026 · to date"."""
    year, q = label.split("-Q")
    return {"en": f"Q{q} {year}" + (" · to date" if partial else ""),
            "es": f"T{q} {year}" + (" · en curso" if partial else "")}


def _latest_evidence(service: Any, client_id: str, ledger_dates: Iterable[date | None], today: date) -> date | None:
    """The latest ledger entry or statement date on or before today (None when there is neither)."""
    days = [d for d in ledger_dates if d]
    try:
        facts = _snapshot(service, client_id)["facts"]
    except Exception:  # noqa: BLE001 - the review still works from the ledger alone
        facts = []
    for fact in facts:
        if fact["key"].startswith("account.") and isinstance(fact.get("value"), dict):
            day = _as_date(fact["value"].get("as_of"))
            if day:
                days.append(day)
    days = [d for d in days if d <= today]
    return max(days, default=None)


def _review_summary(narrative: dict, label: str, partial: bool = False) -> dict:
    """The letter's slot until the model writes it: two plain sentences from ``narrative_inputs``."""
    nw = narrative.get("net_worth") or {}
    cf = narrative.get("cash_flow") or {}
    pf = narrative.get("performance") or {}
    year, q = label.split("-Q")
    when = ({"en": f"Q{q} {year} so far", "es": f"lo que va del T{q} {year}"} if partial else
            {"en": f"Q{q} {year}", "es": f"el T{q} {year}"})
    before = ({"en": "the same stretch of the quarter before", "es": "el mismo tramo del trimestre anterior"}
              if partial else {"en": "the quarter before", "es": "el trimestre anterior"})
    start, end = _money_words(nw.get("start")), _money_words(nw.get("end"))
    contrib, market = _money_words(nw.get("contributions")), _money_words(nw.get("market"))
    if start and end:
        first = {"en": f"Your net worth went from {start} to {end} in {when['en']}"
                       + (f": {contrib} you added and {market} from markets." if contrib and market else "."),
                 "es": f"Tu patrimonio pasó de {start} a {end} en {when['es']}"
                       + (f": {contrib} que aportaste y {market} del mercado." if contrib and market else ".")}
    elif contrib and _dec(nw.get("contributions")) != 0:
        # Without a start and end value a zero here is not a known zero, and the page never guesses why.
        first = {"en": f"You added {contrib} net in {when['en']}; the total change is not known yet.",
                 "es": f"Aportaste {contrib} netos en {when['es']}; el cambio total aún no se sabe."}
    else:
        first = {"en": f"The change in your net worth in {when['en']} is not known yet.",
                 "es": f"Aún no se sabe cuánto cambió tu patrimonio en {when['es']}."}
    rate, change = _pct_words(cf.get("savings_rate"), 0), _dec(cf.get("savings_rate_change"))
    twr, bench = _pct_words(pf.get("twr_period")), _pct_words(pf.get("ips_benchmark") or pf.get("global_60_40"))
    if rate:
        pts = None if change is None or change == 0 else f"{abs(change) * 100:.0f}"
        second_en = f"You saved {rate} of your income" + (
            f", {pts} points {'more' if change > 0 else 'less'} than {before['en']}" if pts else "")
        second_es = f"Ahorraste el {rate} de tu ingreso" + (
            f", {pts} puntos {'más' if change > 0 else 'menos'} que {before['es']}" if pts else "")
        if twr and bench:
            second_en += f"; your portfolio returned {twr} against {bench} for its benchmark."
            second_es += f"; tu portafolio rindió {twr} contra {bench} de su referencia."
        else:
            second_en += "."
            second_es += "."
        second = {"en": second_en, "es": second_es}
    elif twr and bench:
        second = {"en": f"Your portfolio returned {twr} against {bench} for its benchmark.",
                  "es": f"Tu portafolio rindió {twr} contra {bench} de su referencia."}
    else:
        second = {"en": "Your savings rate and returns are not known yet for this quarter.",
                  "es": "Tu tasa de ahorro y tu rendimiento aún no se conocen para este trimestre."}
    return {lang: [first[lang], second[lang]] for lang in ("en", "es")}


def review_view(service: Any, client_id: str, period: str | None = None, today: Any = None,
                inputs: dict | None = None) -> dict:
    """The quarterly letter for one completed quarter (default: the last one), shaped for /review.

    ``inputs`` are extra quarterly_review inputs (prices, benchmarks, fees options) a host may supply.
    """
    from . import views as V
    from .store import WealthStore

    today = _today(today)
    with WealthStore(service.db_path) as store:
        ledger = store.ledger(client_id)
    dates = [_as_date(e.get("date")) for e in ledger.get("entries") or []]
    first = min((d for d in dates if d), default=None)
    quarters = review_quarters(first, today)
    # The quarter in progress is offered to date once a statement or ledger entry falls inside it.
    current_label = _quarter_of(today)
    current_start, current_end = quarter_bounds(current_label)
    latest = _latest_evidence(service, client_id, dates, today)
    current = None
    if latest is not None and latest >= current_start:
        current = {"period": current_label, "start": current_start.isoformat(), "end": latest.isoformat(),
                   "quarter_end": current_end.isoformat(), "label": _quarter_label(current_label, partial=True)}
    label = period or (quarters[0] if quarters else current_label if current else
                       _quarter_of(current_start - timedelta(days=1)))
    start, end = quarter_bounds(label)
    partial = label == current_label
    if partial:
        if current is None:
            raise ValueError("Choose a quarter that has ended, or upload a statement from this quarter.")
        end = latest
    elif end >= today:
        raise ValueError("Choose a quarter that has ended.")
    base = {"version": 1, "period": label, "start": start.isoformat(), "end": end.isoformat(), "quarters": quarters,
            "current": current, "partial": partial, "label": _quarter_label(label, partial=partial),
            "quarter_end": quarter_bounds(label)[1].isoformat(), "today": today.isoformat()}
    if first is None or end < first or not (quarters or partial):
        return {**base, "status": "needs_input", "currency": None, "letter": None, "sections": None}
    report = service.run("quarterly_review", {**(inputs or {}), "period_start": start.isoformat(),
                                              "period_end": end.isoformat()}, client_id)
    if report.get("status") == "needs_input":
        return {**base, "status": "needs_input", "currency": None, "letter": None, "sections": None}
    result = report["result"]
    cur = result.get("currency")
    sec = result["sections"]
    money = lambda v: V.money(v, cur)  # noqa: E731
    L = V.L

    nw = sec["net_worth"]["data"]
    income_net = _add((nw.get("growth") or {}).get("investment_income"), (nw.get("growth") or {}).get("fees_and_withholding"))
    ticket_rows = [{"label": L("Start", "Inicio"), "date": V.when(nw.get("opening_date")), "value": money(nw.get("start"))},
                   {"label": L("Net contributions", "Aportaciones netas"), "value": money((nw.get("contributions") or {}).get("net"))},
                   {"label": L("Markets", "Mercado"), "value": money((nw.get("growth") or {}).get("market"))}]
    if _dec(income_net) not in (None, Decimal(0)):
        ticket_rows.append({"label": L("Dividends and interest, less fees", "Dividendos e intereses, menos comisiones"),
                            "value": money(income_net)})
    residual = (nw.get("identity") or {}).get("residual")
    if _dec(residual) not in (None, Decimal(0)):
        # What the flows and market move do not explain, mostly foreign cash moving with the exchange rate.
        ticket_rows.append({"label": L("Exchange rate and other", "Tipo de cambio y otros"), "value": money(residual)})
    net_worth = {"rows": ticket_rows, "total": {"label": L("End", "Cierre"), "date": V.when(nw.get("closing_date")),
                                               "value": money(nw.get("end"))},
                 "note": _note(sec["net_worth"]["missing"]) if nw.get("start") is None or nw.get("end") is None else None}

    pf = sec["performance"]["data"]
    total = pf.get("total") or {}
    bench = (pf.get("benchmarks") or {}).get("ips_benchmark") or {}
    ref = (pf.get("benchmarks") or {}).get("global_60_40") or {}
    chosen_key = ("ips_benchmark" if bench.get("period_return") is not None else
                  "global_60_40" if ref.get("period_return") is not None or not bench else "ips_benchmark")
    chosen = bench if chosen_key == "ips_benchmark" else ref
    market = result.get("market_data") or {}
    bench_label, bench_parts = _benchmark(chosen_key, (sec["allocation"]["data"] or {}).get("sleeves") or [],
                                          (inputs or {}).get("benchmarks") or market.get("benchmarks"))
    performance_view = {
        "portfolio": V.ratio(total.get("twr_period")), "xirr": V.ratio(total.get("xirr_annual")),
        "benchmark": {"label": bench_label, "parts": bench_parts, "value": V.ratio(chosen.get("period_return"))},
        "difference": V.ratio(chosen.get("excess_twr")),
        "note": (_note(sec["performance"]["missing"]) if total.get("twr_period") is None else
                 _note([], "benchmark") if chosen.get("period_return") is None else None)}

    al = sec["allocation"]["data"]
    priced = al.get("portfolio_value") is not None  # weights of a partly priced portfolio would mislead
    sleeves = [{"name": _label(s.get("name") or s.get("sleeve"), s.get("sleeve")), "id": s.get("sleeve"),
                "weight": V.ratio(s.get("weight") if priced else None), "min": V.ratio(s.get("min")), "max": V.ratio(s.get("max")),
                "target": V.ratio(s.get("target")), "outside": priced and bool(s.get("outside_band"))}
               for s in al.get("sleeves") or []]
    has_bands = any(s["min"]["v"] is not None for s in sleeves)
    allocation = {"as_of": al.get("as_of"), "sleeves": sleeves,
                  "note": (_note(sec["allocation"]["missing"]) if al.get("portfolio_value") is None else
                           _note([], "ips") if not has_bands else None)}

    cf = sec["cash_flow"]["data"]

    def flow(block: dict | None) -> dict | None:
        if not block:
            return None
        return {"income": money(block.get("income")), "spending": money(block.get("spending")),
                "net": money(block.get("net")), "savings_rate": V.ratio(block.get("savings_rate"))}
    cash_flow = {"current": flow(cf.get("current")), "prior": flow(cf.get("prior")),
                 "change": V.ratio((cf.get("change") or {}).get("savings_rate")),
                 "note": _note(sec["cash_flow"]["missing"], "flows") if not cf.get("current") or sec["cash_flow"]["missing"] else None}

    gl = sec["goals"]["data"]
    goals = {"items": [{"name": str(g.get("name") or g.get("id") or ""), "funded": V.ratio(g.get("funded_pct")),
                        "target": V.money(g.get("target"), g.get("currency") or cur), "by": V.when(g.get("target_date")),
                        "status": g.get("status") if g.get("status") in {"on_track", "behind", "funded", "unknown"} else "unknown"}
                       for g in gl.get("goals") or []],
             "note": _note(sec["goals"]["missing"], "goal") if sec["goals"]["missing"] else None}

    dc = sec["decisions"]["data"]
    decisions = {"items": [{"what": str(d.get("what") or "")[:200], "status": d.get("status"),
                            "on": V.when(d.get("decided_on") or d.get("proposed_on")),
                            "trades": V.count((d.get("what_happened") or {}).get("trades")),
                            "invested": money((d.get("what_happened") or {}).get("net_invested"))}
                           for d in dc.get("decisions") or []],
                 "open_before": len(dc.get("still_open_from_before") or []), "note": None}

    plan_names = _plan_names((inputs or {}).get("dca_plans") or (_facts_by_key(_snapshot(service, client_id))
                                                                     .get("planning.dca") or {}).get("value"),
                             ledger.get("instruments") or [])
    dca = {"items": [{"name": plan_names.get(str(p.get("plan_id") or "")) or _both(_humanize(str(p.get("plan_id") or ""))),
                      "on_time": V.count((p.get("counts") or {}).get("on_time")),
                      "installments": V.count(p.get("installments")), "rate": V.ratio(p.get("on_time_rate")),
                      "invested": V.money(p.get("invested"), p.get("currency") or cur),
                      "planned": V.money(p.get("planned"), p.get("currency") or cur),
                      "missed": [V.when(d) for d in (p.get("missed") or [])[:3]]}
                     for p in sec["dca"]["data"].get("plans") or []],
           "note": _note(sec["dca"]["missing"]) if sec["dca"]["missing"] else None}

    tx = sec["taxes"]["data"]
    tax_period = ((tx.get("period") or {}).get("estimated_tax") or {}).get("total")
    tax_ytd = ((tx.get("year_to_date") or {}).get("estimated_tax") or {}).get("total")
    taxes = {"jurisdiction": tx.get("jurisdiction"), "period": money(tax_period), "ytd": money(tax_ytd),
             "note": _note(sec["taxes"]["missing"]) if tax_period is None or tax_ytd is None else None}

    fe = sec["fees"]["data"]
    annual = fe.get("annual_cost") or {}
    fees = {"paid": money((fe.get("paid_in_period") or {}).get("total")),
            "annual": V.span(annual.get("low"), annual.get("high"), cur),
            "bps": {"lo": None if _dec(annual.get("bps_low")) is None else str(annual["bps_low"]),
                    "hi": None if _dec(annual.get("bps_high")) is None else str(annual["bps_high"])},
            "complete": bool(annual.get("complete")),
            "note": None if annual.get("complete") and annual.get("low") is not None else _note([], "floor")}

    nq = sec["next_quarter"]["data"]
    next_quarter = {"items": [_next_item(c, cur, plan_names) for c in (nq.get("two_decisions") or [])[:2]], "note": None}

    ticket_spec = {"id": "review-" + "0" * 10, "kind": "ticket", "title": L("Net worth", "Patrimonio"),
                   "data": {"rows": [{k: v for k, v in r.items() if k != "date"} for r in ticket_rows],
                            "total": {"label": net_worth["total"]["label"], "value": net_worth["total"]["value"]}},
                   "source": {"task": "quarterly_review", "label": L("Quarterly review", "Revisión trimestral")}}
    V.validate(ticket_spec)  # the identity ticket is a view primitive; its shape must stay drawable anywhere

    return {**base, "status": report.get("status"), "currency": cur,
            "name": (result.get("narrative_inputs") or {}).get("name"),
            "letter": {"narrative": None,
                       "summary": _review_summary(result.get("narrative_inputs") or {}, label, partial)},
            "sections": {"net_worth": net_worth, "performance": performance_view, "allocation": allocation,
                         "cash_flow": cash_flow, "goals": goals, "decisions": decisions, "dca": dca, "taxes": taxes,
                         "fees": fees, "next_quarter": next_quarter},
            # Market data the provider supplied (none when the host passed its own prices): every source with
            # its date, the benchmark proxies (named as proxies), and prices too old to be current.
            "market_data": {"sources": [{"source": p.get("source"), "date": p.get("last") or p.get("date"),
                                         "symbol": p.get("instrument_id")} for p in market.get("prices") or []],
                            "proxies": market.get("proxies") or [], "offline": market.get("offline")}
            if market else None,
            "stale_prices": market.get("stale_prices") or []}


# ---- connections

_CONNECTOR_ORDER = ("ibkr_flex", "alpaca", "cuenca")
_STALE_DAYS = 35


def _connector_setup(name: str) -> dict:
    """The exact keychain commands from docs/cli.md (the page shows them; it never takes a secret)."""
    from .connectors import alpaca, cuenca, ibkr_flex

    if name == "ibkr_flex":
        return {"commands": [f'security add-generic-password -U -s {ibkr_flex.KEYCHAIN_SERVICE} -a "$USER" -w'],
                "env": [ibkr_flex.TOKEN_ENV], "needs": ["query_id"]}
    if name == "alpaca":
        return {"commands": [f"security add-generic-password -U -s {alpaca.KEYCHAIN_SERVICE} -a key_id -w",
                             f"security add-generic-password -U -s {alpaca.KEYCHAIN_SERVICE} -a secret -w"],
                "env": [alpaca.KEY_ENV, alpaca.SECRET_ENV], "needs": []}
    return {"commands": [f"security add-generic-password -U -s {cuenca.KEYCHAIN_SERVICE} -a api_key -w",
                         f"security add-generic-password -U -s {cuenca.KEYCHAIN_SERVICE} -a api_secret -w"],
            "env": [cuenca.KEY_ENV, cuenca.SECRET_ENV], "needs": []}


def _account_freshness(service: Any, client_id: str, today: date) -> list[dict]:
    """Every known account with the date its data runs to (statement date, else its latest ledger line)."""
    from .store import WealthStore

    with WealthStore(service.db_path) as store:
        ledger = store.ledger(client_id)
    latest: dict[str, date] = {}
    for entry in ledger.get("entries") or []:
        day = _as_date(entry.get("date"))
        account = entry.get("account_id")
        if day and account and (account not in latest or day > latest[account]):
            latest[account] = day
    rows: dict[str, dict] = {}
    try:
        sit = service.situation(client_id, today=today) if hasattr(service, "situation") else {}
    except Exception:  # noqa: BLE001 - freshness is best effort; the ledger still answers
        sit = {}
    for account in (sit or {}).get("accounts") or []:
        if not account.get("id"):
            continue
        # Raw account types ('checking', 'roth_ira') never reach the page: the type in plain words, or the currency.
        rows[str(account["id"])] = {"id": str(account["id"]), "label": account_label(account, "en")
                                    if not account.get("institution") else str(account["institution"]) + (
                                        f" ({account['currency']})" if account.get("currency") else ""),
                                    "institution": account.get("institution"), "as_of": _as_date(account.get("as_of"))}
    for account in ledger.get("accounts") or []:
        aid = str(account.get("id") or "")
        if not aid:
            continue
        row = rows.setdefault(aid, {"id": aid, "label": str(account.get("institution") or aid),
                                    "institution": account.get("institution"), "as_of": None})
        if row["as_of"] is None or (latest.get(aid) and latest[aid] > row["as_of"]):
            row["as_of"] = latest.get(aid) or row["as_of"]
    out = []
    names: dict[str, int] = {}
    for row in rows.values():
        # The institution is the name a person uses; two accounts at one institution keep their own labels.
        if row["institution"]:
            names[str(row["institution"])] = names.get(str(row["institution"]), 0) + 1
    for row in rows.values():
        days = (today - row["as_of"]).days if row["as_of"] else None
        label = str(row["institution"]) if row["institution"] and names[str(row["institution"])] == 1 else row["label"]
        out.append({"id": row["id"], "label": label[:80], "institution": row["institution"],
                    "as_of": row["as_of"].isoformat() if row["as_of"] else None, "days": days,
                    "stale": days is None or days > _STALE_DAYS})
    out.sort(key=lambda r: (r["label"].lower(), r["id"]))
    return out


def connections_view(service: Any, client_id: str, today: Any = None) -> dict:
    """One row per read-only connector plus the statement row, with per-account freshness; never a secret."""
    import shlex

    today = _today(today)
    status = service.ingest(client_id, "connector_status", {})
    by_name = {row["name"]: row for row in (status.get("result") or {}).get("connectors") or []}
    accounts = _account_freshness(service, client_id, today)
    claimed: set[str] = set()
    rows = []
    for name in _CONNECTOR_ORDER:
        entry = by_name.get(name)
        if entry is None:
            continue
        institution = str(entry.get("institution") or name)
        key = institution.split()[0].lower()
        mine = [a for a in accounts if key in str(a.get("institution") or a["label"]).lower()]
        claimed.update(a["id"] for a in mine)
        last = entry.get("last_sync") or {}
        token = entry.get("token")
        rows.append({"name": name, "institution": institution, "configured": bool(entry.get("ready")),
                     "via": token if token in {"keychain", "env"} else None,
                     "last_sync": str(last.get("pulled_at"))[:25] if last.get("pulled_at") else None,
                     "accounts": mine, "setup": _connector_setup(name)})
    statements = [a for a in accounts if a["id"] not in claimed]
    return {"today": today.isoformat(), "connectors": rows, "statements": {"accounts": statements},
            "data": {"export": True, "forget_command": f"uv run wealth forget --client {shlex.quote(client_id)}"}}


def export_payload(service: Any, client_id: str) -> dict:
    """The full private export (the same one ``wealth client`` action export writes)."""
    return service.inspect(client_id, detail="export")


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
    from .situation.model import underlying_of

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
    return {**base, "status": "ready", "missing": [], "segments": segments,
            "committed": [{"name": c.get("name"), "kind": c.get("kind"), "value": _r(c.get("monthly"))} for c in committed_items],
            "savings_rate": round(saved / income, 4) if income > 0 else None}


def _spending_months(sit: dict, ledger: dict | None) -> dict | None:
    """Spending per complete ledger month and the three largest categories, when the ledger has two or more months."""
    spending = sit.get("spending") or {}
    period = spending.get("ledger_period")
    if spending.get("source") != "ledger" or not ledger or not period or not sit.get("currency"):
        return None
    from .cashflow import spending_report

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


def _amortization(balance: float, annual_rate: float, payment: float, limit: int = 600) -> list[tuple[int, float]]:
    rate, remaining, points, month = annual_rate / 12, balance, [(0, balance)], 0
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
            row["points"] = [[m, round(b, 2)] for m, b in _amortization(balance, row["rate"], _num(r["monthly_payment"]))]
        out.append(row)
    out.sort(key=lambda d: -(d["value"] or 0))
    return out


def _goal_funded(goal: dict, sit: dict) -> float | None:
    """Cash earmarked for the goal (purpose goal:<id>), in the goal's currency; None when it cannot be converted."""
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
    return total if found else 0.0


def _dca_plans(snapshot_facts: dict[str, dict]) -> list[dict]:
    from . import dca as dca_module

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
    from . import dca as dca_module

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
        funded = _goal_funded(goal, sit) if target is not None else None
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
    from .ledger.derive import active_entries, price_table
    from .ledger.performance import external_flows, valuation_series
    from .prices import benchmarks, fx_rows, ledger_series, with_fx
    from .service import current_ledger

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
    scope = [a for a in valued if kinds.get(a) in invest]
    skipped = sorted({labels[a] for a in first if kinds.get(a) in invest and a not in scope})
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
            bench_name = (_pair(("Global 60/40 (ACWI and BNDW proxies)", "Global 60/40 (referencia ACWI y BNDW)")) if b0
                          else _pair(("Global equity (ACWI proxy)", "Acciones globales (referencia ACWI)")))
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
        from .store import WealthStore

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


__all__ = ["profile_view", "situation_overview", "differences", "fact_detail", "fact_action", "form_facts", "time_weighted_return", "xirr", "classify_key",
           "memory_groups", "completeness", "overview", "performance", "goals_view", "upcoming",
           "today_view", "review_view", "review_quarters", "quarter_bounds", "connections_view", "export_payload"]
