"""Deterministic layout-text statement parser for US and Mexican statements.

Input is page text with column alignment preserved (pypdf layout mode).  The
parser finds accounts, holdings tables, cash, totals, bank cash flows, credit
balances, FX and UDI values, the statement date and currency.  It returns the
intermediate statement consumed by :func:`wealth.ingest.model.build_proposal`
and never invents a missing number.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
import re
from typing import Any

from .classify import CASH_LABEL, account_type, detect_institution
from .columns import build_alias_index, match_header
from .common import _DATE_PATTERNS, find_dates, fold, parse_amount
from .redact import last4
from .transactions import installment, match_tx_header, resolve_date


_CELL = re.compile(r"\S+(?: \S+)*")
_ACCOUNT = re.compile(
    r"(?i)(?:\b|^)(?P<label>account(?:\s+(?:number|no\.?|num(?:ber)?\.?|#))?|acct\.?(?:\s+no\.?)?|"
    r"(?:no\.?|n[uú]m(?:ero)?\.?)\s+de\s+(?:cuenta|contrato)|cuenta(?:\s+(?:no\.?|n[uú]m(?:ero)?\.?))?|"
    r"contrato(?:\s+(?:no\.?|n[uú]m(?:ero)?\.?))?|(?:n[uú]mero\s+de\s+)?tarjeta(?:\s+(?:no\.?|n[uú]m(?:ero)?\.?))?|"
    r"card(?:\s+(?:number|no\.?|ending\s+in))?)\s*[:#]\s*(?P<num>[A-Z]{0,3}[\dXx*•\-]{2,}(?: [\dXx*•\-]+)*\d)"
)
# Mexican bank statements print the number in its own column, without a colon ("No. de Cuenta   0482917365").
# Only these explicit labels are read that way: "cuenta 0012..." inside a transaction description is not an account.
_ACCOUNT_SPACED = re.compile(
    r"(?i)(?:^|\s)(?P<label>(?:no\.?|n[uú]m(?:ero)?\.?)\s+de\s+(?:cuenta|contrato|tarjeta)|account\s+(?:number|no\.?))"
    r"\s{2,}(?P<num>[A-Z]{0,3}[\dXx*•\-]{4,}(?: [\dXx*•\-]+)*\d)\b"
)
_COLUMN_GAP = re.compile(r"\s{8,}")  # layout text separates side-by-side columns with a run of spaces
_ACCOUNT_TYPE = re.compile(r"(?i)\b(?:account\s+type|tipo\s+de\s+(?:cuenta|contrato)|registration|producto)\s*[:\-]\s*(?P<t>.+)$")
_CURRENCY_DECL = re.compile(
    r"(?i)\b(?:base\s+currency|reporting\s+currency|currency|moneda|divisa|cifras\s+en|amounts\s+in|"
    r"expresad[oa]s?\s+en|valores\s+en|posici[oó]n\s+en|inversiones\s+en)\s*[:\-]?\s*"
    r"(?P<c>usd|mxn|eur|cad|gbp|u\.?s\.?\s+dollars?|d[oó]lares(?:\s+americanos)?|dlls?\.?|dls\.?|pesos(?:\s+mexicanos)?|"
    r"moneda\s+nacional|m\.\s?n\.|mn|euros?|udis?)\b"
)
_CCY_WORDS = (
    (re.compile(r"(?i)^(usd|u\.?s\.?\s+dollars?|d[oó]lares.*|dlls?\.?|dls\.?)$"), "USD"),
    (re.compile(r"(?i)^(mxn|pesos.*|moneda\s+nacional|m\.\s?n\.|mn)$"), "MXN"),
    (re.compile(r"(?i)^(eur|euros?)$"), "EUR"),
    (re.compile(r"(?i)^cad$"), "CAD"),
    (re.compile(r"(?i)^gbp$"), "GBP"),
    (re.compile(r"(?i)^udis?$"), "UDI"),
)
_ROW_USD = re.compile(r"(?i)(\bUSD\b|\bdlls?\b\.?|\bdls\b\.?|US\$)")
_ROW_MXN = re.compile(r"(?i)(\bMXN\b|\bM\.\s?N\.|\d\s*MN\b|\bMN\s*\$|\bpesos\b)")
_FX = re.compile(
    r"(?i)\b(?:tipo\s+de\s+cambio|exchange\s+rate|fx\s+rate)\b[^0-9A-Z]*(?:(?P<a>[A-Z]{3})\s*/\s*(?P<b>[A-Z]{3}))?"
    r"[^0-9]*?(?P<r>\d{1,3}(?:[.,]\d{2,6}))"
)
_ANY_DATE = tuple(pattern for _, pattern in _DATE_PATTERNS)
_FX_EQ = re.compile(r"\b1\s*(?P<a>USD|EUR|CAD|GBP)\s*=\s*(?P<r>\d{1,3}(?:[.,]\d{2,6}))\s*(?P<b>MXN|USD)\b")
_UDI = re.compile(r"(?i)\b(?:valor\s+de\s+la\s+udi|valor\s+udi|udi\s+value|precio\s+de\s+la\s+udi)\b[^0-9]*(?P<r>\d{1,2}[.,]\d{4,6})")
_PERIOD = re.compile(r"(?i)\b(statement\s+period|period|periodo|per[ií]odo|for\s+the\s+period|del|from)\b")
_ASOF = re.compile(
    r"(?i)\b(as\s+of|fecha\s+de\s+corte|corte\s+al|al\s+corte|period\s+ending|statement\s+date|valuaci[oó]n\s+al|"
    r"posici[oó]n\s+al|saldos?\s+al|cierre\s+al|fecha\s+de\s+cierre|closing\s+date|al)\b"
)

_ACCOUNT_TOTAL = re.compile(
    r"^(total (account|portfolio) value|account total|total account( value)?|net account value|total value|"
    r"ending (account )?value|total assets|valor total( de (la |tu )?(cartera|cuenta|inversion|portafolio))?|"
    r"valor total del portafolio|total (de )?(la |tu )?cartera|total del portafolio|total de (la )?cuenta|"
    r"valor de (la |tu )?(cartera|portafolio)|valor del portafolio|total (de )?(tus )?inversiones|total patrimonio|"
    r"patrimonio total|total general|gran total|total portfolio)\b"
)
_POSITIONS_SUBTOTAL = re.compile(
    r"^(total (positions|securities|holdings|investments|market value|valores|posiciones)|"
    r"total de (valores|posiciones|la posicion)|total posicion|subtotal( valores)?)\b"
)
_TOTAL_ANY = re.compile(r"^(sub)?total\b")
_OPENING = re.compile(r"^(saldo anterior|saldo inicial|beginning balance|opening balance|previous balance|starting balance|balance forward)\b")
_DEPOSITS = re.compile(r"^(depositos|abonos|total (de )?(depositos|abonos)|deposits|total deposits|credits|deposits and (other )?(additions|credits))\b")
_WITHDRAWALS = re.compile(r"^(retiros|cargos|total (de )?(retiros|cargos)|withdrawals|total withdrawals|debits|checks paid|withdrawals and (other )?(subtractions|debits))\b")
_CLOSING = re.compile(r"^(saldo final|saldo al corte|saldo actual|saldo al cierre|ending balance|closing balance|new balance|saldo final del periodo|saldo total)\b")
# Exact labels: "Pago mínimo + compras y cargos diferidos a meses" is a different, larger figure.
_MIN_PAYMENT = re.compile(r"^(minimum payment( due)?|pago minimo( a pagar| requerido| del periodo)?)$")
_NO_INTEREST = re.compile(r"^(pago para no generar intereses|pago sin intereses|payment to avoid interest( charges)?)$")
_TOTAL_DEBT = re.compile(r"^(saldo deudor total|adeudo total|deuda total|total balance owed)$")
_CREDIT_LIMIT = re.compile(r"^(limite de credito|credit limit|linea de credito)$")
_DUE_DATE = re.compile(r"(?i)\b(fecha\s+l[ií]mite\s+de\s+pago|payment\s+due\s+date)\b")
_RATE = re.compile(r"(?i)\b(apr|annual percentage rate|tasa de inter[eé]s(?: anual)?|tasa anual|tasa ordinaria)\b[^0-9]*(?P<r>\d{1,2}(?:[.,]\d+)?)\s*%")
_RATE_OTHER = re.compile(r"(?i)\b(mensual|monthly|moratori[ao]|penalty|promedio|cat|bruta)\b")
_CAT = re.compile(r"(?i)\bCAT\b(?:\s+promedio)?\s*:?\s*(?P<r>\d{1,3}(?:[.,]\d+)?)\s*%")
_PAIR_AMOUNT = re.compile(r"(?<![\w.,/$])(?:\(\s*)?[-−–]?\$?\s?\d{1,3}(?:,\d{3})*\.\d{2}(?:\s*\))?(?![\w/%]|[.,]\d)")
_CONTINUED = re.compile(r"\b(continuacion|continua|cont|continued|continuation)\b")
_TICKER_IN_DESC = re.compile(r"\(([A-Z][A-Z0-9.]{0,6})\)\s*$")
_ES_WORDS = ("saldo", "cuenta", "periodo", "emisora", "titulos", "fecha", "corte", "efectivo", "cartera", "moneda",
             "inversion", "rendimiento", "contrato", "estado de cuenta", "valor de mercado", "plusvalia")
_EN_WORDS = ("account", "statement", "period", "balance", "quantity", "shares", "market value", "holdings", "cash")


@dataclass
class _Account:
    label: str | None = None
    number_last4: str | None = None
    type: str | None = None
    type_confidence: str = "low"
    currency: str | None = None
    page: int | None = None
    positions: list[dict[str, Any]] = field(default_factory=list)
    cash: list[dict[str, Any]] = field(default_factory=list)
    totals: list[dict[str, Any]] = field(default_factory=list)
    subtotals: list[dict[str, Any]] = field(default_factory=list)
    section_start: int = 0  # first position row the next printed subtotal covers
    flows: dict[str, Any] = field(default_factory=dict)
    min_payment: str | None = None
    interest_rate: str | None = None
    rate_rank: int = -1
    card: dict[str, Any] = field(default_factory=dict)  # pago para no generar intereses, CAT, límite, fecha límite
    transactions: list[dict[str, Any]] = field(default_factory=list)

    def has_content(self) -> bool:
        return bool(self.positions or self.cash or self.totals or self.flows or self.transactions)


def _cells(line: str) -> list[tuple[str, int, int]]:
    return [(m.group(0), m.start(), m.end()) for m in _CELL.finditer(line)]


def _is_amount(text: str) -> bool:
    return parse_amount(text) is not None and not _is_reference(text)


def _is_reference(text: str, *, long_numbers: bool = False) -> bool:
    """A bare digit string with a leading zero ("0150826") is a reference or folio, never money.

    With ``long_numbers`` (transaction rows) so is a bare run of seven or more digits: statements print
    money with thousands separators and decimals, and reading a SPEI reference as an amount turned a
    42,500.00 payroll deposit into a 108,326 withdrawal.
    """
    return re.fullmatch(r"0\d{3,}" + (r"|\d{7,}" if long_numbers else ""), text.strip()) is not None


def _adjacent(before: str, after: str) -> str:
    """The text touching an inline match: a neighbouring column (a name or address) is not its label."""
    left = "" if re.search(r"\s{8,}$", before) else _COLUMN_GAP.split(before.strip())[-1] if before.strip() else ""
    right = "" if re.match(r"\s{8,}", after) else _COLUMN_GAP.split(after.strip())[0] if after.strip() else ""
    return f"{left} {right}"


def _without_dates(line: str) -> str:
    for pattern in _ANY_DATE:
        line = pattern.sub(" ", line)
    return line


_SUMMARY = (("opening", _OPENING), ("deposits", _DEPOSITS), ("withdrawals", _WITHDRAWALS), ("closing", _CLOSING),
            ("total_debt", _TOTAL_DEBT), ("minimum_payment", _MIN_PAYMENT), ("no_interest_payment", _NO_INTEREST),
            ("credit_limit", _CREDIT_LIMIT))


def _summary_figures(pairs: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """The balance-summary figures among a line's (label, amount) pairs."""
    found = []
    for label, amount in pairs:
        label = re.sub(r"^(?:\d+\s+)+", "", label)  # a leftover row number
        name = next((name for name, pattern in _SUMMARY if pattern.match(label)), None)
        if name:
            found.append((name, amount))
    return found


