"""Retirement engine for Mexico (IMSS Ley 73 / Ley 97, AFORE) and US residents.

Three service tasks:

* ``retirement_mx``: IMSS regime, Ley 73 pension (Art. 167 table, cesantia
  percentages, 11% decree factor, minimum pension), Modalidad 40 break-even
  with payback and IRR, Ley 97 AFORE projection under the 2020 reform schedule
  with fees, weeks requirement, programmed withdrawal and pension garantizada,
  voluntary contributions, and the gap against target spending in real MXN.
* ``retirement_us``: Social Security claim ages 62-70 from a supplied PIA with
  breakeven ages and a spousal note, 2026 contribution limits (including the
  Roth catch-up wage rule), RMD age and amount, and account-location-aware
  withdrawal ordering with bracket-filling Roth conversions on the existing US
  bracket engine.
* ``retirement_readiness``: required nest egg over a safe-withdrawal range, the
  Monte Carlo probability from :mod:`wealth.planning`, and the gap with the
  monthly contribution that would close it.

Parameters follow the :mod:`wealth.mexico` pattern: a dated table where each
entry is ``verified`` (official text read on ``checked_on``), ``statutory``
(long-standing statutory figure cited to its section, not re-read here) or
``needs_verification`` (never used; the caller must supply it under
``inputs["parameters"][key] = {"value": ..., "source": ...}`` or the affected
calculation fails closed).  Capital-market figures (returns, longevity,
withdrawal rates) are assumptions, labeled as such, never parameters.

All MX amounts are in today's (real) pesos and all US amounts in today's
dollars unless a field says otherwise: salaries, UMA, salario minimo and
bracket thresholds are held at their current real value.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
import math
from typing import Any

from . import mexico
from . import planning
from ._common import number as _common_number
from .us_tax_parameters import US_FEDERAL_PARAMETERS, federal_tax


CHECKED = "2026-09-21"
ANY = "any"

# --- primary sources --------------------------------------------------------
LSS_URL = "https://www.diputados.gob.mx/LeyesBiblio/pdf/LSS.pdf"
LSS_VERSION = "Ley del Seguro Social, texto vigente, ultima reforma DOF 15-01-2026; read 2026-09-21"
DOF_2020_URL = "https://www.diputados.gob.mx/LeyesBiblio/ref/lss/LSS_ref25_16dic20.pdf"
DOF_2020_NOTE = "https://www.dof.gob.mx/nota_detalle.php?codigo=5607729&fecha=16/12/2020"
LSS73_DOF_1990_URL = "https://dof.gob.mx/nota_detalle.php?codigo=4695817&fecha=27/12/1990"
IMSS_EXAMPLES_URL = "http://icpr.itam.mx/Planes2013/Regimen_73_97_Ejemplos.pdf"
CONASAMI_2026_URL = "https://dof.gob.mx/nota_detalle.php?codigo=5775534&fecha=09/12/2025"
CONSAR_FEES_2026_URL = "https://www.gob.mx/consar/articulos/junta-de-gobierno-de-la-consar-autoriza-comisiones-de-las-afore-para-2026-413436"
INEGI_INPC_URL = "https://www.inegi.org.mx/temas/inpc/"
IRS_NOTICE_2025_67_URL = "https://www.irs.gov/pub/irs-drop/n-25-67.pdf"
IRS_RP_2025_19_URL = "https://www.irs.gov/pub/irs-drop/rp-25-19.pdf"
IRS_RP_2025_32_URL = "https://www.irs.gov/pub/irs-drop/rp-25-32.pdf"
IRS_RMD_FAQ_URL = "https://www.irs.gov/retirement-plans/retirement-plan-and-ira-required-minimum-distributions-faqs"
USC_402_URL = "https://www.govinfo.gov/content/pkg/USCODE-2023-title42/html/USCODE-2023-title42-chap7-subchapII-sec402.htm"
USC_416_URL = "https://www.govinfo.gov/content/pkg/USCODE-2023-title42/html/USCODE-2023-title42-chap7-subchapII-sec416.htm"
IRC_401_URL = "https://www.govinfo.gov/content/pkg/USCODE-2023-title26/html/USCODE-2023-title26-subtitleA-chap1-subchapD-partI-subpartA-sec401.htm"
ECFR_RMD_URL = "https://www.ecfr.gov/current/title-26/part-1/section-1.401(a)(9)-9"
IRC_223_URL = "https://www.law.cornell.edu/uscode/text/26/223"


def _lss(article: str, rule: str) -> dict[str, Any]:
    return {"title": f"Ley del Seguro Social (1997), {article}", "url": LSS_URL, "version": LSS_VERSION, "rules": [rule]}


def _entry(value: Any, status: str, source: dict[str, Any], *, checked_on: str | None = None,
           reported_value: Any = None, verify_with: str | None = None) -> dict[str, Any]:
    record: dict[str, Any] = {"value": value, "status": status, "source": source}
    if checked_on:
        record["checked_on"] = checked_on
    if reported_value is not None:
        record["reported_value_not_used"] = reported_value
    if verify_with:
        record["verify_with"] = verify_with
    return record


# Art. 167 LSS 1973 (as reformed DOF 27-12-1990): (upper bound in times the
# salario minimo, cuantia basica %, incremento anual %).  The DOF HTML carries
# the article text but the table only as an image; the rows are transcribed
# from IMSS, Direccion de Prestaciones Economicas y Sociales, "Ejemplos de
# calculos de pension" (pp. 26-27), which reproduces the article.
_ART167 = [
    ("1.00", "80.00", "0.563"), ("1.25", "77.11", "0.814"), ("1.50", "58.18", "1.178"), ("1.75", "49.23", "1.430"),
    ("2.00", "42.67", "1.615"), ("2.25", "37.65", "1.756"), ("2.50", "33.68", "1.868"), ("2.75", "30.48", "1.958"),
    ("3.00", "27.83", "2.033"), ("3.25", "25.60", "2.096"), ("3.50", "23.70", "2.149"), ("3.75", "22.07", "2.195"),
    ("4.00", "20.65", "2.235"), ("4.25", "19.39", "2.271"), ("4.50", "18.29", "2.302"), ("4.75", "17.30", "2.330"),
    ("5.00", "16.41", "2.355"), ("5.25", "15.61", "2.377"), ("5.50", "14.88", "2.398"), ("5.75", "14.22", "2.416"),
    ("6.00", "13.62", "2.433"), (None, "13.00", "2.450"),
]
_IMSS_ART167_SOURCE = {"title": "Ley del Seguro Social 1973, articulo 167 (reforma DOF 27-12-1990), table as reproduced by IMSS "
                                "Direccion de Prestaciones Economicas y Sociales, 'Ejemplos de calculos de pension', pp. 26-27",
                       "url": IMSS_EXAMPLES_URL, "dof_text": LSS73_DOF_1990_URL}

# Art. 168 fr. II a) LSS as reformed DOF 16-12-2020, transitorio Segundo: employer
# cesantia y vejez rate by SBC band and year (DOF 16-12-2020 p. 32 table, read
# from the page image).  Band upper bounds: "sm" = one salario minimo, else UMA multiples.
_CEAV_BANDS = ["1.0 SM", "1.01 SM a 1.50 UMA", "1.51 a 2.00 UMA", "2.01 a 2.50 UMA", "2.51 a 3.00 UMA",
               "3.01 a 3.50 UMA", "3.51 a 4.00 UMA", "4.01 UMA en adelante"]
_CEAV_UPPER_UMA = [None, 1.50, 2.00, 2.50, 3.00, 3.50, 4.00, None]
_CEAV_SCHEDULE = {
    2023: ["3.150", "3.281", "3.575", "3.751", "3.869", "3.953", "4.016", "4.241"],
    2024: ["3.150", "3.413", "4.000", "4.353", "4.588", "4.756", "4.882", "5.331"],
    2025: ["3.150", "3.544", "4.426", "4.954", "5.307", "5.559", "5.747", "6.422"],
    2026: ["3.150", "3.676", "4.851", "5.556", "6.026", "6.361", "6.613", "7.513"],
    2027: ["3.150", "3.807", "5.276", "6.157", "6.745", "7.164", "7.479", "8.603"],
    2028: ["3.150", "3.939", "5.701", "6.759", "7.464", "7.967", "8.345", "9.694"],
    2029: ["3.150", "4.070", "6.126", "7.360", "8.183", "8.770", "9.211", "10.784"],
    2030: ["3.150", "4.202", "6.552", "7.962", "8.902", "9.573", "10.077", "11.875"],
}

# Pension garantizada (Art. 170 LSS; DOF 16-12-2020 transitorio Cuarto, p. 33 table
# read from the page image).  Monthly pesos of December 2020, updated each February by
# INPC.  Rows: salary band (average career SBC in UMA) -> age 60..65+ -> 11 columns.
# Column k means "base weeks for the year + 25k" (last column: "or more").
_PG_BANDS = ["1 SM a 1.99 UMA", "2.0 a 2.99 UMA", "3.0 a 3.99 UMA", "4.0 a 4.99 UMA", "5.0 UMA en adelante"]
_PG_TABLE = {
    "1 SM a 1.99 UMA": {
        60: [2622, 2716, 2809, 2903, 2997, 3090, 3184, 3278, 3371, 3465, 3559],
        61: [2660, 2753, 2847, 2941, 3034, 3128, 3221, 3315, 3409, 3502, 3596],
        62: [2697, 2791, 2884, 2978, 3072, 3165, 3259, 3353, 3446, 3540, 3634],
        63: [2734, 2828, 2922, 3015, 3109, 3203, 3296, 3390, 3484, 3577, 3671],
        64: [2772, 2866, 2959, 3053, 3147, 3240, 3334, 3427, 3521, 3615, 3708],
        65: [2809, 2903, 2997, 3090, 3184, 3278, 3371, 3465, 3559, 3652, 3746]},
    "2.0 a 2.99 UMA": {
        60: [3409, 3530, 3652, 3774, 3896, 4017, 4139, 4261, 4383, 4504, 4626],
        61: [3457, 3579, 3701, 3823, 3944, 4066, 4188, 4310, 4431, 4553, 4675],
        62: [3506, 3628, 3750, 3871, 3993, 4115, 4237, 4358, 4480, 4602, 4724],
        63: [3555, 3677, 3798, 3920, 4042, 4164, 4285, 4407, 4529, 4651, 4772],
        64: [3604, 3725, 3847, 3969, 4091, 4212, 4334, 4456, 4577, 4699, 4821],
        65: [3652, 3774, 3896, 4017, 4139, 4261, 4383, 4504, 4626, 4748, 4870]},
    "3.0 a 3.99 UMA": {
        60: [4195, 4345, 4495, 4645, 4795, 4945, 5094, 5244, 5394, 5544, 5694],
        61: [4255, 4405, 4555, 4705, 4855, 5005, 5154, 5304, 5454, 5604, 5754],
        62: [4315, 4465, 4615, 4765, 4915, 5064, 5214, 5364, 5514, 5664, 5814],
        63: [4375, 4525, 4675, 4825, 4975, 5124, 5274, 5424, 5574, 5724, 5874],
        64: [4435, 4585, 4735, 4885, 5034, 5184, 5334, 5484, 5634, 5784, 5933],
        65: [4495, 4645, 4795, 4945, 5094, 5244, 5394, 5544, 5694, 5844, 5993]},
    "4.0 a 4.99 UMA": {
        60: [4982, 5160, 5338, 5516, 5694, 5872, 6050, 6228, 6405, 6583, 6761],
        61: [5053, 5231, 5409, 5587, 5765, 5943, 6121, 6299, 6477, 6655, 6832],
        62: [5124, 5302, 5480, 5658, 5836, 6014, 6192, 6370, 6548, 6726, 6904],
        63: [5196, 5373, 5551, 5729, 5907, 6085, 6263, 6441, 6619, 6797, 6975],
        64: [5267, 5445, 5623, 5801, 5978, 6156, 6334, 6512, 6690, 6868, 7046],
        65: [5338, 5516, 5694, 5872, 6050, 6228, 6405, 6583, 6761, 6939, 7117]},
    "5.0 UMA en adelante": {
        60: [5769, 5975, 6181, 6387, 6593, 6799, 7005, 7211, 7417, 7623, 7829],
        61: [5851, 6057, 6263, 6469, 6675, 6881, 7087, 7293, 7499, 7705, 7911],
        62: [5933, 6140, 6346, 6552, 6758, 6964, 7170, 7376, 7582, 7788, 7994],
        63: [6016, 6222, 6428, 6634, 6840, 7046, 7252, 7458, 7664, 7870, 8076],
        64: [6098, 6304, 6510, 6716, 6922, 7128, 7334, 7540, 7746, 7953, 8159],
        65: [6181, 6387, 6593, 6799, 7005, 7211, 7417, 7623, 7829, 8035, 8241]},
}

# Treas. Reg. 1.401(a)(9)-9(c), Table 2 (Uniform Lifetime Table), eCFR as of 2026-01-01.
_UNIFORM_LIFETIME = {
    72: 27.4, 73: 26.5, 74: 25.5, 75: 24.6, 76: 23.7, 77: 22.9, 78: 22.0, 79: 21.1, 80: 20.2, 81: 19.4, 82: 18.5,
    83: 17.7, 84: 16.8, 85: 16.0, 86: 15.2, 87: 14.4, 88: 13.7, 89: 12.9, 90: 12.2, 91: 11.5, 92: 10.8, 93: 10.1,
    94: 9.5, 95: 8.9, 96: 8.4, 97: 7.8, 98: 7.3, 99: 6.8, 100: 6.4, 101: 6.0, 102: 5.6, 103: 5.2, 104: 4.9, 105: 4.6,
    106: 4.3, 107: 4.1, 108: 3.9, 109: 3.7, 110: 3.5, 111: 3.4, 112: 3.3, 113: 3.1, 114: 3.0, 115: 2.9, 116: 2.8,
    117: 2.7, 118: 2.5, 119: 2.3, 120: 2.0,
}

_NOTICE_2025_67 = {"title": "IRS Notice 2025-67 (2026 retirement plan limitations); IRS news release IR-2025-111", "url": IRS_NOTICE_2025_67_URL}
_RP_2025_19 = {"title": "IRS Rev. Proc. 2025-19, section 2 (2026 HSA limits)", "url": IRS_RP_2025_19_URL}


PARAMETERS: dict[str, dict[Any, dict[str, Any]]] = {
    # --- Mexico: IMSS Ley 73 -------------------------------------------------
    "ley73_art167_table": {ANY: _entry(_ART167, "verified", _IMSS_ART167_SOURCE, checked_on=CHECKED)},
    "ley73_cesantia_percent_by_age": {ANY: _entry({60: "75", 61: "80", 62: "85", 63: "90", 64: "95", 65: "100"}, "verified",
        {**_IMSS_ART167_SOURCE, "title": "Ley del Seguro Social 1973, articulo 171 (cesantia en edad avanzada), as reproduced by IMSS; "
                                         "a year is added when age exceeds the completed years by six months"}, checked_on=CHECKED)},
    "ley73_family_assignments": {ANY: _entry({"spouse": "0.15", "child_under_16": "0.10", "dependent_parent": "0.10",
                                              "no_dependants_assistance": "0.15", "single_parent_assistance": "0.10"}, "verified",
        {**_IMSS_ART167_SOURCE, "title": "Ley del Seguro Social 1973, articulo 164 (asignaciones familiares y ayuda asistencial) and 169 (cap: 100% of the average salary), as reproduced by IMSS"},
        checked_on=CHECKED)},
    "ley73_decree_factor": {ANY: _entry("1.11", "verified", _lss(
        "articulo Decimo Cuarto transitorio del Decreto DOF 20-12-2001, reformado DOF 05-01-2004",
        "b) pensioners aged 60 or more with a pension equal to or above one salario minimo: pension x 1.11; a) pensions below one salario minimo rise to it"),
        checked_on=CHECKED)},
    "ley73_minimum_pension_sm_multiple": {ANY: _entry("1", "verified", _lss(
        "articulo Decimo Cuarto transitorio a) del Decreto DOF 20-12-2001 (reformado DOF 05-01-2004); LSS 1973 art. 168 as reproduced by IMSS",
        "pensions below one salario minimo general are raised to one salario minimo"), checked_on=CHECKED)},
    "salario_minimo_general_daily_mxn": {
        2026: _entry("315.04", "verified", {"title": "Resolucion del H. Consejo de Representantes de la CONASAMI (DOF 09-12-2025), Zona del Salario Minimo General, vigente desde 01-01-2026",
                                            "url": CONASAMI_2026_URL}, checked_on=CHECKED),
        2025: _entry(None, "needs_verification", {"title": "Resolucion CONASAMI para 2025 (DOF diciembre 2024)", "url": "https://www.dof.gob.mx/"},
                     reported_value="278.80", verify_with="the CONASAMI resolution published in the DOF in December 2024 (ZSMG daily minimum)"),
    },
    "sbc_cap_uma_multiple": {ANY: _entry("25", "verified", _lss(
        "articulo 28", "SBC upper limit of 25 times the salario minimo, read as 25 UMA under the 2016 desindexation decree for this purpose"),
        checked_on=CHECKED)},
    "mod40_invalidez_vida_rate": {ANY: _entry("0.02375", "verified", _lss(
        "articulos 147 y 218 inciso b)", "invalidez y vida: employer 1.75% + worker 0.625%, both paid by the voluntary contributor"), checked_on=CHECKED)},
    "mod40_art25_rate": {ANY: _entry("0.01425", "verified", _lss(
        "articulos 25 segundo parrafo y 218 ultimo parrafo", "pensioners' health-in-kind quota: employer 1.05% + worker 0.375% paid by the voluntary contributor (the State's 0.075% is not)"),
        checked_on=CHECKED)},
    # --- Mexico: Ley 97 / AFORE ---------------------------------------------
    "retiro_employer_rate": {ANY: _entry("0.02", "verified", _lss("articulo 168 fraccion I", "retiro: employer 2% of SBC"), checked_on=CHECKED)},
    "ceav_worker_rate": {ANY: _entry("0.01125", "verified", _lss("articulo 168 fraccion II inciso b) (DOF 16-12-2020)", "worker 1.125% of SBC"), checked_on=CHECKED)},
    "ceav_employer_schedule": {ANY: _entry({"bands": _CEAV_BANDS, "upper_uma": _CEAV_UPPER_UMA, "by_year_percent": _CEAV_SCHEDULE}, "verified",
        {"title": "Decreto DOF 16-12-2020 que reforma la LSS, articulo Segundo transitorio (tabla 2023-2030) y articulo 168 fr. II a)",
         "url": DOF_2020_URL, "dof": DOF_2020_NOTE}, checked_on=CHECKED)},
    "ley97_weeks_required": {ANY: _entry({"start_year": 2021, "start_weeks": 750, "annual_increase": 25, "final_weeks": 1000, "final_year": 2031}, "verified",
        {"title": "Decreto DOF 16-12-2020, articulo Cuarto transitorio; LSS articulos 154 y 162 (1,000 semanas)", "url": DOF_2020_URL}, checked_on=CHECKED)},
    "pension_garantizada_table_dec2020_mxn": {ANY: _entry({"bands": _PG_BANDS, "table": _PG_TABLE}, "verified",
        {"title": "Decreto DOF 16-12-2020, articulo Cuarto transitorio (tabla de pension garantizada, p. 33); LSS articulo 170 (INPC update each February)",
         "url": DOF_2020_URL}, checked_on=CHECKED)},
    "pension_garantizada_inpc_factor": {ANY: _entry(None, "needs_verification", {"title": "INEGI INPC: index at the latest February update / index of December 2020", "url": INEGI_INPC_URL},
        verify_with="INEGI INPC (LSS art. 170 updates the table each February); supply the ratio as a number")},
    "cuota_social_daily_mxn": {ANY: _entry(None, "needs_verification", {"title": "LSS articulo 168 fraccion IV table, updated quarterly by INPC (IMSS/CONSAR publications)", "url": LSS_URL},
        verify_with="the current quarterly cuota social amount for the worker's SBC band (applies only up to 4 UMA)")},
    "afore_fee_max": {2026: _entry("0.0054", "verified", {"title": "CONSAR, Junta de Gobierno autoriza comisiones de las Afore para 2026 (21-11-2025): 0.54% maximum; PENSIONISSSTE 0.52%; system average 0.538%",
                                                          "url": CONSAR_FEES_2026_URL}, checked_on=CHECKED)},
    "uma_daily_mxn": mexico.PARAMETERS["uma_daily_mxn"],
    "uma_annual_mxn": mexico.PARAMETERS["uma_annual_mxn"],
    # --- United States -------------------------------------------------------
    "us_402g_deferral_limit": {2026: _entry("24500", "verified", {**_NOTICE_2025_67, "section": "402(g)(1)"}, checked_on=CHECKED),
                               2025: _entry(None, "needs_verification", {"title": "IRS Notice 2024-80", "url": "https://www.irs.gov/"}, reported_value="23500", verify_with="IRS Notice 2024-80")},
    "us_catch_up_50": {2026: _entry("8000", "verified", {**_NOTICE_2025_67, "section": "414(v)(2)(B)(i)"}, checked_on=CHECKED)},
    "us_catch_up_60_63": {2026: _entry("11250", "verified", {**_NOTICE_2025_67, "section": "414(v)(2)(E)(i): ages 60, 61, 62 or 63 during the year"}, checked_on=CHECKED)},
    "us_roth_catch_up_wage_threshold": {2026: _entry("150000", "verified", {**_NOTICE_2025_67, "section": "414(v)(7)(A): prior-year FICA wages from the employer sponsoring the plan"}, checked_on=CHECKED)},
    "us_415c_limit": {2026: _entry("72000", "verified", {**_NOTICE_2025_67, "section": "415(c)(1)(A)"}, checked_on=CHECKED)},
    "us_ira_limit": {2026: _entry("7500", "verified", {**_NOTICE_2025_67, "section": "219(b)(5)(A)"}, checked_on=CHECKED)},
    "us_ira_catch_up": {2026: _entry("1100", "verified", {**_NOTICE_2025_67, "section": "219(b)(5)(C)"}, checked_on=CHECKED)},
    "us_hsa_self_limit": {2026: _entry("4400", "verified", _RP_2025_19, checked_on=CHECKED)},
    "us_hsa_family_limit": {2026: _entry("8750", "verified", _RP_2025_19, checked_on=CHECKED)},
    "us_hsa_catch_up_55": {ANY: _entry("1000", "statutory", {"title": "IRC 223(b)(3)(B) (not indexed)", "url": IRC_223_URL})},
    "us_standard_deduction": {2026: _entry({"single": "16100", "married_filing_separately": "16100", "married_filing_jointly": "32200",
                                            "qualifying_surviving_spouse": "32200", "head_of_household": "24150"}, "verified",
                                           {"title": "IRS Rev. Proc. 2025-32, section 4.14 (2026 standard deduction; age 65+ additional amounts not included)", "url": IRS_RP_2025_32_URL},
                                           checked_on=CHECKED)},
    "ss_early_reduction": {ANY: _entry({"worker_first_36_per_month": "5/9", "spouse_first_36_per_month": "25/36", "additional_per_month": "5/12"}, "verified",
        {"title": "42 U.S.C. 402(q)(1)(A) and (q)(9)(A): percent per month of reduction", "url": USC_402_URL}, checked_on=CHECKED)},
    "ss_delayed_credit": {ANY: _entry({"per_month_percent": "2/3", "max_age": 70, "applies_to": "old-age benefit only"}, "verified",
        {"title": "42 U.S.C. 402(w)(1), (w)(2)(A) and (w)(6)(D): first eligible after 2004; increment months end at age 70", "url": USC_402_URL}, checked_on=CHECKED)},
    "ss_full_retirement_age": {
        "1960+": _entry({"years": 67, "months": 0}, "verified", {"title": "42 U.S.C. 416(l)(1)(E): attains age 62 after December 31, 2021", "url": USC_416_URL}, checked_on=CHECKED),
        "1943-1959": _entry({1943: 0, 1954: 0, 1955: 2, 1956: 4, 1957: 6, 1958: 8, 1959: 10}, "statutory",
                            {"title": "42 U.S.C. 416(l)(1)(B)-(D): 66, plus 2 months per year of birth 1955-1959", "url": USC_416_URL}),
    },
    "rmd_applicable_age": {
        "1951-1958": _entry(73, "verified", {"title": "IRC 401(a)(9)(C)(v)(I) (SECURE 2.0 sec. 107)", "url": IRC_401_URL}, checked_on=CHECKED),
        "1959": _entry(None, "needs_verification", {"title": "IRC 401(a)(9)(C)(v)(I) and (II) both describe people born in 1959", "url": IRC_401_URL},
                       reported_value=73, verify_with="final Treasury regulations resolving the 1959 overlap between 401(a)(9)(C)(v)(I) and (II)"),
        "1960+": _entry(75, "verified", {"title": "IRC 401(a)(9)(C)(v)(II): attains age 74 after December 31, 2032", "url": IRC_401_URL}, checked_on=CHECKED),
    },
    "rmd_uniform_lifetime_table": {ANY: _entry(_UNIFORM_LIFETIME, "verified", {"title": "Treas. Reg. 1.401(a)(9)-9(c), Table 2 (eCFR as of 2026-01-01)", "url": ECFR_RMD_URL}, checked_on=CHECKED)},
}


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _num(value: Any, path: str, *, minimum: float | None = None, maximum: float | None = None) -> float:
    out = _common_number(value, path, minimum=minimum)
    if maximum is not None and out > maximum:
        raise ValueError(f"{path} must be at most {maximum}")
    return out


def _int(value: Any, path: str, *, minimum: int | None = None, maximum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{path} must be an integer")
    if (minimum is not None and value < minimum) or (maximum is not None and value > maximum):
        raise ValueError(f"{path} must be between {minimum} and {maximum}")
    return value


def _range(value: Any, path: str, *, minimum: float | None = None, maximum: float | None = None) -> list[float]:
    """A number or a nonempty list of numbers, returned sorted and unique."""
    items = value if isinstance(value, list) else [value]
    if not items:
        raise ValueError(f"{path} must be a number or a nonempty list")
    return sorted({_num(v, f"{path}[{i}]", minimum=minimum, maximum=maximum) for i, v in enumerate(items)})


def _r(value: float) -> float:
    return round(float(value), 2) + 0.0  # + 0.0 after rounding: round(-0.001, 2) is -0.0


def _no_negative_zero(value: Any) -> Any:
    """Every -0.0 in an output becomes 0.0 (a tiny negative float rounds to -0.0 and reads as a sign)."""
    if isinstance(value, float):
        return value + 0.0
    if isinstance(value, dict):
        return {k: _no_negative_zero(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_no_negative_zero(v) for v in value]
    return value


def _envelope(status: str, result: dict[str, Any], *, missing: list[str], warnings: list[str],
              sources: list[Any], assumptions: list[str]) -> dict[str, Any]:
    unique: list[Any] = []
    for source in sources:
        if source not in unique:
            unique.append(source)
    dedupe = lambda items: list(dict.fromkeys(items))  # noqa: E731
    return {"status": status, "result": _no_negative_zero(result), "missing": dedupe(missing), "warnings": dedupe(warnings),
            "sources": unique, "assumptions": dedupe(assumptions)}


def _as_of(inputs: dict[str, Any]) -> date:
    raw = inputs.get("as_of")
    if raw is None:
        return date.today()
    if not isinstance(raw, str):
        raise ValueError("as_of must be an ISO date")
    try:
        return date.fromisoformat(raw)
    except ValueError:
        raise ValueError("as_of must be an ISO date") from None


class _Params:
    """Resolve dated parameters; record what was used, and what is missing (fail closed)."""

    def __init__(self, inputs: dict[str, Any]):
        raw = inputs.get("parameters", {})
        if not isinstance(raw, dict):
            raise ValueError("parameters must be an object")
        self.overrides = raw
        self.used: list[dict[str, Any]] = []
        self.missing: list[str] = []
        self.needs_verification: list[dict[str, Any]] = []
        self.assumptions: list[str] = []
        self.warnings: list[str] = []
        self.sources: list[dict[str, Any]] = []
        self._seen: set[tuple[str, Any]] = set()

    def get(self, key: str, period: Any = ANY) -> Any:
        table = PARAMETERS.get(key, {})
        entry = table.get(period) or table.get(ANY)
        marker = (key, period)
        first = marker not in self._seen
        self._seen.add(marker)
        if key in self.overrides:
            override = self.overrides[key]
            if not isinstance(override, dict) or "value" not in override:
                raise ValueError(f"parameters.{key} must be an object with value and source")
            source = override.get("source")
            if not isinstance(source, str) or not source.strip():
                raise ValueError(f"parameters.{key}.source must be nonempty text")
            value = override["value"]
            if entry is None or not isinstance(entry.get("value"), (dict, list)):
                _num(value, f"parameters.{key}.value", minimum=0)
            if first:
                if entry and entry["status"] in {"verified", "statutory"} and str(entry["value"]) != str(value):
                    self.warnings.append(f"Supplied parameters.{key} differs from the {entry['status']} table value; the supplied value was used.")
                self.assumptions.append(f"Used caller-supplied {key} ({period}) from: {source.strip()}.")
                self.used.append({"key": key, "period": period, "value": value, "status": "caller_supplied", "source": source.strip()})
            return value
        if entry is None or entry["status"] == "needs_verification":
            verify = (entry or {}).get("verify_with") or "the official publication for that period"
            if first:
                self.missing.append(f"parameters.{key} ({period}; verify with {verify})")
                self.needs_verification.append({"key": key, "period": period, "verify_with": verify, "source": (entry or {}).get("source"),
                                                "reported_value_not_used": (entry or {}).get("reported_value_not_used")})
            return None
        if first:
            self.used.append({"key": key, "period": period, "status": entry["status"], "checked_on": entry.get("checked_on"), "source": entry["source"]})
            self.sources.append(entry["source"])
        return entry["value"]

    def number(self, key: str, period: Any = ANY) -> float | None:
        value = self.get(key, period)
        return None if value is None else float(Decimal(str(value)))

    def report(self) -> dict[str, Any]:
        return {"parameters_used": self.used, "parameters_needing_verification": self.needs_verification}


def parameter_inventory() -> list[dict[str, Any]]:
    """Every parameter with its verification status (for reports and tests)."""
    rows = []
    for key, periods in PARAMETERS.items():
        for period, entry in periods.items():
            rows.append({"key": key, "period": period, "status": entry["status"], "checked_on": entry.get("checked_on"),
                         "source": entry["source"].get("title"), "verify_with": entry.get("verify_with")})
    return rows


def _annuity_factor(years: float, rate: float) -> float:
    """Present value of 1 per year paid at the start of each year for ``years`` years."""
    whole = max(int(math.floor(years)), 0)
    fraction = max(years - whole, 0.0)
    if rate == 0:
        return years
    v = 1 / (1 + rate)
    factor = (1 - v ** whole) / (1 - v) if whole else 0.0
    return factor + fraction * v ** whole


def _irr(flows: list[float]) -> float | None:
    """Annual IRR by bisection; ``None`` when the flows never change sign."""
    if not any(f < 0 for f in flows) or not any(f > 0 for f in flows):
        return None

    def npv(rate: float) -> float:
        return sum(f / (1 + rate) ** t for t, f in enumerate(flows))

    low, high = -0.99, 1.0
    f_low, f_high = npv(low), npv(high)
    while f_low * f_high > 0 and high < 1e6:
        high *= 2
        f_high = npv(high)
    if f_low * f_high > 0:
        return None
    for _ in range(200):
        mid = (low + high) / 2
        f_mid = npv(mid)
        if f_low * f_mid <= 0:
            high, f_high = mid, f_mid
        else:
            low, f_low = mid, f_mid
    return (low + high) / 2


def _birth_year(inputs: dict[str, Any], context: dict[str, Any] | None, assumptions: list[str]) -> int | None:
    if inputs.get("birth_year") is not None:
        return _int(inputs["birth_year"], "birth_year", minimum=1900, maximum=2100)
    profile = (context or {}).get("client.profile") if context is not None else None
    if isinstance(profile, dict) and isinstance(profile.get("birth_year"), int):
        assumptions.append("birth_year came from the saved profile (client.profile).")
        return profile["birth_year"]
    return None


# ---------------------------------------------------------------------------
# Mexico: IMSS regime and Ley 73
# ---------------------------------------------------------------------------

LEY73_CUTOFF = date(1997, 7, 1)


def imss_regime(first_cotizacion_date: str) -> dict[str, Any]:
    """Ley 73 if the first IMSS cotizacion was before 1 July 1997, else Ley 97."""
    try:
        first = date.fromisoformat(first_cotizacion_date)
    except (TypeError, ValueError):
        raise ValueError("first_cotizacion_date must be an ISO date") from None
    ley73 = first < LEY73_CUTOFF
    return {"regime": "ley73" if ley73 else "ley97", "first_cotizacion_date": first.isoformat(),
            "rule": "Workers registered before 1997-07-01 may choose, when they retire, the 1973-law pension or the "
                    "1997-law account pension (LSS 1997 transitorio Tercero); later registrants are Ley 97 only.",
            "source": _lss("articulos transitorios Primero y Tercero (vigencia 01-07-1997)", "choice of regime for those insured before the Ley 97 took effect")}


def average_salary_last_250_weeks(history: list[dict[str, Any]]) -> float:
    """Average daily salary over the most recent 250 weeks.

    ``history`` rows are ``{weeks, daily_salary_mxn}`` ordered most recent first.
    With fewer than 250 weeks the weeks available are averaged (Art. 167).
    """
    if not isinstance(history, list) or not history:
        raise ValueError("salary_history must be a nonempty list")
    remaining, total, counted = 250.0, 0.0, 0.0
    for i, row in enumerate(history):
        if not isinstance(row, dict):
            raise ValueError(f"salary_history[{i}] must be an object")
        weeks = _num(row.get("weeks"), f"salary_history[{i}].weeks", minimum=0)
        salary = _num(row.get("daily_salary_mxn"), f"salary_history[{i}].daily_salary_mxn", minimum=0)
        take = min(weeks, remaining)
        total += take * salary
        counted += take
        remaining -= take
        if remaining <= 0:
            break
    if counted == 0:
        raise ValueError("salary_history has no weeks")
    return total / counted


def _art167_row(multiple: float, table: list) -> tuple[str, float, float]:
    rounded = round(multiple + 1e-9, 2)
    lower = "0"
    for upper, basic, increment in table:
        if upper is None or rounded <= float(upper):
            label = f"hasta {upper}" if lower == "0" else (f"{lower} a {upper}" if upper else f"{lower} y hasta el limite superior")
            return label, float(basic) / 100, float(increment) / 100
        lower = format(float(upper) + 0.01, ".2f")
    raise ValueError("art. 167 table must end with an open row")


def ley73_increments(weeks: float) -> float:
    """Annual increments after the first 500 weeks: one per 52 weeks; 13-26 leftover weeks add 0.5, more than 26 add 1."""
    excess = weeks - 500
    if excess < 0:
        return 0.0
    whole = int(excess // 52)
    leftover = excess - whole * 52
    return whole + (1.0 if leftover > 26 else 0.5 if leftover >= 13 else 0.0)


def _effective_age(age: float) -> int:
    whole = int(math.floor(age))
    return whole + (1 if age - whole >= 0.5 else 0)


def _assignments(dependants: dict[str, Any] | None, table: dict[str, str]) -> tuple[float, str]:
    if dependants is None:
        return float(table["no_dependants_assistance"]), (
            "ASSUMED: dependants not supplied, so the 15% ayuda asistencial of Art. 164 (no spouse, children or dependent "
            "parents) is used; a spouse or dependent parents would raise it, a single child under 16 would lower it to 10%")
    if not isinstance(dependants, dict):
        raise ValueError("dependants must be an object")
    spouse = dependants.get("spouse", False)
    if not isinstance(spouse, bool):
        raise ValueError("dependants.spouse must be a boolean")
    children = _int(dependants.get("children_under_16", 0), "dependants.children_under_16", minimum=0, maximum=30)
    parents = _int(dependants.get("dependent_parents", 0), "dependants.dependent_parents", minimum=0, maximum=2)
    t = {k: float(v) for k, v in table.items()}
    if spouse or children:
        return (t["spouse"] if spouse else 0.0) + children * t["child_under_16"], "Art. 164 fr. I-II: spouse 15%, 10% per child under 16"
    if parents == 1:
        return t["dependent_parent"] + t["single_parent_assistance"], "Art. 164 fr. III and V: one dependent parent 10% plus 10% assistance"
    if parents == 2:
        return 2 * t["dependent_parent"], "Art. 164 fr. III: 10% per dependent parent"
    return t["no_dependants_assistance"], "Art. 164 fr. IV: no dependants, 15% ayuda asistencial"


def ley73_pension(average_daily_salary: float, weeks: float, age: float, minimum_wage_daily: float, *,
                  dependants: dict[str, Any] | None, params: _Params) -> dict[str, Any] | None:
    """Monthly Ley 73 pension (cesantia 60-64 or vejez 65+) in the units of the inputs.

    Statutory order (LSS 1973, as reproduced by IMSS):

    1. Art. 167: the vejez cuantia, basic + increments on the average salary, /12.
    2. Art. 164: family assignments or ayuda asistencial on that vejez pension.
    3. Art. 169: the vejez pension with assignments is capped at 100% of the
       average salary, unless the own-right cuantia alone already exceeds it.
    4. Art. 171: cesantia en edad avanzada (60-64) pays the age percentage
       (75% at 60 ... 95% at 64) of the vejez pension that would apply, i.e. of
       the result of steps 1-3; vejez (65+) pays 100%.
    5. Decree DOF 20-12-2001 (reformed 05-01-2004), art. Decimo Cuarto
       transitorio: x 1.11 (age 60+), and the floor of one salario minimo (x 1.11).

    At 65 this reproduces IMSS's worked vejez example; before 65 the cap now
    binds on the vejez pension, not on the reduced cesantia amount.
    """
    table = params.get("ley73_art167_table")
    cesantia = params.get("ley73_cesantia_percent_by_age")
    family = params.get("ley73_family_assignments")
    factor_raw = params.number("ley73_decree_factor")
    minimum_multiple = params.number("ley73_minimum_pension_sm_multiple")
    if None in (table, cesantia, family, factor_raw, minimum_multiple):
        return None
    effective = _effective_age(age)
    if weeks < 500 or effective < 60:
        return {"eligible": False, "reason": "Ley 73 needs 500 recognized weeks and age 60 (cesantia) or 65 (vejez).",
                "weeks": weeks, "age_for_table": effective}
    multiple = average_daily_salary / minimum_wage_daily
    group, basic, increment = _art167_row(multiple, table)
    increments = ley73_increments(weeks)
    annual = average_daily_salary * 365 * (basic + increment * increments)
    vejez_monthly = annual / 12
    percent = float(cesantia[min(effective, 65)] if min(effective, 65) in cesantia else cesantia[str(min(effective, 65))]) / 100
    cuantia = vejez_monthly * percent
    factor = factor_raw if effective >= 60 else 1.0
    cuantia_f = cuantia * factor
    share, basis = _assignments(dependants, family)
    # Arts. 164 and 169: assignments on the vejez pension, capped at 100% of the average salary unless the
    # own-right cuantia is already above it.
    salary_monthly = average_daily_salary * 365 / 12
    vejez_with_family = min(vejez_monthly * (1 + share), max(salary_monthly, vejez_monthly))
    # Art. 171: the cesantia percentage applies to that vejez pension; then the 1.11 decree factor.
    total = vejez_with_family * percent * factor
    cap = salary_monthly * factor  # the Art. 169 cap, shown with the decree factor like the pension
    minimum = minimum_wage_daily * minimum_multiple * 365 / 12 * factor
    floor_applied = total < minimum
    total = max(total, minimum)
    return {
        "eligible": True, "kind": "vejez" if effective >= 65 else "cesantia en edad avanzada", "age_for_table": effective,
        "average_daily_salary_mxn": _r(average_daily_salary), "salary_in_minimum_wages": round(multiple, 2),
        "art167_group": group, "cuantia_basica_percent": round(basic * 100, 3), "incremento_anual_percent": round(increment * 100, 3),
        "weeks": weeks, "increments": increments,
        "annual_cuantia_at_65_mxn": _r(annual), "cesantia_percent": round(percent * 100, 2),
        "monthly_cuantia_mxn": _r(cuantia), "decree_factor": factor, "monthly_cuantia_with_factor_mxn": _r(cuantia_f),
        "family_assignments_share": share, "family_assignments_basis": basis,
        "family_assignments_assumed": dependants is None,
        "cap_monthly_mxn": _r(cap), "minimum_monthly_mxn": _r(minimum), "minimum_applied": floor_applied,
        "monthly_pension_mxn": _r(total), "aguinaldo_annual_mxn": _r(total),
        "annual_income_mxn": _r(total * 13),
        "note": "Plus an annual aguinaldo of one monthly payment (Art. 167 last paragraph); annual_income includes it.",
    }


def _mod40_rate(year: int, params: _Params) -> tuple[float | None, dict[str, float]]:
    retiro = params.number("retiro_employer_rate")
    worker = params.number("ceav_worker_rate")
    iv = params.number("mod40_invalidez_vida_rate")
    art25 = params.number("mod40_art25_rate")
    schedule = params.get("ceav_employer_schedule")
    if None in (retiro, worker, iv, art25, schedule):
        return None, {}
    employer = _ceav_employer_rate(schedule, year, band_index=len(_CEAV_BANDS) - 1)
    parts = {"retiro": retiro, "ceav_employer": employer, "ceav_worker": worker, "invalidez_vida": iv, "art25_health": art25}
    return sum(parts.values()), parts


def _ceav_employer_rate(schedule: dict[str, Any], year: int, *, band_index: int) -> float:
    by_year = {int(k): v for k, v in schedule["by_year_percent"].items()}
    first, last = min(by_year), max(by_year)
    if year < first:
        return 0.0315
    return float(by_year[min(year, last)][band_index]) / 100


def _ceav_band(sbc: float, minimum_wage: float, uma: float, schedule: dict[str, Any]) -> int:
    if sbc <= minimum_wage + 1e-9:
        return 0
    multiple = round(sbc / uma + 1e-9, 2)
    for index, upper in enumerate(schedule["upper_uma"]):
        if index == 0:
            continue
        if upper is None or multiple <= upper:
            return index
    return len(schedule["upper_uma"]) - 1


def modalidad40(inputs: dict[str, Any], *, base: dict[str, Any], params: _Params, as_of: date,
                minimum_wage: float, uma: float | None, warnings: list[str], missing: list[str],
                assumptions: list[str]) -> dict[str, Any] | None:
    """Cost of Modalidad 40 at a chosen salary for N years against the pension increase."""
    data = inputs
    if not isinstance(data, dict):
        raise ValueError("ley73.modalidad40 must be an object")
    years = _int(data.get("years"), "ley73.modalidad40.years", minimum=1, maximum=20)
    if "daily_salary_mxn" in data:
        salary = _num(data["daily_salary_mxn"], "ley73.modalidad40.daily_salary_mxn", minimum=0)
    elif "salary_uma_multiple" in data:
        if uma is None:
            missing.append("parameters.uma_daily_mxn (to convert salary_uma_multiple)")
            return None
        salary = uma * _num(data["salary_uma_multiple"], "ley73.modalidad40.salary_uma_multiple", minimum=1)
    else:
        missing.append("ley73.modalidad40.daily_salary_mxn or salary_uma_multiple")
        return None
    cap_multiple = params.number("sbc_cap_uma_multiple")
    if uma is not None and cap_multiple is not None and salary > uma * cap_multiple + 0.005:
        warnings.append(f"Modalidad 40 salary capped at {cap_multiple:g} UMA ({_r(uma * cap_multiple)} MXN/day).")
        salary = uma * cap_multiple
    elif uma is None:
        missing.append("parameters.uma_daily_mxn (to check the 25-UMA Modalidad 40 ceiling)")
    if salary < base["prior_salary"]:
        warnings.append("Art. 218 requires registering with the last salary or higher; the chosen salary is below the current average.")
    start_year = _int(data.get("start_year", as_of.year), "ley73.modalidad40.start_year", minimum=2000, maximum=2100)
    pension_age = base["pension_age"]
    if start_year < as_of.year:
        raise ValueError("ley73.modalidad40.start_year cannot be in the past")
    start_age = base["age_now"] + (start_year - as_of.year)
    if start_age + years > pension_age + 1e-9:
        raise ValueError("ley73.modalidad40.years must end by the pension age")
    rates = []
    for offset in range(years):
        rate, parts = _mod40_rate(start_year + offset, params)
        if rate is None:
            return None
        rates.append({"year": start_year + offset, "rate": round(rate, 6), "components": {k: round(v, 6) for k, v in parts.items()}})
    mod_weeks = 52 * years
    counted = min(mod_weeks, 250)
    new_avg = (counted * salary + (250 - counted) * base["prior_salary"]) / 250
    with_mod = ley73_pension(new_avg, base["weeks"] + mod_weeks, pension_age, minimum_wage,
                             dependants=base["dependants"], params=params)
    without = base["pension"]
    if with_mod is None or without is None or not with_mod.get("eligible"):
        return {"eligible": False, "reason": "No Ley 73 pension is computable at the pension age with or without Modalidad 40."}
    increase_monthly = with_mod["monthly_pension_mxn"] - (without["monthly_pension_mxn"] if without.get("eligible") else 0.0)
    annual_costs = [salary * 365 * r["rate"] for r in rates]
    total_cost = sum(annual_costs)
    annual_gain = increase_monthly * 13
    payback = total_cost / annual_gain if annual_gain > 0 else None
    longevity = _range(data.get("longevity_ages", base["longevity_ages"]), "ley73.modalidad40.longevity_ages", minimum=pension_age)
    scenarios = []
    gap_years = int(round(pension_age - (start_age + years)))
    for death_age in longevity:
        flows = [-c for c in annual_costs] + [0.0] * max(gap_years, 0)
        paid_years = death_age - pension_age
        whole = int(math.floor(paid_years))
        flows += [annual_gain] * whole
        if paid_years - whole > 0:
            flows.append(annual_gain * (paid_years - whole))
        irr = _irr(flows)
        scenarios.append({"death_age": death_age, "total_increase_received_mxn": _r(annual_gain * paid_years),
                          "net_gain_mxn": _r(annual_gain * paid_years - total_cost),
                          "real_irr_percent": None if irr is None else round(irr * 100, 2)})
    assumptions.append("Modalidad 40: amounts in today's pesos; the chosen salary keeps its real value; the person is not otherwise "
                       "contributing during those years; 52 weeks per year; pension increase paid 13 times a year (with aguinaldo).")
    warnings.append("Modalidad 40 eligibility: at least 52 weeks in the last five years and a written request within five years "
                    "of the baja (LSS arts. 218-219). Whether salary above 10 salarios minimos counts toward the Ley 73 average "
                    "has been litigated; confirm the cap IMSS applies before paying.")
    return {
        "daily_salary_mxn": _r(salary), "years": years, "weeks_added": mod_weeks,
        "contribution_rates_by_year": rates,
        "monthly_cost_first_year_mxn": _r(annual_costs[0] / 12), "total_cost_mxn": _r(total_cost),
        "new_average_daily_salary_mxn": _r(new_avg),
        "pension_without_mxn": without.get("monthly_pension_mxn") if without.get("eligible") else 0.0,
        "pension_with_mxn": with_mod["monthly_pension_mxn"], "monthly_increase_mxn": _r(increase_monthly),
        "annual_increase_mxn": _r(annual_gain),
        "payback_years": None if payback is None else round(payback, 2),
        "break_even_age": None if payback is None else round(pension_age + payback, 2),
        "by_longevity": scenarios,
        "pension_detail_with_modalidad40": with_mod,
    }


# ---------------------------------------------------------------------------
# Mexico: Ley 97
# ---------------------------------------------------------------------------

def ley97_weeks_required(year: int, params: _Params) -> int | None:
    rule = params.get("ley97_weeks_required")
    if rule is None:
        return None
    if year <= rule["start_year"]:
        return rule["start_weeks"]
    return min(rule["start_weeks"] + rule["annual_increase"] * (year - rule["start_year"]), rule["final_weeks"])


def pension_garantizada(year: int, age: float, weeks: float, average_career_sbc_uma: float,
                        params: _Params, warnings: list[str]) -> dict[str, Any] | None:
    """Pension garantizada (monthly, December-2020 pesos) and its current value if the INPC factor is known."""
    table = params.get("pension_garantizada_table_dec2020_mxn")
    required = ley97_weeks_required(year, params)
    if table is None or required is None:
        return None
    effective = _effective_age(age)
    if effective < 60 or weeks < required:
        return {"eligible": False, "weeks_required": required,
                "reason": f"Needs age 60 and {required} weeks in {year}; the person would have {weeks:g} weeks at age {effective}."}
    if year > 2030:
        warnings.append("The pension garantizada table lists 2021-2030; for later years its columns are read from 1,000 weeks "
                        "(the requirement reached in 2031) in steps of 25.")
    base_weeks = required
    column = min(10, int((weeks - base_weeks) // 25))
    bands = table["bands"]
    band = bands[0] if average_career_sbc_uma < 2 else bands[1] if average_career_sbc_uma < 3 else bands[2] \
        if average_career_sbc_uma < 4 else bands[3] if average_career_sbc_uma < 5 else bands[4]
    rows = table["table"][band]
    row = rows.get(min(effective, 65)) or rows.get(str(min(effective, 65)))
    dec2020 = float(row[column])
    factor = params.number("pension_garantizada_inpc_factor")
    warnings.append("The pension garantizada amount is an estimate pending the official IMSS table: which weeks column "
                    "applies (counted from the weeks required in the pension year) could not be confirmed against the "
                    "DOF 16-12-2020 table header. Confirm the amount with IMSS.")
    return {"eligible": True, "estimate": True, "weeks_required": required, "salary_band": band, "age_row": min(effective, 65),
            "weeks_column": f"{base_weeks + 25 * column}{' o mas' if column == 10 else ''}",
            "monthly_dec2020_mxn": dec2020, "inpc_factor": factor,
            "monthly_mxn": None if factor is None else _r(dec2020 * factor)}


def _afore_fee(params: _Params, year: int, warnings: list[str]) -> tuple[float | None, int | None]:
    """The CONSAR maximum AFORE fee for ``year``, or the latest known year's cap with a warning; ``(fee, year used)``."""
    table = PARAMETERS["afore_fee_max"]
    known = sorted(y for y, e in table.items() if isinstance(y, int) and y <= year and e["status"] != "needs_verification")
    if "afore_fee_max" in params.overrides or year in table or not known:
        return params.number("afore_fee_max", year), year
    latest = known[-1]
    warnings.append(f"No CONSAR maximum AFORE fee is recorded for {year}; the {latest} cap is used. Supply ley97.fee "
                    f"(or parameters.afore_fee_max) with the {year} figure.")
    return params.number("afore_fee_max", latest), latest


