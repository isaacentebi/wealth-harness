"""Turn a person-confirmed ingest proposal into one ledger posting batch.

The proposal describes the statement's end state (positions, cash, debt) and,
when printed, the period's transactions.  The ledger stores transactions, so:

* An account new to the ledger gets opening balances dated at the start of the
  statement period, derived as *end state minus the period's posted lines*, so
  the ledger reproduces the statement's closing numbers exactly.
* An account already in the ledger gets only the new lines; its closing
  numbers become balance assertions (a holding the ledger has but a newer
  statement no longer lists is asserted at zero).  After posting, differences
  the statement's own lines do not explain are reconciled with labelled
  adjustment entries (``reconciliation_lines``) when the statement is at least
  as new as anything the ledger knows about the account.

Lines that cannot be expressed in the ledger contract (a dividend without a
symbol, a split without a ratio, an installment-plan memo row) are returned in
``not_posted`` with the reason; nothing is guessed.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Mapping

from .ledger import active_entries, holdings as ledger_holdings
from .ledger.model import LedgerInputError, normalize_transaction


_KIND = {
    "deposit": "deposit", "withdrawal": "withdrawal", "transfer": "transfer", "buy": "buy", "sell": "sell",
    "dividend": "dividend", "interest": "interest", "fee": "fee", "tax_withheld": "tax_withheld",
    "split": "split", "fx": "fx_conversion", "income": "income", "expense": "expense",
    "loan_payment": "loan_payment", "opening_position": "opening_balance",
}
# A cash line whose printed sign contradicts its label is still a real cash movement.
_SIGN_FALLBACK = {"deposit": "transfer", "withdrawal": "transfer", "loan_payment": "transfer"}
_VENUES = {"bmv", "biva", "sic", "us", "other"}
_ZERO = Decimal(0)
# Ledger batches posted by guarded trade execution (wealth/execution/tickets.py) carry this batch-id prefix.
EXECUTION_BATCH_PREFIX = "alpaca-orders:"


def established_accounts(ledger: Mapping[str, Any] | None) -> set[str]:
    """Accounts a connector or statement sync has established in the ledger.

    An account is *known* for posting purposes only once a sync or statement
    has put its opening balances and history there.  Fills posted by trade
    execution alone do not count: otherwise a fill posted before the first
    connector sync would make that sync skip the account's opening balances
    and history.  Both orders (fill first, sync first) end with the same
    ledger because the fill carries the connector's own external id and is
    deduplicated.
    """
    return {e["account_id"] for e in (ledger or {}).get("entries", [])
            if not str(e.get("batch_id") or "").startswith(EXECUTION_BATCH_PREFIX)}


def _d(value: Any) -> Decimal | None:
    return None if value in (None, "") else Decimal(str(value))


def _s(value: Decimal) -> str:
    text = format(value.normalize(), "f")
    return "0" if text in {"-0", ""} else text


def _is_cash(position: Mapping[str, Any]) -> bool:
    return str(position.get("instrument_id", "")).upper().startswith("CASH:") or position.get("asset_class") == "cash"


def _instrument(position: Mapping[str, Any]) -> dict[str, Any]:
    row: dict[str, Any] = {"id": position["instrument_id"], "symbol": str(position.get("symbol") or position["instrument_id"]),
                           "currency": position["currency"]}
    if position.get("name"):
        row["name"] = str(position["name"])[:120]
    if position.get("asset_class"):
        row["asset_class"] = position["asset_class"]
    venue = position.get("venue")
    if venue in _VENUES and not (venue == "sic" and position["currency"] != "MXN"):
        row["venue"] = venue
    if position.get("underlying_symbol"):
        row["underlying_symbol"] = position["underlying_symbol"]
    if position.get("issuer_domicile") in {"US", "IE", "MX", "other"}:
        row["issuer_domicile"] = position["issuer_domicile"]
    return row


def _line(tx: Mapping[str, Any], instruments: dict[str, dict[str, Any]], symbols: dict[tuple[str, str], str]) -> tuple[dict[str, Any] | None, str | None]:
    """Map one ingest transaction to a ledger line, or explain why it cannot be posted."""
    if tx.get("installment"):
        return None, "installment-plan row (informational; the monthly charge is posted separately)"
    kind = _KIND.get(str(tx.get("type") or ""))
    if kind is None:
        return None, f"transaction type {tx.get('type')!r} is not a ledger kind"
    line: dict[str, Any] = {"kind": kind, "account_id": tx["account_id"], "date": tx["date"],
                            "currency": tx.get("currency"), "external_id": tx.get("id")}
    for source, target in (("settlement_date", "settle_date"), ("description", "description"), ("page", "page")):
        if tx.get(source) not in (None, ""):
            line[target] = tx[source]
    amount = _d(tx.get("amount"))
    symbol = tx.get("symbol")
    if symbol:
        instrument = symbols.get((tx["account_id"], str(symbol).upper())) or str(symbol).upper()
        if instrument not in instruments:
            instruments[instrument] = {"id": instrument, "symbol": str(symbol).upper(), "currency": tx.get("currency")}
        line["instrument_id"] = instrument
    quantity = _d(tx.get("quantity"))
    if kind in {"buy", "sell", "opening_balance"} and quantity is not None:
        line["quantity"] = _s(abs(quantity))
    elif kind == "transfer" and symbol and quantity is not None:
        line["quantity"] = _s(quantity)
    if kind in {"buy", "sell"}:
        if tx.get("price") not in (None, ""):
            line["price"] = _s(abs(_d(tx["price"])))
        if tx.get("fees") not in (None, ""):
            line["fee"] = _s(abs(_d(tx["fees"])))
    if kind != "opening_balance" and amount is not None:
        line["amount"] = _s(amount)
    elif kind == "opening_balance" and not symbol and amount is not None:
        line["amount"] = _s(amount)
    if kind == "income":
        line["subtype"] = "other"
    candidates = [line]
    fallback = _SIGN_FALLBACK.get(kind)
    if fallback and not symbol:
        candidates.append({**line, "kind": fallback})
    error = None
    for candidate in candidates:
        try:
            normalize_transaction({k: v for k, v in candidate.items() if v is not None}, 0,
                                  source={"kind": "document", "ref": "check"}, confidence=None)
            return {k: v for k, v in candidate.items() if v is not None}, None
        except LedgerInputError as exc:
            error = error or str(exc).replace("transactions[0]", "line")
    return None, error


def latest_ledger_date(ledger: Mapping[str, Any], account_id: str) -> str | None:
    """The newest date the ledger knows anything about for this account (lines or statement checks)."""
    entries, _ = active_entries(ledger, include_inferred=True)
    dates = [e["date"] for e in entries if e.get("account_id") == account_id]
    dates += [a["date"] for a in ledger.get("assertions") or [] if a.get("account_id") == account_id]
    return max(dates) if dates else None


def is_newest(ledger: Mapping[str, Any], account_id: str, as_of: str) -> bool:
    latest = latest_ledger_date(ledger, account_id)
    return latest is None or as_of >= latest


def missing_positions(proposal: Mapping[str, Any], ledger: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    """Holdings the ledger still has for this statement's accounts that the statement no longer lists.

    Only for a statement at least as new as what the ledger knows about the
    account (an older statement says nothing about today's holdings).
    """
    if not ledger or not ledger.get("entries"):
        return []
    result = proposal.get("result") or {}
    household = result.get("household") or {}
    as_of = result.get("as_of")
    if not as_of:
        return []
    established = established_accounts(ledger)
    accounts = {a["id"] for a in household.get("accounts") or []
                if a["id"] in established and is_newest(ledger, a["id"], as_of)}
    if not accounts:
        return []
    positions = [p for p in household.get("positions") or [] if not _is_cash(p)]
    listed = {(p["account_id"], p["instrument_id"]) for p in positions}
    listed_symbols = {(p["account_id"], str(p.get("symbol") or "").upper()) for p in positions}
    held = ledger_holdings(ledger, as_of, include_inferred=True)["result"]["positions"]
    out = []
    for position in held:
        key = (position["account_id"], position["instrument_id"])
        if position["account_id"] not in accounts or key in listed:
            continue
        if (position["account_id"], str(position.get("symbol") or "").upper()) in listed_symbols:
            continue
        if Decimal(position["quantity"]) == 0:
            continue
        out.append({"account_id": position["account_id"], "instrument_id": position["instrument_id"],
                    "symbol": position.get("symbol") or position["instrument_id"], "quantity": position["quantity"],
                    "as_of": as_of})
    return out


def reconciliation_lines(breaks: list[Mapping[str, Any]], accounts: set[str],
                         instruments: Mapping[str, Mapping[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Adjustment lines that bring the ledger to the statement, and a plain list of what changed.

    ``breaks`` are the statement's balance checks the ledger still misses after
    the statement's own lines were posted; only those on ``accounts`` are
    adjusted.  Cash gets an opening-balance correction; a larger position an
    opening position without basis; a smaller or vanished one an outgoing
    transfer (nothing is assumed sold, so no gain or loss is invented).
    """
    lines, changes = [], []
    for item in breaks:
        if item["account_id"] not in accounts:
            continue
        expected, derived = Decimal(str(item["expected"])), Decimal(str(item["derived"]))
        change = expected - derived
        if change == 0:
            continue
        base = {"account_id": item["account_id"], "date": item["date"], "confirm_not_duplicate": True}
        instrument = item.get("instrument_id")
        if instrument is None:
            lines.append({**base, "kind": "opening_balance", "amount": _s(change), "currency": item["currency"],
                          "description": "Statement reconciliation: cash set to the statement's balance"})
            reason = "cash balance"
        elif change > 0:
            line = {**base, "kind": "opening_balance", "instrument_id": instrument, "quantity": _s(change),
                    "description": "Statement reconciliation: position set to the statement's quantity"}
            if instruments.get(instrument, {}).get("currency"):
                line["currency"] = instruments[instrument]["currency"]
            lines.append(line)
            reason = "more units than the ledger had"
        else:
            gone = expected == 0
            lines.append({**base, "kind": "transfer", "instrument_id": instrument, "quantity": _s(change),
                          "description": "Statement reconciliation: position no longer on the statement" if gone
                          else "Statement reconciliation: position set to the statement's quantity"})
            reason = "no longer on the statement" if gone else "fewer units than the ledger had"
        changes.append({"account_id": item["account_id"], "instrument_id": instrument, "currency": item.get("currency"),
                        "symbol": instruments.get(instrument, {}).get("symbol", instrument) if instrument else None,
                        "date": item["date"], "ledger": _s(derived), "statement": _s(expected), "change": _s(change),
                        "reason": reason})
    return lines, changes


def describe_changes(changes: list[Mapping[str, Any]]) -> str:
    """'VTI 100 to 110; AAPL 10.5 to 0 (no longer on the statement); USD cash 1500 to 1000'."""
    parts = []
    for change in changes:
        what = change["symbol"] or f"{change['currency']} cash"
        text = f"{what} {change['ledger']} to {change['statement']}"
        if change["reason"] == "no longer on the statement":
            text += " (no longer on the statement)"
        parts.append(text)
    return "; ".join(parts)


def proposal_to_batch(proposal: Mapping[str, Any], *, batch_id: str, ledger: Mapping[str, Any]) -> dict[str, Any]:
    """Return ``{"batch": ..., "not_posted": [...], "notes": [...], "prices": {...}}``.

    ``ledger`` is the client's current ledger document; it decides which
    accounts are new (opening balances) and which only receive new lines.
    ``batch`` is ``None`` when there is nothing the ledger can hold.
    """
    result = proposal["result"]
    household = result["household"]
    as_of = result["as_of"]
    provenance = result.get("provenance") or {}
    kind = result.get("source_kind")
    source: dict[str, Any] = {"kind": kind if kind in {"document", "user"} else "tool",
                              "ref": provenance.get("ref") or f"ingest:{result['proposal_id'][:16]}",
                              "observed_on": as_of}
    if provenance.get("sha256"):
        source["file_hash"] = provenance["sha256"]
    active = established_accounts(ledger)
    missing = missing_positions(proposal, ledger)
    notes: list[str] = []
    not_posted: list[dict[str, Any]] = []
    accounts, instruments, lines, assertions = [], {}, [], []
    symbols: dict[tuple[str, str], str] = {}
    prices: dict[str, list[dict[str, str]]] = {}
    for position in household["positions"]:
        if not _is_cash(position):
            instruments.setdefault(position["instrument_id"], _instrument(position))
            symbols[(position["account_id"], str(position.get("symbol") or "").upper())] = position["instrument_id"]
            value, quantity = _d(position.get("value")), _d(position.get("quantity"))
            # value / quantity reproduces the statement's printed value exactly; a rounded price may not.
            price = value / quantity if value is not None and quantity else _d(position.get("price"))
            if price is not None:
                prices.setdefault(position["instrument_id"], []).append({"date": as_of, "price": str(price)})
    periods = {a["account_id"]: a for a in result.get("balance_assertions") or []}
    skipped_accounts = set()
    for account in household["accounts"]:
        if account["currency"] == "XXX":
            skipped_accounts.add(account["id"])
            notes.append(f"Account {account['id']} has no known currency; it was not posted to the ledger.")
            continue
        row = {"id": account["id"], "institution": account.get("institution") or account.get("name") or "unspecified",
               "type": account["type"], "currency": account["currency"],
               "owners": [{"person_id": account.get("owner_id") or "self", "share": "1"}]}
        if account.get("name"):
            row["name"] = account["name"]
        accounts.append(row)

    by_account: dict[str, list[dict[str, Any]]] = {}
    for tx in result.get("transactions") or []:
        if tx.get("account_id") in skipped_accounts:
            continue
        line, reason = _line(tx, instruments, symbols)
        if line is None:
            not_posted.append({"date": tx.get("date"), "description": tx.get("description"), "amount": tx.get("amount"),
                               "account_id": tx.get("account_id"), "reason": reason})
            continue
        by_account.setdefault(tx["account_id"], []).append(line)

    for account in accounts:
        account_id, currency = account["id"], account["currency"]
        own = [p for p in household["positions"] if p["account_id"] == account_id]
        posted = by_account.get(account_id, [])
        cash: dict[str, Decimal] = {}
        cash_known = False
        for position in own:
            if _is_cash(position):
                cash[position["currency"]] = cash.get(position["currency"], _ZERO) + Decimal(str(position["quantity"]))
                cash_known = True
        for liability in household["liabilities"]:
            if liability.get("account_id") == account_id and liability.get("value") not in (None, ""):
                ccy = liability.get("currency") or currency
                cash[ccy] = cash.get(ccy, _ZERO) - Decimal(str(liability["value"]))
                cash_known = True
        period = periods.get(account_id) or {}
        start = period.get("period_start") or min([l["date"] for l in posted] or [as_of])
        end = period.get("period_end") or as_of
        if account_id not in active:
            net_cash: dict[str, Decimal] = {}
            net_qty: dict[str, Decimal] = {}
            traded: set[str] = set()
            for line in posted:
                if line.get("amount") is not None:
                    net_cash[line["currency"]] = net_cash.get(line["currency"], _ZERO) + Decimal(line["amount"])
                if line.get("instrument_id") and line.get("quantity") is not None:
                    sign = {"buy": 1, "sell": -1, "transfer": 1}.get(line["kind"])
                    if sign:
                        traded.add(line["instrument_id"])
                        net_qty[line["instrument_id"]] = net_qty.get(line["instrument_id"], _ZERO) + sign * Decimal(line["quantity"])
            opening: list[dict[str, Any]] = []
            if cash_known or net_cash:
                for ccy in sorted(set(cash) | set(net_cash)):
                    if ccy not in cash and not cash_known:
                        continue
                    amount = cash.get(ccy, _ZERO) - net_cash.get(ccy, _ZERO)
                    if amount != 0 or ccy in cash:
                        opening.append({"kind": "opening_balance", "account_id": account_id, "date": start,
                                        "amount": _s(amount), "currency": ccy, "description": "Opening balance derived from statement"})
            elif posted:
                notes.append(f"Account {account_id}: the statement shows no closing balance, so its opening cash is unknown; "
                             "only the period's lines were posted.")
            for position in own:
                if _is_cash(position):
                    continue
                instrument = position["instrument_id"]
                quantity = Decimal(str(position["quantity"])) - net_qty.get(instrument, _ZERO)
                if quantity < 0:
                    notes.append(f"{instrument} in {account_id}: period trades exceed the closing quantity; opening quantity not posted.")
                    continue
                if quantity == 0:
                    continue
                lots = [l for l in household["lots"] if l["account_id"] == account_id and l["instrument_id"] == instrument]
                base = {"kind": "opening_balance", "account_id": account_id, "date": start, "instrument_id": instrument,
                        "currency": position["currency"]}
                if lots and instrument not in traded:
                    for lot in lots:
                        opening.append({**base, "quantity": str(lot["quantity"]), "cost_basis": str(lot["cost_basis"]),
                                        "acquired_on": lot["acquired_on"], "description": f"Opening lot {lot['id']}"})
                else:
                    entry = {**base, "quantity": _s(quantity), "description": f"Opening position {position.get('symbol') or instrument}"}
                    if position.get("cost_basis") not in (None, "") and instrument not in traded:
                        entry["cost_basis"] = str(position["cost_basis"])
                    opening.append(entry)
            lines.extend(opening)
        elif not posted:
            notes.append(f"Account {account_id} is already in the ledger and this statement lists no transactions; "
                         "its closing numbers were recorded as balance checks only.")
        lines.extend(posted)
        if cash_known:  # debt shows as a negative balance, as the ledger records it
            for ccy, balance in sorted(cash.items()):
                assertions.append({"account_id": account_id, "date": end if end >= start else as_of,
                                   "currency": ccy, "balance": _s(balance)})
        for position in own:
            if not _is_cash(position):
                assertions.append({"account_id": account_id, "date": as_of, "instrument_id": position["instrument_id"],
                                   "quantity": str(position["quantity"])})
        if account_id in active:
            for gone in missing:
                if gone["account_id"] == account_id:
                    assertions.append({"account_id": account_id, "date": as_of, "instrument_id": gone["instrument_id"],
                                       "quantity": "0"})
    fx = []
    for item in household.get("fx") or []:
        fx.append({"date": item.get("as_of") or as_of, "base": item["from"], "quote": item["to"],
                   "rate": str(item["rate"]), "source": item.get("source") or source["ref"]})
    batch = None
    if accounts and (lines or assertions):
        batch = {"batch_id": batch_id, "source": source, "accounts": accounts,
                 "instruments": list(instruments.values()), "transactions": lines,
                 "balance_assertions": assertions, "fx": fx}
    return {"batch": batch, "not_posted": not_posted, "notes": notes, "prices": prices}


__all__ = ["EXECUTION_BATCH_PREFIX", "describe_changes", "established_accounts", "is_newest", "latest_ledger_date",
           "missing_positions", "proposal_to_batch",
           "reconciliation_lines"]
