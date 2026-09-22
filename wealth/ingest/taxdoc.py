"""Annual tax documents: Mexican constancias, US 1099 composites and Form 5498.

The pipeline mirrors statements: detect the document from its text, read it
with a deterministic parser where the layout is regular (label or box lines,
the 1099-B lot table), otherwise hand the host model an extraction request
with :data:`TAX_EXTRACTION_SCHEMA`.  Either way every figure is checked
against the page text, totals are reconciled (1099-B lots against the printed
term totals, the Art. 129 net against gain and loss, real interest against
the inflation adjustment) and the result is a proposal.  Nothing is saved
here: after the person's yes :func:`tax_proposal_to_facts` turns it into
``constancia.<id>`` facts with provenance ``document`` whose figures are
exactly the proposal's, and ``tax_pack`` reads them as the source of truth.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
import re
import unicodedata
from typing import Any, Iterable

from .common import digest, envelope, find_dates, fold, number_tokens, parse_amount, slug
from .redact import last4, redact_text

TYPES = ("mx_constancia", "us_1099", "us_5498")
LABELS = {"mx_constancia": "Constancia fiscal anual", "us_1099": "Form 1099 composite", "us_5498": "Form 5498"}

# Every field a block can carry; SAVED (from the fact schema) is what a constancia fact keeps.
BLOCK_FIELDS: dict[str, tuple[str, ...]] = {
    "enajenacion": ("gain", "loss", "net", "isr_withheld"),
    "intereses": ("nominal", "inflation_adjustment", "real", "real_loss", "isr_withheld"),
    "dividendos": ("domestic_gross", "foreign_gross", "isr_withheld", "isr_creditable", "foreign_tax_withheld",
                   "total"),
    "form_1099_b": ("proceeds", "cost_basis", "short_term_gain", "long_term_gain", "wash_sale_disallowed"),
    "form_1099_div": ("ordinary", "qualified", "capital_gain_distributions", "foreign_tax_paid",
                      "federal_tax_withheld"),
    "form_1099_int": ("interest", "us_treasury_interest", "foreign_tax_paid", "federal_tax_withheld"),
    "form_5498": ("ira_contributions", "rollover_contributions", "roth_conversion", "recharacterized",
                  "fair_market_value", "sep_contributions", "simple_contributions", "roth_contributions",
                  "rmd_next_year"),
}
_CURRENCY = {"mx_constancia": "MXN", "us_1099": "USD", "us_5498": "USD"}
_KEY_SUFFIX = {"mx_constancia": "", "us_1099": "_1099", "us_5498": "_5498"}
TOLERANCE = Decimal("0.01")

_INSTITUTIONS = tuple((key, name, re.compile(pattern, re.IGNORECASE)) for key, name, pattern in (
    ("gbm", "GBM", r"\bGBM\b|grupo burs[aá]til mexicano"),
    ("actinver", "Actinver", r"\bactinver\b"),
    ("banorte", "Banorte", r"\bbanorte\b"),
    ("kuspit", "Kuspit", r"\bkuspit\b"),
    ("cetesdirecto", "Cetesdirecto", r"\bcetes\s?directo\b"),
    ("bbva", "BBVA", r"\bbbva\b|\bbancomer\b"),
    ("santander", "Santander", r"\bsantander\b"),
    ("nu", "Nu México", r"\bnu\s+m[eé]xico\b|\bnubank\b|\bnu\s+bank\b"),
    ("schwab", "Charles Schwab", r"\bschwab\b"),
    ("fidelity", "Fidelity", r"\bfidelity\b"),
    ("vanguard", "Vanguard", r"\bvanguard\b"),
    ("alpaca", "Alpaca", r"\balpaca\b"),
))

# ----------------------------------------------------------------- detection


def _plain(text: Any) -> str:
    """Lower case without accents, keeping punctuation (dates, box numbers and "1099-DIV" stay intact)."""
    decomposed = unicodedata.normalize("NFKD", str(text or ""))
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch)).lower()


def _title(pages: list[tuple[int, str]], lines: int = 5) -> str:
    first = next((text for _, text in pages if text.strip()), "")
    return _plain("\n".join([line for line in first.splitlines() if line.strip()][:lines]))


def _institution(text: str) -> tuple[str | None, str | None]:
    head = text[:4000]
    best = None
    for key, name, pattern in _INSTITUTIONS:
        match = pattern.search(head)
        if match and (best is None or match.start() < best[0]):
            best = (match.start(), key, name)
    return (best[1], best[2]) if best else (None, None)


_YEAR_PATTERNS = tuple(re.compile(p) for p in (
    r"\bejercicio(?:\s+fiscal)?\s*:?\s*(20\d{2})\b",
    r"\bano(?:\s+(?:fiscal|gravable))?\s*:?\s*(20\d{2})\b",
    r"\btax\s+year\s*:?\s*(20\d{2})\b",
    r"\b(20\d{2})\s+(?:form\s+)?(?:1099|5498)\b",
    r"\bperiodo\b[^\n]*?\b31/12/(20\d{2})\b",
))
_ISSUED = re.compile(r"(?:fecha\s+de\s+(?:emision|expedicion)|date\s+(?:prepared|issued)|issued\s+on|prepared\s+on)"
                     r"\s*:?\s*(.+)$", re.MULTILINE)
_ACCOUNT = re.compile(r"(?:contrato|cuenta|account(?:\s+(?:number|no\.?))?)\s*(?:no\.?\s*)?[:#]\s*([\dxX*\- ]{4,24})")


def detect_tax_document(pages: list[tuple[int, str]]) -> dict[str, Any] | None:
    """The tax document type, institution and year read from the text, or None for anything else.

    Only the title lines of the first page decide the type, so a statement that
    merely mentions "your 1099 will be mailed" is not taken for a tax form.
    """
    title = _title(pages)
    if re.search(r"brokerage\s+statement|account\s+statement|statement\s+period|estado\s+de\s+cuenta", title):
        return None  # a statement that mentions the tax forms, not one of them
    if re.search(r"\b5498\b", title):
        kind = "us_5498"
    elif re.search(r"\b1099\b", title):
        kind = "us_1099"
    elif re.search(r"\bconstancia\b", title) and re.search(
            r"retencion|interes|enajenacion|dividendo|anual|fiscal|ejercicio", title):
        kind = "mx_constancia"
    else:
        return None
    text = "\n".join(t for _, t in pages)
    folded = _plain(text)
    key, name = _institution(text)
    year = None
    for pattern in _YEAR_PATTERNS:
        match = pattern.search(folded)
        if match:
            year = int(match.group(1))
            break
    issued = None
    match = _ISSUED.search(folded)
    if match:
        found = find_dates(match.group(1), day_first=kind == "mx_constancia")
        issued = found[0][0].isoformat() if found else None
    account = _ACCOUNT.search(folded)
    return {"document_type": kind, "institution_key": key, "institution": name, "tax_year": year,
            "issued_on": issued, "account_last4": last4(account.group(1)) if account else None,
            "currency": _CURRENCY[kind]}


# ------------------------------------------------------------ line parsing

_MONEY = re.compile(r"(?<![\w.,/$])(?:\(\s*)?[-−–]?\$?\s?(?:\d{1,3}(?:,\d{3})+|\d+)\.\d{2}(?:\s*\))?(?![\w/%]|[.,]\d)")
_DATE_TOKEN = re.compile(r"\b(?:\d{1,2}/\d{1,2}/(?:\d{4}|\d{2})|\d{4}-\d{2}-\d{2}|various|varios)\b", re.IGNORECASE)
_SECTION_MX = (("dividendos", re.compile(r"\bdividendos?\b")),
               ("intereses", re.compile(r"\bintereses?\b")),
               ("enajenacion", re.compile(r"enajenacion|\bacciones\b|art(?:iculo|\.)?\s*129")))
_SECTION_US = re.compile(r"\b1099-(b|div|int|misc|oid)\b")
_BLOCK_OF_US = {"b": "form_1099_b", "div": "form_1099_div", "int": "form_1099_int"}

_FIELDS_MX = {
    "enajenacion": (("net", r"\bnet[oa]\b|\bresultado\b"), ("loss", r"\bperdida"), ("gain", r"\bganancia|\butilidad"),
                    ("isr_withheld", r"\bisr\b|retenci|impuesto")),
    "intereses": (("real_loss", r"perdida\s+real|reales?\s+negativ"), ("real", r"\breal(?:es)?\b"),
                  ("inflation_adjustment", r"ajuste\b.*inflaci|componente\s+inflacionario"),
                  ("isr_withheld", r"\bisr\b|retenci|impuesto"),
                  ("nominal", r"\bnominal(?:es)?\b|^intereses?(?:\s+(?:totales|pagados|devengados|cobrados|ganados|"
                              r"generados|del\s+ejercicio))*$")),
    "dividendos": (("total", r"^total\b"), ("foreign_tax_withheld", r"(?:impuesto|isr)\b.*\bextranjer"),
                   ("foreign_gross", r"extranjer|del\s+exterior"),
                   ("isr_creditable", r"acreditable|cufin|pagado\s+por\s+la\s+(?:persona\s+)?moral"),
                   ("isr_withheld", r"\bisr\b|retenci|impuesto"),
                   ("domestic_gross", r"nacional|mexic|personas\s+morales|^dividendos?$")),
}
_BOXES = {
    "form_1099_div": {"1a": "ordinary", "1b": "qualified", "2a": "capital_gain_distributions", "4": "federal_tax_withheld",
                      "7": "foreign_tax_paid"},
    "form_1099_int": {"1": "interest", "3": "us_treasury_interest", "4": "federal_tax_withheld", "6": "foreign_tax_paid"},
    "form_5498": {"1": "ira_contributions", "2": "rollover_contributions", "3": "roth_conversion",
                  "4": "recharacterized", "5": "fair_market_value", "8": "sep_contributions",
                  "9": "simple_contributions", "10": "roth_contributions", "12b": "rmd_next_year"},
}
_WORDS_US = {
    "form_1099_div": (("qualified", r"qualified\s+dividends"), ("ordinary", r"ordinary\s+dividends"),
                      ("capital_gain_distributions", r"capital\s+gain\s+distr"),
                      ("foreign_tax_paid", r"foreign\s+tax\s+paid"), ("federal_tax_withheld", r"federal\s+income\s+tax")),
    "form_1099_int": (("us_treasury_interest", r"savings\s+bonds|treasury\s+obligations"),
                      ("foreign_tax_paid", r"foreign\s+tax\s+paid"), ("federal_tax_withheld", r"federal\s+income\s+tax"),
                      ("interest", r"^interest\s+income")),
    "form_5498": (("roth_conversion", r"roth\s+ira\s+conversion"), ("roth_contributions", r"roth\s+ira\s+contributions"),
                  ("rollover_contributions", r"rollover"), ("recharacterized", r"recharacteriz"),
                  ("fair_market_value", r"fair\s+market\s+value"), ("sep_contributions", r"\bsep\s+contrib"),
                  ("simple_contributions", r"simple\s+contrib"), ("rmd_next_year", r"\brmd\s+amount"),
                  ("ira_contributions", r"ira\s+contributions")),
}
_BOX = re.compile(r"^(?:box\s+)?(\d{1,2}[a-h]?)\b\s*")


def _segments(line: str) -> list[tuple[str, str]]:
    """(label, printed amount) pairs: each amount with the text since the previous one."""
    out, start = [], 0
    for match in _MONEY.finditer(line):
        label = line[start:match.start()]
        out.append((label, match.group(0).strip()))
        start = match.end()
    return out


def _clean_label(text: str) -> str:
    return " ".join(_plain(text).replace("$", " ").split()).strip(" .:·…-_|()")


def _match_field(label: str, rules: Iterable[tuple[str, str]]) -> str | None:
    for name, pattern in rules:
        if re.search(pattern, label):
            return name
    return None


def _put(doc: dict, block: str, field: str, raw: str, page: int) -> None:
    fields = doc["blocks"].setdefault(block, {})
    if field in fields:
        if parse_amount(fields[field]) != parse_amount(raw):
            doc["conflicts"].append(f"{block}.{field} is printed twice with different values "
                                    f"({fields[field]} and {raw})")
        return
    fields[field] = raw
    doc["pages"][f"{block}.{field}"] = page


def _parse_mx(pages: list[tuple[int, str]], doc: dict) -> None:
    section = None
    for number, text in pages:
        for line in text.splitlines():
            segments = _segments(line)
            label_all = _clean_label(line)
            if not segments:
                for name, pattern in _SECTION_MX:
                    if pattern.search(label_all):
                        section = name
                        break
                continue
            for raw_label, raw in segments:
                label = _clean_label(raw_label)
                if len(re.sub(r"[^a-z]", "", label)) < 3:
                    continue
                block = next((name for name, pattern in _SECTION_MX if pattern.search(label)), None) or section
                if block is None:
                    continue
                field = _match_field(label, _FIELDS_MX[block])
                if field:
                    _put(doc, block, field, raw, number)


def _parse_us_boxes(block: str, label: str, raw: str, number: int, doc: dict) -> None:
    box = _BOX.match(label)
    field = _BOXES[block].get(box.group(1)) if box else None
    if field is None:
        field = _match_field(_BOX.sub("", label), _WORDS_US[block])
    if field:
        _put(doc, block, field, raw, number)


def _term_of(label: str) -> str | None:
    if re.search(r"short[\s-]*term|\bbox\s+[abc]\b|corto\s+plazo", label):
        return "short"
    if re.search(r"long[\s-]*term|\bbox\s+[def]\b|largo\s+plazo", label):
        return "long"
    return None


def _lot(line: str, term: str | None, number: int) -> dict | None:
    dates = list(_DATE_TOKEN.finditer(line))
    if len(dates) < 2:
        return None
    amounts = [m.group(0).strip() for m in _MONEY.finditer(line, dates[-1].end())]
    if len(amounts) not in (3, 4):
        return None
    before = line[:dates[-2].start()].split()
    quantity = before.pop() if before and re.fullmatch(r"[\d,]*\.?\d+", before[-1]) else None
    symbol = before.pop() if before and re.fullmatch(r"[A-Z][A-Z.]{0,5}|[0-9A-Z]{9}", before[-1]) else None
    lot = {"description": redact_text(" ".join(before))[:120] or None, "symbol": symbol, "quantity": quantity,
           "date_acquired": dates[-2].group(0), "date_sold": dates[-1].group(0),
           "proceeds": amounts[0], "cost_basis": amounts[1],
           "wash_sale_disallowed": amounts[2] if len(amounts) == 4 else None, "gain": amounts[-1],
           "term": term, "page": number}
    return lot


def _total(label: str, line: str, term: str | None, number: int) -> dict | None:
    amounts = [m.group(0).strip() for m in _MONEY.finditer(line)]
    if len(amounts) not in (3, 4):
        return None
    return {"term": _term_of(label) or ("all" if term is None or re.search(r"1099|grand|overall", label) else term),
            "proceeds": amounts[0], "cost_basis": amounts[1],
            "wash_sale_disallowed": amounts[2] if len(amounts) == 4 else None, "gain": amounts[-1], "page": number}


def _parse_us(pages: list[tuple[int, str]], doc: dict) -> None:
    kind = doc["document_type"]
    section = "form_5498" if kind == "us_5498" else None
    term = None
    for number, text in pages:
        for line in text.splitlines():
            folded = _clean_label(line)
            segments = _segments(line)
            if not segments:
                marker = _SECTION_US.search(folded) if kind == "us_1099" else None
                if marker:
                    section = _BLOCK_OF_US.get(marker.group(1))  # MISC/OID are not read: None
                    term = None
                if section == "form_1099_b":
                    term = _term_of(folded) or term
                continue
            if section == "form_1099_b":
                if folded.startswith("total"):
                    total = _total(folded, line, term, number)
                    if total:
                        doc["totals"].append(total)
                    continue
                lot = _lot(line, term, number)
                if lot:
                    doc["lots"].append(lot)
                continue
            if section in _BOXES:
                for raw_label, raw in segments:
                    label = _clean_label(raw_label)
                    if re.search(r"[a-z]{3}", label):
                        _parse_us_boxes(section, label, raw, number, doc)


def parse_tax_document(pages: list[tuple[int, str]], detected: dict[str, Any]) -> dict[str, Any]:
    """The printed figures of a detected tax document (raw strings, as printed) and where they are."""
    doc = {**detected, "blocks": {}, "lots": [], "totals": [], "pages": {}, "conflicts": [], "notes": []}
    if detected["document_type"] == "mx_constancia":
        _parse_mx(pages, doc)
    else:
        _parse_us(pages, doc)
    if doc["lots"] or doc["totals"]:
        doc["blocks"].setdefault("form_1099_b", {})
    return doc


# ------------------------------------------------------------- validation


def _d(raw: Any) -> Decimal | None:
    return parse_amount(raw) if raw not in (None, "") else None


def _s(value: Decimal | None) -> str | None:
    return None if value is None else format(value.quantize(Decimal("0.01")), "f")


def _verify(doc: dict, text: str) -> dict[str, Any]:
    """Every printed figure, date, symbol and the tax year must appear in the source text."""
    tokens = number_tokens(text)
    compact = re.sub(r"\s+", "", text)
    folded = _plain(text)
    unverified, checked = [], 0

    def number(path: str, raw: Any) -> None:
        nonlocal checked
        if raw in (None, ""):
            return
        checked += 1
        amount = parse_amount(str(raw))
        if amount is None:
            unverified.append({"path": path, "value": raw, "reason": "not a printed amount"})
        elif re.sub(r"\s+", "", str(raw)) not in compact and amount not in tokens and abs(amount) not in tokens:
            unverified.append({"path": path, "value": raw, "reason": "number does not appear in the source text"})

    for block, fields in doc["blocks"].items():
        for field, raw in fields.items():
            number(f"{block}.{field}", raw)
    for index, lot in enumerate(doc["lots"]):
        for field in ("proceeds", "cost_basis", "wash_sale_disallowed", "gain", "quantity"):
            number(f"lots[{index}].{field}", lot.get(field))
        for field in ("date_acquired", "date_sold"):
            value = lot.get(field)
            if value and not re.fullmatch(r"(?i)various|varios", value):
                checked += 1
                if not _date_printed(value, text):
                    unverified.append({"path": f"lots[{index}].{field}", "value": value,
                                       "reason": "date does not appear in the source text"})
        if lot.get("symbol"):
            checked += 1
            if fold(lot["symbol"]).upper() not in folded.upper():
                unverified.append({"path": f"lots[{index}].symbol", "value": lot["symbol"],
                                   "reason": "symbol does not appear in the source text"})
    for index, total in enumerate(doc["totals"]):
        for field in ("proceeds", "cost_basis", "wash_sale_disallowed", "gain"):
            number(f"totals[{index}].{field}", total.get(field))
    if doc.get("tax_year") is not None:
        checked += 1
        if str(doc["tax_year"]) not in text:
            unverified.append({"path": "tax_year", "value": doc["tax_year"], "reason": "year does not appear in the source text"})
    if doc.get("issued_on"):
        checked += 1
        if not _date_printed(doc["issued_on"], text):
            unverified.append({"path": "issued_on", "value": doc["issued_on"],
                               "reason": "date does not appear in the source text"})
    if doc.get("account_last4"):
        checked += 1
        if doc["account_last4"] not in text:
            unverified.append({"path": "account_last4", "value": doc["account_last4"],
                               "reason": "account suffix does not appear in the source text"})
    return {"source_text": True, "checked": checked, "unverified": unverified}


def _to_date(value: str) -> date | None:
    try:
        return date.fromisoformat(value)
    except ValueError:
        found = find_dates(value)
        return found[0][0] if found else None


def _date_printed(value: str, text: str) -> bool:
    wanted = _to_date(value)
    if wanted is None:
        return False
    printed = {d for first in (True, False) for d, _ in find_dates(text, day_first=first)}
    return wanted in printed


def _checks(doc: dict) -> tuple[list[dict], list[str], dict[str, dict[str, str]], list[str]]:
    """Arithmetic checks, review reasons, the figures (normalised) and what was derived rather than printed."""
    checks: list[dict] = []
    reasons: list[str] = list(doc["conflicts"])
    derived: list[str] = []
    figures: dict[str, dict[str, str]] = {}
    for block, fields in doc["blocks"].items():
        values = {}
        for field, raw in fields.items():
            amount = _d(raw)
            if amount is None:
                reasons.append(f"{block}.{field}: {raw!r} is not an amount")
                continue
            values[field] = _s(amount)
        figures[block] = values

    def check(name: str, expected: Decimal | None, printed: Decimal | None) -> None:
        if expected is None or printed is None:
            return
        ok = abs(expected - printed) <= TOLERANCE
        checks.append({"check": name, "expected": _s(expected), "printed": _s(printed),
                       "difference": _s(printed - expected), "ok": ok})
        if not ok:
            reasons.append(f"{name}: the parts give {_s(expected)} but the document prints {_s(printed)}")

    ena = {k: _d(v) for k, v in figures.get("enajenacion", {}).items()}
    if {"gain", "loss", "net"} <= set(ena):
        check("Art. 129: ganancia - pérdida = resultado neto", ena["gain"] - abs(ena["loss"]), ena["net"])
    intereses = {k: _d(v) for k, v in figures.get("intereses", {}).items()}
    if {"nominal", "inflation_adjustment"} <= set(intereses) and ("real" in intereses or "real_loss" in intereses):
        real = (intereses.get("real") or Decimal(0)) - abs(intereses.get("real_loss") or Decimal(0))
        check("Intereses: nominal - ajuste por inflación = real - pérdida real",
              intereses["nominal"] - intereses["inflation_adjustment"], real)
    if intereses.get("real") and intereses.get("real_loss") and intereses["real"] > 0 and intereses["real_loss"] > 0:
        reasons.append("intereses: the document prints both a real interest and a real loss; only one can apply")
    div = {k: _d(v) for k, v in figures.get("dividendos", {}).items()}
    if "total" in div and ("domestic_gross" in div or "foreign_gross" in div):
        check("Dividendos: nacionales + extranjeros = total",
              (div.get("domestic_gross") or Decimal(0)) + (div.get("foreign_gross") or Decimal(0)), div["total"])
    d1099 = {k: _d(v) for k, v in figures.get("form_1099_div", {}).items()}
    if "qualified" in d1099 and "ordinary" in d1099 and d1099["qualified"] > d1099["ordinary"] + TOLERANCE:
        reasons.append(f"1099-DIV: qualified dividends ({_s(d1099['qualified'])}) exceed total ordinary dividends "
                       f"({_s(d1099['ordinary'])})")
    if "form_1099_b" in doc["blocks"]:
        _check_1099b(doc, figures, check, reasons, derived)
    return checks, reasons, figures, derived


def _check_1099b(doc: dict, figures: dict, check, reasons: list[str], derived: list[str]) -> None:
    lots, totals = doc["lots"], doc["totals"]
    fields = ("proceeds", "cost_basis", "wash_sale_disallowed", "gain")
    for index, lot in enumerate(lots):
        p, c, w, g = (_d(lot.get(f)) for f in fields)
        if None in (p, c, g):
            reasons.append(f"1099-B lot {index + 1}: proceeds, cost basis or gain is missing")
            continue
        check(f"1099-B lot {index + 1} ({lot.get('symbol') or lot.get('description') or '?'} sold "
              f"{lot.get('date_sold')}): proceeds - cost + wash sale = gain", p - c + (w or Decimal(0)), g)
        if lot.get("term") is None:
            reasons.append(f"1099-B lot {index + 1}: short or long term is not stated")
    by_term: dict[str, dict] = {}
    for total in totals:
        if total["term"] in by_term:
            reasons.append(f"1099-B: two printed {total['term']} totals")
        by_term[total["term"]] = total
        p, c, w, g = (_d(total.get(f)) for f in fields)
        if None not in (p, c, g):
            check(f"1099-B {total['term']} total: proceeds - cost + wash sale = gain", p - c + (w or Decimal(0)), g)
    for term in ("short", "long"):
        chosen = [lot for lot in lots if lot.get("term") == term]
        total = by_term.get(term)
        if chosen and total is None:
            reasons.append(f"1099-B: {len(chosen)} {term}-term lot(s) but no printed {term}-term total to check them "
                           "against")
            continue
        if chosen and total:
            for field in fields:
                printed = _d(total.get(field))
                parts = [_d(lot.get(field)) for lot in chosen]
                if printed is None and not any(parts):
                    continue
                check(f"1099-B {term}-term lots sum to the printed {field.replace('_', ' ')}",
                      sum((x or Decimal(0) for x in parts), Decimal(0)), printed or Decimal(0))
    terms = [by_term[t] for t in ("short", "long") if t in by_term]
    grand = by_term.get("all")
    if grand and terms:
        for field in fields:
            check(f"1099-B term totals sum to the printed overall {field.replace('_', ' ')}",
                  sum((_d(t.get(field)) or Decimal(0) for t in terms), Decimal(0)), _d(grand.get(field)) or Decimal(0))
    if not totals and not lots:
        return
    block = figures.setdefault("form_1099_b", {})
    for term, name in (("short", "short_term_gain"), ("long", "long_term_gain")):
        if term in by_term and _d(by_term[term].get("gain")) is not None:
            block[name] = _s(_d(by_term[term]["gain"]))
    source = [grand] if grand else terms
    for name in ("proceeds", "cost_basis", "wash_sale_disallowed"):
        values = [_d(t.get(name)) for t in source]
        if not values or all(v is None for v in values):
            continue
        block[name] = _s(sum((v or Decimal(0) for v in values), Decimal(0)))
        if len(source) > 1:
            derived.append(f"form_1099_b.{name} = sum of the printed term totals")
    if not totals and lots:
        reasons.append("1099-B: no printed totals; the lots could not be checked")


# --------------------------------------------------------------- proposal


def _fact_key(doc: dict) -> str:
    base = doc.get("institution_key") or slug(doc.get("institution") or "institution").replace("-", "_")
    return f"constancia.{base}_{doc['tax_year']}{_KEY_SUFFIX[doc['document_type']]}"


def _fact_value(doc: dict, figures: dict[str, dict[str, str]]) -> dict[str, Any]:
    from ..situation.schema import CONSTANCIA_BLOCKS
    value: dict[str, Any] = {"tax_year": doc["tax_year"], "institution": doc["institution"],
                             "currency": doc.get("currency") or _CURRENCY[doc["document_type"]]}
    if doc.get("issued_on"):
        value["issued_on"] = doc["issued_on"]
    for block, fields in figures.items():
        keep = {f: float(Decimal(v)) for f, v in fields.items() if f in CONSTANCIA_BLOCKS.get(block, ())}
        if keep:
            value[block] = keep
    return value


def _lines(result: dict) -> list[str]:
    lines = []
    for block, fields in result["figures"].items():
        if fields:
            lines.append(f"{block}: " + ", ".join(f"{k} {v}" for k, v in fields.items()))
    if result["lots"]:
        lines.append(f"1099-B: {len(result['lots'])} lot(s) listed")
    return lines


def build_tax_proposal(doc: dict[str, Any], pages: list[tuple[int, str]], *, provenance: dict[str, Any],
                       confidence: str = "high", warnings: Iterable[str] = (),
                       review_reasons: Iterable[str] = ()) -> dict[str, Any]:
    """Validate a parsed or extracted tax document and return a confirmable proposal."""
    text = "\n".join(t for _, t in pages)
    verification = _verify(doc, text)
    checks, reasons, figures, derived = _checks(doc)
    reasons = list(review_reasons) + reasons
    if verification["unverified"]:
        reasons.append(f"{len(verification['unverified'])} figure(s) do not appear in the document text.")
    if "instruction_like_text" in (provenance.get("risk_flags") or []):
        reasons.append("The document contains text addressed to an assistant; it was read as data only.")
    if not any(figures.values()):
        reasons.append("No figures were read from the document.")
    as_of = doc.get("issued_on") or f"{doc['tax_year']}-12-31"
    if date.fromisoformat(as_of) > date.today():
        as_of = date.today().isoformat()
    lots = [{k: v for k, v in lot.items()} for lot in doc["lots"]]
    result: dict[str, Any] = {
        "kind": "tax_document", "source_kind": "document", "document_type": doc["document_type"],
        "document_label": LABELS[doc["document_type"]], "institution": doc["institution"],
        "tax_year": doc["tax_year"], "issued_on": doc.get("issued_on"), "as_of": as_of,
        "currency": doc.get("currency") or _CURRENCY[doc["document_type"]], "account_last4": doc.get("account_last4"),
        "figures": figures, "lots": lots, "term_totals": doc["totals"], "derived": derived,
        "field_pages": doc["pages"], "confidence": confidence,
        "reconciliation": {"status": ("unreconciled" if any(not c["ok"] for c in checks)
                                      else "reconciled" if checks else "no_totals_to_check"),
                           "checks": checks},
        "verification": verification, "provenance": provenance,
        "review_reasons": list(dict.fromkeys(reasons)),
    }
    key = _fact_key(doc)
    result["facts_preview"] = [{"key": key, "value": _fact_value(doc, figures)}]
    result["summary"] = {"document": f"{LABELS[doc['document_type']]} {doc['tax_year']} ({doc['institution']})",
                         "saves": key, "figures": _lines(result),
                         "reconciliation": result["reconciliation"]["status"]}
    result["proposal_id"] = tax_proposal_digest(result)
    status = "needs_review" if result["review_reasons"] else "ready_to_confirm"
    return envelope(status, result, warnings=warnings, sources=[provenance.get("ref") or "document"],
                    assumptions=["Figures are copied as printed; the document is the source of truth in tax_pack.",
                                 *(["Totals marked derived are sums of printed totals."] if derived else [])])


def tax_proposal_digest(result: dict[str, Any]) -> str:
    provenance = {k: v for k, v in (result.get("provenance") or {}).items() if k not in {"retrieved_at", "received_at"}}
    return digest({k: result.get(k) for k in ("document_type", "institution", "tax_year", "issued_on", "as_of",
                                              "currency", "figures", "lots", "term_totals", "reconciliation",
                                              "verification", "facts_preview", "review_reasons")}
                  | {"provenance": provenance})


def tax_proposal_to_facts(proposal: dict[str, Any], *, confirmed: bool, proposal_id: str,
                          acknowledge_discrepancies: bool = False, expires_on: str | None = None) -> dict[str, Any]:
    """The ``remember`` payloads for a person-confirmed tax document: exactly its ``facts_preview``."""
    result = proposal.get("result") if isinstance(proposal, dict) else None
    if not isinstance(result, dict) or result.get("kind") != "tax_document":
        raise ValueError("proposal must be a tax document proposal")
    if confirmed is not True:
        return envelope("needs_input", {}, missing=[{"key": "confirmed", "reason": "missing",
                        "detail": "Ask the person to confirm the displayed figures before saving."}])
    if proposal_id != result.get("proposal_id") or tax_proposal_digest(result) != proposal_id:
        raise ValueError("proposal_id does not match this proposal; show the current figures and confirm again")
    status = proposal.get("status")
    if status not in ("ready_to_confirm", "needs_review"):
        raise ValueError(f"a {status} proposal cannot be saved")
    if status == "needs_review" and not acknowledge_discrepancies:
        return envelope("needs_review", {"review_reasons": result.get("review_reasons", [])},
                        warnings=["The person must acknowledge the listed discrepancies before this can be saved."])
    ref = (result.get("provenance") or {}).get("ref") or f"document:{proposal_id[:16]}"
    source = {"kind": "document", "ref": ref, "observed_on": result["as_of"]}
    facts = []
    for item in result["facts_preview"]:
        fact = {"key": item["key"], "value": item["value"], "source": source, "confidence": "reported"}
        if expires_on:
            fact["expires_on"] = expires_on
        facts.append(fact)
    warnings = []
    if status == "needs_review":
        warnings.append("Saved with discrepancies the person acknowledged: " + "; ".join(result["review_reasons"][:4]))
    return envelope("ready", {"facts": facts, "request_id": f"ingest-{proposal_id[:32]}", "expires_on": expires_on},
                    warnings=warnings, sources=[ref],
                    assumptions=["tax_pack reads constancia facts as the institution's figures (source of truth)."])


# ---------------------------------------------------------- host extraction

_NUM = {"type": ["string", "null"], "maxLength": 40,
        "description": "Copy the number exactly as printed (e.g. \"1,234.56\", \"(12.50)\"); null if not printed."}
_TEXT = {"type": ["string", "null"], "maxLength": 200}
_DATE = {"type": ["string", "null"], "pattern": r"^\d{4}-\d{2}-\d{2}$"}
_PAGE = {"type": ["integer", "null"], "minimum": 1}
_TERM = {"type": ["string", "null"], "enum": [None, "short", "long", "all"]}


def _obj(properties: dict[str, Any], max_items: int | None = None) -> dict[str, Any]:
    schema = {"type": "object", "additionalProperties": False, "required": sorted(properties), "properties": properties}
    return schema if max_items is None else {"type": "array", "maxItems": max_items, "items": schema}


TAX_EXTRACTION_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "Wealth annual tax document extraction",
    **_obj({
        "document_type": {"type": ["string", "null"], "enum": [None, *TYPES]},
        "institution": _TEXT, "tax_year": {"type": ["string", "null"], "pattern": r"^20\d{2}$"},
        "issued_on": _DATE, "account_last4": {"type": ["string", "null"], "pattern": r"^\d{4}$"},
        "currency": {"type": ["string", "null"], "pattern": r"^[A-Z]{3}$"},
        **{block: {"anyOf": [{"type": "null"}, _obj({f: _NUM for f in fields})]} for block, fields in BLOCK_FIELDS.items()},
        "lots": _obj({"description": _TEXT, "symbol": _TEXT, "quantity": _NUM,
                      "date_acquired": {"type": ["string", "null"], "pattern": r"^(\d{4}-\d{2}-\d{2}|VARIOUS)$"},
                      "date_sold": _DATE, "proceeds": _NUM, "cost_basis": _NUM, "wash_sale_disallowed": _NUM,
                      "gain": _NUM, "term": _TERM, "page": _PAGE}, 5000),
        "term_totals": _obj({"term": _TERM, "proceeds": _NUM, "cost_basis": _NUM, "wash_sale_disallowed": _NUM,
                             "gain": _NUM, "page": _PAGE}, 10),
    }),
}

TAX_INSTRUCTIONS = (
    "This is an annual tax document (a Mexican constancia fiscal, a US Form 1099 composite or Form 5498). Extract only "
    "what is printed. Copy every number exactly as it appears; use null for anything not printed and never compute a "
    "value. Constancia (MXN): enajenacion = Art. 129 share sales {gain, loss, net, isr_withheld}; intereses = "
    "{nominal, inflation_adjustment (ajuste anual por inflacion), real, real_loss (perdida real), isr_withheld}; "
    "dividendos = {domestic_gross, foreign_gross, isr_withheld, isr_creditable, foreign_tax_withheld, total}. "
    "1099 (USD): form_1099_div boxes 1a ordinary, 1b qualified, 2a capital_gain_distributions, 4 federal_tax_withheld, "
    "7 foreign_tax_paid; form_1099_int boxes 1 interest, 3 us_treasury_interest, 4, 6; form_1099_b = the printed "
    "totals, with every lot in lots (term short or long) and every printed total in term_totals (term all for the "
    "overall total). Form 5498 boxes 1 ira_contributions, 2, 3, 4, 5 fair_market_value, 8, 9, 10 roth_contributions, "
    "12b rmd_next_year. Give only the last four digits of the account. Never output RFC, CURP, SSN/TIN, addresses or "
    "names of people. Return one JSON object matching the schema."
)


def tax_extraction_request(pages: list[tuple[int, str]], *, provenance: dict[str, Any], reason: str,
                           detected: dict[str, Any] | None = None) -> dict[str, Any]:
    from .llm import extraction_request
    request = extraction_request(pages, provenance=provenance, reason=reason)
    request.update(schema=TAX_EXTRACTION_SCHEMA, instructions=TAX_INSTRUCTIONS, document_kind="tax_document",
                   detected={k: (detected or {}).get(k) for k in ("document_type", "institution", "tax_year")})
    return request


def validate_tax_extraction(payload: Any, source: Any, *, provenance: dict[str, Any] | None = None) -> dict[str, Any]:
    """Validate a host-model tax document extraction against its request's page text."""
    from .llm import _source_texts, check_schema
    text, request_source = _source_texts(source)
    base = dict(provenance or request_source or {})
    base.setdefault("kind", "document")
    base.setdefault("ref", "document:llm-extraction")
    base["parser"] = "host-llm-validated"
    errors = check_schema(TAX_EXTRACTION_SCHEMA, payload)
    detected = (source.get("detected") if isinstance(source, dict) else None) or {}
    if not errors:
        for path, value in [("issued_on", payload.get("issued_on"))] + [
                (f"lots[{i}].{f}", lot.get(f)) for i, lot in enumerate(payload.get("lots") or [])
                for f in ("date_acquired", "date_sold")]:
            if isinstance(value, str) and value != "VARIOUS":
                try:
                    date.fromisoformat(value)
                except ValueError:
                    errors.append(f"$.{path} is not a real date")
        kind = payload.get("document_type") or detected.get("document_type")
        year = payload.get("tax_year") or (str(detected["tax_year"]) if detected.get("tax_year") else None)
        institution = payload.get("institution") or detected.get("institution")
        if kind not in TYPES:
            errors.append("$.document_type is required")
        if not year:
            errors.append("$.tax_year is required")
        if not institution:
            errors.append("$.institution is required")
    if errors:
        return envelope("rejected", {"provenance": base, "schema_errors": errors[:50]},
                        warnings=["The extraction does not match the required schema; nothing was accepted."],
                        sources=[base["ref"]])
    key = next((k for k, name, pattern in _INSTITUTIONS if pattern.search(institution)), None)
    doc = {"document_type": kind, "institution_key": key,
           "institution": next((name for k, name, _ in _INSTITUTIONS if k == key), institution),
           "tax_year": int(year), "issued_on": payload.get("issued_on"), "account_last4": payload.get("account_last4"),
           "currency": payload.get("currency") or _CURRENCY[kind], "blocks": {}, "pages": {}, "conflicts": [],
           "notes": [], "lots": [], "totals": []}
    for block in BLOCK_FIELDS:
        fields = payload.get(block) or {}
        kept = {f: v for f, v in fields.items() if v not in (None, "")}
        if kept:
            doc["blocks"][block] = kept
    for lot in payload.get("lots") or []:
        doc["lots"].append({**lot, "date_acquired": lot.get("date_acquired"), "description":
                            redact_text(lot.get("description") or "")[:120] or None})
    doc["totals"] = [dict(t, term=t.get("term") or "all") for t in payload.get("term_totals") or []]
    if doc["lots"] or doc["totals"]:
        doc["blocks"].setdefault("form_1099_b", {})
    reasons = []
    warnings = []
    if text is None:
        warnings.append("No source text was supplied, so extracted values could not be verified.")
        reasons.append("No source text to verify the figures against.")
        text = ""
    elif fold(institution).split()[0] not in fold(text):
        reasons.append(f"The institution {institution!r} does not appear in the document text.")
    if kind != (detected.get("document_type") or kind):
        reasons.append(f"The document was detected as {detected['document_type']}, not {kind}.")
    return build_tax_proposal(doc, [(1, text)], provenance=base, confidence="medium", warnings=warnings,
                              review_reasons=reasons)


