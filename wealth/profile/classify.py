"""Classification of stored facts into the profile's groups, rows, completeness and fact detail."""
from __future__ import annotations

import re
from datetime import date
from typing import Any, Iterable

from ..situation.model import goal_name
from .helpers import (
    FORM_FIELDS, _CURRENCY, _MARKET_VALUED, _dict_value, _facts_by_key, _history, _humanize, _memory_lang,
    _num, _snapshot, _stale, _today,
)


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
