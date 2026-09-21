"""Redaction applied to every ingestion output.

Account and card numbers keep only their last four digits.  US SSNs and
Mexican RFC/CURP identifiers are removed entirely; ingestion never emits them.
API keys and tokens (sk-…, sk-ant-…, ghp_…, AKIA…, xox…, bearer tokens, JWTs,
long hex or base64 strings) and "password/contraseña/pwd <value>" phrases are
replaced too, so stored conversations and diagnostics never keep a secret.
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


_SECRET_SHAPES = (
    re.compile(r"\bsk-ant-[A-Za-z0-9_-]{6,}"),                      # Anthropic keys
    re.compile(r"\b(?:sk|pk|rk)-(?:proj-|live-|test-)?[A-Za-z0-9_-]{8,}"),  # OpenAI / Stripe-style keys
    re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr|github_pat)_[A-Za-z0-9_]{16,}"),  # GitHub tokens
    re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),                    # AWS access key ids
    re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{8,}"),                   # Slack tokens
    re.compile(r"\beyJ[\w-]{8,}\.[\w-]{8,}(?:\.[\w-]*)?"),           # JWTs
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}"),            # Authorization: Bearer ...
)
# Long random-looking standalone strings: 32+ hex characters, or 40+ base64 characters mixing cases and digits
# (identifiers joined to a prefix by _, -, : or . are left alone: ledger and broker ids look like this).
_HEX_SECRET = re.compile(r"(?<![A-Za-z0-9_:./-])[0-9a-fA-F]{32,}(?![A-Za-z0-9_/-])")
_B64_SECRET = re.compile(r"(?<![A-Za-z0-9+/_.:-])[A-Za-z0-9+/]{40,}={0,2}(?![A-Za-z0-9+/_-])")
# "password hunter2", "contraseña: ...", "pwd=..." keep the word and drop the value.
_PASSWORD = re.compile(
    r"(?i)\b(password|passwd|passcode|pwd|contraseña|contrasena|clave de acceso|nip)"
    r"(\s*(?:[:=]|\bis\b|\bes\b|\bera\b|\bwas\b)?\s*)([^\s,;]+)"
)
_SEPARATED = re.compile(r"\s*(?:[:=]|\bis\b|\bes\b|\bera\b|\bwas\b)\s*", re.IGNORECASE)


def _mask_password(match: re.Match[str]) -> str:
    word, gap, secret = match.groups()
    # Without ":", "=", "is" or "es" the next word is only a secret when it is not a plain word
    # ("password reset" stays; "pwd hunter2" does not).
    if not _SEPARATED.fullmatch(gap) and secret.isalpha():
        return match.group(0)
    return f"{word}{gap or ' '}[REDACTED-SECRET]"


def _b64_secret(match: re.Match[str]) -> str:
    token = match.group(0)
    kinds = sum(bool(re.search(p, token)) for p in (r"[a-z]", r"[A-Z]", r"\d"))
    return "[REDACTED-SECRET]" if kinds == 3 and "/" not in token.strip("/") else token


def redact_secrets(text: str) -> str:
    """Remove API keys, tokens, long random strings and "password <value>" phrases."""
    if not text:
        return text
    value = text
    for pattern in _SECRET_SHAPES:
        value = pattern.sub("[REDACTED-SECRET]", value)
    value = _PASSWORD.sub(_mask_password, value)
    value = _HEX_SECRET.sub("[REDACTED-SECRET]", value)
    return _B64_SECRET.sub(_b64_secret, value)


def redact_text(text: str) -> str:
    """Remove SSN/RFC/CURP and secrets, and mask account-like numbers to their last four digits."""
    if not text:
        return text
    value = redact_secrets(text)
    value = _SSN.sub("[REDACTED-SSN]", value)
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
