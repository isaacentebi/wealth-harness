"""Build one deterministic picture of the person from facts and the ledger.

``build(snapshot, ledger, today)`` is the only reader of the canonical model.
The brief, the profile page, the plan/calendar tasks and statement insights
all start from its result.  Rules (documented once, applied everywhere):

* Only eligible facts drive numbers: not inferred, not past review, not null.
  Stale and inferred keys are listed, never silently used.
* Canonical keys win per section.  A legacy shape (``plan.resources`` lists,
  ``income.schedule.items``, profile-form amounts, ``household``) is read only
  when no canonical fact exists for that section, and is marked ``legacy``.
* Statements win over stated balances for the same institution; the stated
  figure is kept in ``differences``, never overwritten or added twice.  A
  stated balance without an institution counts only when no statement exists.
* Spending comes from the ledger when it covers at least two full calendar
  months of bank activity; otherwise from what the person stated.
* Annual and one-off income is listed as ``extras`` and kept out of the
  monthly surplus.
* Conversion uses stored rates only (ledger, statement and household FX).  An
  amount without a rate stays in ``unconverted``; unknown is never zero.
"""
from __future__ import annotations

import math
import re
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Iterable, Mapping

from .schema import country_code

# ------------------------------------------------------------------ constants

SPENDING_LEDGER_MIN_MONTHS = 2
CONCENTRATION_SHARE = Decimal("0.25")
CASH_DRAG_SHARE = Decimal("0.10")
_FREQ_PER_MONTH = {"monthly": Decimal(1), "biweekly": Decimal(26) / Decimal(12), "annual": Decimal(1) / Decimal(12)}
_CADENCE_PER_MONTH = {"weekly": Decimal(52) / Decimal(12), "biweekly": Decimal(26) / Decimal(12),
                      "monthly": Decimal(1), "quarterly": Decimal(1) / Decimal(3)}
_COUNTRY_CURRENCY = {"MX": "MXN", "US": "USD", "CA": "CAD", "ES": "EUR"}
_ILLIQUID_TYPES = {"retirement", "afore", "pension", "ira", "401k", "real_estate", "ppr"}
_LIABILITY_ALIASES = {"car": "auto", "auto": "auto", "vehicle": "auto", "coche": "auto", "carro": "auto",
                      "mortgage": "mortgage", "hipoteca": "mortgage", "card": "card", "credit_card": "card",
                      "tarjeta": "card", "personal": "personal", "student": "student", "other": "other"}
# Funds and listings that track the same index, keyed by symbol (upper case, series dropped).
UNDERLYING = {
    "IVV": "S&P 500", "CSPX": "S&P 500", "VOO": "S&P 500", "SPY": "S&P 500", "SPLG": "S&P 500",
    "VUSA": "S&P 500", "VUAA": "S&P 500", "SXR8": "S&P 500", "IVVPESO": "S&P 500",
    "VTI": "US total market", "ITOT": "US total market", "QQQ": "Nasdaq-100", "QQQM": "Nasdaq-100",
    "CNDX": "Nasdaq-100", "EQQQ": "Nasdaq-100", "NAFTRAC": "S&P/BMV IPC", "VT": "Global equity",
    "VWRA": "Global equity", "ACWI": "Global equity", "IWDA": "Developed markets", "SWDA": "Developed markets",
    "BND": "US aggregate bonds", "AGG": "US aggregate bonds",
}


# ------------------------------------------------------------------ numbers


def D(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        return value if value.is_finite() else None
    if isinstance(value, (int, float)):
        return Decimal(str(value)) if math.isfinite(value) else None
    if isinstance(value, str):
        try:
            parsed = Decimal(value.strip().replace(",", ""))
        except InvalidOperation:
            return None
        return parsed if parsed.is_finite() else None
    return None


def num(value: Decimal | None, places: int = 2) -> int | float | None:
    """JSON number: an int when whole, else rounded to ``places``."""
    if value is None:
        return None
    rounded = value.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)
    return int(rounded) if rounded == rounded.to_integral_value() else float(rounded)


def _money(value: Decimal | None, currency: str | None) -> dict | None:
    return None if value is None else {"amount": num(value), "currency": currency}


def _as_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str) and len(value) >= 10:
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return None
    return None


def humanize(text: str) -> str:
    words = re.sub(r"[._-]+", " ", str(text)).strip()
    return words[:1].upper() + words[1:] if words else str(text)


def add_months(day: date, months: int) -> date:
    total = day.year * 12 + day.month - 1 + months
    year, month = divmod(total, 12)
    last = [31, 29 if year % 4 == 0 and (year % 100 or year % 400 == 0) else 28, 31, 30, 31, 30,
            31, 31, 30, 31, 30, 31][month]
    return date(year, month + 1, min(day.day, last))


# ------------------------------------------------------------------ facts


class _Facts:
    """Current facts split into eligible (drive numbers) and the rest (listed)."""

    def __init__(self, snapshot: Mapping[str, Any], today: date):
        self.today = today
        self.all: dict[str, dict] = {}
        for fact in snapshot.get("facts") or []:
            if isinstance(fact, dict) and isinstance(fact.get("key"), str):
                self.all[fact["key"]] = fact
        self.stale = sorted(k for k, f in self.all.items() if f.get("value") is not None and self._stale(f))
        self.inferred = sorted(k for k, f in self.all.items() if f.get("value") is not None
                               and f.get("confidence") == "inferred" and k not in self.stale)
        self.used: dict[str, str] = {}  # key -> fact id, for evidence

    def _stale(self, fact: Mapping[str, Any]) -> bool:
        expires = _as_date(fact.get("expires_on"))
        return bool(expires and expires < self.today)

    def eligible(self, key: str) -> bool:
        fact = self.all.get(key)
        return bool(fact) and fact.get("value") is not None and key not in self.stale and key not in self.inferred

    def value(self, key: str, *, use: bool = True) -> Any:
        if not self.eligible(key):
            return None
        if use:
            self.used[key] = self.all[key].get("id")
        return self.all[key]["value"]

    def any_value(self, key: str) -> Any:
        """Descriptive value even when stale or inferred (for profile fields and listing)."""
        fact = self.all.get(key)
        return fact.get("value") if fact else None

    def keys(self, prefix: str) -> list[str]:
        return sorted(k for k in self.all if k.startswith(prefix) and k.count(".") == prefix.count("."))

    def meta(self, key: str) -> dict:
        fact = self.all.get(key) or {}
        source = fact.get("source") if isinstance(fact.get("source"), dict) else {}
        observed = _as_date(source.get("observed_on"))
        return {"key": key, "source": source.get("kind"), "observed_on": source.get("observed_on"),
                "age_days": (self.today - observed).days if observed else None,
                "stale": key in self.stale, "inferred": key in self.inferred, "revision": fact.get("revision")}


