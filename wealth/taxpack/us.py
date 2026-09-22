"""US sections: Form 8949, Schedule D, 1099s, foreign tax credit, retirement accounts, FBAR and 8938."""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from typing import Any, Mapping

from .. import retirement
from ..ledger.derive import replay
from ..ledger.model import fold
from .sources import (
    CENT, FBAR_THRESHOLD_USD, FORM_8938_THRESHOLDS, SRC_1099DIV, SRC_1116, SRC_590A, SRC_590B, SRC_8938,
    SRC_8949, SRC_FBAR, SRC_FBAR_DUE, SRC_PUB550, SRC_RR_2008_5, SRC_SCHED_D, SRC_TREASURY_RATES,
    US_CAPITAL_LOSS_LIMIT, US_DEFERRAL_LIMITS, ZERO, _TAX_SHELTERED, _US_IRA_ONLY,
)
from .book import (
    _Book, _cols, _combine, _d, _institution_country, _long_term, _m, _need, _q, _recon, _same_institution,
    _section, _sum,
)


# ----------------------------------------------------------------- US sections


def _us_rows(book: _Book) -> tuple[list[dict], list[dict], list[str]]:
    """Form 8949 rows (taxable accounts, FIFO lots) with wash sales matched share for share."""
    sid = "us_8949"
    state, meta = replay(book.ledger, f"{book.year + 1}-01-31")
    missing, warnings = [], []
    window_start = (date(book.year, 1, 1) - timedelta(days=31)).isoformat()
    buys = [e for e in book.entries if e["kind"] == "buy" and e.get("instrument_id")]
    remaining = {b["id"]: _d(b.get("quantity")) or ZERO for b in buys}
    adjust: dict[str, dict] = {}   # replacement lot id -> {per_share, qty, tacked_acquired}
    rows = []
    realized = sorted(state.realized, key=lambda r: (r["date"], r["entry_id"], r["lot_id"]))
    same_sale: dict[str, set[str]] = {}  # shares sold in the same sale are never its replacement
    for item in realized:
        same_sale.setdefault(item["entry_id"], set()).add(item["lot_id"])
    for item in realized:
        if item["date"] < window_start or item["date"] > book.end:
            continue
        account = item["account_id"]
        if book.kind(account) in _TAX_SHELTERED:
            continue
        qty = item["quantity"]
        proceeds = book.convert(item["proceeds"], item["proceeds_currency"], "USD", item["date"])
        acquired = item["acquired_on"]
        basis = None
        if item["basis"] is not None and item["basis_currency"] == "USD":
            basis = item["basis"]  # a USD basis needs no rate, so an unknown purchase date leaves it known
        elif item["basis"] is not None and acquired is not None:
            basis = book.convert(item["basis"], item["basis_currency"], "USD", acquired)
        extra = adjust.get(item["lot_id"])
        if extra and basis is not None:
            share = min(qty, extra["qty"])
            basis += extra["per_share"] * share
            extra["qty"] -= share
            acquired = extra["tacked"] or acquired
        gain = None if proceeds is None or basis is None else proceeds - basis
        code, adjustment, permanent = "", ZERO, ZERO
        if gain is not None and gain < 0:
            key = book.identity(item["instrument_id"])
            sold = date.fromisoformat(item["date"])
            low, high = (sold - timedelta(days=30)).isoformat(), (sold + timedelta(days=30)).isoformat()
            matched = ZERO
            for buy in buys:
                if matched >= qty:
                    break
                if buy["id"] in same_sale.get(item["entry_id"], ()) or book.identity(buy["instrument_id"]) != key:
                    continue
                if not low <= buy["date"] <= high or remaining[buy["id"]] <= 0:
                    continue
                take = min(remaining[buy["id"]], qty - matched)
                remaining[buy["id"]] -= take
                matched += take
                loss_part = -gain * take / qty
                if book.kind(buy["account_id"]) in {"ira", "roth", "workplace"}:
                    permanent += loss_part
                else:
                    held = (sold - date.fromisoformat(acquired)).days if acquired else 0
                    tacked = (date.fromisoformat(buy["date"]) - timedelta(days=held)).isoformat()
                    slot = adjust.setdefault(buy["id"], {"per_share": ZERO, "qty": ZERO, "tacked": tacked})
                    total_add = slot["per_share"] * slot["qty"] + loss_part
                    slot["qty"] += take
                    slot["per_share"] = total_add / slot["qty"]
            if matched > 0:
                code = "W"
                adjustment = -gain * matched / qty
        if not book.start <= item["date"] <= book.end:
            continue
        country, _ = book.country(account)
        term = None if acquired is None else ("long" if _long_term(acquired, item["date"]) else "short")
        box = None
        if term:
            box = ("A" if term == "short" else "D") if country == "US" else ("C" if term == "short" else "F")
        rows.append({
            "account": book.institution(account), "account_id": account,
            "description": f"{_q(qty)} {book.symbol(item['instrument_id'])}", "instrument_id": item["instrument_id"],
            "date_acquired": acquired, "date_sold": item["date"], "proceeds_usd": _m(proceeds), "cost_usd": _m(basis),
            "code": code, "adjustment_usd": _m(adjustment) if code else None,
            "gain_usd": _m(None if gain is None else gain + adjustment), "term": term, "box": box,
            "permanently_disallowed_usd": _m(permanent) if permanent else None,
            "currency": item["proceeds_currency"]})
        if term is None and gain is not None:
            rows[-1]["note"] = ("Acquisition date unknown: the gain is known but its term (short or long) is not. "
                                "Form 8949 accepts VARIOUS in column (b) only for lots bought on several dates.")
        if proceeds is None or (item["basis"] is not None and basis is None and acquired is not None):
            pair = item["proceeds_currency"] if proceeds is None else item["basis_currency"]
            missing.append(_need(sid, f"fx.{pair}/USD@{item['date'] if proceeds is None else acquired}",
                                 f"Tipo de cambio {pair}/USD del {item['date']} o de la fecha de compra.",
                                 f"A {pair}/USD rate for {item['date']} or the purchase date."))
        if item["basis"] is None:
            missing.append(_need(sid, f"lots.{item['lot_id']}.cost_basis",
                                 f"Costo y fecha de compra del lote {item['lot_id']}.",
                                 f"Cost basis and purchase date of lot {item['lot_id']}."))
        elif acquired is None:
            missing.append(_need(sid, f"lots.{item['lot_id']}.acquired_on",
                                 f"Fecha de compra del lote {item['lot_id']} (corto o largo plazo"
                                 + ("" if basis is not None else "; y el tipo de cambio de ese día") + ").",
                                 f"Purchase date of lot {item['lot_id']} (short or long term"
                                 + ("" if basis is not None else ", and that day's exchange rate") + ")."))
        if permanent:
            warnings.append(f"{rows[-1]['description']} sold {item['date']}: replaced in an IRA/Roth; "
                            f"{_m(permanent)} USD of loss is permanently disallowed (Rev. Rul. 2008-5).")
    late_losses = [r for r in rows if r["date_sold"] >= f"{book.year}-12-02" and (_d(r["gain_usd"]) or ZERO) < 0]
    if late_losses and (book.last_day or "") < f"{book.year + 1}-01-30":
        missing.append(_need(sid, f"ledger.{book.year + 1}-01", f"Compras del 1 al 30 de enero de {book.year + 1}: una "
                             "recompra podría ser venta de lavado (wash sale) de pérdidas de diciembre.",
                             f"Purchases from January 1-30, {book.year + 1}: a repurchase could make a December loss "
                             "a wash sale."))
    return rows, missing, warnings