# ------------------------------------------------------------------- entry


def ingest_tax_pages(pages: list[tuple[int, str]], detected: dict[str, Any], *, provenance: dict[str, Any],
                     warnings: Iterable[str] = (), review_reasons: Iterable[str] = ()) -> dict[str, Any]:
    """Deterministic read of a detected tax document, or a tax extraction request when the layout is unknown."""
    provenance = {**provenance, "parser": f"taxdoc:{detected['document_type']}", "document_type": detected["document_type"]}
    warnings = list(warnings)
    doc = parse_tax_document(pages, detected)
    gaps = [name for name, missing in (("tax year", doc["tax_year"] is None), ("institution", doc["institution"] is None),
                                        ("figures", not any(doc["blocks"].values()) and not doc["lots"]))
            if missing]
    if gaps:
        request = tax_extraction_request(
            pages, provenance=provenance, detected=detected,
            reason=f"{LABELS[detected['document_type']]} detected, but its {', '.join(gaps)} did not match a known layout.")
        return envelope("needs_extraction", {"provenance": provenance, "document_type": detected["document_type"],
                                             "extraction_request": request},
                        warnings=warnings, sources=[provenance["ref"]])
    proposal = build_tax_proposal(doc, pages, provenance=provenance, warnings=warnings, review_reasons=review_reasons)
    if proposal["status"] == "needs_review" and proposal["result"]["reconciliation"]["status"] == "unreconciled":
        proposal["result"]["extraction_request"] = tax_extraction_request(
            pages, provenance=provenance, detected=detected,
            reason="The printed totals do not reconcile; a structured re-extraction can be validated instead.")
    return proposal


__all__ = ["BLOCK_FIELDS", "TAX_EXTRACTION_SCHEMA", "build_tax_proposal", "detect_tax_document", "ingest_tax_pages",
           "parse_tax_document", "tax_extraction_request", "tax_proposal_digest", "tax_proposal_to_facts",
           "validate_tax_extraction"]