class _FX:
    """Stored rates only; the latest on or before today wins."""

    def __init__(self, today: date):
        self.today = today
        self.rates: dict[tuple[str, str], tuple[str, Decimal, str]] = {}
        self.used: dict[tuple[str, str], tuple[str, Decimal, str]] = {}

    def add(self, base: Any, quote: Any, rate: Any, on: Any, source: str) -> None:
        value, when = D(rate), _as_date(on)
        if not (isinstance(base, str) and isinstance(quote, str)) or value is None or value <= 0:
            return
        day = (when or self.today).isoformat()
        if day > self.today.isoformat():
            return
        current = self.rates.get((base, quote))
        if current is None or day > current[0]:
            self.rates[(base, quote)] = (day, value, source)

    def rate(self, base: str, quote: str) -> Decimal | None:
        if base == quote:
            return Decimal(1)
        if (base, quote) in self.rates:
            self.used[(base, quote)] = self.rates[(base, quote)]
            return self.rates[(base, quote)][1]
        if (quote, base) in self.rates:
            self.used[(quote, base)] = self.rates[(quote, base)]
            return Decimal(1) / self.rates[(quote, base)][1]
        return None

    def convert(self, amount: Decimal | None, currency: str | None, to: str | None) -> Decimal | None:
        if amount is None or not currency or not to:
            return None
        rate = self.rate(currency, to)
        return None if rate is None else amount * rate

    def listing(self) -> list[dict]:
        return [{"pair": f"{b}/{q}", "rate": num(r, 6), "date": d, "source": s}
                for (b, q), (d, r, s) in sorted(self.used.items())]


# ------------------------------------------------------------------ sections


def _profile(facts: _Facts) -> dict:
    raw = facts.any_value("client.profile")
    raw = raw if isinstance(raw, dict) else {}
    residence = raw.get("residence") if isinstance(raw.get("residence"), dict) else (
        {"country": raw["residence"]} if isinstance(raw.get("residence"), str) else {})
    country = country_code(residence.get("country")) or country_code(raw.get("country"))
    tax = raw.get("tax_residence", raw.get("tax_residences"))
    tax_list = [c for c in (country_code(t) for t in (tax if isinstance(tax, list) else [tax] if tax else [])) if c]
    risk = facts.any_value("preference.risk")
    onboarding = facts.any_value("onboarding")
    return {
        "name": raw.get("name") if isinstance(raw.get("name"), str) else None,
        "birth_year": raw.get("birth_year") if isinstance(raw.get("birth_year"), int) else None,
        "residence": {"country": country, "region": residence.get("region"), "city": residence.get("city")}
        if (country or residence.get("city")) else None,
        "tax_residence": tax_list or None,
        "tax_residence_assumed": None if tax_list else country,
        "citizenship": raw.get("citizenship") if isinstance(raw.get("citizenship"), list) else None,
        "us_person": raw.get("us_person") if isinstance(raw.get("us_person"), bool) else None,
        "dependents": raw.get("dependents") if isinstance(raw.get("dependents"), int) else None,
        "dependent_ages": raw.get("dependent_ages") if isinstance(raw.get("dependent_ages"), list) else None,
        "currencies": raw.get("currencies") if isinstance(raw.get("currencies"), list) else None,
        "language": raw.get("language") if raw.get("language") in ("es", "en") else None,
        "timezone": raw.get("timezone") if isinstance(raw.get("timezone"), str) else None,
        "risk": risk if isinstance(risk, dict) else ({"label": risk} if isinstance(risk, str) else None),
        "onboarding": onboarding if isinstance(onboarding, dict) else None,
        "key": "client.profile" if "client.profile" in facts.all else None,
    }


def _reporting_currency(facts: _Facts, profile: dict, seen: Iterable[str]) -> tuple[str | None, str]:
    raw = facts.any_value("client.profile")
    raw = raw if isinstance(raw, dict) else {}
    if isinstance(raw.get("reporting_currency"), str):
        return raw["reporting_currency"], "stated"
    if profile.get("currencies"):
        return profile["currencies"][0], "first stated currency"
    country = (profile.get("residence") or {}).get("country")
    if country in _COUNTRY_CURRENCY:
        return _COUNTRY_CURRENCY[country], "currency of the country of residence"
    counts: dict[str, int] = {}
    for currency in seen:
        if currency:
            counts[currency] = counts.get(currency, 0) + 1
    if counts:
        return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[0][0], "most common currency in the facts"
    return None, "unknown"


def _plan_resources(facts: _Facts) -> dict:
    value = facts.value("plan.resources", use=False)
    return value if isinstance(value, dict) else {}


def _profile_amount(facts: _Facts, name: str) -> dict | None:
    raw = facts.value("client.profile", use=False)
    item = raw.get(name) if isinstance(raw, dict) else None
    if isinstance(item, dict) and D(item.get("amount")) is not None and isinstance(item.get("currency"), str):
        return item
    return None


def _income(facts: _Facts) -> tuple[list[dict], str | None]:
    items = []
    for key in facts.keys("income."):
        value = facts.value(key)
        if key == "income.schedule" or not isinstance(value, dict) or "income" in value:
            continue
        items.append({**value, "id": key.split(".", 1)[1], "key": key})
    if items:
        return items, None
    schedule = facts.value("income.schedule", use=False)
    if isinstance(schedule, dict) and isinstance(schedule.get("items"), list):
        facts.used["income.schedule"] = facts.all["income.schedule"].get("id")
        for index, item in enumerate(schedule["items"]):
            if not isinstance(item, dict) or D(item.get("amount")) is None:
                continue
            items.append({**item, "currency": item.get("currency") or schedule.get("currency"),
                          "frequency": item.get("frequency") or "monthly",
                          "id": str(item.get("id") or f"item{index}"), "key": "income.schedule", "legacy": True})
        if items:
            return items, "income.schedule"
    stated = _profile_amount(facts, "monthly_income")
    if stated:
        facts.used["client.profile"] = facts.all["client.profile"].get("id")
        return [{"amount": stated["amount"], "currency": stated["currency"],
                 "frequency": "annual" if stated.get("period") == "year" else "monthly",
                 "id": "monthly_income", "key": "client.profile", "legacy": True}], "client.profile"
    return [], None