def _pairs(line: str) -> list[tuple[str, str]]:
    """(folded label, printed amount) for every amount on a line, labelled by the text since the previous one.

    Summary boxes print two columns side by side ("Saldo Promedio 38,214.77   Saldo Anterior 31,245.67");
    each figure keeps its own label instead of the line's first words.
    """
    result, start = [], 0
    for match in _PAIR_AMOUNT.finditer(line):
        result.append((fold(line[start:match.start()]), match.group(0).strip()))
        start = match.end()
    return result


def _currency_word(text: str) -> str | None:
    value = text.strip().rstrip(".").strip()
    for pattern, code in _CCY_WORDS:
        if pattern.match(value) or pattern.match(value + "."):
            return code
    return None


def _language(text: str) -> str | None:
    folded = fold(text)
    es = sum(folded.count(word) for word in _ES_WORDS)
    en = sum(folded.count(word) for word in _EN_WORDS)
    if es >= 3 and es >= en / 2:
        return "es"
    if en >= 3:
        return "en"
    return None


def _statement_date(pages: list[tuple[int, str]], day_first: bool | None) -> tuple[str | None, str, int | None, str | None]:
    best: tuple[int, str, str, int, str | None] | None = None
    for page, text in pages:
        for line in text.splitlines():
            dates = find_dates(line, day_first=day_first)
            if not dates:
                continue
            start = None
            if _PERIOD.search(line) and (len(dates) >= 2 or re.search(r"(?i)\b(al|to|through|-|–)\b", line)):
                rank, value = 0, dates[-1]
                start = dates[0][0].isoformat() if len(dates) >= 2 else None
            elif _ASOF.search(line):
                rank, value = 1, dates[0]
            else:
                continue
            if best is None or rank < best[0]:
                best = (rank, value[0].isoformat(), value[1], page, start)
        if best is not None and best[0] == 0:
            break
    if best is None:
        return None, "low", None, None
    return best[1], best[2], best[3], best[4]


