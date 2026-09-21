"""Ledger input contract: validation, normalization, and deterministic identity.

A *posting batch* is the one input shape the ledger accepts, whether it comes
from a confirmed statement ingest proposal, a chat message, or a test fixture::

    {
      "batch_id": "ingest:<file sha256>",          # idempotency key (required)
      "source": {"kind": "document", "ref": "BBVA estado de cuenta 2026-08",
                 "observed_on": "2026-09-01", "file_hash": "<sha256>"},
      "confidence": "reported",                    # default for every line
      "accounts": [...], "instruments": [...],     # upserted before lines
      "transactions": [...],                       # see normalize_transaction
      "balance_assertions": [...],                 # "statement says X at D"
      "fx": [...],                                 # dated rates with a source
    }

Money and quantities are Decimals internally and decimal strings once
normalized, so a normalized batch is JSON-safe and hashes reproducibly.  No
module here touches a database; :mod:`wealth.store` persists normalized rows.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal, InvalidOperation
import hashlib
import json
import math
import re
import unicodedata
from typing import Any, Mapping


SOURCE_KINDS = frozenset({"user", "document", "web", "tool", "inference"})
CONFIDENCES = frozenset({"confirmed", "reported", "inferred"})

KINDS = frozenset({
    "deposit", "withdrawal", "transfer", "buy", "sell", "dividend", "interest", "fee",
    "tax_withheld", "split", "merger", "spin_off", "fx_conversion", "income", "expense",
    "loan_payment", "opening_balance", "reversal",
})
INCOME_SUBTYPES = frozenset({"salary", "aguinaldo", "ptu", "bonus", "freelance", "rental", "pension", "other"})
LISTINGS = frozenset({"BMV", "BIVA", "SIC", "NYSE", "NASDAQ", "ARCA", "OTC", "LSE", "OTHER"})
# Where a holding trades.  The same underlying (e.g. VOO) bought through the SIC
# in MXN and at a US broker in USD is one economic exposure but two venues for
# tax and estate purposes, so both ``underlying_symbol`` and ``venue`` are kept.
VENUES = frozenset({"bmv", "biva", "sic", "us", "other"})
ISSUER_DOMICILES = frozenset({"US", "IE", "MX", "other"})
_LISTING_VENUE = {"BMV": "bmv", "BIVA": "biva", "SIC": "sic", "NYSE": "us", "NASDAQ": "us",
                  "ARCA": "us", "OTC": "us", "LSE": "other", "OTHER": "other"}
ACCOUNT_TYPES_HINT = (
    "checking", "savings", "bank", "cash", "brokerage", "taxable", "retirement", "afore",
    "ppr", "ira", "401k", "credit_card", "loan", "mortgage", "other",
)
LIABILITY_TYPES = frozenset({"credit_card", "loan", "mortgage", "line_of_credit"})

# Kinds whose cash effect has a fixed sign (the amount is the signed cash effect
# on the account: positive in, negative out).
_NEGATIVE = frozenset({"withdrawal", "buy", "fee", "tax_withheld", "loan_payment", "fx_conversion"})
_POSITIVE = frozenset({"deposit", "sell", "dividend", "interest", "income"})
_NONNEGATIVE = frozenset({"split", "merger", "spin_off"})  # optional cash in lieu

_TX_FIELDS = frozenset({
    "id", "external_id", "account_id", "kind", "date", "settle_date", "amount", "currency",
    "description", "category", "subtype", "instrument_id", "quantity", "price", "fee",
    "lot_selection", "transfer_group", "counterparty_account_id", "ratio", "new_instrument_id",
    "basis_allocation", "to_amount", "to_currency", "principal", "interest", "cost_basis",
    "acquired_on", "reverses_id", "confirm_not_duplicate", "page", "confidence", "source",
    "liability_id", "memo", "executed_at",
})
_SENSITIVE_KEYS = frozenset({
    "password", "passcode", "pin", "ssn", "curp", "rfc", "account_number", "routing_number",
    "clabe", "card_number", "cvv", "api_key", "secret", "credential", "credentials", "token",
    "address", "street_address",
})
_CURRENCY = re.compile(r"[A-Z]{3}")
_ISIN = re.compile(r"[A-Z]{2}[A-Z0-9]{9}\d")
_CUSIP = re.compile(r"[0-9A-Z]{8}\d")
_LONG_DIGITS = re.compile(r"\d(?:[ -]?\d){9,}")


class LedgerInputError(ValueError):
    """A posting batch does not satisfy the ledger contract."""


# -- scalar helpers ---------------------------------------------------------

def dec(value: Any, field: str, *, positive: bool = False, nonnegative: bool = False) -> Decimal:
    if isinstance(value, bool) or value is None or value == "":
        raise LedgerInputError(f"{field} must be a finite number")
    if isinstance(value, float) and not math.isfinite(value):
        raise LedgerInputError(f"{field} must be a finite number")
    try:
        result = Decimal(str(value).strip().replace(",", "")) if isinstance(value, str) else Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise LedgerInputError(f"{field} must be a finite number") from exc
    if not result.is_finite():
        raise LedgerInputError(f"{field} must be a finite number")
    if positive and result <= 0:
        raise LedgerInputError(f"{field} must be greater than zero")
    if nonnegative and result < 0:
        raise LedgerInputError(f"{field} must not be negative")
    return result


def opt_dec(value: Any, field: str, **kwargs: bool) -> Decimal | None:
    return None if value is None or value == "" else dec(value, field, **kwargs)


def text(value: Any, field: str, *, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str) or not value.strip():
        raise LedgerInputError(f"{field} must be a nonempty string")
    return value.strip()


def currency(value: Any, field: str) -> str:
    value = text(value, field)
    if _CURRENCY.fullmatch(value) is None:
        raise LedgerInputError(f"{field} must be a three-letter uppercase currency code")
    return value


def iso(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise LedgerInputError(f"{field} must be an ISO date (YYYY-MM-DD)")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise LedgerInputError(f"{field} must be an ISO date (YYYY-MM-DD)") from exc
    if parsed.isoformat() != value:
        raise LedgerInputError(f"{field} must be an ISO date (YYYY-MM-DD)")
    return value


def out(value: Decimal | None) -> str | None:
    """Canonical decimal string (no exponent, no trailing zeros)."""

    if value is None:
        return None
    if value == 0:
        return "0"
    rendered = format(value.normalize(), "f")
    return rendered.rstrip("0").rstrip(".") if "." in rendered else rendered


def money(value: Decimal | None) -> str | None:
    return None if value is None else format(value.quantize(Decimal("0.01")), "f")


def fold(value: str | None) -> str:
    """Lowercase, accent-free, single-spaced text for matching (es-MX safe)."""

    if not value:
        return ""
    decomposed = unicodedata.normalize("NFKD", value)
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return " ".join(re.sub(r"[^a-z0-9&+ ]+", " ", stripped.lower()).split())


def redact(description: str | None) -> tuple[str | None, bool]:
    """Mask account/card/CLABE-like digit runs, keeping the last four digits."""

    if description is None:
        return None, False
    changed = False

    def mask(match: re.Match[str]) -> str:
        nonlocal changed
        changed = True
        digits = re.sub(r"\D", "", match.group(0))
        return "****" + digits[-4:]

    return _LONG_DIGITS.sub(mask, description), changed


def _sensitive_keys(value: Any, field: str) -> None:
    if isinstance(value, Mapping):
        for name, item in value.items():
            if isinstance(name, str) and name.lower() in _SENSITIVE_KEYS and item not in (None, ""):
                raise LedgerInputError(
                    f"{field}.{name} is an identifier or credential field; do not store account "
                    "numbers, CLABEs, government IDs, addresses, or credentials"
                )
            _sensitive_keys(item, f"{field}.{name}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _sensitive_keys(item, f"{field}[{index}]")


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


# -- provenance -------------------------------------------------------------

def normalize_source(raw: Any, field: str, *, default: Mapping[str, Any] | None = None) -> dict[str, Any]:
    if raw is None and default is not None:
        return dict(default)
    if not isinstance(raw, Mapping):
        raise LedgerInputError(f"{field} must be an object with kind and ref")
    merged = {**(default or {}), **raw}
    allowed = {"kind", "ref", "observed_on", "file_hash", "page"}
    unknown = set(merged) - allowed
    if unknown:
        raise LedgerInputError(f"{field} has unknown fields {sorted(unknown)!r}; allowed {sorted(allowed)!r}")
    kind = merged.get("kind")
    if kind not in SOURCE_KINDS:
        raise LedgerInputError(f"{field}.kind must be one of {sorted(SOURCE_KINDS)!r}")
    ref = text(merged.get("ref"), f"{field}.ref")
    if kind == "web" and not ref.startswith(("https://", "http://")):
        raise LedgerInputError(f"{field}.ref must be the page URL for a web source")
    result = {"kind": kind, "ref": ref}
    if merged.get("observed_on") is not None:
        result["observed_on"] = iso(merged["observed_on"], f"{field}.observed_on")
    if merged.get("file_hash") is not None:
        file_hash = text(merged["file_hash"], f"{field}.file_hash").lower()
        if re.fullmatch(r"[0-9a-f]{16,128}", file_hash) is None:
            raise LedgerInputError(f"{field}.file_hash must be a hex digest")
        result["file_hash"] = file_hash
    if merged.get("page") is not None:
        page = merged["page"]
        if isinstance(page, bool) or not isinstance(page, (int, str)) or (isinstance(page, int) and page < 1):
            raise LedgerInputError(f"{field}.page must be a positive integer or page label")
        result["page"] = page
    return result


def normalize_confidence(value: Any, source: Mapping[str, Any], field: str) -> str:
    confidence = value or ("inferred" if source["kind"] == "inference" else "reported")
    if confidence not in CONFIDENCES:
        raise LedgerInputError(f"{field} must be one of {sorted(CONFIDENCES)!r}")
    if confidence == "confirmed" and source["kind"] != "user":
        raise LedgerInputError(f"{field}: only a user source can carry confirmed confidence")
    if source["kind"] == "inference" and confidence != "inferred":
        raise LedgerInputError(f"{field}: an inference source must carry inferred confidence")
    return confidence


def source_identity(source: Mapping[str, Any]) -> str:
    """What makes two lines come from the *same* document (for dedupe)."""

    return source.get("file_hash") or f"{source['kind']}:{source['ref']}"


# -- reference data ---------------------------------------------------------

def normalize_account(raw: Any, index: int) -> dict[str, Any]:
    field = f"accounts[{index}]"
    if not isinstance(raw, Mapping):
        raise LedgerInputError(f"{field} must be an object")
    _sensitive_keys(raw, field)
    allowed = {"id", "institution", "type", "currency", "name", "owners", "tax_unit", "liquid",
               "restricted", "opened_on", "closed_on", "country"}
    unknown = set(raw) - allowed
    if unknown:
        raise LedgerInputError(f"{field} has unknown fields {sorted(unknown)!r}; allowed {sorted(allowed)!r}")
    account = {
        "id": text(raw.get("id"), f"{field}.id"),
        "institution": text(raw.get("institution"), f"{field}.institution"),
        "type": text(raw.get("type"), f"{field}.type").lower().replace(" ", "_").replace("-", "_"),
        "currency": currency(raw.get("currency"), f"{field}.currency"),
    }
    for name in ("name", "tax_unit", "country"):
        if raw.get(name) is not None:
            account[name] = text(raw[name], f"{field}.{name}")
    for name in ("liquid", "restricted"):
        if name in raw:
            if not isinstance(raw[name], bool):
                raise LedgerInputError(f"{field}.{name} must be a boolean")
            account[name] = raw[name]
    for name in ("opened_on", "closed_on"):
        if raw.get(name) is not None:
            account[name] = iso(raw[name], f"{field}.{name}")
    owners = raw.get("owners")
    if owners is not None:
        if not isinstance(owners, list) or not owners:
            raise LedgerInputError(f"{field}.owners must be a nonempty list of {{person_id, share}}")
        seen, total, normalized = set(), Decimal(0), []
        for position, owner in enumerate(owners):
            if not isinstance(owner, Mapping):
                raise LedgerInputError(f"{field}.owners[{position}] must be an object")
            person = text(owner.get("person_id"), f"{field}.owners[{position}].person_id")
            if person in seen:
                raise LedgerInputError(f"{field}.owners has duplicate person {person}")
            seen.add(person)
            share = dec(owner.get("share"), f"{field}.owners[{position}].share", positive=True)
            total += share
            normalized.append({"person_id": person, "share": out(share)})
        if total != 1:
            raise LedgerInputError(f"{field}.owners shares must sum to 1 (got {out(total)})")
        account["owners"] = normalized
    return account


def normalize_instrument(raw: Any, index: int) -> dict[str, Any]:
    field = f"instruments[{index}]"
    if not isinstance(raw, Mapping):
        raise LedgerInputError(f"{field} must be an object")
    _sensitive_keys(raw, field)
    allowed = {"id", "symbol", "name", "isin", "cusip", "listing", "asset_class", "currency",
               "domicile", "issuer", "sector", "country", "economic_currency", "venue",
               "listing_currency", "underlying_symbol", "issuer_domicile"}
    unknown = set(raw) - allowed
    if unknown:
        raise LedgerInputError(f"{field} has unknown fields {sorted(unknown)!r}; allowed {sorted(allowed)!r}")
    listed = raw.get("listing_currency", raw.get("currency"))
    instrument = {
        "id": text(raw.get("id"), f"{field}.id"),
        "symbol": text(raw.get("symbol"), f"{field}.symbol").upper(),
        "currency": currency(raw.get("currency", listed), f"{field}.currency"),
        "listing_currency": currency(listed, f"{field}.listing_currency"),
        "asset_class": (text(raw.get("asset_class"), f"{field}.asset_class", optional=True) or "unknown").lower(),
    }
    if instrument["currency"] != instrument["listing_currency"]:
        raise LedgerInputError(f"{field}.currency is the listing currency; it must equal listing_currency")
    instrument["underlying_symbol"] = (
        text(raw.get("underlying_symbol"), f"{field}.underlying_symbol", optional=True) or instrument["symbol"]
    ).upper()
    for name in ("name", "issuer", "sector", "country"):
        if raw.get(name) is not None:
            instrument[name] = text(raw[name], f"{field}.{name}")
    if raw.get("isin") is not None:
        isin = text(raw["isin"], f"{field}.isin").upper()
        if _ISIN.fullmatch(isin) is None:
            raise LedgerInputError(f"{field}.isin must be a 12-character ISIN")
        instrument["isin"] = isin
    if raw.get("cusip") is not None:
        cusip = text(raw["cusip"], f"{field}.cusip").upper()
        if _CUSIP.fullmatch(cusip) is None:
            raise LedgerInputError(f"{field}.cusip must be a 9-character CUSIP")
        instrument["cusip"] = cusip
    if raw.get("listing") is not None:
        listing = text(raw["listing"], f"{field}.listing").upper()
        if listing not in LISTINGS:
            raise LedgerInputError(f"{field}.listing must be one of {sorted(LISTINGS)!r}")
        instrument["listing"] = listing
    venue = raw.get("venue")
    if venue is None and instrument.get("listing"):
        venue = _LISTING_VENUE.get(instrument["listing"])
    if venue is not None:
        venue = text(venue, f"{field}.venue").lower()
        if venue not in VENUES:
            raise LedgerInputError(f"{field}.venue must be one of {sorted(VENUES)!r}")
        instrument["venue"] = venue
    if venue == "sic" and instrument["listing_currency"] != "MXN":
        raise LedgerInputError(f"{field}: SIC listings trade in MXN; listing_currency must be MXN")
    if raw.get("domicile") is not None:
        domicile = text(raw["domicile"], f"{field}.domicile").upper()
        if re.fullmatch(r"[A-Z]{2}", domicile) is None:
            raise LedgerInputError(f"{field}.domicile must be a two-letter country code")
        instrument["domicile"] = domicile
    issuer_domicile = raw.get("issuer_domicile", instrument.get("domicile") if instrument.get("domicile") in {"US", "IE", "MX"} else None)
    if issuer_domicile is not None:
        issuer_domicile = text(issuer_domicile, f"{field}.issuer_domicile").upper()
        issuer_domicile = "other" if issuer_domicile == "OTHER" else issuer_domicile
        if issuer_domicile not in ISSUER_DOMICILES:
            raise LedgerInputError(f"{field}.issuer_domicile must be one of {sorted(ISSUER_DOMICILES)!r}")
        instrument["issuer_domicile"] = issuer_domicile
    if raw.get("economic_currency") is not None:
        instrument["economic_currency"] = currency(raw["economic_currency"], f"{field}.economic_currency")
    return instrument


def normalize_fx(raw: Any, index: int) -> dict[str, Any]:
    field = f"fx[{index}]"
    if not isinstance(raw, Mapping):
        raise LedgerInputError(f"{field} must be an object")
    unknown = set(raw) - {"date", "base", "quote", "rate", "source"}
    if unknown:
        raise LedgerInputError(f"{field} has unknown fields {sorted(unknown)!r}")
    base, quote = currency(raw.get("base"), f"{field}.base"), currency(raw.get("quote"), f"{field}.quote")
    if base == quote:
        raise LedgerInputError(f"{field} is an identity pair")
    return {
        "date": iso(raw.get("date"), f"{field}.date"), "base": base, "quote": quote,
        "rate": out(dec(raw.get("rate"), f"{field}.rate", positive=True)),
        "source": text(raw.get("source"), f"{field}.source"),
    }


def normalize_assertion(raw: Any, index: int, source: Mapping[str, Any]) -> dict[str, Any]:
    field = f"balance_assertions[{index}]"
    if not isinstance(raw, Mapping):
        raise LedgerInputError(f"{field} must be an object")
    unknown = set(raw) - {"account_id", "date", "currency", "balance", "instrument_id", "quantity", "page"}
    if unknown:
        raise LedgerInputError(f"{field} has unknown fields {sorted(unknown)!r}")
    assertion: dict[str, Any] = {
        "account_id": text(raw.get("account_id"), f"{field}.account_id"),
        "date": iso(raw.get("date"), f"{field}.date"),
    }
    if raw.get("instrument_id") is not None:
        assertion["instrument_id"] = text(raw["instrument_id"], f"{field}.instrument_id")
        assertion["quantity"] = out(dec(raw.get("quantity"), f"{field}.quantity", nonnegative=True))
        if raw.get("balance") is not None:
            raise LedgerInputError(f"{field} asserts either a cash balance or an instrument quantity, not both")
    else:
        assertion["currency"] = currency(raw.get("currency"), f"{field}.currency")
        assertion["balance"] = out(dec(raw.get("balance"), f"{field}.balance"))
    line_source = dict(source)
    if raw.get("page") is not None:
        line_source["page"] = raw["page"]
    assertion["source"] = normalize_source(line_source, f"{field}.source")
    identity = {k: v for k, v in assertion.items() if k != "source"}
    assertion["id"] = "ba_" + digest([identity, source_identity(assertion["source"])])[:24]
    return assertion


# -- transactions -----------------------------------------------------------

def _lot_selection(value: Any, field: str) -> list[dict[str, str]]:
    if not isinstance(value, list) or not value:
        raise LedgerInputError(f"{field} must be a nonempty list of {{lot_id, quantity}}")
    selection, seen = [], set()
    for index, item in enumerate(value):
        if not isinstance(item, Mapping):
            raise LedgerInputError(f"{field}[{index}] must be an object")
        lot_id = text(item.get("lot_id"), f"{field}[{index}].lot_id")
        if lot_id in seen:
            raise LedgerInputError(f"{field} selects lot {lot_id} twice")
        seen.add(lot_id)
        selection.append({"lot_id": lot_id, "quantity": out(dec(item.get("quantity"), f"{field}[{index}].quantity", positive=True))})
    return selection


def _execution_time(value: Any, field: str, day: str) -> str:
    """ISO execution timestamp on the entry's date; orders same-day trades."""

    if not isinstance(value, str):
        raise LedgerInputError(f"{field} must be an ISO date-time")
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise LedgerInputError(f"{field} must be an ISO date-time") from exc
    if parsed.date().isoformat() != day:
        raise LedgerInputError(f"{field} must fall on the entry date {day}")
    return parsed.replace(tzinfo=None).isoformat(timespec="seconds") if parsed.tzinfo is None else parsed.isoformat(timespec="seconds")


