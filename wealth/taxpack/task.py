"""The tax pack task: inputs, jurisdictions and the assembled report."""
from __future__ import annotations

from datetime import date
from typing import Any, Mapping

from .sources import DOCUMENT_BLOCKS
from .book import _Book, _d, _need, _unique
from .mx import (
    _average_cost_disposals, _carryforwards, _mx_aguinaldo_ptu, _mx_deducciones, _mx_dividendos,
    _mx_enajenacion, _mx_foreign, _mx_intereses,
)
from .us import _us_1099, _us_8949, _us_accounts, _us_fbar, _us_foreign_tax, _us_retirement, _us_schedule_d
from .deadlines import _deadlines
from .render import csv_files, render_html


# ----------------------------------------------------------------- the task


def _jurisdictions(inputs: Mapping[str, Any], book: _Book) -> tuple[list[str], str]:
    raw = inputs.get("jurisdiction")
    if raw is not None:
        values = raw if isinstance(raw, list) else str(raw).upper().replace("+", ",").replace("BOTH", "MX,US").split(",")
        chosen = sorted({str(v).strip().upper() for v in values if str(v).strip()})
        if not chosen or set(chosen) - {"MX", "US"}:
            raise ValueError("jurisdiction must be MX, US or both (\"MX,US\")")
        return chosen, "stated in the request"
    stated = book.tax.get("jurisdiction")
    if isinstance(stated, list) and stated:
        return sorted({str(s).upper() for s in stated} & {"MX", "US"}), f"tax.{book.year}"
    profile = book.profile
    codes = [c for c in (profile.get("tax_residence") or []) if c in {"MX", "US"}]
    basis = "stated tax residence"
    if not codes:
        country = (profile.get("residence") or {}).get("country") or profile.get("country")
        if isinstance(country, str) and country.upper() in {"MX", "US"}:
            country = country.upper()
            codes, basis = [country], "country of residence (tax residence assumed there)"
    citizenship = profile.get("citizenship")
    # A US citizen is a US person whether or not anyone wrote us_person.
    citizen = isinstance(citizenship, list) and any(str(c).strip().upper() in {"US", "USA"} for c in citizenship)
    if (profile.get("us_person") is True or citizen) and "US" not in codes:
        codes.append("US")
        basis += "; US person (citizen or green card): US tax on worldwide income"
        if citizen and profile.get("us_person") is False:
            # Citizenship makes a US person; a saved "not a US person" beside it is stale or wrong, never a reason
            # to leave out the US return.
            basis += " (the profile also says us_person false; citizenship decides: check it)"
    return sorted(set(codes)), basis if codes else "unknown"


ALLOWED_INPUTS = frozenset({"tax_year", "jurisdiction", "facts", "ledger", "parameters", "inpc", "prices",
                            "language", "exports", "as_of"})