def _income_view(items: list[dict], fx: _FX, currency: str | None) -> dict:
    rows, extras, monthly, unconverted = [], [], Decimal(0), []
    for item in items:
        amount, freq = D(item.get("amount")), item.get("frequency")
        per_month = amount * _FREQ_PER_MONTH[freq] if amount is not None and freq in ("monthly", "biweekly") else None
        row = {"id": item["id"], "key": item["key"], "name": item.get("name"), "kind": item.get("kind"),
               "amount": num(amount), "currency": item.get("currency"), "frequency": freq,
               "net": item.get("net") if isinstance(item.get("net"), bool) else None,
               "month": item.get("month"), "approximate": bool(item.get("approximate")),
               "monthly": num(per_month), "legacy": bool(item.get("legacy"))}
        (rows if per_month is not None else extras).append(row)
        if per_month is not None:
            converted = fx.convert(per_month, item.get("currency"), currency)
            if converted is None:
                unconverted.append(_money(per_month, item.get("currency")))
            else:
                monthly += converted
    known = bool(rows) and not unconverted
    return {"items": rows, "extras": extras, "monthly": num(monthly) if known else None,
            "currency": currency, "unconverted": unconverted,
            "net": all(r["net"] is True for r in rows) if rows else None,
            "approximate": any(r["approximate"] for r in rows)}


def _ledger_spending(ledger: Mapping[str, Any] | None, currency: str | None, today: date) -> dict | None:
    """Average monthly spending from the ledger when it covers two full months of bank activity."""
    if not ledger or not currency or not ledger.get("entries"):
        return None
    brokerage = {a.get("id") for a in ledger.get("accounts") or [] if a.get("type") in {"brokerage", "retirement"}}
    days = sorted(e["date"] for e in ledger["entries"] if e.get("kind") in {"expense", "income", "fee", "opening_balance"}
                  and e.get("account_id") not in brokerage and isinstance(e.get("date"), str))
    if not days:
        return None
    first, last = date.fromisoformat(days[0]), min(date.fromisoformat(days[-1]), today)
    start = first if first.day == 1 else add_months(first.replace(day=1), 1)
    end_exclusive = (last + timedelta(days=1)).replace(day=1) if (last + timedelta(days=1)).day == 1 else last.replace(day=1)
    months = (end_exclusive.year - start.year) * 12 + end_exclusive.month - start.month
    if months < SPENDING_LEDGER_MIN_MONTHS:
        return None
    try:
        from ..cashflow import spending_report
        report = spending_report(ledger, start.isoformat(), (end_exclusive - timedelta(days=1)).isoformat(), currency)
    except Exception:  # an unreadable ledger falls back to stated spending
        return None
    result = report.get("result") or {}
    total, month_rows = D(result.get("total")), result.get("months") or {}
    if total is None or not month_rows:
        return None
    essential = sum((D(m.get("essential")) or Decimal(0) for m in month_rows.values()), Decimal(0)) / len(month_rows)
    discretionary = sum((D(m.get("discretionary")) or Decimal(0) for m in month_rows.values()), Decimal(0)) / len(month_rows)
    unknown = sum((D(m.get("unknown_essentiality")) or Decimal(0) for m in month_rows.values()), Decimal(0))
    return {"total": total / len(month_rows), "essential": essential if not unknown else None,
            "discretionary": discretionary if not unknown else None, "currency": currency,
            "months": len(month_rows), "start": start.isoformat(),
            "end": (end_exclusive - timedelta(days=1)).isoformat(), "partial": report.get("status") != "ready"}


def _spending(facts: _Facts, ledger: Mapping[str, Any] | None, fx: _FX, currency: str | None, today: date) -> dict:
    from_ledger = _ledger_spending(ledger, currency, today)
    stated, key, legacy = None, None, False
    value = facts.value("spending.monthly")
    if isinstance(value, dict):
        stated, key = value, "spending.monthly"
    elif _profile_amount(facts, "monthly_spending"):
        item = _profile_amount(facts, "monthly_spending")
        stated, key, legacy = {"total": item["amount"], "currency": item["currency"]}, "client.profile", True
        facts.used["client.profile"] = facts.all["client.profile"].get("id")
    elif _plan_resources(facts).get("monthly_essentials") is not None:
        resources = _plan_resources(facts)
        stated, key, legacy = {"essential": resources["monthly_essentials"], "currency": resources.get("currency")}, \
            "plan.resources", True
        facts.used["plan.resources"] = facts.all["plan.resources"].get("id")
    view = {"source": None, "key": key, "currency": currency, "total": None, "essential": None,
            "discretionary": None, "approximate": False, "legacy": legacy, "stated": None, "ledger_months": None}
    if stated:
        cur = stated.get("currency")
        conv = {n: fx.convert(D(stated.get(n)), cur, currency) for n in ("total", "essential", "discretionary")}
        if conv["total"] is None and conv["essential"] is not None and conv["discretionary"] is not None:
            conv["total"] = conv["essential"] + conv["discretionary"]
        view["stated"] = {n: num(D(stated.get(n))) for n in ("total", "essential", "discretionary")} | {"currency": cur}
        view.update(source="stated", approximate=bool(stated.get("approximate")),
                    total=num(conv["total"]), essential=num(conv["essential"]), discretionary=num(conv["discretionary"]))
    if from_ledger:
        view.update(source="ledger", total=num(from_ledger["total"]), essential=num(from_ledger["essential"]),
                    discretionary=num(from_ledger["discretionary"]), approximate=False,
                    ledger_months=from_ledger["months"], ledger_period=[from_ledger["start"], from_ledger["end"]])
    # The amount used for the monthly flow and for reserve months.
    view["monthly"] = view["total"] if view["total"] is not None else view["essential"]
    view["monthly_basis"] = "total" if view["total"] is not None else "essential" if view["essential"] is not None else None
    view["essential_for_reserve"] = view["essential"] if view["essential"] is not None else view["total"]
    return view


def _covered(institution: str | None, statements: set[str]) -> bool:
    return bool(institution) and institution.strip().lower() in statements


def _cash(facts: _Facts) -> tuple[list[dict], str | None]:
    items = []
    for key in facts.keys("cash."):
        value = facts.value(key)
        if isinstance(value, dict):
            items.append({**value, "id": key.split(".", 1)[1], "key": key})
    if items:
        return items, None
    resources = _plan_resources(facts)
    if isinstance(resources.get("cash"), list):
        for index, item in enumerate(resources["cash"]):
            if isinstance(item, dict) and D(item.get("amount", item.get("balance"))) is not None:
                items.append({**item, "amount": item.get("amount", item.get("balance")),
                              "currency": item.get("currency") or resources.get("currency"),
                              "id": str(item.get("id") or f"cash{index}"), "key": "plan.resources", "legacy": True})
        if items:
            facts.used["plan.resources"] = facts.all["plan.resources"].get("id")
            return items, "plan.resources"
    stated = _profile_amount(facts, "savings")
    if stated:
        facts.used["client.profile"] = facts.all["client.profile"].get("id")
        return [{"amount": stated["amount"], "currency": stated["currency"], "id": "savings",
                 "key": "client.profile", "legacy": True}], "client.profile"
    return [], None


