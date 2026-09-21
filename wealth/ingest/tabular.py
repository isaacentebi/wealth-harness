"""CSV/XLSX brokerage and bank exports (Schwab, Fidelity, Vanguard, IBKR, Mexican exports).

Columns are mapped through configurable header aliases
(:data:`wealth.ingest.columns.HEADER_ALIASES` plus caller ``aliases``).  Presets
only add what a header cannot say: institution, currency convention and how to
recognise the export.  Interactive Brokers activity statements use their
section layout (``Section,Header|Data|Total,...``).
"""

from __future__ import annotations

import csv
from datetime import date, datetime
import io
import re
from typing import Any

from .classify import account_type, detect_institution
from .columns import build_alias_index, match_header
from .common import find_dates, fold, parse_amount
from .redact import last4
from .safety import MAX_ROWS
from .statement import _ACCOUNT, _ACCOUNT_TOTAL
from .transactions import match_tx_header, resolve_date


PRESETS: dict[str, dict[str, Any]] = {
    "schwab": {"institution": "Charles Schwab", "currency": "USD",
               "signature": re.compile(r"(?i)positions for (?:account|all-accounts)|schwab")},
    "fidelity": {"institution": "Fidelity", "currency": "USD",
                 "headers": {"account number", "account name", "symbol", "description", "current value"}},
    "vanguard": {"institution": "Vanguard", "currency": "USD",
                 "headers": {"account number", "investment name", "symbol", "shares", "share price", "total value"}},
    "ibkr": {"institution": "Interactive Brokers", "currency": None},
    "mx": {"institution": None, "currency": "MXN",
           "headers_any": {"emisora", "titulos", "valor de mercado", "cargo", "abono", "concepto", "descripcion"}},
}
_ACCOUNT_LINE = re.compile(r"(?i)^(?:positions for account\s+)?(?P<label>[A-Za-z][A-Za-z &'\-]{1,40}?)\s*(?:\.\.\.|…|[Xx*]{2,})\s*(?P<tail>\d{3,4})\b")
_PENDING = re.compile(r"(?i)^pending activity\b")
_IBKR_SECTIONS = {"Statement", "Account Information", "Net Asset Value", "Open Positions", "Trades", "Dividends",
                  "Withholding Tax", "Deposits & Withdrawals", "Interest", "Fees", "Base Currency Exchange Rate"}


def _cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, float):
        return repr(value) if value == value else ""
    return str(value).strip()


def read_rows(data: bytes, kind: str) -> list[list[str]]:
    """Decode CSV (UTF-8/UTF-16/cp1252, sniffed delimiter) or every XLSX sheet into rows."""
    if kind == "xlsx":
        from openpyxl import load_workbook

        workbook = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        rows: list[list[str]] = []
        try:
            for sheet in workbook.worksheets:
                for values in sheet.iter_rows(values_only=True):
                    rows.append([_cell(v) for v in values])
                    if len(rows) > MAX_ROWS:
                        raise ValueError(f"export exceeds {MAX_ROWS} rows")
                rows.append([])
        finally:
            workbook.close()
        return rows
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        text = data.decode("utf-16")
    else:
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = data.decode("cp1252", errors="replace")
    sample = text[:8192]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
        delimiter = dialect.delimiter
    except csv.Error:
        delimiter = ","
    rows = []
    for row in csv.reader(io.StringIO(text), delimiter=delimiter):
        rows.append([cell.strip() for cell in row])
        if len(rows) > MAX_ROWS:
            raise ValueError(f"export exceeds {MAX_ROWS} rows")
    return rows


def _detect_preset(rows: list[list[str]]) -> str | None:
    head = [row for row in rows[:40] if any(row)]
    if sum(1 for row in head if len(row) > 2 and row[1] in ("Header", "Data") and row[0] in _IBKR_SECTIONS) >= 2:
        return "ibkr"
    blob = " ".join(" ".join(row) for row in head)
    if PRESETS["schwab"]["signature"].search(blob):
        return "schwab"
    for row in head:
        folded = {fold(cell) for cell in row if cell}
        for name in ("fidelity", "vanguard"):
            if PRESETS[name]["headers"] <= folded:
                return name
        if folded & PRESETS["mx"]["headers_any"]:
            return "mx"
    return None


