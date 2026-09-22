"""Deterministic onboarding: one question per card, typed answers, canonical facts.

The model is not needed to collect a birth year or an income.  Each step has an
id, a prompt in English and Mexican Spanish, an input schema the page renders
(chips, an amount with currency, a year, a count, multi-select with a
per-item amount or upload, or an upload), a condition over the situation and a
writer that turns the answer into canonical facts (source ``user``, confidence
``confirmed``; every value passes ``situation.schema.validate``).

* ``next_step(situation, language)`` -> the next card, or ``None`` when done.
* ``apply(service, client_id, step_id, answer)`` validates, writes the facts and
  the step status in one atomic ``remember`` and returns the next card and the
  running picture.  ``skip`` records a skip.  ``{"unsure": true}`` is a valid
  answer: nothing is written and the item stays unknown.
* ``parse_free_text(card, text)`` reads a typed reply deterministically when it
  is a simple amount, year, count or chip name, and otherwise says the model is
  needed.

A step already known from memory (a statement, an earlier conversation) is
shown prefilled for one-tap confirmation while the setup flow is running, never
asked blank.  Outside the flow (someone who told us everything in chat) a known
step counts as answered: completion comes from the facts, not only from the
onboarding record.  A step that is done, skipped or unsure is never asked again.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Mapping

from . import _common
from .situation.schema import SchemaError, country_code, validate

RESOLVED = ("done", "skipped", "unsure")
HOME_CURRENCY = {"MX": "MXN", "US": "USD"}
SOURCE_REF = "onboarding"
MAX_AMOUNT = Decimal("1e12")


_lang = _common.lang


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _fold(text: str) -> str:
    """Lowercase without accents, for matching typed words to chips."""
    decomposed = unicodedata.normalize("NFD", str(text or "").lower())
    return "".join(ch for ch in decomposed if unicodedata.category(ch) != "Mn").strip()


# ------------------------------------------------------------------ amounts

_CURRENCY_WORDS = [
    (re.compile(r"us\$|\busd\b|\bd[oó]lar(?:es)?\b|\bdollars?\b|\bdls\b|\bbucks\b", re.I), "USD"),
    (re.compile(r"mx\$|\bmxn\b|\bpesos?\b|\bmn\b", re.I), "MXN"),
]
_MULTIPLIERS = {"k": 1_000, "mil": 1_000, "thousand": 1_000, "m": 1_000_000, "mm": 1_000_000, "mdp": 1_000_000,
                "millon": 1_000_000, "millón": 1_000_000, "millones": 1_000_000, "million": 1_000_000,
                "millions": 1_000_000}
_NUMBER = re.compile(
    r"(?<![\w.,])(\d[\d.,]*\d|\d)\s*(millones|millón|millon|millions|million|thousand|mil|mdp|mm|k|m)?(?![a-záéíóúñ])",
    re.I)


def _number(token: str) -> Decimal | None:
    token = token.strip()
    if re.fullmatch(r"\d{1,3}(,\d{3})+(\.\d+)?", token):          # 85,000 or 1,234.50
        token = token.replace(",", "")
    elif re.fullmatch(r"\d{1,3}(\.\d{3})+(,\d{1,2})?", token):    # 85.000 or 1.234,50
        token = token.replace(".", "").replace(",", ".")
    elif re.fullmatch(r"\d+,\d{1,2}", token):                     # 85,5
        token = token.replace(",", ".")
    elif not re.fullmatch(r"\d+(\.\d+)?", token):
        return None
    try:
        return Decimal(token)
    except InvalidOperation:
        return None


def parse_amount(text: str) -> dict | None:
    """``{"amount": 85000, "currency": "MXN"|"USD"|None}`` from "85 mil", "85k", "$85,000", "85,000 pesos".

    Returns ``None`` when there is no amount or more than one (that needs the model).
    """
    raw = str(text or "")
    found = [m for m in _NUMBER.finditer(raw)]
    if len(found) != 1:
        return None
    match = found[0]
    value = _number(match.group(1))
    if value is None:
        return None
    unit = (match.group(2) or "").lower()
    if unit:
        value *= _MULTIPLIERS[unit]
    if value < 0 or value > MAX_AMOUNT:
        return None
    currency = None
    for pattern, code in _CURRENCY_WORDS:
        if pattern.search(raw):
            currency = code
            break
    amount = int(value) if value == value.to_integral_value() else float(round(value, 2))
    return {"amount": amount, "currency": currency}


def _year(text: str) -> int | None:
    years = re.findall(r"\b(19\d{2}|20[0-3]\d)\b", str(text or ""))
    return int(years[0]) if len(years) == 1 else None


_COUNT_WORDS = {"nadie": 0, "ninguno": 0, "ninguna": 0, "no": 0, "none": 0, "nobody": 0, "no one": 0, "cero": 0,
                "zero": 0, "uno": 1, "una": 1, "one": 1, "dos": 2, "two": 2, "tres": 3, "three": 3,
                "cuatro": 4, "four": 4}


def _count(text: str) -> int | None:
    folded = _fold(text)
    numbers = re.findall(r"\b\d{1,2}\b", folded)
    if len(numbers) == 1:
        return int(numbers[0])
    for word, value in _COUNT_WORDS.items():
        if re.search(rf"\b{re.escape(word)}\b", folded):
            return value
    return None


# ------------------------------------------------------------------ money helpers


def _money(value: Any, default_currency: str | None, path: str, *, required: bool = True) -> dict | None:
    """Accept 85000, "85 mil", {"amount": 85000, "currency": "MXN"}; return a checked amount and currency."""
    if value is None or value == "" or (isinstance(value, dict) and value.get("amount") in (None, "")):
        if required:
            raise ValueError(f"Enter an amount for {path}.")
        return None
    currency = None
    if isinstance(value, dict):
        currency, value = value.get("currency"), value.get("amount")
    if isinstance(value, str):
        parsed = parse_amount(value)
        if parsed is None:
            raise ValueError(f"Enter {path} as a number, such as 85000.")
        value, currency = parsed["amount"], currency or parsed["currency"]
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value or not 0 <= value <= MAX_AMOUNT:
        raise ValueError(f"Enter {path} as a positive number.")
    currency = currency or default_currency
    if not isinstance(currency, str) or not re.fullmatch(r"[A-Z]{3}", currency):
        raise ValueError(f"Choose a currency for {path}.")
    return {"amount": value, "currency": currency}


def _rate(value: Any, path: str) -> float | None:
    """A percent as typed (13 or "13%") -> the decimal the schema stores (0.13)."""
    if value in (None, ""):
        return None
    if isinstance(value, str):
        cleaned = value.replace("%", "").replace(",", ".").strip()
        try:
            value = float(cleaned)
        except ValueError:
            raise ValueError(f"Enter {path} as a percent, such as 13.") from None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 100:
        raise ValueError(f"Enter {path} as a percent, such as 13.")
    return round(value / 100, 6)


def money_text(amount: Any, currency: str | None, home: str | None) -> str:
    """$85,000 in the home currency; US$1,000 or MX$1,000 otherwise."""
    if amount is None:
        return "?"
    number = Decimal(str(amount))
    places = 0 if number == number.to_integral_value() else 2
    text = f"{abs(number):,.{places}f}"
    sign = "−" if number < 0 else ""
    if not currency or currency == home:
        return f"{sign}${text}"
    prefix = {"USD": "US$", "MXN": "MX$"}.get(currency)
    return f"{sign}{prefix}{text}" if prefix else f"{sign}{text} {currency}"


# ------------------------------------------------------------------ step definitions


@dataclass(frozen=True)
class Step:
    id: str
    marks: tuple[str, ...]                        # schema onboarding step names this card settles
    prompt: dict[str, str]
    title: dict[str, str]
    fields: Callable[[str, dict], list[dict]]     # (lang, ctx) -> input schema
    known: Callable[[Mapping[str, Any]], bool]
    prefill: Callable[[Mapping[str, Any], dict], dict | None]
    writer: Callable[[dict, dict], list[tuple[str, Any]]]
    summary: Callable[[dict, str, dict], str]
    condition: Callable[[Mapping[str, Any]], bool] = lambda sit: True
    unsure: bool = True
    upload: bool = False                          # offers "Upload a statement"


def _opt(id_: str, en: str, es: str | None = None) -> dict:
    return {"id": id_, "label": {"en": en, "es": es or en}}


def _labels(options: list[dict], lang: str) -> list[dict]:
    return [{"id": o["id"], "label": o["label"][lang]} for o in options]


def _country(sit: Mapping[str, Any]) -> str | None:
    return ((sit.get("profile") or {}).get("residence") or {}).get("country")


def _context(sit: Mapping[str, Any]) -> dict:
    country = _country(sit)
    home = HOME_CURRENCY.get(country or "", sit.get("currency") or "MXN")
    spending = (sit.get("spending") or {}).get("monthly") if (sit.get("spending") or {}).get("source") != "ledger" else None
    return {"country": country, "home": home, "currencies": ["MXN", "USD"] if home in ("MXN", "USD") else [home, "USD"],
            "spending": spending, "spending_currency": sit.get("currency")}


def _amount_field(name: str, lang: str, ctx: dict, label: dict[str, str] | None = None, *,
                  required: bool = True, currency: str | None = None) -> dict:
    field = {"name": name, "type": "amount", "currency": currency or ctx["home"], "currencies": ctx["currencies"],
             "required": required}
    if label:
        field["label"] = label[lang]
    return field


# -- identity


_COUNTRIES = [_opt("MX", "Mexico", "México"), _opt("US", "United States", "Estados Unidos"),
              _opt("other", "Somewhere else", "Otro país")]
_COUNTRY_NAME = {"es": {"MX": "México", "US": "Estados Unidos", "CA": "Canadá", "ES": "España"},
                 "en": {"MX": "Mexico", "US": "the United States", "CA": "Canada", "ES": "Spain"}}


def _identity_fields(lang: str, ctx: dict) -> list[dict]:
    return [
        {"name": "name", "type": "text", "label": {"en": "Your name", "es": "Tu nombre"}[lang],
         "autocomplete": "given-name", "max": 80, "required": True},
        {"name": "country", "type": "chips", "label": {"en": "Where you live", "es": "Dónde vives"}[lang],
         "options": _labels(_COUNTRIES, lang), "max": 1, "required": True},
        {"name": "region", "type": "text", "label": {"en": "State", "es": "Estado"}[lang], "max": 80,
         "when": {"country": "US"}, "required": False, "autocomplete": "address-level1"},
        {"name": "city", "type": "text", "label": {"en": "City", "es": "Ciudad"}[lang], "max": 80,
         "when": {"country": "MX"}, "required": False, "autocomplete": "address-level2"},
    ]


def _identity_known(sit) -> bool:
    profile = sit.get("profile") or {}
    return bool(profile.get("name")) and bool((profile.get("residence") or {}).get("country"))


def _identity_prefill(sit, ctx) -> dict | None:
    profile = sit.get("profile") or {}
    residence = profile.get("residence") or {}
    out = {}
    if profile.get("name"):
        out["name"] = profile["name"]
    if residence.get("country"):
        out["country"] = residence["country"] if residence["country"] in ("MX", "US") else "other"
    if residence.get("region"):
        out["region"] = residence["region"]
    if residence.get("city"):
        out["city"] = residence["city"]
    return out or None


def _identity_writer(answer: dict, ctx: dict) -> list[tuple[str, Any]]:
    name = str(answer.get("name") or "").strip()
    if not name or len(name) > 80:
        raise ValueError("Enter the name you want to be called.")
    name = " ".join(name.split())
    profile: dict[str, Any] = {"name": name}
    raw_country = answer.get("country")
    country = country_code(raw_country) if raw_country not in (None, "", "other") else None
    if raw_country not in (None, "", "other") and country is None:
        raise ValueError("Choose where you live.")
    if country:
        residence: dict[str, Any] = {"country": country}
        region = " ".join(str(answer.get("region") or "").split())
        if region and country == "US":
            residence["region"] = region[:80]
        city = " ".join(str(answer.get("city") or "").split())
        if city:  # "Ciudad de México" is kept, so "Vives en…" names it
            residence["city"] = city[:80]
        profile["residence"] = residence
    if ctx.get("language") in ("es", "en"):
        profile["language"] = ctx["language"]
    return [("client.profile", profile)]


def _identity_summary(answer: dict, lang: str, ctx: dict) -> str:
    name = " ".join(str(answer.get("name") or "").split())
    country = country_code(answer.get("country")) if answer.get("country") not in (None, "other") else None
    place = _COUNTRY_NAME[lang].get(country or "", country or "")
    city = " ".join(str(answer.get("city") or "").split())
    region = " ".join(str(answer.get("region") or "").split()) if country == "US" else ""
    local = [p for p in (city, region) if p]
    if local and place:
        # "Texas, United States": after a city or state the country reads without its article.
        place = ", ".join([*dict.fromkeys(local), place.removeprefix("the ")])
    if not place:
        return name
    return f"{name}, en {place}" if lang == "es" else f"{name}, in {place}"


# -- about


_DEPENDENTS = [_opt("0", "No one", "Nadie"), _opt("1", "1"), _opt("2", "2"), _opt("3", "3"), _opt("4", "4+", "4 o más")]


def _about_fields(lang: str, ctx: dict) -> list[dict]:
    return [
        {"name": "birth_year", "type": "year", "label": {"en": "Birth year", "es": "Año de nacimiento"}[lang],
         "min": 1900, "max": date.today().year - 14, "required": False},
        {"name": "dependents", "type": "chips", "label": {"en": "Who depends on you", "es": "Quién depende de ti"}[lang],
         "options": _labels(_DEPENDENTS, lang), "max": 1, "required": False},
    ]


def _about_known(sit) -> bool:
    profile = sit.get("profile") or {}
    return profile.get("birth_year") is not None and profile.get("dependents") is not None


def _about_prefill(sit, ctx) -> dict | None:
    profile = sit.get("profile") or {}
    out = {}
    if profile.get("birth_year") is not None:
        out["birth_year"] = profile["birth_year"]
    if profile.get("dependents") is not None:
        out["dependents"] = str(min(int(profile["dependents"]), 4))
    return out or None


def _about_writer(answer: dict, ctx: dict) -> list[tuple[str, Any]]:
    profile: dict[str, Any] = {}
    year = answer.get("birth_year")
    if year not in (None, ""):
        try:
            year = int(str(year).strip())
        except ValueError:
            raise ValueError("Enter a four-digit birth year, such as 1988.") from None
        if not 1900 <= year <= date.today().year - 14:
            raise ValueError("Enter a four-digit birth year, such as 1988.")
        profile["birth_year"] = year
    dependents = answer.get("dependents")
    if dependents not in (None, ""):
        try:
            dependents = int(str(dependents).rstrip("+"))
        except ValueError:
            raise ValueError("Choose how many people depend on you.") from None
        if not 0 <= dependents <= 20:
            raise ValueError("Choose how many people depend on you.")
        profile["dependents"] = dependents
    if not profile:
        raise ValueError("Enter your birth year or who depends on you, or choose Not sure.")
    return [("client.profile", profile)]


def _about_summary(answer: dict, lang: str, ctx: dict) -> str:
    parts = []
    if answer.get("birth_year") not in (None, ""):
        parts.append(f"Naciste en {answer['birth_year']}" if lang == "es" else f"Born {answer['birth_year']}")
    dependents = answer.get("dependents")
    if dependents not in (None, ""):
        n = int(str(dependents).rstrip("+"))
        if n == 0:
            parts.append("nadie depende de ti" if lang == "es" else "no dependents")
        else:
            more = "+" if n >= 4 else ""
            parts.append(f"{n}{more} {'dependiente' if n == 1 else 'dependientes'}" if lang == "es"
                         else f"{n}{more} {'dependent' if n == 1 else 'dependents'}")
    text = " · ".join(parts)
    return text[:1].upper() + text[1:]


# -- income


_EXTRAS_MX = [_opt("aguinaldo", "Aguinaldo"), _opt("bonus", "Bonus", "Bono"), _opt("ptu", "PTU"),
              _opt("rent", "Rent", "Rentas"), _opt("other", "Other", "Otro")]
_EXTRAS_US = [_opt("bonus", "Bonus", "Bono"), _opt("rent", "Rent", "Rentas"), _opt("other", "Other", "Otro")]
_EXTRA_KIND = {"aguinaldo": ("aguinaldo", "annual", 12), "bonus": ("bonus", "annual", None),
               "ptu": ("ptu", "annual", 5), "rent": ("rent", "monthly", None), "other": ("other", "monthly", None)}


def _extras(ctx: dict) -> list[dict]:
    return _EXTRAS_US if ctx.get("country") == "US" else _EXTRAS_MX


def _income_fields(lang: str, ctx: dict) -> list[dict]:
    per = {"aguinaldo": {"en": "Per year", "es": "Al año"}, "bonus": {"en": "Per year", "es": "Al año"},
           "ptu": {"en": "Per year", "es": "Al año"}, "rent": {"en": "Per month", "es": "Al mes"},
           "other": {"en": "Per month", "es": "Al mes"}}
    options = []
    for option in _extras(ctx):
        options.append({"id": option["id"], "label": option["label"][lang],
                        "fields": [_amount_field("amount", lang, ctx, per[option["id"]], required=False)]})
    # Only take-home pay is asked: an aguinaldo, bonus or PTU is recorded when the person mentions it or a
    # statement shows the deposit, never prompted "just in case". (Answers carrying extras are still accepted.)
    del options
    return [_amount_field("amount", lang, ctx, {"en": "Take-home per month", "es": "Neto al mes"})]


def _income_known(sit) -> bool:
    return bool(sit["income"]["items"] or sit["income"]["extras"])


def _income_prefill(sit, ctx) -> dict | None:
    rows = [r for r in sit["income"]["items"] if r.get("frequency") == "monthly" and r.get("amount") is not None]
    if not rows:
        return None
    main = max(rows, key=lambda r: r["amount"])
    out: dict[str, Any] = {"amount": {"amount": main["amount"], "currency": main["currency"]}}
    extras = {}
    for row in sit["income"]["extras"] + [r for r in rows if r is not main]:
        kind = row.get("kind") if row.get("kind") in _EXTRA_KIND else "other"
        if kind in {o["id"] for o in _extras(ctx)} and row.get("amount") is not None:
            extras[kind] = {"amount": {"amount": row["amount"], "currency": row["currency"]}}
    if extras:
        out["extras"] = extras
    return out


def _income_writer(answer: dict, ctx: dict) -> list[tuple[str, Any]]:
    main = _money(answer.get("amount", answer.get("salary")), ctx["home"], "your monthly take-home")
    main_currency = answer.get("currency") if isinstance(answer.get("currency"), str) else None
    if main_currency and not isinstance(answer.get("amount"), dict):
        main["currency"] = main_currency
    facts = [("income.salary", {**main, "frequency": "monthly", "net": True, "kind": "salary", "approximate": True})]
    extras = answer.get("extras") or {}
    if isinstance(extras, list):
        extras = {e: {} for e in extras}
    if not isinstance(extras, dict):
        raise ValueError("Choose extra income from the list.")
    allowed = {o["id"] for o in _extras(ctx)}
    for extra, detail in extras.items():
        if extra not in allowed:
            raise ValueError("Choose extra income from the list.")
        amount = _money((detail or {}).get("amount") if isinstance(detail, dict) else detail, ctx["home"],
                        "that income", required=False)
        if amount is None:
            continue  # chosen without an amount: known to exist, amount unknown, nothing invented
        kind, frequency, month = _EXTRA_KIND[extra]
        value = {**amount, "frequency": frequency, "kind": kind, "approximate": True}
        if month:
            value["month"] = month
        facts.append((f"income.{extra}", value))
    return facts


_EXTRA_NAMES = {o["id"]: o["label"] for o in _EXTRAS_MX}


def _income_summary(answer: dict, lang: str, ctx: dict) -> str:
    main = _money(answer.get("amount", answer.get("salary")), ctx["home"], "income")
    text = (f"Recibes {money_text(main['amount'], main['currency'], ctx['home'])} al mes" if lang == "es"
            else f"You take home {money_text(main['amount'], main['currency'], ctx['home'])} a month")
    extras = answer.get("extras") or {}
    names = [_EXTRA_NAMES[e][lang].lower() if e not in ("ptu",) else "PTU" for e in extras if e in _EXTRA_NAMES]
    if names:
        text += " + " + ", ".join(names)
    return text


# -- spending


def _spending_fields(lang: str, ctx: dict) -> list[dict]:
    return [_amount_field("amount", lang, ctx, {"en": "Per month", "es": "Al mes"})]


def _spending_known(sit) -> bool:
    return sit["spending"]["monthly"] is not None


def _spending_prefill(sit, ctx) -> dict | None:
    spending = sit["spending"]
    stated = spending.get("stated") or {}
    if spending.get("source") == "stated" and stated.get("total") is not None:
        return {"amount": {"amount": stated["total"], "currency": stated.get("currency") or ctx["home"]}}
    if spending.get("monthly") is not None:
        return {"amount": {"amount": round(spending["monthly"]), "currency": sit.get("currency") or ctx["home"]}}
    return None


def _spending_writer(answer: dict, ctx: dict) -> list[tuple[str, Any]]:
    amount = _money(answer.get("amount"), ctx["home"], "your monthly spending")
    if answer.get("currency") and not isinstance(answer.get("amount"), dict):
        amount["currency"] = answer["currency"]
    return [("spending.monthly", {"total": amount["amount"], "currency": amount["currency"], "approximate": True})]


def _spending_summary(answer: dict, lang: str, ctx: dict) -> str:
    amount = _money(answer.get("amount"), ctx["home"], "spending")
    shown = money_text(amount["amount"], amount["currency"], ctx["home"])
    return f"Gastas {shown} al mes" if lang == "es" else f"You spend {shown} a month"


# -- money


# Nu comes after the older chips so numbered replies over text keep their meaning.
_MONEY_MX = [_opt("bank", "Bank", "Banco"), _opt("cetes", "CETES"),
             _opt("brokerage", "GBM / brokerage", "GBM / casa de bolsa"), _opt("afore", "AFORE"), _opt("ppr", "PPR"),
             _opt("us_broker", "US broker", "Broker en EE. UU."), _opt("nu", "Nu"), _opt("none", "Nothing yet", "Nada aún")]
_MONEY_US = [_opt("bank", "Checking / savings", "Cheques / ahorro"), _opt("brokerage", "Brokerage", "Casa de bolsa"),
             _opt("retirement", "401(k) / IRA"), _opt("hsa", "HSA"), _opt("none", "Nothing yet", "Nada aún")]
_MONEY_OTHER = [_opt("bank", "Bank", "Banco"), _opt("brokerage", "Brokerage", "Casa de bolsa"),
                _opt("retirement", "Retirement", "Retiro"), _opt("none", "Nothing yet", "Nada aún")]
# option -> (key, kind, currency override)
_MONEY_KEYS = {"bank": ("cash.bank", None, None), "nu": ("cash.nu", None, "MXN"),
               "cetes": ("investment.cetes", "fund", "MXN"),
               "brokerage": ("investment.brokerage", "brokerage", None), "afore": ("investment.afore", "afore", "MXN"),
               "ppr": ("investment.ppr", "retirement", "MXN"), "us_broker": ("investment.us_broker", "brokerage", "USD"),
               "retirement": ("investment.retirement", "retirement", None), "hsa": ("investment.hsa", "retirement", "USD")}


# Chips that name a firm save it as the institution, so a statement from that firm settles the balance.
# The generic "bank" chip names no one.  Keyed by (country or None, option).
_MONEY_INSTITUTIONS = {("MX", "nu"): "Nu", ("MX", "cetes"): "Cetesdirecto", ("MX", "brokerage"): "GBM"}
# The name saved with the fact: the person's words for it, not the chip's "X / Y" wording.
_MONEY_NAMES = {("MX", "brokerage"): {"en": "GBM", "es": "GBM"}, ("MX", "cetes"): {"en": "CETES", "es": "CETES"}}


def _money_options(ctx: dict) -> list[dict]:
    return {"MX": _MONEY_MX, "US": _MONEY_US}.get(ctx.get("country") or "", _MONEY_OTHER)


def _money_labels(ctx: dict) -> dict:
    return {o["id"]: o["label"] for o in _money_options(ctx)}


def _money_fields(lang: str, ctx: dict) -> list[dict]:
    options = []
    for option in _money_options(ctx):
        entry = {"id": option["id"], "label": option["label"][lang]}
        if option["id"] == "none":
            entry["exclusive"] = True
        else:
            currency = _MONEY_KEYS[option["id"]][2]
            entry["fields"] = [_amount_field("amount", lang, ctx, {"en": "About how much", "es": "Cuánto, más o menos"},
                                             required=False, currency=currency)]
            entry["upload"] = True
        options.append(entry)
    return [{"name": "items", "type": "chips", "options": options, "max": len(options), "required": True}]


def _money_known(sit) -> bool:
    return bool(sit["cash"]) or bool(sit["investments"]) or any(a["source"] != "ledger" for a in sit["accounts"])


def _money_prefill(sit, ctx) -> dict | None:
    items: dict[str, Any] = {}
    by_key = {key: option for option, (key, _, _) in _MONEY_KEYS.items()}
    allowed = {o["id"] for o in _money_options(ctx)}
    for row in [*sit["cash"], *sit["investments"]]:
        option = by_key.get(row.get("key"))
        if option in allowed and row.get("amount") is not None:
            items[option] = {"amount": {"amount": row["amount"], "currency": row["currency"]}}
    return {"items": items} if items else None


def _money_writer(answer: dict, ctx: dict) -> list[tuple[str, Any]]:
    items = answer.get("items")
    if isinstance(items, list):
        items = {i: {} for i in items}
    if not isinstance(items, dict) or not items:
        raise ValueError("Choose where your money is, or Nothing yet.")
    allowed = {o["id"] for o in _money_options(ctx)}
    if any(i not in allowed for i in items):
        raise ValueError("Choose from the list.")
    if "none" in items:
        if len(items) > 1:
            raise ValueError("Nothing yet can't be combined with other choices.")
        return []
    facts = []
    for option, detail in items.items():
        detail = detail if isinstance(detail, dict) else {"amount": detail}
        key, kind, currency = _MONEY_KEYS[option]
        amount = _money(detail.get("amount"), currency or ctx["home"], "that balance", required=False)
        lang = ctx.get("language") or "es"
        name = (_MONEY_NAMES.get((ctx.get("country"), option)) or _money_labels(ctx)[option])[lang]
        institution = _MONEY_INSTITUTIONS.get((ctx.get("country"), option))
        if amount is None:
            # They have it but did not say how much: remember that it exists, with an unknown balance.
            amount = {"currency": currency or ctx["home"], "balance_unknown": True}
        if key.startswith("cash."):
            value = {**amount, "liquid": True, "name": name, "approximate": True}
        else:
            value = {**amount, "kind": kind, "name": name, "approximate": True}
        if institution:
            value["institution"] = institution
        facts.append((key, value))
    return facts


def _items_summary(items: dict, labels: dict, lang: str, ctx: dict, field: str = "amount") -> str:
    parts = []
    for option, detail in items.items():
        name = labels.get(option, {}).get(lang, option)
        detail = detail if isinstance(detail, dict) else {field: detail}
        try:
            amount = _money(detail.get(field), ctx["home"], "amount", required=False)
        except ValueError:
            amount = None
        parts.append(f"{name} {money_text(amount['amount'], amount['currency'], ctx['home'])}" if amount else name)
    return " · ".join(parts)


def _money_summary(answer: dict, lang: str, ctx: dict) -> str:
    items = answer.get("items") or {}
    if isinstance(items, list):
        items = {i: {} for i in items}
    if "none" in items:
        return "Nada invertido ni ahorrado aún" if lang == "es" else "Nothing saved or invested yet"
    return _items_summary(items, _money_labels(ctx), lang, ctx)


# -- debts


_DEBTS = [_opt("none", "No", "No"), _opt("card", "Credit card", "Tarjeta de crédito"), _opt("auto", "Car loan", "Auto"),
          _opt("mortgage", "Mortgage", "Hipoteca"), _opt("personal", "Personal loan", "Préstamo personal"),
          _opt("student", "Student loan", "Crédito educativo"), _opt("other", "Other", "Otra")]
_DEBT_LABELS = {o["id"]: o["label"] for o in _DEBTS}


def _debt_options(ctx: dict) -> list[dict]:
    return [o for o in _DEBTS if o["id"] != "student" or ctx.get("country") == "US"]


def _debts_fields(lang: str, ctx: dict) -> list[dict]:
    options = []
    for option in _debt_options(ctx):
        entry = {"id": option["id"], "label": option["label"][lang]}
        if option["id"] == "none":
            entry["exclusive"] = True
        else:
            entry["fields"] = [
                _amount_field("balance", lang, ctx, {"en": "Balance", "es": "Saldo"}, required=True),
                {"name": "rate", "type": "percent", "label": {"en": "Annual rate", "es": "Tasa anual"}[lang],
                 "required": False},
                _amount_field("payment", lang, ctx, {"en": "Monthly payment", "es": "Pago mensual"}, required=False),
                _in_spending_field(lang, ctx),
            ]
        options.append(entry)
    return [{"name": "items", "type": "chips", "options": options, "max": len(options), "required": True}]


_IN_SPENDING = [_opt("yes", "Yes, it's included", "Sí, ya está incluido"),
                _opt("no", "No, it's on top", "No, es aparte")]
_YES = re.compile(r"^\s*(yes|y|yeah|yep|included|s[ií]|ya|incluido|ya est[aá])\b", re.I)
_NO = re.compile(r"^\s*(no|nope|aparte|on top|separate|not included)\b", re.I)


def _in_spending_field(lang: str, ctx: dict) -> dict:
    """Asked with every payment: is it already inside the monthly spending they gave?  Unanswered stays unknown."""
    spending = ctx.get("spending")
    if spending is not None:
        amount = money_text(spending, ctx.get("spending_currency") or ctx["home"], ctx["home"])
        label = {"en": f"Is this payment already inside your {amount} of spending?",
                 "es": f"¿Este pago ya está dentro de tus {amount} de gasto?"}[lang]
    else:
        label = {"en": "Is this payment already inside your monthly spending?",
                 "es": "¿Este pago ya está dentro de tu gasto mensual?"}[lang]
    return {"name": "in_spending", "type": "chips", "label": label, "options": _labels(_IN_SPENDING, lang), "max": 1,
            "required": False}


def _in_spending(value: Any) -> bool | None:
    """yes/no (chip id, bool or a typed sí/no) -> True/False; anything else (unsure, blank) -> None: ask later."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip():
        if _NO.search(value):
            return False
        if _YES.search(value):
            return True
    return None