def _liability_kind(value: Any, name: Any = None) -> str:
    for text in (value, name):
        if isinstance(text, str):
            lowered = text.lower()
            for alias, kind in _LIABILITY_ALIASES.items():
                if alias in lowered:
                    return kind
    return "other"


def _liabilities(facts: _Facts, statement_accounts: list[dict]) -> tuple[list[dict], str | None]:
    items, legacy_from = [], None
    for key in facts.keys("liability."):
        value = facts.value(key)
        if not isinstance(value, dict):
            continue
        if isinstance(value.get("liability"), dict):  # statement record
            raw = value["liability"]
            items.append({"id": key.split(".", 1)[1], "key": key, "kind": _liability_kind(None, raw.get("name")),
                          "name": raw.get("name"), "balance": raw.get("value"), "currency": raw.get("currency"),
                          "payment": raw.get("monthly_payment"),
                          "payment_frequency": "monthly" if raw.get("monthly_payment") is not None else None,
                          "annual_rate": raw.get("interest_rate"), "source": "statement", "as_of": value.get("as_of")})
        else:
            items.append({**value, "id": key.split(".", 1)[1], "key": key, "source": "stated"})
    for account in statement_accounts:
        for raw in account.get("liabilities") or []:
            items.append({"id": raw.get("id"), "key": account["key"], "kind": _liability_kind(account.get("type"), raw.get("name")),
                          "name": raw.get("name"), "balance": raw.get("value"), "currency": raw.get("currency"),
                          "payment": raw.get("monthly_payment"),
                          "payment_frequency": "monthly" if raw.get("monthly_payment") is not None else None,
                          "annual_rate": raw.get("interest_rate"), "lender": account.get("institution"),
                          "source": "statement", "as_of": account.get("as_of")})
    household = facts.value("household", use=False)
    if isinstance(household, dict):
        for raw in household.get("liabilities") or []:
            if isinstance(raw, dict) and D(raw.get("value")) is not None:
                facts.used["household"] = facts.all["household"].get("id")
                items.append({"id": raw.get("id") or "debt", "key": "household",
                              "kind": _liability_kind(raw.get("kind") or raw.get("type") or raw.get("id"), raw.get("name")),
                              "name": raw.get("name"), "balance": raw.get("value"), "currency": raw.get("currency"),
                              "payment": raw.get("monthly_payment"),
                              "payment_frequency": "monthly" if raw.get("monthly_payment") is not None else None,
                              "annual_rate": raw.get("interest_rate"), "source": "household", "as_of": household.get("as_of")})
    if any(i["source"] == "stated" for i in items):
        return items, None
    resources = _plan_resources(facts)
    if isinstance(resources.get("debts"), list):
        for index, raw in enumerate(resources["debts"]):
            if isinstance(raw, dict) and D(raw.get("balance", raw.get("amount"))) is not None:
                items.append({**raw, "kind": _liability_kind(raw.get("kind") or raw.get("type"), raw.get("name")),
                              "balance": raw.get("balance", raw.get("amount")),
                              "currency": raw.get("currency") or resources.get("currency"),
                              "id": str(raw.get("id") or f"debt{index}"), "key": "plan.resources",
                              "source": "stated", "legacy": True})
                legacy_from = "plan.resources"
        if legacy_from:
            facts.used["plan.resources"] = facts.all["plan.resources"].get("id")
    elif _profile_amount(facts, "debts"):
        stated = _profile_amount(facts, "debts")
        facts.used["client.profile"] = facts.all["client.profile"].get("id")
        items.append({"id": "debts", "key": "client.profile", "kind": "other", "balance": stated["amount"],
                      "currency": stated["currency"], "source": "stated", "legacy": True})
        legacy_from = "client.profile"
    return items, legacy_from


def payoff(balance: Decimal | None, annual_rate: Decimal | None, monthly_payment: Decimal | None,
           today: date, *, max_months: int = 600) -> dict:
    """Months, date and interest to repay ``balance`` at a fixed monthly payment."""
    if balance is None or annual_rate is None or monthly_payment is None:
        return {"status": "unknown"}
    if balance <= 0:
        return {"status": "paid", "months": 0, "date": today.isoformat(), "interest": 0}
    rate = annual_rate / 12
    if monthly_payment <= balance * rate:
        return {"status": "never", "detail": "the payment does not cover the monthly interest"}
    remaining, interest, months = balance, Decimal(0), 0
    while remaining > 0 and months < max_months:
        charge = remaining * rate
        interest += charge
        remaining = remaining + charge - monthly_payment
        months += 1
    if remaining > 0:
        return {"status": "never", "detail": f"not repaid within {max_months} months"}
    interest += remaining  # the last payment is smaller
    return {"status": "ready", "months": months, "date": add_months(today, months).isoformat()[:7],
            "interest": num(interest)}


def annuity_payment(balance: Decimal, annual_rate: Decimal, months: int) -> Decimal:
    if months <= 0:
        return balance
    rate = annual_rate / 12
    if rate == 0:
        return balance / months
    factor = (1 + rate) ** months
    return balance * rate * factor / (factor - 1)


def _liability_view(item: dict, fx: _FX, currency: str | None, today: date) -> dict:
    balance, rate = D(item.get("balance")), D(item.get("annual_rate"))
    payment, freq = D(item.get("payment")), item.get("payment_frequency")
    monthly = payment * _FREQ_PER_MONTH[freq] if payment is not None and freq in _FREQ_PER_MONTH else None
    term = item.get("remaining_term_months")
    maturity = _as_date(item.get("maturity"))
    if term is None and maturity and maturity > today:
        term = (maturity.year - today.year) * 12 + maturity.month - today.month
    payment_basis = "stated" if monthly is not None else None
    if monthly is None and balance is not None and rate is not None and isinstance(term, int) and term > 0:
        monthly, payment_basis = annuity_payment(balance, rate, term), "from remaining term"
    missing = []
    if rate is None:
        missing.append("annual_rate")
    if monthly is None:
        missing.append("payment or remaining_term_months")
    plan = payoff(balance, rate, monthly, today) if not missing else {"status": "unknown"}
    converted = fx.convert(balance, item.get("currency"), currency)
    return {
        "id": item["id"], "key": item["key"], "kind": item.get("kind") or "other", "name": item.get("name"),
        "lender": item.get("lender"), "balance": num(balance), "currency": item.get("currency"),
        "value": num(converted), "annual_rate": num(rate, 6), "monthly_payment": num(monthly),
        "payment_basis": payment_basis, "in_spending": bool(item.get("in_spending")),
        "payoff": plan, "missing": missing, "source": item.get("source", "stated"),
        "approximate": bool(item.get("approximate")), "legacy": bool(item.get("legacy")),
    }