_SHORT_BOXES, _LONG_BOXES = frozenset("ABC"), frozenset("DEF")


def _lot_row(doc_id: str, institution: str, lot: Mapping[str, Any], account_id: str | None) -> dict:
    """A Form 8949 row from a 1099-B lot as the broker printed it (the source of truth for that sale)."""
    acquired, sold = lot.get("acquired"), lot.get("sold")
    proceeds, basis, wash = _d(lot.get("proceeds")), _d(lot.get("basis")), _d(lot.get("wash_sale_disallowed"))
    gain = _d(lot.get("gain"))
    if gain is None and proceeds is not None and basis is not None:
        gain = proceeds - basis + (wash or ZERO)
    box = lot.get("box") if lot.get("box") in _SHORT_BOXES | _LONG_BOXES else None
    term = lot.get("term") if lot.get("term") in ("short", "long") else None
    if term is None and box:
        term = "short" if box in _SHORT_BOXES else "long"
    if term is None and acquired and sold and acquired != "VARIOUS":
        try:
            term = "long" if _long_term(acquired, sold) else "short"
        except ValueError:
            term = None
    if box is None and term:
        box = "A" if term == "short" else "D"  # a US broker's 1099-B: basis reported (see the assumptions)
    quantity = _q(_d(lot.get("quantity")))
    what = lot.get("symbol") or lot.get("description")
    row = {"account": institution, "account_id": account_id,
           "description": " ".join(x for x in (quantity, what) if x) or "?", "instrument_id": None,
           "date_acquired": acquired, "date_sold": sold, "proceeds_usd": _m(proceeds), "cost_usd": _m(basis),
           "code": "W" if wash else "", "adjustment_usd": _m(wash) if wash else None, "gain_usd": _m(gain),
           "term": term, "box": box, "permanently_disallowed_usd": None, "currency": "USD",
           "source": "1099-B", "document_id": doc_id}
    if term is None:
        row["note"] = "The 1099-B does not say whether this lot is short or long term."
    return row


def _us_8949(book: _Book) -> tuple[dict, dict]:
    """Form 8949 rows: a broker's 1099-B lots where it listed them (the source of truth), else the ledger's.

    A sale is never counted twice: the lots of a 1099-B replace the ledger's rows for the accounts it
    covers, and the ledger's figures for those accounts are shown against the document instead."""
    sid = "us_8949"
    ledger_rows, missing, warnings = _us_rows(book)
    groups: dict[str, list[dict]] = {}
    for doc in book.constancias:
        if isinstance(doc.get("form_1099_b"), dict):
            name = next((k for k in groups if _same_institution(k, doc.get("institution"))),
                        doc.get("institution") or doc["id"])
            groups.setdefault(name, []).append(doc)
    recon, document_rows, replaced = [], [], set()
    for name, docs in groups.items():
        doc = _combine(docs)
        block = doc["form_1099_b"]
        accounts = book.accounts_for(doc)
        ours_rows = [r for r in ledger_rows if r["account_id"] in accounts]
        with_lots = [d for d in docs if d["form_1099_b"].get("lots")]
        use_lots = bool(with_lots) and (len(with_lots) == len(docs) or not ours_rows)
        mine: list[dict] = []
        if use_lots:
            single = next(iter(accounts)) if len(accounts) == 1 else None
            mine = [_lot_row(d["id"], name, lot, single) for d in with_lots for lot in d["form_1099_b"]["lots"]]
            document_rows += mine
            replaced |= accounts
            for d in docs:
                if d not in with_lots:
                    missing.append(_need(sid, f"constancia.{d['id']}.form_1099_b.lots",
                                         f"Los lotes del 1099-B {d['id']} de {name} (solo se guardaron sus totales).",
                                         f"The lots of {name}'s 1099-B {d['id']} (only its totals were saved)."))
            if not ours_rows:
                warnings.append(f"{name}: the Form 8949 rows are the 1099-B's lots; the ledger has no sales there "
                                "to check them against.")
        elif with_lots:
            warnings.append(f"{name}: only some of the 1099-Bs list their lots; the rows come from the ledger and "
                            "each 1099-B's totals are reconciled against it.")
        ours_known = bool(ours_rows)
        ours_wash = sum((_d(r["adjustment_usd"]) or ZERO for r in ours_rows), ZERO) if ours_known else None
        theirs_wash = _d(block.get("wash_sale_disallowed"))
        if theirs_wash is None and mine:
            theirs_wash = sum((_d(r["adjustment_usd"]) or ZERO for r in mine), ZERO)
        if theirs_wash is not None:
            recon.append(_recon(f"{name}: wash sale loss disallowed (1099-B box 1g)", ours_wash, theirs_wash,
                                "1099-B", doc, "form_1099_b", "wash_sale_disallowed"))
            if use_lots and ours_wash is not None and ours_wash > theirs_wash + CENT:
                warnings.append(f"{name}: the ledger finds {_m(ours_wash - theirs_wash)} USD more wash-sale loss "
                                "than the 1099-B (a replacement bought in another account, which the broker cannot "
                                "see): add that adjustment (code W) to the rows from the 1099-B.")
        for term, key in (("short", "short_term_gain"), ("long", "long_term_gain")):
            theirs = _d(block.get(key))
            if theirs is None and mine:
                chosen = [r for r in mine if r["term"] == term]
                theirs = _sum(_d(r["gain_usd"]) for r in chosen) if chosen else None
            if theirs is None:
                continue
            ours = _sum(_d(r["gain_usd"]) for r in ours_rows if r["term"] == term) if ours_known else None
            recon.append(_recon(f"{name}: {term}-term gain (1099-B)", ours, theirs, "1099-B", doc, "form_1099_b",
                                key))
    rows = [r for r in ledger_rows if r["account_id"] not in replaced] + document_rows
    totals: dict[str, dict[str, Decimal | None]] = {}
    for term in ("short", "long"):
        chosen = [r for r in rows if r["term"] == term]
        totals[term] = {"proceeds": _sum(_d(r["proceeds_usd"]) for r in chosen),
                        "cost": _sum(_d(r["cost_usd"]) for r in chosen),
                        "adjustments": sum((_d(r["adjustment_usd"]) or ZERO for r in chosen), ZERO),
                        "gain": _sum(_d(r["gain_usd"]) for r in chosen), "count": len(chosen)}
    unknown_term = [r for r in rows if r["term"] is None]
    if unknown_term:
        totals["short"]["gain"] = totals["long"]["gain"] = None
        # The gain itself may be known: shown apart so the pack says how much awaits a term.
        totals["unknown_term"] = {"proceeds": _sum(_d(r["proceeds_usd"]) for r in unknown_term),
                                  "cost": _sum(_d(r["cost_usd"]) for r in unknown_term),
                                  "adjustments": sum((_d(r["adjustment_usd"]) or ZERO for r in unknown_term), ZERO),
                                  "gain": _sum(_d(r["gain_usd"]) for r in unknown_term), "count": len(unknown_term)}
    us_brokers = sorted({r["account"] for r in rows if r["box"] in {"A", "D"}})
    for broker in us_brokers:
        if not any(_same_institution(name, broker) for name in groups):
            missing.append(_need(sid, f"constancia.{fold(broker).replace(' ', '_')}.{book.year}.1099b",
                                 f"El 1099-B {book.year} de {broker} (fuente de verdad del costo y de las ventas de "
                                 "lavado).", f"{broker}'s {book.year} Form 1099-B (the source of truth for basis and "
                                             "wash sales)."))
    summary = {term: {k: (v if k == "count" else _m(v)) for k, v in data.items()} for term, data in totals.items()}
    status = "not_applicable" if not rows and book.covers_year() else None
    if not rows and not book.covers_year():
        missing.append(_need(sid, f"ledger.{book.year}", f"Estados de cuenta de todo {book.year}.",
                             f"Statements covering all of {book.year}."))
    section = _section(
        sid, "Formulario 8949 (lotes vendidos)", "Form 8949 (lots sold)", "US", currency="USD", summary=summary,
        columns=_cols(("box", "Casilla", "Box"), ("description", "(a) Descripción", "(a) Description"),
                      ("date_acquired", "(b) Adquirido", "(b) Acquired"), ("date_sold", "(c) Vendido", "(c) Sold"),
                      ("proceeds_usd", "(d) Ingreso", "(d) Proceeds"), ("cost_usd", "(e) Costo", "(e) Cost basis"),
                      ("code", "(f) Código", "(f) Code"), ("adjustment_usd", "(g) Ajuste", "(g) Adjustment"),
                      ("gain_usd", "(h) Ganancia/pérdida", "(h) Gain or loss"), ("account", "Cuenta", "Account")),
        rows=rows, reconciliation=recon, missing=missing, warnings=warnings, status=status,
        sources=[SRC_8949, SRC_PUB550, SRC_RR_2008_5],
        assumptions=["Lots are relieved first-in first-out unless the sale named its lots.",
                     "Foreign-currency sales convert proceeds at the sale-date rate and cost at the purchase-date rate.",
                     "Where a broker's 1099-B lists its lots, those lots are the rows for its accounts (the "
                     "ledger's sales there are only reconciled against them, never added).",
                     "Box A/D assumes the US broker reported basis to the IRS (check the 1099-B; otherwise B/E); "
                     "foreign brokers issue no 1099-B (box C/F).",
                     "Wash sales: purchases of the same security (same underlying symbol) in any of your accounts "
                     "within 30 days before or after a loss sale, matched share for share; the disallowed loss is "
                     "added to the replacement's basis with its holding period (spread over the replacement lot)."])
    return section, totals


