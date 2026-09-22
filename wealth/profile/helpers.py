"""Form constants and small readers shared by the profile views."""
from __future__ import annotations

import math
import re
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from .. import _common


FORM_FIELDS = ("income", "spending", "savings", "investments", "debts", "dependents",
               "tax_residence", "currencies", "goals", "risk")
# client.profile field written by the "Complete your profile" form for each amount field.
_FORM_PROFILE_FIELDS = {"income": "monthly_income", "spending": "monthly_spending", "savings": "savings",
                        "investments": "investments", "debts": "debts"}
_MARKET_VALUED = ("household", "portfolio.snapshot")
_CURRENCY = re.compile(r"^[A-Z]{3}$")


# ---------------------------------------------------------------- small helpers

def _as_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str) and value:
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return None
    return None


def _today(today: Any) -> date:
    parsed = _as_date(today)
    return parsed or datetime.now(timezone.utc).date()


def _num(value: Any) -> float | None:
    """A finite number from a number or numeric string; ``None`` when unknown."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value) if math.isfinite(value) else None
    if isinstance(value, Decimal):
        return float(value) if value.is_finite() else None
    if isinstance(value, str):
        text = value.strip().replace(",", "")
        if not text:
            return None
        try:
            parsed = Decimal(text)
        except InvalidOperation:
            return None
        return float(parsed) if parsed.is_finite() else None
    return None


def _ratio(numerator: float | None, denominator: float | None) -> float | None:
    if numerator is None or not denominator:
        return None
    return round(numerator / denominator, 6)


def _humanize(name: str) -> str:
    words = re.sub(r"[._]+", " ", str(name)).strip().split()
    if not words:
        return str(name)
    words = [w.upper() if w.lower() in _ACRONYMS else w for w in words]
    return " ".join([words[0][:1].upper() + words[0][1:], *words[1:]])


def _stale(fact: dict, today: date) -> bool:
    if isinstance(fact.get("stale"), bool) and today == datetime.now(timezone.utc).date():
        return fact["stale"]  # computed by the service
    expires = _as_date(fact.get("expires_on"))
    return bool(expires and expires < today)


def _snapshot(service: Any, client_id: str) -> dict:
    snapshot = service.inspect(client_id) if hasattr(service, "inspect") else service.client("inspect", client_id)
    if not isinstance(snapshot, dict):
        raise ValueError("client snapshot has an unexpected shape")
    facts = [f for f in snapshot.get("facts") or [] if isinstance(f, dict) and isinstance(f.get("key"), str)]
    decisions = [d for d in snapshot.get("decisions") or [] if isinstance(d, dict)]
    client = snapshot.get("client") if isinstance(snapshot.get("client"), dict) else {}
    return {"client": client, "facts": facts, "decisions": decisions}


def _history(service: Any, client_id: str, key: str) -> list[dict]:
    try:
        result = service.inspect(client_id, detail="history", key=key)
    except Exception:  # history is optional context for the dashboard
        return []
    rows = result.get("history") if isinstance(result, dict) else None
    return [row for row in rows or [] if isinstance(row, dict)]


def _facts_by_key(snapshot: dict) -> dict[str, dict]:
    return {fact["key"]: fact for fact in snapshot["facts"]}


def _dict_value(fact: dict | None) -> dict:
    value = fact.get("value") if fact else None
    return value if isinstance(value, dict) else {}


_ACRONYMS = {"esg", "etf", "fx", "usd", "mxn", "ira", "afore", "cetes", "ipc", "sp"}
_memory_lang = _common.lang