def _account_label(account: dict, duplicates: set[tuple]) -> str:
    institution = account.get("institution") or humanize(account.get("id") or "account")
    kind = account.get("type")
    label = f"{institution} · {kind}" if kind else institution
    if (institution, kind) in duplicates and account.get("currency"):
        label += f" ({account['currency']})"
    return label


def _statement_accounts(facts: _Facts, ledger: Mapping[str, Any] | None) -> list[dict]:
    accounts = []
    for key in facts.keys("account."):
        fact = facts.all[key]
        value = fact.get("value")
        if not isinstance(value, dict) or not isinstance(value.get("account"), dict):
            continue
        account = value["account"]
        native: dict[str, Decimal] = {}
        positions = []
        for position in value.get("positions") or []:
            amount = D(position.get("value"))
            if amount is None or not position.get("currency"):
                continue
            native[position["currency"]] = native.get(position["currency"], Decimal(0)) + amount
            positions.append(position)
        accounts.append({"key": key, "id": account.get("id") or key.split(".", 1)[1],
                         "institution": account.get("institution"), "type": account.get("type"),
                         "currency": account.get("currency") or value.get("currency"), "as_of": value.get("as_of"),
                         "native": native, "positions": positions, "liabilities": value.get("liabilities") or [],
                         "fx": value.get("fx") or [], "eligible": facts.eligible(key), "source": "statement"})
        if facts.eligible(key):
            facts.used[key] = fact.get("id")
    household = facts.value("household", use=False)
    if isinstance(household, dict):
        covered = {a["id"] for a in accounts}
        for account in household.get("accounts") or []:
            if not isinstance(account, dict) or account.get("id") in covered:
                continue
            native: dict[str, Decimal] = {}
            positions = [p for p in household.get("positions") or []
                         if isinstance(p, dict) and p.get("account_id") == account.get("id")]
            for position in positions:
                amount = D(position.get("value"))
                if amount is not None and position.get("currency"):
                    native[position["currency"]] = native.get(position["currency"], Decimal(0)) + amount
            accounts.append({"key": "household", "id": account.get("id"), "institution": account.get("institution")
                             or account.get("name"), "type": account.get("type"), "currency": account.get("currency"),
                             "as_of": household.get("as_of"), "native": native, "positions": positions,
                             "liabilities": [], "fx": household.get("fx") or [], "eligible": True, "source": "household"})
            facts.used["household"] = facts.all["household"].get("id")
    if ledger:
        covered = {a["id"] for a in accounts}
        entries = {e.get("account_id") for e in ledger.get("entries") or []}
        for account in ledger.get("accounts") or []:
            if account.get("id") in covered or account.get("id") not in entries:
                continue
            accounts.append({"key": None, "id": account.get("id"), "institution": account.get("institution"),
                             "type": account.get("type"), "currency": account.get("currency"), "as_of": None,
                             "native": None, "positions": [], "liabilities": [], "fx": [], "eligible": True,
                             "source": "ledger"})
    return accounts


def _stated_investments(facts: _Facts) -> list[dict]:
    items = []
    for key in facts.keys("investment."):
        value = facts.value(key)
        if isinstance(value, dict):
            items.append({**value, "id": key.split(".", 1)[1], "key": key})
    if items:
        return items
    resources = _plan_resources(facts)
    if isinstance(resources.get("investments"), list):
        for index, raw in enumerate(resources["investments"]):
            if isinstance(raw, dict) and D(raw.get("amount", raw.get("balance"))) is not None:
                items.append({**raw, "amount": raw.get("amount", raw.get("balance")),
                              "currency": raw.get("currency") or resources.get("currency"),
                              "id": str(raw.get("id") or f"investment{index}"), "key": "plan.resources", "legacy": True})
        if items:
            facts.used["plan.resources"] = facts.all["plan.resources"].get("id")
            return items
    stated = _profile_amount(facts, "investments")
    if stated:
        facts.used["client.profile"] = facts.all["client.profile"].get("id")
        return [{"amount": stated["amount"], "currency": stated["currency"], "id": "investments",
                 "key": "client.profile", "legacy": True}]
    return []


def underlying_of(symbol: Any) -> str | None:
    if not isinstance(symbol, str):
        return None
    head = symbol.upper().split(":")[-1].split()[0].split(".")[0]
    return UNDERLYING.get(head)


def _holdings(accounts: list[dict], fx: _FX, currency: str | None) -> dict:
    rows = []
    for account in accounts:
        if not account["eligible"] or account["source"] == "ledger":
            continue
        for position in account["positions"]:
            value = fx.convert(D(position.get("value")), position.get("currency"), currency)
            symbol = position.get("symbol") or position.get("instrument_id")
            asset_class = position.get("asset_class") or ("cash" if str(position.get("instrument_id", "")).startswith("CASH:") else None)
            rows.append({"account": account["id"], "symbol": symbol, "value": value,
                         "underlying": underlying_of(position.get("underlying_symbol") or symbol) or symbol,
                         "venue": position.get("venue"), "domicile": position.get("issuer_domicile"),
                         "asset_class": asset_class})
    known = [r for r in rows if r["value"] is not None]
    total = sum((r["value"] for r in known), Decimal(0))
    groups: dict[str, dict] = {}
    for row in known:
        if row["asset_class"] == "cash":
            continue
        group = groups.setdefault(row["underlying"], {"underlying": row["underlying"], "value": Decimal(0), "symbols": []})
        group["value"] += row["value"]
        if row["symbol"] not in group["symbols"]:
            group["symbols"].append(row["symbol"])
    top = sorted(groups.values(), key=lambda g: (-g["value"], g["underlying"]))
    split = lambda field: {k: num(v) for k, v in sorted(  # noqa: E731
        _sum_by(known, lambda r: r[field] or ("cash" if r["asset_class"] == "cash" else "other")).items())}
    return {
        "currency": currency, "total": num(total) if rows else None,
        "unvalued": len(rows) - len(known),
        "top": [{"underlying": g["underlying"], "value": num(g["value"]), "symbols": g["symbols"],
                 "weight": num(g["value"] / total, 4) if total else None} for g in top[:3]],
        "overlaps": [{"underlying": g["underlying"], "symbols": g["symbols"], "value": num(g["value"])}
                     for g in top if len(g["symbols"]) > 1],
        "venue": split("venue"), "domicile": split("domicile"),
        "positions": len(rows),
    }


