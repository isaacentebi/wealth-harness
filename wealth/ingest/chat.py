"""Conversation-sourced financial facts in the common proposal shape.

The host model extracts structured items from what the person said; this module
checks each number against the person's own words (``quote``), then builds the
same proposal a statement would produce, with ``source_kind="user"``.

Item shape (unknown fields are omitted, never zero)::

    {"kind": "cash" | "account" | "position" | "liability" | "income",
     "label": "HYSA", "institution": None, "account_type": "savings",
     "amount": "40000", "currency": "USD", "rate": "4.2%",
     "symbol": None, "quantity": None, "account_label": None,
     "monthly_payment": None, "quote": "I have $40k in a HYSA at 4.2%"}
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .classify import account_type as infer_type
from .common import envelope, number_tokens, parse_amount, parse_percent, slug
from .model import build_proposal


_KINDS = ("cash", "account", "position", "liability", "income")


def _verify(item: dict[str, Any], index: int) -> list[dict[str, Any]]:
    quote = item.get("quote")
    unverified = []
    tokens = number_tokens(quote) if isinstance(quote, str) and quote.strip() else None
    for field in ("amount", "quantity", "rate", "monthly_payment"):
        value = item.get(field)
        if value in (None, ""):
            continue
        path = f"$.items[{index}].{field}"
        if tokens is None:
            unverified.append({"path": path, "value": str(value), "reason": "no quote from the person to verify against"})
            continue
        amount = parse_percent(value) if field == "rate" else parse_amount(value)
        candidates = {amount, abs(amount)} if amount is not None else set()
        if field == "rate" and amount is not None:
            candidates.add(amount * 100)
        if not candidates & tokens:
            unverified.append({"path": path, "value": str(value), "reason": "number does not appear in what the person said"})
    return unverified


def proposal_from_chat(items: list[dict[str, Any]], *, as_of: str | None = None, currency: str | None = None,
                       owner_id: str = "self", conversation_ref: str = "conversation") -> dict[str, Any]:
    """Build a user-sourced proposal from host-extracted conversation facts."""
    if not isinstance(items, list) or not items:
        raise ValueError("items must be a nonempty list")
    as_of = as_of or datetime.now(timezone.utc).date().isoformat()
    missing: list[dict[str, Any]] = []
    accounts: dict[str, dict[str, Any]] = {}
    income: list[dict[str, Any]] = []
    loose_liabilities: list[dict[str, Any]] = []
    unverified: list[dict[str, Any]] = []
    checked = 0
    for index, item in enumerate(items):
        if not isinstance(item, dict) or item.get("kind") not in _KINDS:
            raise ValueError(f"items[{index}].kind must be one of {_KINDS}")
        checked += sum(1 for f in ("amount", "quantity", "rate", "monthly_payment") if item.get(f) not in (None, ""))
        unverified.extend(_verify(item, index))
        kind = item["kind"]
        ccy = item.get("currency") or currency
        if kind == "income":
            income.append({"label": item.get("label") or "Income", "annual_amount": item.get("amount"), "currency": ccy})
            if item.get("amount") in (None, ""):
                missing.append({"key": f"items[{index}].amount", "reason": "missing", "detail": "Annual amount was not stated."})
            continue
        if kind == "liability":
            entry = {"label": item.get("label") or "Debt", "balance": item.get("amount"), "currency": ccy,
                     "minimum_payment": item.get("monthly_payment"), "interest_rate": item.get("rate")}
            if item.get("amount") in (None, ""):
                missing.append({"key": f"items[{index}].amount", "reason": "missing", "detail": "Balance owed was not stated."})
            loose_liabilities.append(entry)
            continue
        label = item.get("account_label") if kind == "position" else item.get("label")
        label = label or item.get("institution") or "Account"
        key = slug(label, 24)
        stated_type = item.get("account_type") or infer_type(f"{label} {item.get('institution') or ''}")[0]
        account = accounts.setdefault(key, {
            "label": label, "number_last4": None, "type": stated_type or ("savings" if kind == "cash" else None),
            "currency": ccy, "positions": [], "cash": [], "reported_total": None, "single_value": True,
            "liabilities": [],
        })
        if kind in ("cash", "account"):
            if item.get("amount") in (None, ""):
                missing.append({"key": f"items[{index}].amount", "reason": "missing", "detail": f"The balance of {label} was not stated."})
                continue
            if kind == "cash" or (stated_type in {"savings", "checking"}):
                account["cash"].append({"amount": item["amount"], "currency": ccy, "label": label})
            else:
                account["positions"].append({"symbol": None, "description": f"{label} (stated total)", "quantity": None,
                                             "market_value": item["amount"], "currency": ccy})
            if item.get("rate") not in (None, ""):
                account["interest_rate"] = item["rate"]
        else:
            if item.get("amount") in (None, "") and (item.get("quantity") in (None, "") or item.get("price") in (None, "")):
                missing.append({"key": f"items[{index}].amount", "reason": "missing",
                                "detail": f"The value of {item.get('symbol') or label} was not stated."})
            account["positions"].append({"symbol": item.get("symbol"), "description": item.get("label"),
                                         "quantity": item.get("quantity"), "price": item.get("price"),
                                         "market_value": item.get("amount"), "currency": ccy})
    statement_accounts = list(accounts.values())
    statement = {"institution": None, "institution_key": "user", "as_of": as_of, "currency": currency,
                 "accounts": statement_accounts, "liabilities": loose_liabilities, "income": income, "fx": []}
    provenance = {"kind": "user", "ref": f"{conversation_ref} {as_of}", "conversation": conversation_ref}
    if not statement_accounts and not income and not loose_liabilities:
        return envelope("needs_input", {"provenance": provenance}, missing=missing or [
            {"key": "items", "reason": "missing", "detail": "No usable balances, holdings, debts or income were stated."}])
    return build_proposal(statement, kind="user", provenance=provenance, owner_id=owner_id,
                          verification={"source_text": True, "checked": checked, "unverified": unverified},
                          missing=missing, confidence={"as_of": "high"},
                          assumptions=["Values are as the person stated them in conversation; they are reported, not statement-verified."])
