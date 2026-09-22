"""Mexico sections: average-cost replay, enajenacion, foreign income, interest, dividends, deductions."""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from .. import mexico
from ..ledger.derive import match_transfers, replay
from ..ledger.model import fold
from .sources import (
    ART140_CORPORATE_RATE, ART140_GROSS_UP, CENT, CFDI_CHECKLIST, SRC_ART129, SRC_ART133_136, SRC_ART140,
    SRC_ART142V, SRC_ART151, SRC_ART22_23, SRC_ART5, SRC_ART55, SRC_ART93_XIV, SRC_CFF17A, SRC_INPC,
    SRC_USO_CFDI, ZERO, _DEBT_CLASSES, _FIBRA_CLASSES,
)
from .book import (
    _Book, _cols, _combine, _d, _m, _month, _need, _normalized, _prior_month, _q, _recon, _same_institution,
    _section, _sum,
)


# ----------------------------------------------------------------- average-cost replay (Mexico)


class _Pool:
    """Per account and instrument: lots with quantity, nominal cost, currency and acquisition date."""

    def __init__(self):
        self.lots: dict[tuple[str, str], list[dict]] = {}

    def add(self, account: str, instrument: str, qty: Decimal, cost: Decimal | None, currency: str | None,
            acquired: str | None) -> None:
        self.lots.setdefault((account, instrument), []).append(
            {"qty": qty, "cost": cost, "currency": currency, "acquired": acquired})

    def relieve(self, account: str, instrument: str, qty: Decimal) -> list[dict]:
        """Pro-rata relief: every open lot gives the same share, so the average cost is unchanged."""
        lots = self.lots.get((account, instrument), [])
        held = sum((lot["qty"] for lot in lots), ZERO)
        taken = []
        if held > 0:
            share = min(qty, held) / held
            for lot in lots:
                portion = {"qty": lot["qty"] * share, "cost": None if lot["cost"] is None else lot["cost"] * share,
                           "currency": lot["currency"], "acquired": lot["acquired"]}
                taken.append(portion)
                lot["qty"] -= portion["qty"]
                if lot["cost"] is not None:
                    lot["cost"] -= portion["cost"]
            self.lots[(account, instrument)] = [lot for lot in lots if lot["qty"] > 0]
        if qty > held:
            taken.append({"qty": qty - max(held, ZERO), "cost": None, "currency": None, "acquired": None,
                          "oversold": True})
        return taken


def _average_cost_disposals(book: _Book) -> tuple[list[dict], list[str]]:
    """Every sale through the year's end with the lots it relieved at average cost."""
    pool = _Pool()
    notes: list[str] = []
    transfers = match_transfers(book.entries)
    pair_in = {pair["in"]: pair["out"] for pair in transfers["pairs"]}
    moved: dict[str, list[dict]] = {}
    disposals: list[dict] = []
    for entry in book.entries:
        if entry["date"] > book.end:
            break
        kind, account, instrument = entry["kind"], entry["account_id"], entry.get("instrument_id")
        qty, amount = _d(entry.get("quantity")), _d(entry.get("amount"))
        if not instrument:
            continue
        if kind == "buy" and qty is not None:
            pool.add(account, instrument, qty, None if amount is None else -amount, entry.get("currency"), entry["date"])
        elif kind == "opening_balance" and qty and qty > 0:
            basis = _d(entry.get("cost_basis"))
            pool.add(account, instrument, qty, basis, entry.get("currency") if basis is not None else None,
                     entry.get("acquired_on"))
        elif kind == "transfer" and qty is not None:
            if qty < 0:
                moved[entry["id"]] = pool.relieve(account, instrument, -qty)
            else:
                origin = moved.pop(pair_in.get(entry["id"], ""), None)
                if origin:
                    for lot in origin:
                        pool.add(account, instrument, lot["qty"], lot["cost"], lot["currency"], lot["acquired"])
                else:
                    basis = _d(entry.get("cost_basis"))
                    pool.add(account, instrument, qty, basis, entry.get("currency") if basis is not None else None,
                             entry.get("acquired_on"))
        elif kind == "sell" and qty is not None:
            disposals.append({"entry_id": entry["id"], "date": entry["date"], "account_id": account,
                              "instrument_id": instrument, "quantity": qty, "proceeds": amount,
                              "currency": entry.get("currency"), "portions": pool.relieve(account, instrument, qty)})
        elif kind == "split":
            ratio = _d(entry.get("ratio")) or Decimal(1)
            for lot in pool.lots.get((account, instrument), []):
                lot["qty"] *= ratio
        elif kind in {"merger", "spin_off"}:
            ratio = _d(entry.get("ratio")) or Decimal(1)
            new = entry.get("new_instrument_id")
            parents = pool.lots.get((account, instrument), [])
            allocation = _d(entry.get("basis_allocation"))
            if kind == "merger":
                pool.lots.pop((account, instrument), None)
                for lot in parents:
                    pool.add(account, new, lot["qty"] * ratio, lot["cost"], lot["currency"], lot["acquired"])
            else:
                for lot in parents:
                    child = None if lot["cost"] is None or allocation is None else lot["cost"] * allocation
                    pool.add(account, new, lot["qty"] * ratio, child, lot["currency"], lot["acquired"])
                    lot["cost"] = None if lot["cost"] is None or allocation is None else lot["cost"] - child
                if parents and allocation is None:
                    notes.append(f"Spin-off {entry['id']}: basis allocation unknown, so both costs are unknown.")
    return disposals, notes


# ----------------------------------------------------------------- Mexico sections


def _mx_sale_row(book: _Book, sale: dict, currency: str = "MXN") -> dict:
    """One sale at average cost: nominal and INPC-updated cost in MXN (None when unknown)."""
    proceeds = book.convert(sale["proceeds"], sale["currency"], currency, sale["date"])
    nominal = updated = ZERO
    unknown_cost = unknown_update = False
    for portion in sale["portions"]:
        cost = portion["cost"]
        if cost is None or portion.get("acquired") is None:
            unknown_cost = True
            continue
        cost_mxn = book.convert(cost, portion["currency"], currency, portion["acquired"])
        if cost_mxn is None:
            unknown_cost = True
            continue
        nominal += cost_mxn
        factor = book.factor(portion["acquired"], sale["date"])
        if factor is None:
            unknown_update = True
        else:
            updated += cost_mxn * factor
    nominal_cost = None if unknown_cost else nominal
    updated_cost = None if unknown_cost or unknown_update else updated
    return {
        "entry_id": sale["entry_id"], "date": sale["date"], "account_id": sale["account_id"],
        "instrument_id": sale["instrument_id"], "symbol": book.symbol(sale["instrument_id"]),
        "quantity": _q(sale["quantity"]), "proceeds_mxn": _m(proceeds),
        "cost_nominal_mxn": _m(nominal_cost), "cost_updated_mxn": _m(updated_cost),
        "gain_nominal_mxn": _m(None if proceeds is None or nominal_cost is None else proceeds - nominal_cost),
        "gain_mxn": _m(None if proceeds is None or updated_cost is None else proceeds - updated_cost),
        "_unknown_cost": unknown_cost, "_unknown_update": unknown_update and not unknown_cost,
        "_no_fx": proceeds is None,
    }