def _debts_known(sit) -> bool:
    return bool(sit["liabilities"])


def _debts_prefill(sit, ctx) -> dict | None:
    items = {}
    allowed = {o["id"] for o in _debt_options(ctx)}
    for row in sit["liabilities"]:
        kind = row.get("kind") if row.get("kind") in allowed else "other"
        if kind in items or row.get("balance") is None:
            continue
        detail: dict[str, Any] = {"balance": {"amount": row["balance"], "currency": row["currency"]}}
        if row.get("annual_rate") is not None:
            detail["rate"] = round(row["annual_rate"] * 100, 4)
        if row.get("monthly_payment") is not None:
            detail["payment"] = {"amount": round(row["monthly_payment"]), "currency": row["currency"]}
        if isinstance(row.get("in_spending"), bool):
            detail["in_spending"] = "yes" if row["in_spending"] else "no"
        items[kind] = detail
    return {"items": items} if items else None


def _debts_writer(answer: dict, ctx: dict) -> list[tuple[str, Any]]:
    items = answer.get("items")
    if answer.get("none") is True:
        items = {"none": {}}
    if isinstance(items, list):
        items = {i: {} for i in items}
    if not isinstance(items, dict) or not items:
        raise ValueError("Choose what you owe, or No.")
    allowed = {o["id"] for o in _debt_options(ctx)}
    if any(i not in allowed for i in items):
        raise ValueError("Choose from the list.")
    if "none" in items:
        if len(items) > 1:
            raise ValueError("No can't be combined with a debt.")
        return []
    facts = []
    for option, detail in items.items():
        detail = detail if isinstance(detail, dict) else {"balance": detail}
        balance = _money(detail.get("balance", detail.get("amount")), ctx["home"], "the balance")
        value: dict[str, Any] = {"kind": option, "balance": balance["amount"], "currency": balance["currency"],
                                 "approximate": True}
        rate = _rate(detail.get("rate"), "the rate")
        if rate is not None:
            value["annual_rate"] = rate
        payment = _money(detail.get("payment"), balance["currency"], "the payment", required=False)
        if payment is not None:
            if payment["currency"] != balance["currency"]:
                raise ValueError("Enter the payment in the same currency as the balance.")
            value["payment"], value["payment_frequency"] = payment["amount"], "monthly"
            # Whether the payment is already inside the stated spending: only as answered.  Unknown is left
            # out, so the picture asks instead of subtracting it twice (or not at all).
            inside = _in_spending(detail.get("in_spending"))
            if inside is not None:
                value["in_spending"] = inside
        facts.append((f"liability.{option}", value))
    return facts