def _statement_currency(text: str, language: str | None) -> tuple[str | None, str, list[str]]:
    notes: list[str] = []
    declared = [c for c in (_currency_word(m.group("c")) for m in _CURRENCY_DECL.finditer(text)) if c and c != "UDI"]
    if declared:
        return Counter(declared).most_common(1)[0][0], "high", notes
    usd = len(_ROW_USD.findall(text))
    mxn = len(_ROW_MXN.findall(text)) + len(re.findall(r"(?i)moneda nacional", text))
    if usd and not mxn:
        return "USD", "medium", notes
    if mxn and not usd:
        return "MXN", "medium", notes
    if usd and mxn:
        notes.append("Both USD and MXN markers appear; account and row currencies were read from their own markers.")
        return ("MXN" if language == "es" else "USD"), "medium", notes
    if "$" in text:
        if language == "es":
            notes.append("A Spanish-language statement with '$' and no currency code was read as MXN (Mexican convention).")
            return "MXN", "medium", notes
        if language == "en":
            notes.append("An English-language statement with '$' and no currency code was read as USD.")
            return "USD", "medium", notes
    return None, "low", notes




_HOLDING_NUMERIC = frozenset({"quantity", "price", "value", "cost_basis", "avg_cost", "gain", "rate"})
_TX_NUMERIC = frozenset({"debit", "credit", "amount", "balance", "quantity", "price", "fees", "fees_tax",
                         "original_amount", "remaining_balance", "installment_payment"})
_IN_SECTION = re.compile(r"^(deposits|depositos|abonos|credits|payments (and|&) (other )?credits|pagos y abonos|additions|entradas)")
_OUT_SECTION = re.compile(r"^(withdrawals|retiros|cargos|debits|checks|purchases|compras|electronic withdrawals|atm|card purchases|"
                          r"other withdrawals|consumos|salidas)")
_MSI_SECTION = re.compile(r"(meses sin intereses|\bmsi\b|compras diferidas|cargos diferidos|installment)")
_MX_INSTITUTIONS = frozenset({"gbm", "actinver", "banorte", "bbva", "cetesdirecto", "nu", "hey", "kuspit"})
_US_INSTITUTIONS = frozenset({"schwab", "fidelity", "vanguard", "merrill", "etrade", "morganstanley", "robinhood",
                               "chase", "wellsfargo", "bofa"})