def ley97(data: dict[str, Any], *, age_now: float, retirement_age: float, weeks_now: float, as_of: date,
          params: _Params, minimum_wage: float | None, uma: float | None, longevity: list[float],
          missing: list[str], warnings: list[str], assumptions: list[str]) -> dict[str, Any] | None:
    if not isinstance(data, dict):
        raise ValueError("ley97 must be an object")
    needed = [f"ley97.{k}" for k in ("sbc_daily_mxn", "afore_balance_mxn") if k not in data]
    if needed:
        missing.extend(needed)
        return None
    sbc = _num(data["sbc_daily_mxn"], "ley97.sbc_daily_mxn", minimum=0)
    balance0 = _num(data["afore_balance_mxn"], "ley97.afore_balance_mxn", minimum=0)
    growth = _num(data.get("salary_real_growth", 0), "ley97.salary_real_growth", minimum=-0.5, maximum=0.5)
    weeks_per_year = _num(data.get("weeks_per_year", 52), "ley97.weeks_per_year", minimum=0, maximum=52)
    voluntary = _num(data.get("voluntary_monthly_mxn", 0), "ley97.voluntary_monthly_mxn", minimum=0)
    if "real_return" in data:
        returns = _range(data["real_return"], "ley97.real_return", minimum=-0.5, maximum=0.5)
    else:
        returns = [0.02, 0.035, 0.05]
        assumptions.append("SIEFORE generacional real returns before fees assumed at 2.0% / 3.5% / 5.0% (low / base / high). "
                           "CONSAR reports a 5.02% historical real system return (21-11-2025); history is not a forecast. "
                           "Supply ley97.real_return to replace this range.")
    if "fee" in data:
        fee, fee_year = _num(data["fee"], "ley97.fee", minimum=0, maximum=0.05), None
    else:
        fee, fee_year = _afore_fee(params, as_of.year, warnings)
    if fee is None:
        return None
    if "fee" not in data:
        assumptions.append(f"AFORE fee {fee:.2%} of assets per year (the {fee_year} CONSAR maximum) held constant.")
    annuity_rates = _range(data.get("annuity_real_rate", [0.02, 0.035]), "ley97.annuity_real_rate", minimum=-0.05, maximum=0.2)
    if "annuity_real_rate" not in data:
        assumptions.append("Programmed withdrawal priced at a 2.0%-3.5% real rate (Art. 194 divides the balance by the capital "
                           "needed per unit of life annuity); insurers' and CONSAR's actual factors differ.")
    schedule = params.get("ceav_employer_schedule")
    retiro = params.number("retiro_employer_rate")
    worker = params.number("ceav_worker_rate")
    cap_multiple = params.number("sbc_cap_uma_multiple")
    if None in (schedule, retiro, worker) or minimum_wage is None or uma is None:
        if uma is None:
            missing.append("parameters.uma_daily_mxn")
        if minimum_wage is None:
            missing.append("parameters.salario_minimo_general_daily_mxn")
        return None
    cuota_social = None
    if "cuota_social_daily_mxn" in data:
        cuota_social = _num(data["cuota_social_daily_mxn"], "ley97.cuota_social_daily_mxn", minimum=0)
        assumptions.append("Cuota social supplied by the caller is added for every contributed day.")
    else:
        cuota = params.number("cuota_social_daily_mxn")
        if cuota is not None:
            cuota_social = cuota
    if cuota_social is None and sbc <= 4 * uma:
        warnings.append("The cuota social (Art. 168 fr. IV, up to 4 UMA) is excluded because its current amount is not verified; "
                        "the projection is understated by it.")
    years = max(int(round(retirement_age - age_now)), 0)
    retire_year = as_of.year + years
    contributions = []
    salary = sbc
    for offset in range(years):
        year = as_of.year + offset
        capped = min(salary, uma * (cap_multiple or 25))
        band = _ceav_band(capped, minimum_wage, uma, schedule)
        employer = _ceav_employer_rate(schedule, year, band_index=band)
        rate = retiro + employer + worker
        work_days = 365 * weeks_per_year / 52
        amount = capped * work_days * rate + (cuota_social or 0.0) * work_days + voluntary * 12
        contributions.append({"year": year, "sbc_band": schedule["bands"][band], "total_rate_percent": round(rate * 100, 3),
                              "contribution_mxn": _r(amount)})
        salary *= 1 + growth
    weeks_at_retirement = weeks_now + weeks_per_year * years
    scenarios = []
    for r in returns:
        balance = balance0
        for row in contributions:
            balance = balance * (1 + r - fee) + row["contribution_mxn"]
        pw = []
        for death_age in longevity:
            for ar in annuity_rates:
                factor = _annuity_factor(max(death_age - retirement_age, 1), ar)
                pw.append(balance / factor / 12)
        scenarios.append({"real_return": r, "balance_at_retirement_mxn": _r(balance),
                          "programmed_withdrawal_monthly_mxn": {"low": _r(min(pw)), "high": _r(max(pw))}})
    career = data.get("average_career_sbc_daily_mxn")
    pg = None
    if career is None:
        missing.append("ley97.average_career_sbc_daily_mxn (INPC-updated average over the whole affiliation, for the pension garantizada band)")
    else:
        career_uma = _num(career, "ley97.average_career_sbc_daily_mxn", minimum=0) / uma
        pg = pension_garantizada(retire_year, retirement_age, weeks_at_retirement, career_uma, params, warnings)
    required = ley97_weeks_required(retire_year, params)
    eligible_weeks = required is not None and weeks_at_retirement >= required
    for s in scenarios:
        pw_low = s["programmed_withdrawal_monthly_mxn"]["low"]
        pw_high = s["programmed_withdrawal_monthly_mxn"]["high"]
        floor = pg.get("monthly_mxn") if pg and pg.get("eligible") else None
        if not eligible_weeks:
            s["estimated_pension_monthly_mxn"] = None
            s["outcome"] = "negativa de pension: the weeks requirement is not met; the balance would be paid as a lump sum"
        elif floor is not None:
            s["estimated_pension_monthly_mxn"] = {"low": max(pw_low, floor), "high": max(pw_high, floor)}
            s["outcome"] = "pension garantizada applies" if pw_high < floor else ("programmed withdrawal or annuity (above the guarantee)"
                                                                                 if pw_low >= floor else "guarantee binds in the low case")
        else:
            s["estimated_pension_monthly_mxn"] = {"low": pw_low, "high": pw_high}
            s["outcome"] = "programmed withdrawal or annuity; pension garantizada floor not valued"
    return {
        "years_to_retirement": years, "retirement_year": retire_year, "fee": fee,
        "weeks_at_retirement": weeks_at_retirement, "weeks_required": required, "meets_weeks": eligible_weeks,
        "contributions_by_year": contributions, "scenarios": scenarios, "pension_garantizada": pg,
        "early_retirement_rule": "Retiring before 60/65 is allowed only if the annuity exceeds the pension garantizada by more than 30% (LSS art. 158).",
        "subaccounts_excluded": "Vivienda (INFONAVIT) balance is excluded unless supplied inside afore_balance_mxn.",
    }