def _us_schedule_d(book: _Book, totals: dict, cap_gain_distributions: Decimal | None) -> dict:
    sid = "us_schedule_d"
    st, lt = totals["short"]["gain"], totals["long"]["gain"]
    carry = book.us.get("capital_loss_carryover")
    missing = []
    status_filing = book.us.get("filing_status")
    limit = US_CAPITAL_LOSS_LIMIT.get(status_filing, Decimal(3000))
    if not isinstance(carry, dict):
        missing.append(_need(sid, f"tax.{book.year}.us.capital_loss_carryover",
                             "Pérdida de capital arrastrada del año anterior (corto y largo plazo; 0 si ninguna), de la "
                             "hoja de trabajo del Schedule D del año pasado.",
                             "Capital loss carryover from last year (short and long term; 0 if none), from last year's "
                             "Schedule D worksheet."))
        st_carry = lt_carry = None
    else:
        st_carry, lt_carry = _d(carry.get("short_term")), _d(carry.get("long_term"))
    lt_all = None if lt is None else lt + (cap_gain_distributions or ZERO)
    summary: dict[str, Any] = {"short_term_before_carryover_usd": _m(st), "long_term_before_carryover_usd": _m(lt_all),
                               "capital_gain_distributions_usd": _m(cap_gain_distributions),
                               "loss_limit_usd": _m(limit)}
    if totals.get("unknown_term"):
        summary["unknown_term_gain_usd"] = _m(totals["unknown_term"]["gain"])
    if st is None or lt_all is None or st_carry is None or lt_carry is None:
        summary.update(net_short_term_usd=None, net_long_term_usd=None, net_capital_gain_or_loss_usd=None,
                       deductible_loss_usd=None, carryover_to_next_year=None)
    else:
        from ..tax import _net_us
        net = _net_us(st - abs(st_carry), lt_all - abs(lt_carry), limit)
        summary.update(net_short_term_usd=_m(net["net_short_term"]), net_long_term_usd=_m(net["net_long_term"]),
                       net_capital_gain_or_loss_usd=_m(net["net_short_term"] + net["net_long_term"]),
                       deductible_loss_usd=_m(net["ordinary_income_loss_deduction"]),
                       carryover_to_next_year={"short_term_usd": _m(net["short_term_carryforward"]),
                                               "long_term_usd": _m(net["long_term_carryforward"])})
    if not status_filing:
        missing.append(_need(sid, f"tax.{book.year}.us.filing_status", "Estado civil fiscal (single, married_filing_"
                             "jointly, married_filing_separately, head_of_household).",
                             "Filing status (single, married_filing_jointly, married_filing_separately, "
                             "head_of_household)."))
    rows = [{"line": "1a-3", "part": "I (short term)", **{k: _m(v) if k != "count" else v for k, v in totals["short"].items()}},
            {"line": "8a-10", "part": "II (long term)", **{k: _m(v) if k != "count" else v for k, v in totals["long"].items()}}]
    return _section(sid, "Schedule D (totales y pérdidas arrastradas)", "Schedule D (totals and carryovers)", "US",
                    currency="USD", summary=summary,
                    columns=_cols(("part", "Parte", "Part"), ("count", "Lotes", "Lots"),
                                  ("proceeds", "Ingreso", "Proceeds"), ("cost", "Costo", "Cost"),
                                  ("adjustments", "Ajustes", "Adjustments"), ("gain", "Ganancia/pérdida", "Gain/loss")),
                    rows=rows, missing=missing, sources=[SRC_SCHED_D],
                    assumptions=["Short- and long-term results are netted against each other after the carryover; "
                                 "net losses deduct up to $3,000 ($1,500 married filing separately) and the rest "
                                 "carries over with its character."])


def _us_accounts(book: _Book, person_scope: bool) -> set[str]:
    """US-reportable taxable accounts: all of a US person's accounts (worldwide income), else US-country ones."""
    out = set()
    for account_id in book.accounts:
        if book.kind(account_id) in _TAX_SHELTERED:
            continue
        if person_scope or book.country(account_id)[0] == "US":
            out.add(account_id)
    return out