def parse_export(rows: list[list[str]], *, preset: str | None = None, aliases: dict[str, list[str]] | None = None,
                 currency: str | None = None, as_of: str | None = None) -> dict[str, Any]:
    """Parse export rows into ``{"statement", "confidence", "notes", "parsed", "preset"}``."""
    preset = preset or _detect_preset(rows)
    if preset is not None and preset not in PRESETS:
        raise ValueError(f"unknown preset: {preset}; use one of {sorted(PRESETS)}")
    if preset == "ibkr":
        return _parse_ibkr(rows, currency=currency, as_of=as_of)
    index = build_alias_index(aliases)
    titles = "\n".join(" ".join(c for c in row if c) for row in rows if 0 < sum(1 for c in row if c) <= 2)
    institution_key, institution = detect_institution(titles)
    if preset and PRESETS[preset]["institution"] and institution is None:
        institution, institution_key = PRESETS[preset]["institution"], preset
    spanish = preset == "mx"
    day_first = True if spanish else False if preset in ("schwab", "fidelity", "vanguard") else None
    notes: list[str] = []
    warnings: list[str] = []
    confidence: dict[str, str] = {}

    accounts: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    current_key: str | None = None
    header: list[tuple[str | None, str | None]] | None = None
    tx_header: list[str | None] | None = None
    detected_dates: list[date] = []

    def account(key: str | None, label: str | None = None, number: str | None = None) -> dict[str, Any]:
        nonlocal current_key
        key = key or current_key or "default"
        if key not in accounts:
            accounts[key] = {"label": label, "number_last4": last4(number) if number else None,
                             "type": account_type(label or "")[0],
                             "currency": None, "positions": [], "cash": [], "reported_total": None,
                             "transactions": [], "flows": None, "liabilities": []}
            order.append(key)
        elif label and not accounts[key]["label"]:
            accounts[key]["label"] = label
            accounts[key]["type"] = accounts[key]["type"] or account_type(label)[0]
        current_key = key
        return accounts[key]

    for number, row in enumerate(rows, 1):
        cells = [c for c in row if c]
        if not cells:
            header = tx_header = None
            continue
        if len(cells) == 1 or (header is None and tx_header is None and len(cells) <= 2):
            text = " ".join(cells)
            detected_dates.extend(d for d, _ in find_dates(text, day_first=day_first))
            line = _ACCOUNT_LINE.match(text)
            numbered = _ACCOUNT.search(text)
            if numbered and header is None and tx_header is None and last4(numbered.group("num")):
                account(f"acct:{numbered.group('num')}", numbered.group("label").strip().title(), numbered.group("num"))
            elif line and header is None:
                account(f"line:{line.group('label')}:{line.group('tail')}", f"{line.group('label').strip()} ...{line.group('tail')}",
                        line.group("tail") if len(line.group("tail")) == 4 else None)
            if len(cells) == 1:
                continue
        tx_fields = match_tx_header(row)
        if tx_fields:
            tx_header, header = tx_fields, None
            continue
        mapped = [match_header(cell, index) if cell else (None, None) for cell in row]
        fields = {name for name, _ in mapped if name}
        if len(fields) >= 3 and "value" in fields and fields & {"quantity", "price"} and fields & {"symbol", "description"}:
            header, tx_header = mapped, None
            continue
        if tx_header is not None:
            record = {name: row[i] for i, name in enumerate(tx_header) if name and i < len(row) and row[i]}
            when = resolve_date(re.split(r"\s+as of\s+", record.get("date", ""), flags=re.IGNORECASE)[0],
                                None, day_first=day_first)
            if not when:
                continue
            target = account(record.get("account") and f"acct:{record['account']}", None, record.get("account"))
            target["transactions"].append({
                "date": when, "settlement_date": resolve_date(record.get("settlement_date", ""), None, day_first=day_first),
                "description": record.get("description") or record.get("type") or "", "type": record.get("type"),
                "debit": record.get("debit"), "credit": record.get("credit"), "amount": record.get("amount"),
                "balance": record.get("balance"), "symbol": record.get("symbol"), "quantity": record.get("quantity"),
                "price": record.get("price"), "fees": record.get("fees"), "page": None, "row": number,
            })
            continue
        if header is None:
            continue
        record: dict[str, str] = {}
        hints: dict[str, str] = {}
        for i, (name, hint) in enumerate(header):
            if name and i < len(row) and row[i] and name not in record:
                record[name] = row[i]
                if hint:
                    hints[name] = hint
        first = fold(record.get("symbol") or record.get("description") or cells[0])
        key = f"acct:{record['account']}" if record.get("account") else None
        target = account(key, record.get("account_name"), record.get("account"))
        if _PENDING.match(first):
            warnings.append(f"Pending activity of {record.get('value')} was excluded; it is not a settled holding.")
            continue
        if _ACCOUNT_TOTAL.match(first) or first in {"total", "account total", "totals"}:
            if record.get("value"):
                target["reported_total"] = {"amount": record["value"], "currency": hints.get("value"),
                                            "label": cells[0][:60], "page": None}
            continue
        if not (record.get("symbol") or record.get("description")):
            continue
        acquired = resolve_date(record.get("acquired_on", ""), None, day_first=day_first) if record.get("acquired_on") else None
        target["positions"].append({
            "symbol": (f"{record['symbol']} {record['serie']}" if record.get("symbol") and record.get("serie")
                       else record.get("symbol")),
            "currency_explicit": bool(record.get("currency") or hints.get("value")),
            "description": record.get("description"), "quantity": record.get("quantity"), "price": record.get("price"),
            "market_value": record.get("value"), "cost_basis": record.get("cost_basis"), "avg_cost": record.get("avg_cost"),
            "gain": record.get("gain"), "currency": (record.get("currency") or hints.get("value") or "").upper() or None,
            "asset_type": record.get("asset_type"), "acquired_on": acquired, "page": None, "row": number,
        })

    statement_currency = currency
    if statement_currency:
        confidence["currency"] = "high"
    else:
        row_currencies = {p["currency"] for a in accounts.values() for p in a["positions"] if p.get("currency")}
        if len(row_currencies) == 1:
            statement_currency, confidence["currency"] = row_currencies.pop(), "high"
        elif preset and PRESETS[preset]["currency"]:
            statement_currency, confidence["currency"] = PRESETS[preset]["currency"], "medium"
            notes.append(f"Amounts without a currency column were read as {statement_currency} ({preset} export convention).")
    if as_of:
        statement_as_of, confidence["as_of"] = as_of, "high"
    elif detected_dates:
        statement_as_of, confidence["as_of"] = max(detected_dates).isoformat(), "medium"
    else:
        tx_dates = [t["date"] for a in accounts.values() for t in a["transactions"]]
        statement_as_of = max(tx_dates) if tx_dates else None
        confidence["as_of"] = "medium" if statement_as_of else "low"
        if statement_as_of:
            notes.append("The export prints no statement date; the latest transaction date was used as the as-of date.")

    statement_accounts = []
    for key in order:
        item = accounts[key]
        if not (item["positions"] or item["transactions"] or item["reported_total"]):
            continue
        rows_with_balance = [t for t in item["transactions"] if parse_amount(t.get("balance")) is not None]
        if rows_with_balance and not item["positions"]:
            ordered = sorted(rows_with_balance, key=lambda t: (t["date"], t["row"]))
            descending = item["transactions"][0]["date"] > item["transactions"][-1]["date"]
            if descending:
                item["transactions"].reverse()
                ordered = sorted(rows_with_balance, key=lambda t: (t["date"], -t["row"]))
            item["flows"] = {"closing": ordered[-1]["balance"], "page": None}
            item["type"] = "checking"
            notes.append(f"The closing balance of {item['label'] or 'the account'} is the last printed running balance.")
        statement_accounts.append(item)
    return {
        "statement": {"institution": institution, "institution_key": institution_key, "as_of": statement_as_of,
                      "currency": statement_currency, "decimal_comma": None, "accounts": statement_accounts, "fx": [],
                      "market": "mx" if preset == "mx" else "us" if preset in ("schwab", "fidelity", "vanguard") else None},
        "confidence": confidence, "notes": notes, "warnings": warnings, "preset": preset,
        "parsed": any(a["positions"] or a["transactions"] for a in statement_accounts),
    }