def _debts_summary(answer: dict, lang: str, ctx: dict) -> str:
    items = answer.get("items") or {}
    if isinstance(items, list):
        items = {i: {} for i in items}
    if answer.get("none") is True or "none" in items:
        return "Sin deudas" if lang == "es" else "No debts"
    return _items_summary(items, _DEBT_LABELS, lang, ctx, "balance")


# -- goals


_GOALS = [
    (_opt("emergency_fund", "Emergency fund", "Fondo de emergencia"), "emergency-fund", "save", None),
    (_opt("pay_off_debt", "Pay off debt", "Pagar deudas"), "pay-off-debt", "pay_off", None),
    (_opt("home", "Home down payment", "Enganche de casa"), "home-down-payment", "buy", {"en": "a home", "es": "una casa"}),
    (_opt("retirement", "Retirement", "Retiro"), "retirement", "retire", None),
    (_opt("grow", "Grow my wealth", "Hacer crecer mi dinero"), "grow-wealth", "invest", None),
    (_opt("education", "Education", "Educación"), "education", "education", None),
]
_GOAL_BY_ID = {g[0]["id"]: g for g in _GOALS}


def _goals_fields(lang: str, ctx: dict) -> list[dict]:
    return [
        {"name": "goals", "type": "chips", "options": _labels([g[0] for g in _GOALS], lang), "max": 2,
         "required": True, "hint": {"en": "Pick one or two", "es": "Elige una o dos"}[lang]},
        _amount_field("target_amount", lang, ctx, {"en": "Target, if you have one", "es": "Meta, si la tienes"},
                      required=False),
        {"name": "target_year", "type": "year", "label": {"en": "By", "es": "Para"}[lang], "min": date.today().year,
         "max": date.today().year + 60, "required": False},
    ]


