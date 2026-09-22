"""The common ingestion proposal: normalise, reconcile, confirm, diff, merge.

Every path (PDF heuristics, validated LLM extraction, CSV/XLSX exports, chat)
produces an intermediate *statement* and calls :func:`build_proposal`.  A
proposal is never saved automatically.  :func:`proposal_to_facts` turns an
explicitly confirmed proposal into ``WealthStore.remember`` payloads without
touching the store.

Statement shape (numbers are printed strings or decimals)::

    {"institution", "as_of", "currency", "decimal_comma",
     "accounts": [{"label", "number_last4", "type", "currency", "page",
                   "positions": [{"symbol", "description", "quantity", "price",
                                  "market_value", "cost_basis", "avg_cost", "gain",
                                  "currency", "acquired_on", "page", "section"}],
                   "cash": [{"amount", "currency", "label", "page"}],
                   "reported_total": {"amount", "currency", "label", "page"} | None,
                   "positions_subtotal": {...} | None,
                   "flows": {"opening", "deposits", "withdrawals", "closing", "page"} | None,
                   "liabilities": [{"label", "balance", "currency", "minimum_payment",
                                    "interest_rate", "page"}]}],
     "fx": [{"from", "to", "rate", "page"}],
     "liabilities": [...],                     # debts not tied to an account (chat)
     "income": [{"label", "annual_amount", "currency"}],
     "market": "mx" | "us" | None, "period_start": date | None}

Accounts may also carry ``transactions`` (see :mod:`wealth.ingest.transactions`),
``debt_flows`` (card/mortgage opening and closing debt), ``period_start``/``period_end``,
``interest_rate`` and ``single_value`` (one stated balance, e.g. from chat).
Position rows may carry ``currency_explicit`` so a SIC listing keeps MXN unless
the row itself prints another currency.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import re
from typing import Any, Iterable, TypedDict

from .classify import CASH_LABEL, classify_instrument, listing
from .common import (decimals, digest, envelope, fold as fold_text, out, parse_amount, parse_percent, slug,
                     with_institution)
from .redact import mask_account, redact, redact_text
from .safety import INSTRUCTION_REASON, flag_instructions, mark_untrusted, text_leaves
from .transactions import normalize as normalize_transactions


SOURCE_KINDS = ("document", "user", "connector")
_ENTITIES = ("accounts", "positions", "lots", "liabilities", "income_exposures")
_CURRENCY = re.compile(r"[A-Z]{3}")


class IngestProposal(TypedDict):
    """Envelope returned by every ingestion path (see module docstring)."""

    status: str
    result: dict[str, Any]
    missing: list[Any]
    warnings: list[str]
    sources: list[str]
    assumptions: list[str]


def _today() -> date:
    return datetime.now(timezone.utc).date()


def _amount(value: Any, comma: bool | None) -> Decimal | None:
    if isinstance(value, dict):
        value = value.get("amount")
    return parse_amount(value, decimal_comma=comma)


def _ccy(value: Any) -> str | None:
    text = str(value or "").strip().upper()
    aliases = {"MN": "MXN", "M.N.": "MXN", "PESOS": "MXN", "DLS": "USD", "DLLS": "USD", "DOLARES": "USD",
               "DÓLARES": "USD", "US$": "USD", "UDIS": "UDI"}
    text = aliases.get(text, text)
    return text if _CURRENCY.fullmatch(text) else None


class _Rates:
    def __init__(self, fx: list[dict[str, Any]]):
        self.rates: dict[tuple[str, str], Decimal] = {}
        for item in fx:
            rate = parse_amount(item.get("rate"))
            if rate and rate > 0:
                self.rates[(item["from"], item["to"])] = rate
                self.rates.setdefault((item["to"], item["from"]), Decimal(1) / rate)

    def convert(self, value: Decimal, source: str, target: str) -> Decimal | None:
        if source == target:
            return value
        rate = self.rates.get((source, target))
        return None if rate is None else value * rate


_ASSET_WORDS = (
    ("cash", r"\b(cash|money market|efectivo|mercado de dinero)\b"),
    ("fund", r"\b(etfs?|funds?|fondos?|mutual|closed end|sociedades de inversion)\b"),
    ("fixed_income", r"\b(bonds?|fixed income|renta fija|deuda|treasur(y|ies)|notes?)\b"),
    ("equity", r"\b(equit(y|ies)|stocks?|acciones|renta variable|common)\b"),
    ("derivative", r"\b(options?|futures?|derivad[oa]s?|warrants?)\b"),
)


def _asset_class(printed: Any) -> str | None:
    text = fold_text(printed)
    return next((name for name, pattern in _ASSET_WORDS if re.search(pattern, text)), None)


def _unique(identifier: str, used: set[str]) -> str:
    candidate, counter = identifier, 2
    while candidate in used:
        candidate, counter = f"{identifier}-{counter}", counter + 1
    used.add(candidate)
    return candidate


def build_proposal(
    statement: dict[str, Any],
    *,
    kind: str,
    provenance: dict[str, Any],
    owner_id: str = "self",
    confidence: dict[str, str] | None = None,
    verification: dict[str, Any] | None = None,
    warnings: Iterable[str] = (),
    missing: Iterable[Any] = (),
    assumptions: Iterable[str] = (),
    review_reasons: Iterable[str] = (),
    transactions: list[dict[str, Any]] | None = None,
    tolerance: Any = None,
) -> IngestProposal:
    """Normalise an intermediate statement into a reconciled household proposal."""
    if kind not in SOURCE_KINDS:
        raise ValueError(f"kind must be one of {SOURCE_KINDS}")
    comma = statement.get("decimal_comma")
    warnings, missing, assumptions = list(warnings), list(missing), list(assumptions)
    reasons = list(review_reasons) + [str(r) for r in statement.get("review_reasons") or []]
    # Text in the file that addresses an assistant is a risk flag the person must see: never ready_to_confirm.
    if flag_instructions(provenance, [*text_leaves(statement), *text_leaves(transactions or [])]):
        reasons.append(INSTRUCTION_REASON)
    field_confidence = dict(confidence or {})
    institution_key = slug(statement.get("institution_key") or statement.get("institution") or "statement", 16)
    as_of = statement.get("as_of")
    currency = _ccy(statement.get("currency"))
    accounts_in = statement.get("accounts") or []
    if currency is None:
        currency = next((c for c in (_ccy(a.get("currency")) for a in accounts_in) if c), None)
    market = statement.get("market") or ("mx" if currency == "MXN" or any(_ccy(a.get("currency")) == "MXN" for a in accounts_in) else None)
    if currency is None:
        missing.append({"key": "currency", "reason": "missing", "detail": "Statement currency was not found; confirm it."})
        reasons.append("Statement currency is unknown.")
    if not as_of:
        missing.append({"key": "as_of", "reason": "missing", "detail": "Statement date was not found; confirm the as-of date."})
        reasons.append("Statement as-of date is unknown.")
    elif date.fromisoformat(as_of) > _today():
        reasons.append(f"Statement date {as_of} is in the future.")
    for field in ("as_of", "currency"):
        if field_confidence.get(field) == "low" and statement.get(field):
            reasons.append(f"Statement {field.replace('_', '-')} is ambiguous; confirm it.")

    fragment: dict[str, Any] = {
        "currency": currency, "as_of": as_of, "complete": False,
        "people": [{"id": owner_id}], "accounts": [], "positions": [], "lots": [], "liabilities": [],
        "external_assets": [], "income_exposures": [], "fx": [], "fund_holdings": [],
    }
    for item in statement.get("fx") or []:
        source, target, rate = _ccy(item.get("from")), _ccy(item.get("to")), parse_amount(item.get("rate"))
        if source and target and source != target and rate and rate > 0 and as_of:
            fragment["fx"].append({"from": source, "to": target, "rate": out(rate), "as_of": as_of,
                                   "source": f"statement page {item.get('page')}" if item.get("page") else "statement"})
    rates = _Rates(fragment["fx"])
    used_ids: set[str] = set()
    recon_accounts: list[dict[str, Any]] = []
    differences: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []
    all_transactions: list[dict[str, Any]] = list(transactions or [])
    assertions: list[dict[str, Any]] = []

    for index, raw in enumerate(accounts_in):
        tail = raw.get("number_last4")
        tail = tail if isinstance(tail, str) and re.fullmatch(r"\d{4}", tail) else None
        label = redact_text(str(raw.get("label") or "")).strip() or None
        # An institution the person named with the account ("Checking" at BBVA) is part of its identity.
        named_at = redact_text(str(raw.get("institution") or "")).strip() or None
        ident = with_institution(label, named_at) or None
        base = f"{institution_key}-{tail}" if tail else f"{institution_key}-{slug(ident, 32)}" if ident else f"{institution_key}-{index + 1}"
        account_id = _unique(base, used_ids)
        account_currency = _ccy(raw.get("currency")) or currency
        if account_currency is None:
            reasons.append(f"Account {account_id} currency is unknown.")
            account_currency = "XXX"
        account_type = raw.get("type") or "unspecified"
        if account_type == "unspecified":
            warnings.append(f"Account {account_id} type was not stated; it is treated as not liquid until confirmed.")
        account = {"id": account_id, "owner_id": owner_id, "type": account_type, "currency": account_currency}
        if label:
            account["name"] = label
        if named_at or statement.get("institution"):
            account["institution"] = named_at or statement["institution"]
        if tail:
            account["number_masked"] = mask_account(tail)
        if raw.get("interest_rate") not in (None, ""):
            rate = parse_percent(raw["interest_rate"])
            if rate is not None:
                account["interest_rate"] = out(rate)
        if raw.get("restricted") is True:
            account["restricted"] = True
        fragment["accounts"].append(account)
        page_ref = raw.get("page")
        rows_seen = 0
        items: list[tuple[Decimal, str, str]] = []
        positions_value = Decimal(0)
        cash_total: dict[str, Decimal] = {}
        merged: dict[str, dict[str, Any]] = {}
        lots_by_instrument: dict[str, list[dict[str, Any]]] = {}
        row_values: list[Decimal | None] = []  # each printed row in the account currency, for section subtotals

        for row in raw.get("positions") or []:
            rows_seen += 1
            row_values.append(None)
            row_ccy = _ccy(row.get("currency")) or account_currency
            qty = parse_amount(row.get("quantity"), decimal_comma=comma)
            price = parse_amount(row.get("price"), decimal_comma=comma)
            value = parse_amount(row.get("market_value"), decimal_comma=comma)
            cost = parse_amount(row.get("cost_basis"), decimal_comma=comma)
            avg = parse_amount(row.get("avg_cost"), decimal_comma=comma)
            gain = parse_amount(row.get("gain"), decimal_comma=comma)
            printed = " ".join(str(row.get("symbol") or "").split()) or None
            symbol = re.sub(r"\s*\*+$", "", printed or "") or None
            description = str(row.get("description") or "").strip() or None
            if description and parse_amount(description) is None and fold_text(description) in ("", "na", "n a"):
                description = None
            if description and set(description) <= set("-–—*. "):
                description = None
            label_text = printed or description or f"row {rows_seen}"
            section = str(row.get("section") or "")
            meta = classify_instrument(printed, description, section)
            if meta.get("asset_class") != "cash" and meta.get("issuer") != "Gobierno Federal (MX)" and printed and (
                    market == "mx" or "sic" in section.lower() or "mercado global" in section.lower()):
                for key, fact in listing(printed, description, section, market=market).items():
                    meta.setdefault(key, fact)
            if row.get("asset_type") and "asset_class" not in meta:
                printed_class = _asset_class(row["asset_type"])
                if printed_class:
                    meta["asset_class"] = printed_class
            if meta.get("venue") == "sic":
                if not row.get("currency_explicit"):
                    row_ccy = "MXN"
                elif row_ccy != "MXN":
                    warnings.append(f"{printed} is a SIC listing but the statement prints it in {row_ccy}; kept as printed.")
            where = f"{account_id} {label_text}" + (f" (page {row.get('page')})" if row.get("page") else "")
            value_confidence = "high"
            if value is None and qty is not None and price is not None:
                value, value_confidence = qty * price, "medium"
                assumptions.append(f"{where}: value derived as quantity x price.")
            if qty is not None and price is not None and value is not None and value_confidence == "high":
                places = decimals(str(row.get("price")))
                bound = abs(qty) * Decimal(5) * Decimal(10) ** -(places + 1) + Decimal("0.02")
                product = qty * price
                if abs(product - value) > bound and meta.get("denomination") == "UDI":
                    in_udi = rates.convert(product, "UDI", row_ccy)
                    if in_udi is not None and abs(in_udi - value) <= bound * (rates.convert(Decimal(1), "UDI", row_ccy) or 1):
                        product = value
                        assumptions.append(f"{where}: price is per UDI and was converted with the statement UDI value.")
                if abs(product - value) > bound:
                    differences.append({"account_id": account_id, "check": "row_arithmetic", "item": label_text,
                                        "expected": out(value), "computed": out(qty * price),
                                        "difference": out(qty * price - value), "page": row.get("page"),
                                        "detail": "quantity x price does not match market value"})
            if cost is None and avg is not None and qty is not None:
                cost = avg * qty
                field_confidence[f"positions.{account_id}:{label_text}.cost_basis"] = "medium"
            if gain is not None and cost is not None and value is not None and abs(value - cost - gain) > Decimal("0.05"):
                differences.append({"account_id": account_id, "check": "row_gain", "item": label_text,
                                    "expected": out(gain), "computed": out(value - cost),
                                    "difference": out(value - cost - gain), "page": row.get("page"),
                                    "detail": "market value minus cost does not match printed gain/loss"})
            if value is not None:
                row_values[-1] = rates.convert(value, row_ccy, account_currency)
            if value is None:
                unresolved.append({"account_id": account_id, "item": label_text, "reason": "no market value or price",
                                   "page": row.get("page")})
                missing.append({"key": f"positions.{account_id}.{label_text}.value", "reason": "missing",
                                "detail": f"{label_text} has no readable value."})
                reasons.append(f"{label_text} in {account_id} has no readable value.")
                continue
            items.append((value, row_ccy, label_text))
            if meta.get("asset_class") == "cash":
                cash_total[row_ccy] = cash_total.get(row_ccy, Decimal(0)) + value
                if qty is None:
                    qty = value
            else:
                positions_value += rates.convert(value, row_ccy, account_currency) or Decimal(0)
            if qty is None:
                unresolved.append({"account_id": account_id, "item": label_text, "value": out(value),
                                   "currency": row_ccy, "reason": "quantity not printed", "page": row.get("page")})
                reasons.append(f"{label_text} in {account_id} has a value but no quantity; it cannot enter the household until confirmed.")
                continue
            if qty < 0 or value < 0:
                unresolved.append({"account_id": account_id, "item": label_text, "value": out(value),
                                   "currency": row_ccy, "reason": "short or negative position", "page": row.get("page")})
                reasons.append(f"{label_text} in {account_id} is short or negative; confirm how to record it.")
                continue
            instrument = (symbol or ("NAME:" + description[:48] if description else f"ROW:{rows_seen}")).upper()
            instrument = re.sub(r"\s+", " ", instrument)
            if meta.get("venue") == "sic":
                instrument, symbol = f"SIC:{meta['underlying_symbol']}", meta["underlying_symbol"]
            elif meta.get("venue") == "bmv":
                instrument = f"BMV:{instrument}"
            if meta.get("asset_class") == "cash" and (not symbol or CASH_LABEL.match(symbol)):
                instrument, description = f"CASH:{row_ccy}", description or symbol
                symbol = row_ccy
            key = f"{instrument}|{row_ccy}"
            position = merged.get(key)
            if position is None:
                position = {"id": "", "account_id": account_id, "instrument_id": instrument,
                            "symbol": symbol or (description or instrument)[:48], "quantity": Decimal(0),
                            "value": Decimal(0), "currency": row_ccy}
                if description and description != symbol:
                    position["name"] = description[:120]
                if price is not None:
                    position["price"] = out(price)
                if printed and printed != position["symbol"] and meta.get("venue"):
                    position["listing_symbol"] = printed
                for field in ("asset_class", "instrument_type", "issuer", "country", "maturity", "denomination",
                              "venue", "listing_currency", "underlying_symbol", "issuer_domicile", "series"):
                    if meta.get(field):
                        position[field] = meta[field]
                if meta.get("liquid") is False:
                    position["liquid"] = False
                if page_ref or row.get("page"):
                    position["page"] = row.get("page") or page_ref
                merged[key] = position
                if value_confidence != "high":
                    field_confidence[f"positions.{account_id}:{instrument}.value"] = value_confidence
            position["quantity"] += qty
            position["value"] += value
            if cost is not None:
                position["cost_basis"] = position.get("cost_basis", Decimal(0)) + cost
            elif "cost_basis" in position:
                position["cost_basis_partial"] = True
            acquired = row.get("acquired_on")
            lots_by_instrument.setdefault(key, []).append(
                {"quantity": qty, "cost_basis": cost, "acquired_on": acquired if isinstance(acquired, str) else None}
            )

        for cash in raw.get("cash") or []:
            amount = parse_amount(cash.get("amount"), decimal_comma=comma)
            cash_ccy = _ccy(cash.get("currency")) or account_currency
            if amount is None:
                continue
            rows_seen += 1
            items.append((amount, cash_ccy, cash.get("label") or "cash"))
            cash_total[cash_ccy] = cash_total.get(cash_ccy, Decimal(0)) + amount
            key = f"CASH:{cash_ccy}|{cash_ccy}"
            if amount < 0:
                fragment["liabilities"].append({"id": f"{account_id}:debit-{cash_ccy.lower()}", "account_id": account_id,
                                                "name": "Negative cash balance", "type": "margin_or_overdraft",
                                                "value": out(-amount), "currency": cash_ccy})
                continue
            position = merged.setdefault(key, {"id": "", "account_id": account_id, "instrument_id": f"CASH:{cash_ccy}",
                                               "symbol": cash_ccy, "quantity": Decimal(0), "value": Decimal(0),
                                               "currency": cash_ccy, "asset_class": "cash"})
            position["quantity"] += amount
            position["value"] += amount
            if cash.get("page"):
                position.setdefault("page", cash["page"])

        flows = raw.get("flows") or None
        closing = _amount(flows.get("closing"), comma) if flows else None
        if flows and closing is not None and not items:
            rows_seen += 1
            items.append((closing, account_currency, "closing balance"))
            cash_total[account_currency] = closing
            if closing >= 0:
                merged[f"CASH:{account_currency}|{account_currency}"] = {
                    "id": "", "account_id": account_id, "instrument_id": f"CASH:{account_currency}",
                    "symbol": account_currency, "quantity": closing, "value": closing, "currency": account_currency,
                    "asset_class": "cash", **({"page": flows.get("page")} if flows.get("page") else {}),
                }
            else:
                fragment["liabilities"].append({"id": f"{account_id}:overdraft", "account_id": account_id,
                                                "name": "Overdrawn balance", "type": "overdraft",
                                                "value": out(-closing), "currency": account_currency})

        for key, position in merged.items():
            position["id"] = _unique(f"{account_id}:{position['instrument_id']}", used_ids)
            lots = lots_by_instrument.get(key, [])
            if lots and all(l["acquired_on"] and l["cost_basis"] is not None and l["quantity"] >= 0 for l in lots):
                for number, lot in enumerate(sorted(lots, key=lambda l: (l["acquired_on"], l["quantity"])), 1):
                    fragment["lots"].append({"id": f"{position['id']}:lot-{number}", "account_id": account_id,
                                             "instrument_id": position["instrument_id"], "quantity": out(lot["quantity"]),
                                             "acquired_on": lot["acquired_on"], "cost_basis": out(lot["cost_basis"]),
                                             "currency": position["currency"]})
            elif any(l["acquired_on"] for l in lots):
                warnings.append(f"Lots for {position['instrument_id']} in {account_id} were incomplete and were not recorded.")
            for field in ("quantity", "value", "cost_basis"):
                if isinstance(position.get(field), Decimal):
                    position[field] = out(position[field])
            if position.pop("cost_basis_partial", False):
                position.pop("cost_basis", None)
                warnings.append(f"Cost basis for {position['instrument_id']} in {account_id} was partial and was dropped.")
            fragment["positions"].append(position)

        for liability in raw.get("liabilities") or []:
            balance = parse_amount(liability.get("balance"), decimal_comma=comma)
            entry = {"id": _unique(f"{account_id}:liability-{slug(liability.get('label') or 'balance', 20)}", used_ids),
                     "account_id": account_id, "name": redact_text(str(liability.get("label") or "Balance owed")),
                     "value": out(abs(balance)) if balance is not None else None,
                     "currency": _ccy(liability.get("currency")) or account_currency}
            payment = parse_amount(liability.get("minimum_payment"), decimal_comma=comma)
            if payment is not None:
                entry["monthly_payment"] = out(abs(payment))
            rate = parse_percent(liability.get("interest_rate")) if liability.get("interest_rate") not in (None, "") else None
            if rate is not None:
                entry["interest_rate"] = out(rate)
            for field in ("no_interest_payment", "credit_limit"):  # what a card statement prints beside the balance
                figure = parse_amount(liability.get(field), decimal_comma=comma)
                if figure is not None:
                    entry[field] = out(abs(figure))
            if liability.get("cat") not in (None, ""):
                cat = parse_percent(liability["cat"])
                if cat is not None:
                    entry["cat"] = out(cat)
            if isinstance(liability.get("due_date"), str):
                entry["due_date"] = liability["due_date"]
            if liability.get("kind") in ("installments",):
                entry["kind"] = liability["kind"]
            if balance is None:
                missing.append({"key": f"liabilities.{entry['id']}.value", "reason": "missing", "detail": "Balance owed was not readable."})
            fragment["liabilities"].append(entry)

        tx_reconciled = None
        if raw.get("transactions") or raw.get("flows") or raw.get("debt_flows"):
            kind_for_tx = account_type if account_type != "unspecified" else ("brokerage" if raw.get("positions") else "checking")
            rows, tx_warnings, unread = normalize_transactions(raw.get("transactions") or [], account_id=account_id,
                                                               currency=account_currency, account_kind=kind_for_tx,
                                                               comma=comma, redact_text=redact_text)
            warnings.extend(tx_warnings)
            reasons.extend(unread)  # a movement the parser could not read makes the whole proposal a review
            all_transactions.extend(rows)
            tx_reconciled = _check_transactions(account_id, account_currency, raw, rows, comma, differences, assertions,
                                                statement.get("period_start"), as_of)
        recon_accounts.append(_reconcile_account(
            account_id, account_currency, raw, items, positions_value, cash_total, rows_seen, rates, comma,
            tolerance, differences, tx_reconciled, row_values,
        ))

    for item in statement.get("liabilities") or []:
        balance = parse_amount(item.get("balance"), decimal_comma=comma)
        lender = redact_text(str(item.get("institution") or "")).strip() or None
        ident = with_institution(str(item.get("label") or "debt"), lender)
        entry = {"id": _unique(f"liability-{slug(ident, 32)}", used_ids),
                 "name": redact_text(str(item.get("label") or "Debt")), "value": out(abs(balance)) if balance is not None else None,
                 "currency": _ccy(item.get("currency")) or currency or "XXX"}
        if lender:
            entry["lender"] = lender
        payment = parse_amount(item.get("minimum_payment"), decimal_comma=comma)
        if payment is not None:
            entry["monthly_payment"] = out(abs(payment))
        rate = parse_percent(item.get("interest_rate")) if item.get("interest_rate") not in (None, "") else None
        if rate is not None:
            entry["interest_rate"] = out(rate)
        fragment["liabilities"].append(entry)

    for item in statement.get("income") or []:
        amount = parse_amount(item.get("annual_amount"), decimal_comma=comma)
        income_ccy = _ccy(item.get("currency")) or currency or "XXX"
        entry = {"id": _unique(f"income-{slug(item.get('label') or 'income', 24)}", used_ids),
                 "description": redact_text(str(item.get("label") or "Income")), "currency": income_ccy}
        if amount is not None:
            entry["annual_amount"] = out(amount)
        for field in ("sector", "country", "frequency"):
            if item.get(field):
                entry[field] = item[field]
        per_period = parse_amount(item.get("per_period"), decimal_comma=comma)
        if per_period is not None and item.get("frequency"):
            entry["per_period"] = out(per_period)  # as the person said it ("85 mil al mes"), beside the year's figure
        fragment["income_exposures"].append(entry)

    fragment["unknown_sections"] = sorted(
        {"external_assets", "fund_holdings"}
        | ({"lots"} if not fragment["lots"] else set())
        | ({"income_exposures"} if not fragment["income_exposures"] else set())
        | ({"liabilities"} if not fragment["liabilities"] else set())
    )
    if unresolved:
        warnings.append(f"{len(unresolved)} statement line(s) could not enter the household; see result.unresolved.")
    statuses = [a["status"] for a in recon_accounts]
    if not recon_accounts and (fragment["liabilities"] or fragment["income_exposures"]):
        recon_status = "single_value"
    elif not recon_accounts:
        recon_status = "unverifiable"
        reasons.append("No accounts were found.")
    elif all(s in ("reconciled", "single_value") for s in statuses) and not differences:
        recon_status = "reconciled" if "reconciled" in statuses else "single_value"
    elif "mismatch" in statuses or differences:
        recon_status = "mismatch"
    else:
        recon_status = "unverifiable"
    if recon_status == "mismatch":
        reasons.append("Extracted values do not reconcile to the statement totals.")
    elif recon_status == "unverifiable":
        reasons.append("No statement total (or FX) was available to reconcile against.")
    if verification and verification.get("unverified"):
        reasons.append(f"{len(verification['unverified'])} extracted value(s) do not appear in the source text.")
    reconciliation = {"status": recon_status, "accounts": recon_accounts, "differences": differences}

    result: dict[str, Any] = {
        "source_kind": kind, "as_of": as_of, "currency": currency,
        "household": fragment, "transactions": all_transactions, "balance_assertions": assertions,
        "reconciliation": reconciliation, "unresolved": unresolved,
        "field_confidence": dict(sorted(field_confidence.items())),
        "provenance": provenance,
    }
    if verification is not None:
        result["verification"] = verification
    result = redact(result)
    result["proposal_id"] = proposal_digest(result)
    result["review_reasons"] = list(dict.fromkeys(str(reason) for reason in reasons))
    result["summary"] = _summary(result)
    status = "ready_to_confirm" if not result["review_reasons"] else "needs_review"
    result["confirmation"] = {
        "required": True,
        "instructions": "Show the summary and reconciliation to the person. Save only after they confirm, by calling "
                        "proposal_to_facts(proposal, confirmed=True, proposal_id=...)."
                        + ("" if status == "ready_to_confirm" else " Unresolved discrepancies also need acknowledge_discrepancies=True."),
    }
    sources = [provenance.get("ref")] if provenance.get("ref") else []
    report = envelope(status, result, missing=redact(missing), warnings=[redact_text(w) for w in warnings],
                      sources=sources, assumptions=assumptions)
    return mark_untrusted(report) if kind != "user" else report  # type: ignore[return-value]


def _reconcile_account(account_id, currency, raw, items, positions_value, cash_total, rows_seen, rates, comma,
                       tolerance, differences, tx_reconciled=None, row_values=None) -> dict[str, Any]:
    reported_raw = raw.get("reported_total") if isinstance(raw.get("reported_total"), dict) else None
    reported = _amount(reported_raw, comma) if reported_raw else None
    label = reported_raw.get("label") if reported_raw else None
    page = reported_raw.get("page") if reported_raw else raw.get("page")
    target = (_ccy(reported_raw.get("currency")) if reported_raw else None) or currency
    tol = parse_amount(tolerance) if tolerance is not None else Decimal("0.01") * max(1, rows_seen)
    entry: dict[str, Any] = {"account_id": account_id, "currency": target}
    flows = raw.get("flows") or None
    if flows:
        opening, deposits, withdrawals, closing = (_amount(flows.get(k), comma) for k in ("opening", "deposits", "withdrawals", "closing"))
        if None not in (opening, deposits, withdrawals, closing):
            expected = opening + deposits - abs(withdrawals)
            ok = abs(expected - closing) <= Decimal("0.02")
            entry["cash_flow"] = {"opening": out(opening), "deposits": out(deposits), "withdrawals": out(abs(withdrawals)),
                                  "closing": out(closing), "matches": ok}
            if not ok:
                differences.append({"account_id": account_id, "check": "cash_flow", "expected": out(closing),
                                    "computed": out(expected), "difference": out(expected - closing),
                                    "page": flows.get("page"),
                                    "detail": "opening + deposits - withdrawals does not equal the closing balance"})
        if reported is None and closing is not None and len(items) > 1:
            reported, label, page = closing, "closing balance", flows.get("page")
    computed: Decimal | None = Decimal(0)
    fx_missing: list[str] = []
    for value, ccy, _ in items:
        converted = rates.convert(value, ccy, target)
        if converted is None:
            fx_missing.append(f"{ccy}/{target}")
            computed = None
        elif computed is not None:
            computed += converted
    entry.update({
        "positions_value": out(positions_value), "cash": {k: out(v) for k, v in sorted(cash_total.items())},
        "computed_total": out(computed) if computed is not None else None,
        "reported_total": out(reported), "reported_label": label, "difference": None,
        "tolerance": out(tol), "page": page,
    })
    if fx_missing:
        entry["status"] = "unverifiable"
        entry["detail"] = "Missing statement FX for " + ", ".join(sorted(set(fx_missing))) + "."
    elif reported is not None:
        difference = computed - reported
        entry["difference"] = out(difference)
        entry["status"] = "reconciled" if abs(difference) <= tol else "mismatch"
        if entry["status"] == "mismatch":
            differences.append({"account_id": account_id, "check": "account_total", "expected": out(reported),
                                "computed": out(computed), "difference": out(difference), "page": page,
                                "detail": "positions plus cash do not equal the reported total"})
    elif tx_reconciled or (entry.get("cash_flow") or {}).get("matches"):
        entry["status"] = "reconciled"
        entry["detail"] = ("Closing balance reconciled through the opening balance and "
                           + ("the listed transactions." if tx_reconciled else "the period deposits and withdrawals."))
    elif raw.get("single_value") or len(items) <= 1:
        entry["status"] = "single_value"
        entry["detail"] = "Only one stated balance; there is no independent total to reconcile against."
    else:
        entry["status"] = "unverifiable"
        entry["detail"] = "No reported account total."
    subtotals = [s for s in raw.get("positions_subtotals") or [] if isinstance(s, dict)]
    if isinstance(raw.get("positions_subtotal"), dict):
        subtotals.append(raw["positions_subtotal"])
    checks = []
    for subtotal_raw in subtotals:
        subtotal = _amount(subtotal_raw, comma)
        if subtotal is None:
            continue
        # A section subtotal covers the rows since its header or the previous subtotal; a "Total positions"
        # line covers the whole account (with or without its cash rows).
        candidates = []
        rows = subtotal_raw.get("rows")
        if isinstance(rows, list) and len(rows) == 2 and row_values is not None:
            section = row_values[rows[0]:rows[1]]
            if section and all(v is not None for v in section):
                candidates.append(sum(section, Decimal(0)))
        candidates.append(positions_value)
        if row_values and all(v is not None for v in row_values):
            candidates.append(sum(row_values, Decimal(0)))
        check = any(abs(value - subtotal) <= tol for value in candidates)
        shown = next((value for value in candidates if abs(value - subtotal) <= tol), candidates[0])
        checks.append({"label": subtotal_raw.get("label"), "reported": out(subtotal), "computed": out(shown),
                       "matches": check})
        if not check:
            differences.append({"account_id": account_id, "check": "positions_subtotal", "expected": out(subtotal),
                                "computed": out(shown), "difference": out(shown - subtotal),
                                "page": subtotal_raw.get("page"),
                                "detail": "security rows do not sum to the printed positions subtotal"})
    if len(checks) == 1:
        entry["positions_subtotal"] = {k: v for k, v in checks[0].items() if k != "label"}
    elif checks:
        entry["positions_subtotals"] = checks
    return entry


def _check_transactions(account_id, currency, raw, rows, comma, differences, assertions, period_start, period_end) -> bool | None:
    """Balance assertions, transaction-sum and running-balance checks for one account."""
    debt = bool(raw.get("debt_flows"))
    flows = raw.get("debt_flows") or raw.get("flows") or {}
    opening, closing = _amount(flows.get("opening"), comma), _amount(flows.get("closing"), comma)
    regular = [r for r in rows if not r.get("installment")]
    total = sum((Decimal(r["amount"]) for r in regular), Decimal(0))
    reconciled = None
    if opening is not None or closing is not None:
        assertion: dict[str, Any] = {
            "account_id": account_id, "period_start": raw.get("period_start") or period_start,
            "period_end": raw.get("period_end") or period_end, "opening": out(opening), "closing": out(closing),
            "currency": currency, "balance_kind": "debt" if debt else "cash", "page": flows.get("page"),
        }
        if opening is not None and closing is not None and regular:
            expected = opening - total if debt else opening + total
            tol = Decimal("0.01") * max(2, len(regular))
            assertion["transactions_reconcile"] = reconciled = abs(expected - closing) <= tol
            if not assertion["transactions_reconcile"]:
                differences.append({"account_id": account_id, "check": "transaction_sum", "expected": out(closing),
                                    "computed": out(expected), "difference": out(expected - closing),
                                    "page": flows.get("page"),
                                    "detail": f"opening balance {'minus' if debt else 'plus'} {len(regular)} transactions does not equal the closing balance"})
        assertions.append(assertion)
    previous = opening if not debt else None
    breaks, chained = [], 0
    for row in regular:
        if row.get("balance") is None:
            previous = None
            continue
        balance = Decimal(row["balance"])
        if previous is not None and not debt:
            if abs(previous + Decimal(row["amount"]) - balance) > Decimal("0.01"):
                breaks.append(row)
            else:
                chained += 1
        previous = balance
    if breaks:
        first = breaks[0]
        differences.append({"account_id": account_id, "check": "running_balance", "item": first["description"][:60],
                            "expected": first["balance"], "computed": None, "difference": None, "page": first.get("page"),
                            "detail": f"{len(breaks)} transaction(s) do not follow the printed running balance (first on {first['date']})"})
        reconciled = False
    elif reconciled is None and chained and chained == len(regular) - (0 if opening is not None else 1) \
            and closing is not None and regular and regular[-1].get("balance") == out(closing):
        reconciled = True
        if assertions and assertions[-1]["account_id"] == account_id:
            assertions[-1]["running_balance_verified"] = True
    return reconciled


def proposal_digest(result: dict[str, Any]) -> str:
    provenance = {k: v for k, v in (result.get("provenance") or {}).items() if k not in {"retrieved_at", "received_at"}}
    return digest({
        "source_kind": result.get("source_kind"), "as_of": result.get("as_of"), "currency": result.get("currency"),
        "household": result.get("household"), "transactions": result.get("transactions"),
        "reconciliation": result.get("reconciliation"), "unresolved": result.get("unresolved"),
        "balance_assertions": result.get("balance_assertions"),
        "verification": result.get("verification"), "provenance": provenance,
    })


def _summary(result: dict[str, Any]) -> dict[str, Any]:
    household = result["household"]
    by_account = {a["account_id"]: a for a in result["reconciliation"]["accounts"]}
    rows = []
    for account in household["accounts"]:
        recon = by_account.get(account["id"], {})
        rows.append({
            "account_id": account["id"], "name": account.get("name"), "number": account.get("number_masked"),
            "type": account["type"], "currency": account["currency"],
            "positions": sum(1 for p in household["positions"] if p["account_id"] == account["id"] and p.get("asset_class") != "cash"),
            "positions_value": recon.get("positions_value"), "cash": recon.get("cash"),
            "computed_total": recon.get("computed_total"), "reported_total": recon.get("reported_total"),
            "difference": recon.get("difference"), "status": recon.get("status"),
        })
    return {"as_of": result["as_of"], "currency": result["currency"], "accounts": rows,
            "liabilities": len(household["liabilities"]), "reconciliation": result["reconciliation"]["status"]}


def proposal_to_facts(
    proposal: dict[str, Any],
    *,
    confirmed: bool,
    proposal_id: str,
    acknowledge_discrepancies: bool = False,
    review_days: int = 45,
    expires_on: str | None = None,
    today: date | None = None,
) -> dict[str, Any]:
    """Turn a person-confirmed proposal into ``WealthStore.remember`` fact payloads.

    Nothing is written.  ``confirmed`` must record the person's explicit
    confirmation and ``proposal_id`` must match the displayed proposal, so a
    changed or re-extracted proposal cannot be saved under an old confirmation.
    """
    today = today or _today()
    result = proposal.get("result") if isinstance(proposal, dict) else None
    if not isinstance(result, dict) or "household" not in result:
        raise ValueError("proposal must be an ingest proposal envelope")
    if confirmed is not True:
        return envelope("needs_input", {}, missing=[{"key": "confirmed", "reason": "missing",
                        "detail": "Ask the person to confirm the displayed summary before saving."}])
    if proposal_id != result.get("proposal_id") or proposal_digest(result) != proposal_id:
        raise ValueError("proposal_id does not match this proposal; show the current summary and confirm again")
    status = proposal.get("status")
    if status not in ("ready_to_confirm", "needs_review"):
        raise ValueError(f"a {status} proposal cannot be saved")
    if status == "needs_review" and not acknowledge_discrepancies:
        return envelope("needs_review", {"review_reasons": result.get("review_reasons", [])},
                        warnings=["The person must acknowledge the listed discrepancies before this can be saved."])
    as_of = result.get("as_of")
    if not as_of:
        raise ValueError("an as-of date is required before saving")
    observed = date.fromisoformat(as_of)
    if observed > today:
        raise ValueError("the as-of date cannot be in the future")
    expiry = date.fromisoformat(expires_on) if expires_on else observed + timedelta(days=review_days)
    if expiry < observed:
        raise ValueError("expires_on cannot precede the as-of date")
    warnings = []
    if expiry < today:
        warnings.append(f"This statement is older than {review_days} days; saved facts are already expired and will not drive calculations.")
    kind = result["source_kind"]
    store_kind = kind if kind in ("document", "user", "connector") else "tool"
    provenance = result.get("provenance") or {}
    ref = provenance.get("ref") or f"{kind}:{proposal_id[:16]}"
    source = {"kind": store_kind, "ref": ref, "observed_on": as_of}
    household = result["household"]
    recon = {a["account_id"]: a for a in result["reconciliation"]["accounts"]}
    acknowledged = result.get("review_reasons", []) if status == "needs_review" else []
    common = {"as_of": as_of, "proposal_id": proposal_id, "provenance": provenance}
    if acknowledged:
        common["acknowledged_discrepancies"] = acknowledged
    facts = []
    for account in household["accounts"]:
        account_id = account["id"]
        value = {
            **common, "currency": account["currency"], "account": account,
            "positions": [p for p in household["positions"] if p["account_id"] == account_id],
            "lots": [l for l in household["lots"] if l["account_id"] == account_id],
            "liabilities": [l for l in household["liabilities"] if l.get("account_id") == account_id],
            "reconciliation": recon.get(account_id),
            "balance_assertions": [a for a in result.get("balance_assertions") or [] if a["account_id"] == account_id],
            "fx": household["fx"],
        }
        facts.append({"key": f"account.{account_id}", "value": value, "source": source,
                      "confidence": "reported", "expires_on": expiry.isoformat()})
        activity = [t for t in result.get("transactions") or [] if t.get("account_id") == account_id]
        if activity:
            facts.append({"key": f"account.{account_id}.activity", "value": {**common, "transactions": activity},
                          "source": source, "confidence": "reported", "expires_on": expiry.isoformat()})
    for liability in household["liabilities"]:
        if liability.get("account_id"):
            continue
        facts.append({"key": f"liability.{liability['id']}", "value": {**common, "liability": liability},
                      "source": source, "confidence": "reported", "expires_on": expiry.isoformat()})
    for income in household["income_exposures"]:
        facts.append({"key": f"income.{income['id']}", "value": {**common, "income": income},
                      "source": source, "confidence": "reported", "expires_on": expiry.isoformat()})
    if not facts:
        raise ValueError("the proposal contains nothing to save")
    return envelope("ready", {
        "facts": facts, "request_id": f"ingest-{proposal_id[:32]}",
        "household_fragment": household, "expires_on": expiry.isoformat(),
    }, warnings=warnings, sources=[ref], assumptions=[
        f"Facts expire {review_days} days after the statement date unless an explicit expiry was supplied.",
        "Pass result.request_id to remember so a repeated confirmation is idempotent.",
    ])


def diff_proposals(previous: dict[str, Any] | None, current: dict[str, Any]) -> list[dict[str, Any]]:
    """List added, removed and changed accounts/positions/lots/liabilities/income by stable id."""
    def entities(proposal: dict[str, Any] | None) -> dict[tuple[str, str], dict[str, Any]]:
        if not proposal:
            return {}
        household = (proposal.get("result") or proposal).get("household") or {}
        return {(entity, item["id"]): item for entity in _ENTITIES for item in household.get(entity, [])}

    old, new = entities(previous), entities(current)
    changes = []
    for key in sorted(set(old) | set(new)):
        entity, identifier = key
        if key not in old:
            changes.append({"entity": entity, "id": identifier, "change": "added"})
        elif key not in new:
            changes.append({"entity": entity, "id": identifier, "change": "removed"})
        elif old[key] != new[key]:
            fields = {field: {"before": old[key].get(field), "after": new[key].get(field)}
                      for field in sorted(set(old[key]) | set(new[key])) if old[key].get(field) != new[key].get(field)}
            changes.append({"entity": entity, "id": identifier, "change": "changed", "fields": fields})
    return changes


def merge_household(household: dict[str, Any] | None, proposal: dict[str, Any]) -> dict[str, Any]:
    """Merge a confirmed proposal into a canonical household and validate it.

    Accounts in the proposal replace same-id accounts with all of their
    positions, lots and liabilities.  Other accounts are kept.  Returns the
    ``household.validate_household`` result (``household`` plus ``warnings``).
    """
    from ..household import validate_household

    result = proposal.get("result") or proposal
    fragment = deepcopy(result["household"] if "household" in result else result["household_fragment"])
    warnings: list[str] = []
    if household is None:
        merged = deepcopy(fragment)
        for account in merged["accounts"]:
            if account["currency"] == "XXX":
                raise ValueError(f"account {account['id']} has no currency")
    else:
        merged = deepcopy(household)
        for entity in ("people", "accounts", "positions", "lots", "liabilities", "income_exposures", "fx"):
            merged.setdefault(entity, [])
        people = {p["id"] for p in merged["people"]}
        merged["people"].extend(p for p in fragment["people"] if p["id"] not in people)
        replaced = {a["id"] for a in fragment["accounts"]}
        merged["accounts"] = [a for a in merged["accounts"] if a["id"] not in replaced] + fragment["accounts"]
        for entity in ("positions", "lots"):
            merged[entity] = [x for x in merged[entity] if x["account_id"] not in replaced] + fragment[entity]
        merged["liabilities"] = [x for x in merged["liabilities"] if x.get("account_id") not in replaced
                                 and x["id"] not in {l["id"] for l in fragment["liabilities"]}] + fragment["liabilities"]
        incoming = {i["id"] for i in fragment["income_exposures"]}
        merged["income_exposures"] = [i for i in merged["income_exposures"] if i["id"] not in incoming] + fragment["income_exposures"]
        pairs = {(f["from"], f["to"]) for f in fragment["fx"]}
        merged["fx"] = [f for f in merged["fx"] if (f["from"], f["to"]) not in pairs] + fragment["fx"]
        if fragment.get("as_of") and fragment["as_of"] != merged.get("as_of"):
            warnings.append(f"Merged values are dated {fragment['as_of']} while the household was dated {merged.get('as_of')}.")
            merged["as_of"] = max(fragment["as_of"], merged.get("as_of") or fragment["as_of"])
        if "unknown_sections" in merged:
            filled = {e for e in _ENTITIES if fragment.get(e)}
            merged["unknown_sections"] = sorted(set(merged["unknown_sections"]) - filled)
    checked = validate_household(merged)
    checked["warnings"] = warnings + checked["warnings"]
    return checked