def _us_1099(book: _Book, scope: set[str]) -> tuple[dict, dict, Decimal | None]:
    sid = "us_1099"
    missing, rows, recon = [], [], []
    per_account: dict[str, dict] = {}
    foreign_tax: dict[str, Decimal | None] = {}
    for e in book.entries:
        if e["account_id"] not in scope or not book.start <= e["date"] <= book.end:
            continue
        if e["kind"] not in {"dividend", "interest", "tax_withheld"}:
            continue
        value = book.convert(_d(e.get("amount")), e.get("currency"), "USD", e["date"])
        bucket = per_account.setdefault(e["account_id"], {"dividend": ZERO, "interest": ZERO, "tax_withheld": ZERO,
                                                          "known": True})
        if value is None:
            bucket["known"] = False
            missing.append(_need(sid, f"fx.{e.get('currency')}/USD@{e['date']}",
                                 f"Tipo de cambio {e.get('currency')}/USD del {e['date']}.",
                                 f"A {e.get('currency')}/USD rate for {e['date']}."))
            continue
        bucket[e["kind"]] += value
        if e["kind"] == "tax_withheld":
            meta = book.instruments.get(e.get("instrument_id") or "") or {}
            account_country = book.country(e["account_id"])[0]
            country = account_country if account_country and account_country != "US" else \
                str(meta.get("issuer_domicile") or meta.get("country") or "US")
            if country != "US":
                foreign_tax[country] = (foreign_tax.get(country) or ZERO) + abs(value)
    cap_gain_distributions: Decimal | None = ZERO
    # Accounts one document (or one institution's documents, summed) covers are one row: never counted twice.
    groups: dict[str, dict] = {}
    for account_id, bucket in sorted(per_account.items()):
        doc = book.constancia_for([account_id], book.institution(account_id), ("form_1099_div", "form_1099_int"))
        group = groups.setdefault(doc["id"] if doc else account_id, {
            "account_id": account_id, "doc": doc, "dividend": ZERO, "interest": ZERO, "tax_withheld": ZERO,
            "known": True})
        for kind in ("dividend", "interest", "tax_withheld"):
            group[kind] += bucket[kind]
        group["known"] = group["known"] and bucket["known"]
    # A 1099 whose accounts the ledger does not have still counts: its figures are shown and declared.
    used = {p["id"] for g in groups.values() if g["doc"] for p in (g["doc"].get("_parts") or [g["doc"]])}
    loose: dict[str, list[dict]] = {}
    for d in book.constancias:
        if d["id"] not in used and any(isinstance(d.get(b), dict) for b in ("form_1099_div", "form_1099_int")):
            name = next((k for k in loose if _same_institution(k, d.get("institution"))),
                        d.get("institution") or d["id"])
            loose.setdefault(name, []).append(d)
    for name, docs in loose.items():
        doc = _combine(docs)
        groups[doc["id"]] = {"account_id": None, "name": name, "doc": doc, "dividend": ZERO, "interest": ZERO,
                             "tax_withheld": ZERO, "known": False}
    for group in groups.values():
        account_id, bucket, doc = group["account_id"], group, group["doc"]
        name = group.get("name") or book.institution(account_id)
        country = book.country(account_id)[0] if account_id else "US"
        div = (doc or {}).get("form_1099_div") or {}
        intr = (doc or {}).get("form_1099_int") or {}
        ours_div = bucket["dividend"] if bucket["known"] else None
        ours_int = bucket["interest"] if bucket["known"] else None
        qualified = _d(div.get("qualified"))
        if qualified is None and bucket["known"] and bucket["dividend"] == 0:
            qualified = ZERO  # no dividends: nothing to qualify
        rows.append({"account": name, "country": country, "dividends_usd": _m(ours_div),
                     "form_ordinary_dividends_usd": _m(_d(div.get("ordinary"))),
                     "qualified_dividends_usd": _m(qualified),
                     "ordinary_non_qualified_usd": _m(None if qualified is None or _d(div.get("ordinary")) is None
                                                      else _d(div["ordinary"]) - qualified),
                     "capital_gain_distributions_usd": _m(_d(div.get("capital_gain_distributions"))),
                     "interest_usd": _m(ours_int), "form_interest_usd": _m(_d(intr.get("interest"))),
                     "foreign_tax_paid_usd": _m(_d(div.get("foreign_tax_paid"))
                                                if div.get("foreign_tax_paid") is not None else None),
                     "tax_withheld_usd": _m(abs(bucket["tax_withheld"])) if bucket["known"] else None,
                     "basis": "1099" if doc else "ledger"})
        cgd = _d(div.get("capital_gain_distributions"))
        if cgd is not None and cap_gain_distributions is not None:
            cap_gain_distributions += cgd
        if doc:
            for label, ours, theirs, block, field in (
                    ("dividends (1099-DIV 1a)", ours_div, _d(div.get("ordinary")), "form_1099_div", "ordinary"),
                    ("interest (1099-INT 1)", ours_int, _d(intr.get("interest")), "form_1099_int", "interest")):
                if theirs is not None:
                    recon.append(_recon(f"{name}: {label}", ours, theirs, "1099", doc, block, field))
            ftp = _d(div.get("foreign_tax_paid"))
            if ftp is not None and country == "US":
                foreign_tax["per 1099-DIV box 7"] = (foreign_tax.get("per 1099-DIV box 7") or ZERO) + ftp
        elif country == "US" and (bucket["dividend"] or bucket["interest"]):
            missing.append(_need(sid, f"constancia.{fold(name).replace(' ', '_')}.{book.year}.1099",
                                 f"1099-DIV/INT {book.year} de {name}: dice qué dividendos son calificados.",
                                 f"{name}'s {book.year} 1099-DIV/INT: it says which dividends are qualified."))
        elif country != "US" and bucket["dividend"]:
            missing.append(_need(sid, f"qualified.{account_id}",
                                 f"Dividendos de {name} (extranjero): ¿calificados? Emisoras de países con tratado "
                                 "(México) pueden serlo si se cumplen 61 días de tenencia.",
                                 f"{name} (foreign) dividends: qualified? Treaty-country issuers (Mexico) can be, with "
                                 "the 61-day holding period."))
    totals = {"dividends_usd": _m(_sum(_d(r["dividends_usd"]) for r in rows)) if rows else _m(ZERO),
              "qualified_dividends_usd": _m(_sum(_d(r["qualified_dividends_usd"]) for r in rows)) if rows else _m(ZERO),
              "interest_usd": _m(_sum(_d(r["interest_usd"]) for r in rows)) if rows else _m(ZERO),
              # What goes on the return: each account's 1099 figure where there is one, else the ledger's.
              "ordinary_dividends_to_report_usd": _m(_sum(
                  _d(r["form_ordinary_dividends_usd"]) if r["form_ordinary_dividends_usd"] is not None
                  else _d(r["dividends_usd"]) for r in rows)) if rows else _m(ZERO),
              "interest_to_report_usd": _m(_sum(_d(r["form_interest_usd"]) if r["form_interest_usd"] is not None
                                                else _d(r["interest_usd"]) for r in rows)) if rows else _m(ZERO)}
    section = _section(
        sid, "Resumen 1099-DIV / 1099-INT", "1099-DIV / 1099-INT summary", "US", currency="USD", summary=totals,
        columns=_cols(("account", "Cuenta", "Account"), ("dividends_usd", "Dividendos (ledger)", "Dividends (ledger)"),
                      ("form_ordinary_dividends_usd", "1a ordinarios (1099)", "1a ordinary (1099)"),
                      ("qualified_dividends_usd", "1b calificados", "1b qualified"),
                      ("capital_gain_distributions_usd", "2a dist. de ganancias", "2a cap. gain distr."),
                      ("interest_usd", "Intereses (ledger)", "Interest (ledger)"),
                      ("form_interest_usd", "Intereses (1099-INT)", "Interest (1099-INT)"),
                      ("tax_withheld_usd", "Impuesto retenido", "Tax withheld")),
        rows=rows, reconciliation=recon, missing=missing, status="not_applicable" if not rows else None,
        sources=[SRC_1099DIV],
        assumptions=["Qualified status comes only from the 1099-DIV; without it a dividend is not assumed qualified.",
                     "A US person's foreign-account income is included, converted at the payment-date rate."])
    return section, foreign_tax, cap_gain_distributions