def _voluntary(data: dict[str, Any], *, years: int, returns: list[float], fee: float | None, params: _Params,
               as_of: date, missing: list[str]) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise ValueError("voluntary must be an object")
    short = _num(data.get("short_term_monthly_mxn", 0), "voluntary.short_term_monthly_mxn", minimum=0)
    long = _num(data.get("long_term_monthly_mxn", 0), "voluntary.long_term_monthly_mxn", minimum=0)
    net = [r - (fee or 0.0) for r in returns]
    fv = lambda monthly, r: sum(monthly * 12 * (1 + r) ** (years - 1 - t) for t in range(years))  # noqa: E731
    out: dict[str, Any] = {
        "short_term": {"monthly_mxn": short, "value_at_retirement_mxn": {"low": _r(fv(short, min(net))), "high": _r(fv(short, max(net)))},
                       "liquidity": "Withdrawable (LSS art. 192: 'en cualquier momento' under CONSAR's procedure; each AFORE sets a minimum holding period).",
                       "tax": "Not deductible when kept short term; real gains are interest income."},
        "long_term": {"monthly_mxn": long, "value_at_retirement_mxn": {"low": _r(fv(long, min(net))), "high": _r(fv(long, max(net)))},
                      "liquidity": "Aportaciones complementarias de retiro: locked until pension or age 65.",
                      "tax": "Deductible under LISR Art. 151 fr. V up to the lesser of 10% of accumulable income or five annual UMAs; early withdrawal is accumulable income."},
    }
    if "accumulable_income_mxn" in data:
        income = _num(data["accumulable_income_mxn"], "voluntary.accumulable_income_mxn", minimum=0)
        uma_annual = params.number("uma_annual_mxn", as_of.year)
        if uma_annual is not None:
            cap = min(0.10 * income, 5 * uma_annual)
            out["long_term"]["art151v_cap_mxn"] = _r(cap)
            out["long_term"]["deductible_mxn"] = _r(min(long * 12, cap))
    else:
        missing.append("voluntary.accumulable_income_mxn (for the Art. 151 fr. V deductible amount)")
    out["related_task"] = {"task": "mx_deductions", "why": "ISR saving of the long-term contribution",
                           "inputs_hint": {"tax_year": as_of.year, "proposed_ppr_contribution_mxn": _r(long * 12)}}
    return out