def _continues(cell: tuple[str, int, int], columns: list[dict[str, Any]] | None) -> bool:
    """A wrapped description line starts under the description column, not at the margin."""
    description = next((c for c in columns or [] if c["field"] == "description"), None)
    return bool(description and cell[1] > 0 and abs(cell[1] - description["start"]) <= 4 and len(cell[0]) < 90)


def _header(line: str, index: dict[str, str]) -> list[dict[str, Any]] | None:
    columns = []
    for text, start, end in _cells(line):
        name, hint = match_header(text, index)
        if name:
            columns.append({"field": name, "start": start, "end": end, "currency": hint})
    fields = {c["field"] for c in columns}
    if len(columns) >= 3 and "value" in fields and ({"quantity", "price"} & fields) and ({"symbol", "description"} & fields):
        return columns
    return None


def _tx_header(line: str) -> list[dict[str, Any]] | None:
    cells = _cells(line)
    fields = match_tx_header([c[0] for c in cells])
    if not fields:
        return None
    if fields.count("date") > 1 and "settlement_date" not in fields:
        # "Fecha | Fecha" over "operación | liquidación": the second date column is the settlement date.
        second = [i for i, f in enumerate(fields) if f == "date"][1]
        fields = [*fields[:second], "settlement_date", *fields[second + 1:]]
    return [{"field": f, "start": c[1], "end": c[2], "currency": None} for f, c in zip(fields, cells) if f]


def _merge_header(first: str, second: str) -> str:
    """Join a two-line header by column overlap ('Valor de' / 'Mercado')."""
    top, bottom = _cells(first), _cells(second)
    merged = [" "] * (max(len(first), len(second)) + 2)
    used: set[int] = set()
    for text, start, end in top:
        extra = [i for i, b in enumerate(bottom) if b[1] < end + 1 and b[2] > start - 1 and i not in used]
        used.update(extra)
        label = " ".join([text] + [bottom[i][0] for i in extra])
        left = min([start] + [bottom[i][1] for i in extra])
        merged[left:left + len(label)] = list(label)
    for i, (text, start, _) in enumerate(bottom):
        if i not in used:
            merged[start:start + len(text)] = list(text)
    return "".join(merged)


def _assign(cells: list[tuple[str, int, int]], columns: list[dict[str, Any]], numeric_fields=_HOLDING_NUMERIC) -> dict[str, str]:
    """Map row cells to header columns: numbers by right edge, text by nearest edge."""
    numeric = [c for c in columns if c["field"] in numeric_fields]
    textual = [c for c in columns if c["field"] not in numeric_fields]
    row: dict[str, str] = {}
    distance: dict[str, int] = {}
    for text, start, end in cells:
        if _is_reference(text, long_numbers=numeric_fields is _TX_NUMERIC):
            numeric_like = False
        else:
            numeric_like = _is_amount(text) or re.fullmatch(r"[-(]?\$?\s?[\d.,]+%?\)?", text) is not None
        if numeric_like:
            pool = numeric or columns
            target = min(pool, key=lambda c: abs(c["end"] - end))
            gap = abs(target["end"] - end)
            if gap > 18 and textual:
                target = min(columns, key=lambda c: min(abs(c["start"] - start), abs(c["end"] - end)))
                gap = min(abs(target["start"] - start), abs(target["end"] - end))
        else:
            pool = textual or columns
            target = min(pool, key=lambda c: min(abs(c["start"] - start), abs(c["end"] - end)))
            gap = min(abs(target["start"] - start), abs(target["end"] - end))
        name = target["field"]
        if name in row and name not in numeric_fields:
            row[name] = f"{row[name]} {text}"
        elif name not in row or gap < distance[name]:
            row[name], distance[name] = text, gap
    return row


def _line_amount(cells: list[tuple[str, int, int]], columns: list[dict[str, Any]] | None) -> str | None:
    amounts = [c for c in cells[1:] if _is_amount(c[0])]
    if not amounts:
        return None
    value_column = next((c for c in columns or [] if c["field"] in ("value", "balance", "amount")), None)
    if value_column:
        return min(amounts, key=lambda c: abs(c[2] - value_column["end"]))[0]
    return amounts[-1][0]


def _row_currency(line: str, section_currency: str | None, columns: list[dict[str, Any]] | None) -> str | None:
    if _ROW_USD.search(line) and not _ROW_MXN.search(line):
        return "USD"
    if _ROW_MXN.search(line) and not _ROW_USD.search(line):
        return "MXN"
    hint = next((c["currency"] for c in columns or [] if c["field"] == "value" and c.get("currency")), None)
    return hint or section_currency


