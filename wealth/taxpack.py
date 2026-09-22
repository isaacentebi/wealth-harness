"""The annual tax pack: what a person hands their contador (Mexico) or CPA (US).

``tax_pack`` builds one year's working papers from the transaction ledger and
the saved facts; it never prepares or files a return.  Numbers come from the
existing engines and dated tables:

* Mexico (persona fisica): Art. 129 sales per broker with cost updated by INPC
  (average cost, CFF Art. 17-A factor), the net result, the 10% and loss
  carryforwards; interest nominal and real per institution with the ISR
  withheld; domestic and foreign dividends; foreign securities at a foreign
  broker through :func:`wealth.mexico.foreign_securities`; Art. 151/185
  deductions through :func:`wealth.mexico.personal_deductions` with the CFDI
  checklist; aguinaldo/PTU exemptions only when stated.
* US: a Form 8949-style list of lots with wash-sale adjustments (code W),
  Schedule D totals and carryovers, 1099-DIV/INT summaries, foreign tax paid
  (Form 1116 inputs), IRA/Roth contributions against the limit, RMDs taken, and
  FBAR/Form 8938 flags for foreign accounts.

Honesty rules: unknown is never zero (a figure that cannot be computed is
``None`` and appears in ``pendientes``); where an institution's constancia or
1099 was saved (``constancia.<id>``) it is the source of truth, and the pack
shows our computation, the document and the difference.

Saved facts read: ``client.profile``, ``tax.<year>`` (the year's stated tax
facts), ``constancia.<id>`` (documents), ``income.<id>`` (aguinaldo/PTU),
``cash.<id>``/``investment.<id>`` (foreign-account values).  See
:data:`wealth.situation.schema.SCHEMA` for their shapes.
"""
from __future__ import annotations

import csv
import html
import io
import json
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Mapping

from . import mexico
from . import retirement
from .finmath import FxTable
from .ledger.derive import active_entries, match_transfers, replay
from .ledger.model import fold

ZERO = Decimal(0)
CENT = Decimal("0.01")
FX_AGE_DAYS = 7

# --------------------------------------------------------------------- sources

LISR = mexico.LISR_URL
CFF_URL = "https://www.diputados.gob.mx/LeyesBiblio/pdf/CFF.pdf"
SAT_USO_CFDI_URL = "http://omawww.sat.gob.mx/tramitesyservicios/Paginas/anexo_20.htm"
SAT_ANUAL_URL = "https://www.sat.gob.mx/personas/declaraciones"
IRS_8949_URL = "https://www.irs.gov/instructions/i8949"
IRS_SCHED_D_URL = "https://www.irs.gov/instructions/i1040sd"
IRS_PUB_550_URL = "https://www.irs.gov/publications/p550"
IRS_1116_URL = "https://www.irs.gov/instructions/i1116"
IRS_1099DIV_URL = "https://www.irs.gov/instructions/i1099div"
IRS_PUB_590A_URL = "https://www.irs.gov/publications/p590a"
IRS_PUB_590B_URL = "https://www.irs.gov/publications/p590b"
FINCEN_FBAR_URL = "https://www.fincen.gov/report-foreign-bank-and-financial-accounts"
ECFR_FBAR_URL = "https://www.ecfr.gov/current/title-31/subtitle-B/chapter-X/part-1010/subpart-C/section-1010.350"
ECFR_FBAR_DUE_URL = "https://www.ecfr.gov/current/title-31/subtitle-B/chapter-X/part-1010/subpart-C/section-1010.306"
IRS_8938_URL = "https://www.irs.gov/instructions/i8938"
IRS_8938_VS_FBAR_URL = "https://www.irs.gov/businesses/comparison-of-form-8938-and-fbar-requirements"
TREASURY_RATES_URL = "https://fiscaldata.treasury.gov/datasets/treasury-reporting-rates-exchange/"


def _lisr(article: str, rule: str) -> dict[str, Any]:
    return {"title": f"Ley del Impuesto sobre la Renta, {article}", "url": LISR, "version": mexico.LISR_VERSION,
            "rules": [rule]}


SRC_ART129 = _lisr("articulo 129", "10% on the net gain from listed shares per year; losses offset Art. 129 gains of "
                   "the year and of the next ten years, updated for inflation")
SRC_ART22_23 = _lisr("articulos 22 y 23", "cost of shares: average cost per share, updated for inflation")
SRC_CFF17A = {"title": "Codigo Fiscal de la Federacion, articulo 17-A", "url": CFF_URL,
              "rules": ["factor de actualizacion = INPC of the later month / INPC of the earlier month; "
                        "a factor below 1 is taken as 1"]}