def retirement_mx(inputs: dict[str, Any], context: dict[str, Any] | None = None) -> dict[str, Any]:
    """Mexico retirement: regime, Ley 73 (+ Modalidad 40) or Ley 97 projection, voluntary savings and the gap."""
    missing: list[str] = []
    warnings: list[str] = []
    assumptions: list[str] = ["All amounts are real MXN (today's pesos): salaries, UMA and salario minimo keep today's real value."]
    params = _Params(inputs)
    as_of = _as_of(inputs)
    birth_year = _birth_year(inputs, context, assumptions)
    regime_info = None
    if inputs.get("first_cotizacion_date") is not None:
        regime_info = imss_regime(inputs["first_cotizacion_date"])
    regime = inputs.get("regime") or (regime_info or {}).get("regime")
    if inputs.get("regime") and regime_info and inputs["regime"] != regime_info["regime"]:
        if not (regime_info["regime"] == "ley73" and inputs["regime"] == "ley97"):
            raise ValueError("regime ley73 requires a first cotizacion before 1997-07-01")
        assumptions.append("A Ley 73 worker chose to be evaluated under Ley 97 (transitorio Tercero).")
    if regime not in {"ley73", "ley97"}:
        return _envelope("needs_input", {}, missing=["first_cotizacion_date (or regime: ley73|ley97)"], warnings=[],
                         sources=[_lss("articulo transitorio Tercero", "regime choice")], assumptions=[])
    if birth_year is None and "age" not in inputs:
        missing.append("birth_year (or age)")
    age_now = _num(inputs["age"], "age", minimum=14, maximum=110) if "age" in inputs else (as_of.year - birth_year if birth_year else None)
    retirement_age = _num(inputs.get("retirement_age", 65), "retirement_age", minimum=60, maximum=75)
    if "retirement_age" not in inputs:
        assumptions.append("retirement_age defaulted to 65 (vejez).")
    weeks = inputs.get("weeks_cotizadas")
    if weeks is None:
        missing.append("weeks_cotizadas (IMSS reporte de semanas cotizadas)")
    else:
        weeks = _num(weeks, "weeks_cotizadas", minimum=0, maximum=4000)
    longevity = _range(inputs.get("longevity_ages", [85, 90]), "longevity_ages", minimum=retirement_age)
    if "longevity_ages" not in inputs:
        assumptions.append("Longevity range ages 85 and 90 (assumption; supply longevity_ages).")
    minimum_wage = params.number("salario_minimo_general_daily_mxn", as_of.year)
    uma = params.number("uma_daily_mxn", as_of.year)
    result: dict[str, Any] = {"as_of": as_of.isoformat(), "currency": "MXN", "basis": "real (today's pesos)", "regime": regime,
                              "regime_rule": regime_info}
    pension_monthly: dict[str, float] | None = None
    if regime == "ley73" and age_now is not None and weeks is not None:
        data = inputs.get("ley73") or {}
        if not isinstance(data, dict):
            raise ValueError("ley73 must be an object")
        if "salary_history" in data:
            avg = average_salary_last_250_weeks(data["salary_history"])
        elif "average_daily_salary_mxn" in data:
            avg = _num(data["average_daily_salary_mxn"], "ley73.average_daily_salary_mxn", minimum=0)
        else:
            avg = None
            missing.append("ley73.average_daily_salary_mxn (last 250 weeks) or ley73.salary_history")
        if avg is not None:
            cap_multiple = params.number("sbc_cap_uma_multiple")
            if uma is None or cap_multiple is None:
                missing.append("parameters.uma_daily_mxn (to check the 25-UMA salary ceiling, LSS art. 28)")
            elif avg > uma * cap_multiple + 0.005:
                warnings.append(f"The Ley 73 average daily salary {_r(avg)} MXN is above the LSS art. 28 ceiling of "
                                f"{cap_multiple:g} UMA ({_r(uma * cap_multiple)} MXN/day); the pension uses the ceiling.")
                avg = uma * cap_multiple
        dependants = data.get("dependants")
        if dependants is None:
            missing.append("ley73.dependants {spouse, children_under_16, dependent_parents} (family assignments)")
            assumptions.append("Dependants not supplied: the Ley 73 pension includes the 15% ayuda asistencial (LSS 1973 "
                               "art. 164, pensioner with no spouse, children or dependent parents). Supply ley73.dependants "
                               "to replace this assumption.")
        contributing = data.get("still_contributing", False)
        if not isinstance(contributing, bool):
            raise ValueError("ley73.still_contributing must be a boolean")
        earning = sorted(k for k, v in (context or {}).items() if isinstance(k, str) and k.startswith("income.")
                         and isinstance(v, dict) and v.get("kind") in (None, "salary", "wages", "employment"))
        if "still_contributing" not in data and earning and age_now < 65:
            # Someone with a paycheck under 65 is still paying into IMSS unless they say otherwise.
            contributing = True
            assumptions.append(f"Still contributing assumed from your earned income ({', '.join(earning)}) and age under "
                               "65; pass ley73.still_contributing: false if you no longer contribute to IMSS.")

        def weeks_at(pension_age: float) -> float:
            """Recognized weeks at ``pension_age``: 52 more per working year while the person still contributes."""
            return weeks + 52 * max(pension_age - age_now, 0.0) if contributing else weeks

        if contributing:
            assumptions.append("Still contributing: 52 weeks are added for each working year until the pension age.")
        elif "still_contributing" not in data and age_now < max(retirement_age, 65):
            assumptions.append(f"Weeks held at the reported {weeks:g} (no further contributions); pass "
                               "ley73.still_contributing: true to add 52 weeks per working year.")
        group_note = ("Art. 167 groups use the salario minimo general at the pension date (statutory text); some IMSS "
                      "practice and advisers use the UMA since the 2016 desindexation. Confirm with IMSS; the lower "
                      "reference raises the times-multiple and lowers the basic percentage.")
        warnings.append(group_note)
        if minimum_wage is None:
            pass
        elif avg is not None:
            pension = ley73_pension(avg, weeks_at(retirement_age), retirement_age, minimum_wage, dependants=dependants,
                                    params=params)
            result["ley73"] = {"pension_at_retirement_age": pension}
            if pension and pension.get("eligible"):
                pension_monthly = {"low": pension["monthly_pension_mxn"], "high": pension["monthly_pension_mxn"]}
                result["ley73"]["by_age"] = {}
                for age in range(60, 66):
                    alt = ley73_pension(avg, weeks_at(age), age, minimum_wage, dependants=dependants, params=params)
                    result["ley73"]["by_age"][str(age)] = alt.get("monthly_pension_mxn") if alt and alt.get("eligible") else None
            elif pension is not None:
                warnings.append(pension["reason"])
            if "modalidad40" in data and pension is not None:
                # Modalidad 40 replaces ordinary contributions, so its baseline is the pension on today's weeks.
                baseline = pension if not contributing else ley73_pension(
                    avg, weeks, retirement_age, minimum_wage, dependants=dependants, params=params)
                base = {"prior_salary": avg, "weeks": weeks, "pension_age": retirement_age, "age_now": age_now,
                        "dependants": dependants, "pension": baseline, "longevity_ages": longevity}
                mod = modalidad40(data["modalidad40"], base=base, params=params, as_of=as_of, minimum_wage=minimum_wage,
                                  uma=uma, warnings=warnings, missing=missing, assumptions=assumptions)
                if mod is not None:
                    result["ley73"]["modalidad40"] = mod
                    if mod.get("pension_with_mxn") is not None and "ley73" in result:
                        result["ley73"]["pension_with_modalidad40_mxn"] = mod["pension_with_mxn"]
        warnings.append("If the person had weeks recognized by 31-12-1990, IMSS also computes the pre-1991 formula (35% + 1.25% "
                        "per increment) and pays the greater; that comparison is not modeled.")
        warnings.append("Ley 73 workers also hold an AFORE balance (retiro 97 and vivienda subaccounts) returned at pension; it is not included here.")
    elif regime == "ley97" and age_now is not None and weeks is not None:
        data = inputs.get("ley97") or {}
        projection = ley97(data, age_now=age_now, retirement_age=retirement_age, weeks_now=weeks, as_of=as_of, params=params,
                           minimum_wage=minimum_wage, uma=uma, longevity=longevity, missing=missing, warnings=warnings,
                           assumptions=assumptions)
        if projection is not None:
            result["ley97"] = projection
            values = [s["estimated_pension_monthly_mxn"] for s in projection["scenarios"] if s["estimated_pension_monthly_mxn"]]
            if values:
                pension_monthly = {"low": min(v["low"] for v in values), "high": max(v["high"] for v in values)}
    if "voluntary" in inputs and age_now is not None:
        vol_returns = [0.02, 0.035, 0.05]
        if isinstance(inputs.get("ley97"), dict) and "real_return" in inputs["ley97"]:
            vol_returns = _range(inputs["ley97"]["real_return"], "ley97.real_return", minimum=-0.5, maximum=0.5)
        ley97_in = inputs.get("ley97") if isinstance(inputs.get("ley97"), dict) else {}
        vol_fee = _num(ley97_in["fee"], "ley97.fee", minimum=0, maximum=0.05) if "fee" in ley97_in \
            else _afore_fee(params, as_of.year, warnings)[0]
        result["voluntary"] = _voluntary(inputs["voluntary"], years=max(int(round(retirement_age - age_now)), 0), returns=vol_returns,
                                         fee=vol_fee, params=params, as_of=as_of, missing=missing)
    target = inputs.get("target_monthly_spending_mxn")
    if target is None:
        missing.append("target_monthly_spending_mxn (real, today's pesos)")
    else:
        target = _num(target, "target_monthly_spending_mxn", minimum=0)
        other = _num(inputs.get("other_monthly_income_mxn", 0), "other_monthly_income_mxn", minimum=0)
        if pension_monthly is None:
            result["gap"] = None
        else:
            gap_high = max(target - other - pension_monthly["low"], 0.0)
            gap_low = max(target - other - pension_monthly["high"], 0.0)
            result["gap"] = {"target_monthly_mxn": target, "other_monthly_income_mxn": other,
                             "pension_monthly_mxn": pension_monthly,
                             "monthly_gap_mxn": {"low": _r(gap_low), "high": _r(gap_high)},
                             "annual_gap_mxn": {"low": _r(gap_low * 12), "high": _r(gap_high * 12)},
                             "next_step": {"task": "retirement_readiness", "inputs_hint": {
                                 "currency": "MXN", "target_annual_spending": _r(target * 12),
                                 "guaranteed_annual_income": _r((pension_monthly["low"] + other) * 12)}}}
            if "ley73" in result and pension_monthly:
                result["gap"]["note"] = "Ley 73 monthly pension compared against monthly spending; the aguinaldo adds one extra payment a year."
    missing.extend(m for m in params.missing if m not in missing)
    result.update(params.report())
    status = "needs_input" if (pension_monthly is None and ("ley73" not in result and "ley97" not in result)) else ("partial" if missing else "ready")
    sources = params.sources + [_lss("articulos 154, 162, 168, 170, 218", "Ley 97 requirements and voluntary continuation")]
    warnings.append("Estimates, not an IMSS resolution. Pension filing and annuity purchase: refer to IMSS, the AFORE, or an actuary.")
    return _envelope(status, result, missing=missing, warnings=params.warnings + warnings, sources=sources,
                     assumptions=params.assumptions + assumptions)