def run_task(inputs: Mapping[str, Any], snapshot: Mapping[str, Any], ledger: Mapping[str, Any] | None,
             today: str) -> dict:
    """Service entry for ``tax_pack``: one year's working papers for the contador or CPA."""
    inputs = dict(inputs)
    if "year" in inputs and "tax_year" not in inputs:  # the natural name for it
        inputs["tax_year"] = inputs.pop("year")
    unknown = sorted(set(inputs) - ALLOWED_INPUTS)
    if unknown:
        raise ValueError(f"tax_pack inputs: unknown {unknown}; allowed {sorted(ALLOWED_INPUTS)}")
    today = str(inputs.get("as_of") or today)[:10]
    if "facts" in inputs:
        from ..policy import snapshot_from_facts
        snapshot = snapshot_from_facts(inputs["facts"], today)
    if "ledger" in inputs:
        ledger = inputs["ledger"]
    year = inputs.get("tax_year")
    year_basis = "stated in the request"
    if year is None:
        year, year_basis = date.fromisoformat(today).year - 1, "the last completed calendar year"
    if isinstance(year, bool) or not isinstance(year, int) or not 2000 <= year <= 2100:
        raise ValueError("tax_year must be an integer year")
    book = _Book(inputs, snapshot, ledger, year, today)
    jurisdictions, jurisdiction_basis = _jurisdictions(inputs, book)
    if not jurisdictions:
        missing = [_need("pack", "jurisdiction", "¿Dónde eres residente fiscal (MX, US o ambos)?",
                         "Where are you tax resident (MX, US or both)?")]
        if book.stale_profile:
            missing.append(_need("pack", "client.profile", "Confirma tu perfil (residencia fiscal y si eres "
                                 "persona estadounidense): su fecha de revisión pasó.",
                                 "Reconfirm your profile (tax residence and US person status): its review date "
                                 "has passed.", reason="stale"))
        return {"status": "needs_input", "result": {"tax_year": year}, "_evidence": sorted(book.evidence),
                "missing": missing, "warnings": list(book.warnings), "sources": [], "assumptions": []}
    us_person = book.profile.get("us_person") is True or "US" in jurisdictions
    sections: list[dict] = []
    if "MX" in jurisdictions:
        disposals, notes = _average_cost_disposals(book)
        carries, _, _ = _carryforwards(book, "mx_enajenacion")
        # First the Mexican brokers (their net feeds the foreign broker's netting), then the foreign broker.
        mx_only, _ = _mx_enajenacion(book, disposals, False)
        mx_net = _d(mx_only["summary"].get("net_result_mxn"))
        foreign, sic_net = _mx_foreign(book, disposals, mx_net, carries)
        enajenacion, _ = _mx_enajenacion(book, disposals, sic_net)
        enajenacion["warnings"] = notes + enajenacion["warnings"]
        if sic_net is None:
            enajenacion["missing"].append(_need("mx_enajenacion", "mx_extranjero", "Resultado de valores SIC en el "
                                                "intermediario extranjero (ver esa sección).",
                                                "Result of SIC securities at the foreign broker (see that section)."))
            enajenacion["summary"].update(net_result_mxn=None, taxable_gain_mxn=None, tax_10pct_mxn=None)
            enajenacion["status"] = "partial"
        sections.append(enajenacion)
        if foreign:
            sections.append(foreign)
        sections.append(_mx_intereses(book, enajenacion.get("debt_sales") or []))
        sections.append(_mx_dividendos(book))
        sections.append(_mx_deducciones(book))
        bonus = _mx_aguinaldo_ptu(book)
        if bonus:
            sections.append(bonus)
    if "US" in jurisdictions:
        s8949, totals = _us_8949(book)
        s1099, foreign_tax, cgd = _us_1099(book, _us_accounts(book, us_person))
        sections += [s8949, _us_schedule_d(book, totals, cgd), s1099,
                     _us_foreign_tax(book, foreign_tax, "MX" in jurisdictions)]
        retire = _us_retirement(book)
        if retire:
            sections.append(retire)
    if us_person:
        fbar = _us_fbar(book, inputs.get("prices"))
        if fbar:
            sections.append(fbar)
    pendientes = _unique(item for s in sections for item in s["missing"])
    if book.stale_profile:
        pendientes = _unique([*pendientes, _need(
            "pack", "client.profile", "Confirma tu perfil (residencia, si eres persona estadounidense, año de "
            "nacimiento): su fecha de revisión pasó y no se usó.",
            "Reconfirm your profile (residence, US person status, birth year): its review date has passed and it "
            "was not used.", reason="stale")])
    deadlines = _deadlines(book, jurisdictions, us_person)
    computed = [s for s in sections if s["status"] not in {"not_applicable", "needs_input"}]
    status = "needs_input" if not computed and pendientes else ("partial" if pendientes else "ready")
    if not book.entries:
        status = "needs_input" if not computed else status
    warnings = list(book.warnings) + [w for s in sections for w in s["warnings"]]
    sources = _unique([*[src for s in sections for src in s["sources"]], *book.ledger_sources])
    assumptions = [f"Tax year {year}: {year_basis}.", f"Jurisdiction {', '.join(jurisdictions)}: {jurisdiction_basis}.",
                   "Working papers for your contador or CPA, not a return: every figure shows its source; where an "
                   "institution's constancia or 1099 was saved it is the source of truth and the difference is shown.",
                   "Unknown is never zero: a figure that cannot be computed is empty and listed under Pendientes."]
    language = inputs.get("language") or book.profile.get("language") or ("es" if "MX" in jurisdictions else "en")
    overdue = [d[f"overdue_{'es' if language == 'es' else 'en'}"] for d in deadlines if d["status"] == "overdue"]
    warnings = overdue + warnings
    if overdue:
        year_basis += "; its return deadline has passed, so it is due now (overdue unless already filed)"
    result = {"tax_year": year, "tax_year_basis": year_basis, "jurisdictions": jurisdictions,
              "jurisdiction_basis": jurisdiction_basis, "us_person": us_person, "language": language,
              "person": book.profile.get("name"), "prepared_on": today,
              "sections": {s["id"]: s for s in sections}, "section_order": [s["id"] for s in sections],
              "pendientes": pendientes, "deadlines": deadlines,
              "documents": [{"id": d["id"], "institution": d.get("institution"),
                             "blocks": sorted(k for k in d if k in DOCUMENT_BLOCKS),
                             "source": "uploaded document" if d["_source"] == "document" else "stated",
                             "ref": d["_ref"] if d["_source"] == "document" else None} for d in book.constancias],
              "ledger": {"first_entry": book.first_day, "last_entry": book.last_day,
                         "covers_year": book.covers_year(), "sources": book.ledger_sources}}
    report = {"status": status, "result": result, "missing": pendientes, "warnings": list(dict.fromkeys(warnings)),
              "sources": sources, "assumptions": assumptions, "_evidence": sorted(book.evidence)}
    exports = inputs.get("exports") or []
    if not isinstance(exports, list) or set(exports) - {"csv", "html"}:
        raise ValueError("exports must be a list of csv and/or html")
    if "csv" in exports:
        result["csv"] = csv_files(report)
    if "html" in exports:
        result["html"] = render_html(report)
    return report
