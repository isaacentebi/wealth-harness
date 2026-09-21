"""Host-LLM structured extraction with anti-hallucination validation.

When deterministic parsing cannot read a layout (or the upload is an image),
ingestion returns an ``extraction_request``: redacted page texts, a strict JSON
schema and instructions.  The host model fills the schema; the result comes
back through :func:`validate_llm_extraction`, which checks every number, date,
symbol and account suffix against the source text before reconciling it like
any other statement.  Nothing unverified can be confirmed silently.
"""

from __future__ import annotations

from datetime import date
import re
from typing import Any

from .common import envelope, find_dates, fold, number_tokens, parse_amount
from .redact import redact_text
from .transactions import TYPES


MAX_REQUEST_CHARS = 120_000
_NUM = {"type": ["string", "null"], "maxLength": 40,
        "description": "Copy the number exactly as printed (e.g. \"1,234.56\", \"(12.50)\"); null if not printed."}
_TEXT = {"type": ["string", "null"], "maxLength": 200}
_DATE = {"type": ["string", "null"], "pattern": r"^\d{4}-\d{2}-\d{2}$"}
_CCY = {"type": ["string", "null"], "pattern": r"^[A-Z]{3}$"}
_PAGE = {"type": ["integer", "null"], "minimum": 1}


def _obj(properties: dict[str, Any], max_items: int | None = None) -> dict[str, Any]:
    schema = {"type": "object", "additionalProperties": False, "required": sorted(properties), "properties": properties}
    return schema if max_items is None else {"type": "array", "maxItems": max_items, "items": schema}


EXTRACTION_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "Wealth statement extraction",
    **_obj({
        "institution": _TEXT, "as_of": _DATE, "period_start": _DATE, "currency": _CCY,
        "accounts": _obj({
            "label": _TEXT,
            "number_last4": {"type": ["string", "null"], "pattern": r"^\d{4}$"},
            "type": {"type": ["string", "null"], "enum": [None, "brokerage", "checking", "savings", "credit_card",
                                                          "mortgage", "ira", "roth_ira", "401k", "ppr", "afore", "other"]},
            "currency": _CCY, "page": _PAGE,
            "positions": _obj({"symbol": _TEXT, "description": _TEXT, "quantity": _NUM, "price": _NUM,
                               "market_value": _NUM, "cost_basis": _NUM, "currency": _CCY, "page": _PAGE}, 2000),
            "cash": _obj({"label": _TEXT, "amount": _NUM, "currency": _CCY, "page": _PAGE}, 50),
            "reported_total": {"anyOf": [{"type": "null"}, _obj({"label": _TEXT, "amount": _NUM, "currency": _CCY, "page": _PAGE})]},
            "flows": {"anyOf": [{"type": "null"}, _obj({"opening": _NUM, "deposits": _NUM, "withdrawals": _NUM,
                                                         "closing": _NUM, "page": _PAGE})]},
            "liabilities": _obj({"label": _TEXT, "balance": _NUM, "currency": _CCY, "minimum_payment": _NUM,
                                 "interest_rate": _NUM, "page": _PAGE}, 50),
            "transactions": _obj({"date": _DATE, "settlement_date": _DATE, "description": _TEXT, "amount": _NUM,
                                  "direction": {"type": ["string", "null"], "enum": [None, "in", "out"]},
                                  "type": {"type": ["string", "null"], "enum": [None, *TYPES]},
                                  "symbol": _TEXT, "quantity": _NUM, "price": _NUM, "fees": _NUM,
                                  "balance": _NUM, "currency": _CCY, "page": _PAGE}, 5000),
        }, 50),
        "fx": _obj({"from": _CCY, "to": _CCY, "rate": _NUM, "page": _PAGE}, 20),
    }),
}

INSTRUCTIONS = (
    "Extract only what is printed. Copy every number exactly as it appears (keep separators, signs and parentheses). "
    "Use null for anything not printed; never compute, estimate or infer a value. Give only the last four digits of "
    "account numbers. Never output RFC, CURP, SSN, addresses or names of people. Dates are YYYY-MM-DD; when a row "
    "date omits the year, use the statement period's year. Transaction direction is 'in' for money received by the "
    "account and 'out' for money leaving it. Include every holding, cash balance, statement total, cash-flow summary "
    "and transaction with its page number. Return one JSON object matching the schema."
)
_NUMERIC_FIELDS = frozenset({"quantity", "price", "market_value", "cost_basis", "amount", "opening", "deposits",
                             "withdrawals", "closing", "balance", "minimum_payment", "interest_rate", "rate", "fees"})