# ---------------------------------------------------------------------------
# United States
# ---------------------------------------------------------------------------

def full_retirement_age_months(birth_year: int, params: _Params) -> int | None:
    if birth_year >= 1960:
        value = params.get("ss_full_retirement_age", "1960+")
        return None if value is None else value["years"] * 12 + value["months"]
    if birth_year < 1943:
        raise ValueError("Social Security comparison supports birth years 1943 and later")
    table = params.get("ss_full_retirement_age", "1943-1959")
    if table is None:
        return None
    extra = table.get(birth_year, table.get(str(birth_year), 0)) if birth_year >= 1955 else 0
    return 66 * 12 + int(extra)


def benefit_factor(months_from_fra: int, params: _Params, *, spouse: bool = False) -> float | None:
    """Benefit as a fraction of PIA claimed ``months_from_fra`` months after (positive) or before (negative) FRA."""
    early = params.get("ss_early_reduction")
    late = params.get("ss_delayed_credit")
    if early is None or late is None:
        return None
    frac = lambda s: float(Decimal(s.split("/")[0]) / Decimal(s.split("/")[1])) / 100  # noqa: E731
    if months_from_fra < 0:
        m = -months_from_fra
        first = frac(early["spouse_first_36_per_month"] if spouse else early["worker_first_36_per_month"])
        return 1 - min(m, 36) * first - max(m - 36, 0) * frac(early["additional_per_month"])
    if spouse:
        return 1.0
    return 1 + months_from_fra * frac(late["per_month_percent"])


