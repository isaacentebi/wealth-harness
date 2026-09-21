"""Spending, income, and investable surplus derived from the ledger.

Categorisation is deterministic and explainable.  Each expense or income entry
gets one category from, in order:

1. the person's confirmed label for that entry;
2. the person's remembered rules (``ledger.category_rules``);
3. a category the statement itself reported;
4. the built-in English/es-MX merchant and keyword rules below;
5. a model suggestion, kept as ``inferred`` until the person confirms it.

Anything else is ``uncategorized``.  For the surplus, uncategorized and
unknown-essentiality spending counts as essential so that uncertainty lowers,
never raises, the amount called investable.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal
import re
from statistics import median, pstdev
from typing import Any, Callable, Iterable, Mapping, Sequence

from .ledger.derive import FxTable, _d, active_entries, envelope, replay
from .ledger.model import fold, money, out


_ZERO = Decimal(0)

# essential: counts toward essential spending; fixed: typical monthly commitment.
CATEGORIES: dict[str, dict[str, Any]] = {
    "housing": {"essential": True, "fixed": True},
    "utilities": {"essential": True, "fixed": True},
    "telecom": {"essential": True, "fixed": True},
    "groceries": {"essential": True, "fixed": False},
    "transport": {"essential": True, "fixed": False},
    "education": {"essential": True, "fixed": True},
    "health": {"essential": True, "fixed": False},
    "insurance": {"essential": True, "fixed": True},
    "taxes": {"essential": True, "fixed": False},
    "childcare": {"essential": True, "fixed": True},
    "convenience": {"essential": False, "fixed": False},
    "dining": {"essential": False, "fixed": False},
    "food_delivery": {"essential": False, "fixed": False},
    "subscriptions": {"essential": False, "fixed": True},
    "shopping": {"essential": False, "fixed": False},
    "travel": {"essential": False, "fixed": False},
    "entertainment": {"essential": False, "fixed": False},
    "personal_care": {"essential": False, "fixed": False},
    "gifts_donations": {"essential": False, "fixed": False},
    "bank_fees": {"essential": False, "fixed": False},
    "cash_withdrawal": {"essential": None, "fixed": False},
    "other": {"essential": None, "fixed": False},
}
INCOME_CATEGORIES = frozenset({"salary", "aguinaldo", "ptu", "bonus", "freelance", "rental", "pension",
                               "refund", "other_income"})
IRREGULAR_INCOME = frozenset({"aguinaldo", "ptu", "bonus", "refund"})

# (keywords, category, applies_to).  Keywords are matched as whole words on
# accent-free lowercase text; longer keywords win (so "uber eats" beats "uber").
_RULES: tuple[tuple[tuple[str, ...], str, str], ...] = (
    (("renta", "arrendamiento", "rent", "cuota de mantenimiento", "mantenimiento condominio", "hoa"), "housing", "expense"),
    (("cfe", "comision federal de electricidad", "sacmex", "siapa", "agua", "naturgy", "gas natural", "gas lp",
      "electricity", "water bill", "con edison", "pg&e", "duke energy"), "utilities", "expense"),
    (("telmex", "telcel", "izzi", "totalplay", "megacable", "at&t", "att", "movistar", "bait", "verizon",
      "t mobile", "comcast", "xfinity", "spectrum"), "telecom", "expense"),
    (("walmart", "bodega aurrera", "aurrera", "soriana", "chedraui", "la comer", "fresko", "city market",
      "superama", "costco", "sams club", "heb", "h e b", "whole foods", "trader joe", "kroger", "safeway",
      "publix", "aldi", "supermercado", "mercado"), "groceries", "expense"),
    (("oxxo", "7 eleven", "seven eleven", "circle k"), "convenience", "expense"),
    (("restaurante", "restaurant", "starbucks", "cafe", "taqueria", "tacos", "mcdonalds", "mcdonald s",
      "burger king", "kfc", "dominos", "little caesars", "vips", "toks", "sanborns"), "dining", "expense"),
    (("rappi", "uber eats", "didi food", "doordash", "grubhub", "sin delantal"), "food_delivery", "expense"),
    (("uber", "didi", "cabify", "lyft", "metro", "metrobus", "gasolina", "gasolinera", "pemex", "shell", "bp",
      "mobil", "caseta", "iave", "pase", "televia", "estacionamiento", "parking", "gas station"), "transport", "expense"),
    (("colegiatura", "escuela", "universidad", "inscripcion", "tuition", "school", "colegio"), "education", "expense"),
    (("farmacia", "farmacias guadalajara", "farmacias del ahorro", "farmacias san pablo", "benavides",
      "hospital", "medico", "doctor", "consultorio", "laboratorio", "chopo", "dentista", "cvs", "walgreens",
      "pharmacy"), "health", "expense"),
    (("seguro", "seguros", "gnp", "axa", "metlife", "qualitas", "mapfre", "insurance", "geico", "allstate"), "insurance", "expense"),
    (("netflix", "spotify", "disney", "hbo", "max com", "amazon prime", "prime video", "apple com bill",
      "itunes", "icloud", "youtube premium", "google storage", "google one", "openai", "chatgpt", "xbox",
      "playstation", "paramount", "crunchyroll"), "subscriptions", "expense"),
    (("amazon", "mercado libre", "mercadolibre", "mercadopago", "liverpool", "palacio de hierro", "coppel",
      "elektra", "suburbia", "sears", "zara", "h&m", "target", "best buy", "home depot", "shein"), "shopping", "expense"),
    (("aeromexico", "volaris", "viva aerobus", "vivaaerobus", "hotel", "airbnb", "booking com", "expedia",
      "united airlines", "american airlines", "delta air", "ado"), "travel", "expense"),
    (("cinepolis", "cinemex", "ticketmaster", "boletos", "cine"), "entertainment", "expense"),
    (("sat", "predial", "tenencia", "impuesto", "impuestos", "irs", "tesoreria"), "taxes", "expense"),
    (("comision", "comisiones", "anualidad", "iva comision", "overdraft", "cargo por"), "bank_fees", "expense"),
    (("retiro cajero", "retiro en cajero", "retiro efectivo", "atm", "disposicion efectivo", "cajero"), "cash_withdrawal", "expense"),
    (("guarderia", "estancia infantil", "daycare", "nanny", "ninera"), "childcare", "expense"),
    (("estetica", "barberia", "spa", "gym", "gimnasio", "smart fit", "sport city", "salon"), "personal_care", "expense"),
    (("donativo", "donacion", "cruz roja", "teleton", "donation"), "gifts_donations", "expense"),
    (("nomina", "pago de nomina", "salario", "sueldo", "payroll", "direct dep", "direct deposit"), "salary", "income"),
    (("aguinaldo",), "aguinaldo", "income"),
    (("ptu", "reparto de utilidades"), "ptu", "income"),
    (("bono", "bonus"), "bonus", "income"),
    (("honorarios", "freelance", "invoice", "factura cobrada"), "freelance", "income"),
    (("pension", "imss pension", "social security"), "pension", "income"),
    (("devolucion", "reembolso", "refund", "cashback"), "refund", "income"),
)
_BUILTIN = sorted(
    ((kw, category, applies, f"builtin:{category}") for kws, category, applies in _RULES for kw in kws),
    key=lambda rule: (-len(rule[0]), rule[0]),
)


def _matches(keyword: str, text: str) -> bool:
    return re.search(r"(?:^| )" + re.escape(keyword) + r"(?: |$)", text) is not None


_SPENDING_ACCOUNTS = frozenset({"checking", "savings", "bank", "cash", "credit_card", "debit"})


def _applies(entry: Mapping[str, Any], account_types: Mapping[str, str] | None = None) -> str | None:
    """expense/income for spending analysis; bank-account fees count as spending,
    brokerage fees stay investment costs (they belong to performance)."""

    if entry["kind"] == "expense":
        return "expense"
    if entry["kind"] == "income":
        return "income"
    if entry["kind"] == "fee" and not entry.get("instrument_id") and \
            (account_types or {}).get(entry["account_id"]) in _SPENDING_ACCOUNTS:
        return "expense"
    return None


def merchant_key(description: str | None) -> str:
    """Stable merchant identity: folded text without digits, first three words."""

    words = [w for w in fold(description).split() if not any(ch.isdigit() for ch in w)]
    return " ".join(words[:3])


def categorize(ledger: Mapping[str, Any], *, include_inferred: bool = False) -> dict[str, dict[str, Any]]:
    """Map every spending/income entry id to ``{category, status, basis}``.

    ``status`` is ``confirmed`` (the person), ``reported`` (statement),
    ``rule`` (deterministic rule), ``inferred`` (model suggestion awaiting
    confirmation), or ``uncategorized``.
    """

    entries, _ = active_entries(ledger, include_inferred=include_inferred)
    labels: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
    for label in ledger.get("labels", []):
        labels[label["entry_id"]][label["status"]] = label  # later labels win per status
    user_rules = []
    for rule in ledger.get("category_rules", []):
        pattern = fold(rule.get("pattern"))
        if pattern and rule.get("category"):
            user_rules.append((pattern, rule))
    user_rules.sort(key=lambda item: (-len(item[0]), item[1].get("id", "")))
    result = {}
    account_types = {a["id"]: a.get("type") for a in ledger.get("accounts", [])}
    for entry in entries:
        applies = _applies(entry, account_types)
        if applies is None:
            continue
        text = fold(" ".join(filter(None, [entry.get("description"), entry.get("memo")])))
        own = labels.get(entry["id"], {})
        choice = None
        if "confirmed" in own:
            choice = {"category": own["confirmed"]["category"], "status": "confirmed", "basis": own["confirmed"]["source"]}
        if choice is None:
            for pattern, rule in user_rules:
                if rule.get("kind") not in (None, applies) or rule.get("account_id") not in (None, entry["account_id"]):
                    continue
                if (pattern == text) if rule.get("match") == "exact" else _matches(pattern, text):
                    choice = {"category": rule["category"], "status": "confirmed", "basis": rule.get("id", "user rule")}
                    break
        if choice is None and ("reported" in own or entry.get("category")):
            category = own["reported"]["category"] if "reported" in own else entry["category"]
            choice = {"category": category, "status": "reported", "basis": "statement"}
        if choice is None and applies == "income" and entry.get("subtype") not in (None, "other"):
            choice = {"category": entry["subtype"], "status": "reported", "basis": "income subtype"}
        if choice is None:
            for keyword, category, rule_applies, rule_id in _BUILTIN:
                if rule_applies == applies and _matches(keyword, text):
                    choice = {"category": category, "status": "rule", "basis": f"{rule_id}:{keyword}"}
                    break
        if choice is None and entry["kind"] == "fee":
            choice = {"category": "bank_fees", "status": "rule", "basis": "kind:fee"}
        if choice is None and "inferred" in own:
            choice = {"category": own["inferred"]["category"], "status": "inferred", "basis": own["inferred"]["source"]}
        if choice is None:
            choice = {"category": "uncategorized" if applies == "expense" else "other_income",
                      "status": "uncategorized", "basis": None}
        choice["kind"] = applies
        result[entry["id"]] = choice
    return result


def suggest_categories(ledger: Mapping[str, Any], suggester: Callable[[dict[str, Any], list[str]], str | None], *,
                       limit: int = 50) -> list[dict[str, Any]]:
    """Ask the host model to propose categories for uncategorized entries.

    ``suggester(entry, allowed)`` receives only the date, amount, currency,
    description and kind, and returns one of ``allowed`` or ``None``.  Invalid
    answers are dropped.  Proposals must be saved as ``inferred`` labels (see
    :func:`save_suggestions`) and count only after the person confirms them.
    """

    labels = categorize(ledger)
    entries = {e["id"]: e for e in ledger.get("entries", [])}
    proposals = []
    for entry_id, choice in labels.items():
        if choice["status"] != "uncategorized" or len(proposals) >= limit:
            continue
        entry = entries[entry_id]
        allowed = sorted(INCOME_CATEGORIES if choice["kind"] == "income" else set(CATEGORIES))
        answer = suggester({k: entry.get(k) for k in ("date", "amount", "currency", "description", "kind")}, allowed)
        if isinstance(answer, str) and answer.strip().lower() in allowed:
            proposals.append({"entry_id": entry_id, "category": answer.strip().lower(), "status": "inferred"})
    return proposals


def save_suggestions(store: Any, client_id: str, proposals: Iterable[Mapping[str, Any]], model: str) -> list[dict[str, Any]]:
    return [store.label_entry(client_id, p["entry_id"], p["category"], "inferred", f"model:{model}") for p in proposals]


def remember_override(store: Any, client_id: str, entry_id: str, category: str, *, pattern: str | None = None,
                      as_rule: bool = True) -> dict[str, Any]:
    """Record the person's category for one entry and, by default, remember a rule.

    The rule matches the entry's merchant (or ``pattern``) so future lines from
    the same merchant get the same category.
    """

    category = category.strip().lower()
    if category not in CATEGORIES and category not in INCOME_CATEGORIES:
        raise ValueError(f"unknown category {category!r}; use one of {sorted({*CATEGORIES, *INCOME_CATEGORIES})}")
    label = store.label_entry(client_id, entry_id, category, "confirmed", "user")
    rule = None
    if as_rule:
        ledger = store.ledger(client_id)
        entry = next(e for e in ledger["entries"] if e["id"] == entry_id)
        key = fold(pattern) if pattern else merchant_key(entry.get("description"))
        if key:
            kind = _applies(entry, {a["id"]: a.get("type") for a in ledger["accounts"]})
            rule = store.add_category_rule(client_id, {"pattern": key, "category": category, "source": "user",
                                                       "kind": kind})
    return {"label": label, "rule": rule}


# -- aggregation ------------------------------------------------------------

def _months(start: str, end: str) -> list[str]:
    months, current = [], date.fromisoformat(start).replace(day=1)
    last = date.fromisoformat(end)
    while current <= last:
        months.append(current.strftime("%Y-%m"))
        current = (current + timedelta(days=32)).replace(day=1)
    return months


def _collect(ledger: Mapping[str, Any], start: str, end: str, currency: str, *, include_inferred: bool,
             fx_max_age_days: int) -> dict[str, Any]:
    entries, notes = active_entries(ledger, include_inferred=include_inferred)
    labels = categorize(ledger, include_inferred=include_inferred)
    fx = FxTable(ledger.get("fx", []), fx_max_age_days)
    rows, missing, debt = [], [], []
    for entry in entries:
        if not (start <= entry["date"] <= end):
            continue
        amount = _d(entry.get("amount"))
        converted = fx.convert(amount, entry.get("currency"), currency, entry["date"]) if amount is not None else None
        if entry["id"] in labels:
            if converted is None:
                missing.append({"key": f"fx.{entry['currency']}/{currency}@{entry['date']}", "reason": "missing",
                                "detail": f"Entry {entry['id']} ({entry['currency']}) cannot be converted; excluded from totals."})
            rows.append({"entry": entry, "amount": converted, **labels[entry["id"]]})
        elif entry["kind"] == "loan_payment":
            rate = fx.rate(entry["currency"], currency, entry["date"])
            if rate is None:
                missing.append({"key": f"fx.{entry['currency']}/{currency}@{entry['date']}", "reason": "missing",
                                "detail": f"Loan payment {entry['id']} cannot be converted; excluded from debt service."})
            debt.append({"entry": entry, "amount": None if rate is None else -amount * rate,
                         "interest": None if rate is None or entry.get("interest") is None else _d(entry["interest"]) * rate,
                         "principal": None if rate is None or entry.get("principal") is None else _d(entry["principal"]) * rate})
    seen, unique = set(), []
    for item in missing:
        if item["key"] not in seen:
            seen.add(item["key"])
            unique.append(item)
    return {"rows": rows, "debt": debt, "missing": unique, "notes": notes, "months": _months(start, end)}


def _essential(category: str) -> bool | None:
    return CATEGORIES.get(category, {}).get("essential")


def spending_report(ledger: Mapping[str, Any], start: str, end: str, currency: str, *,
                    include_inferred: bool = False, fx_max_age_days: int = 5) -> dict[str, Any]:
    """Monthly spending by category, essential vs discretionary, fixed vs variable."""

    data = _collect(ledger, start, end, currency, include_inferred=include_inferred, fx_max_age_days=fx_max_age_days)
    recurring_keys = {r["merchant"] for r in recurring(ledger, as_of=end, include_inferred=include_inferred)["result"]["series"]}
    months = {m: {"total": _ZERO, "by_category": defaultdict(lambda: _ZERO), "essential": _ZERO,
                  "discretionary": _ZERO, "unknown_essentiality": _ZERO, "fixed": _ZERO, "variable": _ZERO}
              for m in data["months"]}
    status_amounts: dict[str, Decimal] = defaultdict(lambda: _ZERO)
    for row in data["rows"]:
        if row["kind"] != "expense" or row["amount"] is None:
            continue
        spend = -row["amount"]  # outflows positive; refunds negative
        bucket = months[row["entry"]["date"][:7]]
        bucket["total"] += spend
        bucket["by_category"][row["category"]] += spend
        essential = _essential(row["category"])
        bucket["essential" if essential else "discretionary" if essential is False else "unknown_essentiality"] += spend
        fixed = CATEGORIES.get(row["category"], {}).get("fixed") or merchant_key(row["entry"].get("description")) in recurring_keys
        bucket["fixed" if fixed else "variable"] += spend
        status_amounts[row["status"]] += spend
    result_months = {
        m: {"total": money(b["total"]), "essential": money(b["essential"]), "discretionary": money(b["discretionary"]),
            "unknown_essentiality": money(b["unknown_essentiality"]), "fixed": money(b["fixed"]),
            "variable": money(b["variable"]),
            "by_category": {c: money(v) for c, v in sorted(b["by_category"].items())}}
        for m, b in months.items()
    }
    total = sum((b["total"] for b in months.values()), _ZERO)
    warnings = list(data["notes"])
    for status, label in (("inferred", "model-suggested (unconfirmed)"), ("uncategorized", "uncategorized")):
        if status_amounts.get(status):
            share = status_amounts[status] / total if total else None
            warnings.append(f"{money(status_amounts[status])} {currency} of spending is {label}"
                            + (f" ({out((share * 100).quantize(Decimal('0.1')))}%)" if share is not None else "")
                            + "; confirm categories to sharpen the essential/discretionary split.")
    return envelope("partial" if data["missing"] or warnings else "ready", {
        "currency": currency, "start": start, "end": end, "months": result_months,
        "total": money(total), "monthly_average": money(total / len(months)) if months else None,
        "by_status": {k: money(v) for k, v in sorted(status_amounts.items())},
    }, missing=data["missing"], warnings=warnings, assumptions=[
        "Spending is expense entries plus bank fees; transfers between own accounts, investments and loan payments are excluded.",
        "Amounts convert at the transaction-date rate; an unconvertible entry is excluded and listed as missing.",
        "Monthly averages divide by every calendar month in the window, including partial first and last months.",
    ])


def recurring(ledger: Mapping[str, Any], *, as_of: str | None = None, include_inferred: bool = False,
              min_occurrences: int = 3, amount_tolerance: str = "0.10") -> dict[str, Any]:
    """Detect recurring charges and income (subscriptions, rent, payroll).

    A series is the same merchant in the same account and currency, at least
    ``min_occurrences`` times, with a regular interval (weekly, biweekly,
    monthly, annual) and amounts within ``amount_tolerance`` of their median.
    """

    entries, _ = active_entries(ledger, include_inferred=include_inferred)
    labels = categorize(ledger, include_inferred=include_inferred)
    groups: dict[tuple, list[Mapping[str, Any]]] = defaultdict(list)
    for entry in entries:
        if entry["id"] not in labels or (as_of and entry["date"] > as_of):
            continue
        key = merchant_key(entry.get("description"))
        if key:
            groups[(labels[entry["id"]]["kind"], entry["account_id"], entry["currency"], key)].append(entry)
    cadences = (("weekly", 6, 8), ("biweekly", 13, 16), ("monthly", 26, 35), ("annual", 355, 375))
    series = []
    tolerance = Decimal(amount_tolerance)
    for (kind, account, ccy, key), items in sorted(groups.items()):
        if len(items) < min_occurrences:
            continue
        items = sorted(items, key=lambda e: e["date"])
        dates = [date.fromisoformat(e["date"]) for e in items]
        gaps = [(b - a).days for a, b in zip(dates, dates[1:])]
        typical_gap = median(gaps)
        found = next(((name, low, high) for name, low, high in cadences if low <= typical_gap <= high), None)
        if found is None or any(not (found[1] <= gap <= found[2]) for gap in gaps):
            continue
        cadence = found[0]
        amounts = [abs(Decimal(e["amount"])) for e in items]
        center = Decimal(str(median(amounts)))
        if center == 0 or any(abs(a - center) / center > tolerance for a in amounts):
            continue
        last = dates[-1]
        if cadence == "monthly":
            following = (last.replace(day=1) + timedelta(days=32)).replace(day=1)
            try:
                next_expected = following.replace(day=last.day)
            except ValueError:
                next_expected = (following + timedelta(days=32)).replace(day=1) - timedelta(days=1)
        else:
            next_expected = last + timedelta(days=round(typical_gap))
        series.append({
            "merchant": key, "kind": kind, "account_id": account, "currency": ccy, "cadence": cadence,
            "typical_amount": money(center), "occurrences": len(items), "first": items[0]["date"], "last": items[-1]["date"],
            "next_expected": next_expected.isoformat(), "category": labels[items[-1]["id"]]["category"],
            "entry_ids": [e["id"] for e in items],
        })
    return envelope("ready", {"series": series}, assumptions=[
        f"A recurring series needs {min_occurrences}+ occurrences at a regular interval with amounts within {amount_tolerance} of their median.",
    ])


def income_report(ledger: Mapping[str, Any], start: str, end: str, currency: str, *,
                  include_inferred: bool = False, fx_max_age_days: int = 5) -> dict[str, Any]:
    """Monthly income by type and how stable the regular part is."""

    data = _collect(ledger, start, end, currency, include_inferred=include_inferred, fx_max_age_days=fx_max_age_days)
    months = {m: defaultdict(lambda: _ZERO) for m in data["months"]}
    for row in data["rows"]:
        if row["kind"] != "income" or row["amount"] is None:
            continue
        months[row["entry"]["date"][:7]][row["category"]] += row["amount"]
    regular = [sum((v for c, v in b.items() if c not in IRREGULAR_INCOME), _ZERO) for b in months.values()]
    irregular = sum((v for b in months.values() for c, v in b.items() if c in IRREGULAR_INCOME), _ZERO)
    mean = sum(regular, _ZERO) / len(regular) if regular else None
    variation = None
    if regular and mean:
        variation = Decimal(str(pstdev([float(v) for v in regular]))) / mean
    months_without = sum(1 for v in regular if v == 0)
    stability = None
    if variation is not None:
        stability = "stable" if variation <= Decimal("0.10") and not months_without else \
            "variable" if variation <= Decimal("0.35") else "irregular"
    warnings = list(data["notes"])
    if len(regular) < 3:
        warnings.append("Fewer than three months of income; stability is not meaningful yet.")
    return envelope("partial" if data["missing"] or warnings else "ready", {
        "currency": currency, "start": start, "end": end,
        "months": {m: {c: money(v) for c, v in sorted(b.items())} for m, b in months.items()},
        "regular_monthly_mean": money(mean), "regular_monthly_median": money(Decimal(str(median(regular)))) if regular else None,
        "coefficient_of_variation": None if variation is None else format(variation.quantize(Decimal("0.0001")), "f"),
        "months_without_regular_income": months_without, "stability": stability,
        "irregular_total": money(irregular),
    }, missing=data["missing"], warnings=warnings, assumptions=[
        "Regular income excludes aguinaldo, PTU, bonuses and refunds, which are reported as irregular.",
        "Stability: coefficient of variation of monthly regular income (stable <= 10% with no empty month, variable <= 35%).",
    ])


def investable_surplus(ledger: Mapping[str, Any], start: str, end: str, currency: str, *,
                       plan_resources: Mapping[str, Any] | None = None, reserve_balance: Any = None,
                       reserve_account_ids: Sequence[str] | None = None, reserve_fill_months: int = 12,
                       include_inferred: bool = False, fx_max_age_days: int = 5) -> dict[str, Any]:
    """Monthly investable surplus = regular income - essential spending - debt service - reserve top-up.

    The reserve target is ``plan.resources.monthly_essentials x reserve_months``
    from memory; without it the surplus is unknown (missing), not computed with
    a zero reserve.  Uncategorized spending counts as essential.
    """

    spending = spending_report(ledger, start, end, currency, include_inferred=include_inferred,
                               fx_max_age_days=fx_max_age_days)
    income = income_report(ledger, start, end, currency, include_inferred=include_inferred,
                           fx_max_age_days=fx_max_age_days)
    data = _collect(ledger, start, end, currency, include_inferred=include_inferred, fx_max_age_days=fx_max_age_days)
    months = len(data["months"])
    missing = list({m["key"]: m for m in spending["missing"] + income["missing"] + data["missing"]}.values())
    warnings: list[str] = []
    essential_total = discretionary_total = _ZERO
    for month in spending["result"]["months"].values():
        essential_total += Decimal(month["essential"]) + Decimal(month["unknown_essentiality"])
        discretionary_total += Decimal(month["discretionary"])
    essential = essential_total / months
    discretionary = discretionary_total / months
    regular_income = Decimal(income["result"]["regular_monthly_mean"]) if income["result"]["regular_monthly_mean"] else _ZERO
    debt_known = [d for d in data["debt"] if d["amount"] is not None]
    debt = sum((d["amount"] for d in debt_known), _ZERO) / months
    interest_unknown = [d for d in debt_known if d["interest"] is None]
    interest = sum((d["interest"] if d["interest"] is not None else d["amount"] for d in debt_known), _ZERO)
    if interest_unknown:
        warnings.append(f"{len(interest_unknown)} loan payment(s) have no principal/interest split; the whole payment is "
                        "treated as cost in the savings rate.")
    income_total = sum((Decimal(v) for m in income["result"]["months"].values() for v in m.values()), _ZERO)
    spend_total = Decimal(spending["result"]["total"])
    savings_rate = None if income_total <= 0 else (income_total - spend_total - interest) / income_total
    cash_savings_rate = None if income_total <= 0 else (income_total - spend_total - debt * months) / income_total

    target = current = top_up = None
    if plan_resources is None:
        missing.append({"key": "plan.resources", "reason": "missing",
                        "detail": "Reserve target unknown: remember plan.resources (monthly_essentials, reserve_months)."})
    else:
        resources_ccy = plan_resources.get("currency")
        try:
            monthly = Decimal(str(plan_resources["monthly_essentials"]))
            reserve_months = Decimal(str(plan_resources["reserve_months"]))
        except (KeyError, ArithmeticError, ValueError):
            monthly = reserve_months = None
        if monthly is None or reserve_months is None:
            missing.append({"key": "plan.resources.monthly_essentials", "reason": "missing",
                            "detail": "plan.resources needs monthly_essentials and reserve_months for the reserve target."})
        elif resources_ccy != currency:
            missing.append({"key": "plan.resources.currency", "reason": "mismatch",
                            "detail": f"plan.resources is in {resources_ccy}; request the surplus in {resources_ccy}."})
        else:
            target = monthly * reserve_months
    if reserve_balance is not None:
        current = Decimal(str(reserve_balance))
    elif reserve_account_ids:
        state, _ = replay(ledger, end, include_inferred=include_inferred)
        fx = FxTable(ledger.get("fx", []), fx_max_age_days)
        current = _ZERO
        for (account, ccy), value in state.cash.items():
            if account in reserve_account_ids:
                converted = fx.convert(value, ccy, currency, end)
                if converted is None:
                    current = None
                    missing.append({"key": f"fx.{ccy}/{currency}@{end}", "reason": "missing",
                                    "detail": f"Cannot value reserve cash in {account}."})
                    break
                current += converted
    elif target is not None:
        missing.append({"key": "reserve_balance", "reason": "missing",
                        "detail": "How much is already set aside as the emergency reserve? Supply reserve_balance or reserve_account_ids."})
    if target is not None and current is not None:
        top_up = max(_ZERO, target - current) / Decimal(reserve_fill_months)
    before_reserve = regular_income - essential - debt
    surplus = None if top_up is None or any(m["reason"] == "missing" and m["key"].startswith("fx.") for m in missing) \
        else before_reserve - top_up
    if months < 3:
        warnings.append("Fewer than three months of history; the surplus is a rough indication.")
    if surplus is not None and surplus <= 0:
        warnings.append("No investable surplus at current spending and reserve needs.")
    status = "needs_input" if surplus is None and any(m["key"] in {"plan.resources", "reserve_balance"} for m in missing) \
        else ("partial" if missing or warnings or spending["warnings"] else "ready")
    ratio = lambda v: None if v is None else format(v.quantize(Decimal("0.0001")), "f")  # noqa: E731
    return envelope(status, {
        "currency": currency, "start": start, "end": end, "months": months,
        "monthly": {
            "regular_income": money(regular_income), "essential_spending": money(essential),
            "discretionary_spending": money(discretionary), "debt_service": money(debt),
            "reserve_top_up": money(top_up), "surplus_before_reserve": money(before_reserve),
            "investable_surplus": money(surplus),
            "after_discretionary": money(None if surplus is None else surplus - discretionary),
        },
        "reserve": {"target": money(target), "current": money(current), "fill_months": reserve_fill_months},
        "irregular_income_in_window": income["result"]["irregular_total"],
        "savings_rate": ratio(savings_rate), "cash_savings_rate": ratio(cash_savings_rate),
    }, missing=missing, warnings=warnings + spending["warnings"] + income["warnings"], assumptions=[
        "Investable surplus uses average regular income and essential spending over the window; irregular income (aguinaldo, PTU, bonuses) is excluded and shown separately.",
        "Uncategorized, cash-withdrawal and unknown-essentiality spending counts as essential.",
        f"The reserve gap is spread over {reserve_fill_months} months.",
        "Savings rate = (income - spending - loan interest) / income; cash savings rate also subtracts loan principal.",
        "This is a description of past cash flow, not advice to invest any amount.",
    ])


def run(task: str, inputs: dict[str, Any], context: dict[str, Any] | None = None) -> dict[str, Any]:
    """``spending`` task: view=monthly|recurring|income|surplus|categories."""

    context = context or {}
    if task != "spending":
        raise ValueError("task must be spending")
    ledger = inputs.get("ledger", context.get("ledger"))
    if ledger is None:
        return envelope("needs_input", {}, missing=[{"key": "ledger", "reason": "missing",
                        "detail": "No ledger is stored for this client; post statements first."}])
    view = inputs.get("view", "monthly")
    include_inferred = bool(inputs.get("include_inferred", False))
    if view == "recurring":
        return recurring(ledger, as_of=inputs.get("as_of"), include_inferred=include_inferred)
    if view == "categories":
        labels = categorize(ledger, include_inferred=include_inferred)
        counts: dict[str, int] = defaultdict(int)
        for label in labels.values():
            counts[label["status"]] += 1
        return envelope("ready", {"labels": labels, "counts": dict(counts),
                                  "categories": sorted(CATEGORIES), "income_categories": sorted(INCOME_CATEGORIES)})
    missing = [{"key": k, "reason": "missing", "detail": f"spending needs {k}."} for k in ("start", "end", "currency") if k not in inputs]
    if missing:
        return envelope("needs_input", {}, missing=missing)
    args = (ledger, inputs["start"], inputs["end"], inputs["currency"])
    kwargs = {"include_inferred": include_inferred, "fx_max_age_days": int(inputs.get("max_fx_age_days", 5))}
    if view == "monthly":
        return spending_report(*args, **kwargs)
    if view == "income":
        return income_report(*args, **kwargs)
    if view == "surplus":
        return investable_surplus(*args, plan_resources=inputs.get("plan.resources", context.get("plan.resources")),
                                  reserve_balance=inputs.get("reserve_balance"),
                                  reserve_account_ids=inputs.get("reserve_account_ids"),
                                  reserve_fill_months=int(inputs.get("reserve_fill_months", 12)), **kwargs)
    raise ValueError("view must be monthly, recurring, income, surplus, or categories")