def _carryforwards(book: _Book, section: str) -> tuple[list[dict] | None, Decimal | None, list[dict]]:
    raw = book.mx.get("article_129_loss_carryforwards")
    if raw is None:
        return None, None, [_need(section, f"tax.{book.year}.mx.article_129_loss_carryforwards",
                                  "Pérdidas de años anteriores por enajenación de acciones (Art. 129) pendientes de "
                                  "amortizar, actualizadas; [] si no hay.",
                                  "Prior-year Art. 129 share-sale losses not yet used, updated; [] if none.")]
    if not isinstance(raw, list):
        raise ValueError(f"tax.{book.year}.mx.article_129_loss_carryforwards must be a list")
    rows, available = [], ZERO
    for index, item in enumerate(raw):
        origin, amount = (item or {}).get("origin_year"), _d((item or {}).get("available_updated_mxn"))
        if not isinstance(origin, int) or amount is None or amount < 0:
            raise ValueError(f"article_129_loss_carryforwards[{index}] needs origin_year and available_updated_mxn")
        eligible = 1 <= book.year - origin <= 10
        if eligible:
            available += amount
        rows.append({"origin_year": origin, "available_updated_mxn": _m(amount),
                     "updated_through": item.get("updated_through"), "eligible_this_year": eligible})
    return rows, available, []


def _mx_enajenacion(book: _Book, disposals: list[dict], foreign_129: Decimal | None | bool) -> tuple[dict, dict]:
    """Art. 129 per Mexican broker; returns the section and the per-broker declared results for netting."""
    sid = "mx_enajenacion"
    missing, warnings, assumptions = [], [], []
    rows, brokers = [], {}
    fibra_rows, debt_rows = [], []
    for sale in disposals:
        if not book.start <= sale["date"] <= book.end:
            continue
        country, _ = book.country(sale["account_id"])
        if country != "MX" or book.kind(sale["account_id"]) not in {"brokerage"}:
            continue
        klass = book.asset_class(sale["instrument_id"])
        row = _mx_sale_row(book, sale)
        row["broker"] = book.institution(sale["account_id"])
        if klass in _DEBT_CLASSES:
            debt_rows.append(row)  # a gain on a debt security is interest (Art. 133), shown under interest
            continue
        if klass in _FIBRA_CLASSES:
            fibra_rows.append(row)
            continue
        rows.append(row)
        brokers.setdefault(row["broker"], {"accounts": set(), "rows": []})
        brokers[row["broker"]]["accounts"].add(sale["account_id"])
        brokers[row["broker"]]["rows"].append(row)
    months: set[str] = set()
    for row in rows:
        if row["_unknown_cost"]:
            missing.append(_need(sid, f"lots.{row['account_id']}.{row['instrument_id']}.cost",
                                 f"Costo de adquisición de {row['symbol']} en {row['broker']} (falta en el ledger).",
                                 f"Acquisition cost of {row['symbol']} at {row['broker']} (missing from the ledger)."))
        if row["_no_fx"]:
            missing.append(_need(sid, f"fx.MXN@{row['date']}", f"Tipo de cambio a MXN del {row['date']}.",
                                 f"An MXN exchange rate for {row['date']}."))
        if row["_unknown_update"]:
            sale = next(s for s in disposals if s["entry_id"] == row["entry_id"])
            for portion in sale["portions"]:
                if portion.get("acquired"):
                    for month in (_month(portion["acquired"]), _prior_month(sale["date"])):
                        if month not in book.inpc:
                            months.add(month)
    if months:
        missing.append(_need(sid, "inpc", "INPC (INEGI) de los meses " + ", ".join(sorted(months))
                             + " para actualizar el costo.",
                             "INPC (INEGI) for " + ", ".join(sorted(months)) + " to update the cost."))
    summary_rows, recon, declared = [], [], {}
    used: set[str] = set()
    for broker, data in sorted(brokers.items()):
        ours = [_d(r["gain_mxn"]) for r in data["rows"]]
        nominal = [_d(r["gain_nominal_mxn"]) for r in data["rows"]]
        net = _sum(ours)
        gains = None if net is None else sum((g for g in ours if g > 0), ZERO)
        losses = None if net is None else -sum((g for g in ours if g < 0), ZERO)
        doc = book.constancia_for(data["accounts"], broker, "enajenacion")
        used.update(p["id"] for p in (doc or {}).get("_parts") or ([doc] if doc else []))
        block = _normalized("enajenacion", doc["enajenacion"]) if doc else None
        doc_net = _d(block.get("net")) if block else None
        use = doc_net if doc_net is not None else net
        declared[broker] = use
        summary_rows.append({"broker": broker, "sales": len(data["rows"]), "gains_mxn": _m(gains),
                             "losses_mxn": _m(losses), "net_mxn": _m(net), "net_nominal_mxn": _m(_sum(nominal)),
                             "constancia_net_mxn": _m(doc_net), "declared_net_mxn": _m(use),
                             "basis": "constancia" if doc_net is not None else ("computed" if net is not None
                                                                                else "unknown")})
        if block:
            recon.append(_recon(f"{broker}: resultado neto Art. 129 / net Art. 129 result", net, doc_net,
                                "constancia", doc, "enajenacion", "net"))
            for field in ("gain", "loss"):
                ours_value = gains if field == "gain" else losses
                if _d(block.get(field)) is not None:
                    recon.append(_recon(f"{broker}: {'ganancias / gains' if field == 'gain' else 'pérdidas / losses'}",
                                        ours_value, abs(_d(block[field])), "constancia", doc, "enajenacion", field))
        else:
            missing.append(_need(sid, f"constancia.{fold(broker).replace(' ', '_')}.{book.year}",
                                 f"Constancia anual de {broker} {book.year} (enajenación de acciones): súbela; es la "
                                 "fuente de verdad.",
                                 f"{broker}'s {book.year} annual constancia (share sales): upload it; it is the "
                                 "source of truth."))
    # Brokers with a constancia but no sales in the ledger still count (the ledger may be incomplete).
    unmatched: dict[str, list[dict]] = {}
    for doc in book.constancias:
        if isinstance(doc.get("enajenacion"), dict) and doc["id"] not in used:
            group = next((k for k in unmatched if _same_institution(k, doc.get("institution"))),
                         doc.get("institution") or doc["id"])
            unmatched.setdefault(group, []).append(doc)
    for name, docs in unmatched.items():
        doc = _combine(docs)
        doc_net = _d(_normalized("enajenacion", doc["enajenacion"]).get("net"))
        declared[name] = doc_net
        summary_rows.append({"broker": name, "sales": 0, "gains_mxn": None,
                             "losses_mxn": None, "net_mxn": None, "net_nominal_mxn": None,
                             "constancia_net_mxn": _m(doc_net), "declared_net_mxn": _m(doc_net),
                             "basis": "constancia"})
        warnings.append(f"{name}: the constancia reports Art. 129 sales the ledger does not "
                        "have; the constancia is used.")
    if foreign_129 is not False:
        summary_rows.append({"broker": "Intermediario extranjero (SIC) / foreign broker (SIC)", "sales": None,
                             "gains_mxn": None, "losses_mxn": None, "net_mxn": _m(foreign_129),
                             "net_nominal_mxn": None, "constancia_net_mxn": None,
                             "declared_net_mxn": _m(foreign_129), "basis": "computed (no constancia is issued)"})
        declared["_foreign_sic"] = foreign_129
    carries, available, carry_missing = _carryforwards(book, sid)
    missing += carry_missing
    total = _sum(declared.values()) if declared else ZERO
    netting: dict[str, Any] = {"net_result_mxn": _m(total), "loss_carryforwards": carries}
    if total is not None and available is not None:
        gain = max(total, ZERO)
        used = min(gain, available)
        taxable = gain - used
        netting.update(carry_used_mxn=_m(used), taxable_gain_mxn=_m(taxable), tax_10pct_mxn=_m(taxable * Decimal("0.10")),
                       new_loss_carryforward_mxn=_m(max(-total, ZERO)))
        if total < 0:
            netting["new_loss_expires"] = f"usable through {book.year + 10}"
    else:
        netting.update(carry_used_mxn=None, taxable_gain_mxn=None, tax_10pct_mxn=None,
                       new_loss_carryforward_mxn=_m(max(-total, ZERO)) if total is not None else None)
    if not rows and not declared:
        status = "not_applicable"
        if not book.covers_year():
            status = "partial"
            missing.append(_need(sid, f"ledger.{book.year}", f"Estados de cuenta de casas de bolsa de todo {book.year} "
                                 "(sin ellos no sabemos si hubo ventas).",
                                 f"Broker statements covering all of {book.year} (without them sales are unknown)."))
    else:
        status = None
    for row in debt_rows:
        warnings.append(f"{row['symbol']} sold on {row['date']}: a debt security; its gain is interest (Art. 133), "
                        "shown under interest.")
    for row in fibra_rows:
        warnings.append(f"{row['symbol']} sold on {row['date']}: FIBRA certificates sold on the exchange are exempt "
                        "for resident individuals (Art. 188 fr. X); not included in the Art. 129 result.")
    assumptions += ["Cost is the average cost per share of each account (Arts. 22-23), each purchase updated with the "
                    "INPC from its month to the month before the sale (CFF Art. 17-A, factor floored at 1).",
                    "Proceeds are the cash the sale posted; commissions posted separately are not deducted.",
                    "Where a constancia exists its figure is declared; ours is shown for review."]
    public = [{k: v for k, v in r.items() if not k.startswith("_")} for r in rows]
    section = _section(
        sid, "Enajenación de acciones (Art. 129)", "Sale of listed shares (Art. 129)", "MX", currency="MXN",
        summary={"brokers": summary_rows, **netting},
        columns=_cols(("broker", "Casa de bolsa", "Broker"), ("symbol", "Emisora", "Security"),
                      ("date", "Fecha de venta", "Sale date"), ("quantity", "Títulos", "Shares"),
                      ("proceeds_mxn", "Ingreso", "Proceeds"), ("cost_nominal_mxn", "Costo nominal", "Nominal cost"),
                      ("cost_updated_mxn", "Costo actualizado", "Updated cost"),
                      ("gain_mxn", "Ganancia/pérdida", "Gain/loss")),
        rows=public, reconciliation=recon, missing=missing, warnings=warnings,
        sources=[SRC_ART129, SRC_ART22_23, SRC_CFF17A, SRC_INPC], assumptions=assumptions, status=status,
        extra={"debt_sales": [{k: v for k, v in r.items() if not k.startswith("_")} for r in debt_rows]})
    return section, declared


