"""Deterministic parsing helpers shared by every ingestion path.

Amounts are :class:`~decimal.Decimal` internally and decimal strings in output.
A value that cannot be read is ``None``: unknown is not zero.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation
import hashlib
import json
import re
import unicodedata
from typing import Any, Iterable


STATUSES = ("ready_to_confirm", "needs_review", "needs_extraction", "needs_input", "rejected", "ready")
_EMPTY = {"", "-", "--", "---", "—", "–", "n/a", "na", "n.a.", "nan", "none", "null", "*", "s/d", "s d", "n a", "nd", "n.d."}
_CURRENCY_MARKS = re.compile(
    r"(?i)(us\$|mx\$|u\$s|\$|€|£|\b(?:usd|mxn|eur|cad|gbp|udis?|dlls?|dls|m\.?\s?n\.?|mn|pesos|d[oó]lares)\b\.?)"
)
_MONTHS = {
    "january": 1, "jan": 1, "enero": 1, "ene": 1,
    "february": 2, "feb": 2, "febrero": 2,
    "march": 3, "mar": 3, "marzo": 3,
    "april": 4, "apr": 4, "abril": 4, "abr": 4,
    "may": 5, "mayo": 5,
    "june": 6, "jun": 6, "junio": 6,
    "july": 7, "jul": 7, "julio": 7,
    "august": 8, "aug": 8, "agosto": 8, "ago": 8,
    "september": 9, "sep": 9, "sept": 9, "septiembre": 9, "setiembre": 9,
    "october": 10, "oct": 10, "octubre": 10,
    "november": 11, "nov": 11, "noviembre": 11,
    "december": 12, "dec": 12, "diciembre": 12, "dic": 12,
}
_MONTH = "|".join(sorted(_MONTHS, key=len, reverse=True))
_DATE_PATTERNS = (
    ("iso", re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")),
    ("mdy_name", re.compile(rf"(?i)\b({_MONTH})\.?\s+(\d{{1,2}}),?\s+(\d{{4}})\b")),
    ("dmy_name", re.compile(rf"(?i)\b(\d{{1,2}})(?:\s+de)?[\s/.-]+({_MONTH})\.?(?:\s+de(?:l)?)?[\s/.,-]+(\d{{4}})\b")),
    ("numeric", re.compile(r"\b(\d{1,2})[/.-](\d{1,2})[/.-](\d{4}|\d{2})\b")),
    ("ymd_slash", re.compile(r"\b(\d{4})/(\d{1,2})/(\d{1,2})\b")),
)


def fold(text: Any) -> str:
    """Lowercase, strip accents and collapse punctuation for alias matching."""
    value = unicodedata.normalize("NFKD", str(text or ""))
    value = "".join(ch for ch in value if not unicodedata.combining(ch)).lower()
    return " ".join(re.sub(r"[^a-z0-9%]+", " ", value).split())


def with_institution(name: str | None, institution: str | None) -> str:
    """``name`` prefixed with the institution unless it already names it ("BBVA" is BBVA México; "Checking" is not)."""
    name = (name or "").strip()
    if not institution or not str(institution).strip():
        return name
    from ..situation.model import institution_key  # lazy: the situation model imports ingest helpers

    alias, tokens = institution_key(institution)
    named_alias, named_tokens = institution_key(name)
    if (alias and alias == named_alias) or fold(institution) in fold(name) or (tokens and tokens <= named_tokens):
        return name
    return f"{institution} {name}".strip()


def slug(text: Any, limit: int = 32) -> str:
    return "-".join(fold(text).replace("%", "").split())[:limit].strip("-") or "x"


def digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def out(value: Decimal | None) -> str | None:
    if value is None:
        return None
    if value == 0:
        return "0"
    rendered = format(value.normalize(), "f")
    return rendered.rstrip("0").rstrip(".") if "." in rendered else rendered


def decimals(raw: str) -> int:
    """Printed decimal places of a numeric string (for rounding tolerances)."""
    match = re.search(r"[.,](\d+)\s*\)?-?\s*$", raw.strip())
    if match and not re.fullmatch(r"\d{1,3}(?:,\d{3})+", re.sub(r"[^\d,.]", "", raw)):
        return len(match.group(1))
    return 0


def parse_amount(value: Any, *, decimal_comma: bool | None = None) -> Decimal | None:
    """Read a printed amount: ``$1,234.56``, ``(1,234.56)``, ``1.234,56``, ``1,234.56 MN``.

    ``decimal_comma=None`` auto-detects unambiguous European formatting.
    Percent signs are rejected here; use :func:`parse_percent`.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, Decimal)):
        return Decimal(value)
    if isinstance(value, float):
        return Decimal(str(value)) if value == value and abs(value) != float("inf") else None
    text = str(value).strip()
    if fold(text) in _EMPTY or "%" in text:
        return None
    negative = False
    if text.startswith("(") and text.endswith(")"):
        negative, text = True, text[1:-1]
    text = _CURRENCY_MARKS.sub("", text).strip()
    if text.endswith("-") and not text.startswith("-"):
        negative, text = True, text[:-1]
    if text.startswith(("-", "−", "–")):
        negative, text = True, text[1:]
    if text.startswith("+"):
        text = text[1:]
    text = text.replace(" ", "").replace(" ", "").replace("'", "")
    if not text or re.fullmatch(r"[\d.,]+", text) is None or not re.search(r"\d", text):
        return None
    comma = decimal_comma
    if comma is None:
        comma = bool(
            re.fullmatch(r"\d{1,3}(?:\.\d{3})+,\d+", text)
            or re.fullmatch(r"\d+,\d{1,2}", text)
            or re.fullmatch(r"\d+,\d{4,}", text)
        )
    if comma:
        if text.count(",") > 1:
            return None
        text = text.replace(".", "").replace(",", ".")
    else:
        if text.count(".") > 1:
            return None
        if "," in text and re.fullmatch(r"\d{1,3}(?:,\d{3})+(?:\.\d+)?", text) is None:
            return None
        text = text.replace(",", "")
    try:
        result = Decimal(text)
    except InvalidOperation:
        return None
    return -result if negative else result