def parse_statement_text(pages: list[tuple[int, str]], *, aliases: dict[str, list[str]] | None = None) -> dict[str, Any]:
    """Parse page texts into ``{"statement", "confidence", "notes", "parsed"}``."""
    index = build_alias_index(aliases)
    full = "\n".join(text for _, text in pages)
    language = _language(full)
    day_first = True if language == "es" else False if language == "en" else None
    institution_key, institution = detect_institution(full)
    as_of, as_of_confidence, as_of_page, period_start = _statement_date(pages, day_first)
    period_end = date.fromisoformat(as_of) if as_of else None
    currency, currency_confidence, notes = _statement_currency(full, language)
    if currency is None and institution_key in _US_INSTITUTIONS:
        currency, currency_confidence = "USD", "medium"
        notes.append(f"No currency marker was printed; {institution} statements were read as USD.")
    elif currency is None and (institution_key in _MX_INSTITUTIONS or (institution_key == "santander" and language == "es")):
        currency, currency_confidence = "MXN", "medium"
        notes.append(f"No currency marker was printed; {institution} statements were read as MXN.")
    comma = True if re.search(r"\b\d{1,3}(?:\.\d{3})+,\d{2}\b", full) and not re.search(r"\b\d{1,3}(?:,\d{3})+\.\d{2}\b", full) else None
    header_text = "\n".join(pages[0][1].splitlines()[:12]) if pages else ""
    document_type, _ = account_type(header_text)

    accounts: list[_Account] = []
    current = _Account()
    fx: list[dict[str, Any]] = []
    columns: list[dict[str, Any]] | None = None
    tx_columns: list[dict[str, Any]] | None = None
    section, section_currency = "", None
    last_row: dict[str, Any] | None = None
    last_tx: dict[str, Any] | None = None

    def start_account(page: int) -> _Account:
        nonlocal columns, tx_columns, section_currency, last_row, last_tx
        if current.has_content() or current.number_last4:
            accounts.append(current)
        columns = tx_columns = section_currency = last_row = last_tx = None
        return _Account(page=page)

    # A column header repeated after a page break continues its section; only a heading the statement has not
    # printed on an earlier page (page chrome repeats) and that is not a "(continuación)" marker starts a new one.
    earlier_headings: set[str] = set()
    page_headings: set[str] = set()
    new_section = False

    def heading(text: str) -> None:
        nonlocal new_section
        folded = fold(text)
        page_headings.add(folded)
        if folded not in earlier_headings and not _CONTINUED.search(folded):
            new_section = True

    for page, text in pages:
        earlier_headings |= page_headings
        page_headings = set()
        lines = text.splitlines()
        skip_next = False
        for number, line in enumerate(lines):
            if skip_next:
                skip_next = False
                continue
            stripped = line.strip()
            if not stripped:
                last_row = last_tx = None
                continue
            cells = _cells(line)
            has_amount = any(_is_amount(c[0]) for c in cells)
            match = _ACCOUNT.search(line)
            if match is None and not (cells and resolve_date(cells[0][0], period_end, day_first=day_first)):
                match = _ACCOUNT_SPACED.search(line)
            if match and "clabe" not in fold(match.group("label")):
                tail = last4(match.group("num"))
                if tail and tail != current.number_last4:
                    if current.number_last4 is None and not current.has_content():
                        current.page = page
                    else:
                        current = start_account(page)
                    current.number_last4 = tail
                    rest = line[: match.start()] + " " + line[match.end():]
                    declared = _CURRENCY_DECL.search(rest)
                    if declared and _currency_word(declared.group("c")) not in (None, "UDI"):
                        current.currency = _currency_word(declared.group("c"))
                    # Only text touching the number names the account; a column beside it is usually the
                    # holder's name or address, which must not become the account's name.
                    rest = _adjacent(line[: match.start()], line[match.end():])
                    if declared:
                        rest = rest.replace(declared.group(0).strip(), " ")
                    label = " ".join(rest.strip(" -:|").split()) or None
                    kind, confidence = account_type(f"{label or ''} {match.group('label')}")
                    if label and not _is_amount(label):
                        current.label = label[:80]
                    if kind:
                        current.type, current.type_confidence = kind, confidence
                    continue
            type_match = _ACCOUNT_TYPE.search(line)
            if type_match:
                kind, confidence = account_type(type_match.group("t"))
                if kind:
                    current.type, current.type_confidence = kind, confidence
                current.label = current.label or " ".join(type_match.group("t").split())[:80]
                continue
            udi = _UDI.search(line)
            if udi:
                fx.append({"from": "UDI", "to": "MXN", "rate": udi.group("r").replace(",", "."), "page": page})
                continue
            fx_match = _FX_EQ.search(line) or _FX.search(_without_dates(line))
            if fx_match:
                source = fx_match.groupdict().get("a") or "USD"
                target = fx_match.groupdict().get("b") or ("MXN" if language == "es" or currency == "MXN" else None)
                if target and source != target:
                    fx.append({"from": source, "to": target, "rate": fx_match.group("r").replace(",", "."), "page": page})
                continue
            declared = _CURRENCY_DECL.search(line)
            if declared and not has_amount:
                code = _currency_word(declared.group("c"))
                if code and code != "UDI":
                    if columns is None and not current.positions and current.currency is None:
                        current.currency = code
                    section_currency = code
                if len(cells) <= 2:
                    section = stripped
                    heading(stripped)
                continue

            tx_header = _tx_header(line)
            header = None if tx_header else _header(line, index)
            if tx_header is None and header is None and number + 1 < len(lines) and len(cells) >= 2 and not has_amount:
                candidate = _merge_header(line, lines[number + 1])
                tx_header = _tx_header(candidate)
                header = None if tx_header else _header(candidate, index)
                skip_next = bool(tx_header or header)
            if tx_header:
                tx_columns, columns, last_tx, last_row = tx_header, None, None, None
                continue
            if header:
                columns, tx_columns, last_row, last_tx = header, None, None, None
                if new_section or not current.positions:
                    current.section_start = len(current.positions)
                new_section = False
                continue

            first = fold(cells[0][0]) if cells else ""
            amount = _line_amount(cells, columns)
            if amount is not None and _ACCOUNT_TOTAL.match(first):
                printed = [d.isoformat() for d, _ in find_dates(line, day_first=day_first)]
                if printed and as_of and as_of not in printed:
                    continue  # "Valor del portafolio al 31/07/2026": the opening value, not this statement's total
                current.totals.append({"amount": amount, "currency": _row_currency(line, None, None),
                                       "label": cells[0][0][:60], "page": page})
                columns = tx_columns = None
                continue
            if amount is not None and _POSITIONS_SUBTOTAL.match(first):
                # A statement prints one subtotal per section (SIC, BMV, ...): each covers the rows since the
                # section's header or the previous subtotal.
                current.subtotals.append({"amount": amount, "label": cells[0][0][:60], "page": page,
                                          "rows": [current.section_start, len(current.positions)]})
                current.section_start = len(current.positions)
                continue
            dated_row = tx_columns is not None and bool(cells) and resolve_date(
                cells[0][0], period_end, day_first=day_first) is not None
            if _DUE_DATE.search(line) and not dated_row:
                due = [d for d, _ in find_dates(line, day_first=day_first)]
                if due:
                    current.card.setdefault("due_date", due[-1].isoformat())
            cat = _CAT.search(line)
            if cat:
                current.card.setdefault("cat", cat.group("r").replace(",", ".") + "%")
            rate = _RATE.search(line)
            if rate:
                # The ordinary annual rate; a monthly, penalty (moratoria) or average rate printed nearby is not it.
                rank = -1 if _RATE_OTHER.search(line[rate.start():rate.end()]) else (
                    1 if re.search(r"(?i)\b(anual|annual|apr|ordinaria)\b", line) else 0)
                if rank > current.rate_rank:
                    current.interest_rate, current.rate_rank = rate.group("r") + "%", rank
            summary = [] if dated_row else _summary_figures(_pairs(line))
            if summary:
                for name, value in summary:
                    if name in ("opening", "deposits", "withdrawals", "closing", "total_debt"):
                        current.flows.setdefault(name, value)
                        current.flows.setdefault("page", page)
                    elif name == "minimum_payment":
                        current.min_payment = current.min_payment or value
                    else:
                        current.card.setdefault(name, value)
                continue
            if amount is not None and not dated_row:  # amounts _pairs does not read (1.234,56; no decimals)
                flow = next((key for key, pattern in (("opening", _OPENING), ("deposits", _DEPOSITS),
                             ("withdrawals", _WITHDRAWALS), ("closing", _CLOSING)) if pattern.match(first)), None)
                if flow:
                    current.flows.setdefault(flow, amount)
                    current.flows.setdefault("page", page)
                    continue
                if _MIN_PAYMENT.match(first):
                    current.min_payment = current.min_payment or amount
                    continue
            if amount is not None and _TOTAL_ANY.match(first):
                continue

            if tx_columns is not None:
                when = resolve_date(cells[0][0], period_end, day_first=day_first) if cells else None
                if when:
                    rest = cells[1:]
                    settlement = None
                    if rest:  # a second leading date is the posting (liquidación) date, never the description
                        settlement = resolve_date(rest[0][0], period_end, day_first=day_first)
                        if settlement:
                            rest = rest[1:]
                    fields = [c for c in tx_columns if c["field"] not in ("date", "settlement_date")]
                    row = _assign(rest, fields, _TX_NUMERIC) if fields else {}
                    description = row.get("description") or row.get("type") or ""
                    folded_description = fold(description)
                    marker = row.get("balance") or row.get("amount") or row.get("credit") or row.get("debit")
                    if _OPENING.match(folded_description) and marker:
                        current.flows.setdefault("opening", marker)
                        current.flows.setdefault("page", page)
                        continue
                    if _CLOSING.match(folded_description) and marker:
                        current.flows.setdefault("closing", marker)
                        current.flows.setdefault("page", page)
                        continue
                    tx: dict[str, Any] = {
                        "date": when, "settlement_date": settlement, "description": description,
                        "debit": row.get("debit"), "credit": row.get("credit"), "amount": row.get("amount"),
                        "balance": row.get("balance"), "symbol": row.get("symbol"), "quantity": row.get("quantity"),
                        "price": row.get("price"), "fees": row.get("fees"), "type": row.get("type"), "page": page,
                        "currency": _row_currency(line, section_currency or current.currency, None),
                    }
                    if row.get("fees_tax"):
                        tx["fees_tax"] = row["fees_tax"]
                    section_key = fold(section)
                    if _IN_SECTION.match(section_key):
                        tx["direction"] = "in"
                    elif _OUT_SECTION.match(section_key):
                        tx["direction"] = "out"
                    if _MSI_SECTION.search(section_key) or row.get("original_amount") or row.get("remaining_balance"):
                        number_of = installment(row.get("installment_number") or line)
                        tx["installment"] = {
                            "number": number_of[0] if number_of else None, "count": number_of[1] if number_of else None,
                            "original_amount": row.get("original_amount"), "remaining_balance": row.get("remaining_balance"),
                        }
                        tx["amount"] = row.get("installment_payment") or row.get("amount")
                        tx["direction"], tx["type_override"] = "out", "expense"
                    current.transactions.append(tx)
                    last_tx = tx
                    continue
                if last_tx is not None and not has_amount and len(cells) == 1 and _continues(cells[0], tx_columns):
                    last_tx["description"] = f"{last_tx['description']} {stripped}".strip()
                    continue

            if columns is not None:
                numeric_cells = [c for c in cells if _is_amount(c[0])]
                if numeric_cells and len(cells) >= 2:
                    row = _assign(cells, columns)
                    if "value" not in row and not ({"quantity", "price"} <= row.keys()):
                        if CASH_LABEL.match(stripped) and amount is not None:
                            current.cash.append({"amount": amount, "currency": _row_currency(line, section_currency or current.currency, columns),
                                                 "label": cells[0][0][:60], "page": page})
                        continue
                    symbol = row.get("symbol")
                    if symbol and row.get("serie"):
                        symbol = f"{symbol} {row['serie']}"
                    description = row.get("description")
                    if not symbol and description:
                        ticker = _TICKER_IN_DESC.search(description)
                        if ticker:
                            symbol = ticker.group(1)
                    if not symbol and not description:
                        continue
                    last_row = {
                        "symbol": symbol, "description": description,
                        "quantity": row.get("quantity"), "price": row.get("price"), "market_value": row.get("value"),
                        "cost_basis": row.get("cost_basis"), "avg_cost": row.get("avg_cost"), "gain": row.get("gain"),
                        "currency": _currency_word(row["currency"]) if row.get("currency") else _row_currency(line, section_currency or current.currency, columns),
                        "asset_type": row.get("asset_type"), "page": page, "section": section,
                        "currency_explicit": bool(row.get("currency") or _row_currency(line, None, columns)),
                    }
                    current.positions.append(last_row)
                    continue
                if last_row is not None and not numeric_cells and len(cells) == 1 and _continues(cells[0], columns):
                    if not CASH_LABEL.match(stripped) and not _currency_word(stripped) and not _TOTAL_ANY.match(fold(stripped)):
                        last_row["description"] = f"{last_row['description'] or ''} {stripped}".strip()
                        continue
            if amount is not None and CASH_LABEL.match(stripped):
                current.cash.append({"amount": amount, "currency": _row_currency(line, section_currency or current.currency, columns),
                                     "label": cells[0][0][:60], "page": page})
                continue
            if not has_amount and len(stripped) <= 70 and len(cells) <= 3:
                section = stripped
                heading(stripped)
                if re.search(r"(?i)\b(d[oó]lares|usd|dls)\b", stripped):
                    section_currency = "USD"
                elif re.search(r"(?i)\b(pesos|moneda nacional|mxn)\b", stripped):
                    section_currency = "MXN"
                last_row = None
    if current.has_content() or current.number_last4:
        accounts.append(current)

    statement_accounts = []
    review: list[str] = []
    confidence: dict[str, str] = {"as_of": as_of_confidence, "currency": currency_confidence}
    for position, account in enumerate(accounts, 1):
        if not account.has_content():
            continue
        if account.type is None and document_type and (not account.positions or document_type == "brokerage"):
            account.type, account.type_confidence = document_type, "medium"
        reported = None
        if account.totals:
            values = {t["amount"] for t in account.totals}
            reported = account.totals[-1]
            if len(values) > 1:
                notes.append(f"Account ending {account.number_last4 or '?'} prints different totals {sorted(values)}; the last one was used.")
        if account.type not in {"credit_card", "mortgage"}:
            unsigned = _orient_transactions(account, comma, notes)
            if unsigned:
                # Money in or out cannot be told for these lines: never guess the sign silently.
                review.append(f"{unsigned} movement(s) in account ending {account.number_last4 or '?'} print an "
                              "amount without a sign, balance or concept that says whether money came in or went out; "
                              "confirm their direction.")
        entry: dict[str, Any] = {
            "label": account.label, "number_last4": account.number_last4, "type": account.type,
            "currency": account.currency, "page": account.page, "positions": account.positions,
            "cash": _dedupe_cash(account), "reported_total": reported, "positions_subtotals": account.subtotals,
            "flows": {k: v for k, v in account.flows.items() if k != "total_debt"} or None, "liabilities": [],
            "transactions": account.transactions, "period_start": period_start, "period_end": as_of,
        }
        if account.type in {"credit_card", "mortgage"}:
            balance = account.flows.get("closing") or (reported or {}).get("amount")
            debt_flows = {k: v for k, v in account.flows.items() if k != "total_debt"}
            entry.update(positions=[], cash=[], reported_total=None, positions_subtotals=[], flows=None,
                         debt_flows=debt_flows or None)
            main = {"label": account.label or account.type.replace("_", " "), "balance": balance,
                    "currency": account.currency, "minimum_payment": account.min_payment,
                    "interest_rate": account.interest_rate, "page": account.page,
                    **{k: v for k, v in account.card.items() if k in ("no_interest_payment", "cat", "credit_limit",
                                                                      "due_date")}}
            entry["liabilities"] = [main, *_deferred_installments(account, balance, notes,
                                                                  account.currency or currency)]
        elif account.interest_rate:
            entry["interest_rate"] = account.interest_rate
        statement_accounts.append(entry)
        if account.type is None:
            confidence[f"accounts.{account.number_last4 or position}.type"] = "low"
    parsed = any(a["positions"] or a["cash"] or a["flows"] or a["liabilities"] or a["transactions"] for a in statement_accounts)
    return {
        "statement": {
            "institution": institution, "institution_key": institution_key, "language": language,
            "as_of": as_of, "period_start": period_start, "currency": currency, "decimal_comma": comma,
            "market": "mx" if (language == "es" or institution_key in _MX_INSTITUTIONS) else "us" if institution_key in _US_INSTITUTIONS else None,
            "accounts": statement_accounts, "fx": _dedupe_fx(fx), "as_of_page": as_of_page,
            "review_reasons": review,
        },
        "confidence": confidence, "notes": notes, "parsed": parsed,
    }