def extraction_request(pages: list[tuple[int, str]], *, provenance: dict[str, Any], reason: str) -> dict[str, Any]:
    """Build the artifact a host model uses for structured extraction."""
    budget, texts, truncated = MAX_REQUEST_CHARS, [], False
    for number, text in pages:
        clean = redact_text(text)
        if len(clean) > budget:
            clean, truncated = clean[:budget], True
        budget -= len(clean)
        texts.append({"page": number, "text": clean})
        if budget <= 0:
            truncated = True
            break
    return {
        "kind": "extraction_request", "reason": reason, "instructions": INSTRUCTIONS,
        "schema": EXTRACTION_SCHEMA, "pages": texts, "truncated": truncated,
        "source": {k: provenance.get(k) for k in ("ref", "sha256", "filename", "media_type", "pages")},
        "next_step": "Call validate_llm_extraction(payload, source_text=<this request>) with the filled JSON.",
    }


def _types(value: Any) -> set[str]:
    if value is None:
        return {"null"}
    if isinstance(value, bool):
        return {"boolean"}
    if isinstance(value, int):
        return {"integer", "number"}
    if isinstance(value, float):
        return {"number"}
    if isinstance(value, str):
        return {"string"}
    if isinstance(value, list):
        return {"array"}
    if isinstance(value, dict):
        return {"object"}
    return {"unknown"}


def check_schema(schema: dict[str, Any], value: Any, path: str = "$", errors: list[str] | None = None) -> list[str]:
    """Validate the JSON-schema subset used by :data:`EXTRACTION_SCHEMA`."""
    errors = [] if errors is None else errors
    if len(errors) > 50:
        return errors
    if "anyOf" in schema:
        if not any(not check_schema(option, value, path, []) for option in schema["anyOf"]):
            errors.append(f"{path} matches no allowed shape")
        return errors
    expected = schema.get("type")
    if expected is not None:
        allowed = {expected} if isinstance(expected, str) else set(expected)
        if not (_types(value) & allowed):
            errors.append(f"{path} must be {'/'.join(sorted(allowed))}")
            return errors
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{path} is not an allowed value")
    if isinstance(value, str):
        if "pattern" in schema and re.fullmatch(schema["pattern"].strip("^$"), value) is None:
            errors.append(f"{path} has an invalid format")
        if len(value) > schema.get("maxLength", 10_000):
            errors.append(f"{path} is too long")
    if isinstance(value, int) and not isinstance(value, bool) and value < schema.get("minimum", value):
        errors.append(f"{path} is below the minimum")
    if isinstance(value, dict):
        properties = schema.get("properties", {})
        for key in schema.get("required", []):
            if key not in value:
                errors.append(f"{path}.{key} is required")
        if schema.get("additionalProperties") is False:
            for key in value:
                if key not in properties:
                    errors.append(f"{path}.{key} is not allowed")
        for key, item in value.items():
            if key in properties:
                check_schema(properties[key], item, f"{path}.{key}", errors)
    if isinstance(value, list):
        if len(value) > schema.get("maxItems", 10**9):
            errors.append(f"{path} has too many items")
        for index, item in enumerate(value):
            check_schema(schema.get("items", {}), item, f"{path}[{index}]", errors)
    return errors


def _source_texts(source_text: Any) -> tuple[str | None, dict[str, Any] | None]:
    if source_text is None:
        return None, None
    if isinstance(source_text, dict) and source_text.get("kind") == "extraction_request":
        return "\n".join(page["text"] for page in source_text.get("pages", [])), source_text.get("source")
    if isinstance(source_text, (list, tuple)):
        return "\n".join(item[1] if isinstance(item, (list, tuple)) else str(item) for item in source_text), None
    return str(source_text), None


def _compact(text: str) -> str:
    return re.sub(r"\s+", "", text)


def _day_months(text: str) -> set[tuple[int, int]]:
    found = {(d.month, d.day) for first in (True, False) for d, _ in find_dates(text, day_first=first)}
    for match in re.finditer(r"(?i)\b(\d{1,2})[\s/.\-]?(ene|feb|mar|abr|may|jun|jul|ago|sep|set|oct|nov|dic|jan|apr|aug|dec)[a-z]*\b", text):
        months = {"ene": 1, "jan": 1, "feb": 2, "mar": 3, "abr": 4, "apr": 4, "may": 5, "jun": 6, "jul": 7,
                  "ago": 8, "aug": 8, "sep": 9, "set": 9, "oct": 10, "nov": 11, "dic": 12, "dec": 12}
        found.add((months[match.group(2).lower()[:3]], int(match.group(1))))
    for match in re.finditer(r"\b(\d{1,2})[/\-.](\d{1,2})\b", text):
        a, b = int(match.group(1)), int(match.group(2))
        found.update({(a, b), (b, a)})
    return found