def parse_percent(value: Any) -> Decimal | None:
    """``4.2%`` or ``4.2`` (percent points) -> ``0.042``."""
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip().replace("%", "")
    amount = parse_amount(text)
    return None if amount is None else amount / 100


def parse_date(text: Any, *, day_first: bool | None = None) -> tuple[date | None, str]:
    """Return the first date in ``text`` and a confidence of high|medium|low.

    Numeric ``dd/mm`` versus ``mm/dd`` is resolved by ``day_first`` (Spanish
    statements are day first); when neither part exceeds 12 and no locale is
    known the result is low confidence.
    """
    found = find_dates(str(text or ""), day_first=day_first)
    return found[0] if found else (None, "low")


def find_dates(text: str, *, day_first: bool | None = None) -> list[tuple[date, str]]:
    hits: list[tuple[int, date, str]] = []
    for kind, pattern in _DATE_PATTERNS:
        for match in pattern.finditer(text):
            parsed = _date_from(kind, match.groups(), day_first)
            if parsed is not None:
                hits.append((match.start(), parsed[0], parsed[1]))
    hits.sort(key=lambda item: item[0])
    seen: set[int] = set()
    result = []
    for start, value, confidence in hits:
        if start not in seen:
            seen.add(start)
            result.append((value, confidence))
    return result


def _date_from(kind: str, groups: tuple[str, ...], day_first: bool | None) -> tuple[date, str] | None:
    try:
        if kind == "iso":
            return date(int(groups[0]), int(groups[1]), int(groups[2])), "high"
        if kind == "ymd_slash":
            return date(int(groups[0]), int(groups[1]), int(groups[2])), "high"
        if kind == "mdy_name":
            return date(int(groups[2]), _MONTHS[groups[0].lower()], int(groups[1])), "high"
        if kind == "dmy_name":
            return date(int(groups[2]), _MONTHS[groups[1].lower()], int(groups[0])), "high"
        first, second, year = int(groups[0]), int(groups[1]), int(groups[2])
        year += 2000 if year < 100 else 0
        if first > 12 and second <= 12:
            return date(year, second, first), "high"
        if second > 12 and first <= 12:
            return date(year, first, second), "high"
        if first == second:
            return date(year, first, second), "high"
        if day_first is None:
            return date(year, first, second), "low"
        return (date(year, second, first) if day_first else date(year, first, second)), "medium"
    except (ValueError, KeyError):
        return None


def number_tokens(text: str) -> set[Decimal]:
    """Every number printed in ``text`` under both decimal conventions.

    Used for anti-hallucination checks: an extracted amount must equal a
    printed number.  Percentages also contribute their fraction; ``40k`` and
    ``1.2 millones`` contribute their scaled value.
    """
    values: set[Decimal] = set()
    pattern = re.compile(
        r"(?<![\w])(\d[\d,.' ]*\d|\d)(\s*%)?(\s*(?:k|mm|m|mil(?:l[oó]n(?:es)?)?|millones|thousand|million|bn|billion)\b)?",
        re.IGNORECASE,
    )
    for match in pattern.finditer(text):
        raw, percent, scale = match.group(1), match.group(2), match.group(3)
        candidates = set(raw.split())
        if " " not in raw or re.fullmatch(r"\d{1,3}(?: \d{3})+(?:[.,]\d+)?", raw):
            candidates.add(raw)
        for candidate in candidates:
            candidate = candidate.strip(" ,.'")
            if not candidate:
                continue
            for comma in (False, True):
                amount = parse_amount(candidate, decimal_comma=comma)
                if amount is None:
                    continue
                values.add(amount)
                if percent:
                    values.add(amount / 100)
                if scale:
                    word = scale.strip().lower()
                    factor = Decimal(1000) if word in {"k", "mil", "thousand"} else (
                        Decimal(10) ** 9 if word in {"bn", "billion"} else Decimal(10) ** 6
                    )
                    values.add(amount * factor)
    return values


def envelope(status: str, result: dict[str, Any], *, missing: Iterable[Any] = (), warnings: Iterable[str] = (),
             sources: Iterable[str] = (), assumptions: Iterable[str] = ()) -> dict[str, Any]:
    if status not in STATUSES:
        raise ValueError(f"unknown ingest status: {status}")
    return {
        "status": status,
        "result": result,
        "missing": list(missing),
        "warnings": list(dict.fromkeys(warnings)),
        "sources": list(dict.fromkeys(sources)),
        "assumptions": list(dict.fromkeys(assumptions)),
    }