def normalize_transaction(raw: Any, index: int, *, source: Mapping[str, Any], confidence: str | None) -> tuple[dict[str, Any], list[str]]:
    """Validate one line and return the canonical entry (without its id) plus warnings.

    ``amount`` is the signed cash effect on ``account_id`` in ``currency``:
    positive money in, negative money out.  Buys and sells may give ``price``
    and ``fee`` instead of ``amount``; the cash effect is then derived.
    """

    field = f"transactions[{index}]"
    if not isinstance(raw, Mapping):
        raise LedgerInputError(f"{field} must be an object")
    unknown = set(raw) - _TX_FIELDS
    if unknown:
        raise LedgerInputError(f"{field} has unknown fields {sorted(unknown)!r}; allowed {sorted(_TX_FIELDS)!r}")
    _sensitive_keys(raw, field)
    warnings: list[str] = []
    kind = raw.get("kind")
    if kind not in KINDS:
        raise LedgerInputError(f"{field}.kind must be one of {sorted(KINDS)!r}")
    line_source = normalize_source(raw.get("source"), f"{field}.source", default=source)
    if raw.get("page") is not None:
        line_source = normalize_source({**line_source, "page": raw["page"]}, f"{field}.source")
    entry: dict[str, Any] = {
        "kind": kind,
        "account_id": text(raw.get("account_id"), f"{field}.account_id"),
        "date": iso(raw.get("date"), f"{field}.date"),
        "source": line_source,
        "confidence": normalize_confidence(raw.get("confidence", confidence), line_source, f"{field}.confidence"),
    }
    if raw.get("settle_date") is not None:
        entry["settle_date"] = iso(raw["settle_date"], f"{field}.settle_date")
    if raw.get("executed_at") is not None:
        entry["executed_at"] = _execution_time(raw["executed_at"], f"{field}.executed_at", entry["date"])
    if raw.get("external_id") is not None:
        entry["external_id"] = text(str(raw["external_id"]) if isinstance(raw["external_id"], int) else raw["external_id"], f"{field}.external_id")
    for name in ("description", "memo"):
        if raw.get(name) is not None:
            cleaned, masked = redact(text(raw[name], f"{field}.{name}"))
            entry[name] = cleaned
            if masked:
                warnings.append(f"{field}.{name}: masked an account/card-like number (kept the last four digits).")
    if raw.get("category") is not None:
        entry["category"] = text(raw["category"], f"{field}.category").lower()
    for name in ("transfer_group", "counterparty_account_id", "liability_id"):
        if raw.get(name) is not None:
            entry[name] = text(raw[name], f"{field}.{name}")
    if raw.get("confirm_not_duplicate") is not None:
        if not isinstance(raw["confirm_not_duplicate"], bool):
            raise LedgerInputError(f"{field}.confirm_not_duplicate must be a boolean")
        if raw["confirm_not_duplicate"]:
            entry["confirm_not_duplicate"] = True

    amount = opt_dec(raw.get("amount"), f"{field}.amount")
    ccy = currency(raw["currency"], f"{field}.currency") if raw.get("currency") is not None else None
    instrument = text(raw.get("instrument_id"), f"{field}.instrument_id", optional=True)
    quantity = opt_dec(raw.get("quantity"), f"{field}.quantity")
    price = opt_dec(raw.get("price"), f"{field}.price", nonnegative=True)
    fee = opt_dec(raw.get("fee"), f"{field}.fee", nonnegative=True)

    def need(condition: bool, message: str) -> None:
        if not condition:
            raise LedgerInputError(f"{field}: {message}")

    if kind == "reversal":
        entry["reverses_id"] = text(raw.get("reverses_id"), f"{field}.reverses_id")
        need(amount is None and instrument is None, "a reversal names reverses_id only; post the corrected entry separately")
        return entry, warnings
    need(raw.get("reverses_id") is None, "reverses_id is only valid on a reversal")

    if kind in {"buy", "sell"}:
        need(instrument is not None, f"{kind} requires instrument_id")
        need(quantity is not None and quantity > 0, f"{kind} requires a positive quantity")
        need(ccy is not None, f"{kind} requires currency (of the cash leg)")
        if amount is None:
            need(price is not None, f"{kind} requires amount or price")
            gross = quantity * price
            amount = -(gross + (fee or 0)) if kind == "buy" else gross - (fee or 0)
        elif price is not None:
            expected = quantity * price + (fee or 0) * (1 if kind == "buy" else -1)
            if abs(abs(amount) - expected) > Decimal("0.01") * max(Decimal(1), quantity):
                warnings.append(f"{field}: amount {out(amount)} differs from quantity x price +/- fee ({out(expected)}); the cash amount is used as basis/proceeds.")
        need(amount <= 0 if kind == "buy" else amount >= 0,
             "amount is the signed cash effect: negative for a buy, positive for a sell")
        if kind == "sell" and raw.get("lot_selection") is not None:
            entry["lot_selection"] = _lot_selection(raw["lot_selection"], f"{field}.lot_selection")
    elif kind in {"split", "merger", "spin_off"}:
        need(instrument is not None, f"{kind} requires instrument_id")
        entry["ratio"] = out(dec(raw.get("ratio"), f"{field}.ratio", positive=True))
        if kind != "split":
            entry["new_instrument_id"] = text(raw.get("new_instrument_id"), f"{field}.new_instrument_id")
        if kind == "spin_off" and raw.get("basis_allocation") is not None:
            allocation = dec(raw["basis_allocation"], f"{field}.basis_allocation", nonnegative=True)
            need(allocation <= 1, "basis_allocation is the fraction (0-1) of parent basis moved to the new instrument")
            entry["basis_allocation"] = out(allocation)
        if amount is not None:
            need(ccy is not None, "cash in lieu requires currency")
    elif kind == "fx_conversion":
        need(amount is not None and ccy is not None, "fx_conversion requires amount and currency (the sold leg)")
        to_amount = dec(raw.get("to_amount"), f"{field}.to_amount", positive=True)
        to_currency = currency(raw.get("to_currency"), f"{field}.to_currency")
        need(to_currency != ccy, "fx_conversion currencies must differ")
        entry["to_amount"], entry["to_currency"] = out(to_amount), to_currency
    elif kind == "transfer":
        if instrument is not None:
            need(quantity is not None and quantity != 0, "an in-kind transfer requires a signed quantity (negative out, positive in)")
            if raw.get("cost_basis") is not None:
                entry["cost_basis"] = out(dec(raw["cost_basis"], f"{field}.cost_basis", nonnegative=True))
                need(ccy is not None, "cost_basis requires currency")
            if raw.get("acquired_on") is not None:
                entry["acquired_on"] = iso(raw["acquired_on"], f"{field}.acquired_on")
        else:
            need(amount is not None and amount != 0 and ccy is not None, "a cash transfer requires a nonzero signed amount and currency")
    elif kind == "opening_balance":
        if instrument is not None:
            need(quantity is not None and quantity >= 0, "a position snapshot requires a nonnegative quantity")
            if raw.get("cost_basis") is not None:
                entry["cost_basis"] = out(dec(raw["cost_basis"], f"{field}.cost_basis", nonnegative=True))
                need(ccy is not None, "cost_basis requires currency")
            if raw.get("acquired_on") is not None:
                entry["acquired_on"] = iso(raw["acquired_on"], f"{field}.acquired_on")
            need(amount is None, "a position snapshot has no cash amount; post cash as its own opening_balance")
        else:
            need(amount is not None and ccy is not None, "a cash opening balance requires amount and currency")
    else:
        need(amount is not None and ccy is not None, f"{kind} requires amount and currency")
        if kind in {"dividend"}:
            need(instrument is not None, "dividend requires instrument_id")

    if amount is not None:
        need(ccy is not None, "amount requires currency")
        if kind in _NEGATIVE:
            need(amount < 0 or (kind == "buy" and amount == 0), f"{kind} amount is the signed cash effect and must be negative")
        if kind in _POSITIVE:
            need(amount > 0 or (kind == "sell" and amount == 0), f"{kind} amount is the signed cash effect and must be positive")
        if kind in _NONNEGATIVE:
            need(amount >= 0, f"{kind} cash in lieu must not be negative")
        if kind == "expense" and amount > 0:
            warnings.append(f"{field}: positive expense recorded as a refund that offsets spending.")
        if kind == "expense" and amount == 0:
            raise LedgerInputError(f"{field}: expense amount must not be zero")
        entry["amount"], entry["currency"] = out(amount), ccy
    elif ccy is not None:
        entry["currency"] = ccy
    if instrument is not None:
        entry["instrument_id"] = instrument
    if quantity is not None:
        entry["quantity"] = out(quantity)
    if price is not None:
        entry["price"] = out(price)
    if fee is not None:
        entry["fee"] = out(fee)
    if kind == "income":
        subtype = (raw.get("subtype") or "other")
        if subtype not in INCOME_SUBTYPES:
            raise LedgerInputError(f"{field}.subtype must be one of {sorted(INCOME_SUBTYPES)!r}")
        entry["subtype"] = subtype
    elif raw.get("subtype") is not None:
        entry["subtype"] = text(raw["subtype"], f"{field}.subtype").lower()
    if kind == "loan_payment":
        principal = opt_dec(raw.get("principal"), f"{field}.principal", nonnegative=True)
        interest = opt_dec(raw.get("interest"), f"{field}.interest", nonnegative=True)
        if principal is not None and interest is not None and abs(principal + interest + amount) > Decimal("0.01"):
            raise LedgerInputError(f"{field}: principal + interest must equal the payment amount")
        if principal is not None and interest is None:
            interest = -amount - principal
        elif interest is not None and principal is None:
            principal = -amount - interest
        if principal is not None:
            need(principal >= 0 and interest >= 0, "principal and interest cannot exceed the payment")
            entry["principal"], entry["interest"] = out(principal), out(interest)
    return entry, warnings


