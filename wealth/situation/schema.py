"""The canonical personal-finance schema, validated on ``remember``.

One small, documented shape per fact key.  Every section is optional; an
absent field is unknown, never zero.  Amounts are plain numbers in the stated
``currency`` (ISO 4217); rates are decimals (0.13 is 13 %).

This module imports nothing from the store so the store can call it.
"""
from __future__ import annotations

import math
import re
from datetime import date, datetime
from typing import Any, Callable

# ------------------------------------------------------------------ the contract

LANGUAGES = ("es", "en")
FREQUENCIES = ("monthly", "biweekly", "annual", "one_off")
INCOME_KINDS = ("salary", "aguinaldo", "ptu", "bonus", "rent", "business", "pension", "other")
LIABILITY_KINDS = ("auto", "mortgage", "card", "personal", "student", "other")
PAYMENT_FREQUENCIES = ("monthly", "biweekly", "annual")
GOAL_PRIORITIES = ("high", "medium", "low")
GOAL_STATUSES = ("active", "paused", "done", "dropped")
GOAL_ACTIONS = ("invest", "save", "buy", "pay_off", "retire", "education", "other")
THREAD_KINDS = ("advice", "question", "commitment")
THREAD_STATUSES = ("open", "resolved", "superseded")
DROP_REACTIONS = ("sell", "hold", "buy_more")
EXPERIENCE = ("none", "some", "experienced")
ONBOARDING_STEPS = ("name", "language", "residence", "tax_residence", "birth_year", "dependents", "income",
                    "spending", "cash", "debts", "investments", "goals", "risk")
STEP_STATUSES = ("done", "skipped", "pending", "unsure")
IPS_PROFILES = ("conservative", "balanced", "growth")
IPS_CADENCES = ("annual", "semiannual", "quarterly")  # unsure: answered "not sure"; stays unknown

