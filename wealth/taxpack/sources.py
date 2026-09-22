"""Dated sources, thresholds and account classes the tax pack cites and applies."""
from __future__ import annotations

from decimal import Decimal
from typing import Any

from .. import mexico


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

DOCUMENT_BLOCKS = ("enajenacion", "intereses", "dividendos", "form_1099_b", "form_1099_div", "form_1099_int",
                   "form_5498")