_OUT_WORDS = re.compile(r"(?i)\b(compra|cpa|retenci[oó]n|isr|comisi[oó]n|iva|retiro|cargo|buy|bought|purchase|"
                        r"withdrawal|fee|tax)\b")
_IN_WORDS = re.compile(r"(?i)\b(venta|vta|dividendo|distribuci[oó]n|inter[eé]s(es)?|dep[oó]sito|abono|sell|sold|"
                       r"sale|dividend|interest|deposit|rendimiento)\b")
_MAX_UNSIGNED_RUN = 4  # rows between two printed balances whose signs are solved together


def _signed_options(tx: dict[str, Any], comma: bool | None) -> list[Decimal]:
    """What an unsigned "Importe" can mean for cash: in or out, and with commission and IVA added or netted."""
    amount = parse_amount(tx.get("amount"), decimal_comma=comma)
    fees = sum((abs(v) for v in (parse_amount(tx.get(k), decimal_comma=comma) for k in ("fees", "fees_tax"))
                if v is not None), Decimal(0))
    options = [amount, -amount]
    if fees:
        options += [-(amount + fees), amount - fees]
    return options


def _orient_transactions(account: _Account, comma: bool | None, notes: list[str]) -> int:
    """Sign an unsigned amount column ("Importe" printed without +/-) from the printed running balance.

    A brokerage "Importe" is printed unsigned: a purchase, a retention and a deposit all look positive.
    Consecutive printed balances fix each line's direction (and whether commission and IVA were added);
    lines the balances cannot settle take the direction their concept names (compra, retención ... out;
    depósito, dividendo, venta ... in).  A column that prints any negative amount is already signed.
    """
    rows = [t for t in account.transactions if not t.get("installment")]
    unsigned = [t for t in rows if t.get("amount") not in (None, "") and not t.get("debit") and not t.get("credit")
                and not t.get("direction")]
    values = [parse_amount(t["amount"], decimal_comma=comma) for t in unsigned]
    if not unsigned or any(v is None or v < 0 or str(t["amount"]).strip().startswith(("(", "-"))
                           for v, t in zip(values, unsigned)):
        return 0
    targets = {id(t) for t in unsigned}
    previous = parse_amount(account.flows.get("opening"), decimal_comma=comma)
    pending: list[dict[str, Any]] = []
    solved = 0
    for tx in rows:
        if id(tx) not in targets:
            previous, pending = None, []
            continue
        pending.append(tx)
        balance = parse_amount(tx.get("balance"), decimal_comma=comma)
        if balance is None:
            continue
        if previous is not None and len(pending) <= _MAX_UNSIGNED_RUN:
            options = [_signed_options(t, comma) for t in pending]
            matches = {combo for combo in _combos(options) if abs(previous + sum(combo) - balance) <= Decimal("0.01")}
            if len(matches) == 1:
                for t, value in zip(pending, matches.pop()):
                    t["amount"], t["signed_by"] = format(value, "f"), "balance"
                    solved += 1
        previous, pending = balance, []
    guessed = 0
    for tx in unsigned:
        if tx.get("signed_by"):
            continue
        text = f"{tx.get('type') or ''} {tx.get('description') or ''}"
        out_hit, in_hit = bool(_OUT_WORDS.search(text)), bool(_IN_WORDS.search(text))
        if re.search(r"(?i)\b(retenci[oó]n|isr|withholding|tax withheld)\b", text):
            out_hit, in_hit = True, False  # "Retención ISR dividendos" is money out, whatever it names
        if out_hit == in_hit:
            continue
        amount = parse_amount(tx["amount"], decimal_comma=comma)
        fees = sum((abs(v) for v in (parse_amount(tx.get(k), decimal_comma=comma) for k in ("fees", "fees_tax"))
                    if v is not None), Decimal(0))
        value = -(amount + fees) if out_hit else amount - fees
        tx["amount"], tx["signed_by"] = format(value, "f"), "concept"
        guessed += 1
    if solved:
        notes.append(f"{solved} unsigned amount(s) were signed from the printed running balance.")
    if guessed:
        notes.append(f"{guessed} unsigned amount(s) were signed from their concept (no running balance settled them).")
    return sum(1 for tx in unsigned if not tx.get("signed_by"))