def _goals_known(sit) -> bool:
    return bool(sit["goals"])


def _goals_prefill(sit, ctx) -> dict | None:
    slugs = {g[1]: g[0]["id"] for g in _GOALS}
    chosen = [slugs[g["id"]] for g in sit["goals"] if g["id"] in slugs][:2]
    if not chosen:
        return None
    out: dict[str, Any] = {"goals": chosen}
    first = next(g for g in sit["goals"] if g["id"] in slugs)
    if first.get("target_amount") is not None:
        out["target_amount"] = {"amount": first["target_amount"], "currency": first.get("currency") or ctx["home"]}
    if first.get("target_date"):
        out["target_year"] = int(first["target_date"][:4])
    return out


# Goals that have a target amount and date by nature: a down payment, tuition, a reserve.
_TARGET_GOALS = ("home", "education", "emergency_fund")  # in order of how naturally they carry one


def _chosen_goals(answer: Mapping[str, Any]) -> list[str]:
    chosen = answer.get("goals")
    if isinstance(chosen, str):
        chosen = [chosen]
    if isinstance(chosen, dict):
        chosen = list(chosen)
    if not isinstance(chosen, list) or not 1 <= len(chosen) <= 2 or any(c not in _GOAL_BY_ID for c in chosen):
        raise ValueError("Choose one or two goals.")
    return list(dict.fromkeys(chosen))