def _sum_by(rows: list[dict], key) -> dict[str, Decimal]:
    out: dict[str, Decimal] = {}
    for row in rows:
        name = key(row)
        out[name] = out.get(name, Decimal(0)) + row["value"]
    return out


def goal_name(goal: Mapping[str, Any]) -> str:
    for field in ("name", "description", "label", "title"):
        if isinstance(goal.get(field), str) and goal[field].strip():
            return goal[field].strip()
    return humanize(goal.get("id") or "goal")


def _goals(facts: _Facts, fx: _FX, currency: str | None, today: date) -> list[dict]:
    raw = facts.any_value("goals")
    if not isinstance(raw, list):
        return []
    usable = facts.eligible("goals")
    if usable:
        facts.used["goals"] = facts.all["goals"].get("id")
    goals = []
    for index, goal in enumerate(raw):
        if not isinstance(goal, dict):
            continue
        frequency = goal.get("frequency")
        monthly = D(goal.get("monthly_contribution"))
        target = D(goal.get("target_amount", goal.get("target")))
        if monthly is None and frequency == "monthly":
            monthly = D(goal.get("amount"))
        elif target is None and frequency in (None, "one_off"):
            target = D(goal.get("amount"))
        when = _as_date(goal.get("target_date") or goal.get("due"))
        goal_currency, assumed = goal.get("currency"), False
        if goal_currency is None and (target is not None or monthly is not None) and "name" not in goal and currency:
            goal_currency, assumed = currency, True  # legacy goals often omitted it; said so in the result
        action = goal.get("action")
        text = " ".join(str(goal.get(f) or "") for f in ("id", "name", "description")).lower()
        if action is None and re.search(r"invest|invertir|dca|s&p|sp500", text):
            action = "invest"
        goals.append({
            "id": str(goal.get("id") or index), "name": goal_name(goal), "action": action,
            "object": goal.get("object"), "target_amount": num(target), "currency": goal_currency,
            "currency_assumed": assumed,
            "target_date": when.isoformat() if when else None, "monthly_contribution": num(monthly),
            "monthly_value": num(fx.convert(monthly, goal_currency, currency)) if monthly is not None else None,
            "priority": goal.get("priority"), "status": goal.get("status") or "active",
            "protect_now": goal.get("protect_now") if isinstance(goal.get("protect_now"), bool) else None,
            "months_left": ((when.year - today.year) * 12 + when.month - today.month) if when else None,
            "approximate": bool(goal.get("approximate")) or "aproximad" in text or "about" in text,
            "eligible": usable, "legacy": "name" not in goal,
        })
    return goals


def _dca(facts: _Facts, fx: _FX, currency: str | None) -> list[dict]:
    plans = facts.value("planning.dca")
    plans = plans if isinstance(plans, list) else [plans] if isinstance(plans, dict) else []
    out = []
    for plan in plans:
        if not isinstance(plan, dict) or plan.get("status") in {"stopped", "paused", "ended"}:
            continue
        factor = _CADENCE_PER_MONTH.get(plan.get("cadence"))
        legs = sum((D(l.get("amount")) or Decimal(0) for l in plan.get("legs") or [] if isinstance(l, dict)), Decimal(0))
        if factor is None or not legs:
            continue
        monthly = legs * factor
        out.append({"id": plan.get("id"), "monthly": num(monthly), "currency": plan.get("currency"),
                    "value": num(fx.convert(monthly, plan.get("currency"), currency))})
    return out


def _threads(facts: _Facts) -> list[dict]:
    threads = []
    for key in facts.keys("thread."):
        value = facts.any_value(key)
        if not isinstance(value, dict) or not isinstance(value.get("text"), str):
            continue
        meta = facts.meta(key)
        threads.append({"id": key.split(".", 1)[1], "key": key, "kind": value.get("kind"), "text": value["text"],
                        "status": value.get("status") or "open",
                        "created": value.get("created") or meta["observed_on"],
                        "related": value.get("related") or [], "resolution": value.get("resolution")})
    threads.sort(key=lambda t: (t["created"] or "", t["id"]), reverse=True)
    return threads


# ------------------------------------------------------------------ build