def _combos(options: list[list[Decimal]]):
    if not options:
        yield ()
        return
    for head in dict.fromkeys(options[0]):
        for tail in _combos(options[1:]):
            yield (head, *tail)


def _deferred_installments(account: _Account, balance: Any, notes: list[str],
                           currency: str | None) -> list[dict[str, Any]]:
    """Purchases at meses sin intereses owed beyond the revolving balance, as their own 0% debt.

    "Saldo al corte" leaves out the installments still to be charged; "saldo deudor total" includes them.
    Their difference is what the person still owes on the plans (else the printed pending balances).
    """
    plans = [t for t in account.transactions if t.get("installment")]
    total = parse_amount(account.flows.get("total_debt"))
    closing = parse_amount(balance)
    payments = [parse_amount(t.get("amount")) for t in plans]
    deferred = None
    if total is not None and closing is not None and total - closing > Decimal("0.005"):
        deferred = total - closing
    elif plans and all(parse_amount((t["installment"] or {}).get("remaining_balance")) is not None for t in plans):
        deferred = sum(abs(parse_amount(t["installment"]["remaining_balance"])) for t in plans)
        notes.append("The installment-plan debt is the sum of the printed pending balances; check whether this "
                     "month's installment is included.")
    if deferred is None:
        return []
    return [{"label": "Compras a meses sin intereses" if currency == "MXN" else "Installment plans",
             "balance": format(deferred, "f"), "currency": account.currency or currency,
             "minimum_payment": format(sum((abs(p) for p in payments if p is not None), Decimal(0)), "f")
             if any(p is not None for p in payments) else None,
             "interest_rate": "0%", "page": account.page, "kind": "installments"}]


def _dedupe_cash(account: _Account) -> list[dict[str, Any]]:
    in_table = {(parse_amount(p.get("market_value")), p.get("currency")) for p in account.positions
                if CASH_LABEL.match(p.get("symbol") or "") or CASH_LABEL.match(p.get("description") or "")}
    seen, result = set(), []
    for cash in account.cash:
        key = (parse_amount(cash["amount"]), cash.get("currency"))
        if key in seen or key in in_table:
            continue
        seen.add(key)
        result.append(cash)
    return result


def _dedupe_fx(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: dict[tuple[str, str], dict[str, Any]] = {}
    for item in items:
        result.setdefault((item["from"], item["to"]), item)
    return list(result.values())
