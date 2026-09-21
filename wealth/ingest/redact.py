"""Redaction applied to every ingestion output.

Account and card numbers keep only their last four digits.  US SSNs and
Mexican RFC/CURP identifiers are removed entirely; ingestion never emits them.
"""

from __future__ import annotations

import re
from typing import Any


_SSN = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
_CURP = re.compile(r"\b[A-Z][AEIOUX][A-Z]{2}\d{6}[HM][A-Z]{5}[A-Z0-9]\d\b", re.IGNORECASE)
_RFC = re.compile(r"\b[A-ZÑ&]{3,4}-?\d{6}-?[A-Z0-9]{3}\b", re.IGNORECASE)
_LABELED = re.compile(
    r"(?i)\b((?:account|acct|a/c|cuenta|contrato|clabe|tarjeta|card|n[uú]mero de cuenta|no\.? de cuenta)"
    r"(?:\s*(?:number|no\.?|num\.?|n[uú]m(?:ero)?\.?|#|ending in|terminaci[oó]n))?\s*[:#.]?\s*)"
    r"([A-Z]{0,4}[\dXx*•\-]+(?: [\dXx*•\-]+)*\d)"
)
_CARD = re.compile(r"(?<![\d.,])\d{4}(?:[ -]\d{4}){3}(?![\d.,]*\d)")
_LONG = re.compile(r"(?<![\w.,\-/])\d{10,}(?![\w]|[.,]\d)")
_DASHED = re.compile(r"(?<![\d.,])\d{2,6}(?:-\d{2,8}){2,}(?![\d.,]*\d)")


def last4(number: Any) -> str | None:
    digits = re.sub(r"\D", "", str(number or ""))
    return digits[-4:] if len(digits) >= 4 else None


def mask_account(number: Any) -> str | None:
    tail = last4(number)
    return f"****{tail}" if tail else None


def _mask_match(match: re.Match[str]) -> str:
    return mask_account(match.group(0)) or "****"


def _mask_labeled(match: re.Match[str]) -> str:
    identifier = match.group(2)
    digits = re.sub(r"\D", "", identifier)
    if len(digits) < 4:
        return match.group(0)
    if len(digits) == 4 and re.search(r"[Xx*•]", identifier) is None and len(identifier.strip()) == 4:
        return match.group(0)
    trailing = " " if identifier.endswith(" ") else ""
    return match.group(1) + f"****{digits[-4:]}" + trailing


def redact_text(text: str) -> str:
    """Remove SSN/RFC/CURP and mask account-like numbers to their last four digits."""
    if not text:
        return text
    value = _SSN.sub("[REDACTED-SSN]", text)
    value = _CURP.sub("[REDACTED-CURP]", value)
    value = _RFC.sub(lambda m: m.group(0) if not re.search(r"\d{6}", m.group(0)) else "[REDACTED-RFC]", value)
    value = _LABELED.sub(_mask_labeled, value)
    value = _CARD.sub(_mask_match, value)
    value = _DASHED.sub(lambda m: _mask_match(m) if len(re.sub(r"\D", "", m.group(0))) >= 9 else m.group(0), value)
    return _LONG.sub(_mask_match, value)


_NUMERIC_KEYS = frozenset({
    "quantity", "value", "price", "cost_basis", "amount", "computed", "reported", "difference",
    "tolerance", "rate", "annual_amount", "monthly_payment", "interest_rate", "market_value",
    "expected", "opening", "deposits", "withdrawals", "closing", "positions_value", "cash",
    "computed_total", "reported_total", "positions_subtotal",
})


def redact(value: Any, key: str | None = None) -> Any:
    """Recursively redact strings, leaving numeric fields and digests untouched."""
    if isinstance(value, str):
        if key in _NUMERIC_KEYS or key in {"sha256", "proposal_id", "request_id"}:
            return value
        return redact_text(value)
    if isinstance(value, dict):
        return {k: redact(v, k) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(item, key) for item in value]
    return value