def _dividend_rows(book: _Book, account_filter) -> list[dict]:
    """Dividends in the year with the tax withheld on the same account and security within five days."""
    withheld = [e for e in book.entries if e["kind"] == "tax_withheld" and book.start <= e["date"] <= book.end]
    used: set[str] = set()
    rows = []
    for entry in book.entries:
        if entry["kind"] != "dividend" or not book.start <= entry["date"] <= book.end or not account_filter(entry):
            continue
        match = ZERO
        for tax in withheld:
            if tax["id"] in used or tax["account_id"] != entry["account_id"]:
                continue
            if tax.get("instrument_id") not in (None, entry.get("instrument_id")):
                continue
            if abs((date.fromisoformat(tax["date"]) - date.fromisoformat(entry["date"])).days) <= 5:
                match += abs(_d(tax.get("amount")) or ZERO)
                used.add(tax["id"])
        rows.append({"entry": entry, "gross": _d(entry.get("amount")), "withheld": match,
                     "currency": entry.get("currency")})
    return rows


def _mx_foreign(book: _Book, disposals: list[dict], mx_129_net: Decimal | None,
                carries: list | None) -> tuple[dict | None, Decimal | None | bool]:
    """Foreign broker: SIC-listed sales at 10% (criterio 37/ISR/N), others progressive; foreign dividends."""
    sid = "mx_extranjero"
    foreign = {a for a in book.accounts if book.country(a)[0] not in (None, "MX") and book.kind(a) == "brokerage"}
    if not foreign:
        return None, False
    missing, warnings = [], []
    sales, unclassified = [], set()
    for sale in disposals:
        if sale["account_id"] not in foreign or not book.start <= sale["date"] <= book.end:
            continue
        listed = book.sic_listed(sale["instrument_id"])
        if listed is None:
            unclassified.add(sale["instrument_id"])
            continue
        proceeds, qty = sale["proceeds"], sale["quantity"]
        fx_sale = book.fx.rate(sale["currency"], "MXN", sale["date"]) if sale["currency"] else None
        if fx_sale is None:
            missing.append(_need(sid, f"fx.{sale['currency']}/MXN@{sale['date']}",
                                 f"Tipo de cambio {sale['currency']}/MXN del {sale['date']} (Banxico FIX).",
                                 f"A {sale['currency']}/MXN rate for {sale['date']} (Banxico FIX)."))
            continue
        for index, portion in enumerate(sale["portions"]):
            if portion["cost"] is None or portion["acquired"] is None:
                missing.append(_need(sid, f"lots.{sale['account_id']}.{sale['instrument_id']}.cost",
                                     f"Costo y fecha de compra de {book.symbol(sale['instrument_id'])}.",
                                     f"Cost and purchase date of {book.symbol(sale['instrument_id'])}."))
                continue
            fx_acq = book.fx.rate(portion["currency"], "MXN", portion["acquired"])
            if fx_acq is None:
                missing.append(_need(sid, f"fx.{portion['currency']}/MXN@{portion['acquired']}",
                                     f"Tipo de cambio {portion['currency']}/MXN del {portion['acquired']}.",
                                     f"A {portion['currency']}/MXN rate for {portion['acquired']}."))
                continue
            row = {"id": f"{sale['entry_id']}#{index + 1}", "currency": sale["currency"],
                   "proceeds": str(proceeds * portion["qty"] / qty), "fx_sale": str(fx_sale),
                   "cost": str(portion["cost"]), "fx_acquisition": str(fx_acq), "acquired_on": portion["acquired"],
                   "sold_on": sale["date"], "sic_listed": listed,
                   "security_type": "equity_etf" if book.asset_class(sale["instrument_id"]) == "fund" else "share"}
            factor = book.factor(portion["acquired"], sale["date"])
            if factor is not None:
                row["cost_update_factor"] = str(factor)
            sales.append(row)
    for instrument in sorted(unclassified):
        missing.append(_need(sid, f"tax.{book.year}.mx.sic_listed.{instrument}",
                             f"¿{book.symbol(instrument)} cotiza en el SIC? (Sí: 10% Art. 129; No: tarifa progresiva).",
                             f"Is {book.symbol(instrument)} listed in the SIC? (Yes: 10% Art. 129; no: progressive)."))
    dividends = []
    w8 = book.mx.get("w8ben_on_file")
    for item in _dividend_rows(book, lambda e: e["account_id"] in foreign):
        entry = item["entry"]
        rate = book.fx.rate(item["currency"], "MXN", entry["date"]) if item["currency"] else None
        if rate is None:
            missing.append(_need(sid, f"fx.{item['currency']}/MXN@{entry['date']}",
                                 f"Tipo de cambio {item['currency']}/MXN del {entry['date']}.",
                                 f"A {item['currency']}/MXN rate for {entry['date']}."))
            continue
        meta = book.instruments.get(entry.get("instrument_id") or "") or {}
        row = {"id": entry["id"], "currency": item["currency"], "gross": str(item["gross"]),
               "withheld": str(item["withheld"]), "fx": str(rate), "paid_on": entry["date"],
               "source_country": str(meta.get("issuer_domicile") or meta.get("country")
                                     or book.country(entry["account_id"])[0] or "US")}
        if isinstance(w8, bool):
            row["w8ben_on_file"] = w8
        dividends.append(row)
    if not sales and not dividends and not missing:
        return None, False
    payload: dict[str, Any] = {"tax_year": book.year, "sales": sales, "dividends": dividends,
                               "parameters": book.parameters}
    if mx_129_net is not None and carries is not None:
        payload["article_129_realized_gain_or_loss_mxn"] = str(mx_129_net)
        payload["article_129_loss_carryforwards"] = [
            {"origin_year": c["origin_year"], "available_updated_mxn": c["available_updated_mxn"],
             "updated_through": c.get("updated_through") or "stated"} for c in carries]
    for key in ("taxable_income_before_mxn", "marginal_rate"):
        if book.mx.get(key) is not None:
            payload[key] = book.mx[key]
    report = mexico.foreign_securities(payload) if (sales or dividends) else {
        "status": "needs_input", "result": {}, "missing": [], "warnings": [], "sources": [], "assumptions": []}
    result = report.get("result") or {}
    for item in report.get("missing") or []:
        if item in {"sales or dividends"}:
            continue
        missing.append(_need(sid, f"mx_foreign.{item}", f"Dato para el cálculo de valores extranjeros: {item}.",
                             f"Input for the foreign-securities calculation: {item}."))
    sic_net = _d((result.get("totals") or {}).get("article_129_net_gain_or_loss_mxn"))
    has_sic = any(s["sic_listed"] for s in sales)
    if has_sic and any("cost_update_factor" not in s for s in sales if s["sic_listed"]):
        missing.append(_need(sid, "inpc", "INPC de los meses de compra y del mes anterior a cada venta (costo "
                                           "actualizado).",
                             "INPC for each purchase month and the month before each sale (updated cost)."))
    rows = [{"id": r["id"], "regime": r["regime"], "proceeds_mxn": r["proceeds_mxn"],
             "cost_mxn": r["cost_mxn_at_acquisition_fx"], "update_factor": r["cost_update_factor"],
             "updated_cost_mxn": r["updated_cost_mxn"], "gain_mxn": r["gain_or_loss_mxn"],
             "fx_effect_mxn": r["components"]["fx_effect_on_cost_mxn"]} for r in result.get("sales") or []]
    div_rows = result.get("dividends") or []
    section = _section(
        sid, "Valores extranjeros vía intermediario extranjero", "Foreign securities at a foreign broker", "MX",
        currency="MXN",
        summary={"totals": result.get("totals"), "foreign_tax_credit": result.get("foreign_tax_credit"),
                 "known_tax_mxn": result.get("known_tax_mxn"),
                 "net_estimated_mexican_tax_mxn": result.get("net_estimated_mexican_tax_mxn"),
                 "unknown_tax_components": result.get("unknown_tax_components"), "dividends": div_rows,
                 "accounts": sorted(book.institution(a) for a in foreign)},
        columns=_cols(("id", "Venta", "Sale"), ("regime", "Régimen", "Regime"),
                      ("proceeds_mxn", "Ingreso MXN", "Proceeds MXN"), ("cost_mxn", "Costo MXN", "Cost MXN"),
                      ("update_factor", "Factor INPC", "INPC factor"),
                      ("updated_cost_mxn", "Costo actualizado", "Updated cost"),
                      ("gain_mxn", "Ganancia/pérdida", "Gain/loss"), ("fx_effect_mxn", "Efecto cambiario", "FX effect")),
        rows=rows, missing=missing, warnings=list(report.get("warnings") or []) + warnings,
        sources=list(report.get("sources") or []), assumptions=list(report.get("assumptions") or []) + [
            "Each sale is split into the lots it relieved at average cost; each lot's MXN cost uses the rate of its "
            "purchase date. No constancia exists for a foreign broker: this computation is what you declare."])
    if has_sic:
        known = not unclassified and not any(m["key"].startswith(("fx.", "lots.")) for m in missing)
        return section, sic_net if known else None
    if unclassified:
        return section, None
    return section, False


