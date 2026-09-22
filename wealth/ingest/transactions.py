"""Statement transactions: header aliases, dates, signs, types and dedupe hashes.

Transaction shape emitted in ``IngestProposal.result.transactions``::

    {"id": dedupe hash, "dedupe_hash": same, "account_id", "date": operation date,
     "settlement_date": liquidación/posting date or None,
     "description": original-language text (redacted), "amount": signed decimal string,
     "currency", "type", "symbol", "quantity", "price", "fees",
     "balance": printed running balance or None, "page",
     "installment": {"number", "count", "original_amount", "remaining_balance"} or None}

``amount`` is signed from the account holder's cash perspective: money into
the account is positive, money out is negative.  On a credit card a purchase is
negative (it adds debt) and a payment received is positive.

``dedupe_hash`` is ``sha256(account_id | date | amount | normalised description
| occurrence)``; ``occurrence`` numbers identical same-day lines so genuine
duplicates (two equal coffees) stay distinct while re-imports collide.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
import hashlib
import re
from typing import Any, Iterable, Mapping

from .common import fold, out, parse_amount


TYPES = ("deposit", "withdrawal", "transfer", "buy", "sell", "dividend", "interest", "fee", "tax_withheld",
         "split", "fx", "income", "expense", "loan_payment", "opening_position")

TX_ALIASES: dict[str, tuple[str, ...]] = {
    "date": ("date", "fecha", "fecha de operacion", "fecha operacion", "fecha de la operacion", "trade date",
             "transaction date", "posting date", "run date", "fecha oper", "dia", "tradedate", "activity date",
             "fecha de movimiento", "fecha movimiento"),
    "settlement_date": ("fecha de liquidacion", "fecha liquidacion", "fecha liq", "settlement date", "settle date",
                        "fecha de aplicacion", "fecha de cargo", "fecha de registro", "post date", "settledate"),
    "description": ("description", "descripcion", "concepto", "detalle", "details", "transaction", "movimiento",
                    "descripcion del movimiento", "concepto referencia", "transaction description", "memo",
                    "descripcion de la operacion", "establecimiento", "comercio"),
    "debit": ("cargo", "cargos", "retiro", "retiros", "withdrawals", "withdrawal", "debit", "debits",
              "withdrawal amount", "debit amount", "cargos retiros", "retiros cargos", "salidas"),
    "credit": ("abono", "abonos", "deposito", "depositos", "deposits", "deposit", "credit", "credits",
               "deposit amount", "credit amount", "abonos depositos", "depositos abonos", "entradas"),
    "amount": ("amount", "monto", "importe", "net amount", "amount usd", "importe neto", "netcash", "proceeds",
               "monto de la operacion", "importe de la operacion", "cantidad"),
    "balance": ("saldo", "balance", "running balance", "saldo operacion", "saldo liquidacion", "saldo final",
                "ending balance"),
    "symbol": ("symbol", "ticker", "emisora", "emisora serie", "security id"),
    "quantity": ("quantity", "qty", "shares", "titulos", "units", "no de titulos"),
    "price": ("price", "precio", "unit price", "precio unitario", "tradeprice", "share price"),
    "fees": ("fees", "fee", "fees comm", "fees and comm", "commission", "comision", "comisiones", "ibcommission",
             "commissions and fees", "commission fees"),
    # IVA on the commission: part of what a trade costs, printed in its own column on Mexican statements.
    "fees_tax": ("iva", "iva comision", "iva de comision", "iva comisiones", "i v a"),
    # Printed but not money: a numeric reference must not be read into the amount columns beside it.
    "reference": ("referencia", "reference", "ref", "no de referencia", "referencia numerica", "folio",
                  "clave de rastreo", "numero de referencia", "no referencia"),
    "code": ("cod", "codigo", "cod mov", "cod operacion", "clave de movimiento"),
    "type": ("type", "tipo", "action", "activity", "transaction type", "tipo de operacion", "tipo de movimiento",
             "operacion", "activity type"),
    "original_amount": ("monto original", "importe original", "original amount", "monto de la compra"),
    "remaining_balance": ("saldo pendiente", "saldo insoluto", "remaining balance", "pendiente por pagar"),
    "installment_payment": ("pago requerido", "pago mensual", "mensualidad", "monthly payment", "pago del periodo"),
    "installment_number": ("num de pago", "numero de pago", "no de pago", "pago", "parcialidad", "mensualidades"),
}
_INDEX = {fold(alias): field for field, aliases in TX_ALIASES.items() for alias in aliases}

_MONTH = {"ene": 1, "jan": 1, "feb": 2, "mar": 3, "abr": 4, "apr": 4, "may": 5, "jun": 6, "jul": 7, "ago": 8,
          "aug": 8, "sep": 9, "set": 9, "oct": 10, "nov": 11, "dic": 12, "dec": 12}
_PARTIAL_NAME = re.compile(r"(?i)^(\d{1,2})[\s/\-.]?([a-z]{3})[a-z]*\.?(?:[\s/\-.]?(\d{2,4}))?$")
_PARTIAL_NAME_MDY = re.compile(r"(?i)^([a-z]{3})[a-z]*\.?\s+(\d{1,2})(?:,?\s+(\d{4}))?$")
_PARTIAL_NUMERIC = re.compile(r"^(\d{1,2})[/\-.](\d{1,2})(?:[/\-.](\d{2,4}))?$")
_ISO = re.compile(r"^(\d{4})-(\d{2})-(\d{2})")
_INSTALLMENT = re.compile(r"(?i)\b(\d{1,2})\s*(?:de|of|/)\s*(\d{1,2})\b")

_RULES: tuple[tuple[str, re.Pattern[str]], ...] = tuple((kind, re.compile(pattern, re.IGNORECASE)) for kind, pattern in (
    ("tax_withheld", r"\b(isr|retenci[oó]n|withholding|tax withheld|foreign tax|impuesto retenido)\b"),
    ("dividend", r"\b(dividend|dividendo|div\b|qualified div|cash div|distribuci[oó]n fibra|distribuci[oó]n de "
                 r"(dividendos|efectivo|rendimientos))"),
    ("interest", r"\b(inter[eé]s(es)?|interest|rendimientos?|intereses ganados|bank int)\b"),
    ("fee", r"\b(comisi[oó]n|commission|fee|cuota|anualidad|iva comisi|service charge|monthly maintenance)\b"),
    ("split", r"\b(split|reverse split|canje)\b"),
    ("fx", r"\b(fx|forex|cambio de divisa|currency exchange|conversion de divisas|compra de d[oó]lares|venta de d[oó]lares)\b"),
    ("sell", r"\b(sell|sold|venta|vta)\b"),
    ("buy", r"\b(buy|bought|reinvest|compra de valores|cpa|compra acciones|purchase of securit|compra\b)"),
    ("income", r"\b(n[oó]mina|payroll|salary|sueldo|direct dep|honorarios|pago de n[oó]mina)\b"),
    ("loan_payment", r"\b(pago (a )?(tarjeta|tdc|cr[eé]dito|hipoteca|pr[eé]stamo)|mortgage|loan payment|payment thank you|"
                     r"pago recibido|su pago|gracias por su pago|autopay|credit card payment)\b"),
    ("transfer", r"\b(spei|traspaso|transferencia|transfer|wire|zelle|ach|codi|dimo)\b"),
    ("withdrawal", r"\b(retiro|withdrawal|atm|cajero|disposici[oó]n)\b"),
    ("deposit", r"\b(dep[oó]sito|deposit|abono en cuenta)\b"),
))


_PAID_TO_OTHERS = re.compile(r"\b(renta|arrendamiento|alquiler|rent|colegiatura|tuition|mantenimiento|predial)\b")


def match_tx_header(cells: Iterable[str]) -> list[str | None] | None:
    """Canonical field per header cell, or None when this is not a transaction header."""
    fields = [_INDEX.get(fold(cell).replace("%", "").strip()) for cell in cells]
    present = {f for f in fields if f}
    if "date" in present and ("description" in present or "type" in present) and present & {"amount", "debit", "credit", "installment_payment", "original_amount"}:
        return fields
    return None


def resolve_date(text: str, period_end: date | None, *, day_first: bool | None) -> str | None:
    """Read a full or year-less row date; the year comes from the statement period."""
    value = text.strip().rstrip(".")
    iso = _ISO.match(value)
    if iso:
        try:
            return date(int(iso.group(1)), int(iso.group(2)), int(iso.group(3))).isoformat()
        except ValueError:
            return None
    day = month = year = None
    match = _PARTIAL_NAME.match(value)
    if match and match.group(2).lower()[:3] in _MONTH:
        day, month, year = int(match.group(1)), _MONTH[match.group(2).lower()[:3]], match.group(3)
    else:
        match = _PARTIAL_NAME_MDY.match(value)
        if match and match.group(1).lower()[:3] in _MONTH:
            month, day, year = _MONTH[match.group(1).lower()[:3]], int(match.group(2)), match.group(3)
        else:
            match = _PARTIAL_NUMERIC.match(value)
            if not match:
                return None
            first, second, year = int(match.group(1)), int(match.group(2)), match.group(3)
            if first > 12:
                day, month = first, second
            elif second > 12:
                month, day = first, second
            elif day_first is None:
                return None
            else:
                day, month = (first, second) if day_first else (second, first)
    if year is None:
        if period_end is None:
            return None
        resolved = period_end.year - (1 if month > period_end.month else 0)
    else:
        resolved = int(year) + (2000 if int(year) < 100 else 0)
    try:
        return date(resolved, month, day).isoformat()
    except ValueError:
        return None


def classify(description: str, amount: Decimal | None, *, account_kind: str, printed_type: str | None = None,
             has_security: bool = False) -> str:
    text = f"{printed_type or ''} {description or ''}"
    folded = fold(text)
    spending_account = account_kind not in ("brokerage", "credit_card")
    for kind, pattern in _RULES:
        if pattern.search(text) or pattern.search(folded):
            if kind in {"buy", "sell"} and not has_security and account_kind != "brokerage":
                break
            if kind == "fee" and amount is not None and amount > 0 and account_kind != "credit_card":
                continue
            if kind == "interest" and account_kind == "credit_card" and amount is not None and amount < 0:
                return "fee"
            if kind == "transfer" and account_kind == "credit_card" and amount is not None and amount > 0:
                return "loan_payment"  # "PAGO SPEI RECIBIDO": money into a card is a payment on it
            if kind == "transfer" and spending_account and amount is not None and amount < 0 \
                    and _PAID_TO_OTHERS.search(folded):
                return "expense"  # rent or school fees sent by SPEI are spending, not a move between own accounts
            if kind == "withdrawal" and spending_account and amount is not None and amount < 0:
                return "expense"  # cash taken at an ATM is spent (spending files it under cash_withdrawal)
            return kind
    if account_kind == "credit_card":
        return "expense" if amount is None or amount < 0 else "loan_payment"
    if amount is None:
        return "transfer"
    if amount > 0:
        return "deposit"
    if spending_account:
        # A bank-account outflow that is not a transfer, ATM withdrawal, fee or loan payment is a purchase
        # ("OXXO", "AMAZON MX", "UBER *TRIP"): spending.
        return "expense"
    return "expense" if re.search(r"(?i)\b(compra|purchase|pos|pago de servicio|domiciliaci[oó]n)\b", text) else "withdrawal"


def dedupe_hash(account_id: str, when: str, amount: str, description: str, occurrence: int) -> str:
    normal = " ".join(fold(description).split())
    raw = f"{account_id}|{when}|{amount}|{normal}|{occurrence}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def normalize(rows: list[Mapping[str, Any]], *, account_id: str, currency: str, account_kind: str,
              comma: bool | None, redact_text) -> tuple[list[dict[str, Any]], list[str]]:
    """Turn parsed rows into signed, typed, hashed transactions plus warnings."""
    warnings: list[str] = []
    result: list[dict[str, Any]] = []
    seen: dict[tuple[str, str, str], int] = {}
    for row in rows:
        when = row.get("date")
        if not when:
            warnings.append(f"A transaction in {account_id} has no readable date and was skipped.")
            continue
        description = " ".join(str(row.get("description") or "").split())
        amount = _signed(row, account_kind, comma)
        if amount is None:
            warnings.append(f"Transaction '{description[:40]}' on {when} in {account_id} has no readable amount and was skipped.")
            continue
        quantity = parse_amount(row.get("quantity"), decimal_comma=comma)
        price = parse_amount(row.get("price"), decimal_comma=comma)
        fees = parse_amount(row.get("fees"), decimal_comma=comma)
        fees_tax = parse_amount(row.get("fees_tax"), decimal_comma=comma)
        if fees_tax is not None:  # commission plus its IVA is what the trade cost
            fees = abs(fees or Decimal(0)) + abs(fees_tax)
        symbol =(str(row.get("symbol") or "").strip() or None)
        kind = row.get("type_override") or classify(description, amount, account_kind=account_kind,
                                                    printed_type=row.get("type"), has_security=bool(symbol or quantity))
        amount_text = out(amount)
        key = (when, amount_text, " ".join(fold(description).split()))
        seen[key] = seen.get(key, 0) + 1
        identifier = dedupe_hash(account_id, when, amount_text, description, seen[key])
        entry: dict[str, Any] = {
            "id": identifier, "dedupe_hash": identifier, "account_id": account_id, "date": when,
            "settlement_date": row.get("settlement_date"), "description": redact_text(description),
            "amount": amount_text, "currency": row.get("currency") or currency, "type": kind,
            "symbol": symbol, "quantity": out(quantity) if quantity is not None else None,
            "price": out(price) if price is not None else None,
            "fees": out(abs(fees)) if fees is not None else None,
            "balance": out(parse_amount(row.get("balance"), decimal_comma=comma)) if row.get("balance") not in (None, "") else None,
            "page": row.get("page"), "installment": _installment(row.get("installment"), comma),
        }
        result.append(entry)
    return result, warnings


def _signed(row: Mapping[str, Any], account_kind: str, comma: bool | None) -> Decimal | None:
    debit = parse_amount(row.get("debit"), decimal_comma=comma)
    credit = parse_amount(row.get("credit"), decimal_comma=comma)
    if debit is not None or credit is not None:
        return (abs(credit) if credit is not None else Decimal(0)) - (abs(debit) if debit is not None else Decimal(0))
    raw = row.get("amount")
    amount = parse_amount(raw, decimal_comma=comma)
    if amount is None:
        return None
    text = str(raw or "")
    printed_negative = amount < 0
    credit_mark = bool(re.search(r"(?i)\b(cr|abono)\b|\+\s*$", text))
    direction = row.get("direction")
    if direction == "in":
        return abs(amount)
    if direction == "out":
        return -abs(amount)
    if account_kind == "credit_card":
        return abs(amount) if printed_negative or credit_mark else -abs(amount)
    return amount


def _installment(value: Any, comma: bool | None) -> dict[str, Any] | None:
    if not value:
        return None
    result = {"number": value.get("number"), "count": value.get("count")}
    for key in ("original_amount", "remaining_balance"):
        amount = parse_amount(value.get(key), decimal_comma=comma)
        result[key] = out(abs(amount)) if amount is not None else None
    return result


def installment(text: str) -> tuple[int, int] | None:
    match = _INSTALLMENT.search(text or "")
    if match and 0 < int(match.group(1)) <= int(match.group(2)) <= 60:
        return int(match.group(1)), int(match.group(2))
    return None