def _us_foreign_tax(book: _Book, foreign_tax: dict[str, Decimal | None], mx_resident: bool) -> dict:
    sid = "us_foreign_tax"
    rows = [{"country": country, "tax_usd": _m(amount), "category": "passive"}
            for country, amount in sorted(foreign_tax.items())]
    missing, warnings = [], []
    stated_isr = _d(book.us.get("mx_annual_isr_usd"))
    passive_isr = _d(book.us.get("mx_annual_isr_passive_usd"))
    general_isr = _d(book.us.get("mx_annual_isr_general_usd"))
    if passive_isr is not None or general_isr is not None:
        # IRC 904(d): passive (interest, dividends, Art. 129 gains) and general (wages) baskets are separate.
        for label, amount, category in (("MX annual ISR on interest, dividends and gains (stated)", passive_isr,
                                         "passive"),
                                        ("MX annual ISR on wages and other income (stated)", general_isr, "general")):
            if amount is not None:
                rows.append({"country": label, "tax_usd": _m(amount), "category": category})
                foreign_tax = {**foreign_tax, label: amount}
        if stated_isr is not None and passive_isr is not None and general_isr is not None \
                and abs(passive_isr + general_isr - stated_isr) > CENT:
            warnings.append("The passive and general parts of the Mexican ISR do not add to the stated total.")
    elif stated_isr is not None:
        passive_income = _mx_passive_income(book)
        if passive_income:
            rows.append({"country": "MX (annual ISR, stated)", "tax_usd": _m(stated_isr), "category": "unsplit"})
            warnings.append("The Mexican annual ISR covers wages and investment income (interest, dividends, Art. 129 "
                            "gains): Form 1116 needs it split between the passive and general categories (IRC "
                            "904(d)), apportioned by income. It is left unsplit here.")
            missing.append(_need(sid, f"tax.{book.year}.us.mx_annual_isr_passive_usd",
                                 "Parte del ISR anual mexicano que corresponde a intereses, dividendos y ganancias "
                                 "(categoría pasiva del 1116); el resto es general.",
                                 "The part of the Mexican annual ISR on interest, dividends and gains (Form 1116 "
                                 "passive category); the rest is general."))
        else:
            rows.append({"country": "MX (annual ISR, stated)", "tax_usd": _m(stated_isr), "category": "general"})
        foreign_tax = {**foreign_tax, "MX (annual ISR, stated)": stated_isr}
    elif mx_resident:
        missing.append(_need(sid, f"tax.{book.year}.us.mx_annual_isr_usd",
                             "ISR mexicano pagado en la declaración anual (acreditable en el 1116, categoría general "
                             "para salarios).", "Mexican ISR paid with the annual return (creditable on Form 1116; "
                                                "general category for wages)."))
    return _section(sid, "Impuesto extranjero pagado (Form 1116)", "Foreign tax paid (Form 1116 inputs)", "US",
                    currency="USD", summary={"total_usd": _m(_sum(foreign_tax.values())) if foreign_tax else _m(ZERO)},
                    columns=_cols(("country", "País", "Country"), ("tax_usd", "Impuesto USD", "Tax USD"),
                                  ("category", "Categoría", "Category")),
                    rows=rows, missing=missing, warnings=warnings,
                    status="not_applicable" if not rows and not missing else None,
                    sources=[SRC_1116],
                    assumptions=["Withholding on dividends and interest from foreign accounts or foreign issuers is "
                                 "passive-category foreign tax, converted at the payment-date rate.",
                                 "Only the tax legally owed is creditable: a withholding above the treaty rate is a "
                                 "refund claim, not a credit."])


def _rmd_start(birth: int, year: int, params: Any) -> tuple[Any, bool | None, str | None]:
    """(applicable age, whether an RMD is due for ``year``, note) under IRC 401(a)(9)(C) as amended.

    SECURE Act sec. 114 (2019): 72 for those who reach 70½ after 2019 (born July 1, 1949 or later);
    SECURE 2.0 sec. 107: 73 for those born 1951-1959 and 75 from 1960.  Born before July 1, 1949: 70½.
    The first RMD is for the year the age is reached; None when a birth year alone cannot tell."""
    if birth >= 1951:
        age = retirement.rmd_age(birth, params)
        return age, (None if age is None else year - birth >= age), None
    if birth == 1950:
        return 72, year - birth >= 72, "Born in 1950: RMD age 72 (SECURE Act sec. 114)."
    # Born 1949: 70½ if born before July 1 (first RMD year 2019), else 72 (first RMD year 2021).
    # Born 1948 or earlier: 70½, reached in the year of the 70th or 71st birthday.
    first_early, first_late = (2019, 2021) if birth == 1949 else (birth + 70, birth + 71)
    due = True if year >= first_late else (False if year < first_early else None)
    note = ("Born in 1949: RMD age 70½ if born before July 1, 1949, else 72; either way RMDs are due from 2021."
            if birth == 1949 else f"Born in {birth}: RMD age 70½ (born before July 1, 1949).")
    return (70.5 if birth <= 1948 else None), due, note


def _deferral_limit(book: _Book, age: int | None) -> tuple[Decimal | None, str | None, dict | None]:
    """IRC 402(g)(1) plus the 414(v) catch-up for the year: (limit, catch-up basis, source)."""
    override = book.parameters.get("us_402g_deferral_limit")
    table = US_DEFERRAL_LIMITS.get(book.year) or {}
    if isinstance(override, dict) and _d(override.get("value")) is not None:
        base, source = _d(override["value"]), {"title": str(override.get("source") or "caller-supplied")}
    elif table:
        base, source = Decimal(table["deferral"]), table["source"]
    else:
        return None, None, None
    if age is None:
        return None, None, source
    if 60 <= age <= 63 and table.get("catch_up_60_63"):
        return base + Decimal(table["catch_up_60_63"]), "ages 60-63 (IRC 414(v)(2)(E))", source
    if age >= 50 and table.get("catch_up_50"):
        return base + Decimal(table["catch_up_50"]), "age 50+ (IRC 414(v)(2)(B))", source
    if age >= 50:
        return None, None, source
    return base, "not eligible (under 50)", source


def _mx_passive_income(book: _Book) -> bool:
    """Interest, dividends or sales in a Mexican account this year: income in the 1116 passive category."""
    return any(book.country(e["account_id"])[0] == "MX" and book.start <= e["date"] <= book.end
               and e["kind"] in {"interest", "dividend", "sell"} and book.kind(e["account_id"]) in {"bank", "brokerage"}
               for e in book.entries)


