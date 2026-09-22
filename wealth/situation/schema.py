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
TITLINGS = ("individual", "joint", "mancomunada", "fideicomiso", "trust")
RELATIONSHIPS = ("spouse", "partner", "child", "grandchild", "parent", "sibling", "other_relative", "friend",
                 "ex_spouse", "trust", "estate", "charity", "other")
PLAN_TYPES = ("401k", "403b", "457b", "ira", "roth_ira", "sep_ira", "simple_ira", "afore", "ppr", "hsa", "529", "other")
INSURANCE_KINDS = ("life", "accident", "disability", "other")
PROPERTY_KINDS = ("home", "land", "rental", "commercial", "other")
WILL_KINDS = ("publico_abierto", "publico_cerrado", "olografo", "simplificado", "us_will", "other")
MARITAL_STATUSES = ("single", "married", "free_union", "divorced", "widowed")
MARITAL_REGIMES = ("sociedad_conyugal", "separacion_de_bienes", "community_property", "separate_property")
GUARDIAN_DOCUMENTS = ("will", "separate", "none")
_BENEFICIARY_DOC = ("list of {name, relationship?: " + "|".join(RELATIONSHIPS) + ", share?: 0-1, contingent?: "
                    "true for a backup, minor?, birth_year?, deceased?, via_trust?: paid to a trust or custodian "
                    "for them}; [] means they said there are none; leave it out when unknown")
DESIGNATION_DOC = {
    "beneficiaries?": _BENEFICIARY_DOC,
    "designation_date?": "YYYY-MM-DD the beneficiaries were last named or changed",
    "titling?": "|".join(TITLINGS) + " (mancomunada is a Mexican joint account: not a beneficiary)",
    "co_owners?": "names of the other holders of a joint or mancomunada account",
    "owner_share?": "0-1 of the balance that is the person's (default: split equally with co_owners)",
    "country?": "ISO-2 where the account or asset is (MX, US)",
}