def build(snapshot: Mapping[str, Any], ledger: Mapping[str, Any] | None = None, today: date | str | None = None,
          *, since_revision: int | None = None) -> dict:
    """The person's current picture: deterministic, JSON-safe, unknown kept as ``None``."""
    today = _as_date(today) or datetime.now(timezone.utc).date()
    facts = _Facts(snapshot, today)
    fx = _FX(today)
    for row in (ledger or {}).get("fx") or []:
        fx.add(row.get("base"), row.get("quote"), row.get("rate"), row.get("date"), "ledger")
    statement_accounts = _statement_accounts(facts, ledger)
    for account in statement_accounts:
        for row in account["fx"]:
            if isinstance(row, dict):
                fx.add(row.get("from"), row.get("to"), row.get("rate"), row.get("as_of") or account["as_of"],
                       f"statement {account['as_of']}")
    profile = _profile(facts)
    income_items, income_legacy = _income(facts)
    cash_items, cash_legacy = _cash(facts)
    liability_items, liability_legacy = _liabilities(facts, statement_accounts)
    stated_investments = _stated_investments(facts)
    seen = [i.get("currency") for i in (*income_items, *cash_items, *liability_items, *stated_investments)]
    seen += [a.get("currency") for a in statement_accounts]
    currency, currency_basis = _reporting_currency(facts, profile, seen)

    income = _income_view(income_items, fx, currency)
    income["legacy_source"] = income_legacy
    spending = _spending(facts, ledger, fx, currency, today)

    # -- assets
    statements = {a["institution"].strip().lower() for a in statement_accounts
                  if a["eligible"] and a["source"] != "ledger" and a.get("institution")}
    has_statement = any(a["eligible"] and a["source"] != "ledger" for a in statement_accounts)
    unconverted: list[dict] = []
    by_currency: dict[str, Decimal] = {}
    differences = []

    def value_of(amount: Decimal | None, cur: str | None) -> Decimal | None:
        converted = fx.convert(amount, cur, currency)
        if amount is not None and cur:
            by_currency[cur] = by_currency.get(cur, Decimal(0)) + amount
        if converted is None and amount is not None:
            unconverted.append(_money(amount, cur))
        return converted

    cash_rows = []
    for item in cash_items:
        amount = D(item.get("amount"))
        row = {"id": item["id"], "key": item["key"], "institution": item.get("institution"), "name": item.get("name"),
               "amount": num(amount), "currency": item.get("currency"), "purpose": item.get("purpose"),
               "liquid": item.get("liquid") is not False, "approximate": bool(item.get("approximate")),
               "legacy": bool(item.get("legacy")), "counted": True, "value": None}
        if _covered(item.get("institution"), statements):
            row["counted"] = False
        else:
            row["value"] = num(value_of(amount, item.get("currency")))
        cash_rows.append(row)

    accounts = []
    duplicates: dict[tuple, int] = {}
    for account in statement_accounts:
        pair = (account.get("institution") or humanize(account.get("id") or "account"), account.get("type"))
        duplicates[pair] = duplicates.get(pair, 0) + 1
    dup = {k for k, n in duplicates.items() if n > 1}
    for account in statement_accounts:
        row = {"id": account["id"], "key": account["key"], "label": _account_label(account, dup),
               "institution": account.get("institution"), "type": account.get("type"),
               "currency": account.get("currency"), "as_of": account["as_of"], "source": account["source"],
               "native": {c: num(v) for c, v in sorted((account["native"] or {}).items())} if account["native"] is not None else None,
               "value": None, "liquid": (account.get("type") or "").lower() not in _ILLIQUID_TYPES,
               "stale": not account["eligible"], "positions": len(account["positions"])}
        if account["eligible"] and account["native"] is not None:
            total, known = Decimal(0), True
            for cur, amount in account["native"].items():
                converted = value_of(amount, cur)
                if converted is None:
                    known = False
                else:
                    total += converted
            row["value"] = num(total) if known else None
            row["value_known_part"] = num(total)
        accounts.append(row)

    investments = []
    for item in stated_investments:
        amount = D(item.get("amount"))
        institution = item.get("institution")
        row = {"id": item["id"], "key": item["key"], "institution": institution, "kind": item.get("kind"),
               "name": item.get("name"), "amount": num(amount), "currency": item.get("currency"),
               "approximate": bool(item.get("approximate")), "legacy": bool(item.get("legacy")),
               "liquid": (item.get("kind") or "").lower() not in _ILLIQUID_TYPES, "counted": True, "value": None}
        covered = _covered(institution, statements) or (not institution and has_statement)
        if covered:
            row["counted"] = False
            matching = [a for a in accounts if a["source"] != "ledger" and not a["stale"] and (
                not institution or (a.get("institution") or "").strip().lower() == institution.strip().lower())]
            statement_value = sum((D(a.get("value_known_part")) or Decimal(0) for a in matching), Decimal(0))
            native: dict[str, Any] = {}
            for account in matching:
                for cur, amount_ in (account.get("native") or {}).items():
                    native[cur] = num((D(native.get(cur)) or Decimal(0)) + D(amount_))
            stated_value = fx.convert(amount, item.get("currency"), currency)
            differences.append({
                "institution": institution, "key": item["key"], "stated": _money(amount, item.get("currency")),
                "stated_approximate": row["approximate"], "statement": native,
                "statement_value": num(statement_value), "currency": currency,
                "as_of": max((a["as_of"] for a in matching if a["as_of"]), default=None),
                "difference": num(statement_value - stated_value) if stated_value is not None else None,
            })
        else:
            row["value"] = num(value_of(amount, item.get("currency")))
        investments.append(row)
    for row in cash_rows:
        if not row["counted"]:
            differences.append({"institution": row["institution"], "key": row["key"],
                                "stated": _money(D(row["amount"]), row["currency"]), "stated_approximate": row["approximate"],
                                "statement": None, "statement_value": None, "currency": currency, "as_of": None,
                                "difference": None, "kind": "cash"})

    liabilities = [_liability_view(item, fx, currency, today) for item in liability_items]
    for row in liabilities:
        if row["balance"] is not None and row["currency"]:
            by_currency[row["currency"]] = by_currency.get(row["currency"], Decimal(0)) - D(row["balance"])
            if row["value"] is None:
                unconverted.append({"amount": -row["balance"], "currency": row["currency"]})

    liquid = sum((D(r["value"]) or Decimal(0) for r in (*cash_rows, *accounts, *investments)
                  if r.get("value") is not None and r.get("liquid", True)), Decimal(0))
    illiquid = sum((D(r["value"]) or Decimal(0) for r in (*accounts, *investments)
                    if r.get("value") is not None and not r.get("liquid", True)), Decimal(0))
    owed = sum((D(r["value"]) or Decimal(0) for r in liabilities if r["value"] is not None), Decimal(0))
    unvalued_accounts = [a["label"] for a in accounts if a["source"] == "ledger"]
    any_assets = any(r.get("value") is not None for r in (*cash_rows, *accounts, *investments))
    net_worth = {
        "currency": currency, "currency_basis": currency_basis,
        "total": num(liquid + illiquid - owed) if (any_assets or liabilities) and currency else None,
        "assets": num(liquid + illiquid) if any_assets else None, "liquid": num(liquid) if any_assets else None,
        "illiquid": num(illiquid) if any_assets else None, "liabilities": num(owed),
        "by_currency": {c: num(v) for c, v in sorted(by_currency.items())},
        "unconverted": unconverted, "unvalued_accounts": unvalued_accounts,
        "complete": not unconverted and not unvalued_accounts and bool(any_assets),
    }

    # -- monthly flow
    debt_known = sum((D(r["monthly_payment"]) for r in liabilities
                      if r["monthly_payment"] is not None and not r["in_spending"]), Decimal(0))
    debt_unknown = [r["id"] for r in liabilities if r["monthly_payment"] is None and not r["in_spending"]
                    and (r["balance"] or 0) > 0]
    debt_converted = sum((fx.convert(D(r["monthly_payment"]), r["currency"], currency) or Decimal(0)
                          for r in liabilities if r["monthly_payment"] is not None and not r["in_spending"]), Decimal(0))
    income_monthly, spend_monthly = D(income["monthly"]), D(spending["monthly"])
    surplus = income_monthly - spend_monthly - debt_converted if income_monthly is not None and spend_monthly is not None else None
    cash_flow = {"currency": currency, "income": income["monthly"], "spending": spending["monthly"],
                 "spending_basis": spending["monthly_basis"], "spending_source": spending["source"],
                 "debt_payments": num(debt_converted) if debt_known or not debt_unknown else None,
                 "debt_payments_known": num(debt_converted), "debt_payments_unknown": debt_unknown,
                 "surplus": num(surplus), "complete": surplus is not None and not debt_unknown}

    # -- commitments
    goals = _goals(facts, fx, currency, today)
    dca = _dca(facts, fx, currency)
    committed = [{"kind": "goal", "id": g["id"], "name": g["name"], "monthly": g["monthly_value"]}
                 for g in goals if g["status"] == "active" and g["monthly_value"] is not None and g["eligible"]]
    committed += [{"kind": "dca", "id": d["id"], "name": d["id"], "monthly": d["value"]} for d in dca if d["value"] is not None]
    total_committed = sum((D(c["monthly"]) for c in committed), Decimal(0))
    commitments = {"currency": currency, "items": committed, "total": num(total_committed),
                   "unallocated": num(surplus - total_committed) if surplus is not None else None,
                   "overcommitted": surplus is not None and total_committed > surplus}

    # -- reserve
    reserve_fact = facts.value("reserve")
    reserve_fact = reserve_fact if isinstance(reserve_fact, dict) else {}
    target_months = D(reserve_fact.get("target_months"))
    if target_months is None and _plan_resources(facts).get("reserve_months") is not None:
        target_months = D(_plan_resources(facts)["reserve_months"])
    funded_by = set(reserve_fact.get("funded_by") or [])
    designated = [r for r in cash_rows if r["counted"] and r["value"] is not None
                  and (r["purpose"] == "reserve" or r["id"] in funded_by)]
    basis = "designated"
    if not designated:
        designated = [r for r in cash_rows if r["counted"] and r["value"] is not None and r["liquid"]
                      and (r["purpose"] in (None, "general"))]
        basis = "all undesignated cash" if designated else None
    reserve_amount = sum((D(r["value"]) for r in designated), Decimal(0)) if designated else None
    essential = D(spending["essential_for_reserve"])
    months = reserve_amount / essential if reserve_amount is not None and essential else None
    target_amount = D(reserve_fact.get("target_amount"))
    if target_amount is None and target_months is not None and essential:
        target_amount = target_months * essential
    reserve = {"currency": currency, "amount": num(reserve_amount), "months": num(months, 1),
               "basis": basis, "spending_basis": "essential" if spending["essential"] is not None else spending["monthly_basis"],
               "target_months": num(target_months, 1), "target_amount": num(target_amount),
               "gap": num(target_amount - reserve_amount) if target_amount is not None and reserve_amount is not None else None,
               "sources": [r["id"] for r in designated]}

    holdings = _holdings(statement_accounts, fx, currency)
    threads = _threads(facts)

    # -- unknowns that matter, most consequential first
    # ``code`` is stable for renderers; ``field`` names what to save.
    unknowns = []
    if income["monthly"] is None:
        unknowns.append({"code": "income", "field": "income.<id>"})
    if spending["monthly"] is None:
        unknowns.append({"code": "spending", "field": "spending.monthly"})
    for row in liabilities:
        for field in row["missing"]:
            code = "liability_rate" if field == "annual_rate" else "liability_payment"
            unknowns.append({"code": code, "field": f"liability.{row['id']}.{field.split()[0]}", "liability": row["id"]})
    for currency_code in sorted({u["currency"] for u in unconverted if u and u.get("currency")}):
        if currency:
            unknowns.append({"code": "fx", "field": f"fx.{currency_code}/{currency}", "pair": f"{currency_code}/{currency}"})
    if profile["residence"] and not profile["tax_residence"]:
        unknowns.append({"code": "tax_residence", "field": "client.profile.tax_residence",
                         "residence": profile["residence"].get("country")})
    if reserve["target_months"] is None and reserve["target_amount"] is None and (cash_rows or spending["monthly"]):
        unknowns.append({"code": "reserve_target", "field": "reserve.target_months"})
    for goal in goals:
        if goal["status"] == "active" and goal["target_amount"] is None and goal["monthly_contribution"] is None:
            unknowns.append({"code": "goal_amount", "field": f"goals.{goal['id']}.target_amount", "goal": goal["name"]})

    revision = (snapshot.get("client") or {}).get("revision")
    changes = None
    if since_revision is not None:
        changes = [{"key": k, "revision": f.get("revision")} for k, f in sorted(facts.all.items())
                   if isinstance(f.get("revision"), int) and f["revision"] > since_revision]
    return {
        "version": 1, "as_of": today.isoformat(), "revision": revision, "currency": currency,
        "profile": profile, "income": income, "spending": spending, "cash": cash_rows,
        "accounts": accounts, "investments": investments, "liabilities": liabilities,
        "differences": differences, "net_worth": net_worth, "cash_flow": cash_flow,
        "commitments": commitments, "reserve": reserve, "goals": goals, "dca": dca, "holdings": holdings,
        "threads": {"open": [t for t in threads if t["status"] == "open"],
                    "closed": [t for t in threads if t["status"] != "open"][:5]},
        "stale": facts.stale, "inferred": facts.inferred, "unknowns": unknowns,
        "legacy": sorted({k for k in (income_legacy, cash_legacy, liability_legacy) if k}
                         | {i["key"] for i in stated_investments if i.get("legacy")}
                         | ({"goals"} if any(g["legacy"] for g in goals) else set())),
        "fx": fx.listing(), "evidence": {k: v for k, v in sorted(facts.used.items()) if v},
        "meta": {k: facts.meta(k) for k in sorted(facts.all)},
        "changes": changes, "changes_since": since_revision,
    }