def _us_retirement(book: _Book) -> dict | None:
    sid = "us_retirement"
    # Traditional IRAs (with SEP and SIMPLE IRAs) and Roth IRAs; employer plans are their own group.
    ira = [a for a in book.accounts if book.kind(a) in {"ira", "roth"}]
    workplace = [a for a in book.accounts if book.kind(a) == "workplace"]
    # IRC 219(b)(5): only deposits into traditional and Roth IRAs are IRA contributions.
    contributory = [a for a in ira if str(book.accounts[a].get("type")) in _US_IRA_ONLY]
    stated_trad, stated_roth = _d(book.us.get("ira_contributions_usd")), _d(book.us.get("roth_contributions_usd"))
    stated_deferrals = _d(book.us.get("elective_deferrals_usd"))
    forms = [d for d in book.constancias if isinstance(d.get("form_5498"), dict)]
    if not ira and not workplace and stated_trad is None and stated_roth is None and not forms \
            and stated_deferrals is None:
        return None
    missing, warnings, rows, recon = [], [], [], []
    if forms:
        # Form 5498 is the source of truth: it includes contributions made by April 15 for this year and
        # excludes rollovers and conversions, which deposits in the ledger cannot tell apart.
        def form_sum(field: str) -> Decimal | None:
            values = [_d(d["form_5498"].get(field)) for d in forms]
            return None if all(v is None for v in values) else sum((v or ZERO for v in values), ZERO)

        doc_trad, doc_roth = form_sum("ira_contributions"), form_sum("roth_contributions")
        ours = {"ira": ZERO, "roth": ZERO}
        for e in book.entries:
            if (e["account_id"] in contributory and book.start <= e["date"] <= book.end and not e.get("instrument_id")
                    and e["kind"] in {"deposit", "transfer"}):
                amount = book.convert(_d(e.get("amount")), e.get("currency"), "USD", e["date"])
                if amount is not None and amount > 0:
                    ours[book.kind(e["account_id"])] += amount
        for label, kind, stated, theirs in (("traditional IRA contributions (5498 box 1)", "ira", stated_trad, doc_trad),
                                            ("Roth IRA contributions (5498 box 10)", "roth", stated_roth, doc_roth)):
            if theirs is None:
                continue
            mine = stated if stated is not None else (ours[kind] if contributory else None)
            recon.append({"item": label, "ours": _m(mine), "document": _m(theirs),
                          "difference": _m(None if mine is None else mine - theirs), "source_of_truth": "5498",
                          "document_id": ", ".join(d["id"] for d in forms)})
        stated_trad = doc_trad if doc_trad is not None else stated_trad
        stated_roth = doc_roth if doc_roth is not None else stated_roth
    ledger_contrib = {"ira": ZERO, "roth": ZERO}
    withdrawals = {"ira": ZERO, "roth": ZERO, "workplace": ZERO}
    plan_deposits = ZERO
    other_ira_deposits = ZERO
    for e in book.entries:
        if e["account_id"] not in ira and e["account_id"] not in workplace:
            continue
        if not book.start <= e["date"] <= book.end or e.get("instrument_id"):
            continue
        amount = book.convert(_d(e.get("amount")), e.get("currency"), "USD", e["date"])
        if amount is None:
            continue
        kind = book.kind(e["account_id"])
        if e["kind"] in {"deposit", "transfer"} and amount > 0:
            if kind == "workplace":
                plan_deposits += amount
            elif e["account_id"] in contributory:
                ledger_contrib[kind] += amount
            else:
                other_ira_deposits += amount
        elif e["kind"] in {"withdrawal", "transfer"} and amount < 0:
            withdrawals[kind] += -amount
    # IRA deposits in the ledger are the contributions; with no IRA in the ledger they are unknown, not zero.
    trad = stated_trad if stated_trad is not None else (ledger_contrib["ira"] if ira else None)
    roth = stated_roth if stated_roth is not None else (ledger_contrib["roth"] if ira else None)
    if (stated_trad is None or stated_roth is None) and not forms and contributory:
        warnings.append("Contributions are read from deposits into IRA accounts; rollovers, conversions and "
                        "prior-year contributions made by April 15 must be excluded (Form 5498 is the source).")
    if other_ira_deposits:
        warnings.append("Deposits into SEP or SIMPLE IRAs are employer contributions or SIMPLE deferrals with their "
                        "own limits (IRC 402(h), 408(p)); they are not counted against the IRA limit.")
    params = retirement._Params({"parameters": book.parameters})
    limit = params.get("us_ira_limit", book.year)
    birth = book.profile.get("birth_year")
    age = book.year - birth if isinstance(birth, int) else None
    catch = params.get("us_ira_catch_up", book.year) if age is not None and age >= 50 else None
    total_limit = None
    if limit is not None and age is not None:
        total_limit = Decimal(str(limit)) + (Decimal(str(catch)) if age >= 50 and catch is not None else ZERO)
        if age >= 50 and catch is None:
            total_limit = None
    if age is None:
        missing.append(_need(sid, "client.profile.birth_year", "Año de nacimiento (límite de recuperación a partir de "
                             "50 años y edad de RMD).", "Birth year (catch-up from age 50 and RMD age)."))
    for item in params.missing:
        missing.append(_need(sid, item, f"Límite IRA {book.year} (IRS) con su fuente.",
                             f"The {book.year} IRA limit (IRS) with its source."))
    combined = None if trad is None or roth is None else trad + roth
    rows.append({"item": "IRA + Roth contributions", "traditional_usd": _m(trad), "roth_usd": _m(roth),
                 "combined_usd": _m(combined), "limit_usd": _m(total_limit),
                 "excess_usd": _m(None if combined is None or total_limit is None else max(combined - total_limit, ZERO))})
    # Employer plans: elective deferrals against IRC 402(g) (401(k), 403(b); 457(b) has its own equal limit).
    deferral_row = None
    extra_sources: list[dict] = []
    if workplace or stated_deferrals is not None:
        deferral_limit, catch_basis, deferral_source = _deferral_limit(book, age)
        if deferral_source:
            extra_sources.append(deferral_source)
        else:
            missing.append(_need(sid, "parameters.us_402g_deferral_limit",
                                 f"Límite de aportaciones diferidas {book.year} (IRC 402(g)) con su fuente.",
                                 f"The {book.year} elective deferral limit (IRC 402(g)) with its source."))
        if stated_deferrals is None and workplace:
            missing.append(_need(sid, f"tax.{book.year}.us.elective_deferrals_usd",
                                 "Aportaciones diferidas del año al 401(k)/403(b)/457(b) (W-2 casilla 12, códigos D, "
                                 "E, G, AA, BB, EE); los depósitos del estado de cuenta incluyen las del patrón.",
                                 "This year's elective deferrals to the 401(k)/403(b)/457(b) (W-2 box 12, codes D, E, "
                                 "G, AA, BB, EE); plan deposits also include employer contributions."))
        deferral_row = {"item": "Elective deferrals (401(k)/403(b)/457(b))",
                        "plan_deposits_usd": _m(plan_deposits) if workplace else None,
                        "deferrals_usd": _m(stated_deferrals), "limit_usd": _m(deferral_limit),
                        "catch_up_basis": catch_basis,
                        "excess_usd": _m(None if stated_deferrals is None or deferral_limit is None
                                         else max(stated_deferrals - deferral_limit, ZERO)),
                        "rule": "IRC 402(g)(1): one limit per person across all 401(k), 403(b), SARSEP and SIMPLE "
                                "plans (traditional and Roth deferrals together); a governmental 457(b) has its own "
                                "equal limit (IRC 457(e)(15)). Catch-up (IRC 414(v)): age 50+, or the higher amount "
                                "at ages 60-63 (SECURE 2.0 sec. 109, from 2025). Employer contributions count only "
                                "against IRC 415(c)."}
        rows.append(deferral_row)
        if book.year >= 2026:
            warnings.append("From 2026, catch-up deferrals of employees whose prior-year FICA wages from the plan "
                            "sponsor exceeded $150,000 must be designated Roth (IRC 414(v)(7), SECURE 2.0 sec. 603).")
        if workplace and stated_deferrals is None and deferral_limit is not None and plan_deposits > deferral_limit:
            warnings.append("Plan deposits exceed the deferral limit; they may include employer contributions, "
                            "rollovers or a 457(b)'s separate limit. Confirm deferrals with the W-2.")
    rmd_age = None
    required = None
    if isinstance(birth, int):
        try:
            rmd_age, due, rmd_note = _rmd_start(birth, book.year, params)
        except ValueError:
            rmd_age, due, rmd_note = None, None, None
        if book.year == 2020:
            due, rmd_note = False, "2020 RMDs were waived (CARES Act sec. 2203)."
        if due and ira:
            balance = _d(book.us.get("ira_prior_year_end_balance_usd"))
            if balance is None:
                missing.append(_need(sid, f"tax.{book.year}.us.ira_prior_year_end_balance_usd",
                                     f"Saldo de la IRA tradicional al 31 de diciembre de {book.year - 1} (para el RMD).",
                                     f"Traditional IRA balance on December 31, {book.year - 1} (for the RMD)."))
            else:
                try:
                    value = retirement.rmd_amount(float(balance), age, params)
                except ValueError:
                    value = None
                required = None if value is None else Decimal(str(value))
        elif due is False or (due and not ira):
            required = ZERO
        elif due is None and ira:
            missing.append(_need(sid, "client.profile.birth_date",
                                 "Fecha de nacimiento (para saber si el RMD ya empezó: 70½ o 72 años).",
                                 "Date of birth (whether RMDs have started: age 70½ or 72)."))
        if rmd_note:
            warnings.append(rmd_note)
        if due and workplace:
            warnings.append("Employer-plan RMDs (401(k), 403(b), 457(b)) are figured per plan and cannot be taken from "
                            "an IRA (403(b)s may be combined among themselves); a participant still working who "
                            "is not a 5% owner may delay them (IRC 401(a)(9)(C)). Roth 401(k)s have no lifetime RMD "
                            "from 2024.")
    taken = _d(book.us.get("rmd_taken_usd"))
    taken = taken if taken is not None else (withdrawals["ira"] if ira else None)
    rows.append({"item": "RMD", "required_usd": _m(required), "taken_usd": _m(taken), "rmd_start_age": rmd_age,
                 "shortfall_usd": _m(None if required is None or taken is None else max(required - taken, ZERO)),
                 "accounts": "traditional, SEP and SIMPLE IRAs (aggregated); Roth IRAs have no owner RMD"})
    if any(withdrawals.values()) and age is not None and age < 59:
        warnings.append("A withdrawal before 59½ may carry the 10% additional tax unless an exception applies.")
    summary = {"contributions": rows[0], "rmd": rows[-1]}
    if deferral_row:
        summary["workplace_deferrals"] = deferral_row
    return _section(sid, "IRA / Roth / 401(k): aportaciones y RMD", "IRA / Roth / 401(k): contributions and RMDs",
                    "US", currency="USD", summary=summary,
                    columns=_cols(("item", "Concepto", "Item"), ("traditional_usd", "Tradicional", "Traditional"),
                                  ("roth_usd", "Roth", "Roth"), ("combined_usd", "Total", "Combined"),
                                  ("deferrals_usd", "Diferidas (W-2)", "Deferrals (W-2)"),
                                  ("limit_usd", "Límite", "Limit"), ("excess_usd", "Exceso", "Excess"),
                                  ("required_usd", "RMD requerido", "RMD required"), ("taken_usd", "Retirado", "Taken")),
                    rows=rows, reconciliation=recon, missing=missing, warnings=warnings,
                    sources=[SRC_590A, SRC_590B, *params.sources, *extra_sources],
                    assumptions=["The IRA limit (IRC 219(b)(5)) is shared by traditional and Roth IRAs only; 401(k), "
                                 "403(b) and 457(b) deferrals are measured against IRC 402(g) instead. Income "
                                 "phase-outs for Roth and deductibility are not applied.",
                                 "Contributions need taxable compensation; excluded foreign earned income (Form 2555) "
                                 "does not count."])