SCHEMA: dict[str, dict[str, str]] = {
    "client.profile": {
        "name?": "what the person wants to be called",
        "birth_year?": "YYYY (not age, so it stays true)",
        "residence?": "{country: ISO-2 (MX, US), region?: state, city?}",
        "tax_residence?": "list of ISO-2 countries the person stated; never inferred",
        "citizenship?": "list of ISO-2 countries", "us_person?": "true|false",
        "dependents?": "count", "dependent_ages?": "list of ages",
        "currencies?": "list of ISO 4217 codes they hold", "reporting_currency?": "ISO 4217",
        "timezone?": "IANA name", "language?": "es|en",
    },
    "income.<id>": {
        "amount": "number", "currency": "ISO 4217", "frequency": "|".join(FREQUENCIES),
        "net?": "true = take-home, false = before tax", "kind?": "|".join(INCOME_KINDS),
        "month?": "1-12 for annual items (aguinaldo: 12)", "date?": "YYYY-MM-DD for one_off",
        "name?": "their words", "approximate?": "true when they said 'about'",
    },
    "spending.monthly": {
        "essential?": "number", "discretionary?": "number", "total?": "number (at least one of the three)",
        "currency": "ISO 4217", "approximate?": "true|false",
    },
    "cash.<id>": {
        "amount": "number (omit only with balance_unknown: true)", "currency": "ISO 4217",
        "institution?": "bank or fintech name", "balance_unknown?": "true when they have it but the amount is not known",
        "purpose?": "reserve|general|goal:<goal id>", "liquid?": "true (default for cash) | false",
        "name?": "their words", "approximate?": "true|false",
    },
    "liability.<id>": {
        "kind": "|".join(LIABILITY_KINDS), "balance": "number owed", "currency": "ISO 4217",
        "annual_rate?": "decimal (0.13)", "payment?": "number per payment_frequency",
        "payment_frequency?": "|".join(PAYMENT_FREQUENCIES), "remaining_term_months?": "integer",
        "maturity?": "YYYY-MM-DD", "lender?": "name", "in_spending?": "true if the payment is inside spending.monthly",
        "approximate?": "true|false",
    },
    "investment.<id>": {
        "amount": "number (a stated balance; statements replace it)", "currency": "ISO 4217",
        "institution?": "name", "kind?": "brokerage|retirement|afore|fund|other", "approximate?": "true|false",
        "purpose?": "reserve|general|goal:<goal id> (only as the person stated it)",
        "liquidity_days?": "days to get the money out (1 daily, 28 CETES at 28 days); counts toward the reserve "
                           "when 31 or less",
    },
    "goals": {
        "[]": "list; merge by id",
        "id": "slug", "name": "short human name in the person's language",
        "action?": "|".join(GOAL_ACTIONS), "object?": "what it is for, e.g. 'el S&P 500'",
        "target_amount?": "number", "currency?": "ISO 4217 (required with an amount)",
        "target_date?": "YYYY-MM-DD", "monthly_contribution?": "number",
        "liability?": "liability.<id> key a pay_off goal pays down",
        "priority?": "|".join(GOAL_PRIORITIES), "status?": "|".join(GOAL_STATUSES) + " (default active)",
        "protect_now?": "true to reserve the target from current capital",
        "funded_amount?": "number already set aside for it (\"tengo 400k apartados\"), in the goal currency",
        "accounts?": "fact keys of the money earmarked for it (cash.<id>, investment.<id>)",
    },
    "reserve": {"target_months?": "number", "target_amount?": "number", "currency?": "ISO 4217",
                "funded_by?": "list of cash.<id> ids"},
    "thread.<id>": {
        "kind": "|".join(THREAD_KINDS), "text": "one sentence in the person's language",
        "status": "|".join(THREAD_STATUSES), "created?": "YYYY-MM-DD (defaults to observed_on)",
        "related?": "list of fact keys", "resolution?": "how it was resolved or why it was revised",
    },
    "constraint.<id>": {"note": "a stated limit or condition; never a payment (liability.<id>.payment), a balance "
                                "or a set-aside amount (goals funded_amount)"},
    "preference.risk": {"drop_reaction?": "|".join(DROP_REACTIONS) + " after a 20% fall",
                        "experience?": "|".join(EXPERIENCE)},
    "onboarding": {"steps": "{" + "|".join(ONBOARDING_STEPS) + ": done|skipped|pending|unsure}",
                   "started_at": "ISO date-time", "completed_at?": "ISO date-time"},
    "policy.ips": {
        "note": "written only when the person accepts an IPS decision (policy_draft propose=true, then "
                "wealth_decision accept); never written directly",
        "version": "integer, 1 for the first accepted policy", "decision_id": "the accepted decision",
        "accepted_on": "YYYY-MM-DD", "supersedes?": "decision id of the policy it replaced",
        "as_of": "YYYY-MM-DD", "currency": "ISO 4217",
        "allocation": "{model: " + "|".join(IPS_PROFILES) + ", sleeves: [{id, name, target, min, max}]} "
                      "(targets sum to 1, min <= target <= max)",
        "constraints": "{concentration: {limit}, leverage: {allowed}, exclusions, estate_situs, tax, other}",
        "rebalancing": "{absolute_band, relative_band}", "review": "{cadence: " + "|".join(IPS_CADENCES) + ", next_review}",
        "objectives?": "list", "risk?": "{ability, willingness, profile}", "liquidity?": "{reserve, near_goals}",
        "buckets?": "list", "return_requirement?": "{value, goal_id}", "residence?": "object",
        "missing?": "list", "evidence?": "{fact key: fact id}",
    },
    "follow.<cik>": {
        "note": "a 13F manager the person follows; <cik> is the 10-digit SEC CIK (find it with manager_search)",
        "cik": "the same 10-digit CIK", "name": "the manager as EDGAR names it",
        "since?": "YYYY-MM-DD", "notify?": "false to stop new-13F items (default true)",
        "mirror?": "{sleeve_amount, currency, top_n?} when the person mirrors it in a sleeve",
        "note_text?": "their words on why they follow it",
    },
}
"""Human-readable contract, returned in ``fact_contract`` and by ``wealth_context``."""

LEGACY_KEYS = ("plan.resources", "income.schedule", "household", "portfolio.snapshot")

# ------------------------------------------------------------------ validation


class SchemaError(ValueError):
    """A canonical fact does not match the schema; the message names the field and the fix."""