def dedupe_key(entry: Mapping[str, Any], occurrence: int) -> str:
    """Content identity: the same line from any statement hashes the same.

    ``occurrence`` counts identical lines inside one source so that two genuine
    identical purchases on one statement stay two entries, while re-posting the
    statement (or an overlapping one) finds both again.
    """

    if entry.get("external_id"):
        return digest(["ext", entry["account_id"], entry["external_id"]])
    return digest([
        entry["account_id"], entry["kind"], entry["date"], entry.get("amount"), entry.get("currency"),
        entry.get("instrument_id"), entry.get("quantity"), fold(entry.get("description")),
        entry.get("reverses_id"), occurrence,
    ])


def normalize_batch(batch: Any) -> tuple[dict[str, Any], list[str]]:
    """Validate a whole posting batch before anything is written.

    Returns the normalized batch (entries carry ``id`` and ``dedupe_hash``) and
    warnings.  Cross-row checks that need stored state (unknown accounts,
    duplicates against prior statements, reversal targets) live in the store.
    """

    if not isinstance(batch, Mapping):
        raise LedgerInputError("batch must be an object")
    allowed = {"batch_id", "source", "confidence", "accounts", "instruments", "transactions",
               "balance_assertions", "fx", "statement"}
    unknown = set(batch) - allowed
    if unknown:
        raise LedgerInputError(f"batch has unknown fields {sorted(unknown)!r}; allowed {sorted(allowed)!r}")
    batch_id = text(batch.get("batch_id"), "batch_id")
    source = normalize_source(batch.get("source"), "source")
    confidence = batch.get("confidence")
    normalize_confidence(confidence, source, "confidence")
    warnings: list[str] = []
    lists = {}
    for name in ("accounts", "instruments", "transactions", "balance_assertions", "fx"):
        value = batch.get(name, [])
        if not isinstance(value, list):
            raise LedgerInputError(f"{name} must be a list")
        lists[name] = value
    if not any(lists.values()):
        raise LedgerInputError("batch contains nothing to post")
    accounts = [normalize_account(item, i) for i, item in enumerate(lists["accounts"])]
    instruments = [normalize_instrument(item, i) for i, item in enumerate(lists["instruments"])]
    for name, rows in (("accounts", accounts), ("instruments", instruments)):
        ids = [row["id"] for row in rows]
        if len(ids) != len(set(ids)):
            raise LedgerInputError(f"batch {name} contain duplicate ids")
    fx = [normalize_fx(item, i) for i, item in enumerate(lists["fx"])]
    entries, occurrences = [], {}
    for index, raw in enumerate(lists["transactions"]):
        entry, notes = normalize_transaction(raw, index, source=source, confidence=confidence)
        warnings.extend(notes)
        base = dedupe_key(entry, 0)
        occurrence = occurrences.get((source_identity(entry["source"]), base), 0)
        occurrences[(source_identity(entry["source"]), base)] = occurrence + 1
        entry["dedupe_hash"] = dedupe_key(entry, occurrence)
        entry["id"] = "tx_" + entry["dedupe_hash"][:24]
        entry["line"] = index
        entries.append(entry)
    ids = [entry["id"] for entry in entries]
    if len(ids) != len(set(ids)):
        raise LedgerInputError("batch repeats an external_id for the same account")
    assertions = [normalize_assertion(item, i, source) for i, item in enumerate(lists["balance_assertions"])]
    statement = batch.get("statement")
    if statement is not None:
        if not isinstance(statement, Mapping):
            raise LedgerInputError("statement must be an object")
        statement = {
            "account_id": text(statement.get("account_id"), "statement.account_id"),
            "period_start": iso(statement.get("period_start"), "statement.period_start"),
            "period_end": iso(statement.get("period_end"), "statement.period_end"),
        }
        if statement["period_end"] < statement["period_start"]:
            raise LedgerInputError("statement.period_end precedes period_start")
    return {
        "batch_id": batch_id, "source": source, "confidence": confidence,
        "accounts": accounts, "instruments": instruments, "transactions": entries,
        "balance_assertions": assertions, "fx": fx, "statement": statement,
    }, warnings