SRC_INPC = {"title": "INEGI, Indice Nacional de Precios al Consumidor", "url": mexico.INEGI_INPC_URL}
SRC_ART133_136 = _lisr("articulos 133-136", "interest: real interest is accumulable; retention on capital is creditable")
SRC_ART55 = _lisr("articulo 55 fraccion II", "institutions deliver the constancia of nominal and real interest and "
                  "retention by February 15")
SRC_ART140 = _lisr("articulo 140", "dividends from Mexican companies: accumulable with credit for corporate ISR; "
                   "additional 10% definitive withheld on post-2013 CUFIN")
SRC_ART142V = _lisr("articulo 142 fraccion V", "dividends from foreign residents: accumulable plus an additional "
                    "definitive 10%")
SRC_ART5 = _lisr("articulo 5", "credit for foreign income tax on foreign-source income taxable in Mexico")
SRC_ART151 = _lisr("articulo 151", "personal deductions, global cap and CFDI/payment requirements")
SRC_ART93_XIV = _lisr("articulo 93 fraccion XIV", "aguinaldo exempt up to 30 UMA daily values; PTU exempt up to 15")
SRC_USO_CFDI = {"title": "SAT Anexo 20, catalogo c_UsoCFDI (D01-D10 deducciones personales)", "url": SAT_USO_CFDI_URL}
SRC_ANUAL = {"title": "SAT, declaracion anual de personas fisicas (LISR Art. 150: April of the following year)",
             "url": SAT_ANUAL_URL}
SRC_8949 = {"title": "IRS Instructions for Form 8949 (boxes A-F, adjustment code W)", "url": IRS_8949_URL}
SRC_SCHED_D = {"title": "IRS Instructions for Schedule D (Form 1040): netting, $3,000 limit ($1,500 MFS), "
                        "capital loss carryover", "url": IRS_SCHED_D_URL}
SRC_PUB550 = {"title": "IRS Publication 550: wash sales (30 days before or after), basis and holding-period "
                       "adjustment", "url": IRS_PUB_550_URL}
SRC_RR_2008_5 = {"title": "Rev. Rul. 2008-5: a loss is disallowed and not added to basis when the replacement is "
                          "bought in an IRA or Roth IRA", "url": "https://www.irs.gov/pub/irs-irbs/irb08-03.pdf"}
SRC_1099DIV = {"title": "IRS Instructions for Forms 1099-DIV (box 1a ordinary, 1b qualified, 2a capital gain "
                        "distributions, 7 foreign tax paid)", "url": IRS_1099DIV_URL}
SRC_1116 = {"title": "IRS Instructions for Form 1116 (foreign tax credit, passive category income)",
            "url": IRS_1116_URL}
SRC_590A = {"title": "IRS Publication 590-A: IRA contribution limits (traditional and Roth combined)",
            "url": IRS_PUB_590A_URL}
SRC_590B = {"title": "IRS Publication 590-B: required minimum distributions", "url": IRS_PUB_590B_URL}
SRC_FBAR = {"title": "FinCEN, Report of Foreign Bank and Financial Accounts (FBAR, FinCEN Form 114): required when "
                     "the aggregate maximum value of foreign financial accounts exceeds $10,000 at any time in the "
                     "calendar year; 31 CFR 1010.350", "url": FINCEN_FBAR_URL, "regulation": ECFR_FBAR_URL}
SRC_FBAR_DUE = {"title": "31 CFR 1010.306(c): FBAR due April 15, automatic extension to October 15",
                "url": ECFR_FBAR_DUE_URL}
SRC_8938 = {"title": "IRS Instructions for Form 8938 (IRC 6038D; Treas. Reg. 1.6038D-2): reporting thresholds for "
                     "specified foreign financial assets", "url": IRS_8938_URL,
            "comparison": IRS_8938_VS_FBAR_URL}
SRC_TREASURY_RATES = {"title": "US Treasury Reporting Rates of Exchange (FBAR and Form 8938 use the last-day-of-year "
                                "rate)", "url": TREASURY_RATES_URL}

