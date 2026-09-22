"""Small helpers and the book: the year's facts, documents and ledger, read once."""
from __future__ import annotations

import json
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Mapping

from .. import _common
from ..finmath import FxTable
from ..ledger.derive import active_entries
from ..ledger.model import fold
from .sources import (
    CENT, DOCUMENT_BLOCKS, FX_AGE_DAYS, ZERO, _BANK_TYPES, _IRA_TYPES, _LIABILITY_TYPES, _MX_INSTITUTIONS,
    _MX_RETIREMENT_TYPES, _ROTH_TYPES, _TAX_SHELTERED, _US_INSTITUTIONS, _WORKPLACE_TYPES,
)


# ----------------------------------------------------------------- small helpers


def _d(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value).replace(",", ""))
    except (InvalidOperation, ValueError):
        return None
    return number if number.is_finite() else None


def _m(value: Decimal | None) -> str | None:
    return None if value is None else format(value.quantize(CENT), "f")


def _q(value: Decimal | None) -> str | None:
    if value is None:
        return None
    text = format(value.normalize(), "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


_sum = _common.decimal_sum


def _need(section: str, key: str, es: str, en: str, reason: str = "missing") -> dict[str, str]:
    return {"key": key, "section": section, "reason": reason, "detail": en, "detail_es": es}


def _unique(items: Iterable[Mapping[str, Any]]) -> list[dict]:
    seen, out = set(), []
    for item in items:
        marker = json.dumps(item, sort_keys=True, default=str)
        if marker not in seen:
            seen.add(marker)
            out.append(dict(item))
    return out


def _cols(*spec: tuple[str, str, str]) -> list[dict[str, str]]:
    return [{"key": k, "es": es, "en": en} for k, es, en in spec]


def _section(sid: str, es: str, en: str, jurisdiction: str, *, currency: str | None = None,
             summary: Mapping[str, Any] | None = None, columns: list | None = None, rows: list | None = None,
             reconciliation: list | None = None, missing: Iterable = (), warnings: Iterable[str] = (),
             sources: Iterable = (), assumptions: Iterable[str] = (), status: str | None = None,
             extra: Mapping[str, Any] | None = None) -> dict[str, Any]:
    missing = _unique(missing)
    section = {"id": sid, "title": {"es": es, "en": en}, "jurisdiction": jurisdiction,
               "status": status or ("partial" if missing else "ready"), "currency": currency,
               "summary": dict(summary or {}), "table": {"columns": columns or [], "rows": rows or []},
               "reconciliation": reconciliation or [], "missing": missing,
               "warnings": list(dict.fromkeys(warnings)), "sources": _unique(sources),
               "assumptions": list(dict.fromkeys(assumptions))}
    if extra:
        section.update(extra)
    return section


def _month(day: str) -> str:
    return day[:7]


def _prior_month(day: str) -> str:
    d = date.fromisoformat(day)
    first = d.replace(day=1) - timedelta(days=1)
    return first.isoformat()[:7]


def _long_term(acquired: str, sold: str) -> bool:
    """IRC 1222: held more than one year (the day after the anniversary or later)."""
    a, s = date.fromisoformat(acquired), date.fromisoformat(sold)
    try:
        anniversary = a.replace(year=a.year + 1)
    except ValueError:  # Feb 29
        anniversary = date(a.year + 1, 3, 1) - timedelta(days=1)
    return s > anniversary


def _same_institution(a: Any, b: Any) -> bool:
    """GBM and "GBM Grupo Bursátil", Charles Schwab and Schwab: the same institution."""
    one, two = fold(a), fold(b)
    return bool(one and two) and (one == two or one in two.split() or two in one.split()
                                  or one.startswith(two) or two.startswith(one))


def _combine(docs: list[dict]) -> dict | None:
    """One document, or several of one institution (one per account) summed into one, block by block.

    A figure is summed only when every document carrying the block prints it; 1099-B lots are
    concatenated, each keeping the id of the document it came from.  ``_parts`` keeps the originals
    so each one is still reconciled on its own."""
    if not docs:
        return None
    if len(docs) == 1:
        return docs[0]
    out: dict[str, Any] = {"id": "+".join(d["id"] for d in docs), "institution": docs[0].get("institution"),
                           "tax_year": docs[0].get("tax_year"), "_source": docs[0].get("_source"),
                           "_ref": docs[0].get("_ref"), "_parts": docs}
    for name in DOCUMENT_BLOCKS:
        blocks = [(d, _normalized(name, d[name])) for d in docs if isinstance(d.get(name), dict)]
        if not blocks:
            continue
        fields = {f for _, b in blocks for f in b if f != "lots"}
        merged: dict[str, Any] = {}
        for field in sorted(fields):
            values = [_d(b.get(field)) for _, b in blocks]
            if None not in values:
                merged[field] = str(sum(values, ZERO))
        lots = [dict(lot, _document_id=d["id"]) for d, b in blocks for lot in b.get("lots") or []]
        if lots:
            merged["lots"] = lots
        out[name] = merged
    return out


def _normalized(name: str, block: Mapping[str, Any]) -> dict:
    """An Art. 129 block with its net (gain less loss) stated, so documents printing either can be summed."""
    block = dict(block)
    if name == "enajenacion":
        gain, loss = _d(block.get("gain")), _d(block.get("loss"))
        if loss is not None:
            block["loss"] = str(abs(loss))
        if _d(block.get("net")) is None and gain is not None and loss is not None:
            block["net"] = str(gain - abs(loss))
    return block


def _parts(doc: Mapping[str, Any], block: str, field: str) -> list[dict] | None:
    """Each document's own figure behind a summed one: [{document_id, document}], None for a single document."""
    parts = doc.get("_parts")
    if not parts:
        return None
    return [{"document_id": p["id"], "document": _m(_d(_normalized(block, p.get(block) or {}).get(field)))}
            for p in parts if isinstance(p.get(block), dict)]


def _recon(item: str, ours: Decimal | None, theirs: Decimal | None, truth: str, doc: Mapping[str, Any],
           block: str, field: str) -> dict:
    """A reconciliation row; a summed document also lists each document's own figure."""
    row = {"item": item, "ours": _m(ours), "document": _m(theirs),
           "difference": _m(None if ours is None or theirs is None else ours - theirs),
           "source_of_truth": truth, "document_id": doc["id"]}
    parts = _parts(doc, block, field)
    if parts:
        row["documents"] = parts
    return row


def _fingerprint(doc: Mapping[str, Any]) -> str:
    return json.dumps({"institution": fold(doc.get("institution")), "tax_year": doc.get("tax_year"),
                       **{b: doc.get(b) for b in DOCUMENT_BLOCKS}}, sort_keys=True, default=str)


# ----------------------------------------------------------------- the inputs


class _Book:
    """The year's inputs: ledger, facts, documents and parameters, with what each section reads."""

    def __init__(self, inputs: Mapping[str, Any], snapshot: Mapping[str, Any], ledger: Mapping[str, Any] | None,
                 year: int, today: str):
        self.year = year
        self.start, self.end = f"{year}-01-01", f"{year}-12-31"
        self.today = today
        self.ledger = ledger or {"accounts": [], "instruments": [], "entries": [], "fx": []}
        # Inferred facts wait for the person's yes; a past review date retires a balance or a profile, not a
        # dated document: only the year's stated tax facts and the institutions' documents are about a closed year.
        self.facts = {f["key"]: f for f in snapshot.get("facts") or [] if f.get("value") is not None
                      and f.get("confidence") != "inferred"
                      and (not f.get("expires_on") or f["expires_on"] >= today
                           or f["key"].startswith(("tax.", "constancia.")))}
        self.evidence: set[str] = set()
        self.warnings: list[str] = []
        stale_profile = next((f for f in snapshot.get("facts") or [] if f.get("key") == "client.profile"
                              and f.get("value") is not None and f.get("confidence") != "inferred"
                              and f.get("expires_on") and f["expires_on"] < today), None)
        self.stale_profile = stale_profile is not None
        if stale_profile:
            self.warnings.append(f"client.profile is past its review date ({stale_profile['expires_on']}): it was "
                                 "left out (residence, US status, birth year). Reconfirm it with the person; the "
                                 "jurisdictions come from fresh facts or are asked for.")
        profile_fact = self.facts.get("client.profile")
        self.profile = profile_fact["value"] if profile_fact and isinstance(profile_fact["value"], dict) else {}
        if profile_fact:
            self.evidence.add(profile_fact["id"])
        tax_fact = self.facts.get(f"tax.{year}")
        self.tax = tax_fact["value"] if tax_fact and isinstance(tax_fact["value"], dict) else {}
        if tax_fact:
            self.evidence.add(tax_fact["id"])
        self.mx = self.tax.get("mx") if isinstance(self.tax.get("mx"), dict) else {}
        self.us = self.tax.get("us") if isinstance(self.tax.get("us"), dict) else {}
        self.constancias = []
        for key, fact in sorted(self.facts.items()):
            value = fact["value"]
            if not key.startswith("constancia.") or not isinstance(value, dict):
                continue
            source = fact.get("source") or {}
            doc = {"id": key.partition(".")[2], "fact_id": fact["id"], **value,
                   "_source": source.get("kind"), "_ref": source.get("ref")}
            if value.get("tax_year") == year:
                self.constancias.append(doc)
                self.evidence.add(fact["id"])
        # An uploaded document (source kind document) wins over one typed in for the same institution.
        self.constancias.sort(key=lambda d: d["_source"] != "document")
        # The same document saved twice (a re-upload under an older key) counts once; two accounts' documents
        # of one institution are all kept and summed where the pack reads them.
        seen: dict[str, dict] = {}
        kept = []
        for doc in self.constancias:
            mark = _fingerprint(doc)
            twin = seen.get(mark)
            if twin is not None and (not twin.get("account_last4") or not doc.get("account_last4")
                                     or twin["account_last4"] == doc["account_last4"]):
                self.warnings.append(f"constancia.{doc['id']} has the same figures as constancia.{twin['id']}: "
                                     "read as one document, counted once.")
                continue
            seen[mark] = doc
            kept.append(doc)
        self.constancias = kept
        for name in DOCUMENT_BLOCKS:
            unlabeled: dict[str, list[str]] = {}
            for doc in kept:
                if isinstance(doc.get(name), dict) and not doc.get("account_id") and not doc.get("account_last4"):
                    unlabeled.setdefault(fold(doc.get("institution")), []).append(doc["id"])
            for ids in unlabeled.values():
                if len(ids) > 1:
                    self.warnings.append(f"{', '.join('constancia.' + i for i in ids)} ({name}) name no account and "
                                         "are summed as separate accounts; if one is a corrected copy of the other, "
                                         "forget the older one.")
        self.parameters = inputs.get("parameters") or {}
        if not isinstance(self.parameters, dict):
            raise ValueError("parameters must be an object {key: {value, source}}")
        inpc = inputs.get("inpc") if inputs.get("inpc") is not None else self.mx.get("inpc")
        self.inpc: dict[str, Decimal] = {}
        self.inpc_source = None
        if inpc is not None:
            if not isinstance(inpc, dict):
                raise ValueError("inpc must be {\"YYYY-MM\": value, ..., \"source\": text}")
            self.inpc_source = inpc.get("source") or self.mx.get("inpc_source")
            for month, value in inpc.items():
                if month == "source":
                    continue
                number = _d(value)
                if len(str(month)) != 7 or number is None or number <= 0:
                    raise ValueError(f"inpc[{month!r}] must be a positive number keyed YYYY-MM")
                self.inpc[str(month)] = number
        self.entries, self.notes = active_entries(self.ledger)
        self.accounts = {a["id"]: a for a in self.ledger.get("accounts") or []}
        self.instruments = {i["id"]: i for i in self.ledger.get("instruments") or []}
        self.fx = FxTable(self.ledger.get("fx") or [], FX_AGE_DAYS)
        self.ledger_sources = _ledger_sources(self.entries)
        self.first_day = min((e["date"] for e in self.entries), default=None)
        self.last_day = max((e["date"] for e in self.entries), default=None)
        self.daily = None  # the year's daily replay, built once when interest needs average balances

    # -- classification

    def country(self, account_id: str) -> tuple[str | None, str]:
        account = self.accounts.get(account_id) or {}
        stated = (self.tax.get("account_countries") or {}).get(account_id) if isinstance(
            self.tax.get("account_countries"), dict) else None
        if stated:
            return str(stated).upper(), "stated"
        if account.get("country"):
            return str(account["country"]).upper(), "statement"
        return _institution_country(account.get("institution"), account.get("currency"))

    def kind(self, account_id: str) -> str:
        kind = str((self.accounts.get(account_id) or {}).get("type") or "other")
        if kind in _BANK_TYPES:
            return "bank"
        if kind in _LIABILITY_TYPES:
            return "liability"
        if kind in _MX_RETIREMENT_TYPES:
            return "mx_retirement"
        if kind in _WORKPLACE_TYPES:
            return "workplace"
        if kind in _ROTH_TYPES:
            return "roth"
        if kind in _IRA_TYPES:
            return "ira"
        if kind == "hsa":
            return "hsa"
        return "brokerage"

    def institution(self, account_id: str) -> str:
        account = self.accounts.get(account_id) or {}
        return account.get("institution") or account.get("name") or account_id

    def asset_class(self, instrument_id: str | None) -> str:
        return str((self.instruments.get(instrument_id or "") or {}).get("asset_class") or "")

    def symbol(self, instrument_id: str | None) -> str:
        meta = self.instruments.get(instrument_id or "") or {}
        return meta.get("symbol") or instrument_id or ""

    def identity(self, instrument_id: str) -> str:
        """Substantially-identical key: the underlying symbol (same security on another venue), else the symbol."""
        meta = self.instruments.get(instrument_id) or {}
        return str(meta.get("underlying_symbol") or meta.get("symbol") or instrument_id).upper()

    def sic_listed(self, instrument_id: str) -> bool | None:
        stated = self.mx.get("sic_listed") if isinstance(self.mx.get("sic_listed"), dict) else {}
        if isinstance(stated.get(instrument_id), bool):
            return stated[instrument_id]
        meta = self.instruments.get(instrument_id) or {}
        if str(meta.get("listing") or "").upper() == "SIC" or meta.get("venue") == "sic":
            return True
        return None

    def constancias_for(self, account_ids: Iterable[str], institution: str,
                        blocks: str | tuple[str, ...] | None = None) -> list[dict]:
        """Every saved document for these accounts or this institution (with one of ``blocks``).

        A document naming one of the accounts matches; so does one of the institution that names no
        account (or an account the ledger does not have).  Uploaded documents win over typed-in ones."""
        ids = set(account_ids)
        wanted = (blocks,) if isinstance(blocks, str) else blocks
        docs = [d for d in self.constancias if wanted is None or any(isinstance(d.get(b), dict) for b in wanted)]
        matched = [d for d in docs if d.get("account_id") in ids]
        matched += [d for d in docs if d not in matched and _same_institution(d.get("institution"), institution)
                    and (not d.get("account_id") or d["account_id"] not in self.accounts)]
        if not matched:
            matched = [d for d in docs if _same_institution(d.get("institution"), institution)]
        if any(d["_source"] == "document" for d in matched):
            matched = [d for d in matched if d["_source"] == "document"]
        return matched

    def constancia_for(self, account_ids: Iterable[str], institution: str,
                       blocks: str | tuple[str, ...] | None = None) -> dict | None:
        """The institution's document, or the sum of all of them (two accounts, two 1099s) as one."""
        return _combine(self.constancias_for(account_ids, institution, blocks))

    def accounts_for(self, doc: Mapping[str, Any]) -> set[str]:
        """The taxable ledger accounts a document covers: its account, else its institution's (by last four)."""
        parts = doc.get("_parts") or [doc]
        out: set[str] = set()
        for part in parts:
            if part.get("account_id") in self.accounts:
                out.add(part["account_id"])
                continue
            same = {a for a in self.accounts if _same_institution(part.get("institution"), self.institution(a))
                    and self.kind(a) not in _TAX_SHELTERED}
            tail = part.get("account_last4")
            if tail:
                narrowed = {a for a in same if tail in f"{a} {self.accounts[a].get('name') or ''}"}
                same = narrowed or same
            out |= same
        return out

    def convert(self, amount: Decimal | None, currency: str | None, target: str, on: str) -> Decimal | None:
        if amount is None or currency is None:
            return None
        return self.fx.convert(amount, currency, target, on)

    def factor(self, acquired: str | None, sold: str) -> Decimal | None:
        """CFF Art. 17-A update from the acquisition month to the month before the sale (floor 1)."""
        if acquired is None:
            return None
        early, late = self.inpc.get(_month(acquired)), self.inpc.get(_prior_month(sold))
        if early is None or late is None:
            return None
        return max(late / early, Decimal(1))

    def covers_year(self) -> bool:
        return self.first_day is not None and self.first_day <= self.start and (self.last_day or "") >= self.end[:7]


def _institution_country(institution: Any, currency: Any) -> tuple[str | None, str]:
    name = fold(institution)
    words = f" {name} "
    for key in _MX_INSTITUTIONS:
        if f" {key} " in words or name.startswith(key + " ") or name == key:
            return "MX", "institution"
    for key in _US_INSTITUTIONS:
        if f" {key} " in words or name == key:
            return "US", "institution"
    if currency == "MXN":
        return "MX", "currency"
    if currency == "USD":
        return "US", "currency"
    return None, "unknown"


def _ledger_sources(entries: list[dict]) -> list[dict]:
    seen: dict[str, dict] = {}
    for entry in entries:
        source = entry.get("source") or {}
        ident = source.get("file_hash") or f"{source.get('kind')}:{source.get('ref')}"
        seen.setdefault(ident, {k: v for k, v in source.items() if k in {"kind", "ref", "observed_on"}})
    return [value for _, value in sorted(seen.items())]