def _breakeven(a_age: float, a_benefit: float, b_age: float, b_benefit: float) -> float | None:
    if b_benefit <= a_benefit:
        return None
    return (b_benefit * b_age - a_benefit * a_age) / (b_benefit - a_benefit)


def social_security(data: dict[str, Any], params: _Params, missing: list[str], assumptions: list[str],
                    warnings: list[str]) -> dict[str, Any] | None:
    if not isinstance(data, dict):
        raise ValueError("social_security must be an object")
    need = [f"social_security.{k}" for k in ("pia_monthly_usd", "birth_year") if k not in data]
    if need:
        missing.extend(need)
        return None
    pia = _num(data["pia_monthly_usd"], "social_security.pia_monthly_usd", minimum=0)
    birth_year = _int(data["birth_year"], "social_security.birth_year", minimum=1943, maximum=2100)
    fra = full_retirement_age_months(birth_year, params)
    if fra is None:
        return None
    ages_months = sorted({a * 12 for a in range(62, 71)} | {fra})
    claims = []
    for months in ages_months:
        factor = benefit_factor(months - fra, params)
        if factor is None:
            return None
        claims.append({"claim_age": round(months / 12, 4), "claim_age_label": f"{months // 12}y{months % 12}m",
                       "percent_of_pia": round(factor * 100, 4), "monthly_benefit_usd": _r(pia * factor),
                       "annual_benefit_usd": _r(pia * factor * 12)})
    by_age = {c["claim_age"]: c for c in claims}
    fra_years = round(fra / 12, 4)
    pairs, seen = [], set()
    for a, b in ((62, fra_years), (62, 70), (fra_years, 70), (62, 67), (67, 70)):
        if a >= b or a not in by_age or b not in by_age or (float(a), float(b)) in seen:
            continue
        seen.add((float(a), float(b)))
        x = _breakeven(a, by_age[a]["monthly_benefit_usd"], b, by_age[b]["monthly_benefit_usd"])
        pairs.append({"earlier": a, "later": b, "breakeven_age": None if x is None else round(x, 2)})
    discount = _num(data.get("real_discount_rate", 0.0), "social_security.real_discount_rate", minimum=-0.05, maximum=0.2)
    longevity = _range(data.get("longevity_ages", [78, 85, 90, 95]), "social_security.longevity_ages", minimum=62, maximum=120)
    if "longevity_ages" not in data:
        assumptions.append("Social Security lifetime comparison at ages 78, 85, 90 and 95 (assumption).")
    monthly = (1 + discount) ** (1 / 12)
    lifetime = []
    for death in longevity:
        best, rows = None, []
        for c in claims:
            start = int(round(c["claim_age"] * 12))
            end = int(round(death * 12))
            pv = sum(c["monthly_benefit_usd"] / monthly ** (m - 62 * 12) for m in range(start, end))
            rows.append({"claim_age": c["claim_age"], "present_value_at_62_usd": _r(pv)})
            if best is None or pv > best[1]:
                best = (c["claim_age"], pv)
        lifetime.append({"death_age": death, "best_claim_age": best[0] if best else None, "by_claim_age": rows})
    out: dict[str, Any] = {"pia_monthly_usd": pia, "birth_year": birth_year, "full_retirement_age": f"{fra // 12}y{fra % 12}m",
                           "claims": claims, "breakevens": pairs, "real_discount_rate": discount, "lifetime_value": lifetime,
                           "notes": ["Benefits are in today's dollars: COLAs keep them level in real terms.",
                                     "Claiming before FRA while working triggers the earnings test (withheld, later recredited).",
                                     "Breakeven ages compare cumulative undiscounted benefits."]}
    spouse = data.get("spouse")
    spousal_note = ("A spouse may receive up to 50% of the worker's PIA at the spouse's FRA (42 U.S.C. 402(b)-(c)), reduced by "
                    "25/36 of 1% per month for the first 36 early months and 5/12 of 1% after; delayed credits do not raise the "
                    "spousal benefit. A surviving spouse's benefit is based on the worker's actual benefit, delayed credits "
                    "included, so the higher earner delaying protects the survivor.")
    if isinstance(spouse, dict) and "birth_year" in spouse:
        s_birth = _int(spouse["birth_year"], "social_security.spouse.birth_year", minimum=1943, maximum=2100)
        own = _num(spouse.get("own_pia_monthly_usd", 0), "social_security.spouse.own_pia_monthly_usd", minimum=0)
        s_fra = full_retirement_age_months(s_birth, params)
        excess = max(0.5 * pia - own, 0.0)
        rows = []
        if s_fra is not None:
            for age in range(62, 71):
                f = benefit_factor(min(age * 12 - s_fra, 0), params, spouse=True)
                rows.append({"spouse_claim_age": age, "spousal_excess_monthly_usd": _r(excess * (f or 0))})
        out["spousal"] = {"note": spousal_note, "spousal_excess_at_spouse_fra_usd": _r(excess), "by_spouse_claim_age": rows,
                          "rule": "The spousal benefit cannot start before the worker has filed."}
        if "own_pia_monthly_usd" not in spouse:
            missing.append("social_security.spouse.own_pia_monthly_usd (spousal excess assumes zero own benefit)")
    else:
        out["spousal"] = {"note": spousal_note}
    return out