# FBAR: 31 CFR 1010.306(c) and FinCEN guidance; not indexed.
FBAR_THRESHOLD_USD = Decimal(10000)
# Form 8938 thresholds (Instructions for Form 8938; Treas. Reg. 1.6038D-2): (last day of year, any time in year).
FORM_8938_THRESHOLDS = {
    ("us", False): (Decimal(50000), Decimal(75000)),
    ("us", True): (Decimal(100000), Decimal(150000)),
    ("abroad", False): (Decimal(200000), Decimal(300000)),
    ("abroad", True): (Decimal(400000), Decimal(600000)),
}
ART140_GROSS_UP = Decimal("1.4286")  # LISR Art. 140 first paragraph
ART140_CORPORATE_RATE = Decimal("0.30")  # LISR Art. 9
US_CAPITAL_LOSS_LIMIT = {"married_filing_separately": Decimal(1500)}  # everyone else: $3,000 (IRC 1211(b))
# IRC 402(g)(1)(B) as indexed and 414(v)(2)(B)/(E) catch-ups (ages 60-63 from 2025, SECURE 2.0 sec. 109).
US_DEFERRAL_LIMITS = {
    2025: {"deferral": 23500, "catch_up_50": 7500, "catch_up_60_63": 11250,
           "source": {"title": "IRS Notice 2024-80 (2025 limits: 402(g) $23,500; 414(v) catch-up $7,500, ages 60-63 "
                               "$11,250)", "url": "https://www.irs.gov/pub/irs-drop/n-24-80.pdf"}},
    2026: {"deferral": 24500, "catch_up_50": 8000, "catch_up_60_63": 11250,
           "source": {"title": "IRS Notice 2025-67 (2026 limits: 402(g) $24,500; 414(v) catch-up $8,000, ages 60-63 "
                               "$11,250)", "url": "https://www.irs.gov/pub/irs-drop/n-25-67.pdf"}},
}

# Institutions we can place without a stated country (checked before the currency fallback).
_MX_INSTITUTIONS = ("gbm", "bbva", "banorte", "santander mexico", "banamex", "citibanamex", "hsbc mexico",
                    "scotiabank", "inbursa", "actinver", "kuspit", "bursanet", "cetesdirecto", "cetes directo",
                    "nu mexico", "nu", "hey banco", "mercado pago", "stori", "klar", "vector", "monex", "finamex",
                    "banregio", "afirme", "ve por mas", "fintual", "flink", "afore", "invex", "multiva", "bajio",
                    "banco azteca", "cuenca", "uala", "openbank", "plata", "profuturo", "sura", "principal", "coppel",
                    "pensionissste", "infonavit")
_US_INSTITUTIONS = ("charles schwab", "schwab", "fidelity", "vanguard", "interactive brokers", "alpaca", "robinhood",
                    "e trade", "etrade", "td ameritrade", "merrill", "wells fargo", "chase", "jpmorgan",
                    "bank of america", "morgan stanley", "ally", "marcus", "sofi", "webull", "tastytrade",
                    "firstrade", "vest", "public", "wealthfront", "betterment")

_BANK_TYPES = frozenset({"checking", "savings", "bank", "cash", "debit"})
_LIABILITY_TYPES = frozenset({"credit_card", "loan", "mortgage", "line_of_credit"})
_MX_RETIREMENT_TYPES = frozenset({"afore", "ppr", "retirement"})
_IRA_TYPES = frozenset({"ira", "traditional_ira", "rollover_ira", "sep_ira", "simple_ira"})
_ROTH_TYPES = frozenset({"roth_ira", "roth"})
# Employer plans: elective deferrals count against IRC 402(g) (457(b): 457(e)(15)), never the IRA limit.
_WORKPLACE_TYPES = frozenset({"401k", "403b", "457", "457b", "roth_401k", "roth_403b", "roth_457b"})
# IRC 219(b)(5) and 408A(c)(2): the IRA limit covers traditional and Roth IRA contributions only (SEP employer
# contributions and SIMPLE deferrals have their own limits).
_US_IRA_ONLY = frozenset({"ira", "traditional_ira", "rollover_ira", "roth_ira", "roth"})
_TAX_SHELTERED = frozenset({"ira", "roth", "workplace", "hsa", "mx_retirement", "liability"})
_DEBT_CLASSES = frozenset({"fixed_income", "bond", "money_market", "cash"})
_FIBRA_CLASSES = frozenset({"reit", "fibra", "real_estate"})

