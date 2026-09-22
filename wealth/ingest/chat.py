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
from decimal import Decimal
import re
from typing import Any

from .classify import account_type as infer_type
from .common import envelope, number_tokens, parse_amount, parse_percent, slug, with_institution
from .model import build_proposal


_KINDS = ("cash", "account", "position", "liability", "income")
# The words a model naturally writes for each kind, and the account type they imply.
_KIND_ALIASES = {
    "investment": ("account", "brokerage"), "investments": ("account", "brokerage"),
    "brokerage": ("account", "brokerage"), "retirement": ("account", None), "afore": ("account", "afore"),
    "savings": ("cash", "savings"), "checking": ("cash", "checking"), "bank": ("cash", "checking"),
    "deposit": ("cash", "savings"), "debt": ("liability", None), "loan": ("liability", None),
    "card": ("liability", None), "credit_card": ("liability", None), "mortgage": ("liability", None),
    "salary": ("income", None), "wage": ("income", None), "holding": ("position", None),
}
# (times a year, the saved frequency or None when Wealth keeps no such frequency: then the year's figure is saved).
# Quincenal is twice a month (24 a year, "semimonthly"); catorcenal is every 14 days (26, "biweekly").
_PERIODS = {
    "weekly": (52, "weekly"), "semanal": (52, "weekly"), "week": (52, "weekly"),
    "biweekly": (26, "biweekly"), "catorcenal": (26, "biweekly"), "fortnightly": (26, "biweekly"),
    "semimonthly": (24, "semimonthly"), "quincenal": (24, "semimonthly"), "twicemonthly": (24, "semimonthly"),
    "twiceamonth": (24, "semimonthly"),
    "monthly": (12, "monthly"), "month": (12, "monthly"), "mensual": (12, "monthly"),
    "bimonthly": (6, None), "bimestral": (6, None), "quarterly": (4, None), "trimestral": (4, None),
    "semiannual": (2, None), "semiannually": (2, None), "semestral": (2, None),
    "annual": (1, "annual"), "yearly": (1, "annual"), "anual": (1, "annual"), "year": (1, "annual"),
}
# The period said in the person's words when the model did not name one (most specific first).
_QUOTE_PERIODS = (
    (re.compile(r"(?i)\b(quincenal(es)?|a la quincena|por quincena|cada quincena|twice a month|"
                r"semi[- ]?monthly)\b"), "quincenal"),
    (re.compile(r"(?i)\b(catorcenal(es)?|cada catorce d[ií]as|every two weeks|bi[- ]?weekly|fortnightly)\b"),
     "catorcenal"),
    (re.compile(r"(?i)\b(semanal(es)?|a la semana|por semana|cada semana|a week|per week|weekly)\b"), "semanal"),
    (re.compile(r"(?i)\b(bimestral(es)?|cada dos meses|al bimestre|por bimestre)\b"), "bimestral"),
    (re.compile(r"(?i)\b(trimestral(es)?|al trimestre|por trimestre|quarterly|a quarter)\b"), "trimestral"),
    (re.compile(r"(?i)\b(semestral(es)?|al semestre|por semestre|semi[- ]?annual(ly)?|twice a year)\b"), "semestral"),
    (re.compile(r"(?i)(\bal mes\b|\bmensual(es)?\b|\bpor mes\b|\bcada mes\b|\ba month\b|\bper month\b|"
                r"\bmonthly\b|/mes\b|/mo\b)"), "mensual"),
    (re.compile(r"(?i)\b(al a[nñ]o|anual(es)?|por a[nñ]o|a year|per year|yearly|annual)\b"), "anual"),
)


def _canonical(item: dict[str, Any]) -> dict[str, Any]:
    kind = str(item.get("kind") or "").strip().lower()
    if kind in _KINDS:
        return item
    alias = _KIND_ALIASES.get(kind)
    if alias is None:
        return item
    canonical, implied = alias
    out = {**item, "kind": canonical}
    if implied and not out.get("account_type") and canonical in ("account", "cash"):
        out["account_type"] = implied
    return out


def _annual(item: dict[str, Any]) -> tuple[Any, str | None, str | None]:
    """An income amount per year, the note saying so, and the frequency to save it with.

    "gano 85 mil al mes" is 1,020,000 a year, never 85,000; "10 mil a la quincena" is 240,000 (24 a year).
    The frequency is None when Wealth keeps none for that period (bimestral): the year's figure is saved."""
    amount = parse_amount(item.get("amount"))
    if amount is None:
        return item.get("amount"), None, None
    # "semi-monthly", "semi_monthly", "Semi Monthly" and "semimonthly" are one period.
    period = re.sub(r"[\s_-]+", "", str(item.get("frequency") or item.get("period") or "").strip().lower())
    if period not in _PERIODS:
        period = next((name for pattern, name in _QUOTE_PERIODS if pattern.search(str(item.get("quote") or ""))),
                      period)
    factor, frequency = _PERIODS.get(period, (None, None))
    if factor is None or factor == 1:
        return item.get("amount"), None, "annual" if factor == 1 else None
    note = f"{item.get('label') or 'Income'}: {amount} {period} was saved as {amount * factor} a year."
    return str(amount * factor), note, frequency