def _foreign_values(book: _Book, prices: Mapping[str, Any] | None) -> tuple[list[dict], list[dict]]:
    """Per foreign account: a lower bound on the year's maximum and on the year-end value, in the account currency."""
    sid = "us_fbar_8938"
    month_ends = [(date(book.year, m % 12 + 1, 1) - timedelta(days=1)).isoformat() if m < 12 else book.end
                  for m in range(1, 13)]
    state, _ = replay(book.ledger, book.end, checkpoints=month_ends)
    series = {}
    if isinstance(prices, dict):
        for key, points in prices.items():
            if isinstance(points, list):
                series[key] = sorted(((p.get("date"), _d(p.get("price"))) for p in points if isinstance(p, dict)
                                      and p.get("date") and _d(p.get("price")) is not None), key=lambda x: x[0])
    rows, missing = [], []
    for account_id in sorted(book.accounts):
        country, basis = book.country(account_id)
        if country in (None, "US") or book.kind(account_id) == "liability":
            continue
        currency = book.accounts[account_id].get("currency") or "MXN"
        best, year_end, securities_unknown = None, None, False
        for day in month_ends:
            snap = state.snapshots.get(day) or {}
            value = sum((amount for (acct, ccy), amount in (snap.get("cash") or {}).items()
                         if acct == account_id and ccy == currency), ZERO)
            for (acct, instrument), qty in (snap.get("quantities") or {}).items():
                if acct != account_id or not qty:
                    continue
                points = [p for d, p in series.get(instrument, []) if d <= day]
                if not points:
                    securities_unknown = True
                    continue
                inst_ccy = (book.instruments.get(instrument) or {}).get("currency") or currency
                converted = book.convert(qty * points[-1], inst_ccy, currency, day)
                if converted is None:
                    securities_unknown = True
                else:
                    value += converted
            best = value if best is None else max(best, value)
            if day == book.end:
                year_end = value
        for assertion in book.ledger.get("assertions") or []:
            if assertion.get("account_id") == account_id and book.start <= assertion.get("date", "") <= book.end \
                    and assertion.get("balance") is not None and assertion.get("currency") == currency:
                best = max(best or ZERO, _d(assertion["balance"]) or ZERO)
        if not book.entries or not any(e["account_id"] == account_id for e in book.entries):
            best = year_end = None
        rows.append({"account_id": account_id, "institution": book.institution(account_id), "country": country,
                     "country_basis": basis, "type": book.accounts[account_id].get("type"), "currency": currency,
                     "max_lower_bound": best, "year_end_lower_bound": year_end,
                     "securities_unpriced": securities_unknown})
        if securities_unknown:
            missing.append(_need(sid, f"prices.{account_id}", f"Precios de cierre de mes de los valores en "
                                 f"{book.institution(account_id)} (para el saldo máximo).",
                                 f"Month-end prices for the securities at {book.institution(account_id)} (for the "
                                 "maximum value)."))
    # Stated balances (cash.<id>, investment.<id>) the ledger does not have, observed in the tax year.
    for key, fact in sorted(book.facts.items()):
        value = fact["value"]
        if not key.startswith(("cash.", "investment.")) or not isinstance(value, dict):
            continue
        if any(fold(r["institution"]) == fold(value.get("institution")) for r in rows):
            continue
        country, basis = _institution_country(value.get("institution"), value.get("currency"))
        if country in (None, "US"):
            continue
        observed = str((fact.get("source") or {}).get("observed_on") or "")
        amount = _d(value.get("amount"))
        in_year = book.start <= observed <= book.end
        book.evidence.add(fact["id"])
        rows.append({"account_id": key, "institution": value.get("institution") or key, "country": country,
                     "country_basis": basis, "type": value.get("kind") or key.split(".")[0],
                     "currency": value.get("currency") or "MXN",
                     "max_lower_bound": amount if in_year else None, "year_end_lower_bound": None,
                     "securities_unpriced": False, "stated": True, "observed_on": observed or None})
        if not in_year:
            missing.append(_need(sid, f"{key}.max_{book.year}", f"Saldo máximo de {value.get('institution') or key} en "
                                 f"{book.year}.", f"The {book.year} maximum balance of "
                                                  f"{value.get('institution') or key}."))
    return rows, missing