CFDI_CHECKLIST = (
    ("medical_mxn", "D01", "Honorarios médicos, dentales, psicología y nutrición; gastos hospitalarios; lentes ópticos "
                           "(hasta $2,500)", "Medical, dental, psychology and nutrition fees; hospital costs; "
                           "prescription lenses (up to MXN 2,500)",
     "CFDI a tu RFC, pagado con tarjeta, transferencia o cheque (no en efectivo).",
     "A CFDI to your RFC, paid by card, transfer or cheque (not cash)."),
    ("disability_medical_mxn", "D02", "Gastos médicos por incapacidad o discapacidad",
     "Medical costs for incapacity or disability", "CFDI y certificado de incapacidad/discapacidad.",
     "CFDI and the incapacity/disability certificate."),
    ("funeral_mxn", "D03", "Gastos funerales (hasta un UMA anual)", "Funeral costs (up to one annual UMA)",
     "CFDI a tu RFC.", "A CFDI to your RFC."),
    ("donations_mxn", "D04", "Donativos a donatarias autorizadas (hasta 7% del ingreso acumulable del año anterior)",
     "Donations to authorised charities (up to 7% of the prior year's accumulable income)",
     "CFDI de la donataria autorizada.", "The charity's CFDI."),
    ("mortgage", "D05", "Intereses reales de crédito hipotecario (casa habitación, hasta 750,000 UDIs)",
     "Real mortgage interest (own home, credit up to 750,000 UDIs)",
     "Constancia anual de intereses reales del banco o Infonavit.", "The lender's annual real-interest constancia."),
    ("ppr_mxn", "D06", "Aportaciones voluntarias / complementarias de retiro y PPR (Art. 151 fr. V)",
     "Voluntary retirement contributions and PPR (Art. 151 fr. V)",
     "Constancia de aportaciones de la AFORE o institución del PPR.",
     "The AFORE's or PPR institution's contribution constancia."),
    ("insurance_premiums_mxn", "D07", "Primas de seguros de gastos médicos mayores", "Major medical insurance premiums",
     "CFDI de la aseguradora.", "The insurer's CFDI."),
    ("school_transport_mxn", "D08", "Transporte escolar obligatorio", "Mandatory school transport",
     "CFDI con el transporte desglosado.", "A CFDI showing transport separately."),
    ("art185_mxn", "D09", "Depósitos en cuentas especiales para el ahorro (Art. 185)",
     "Deposits in special savings accounts (Art. 185)", "Constancia de la institución.", "The institution's constancia."),
    ("tuition_mxn", "D10", "Colegiaturas (decreto; límites por nivel escolar)", "School tuition (decree; limits per "
                                                                                 "school level)",
     "CFDI con la CURP del alumno y el nivel escolar.", "A CFDI with the student's CURP and school level."),
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


def _sum(values: Iterable[Decimal | None]) -> Decimal | None:
    total = ZERO
    for value in values:
        if value is None:
            return None
        total += value
    return total


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
                          withheld=_d(block.get("isr_withheld")) or ZERO,
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
        if block and bucket.get("ledger") is not False:
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
        from .tax import _net_us
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
    if (profile.get("us_person") is True or (citizen and profile.get("us_person") is not False)) and "US" not in codes:
        codes.append("US")
        basis += "; US person (citizen or green card): US tax on worldwide income"
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
        from .policy import snapshot_from_facts
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


DOCUMENT_BLOCKS = ("enajenacion", "intereses", "dividendos", "form_1099_b", "form_1099_div", "form_1099_int",
                   "form_5498")


# ----------------------------------------------------------------- CSV and HTML


def _cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, default=str)
    return str(value)


def csv_files(report: Mapping[str, Any], language: str | None = None) -> dict[str, str]:
    """One CSV per section (its table), plus pendientes, deadlines and reconciliation.  Empty cell = unknown."""
    result = report.get("result") or {}
    lang = language or result.get("language") or "es"
    lang = lang if lang in {"es", "en"} else "es"
    year = result.get("tax_year")
    files: dict[str, str] = {}

    def write(name: str, columns: list[dict], rows: list[Mapping[str, Any]]) -> None:
        buffer = io.StringIO()
        writer = csv.writer(buffer, lineterminator="\n")
        writer.writerow([c.get(lang) or c["key"] for c in columns])
        for row in rows:
            writer.writerow([_cell(row.get(c["key"])) for c in columns])
        files[f"tax-pack-{year}-{name}.csv"] = buffer.getvalue()

    recon_rows = []
    for sid in result.get("section_order") or []:
        section = result["sections"][sid]
        table = section.get("table") or {}
        if table.get("columns"):
            write(sid, table["columns"], table.get("rows") or [])
        for item in section.get("reconciliation") or []:
            recon_rows.append({"section": sid, **item})
    write("pendientes", _cols(("section", "Sección", "Section"), ("key", "Clave", "Key"),
                              ("detail_es", "Qué falta", "What is missing (es)"),
                              ("detail", "Qué falta (en)", "What is missing")), result.get("pendientes") or [])
    write("deadlines", _cols(("date", "Fecha", "Date"), ("jurisdiction", "País", "Jurisdiction"),
                             ("es", "Qué", "What (es)"), ("en", "Qué (en)", "What"), ("basis", "Fundamento", "Basis")),
          result.get("deadlines") or [])
    if recon_rows:
        write("reconciliation", _cols(("section", "Sección", "Section"), ("item", "Concepto", "Item"),
                                      ("ours", "Nuestro cálculo", "Our computation"),
                                      ("document", "Documento", "Document"), ("difference", "Diferencia", "Difference"),
                                      ("source_of_truth", "Fuente de verdad", "Source of truth")), recon_rows)
    return files