def _interest_retention(book: _Book, account: str) -> Decimal:
    total = ZERO
    for e in book.entries:
        if e["kind"] != "tax_withheld" or e["account_id"] != account or not book.start <= e["date"] <= book.end:
            continue
        klass = book.asset_class(e.get("instrument_id"))
        if e.get("instrument_id") and klass not in _DEBT_CLASSES:
            continue  # withholding on a share or fund is a dividend withholding
        total += abs(_d(e.get("amount")) or ZERO)
    return total


def _average_daily_cash(book: _Book, account: str, currency: str) -> Decimal | None:
    """Average daily cash balance over the year from the ledger (only when the ledger covers the whole year)."""
    if not book.covers_year():
        return None
    days = [(date(book.year, 1, 1) + timedelta(days=i)).isoformat()
            for i in range((date(book.year, 12, 31) - date(book.year, 1, 1)).days + 1)]
    if book.daily is None:
        book.daily, _ = replay(book.ledger, book.end, checkpoints=days)
    state = book.daily
    values = [state.snapshots.get(d, {}).get("cash", {}).get((account, currency), ZERO) for d in days]
    return sum(values, ZERO) / len(values)


def _mx_intereses(book: _Book, debt_sales: list[dict]) -> dict:
    sid = "mx_intereses"
    missing, warnings, rows, recon = [], [], [], []
    by_institution: dict[str, dict] = {}
    for account_id in sorted(book.accounts):
        if book.country(account_id)[0] != "MX" or book.kind(account_id) not in {"bank", "brokerage"}:
            continue
        nominal = ZERO
        found = False
        for e in book.entries:
            if e["kind"] == "interest" and e["account_id"] == account_id and book.start <= e["date"] <= book.end:
                value = book.convert(_d(e.get("amount")), e.get("currency"), "MXN", e["date"])
                if value is None:
                    missing.append(_need(sid, f"fx.MXN@{e['date']}", f"Tipo de cambio a MXN del {e['date']}.",
                                         f"An MXN rate for {e['date']}."))
                    continue
                nominal += value
                found = True
        debt_gain = ZERO
        for row in debt_sales:
            if row["account_id"] == account_id:
                gain = _d(row["gain_nominal_mxn"])
                if gain is None:
                    missing.append(_need(sid, f"lots.{account_id}.{row['instrument_id']}.cost",
                                         f"Costo de {row['symbol']} vendido el {row['date']}.",
                                         f"Cost of {row['symbol']} sold on {row['date']}."))
                else:
                    debt_gain += gain
                found = True
        retention = _interest_retention(book, account_id)
        doc = book.constancia_for([account_id], book.institution(account_id), "intereses")
        block = doc["intereses"] if doc else None
        if not found and not block and not retention:
            continue
        name = book.institution(account_id)
        agg = by_institution.setdefault(name, {"accounts": [], "nominal": ZERO, "debt_gain": ZERO, "retention": ZERO,
                                               "docs": {}, "avg": ZERO, "avg_known": True, "bank": True})
        agg["accounts"].append(account_id)
        agg["nominal"] += nominal
        agg["debt_gain"] += debt_gain
        agg["retention"] += retention
        for part in (doc or {}).get("_parts") or ([doc] if doc else []):
            agg["docs"].setdefault(part["id"], part)  # each account's document once, summed per institution
        if book.kind(account_id) == "bank":
            avg = _average_daily_cash(book, account_id, (book.accounts[account_id].get("currency") or "MXN"))
            if avg is None:
                agg["avg_known"] = False
            else:
                agg["avg"] += avg
        else:
            agg["bank"] = False
    first, last = f"{book.year}-01", f"{book.year}-12"
    inflation = None
    if first in book.inpc and last in book.inpc:
        inflation = book.inpc[last] / book.inpc[first] - 1
    for name, agg in sorted(by_institution.items()):
        nominal_total = agg["nominal"] + agg["debt_gain"]
        real = None
        basis = "constancia"
        if agg["bank"] and agg["avg_known"] and inflation is not None:
            real = nominal_total - agg["avg"] * inflation
            basis = "computed"
        agg["doc"] = _combine(list(agg["docs"].values()))
        block = (agg["doc"] or {}).get("intereses") or {}
        doc_nominal, doc_real = _d(block.get("nominal")), _d(block.get("real"))
        doc_loss, doc_ret = _d(block.get("real_loss")), _d(block.get("isr_withheld"))
        rows.append({"institution": name, "nominal_mxn": _m(nominal_total),
                     "of_which_debt_sale_gains_mxn": _m(agg["debt_gain"]) if agg["debt_gain"] else None,
                     "average_daily_balance_mxn": _m(agg["avg"]) if agg["bank"] and agg["avg_known"] else None,
                     "real_mxn": _m(None if real is None else max(real, ZERO)),
                     "real_loss_mxn": _m(None if real is None else max(-real, ZERO)),
                     "retention_mxn": _m(agg["retention"]),
                     "constancia_nominal_mxn": _m(doc_nominal), "constancia_real_mxn": _m(doc_real),
                     "constancia_real_loss_mxn": _m(doc_loss), "constancia_retention_mxn": _m(doc_ret),
                     "declared_real_mxn": _m(doc_real if doc_real is not None
                                             else None if real is None else max(real, ZERO)),
                     "declared_real_loss_mxn": _m(doc_loss if doc_real is not None
                                                  else None if real is None else max(-real, ZERO)),
                     "declared_retention_mxn": _m(doc_ret if doc_ret is not None else agg["retention"]),
                     "basis": "constancia" if agg["doc"] else basis if real is not None else "unknown"})
        if agg["doc"]:
            for label, ours, theirs, field in (
                    ("interés nominal / nominal interest", nominal_total, doc_nominal, "nominal"),
                    ("interés real / real interest", real, doc_real, "real"),
                    ("ISR retenido / ISR withheld", agg["retention"], doc_ret, "isr_withheld")):
                if theirs is not None:
                    recon.append(_recon(f"{name}: {label}", ours, theirs, "constancia", agg["doc"], "intereses",
                                        field))
        else:
            missing.append(_need(sid, f"constancia.{fold(name).replace(' ', '_')}.{book.year}",
                                 f"Constancia de intereses {book.year} de {name} (nominal, real y retención; se "
                                 "entrega a más tardar el 15 de febrero).",
                                 f"{name}'s {book.year} interest constancia (nominal, real, withholding; due by "
                                 "February 15)."))
            if real is None:
                missing.append(_need(sid, f"real_interest.{fold(name).replace(' ', '_')}",
                                     f"Interés real de {name}: sin constancia se necesita el INPC de enero y "
                                     f"diciembre de {book.year} y estados de cuenta de todo el año.",
                                     f"{name}'s real interest: without the constancia it needs the INPC for January "
                                     f"and December {book.year} and statements for the whole year."))
    total_real = _sum(_d(r["declared_real_mxn"]) for r in rows) if rows else ZERO
    total_ret = _sum(_d(r["declared_retention_mxn"]) for r in rows) if rows else ZERO
    status = "not_applicable" if not rows else None
    return _section(
        sid, "Intereses", "Interest", "MX", currency="MXN",
        summary={"real_interest_mxn": _m(total_real), "retention_mxn": _m(total_ret),
                 "real_interest_loss_mxn": _m(_sum(_d(r["declared_real_loss_mxn"]) for r in rows) if rows else ZERO),
                 # What is declared: the constancia's nominal where there is one (the ledger rarely has a year).
                 "nominal_interest_mxn": _m(_sum(_d(r["constancia_nominal_mxn"]) if r["basis"] == "constancia"
                                                 and r.get("constancia_nominal_mxn") is not None
                                                 else _d(r["nominal_mxn"]) for r in rows) if rows else ZERO),
                 "inflation_factor": None if inflation is None else format(inflation.quantize(Decimal("0.000001")), "f")},
        columns=_cols(("institution", "Institución", "Institution"), ("nominal_mxn", "Interés nominal", "Nominal"),
                      ("real_mxn", "Interés real (cálculo)", "Real (ours)"),
                      ("real_loss_mxn", "Pérdida real (cálculo)", "Real loss (ours)"),
                      ("constancia_real_mxn", "Interés real (constancia)", "Real (constancia)"),
                      ("retention_mxn", "Retención (ledger)", "Withheld (ledger)"),
                      ("constancia_retention_mxn", "Retención (constancia)", "Withheld (constancia)"),
                      ("declared_real_mxn", "Real a declarar", "Real to declare")),
        rows=rows, reconciliation=recon, missing=missing, warnings=warnings,
        sources=[SRC_ART133_136, SRC_ART55, SRC_INPC], status=status,
        assumptions=["Real interest = nominal interest less the average daily balance times the INPC change from "
                     "January to December (Art. 134); computed only for bank accounts with a whole-year ledger. For "
                     "brokerage (CETES, bonds) the constancia's real interest is used.",
                     "Gains on sales or redemptions of debt securities are interest (Art. 133).",
                     "The constancia is what the institution reported to SAT; it is what you declare."])