def _target_goal(answer: Mapping[str, Any], chosen: list[str]) -> str | None:
    """The goal the optional target and year belong to, or None when that is ambiguous (never guessed).

    One goal takes it; else the person's explicit ``target_goal``; else the one chosen goal that has a
    target by nature (home, education, emergency fund).  Retirement + home puts it on the home.
    """
    if len(chosen) == 1:
        return chosen[0]
    if answer.get("target_goal") in chosen:
        return answer["target_goal"]
    # A home or education has both an amount and a date by nature; an emergency fund an amount.
    for tier in (("home", "education"), ("emergency_fund",)):
        natural = [c for c in chosen if c in tier]
        if natural:
            return natural[0] if len(natural) == 1 else None
    return None


def _has_target(answer: Mapping[str, Any]) -> bool:
    return answer.get("target_amount") not in (None, "", {}) or answer.get("target_year") not in (None, "")


def _goals_writer(answer: dict, ctx: dict) -> list[tuple[str, Any]]:
    chosen = _chosen_goals(answer)
    lang = ctx.get("language") or "es"
    target_goal = _target_goal(answer, chosen)
    goals = []
    for index, goal_id in enumerate(chosen):
        option, slug, action, obj = _GOAL_BY_ID[goal_id]
        goal: dict[str, Any] = {"id": slug, "name": option["label"][lang], "action": action,
                                "priority": "high" if index == 0 else "medium", "status": "active"}
        if obj:
            goal["object"] = obj[lang]
        if goal_id == target_goal:
            target = _money(answer.get("target_amount"), ctx["home"], "the target", required=False)
            if target is not None:
                goal["target_amount"], goal["currency"] = target["amount"], target["currency"]
            year = answer.get("target_year")
            if year not in (None, ""):
                try:
                    year = int(str(year))
                except ValueError:
                    raise ValueError("Enter a year, such as 2030.") from None
                if not date.today().year <= year <= date.today().year + 60:
                    raise ValueError("Enter a year from this year on.")
                goal["target_date"] = f"{year}-12-31"
        goals.append(goal)
    return [("goals", goals)]


def _goals_summary(answer: dict, lang: str, ctx: dict) -> str:
    chosen = answer.get("goals")
    chosen = [chosen] if isinstance(chosen, str) else list(chosen or [])
    chosen = [c for c in dict.fromkeys(chosen) if c in _GOAL_BY_ID]
    names = [_GOAL_BY_ID[c][0]["label"][lang] for c in chosen]
    text = ", ".join(names[:1] + [n[:1].lower() + n[1:] for n in names[1:]])
    if len(chosen) > 1 and _has_target(answer):
        target_goal = _target_goal(answer, chosen)
        if target_goal is None:
            # Not assigned: say so and name the goals instead of guessing.
            between = (" o " if lang == "es" else " or ").join(n.lower() for n in names)
            return text + (f" · meta sin asignar ({between})" if lang == "es" else f" · target not assigned yet ({between})")
        text += f" · {_GOAL_BY_ID[target_goal][0]['label'][lang]}"
    target = None
    try:
        target = _money(answer.get("target_amount"), ctx["home"], "target", required=False)
    except ValueError:
        pass
    if target:
        text += f": {money_text(target['amount'], target['currency'], ctx['home'])}"
    if answer.get("target_year"):
        text += f" {'para' if lang == 'es' else 'by'} {answer['target_year']}"
    return text


# -- risk


_DROP = [_opt("sell", "Sell", "Vendería"), _opt("hold", "Hold", "Esperaría"), _opt("buy_more", "Buy more", "Compraría más")]
_EXPERIENCE = [_opt("none", "Never invested", "Nunca he invertido"), _opt("some", "Some", "Algo"),
               _opt("experienced", "Experienced", "Con experiencia")]