def _e(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def _bi(es: str, en: str) -> str:
    return f'<span class="l-es" lang="es">{_e(es)}</span><span class="l-en" lang="en">{_e(en)}</span>'


_UNKNOWN = '<span class="unk">' + _bi("desconocido", "unknown") + "</span>"


def _num(value: Any) -> str:
    """Money strings (with a decimal point) get thousands separators; other values print as they are."""
    if value is None:
        return _UNKNOWN
    if isinstance(value, bool):
        return _bi("sí", "yes") if value else _bi("no", "no")
    if isinstance(value, str) and "." in value:
        number = _d(value)
        if number is not None and value.lstrip("-").replace(".", "", 1).isdigit():
            places = max(2, len(value.split(".", 1)[1]))  # rates and factors keep their precision
            return _e(f"{number:,.{places}f}")
    if isinstance(value, (dict, list)):
        return _e(json.dumps(value, ensure_ascii=False, default=str))
    return _e(value)


_SUMMARY_LABELS = {
    "net_result_mxn": ("Resultado neto Art. 129", "Net Art. 129 result"),
    "carry_used_mxn": ("Pérdidas anteriores aplicadas", "Prior losses used"),
    "taxable_gain_mxn": ("Ganancia gravable", "Taxable gain"),
    "tax_10pct_mxn": ("ISR 10% a pagar", "10% tax due"),
    "new_loss_carryforward_mxn": ("Pérdida nueva por amortizar", "New loss to carry forward"),
    "new_loss_expires": ("Vence (año)", "Expires (year)"),
    "real_interest_mxn": ("Interés real a acumular", "Real interest to accumulate"),
    "real_interest_loss_mxn": ("Pérdida real (Art. 134)", "Real interest loss (Art. 134)"),
    "retention_mxn": ("ISR retenido acreditable", "Creditable ISR withheld"),
    "nominal_interest_mxn": ("Interés nominal", "Nominal interest"),
    "inflation_factor": ("Factor de inflación del año", "Inflation factor for the year"),
    "known_tax_mxn": ("Impuesto conocido", "Known tax"),
    "net_estimated_mexican_tax_mxn": ("Impuesto mexicano estimado", "Estimated Mexican tax"),
    "short_term_before_carryover_usd": ("Corto plazo (antes de arrastre)", "Short term (before carryover)"),
    "long_term_before_carryover_usd": ("Largo plazo (antes de arrastre)", "Long term (before carryover)"),
    "capital_gain_distributions_usd": ("Distribuciones de ganancias (1099-DIV 2a)", "Capital gain distributions (2a)"),
    "net_short_term_usd": ("Neto corto plazo", "Net short term"),
    "net_long_term_usd": ("Neto largo plazo", "Net long term"),
    "net_capital_gain_or_loss_usd": ("Ganancia o pérdida neta", "Net capital gain or loss"),
    "deductible_loss_usd": ("Pérdida deducible este año", "Loss deductible this year"),
    "loss_limit_usd": ("Límite de pérdida deducible", "Deductible loss limit"),
    "dividends_usd": ("Dividendos", "Dividends"), "qualified_dividends_usd": ("Dividendos calificados", "Qualified"),
    "interest_usd": ("Intereses", "Interest"), "total_usd": ("Total", "Total"),
    "ordinary_dividends_to_report_usd": ("Dividendos ordinarios a declarar", "Ordinary dividends to report"),
    "interest_to_report_usd": ("Intereses a declarar", "Interest to report"),
    "uma_daily_mxn": ("UMA diaria", "Daily UMA"), "ppr_from_ledger_mxn": ("Aportaciones PPR (ledger)", "PPR deposits (ledger)"),
}


def _summary_html(section: Mapping[str, Any]) -> str:
    summary = section.get("summary") or {}
    parts = []
    scalars = [(k, v) for k, v in summary.items() if k in _SUMMARY_LABELS and not isinstance(v, (dict, list))]
    if scalars:
        parts.append('<dl class="ticket">' + "".join(
            f'<div class="row{" total" if k in {"tax_10pct_mxn", "net_capital_gain_or_loss_usd", "real_interest_mxn"} else ""}">'
            f"<dt>{_bi(*_SUMMARY_LABELS[k])}</dt><dd class=\"v\">{_num(v)}</dd></div>" for k, v in scalars) + "</dl>")
    brokers = summary.get("brokers")
    if isinstance(brokers, list) and brokers and isinstance(brokers[0], dict) and "declared_net_mxn" in brokers[0]:
        head = ("<tr><th>" + _bi("Casa de bolsa", "Broker") + "</th><th>" + _bi("Ganancias", "Gains") + "</th><th>"
                + _bi("Pérdidas", "Losses") + "</th><th>" + _bi("Neto (cálculo)", "Net (ours)") + "</th><th>"
                + _bi("Neto (constancia)", "Net (constancia)") + "</th><th>" + _bi("A declarar", "Declared")
                + "</th></tr>")
        body = "".join(f"<tr><th>{_e(b['broker'])}</th><td>{_num(b['gains_mxn'])}</td><td>{_num(b['losses_mxn'])}</td>"
                       f"<td>{_num(b['net_mxn'])}</td><td>{_num(b['constancia_net_mxn'])}</td>"
                       f"<td>{_num(b['declared_net_mxn'])}</td></tr>" for b in brokers)
        parts.append(f'<table class="grid"><thead>{head}</thead><tbody>{body}</tbody></table>')
    for key in ("fbar", "form_8938"):
        block = summary.get(key)
        if isinstance(block, dict):
            label = ("FBAR (FinCEN 114)", "FBAR (FinCEN 114)") if key == "fbar" else ("Formulario 8938", "Form 8938")
            required = block.get("required")
            verdict = (_bi("Requerido", "Required") if required is True else _bi("No requerido", "Not required")
                       if required is False else _bi("Desconocido: faltan saldos", "Unknown: balances missing"))
            detail = (f"{_num(block.get('aggregate_max_value_usd_at_least') or block.get('max_value_usd_at_least'))} "
                      f"USD ≥ · {_bi('umbral', 'threshold')} "
                      f"{_num(block.get('threshold_usd') or block.get('threshold_any_time_usd'))} USD")
            parts.append(f'<div class="flag"><strong>{_bi(*label)}: {verdict}</strong>'
                         f'<div class="sub">{detail}</div><div class="sub">{_e(block.get("rule"))}</div></div>')
    for key in ("contributions", "rmd"):
        block = summary.get(key)
        if isinstance(block, dict):
            parts.append('<dl class="ticket">' + "".join(
                f'<div class="row"><dt>{_e(k)}</dt><dd class="v">{_num(v)}</dd></div>'
                for k, v in block.items() if k != "item") + "</dl>")
    return "".join(parts)


def _table_html(section: Mapping[str, Any]) -> str:
    table = section.get("table") or {}
    columns, rows = table.get("columns") or [], table.get("rows") or []
    if not columns or not rows:
        return ""
    head = "".join(f"<th>{_bi(c['es'], c['en'])}</th>" for c in columns)
    body = "".join("<tr>" + "".join(f"<td>{_num(row.get(c['key']))}</td>" for c in columns) + "</tr>" for row in rows)
    return f'<div class="scroll"><table class="grid"><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>'


def _recon_html(section: Mapping[str, Any]) -> str:
    rows = section.get("reconciliation") or []
    if not rows:
        return ""
    head = ("<tr><th>" + _bi("Concepto", "Item") + "</th><th>" + _bi("Nuestro cálculo", "Our computation")
            + "</th><th>" + _bi("Documento", "Document") + "</th><th>" + _bi("Diferencia", "Difference") + "</th></tr>")
    body = "".join(f"<tr><th>{_e(r['item'])}</th><td>{_num(r['ours'])}</td><td>{_num(r['document'])}</td>"
                   f"<td>{_num(r['difference'])}</td></tr>" for r in rows)
    return (f'<h3>{_bi("Conciliación con la constancia (la constancia manda)", "Reconciliation with the document (the document wins)")}'
            f'</h3><table class="grid recon"><thead>{head}</thead><tbody>{body}</tbody></table>')


_STATUS = {"ready": ("completo", "complete"), "partial": ("parcial", "partial"),
           "needs_input": ("faltan datos", "needs input"), "not_applicable": ("sin movimientos", "no activity")}

_CSS = """
:root{--canvas:#FBF8F2;--ink:#2B2522;--ink-2:#4A413C;--muted:#6E625B;--hairline:#D9CEC6;--vermilion:#C84335;
--sans:Inter,ui-sans-serif,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
--mono:"Roboto Mono",ui-monospace,SFMono-Regular,Menlo,monospace;color-scheme:light}
*{box-sizing:border-box}html{background:var(--canvas)}
body{margin:0;background:var(--canvas);color:var(--ink);font:400 15px/24px var(--sans);font-variant-numeric:tabular-nums}
.page{width:min(960px,100%);margin:0 auto;padding:16px 24px 96px}
.top{display:flex;justify-content:space-between;align-items:center;min-height:48px}
.top button{min-height:40px;border:0;background:none;cursor:pointer;font:500 13px var(--mono);color:var(--muted)}
.top button[aria-pressed=true]{color:var(--ink);text-decoration:underline;text-underline-offset:6px}
.origin{margin:24px 0 0;font:400 13px/20px var(--sans);color:var(--muted)}
h1{margin:6px 0 0;font:700 40px/44px var(--sans);letter-spacing:-.04em}
.meta{margin-top:12px;padding-bottom:8px;border-bottom:1.5px solid var(--ink);font:400 13px/20px var(--mono);color:var(--ink-2)}
section.part{margin-top:40px;padding-top:12px;border-top:1px solid var(--hairline)}
h2{margin:0 0 10px;font:600 18px/26px var(--sans)}h3{margin:18px 0 6px;font:600 14px/20px var(--sans);color:var(--ink-2)}
.status{font:400 12px var(--mono);color:var(--muted);margin-left:8px}
dl.ticket{margin:0}.row{display:flex;justify-content:space-between;gap:16px;padding:8px 0;border-top:1px solid var(--hairline)}
.row:first-child{border-top:0}.row dt{color:var(--ink-2)}.row dd{margin:0}.v{font:400 14px var(--mono);white-space:nowrap}
.row.total{border-top:1.5px solid var(--ink)}.row.total dt,.row.total .v{font-weight:600;color:var(--ink)}
.scroll{overflow-x:auto}
table.grid{width:100%;border-collapse:collapse;margin-top:8px;font-size:13px}
table.grid th,table.grid td{padding:6px 8px 6px 0;border-top:1px solid var(--hairline);text-align:right;vertical-align:baseline}
table.grid thead th{border-top:0;font:400 11px/16px var(--mono);color:var(--muted);text-align:right}
table.grid th:first-child,table.grid td:first-child,table.grid tbody th{text-align:left}
table.grid td{font-family:var(--mono);white-space:nowrap}
.unk{color:var(--vermilion);font-style:italic}
.flag{margin-top:10px;padding:8px 0;border-top:1px solid var(--hairline)}
.sub{font:400 12px/18px var(--mono);color:var(--muted)}
ul.notes{margin:8px 0 0;padding-left:18px;color:var(--muted);font-size:13px;line-height:20px}
ol.pend{margin:0;padding-left:22px}ol.pend li{padding:6px 0;border-top:1px solid var(--hairline)}
.foot{margin-top:40px;font:400 12px/18px var(--mono);color:var(--muted)}
html[data-lang=es] .l-en,html[data-lang=en] .l-es{display:none}
@page{margin:14mm 12mm 16mm}
@media print{html,body{background:#fff}body{font-size:9.5pt;line-height:1.4}.page{width:auto;padding:0}
.top{display:none!important}h1{font-size:22pt;line-height:1.1}section.part{margin-top:12pt;padding-top:6pt;break-inside:auto}
h2{font-size:12pt;break-after:avoid}table.grid{font-size:8pt}table.grid tr{break-inside:avoid}.scroll{overflow:visible}
.row{padding:3pt 0}a{text-decoration:none}}
@media (max-width:480px){h1{font-size:30px;line-height:34px}.page{padding:8px 16px 64px}}
"""


def render_html(report: Mapping[str, Any], language: str | None = None) -> str:
    """A self-contained printable page (es/en toggle; print to PDF from the browser)."""
    result = report.get("result") or {}
    lang = language or result.get("language") or "es"
    lang = lang if lang in {"es", "en"} else "es"
    year = result.get("tax_year")
    status = _STATUS.get(report.get("status"), (report.get("status"), report.get("status")))
    out = [f'<!doctype html><html lang="{lang}" data-lang="{lang}"><head><meta charset="utf-8">'
           '<meta name="viewport" content="width=device-width, initial-scale=1"><meta name="color-scheme" content="light">'
           f'<meta name="referrer" content="no-referrer"><title>Tax pack {_e(year)} · Wealth</title>'
           f"<style>{_CSS}</style></head><body><div class=\"page\">"
           '<div class="top"><button type="button" onclick="window.print()">'
           + _bi("Imprimir / PDF", "Print / PDF") + '</button><div role="group" aria-label="Language">'
           f'<button type="button" data-lang="es" aria-pressed="{str(lang == "es").lower()}">ES</button>'
           f'<button type="button" data-lang="en" aria-pressed="{str(lang == "en").lower()}">EN</button></div></div>'
           f'<p class="origin">{_bi("Paquete fiscal para tu contador", "Tax pack for your CPA")}'
           f'{" · " + _e(result.get("person")) if result.get("person") else ""}</p>'
           f"<h1>{_bi(f'Paquete fiscal {year}', f'Tax pack {year}')}</h1>"
           f'<div class="meta">{_e(", ".join(result.get("jurisdictions") or []))} · {_bi(*status)} · '
           f'{_bi("preparado el", "prepared")} {_e(result.get("prepared_on"))}</div>']
    pend = result.get("pendientes") or []
    out.append(f'<section class="part"><h2>{_bi("Pendientes", "Pending items")}'
               f'<span class="status">{len(pend)}</span></h2>')
    if pend:
        out.append('<ol class="pend">' + "".join(
            f'<li>{_bi(p.get("detail_es") or p["detail"], p["detail"])}<div class="sub">{_e(p["section"])} · '
            f'{_e(p["key"])}</div></li>' for p in pend) + "</ol>")
    else:
        out.append(f'<p>{_bi("Nada pendiente.", "Nothing pending.")}</p>')
    out.append("</section>")
    deadlines = result.get("deadlines") or []
    if deadlines:
        out.append(f'<section class="part"><h2>{_bi("Fechas clave", "Key deadlines")}</h2><dl class="ticket">'
                   + "".join(f'<div class="row"><dt>{_bi(d["es"], d["en"])}<div class="sub">{_e(d["jurisdiction"])} · '
                             f'{_e(d["basis"])}</div></dt><dd class="v">{_e(d["date"])}</dd></div>' for d in deadlines)
                   + "</dl></section>")
    for number, sid in enumerate(result.get("section_order") or [], start=1):
        section = result["sections"][sid]
        st = _STATUS.get(section["status"], (section["status"], section["status"]))
        out.append(f'<section class="part" id="{_e(sid)}"><h2>{number}. {_bi(section["title"]["es"], section["title"]["en"])}'
                   f'<span class="status">{_e(section.get("currency") or "")} · {_bi(*st)}</span></h2>')
        out.append(_summary_html(section))
        out.append(_table_html(section))
        out.append(_recon_html(section))
        notes = [*section.get("warnings", []), *section.get("assumptions", [])]
        if notes:
            out.append(f'<h3>{_bi("Notas y supuestos", "Notes and assumptions")}</h3><ul class="notes">'
                       + "".join(f"<li>{_e(n)}</li>" for n in notes) + "</ul>")
        if section.get("sources"):
            out.append(f'<h3>{_bi("Fuentes", "Sources")}</h3><ul class="notes">' + "".join(
                f'<li>{_e(s.get("title") or s.get("ref"))}{" — " + _e(s["url"]) if s.get("url") else ""}</li>'
                for s in section["sources"] if isinstance(s, dict)) + "</ul>")
        out.append("</section>")
    out.append(f'<p class="foot">{_bi("Preparado por Wealth con tus estados de cuenta, constancias y datos guardados. No es una declaración: revísalo con tu contador. Un dato vacío es desconocido, nunca cero.", "Prepared by Wealth from your statements, documents and saved facts. Not a return: review it with your CPA. An empty figure is unknown, never zero.")}</p>')
    out.append("</div><script>document.querySelectorAll('[data-lang]').forEach(function(b){if(b.tagName!=='BUTTON')return;"
               "b.addEventListener('click',function(){var l=b.getAttribute('data-lang');document.documentElement.setAttribute('data-lang',l);"
               "document.documentElement.lang=l;document.querySelectorAll('button[data-lang]').forEach(function(x){"
               "x.setAttribute('aria-pressed',String(x===b));});});});</script></body></html>")
    return "".join(out)


def write_exports(report: Mapping[str, Any], out_dir: Any, language: str | None = None,
                  formats: Iterable[str] = ("json", "csv", "html")) -> list[str]:
    """Write the pack as JSON, one CSV per section and the printable HTML; returns the paths written."""
    from pathlib import Path
    folder = Path(out_dir).expanduser()
    folder.mkdir(parents=True, exist_ok=True)
    year = (report.get("result") or {}).get("tax_year")
    written = []
    formats = set(formats)
    if "json" in formats:
        path = folder / f"tax-pack-{year}.json"
        body = {k: v for k, v in report.items() if k != "views"}
        path.write_text(json.dumps(body, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        written.append(str(path))
    if "csv" in formats:
        for name, text in csv_files(report, language).items():
            path = folder / name
            path.write_text(text, encoding="utf-8")
            written.append(str(path))
    if "html" in formats:
        path = folder / f"tax-pack-{year}.html"
        path.write_text(render_html(report, language), encoding="utf-8")
        written.append(str(path))
    return written


__all__ = ["run_task", "csv_files", "render_html", "write_exports", "FBAR_THRESHOLD_USD", "FORM_8938_THRESHOLDS"]