def contribution_limits(data: dict[str, Any], params: _Params, missing: list[str], warnings: list[str]) -> dict[str, Any] | None:
    if not isinstance(data, dict):
        raise ValueError("contributions must be an object")
    year = _int(data.get("tax_year", 2026), "contributions.tax_year", minimum=2000, maximum=2100)
    if "birth_year" not in data:
        missing.append("contributions.birth_year (catch-up eligibility)")
        return None
    age = year - _int(data["birth_year"], "contributions.birth_year", minimum=1900, maximum=2100)
    values = {k: params.number(k, year) for k in ("us_402g_deferral_limit", "us_catch_up_50", "us_catch_up_60_63",
                                                  "us_roth_catch_up_wage_threshold", "us_415c_limit", "us_ira_limit",
                                                  "us_ira_catch_up", "us_hsa_self_limit", "us_hsa_family_limit")}
    hsa_catch = params.number("us_hsa_catch_up_55", year)
    if any(v is None for v in values.values()):
        return None
    catch_up = values["us_catch_up_60_63"] if 60 <= age <= 63 else values["us_catch_up_50"] if age >= 50 else 0.0
    roth_rule = None
    wages = data.get("prior_year_fica_wages_usd")
    if catch_up:
        if wages is None:
            missing.append("contributions.prior_year_fica_wages_usd (Roth catch-up rule)")
        else:
            roth_rule = _num(wages, "contributions.prior_year_fica_wages_usd", minimum=0) > values["us_roth_catch_up_wage_threshold"]
    hdhp = data.get("hdhp_coverage", "none")
    if hdhp not in {"none", "self", "family"}:
        raise ValueError("contributions.hdhp_coverage must be none, self or family")
    hsa = 0.0 if hdhp == "none" else values["us_hsa_self_limit" if hdhp == "self" else "us_hsa_family_limit"] + (hsa_catch if age >= 55 else 0.0)
    warnings.append("Roth catch-up: from 2026, catch-up contributions of employees whose prior-year FICA wages from the plan "
                    "sponsor exceeded the threshold must be designated Roth (IRC 414(v)(7), SECURE 2.0 sec. 603); plans "
                    "without a Roth option cannot accept their catch-up.")
    return {
        "tax_year": year, "age_at_year_end": age,
        "employer_plan_401k_403b_457": {"elective_deferral_usd": values["us_402g_deferral_limit"], "catch_up_usd": catch_up,
                                        "catch_up_basis": "ages 60-63" if 60 <= age <= 63 else ("age 50+" if age >= 50 else "not eligible"),
                                        "total_employee_usd": values["us_402g_deferral_limit"] + catch_up,
                                        "catch_up_must_be_roth": roth_rule,
                                        "annual_additions_limit_415c_usd": values["us_415c_limit"]},
        "ira": {"limit_usd": values["us_ira_limit"] + (values["us_ira_catch_up"] if age >= 50 else 0.0),
                "note": "Traditional IRA deductibility and Roth IRA income phase-outs are not modeled."},
        "hsa": {"coverage": hdhp, "limit_usd": hsa, "catch_up_55_usd": hsa_catch if age >= 55 and hdhp != "none" else 0.0},
    }


def rmd_age(birth_year: int, params: _Params) -> int | None:
    if birth_year <= 1950:
        raise ValueError("RMD ages for people born in 1950 or earlier are outside this model")
    key = "1951-1958" if birth_year <= 1958 else "1959" if birth_year == 1959 else "1960+"
    return params.get("rmd_applicable_age", key)


def rmd_amount(prior_year_end_balance: float, age: int, params: _Params) -> float | None:
    table = params.get("rmd_uniform_lifetime_table")
    if table is None:
        return None
    divisor = table.get(min(age, 120)) or table.get(str(min(age, 120)))
    if divisor is None:
        raise ValueError("uniform lifetime table covers ages 72 and above")
    return prior_year_end_balance / float(divisor)


def _bracket_top(table: dict[str, Any], status: str, rate: float) -> float:
    key = "married_filing_jointly" if status == "qualifying_surviving_spouse" else status
    brackets = table["ordinary_brackets"][key]
    for index, (_, r) in enumerate(brackets):
        if abs(float(r) - rate) < 1e-9:
            if index + 1 >= len(brackets):
                raise ValueError("the top bracket has no ceiling to fill")
            return float(brackets[index + 1][0])
    raise ValueError(f"conversion target rate {rate} is not a bracket rate")


def _tax(table: dict[str, Any], status: str, ordinary_gross: float, gain: float, deduction: float) -> float:
    ordinary = max(ordinary_gross - deduction, 0.0)
    leftover = max(deduction - ordinary_gross, 0.0)
    taxable_gain = max(gain - leftover, 0.0)
    out = federal_tax(table, status, ordinary_income=Decimal(str(round(ordinary, 2))), qualified_dividends=Decimal(0),
                      net_short_term_gain=Decimal(0), net_capital_gain=Decimal(str(round(taxable_gain, 2))),
                      capital_loss_deduction=Decimal(0), magi_excluding_capital_gains=Decimal(str(round(ordinary_gross, 2))),
                      nii_excluding_capital_gains=Decimal(0))
    return float(out["total_tax"])


STRATEGIES = ("taxable_first", "proportional", "taxable_first_with_roth_conversions")


def simulate_withdrawals(cfg: dict[str, Any], strategy: str, params: _Params) -> dict[str, Any]:
    """Year-by-year decumulation in real dollars with federal tax from the bracket engine."""
    table = cfg["table"]
    status = cfg["filing_status"]
    taxable, basis = cfg["taxable"], cfg["taxable_basis"]
    deferred, roth = cfg["tax_deferred"], cfg["roth"]
    r = cfg["real_return"]
    rows = []
    total_tax = 0.0
    shortfall_years = 0
    first_shortfall_age = None
    for i in range(cfg["years"]):
        age = cfg["start_age"] + i
        rmd = 0.0
        if cfg["rmd_age"] is not None and age >= cfg["rmd_age"] and deferred > 0:
            rmd = min(rmd_amount(deferred, age, params) or 0.0, deferred)
        top = cfg["conversion_ceiling"] if strategy == "taxable_first_with_roth_conversions" else None
        need = cfg["spending"]
        gross = need
        w_tax = w_def = w_roth = conv = gain = tax = 0.0
        short = 0.0
        for _ in range(100):
            remaining = max(gross - rmd, 0.0)
            avail_def = deferred - rmd
            if strategy == "proportional":
                total = taxable + avail_def + roth
                take = min(remaining, total)
                w_tax = take * taxable / total if total else 0.0
                w_def = take * avail_def / total if total else 0.0
                w_roth = take * roth / total if total else 0.0
            else:
                w_tax = min(remaining, taxable)
                w_def = min(remaining - w_tax, avail_def)
                w_roth = max(min(remaining - w_tax - w_def, roth), 0.0)
            short = remaining - w_tax - w_def - w_roth
            conv = 0.0
            if top is not None:
                room = top + cfg["deduction"] - (cfg["other_income"] + rmd + w_def)
                conv = max(min(room, avail_def - w_def), 0.0)
            gain = w_tax * max(1 - basis / taxable, 0.0) if taxable > 0 else 0.0
            tax = _tax(table, status, cfg["other_income"] + rmd + w_def + conv, gain, cfg["deduction"])
            new_gross = need + tax
            if abs(new_gross - gross) < 0.005:
                gross = new_gross
                break
            gross = new_gross
        cash = rmd + w_tax + w_def + w_roth
        excess = max(cash - (need + tax), 0.0)
        if taxable > 0:
            basis -= basis * w_tax / taxable
        taxable -= w_tax
        taxable += excess
        basis += excess
        deferred -= rmd + w_def + conv
        roth += conv - w_roth
        total_tax += tax
        if short > 0.01:
            shortfall_years += 1
            first_shortfall_age = first_shortfall_age if first_shortfall_age is not None else age
        rows.append({"age": age, "rmd": _r(rmd), "from_taxable": _r(w_tax), "from_tax_deferred": _r(w_def), "from_roth": _r(w_roth),
                     "roth_conversion": _r(conv), "realized_gain": _r(gain), "federal_tax": _r(tax), "shortfall": _r(max(short, 0.0))})
        taxable *= 1 + r
        deferred *= 1 + r
        roth *= 1 + r
    terminal = cfg["terminal_rates"]
    after_tax = (taxable - max(taxable - basis, 0.0) * terminal["taxable_gain"] + roth + deferred * (1 - terminal["tax_deferred"]))
    return {"strategy": strategy, "total_federal_tax_usd": _r(total_tax), "years_with_shortfall": shortfall_years,
            "first_shortfall_age": first_shortfall_age,
            "ending_balances_usd": {"taxable": _r(taxable), "taxable_basis": _r(basis), "tax_deferred": _r(deferred), "roth": _r(roth)},
            "ending_after_tax_wealth_usd": _r(after_tax), "years": rows}


def withdrawal_ordering(data: dict[str, Any], params: _Params, missing: list[str], assumptions: list[str],
                        warnings: list[str]) -> dict[str, Any] | None:
    if not isinstance(data, dict):
        raise ValueError("withdrawals must be an object")
    required = ["balances", "annual_spending_usd", "start_age", "years", "filing_status", "real_return"]
    need = [f"withdrawals.{k}" for k in required if k not in data]
    if need:
        missing.extend(need)
        return None
    balances = data["balances"]
    if not isinstance(balances, dict):
        raise ValueError("withdrawals.balances must be an object")
    for key in ("taxable", "taxable_basis", "tax_deferred", "roth"):
        if key not in balances:
            missing.append(f"withdrawals.balances.{key}")
    if any(m.startswith("withdrawals.balances.") for m in missing):
        return None
    year = _int(data.get("tax_year", 2026), "withdrawals.tax_year", minimum=2000, maximum=2100)
    table = US_FEDERAL_PARAMETERS.get(year)
    if table is None or table.get("status") != "verified":
        missing.append(f"verified US bracket table for {year} (wealth.us_tax_parameters)")
        return None
    status = data["filing_status"]
    if status not in {"single", "married_filing_jointly", "married_filing_separately", "head_of_household", "qualifying_surviving_spouse"}:
        raise ValueError("withdrawals.filing_status is not supported")
    if "deduction_usd" in data:
        deduction = _num(data["deduction_usd"], "withdrawals.deduction_usd", minimum=0)
    else:
        std = params.get("us_standard_deduction", year)
        if std is None:
            return None
        deduction = float(std[status])
        assumptions.append(f"Deduction: {year} standard deduction for {status} (${deduction:,.0f}); age-65 additions and itemizing ignored.")
    taxable_value = _num(balances["taxable"], "withdrawals.balances.taxable", minimum=0)
    basis = _num(balances["taxable_basis"], "withdrawals.balances.taxable_basis", minimum=0)
    birth_year = data.get("birth_year")
    start_age = _int(data["start_age"], "withdrawals.start_age", minimum=40, maximum=110)
    rmd_start = None
    if birth_year is None:
        missing.append("withdrawals.birth_year (RMD start age)")
    else:
        rmd_start = rmd_age(_int(birth_year, "withdrawals.birth_year", minimum=1951, maximum=2100), params)
    target = data.get("conversion_target_rate", 0.12)
    target = _num(target, "withdrawals.conversion_target_rate", minimum=0, maximum=0.37)
    if "conversion_target_rate" not in data:
        assumptions.append("Roth conversions fill ordinary taxable income to the top of the 12% bracket (assumption).")
    terminal = data.get("terminal_rates", {"tax_deferred": 0.22, "taxable_gain": 0.15})
    if not isinstance(terminal, dict):
        raise ValueError("withdrawals.terminal_rates must be an object")
    terminal = {"tax_deferred": _num(terminal.get("tax_deferred", 0.22), "withdrawals.terminal_rates.tax_deferred", minimum=0, maximum=1),
                "taxable_gain": _num(terminal.get("taxable_gain", 0.15), "withdrawals.terminal_rates.taxable_gain", minimum=0, maximum=1)}
    if "terminal_rates" not in data:
        assumptions.append("Ending wealth values tax-deferred dollars at 22% and unrealized gains at 15% (assumption).")
    cfg = {"table": table, "filing_status": status, "taxable": taxable_value, "taxable_basis": min(basis, taxable_value),
           "tax_deferred": _num(balances["tax_deferred"], "withdrawals.balances.tax_deferred", minimum=0),
           "roth": _num(balances["roth"], "withdrawals.balances.roth", minimum=0),
           "spending": _num(data["annual_spending_usd"], "withdrawals.annual_spending_usd", minimum=0),
           "other_income": _num(data.get("other_ordinary_income_usd", 0), "withdrawals.other_ordinary_income_usd", minimum=0),
           "deduction": deduction, "start_age": start_age, "years": _int(data["years"], "withdrawals.years", minimum=1, maximum=60),
           "real_return": _num(data["real_return"], "withdrawals.real_return", minimum=-0.5, maximum=0.5),
           "rmd_age": rmd_start, "conversion_ceiling": _bracket_top(table, status, target), "terminal_rates": terminal}
    if "other_ordinary_income_usd" not in data:
        missing.append("withdrawals.other_ordinary_income_usd (taxable Social Security, pensions; treated as 0)")
    results = {s: simulate_withdrawals(cfg, s, params) for s in STRATEGIES}
    best = max(results.values(), key=lambda x: (-x["years_with_shortfall"], x["ending_after_tax_wealth_usd"]))
    assumptions.append(f"Withdrawals: real dollars; the {year} brackets are held constant in real terms; taxable-account "
                       "dividends are not taxed separately; the conversion tax is paid from withdrawals; RMDs use the Uniform "
                       "Lifetime Table on the start-of-year balance.")
    warnings.append("Federal tax only (no state tax, IRMAA, ACA credits, or the taxation of Social Security benefits beyond "
                    "what is supplied in other_ordinary_income_usd).")
    return {"tax_year_brackets": year, "filing_status": status, "deduction_usd": deduction, "rmd_start_age": rmd_start,
            "conversion_ceiling_taxable_income_usd": cfg["conversion_ceiling"],
            "strategies": results, "highest_ending_after_tax_wealth": best["strategy"],
            "comparison": {s: {"total_federal_tax_usd": v["total_federal_tax_usd"], "ending_after_tax_wealth_usd": v["ending_after_tax_wealth_usd"],
                               "years_with_shortfall": v["years_with_shortfall"]} for s, v in results.items()}}