def _parse_ibkr(rows: list[list[str]], *, currency: str | None, as_of: str | None) -> dict[str, Any]:
    headers: dict[str, list[str]] = {}
    sections: dict[str, list[tuple[str, dict[str, str]]]] = {}
    for row in rows:
        if len(row) < 3 or row[0] not in _IBKR_SECTIONS:
            continue
        name, kind = row[0], row[1]
        if kind == "Header":
            headers[name] = row[2:]
        elif name in headers:
            sections.setdefault(name, []).append((kind, dict(zip(headers[name], row[2:]))))
    info = {r.get("Field Name"): r.get("Field Value") for _, r in sections.get("Account Information", [])}
    statement_info = {r.get("Field Name"): r.get("Field Value") for _, r in sections.get("Statement", [])}
    base = currency or info.get("Base Currency")
    period = statement_info.get("Period") or ""
    dates = [d for d, _ in find_dates(period, day_first=False)]
    notes: list[str] = []
    account: dict[str, Any] = {
        "label": info.get("Account Type") or "Interactive Brokers account",
        "number_last4": last4(info.get("Account")), "type": "brokerage", "currency": base,
        "positions": [], "cash": [], "reported_total": None, "transactions": [], "flows": None, "liabilities": [],
    }
    for kind, record in sections.get("Net Asset Value", []):
        label = fold(record.get("Asset Class"))
        if label == "cash" and record.get("Current Total"):
            account["cash"].append({"amount": record["Current Total"], "currency": base, "label": "Cash", "page": None})
        elif label == "total" and record.get("Current Total"):
            account["reported_total"] = {"amount": record["Current Total"], "currency": base, "label": "Net Asset Value", "page": None}
    for kind, record in sections.get("Open Positions", []):
        if kind != "Data" or record.get("DataDiscriminator", "Summary") != "Summary":
            continue
        account["positions"].append({
            "symbol": record.get("Symbol"), "description": record.get("Description"), "quantity": record.get("Quantity"),
            "price": record.get("Close Price"), "market_value": record.get("Value"), "cost_basis": record.get("Cost Basis"),
            "currency": record.get("Currency"), "asset_type": record.get("Asset Category"), "page": None,
        })
    fx = [{"from": r.get("Currency"), "to": base, "rate": r.get("Rate"), "page": None}
          for kind, r in sections.get("Base Currency Exchange Rate", []) if kind == "Data" and base and r.get("Currency") != base]
    typed = {"Dividends": "dividend", "Withholding Tax": "tax_withheld", "Interest": "interest", "Fees": "fee"}
    for name, kind_name in typed.items():
        for kind, record in sections.get(name, []):
            if kind != "Data" or not record.get("Date") or fold(record.get("Currency")).startswith("total"):
                continue
            account["transactions"].append({"date": resolve_date(record["Date"], None, day_first=False),
                                            "description": record.get("Description", name), "amount": record.get("Amount"),
                                            "currency": record.get("Currency"), "type_override": kind_name, "page": None})
    for kind, record in sections.get("Deposits & Withdrawals", []):
        if kind != "Data" or not record.get("Settle Date"):
            continue
        amount = parse_amount(record.get("Amount"))
        account["transactions"].append({"date": resolve_date(record["Settle Date"], None, day_first=False),
                                        "description": record.get("Description", "Deposit/withdrawal"),
                                        "amount": record.get("Amount"), "currency": record.get("Currency"),
                                        "type_override": "deposit" if amount is not None and amount > 0 else "withdrawal",
                                        "page": None})
    for kind, record in sections.get("Trades", []):
        if kind != "Data" or record.get("DataDiscriminator", "Order") != "Order":
            continue
        quantity = parse_amount(record.get("Quantity"))
        proceeds, fee = parse_amount(record.get("Proceeds")), parse_amount(record.get("Comm/Fee"))
        net = None if proceeds is None else proceeds + (fee or 0)
        account["transactions"].append({
            "date": resolve_date((record.get("Date/Time") or "").split(",")[0], None, day_first=False),
            "description": f"{'Buy' if quantity and quantity > 0 else 'Sell'} {record.get('Symbol')}",
            "amount": str(net) if net is not None else None, "symbol": record.get("Symbol"),
            "quantity": record.get("Quantity"), "price": record.get("T. Price"), "fees": record.get("Comm/Fee"),
            "currency": record.get("Currency"), "type_override": "buy" if quantity and quantity > 0 else "sell", "page": None,
        })
    return {
        "statement": {"institution": "Interactive Brokers", "institution_key": "ibkr",
                      "as_of": as_of or (max(dates).isoformat() if dates else None), "currency": base,
                      "decimal_comma": None, "accounts": [account], "fx": fx},
        "confidence": {"as_of": "high" if (as_of or dates) else "low", "currency": "high" if base else "low"},
        "notes": notes, "preset": "ibkr", "parsed": bool(account["positions"] or account["cash"] or account["transactions"]),
    }