def _risk_fields(lang: str, ctx: dict) -> list[dict]:
    return [
        {"name": "drop_reaction", "type": "chips", "options": _labels(_DROP, lang), "max": 1, "required": True},
        {"name": "experience", "type": "chips", "label": {"en": "Investing experience", "es": "Experiencia invirtiendo"}[lang],
         "options": _labels(_EXPERIENCE, lang), "max": 1, "required": False},
    ]


def _risk_known(sit) -> bool:
    risk = (sit.get("profile") or {}).get("risk") or {}
    return bool(risk.get("drop_reaction"))  # experience is optional on the card


def _risk_prefill(sit, ctx) -> dict | None:
    risk = (sit.get("profile") or {}).get("risk") or {}
    out = {k: risk[k] for k in ("drop_reaction", "experience") if risk.get(k)}
    return out or None


def _risk_writer(answer: dict, ctx: dict) -> list[tuple[str, Any]]:
    value = {}
    if answer.get("drop_reaction") not in {o["id"] for o in _DROP}:
        raise ValueError("Choose what you would do.")
    value["drop_reaction"] = answer["drop_reaction"]
    if answer.get("experience") not in (None, ""):
        if answer["experience"] not in {o["id"] for o in _EXPERIENCE}:
            raise ValueError("Choose your investing experience.")
        value["experience"] = answer["experience"]
    return [("preference.risk", value)]


_DROP_SAID = {"es": {"sell": "venderías", "hold": "esperarías", "buy_more": "comprarías más"},
              "en": {"sell": "you would sell", "hold": "you would hold", "buy_more": "you would buy more"}}


def _risk_summary(answer: dict, lang: str, ctx: dict) -> str:
    # Their stated reaction, said back to them ("Si cae 20%: venderías"), not a label.
    drop = _DROP_SAID[lang].get(answer.get("drop_reaction"), "")
    exp = {o["id"]: o["label"][lang] for o in _EXPERIENCE}.get(answer.get("experience"))
    head = f"Si cae 20%: {drop}" if lang == "es" else f"If it falls 20%: {drop}"
    if exp:
        head += (f" · experiencia: {exp[:1].lower() + exp[1:]}" if lang == "es" else f" · experience: {exp.lower()}")
    return head


# -- statements


def _statements_fields(lang: str, ctx: dict) -> list[dict]:
    return [{"name": "uploads", "type": "upload", "required": False}]


def _statements_writer(answer: dict, ctx: dict) -> list[tuple[str, Any]]:
    return []  # statements go through the ingest flow in the reveal turn; nothing is written here


def _statements_summary(answer: dict, lang: str, ctx: dict) -> str:
    n = len(answer.get("uploads") or [])
    if lang == "es":
        return f"{n} {'estado de cuenta' if n == 1 else 'estados de cuenta'} por revisar"
    return f"{n} {'statement' if n == 1 else 'statements'} to read"


STEPS: tuple[Step, ...] = (
    Step("identity", ("name", "language", "residence"),
         {"en": "What should I call you, and where do you live?", "es": "¿Cómo te llamo y dónde vives?"},
         {"en": "You", "es": "Tú"}, _identity_fields, _identity_known, _identity_prefill, _identity_writer,
         _identity_summary, unsure=False),
    Step("about", ("birth_year", "dependents"),
         {"en": "What year were you born? Anyone who depends on you?", "es": "¿En qué año naciste? ¿Alguien depende de ti?"},
         {"en": "Age and dependents", "es": "Edad y dependientes"}, _about_fields, _about_known, _about_prefill,
         _about_writer, _about_summary),
    Step("income", ("income",),
         {"en": "What do you take home each month?", "es": "¿Cuánto recibes al mes, neto?"},
         {"en": "Income", "es": "Ingreso"}, _income_fields, _income_known, _income_prefill, _income_writer,
         _income_summary),
    Step("spending", ("spending",),
         {"en": "What do you spend in a typical month?", "es": "¿Cuánto gastas en un mes normal?"},
         {"en": "Spending", "es": "Gasto"}, _spending_fields, _spending_known, _spending_prefill, _spending_writer,
         _spending_summary, upload=True),
    Step("money", ("cash", "investments"),
         {"en": "Where is your money today?", "es": "¿Dónde tienes tu dinero hoy?"},
         {"en": "Savings and investments", "es": "Ahorro e inversiones"}, _money_fields, _money_known, _money_prefill,
         _money_writer, _money_summary, upload=True),
    Step("debts", ("debts",),
         {"en": "Do you owe anything?", "es": "¿Debes algo?"},
         {"en": "Debts", "es": "Deudas"}, _debts_fields, _debts_known, _debts_prefill, _debts_writer, _debts_summary),
    Step("goals", ("goals",),
         {"en": "What matters most right now?", "es": "¿Qué es lo más importante ahora?"},
         {"en": "Goals", "es": "Metas"}, _goals_fields, _goals_known, _goals_prefill, _goals_writer, _goals_summary),
    Step("risk", ("risk",),
         {"en": "If your investments fell 20% in a month, you would…",
          "es": "Si tus inversiones cayeran 20% en un mes…"},
         {"en": "Risk", "es": "Riesgo"}, _risk_fields, _risk_known, _risk_prefill, _risk_writer, _risk_summary),
    Step("statements", (),
         {"en": "Add statements for an exact picture", "es": "Agrega estados de cuenta para un panorama exacto"},
         {"en": "Statements", "es": "Estados de cuenta"}, _statements_fields, lambda sit: False,
         lambda sit, ctx: None, _statements_writer, _statements_summary, unsure=False, upload=True),
)
BY_ID = {step.id: step for step in STEPS}


# ------------------------------------------------------------------ state


def _record(sit: Mapping[str, Any]) -> dict:
    record = (sit.get("profile") or {}).get("onboarding")
    return record if isinstance(record, dict) else {}


def step_status(sit: Mapping[str, Any], step: Step) -> str:
    """done | skipped | unsure | pending for a card, from the schema step names it settles."""
    record = _record(sit)
    if step.id == "statements":
        return "done" if record.get("completed_at") else "pending"
    steps = record.get("steps") or {}
    statuses = [steps.get(name) for name in step.marks]
    if all(s in RESOLVED for s in statuses):
        for status in ("done", "unsure", "skipped"):
            if status in statuses:
                return status
    return "pending"


def progress(sit: Mapping[str, Any]) -> dict:
    """Card ids by status and whether onboarding started and completed."""
    record = _record(sit)
    out: dict[str, Any] = {"started": bool(record.get("started_at")), "completed": bool(record.get("completed_at")),
                           "done": [], "skipped": [], "unsure": [], "pending": [], "known": []}
    for step in STEPS:
        if step.condition(sit) and step.marks:  # statements is an offer, not a question
            status = step_status(sit, step)
            if status == "pending" and step.known(sit):
                # Already known from memory (a statement, an earlier conversation): a one-tap confirmation at
                # most, never counted as a question still to answer.
                out["known"].append(step.id)
                continue
            out[step_status(sit, step)].append(step.id)
    return out


def _visible(sit: Mapping[str, Any]) -> list[Step]:
    return [step for step in STEPS if step.condition(sit)]


def card(sit: Mapping[str, Any], step_id: str, language: str | None = None) -> dict:
    """The card for one step, prefilled from memory when it is known (also used to edit an answer)."""
    step = BY_ID.get(step_id)
    if step is None:
        raise ValueError("Unknown onboarding step.")
    lang = _lang(language or (sit.get("profile") or {}).get("language"))
    ctx = _context(sit)
    visible = _visible(sit)
    known = step.known(sit)
    prefill = step.prefill(sit, ctx) if known else None
    return {
        "step": step.id, "index": visible.index(step) + 1 if step in visible else None, "total": len(visible),
        "language": lang, "prompt": step.prompt[lang], "title": step.title[lang],
        "fields": step.fields(lang, ctx), "prefill": prefill, "confirm": bool(prefill),
        # One secondary action: "Omitir" / "Skip" (a typed "no sé" is read as the same skip).
        "status": step_status(sit, step), "currency": ctx["home"], "unsure": False, "skip": True, "upload": step.upload,
    }