def missing_for_onboarding(sit: Mapping[str, Any]) -> list[str]:
    """Onboarding steps still unknown, in asking order; done and skipped steps are not missing."""
    from .schema import ONBOARDING_STEPS
    profile = sit.get("profile") or {}
    steps = ((profile.get("onboarding") or {}).get("steps") or {})
    risk = profile.get("risk") or {}
    known = {
        "name": bool(profile.get("name")),
        "language": bool(profile.get("language")),
        "residence": bool((profile.get("residence") or {}).get("country")),
        "tax_residence": bool(profile.get("tax_residence")),
        "birth_year": profile.get("birth_year") is not None,
        "dependents": profile.get("dependents") is not None,
        "income": bool(sit["income"]["items"] or sit["income"]["extras"]),
        "spending": sit["spending"]["monthly"] is not None,
        "cash": bool(sit["cash"]),
        "debts": bool(sit["liabilities"]),
        "investments": bool(sit["investments"]) or any(a["source"] != "ledger" for a in sit["accounts"]),
        "goals": bool(sit["goals"]),
        "risk": bool(risk.get("drop_reaction")) and bool(risk.get("experience")),
    }
    return [step for step in ONBOARDING_STEPS if not known[step] and steps.get(step) not in ("done", "skipped")]


__all__ = ["build", "payoff", "annuity_payment", "missing_for_onboarding", "underlying_of", "goal_name",
           "add_months", "D", "num", "UNDERLYING"]