def _mx_dividendos(book: _Book) -> dict:
    sid = "mx_dividendos"
    missing, rows, recon = [], [], []
    mx_accounts = {a for a in book.accounts if book.country(a)[0] == "MX"}
    totals: dict[str, dict] = {}
    for item in _dividend_rows(book, lambda e: e["account_id"] in mx_accounts):
        entry = item["entry"]
        meta = book.instruments.get(entry.get("instrument_id") or "") or {}
        domicile = str(meta.get("issuer_domicile") or meta.get("country") or "")
        venue = meta.get("venue")
        if domicile == "MX" or (not domicile and venue in {"bmv", "biva"}):
            origin = "domestic"
        elif domicile:
            origin = "foreign"
        else:
            origin = "unknown"
            missing.append(_need(sid, f"instruments.{entry.get('instrument_id')}.issuer_domicile",
                                 f"¿{book.symbol(entry.get('instrument_id'))} es emisora mexicana o extranjera?",
                                 f"Is {book.symbol(entry.get('instrument_id'))} a Mexican or a foreign issuer?"))
        gross = book.convert(item["gross"], item["currency"], "MXN", entry["date"])
        withheld = book.convert(item["withheld"], item["currency"], "MXN", entry["date"])
        if gross is None:
            missing.append(_need(sid, f"fx.MXN@{entry['date']}", f"Tipo de cambio a MXN del {entry['date']}.",
                                 f"An MXN rate for {entry['date']}."))
        broker = book.institution(entry["account_id"])
        rows.append({"broker": broker, "symbol": book.symbol(entry.get("instrument_id")), "date": entry["date"],
                     "origin": origin, "gross_mxn": _m(gross), "withheld_mxn": _m(withheld),
                     "additional_10pct_mxn": _m(None if gross is None else gross * Decimal("0.10"))})
        bucket = totals.setdefault(broker, {"accounts": set(), "domestic": ZERO, "foreign": ZERO, "withheld": ZERO,
                                            "withheld_domestic": ZERO, "withheld_foreign": ZERO, "known": True})
        bucket["accounts"].add(entry["account_id"])
        if gross is None:
            bucket["known"] = False
        elif origin in {"domestic", "foreign"}:
            bucket[origin] += gross
        bucket["withheld"] += withheld or ZERO
        # Tax withheld abroad on a foreign (SIC) dividend is not on the Mexican constancia's ISR line.
        bucket["withheld_foreign" if origin == "foreign" else "withheld_domestic"] += withheld or ZERO
    # A constancia's dividends count even when the ledger holds none of that year's payments (a statement
    # uploaded for another year, or none at all): the constancia is what the broker reported to SAT.
    for doc in book.constancias:
        name = doc.get("institution")
        if isinstance(doc.get("dividendos"), dict) and name and \
                not any(_same_institution(name, broker) for broker in totals):
            totals[name] = {"accounts": book.accounts_for(doc), "domestic": ZERO, "foreign": ZERO, "withheld": ZERO,
                            "withheld_domestic": ZERO, "withheld_foreign": ZERO, "known": True, "ledger": False}
    summary = []
    for broker, bucket in sorted(totals.items()):
        doc = book.constancia_for(bucket["accounts"], broker, "dividendos")
        block = doc["dividendos"] if doc else None
        if block and bucket.get("ledger") is False:
            # Only the constancia: its figures are the year's, not zeros from a ledger that has no payments.
            bucket.update(domestic=_d(block.get("domestic_gross")) or ZERO,
                          foreign=_d(block.get("foreign_gross")) or ZERO,
                          # The total is both kinds, as the ledger path accumulates them.
                          withheld=(_d(block.get("isr_withheld")) or ZERO) + (_d(block.get("foreign_tax_withheld")) or ZERO),
                          withheld_domestic=_d(block.get("isr_withheld")) or ZERO,
                          withheld_foreign=_d(block.get("foreign_tax_withheld")) or ZERO)
        domestic = bucket["domestic"] if bucket["known"] else None
        # LISR Art. 140: the corporate ISR is (dividend x 1.4286) x 30%; the person accumulates the dividend plus
        # that ISR (piramidación) and credits it, when the dividend comes from CUFIN (the constancia says).
        documented_domestic = _d((block or {}).get("domestic_gross"))
        base = documented_domestic if documented_domestic is not None else domestic
        credit = None if base is None else (base * ART140_GROSS_UP * ART140_CORPORATE_RATE).quantize(CENT)
        stated_credit = _d((block or {}).get("isr_creditable"))
        summary.append({"broker": broker, "domestic_gross_mxn": _m(domestic),
                        "foreign_gross_mxn": _m(bucket["foreign"] if bucket["known"] else None),
                        "withheld_mxn": _m(bucket["withheld"]),
                        "withheld_domestic_mxn": _m(bucket["withheld_domestic"]),
                        "withheld_abroad_mxn": _m(bucket["withheld_foreign"]),
                        "art140": None if base is None or not base else {
                            "gross_up_factor": str(ART140_GROSS_UP),
                            "corporate_isr_credit_mxn": _m(stated_credit if stated_credit is not None else credit),
                            "computed_credit_mxn": _m(credit),
                            "accumulable_mxn": _m(base + (stated_credit if stated_credit is not None else credit)),
                            "basis": "constancia" if stated_credit is not None else "computed (assumes CUFIN)"},
                        "constancia": None if not block else {k: _m(_d(v)) for k, v in block.items()},
                        "basis": "constancia" if bucket.get("ledger") is False else "ledger"})
        if block and bucket.get("ledger") is False:
            pass  # only the constancia: its figures are the year's, nothing to compare them with or to ask for
        elif block:
            for label, ours, key in (("dividendos nacionales / domestic dividends", bucket["domestic"], "domestic_gross"),
                                     ("dividendos extranjeros / foreign dividends", bucket["foreign"], "foreign_gross"),
                                     ("ISR retenido (sin retenciones del extranjero) / Mexican ISR withheld",
                                      bucket["withheld_domestic"], "isr_withheld")):
                theirs = _d(block.get(key))
                if theirs is not None:
                    recon.append(_recon(f"{broker}: {label}", ours if bucket["known"] else None, theirs,
                                        "constancia", doc, "dividendos", key))
        else:
            missing.append(_need(sid, f"constancia.{fold(broker).replace(' ', '_')}.{book.year}.dividendos",
                                 f"Constancia de dividendos {book.year} de {broker} (ISR acreditable por CUFIN y el 10% "
                                 "adicional retenido).",
                                 f"{broker}'s {book.year} dividend constancia (creditable corporate ISR and the "
                                 "additional 10% withheld)."))
    return _section(
        sid, "Dividendos", "Dividends", "MX", currency="MXN", summary={"brokers": summary},
        columns=_cols(("broker", "Casa de bolsa", "Broker"), ("symbol", "Emisora", "Security"),
                      ("date", "Fecha", "Date"), ("origin", "Origen", "Origin"),
                      ("gross_mxn", "Dividendo bruto", "Gross"), ("withheld_mxn", "Retenido", "Withheld"),
                      ("additional_10pct_mxn", "10% adicional esperado", "Expected additional 10%")),
        rows=rows, reconciliation=recon, missing=missing,
        status="not_applicable" if not rows and not missing and not summary else None,
        sources=[SRC_ART140, SRC_ART142V, SRC_ART5],
        assumptions=["Domestic dividends accumulate with a credit for the corporate ISR shown on the constancia "
                     "(Art. 140): the dividend times 1.4286 times 30%, added to income (piramidación) and credited; "
                     "the additional 10% withheld is definitive.",
                     "The constancia's ISR withheld is compared with Mexican withholding only; tax withheld abroad "
                     "on SIC dividends (for example the US 10% treaty rate) is a foreign tax credit (Art. 5).",
                     "Foreign dividends (SIC via a Mexican broker) accumulate; the additional 10% applies (Art. 142 "
                     "fr. V) and foreign tax withheld is a credit up to the Mexican ISR on that income (Art. 5).",
                     "Dividends at a foreign broker are in the foreign-securities section."])