def _us_fbar(book: _Book, prices: Mapping[str, Any] | None) -> dict | None:
    sid = "us_fbar_8938"
    rows, missing = _foreign_values(book, prices)
    if not rows:
        return None
    warnings = []
    stated_rate = book.us.get("treasury_rate_per_usd")
    rates: dict[str, Decimal | None] = {}
    rate_source = None
    for row in rows:
        ccy = row["currency"]
        if ccy == "USD":
            rates[ccy] = Decimal(1)
            continue
        if ccy in rates:
            continue
        if isinstance(stated_rate, dict) and _d(stated_rate.get(ccy)) is not None:
            rates[ccy] = Decimal(1) / _d(stated_rate[ccy])
            rate_source = stated_rate.get("source") or "stated Treasury rate"
        else:
            quote = book.fx.quote(ccy, "USD", book.end)
            rates[ccy] = None if quote is None else quote.rate
            if quote is not None:
                rate_source = "ledger rate on or before December 31 (proxy for the Treasury rate)"
                warnings.append(f"{ccy}: the Treasury Reporting Rate for December 31 should be used; the ledger rate "
                                "of the same date is a proxy.")
            else:
                missing.append(_need(sid, f"tax.{book.year}.us.treasury_rate_per_usd.{ccy}",
                                     f"Tipo de cambio del Tesoro de EE.UU. al 31 de diciembre de {book.year} ({ccy} por "
                                     "USD).", f"The US Treasury Reporting Rate for December 31, {book.year} ({ccy} per "
                                              "USD)."))
    table, max_total, end_total, complete = [], ZERO, ZERO, True
    for row in rows:
        rate = rates.get(row["currency"])
        max_usd = None if rate is None or row["max_lower_bound"] is None else row["max_lower_bound"] * rate
        end_usd = None if rate is None or row["year_end_lower_bound"] is None else row["year_end_lower_bound"] * rate
        if max_usd is None or row["securities_unpriced"] or row.get("stated"):
            complete = False
        max_total += max_usd or ZERO
        end_total += end_usd or ZERO
        table.append({"institution": row["institution"], "country": row["country"], "type": row["type"],
                      "currency": row["currency"], "max_value_usd_at_least": _m(max_usd),
                      "year_end_usd_at_least": _m(end_usd),
                      "note": "AFORE/PPR: foreign pension; confirm reporting with your CPA"
                      if row["type"] in {"afore", "ppr", "retirement"} else
                      ("stated balance" if row.get("stated") else None)})
    fbar_required = True if max_total > FBAR_THRESHOLD_USD else (False if complete else None)
    abroad = str((book.profile.get("residence") or {}).get("country") or "").upper() not in {"", "US"}
    married_joint = book.us.get("filing_status") == "married_filing_jointly"
    # The higher "abroad" thresholds need a tax home abroad and bona fide residence for the whole year or
    # 330 full days abroad in 12 months (Treas. Reg. 1.6038D-2(a)(4)); living abroad alone is not enough.
    test = book.us.get("foreign_residence_test")
    qualifies = True if test in {"bona_fide_residence", "physical_presence"} else (
        False if test == "neither" or not abroad else None)
    if qualifies is None:
        missing.append(_need(sid, f"tax.{book.year}.us.foreign_residence_test",
                             "Para el 8938 en el extranjero: ¿residencia de buena fe todo el año o 330 días completos "
                             "fuera de EE.UU. en 12 meses? (bona_fide_residence, physical_presence o neither).",
                             "For the Form 8938 abroad thresholds: bona fide residence abroad for the whole year, or "
                             "330 full days abroad in 12 months? (bona_fide_residence, physical_presence or neither)."))
    # Shown: the abroad thresholds when they may apply; the US ones stay in the table below.
    end_threshold, any_threshold = FORM_8938_THRESHOLDS[("us" if qualifies is False else "abroad", married_joint)]
    high_end, high_any = FORM_8938_THRESHOLDS[("abroad", married_joint)]
    low_end, low_any = FORM_8938_THRESHOLDS[("us", married_joint)]
    if qualifies is None:
        # Unknown test: required when above even the abroad thresholds, not when complete and below the US ones.
        if end_total > high_end or max_total > high_any:
            form_8938 = True
        elif complete and end_total <= low_end and max_total <= low_any:
            form_8938 = False
        else:
            form_8938 = None
    elif end_total > end_threshold or max_total > any_threshold:
        form_8938 = True
    elif complete:
        form_8938 = False
    else:
        form_8938 = None
    if not book.us.get("filing_status"):
        warnings.append("Filing status unknown: the Form 8938 test uses the unmarried thresholds.")
    summary = {"fbar": {"required": fbar_required, "aggregate_max_value_usd_at_least": _m(max_total),
                        "threshold_usd": _m(FBAR_THRESHOLD_USD),
                        "rule": "Aggregate maximum value of all foreign financial accounts above $10,000 at any time in "
                                "the calendar year (31 CFR 1010.350; FinCEN Form 114).",
                        "due": f"{book.year + 1}-04-15 (automatic extension to {book.year + 1}-10-15)"},
               "form_8938": {"required": form_8938, "lives_abroad": abroad, "abroad_thresholds_apply": qualifies,
                             "foreign_residence_test": test, "married_filing_jointly": married_joint,
                             "year_end_value_usd_at_least": _m(end_total), "max_value_usd_at_least": _m(max_total),
                             "threshold_last_day_usd": _m(end_threshold), "threshold_any_time_usd": _m(any_threshold),
                             "rule": "Specified foreign financial assets above the threshold on the last day of the "
                                     "year or at any time (IRC 6038D; Treas. Reg. 1.6038D-2); filed with Form 1040."},
               "rate_source": rate_source,
               "thresholds_table": [{"residence": r, "married_filing_jointly": mfj, "last_day_usd": _m(a),
                                     "any_time_usd": _m(b)} for (r, mfj), (a, b) in FORM_8938_THRESHOLDS.items()]}
    if fbar_required is None:
        warnings.append("The known maximum values do not reach $10,000 but some values are unknown; the FBAR "
                        "requirement is unknown, not 'no'.")
    pfic = sorted({book.symbol(i) for (a, i) in _held_pairs(book) if book.country(a)[0] not in (None, "US")
                   and book.asset_class(i) == "fund"
                   and str((book.instruments.get(i) or {}).get("issuer_domicile") or "") not in {"US", ""}})
    if pfic:
        warnings.append("Foreign funds held (" + ", ".join(pfic) + ") are likely PFICs: Form 8621 may be required; "
                        "confirm with your CPA.")
    return _section(sid, "FBAR y Formulario 8938 (cuentas extranjeras)", "FBAR and Form 8938 (foreign accounts)", "US",
                    currency="USD", summary=summary,
                    columns=_cols(("institution", "Institución", "Institution"), ("country", "País", "Country"),
                                  ("type", "Tipo", "Type"), ("currency", "Moneda", "Currency"),
                                  ("max_value_usd_at_least", "Saldo máximo (al menos, USD)", "Maximum value (at least, USD)"),
                                  ("year_end_usd_at_least", "Al 31 dic (al menos, USD)", "Year end (at least, USD)"),
                                  ("note", "Nota", "Note")),
                    rows=table, missing=missing, warnings=warnings,
                    sources=[SRC_FBAR, SRC_FBAR_DUE, SRC_8938, SRC_TREASURY_RATES],
                    assumptions=["Form 8938's abroad thresholds apply only with a tax home abroad and bona fide "
                                 "residence for the whole year or 330 full days abroad in 12 months (Treas. Reg. "
                                 "1.6038D-2(a)(4)); otherwise the US thresholds apply.",
                                 "Values are lower bounds: month-end ledger balances, statement balances and "
                                 "balances you stated during the year. A lower bound above a threshold is enough to "
                                 "require the form; below it, the answer stays unknown until every value is known.",
                                 "Accounts are foreign when the statement, the institution or (failing both) the "
                                 "currency places them outside the US."])


def _held_pairs(book: _Book) -> set[tuple[str, str]]:
    return {(e["account_id"], e["instrument_id"]) for e in book.entries
            if e.get("instrument_id") and e["kind"] in {"buy", "opening_balance", "transfer"}
            and e["date"] <= book.end}