COUNTRIES = {
    "mx": "MX", "mex": "MX", "mexico": "MX", "méxico": "MX",
    "us": "US", "usa": "US", "united states": "US", "estados unidos": "US", "ee.uu.": "US", "eeuu": "US", "eua": "US",
    "ca": "CA", "canada": "CA", "canadá": "CA", "es": "ES", "spain": "ES", "españa": "ES",
}
"""Country names the model may write; they are read as these ISO-2 codes."""


def country_code(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if re.fullmatch(r"[A-Z]{2}", text):
        return text
    return COUNTRIES.get(text.lower())


_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_CURRENCY = re.compile(r"^[A-Z]{3}$")


def _fail(path: str, message: str) -> None:
    raise SchemaError(f"{path} {message}")


MAX_AMOUNT = 10 ** 15
"""Amounts, balances and quantities must be smaller than this (a quadrillion) in absolute value.

Anything larger is a typo or an attack, and it would break the decimal arithmetic
every reader of the picture relies on."""
MAX_RATE = 10
MAX_MONTHS = 1200


def _shown(value: Any) -> str:
    text = repr(value)
    return text if len(text) <= 40 else text[:37] + "..."


def _number(value: Any, path: str, *, required: bool = True, minimum: float | None = 0,
            maximum: float = MAX_AMOUNT) -> None:
    if value is None:
        if required:
            _fail(path, "is required (a number; leave the whole fact out if unknown, never 0)")
        return
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        _fail(path, f"must be a number, not {_shown(value)}")
    if abs(value) >= maximum:
        _fail(path, f"is too large: numbers here must be below {maximum:,} (check for a typo)")
    if minimum is not None and value < minimum:
        _fail(path, f"must be at least {minimum}")


def _enum(value: Any, path: str, options: tuple[str, ...], *, required: bool = False) -> None:
    if value is None:
        if required:
            _fail(path, f"is required: one of {'|'.join(options)}")
        return
    if value not in options:
        _fail(path, f"must be one of {'|'.join(options)}, not {value!r}")


def _currency(value: Any, path: str, *, required: bool = True) -> None:
    if value is None and not required:
        return
    if not isinstance(value, str) or not _CURRENCY.match(value):
        _fail(path, "must be a three-letter ISO currency such as MXN or USD")


def _text(value: Any, path: str, *, required: bool = False, limit: int = 300) -> None:
    if value is None:
        if required:
            _fail(path, "is required")
        return
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        _fail(path, f"must be text of 1-{limit} characters")


def _bool(value: Any, path: str) -> None:
    if value is not None and not isinstance(value, bool):
        _fail(path, "must be true or false")


def _iso_date(value: Any, path: str, *, required: bool = False) -> None:
    if value is None:
        if required:
            _fail(path, "is required (YYYY-MM-DD)")
        return
    try:
        ok = isinstance(value, str) and date.fromisoformat(value).isoformat() == value
    except ValueError:
        ok = False
    if not ok:
        _fail(path, "must be a date YYYY-MM-DD")


def _timestamp(value: Any, path: str, *, required: bool = False) -> None:
    if value is None:
        if required:
            _fail(path, "is required (ISO date-time)")
        return
    try:
        datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        _fail(path, "must be an ISO date-time such as 2026-09-21T18:00:00Z")


def _object(value: Any, path: str, fields: set[str]) -> dict:
    if not isinstance(value, dict):
        _fail(path, "must be an object")
    unknown = sorted(set(value) - fields)
    if unknown:
        _fail(path, f"has unknown fields {unknown}; allowed: {sorted(fields)}")
    return value


def _rate(value: Any, path: str) -> None:
    _number(value, path, required=False, maximum=100 * MAX_RATE)  # 45 means 45%: explained below
    if isinstance(value, (int, float)) and value > 1:
        _fail(path, f"is a decimal rate: write {value / 100:g} for {value:g}%")


def _profile(value: dict, key: str) -> None:
    if not isinstance(value, dict):
        _fail(key, "must be an object")
    # Other fields are tolerated for older profiles; known fields must be well typed.
    _text(value.get("name"), f"{key}.name", limit=80)
    year = value.get("birth_year")
    if year is not None and (isinstance(year, bool) or not isinstance(year, int) or not 1900 <= year <= 2030):
        _fail(f"{key}.birth_year", "must be a four-digit year such as 1988 (not an age)")
    residence = value.get("residence")
    if isinstance(residence, str):
        if country_code(residence) is None:
            _fail(f"{key}.residence", "must be {country: ISO-2, region?, city?}")
    elif residence is not None:
        _object(residence, f"{key}.residence", {"country", "region", "city"})
        if country_code(residence.get("country")) is None:
            _fail(f"{key}.residence.country", "must be an ISO-2 country code such as MX or US")
        _text(residence.get("region"), f"{key}.residence.region", limit=80)
        _text(residence.get("city"), f"{key}.residence.city", limit=80)
    for name in ("tax_residence", "citizenship"):
        items = value.get(name)
        items = [items] if isinstance(items, str) else items
        if items is not None and (not isinstance(items, list) or not all(country_code(i) for i in items)):
            _fail(f"{key}.{name}", "must be a list of ISO-2 country codes such as [\"MX\"]")
    _bool(value.get("us_person"), f"{key}.us_person")
    dependents = value.get("dependents")
    if dependents is not None and (isinstance(dependents, bool) or not isinstance(dependents, int)
                                   or not 0 <= dependents <= 100):
        _fail(f"{key}.dependents", "must be a whole number (0 when they said none)")
    ages = value.get("dependent_ages")
    if ages is not None and (not isinstance(ages, list) or not all(
            isinstance(a, int) and not isinstance(a, bool) and 0 <= a <= 120 for a in ages)):
        _fail(f"{key}.dependent_ages", "must be a list of whole-number ages")
    currencies = value.get("currencies")
    if currencies is not None and (not isinstance(currencies, list) or not all(
            isinstance(c, str) and _CURRENCY.match(c) for c in currencies)):
        _fail(f"{key}.currencies", "must be a list of ISO currency codes such as [\"MXN\", \"USD\"]")
    if value.get("reporting_currency") is not None:
        _currency(value["reporting_currency"], f"{key}.reporting_currency")
    _enum(value.get("language"), f"{key}.language", LANGUAGES)
    _text(value.get("timezone"), f"{key}.timezone", limit=64)


def _income(value: dict, key: str) -> None:
    _object(value, key, {"amount", "currency", "frequency", "net", "kind", "month", "date", "name", "approximate", "note"})
    _number(value.get("amount"), f"{key}.amount")
    _currency(value.get("currency"), f"{key}.currency")
    _enum(value.get("frequency"), f"{key}.frequency", FREQUENCIES, required=True)
    _bool(value.get("net"), f"{key}.net")
    _enum(value.get("kind"), f"{key}.kind", INCOME_KINDS)
    month = value.get("month")
    if month is not None and (isinstance(month, bool) or not isinstance(month, int) or not 1 <= month <= 12):
        _fail(f"{key}.month", "must be a month number 1-12")
    _iso_date(value.get("date"), f"{key}.date")
    _text(value.get("name"), f"{key}.name")
    _text(value.get("note"), f"{key}.note")
    _bool(value.get("approximate"), f"{key}.approximate")


def _spending(value: dict, key: str) -> None:
    _object(value, key, {"essential", "discretionary", "total", "currency", "approximate", "note"})
    for name in ("essential", "discretionary", "total"):
        _number(value.get(name), f"{key}.{name}", required=False)
    if all(value.get(n) is None for n in ("essential", "discretionary", "total")):
        _fail(key, "needs total or essential (monthly amounts); leave the fact out if unknown")
    _currency(value.get("currency"), f"{key}.currency")
    _bool(value.get("approximate"), f"{key}.approximate")
    _text(value.get("note"), f"{key}.note")


def _cash(value: dict, key: str) -> None:
    _object(value, key, {"amount", "currency", "institution", "purpose", "liquid", "name", "approximate", "note",
                         "balance_unknown"})
    _bool(value.get("balance_unknown"), f"{key}.balance_unknown")
    _number(value.get("amount"), f"{key}.amount", required=value.get("balance_unknown") is not True)
    _currency(value.get("currency"), f"{key}.currency")
    _text(value.get("institution"), f"{key}.institution", limit=80)
    _purpose(value.get("purpose"), f"{key}.purpose")
    _bool(value.get("liquid"), f"{key}.liquid")
    _text(value.get("name"), f"{key}.name")
    _bool(value.get("approximate"), f"{key}.approximate")
    _text(value.get("note"), f"{key}.note")


def _liability(value: dict, key: str) -> None:
    if "proposal_id" in value:  # a statement record written by ingest confirm
        return
    _object(value, key, {"kind", "balance", "currency", "annual_rate", "payment", "payment_frequency",
                         "remaining_term_months", "maturity", "lender", "name", "in_spending", "approximate", "note"})
    _enum(value.get("kind"), f"{key}.kind", LIABILITY_KINDS, required=True)
    _number(value.get("balance"), f"{key}.balance")
    _currency(value.get("currency"), f"{key}.currency")
    _rate(value.get("annual_rate"), f"{key}.annual_rate")
    _number(value.get("payment"), f"{key}.payment", required=False)
    _enum(value.get("payment_frequency"), f"{key}.payment_frequency", PAYMENT_FREQUENCIES)
    if value.get("payment") is not None and value.get("payment_frequency") is None:
        _fail(f"{key}.payment_frequency", "is required with payment: " + "|".join(PAYMENT_FREQUENCIES))
    term = value.get("remaining_term_months")
    if term is not None and (isinstance(term, bool) or not isinstance(term, int) or not 0 <= term <= MAX_MONTHS):
        _fail(f"{key}.remaining_term_months", "must be a whole number of months")
    _iso_date(value.get("maturity"), f"{key}.maturity")
    _text(value.get("lender"), f"{key}.lender", limit=80)
    _text(value.get("name"), f"{key}.name")
    _bool(value.get("in_spending"), f"{key}.in_spending")
    _bool(value.get("approximate"), f"{key}.approximate")
    _text(value.get("note"), f"{key}.note")


def _purpose(value: Any, path: str) -> None:
    if value is not None and value not in ("reserve", "general") and not (
            isinstance(value, str) and value.startswith("goal:") and _ID.match(value[5:])):
        _fail(path, "must be reserve, general or goal:<goal id>")


def _investment(value: dict, key: str) -> None:
    _object(value, key, {"amount", "currency", "institution", "kind", "name", "approximate", "note",
                         "balance_unknown", "purpose", "liquidity_days"})
    _purpose(value.get("purpose"), f"{key}.purpose")
    days = value.get("liquidity_days")
    if days is not None and (isinstance(days, bool) or not isinstance(days, int) or not 0 <= days <= 36600):
        _fail(f"{key}.liquidity_days", "must be a whole number of days")
    _bool(value.get("balance_unknown"), f"{key}.balance_unknown")
    _number(value.get("amount"), f"{key}.amount", required=value.get("balance_unknown") is not True)
    _currency(value.get("currency"), f"{key}.currency")
    _text(value.get("institution"), f"{key}.institution", limit=80)
    _enum(value.get("kind"), f"{key}.kind", ("brokerage", "retirement", "afore", "fund", "other"))
    _text(value.get("name"), f"{key}.name")
    _bool(value.get("approximate"), f"{key}.approximate")
    _text(value.get("note"), f"{key}.note")


def _goals(value: Any, key: str) -> None:
    if not isinstance(value, list):
        _fail(key, "must be a list of goal objects")
    seen = set()
    for index, goal in enumerate(value):
        path = f"goals[{index}]"
        if not isinstance(goal, dict):
            _fail(path, "must be an object")
        goal_id = goal.get("id")
        if not isinstance(goal_id, str) or not _ID.match(goal_id):
            _fail(f"{path}.id", "must be a lowercase slug such as 'house-2028'")
        if goal_id in seen:
            _fail(f"{path}.id", f"duplicates {goal_id!r}")
        seen.add(goal_id)
        _text(goal.get("name"), f"{path}.name", required=True, limit=120)
        _enum(goal.get("action"), f"{path}.action", GOAL_ACTIONS)
        _text(goal.get("object"), f"{path}.object", limit=120)
        for name in ("target_amount", "monthly_contribution", "funded_amount"):
            _number(goal.get(name), f"{path}.{name}", required=False)
        if any(goal.get(n) is not None for n in ("target_amount", "monthly_contribution", "funded_amount")) \
                and goal.get("currency") is None:
            _fail(f"{path}.currency", "is required with an amount")
        _currency(goal.get("currency"), f"{path}.currency", required=False)
        _iso_date(goal.get("target_date"), f"{path}.target_date")
        _iso_date(goal.get("due"), f"{path}.due")
        _enum(goal.get("priority"), f"{path}.priority", GOAL_PRIORITIES)
        _enum(goal.get("status"), f"{path}.status", GOAL_STATUSES)
        _bool(goal.get("protect_now"), f"{path}.protect_now")
        liability = goal.get("liability")
        if liability is not None and (not isinstance(liability, str) or not liability.startswith("liability.")
                                      or len(liability) > 120):
            _fail(f"{path}.liability", "must be the key of a debt, such as 'liability.auto'")
        accounts = goal.get("accounts")
        if accounts is not None and (not isinstance(accounts, list) or not all(
                isinstance(a, str) and a.startswith(("cash.", "investment.")) and len(a) <= 120 for a in accounts)):
            _fail(f"{path}.accounts", "must be a list of fact keys such as 'cash.nu' or 'investment.cetes'")


def _reserve(value: dict, key: str) -> None:
    _object(value, key, {"target_months", "target_amount", "currency", "funded_by", "note"})
    _number(value.get("target_months"), f"{key}.target_months", required=False, maximum=MAX_MONTHS)
    _number(value.get("target_amount"), f"{key}.target_amount", required=False)
    if value.get("target_amount") is not None:
        _currency(value.get("currency"), f"{key}.currency")
    funded = value.get("funded_by")
    if funded is not None and (not isinstance(funded, list) or not all(isinstance(i, str) and _ID.match(i) for i in funded)):
        _fail(f"{key}.funded_by", "must be a list of cash ids (the part after 'cash.')")


def _thread(value: dict, key: str) -> None:
    _object(value, key, {"kind", "text", "status", "created", "related", "resolution"})
    _enum(value.get("kind"), f"{key}.kind", THREAD_KINDS, required=True)
    _text(value.get("text"), f"{key}.text", required=True, limit=400)
    _enum(value.get("status"), f"{key}.status", THREAD_STATUSES, required=True)
    _iso_date(value.get("created"), f"{key}.created")
    related = value.get("related")
    if related is not None and (not isinstance(related, list) or not all(isinstance(k, str) for k in related)):
        _fail(f"{key}.related", "must be a list of fact keys")
    _text(value.get("resolution"), f"{key}.resolution", limit=400)


def _risk(value: Any, key: str) -> None:
    if isinstance(value, str):  # older free-text risk labels stay readable
        return
    if not isinstance(value, dict):
        _fail(key, "must be {drop_reaction, experience}")
    _enum(value.get("drop_reaction"), f"{key}.drop_reaction", DROP_REACTIONS)
    _enum(value.get("experience"), f"{key}.experience", EXPERIENCE)


def _onboarding(value: dict, key: str) -> None:
    _object(value, key, {"steps", "started_at", "completed_at"})
    steps = value.get("steps")
    if not isinstance(steps, dict):
        _fail(f"{key}.steps", "must map step names to done|skipped|pending|unsure")
    for step, status in steps.items():
        if step not in ONBOARDING_STEPS:
            _fail(f"{key}.steps.{step}", f"is not a step; steps are {'|'.join(ONBOARDING_STEPS)}")
        _enum(status, f"{key}.steps.{step}", STEP_STATUSES, required=True)
    _timestamp(value.get("started_at"), f"{key}.started_at", required=True)
    _timestamp(value.get("completed_at"), f"{key}.completed_at")


_IPS_FIELDS = {"version", "decision_id", "accepted_on", "supersedes", "as_of", "currency", "residence", "objectives",
               "return_requirement", "risk", "liquidity", "buckets", "allocation", "constraints", "rebalancing",
               "review", "missing", "evidence"}


def _share(value: Any, path: str) -> float:
    _number(value, path, maximum=MAX_RATE)
    if value > 1:
        _fail(path, f"is a share between 0 and 1, not {value!r}")
    return float(value)


def _policy_ips(value: dict, key: str) -> None:
    _object(value, key, _IPS_FIELDS)
    version = value.get("version")
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        _fail(f"{key}.version", "must be a whole number from 1")
    _text(value.get("decision_id"), f"{key}.decision_id", required=True, limit=64)
    _text(value.get("supersedes"), f"{key}.supersedes", limit=64)
    _iso_date(value.get("accepted_on"), f"{key}.accepted_on", required=True)
    _iso_date(value.get("as_of"), f"{key}.as_of", required=True)
    _currency(value.get("currency"), f"{key}.currency")
    allocation = value.get("allocation")
    if not isinstance(allocation, dict):
        _fail(f"{key}.allocation", "must be {model, sleeves}")
    _enum(allocation.get("model"), f"{key}.allocation.model", IPS_PROFILES, required=True)
    sleeves = allocation.get("sleeves")
    if not isinstance(sleeves, list) or not sleeves:
        _fail(f"{key}.allocation.sleeves", "must be a nonempty list of {id, name, target, min, max}")
    total, seen = 0.0, set()
    for index, sleeve in enumerate(sleeves):
        path = f"{key}.allocation.sleeves[{index}]"
        if not isinstance(sleeve, dict):
            _fail(path, "must be an object")
        if not isinstance(sleeve.get("id"), str) or not _ID.match(sleeve["id"]) or sleeve["id"] in seen:
            _fail(f"{path}.id", "must be a unique lowercase slug")
        seen.add(sleeve["id"])
        _text(sleeve.get("name"), f"{path}.name", required=True, limit=80)
        low, target, high = (_share(sleeve.get(n), f"{path}.{n}") for n in ("min", "target", "max"))
        if not low <= target <= high:
            _fail(path, "needs min <= target <= max")
        total += target
    if abs(total - 1) > 0.001:
        _fail(f"{key}.allocation.sleeves", f"targets must sum to 1 (they sum to {total:g})")
    constraints = value.get("constraints")
    if not isinstance(constraints, dict):
        _fail(f"{key}.constraints", "must be an object")
    limit = (constraints.get("concentration") or {}).get("limit")
    if limit is not None and _share(limit, f"{key}.constraints.concentration.limit") <= 0:
        _fail(f"{key}.constraints.concentration.limit", "must be above 0")
    leverage = constraints.get("leverage") or {}
    _bool(leverage.get("allowed"), f"{key}.constraints.leverage.allowed")
    rebalancing = value.get("rebalancing")
    if not isinstance(rebalancing, dict):
        _fail(f"{key}.rebalancing", "must be {absolute_band, relative_band}")
    for name in ("absolute_band", "relative_band"):
        if _share(rebalancing.get(name), f"{key}.rebalancing.{name}") <= 0:
            _fail(f"{key}.rebalancing.{name}", "must be above 0")
    review = value.get("review")
    if not isinstance(review, dict):
        _fail(f"{key}.review", "must be {cadence, next_review}")
    _enum(review.get("cadence"), f"{key}.review.cadence", IPS_CADENCES, required=True)
    _iso_date(review.get("next_review"), f"{key}.review.next_review")
    for name in ("objectives", "buckets", "missing"):
        if value.get(name) is not None and not isinstance(value[name], list):
            _fail(f"{key}.{name}", "must be a list")
    for name in ("risk", "liquidity", "return_requirement", "residence", "evidence"):
        if value.get(name) is not None and not isinstance(value[name], dict):
            _fail(f"{key}.{name}", "must be an object")


_CIK = re.compile(r"^\d{10}$")


def _follow(value: dict, key: str) -> None:
    _object(value, key, {"cik", "name", "since", "notify", "mirror", "note_text"})
    cik = key.split(".", 1)[1]
    if not _CIK.match(cik):
        _fail(key, "must be follow.<10-digit CIK>, e.g. follow.0002045724")
    if value.get("cik") != cik:
        _fail(f"{key}.cik", f"must be {cik!r}, the CIK in the key")
    _text(value.get("name"), f"{key}.name", required=True, limit=120)
    _iso_date(value.get("since"), f"{key}.since")
    _bool(value.get("notify"), f"{key}.notify")
    _text(value.get("note_text"), f"{key}.note_text", limit=400)
    mirror = value.get("mirror")
    if mirror is not None:
        _object(mirror, f"{key}.mirror", {"sleeve_amount", "currency", "top_n"})
        _number(mirror.get("sleeve_amount"), f"{key}.mirror.sleeve_amount")
        _currency(mirror.get("currency"), f"{key}.mirror.currency")
        top_n = mirror.get("top_n")
        if top_n is not None and (isinstance(top_n, bool) or not isinstance(top_n, int) or top_n < 1):
            _fail(f"{key}.mirror.top_n", "must be a whole number from 1")


_PAYMENT_WORDS = re.compile(r"(^|[_-])(payment|pago|mensualidad)s?($|[_-])")


def _constraint(value: Any, key: str) -> None:
    """A constraint is a condition, not money: a debt payment belongs on its liability."""
    rest = key.partition(".")[2]
    amount = isinstance(value, dict) and (value.get("payment") is not None or value.get("amount") is not None
                                          or re.search(r"\d", str(value.get("text") or "")))
    if (_PAYMENT_WORDS.search(rest) and amount) or (isinstance(value, dict) and value.get("payment") is not None):
        _fail(key, "is a debt payment: save it on the debt as liability.<id> {payment, payment_frequency} "
                   "(merge=true), not as a constraint")


def _validator(key: str) -> Callable[[Any, str], None] | None:
    if key == "policy.ips":
        return _policy_ips
    if key == "client.profile":
        return _profile
    if key == "spending.monthly":
        return _spending
    if key == "goals":
        return _goals
    if key == "reserve":
        return _reserve
    if key == "onboarding":
        return _onboarding
    if key == "preference.risk":
        return _risk
    head, _, rest = key.partition(".")
    if not rest or "." in rest:
        return None
    if head == "income" and key not in LEGACY_KEYS:
        return _income
    return {"cash": _cash, "liability": _liability, "investment": _investment, "thread": _thread,
            "follow": _follow, "constraint": _constraint}.get(head)


def out_of_range(value: Any, path: str = "value") -> str | None:
    """The first number anywhere in ``value`` that no reader can handle, named by its path.

    Applies to every fact, canonical or not (statement records, legacy shapes):
    a nonfinite number or one of ``MAX_AMOUNT`` or more in absolute value.
    """
    if isinstance(value, bool) or value is None or isinstance(value, str):
        return None
    if isinstance(value, (int, float)):
        if isinstance(value, float) and not math.isfinite(value):
            return f"{path} is not a finite number"
        return f"{path} is too large: numbers must be below {MAX_AMOUNT:,} (check for a typo)" \
            if abs(value) >= MAX_AMOUNT else None
    if isinstance(value, dict):
        items = value.items()
    elif isinstance(value, list):
        items = ((f"[{i}]", v) for i, v in enumerate(value))
    else:
        return None
    for name, item in items:
        found = out_of_range(item, f"{path}{name}" if str(name).startswith("[") else f"{path}.{name}")
        if found:
            return found
    return None


def validate(key: str, value: Any) -> list[str]:
    """Raise ``SchemaError`` for a canonical key whose value breaks the schema.

    Returns warnings (for legacy shapes that still work but should move).
    ``None`` (a retraction) is always valid.  Statement records written by
    ingest (they carry ``proposal_id``) are read by adapters, not validated here.
    """
    if value is None:
        return []
    if isinstance(value, dict) and "proposal_id" in value:
        return []
    if key.startswith("income.") and key.count(".") == 1 and isinstance(value, dict) and "income" in value:
        return []
    check = _validator(key)
    if check is not None:
        if check not in (_goals, _risk, _constraint) and not isinstance(value, dict):
            _fail(key, "must be an object")
        check(value, key)
        return []
    warnings = []
    if key == "plan.resources" and isinstance(value, dict) and any(
            isinstance(value.get(n), list) for n in ("cash", "debts", "investments")):
        warnings.append("plan.resources cash/debts/investments lists are a legacy shape; save each as "
                        "cash.<id>, liability.<id> or investment.<id> instead")
    if key == "income.schedule" and isinstance(value, dict) and isinstance(value.get("items"), list):
        warnings.append("income.schedule items are a legacy shape; save each income as income.<id> instead")
    return warnings


__all__ = ["SCHEMA", "SchemaError", "validate", "MAX_AMOUNT", "out_of_range", "country_code", "COUNTRIES", "ONBOARDING_STEPS", "LANGUAGES", "FREQUENCIES", "INCOME_KINDS",
           "LIABILITY_KINDS", "GOAL_ACTIONS", "THREAD_KINDS", "THREAD_STATUSES", "DROP_REACTIONS", "EXPERIENCE"]