def _mx_deducciones(book: _Book) -> dict:
    sid = "mx_deducciones"
    stated = book.mx.get("deductions") if isinstance(book.mx.get("deductions"), dict) else {}
    missing, warnings = [], []
    ppr_from_ledger = ZERO
    ppr_accounts = [a for a in book.accounts if (book.accounts[a].get("type") or "") == "ppr"]
    for e in book.entries:
        if e["account_id"] in ppr_accounts and e["kind"] in {"deposit", "transfer"} and not e.get("instrument_id") \
                and book.start <= e["date"] <= book.end and (_d(e.get("amount")) or ZERO) > 0:
            ppr_from_ledger += _d(e["amount"])
    checklist = []
    for key, code, es, en, need_es, need_en in CFDI_CHECKLIST:
        amount = stated.get(key)
        if key == "ppr_mxn" and amount is None and ppr_accounts:
            amount = str(ppr_from_ledger)
        checklist.append({"category": key, "uso_cfdi": code, "es": es, "en": en,
                          "amount_mxn": _m(_d(amount)) if not isinstance(amount, dict) else "stated",
                          "document_es": need_es, "document_en": need_en,
                          "status": "stated" if amount is not None else "not_stated"})
    general_keys = ("medical_mxn", "disability_medical_mxn", "funeral_mxn", "insurance_premiums_mxn",
                    "school_transport_mxn", "local_payroll_tax_mxn")
    outside_keys = ("donations_mxn", "tuition_mxn")
    payload: dict[str, Any] = {"tax_year": book.year, "parameters": book.parameters}
    for key in ("total_income_mxn", "accumulable_income_mxn", "taxable_income_before_mxn", "marginal_rate"):
        if book.mx.get(key) is not None:
            payload[key] = book.mx[key]
    if not stated and not ppr_accounts:
        return _section(sid, "Deducciones personales (Art. 151) y Art. 185", "Personal deductions (Art. 151) and "
                        "Art. 185", "MX", currency="MXN", summary={"cfdi_checklist": checklist},
                        columns=_cols(("uso_cfdi", "Uso CFDI", "CFDI use"), ("es", "Concepto", "Concept (es)"),
                                      ("en", "Concepto (en)", "Concept"), ("amount_mxn", "Monto", "Amount"),
                                      ("document_es", "Comprobante", "Receipt (es)")),
                        rows=checklist, status="needs_input", sources=[SRC_ART151, SRC_USO_CFDI],
                        missing=[_need(sid, f"tax.{book.year}.mx.deductions",
                                       "Tus deducciones personales del año (médicos, seguros, hipoteca, PPR, "
                                       "colegiaturas…) o 'ninguna'.",
                                       "Your personal deductions for the year (medical, insurance, mortgage, PPR, "
                                       "tuition…) or 'none'.")])
    general = ZERO
    for key in general_keys:
        value = _d(stated.get(key))
        if value is not None:
            general += value
    outside = sum((_d(stated.get(k)) or ZERO for k in outside_keys), ZERO)
    if outside:
        warnings.append("Donations (7% of the prior year's accumulable income) and tuition (per-level decree caps) are "
                        "outside the global cap; their own limits are not checked here.")
    ppr = _d(stated.get("ppr_mxn")) if stated.get("ppr_mxn") is not None else (ppr_from_ledger if ppr_accounts else None)
    art185 = _d(stated.get("art185_mxn"))
    if ppr is None:
        missing.append(_need(sid, f"tax.{book.year}.mx.deductions.ppr_mxn", "Aportaciones a PPR / AFORE voluntarias "
                             "deducibles del año (0 si ninguna).", "Deductible PPR / voluntary AFORE contributions "
                             "for the year (0 if none)."))
    if art185 is None:
        missing.append(_need(sid, f"tax.{book.year}.mx.deductions.art185_mxn", "Depósitos Art. 185 del año (0 si "
                             "ninguno).", "Art. 185 deposits for the year (0 if none)."))
    payload["deductions"] = {"general_mxn": str(general), "retirement_151v_mxn": str(ppr or ZERO),
                             "art185_mxn": str(art185 or ZERO), "outside_global_cap_mxn": str(outside)}
    if isinstance(stated.get("mortgage"), dict):
        payload["mortgage"] = stated["mortgage"]
    for key in ("total_income_mxn", "accumulable_income_mxn"):
        if key not in payload:
            missing.append(_need(sid, f"tax.{book.year}.mx.{key}",
                                 "Ingreso total y acumulable del año (constancia de sueldos / CFDI de nómina) para los "
                                 "topes.", "Total and accumulable income for the year (payroll constancia / CFDI) "
                                           "for the caps."))
    report = mexico.personal_deductions(payload) if "total_income_mxn" in payload and "accumulable_income_mxn" in payload \
        else None
    result = (report or {}).get("result") or {}
    for item in (report or {}).get("missing") or []:
        if str(item).startswith("parameters."):
            missing.append(_need(sid, item, f"Parámetro por verificar: {item} (con su fuente oficial).",
                                 f"Parameter to verify: {item} (with its official source)."))
        elif not str(item).startswith("taxable_income_before_mxn"):
            missing.append(_need(sid, f"mx_deductions.{item}", f"Dato de deducciones: {item}.",
                                 f"Deduction input: {item}."))
    if ppr is None or art185 is None:
        result = {**result, "allowed": None} if result else result
    return _section(
        sid, "Deducciones personales (Art. 151) y Art. 185", "Personal deductions (Art. 151) and Art. 185", "MX",
        currency="MXN",
        summary={"caps": result.get("caps"), "allowed": result.get("allowed"),
                 "remaining_room": result.get("remaining_room"), "mortgage_interest": result.get("mortgage_interest"),
                 "ppr_from_ledger_mxn": _m(ppr_from_ledger) if ppr_accounts else None, "cfdi_checklist": checklist},
        columns=_cols(("uso_cfdi", "Uso CFDI", "CFDI use"), ("es", "Concepto", "Concept (es)"),
                      ("en", "Concepto (en)", "Concept"), ("amount_mxn", "Monto", "Amount"),
                      ("document_es", "Comprobante", "Receipt (es)"), ("document_en", "Comprobante (en)", "Receipt"),
                      ("status", "Estado", "Status")),
        rows=checklist, missing=missing, warnings=list((report or {}).get("warnings") or []) + warnings,
        sources=[SRC_ART151, SRC_USO_CFDI, *((report or {}).get("sources") or [])],
        assumptions=list((report or {}).get("assumptions") or []) + [
            "Medical deductions require non-cash payment (card, transfer or cheque) and a CFDI to your RFC.",
            "PPR contributions default to deposits into PPR accounts in the ledger when not stated."])