_LIABILITY_WORDS = ((re.compile(r"(?i)\b(hipoteca|mortgage|infonavit|fovissste)\b"), "mortgage"),
                    (re.compile(r"(?i)\b(auto|coche|carro|car)\b"), "auto"),
                    (re.compile(r"(?i)\b(tarjeta|card|tdc)\b"), "card"),
                    (re.compile(r"(?i)\b(student|educativo|estudiantil)\b"), "student"),
                    (re.compile(r"(?i)\b(personal|n[oó]mina)\b"), "personal"))
_BROKERS = frozenset({"gbm", "actinver", "kuspit", "cetesdirecto", "schwab", "fidelity", "vanguard", "ibkr",
                      "merrill", "etrade", "morganstanley", "robinhood"})  # classify.INSTITUTIONS keys
_INVESTMENT_KINDS = {"brokerage": "brokerage", "afore": "afore", "ira": "retirement", "roth_ira": "retirement",
                     "401k": "retirement", "ppr": "retirement", "hsa": "retirement"}


def stated_facts(proposal: dict[str, Any]) -> list[dict[str, Any]] | None:
    """What the person said, as the facts the rest of Wealth reads for stated balances (see :func:`stated_plan`)."""
    plan = stated_plan(proposal)
    return plan[0] if plan else None