def in_flow(sit: Mapping[str, Any]) -> bool:
    """The card-by-card setup is running: started and not completed."""
    record = _record(sit)
    return bool(record.get("started_at")) and not record.get("completed_at")


def settled(sit: Mapping[str, Any], step: Step) -> bool:
    """Answered on a card, skipped, or (outside the setup flow) already known from the facts.

    Someone who gave their name, city, income and debts in chat has answered those questions: the setup
    card never asks them again.  Inside the flow a known step is still shown once, prefilled, to confirm.
    The statements offer settles with the flow, or, outside it, once every question is settled.
    """
    if step_status(sit, step) != "pending":
        return True
    if in_flow(sit):
        return False
    if step.id == "statements":
        return all(settled(sit, other) for other in _visible(sit) if other.marks)
    return step.known(sit)


def remaining(sit: Mapping[str, Any]) -> list[str]:
    """Card ids still to ask, in order (the statements offer included)."""
    return [step.id for step in _visible(sit) if not settled(sit, step)]


def conversation_language(person_messages: Any) -> str | None:
    """es or en when the person's own messages clearly show it; None when there is too little to tell."""
    from .agent import _SPANISH  # lazy: the agent module is heavy and imports the service
    text = " ".join(str(m or "") for m in person_messages or [])
    hits = len(_SPANISH.findall(text))
    if hits >= 2:
        return "es"
    if hits == 0 and len(re.findall(r"[A-Za-z]{2,}", text)) >= 6:
        return "en"
    return None


def reveal_language(sit: Mapping[str, Any], person_messages: Any = (), page_language: str | None = None) -> str:
    """The language of the setup reveal: the one the person has been writing in, else the saved one, else
    the page's.  An all-Spanish conversation gets a Spanish reveal even on an English page."""
    saved = (sit.get("profile") or {}).get("language")
    for choice in (conversation_language(person_messages), saved, page_language):
        if choice in ("es", "en"):
            return choice
    return "es"


def next_step(sit: Mapping[str, Any], language: str | None = None) -> dict | None:
    """The next unanswered card in order, or ``None`` when every step is settled (see :func:`settled`)."""
    for step in _visible(sit):
        if not settled(sit, step):
            return card(sit, step.id, language)
    return None


def picture(sit: Mapping[str, Any], language: str | None = None) -> dict:
    """The numbers so far and one quiet line that states them."""
    lang = _lang(language or (sit.get("profile") or {}).get("language"))
    currency = sit.get("currency")
    home = HOME_CURRENCY.get(_country(sit) or "", currency)
    nw, flow, reserve = sit["net_worth"], sit["cash_flow"], sit["reserve"]
    debts = []
    for row in sit["liabilities"]:
        plan = row.get("payoff") or {}
        debts.append({"kind": row.get("kind"), "balance": row.get("balance"), "currency": row.get("currency"),
                      "rate": row.get("annual_rate"), "payoff": plan.get("date") if plan.get("status") == "ready" else None})
    parts = []
    m = lambda value: money_text(value, currency, home)  # noqa: E731
    if nw.get("total") is not None:
        parts.append(f"Patrimonio neto {m(nw['total'])}" if lang == "es" else f"Net worth {m(nw['total'])}")
    elif nw.get("unknown_balances"):
        from .situation.text import unknown_names  # lazy: the text module imports the model
        names = ", ".join(unknown_names(sit, lang))
        parts.append(f"falta el saldo de {names}" if lang == "es" else f"balance still needed for {names}")
    if flow.get("surplus") is not None:
        surplus = flow["surplus"]
        if surplus >= 0:
            parts.append(f"te quedan {m(surplus)} al mes" if lang == "es" else f"{m(surplus)} left each month")
        else:
            parts.append(f"te faltan {m(-surplus)} al mes" if lang == "es" else f"{m(-surplus)} short each month")
    elif flow.get("surplus_before_unknown_debts") is not None and flow["surplus_before_unknown_debts"] > 0:
        # A debt payment nobody gave: the known part, said as what it is, never as the surplus.
        before = m(flow["surplus_before_unknown_debts"])
        parts.append(f"te quedan {before} al mes antes de pagar tus deudas" if lang == "es"
                     else f"{before} left each month before debt payments")
    elif flow.get("surplus_range"):
        # A payment that may already be inside their spending: both readings, and the question.
        span = flow["surplus_range"]
        parts.append(f"te quedan entre {m(span['low'])} y {m(span['high'])} al mes" if lang == "es"
                     else f"between {m(span['low'])} and {m(span['high'])} left each month")
    if reserve.get("months") is not None and flow.get("spending") is not None:
        months = reserve["months"]
        shown = f"{months:g}"  # es-MX writes decimals with a point, like en
        parts.append(f"reserva de {shown} meses" if lang == "es" else f"{shown} months of reserve")
    line = " · ".join(parts)
    reserve_parts = [{"kind": p["kind"], "label": p.get("label"), "value": p.get("value")}
                     for p in reserve.get("parts") or []]
    return {
        "currency": currency, "net_worth": nw.get("total"), "unknown_balances": nw.get("unknown_balances") or [],
        # "Liquid" on the card is the reserve's money (cash and cash-like instruments such as CETES), so it
        # always agrees with the reserve months beside it.  Everything liquid, brokerage included, is
        # ``liquid_total`` (the net-worth split).
        "liquid": reserve.get("amount") if reserve.get("amount") is not None else None,
        "liquid_basis": "reserve", "liquid_total": nw.get("liquid"), "illiquid": nw.get("illiquid"),
        "debt": nw.get("liabilities"), "income": flow.get("income"), "spending": flow.get("spending"),
        "surplus": flow.get("surplus"), "surplus_range": flow.get("surplus_range"),
        "payments_maybe_in_spending": flow.get("in_spending_unknown") or [],
        "reserve_months": reserve.get("months"), "reserve_amount": reserve.get("amount"),
        "reserve_parts": reserve_parts, "debts": debts,
        "line": line[:1].upper() + line[1:] if line else "",
    }


# ------------------------------------------------------------------ writing


def _source(today: str) -> dict:
    return {"kind": "user", "ref": SOURCE_REF, "observed_on": today}


def _today(today: date | str | None) -> str:
    if isinstance(today, date):
        return today.isoformat()
    return today or datetime.now(timezone.utc).date().isoformat()


def build_facts(sit: Mapping[str, Any], step_id: str, answer: Mapping[str, Any] | None, *, skip: bool = False,
                language: str | None = None, today: date | str | None = None) -> tuple[list[dict], str, str, bool]:
    """Facts for one answer: (facts, status, summary, completes).  Pure; nothing is written.

    Every value is checked with the canonical schema before it is returned.
    """
    step = BY_ID.get(step_id)
    if step is None:
        raise ValueError("Unknown onboarding step.")
    lang = _lang(language or (sit.get("profile") or {}).get("language"))
    ctx = {**_context(sit), "language": lang}
    day = _today(today)
    answer = dict(answer or {})
    pairs: list[tuple[str, Any]] = []
    if skip:
        status = "skipped"
        summary = f"{step.title[lang]}: {'omitido' if lang == 'es' else 'skipped'}"
    elif answer.get("unsure") is True:
        if not step.unsure:
            raise ValueError("This step needs an answer or Skip.")
        # "Not sure" and "Skip" are one action: the step is skipped and stays unknown, never asked again.
        status = "skipped"
        summary = f"{step.title[lang]}: {'omitido' if lang == 'es' else 'skipped'}"
    elif answer.get("confirm") is True and step.known(sit):
        status = "done"
        prefill = step.prefill(sit, ctx) or {}
        try:
            summary = step.summary(prefill, lang, ctx) if prefill else step.title[lang]
        except ValueError:
            summary = step.title[lang]
    else:
        if step.id == "identity" and answer.get("country") and answer.get("country") != "other":
            country = country_code(answer.get("country"))
            ctx = {**_context({**sit, "currency": None, "profile": {"residence": {"country": country}}}), "language": lang}
        pairs = step.writer(answer, ctx)
        status = "done"
        summary = step.summary(answer, lang, ctx)
    for key, value in pairs:
        try:
            validate(key, value)
        except SchemaError as exc:  # a writer bug, never the person's fault
            raise RuntimeError(f"onboarding writer produced an invalid {key}: {exc}") from exc
    record = _record(sit)
    completes = step.id == "statements" or (not record.get("completed_at") and _last_open(sit, step))
    onboarding: dict[str, Any] = {"steps": {name: status for name in step.marks}}
    if not record.get("started_at"):
        onboarding["started_at"] = _now()
        # Starting the cards after telling us things in chat: what is already known stays answered, so the
        # flow does not turn those facts back into questions.
        for other in _visible(sit):
            if other is not step and other.marks and step_status(sit, other) == "pending" and other.known(sit):
                for name in other.marks:
                    onboarding["steps"].setdefault(name, "done")
    if completes and not record.get("completed_at"):
        onboarding["completed_at"] = _now()
    validate("onboarding", {**record, **onboarding, "steps": {**(record.get("steps") or {}), **onboarding["steps"]}})
    facts = [{"key": key, "value": value, "source": _source(day), "confidence": "confirmed", "merge": True}
             for key, value in pairs]
    facts.append({"key": "onboarding", "value": onboarding, "source": _source(day), "confidence": "confirmed",
                  "merge": True})
    return facts, status, summary, bool(completes and not record.get("completed_at"))