def _mx_aguinaldo_ptu(book: _Book) -> dict | None:
    """Only when stated: income facts of kind aguinaldo/ptu, or tax.<year>.mx.aguinaldo_mxn / ptu_mxn."""
    sid = "mx_aguinaldo_ptu"
    items = []
    for kind, key, days in (("aguinaldo", "aguinaldo_mxn", 30), ("ptu", "ptu_mxn", 15)):
        amount = _d(book.mx.get(key))
        fact_id = None
        if amount is None:
            for fact_key, fact in book.facts.items():
                value = fact["value"]
                if fact_key.startswith("income.") and isinstance(value, dict) and value.get("kind") == kind \
                        and value.get("currency", "MXN") == "MXN" and value.get("frequency") in {"annual", "one_off"}:
                    amount, fact_id = _d(value.get("amount")), fact["id"]
                    if value.get("net") is True:
                        amount = None  # a net figure is after tax; the exemption applies to the gross
                    break
        if amount is not None or fact_id:
            items.append((kind, amount, days, fact_id))
    if not items:
        return None
    params = mexico._Params({"parameters": book.parameters})
    uma = params.decimal("uma_daily_mxn", book.year)
    rows, missing = [], []
    for kind, amount, days, fact_id in items:
        if fact_id:
            book.evidence.add(fact_id)
        cap = None if uma is None else uma * days
        exempt = None if cap is None or amount is None else min(amount, cap)
        if amount is None:
            missing.append(_need(sid, f"tax.{book.year}.mx.{kind}_mxn", f"Monto bruto del {kind} del año.",
                                 f"Gross {kind} for the year."))
        rows.append({"concept": kind, "gross_mxn": _m(amount), "exempt_cap_mxn": _m(cap),
                     "exempt_mxn": _m(exempt), "taxable_mxn": _m(None if exempt is None else amount - exempt),
                     "rule": f"{days} UMA diarias / daily UMA"})
    for item in params.missing:
        missing.append(_need(sid, item, f"UMA diaria {book.year} (INEGI/DOF) con su fuente.",
                             f"The {book.year} daily UMA (INEGI/DOF) with its source."))
    return _section(sid, "Aguinaldo y PTU (exenciones)", "Christmas bonus and profit sharing (exemptions)", "MX",
                    currency="MXN", summary={"uma_daily_mxn": None if uma is None else _m(uma)},
                    columns=_cols(("concept", "Concepto", "Concept"), ("gross_mxn", "Bruto", "Gross"),
                                  ("exempt_cap_mxn", "Tope exento", "Exempt cap"), ("exempt_mxn", "Exento", "Exempt"),
                                  ("taxable_mxn", "Gravado", "Taxable")),
                    rows=rows, missing=missing, sources=[SRC_ART93_XIV, *params.sources],
                    assumptions=["Shown only because the person stated these amounts; the employer's CFDI de nómina "
                                 "is the source of truth."])
