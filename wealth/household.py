"""Canonical household imports and deterministic exposure calculations.

The module deliberately has no database or provider integration.  It turns an
explicit source into the canonical household document, or calculates exposure
from a supplied/current household.  Amounts are emitted as decimal strings so
that the result remains JSON safe and reproducible.

Conventions that other modules share live here because this module owns the
canonical household: the account-type vocabulary (:data:`ACCOUNT_TYPES`),
ownership attribution (:func:`ownership_shares`), symbol normalisation
(:func:`normalize_symbol`), FX conversion (:func:`fx_converter`) and fund
look-through (:func:`lookthrough`).

FX convention: an ``fx`` record ``{"from": "USD", "to": "MXN", "rate": 18}``
means one unit of ``from`` buys ``rate`` units of ``to`` (1 USD = 18 MXN).  The
market quote ``USDMXN 18`` is therefore ``from=USD, to=MXN``.  Implausible or
apparently inverted quotes are excluded with a warning unless the record sets
``direction_verified: true``; reciprocal quotes that disagree are rejected.
"""

from __future__ import annotations

import csv
from copy import deepcopy
from dataclasses import dataclass, field as dataclass_field
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
import io
import json
from pathlib import Path
import re
from typing import Any, Callable

from . import _common


_CURRENCY = re.compile(r"[A-Z]{3}")
_ENTITIES = (
    "people", "accounts", "positions", "lots", "liabilities",
    "external_assets", "income_exposures", "fx", "fund_holdings",
)

# --------------------------------------------------------------------------
# Canonical account vocabulary (shared with wealth.tax).
# --------------------------------------------------------------------------

#: Canonical account types.  ``tax_treatment`` drives which accounts the tax
#: module will model; ``liquid_by_default`` is used only when neither the
#: position nor the account states liquidity and the asset is not inherently
#: illiquid.
ACCOUNT_TYPES: dict[str, dict[str, Any]] = {
    "taxable": {"tax_treatment": "taxable", "liquid_by_default": True, "description": "individual or joint taxable brokerage/custody account"},
    "bank": {"tax_treatment": "taxable", "liquid_by_default": True, "description": "checking, savings, deposit or money-market account"},
    "traditional_ira": {"tax_treatment": "tax_deferred", "liquid_by_default": False, "description": "traditional, rollover, SEP or SIMPLE IRA"},
    "roth_ira": {"tax_treatment": "tax_exempt", "liquid_by_default": False, "description": "Roth IRA"},
    "employer_plan": {"tax_treatment": "tax_deferred", "liquid_by_default": False, "description": "401(k), 403(b), 457(b), TSP or similar pre-tax plan"},
    "roth_employer_plan": {"tax_treatment": "tax_exempt", "liquid_by_default": False, "description": "Roth 401(k)/403(b)/457(b)"},
    "hsa": {"tax_treatment": "tax_exempt", "liquid_by_default": False, "description": "health savings account"},
    "education_529": {"tax_treatment": "tax_exempt", "liquid_by_default": False, "description": "529 education savings plan"},
    "annuity": {"tax_treatment": "tax_deferred", "liquid_by_default": False, "description": "deferred annuity contract"},
    "mx_afore": {"tax_treatment": "tax_deferred", "liquid_by_default": False, "description": "Mexican AFORE retirement account"},
    "mx_ppr": {"tax_treatment": "tax_deferred", "liquid_by_default": False, "description": "Mexican Plan Personal de Retiro"},
    "trust": {"tax_treatment": "separate_entity", "liquid_by_default": False, "description": "trust account; tax depends on trust type"},
    "entity": {"tax_treatment": "separate_entity", "liquid_by_default": False, "description": "corporate, partnership or holding-company account"},
    "private_investment": {"tax_treatment": "taxable", "liquid_by_default": False, "description": "subscription/capital account for private funds or direct deals"},
}
ACCOUNT_TYPE_ALIASES: dict[str, str] = {
    "brokerage": "taxable", "taxable_brokerage": "taxable", "individual": "taxable",
    "joint": "taxable", "custody": "taxable", "cuenta_de_inversion": "taxable",
    "cash": "bank", "checking": "bank", "savings": "bank", "deposit": "bank", "money_market": "bank",
    "ira": "traditional_ira", "rollover_ira": "traditional_ira", "sep_ira": "traditional_ira", "simple_ira": "traditional_ira",
    "401k": "employer_plan", "403b": "employer_plan", "457b": "employer_plan", "tsp": "employer_plan",
    "roth_401k": "roth_employer_plan", "roth_403b": "roth_employer_plan", "roth_457b": "roth_employer_plan",
    "529": "education_529", "afore": "mx_afore", "ppr": "mx_ppr",
    "corporate": "entity", "llc": "entity", "partnership": "entity", "holding_company": "entity",
}


def _vocabulary_key(value: Any) -> str:
    text = str(value).strip().lower().replace("(", "").replace(")", "")
    return re.sub(r"[\s\-/]+", "_", text)


def canonical_account_type(value: Any) -> str | None:
    """Return the canonical account type for ``value`` or ``None`` when unknown."""
    if not isinstance(value, str) or not value.strip():
        return None
    key = _vocabulary_key(value)
    key = ACCOUNT_TYPE_ALIASES.get(key, key)
    return key if key in ACCOUNT_TYPES else None


def account_tax_treatment(value: Any) -> str:
    """``taxable``, ``tax_deferred``, ``tax_exempt``, ``separate_entity`` or ``unknown``."""
    canonical = canonical_account_type(value)
    return ACCOUNT_TYPES[canonical]["tax_treatment"] if canonical else "unknown"


# Asset classes that are illiquid by default regardless of the account that
# holds them.  A position-level ``liquid: true`` may override (for example a
# listed REIT recorded as real_estate); an account-level flag may not.
ILLIQUID_ASSET_CLASSES = frozenset({
    "private_equity", "private_credit", "private_debt", "venture_capital", "venture",
    "hedge_fund", "real_estate", "private_real_estate", "direct_real_estate", "restricted_stock",
    "unlisted", "private", "private_company", "private_fund", "collectible", "collectibles",
    "art", "farmland", "timberland", "private_infrastructure",
})
_FUND_ASSET_CLASSES = frozenset({"fund", "etf", "mutual_fund"})
CONCENTRATION_WARNING = Decimal("0.10")  # employer stock above 10% of known assets (with look-through) is flagged
_REDEMPTION_PERIOD_DAYS = {"daily": 1, "weekly": 7, "monthly": 30, "quarterly": 91, "semiannual": 182, "annual": 365}

# Ownership forms.  For forms in ``_EQUAL_PRESUMPTION_FORMS`` equal economic
# shares are presumed when shares are omitted (disclosed as an assumption).
OWNERSHIP_FORMS = frozenset({
    "sole", "joint_tenancy", "tenancy_by_entirety", "tenancy_in_common",
    "community_property", "sociedad_conyugal", "trust", "entity",
})
_EQUAL_PRESUMPTION_FORMS = frozenset({"joint_tenancy", "tenancy_by_entirety", "community_property", "sociedad_conyugal"})
_SPOUSAL_FORMS = frozenset({"tenancy_by_entirety", "community_property", "sociedad_conyugal"})
UNATTRIBUTED = "unattributed"

# Broad multi-decade plausibility bands, expressed as USD per one unit of the
# currency.  They catch inverted or mis-scaled quotes; they are not prices.
_USD_PER_UNIT_BANDS: dict[str, tuple[Decimal, Decimal]] = {
    "USD": (Decimal(1), Decimal(1)),
    "EUR": (Decimal("0.8"), Decimal("1.7")), "GBP": (Decimal("1.0"), Decimal("2.2")),
    "CHF": (Decimal("0.6"), Decimal("1.5")), "JPY": (Decimal("0.005"), Decimal("0.014")),
    "CAD": (Decimal("0.6"), Decimal("1.1")), "AUD": (Decimal("0.45"), Decimal("1.1")),
    "MXN": (Decimal("0.03"), Decimal("0.12")), "BRL": (Decimal("0.1"), Decimal("0.7")),
    "CNY": (Decimal("0.11"), Decimal("0.17")), "HKD": (Decimal("0.125"), Decimal("0.132")),
    "INR": (Decimal("0.009"), Decimal("0.025")), "KRW": (Decimal("0.0005"), Decimal("0.0011")),
}
FX_RECIPROCAL_TOLERANCE = Decimal("0.01")