def _last_open(sit: Mapping[str, Any], step: Step) -> bool:
    """True when this answer leaves nothing pending (statements included)."""
    pending = [s for s in _visible(sit) if step_status(sit, s) == "pending" and s is not step]
    return not pending


def target_chooser(sit: Mapping[str, Any], answer: Any, language: str | None = None) -> dict | None:
    """A small goals card asking which chosen goal the target belongs to; None when it is not ambiguous."""
    if not isinstance(answer, Mapping) or not _has_target(answer):
        return None
    try:
        chosen = _chosen_goals(answer)
    except ValueError:
        return None
    if _target_goal(answer, chosen) is not None:
        return None
    lang = _lang(language or (sit.get("profile") or {}).get("language"))
    base = card(sit, "goals", lang)
    options = [{"id": c, "label": _GOAL_BY_ID[c][0]["label"][lang]} for c in chosen]
    prefill = {k: answer[k] for k in ("goals", "target_amount", "target_year") if answer.get(k) not in (None, "")}
    return {**base, "prompt": {"en": "Which goal is that target for?", "es": "¿Para cuál meta es ese monto?"}[lang],
            "fields": [*base["fields"], {"name": "target_goal", "type": "chips", "options": options, "max": 1,
                                         "required": True, "label": {"en": "Target for", "es": "La meta es para"}[lang]}],
            "prefill": prefill, "confirm": False, "chooser": "target_goal"}


def apply(service: Any, client_id: str, step_id: str, answer: Mapping[str, Any] | None = None, *,
          skip: bool = False, language: str | None = None, today: date | str | None = None) -> dict:
    """Validate and write one answer, then return ``{card, picture, answered, complete, completed_now}``."""
    if not skip and answer is not None and not isinstance(answer, Mapping):
        raise ValueError("Send the answer as an object.")
    if not skip and not answer:
        raise ValueError("Answer the question, or choose Skip.")
    sit = service.situation(client_id)
    facts, status, summary, completed_now = build_facts(sit, step_id, answer, skip=skip, language=language,
                                                        today=today)
    service.remember(client_id, facts)
    after = service.situation(client_id)
    lang = _lang(language or (after.get("profile") or {}).get("language"))
    # A target that could belong to either goal is asked about, not guessed: the chooser comes next.
    chooser = target_chooser(after, answer, lang) if step_id == "goals" and not skip else None
    return {
        "card": chooser or next_step(after, lang), "picture": picture(after, lang),
        "answered": {"step": step_id, "status": status, "summary": summary},
        "complete": bool(_record(after).get("completed_at")), "completed_now": completed_now,
    }


def skip(service: Any, client_id: str, step_id: str, *, language: str | None = None,
         today: date | str | None = None) -> dict:
    """Record a skip: the step is not asked again and nothing is written."""
    return apply(service, client_id, step_id, None, skip=True, language=language, today=today)


# ------------------------------------------------------------------ free text


# A question mark always means a question.  A leading question word only counts when nothing else parses,
# because "como 45 mil" is Mexican Spanish for "about 45 thousand".
_QUESTION = re.compile(r"\?|¿|^\s*(what|how|why|should|can|could|is|are|do|does|which|when|qu[eé]|c[oó]mo|por\s*qu[eé]|"
                       r"cu[aá]l|cu[aá]nto|deber[ií]a|puedo|conviene)\b", re.I)
_MARKED_QUESTION = re.compile(r"\?|¿")
_UNSURE = re.compile(r"^\s*(no\s+s[eé]|no\s+estoy\s+segur[oa]|ni\s+idea|not\s+sure|i\s+don'?t\s+know|no\s+idea|"
                     r"unsure|idk)\b", re.I)


def parse_free_text(card_: Mapping[str, Any] | None, text: str) -> dict:
    """Read a typed reply to the current card without the model when it is simple.

    Returns ``{"status": "parsed", "answer": {...}}``, or ``{"status": "needs_model",
    "reason": ...}`` for questions and anything this cannot read with certainty.
    """
    text = str(text or "").strip()
    if not card_ or not text:
        return {"status": "needs_model", "reason": "no card" if not card_ else "empty"}
    if _MARKED_QUESTION.search(text):
        return {"status": "needs_model", "reason": "question"}
    parsed = _parse_for_card(card_, text)
    if parsed is not None:
        return {"status": "parsed", "answer": parsed}
    return {"status": "needs_model", "reason": "question" if _QUESTION.search(text) else "free text"}


def _parse_for_card(card_: Mapping[str, Any], text: str) -> dict | None:
    step = card_.get("step")
    if _UNSURE.search(text) and BY_ID.get(step) is not None and BY_ID[step].unsure:
        return {"unsure": True}
    if step in ("income", "spending"):
        amount = parse_amount(text)
        if amount is None:
            return None
        return {"amount": {"amount": amount["amount"],
                           "currency": amount["currency"] or card_.get("currency")}}
    if step == "about":
        year = _year(text)
        rest = re.sub(r"\b(19\d{2}|20[0-3]\d)\b", " ", text)
        count = _count(rest) if rest.strip(" ,.;y&and") else None
        if year is None and count is None:
            return None
        answer: dict[str, Any] = {}
        if year is not None:
            answer["birth_year"] = year
        if count is not None:
            answer["dependents"] = str(min(count, 4))
        return answer
    # Chip steps: a reply that names exactly the chips of one single-choice field.
    folded = _fold(text)
    for field in card_.get("fields") or []:
        if field.get("type") != "chips":
            continue
        hits = [o["id"] for o in field.get("options") or []
                if _fold(o["label"]) and (_fold(o["label"]) == folded or re.search(rf"\b{re.escape(_fold(o['label']))}\b", folded))]
        if step in ("risk",) and field["name"] == "drop_reaction" and len(hits) == 1:
            return {"drop_reaction": hits[0]}
        if step == "goals" and 1 <= len(hits) <= 2:
            return {"goals": hits}
        if step == "debts" and hits == ["none"]:
            return {"items": {"none": {}}}
    return None


# ------------------------------------------------------------------ brief


def brief_line(sit: Mapping[str, Any]) -> str | None:
    """One line for the situation brief: which setup questions are settled (never re-ask them)."""
    record = _record(sit)
    if not record:
        return None
    state = progress(sit)
    parts = []
    head = f"Onboarding {'complete' if state['completed'] else 'in progress'}"
    for name in ("done", "unsure", "skipped"):
        if state[name]:
            parts.append(f"{name}: {', '.join(state[name])}")
    if state["pending"] and not state["completed"]:
        parts.append(f"pending: {', '.join(state['pending'])}")
    return head + ("; " + "; ".join(parts) if parts else "") + " (answered, unsure and skipped are not asked again)"


__all__ = ["STEPS", "BY_ID", "apply", "brief_line", "build_facts", "card", "conversation_language", "in_flow", "money_text", "next_step",
           "parse_amount", "parse_free_text", "picture", "progress", "remaining", "reveal_language", "settled", "skip",
           "step_status",
           "target_chooser"]
