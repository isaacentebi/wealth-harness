"""Pure derivations over a ledger document.

A *ledger document* is the plain data returned by ``WealthStore.ledger`` (or
built by hand in tests)::

    {"accounts": [...], "instruments": [...], "entries": [...], "fx": [...],
     "assertions": [...], "category_rules": [...], "labels": [...]}

Every function here is deterministic and side-effect free.  Unknown values stay
``None`` and are reported as missing; nothing unknown is treated as zero.
"""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass, field, replace
from datetime import date
from decimal import Decimal
from typing import Any, Callable, Iterable, Mapping, Sequence

from ..finmath import FxTable, holding_character
from .model import LIABILITY_TYPES, money, out


TRANSFER_WINDOW_DAYS = 5
DEFAULT_FX_AGE_DAYS = 5
_ZERO = Decimal(0)


def _d(value: Any) -> Decimal | None:
    return None if value is None else Decimal(str(value))


def envelope(status: str, result: dict[str, Any], *, missing: list | None = None,
             warnings: list[str] | None = None, sources: list | None = None,
             assumptions: list[str] | None = None) -> dict[str, Any]:
    return {"status": status, "result": result, "missing": missing or [], "warnings": warnings or [],
            "sources": sources or [], "assumptions": assumptions or []}


def status_for(missing: Sequence[Any], warnings: Sequence[str]) -> str:
    return "partial" if missing or warnings else "ready"


# -- FX ---------------------------------------------------------------------

# ``FxTable`` lives in :mod:`wealth.finmath` (one implementation for the ledger and
# the situation); it is re-exported here for existing callers.


# -- selection and transfer matching ---------------------------------------

def active_entries(ledger: Mapping[str, Any], *, include_inferred: bool = False) -> tuple[list[dict[str, Any]], list[str]]:
    """Entries that count: not reversals, not reversed, optionally not inferred."""

    entries = [dict(entry, seq=entry.get("seq", index + 1)) for index, entry in enumerate(ledger.get("entries", []))]
    reversed_ids = {entry["reverses_id"] for entry in entries if entry["kind"] == "reversal"}
    notes, active = [], []
    inferred = 0
    for entry in entries:
        if entry["kind"] == "reversal" or entry["id"] in reversed_ids:
            continue
        if entry.get("confidence") == "inferred" and not include_inferred:
            inferred += 1
            continue
        active.append(entry)
    if reversed_ids:
        notes.append(f"{len(reversed_ids)} reversed entr{'y' if len(reversed_ids) == 1 else 'ies'} and their reversals are excluded.")
    if inferred:
        notes.append(f"{inferred} inferred entr{'y' if inferred == 1 else 'ies'} excluded; confirm them to include.")
    return sorted(active, key=_day_order(active)), notes


def _is_trade(entry: Mapping[str, Any]) -> bool:
    return entry["kind"] in {"buy", "sell"} or (entry["kind"] == "transfer" and bool(entry.get("instrument_id")))


def _disposes(entry: Mapping[str, Any]) -> bool:
    if entry["kind"] == "sell":
        return True
    quantity = _d(entry.get("quantity")) if entry["kind"] == "transfer" and entry.get("instrument_id") else None
    return quantity is not None and quantity < 0


def _day_order(entries: Sequence[Mapping[str, Any]]) -> Callable[[Mapping[str, Any]], tuple]:
    """Sort key: date, opening balances first, then intraday order.

    When every trade on a day carries ``executed_at`` the trades follow the
    execution time.  Otherwise, with no time to go by, acquisitions come before
    disposals on the same day (a same-day buy and sell is a day trade, not a
    sale of shares that were never recorded), and posting order breaks ties.
    """

    timed: dict[str, bool] = {}
    for entry in entries:
        if _is_trade(entry):
            timed[entry["date"]] = timed.get(entry["date"], True) and bool(entry.get("executed_at"))

    def key(entry: Mapping[str, Any]) -> tuple:
        opening = 0 if entry["kind"] == "opening_balance" else 1
        if timed.get(entry["date"]):
            return (entry["date"], opening, entry.get("executed_at") or "", 0, entry["seq"])
        return (entry["date"], opening, "", 1 if _disposes(entry) else 0, entry["seq"])

    return key