def _text(value: Any, field: str, *, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a nonempty string")
    return value.strip()


def _currency(value: Any, field: str) -> str:
    value = _text(value, field)
    if _CURRENCY.fullmatch(value) is None:
        raise ValueError(f"{field} must be three uppercase letters")
    return value


def _number(value: Any, field: str, *, nonnegative: bool = False) -> Decimal:
    if isinstance(value, bool) or value is None or value == "":
        raise ValueError(f"{field} must be a finite number")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be a finite number") from exc
    if not result.is_finite() or (nonnegative and result < 0):
        qualifier = "nonnegative " if nonnegative else ""
        raise ValueError(f"{field} must be a {qualifier}finite number")
    return result


_date = _common.iso_date
_out = _common.decimal_text


def latest_plausible_today() -> date:
    """The later of the local and UTC calendar dates (never rejects a local 'today')."""
    return max(date.today(), datetime.now(timezone.utc).date())


def _unique(items: list[dict[str, Any]], entity: str) -> set[str]:
    seen: set[str] = set()
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            raise ValueError(f"{entity}[{index}] must be an object")
        identifier = _text(item.get("id"), f"{entity}[{index}].id")
        if identifier in seen:
            raise ValueError(f"duplicate {entity} id: {identifier}")
        seen.add(identifier)
    return seen


# --------------------------------------------------------------------------
# Symbols
# --------------------------------------------------------------------------

_EXCHANGE_SUFFIXES = frozenset({
    "MX", "TO", "NE", "CN", "L", "AX", "HK", "DE", "PA", "AS", "SW", "SA", "NS", "BO",
    "KS", "KQ", "SS", "SZ", "MI", "MC", "ST", "OL", "CO", "HE", "BR", "LS", "VI", "IR",
})
_BLOOMBERG_EXCHANGES = frozenset({"US", "UN", "UW", "UQ", "UA", "UR", "MM", "LN", "CN", "CT", "GY", "FP", "NA", "SW", "JP", "JT", "HK", "AU", "BZ"})


def normalize_symbol(value: Any) -> str | None:
    """Normalise a ticker for matching across vendor conventions.

    ``BRK.B``, ``BRK/B``, ``BRK B`` and ``BRK-B`` become ``BRK-B``; BMV series
    markers and venue suffixes are removed (``WALMEX*``, ``WALMEX.MX`` and
    ``WALMEX* MM`` become ``WALMEX``).  Venue stripping can merge same-ticker
    listings on different exchanges, so callers should disclose that matches
    are symbol-based.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().upper()
    text = re.sub(r"\s+EQUITY$", "", text)
    if ":" in text:  # "BMV:WALMEX", "NYSE:BRK.B"
        text = text.split(":", 1)[1].strip()
    parts = text.split()
    if len(parts) == 2 and parts[1] in _BLOOMBERG_EXCHANGES:
        text = parts[0]
    if "." in text:
        base, suffix = text.rsplit(".", 1)
        if base and suffix in _EXCHANGE_SUFFIXES:
            text = base
    text = text.rstrip("*").strip()
    text = re.sub(r"[.\s/]+", "-", text)
    return text or None


# --------------------------------------------------------------------------
# Ownership
# --------------------------------------------------------------------------

def _validate_ownership(item: dict[str, Any], field: str, people: set[str]) -> None:
    ownership = item.get("ownership")
    if ownership is None:
        return
    if not isinstance(ownership, dict):
        raise ValueError(f"{field}.ownership must be an object with form and owners")
    form = _text(ownership.get("form"), f"{field}.ownership.form")
    if form not in OWNERSHIP_FORMS:
        raise ValueError(f"{field}.ownership.form must be one of {', '.join(sorted(OWNERSHIP_FORMS))}")
    owners = ownership.get("owners")
    if not isinstance(owners, list):
        raise ValueError(f"{field}.ownership.owners must be a list")
    if not owners and form not in {"trust", "entity"}:
        raise ValueError(f"{field}.ownership.owners must name at least one person")
    seen: set[str] = set()
    shares: list[Decimal | None] = []
    for index, owner in enumerate(owners):
        if not isinstance(owner, dict):
            raise ValueError(f"{field}.ownership.owners[{index}] must be an object")
        if "pct" in owner or "percent" in owner:
            raise ValueError(f"{field}.ownership.owners[{index}] uses 'share' (a decimal fraction), not pct/percent")
        person = _text(owner.get("person_id"), f"{field}.ownership.owners[{index}].person_id")
        if person not in people:
            raise ValueError(f"{field}.ownership.owners[{index}].person_id does not reference a person")
        if person in seen:
            raise ValueError(f"{field}.ownership lists {person} more than once")
        seen.add(person)
        if owner.get("share") is None:
            shares.append(None)
        else:
            share = _number(owner["share"], f"{field}.ownership.owners[{index}].share", nonnegative=True)
            if share == 0 or share > 1:
                raise ValueError(f"{field}.ownership.owners[{index}].share must be in (0, 1]")
            shares.append(share)
    if form == "sole" and len(owners) != 1:
        raise ValueError(f"{field}.ownership form sole requires exactly one owner")
    if form in _SPOUSAL_FORMS and len(owners) != 2:
        raise ValueError(f"{field}.ownership form {form} requires exactly two spouses")
    supplied = [share for share in shares if share is not None]
    if supplied and len(supplied) != len(shares):
        raise ValueError(f"{field}.ownership must give a share for every owner or for none")
    if not supplied and owners and form not in _EQUAL_PRESUMPTION_FORMS | {"sole"}:
        raise ValueError(f"{field}.ownership form {form} requires explicit shares")
    if supplied and abs(sum(supplied, Decimal(0)) - 1) > Decimal("0.000001"):
        raise ValueError(f"{field}.ownership shares must sum to 1")


def ownership_shares(item: dict[str, Any], *, fallback_owner: str | None = None) -> tuple[list[tuple[str, Decimal]], str | None]:
    """Return ``[(person_id, share)]`` summing to 1, plus an assumption note.

    Records without ``ownership`` are attributed wholly to ``owner_id`` (or
    ``fallback_owner``); records with neither are ``unattributed``.  Household
    totals never double count because shares always sum to one.
    """
    ownership = item.get("ownership")
    if isinstance(ownership, dict):
        owners = ownership.get("owners") or []
        form = ownership.get("form")
        if not owners:
            return [(UNATTRIBUTED, Decimal(1))], f"{form} ownership lists no beneficial owners; value is unattributed."
        if all(owner.get("share") is None for owner in owners):
            share = Decimal(1) / len(owners)
            note = None if len(owners) == 1 else f"Equal economic shares presumed for {form} ownership without stated shares."
            return [(owner["person_id"], share) for owner in owners], note
        return [(owner["person_id"], Decimal(str(owner["share"]))) for owner in owners], None
    owner = item.get("owner_id") or fallback_owner
    if isinstance(owner, str) and owner.strip():
        return [(owner.strip(), Decimal(1))], None
    return [(UNATTRIBUTED, Decimal(1))], None


# --------------------------------------------------------------------------
# FX
# --------------------------------------------------------------------------

def fx_plausibility(source: str, target: str, rate: Decimal) -> str | None:
    """Return ``None`` when plausible, ``"inverted"`` or ``"out_of_range"`` otherwise."""
    if source not in _USD_PER_UNIT_BANDS or target not in _USD_PER_UNIT_BANDS or rate <= 0:
        return None
    source_low, source_high = _USD_PER_UNIT_BANDS[source]
    target_low, target_high = _USD_PER_UNIT_BANDS[target]
    low, high = source_low / target_high, source_high / target_low
    if low <= rate <= high:
        return None
    if low <= Decimal(1) / rate <= high:
        return "inverted"
    return "out_of_range"


def validate_household(household: Any) -> dict[str, Any]:
    """Validate and copy a canonical household, returning data plus scope warnings.

    Unknown external values and liabilities are allowed because unknown is not
    zero.  They are reported by the exposure calculator as excluded coverage.
    See the module docstring for the FX and ownership conventions.
    """
    if not isinstance(household, dict):
        raise ValueError("household must be an object")
    data = deepcopy(household)
    _currency(data.get("currency"), "household.currency")
    household_as_of = _date(data.get("as_of"), "household.as_of")
    if household_as_of > latest_plausible_today():
        raise ValueError("household.as_of cannot be in the future")
    if not isinstance(data.get("complete"), bool):
        raise ValueError("household.complete must be a boolean")

    supplied_unknown = data.get("unknown_sections")
    if supplied_unknown is not None and not (
        isinstance(supplied_unknown, list)
        and all(isinstance(item, str) and item in _ENTITIES for item in supplied_unknown)
        and len(set(supplied_unknown)) == len(supplied_unknown)
    ):
        raise ValueError("household.unknown_sections must contain unique canonical section names")
    unknown_sections = set(supplied_unknown or [])
    infer_unknown = supplied_unknown is None
    warnings: list[str] = []
    for entity in _ENTITIES:
        if entity not in data:
            data[entity] = []
            if infer_unknown:
                unknown_sections.add(entity)
        elif not isinstance(data[entity], list):
            raise ValueError(f"household.{entity} must be a list")
    data["unknown_sections"] = sorted(unknown_sections)
    for entity in data["unknown_sections"]:
        warnings.append(f"{entity} is unknown, not confirmed empty.")
    if not data["complete"]:
        warnings.append("Household is marked incomplete; totals cover known records only.")

    people = _unique(data["people"], "people")
    for index, person in enumerate(data["people"]):
        if "name" in person:
            _text(person["name"], f"people[{index}].name")
        if "tax_residencies" in person and not (
            isinstance(person["tax_residencies"], list)
            and all(isinstance(x, str) and x.strip() for x in person["tax_residencies"])
        ):
            raise ValueError(f"people[{index}].tax_residencies must be a list of strings")

    account_ids = _unique(data["accounts"], "accounts")
    for index, account in enumerate(data["accounts"]):
        owner = _text(account.get("owner_id"), f"accounts[{index}].owner_id")
        if owner not in people:
            raise ValueError(f"accounts[{index}].owner_id does not reference a person")
        account_type = _text(account.get("type"), f"accounts[{index}].type")
        if canonical_account_type(account_type) is None:
            warnings.append(
                f"Account {account['id']} type '{account_type}' is outside the canonical vocabulary; "
                "it is not liquid by default and is not modelled as a taxable account."
            )
        _currency(account.get("currency"), f"accounts[{index}].currency")
        for field in ("liquid", "restricted"):
            if field in account and not isinstance(account[field], bool):
                raise ValueError(f"accounts[{index}].{field} must be a boolean")
        if "restrictions" in account and not (
            isinstance(account["restrictions"], list)
            and all(isinstance(value, str) and value.strip() for value in account["restrictions"])
        ):
            raise ValueError(f"accounts[{index}].restrictions must be a list of strings")
        if "tax_unit" in account:
            _text(account["tax_unit"], f"accounts[{index}].tax_unit")
        _validate_ownership(account, f"accounts[{index}]", people)

    _unique(data["positions"], "positions")
    position_keys: set[tuple[str, str]] = set()
    for index, position in enumerate(data["positions"]):
        account = _text(position.get("account_id"), f"positions[{index}].account_id")
        if account not in account_ids:
            raise ValueError(f"positions[{index}].account_id does not reference an account")
        instrument = _text(position.get("instrument_id"), f"positions[{index}].instrument_id")
        key = (account, instrument)
        if key in position_keys:
            raise ValueError(f"duplicate position for account/instrument: {account}/{instrument}")
        position_keys.add(key)
        _text(position.get("symbol"), f"positions[{index}].symbol")
        _number(position.get("quantity"), f"positions[{index}].quantity", nonnegative=True)
        _number(position.get("value"), f"positions[{index}].value", nonnegative=True)
        _currency(position.get("currency"), f"positions[{index}].currency")
        for field in ("liquid", "restricted", "listed"):
            if field in position and not isinstance(position[field], bool):
                raise ValueError(f"positions[{index}].{field} must be a boolean")
        if "restrictions" in position and not (
            isinstance(position["restrictions"], list)
            and all(isinstance(value, str) and value.strip() for value in position["restrictions"])
        ):
            raise ValueError(f"positions[{index}].restrictions must be a list of strings")
        for field in ("asset_class", "issuer", "sector", "country", "economic_currency"):
            if field in position:
                (_currency if field == "economic_currency" else _text)(position[field], f"positions[{index}].{field}")
        if "lockup_until" in position:
            _date(position["lockup_until"], f"positions[{index}].lockup_until")
        if "redemption" in position:
            redemption = position["redemption"]
            if not isinstance(redemption, dict) or redemption.get("frequency") not in _REDEMPTION_PERIOD_DAYS:
                raise ValueError(f"positions[{index}].redemption.frequency must be one of {', '.join(_REDEMPTION_PERIOD_DAYS)}")
            notice = redemption.get("notice_days", 0)
            if not isinstance(notice, int) or isinstance(notice, bool) or notice < 0:
                raise ValueError(f"positions[{index}].redemption.notice_days must be a nonnegative integer")
            if "gate" in redemption:
                gate = _number(redemption["gate"], f"positions[{index}].redemption.gate", nonnegative=True)
                if gate > 1:
                    raise ValueError(f"positions[{index}].redemption.gate must be a fraction no greater than 1")

    _unique(data["lots"], "lots")
    lot_quantities: dict[tuple[str, str], Decimal] = {}
    for index, lot in enumerate(data["lots"]):
        account = _text(lot.get("account_id"), f"lots[{index}].account_id")
        instrument = _text(lot.get("instrument_id"), f"lots[{index}].instrument_id")
        key = (account, instrument)
        if account not in account_ids:
            raise ValueError(f"lots[{index}].account_id does not reference an account")
        if key not in position_keys:
            raise ValueError(f"lots[{index}] has no matching account/instrument position")
        quantity = _number(lot.get("quantity"), f"lots[{index}].quantity", nonnegative=True)
        lot_quantities[key] = lot_quantities.get(key, Decimal(0)) + quantity
        acquired_on = _date(lot.get("acquired_on"), f"lots[{index}].acquired_on")
        if acquired_on > household_as_of:
            raise ValueError(f"lots[{index}].acquired_on cannot be after household.as_of")
        _number(lot.get("cost_basis"), f"lots[{index}].cost_basis", nonnegative=True)
        _currency(lot.get("currency"), f"lots[{index}].currency")
    position_quantity = {
        (p["account_id"], p["instrument_id"]): _number(p["quantity"], "position.quantity")
        for p in data["positions"]
    }
    for key, quantity in lot_quantities.items():
        if quantity != position_quantity[key]:
            raise ValueError(f"lot quantities do not reconcile to position {key[0]}/{key[1]}")

    for entity in ("liabilities", "external_assets", "income_exposures"):
        _unique(data[entity], entity)
    for index, item in enumerate(data["liabilities"]):
        if item.get("value") not in (None, ""):
            _number(item["value"], f"liabilities[{index}].value", nonnegative=True)
        else:
            warnings.append(f"Liability {item['id']} has unknown value and is excluded from known NAV.")
        _currency(item.get("currency"), f"liabilities[{index}].currency")
        if "monthly_payment" in item:
            _number(item["monthly_payment"], f"liabilities[{index}].monthly_payment", nonnegative=True)
        if "owner_id" in item and _text(item["owner_id"], f"liabilities[{index}].owner_id") not in people:
            raise ValueError(f"liabilities[{index}].owner_id does not reference a person")
        _validate_ownership(item, f"liabilities[{index}]", people)
    for index, item in enumerate(data["external_assets"]):
        _text(item.get("name"), f"external_assets[{index}].name")
        if item.get("value") not in (None, ""):
            _number(item["value"], f"external_assets[{index}].value", nonnegative=True)
        else:
            warnings.append(f"External asset {item['id']} has unknown value and is excluded from known totals.")
        _currency(item.get("currency"), f"external_assets[{index}].currency")
        if not isinstance(item.get("liquid"), bool):
            raise ValueError(f"external_assets[{index}].liquid must be a boolean")
        for field in ("asset_class", "sector", "country"):
            if field in item:
                _text(item[field], f"external_assets[{index}].{field}")
        if "owner_id" in item and _text(item["owner_id"], f"external_assets[{index}].owner_id") not in people:
            raise ValueError(f"external_assets[{index}].owner_id does not reference a person")
        _validate_ownership(item, f"external_assets[{index}]", people)
    for index, item in enumerate(data["income_exposures"]):
        _text(item.get("description"), f"income_exposures[{index}].description")
        _currency(item.get("currency"), f"income_exposures[{index}].currency")
        for field in ("sector", "country", "employer_instrument_id"):
            if field in item:
                _text(item[field], f"income_exposures[{index}].{field}")
        if "annual_amount" in item:
            _number(item["annual_amount"], f"income_exposures[{index}].annual_amount", nonnegative=True)

    fx_rates: dict[tuple[str, str], Decimal] = {}
    for index, item in enumerate(data["fx"]):
        if not isinstance(item, dict):
            raise ValueError(f"fx[{index}] must be an object")
        source, target = _currency(item.get("from"), f"fx[{index}].from"), _currency(item.get("to"), f"fx[{index}].to")
        if source == target or (source, target) in fx_rates:
            raise ValueError(f"duplicate or identity FX pair: {source}/{target}")
        rate = _number(item.get("rate"), f"fx[{index}].rate")
        if rate <= 0:
            raise ValueError(f"fx[{index}].rate must be positive")
        fx_rates[(source, target)] = rate
        fx_as_of = _date(item.get("as_of"), f"fx[{index}].as_of")
        if fx_as_of > household_as_of:
            raise ValueError(f"fx[{index}].as_of cannot be after household.as_of")
        _text(item.get("source"), f"fx[{index}].source")
        if "direction_verified" in item and not isinstance(item["direction_verified"], bool):
            raise ValueError(f"fx[{index}].direction_verified must be a boolean")
        verdict = fx_plausibility(source, target, rate)
        if verdict and item.get("direction_verified") is not True:
            if verdict == "inverted":
                warnings.append(
                    f"FX {source}->{target} rate {_out(rate)} looks inverted (1 {source} = {_out(rate)} {target} is implausible; "
                    f"the inverse {_out(Decimal(1) / rate)} is plausible). It is excluded until corrected or marked direction_verified."
                )
            else:
                warnings.append(f"FX {source}->{target} rate {_out(rate)} is outside the broad plausibility band; check the quote.")
    for (source, target), rate in fx_rates.items():
        reverse = fx_rates.get((target, source))
        if reverse is not None and source < target and abs(rate * reverse - 1) > FX_RECIPROCAL_TOLERANCE:
            raise ValueError(
                f"inconsistent reciprocal FX quotes {source}->{target} {_out(rate)} and {target}->{source} {_out(reverse)}; "
                f"their product must be within {_out(FX_RECIPROCAL_TOLERANCE * 100)}% of 1"
            )

    fund_ids: set[str] = set()
    for index, fund in enumerate(data["fund_holdings"]):
        if not isinstance(fund, dict):
            raise ValueError(f"fund_holdings[{index}] must be an object")
        instrument = _text(fund.get("instrument_id"), f"fund_holdings[{index}].instrument_id")
        if instrument in fund_ids:
            raise ValueError(f"duplicate fund holdings instrument_id: {instrument}")
        fund_ids.add(instrument)
        fund_as_of = _date(fund.get("as_of"), f"fund_holdings[{index}].as_of")
        if fund_as_of > household_as_of:
            raise ValueError(f"fund_holdings[{index}].as_of cannot be after household.as_of")
        _text(fund.get("source"), f"fund_holdings[{index}].source")
        holdings = fund.get("holdings")
        if not isinstance(holdings, list):
            raise ValueError(f"fund_holdings[{index}].holdings must be a list")
        total = Decimal(0)
        children: set[str] = set()
        for child_index, holding in enumerate(holdings):
            if not isinstance(holding, dict):
                raise ValueError(f"fund_holdings[{index}].holdings[{child_index}] must be an object")
            child = _text(holding.get("instrument_id"), f"fund_holdings[{index}].holdings[{child_index}].instrument_id")
            if child in children:
                raise ValueError(f"duplicate holding {child} in fund {instrument}")
            children.add(child)
            weight = _number(holding.get("weight"), f"fund_holdings[{index}].holdings[{child_index}].weight", nonnegative=True)
            total += weight
            for field in ("symbol", "issuer", "sector", "country", "asset_class"):
                if field in holding:
                    _text(holding[field], f"fund holding {child}.{field}")
            if "economic_currency" in holding:
                _currency(holding["economic_currency"], f"fund holding {child}.economic_currency")
        if total > 1:
            raise ValueError(f"fund holdings weights exceed 1 for {instrument}")

    return {"household": data, "warnings": warnings}


def _decode_cell(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    stripped = value.strip()
    if stripped == "":
        return None
    if stripped[:1] in "[{" or stripped in ("true", "false", "null"):
        try:
            return json.loads(stripped)
        except json.JSONDecodeError:
            pass
    return value


def _mapped_record(row: dict[str, Any], mapping: dict[str, Any], defaults: dict[str, Any]) -> dict[str, Any]:
    result = deepcopy(defaults)
    for canonical, source_column in mapping.items():
        if not isinstance(source_column, str):
            raise ValueError(f"mapping for {canonical} must name a source column")
        if source_column in row and row[source_column] not in (None, ""):
            result[canonical] = _decode_cell(row[source_column])
    return result


def _tabular_household(inputs: dict[str, Any], tables: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    mapping = inputs.get("mapping")
    defaults = inputs.get("defaults", {})
    if not isinstance(mapping, dict) or not isinstance(defaults, dict):
        raise ValueError("tabular import requires object mapping and defaults")
    metadata = defaults.get("household", {})
    if not isinstance(metadata, dict):
        raise ValueError("defaults.household must be an object")
    household = deepcopy(metadata)
    for entity in _ENTITIES:
        household[entity] = []

    if "rows" in tables:
        type_column = mapping.get("record_type")
        if not isinstance(type_column, str):
            raise ValueError("CSV mapping.record_type must name the record type column")
        grouped = {entity: [] for entity in _ENTITIES}
        for row_number, row in enumerate(tables["rows"], 2):
            entity = row.get(type_column)
            if entity not in grouped:
                raise ValueError(f"CSV row {row_number} has unknown record type: {entity}")
            grouped[entity].append(row)
        tables = grouped

    if "unknown_sections" not in household:
        household["unknown_sections"] = sorted(entity for entity in _ENTITIES if entity not in tables or not tables[entity])

    for entity in _ENTITIES:
        entity_mapping = mapping.get(entity, {})
        entity_defaults = defaults.get(entity, {})
        if not isinstance(entity_mapping, dict) or not isinstance(entity_defaults, dict):
            raise ValueError(f"mapping/defaults for {entity} must be objects")
        household[entity] = [
            _mapped_record(row, entity_mapping, entity_defaults) for row in tables.get(entity, [])
        ]
    return household


def _import_household(inputs: dict[str, Any]) -> tuple[dict[str, Any], list[str], list[str]]:
    if "household" in inputs:
        checked = validate_household(inputs["household"])
        return checked["household"], checked["warnings"], [str(inputs.get("source", "request.household"))]
    format_name = inputs.get("format")
    if format_name not in ("json", "csv", "xlsx"):
        raise ValueError("format must be json, csv, or xlsx")
    path = inputs.get("path")
    data = inputs.get("data")
    if (path is None) == (data is None):
        raise ValueError("provide exactly one of path or data")
    default_source = str(path) if path is not None else f"request.{format_name}"
    source = _text(inputs.get("source", default_source), "source")
    if format_name == "json":
        if path is not None:
            parsed = json.loads(Path(path).read_text(encoding="utf-8"))
        elif isinstance(data, str):
            parsed = json.loads(data)
        else:
            parsed = data
        checked = validate_household(parsed)
        return checked["household"], checked["warnings"], [source]
    if format_name == "csv":
        if path is not None:
            with Path(path).open(encoding="utf-8-sig", newline="") as handle:
                rows = list(csv.DictReader(handle))
        elif isinstance(data, str):
            rows = list(csv.DictReader(io.StringIO(data)))
        else:
            raise ValueError("CSV data must be text")
        imported = _tabular_household(inputs, {"rows": rows})
    else:
        try:
            from openpyxl import load_workbook
        except ImportError as exc:  # pragma: no cover - depends on optional extra
            raise ValueError("XLSX import requires the analytics extra (openpyxl)") from exc
        if path is not None:
            workbook = load_workbook(path, read_only=True, data_only=True)
        elif isinstance(data, (bytes, bytearray)):
            workbook = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        else:
            raise ValueError("XLSX data must be bytes")
        sheet_names = inputs.get("sheets", {})
        if not isinstance(sheet_names, dict):
            raise ValueError("sheets must be an object mapping entity to sheet name")
        tables: dict[str, list[dict[str, Any]]] = {}
        for entity in _ENTITIES:
            sheet_name = sheet_names.get(entity, entity)
            if sheet_name not in workbook.sheetnames:
                continue
            values = workbook[sheet_name].iter_rows(values_only=True)
            headers = next(values, None)
            if headers is None:
                tables[entity] = []
                continue
            tables[entity] = [dict(zip(headers, row)) for row in values]
        imported = _tabular_household(inputs, tables)
    checked = validate_household(imported)
    return checked["household"], checked["warnings"], [source]


# --------------------------------------------------------------------------
# Evaluation date, FX and look-through (shared with wealth.research)
# --------------------------------------------------------------------------

def evaluation_date(inputs: dict[str, Any], household_as_of: date) -> tuple[date, str | None]:
    """Return the date against which data freshness is judged.

    Freshness is measured at evaluation time, not at ``household.as_of``: a
    household snapshot does not stay current because its FX is as old as it is.
    """
    if inputs.get("evaluation_date") is not None:
        value = _date(inputs["evaluation_date"], "evaluation_date")
        if value < household_as_of:
            raise ValueError("evaluation_date cannot precede household.as_of")
        if value > latest_plausible_today():
            raise ValueError("evaluation_date cannot be in the future")
        return value, None
    today = max(date.today(), household_as_of)
    return today, f"evaluation_date defaulted to the local calendar date {today.isoformat()}."


@dataclass
class FxConverter:
    """Order-independent FX conversion over the fresh, plausible household quotes."""

    quotes: dict[tuple[str, str], tuple[Decimal, dict[str, Any]]]
    stale: set[tuple[str, str]]
    rejected: set[tuple[str, str]]
    warnings: list[str]
    used: dict[tuple[str, str], dict[str, Any]] = dataclass_field(default_factory=dict)

    def rate(self, source: str, target: str) -> Decimal | None:
        if source == target:
            return Decimal(1)
        if (source, target) in self.quotes:
            return self.quotes[(source, target)][0]
        if (target, source) in self.quotes:
            return Decimal(1) / self.quotes[(target, source)][0]
        return None

    def convert(self, value: Decimal, source: str, target: str, label: str) -> Decimal | None:
        rate = self.rate(source, target)
        if rate is None:
            pair = {(source, target), (target, source)}
            if pair & self.rejected:
                reason = "implausible (possibly inverted)"
            elif pair & self.stale:
                reason = "stale"
            else:
                reason = "missing"
            self.warnings.append(f"Excluded {label}: {reason} FX for {source}/{target}.")
            return None
        if source != target:
            quoted = (source, target) if (source, target) in self.quotes else (target, source)
            record = self.quotes[quoted][1]
            self.used[(source, target)] = {
                "from": source, "to": target, "rate": _out(rate),
                "meaning": f"1 {source} = {_out(rate)} {target}",
                "quoted_as": f"{quoted[0]}->{quoted[1]}", "as_of": record["as_of"], "source": record["source"],
            }
        return value * rate

    def used_rows(self) -> list[dict[str, Any]]:
        return [self.used[key] for key in sorted(self.used)]


def fx_converter(household: dict[str, Any], as_of: date, max_age: int, warnings: list[str]) -> FxConverter:
    quotes: dict[tuple[str, str], tuple[Decimal, dict[str, Any]]] = {}
    stale: set[tuple[str, str]] = set()
    rejected: set[tuple[str, str]] = set()
    for item in household["fx"]:
        key = (item["from"], item["to"])
        if (as_of - _date(item["as_of"], "fx.as_of")).days > max_age:
            stale.add(key)
            warnings.append(f"Excluded stale FX {key[0]}/{key[1]} dated {item['as_of']}.")
            continue
        rate = _number(item["rate"], "fx.rate")
        if fx_plausibility(key[0], key[1], rate) == "inverted" and item.get("direction_verified") is not True:
            rejected.add(key)
            continue
        quotes[key] = (rate, item)
    return FxConverter(quotes, stale, rejected, warnings)


def is_fund(metadata: dict[str, Any]) -> bool:
    return _vocabulary_key(metadata.get("asset_class", "")) in _FUND_ASSET_CLASSES


@dataclass(frozen=True)
class Leaf:
    """One look-through leaf.  ``kind`` is ``instrument``, ``match``,
    ``opaque_fund`` (fund without current holdings), ``residual`` (unreported
    fund weight) or ``cycle``.  The last three are explicit unknown exposure."""

    weight: Decimal
    instrument_id: str
    metadata: dict[str, Any]
    path: tuple[str, ...]
    kind: str

    @property
    def unknown(self) -> bool:
        return self.kind in {"opaque_fund", "residual", "cycle"}


def lookthrough(
    instrument_id: str,
    metadata: dict[str, Any],
    funds: dict[str, dict[str, Any]],
    *,
    opaque: set[str] | frozenset[str] = frozenset(),
    stop: Callable[[str, dict[str, Any]], bool] | None = None,
    _path: tuple[str, ...] = (),
) -> list[Leaf]:
    """Expand ``instrument_id`` through current fund holdings.

    Unreported fund weight becomes an explicit ``residual`` leaf; funds with no
    current holdings (stale, missing, or listed in ``opaque``) remain an
    ``opaque_fund`` leaf.  ``stop`` ends expansion at a matching node.
    """
    path = _path + (instrument_id,)
    if stop is not None and stop(instrument_id, metadata):
        return [Leaf(Decimal(1), instrument_id, metadata, path, "match")]
    if instrument_id in _path:
        return [Leaf(Decimal(1), f"unknown:cycle:{instrument_id}", {"asset_class": "unknown"}, path, "cycle")]
    fund = funds.get(instrument_id)
    if fund is None:
        kind = "opaque_fund" if instrument_id in opaque or is_fund(metadata) else "instrument"
        return [Leaf(Decimal(1), instrument_id, metadata, path, kind)]
    leaves: list[Leaf] = []
    total = Decimal(0)
    # A fund whose own asset class is known (e.g. an all-equity ETF) labels holdings that carry none, and its
    # unreported residual: the names are unknown, the asset class is not.
    fund_class = fund.get("asset_class") if isinstance(fund.get("asset_class"), str) else None
    for holding in fund["holdings"]:
        weight = _number(holding["weight"], "holding.weight")
        total += weight
        child_meta = holding if holding.get("asset_class") or not fund_class else {**holding, "asset_class": fund_class}
        for child in lookthrough(holding["instrument_id"], child_meta, funds, opaque=opaque, stop=stop, _path=path):
            leaves.append(Leaf(weight * child.weight, child.instrument_id, child.metadata, child.path, child.kind))
    if total < 1:
        leaves.append(Leaf(Decimal(1) - total, f"unknown:residual:{instrument_id}",
                           {"asset_class": fund_class or "unknown"}, path, "residual"))
    return leaves


def current_funds(household: dict[str, Any], as_of: date, max_age: int, warnings: list[str]) -> tuple[dict[str, dict[str, Any]], set[str]]:
    fresh: dict[str, dict[str, Any]] = {}
    stale: set[str] = set()
    for fund in household["fund_holdings"]:
        age = (as_of - _date(fund["as_of"], "fund_holdings.as_of")).days
        if age > max_age:
            warnings.append(f"Fund holdings for {fund['instrument_id']} are stale ({fund['as_of']}); look-through excluded.")
            stale.add(fund["instrument_id"])
        else:
            fresh[fund["instrument_id"]] = fund
    return fresh, stale


def _targets(inputs: dict[str, Any]) -> list[dict[str, Any]]:
    raw = inputs.get("targets", [])
    if isinstance(raw, dict):
        expanded = []
        for dimension, values in raw.items():
            if not isinstance(values, dict):
                raise ValueError(f"targets.{dimension} must be an object")
            expanded.extend({"dimension": dimension, "name": name, "target_weight": value} for name, value in values.items())
        raw = expanded
    if not isinstance(raw, list):
        raise ValueError("targets must be a list or dimension mapping")
    result = []
    for index, target in enumerate(raw):
        if not isinstance(target, dict):
            raise ValueError(f"targets[{index}] must be an object")
        dimension = _text(target.get("dimension"), f"targets[{index}].dimension")
        if dimension not in ("asset_class", "issuer", "sector", "country", "economic_currency", "account", "person"):
            raise ValueError(f"unsupported target dimension: {dimension}")
        name = _text(target.get("name"), f"targets[{index}].name")
        weight = _number(target.get("target_weight"), f"targets[{index}].target_weight", nonnegative=True)
        if weight > 1:
            raise ValueError(f"targets[{index}].target_weight cannot exceed 1")
        result.append({"dimension": dimension, "name": name, "target_weight": weight})
    return result


def _whole_days(inputs: dict[str, Any], key: str, default: int) -> int:
    value = _number(inputs.get(key, default), key, nonnegative=True)
    if value != value.to_integral_value():
        raise ValueError(f"{key} must be a whole number of days")
    return int(value)


def _position_liquidity(position: dict[str, Any], account: dict[str, Any], as_of: date, horizon: int) -> tuple[Decimal, str]:
    """Return the liquid fraction of a position within ``horizon`` days and why."""
    def unrestricted(item: dict[str, Any]) -> bool:
        return not item.get("restricted", False) and not item.get("restrictions", [])

    if not unrestricted(position) or not unrestricted(account):
        return Decimal(0), "restricted"
    if position.get("lockup_until") and _date(position["lockup_until"], "position.lockup_until") > as_of:
        return Decimal(0), "lockup"
    if position.get("liquid") is False or account.get("liquid") is False:
        return Decimal(0), "declared_illiquid"
    redemption = position.get("redemption")
    if isinstance(redemption, dict):
        days = int(redemption.get("notice_days", 0)) + _REDEMPTION_PERIOD_DAYS[redemption["frequency"]]
        if days > horizon:
            return Decimal(0), "redemption_terms_exceed_horizon"
        gate = _number(redemption.get("gate", 1), "redemption.gate")
        return gate, "redeemable_within_horizon" if gate == 1 else "redemption_gated"
    inherently_illiquid = _vocabulary_key(position.get("asset_class", "")) in ILLIQUID_ASSET_CLASSES or position.get("listed") is False
    if inherently_illiquid:
        if position.get("liquid") is True:
            return Decimal(1), "declared_liquid"
        return Decimal(0), "unlisted" if position.get("listed") is False else "illiquid_asset_class"
    if position.get("liquid") is True:
        return Decimal(1), "declared_liquid"
    if account.get("liquid") is True:
        return Decimal(1), "account_declared_liquid"
    canonical = canonical_account_type(account["type"])
    if canonical is not None and ACCOUNT_TYPES[canonical]["liquid_by_default"]:
        return Decimal(1), f"account_type_default:{canonical}"
    return Decimal(0), "account_type_not_liquid" if canonical else "unknown_account_type"


def _auto_lookthrough(household: dict[str, Any], fresh: dict[str, dict[str, Any]], stale: set[str],
                      inputs: dict[str, Any], context: dict[str, Any] | None, as_of: date, max_age: int,
                      warnings: list[str], assumptions: list[str]) -> list[dict[str, Any]]:
    """Fill fund holdings the household did not supply from :func:`wealth.research.fund_holdings_for`.

    Saved ``research.<SYMBOL>`` fund packets are read offline; a live Yahoo pull happens only for positions
    classed as funds, when market data is online (``WEALTH_OFFLINE`` unset) and ``live_lookthrough`` is not
    false.  Holding symbols that match a household position are mapped to its instrument id so direct and
    indirect exposure to the same company add up.  Returns one row per fund tried.
    """
    from . import research
    from .prices import offline_mode

    allow_live = inputs.get("live_lookthrough", True) is not False and not offline_mode()
    by_symbol: dict[str, str] = {}
    for position in household["positions"]:
        symbol = normalize_symbol(position.get("symbol") or position["instrument_id"])
        if symbol:
            by_symbol.setdefault(symbol, position["instrument_id"])
    rows: list[dict[str, Any]] = []
    tried: set[str] = set()
    for position in household["positions"]:
        instrument = position["instrument_id"]
        if instrument in fresh or instrument in tried:
            continue
        symbol = normalize_symbol(position.get("symbol") or instrument) or instrument
        fundlike = is_fund(position)
        saved = isinstance((context or {}).get(f"research.{symbol}"), dict)
        if not fundlike and not saved:
            continue
        tried.add(instrument)
        record, why = research.fund_holdings_for(symbol, context, live=allow_live and fundlike, today=as_of)
        if record is None:
            rows.append({"instrument_id": instrument, "symbol": symbol, "status": "unavailable", "reason": why})
            continue
        record_as_of = _date(record["as_of"], "fund holdings as_of") if record.get("as_of") else None
        if record_as_of is not None and (as_of - record_as_of).days > max_age:
            warnings.append(f"Researched holdings for {symbol} are stale ({record['as_of']}); look-through excluded.")
            rows.append({"instrument_id": instrument, "symbol": symbol, "status": "stale", "as_of": record["as_of"],
                         "source": record["source"]})
            continue
        holdings = [{**h, "instrument_id": by_symbol.get(h["symbol"], h["instrument_id"])} for h in record["holdings"]]
        fresh[instrument] = {"instrument_id": instrument, "as_of": record["as_of"], "source": record["source"],
                             "holdings": holdings, "asset_class": record.get("asset_class")}
        stale.discard(instrument)
        reported = sum((Decimal(h["weight"]) for h in holdings), Decimal(0))
        rows.append({"instrument_id": instrument, "symbol": symbol, "status": "used", "origin": record["origin"],
                     "as_of": record["as_of"], "source": record["source"], "holdings": len(holdings),
                     "reported_weight": _out(reported), "asset_class": record.get("asset_class"),
                     "note": record.get("note")})
    if any(r["status"] == "used" for r in rows):
        assumptions.append("Fund holdings not supplied in the household came from research (saved research packets, or "
                           "a live Yahoo pull only when market data is online); unreported fund weight stays an "
                           "explicit unknown residual, labelled with the fund's asset class when that is known.")
    return rows


def _exposure(household: dict[str, Any], inputs: dict[str, Any], base_warnings: list[str],
              context: dict[str, Any] | None = None) -> dict[str, Any]:
    warnings = list(base_warnings)
    assumptions: list[str] = []
    reporting = household["currency"]
    household_as_of = _date(household["as_of"], "household.as_of")
    as_of, date_note = evaluation_date(inputs, household_as_of)
    if date_note:
        assumptions.append(date_note)
    max_fx_age = _whole_days(inputs, "max_fx_age_days", 7)
    max_fund_age = _whole_days(inputs, "max_fund_age_days", 90)
    max_household_age = _whole_days(inputs, "max_household_age_days", 31)
    horizon = _whole_days(inputs, "liquidity_horizon_days", 30)
    household_age = (as_of - household_as_of).days
    household_stale = household_age > max_household_age
    if household_stale:
        warnings.append(
            f"Household snapshot is stale: as_of {household['as_of']} is {household_age} days before evaluation date "
            f"{as_of.isoformat()} (limit {max_household_age} days)."
        )
    fx = fx_converter(household, as_of, max_fx_age, warnings)
    fresh_funds, stale_funds = current_funds(household, as_of, max_fund_age, warnings)
    supplied_funds = set(fresh_funds)
    auto_rows = _auto_lookthrough(household, fresh_funds, stale_funds, inputs, context, as_of, max_fund_age,
                                  warnings, assumptions) if inputs.get("auto_lookthrough", True) is not False else []

    accounts = {account["id"]: account for account in household["accounts"]}
    exposure: dict[str, dict[str, Decimal]] = {
        dimension: {} for dimension in ("asset_class", "issuer", "sector", "country", "economic_currency", "account", "person", "instrument")
    }
    person_assets: dict[str, Decimal] = {person["id"]: Decimal(0) for person in household["people"]}
    person_liabilities: dict[str, Decimal] = {person["id"]: Decimal(0) for person in household["people"]}
    total_positions = Decimal(0)
    liquid_positions = Decimal(0)
    liquidity_by_reason: dict[str, Decimal] = {}
    nonliquid_position_records = 0
    leaves_by_position: dict[str, dict[str, Decimal]] = {}
    direct_value: dict[str, Decimal] = {}
    via_value: dict[str, dict[str, Decimal]] = {}
    incomplete_lookthrough = False
    excluded_value_records = 0
    unattributed_value_records = 0

    def add(dimension: str, name: Any, value: Decimal) -> None:
        label = str(name) if name not in (None, "") else "unknown"
        exposure[dimension][label] = exposure[dimension].get(label, Decimal(0)) + value

    def attribute(item: dict[str, Any], value: Decimal, target: dict[str, Decimal], label: str, *, fallback: str | None = None) -> None:
        nonlocal unattributed_value_records
        shares, note = ownership_shares(item, fallback_owner=fallback)
        if note:
            assumptions.append(note)
        for person, share in shares:
            if person == UNATTRIBUTED:
                unattributed_value_records += 1
                warnings.append(f"{label} has no stated owner; its value is unattributed rather than assigned to a person.")
            target[person] = target.get(person, Decimal(0)) + value * share
            if target is person_assets:
                add("person", person, value * share)

    for position in household["positions"]:
        value = fx.convert(_number(position["value"], "position.value"), position["currency"], reporting, f"position {position['id']}")
        if value is None:
            excluded_value_records += 1
            continue
        account = accounts[position["account_id"]]
        total_positions += value
        fraction, reason = _position_liquidity(position, account, as_of, horizon)
        liquid_positions += value * fraction
        liquidity_by_reason[reason] = liquidity_by_reason.get(reason, Decimal(0)) + value
        if fraction < 1:
            nonliquid_position_records += 1
        add("account", position["account_id"], value)
        attribute(account, value, person_assets, f"Account {account['id']}")
        leaf_map: dict[str, Decimal] = {}
        for leaf in lookthrough(position["instrument_id"], position, fresh_funds, opaque=stale_funds):
            if leaf.unknown:
                incomplete_lookthrough = True
                if leaf.kind == "opaque_fund" and leaf.instrument_id not in stale_funds:
                    warnings.append(f"No current fund holdings supplied for {leaf.instrument_id}; kept as a direct fund exposure.")
            leaf_value = value * leaf.weight
            leaf_map[leaf.instrument_id] = leaf_map.get(leaf.instrument_id, Decimal(0)) + leaf.weight
            if len(leaf.path) == 1:
                direct_value[leaf.instrument_id] = direct_value.get(leaf.instrument_id, Decimal(0)) + leaf_value
            else:
                via = via_value.setdefault(leaf.instrument_id, {})
                via[leaf.path[0]] = via.get(leaf.path[0], Decimal(0)) + leaf_value
            add("instrument", leaf.instrument_id, leaf_value)
            for dimension in ("asset_class", "issuer", "sector", "country", "economic_currency"):
                add(dimension, leaf.metadata.get(dimension), leaf_value)
        leaves_by_position[position["id"]] = leaf_map

    external_total = Decimal(0)
    liquid_external = Decimal(0)
    for item in household["external_assets"]:
        if item.get("value") in (None, ""):
            excluded_value_records += 1
            continue
        value = fx.convert(_number(item["value"], "external_asset.value"), item["currency"], reporting, f"external asset {item['id']}")
        if value is None:
            excluded_value_records += 1
            continue
        external_total += value
        if item["liquid"]:
            liquid_external += value
        liquidity_by_reason["external_declared_liquid" if item["liquid"] else "external_declared_illiquid"] = (
            liquidity_by_reason.get("external_declared_liquid" if item["liquid"] else "external_declared_illiquid", Decimal(0)) + value
        )
        add("account", "external", value)
        attribute(item, value, person_assets, f"External asset {item['id']}")
        add("instrument", f"external:{item['id']}", value)
        for dimension in ("asset_class", "issuer", "sector", "country", "economic_currency"):
            add(dimension, item.get(dimension), value)

    known_liabilities = Decimal(0)
    unknown_liabilities = 0
    for item in household["liabilities"]:
        if item.get("value") in (None, ""):
            unknown_liabilities += 1
            continue
        value = fx.convert(_number(item["value"], "liability.value"), item["currency"], reporting, f"liability {item['id']}")
        if value is None:
            unknown_liabilities += 1
        else:
            known_liabilities += value
            attribute(item, value, person_liabilities, f"Liability {item['id']}")

    known_assets = total_positions + external_total
    known_nav = known_assets - known_liabilities
    liquid_capital = liquid_positions + liquid_external
    denominator = known_assets
    dimensions = {}
    for dimension, values in exposure.items():
        dimensions[dimension] = [
            {
                "name": name,
                "value": _out(value),
                "weight_of_known_assets": _out(value / denominator) if denominator else None,
            }
            for name, value in sorted(values.items(), key=lambda item: (-item[1], item[0]))
        ]
    people_rows = [
        {
            "person_id": person,
            "attributed_assets": _out(person_assets.get(person, Decimal(0))),
            "attributed_liabilities": _out(person_liabilities.get(person, Decimal(0))),
            "attributed_nav": _out(person_assets.get(person, Decimal(0)) - person_liabilities.get(person, Decimal(0))),
        }
        for person in sorted(set(person_assets) | set(person_liabilities), key=lambda key: (key == UNATTRIBUTED, key))
    ]

    income_rows = []
    economic_links = []
    income_gap_count = 0
    for item in household["income_exposures"]:
        gaps = []
        annual_amount = None
        if item.get("annual_amount") in (None, ""):
            gaps.append("annual_amount is unknown")
        else:
            annual_amount = {"amount": _out(_number(item["annual_amount"], "income_exposure.annual_amount")), "currency": item["currency"]}
        classifications = {
            "sector": item.get("sector"),
            "country": item.get("country"),
            "economic_currency": item["currency"],
        }
        links = []
        for dimension, name in classifications.items():
            if name in (None, ""):
                gaps.append(f"{dimension} classification is unknown")
                continue
            known_value = exposure[dimension].get(str(name), Decimal(0))
            links.append({
                "dimension": dimension,
                "name": str(name),
                "known_asset_value": _out(known_value),
                "known_assets_weight": _out(known_value / denominator) if denominator else None,
                "weight_basis": "known_assets",
            })
        income_gap_count += len(gaps)
        income_rows.append({
            "id": item["id"], "description": item["description"],
            "annual_amount": annual_amount,
            "sector": item.get("sector"), "country": item.get("country"), "currency": item["currency"],
            "gaps": gaps,
        })
        economic_links.append({
            "income_exposure_id": item["id"], "links": links, "gaps": gaps,
            "interpretation": "Shared labels show measured household concentration; they do not establish correlation or causation.",
        })

    overlaps = []
    position_list = [p for p in household["positions"] if p["id"] in leaves_by_position]
    for left_index, left in enumerate(position_list):
        for right in position_list[left_index + 1:]:
            left_map, right_map = leaves_by_position[left["id"]], leaves_by_position[right["id"]]
            shared = sorted(key for key in set(left_map) & set(right_map) if not key.startswith("unknown:"))
            overlap = sum((min(left_map[key], right_map[key]) for key in shared), Decimal(0))
            if overlap:
                overlaps.append({
                    "left_position_id": left["id"], "right_position_id": right["id"],
                    "overlap_weight": _out(overlap), "shared_instruments": shared,
                })

    top_underlying = []
    for name in sorted(set(direct_value) | set(via_value),
                       key=lambda k: (-(direct_value.get(k, Decimal(0)) + sum(via_value.get(k, {}).values())), k)):
        if name.startswith("unknown:"):
            continue
        via = via_value.get(name, {})
        total = direct_value.get(name, Decimal(0)) + sum(via.values(), Decimal(0))
        top_underlying.append({
            "instrument_id": name, "total_value": _out(total), "direct_value": _out(direct_value.get(name, Decimal(0))),
            "via_funds_value": _out(sum(via.values(), Decimal(0))),
            "via_funds": [{"fund": fund, "value": _out(v)} for fund, v in sorted(via.items(), key=lambda i: (-i[1], i[0]))],
            "weight_of_known_assets": _out(total / denominator) if denominator else None,
        })
        if len(top_underlying) >= 15:
            break
    looked = [p for p in position_list if len(leaves_by_position[p["id"]]) > 1
              or any(k != p["instrument_id"] for k in leaves_by_position[p["id"]])]
    overlap_matrix = None
    if len(looked) >= 2:
        labels = [p["id"] for p in looked]
        rows_m = []
        for left in looked:
            row = []
            for right in looked:
                if left is right:
                    row.append("1")
                    continue
                lm, rm = leaves_by_position[left["id"]], leaves_by_position[right["id"]]
                shared = [k for k in set(lm) & set(rm) if not k.startswith("unknown:")]
                row.append(_out(sum((min(lm[k], rm[k]) for k in shared), Decimal(0))))
            rows_m.append(row)
        overlap_matrix = {"positions": labels, "instruments": [p["instrument_id"] for p in looked], "matrix": rows_m,
                          "measure": "sum over shared holdings of the smaller weight (0 = no overlap, 1 = identical); "
                                     "unreported residual weight is not counted, so overlap is a lower bound"}
    employer_rows = []
    for item in household["income_exposures"]:
        employer = item.get("employer_instrument_id")
        if not employer:
            continue
        key = next((iid for iid in set(direct_value) | set(via_value)
                    if iid == employer or normalize_symbol(iid) == normalize_symbol(employer)), employer)
        direct = direct_value.get(key, Decimal(0))
        via = sum(via_value.get(key, {}).values(), Decimal(0))
        stock = direct + via
        salary = None
        salary_reporting = None
        if item.get("annual_amount") not in (None, ""):
            salary = _number(item["annual_amount"], "income_exposure.annual_amount")
            salary_reporting = fx.convert(salary, item["currency"], reporting, f"income exposure {item['id']}")
        row = {
            "income_exposure_id": item["id"], "employer_instrument_id": employer,
            "direct_value": _out(direct), "via_funds_value": _out(via), "stock_value": _out(stock),
            "stock_weight_of_known_assets": _out(stock / denominator) if denominator else None,
            "annual_income": {"amount": _out(salary), "currency": item["currency"]} if salary is not None else None,
            "annual_income_in_reporting_currency": _out(salary_reporting) if salary_reporting is not None else None,
            "at_risk_if_employer_fails": _out(stock + salary_reporting) if salary_reporting is not None else None,
            "stock_to_income_years": _out((stock / salary_reporting).quantize(Decimal("0.01")))
            if salary_reporting else None,
            "lookthrough_complete": not incomplete_lookthrough,
            "reason": None if salary_reporting is not None else (
                "annual_amount is unknown" if salary is None else f"no FX {item['currency']}->{reporting}"),
            "interpretation": "The same employer pays the salary and issues the stock: a failure hits both at once. "
                              "at_risk adds one year of income to the stock value; it is a scale, not a forecast.",
        }
        if denominator and stock / denominator > CONCENTRATION_WARNING:
            warnings.append(f"{employer} (your employer) is {_out((stock / denominator * 100).quantize(Decimal('0.1')))}% "
                            "of known assets including fund look-through, on top of your salary.")
        employer_rows.append(row)

    target_rows = []
    unknown_asset_sections = sorted(set(household["unknown_sections"]) & {"positions", "external_assets"})
    unbounded_missing_assets = not household["complete"] or bool(unknown_asset_sections) or excluded_value_records > 0
    classification_dimensions = {"asset_class", "issuer", "sector", "country", "economic_currency"}
    for target in _targets(inputs):
        actual_value = exposure[target["dimension"]].get(target["name"], Decimal(0))
        unknown_value = Decimal(0) if target["name"] == "unknown" else exposure[target["dimension"]].get("unknown", Decimal(0))
        measured = actual_value / denominator if denominator else None
        unknown_weight = unknown_value / denominator if denominator else None
        classification_uncertain = (
            incomplete_lookthrough
            and target["dimension"] in classification_dimensions
            and not bool(unknown_weight)
        )
        if unbounded_missing_assets:
            minimum, maximum = Decimal(0), Decimal(1)
            indeterminate = True
            uncertainty = ["unbounded_missing_assets"]
        elif classification_uncertain:
            minimum = measured
            maximum = Decimal(1) if measured is not None else None
            indeterminate = True
            uncertainty = ["incomplete_fund_lookthrough"]
        else:
            minimum = measured
            maximum = measured + unknown_weight if measured is not None and unknown_weight is not None else None
            indeterminate = bool(unknown_weight)
            uncertainty = ["unclassified_known_assets"] if indeterminate else []
        if unbounded_missing_assets:
            status = "indeterminate"
        elif measured is None:
            status = "unknown"
        elif indeterminate and minimum <= target["target_weight"] <= maximum:
            status = "indeterminate"
        elif minimum > target["target_weight"]:
            status = "overweight"
        elif maximum is not None and maximum < target["target_weight"]:
            status = "underweight"
        elif measured == target["target_weight"] and not indeterminate:
            status = "on_target"
        else:
            status = "indeterminate"
        target_rows.append({
            "dimension": target["dimension"], "name": target["name"],
            "measured_weight": _out(measured) if measured is not None else None,
            "measured_weight_basis": "known_assets",
            "unknown_weight": _out(unknown_weight) if unknown_weight is not None else None,
            "possible_weight": {
                "minimum": _out(minimum), "maximum": _out(maximum),
            } if minimum is not None and maximum is not None and indeterminate else None,
            "target_weight": _out(target["target_weight"]),
            "difference": _out(measured - target["target_weight"]) if measured is not None and not indeterminate else None,
            "difference_range": {
                "minimum": _out(minimum - target["target_weight"]),
                "maximum": _out(maximum - target["target_weight"]),
            } if minimum is not None and maximum is not None and indeterminate else None,
            "status": status, "uncertainty": uncertainty,
        })

    rejected_fx = sorted(f"{a}->{b}" for a, b in fx.rejected)
    partial = bool(warnings or incomplete_lookthrough or excluded_value_records or unknown_liabilities or income_gap_count or household_stale)
    return {
        "status": "partial" if partial else "ready",
        "result": {
            "as_of": household["as_of"], "evaluation_date": as_of.isoformat(), "currency": reporting,
            "known_assets": _out(known_assets), "known_liabilities": _out(known_liabilities),
            "known_nav": _out(known_nav), "liquid_capital": _out(liquid_capital),
            "liquidity": {
                "horizon_days": horizon,
                "liquid_capital": _out(liquid_capital),
                "not_liquid_within_horizon": _out(known_assets - liquid_capital),
                "value_by_reason": {reason: _out(value) for reason, value in sorted(liquidity_by_reason.items())},
            },
            "people": people_rows,
            "fx_used": fx.used_rows(),
            "fx_convention": "rate = units of `to` per one unit of `from` (from=USD, to=MXN, rate=18 means 1 USD = 18 MXN)",
            "coverage": {
                "household_complete": household["complete"],
                "household_age_days": household_age,
                "household_stale": household_stale,
                "excluded_value_records": excluded_value_records,
                "unknown_liabilities": unknown_liabilities,
                "unknown_sections": household["unknown_sections"],
                "nonliquid_position_records": nonliquid_position_records,
                "unattributed_value_records": unattributed_value_records,
                "income_exposure_gaps": income_gap_count,
                "lookthrough_complete": not incomplete_lookthrough,
                "rejected_fx_quotes": rejected_fx,
                "weight_denominator": "known_assets",
            },
            "exposures": dimensions, "income_exposures": income_rows,
            "economic_links": economic_links, "overlap": overlaps, "targets": target_rows,
            "top_underlying": top_underlying, "overlap_matrix": overlap_matrix,
            "employer_concentration": employer_rows,
            "lookthrough_sources": [
                *({"instrument_id": iid, "status": "used", "origin": "household", "as_of": fresh_funds[iid]["as_of"],
                   "source": fresh_funds[iid]["source"]} for iid in sorted(supplied_funds)),
                *auto_rows],
        },
        "missing": [], "warnings": list(dict.fromkeys(warnings)),
        "sources": ["household", *sorted({fund["source"] for fund in fresh_funds.values()})],
        "assumptions": list(dict.fromkeys([
            *assumptions,
            f"Freshness is measured at evaluation date {as_of.isoformat()}: FX older than {max_fx_age} days, fund holdings older than {max_fund_age} days are excluded; a household older than {max_household_age} days is flagged stale.",
            f"Liquid capital is value convertible to cash within {horizon} days: restricted, locked-up, declared-illiquid, unlisted and inherently illiquid asset classes (private equity/credit, real estate, hedge funds, restricted stock, collectibles) are excluded regardless of account; redemption terms and gates are applied; otherwise the canonical account-type default is used.",
            "Person attribution uses stated ownership shares (owner_id alone means 100%); household totals count each asset once.",
            "Income amounts are not capitalized into assets or NAV; economic links are shared labels and do not estimate correlation or causation.",
        ])),
    }


def run(task: str, inputs: dict[str, Any], context: dict[str, Any] | None = None) -> dict[str, Any]:
    """Run ``import`` or ``exposure`` using the shared module envelope."""
    if not isinstance(inputs, dict) or not isinstance(context or {}, dict):
        raise ValueError("inputs and context must be objects")
    context = context or {}
    if task == "import":
        household, warnings, sources = _import_household(inputs)
        return {
            "status": "partial" if warnings else "ready",
            "result": {"household": household},
            "household": household,
            "missing": [], "warnings": warnings, "sources": sources,
            "assumptions": [],
        }
    if task != "exposure":
        raise ValueError("task must be import or exposure")
    raw = inputs.get("household", context.get("household"))
    if raw is None:
        return {
            "status": "needs_input", "result": {},
            "missing": [{"key": "household", "reason": "missing", "detail": "Supply inputs.household or current context.household."}],
            "warnings": [], "sources": [], "assumptions": [],
        }
    checked = validate_household(raw)
    result = _exposure(checked["household"], inputs, checked["warnings"], context)
    if "household" in inputs:
        result["assumptions"].append("Used inputs.household in preference to context.household.")
    return result