def retirement_us(inputs: dict[str, Any], context: dict[str, Any] | None = None) -> dict[str, Any]:
    missing: list[str] = []
    warnings: list[str] = []
    assumptions: list[str] = []
    params = _Params(inputs)
    birth_year = _birth_year(inputs, context, assumptions)
    result: dict[str, Any] = {"currency": "USD", "basis": "real (today's dollars)"}
    sections = [s for s in ("social_security", "contributions", "rmd", "withdrawals") if s in inputs]
    if not sections:
        return _envelope("needs_input", {}, missing=["one of social_security, contributions, rmd, withdrawals"], warnings=[],
                         sources=[], assumptions=[])

    def with_birth(section: dict[str, Any]) -> dict[str, Any]:
        if isinstance(section, dict) and "birth_year" not in section and birth_year is not None:
            return {**section, "birth_year": birth_year}
        return section

    if "social_security" in inputs:
        result["social_security"] = social_security(with_birth(inputs["social_security"]), params, missing, assumptions, warnings)
    if "contributions" in inputs:
        result["contributions"] = contribution_limits(with_birth(inputs["contributions"]), params, missing, warnings)
    if "rmd" in inputs:
        data = with_birth(inputs["rmd"])
        if not isinstance(data, dict):
            raise ValueError("rmd must be an object")
        if "birth_year" not in data:
            missing.append("rmd.birth_year")
            result["rmd"] = None
        else:
            by = _int(data["birth_year"], "rmd.birth_year", minimum=1951, maximum=2100)
            start = rmd_age(by, params)
            out: dict[str, Any] = {"birth_year": by, "rmd_start_age": start,
                                   "first_rmd_deadline": None if start is None else f"{by + start + 1}-04-01",
                                   "rules": ["Later RMDs are due by December 31 each year; delaying the first to April 1 means two RMDs in that year.",
                                             "Roth IRAs and designated Roth accounts in 401(k)/403(b) plans have no RMDs during the owner's life (IRS RMD FAQ).",
                                             "Employer plans may let a non-5% owner still working delay until retirement."]}
            if start is not None and "prior_year_end_balance_usd" in data and "age" in data:
                age = _int(data["age"], "rmd.age", minimum=40, maximum=120)
                if age >= start:
                    out["rmd_usd"] = _r(rmd_amount(_num(data["prior_year_end_balance_usd"], "rmd.prior_year_end_balance_usd", minimum=0), age, params) or 0.0)
                else:
                    out["rmd_usd"] = 0.0
            result["rmd"] = out
    if "withdrawals" in inputs:
        result["withdrawals"] = withdrawal_ordering(with_birth(inputs["withdrawals"]), params, missing, assumptions, warnings)
    missing.extend(m for m in params.missing if m not in missing)
    result.update(params.report())
    computed = [s for s in sections if result.get(s) is not None]
    status = "needs_input" if not computed or params.needs_verification and len(computed) < len(sections) else ("partial" if missing else "ready")
    return _envelope(status, result, missing=missing, warnings=params.warnings + warnings, sources=params.sources,
                     assumptions=params.assumptions + assumptions)


# ---------------------------------------------------------------------------
# Shared: readiness
# ---------------------------------------------------------------------------

def required_nest_egg(annual_spending_gap: float, withdrawal_rate: float) -> float:
    if withdrawal_rate <= 0:
        raise ValueError("withdrawal rate must be positive")
    return max(annual_spending_gap, 0.0) / withdrawal_rate


def monthly_contribution_to_close(gap: float, annual_real_return: float, months: int) -> float | None:
    """Level monthly contribution (end of month) that grows to ``gap`` in ``months``."""
    if gap <= 0:
        return 0.0
    if months <= 0:
        return None
    rm = (1 + annual_real_return) ** (1 / 12) - 1
    if abs(rm) < 1e-12:
        return gap / months
    return gap * rm / ((1 + rm) ** months - 1)


def _future_value(balance: float, annual_contribution: float, rate: float, years: int) -> float:
    value = balance
    for _ in range(years):
        value = value * (1 + rate) + annual_contribution
    return value


def retirement_readiness(inputs: dict[str, Any], context: dict[str, Any] | None = None) -> dict[str, Any]:
    missing: list[str] = []
    warnings: list[str] = []
    assumptions: list[str] = ["All amounts are real (today's money) in the stated currency."]
    required = ["currency", "current_age", "retirement_age", "plan_to_age", "target_annual_spending",
                "guaranteed_annual_income", "current_savings", "annual_contribution", "real_return"]
    need = [k for k in required if k not in inputs]
    if "current_age" in need:
        birth = _birth_year(inputs, context, assumptions)
        if birth is not None:
            need.remove("current_age")
            inputs = {**inputs, "current_age": _as_of(inputs).year - birth}
    if need:
        return _envelope("needs_input", {}, missing=need + (["return_model + annual_inflation (Monte Carlo)"] if "return_model" not in inputs else []),
                         warnings=["Guaranteed income (pension, Social Security) must be stated, even as 0; unknown is not zero."],
                         sources=[], assumptions=[])
    currency = inputs["currency"]
    if not isinstance(currency, str) or len(currency) != 3 or not currency.isupper():
        raise ValueError("currency must be three uppercase letters")
    age = _num(inputs["current_age"], "current_age", minimum=14, maximum=110)
    retire = _num(inputs["retirement_age"], "retirement_age", minimum=age, maximum=110)
    plan_to = _num(inputs["plan_to_age"], "plan_to_age", minimum=retire + 1, maximum=120)
    spending = _num(inputs["target_annual_spending"], "target_annual_spending", minimum=0)
    guaranteed = _num(inputs["guaranteed_annual_income"], "guaranteed_annual_income", minimum=0)
    savings = _num(inputs["current_savings"], "current_savings", minimum=0)
    contribution = _num(inputs["annual_contribution"], "annual_contribution", minimum=0)
    returns = _range(inputs["real_return"], "real_return", minimum=-0.5, maximum=0.5)
    swr = _range(inputs.get("withdrawal_rates", [0.03, 0.035, 0.04]), "withdrawal_rates", minimum=0.005, maximum=0.2)
    if "withdrawal_rates" not in inputs:
        assumptions.append("Safe-withdrawal range 3.0% / 3.5% / 4.0% of the starting balance, held in real terms. The 4% "
                           "figure comes from 30-year US historical studies (Bengen 1994); longer horizons, lower expected "
                           "returns and non-US markets argue for the low end.")
    years = int(round(retire - age))
    horizon = int(round(plan_to - retire))
    gap_income = max(spending - guaranteed, 0.0)
    nest = {f"{w:.4g}": _r(required_nest_egg(gap_income, w)) for w in swr}
    projected = {f"{r:.4g}": _r(_future_value(savings, contribution, r, years)) for r in returns}
    gaps = []
    for r in returns:
        for w in swr:
            need_amt = required_nest_egg(gap_income, w)
            have = _future_value(savings, contribution, r, years)
            short = max(need_amt - have, 0.0)
            gaps.append({"real_return": r, "withdrawal_rate": w, "required": _r(need_amt), "projected": _r(have),
                         "gap": _r(short), "surplus": _r(max(have - need_amt, 0.0)),
                         "extra_monthly_contribution": None if (m := monthly_contribution_to_close(short, r, years * 12)) is None else _r(m)})
    result: dict[str, Any] = {
        "currency": currency, "years_to_retirement": years, "years_in_retirement": horizon,
        "annual_spending_from_portfolio": _r(gap_income),
        "required_nest_egg_by_withdrawal_rate": nest,
        "projected_savings_at_retirement_by_return": projected,
        "gap_matrix": gaps,
    }
    # A gap no contribution can close (no months left) is None, never 0: the worst case is then unclosable.
    known = [g["extra_monthly_contribution"] for g in gaps if g["extra_monthly_contribution"] is not None]
    unclosable = len(known) < len(gaps)
    result["worst_case_extra_monthly"] = None if unclosable or not known else max(known)
    result["best_case_extra_monthly"] = min(known) if known else None
    if unclosable:
        result["worst_case_extra_monthly_reason"] = ("At least one scenario has a gap and no months left to contribute before "
                                                     "retirement, so no monthly contribution closes it.")
    if years == 0 and any(g["gap"] > 0 for g in gaps):
        warnings.append("Already at retirement age: a gap cannot be closed by contributions; it means lower spending, later retirement, or more risk.")
    if "return_model" in inputs:
        if "annual_inflation" not in inputs:
            missing.append("annual_inflation (Monte Carlo converts the real target through it)")
        else:
            inflation = _num(inputs["annual_inflation"], "annual_inflation", minimum=0, maximum=1)
            simulations = _int(inputs.get("simulations", 2000), "simulations", minimum=100, maximum=100000)
            seed = _int(inputs.get("seed", 7), "seed", minimum=0)
            fee = _num(inputs.get("annual_fee", 0), "annual_fee", minimum=0, maximum=0.05)
            middle = sorted(returns)[len(returns) // 2]
            start = _future_value(savings, contribution, middle, years)
            runs = {}
            for label, wealth in (("projected_savings", start), ("required_nest_egg_at_middle_rate", required_nest_egg(gap_income, swr[len(swr) // 2]))):
                report = planning.run("income", {
                    "currency": currency, "initial_wealth": wealth, "years": max(horizon, 1), "annual_income_need": gap_income,
                    "annual_need_growth": inflation, "annual_inflation": inflation, "annual_dividend_yield": 0, "annual_fee": fee,
                    "annual_tax_drag": 0, "simulations": simulations, "seed": seed, "return_model": inputs["return_model"],
                    "spending_timing": "start"}, {})
                warnings.extend(report.get("warnings", []))
                if report["status"] != "ready":
                    missing.extend(report.get("missing", []))
                    continue
                strategy = report["result"]["strategies"]["total_return_sales"]
                runs[label] = {"starting_wealth": _r(wealth),
                               "probability_of_success_percent": round(100 - strategy["probability_of_any_income_deficit_percent"], 2),
                               "real_terminal_wealth_percentiles": strategy["real_terminal_wealth_percentiles"],
                               "return_model": report["result"]["return_model"]}
            result["monte_carlo"] = runs
            assumptions.append(f"Monte Carlo via wealth.planning income: {simulations} paths, seed {seed}; spending grows with "
                               f"{inflation:.2%} inflation so it stays level in real terms; starting wealth is the projection at "
                               f"the middle return ({middle:.2%}).")
    else:
        missing.append("return_model + annual_inflation (Monte Carlo probability; see the planning income task)")
    assumptions.append("Contributions are made at the end of each year until retirement; the monthly amount to close a gap is an end-of-month level contribution.")
    status = "partial" if missing else "ready"
    sources = [{"title": "Bengen, W. P. (1994), 'Determining Withdrawal Rates Using Historical Data', Journal of Financial Planning",
                "note": "context for the withdrawal-rate range; not a rule"}]
    return _envelope(status, result, missing=missing, warnings=warnings, sources=sources, assumptions=assumptions)


# ---------------------------------------------------------------------------
# dispatcher
# ---------------------------------------------------------------------------

_TASKS = {"retirement_mx": retirement_mx, "retirement_us": retirement_us, "retirement_readiness": retirement_readiness}


def run(task: str, inputs: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    """Service entry point for ``retirement_mx``, ``retirement_us`` and ``retirement_readiness``."""
    if not isinstance(task, str) or task not in _TASKS:
        raise ValueError(f"retirement.run supports tasks {sorted(_TASKS)}")
    if not isinstance(inputs, dict) or not isinstance(context, dict):
        raise ValueError("inputs and context must be objects")
    return _TASKS[task](dict(inputs), context)


__all__ = ["PARAMETERS", "average_salary_last_250_weeks", "benefit_factor", "contribution_limits", "full_retirement_age_months",
           "imss_regime", "ley73_increments", "ley73_pension", "ley97_weeks_required", "modalidad40", "monthly_contribution_to_close",
           "parameter_inventory", "pension_garantizada", "required_nest_egg", "retirement_mx", "retirement_readiness",
           "retirement_us", "rmd_age", "rmd_amount", "run", "simulate_withdrawals", "social_security", "withdrawal_ordering"]