def match_transfers(entries: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Pair transfer legs between the person's own accounts.

    Explicit ``transfer_group`` pairs win; remaining legs are matched to the
    nearest opposite leg (same currency and amount, or same instrument and
    quantity) in a different account within ``TRANSFER_WINDOW_DAYS``.
    """

    legs = [e for e in entries if e["kind"] == "transfer"]
    pairs: list[dict[str, str]] = []
    matched: set[str] = set()
    warnings: list[str] = []
    groups: dict[str, list[Mapping[str, Any]]] = {}
    for leg in legs:
        if leg.get("transfer_group"):
            groups.setdefault(leg["transfer_group"], []).append(leg)
    for group, members in sorted(groups.items()):
        signs = sorted(_direction(m) for m in members)
        if len(members) == 2 and signs == [-1, 1]:
            out_leg, in_leg = sorted(members, key=_direction)
            pairs.append({"out": out_leg["id"], "in": in_leg["id"], "method": "transfer_group"})
            matched.update({out_leg["id"], in_leg["id"]})
        else:
            warnings.append(f"transfer_group {group} does not have exactly one outgoing and one incoming leg; left unmatched.")
    outs = sorted((l for l in legs if l["id"] not in matched and _direction(l) < 0), key=lambda e: (e["date"], e["seq"]))
    ins = [l for l in legs if l["id"] not in matched and _direction(l) > 0]
    for out_leg in outs:
        best = None
        for in_leg in ins:
            if in_leg["id"] in matched or in_leg["account_id"] == out_leg["account_id"]:
                continue
            if out_leg.get("counterparty_account_id") not in (None, in_leg["account_id"]):
                continue
            if in_leg.get("counterparty_account_id") not in (None, out_leg["account_id"]):
                continue
            if out_leg.get("instrument_id"):
                if in_leg.get("instrument_id") != out_leg["instrument_id"] or _d(in_leg["quantity"]) != -_d(out_leg["quantity"]):
                    continue
            elif in_leg.get("instrument_id") or in_leg.get("currency") != out_leg.get("currency") \
                    or _d(in_leg.get("amount")) != -_d(out_leg.get("amount")):
                continue
            gap = abs((date.fromisoformat(in_leg["date"]) - date.fromisoformat(out_leg["date"])).days)
            if gap > TRANSFER_WINDOW_DAYS:
                continue
            key = (gap, in_leg["date"], in_leg["seq"])
            if best is None or key < best[0]:
                best = (key, in_leg)
        if best is not None:
            pairs.append({"out": out_leg["id"], "in": best[1]["id"], "method": "matched"})
            matched.update({out_leg["id"], best[1]["id"]})
    unmatched = [l["id"] for l in legs if l["id"] not in matched]
    if unmatched:
        warnings.append(
            f"{len(unmatched)} transfer leg(s) have no counterpart in the ledger; they are treated as money "
            "entering or leaving the ledger, not as income or spending. Post the other account to match them."
        )
    return {"pairs": pairs, "unmatched": unmatched, "warnings": warnings}


def _direction(entry: Mapping[str, Any]) -> int:
    value = _d(entry.get("quantity")) if entry.get("instrument_id") else _d(entry.get("amount"))
    return -1 if value is not None and value < 0 else 1


# -- replay -----------------------------------------------------------------

@dataclass(frozen=True)
class Lot:
    id: str
    account_id: str
    instrument_id: str
    quantity: Decimal
    basis: Decimal | None       # total cost in ``currency``; None = unknown
    currency: str | None
    acquired_on: str | None     # None = unknown
    origin: str                 # entry id that created the lot
    order: int                  # tie-breaker preserving acquisition order


@dataclass
class State:
    cash: dict[tuple[str, str], Decimal]
    lots: dict[tuple[str, str], list[Lot]]
    realized: list[dict[str, Any]]
    income: list[dict[str, Any]]
    debt_service: list[dict[str, Any]]
    breaks: list[dict[str, Any]]
    missing: list[dict[str, Any]]
    in_transit: dict[str, list[Lot]]
    snapshots: dict[str, dict[str, Any]]
    # Matched cash transfers whose other leg has not posted yet: key -> (from, to, currency, amount).
    # The amount is positive while money has left one account and not reached the other, and
    # negative while it has reached the destination before leaving the source.
    cash_in_transit: dict[str, tuple[str, str, str | None, Decimal]] = field(default_factory=dict)
    # In-kind transfer (incoming leg id) -> destination account.
    transit_to: dict[str, str] = field(default_factory=dict)

    def transit(self) -> list[dict[str, Any]]:
        """Value in transit between own accounts, attributed to the source and destination."""
        rows = [{"from": source, "to": dest, "currency": ccy, "amount": amount}
                for source, dest, ccy, amount in self.cash_in_transit.values() if amount]
        for in_id, lots in self.in_transit.items():
            for lot in lots:
                rows.append({"from": lot.account_id, "to": self.transit_to.get(in_id), "instrument_id": lot.instrument_id,
                             "quantity": lot.quantity})
        return rows

    def quantity(self, account_id: str, instrument_id: str) -> Decimal:
        return sum((lot.quantity for lot in self.lots.get((account_id, instrument_id), [])), _ZERO)


def _holding(acquired: str | None, sold: str) -> str:
    return holding_character(acquired, sold)


def replay(ledger: Mapping[str, Any], as_of: str | None = None, *, lot_method: str = "fifo",
           include_inferred: bool = False, checkpoints: Sequence[str] = ()) -> tuple[State, dict[str, Any]]:
    """Replay active entries up to ``as_of`` (inclusive) and return the state.

    ``lot_method`` is ``fifo`` (sales without an explicit ``lot_selection``
    relieve the oldest lots) or ``specific`` (sales must name their lots;
    unnamed sales fall back to FIFO with a warning).  ``checkpoints`` are dates
    whose end-of-day cash and quantities are captured for valuation and
    balance reconciliation.
    """

    if lot_method not in {"fifo", "specific"}:
        raise ValueError("lot_method must be fifo or specific")
    entries, notes = active_entries(ledger, include_inferred=include_inferred)
    transfers = match_transfers(entries)
    pair_of = {}
    for pair in transfers["pairs"]:
        pair_of[pair["out"]] = ("out", pair)
        pair_of[pair["in"]] = ("in", pair)
    by_id = {e["id"]: e for e in entries}
    # An incoming in-kind leg is applied no earlier than its outgoing leg, so lots
    # are never in two accounts at once (they are "in transit" in between).
    position = {e["id"]: index for index, e in enumerate(entries)}  # date and intraday order

    def effective(entry: Mapping[str, Any]) -> tuple[str, int, int]:
        role = pair_of.get(entry["id"])
        if role and role[0] == "in" and entry.get("instrument_id"):
            other = by_id[role[1]["out"]]
            if other["date"] > entry["date"]:
                return (other["date"], 1, position[entry["id"]])
        return (entry["date"], 0, position[entry["id"]])

    ordered = sorted(entries, key=effective)
    state = State({}, {}, [], [], [], [], [], {}, {})
    meta = {"notes": notes + transfers["warnings"], "transfers": transfers, "entries": ordered,
            "used_sources": {}}
    points = sorted(set(checkpoints))
    point_index = 0
    split_applied: set[tuple[str, str, str]] = set()
    counter = 0

    def snapshot(day: str) -> None:
        state.snapshots[day] = {
            "cash": dict(state.cash),
            "quantities": {key: sum((l.quantity for l in lots), _ZERO) for key, lots in state.lots.items()},
            "lots": {key: list(lots) for key, lots in state.lots.items()},
            "transit": state.transit(),
        }

    def add_cash(account: str, ccy: str | None, amount: Decimal | None) -> None:
        if amount is None or ccy is None:
            return
        state.cash[(account, ccy)] = state.cash.get((account, ccy), _ZERO) + amount

    def add_lot(lot: Lot) -> None:
        state.lots.setdefault((lot.account_id, lot.instrument_id), []).append(lot)

    def relieve(entry: Mapping[str, Any], quantity: Decimal, selection: list | None) -> list[tuple[Lot, Decimal]]:
        key = (entry["account_id"], entry["instrument_id"])
        lots = state.lots.get(key, [])
        taken: list[tuple[Lot, Decimal]] = []
        remaining = quantity
        if selection:
            by_lot = {lot.id: lot for lot in lots}
            valid = all(item["lot_id"] in by_lot and Decimal(item["quantity"]) <= by_lot[item["lot_id"]].quantity for item in selection)
            if not valid or sum(Decimal(i["quantity"]) for i in selection) != quantity:
                state.breaks.append({"entry_id": entry["id"], "kind": "lot_selection",
                                     "detail": "lot_selection does not match open lots or the sale quantity; FIFO was used"})
                selection = None
            else:
                for item in selection:
                    taken.append((by_lot[item["lot_id"]], Decimal(item["quantity"])))
                remaining = _ZERO
        if not selection:
            if lot_method == "specific":
                meta["notes"].append(f"Sale {entry['id']} names no lots; FIFO relief was used.")
            for lot in sorted(lots, key=lambda l: (l.acquired_on or "", l.order)):
                if remaining <= 0:
                    break
                portion = min(lot.quantity, remaining)
                taken.append((lot, portion))
                remaining -= portion
        updated = {lot.id: lot for lot in lots}
        for lot, portion in taken:
            current = updated[lot.id]
            left = current.quantity - portion
            if left == 0:
                del updated[lot.id]
            else:
                fraction = left / current.quantity
                updated[lot.id] = replace(current, quantity=left,
                                          basis=None if current.basis is None else current.basis * fraction)
        state.lots[key] = [updated[lot.id] for lot in lots if lot.id in updated]
        if not state.lots[key]:
            del state.lots[key]
        if remaining > 0:
            state.breaks.append({"entry_id": entry["id"], "kind": "oversold",
                                 "detail": f"{out(remaining)} units of {entry['instrument_id']} left account "
                                           f"{entry['account_id']} with no recorded acquisition; basis unknown"})
            taken.append((Lot("unknown:" + entry["id"], entry["account_id"], entry["instrument_id"], remaining,
                              None, None, None, entry["id"], -1), remaining))
        return taken

    for entry in ordered:
        day = effective(entry)[0]
        if as_of is not None and day > as_of:
            break
        while point_index < len(points) and points[point_index] < day:
            snapshot(points[point_index])
            point_index += 1
        counter += 1
        source = entry.get("source") or {}
        ident = source.get("file_hash") or f"{source.get('kind')}:{source.get('ref')}"
        meta["used_sources"].setdefault(ident, {**source, "entries": 0})["entries"] += 1
        kind, account = entry["kind"], entry["account_id"]
        amount, ccy = _d(entry.get("amount")), entry.get("currency")
        instrument, quantity = entry.get("instrument_id"), _d(entry.get("quantity"))
        if kind in {"deposit", "withdrawal", "income", "expense", "fee"}:
            add_cash(account, ccy, amount)
        elif kind in {"dividend", "interest", "tax_withheld"}:
            add_cash(account, ccy, amount)
            state.income.append({"entry_id": entry["id"], "date": entry["date"], "kind": kind,
                                 "account_id": account, "instrument_id": instrument,
                                 "amount": amount, "currency": ccy, "subtype": entry.get("subtype")})
        elif kind == "loan_payment":
            add_cash(account, ccy, amount)
            principal, interest = _d(entry.get("principal")), _d(entry.get("interest"))
            state.debt_service.append({"entry_id": entry["id"], "date": entry["date"], "account_id": account,
                                       "amount": -amount, "principal": principal, "interest": interest,
                                       "currency": ccy, "liability": entry.get("counterparty_account_id") or entry.get("liability_id")})
            if entry.get("counterparty_account_id"):
                if principal is None:
                    state.missing.append({"key": f"entries[{entry['id']}].principal", "reason": "missing",
                                          "detail": "Loan balance not reduced: the payment's principal/interest split is unknown."})
                else:
                    add_cash(entry["counterparty_account_id"], ccy, principal)
        elif kind == "buy":
            add_cash(account, ccy, amount)
            add_lot(Lot(entry["id"], account, instrument, quantity, -amount, ccy, entry["date"], entry["id"], counter))
        elif kind == "sell":
            add_cash(account, ccy, amount)
            taken = relieve(entry, quantity, entry.get("lot_selection") if lot_method in {"fifo", "specific"} else None)
            for lot, portion in taken:
                share = portion / quantity
                proceeds = amount * share
                basis = None if lot.basis is None else lot.basis * portion / lot.quantity
                comparable = basis is not None and lot.currency == ccy
                state.realized.append({
                    "entry_id": entry["id"], "date": entry["date"], "account_id": account,
                    "instrument_id": instrument, "lot_id": lot.id, "quantity": portion,
                    "acquired_on": lot.acquired_on, "proceeds": proceeds, "proceeds_currency": ccy,
                    "basis": basis, "basis_currency": lot.currency,
                    "gain": proceeds - basis if comparable else None,
                    "holding": _holding(lot.acquired_on, entry["date"]),
                })
        elif kind == "transfer":
            if instrument is None:
                add_cash(account, ccy, amount)
                role = pair_of.get(entry["id"])
                if role and amount is not None:
                    pair = role[1]
                    key = f"{pair['out']}>{pair['in']}"
                    if key in state.cash_in_transit:
                        del state.cash_in_transit[key]
                    else:
                        state.cash_in_transit[key] = (by_id[pair["out"]]["account_id"], by_id[pair["in"]]["account_id"],
                                                      ccy, -amount)
            else:
                role = pair_of.get(entry["id"])
                add_cash(account, ccy, amount)
                if quantity < 0:
                    moved = relieve(entry, -quantity, None)
                    if role:
                        state.in_transit[role[1]["in"]] = [replace(l, quantity=q, basis=None if l.basis is None else l.basis * q / l.quantity) for l, q in moved]
                        state.transit_to[role[1]["in"]] = by_id[role[1]["in"]]["account_id"]
                else:
                    moving = state.in_transit.pop(entry["id"], None) if role else None
                    state.transit_to.pop(entry["id"], None)
                    if moving is not None:
                        for lot in moving:
                            add_lot(replace(lot, account_id=account))
                    else:
                        basis = _d(entry.get("cost_basis"))
                        if basis is None:
                            state.missing.append({"key": f"entries[{entry['id']}].cost_basis", "reason": "missing",
                                                  "detail": f"Basis of {out(quantity)} {instrument} transferred into {account} is unknown."})
                        add_lot(Lot(entry["id"], account, instrument, quantity, basis, ccy if basis is not None else None,
                                    entry.get("acquired_on"), entry["id"], counter))
        elif kind == "opening_balance":
            if instrument is None:
                add_cash(account, ccy, amount)
            else:
                basis = _d(entry.get("cost_basis"))
                if quantity > 0:
                    add_lot(Lot(entry["id"], account, instrument, quantity, basis,
                                ccy if basis is not None else None, entry.get("acquired_on"), entry["id"], counter))
                    if basis is None or entry.get("acquired_on") is None:
                        state.missing.append({"key": f"entries[{entry['id']}].cost_basis", "reason": "missing",
                                              "detail": f"Opening position {instrument} in {account} has unknown "
                                                        f"{'basis' if basis is None else 'acquisition date'}."})
        elif kind == "split":
            ratio = Decimal(entry["ratio"])
            key = (account, instrument)
            state.lots[key] = [replace(l, quantity=l.quantity * ratio) for l in state.lots.get(key, [])]
            if not state.lots[key]:
                del state.lots[key]
            # Shares moving between the person's accounts split too; the split may be posted by
            # either broker, and is applied once per transfer.
            for in_id, moving in state.in_transit.items():
                if (in_id, entry["date"], entry["ratio"]) in split_applied:
                    continue
                if any(l.instrument_id == instrument for l in moving) and account in {
                        state.transit_to.get(in_id), *(l.account_id for l in moving)}:
                    state.in_transit[in_id] = [replace(l, quantity=l.quantity * ratio) if l.instrument_id == instrument else l
                                               for l in moving]
                    split_applied.add((in_id, entry["date"], entry["ratio"]))
            add_cash(account, ccy, amount)
            if amount:
                meta["notes"].append(f"Cash in lieu on split {entry['id']} is recorded as cash; its gain is not computed.")
        elif kind in {"merger", "spin_off"}:
            ratio = Decimal(entry["ratio"])
            key = (account, instrument)
            parents = state.lots.get(key, [])
            if kind == "merger":
                state.lots.pop(key, None)
                for lot in parents:
                    add_lot(replace(lot, id=f"{lot.id}>{entry['new_instrument_id']}", instrument_id=entry["new_instrument_id"],
                                    quantity=lot.quantity * ratio))
                if amount:
                    meta["notes"].append(f"Cash received in merger {entry['id']} is recorded as cash; gain recognition on boot is not modeled.")
            else:
                allocation = _d(entry.get("basis_allocation"))
                if allocation is None and parents:
                    state.missing.append({"key": f"entries[{entry['id']}].basis_allocation", "reason": "missing",
                                          "detail": "Spin-off basis allocation is unknown, so parent and spun-off lot bases are unknown."})
                kept = []
                for lot in parents:
                    parent_basis = None if lot.basis is None or allocation is None else lot.basis * (1 - allocation)
                    child_basis = None if lot.basis is None or allocation is None else lot.basis * allocation
                    kept.append(replace(lot, basis=parent_basis))
                    add_lot(replace(lot, id=f"{lot.id}>{entry['new_instrument_id']}", instrument_id=entry["new_instrument_id"],
                                    quantity=lot.quantity * ratio, basis=child_basis))
                if parents:
                    state.lots[key] = kept
            add_cash(account, ccy, amount)
        elif kind == "fx_conversion":
            add_cash(account, ccy, amount)
            add_cash(account, entry["to_currency"], Decimal(entry["to_amount"]))
    while point_index < len(points) and (as_of is None or points[point_index] <= as_of):
        snapshot(points[point_index])
        point_index += 1
    if state.in_transit:
        meta["notes"].append(f"{len(state.in_transit)} in-kind transfer(s) are in transit at the as-of date.")
    if state.cash_in_transit:
        meta["notes"].append(f"{len(state.cash_in_transit)} cash transfer(s) between own accounts are in transit at the as-of date.")
    return state, meta


# -- public derivations -----------------------------------------------------

def _sources(meta: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [dict(value) for _, value in sorted(meta["used_sources"].items())]


def _lot_out(lot: Lot, fx: FxTable) -> dict[str, Any]:
    row = {
        "id": lot.id, "account_id": lot.account_id, "instrument_id": lot.instrument_id,
        "quantity": out(lot.quantity), "acquired_on": lot.acquired_on,
        "cost_basis": money(lot.basis), "currency": lot.currency,
    }
    for target in ("MXN", "USD"):
        converted = None
        if lot.basis is not None and lot.acquired_on is not None:
            converted = fx.convert(lot.basis, lot.currency, target, lot.acquired_on)
        row[f"cost_basis_{target.lower()}"] = money(converted)
    return row


def holdings(ledger: Mapping[str, Any], as_of: str, *, lot_method: str = "fifo",
             include_inferred: bool = False, fx_max_age_days: int = DEFAULT_FX_AGE_DAYS) -> dict[str, Any]:
    """Cash, positions, and tax lots per account at the end of ``as_of``.

    Lot basis is in the currency paid; ``cost_basis_mxn``/``cost_basis_usd``
    use the rate on the acquisition date and are ``None`` when that rate is
    not in the ledger.
    """

    state, meta = replay(ledger, as_of, lot_method=lot_method, include_inferred=include_inferred)
    fx = FxTable(ledger.get("fx", []), fx_max_age_days)
    instruments = {i["id"]: i for i in ledger.get("instruments", [])}
    missing = list(state.missing)
    lots_out, positions = [], []
    for (account, instrument), lots in sorted(state.lots.items()):
        quantity = sum((l.quantity for l in lots), _ZERO)
        rows = [_lot_out(lot, fx) for lot in lots]
        for row, lot in zip(rows, lots):
            if lot.basis is not None and lot.acquired_on is not None:
                for target in ("mxn", "usd"):
                    if row[f"cost_basis_{target}"] is None:
                        missing.append({"key": f"fx.{lot.currency}/{target.upper()}@{lot.acquired_on}", "reason": "missing",
                                        "detail": f"No {lot.currency}/{target.upper()} rate within {fx_max_age_days} days of {lot.acquired_on} for lot {lot.id}."})
        lots_out.extend(rows)
        positions.append({
            "account_id": account, "instrument_id": instrument,
            "symbol": instruments.get(instrument, {}).get("symbol", instrument),
            **_identity(instruments.get(instrument, {})),
            "quantity": out(quantity),
            "cost_basis": money(sum((l.basis for l in lots), _ZERO)) if all(l.basis is not None for l in lots) and len({l.currency for l in lots}) == 1 else None,
            "basis_currency": lots[0].currency if len({l.currency for l in lots}) == 1 else None,
            "basis_complete": all(l.basis is not None and l.acquired_on is not None for l in lots),
            "lot_ids": [l.id for l in lots],
        })
    cash = [{"account_id": a, "currency": c, "balance": money(v)} for (a, c), v in sorted(state.cash.items())]
    warnings = list(meta["notes"])
    for lot in (l for lots in state.lots.values() for l in lots):
        if instruments.get(lot.instrument_id, {}).get("venue") == "sic" and lot.currency not in (None, "MXN"):
            warnings.append(f"SIC lot {lot.id} has {lot.currency} basis; SIC basis should be the MXN amount paid.")
    warnings.extend(b["detail"] for b in state.breaks)
    missing = _unique(missing)
    return envelope(status_for(missing, [b for b in state.breaks]), {
        "as_of": as_of, "cash": cash, "positions": positions, "lots": lots_out,
        "breaks": state.breaks, "transfers": meta["transfers"]["pairs"],
        "unmatched_transfers": meta["transfers"]["unmatched"],
    }, missing=missing, warnings=warnings, sources=_sources(meta), assumptions=[
        f"Lots are relieved {'FIFO' if lot_method == 'fifo' else 'by specific identification (FIFO where a sale names no lots)'}; fees on buys are part of basis and fees on sells reduce proceeds.",
        f"MXN/USD basis uses the latest ledger rate on or up to {fx_max_age_days} days before the acquisition date; no cross rate is inferred.",
    ])


def _identity(instrument: Mapping[str, Any]) -> dict[str, Any]:
    """Economic identity (underlying) and legal identity (venue) of a holding."""

    return {"underlying_symbol": instrument.get("underlying_symbol", instrument.get("symbol")),
            "venue": instrument.get("venue"), "issuer_domicile": instrument.get("issuer_domicile"),
            "listing_currency": instrument.get("listing_currency", instrument.get("currency"))}


def _unique(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen, result = set(), []
    for item in items:
        if item["key"] not in seen:
            seen.add(item["key"])
            result.append(item)
    return result


PriceProvider = Callable[[str, str], "Decimal | None"]


def price_table(series: Mapping[str, Any], max_age_days: int = 5) -> PriceProvider:
    """Build a price provider from ``{instrument_id: [{date, price}] | {date: price}}``.

    The provider returns the latest price on or before the date within
    ``max_age_days`` (weekends, holidays) and ``None`` otherwise.
    """

    table: dict[str, tuple[list[str], list[Decimal]]] = {}
    for instrument, rows in series.items():
        points = rows.items() if isinstance(rows, Mapping) else ((r["date"], r.get("price", r.get("close", r.get("value")))) for r in rows)
        values = {}
        for day, price in points:
            if price is None:
                continue
            value = Decimal(str(price))
            if not value.is_finite() or value <= 0:
                raise ValueError(f"prices for {instrument} must be positive finite numbers")
            values[day] = value
        days = sorted(values)
        table[instrument] = (days, [values[d] for d in days])

    def provider(instrument: str, on: str) -> Decimal | None:
        found = table.get(instrument)
        if not found:
            return None
        days, prices = found
        index = bisect_right(days, on) - 1
        if index < 0 or (date.fromisoformat(on) - date.fromisoformat(days[index])).days > max_age_days:
            return None
        return prices[index]

    return provider


def to_household(ledger: Mapping[str, Any], as_of: str, currency: str, prices: PriceProvider, *,
                 default_owner_id: str | None = None, complete: bool = False, lot_method: str = "fifo",
                 include_inferred: bool = False, fx_max_age_days: int = DEFAULT_FX_AGE_DAYS) -> dict[str, Any]:
    """Emit the canonical household document consumed by household.py and tax.py.

    Positions without a price and accounts without an owner are left out and
    listed as missing; lots are included only for positions whose every lot
    has a known basis and acquisition date (tax.py cannot use partial lots).
    """

    from ..household import validate_household

    held = holdings(ledger, as_of, lot_method=lot_method, include_inferred=include_inferred,
                    fx_max_age_days=fx_max_age_days)
    fx = FxTable(ledger.get("fx", []), fx_max_age_days)
    instruments = {i["id"]: i for i in ledger.get("instruments", [])}
    missing = list(held["missing"])
    warnings = list(held["warnings"])
    people, accounts, excluded_accounts = {}, [], set()
    for account in ledger.get("accounts", []):
        owners = account.get("owners") or ([{"person_id": default_owner_id, "share": "1"}] if default_owner_id else None)
        if not owners:
            excluded_accounts.add(account["id"])
            missing.append({"key": f"accounts[{account['id']}].owners", "reason": "missing",
                            "detail": "Account owner is unknown; the account is left out of the household."})
            continue
        primary = sorted(owners, key=lambda o: (-Decimal(o["share"]), o["person_id"]))[0]
        for owner in owners:
            people.setdefault(owner["person_id"], {"id": owner["person_id"]})
        row = {"id": account["id"], "owner_id": primary["person_id"], "type": account["type"],
               "currency": account["currency"], "institution": account["institution"]}
        if len(owners) > 1:
            row["owner_shares"] = owners
        for name in ("name", "tax_unit", "liquid", "restricted"):
            if name in account:
                row[name] = account[name]
        accounts.append(row)
    account_types = {a["id"]: a["type"] for a in ledger.get("accounts", [])}
    positions, liabilities, lots, priced = [], [], [], set()
    for position in held["result"]["positions"]:
        account, instrument = position["account_id"], position["instrument_id"]
        if account in excluded_accounts:
            continue
        meta = instruments.get(instrument, {})
        price = prices(instrument, as_of)
        if price is None:
            missing.append({"key": f"prices.{instrument}@{as_of}", "reason": "missing",
                            "detail": f"No current price for {instrument}; the position is left out of the household."})
            continue
        row = {"id": f"{account}:{instrument}", "account_id": account, "instrument_id": instrument,
               "symbol": meta.get("symbol", instrument), "quantity": position["quantity"],
               "value": money(Decimal(position["quantity"]) * price), "currency": meta.get("currency"),
               "asset_class": meta.get("asset_class", "unknown"),
               **{k: v for k, v in _identity(meta).items() if v is not None}}
        for name in ("issuer", "sector", "country", "economic_currency"):
            if name in meta:
                row[name] = meta[name]
        positions.append(row)
        priced.add((account, instrument))
        if not position["basis_complete"]:
            warnings.append(f"Lots for {instrument} in {account} are omitted because some basis or acquisition dates are unknown.")
    for lot in held["result"]["lots"]:
        key = (lot["account_id"], lot["instrument_id"])
        position = next(p for p in held["result"]["positions"] if (p["account_id"], p["instrument_id"]) == key)
        if key in priced and position["basis_complete"]:
            lots.append(lot)
    for balance in held["result"]["cash"]:
        account, ccy, value = balance["account_id"], balance["currency"], Decimal(balance["balance"])
        if account in excluded_accounts or value == 0:
            continue
        if value > 0:
            positions.append({"id": f"{account}:cash:{ccy}", "account_id": account, "instrument_id": f"cash:{ccy}",
                              "symbol": "CASH", "quantity": money(value), "value": money(value), "currency": ccy,
                              "asset_class": "cash"})
        else:
            liabilities.append({"id": f"{account}:{ccy}", "account_id": account, "value": money(-value), "currency": ccy,
                                "type": account_types.get(account, "unknown")})
            if account_types.get(account) not in LIABILITY_TYPES:
                warnings.append(f"Account {account} has a negative {ccy} balance; shown as a liability.")
    household = {
        "currency": currency, "as_of": as_of, "complete": complete,
        "people": [people[p] for p in sorted(people)], "accounts": accounts, "positions": positions,
        "lots": lots, "liabilities": liabilities, "fx": [dict(r, source="ledger") for r in fx.latest(as_of)],
        "unknown_sections": ["external_assets", "income_exposures", "fund_holdings"],
        "external_assets": [], "income_exposures": [], "fund_holdings": [],
    }
    # Keep only one direction per pair (the household rejects duplicates).
    seen_pairs, fx_rows = set(), []
    for row in household["fx"]:
        pair = frozenset((row["from"], row["to"]))
        if pair not in seen_pairs:
            seen_pairs.add(pair)
            fx_rows.append(row)
    household["fx"] = fx_rows
    checked = validate_household(household)
    missing = _unique(missing)
    return envelope(status_for(missing, warnings), {"household": checked["household"]}, missing=missing,
                    warnings=warnings + checked["warnings"], sources=held["sources"], assumptions=held["assumptions"] + [
                        "Position values use the supplied price on or shortly before the as-of date; cash is shown as CASH positions and negative balances as liabilities.",
                        "Joint accounts list the largest owner as owner_id and every share in owner_shares.",
                    ])


def reconcile(ledger: Mapping[str, Any], *, as_of: str | None = None, tolerance: str = "0.01",
              include_inferred: bool = False) -> dict[str, Any]:
    """Compare statement balance assertions with the ledger and report breaks."""

    assertions = [a for a in ledger.get("assertions", []) if as_of is None or a["date"] <= as_of]
    state, meta = replay(ledger, as_of, include_inferred=include_inferred,
                         checkpoints=[a["date"] for a in assertions])
    limit = Decimal(tolerance)
    checks, breaks = [], []
    for assertion in sorted(assertions, key=lambda a: (a["date"], a["account_id"], a["id"])):
        shot = state.snapshots.get(assertion["date"], {"cash": {}, "quantities": {}})
        if assertion.get("instrument_id"):
            derived = shot["quantities"].get((assertion["account_id"], assertion["instrument_id"]), _ZERO)
            expected = Decimal(assertion["quantity"])
            tolerance_used = _ZERO
        else:
            derived = shot["cash"].get((assertion["account_id"], assertion["currency"]), _ZERO)
            expected = Decimal(assertion["balance"])
            tolerance_used = limit
        difference = derived - expected
        row = {"assertion_id": assertion["id"], "account_id": assertion["account_id"], "date": assertion["date"],
               "currency": assertion.get("currency"), "instrument_id": assertion.get("instrument_id"),
               "expected": out(expected), "derived": out(derived), "difference": out(difference),
               "ok": abs(difference) <= tolerance_used, "source": assertion.get("source")}
        checks.append(row)
        if not row["ok"]:
            breaks.append(row)
    warnings = list(meta["notes"])
    warnings.extend(b["detail"] for b in state.breaks)
    for row in breaks:
        subject = row["instrument_id"] or row["currency"]
        warnings.append(f"Balance break in {row['account_id']} {subject} on {row['date']}: statement {row['expected']}, "
                        f"ledger {row['derived']} (difference {row['difference']}). Look for missing or duplicated lines.")
    return envelope("partial" if breaks or state.breaks else "ready", {
        "checks": checks, "breaks": breaks, "ledger_breaks": state.breaks,
        "transfers": meta["transfers"]["pairs"], "unmatched_transfers": meta["transfers"]["unmatched"],
    }, warnings=warnings, sources=_sources(meta), assumptions=[
        f"A cash assertion passes within {tolerance}; an instrument quantity must match exactly.",
        "Assertions compare the ledger at the end of the asserted date.",
    ])


def realized_gains(ledger: Mapping[str, Any], *, year: int | None = None, as_of: str | None = None,
                   lot_method: str = "fifo", include_inferred: bool = False,
                   fx_max_age_days: int = DEFAULT_FX_AGE_DAYS) -> dict[str, Any]:
    """Realized gains by year, in the traded currency and in MXN/USD at trade-date FX.

    Proceeds convert at the sale date and basis at the acquisition date.  A
    missing rate leaves that conversion unknown rather than zero.
    """

    state, meta = replay(ledger, as_of, lot_method=lot_method, include_inferred=include_inferred)
    fx = FxTable(ledger.get("fx", []), fx_max_age_days)
    instruments = {i["id"]: i for i in ledger.get("instruments", [])}
    rows, missing = [], []
    totals: dict[str, dict[str, Any]] = {}
    for item in state.realized:
        item_year = int(item["date"][:4])
        if year is not None and item_year != year:
            continue
        row = {k: (out(v) if isinstance(v, Decimal) else v) for k, v in item.items()}
        row["proceeds"], row["basis"], row["gain"] = money(item["proceeds"]), money(item["basis"]), money(item["gain"])
        row.update(_identity(instruments.get(item["instrument_id"], {})))
        venue = row["venue"] or "unknown"
        bucket = totals.setdefault(str(item_year), {"by_currency": {}, "MXN": _ZERO, "USD": _ZERO,
                                                     "MXN_complete": True, "USD_complete": True, "unknown_basis_lots": 0,
                                                     "by_venue": {}})
        venue_bucket = bucket["by_venue"].setdefault(venue, {"MXN": _ZERO, "USD": _ZERO, "MXN_complete": True, "USD_complete": True})
        if item["gain"] is not None:
            key = f"{item['proceeds_currency']}:{item['holding']}"
            bucket["by_currency"][key] = bucket["by_currency"].get(key, _ZERO) + item["gain"]
        if item["basis"] is None:
            bucket["unknown_basis_lots"] += 1
            missing.append({"key": f"lots[{item['lot_id']}].cost_basis", "reason": "missing",
                            "detail": f"Sale {item['entry_id']} relieved lot {item['lot_id']} with unknown basis; its gain is unknown."})
        for target in ("MXN", "USD"):
            proceeds = fx.convert(item["proceeds"], item["proceeds_currency"], target, item["date"])
            basis = None if item["basis"] is None or item["acquired_on"] is None else fx.convert(item["basis"], item["basis_currency"], target, item["acquired_on"])
            gain = None if proceeds is None or basis is None else proceeds - basis
            row[f"gain_{target.lower()}"] = money(gain)
            if gain is None:
                bucket[f"{target}_complete"] = False
                venue_bucket[f"{target}_complete"] = False
                if item["basis"] is not None:
                    missing.append({"key": f"fx.{target}@{item['entry_id']}", "reason": "missing",
                                    "detail": f"No {target} rate for sale {item['entry_id']} or its lot's acquisition date."})
            else:
                bucket[target] += gain
                venue_bucket[target] += gain
        rows.append(row)
    years = {
        y: {"gain_by_currency_and_holding": {k: money(v) for k, v in sorted(b["by_currency"].items())},
            "gain_mxn": money(b["MXN"]) if b["MXN_complete"] else None,
            "gain_usd": money(b["USD"]) if b["USD_complete"] else None,
            "unknown_basis_lots": b["unknown_basis_lots"],
            "by_venue": {v: {"gain_mxn": money(vb["MXN"]) if vb["MXN_complete"] else None,
                             "gain_usd": money(vb["USD"]) if vb["USD_complete"] else None}
                         for v, vb in sorted(b["by_venue"].items())}}
        for y, b in sorted(totals.items())
    }
    missing = _unique(missing)
    return envelope(status_for(missing, []), {"years": years, "sales": rows}, missing=missing,
                    warnings=meta["notes"] + [b["detail"] for b in state.breaks], sources=_sources(meta), assumptions=[
                        "Nominal gains only: Mexican Article 129 average-cost and INPC-updated basis and US wash-sale adjustments are not applied here; use the tax task with its explicit inputs.",
                        "A year total in MXN or USD is null when any sale in that year lacks a rate.",
                    ])


def investment_income(ledger: Mapping[str, Any], *, year: int | None = None, as_of: str | None = None,
                      include_inferred: bool = False, fx_max_age_days: int = DEFAULT_FX_AGE_DAYS) -> dict[str, Any]:
    """Dividends, interest, and tax withheld per year, per currency and in MXN/USD."""

    state, meta = replay(ledger, as_of, include_inferred=include_inferred)
    fx = FxTable(ledger.get("fx", []), fx_max_age_days)
    years: dict[str, dict[str, Any]] = {}
    missing = []
    for item in state.income:
        item_year = item["date"][:4]
        if year is not None and int(item_year) != year:
            continue
        bucket = years.setdefault(item_year, {"native": {}, "MXN": {}, "USD": {}, "complete": {"MXN": True, "USD": True}})
        native = bucket["native"].setdefault(item["kind"], {})
        native[item["currency"]] = native.get(item["currency"], _ZERO) + item["amount"]
        for target in ("MXN", "USD"):
            converted = fx.convert(item["amount"], item["currency"], target, item["date"])
            if converted is None:
                bucket["complete"][target] = False
                missing.append({"key": f"fx.{item['currency']}/{target}@{item['date']}", "reason": "missing",
                                "detail": f"No {item['currency']}/{target} rate near {item['date']} for {item['kind']} {item['entry_id']}."})
            else:
                bucket[target][item["kind"]] = bucket[target].get(item["kind"], _ZERO) + converted
    result = {}
    for y, bucket in sorted(years.items()):
        result[y] = {"native": {k: {c: money(v) for c, v in sorted(cs.items())} for k, cs in sorted(bucket["native"].items())}}
        for target in ("MXN", "USD"):
            result[y][target.lower()] = ({k: money(v) for k, v in sorted(bucket[target].items())}
                                         if bucket["complete"][target] else None)
    missing = _unique(missing)
    return envelope(status_for(missing, []), {"years": result}, missing=missing, warnings=meta["notes"],
                    sources=_sources(meta), assumptions=[
                        "Amounts are nominal as posted; Mexican interés real (inflation-adjusted interest) needs INPC data and is not computed here.",
                        "tax_withheld is negative and shown separately from gross dividends and interest.",
                    ])