SCHEMA: dict[str, dict[str, str]] = {
    "client.profile": {
        "name?": "what the person wants to be called",
        "birth_year?": "YYYY (not age, so it stays true): 'tengo 34 años' is the Date's year minus 34",
        "birth_year_approximate?": "true when birth_year comes from a stated age",
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
        "essential?": "number", "discretionary?": "number",
        "total?": "number (at least one of the three; amount is read as total)",
        "currency": "ISO 4217", "approximate?": "true|false",
        "partial?": "true when essential (or discretionary) is only the items named (rent alone); a total is "
                    "always the whole",
        "components?": "the items the figure covers in their words ([\"renta\"]); a list means partial",
    },
    "cash.<id>": {
        "amount": "number (omit only with balance_unknown: true)", "currency": "ISO 4217",
        "institution?": "bank or fintech name", "balance_unknown?": "true when they have it but the amount is not known",
        "purpose?": "reserve|general|goal:<goal id>", "liquid?": "true (default for cash) | false",
        "name?": "their words", "approximate?": "true|false",
        "annual_rate?": "decimal yield they stated (0.15 for 15%)",
    },
    "liability.<id>": {
        "kind": "|".join(LIABILITY_KINDS), "balance": "number owed", "currency": "ISO 4217",
        "annual_rate?": "decimal (0.45 for '45%'; annual unless they say monthly)", "payment?": "number per payment_frequency",
        "payment_frequency?": "|".join(PAYMENT_FREQUENCIES), "remaining_term_months?": "integer",
        "maturity?": "YYYY-MM-DD", "lender?": "name", "in_spending?": "true if the payment is inside spending.monthly",
        "approximate?": "true|false",
        "cat?": "decimal: the Banxico CAT a Mexican statement quotes (0.60), besides the tasa in annual_rate",
        "minimum_payment?": "card rule {percent_of_balance, plus_interest?, floor? | percent_of_limit + credit_limit}",
        "iva_on_interest?": "decimal IVA charged on interest (0.16), or false",
        "denomination?": "VSM|UMA|MXN (an Infonavit/Fovissste credit in units; lender names the institution)",
        "balance_units?": "number of VSM/UMA owed", "monthly_payment_units?": "VSM/UMA paid a month",
        "unit_value_mxn?": "monthly peso value of one unit", "unit_growth_annual?": "decimal yearly update of the unit",
        "months_paid?": "integer months already paid (30-year liberation)",
        "original_principal?": "number first borrowed (US $750,000 mortgage-interest limit)",
    },
    "investment.<id>": {
        "amount": "number (a stated balance; statements replace it)", "currency": "ISO 4217",
        "institution?": "name", "kind?": "brokerage|retirement|afore|fund|other", "approximate?": "true|false",
        "annual_rate?": "decimal yield or return they stated (0.11 for CETES at 11%)",
        "purpose?": "reserve|general|goal:<goal id> (only as the person stated it)",
        "liquidity_days?": "days to get the money out (1 daily, 28 CETES at 28 days); counts toward the reserve "
                           "when 31 or less",
        "plan_type?": "|".join(PLAN_TYPES) + " for retirement accounts (a 401(k) has spousal-consent rules)",
    },
    "insurance.<id>": {
        "kind": "|".join(INSURANCE_KINDS), "coverage?": "number paid at death (suma asegurada)",
        "currency?": "ISO 4217 (required with coverage)", "insurer?": "name",
        "employer_group?": "true for a work policy (it usually ends with the job)", "name?": "their words",
        "approximate?": "true|false", "country?": "ISO-2",
    },
    "property.<id>": {
        "kind": "|".join(PROPERTY_KINDS), "value?": "number they think it is worth",
        "currency?": "ISO 4217 (required with value)", "name?": "their words", "approximate?": "true|false",
        "country?": "ISO-2",
    },
    "estate.designation.<slug>": {
        "note": "who receives one account, policy or property at death, kept apart from its balance so saving a "
                "beneficiary never refreshes a figure; <slug> is the account key with '.' as '-' "
                "(investment.gbm -> estate.designation.investment-gbm)",
        "account": "the key it applies to: cash.<id>, investment.<id>, insurance.<id>, property.<id> or "
                   "account.<id> (a statement)",
        **DESIGNATION_DOC,
        "plan_type?": "|".join(PLAN_TYPES) + " when the account itself does not say",
        "spousal_consent?": "true when the spouse signed consent to another 401(k) beneficiary (ERISA)",
        "marital_property?": "false when it was theirs before the marriage, inherited or a gift (not gananciales)",
    },
    "estate.will": {
        "exists": "true|false (leave the fact out when they do not know)", "date?": "YYYY-MM-DD signed",
        "notaria?": "notaría or attorney (number and city, no address)", "jurisdiction?": "state or country",
        "kind?": "|".join(WILL_KINDS), "heirs?": "list of {name, relationship?, share?} as they told you",
        "executor?": "albacea or executor name", "guardian_named?": "true when the will names a tutor for minors",
    },
    "estate.guardianship": {
        "guardian?": "name of the tutor/guardian for minor children", "alternate?": "name",
        "documented?": "|".join(GUARDIAN_DOCUMENTS) + " (where the choice is written)",
    },
    "estate.family": {
        "marital_status?": "|".join(MARITAL_STATUSES), "marriage_date?": "YYYY-MM-DD",
        "marital_regime?": "|".join(MARITAL_REGIMES) + " (sociedad conyugal: half of what was acquired in the "
                           "marriage is already the spouse's)",
        "spouse?": "name", "divorce_date?": "YYYY-MM-DD",
        "spouse_assets?": "{amount, currency}: what the spouse owns in their own name (CCF Art. 1624); 0 when none",
        "ex_spouses?": "list of names", "children?": "list of {name?, birth_date? | birth_year?, minor?}; the name is "
                                                     "optional ({\"minor\": true} counts a child)",
        "parents_living?": "0-2", "deceased?": "list of names of people who died (to catch stale beneficiaries)",
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
    "tax.<YYYY>": {
        "note": "the person's stated tax facts for one year (read by tax_pack); merge by field",
        "jurisdiction?": "list of MX|US (default: the profile's tax residence, plus US for a US person)",
        "account_countries?": "{ledger account id: ISO-2} when a statement does not say where an account is",
        "mx?": "{article_129_loss_carryforwards: [{origin_year, available_updated_mxn, updated_through}] ([] = none), "
               "total_income_mxn, accumulable_income_mxn, taxable_income_before_mxn|marginal_rate, "
               "deductions {medical_mxn, insurance_premiums_mxn, funeral_mxn, donations_mxn, tuition_mxn, "
               "school_transport_mxn, ppr_mxn, art185_mxn, mortgage {...}}, aguinaldo_mxn, ptu_mxn, "
               "sic_listed {instrument: true|false}, w8ben_on_file, inpc {\"YYYY-MM\": value}, inpc_source}",
        "us?": "{filing_status, capital_loss_carryover {short_term, long_term} (0 = none), ira_contributions_usd, "
               "roth_contributions_usd, rmd_taken_usd, ira_prior_year_end_balance_usd, "
               "treasury_rate_per_usd {MXN: rate, source}}",
    },
    "constancia.<id>": {
        "note": "an institution's annual tax document (constancia, 1099, 5498) as printed; the source of truth in "
                "tax_pack. Upload the PDF with wealth_ingest action=file instead of typing it: confirming saves "
                "it with source.kind=document",
        "tax_year": "YYYY", "institution": "GBM, BBVA, Charles Schwab...", "account_id?": "ledger account id",
        "currency?": "ISO 4217", "issued_on?": "YYYY-MM-DD",
        "enajenacion?": "{gain, loss, net} Art. 129 share sales (MXN)",
        "intereses?": "{nominal, real, real_loss, isr_withheld} (MXN)",
        "dividendos?": "{domestic_gross, foreign_gross, isr_withheld, isr_creditable} (MXN)",
        "form_1099_b?": "{short_term_gain, long_term_gain, wash_sale_disallowed} (USD)",
        "form_1099_div?": "{ordinary, qualified, capital_gain_distributions, foreign_tax_paid} (USD)",
        "form_1099_int?": "{interest, foreign_tax_paid} (USD)",
        "form_5498?": "{ira_contributions, rollover_contributions, roth_conversion, recharacterized, fair_market_value, "
                      "sep_contributions, simple_contributions, roth_contributions, rmd_next_year} (USD)",
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
    _bool(value.get("birth_year_approximate"), f"{key}.birth_year_approximate")
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
    _object(value, key, {"essential", "discretionary", "total", "currency", "approximate", "note", "partial",
                         "components"})
    for name in ("essential", "discretionary", "total"):
        _number(value.get(name), f"{key}.{name}", required=False)
    if all(value.get(n) is None for n in ("essential", "discretionary", "total")):
        _fail(key, "needs total or essential (monthly amounts); leave the fact out if unknown")
    _currency(value.get("currency"), f"{key}.currency")
    _bool(value.get("approximate"), f"{key}.approximate")
    _text(value.get("note"), f"{key}.note")
    _bool(value.get("partial"), f"{key}.partial")
    components = value.get("components")
    if components is not None and (not isinstance(components, list) or not all(
            isinstance(c, str) and c.strip() and len(c) <= 80 for c in components)):
        _fail(f"{key}.components", "must be a list of short item names such as [\"renta\"]")


def _cash(value: dict, key: str) -> None:
    _object(value, key, {"amount", "currency", "institution", "purpose", "liquid", "name", "approximate", "note",
                         "balance_unknown", "annual_rate", *DESIGNATION_FIELDS})
    _designation(value, key)
    _rate(value.get("annual_rate"), f"{key}.annual_rate")
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
                         "remaining_term_months", "maturity", "lender", "name", "in_spending", "approximate", "note",
                         *DEBT_ENGINE_FIELDS})
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
    _rate(value.get("cat"), f"{key}.cat")
    _rate(value.get("unit_growth_annual"), f"{key}.unit_growth_annual")
    for field in ("balance_units", "monthly_payment_units", "unit_value_mxn", "original_principal"):
        _number(value.get(field), f"{key}.{field}", required=False)
    iva = value.get("iva_on_interest")
    if iva is not None and iva is not False:
        _rate(iva, f"{key}.iva_on_interest")
    _enum(value.get("denomination"), f"{key}.denomination", ("VSM", "UMA", "MXN"))
    paid = value.get("months_paid")
    if paid is not None and (isinstance(paid, bool) or not isinstance(paid, int) or not 0 <= paid <= MAX_MONTHS):
        _fail(f"{key}.months_paid", "must be a whole number of months")
    rule = value.get("minimum_payment")
    if rule is not None:
        _object(rule, f"{key}.minimum_payment", {"percent_of_balance", "plus_interest", "floor", "percent_of_limit",
                                                  "credit_limit"})
        _rate(rule.get("percent_of_balance"), f"{key}.minimum_payment.percent_of_balance")
        _rate(rule.get("percent_of_limit"), f"{key}.minimum_payment.percent_of_limit")
        for field in ("floor", "credit_limit"):
            _number(rule.get(field), f"{key}.minimum_payment.{field}", required=False)
        _bool(rule.get("plus_interest"), f"{key}.minimum_payment.plus_interest")


# Optional liability fields only the debt engine (wealth/debt.py) reads.
DEBT_ENGINE_FIELDS = ("cat", "minimum_payment", "iva_on_interest", "denomination", "balance_units", "monthly_payment_units",
                      "unit_value_mxn", "unit_growth_annual", "months_paid", "original_principal",
                      "origination_date", "start_date", "liberation_eligible", "update_month", "credit_limit")


def _purpose(value: Any, path: str) -> None:
    if value is not None and value not in ("reserve", "general") and not (
            isinstance(value, str) and value.startswith("goal:") and _ID.match(value[5:])):
        _fail(path, "must be reserve, general or goal:<goal id>")


def _investment(value: dict, key: str) -> None:
    _object(value, key, {"amount", "currency", "institution", "kind", "name", "approximate", "note",
                         "balance_unknown", "purpose", "liquidity_days", "annual_rate", "plan_type", "spousal_consent",
                         *DESIGNATION_FIELDS})
    _designation(value, key)
    _enum(value.get("plan_type"), f"{key}.plan_type", PLAN_TYPES)
    _bool(value.get("spousal_consent"), f"{key}.spousal_consent")
    _rate(value.get("annual_rate"), f"{key}.annual_rate")
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


# ------------------------------------------------------------------ estate: designations, wills, family

DESIGNATION_FIELDS = ("beneficiaries", "designation_date", "titling", "co_owners", "owner_share", "country")
_BENEFICIARY_FIELDS = {"name", "person", "relationship", "share", "contingent", "minor", "birth_year", "deceased",
                       "via_trust", "note"}


def _names(value: Any, path: str) -> None:
    if value is not None and (not isinstance(value, list) or not all(
            isinstance(v, str) and v.strip() and len(v) <= 80 for v in value)):
        _fail(path, "must be a list of names")


def _year_of_birth(value: Any, path: str) -> None:
    if value is not None and (isinstance(value, bool) or not isinstance(value, int) or not 1900 <= value <= 2100):
        _fail(path, "must be a four-digit year")


def _designation(value: dict, key: str) -> None:
    """Beneficiary designation fields shared by accounts, policies and property."""
    beneficiaries = value.get("beneficiaries")
    if beneficiaries is not None:
        if not isinstance(beneficiaries, list):
            _fail(f"{key}.beneficiaries", "must be a list ([] when they said there are none)")
        for index, person in enumerate(beneficiaries):
            path = f"{key}.beneficiaries[{index}]"
            _object(person, path, _BENEFICIARY_FIELDS)
            if person.get("name") is None and person.get("person") is None:
                _fail(f"{path}.name", "is required (the name they used, e.g. 'Ana' or 'mi esposa')")
            _text(person.get("name"), f"{path}.name", limit=80)
            _text(person.get("person"), f"{path}.person", limit=64)
            _enum(person.get("relationship"), f"{path}.relationship", RELATIONSHIPS)
            if person.get("share") is not None:
                _share(person["share"], f"{path}.share")
            for flag in ("contingent", "minor", "deceased", "via_trust"):
                _bool(person.get(flag), f"{path}.{flag}")
            _year_of_birth(person.get("birth_year"), f"{path}.birth_year")
            _text(person.get("note"), f"{path}.note")
    _iso_date(value.get("designation_date"), f"{key}.designation_date")
    _enum(value.get("titling"), f"{key}.titling", TITLINGS)
    _names(value.get("co_owners"), f"{key}.co_owners")
    if value.get("owner_share") is not None:
        _share(value["owner_share"], f"{key}.owner_share")
    country = value.get("country")
    if country is not None and country_code(country) is None:
        _fail(f"{key}.country", "must be an ISO-2 country code such as MX or US")


_DESIGNATION_TARGET = re.compile(r"^(cash|investment|insurance|property|account)\.[A-Za-z0-9][A-Za-z0-9_.-]{0,119}$")


def designation_key(account: str) -> str:
    """``estate.designation.<slug>`` for an account key (``investment.gbm`` -> ``estate.designation.investment-gbm``)."""
    return "estate.designation." + account.replace(".", "-")


def _estate_designation(value: dict, key: str) -> None:
    _object(value, key, {"account", *DESIGNATION_FIELDS, "plan_type", "spousal_consent", "marital_property", "note"})
    account = value.get("account")
    if not isinstance(account, str) or not _DESIGNATION_TARGET.match(account):
        _fail(f"{key}.account", "is required: the key it applies to, such as investment.gbm or account.gbm-1")
    if key != designation_key(account):
        _fail(key, f"must be {designation_key(account)!r} for account {account!r}")
    _designation(value, key)
    _enum(value.get("plan_type"), f"{key}.plan_type", PLAN_TYPES)
    _bool(value.get("spousal_consent"), f"{key}.spousal_consent")
    _bool(value.get("marital_property"), f"{key}.marital_property")
    _text(value.get("note"), f"{key}.note")


def _insurance(value: dict, key: str) -> None:
    _object(value, key, {"kind", "coverage", "currency", "insurer", "employer_group", "name", "approximate", "note",
                         "beneficiaries", "designation_date", "country"})
    _enum(value.get("kind"), f"{key}.kind", INSURANCE_KINDS, required=True)
    _number(value.get("coverage"), f"{key}.coverage", required=False)
    _currency(value.get("currency"), f"{key}.currency", required=value.get("coverage") is not None)
    _text(value.get("insurer"), f"{key}.insurer", limit=80)
    _bool(value.get("employer_group"), f"{key}.employer_group")
    _text(value.get("name"), f"{key}.name")
    _bool(value.get("approximate"), f"{key}.approximate")
    _text(value.get("note"), f"{key}.note")
    _designation(value, key)


def _property(value: dict, key: str) -> None:
    _object(value, key, {"kind", "value", "currency", "name", "approximate", "note", *DESIGNATION_FIELDS})
    _enum(value.get("kind"), f"{key}.kind", PROPERTY_KINDS, required=True)
    _number(value.get("value"), f"{key}.value", required=False)
    _currency(value.get("currency"), f"{key}.currency", required=value.get("value") is not None)
    _text(value.get("name"), f"{key}.name")
    _bool(value.get("approximate"), f"{key}.approximate")
    _text(value.get("note"), f"{key}.note")
    _designation(value, key)


def _will(value: dict, key: str) -> None:
    _object(value, key, {"exists", "date", "notaria", "jurisdiction", "kind", "heirs", "executor", "guardian_named",
                         "note"})
    if not isinstance(value.get("exists"), bool):
        _fail(f"{key}.exists", "is required: true or false (leave the fact out when they do not know)")
    _iso_date(value.get("date"), f"{key}.date")
    _text(value.get("notaria"), f"{key}.notaria", limit=120)
    _text(value.get("jurisdiction"), f"{key}.jurisdiction", limit=80)
    _enum(value.get("kind"), f"{key}.kind", WILL_KINDS)
    _text(value.get("executor"), f"{key}.executor", limit=80)
    _bool(value.get("guardian_named"), f"{key}.guardian_named")
    _text(value.get("note"), f"{key}.note")
    heirs = value.get("heirs")
    if heirs is not None:
        if not isinstance(heirs, list):
            _fail(f"{key}.heirs", "must be a list of {name, relationship?, share?}")
        for index, heir in enumerate(heirs):
            path = f"{key}.heirs[{index}]"
            _object(heir, path, {"name", "relationship", "share", "note"})
            _text(heir.get("name"), f"{path}.name", required=True, limit=80)
            _enum(heir.get("relationship"), f"{path}.relationship", RELATIONSHIPS)
            if heir.get("share") is not None:
                _share(heir["share"], f"{path}.share")


def _guardianship(value: dict, key: str) -> None:
    _object(value, key, {"guardian", "alternate", "documented", "note"})
    _text(value.get("guardian"), f"{key}.guardian", limit=80)
    _text(value.get("alternate"), f"{key}.alternate", limit=80)
    _enum(value.get("documented"), f"{key}.documented", GUARDIAN_DOCUMENTS)
    _text(value.get("note"), f"{key}.note")


def _family(value: dict, key: str) -> None:
    _object(value, key, {"marital_status", "marriage_date", "marital_regime", "spouse", "divorce_date", "ex_spouses",
                         "children", "parents_living", "deceased", "note", "spouse_assets"})
    assets = value.get("spouse_assets")
    if assets is not None:
        _object(assets, f"{key}.spouse_assets", {"amount", "currency", "approximate"})
        _number(assets.get("amount"), f"{key}.spouse_assets.amount")
        _currency(assets.get("currency"), f"{key}.spouse_assets.currency")
        _bool(assets.get("approximate"), f"{key}.spouse_assets.approximate")
    _enum(value.get("marital_status"), f"{key}.marital_status", MARITAL_STATUSES)
    _enum(value.get("marital_regime"), f"{key}.marital_regime", MARITAL_REGIMES)
    _iso_date(value.get("marriage_date"), f"{key}.marriage_date")
    _iso_date(value.get("divorce_date"), f"{key}.divorce_date")
    _text(value.get("spouse"), f"{key}.spouse", limit=80)
    _names(value.get("ex_spouses"), f"{key}.ex_spouses")
    _names(value.get("deceased"), f"{key}.deceased")
    _text(value.get("note"), f"{key}.note")
    parents = value.get("parents_living")
    if parents is not None and (isinstance(parents, bool) or not isinstance(parents, int) or not 0 <= parents <= 2):
        _fail(f"{key}.parents_living", "must be 0, 1 or 2")
    children = value.get("children")
    if children is not None:
        if not isinstance(children, list):
            _fail(f"{key}.children", 'must be a list of {name?, birth_date? | birth_year?, minor?}, e.g. '
                  '[{"name": "Sofía", "birth_year": 2017}, {"minor": true}]')
        for index, child in enumerate(children):
            path = f"{key}.children[{index}]"
            _object(child, path, {"name", "birth_date", "birth_year", "minor"})
            _text(child.get("name"), f"{path}.name", limit=80)
            _iso_date(child.get("birth_date"), f"{path}.birth_date")
            _year_of_birth(child.get("birth_year"), f"{path}.birth_year")
            _bool(child.get("minor"), f"{path}.minor")


ESTATE_VALIDATORS: dict[str, Callable[[Any, str], None]] = {
    "estate.will": _will, "estate.guardianship": _guardianship, "estate.family": _family}


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


CONSTANCIA_BLOCKS = {
    "enajenacion": ("gain", "loss", "net", "isr_withheld"),
    "intereses": ("nominal", "real", "real_loss", "isr_withheld"),
    "dividendos": ("domestic_gross", "foreign_gross", "isr_withheld", "isr_creditable", "foreign_tax_withheld"),
    "form_1099_b": ("proceeds", "cost_basis", "short_term_gain", "long_term_gain", "wash_sale_disallowed"),
    "form_1099_div": ("ordinary", "qualified", "capital_gain_distributions", "foreign_tax_paid"),
    "form_1099_int": ("interest", "foreign_tax_paid"),
    "form_5498": ("ira_contributions", "rollover_contributions", "roth_conversion", "recharacterized",
                  "fair_market_value", "sep_contributions", "simple_contributions", "roth_contributions",
                  "rmd_next_year"),
}


def _tax_year_value(value: Any, path: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not 2000 <= value <= 2100:
        _fail(path, f"must be a four-digit year, not {_shown(value)}")


def _constancia(value: dict, key: str) -> None:
    fields = {"tax_year", "institution", "account_id", "currency", "issued_on", "note", *CONSTANCIA_BLOCKS}
    _object(value, key, fields)
    _tax_year_value(value.get("tax_year"), f"{key}.tax_year")
    _text(value.get("institution"), f"{key}.institution", required=True)
    _text(value.get("account_id"), f"{key}.account_id")
    _currency(value.get("currency"), f"{key}.currency", required=False)
    _iso_date(value.get("issued_on"), f"{key}.issued_on")
    blocks = [name for name in CONSTANCIA_BLOCKS if value.get(name) is not None]
    if not blocks:
        _fail(key, "needs at least one block: " + ", ".join(CONSTANCIA_BLOCKS))
    for name in blocks:
        block = _object(value[name], f"{key}.{name}", set(CONSTANCIA_BLOCKS[name]))
        for field, number in block.items():
            _number(number, f"{key}.{name}.{field}", required=False, minimum=None)


def _tax_facts(value: dict, key: str) -> None:
    _tax_year_value(int(key.partition(".")[2]), key)
    _object(value, key, {"jurisdiction", "account_countries", "mx", "us", "note"})
    jurisdiction = value.get("jurisdiction")
    if jurisdiction is not None and (not isinstance(jurisdiction, list)
                                     or any(j not in ("MX", "US") for j in jurisdiction)):
        _fail(f"{key}.jurisdiction", "must be a list of MX and/or US")
    for part in ("mx", "us", "account_countries"):
        if value.get(part) is not None and not isinstance(value[part], dict):
            _fail(f"{key}.{part}", "must be an object")
    carries = (value.get("mx") or {}).get("article_129_loss_carryforwards")
    if carries is not None:
        if not isinstance(carries, list):
            _fail(f"{key}.mx.article_129_loss_carryforwards", "must be a list ([] when there are none)")
        for index, item in enumerate(carries):
            path = f"{key}.mx.article_129_loss_carryforwards[{index}]"
            item = _object(item, path, {"origin_year", "available_updated_mxn", "updated_through"})
            _tax_year_value(item.get("origin_year"), f"{path}.origin_year")
            _number(item.get("available_updated_mxn"), f"{path}.available_updated_mxn")


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
    if key in ESTATE_VALIDATORS:
        return ESTATE_VALIDATORS[key]
    if key.startswith("estate.designation."):
        return _estate_designation
    head, _, rest = key.partition(".")
    if not rest or "." in rest:
        return None
    if head == "tax":
        return _tax_facts if re.fullmatch(r"\d{4}", rest) else None
    if head == "income" and key not in LEGACY_KEYS:
        return _income
    return {"cash": _cash, "liability": _liability, "investment": _investment, "thread": _thread,
            "follow": _follow, "constraint": _constraint, "constancia": _constancia, "insurance": _insurance,
            "property": _property}.get(head)


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


ALIASES: dict[str, dict[str, str]] = {"spending.monthly": {"amount": "total"}}
"""Field names a model writes naturally, read as the canonical field (only when that field is absent)."""


def normalize(key: str, value: Any) -> tuple[Any, list[str]]:
    """Rename aliased fields to their canonical names; returns the value and notes for the receipt."""
    if key == "client.profile" and isinstance(value, dict) and value.get("residence") is None \
            and country_code(value.get("country")) is not None:
        # {"country": "MX"} is how people say where they live: it is the residence country.
        out = {k: v for k, v in value.items() if k != "country"}
        out["residence"] = {"country": country_code(value["country"])}
        return out, ["client.profile.country was saved as client.profile.residence.country"]
    if key == "estate.family" and isinstance(value, dict) and isinstance(value.get("children"), list) and any(
            isinstance(c, dict) and "relationship" in c for c in value["children"]):
        # Everyone in children is a child: a relationship field there says nothing more.
        children = [{k: v for k, v in c.items() if k != "relationship"} if isinstance(c, dict) else c
                    for c in value["children"]]
        return {**value, "children": children}, []
    aliases = ALIASES.get(key)
    if not aliases or not isinstance(value, dict):
        return value, []
    out, notes = dict(value), []
    for alias, canonical in aliases.items():
        if alias in out and canonical not in out:
            out[canonical] = out.pop(alias)
            notes.append(f"{key}.{alias} was saved as {key}.{canonical}")
    return out, notes


EXAMPLES: dict[str, Any] = {
    "client.profile": {"name": "Ana", "birth_year": 1990, "residence": {"country": "MX", "region": "CDMX"},
                       "language": "es"},
    "income.": {"amount": 70000, "currency": "MXN", "frequency": "monthly", "net": True},
    "spending.monthly": {"total": 45000, "currency": "MXN"},
    "cash.": {"amount": 150000, "currency": "MXN", "institution": "Nu"},
    "liability.": {"kind": "card", "balance": 25000, "currency": "MXN", "annual_rate": 0.42},
    "investment.": {"amount": 200000, "currency": "MXN", "institution": "GBM"},
    "estate.family": {"marital_status": "married", "spouse": "Luis", "marital_regime": "sociedad_conyugal",
                      "children": [{"name": "Mateo", "birth_year": 2018}, {"minor": True}]},
    "estate.will": {"exists": False},
    "estate.guardianship": {"guardian": "Luis"},
    "goals": [{"id": "house-2028", "name": "Casa", "target_amount": 800000, "currency": "MXN",
               "target_date": "2028-12-31"}],
}
"""A valid value per key (or key prefix), quoted in validation errors as the corrected shape."""


def example(key: str) -> Any | None:
    if key in EXAMPLES:
        return EXAMPLES[key]
    head = key.partition(".")[0] + "."
    return EXAMPLES.get(head) if head != key else None


__all__ = ["SCHEMA", "SchemaError", "validate", "normalize", "example", "ALIASES", "MAX_AMOUNT", "out_of_range", "country_code", "COUNTRIES", "ONBOARDING_STEPS", "LANGUAGES", "FREQUENCIES", "INCOME_KINDS",
           "LIABILITY_KINDS", "GOAL_ACTIONS", "TITLINGS", "RELATIONSHIPS", "PLAN_TYPES", "DESIGNATION_FIELDS", "designation_key", "THREAD_KINDS", "THREAD_STATUSES", "DROP_REACTIONS", "EXPERIENCE"]