def verify_payload(payload: dict[str, Any], text: str | None) -> dict[str, Any]:
    """Check every extracted number, date, symbol and account suffix against the source text."""
    checked, unverified = 0, []
    if text is None:
        return {"source_text": False, "checked": 0,
                "unverified": [{"path": "$", "value": None, "reason": "no source text supplied to verify against"}]}
    tokens = number_tokens(text)
    compact = _compact(text)
    upper = fold(text).upper()
    full_dates = {d for first in (True, False) for d, _ in find_dates(text, day_first=first)}
    partial = _day_months(text)

    def walk(value: Any, path: str, key: str | None) -> None:
        nonlocal checked
        if isinstance(value, dict):
            for k, v in value.items():
                walk(v, f"{path}.{k}", k)
            return
        if isinstance(value, list):
            for i, v in enumerate(value):
                walk(v, f"{path}[{i}]", key)
            return
        if value is None or key is None:
            return
        if key in _NUMERIC_FIELDS:
            checked += 1
            raw = str(value).strip()
            amount = parse_amount(raw.replace("%", "")) if raw else None
            ok = bool(raw) and (_compact(raw) in compact or (amount is not None and (amount in tokens or abs(amount) in tokens)))
            if not ok:
                unverified.append({"path": path, "value": raw, "reason": "number does not appear in the source text"})
        elif key in {"as_of", "period_start"}:
            checked += 1
            if date.fromisoformat(value) not in full_dates:
                unverified.append({"path": path, "value": value, "reason": "date does not appear in the source text"})
        elif key in {"date", "settlement_date"}:
            checked += 1
            parsed = date.fromisoformat(value)
            if parsed not in full_dates and (parsed.month, parsed.day) not in partial:
                unverified.append({"path": path, "value": value, "reason": "date does not appear in the source text"})
        elif key == "number_last4":
            checked += 1
            if value not in text:
                unverified.append({"path": path, "value": value, "reason": "account suffix does not appear in the source text"})
        elif key == "symbol":
            checked += 1
            if fold(value).upper() not in upper:
                unverified.append({"path": path, "value": value, "reason": "symbol does not appear in the source text"})

    walk(payload, "$", None)
    return {"source_text": True, "checked": checked, "unverified": unverified}


def _date_errors(payload: dict[str, Any]) -> list[str]:
    errors = []

    def walk(value: Any, path: str) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                if key in {"as_of", "period_start", "date", "settlement_date"} and isinstance(item, str):
                    try:
                        date.fromisoformat(item)
                    except ValueError:
                        errors.append(f"{path}.{key} is not a real date")
                else:
                    walk(item, f"{path}.{key}")
        elif isinstance(value, list):
            for index, item in enumerate(value):
                walk(item, f"{path}[{index}]")

    walk(payload, "$")
    return errors


def validate_llm_extraction(payload: Any, source_text: Any, *, provenance: dict[str, Any] | None = None,
                            owner_id: str = "self", tolerance: Any = None) -> dict[str, Any]:
    """Validate a host-model extraction against its source text and reconcile it.

    ``source_text`` is the ``extraction_request`` returned earlier, a list of
    page texts, one string, or ``None`` (image without host-supplied text, in
    which case every value stays unverified and the proposal needs review).
    """
    from .model import build_proposal

    text, request_source = _source_texts(source_text)
    errors = check_schema(EXTRACTION_SCHEMA, payload)
    base = dict(provenance or request_source or {})
    base.setdefault("kind", "document")
    base.setdefault("ref", "document:llm-extraction")
    base["parser"] = "host-llm-validated"
    if not errors:
        errors.extend(_date_errors(payload))
    if errors:
        return envelope("rejected", {"provenance": base, "schema_errors": errors[:50]},
                        warnings=["The extraction does not match the required schema; nothing was accepted."],
                        sources=[base["ref"]])
    verification = verify_payload(payload, text)
    statement = {
        "institution": payload.get("institution"), "as_of": payload.get("as_of"),
        "period_start": payload.get("period_start"), "currency": payload.get("currency"),
        "fx": payload.get("fx") or [], "accounts": [],
    }
    for account in payload.get("accounts") or []:
        entry = dict(account)
        if (entry.get("type") in {"credit_card", "mortgage"}) and entry.get("flows"):
            entry["debt_flows"], entry["flows"] = entry["flows"], None
        entry["period_start"], entry["period_end"] = payload.get("period_start"), payload.get("as_of")
        statement["accounts"].append(entry)
    confidence = {"as_of": "medium" if payload.get("as_of") else "low",
                  "currency": "medium" if payload.get("currency") else "low"}
    warnings = []
    if not verification["source_text"]:
        warnings.append("No source text was supplied, so extracted values could not be verified.")
    return build_proposal(statement, kind="document", provenance=base, owner_id=owner_id, confidence=confidence,
                          verification=verification, warnings=warnings, tolerance=tolerance,
                          assumptions=["Values were extracted by the host model and checked against the source text."])


__all__ = ["EXTRACTION_SCHEMA", "check_schema", "extraction_request", "validate_llm_extraction", "verify_payload"]
