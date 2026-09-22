"""Filing deadlines for the jurisdictions the pack covers."""
from __future__ import annotations

from datetime import date

from .book import _Book


# ----------------------------------------------------------------- deadlines


def _deadlines(book: _Book, jurisdictions: list[str], us_person: bool) -> list[dict]:
    y = book.year
    items = []
    if "MX" in jurisdictions:
        items += [
            {"date": f"{y}-12-31", "jurisdiction": "MX", "es": f"Último día para aportar al PPR y pagar deducciones de {y}",
             "en": f"Last day for {y} PPR contributions and deductible payments", "basis": "LISR Art. 151"},
            {"date": f"{y + 1}-02-15", "jurisdiction": "MX", "es": f"Las instituciones entregan las constancias {y}",
             "en": f"Institutions deliver the {y} constancias", "basis": "LISR Art. 55 fr. II"},
            {"date": f"{y + 1}-04-30", "jurisdiction": "MX", "es": f"Declaración anual {y} de personas físicas",
             "en": f"{y} annual return (personas físicas)", "basis": "LISR Art. 150",
             "note_es": "Si cae en inhábil se recorre al siguiente día hábil (CFF Art. 12).",
             "note_en": "Moves to the next business day if it falls on a holiday (CFF Art. 12)."},
            {"date": f"{y + 1}-04-30", "jurisdiction": "MX", "es": f"Depósitos Art. 185 aplicables a {y} (antes de "
                                                                   "presentar la declaración)",
             "en": f"Art. 185 deposits applied to {y} (before filing)", "basis": "LISR Art. 185"},
        ]
    if "US" in jurisdictions or us_person:
        abroad = str((book.profile.get("residence") or {}).get("country") or "").upper() not in {"", "US"}
        items += [
            {"date": f"{y + 1}-04-15", "jurisdiction": "US", "es": f"Declaración federal {y} (Form 1040) y pago",
             "en": f"{y} federal return (Form 1040) and payment", "basis": "IRC 6072(a)"},
            {"date": f"{y + 1}-04-15", "jurisdiction": "US", "es": f"Aportaciones a IRA/Roth para {y}",
             "en": f"IRA/Roth contributions for {y}", "basis": "IRC 219(f)(3)"},
            {"date": f"{y + 1}-04-15", "jurisdiction": "US", "es": f"FBAR {y} (prórroga automática al 15 de octubre)",
             "en": f"{y} FBAR (automatic extension to October 15)", "basis": "31 CFR 1010.306(c)"},
        ]
        if abroad:
            items.append({"date": f"{y + 1}-06-15", "jurisdiction": "US", "es": f"Prórroga automática del 1040 {y} "
                          "para residentes en el extranjero (los intereses corren desde el 15 de abril)",
                          "en": f"Automatic {y} Form 1040 extension for US persons abroad (interest runs from "
                                "April 15)", "basis": "Treas. Reg. 1.6081-5(a)(5)"})
        items.append({"date": f"{y + 1}-10-15", "jurisdiction": "US", "es": f"FBAR {y} con prórroga",
                      "en": f"{y} FBAR, extended", "basis": "31 CFR 1010.306(c)"})
    today = date.fromisoformat(book.today)
    filings = {f"{y} annual return (personas físicas)": ("la declaración anual {y}", "The {y} annual return"),
               f"{y} federal return (Form 1040) and payment": ("la declaración federal {y} (Form 1040)",
                                                               "The {y} federal return (Form 1040)"),
               f"{y} FBAR, extended": ("el FBAR {y}", "The {y} FBAR")}
    extension = next((i for i in items if i["en"].startswith(f"Automatic {y} Form 1040 extension")), None)
    if extension is not None:
        # Abroad, the 1040 is late only after the automatic June 15 extension.
        filings[extension["en"]] = filings.pop(f"{y} federal return (Form 1040) and payment")
    for item in items:
        day = date.fromisoformat(item["date"])
        item["days_until"] = (day - today).days
        filing = filings.get(item["en"])
        if item["days_until"] >= 0:
            item["status"] = "upcoming"
        elif filing is not None:
            es, en = (s.format(y=y) for s in filing)
            surcharge = "hay recargos" if item["jurisdiction"] == "MX" else "hay recargos e intereses"
            item["status"] = "overdue"
            item["overdue_es"] = f"{es[0].upper() + es[1:]} venció el {_fecha_es(day)}; presenta cuanto antes, {surcharge}."
            item["overdue_en"] = (f"{en} was due {day:%B} {day.day}, {day.year}; file as soon as possible, "
                                  + ("surcharges and inflation updates (recargos) accrue."
                                     if item["jurisdiction"] == "MX" else "penalties and interest accrue."))
        else:
            item["status"] = "passed"
    rank = {"overdue": 0, "upcoming": 1, "passed": 2}
    return sorted(items, key=lambda i: (rank[i["status"]], i["date"], i["jurisdiction"]))


_MESES = ("enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto", "septiembre", "octubre",
          "noviembre", "diciembre")


def _fecha_es(day: date) -> str:
    return f"{day.day} de {_MESES[day.month - 1]} de {day.year}"