def stated_plan(proposal: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, list[str]]] | None:
    """The stated facts and, for every proposed account, debt and income id, the fact keys that hold it.

    Every proposed item must map to at least one fact: an account named without a balance ("a Roth IRA at
    Fidelity") is saved with ``balance_unknown: true``, never dropped.

    ``cash.<name>``, ``investment.<name>``, ``liability.<name>`` and ``income.<name>`` (source ``user``) are what a
    statement later covers: its figure replaces the stated one and the difference is shown ("you said 400,000;
    the statement shows 398,365.82").  Saving them as statement records instead left "BBVA 60,000 (stated)" beside
    "BBVA 76,581.87 (statement)" and counted both.  Returns None when the person named holdings (symbols and
    quantities) or a debt or income without its amount, which only an account record can hold.
    """
    from .classify import detect_institution

    result = proposal.get("result") or {}
    household = result.get("household") or {}
    provenance = result.get("provenance") or {}
    source = {"kind": "user", "ref": str(provenance.get("ref") or "conversation"), "observed_on": result.get("as_of")}
    facts: list[dict[str, Any]] = []
    keys: set[str] = set()
    covers: dict[str, list[str]] = {}

    def add(prefix: str, name: str, value: dict[str, Any], item: str) -> None:
        base, counter = slug(name or prefix, 48), 2
        key = f"{prefix}.{base}"
        while key in keys:
            key, counter = f"{prefix}.{base}-{counter}", counter + 1
        keys.add(key)
        facts.append({"key": key, "value": value, "source": source, "confidence": "reported"})
        covers.setdefault(item, []).append(key)

    totals: dict[str, list[dict[str, Any]]] = {}
    for row in result.get("unresolved") or []:
        if row.get("reason") == "quantity not printed" and row.get("value") is not None:
            totals.setdefault(row["account_id"], []).append(row)  # "Tengo 400k en GBM": a stated total
        else:
            return None  # a holding without a value: only an account record keeps it
    for account in household.get("accounts") or []:
        rows = [p for p in household.get("positions") or [] if p.get("account_id") == account["id"]]
        if any(p.get("asset_class") != "cash" for p in rows):
            return None
        name = account.get("name") or account["id"]
        # The institution the person named, else one the account's name implies ("BBVA").
        institution = account.get("institution") or detect_institution(name)[1]
        keyed = with_institution(name, institution)
        # Amounts are never summed across currencies: "USD 100 and MXN 1,000 in Wallet" is two balances, saved as
        # cash.wallet-usd and cash.wallet-mxn (a cash.<id> fact holds one amount in one currency).
        by_currency: dict[str, Decimal] = {}
        for position in rows:
            ccy = position.get("currency") or account["currency"]
            by_currency[ccy] = by_currency.get(ccy, Decimal(0)) + Decimal(position["value"])
        stated = totals.get(account["id"], [])
        currencies = set(by_currency) | {row.get("currency") or account["currency"] for row in stated}
        # Cash said to be at a broker ("5 mil en efectivo en GBM") is part of that brokerage account: saved as an
        # investment, so the broker's statement (which includes its cash) replaces it instead of adding to it.
        at_broker = account.get("type") in (None, "savings", "brokerage") and \
            detect_institution(institution or name)[0] in _BROKERS
        for ccy, amount in sorted(by_currency.items()):
            value: dict[str, Any] = {"amount": float(amount), "currency": ccy, "name": name}
            if institution:
                value["institution"] = institution
            if at_broker:
                add("investment", f"{keyed} {ccy}" if len(currencies) > 1 else keyed,
                    {**value, "institution": institution or name, "kind": "brokerage"}, account["id"])
                continue
            if account.get("interest_rate") is not None:
                value["annual_rate"] = float(account["interest_rate"])
            add("cash", f"{keyed} {ccy}" if len(currencies) > 1 else keyed, value, account["id"])
        for row in stated:
            ccy = row.get("currency") or account["currency"]
            value = {"amount": float(Decimal(row["value"])), "currency": ccy,
                     "institution": institution or name, "name": name,
                     "kind": _INVESTMENT_KINDS.get(account.get("type") or "", "other")}
            if account.get("type") in ("ira", "roth_ira", "401k"):
                value["plan_type"] = account["type"]
            add("investment", f"{keyed} {ccy}" if len(currencies) > 1 else keyed, value, account["id"])
        if not currencies:
            # Named without a balance: it exists, the amount is unknown (never 0, never dropped).
            cash = account.get("type") in ("savings", "checking") and not at_broker
            value = {"balance_unknown": True, "name": name}
            if account.get("currency") and account["currency"] != "XXX":
                value["currency"] = account["currency"]
            if institution:
                value["institution"] = institution
            if not cash:
                value["kind"] = _INVESTMENT_KINDS.get(account.get("type") or "", "other")
                if account.get("type") in ("ira", "roth_ira", "401k"):
                    value["plan_type"] = account["type"]
            add("cash" if cash else "investment", keyed, value, account["id"])
    for liability in household.get("liabilities") or []:
        if liability.get("account_id"):
            return None
        if liability.get("value") is None:
            return None  # a debt without its balance cannot be a stated liability
        name = liability.get("name") or "Debt"
        kind = next((k for pattern, k in _LIABILITY_WORDS if pattern.search(name)), "other")
        value = {"kind": kind, "balance": float(Decimal(liability["value"])), "currency": liability["currency"],
                 "name": name}
        lender = liability.get("lender") or detect_institution(name)[1]
        if lender:
            value["lender"] = lender
        if liability.get("interest_rate") is not None:
            value["annual_rate"] = float(liability["interest_rate"])
        if liability.get("monthly_payment") is not None:
            value.update(payment=float(liability["monthly_payment"]), payment_frequency="monthly")
        add("liability", with_institution(name, lender), value, liability["id"])
    for income in household.get("income_exposures") or []:
        if income.get("annual_amount") is None:
            return None  # an income without its amount cannot be a stated income
        name = income.get("description") or "Income"
        per_period, frequency = income.get("per_period"), income.get("frequency")
        value = {"amount": float(Decimal(per_period if per_period and frequency else income["annual_amount"])),
                 "currency": income["currency"], "frequency": frequency if per_period and frequency else "annual",
                 "name": name}
        add("income", name, value, income["id"])
    return (facts, covers) if facts else None


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
    notes: list[str] = []
    for index, item in enumerate(items):
        item = _canonical(item) if isinstance(item, dict) else item
        if not isinstance(item, dict) or item.get("kind") not in _KINDS:
            raise ValueError(f"items[{index}].kind must be one of {_KINDS} (or investment, savings, debt, card, "
                             "loan, salary)")
        checked += sum(1 for f in ("amount", "quantity", "rate", "monthly_payment") if item.get(f) not in (None, ""))
        unverified.extend(_verify(item, index))
        kind = item["kind"]
        ccy = item.get("currency") or currency
        if kind == "income":
            annual, note, period = _annual(item)
            if note:
                notes.append(note)
            income.append({"label": item.get("label") or "Income", "annual_amount": annual, "currency": ccy,
                           **({"frequency": period, "per_period": item.get("amount")} if period else {})})
            if item.get("amount") in (None, ""):
                missing.append({"key": f"items[{index}].amount", "reason": "missing", "detail": "Annual amount was not stated."})
            continue
        if kind == "liability":
            entry = {"label": item.get("label") or "Debt", "balance": item.get("amount"), "currency": ccy,
                     "minimum_payment": item.get("monthly_payment"), "interest_rate": item.get("rate"),
                     **({"institution": item["institution"]} if isinstance(item.get("institution"), str)
                        and item["institution"].strip() else {})}
            if item.get("amount") in (None, ""):
                missing.append({"key": f"items[{index}].amount", "reason": "missing", "detail": "Balance owed was not stated."})
            loose_liabilities.append(entry)
            continue
        label = item.get("account_label") if kind == "position" else item.get("label")
        institution = item.get("institution").strip() if isinstance(item.get("institution"), str) else ""
        label = label or institution or "Account"
        # "Checking" at BBVA and "Checking" at Banorte are two accounts: the institution is part of the key.
        key = slug(with_institution(label, institution), 48)
        stated_type = item.get("account_type") or infer_type(f"{label} {institution}")[0]
        account = accounts.setdefault(key, {
            "label": label, "number_last4": None, "type": stated_type or ("savings" if kind == "cash" else None),
            "currency": ccy, "positions": [], "cash": [], "reported_total": None, "single_value": True,
            "liabilities": [], **({"institution": institution} if institution else {}),
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
                          assumptions=["Values are as the person stated them in conversation; they are reported, "
                                       "not statement-verified.", *notes])
