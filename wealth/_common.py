"""Small shared validators and result helpers for the financial modules.

Every domain module returns the same envelope and validates caller inputs the
same way. Keeping these helpers in one place prevents the silent divergence
that duplicated copies accumulate (different rounding, different acceptance of
booleans or strings, different error text).
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
import math
import re
from typing import Any, Iterable

_CURRENCY = re.compile(r"[A-Z]{3}")


def envelope(status: str, result: dict | None = None, *, missing: Iterable = (),
             warnings: Iterable = (), sources: Iterable = (),
             assumptions: Iterable = ()) -> dict[str, Any]:
    """The shared domain-module result shape."""
    return {"status": status, "result": result or {}, "missing": list(missing or ()),
            "warnings": list(warnings or ()), "sources": list(sources or ()),
            "assumptions": list(assumptions or ())}


def text(value: Any, field: str) -> str:
    """A nonempty string, stripped of surrounding whitespace."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a nonempty string")
    return value.strip()


def currency(value: Any, field: str = "currency") -> str:
    """An ISO-4217-shaped code; case is never repaired silently."""
    if not isinstance(value, str) or _CURRENCY.fullmatch(value) is None:
        raise ValueError(f"{field} must be three uppercase letters")
    return value


def number(value: Any, field: str, *, minimum: float | None = None) -> float:
    """A finite float. Booleans are rejected; numeric strings are parsed."""
    if isinstance(value, bool) or value is None:
        raise ValueError(f"{field} must be a finite number")
    try:
        out = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be a finite number") from exc
    if not math.isfinite(out):
        raise ValueError(f"{field} must be a finite number")
    if minimum is not None and out < minimum:
        raise ValueError(f"{field} must be a finite number greater than or equal to {minimum}")
    return out


def iso_date(value: Any, field: str) -> date:
    """A strict ``YYYY-MM-DD`` date (no datetimes, no lenient parsing)."""
    if not isinstance(value, str):
        raise ValueError(f"{field} must be an ISO date")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{field} must be an ISO date") from exc
    if parsed.isoformat() != value:
        raise ValueError(f"{field} must be an ISO date")
    return parsed


def decimal_text(value: Decimal) -> str:
    """Stable, JSON-safe decimal representation without scientific notation."""
    if value == 0:
        return "0"
    rendered = format(value.normalize(), "f")
    return rendered.rstrip("0").rstrip(".") if "." in rendered else rendered


def money(amount: float | Decimal, currency_code: str) -> dict[str, Any]:
    """A currency-tagged amount.

    Exact ``Decimal`` arithmetic is rendered as a string without rounding;
    floating-point simulation output is rounded to cents.
    """
    if isinstance(amount, Decimal):
        return {"currency": currency_code, "amount": decimal_text(amount)}
    return {"currency": currency_code, "amount": round(float(amount), 2)}


def decimal_sum(values: Iterable[Decimal | None]) -> Decimal | None:
    """Exact total of ``values``, or ``None`` as soon as one of them is unknown."""
    total = Decimal(0)
    for value in values:
        if value is None:
            return None
        total += value
    return total


def missing(key: str, reason: str, detail: str) -> dict[str, str]:
    """One ``missing`` row of the shared envelope."""
    return {"key": key, "reason": reason, "detail": detail}


def lang(language: str | None) -> str:
    """``"es"`` for any Spanish language tag, otherwise ``"en"``."""
    return "es" if str(language or "").lower().startswith("es") else "en"


def historical_cvar(losses: Any, confidence: float) -> float:
    """Mean of the losses at or beyond the empirical ``confidence`` quantile.

    ``losses`` are positive numbers for losses (negated returns). This is the
    plain historical estimator; with few observations the tail is a handful of
    days and the number is correspondingly noisy.
    """
    import numpy as np

    values = np.asarray(losses, dtype=float)
    if values.size == 0 or not np.isfinite(values).all():
        raise ValueError("historical CVaR requires finite observations")
    if not 0 < confidence < 1:
        raise ValueError("confidence must be strictly between zero and one")
    threshold = np.quantile(values, confidence)
    return float(values[values >= threshold].mean())
