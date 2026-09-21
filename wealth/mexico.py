"""Mexico-resident scenarios for individual (persona física) investors.

Scope: classification and liquidity of Mexican holdings, real-interest
taxation (LISR Arts. 133-136), personal deductions and retirement
contributions (LISR Arts. 151 and 185), foreign securities held outside the
SIC (LISR Arts. 5, 142 and Title IV Chapter IV), and a dated tax calendar.

Every calculation is a scenario, not a return.  Parameters come from a dated
table.  A parameter is used by default only when its status is ``verified``
(the official text was read on the ``checked_on`` date) or ``statutory`` (a
long-standing statutory figure cited to its article).  Parameters marked
``needs_verification`` are never used: the caller must supply them under
``inputs["parameters"][key] = {"value": ..., "source": ...}`` or the task
fails closed with ``status="needs_input"``.  Unknown is not zero.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import math
from typing import Any


LISR_URL = "https://www.diputados.gob.mx/LeyesBiblio/pdf/LISR.pdf"
LISR_VERSION = "texto vigente, ultima reforma DOF 01-04-2024; read 2026-09-21"
LIF_2026_URL = "https://www.diputados.gob.mx/LeyesBiblio/pdf/LIF_2026.pdf"
ANEXO8_2026_URL = "https://www.sat.gob.mx/minisitio/NormatividadRMFyRGCE/documentos2026/rmf/anexos/Anexo-8-RMF-2026_DOF-28122025.pdf"
UMA_2026_URL = "https://dof.gob.mx/nota_detalle.php?codigo=5778072&fecha=09/01/2026"
INEGI_UMA_URL = "https://www.inegi.org.mx/temas/uma/"
INEGI_INPC_URL = "https://www.inegi.org.mx/temas/inpc/"
BANXICO_UDI_URL = "https://www.banxico.org.mx/SieInternet/"
SAT_DEDUCCIONES_URL = "https://www.sat.gob.mx/minisitio/DeduccionesPersonales/index.html"
DOF_SEARCH_URL = "https://www.dof.gob.mx/"
TREATY_URL = "https://www.irs.gov/businesses/international-businesses/mexico-tax-treaty-documents"
CONSAR_URL = "https://www.gob.mx/consar"

CONSULT_FLAG = "consult_contador"


def _lisr(article: str, rule: str) -> dict[str, Any]:
    return {"title": f"Ley del Impuesto sobre la Renta, {article}", "url": LISR_URL, "version": LISR_VERSION, "rules": [rule]}


def _tariff(rows: list[tuple[str, str, str]]) -> list[dict[str, str]]:
    return [{"lower": lower, "fixed": fixed, "rate_percent": rate} for lower, fixed, rate in rows]


# Tariffs transcribed from Anexo 8 RMF 2026 (DOF 28-12-2025), section C:
# C.I is the 2025 annual tariff and C.II the 2026 annual tariff (Arts. 97 and 152).
_TARIFF_2025 = _tariff([
    ("0.01", "0.00", "1.92"), ("8952.50", "171.88", "6.40"), ("75984.56", "4461.94", "10.88"),
    ("133536.08", "10723.55", "16.00"), ("155229.81", "14194.54", "17.92"), ("185852.58", "19682.13", "21.36"),
    ("374837.89", "60049.40", "23.52"), ("590796.00", "110842.74", "30.00"), ("1127926.85", "271981.99", "32.00"),
    ("1503902.47", "392294.17", "34.00"), ("4511707.38", "1414947.85", "35.00"),
])
_TARIFF_2026 = _tariff([
    ("0.01", "0.00", "1.92"), ("10135.12", "194.59", "6.40"), ("86022.12", "5051.37", "10.88"),
    ("151176.20", "12140.13", "16.00"), ("175735.67", "16069.64", "17.92"), ("210403.70", "22282.14", "21.36"),
    ("424353.98", "67981.92", "23.52"), ("668840.15", "125485.07", "30.00"), ("1276925.99", "307910.81", "32.00"),
    ("1702567.98", "444116.23", "34.00"), ("5107703.93", "1601862.46", "35.00"),
])

_ANEXO8 = {"title": "Anexo 8 de la Resolucion Miscelanea Fiscal para 2026 (DOF 28-12-2025)", "url": ANEXO8_2026_URL}
_STATUTE_ANY_YEAR = "any"


def _entry(value: Any, status: str, source: dict[str, Any], *, checked_on: str | None = None, reported_value: Any = None, verify_with: str | None = None) -> dict[str, Any]:
    record = {"value": value, "status": status, "source": source}
    if checked_on:
        record["checked_on"] = checked_on
    if reported_value is not None:
        record["reported_value_not_used"] = reported_value
    if verify_with:
        record["verify_with"] = verify_with
    return record


# Dated parameter table.  Keys map to {year | "any": entry}.
PARAMETERS: dict[str, dict[Any, dict[str, Any]]] = {
    "interest_annual_retention_rate": {
        2026: _entry("0.0090", "verified", {"title": "Ley de Ingresos de la Federacion 2026, articulo 24 (DOF 07-11-2025)", "url": LIF_2026_URL}, checked_on="2026-09-21"),
        2025: _entry(None, "needs_verification", {"title": "Ley de Ingresos de la Federacion 2025, articulo 21", "url": DOF_SEARCH_URL},
                     reported_value="0.0050", verify_with="LIF 2025 article 21 as published in the DOF (tasa de retencion anual, LISR Arts. 54 and 135)"),
        2024: _entry(None, "needs_verification", {"title": "Ley de Ingresos de la Federacion 2024, articulo 21", "url": DOF_SEARCH_URL},
                     reported_value="0.0148", verify_with="LIF 2024 article 21 as published in the DOF"),
    },
    "uma_annual_mxn": {
        2026: _entry("42794.64", "verified", {"title": "INEGI, valor de la UMA 2026 (DOF 09-01-2026), vigente desde 01-02-2026", "url": UMA_2026_URL}, checked_on="2026-09-21"),
        2025: _entry(None, "needs_verification", {"title": "INEGI, valor de la UMA 2025 (DOF 10-01-2025)", "url": INEGI_UMA_URL},
                     reported_value="41273.52", verify_with="INEGI UMA publication in the DOF of 10-01-2025 (valor anual)"),
    },
    "uma_daily_mxn": {
        2026: _entry("117.31", "verified", {"title": "INEGI, valor de la UMA 2026 (DOF 09-01-2026)", "url": UMA_2026_URL}, checked_on="2026-09-21"),
        2025: _entry(None, "needs_verification", {"title": "INEGI, valor de la UMA 2025 (DOF 10-01-2025)", "url": INEGI_UMA_URL},
                     reported_value="113.14", verify_with="INEGI UMA publication in the DOF of 10-01-2025 (valor diario)"),
    },
    "isr_annual_tariff": {
        2026: _entry(_TARIFF_2026, "verified", {**_ANEXO8, "section": "C.II tarifa del ejercicio 2026 (Arts. 97 y 152 LISR)"}, checked_on="2026-09-21"),
        2025: _entry(_TARIFF_2025, "verified", {**_ANEXO8, "section": "C.I tarifa del ejercicio 2025 (Arts. 97 y 152 LISR)"}, checked_on="2026-09-21"),
        2024: _entry(None, "needs_verification", {"title": "Anexo 8 RMF 2024", "url": "https://www.sat.gob.mx/"},
                     verify_with="Anexo 8 of the RMF for 2024, annual tariff Arts. 97 and 152"),
    },
    "art185_annual_limit_mxn": {_STATUTE_ANY_YEAR: _entry("152000", "verified", _lisr("articulo 185, fraccion I", "deposits, premiums and fund purchases capped at $152,000.00 per calendar year, all concepts combined"), checked_on="2026-09-21")},
    "art135_definitive_option_limit_mxn": {_STATUTE_ANY_YEAR: _entry("100000", "verified", _lisr("articulo 135, segundo parrafo", "retention may be treated as definitive when only Chapter VI income is received and it does not exceed $100,000.00"), checked_on="2026-09-21")},
    "art129_rate": {_STATUTE_ANY_YEAR: _entry("0.10", "verified", _lisr("articulo 129", "10% definitive tax on qualifying listed-share gains"), checked_on="2026-09-21")},
    "foreign_dividend_additional_rate": {_STATUTE_ANY_YEAR: _entry("0.10", "verified", _lisr("articulo 142, fraccion V", "additional definitive 10% on dividends distributed by foreign residents, due by the 17th of the following month"), checked_on="2026-09-21")},
    "fibra_distribution_retention_rate": {_STATUTE_ANY_YEAR: _entry("0.30", "verified", _lisr("articulo 188, fraccion V, and articulo 9", "intermediary retains at the Article 9 rate on distributed resultado fiscal; creditable for residents"), checked_on="2026-09-21")},
    "ppr_early_withdrawal_retention_rate": {_STATUTE_ANY_YEAR: _entry(None, "needs_verification", {"title": "Reglamento de la LISR and RMF rules on early withdrawals from planes personales de retiro", "url": "https://www.sat.gob.mx/"},
                                                                  reported_value="0.20", verify_with="RLISR / RMF rule governing retention on PPR withdrawals before age 65 (LISR Art. 151 fr. V; Art. 142 fr. XVIII)")},
    "us_mx_treaty_portfolio_dividend_rate": {_STATUTE_ANY_YEAR: _entry("0.10", "statutory", {"title": "US-Mexico Income Tax Convention, Article 10(2)(b)", "url": TREATY_URL})},
}

# Supported years for UMA-annual derivation: Art. 151 caps use "cinco veces el valor anual de la UMA".


def _decimal(value: Any, path: str, *, nonnegative: bool = True) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise ValueError(f"{path} must be a finite number")
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError(f"{path} must be a finite number")
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError(f"{path} must be a finite number") from None
    if not number.is_finite() or (nonnegative and number < 0):
        raise ValueError(f"{path} must be a {'nonnegative ' if nonnegative else ''}finite number")
    return number


def _date(value: Any, path: str) -> date:
    if not isinstance(value, str):
        raise ValueError(f"{path} must be an ISO date")
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise ValueError(f"{path} must be an ISO date") from None


def _text(value: Any, path: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{path} must be nonempty text")
    return value.strip()


def _year(value: Any, path: str = "tax_year") -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 2000 <= value <= 2100:
        raise ValueError(f"{path} must be an integer year")
    return value


def _money(value: Decimal) -> str:
    return format(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP), "f")


def _rate(value: Decimal) -> str:
    return format(value.normalize(), "f")


def _envelope(status: str, result: dict[str, Any], *, missing: list[str] | None = None, warnings: list[str] | None = None,
              sources: list[dict[str, Any]] | None = None, assumptions: list[str] | None = None) -> dict[str, Any]:
    unique_sources: list[dict[str, Any]] = []
    for source in sources or []:
        if source not in unique_sources:
            unique_sources.append(source)
    return {"status": status, "result": result, "missing": missing or [], "warnings": warnings or [],
            "sources": unique_sources, "assumptions": assumptions or []}


class _Params:
    """Resolve dated parameters, recording what was used and what is missing."""

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

    def get(self, key: str, year: int | None) -> Any:
        table = PARAMETERS.get(key, {})
        entry = table.get(year) if year is not None else None
        entry = entry or table.get(_STATUTE_ANY_YEAR)
        if key in self.overrides:
            override = self.overrides[key]
            if not isinstance(override, dict) or "value" not in override:
                raise ValueError(f"parameters.{key} must be an object with value and source")
            source = _text(override.get("source"), f"parameters.{key}.source")
            value = override["value"]
            if key == "isr_annual_tariff":
                value = _tariff_rows(value, f"parameters.{key}.value")
            else:
                _decimal(value, f"parameters.{key}.value")
                value = str(value)
            if entry and entry["status"] in {"verified", "statutory"} and entry["value"] != value:
                self.warnings.append(f"Supplied parameters.{key} differs from the {entry['status']} table value for {year}; the supplied value was used.")
            self.assumptions.append(f"Used caller-supplied {key} for {year} from: {source}.")
            self.used.append({"key": key, "year": year, "value": value, "status": "caller_supplied", "source": source})
            return value
        if entry is None or entry["status"] == "needs_verification":
            verify = (entry or {}).get("verify_with") or "the official publication for that year"
            self.missing.append(f"parameters.{key} (year {year}; verify with {verify})")
            self.needs_verification.append({"key": key, "year": year, "verify_with": verify, "source": (entry or {}).get("source")})
            return None
        self.used.append({"key": key, "year": year, "value": entry["value"], "status": entry["status"], "checked_on": entry.get("checked_on"), "source": entry["source"]})
        self.sources.append(entry["source"])
        return entry["value"]

    def decimal(self, key: str, year: int | None) -> Decimal | None:
        value = self.get(key, year)
        return None if value is None else Decimal(str(value))

    def report(self) -> dict[str, Any]:
        return {"parameters_used": self.used, "parameters_needing_verification": self.needs_verification}


def _tariff_rows(value: Any, path: str) -> list[dict[str, str]]:
    if not isinstance(value, list) or not value:
        raise ValueError(f"{path} must be a nonempty list of tariff rows")
    rows: list[dict[str, str]] = []
    previous = Decimal("-1")
    for index, row in enumerate(value):
        if isinstance(row, dict):
            lower, fixed, rate = row.get("lower"), row.get("fixed"), row.get("rate_percent")
        elif isinstance(row, (list, tuple)) and len(row) == 3:
            lower, fixed, rate = row
        else:
            raise ValueError(f"{path}[{index}] must be {{lower, fixed, rate_percent}}")
        lower_d = _decimal(lower, f"{path}[{index}].lower")
        rate_d = _decimal(rate, f"{path}[{index}].rate_percent")
        if lower_d <= previous or rate_d > 100:
            raise ValueError(f"{path} rows must have increasing lower limits and percentage rates")
        previous = lower_d
        rows.append({"lower": str(lower), "fixed": str(_decimal(fixed, f"{path}[{index}].fixed")), "rate_percent": str(rate)})
    return rows


def isr_annual_tax(taxable_income_mxn: Any, tariff: list[dict[str, str]]) -> Decimal:
    """Apply an Article 152 annual tariff: fixed fee plus rate on the excess over the lower limit."""
    income = _decimal(taxable_income_mxn, "taxable_income_mxn")
    rows = _tariff_rows(tariff, "tariff")
    if income < Decimal(rows[0]["lower"]):
        return Decimal(0)
    selected = rows[0]
    for row in rows:
        if income >= Decimal(row["lower"]):
            selected = row
    excess = income - Decimal(selected["lower"])
    return Decimal(selected["fixed"]) + excess * Decimal(selected["rate_percent"]) / 100


def _incremental_isr(inputs: dict[str, Any], params: _Params, year: int, amount: Decimal, *, direction: int = 1) -> tuple[dict[str, Any] | None, list[str]]:
    """Estimate ISR change from adding (direction=1) or deducting (-1) ``amount`` of taxable income."""
    base_raw = inputs.get("taxable_income_before_mxn")
    rate_raw = inputs.get("marginal_rate")
    if base_raw is not None:
        base = _decimal(base_raw, "taxable_income_before_mxn")
        tariff = params.get("isr_annual_tariff", year)
        if tariff is not None:
            after = max(base + direction * amount, Decimal(0))
            before_tax = isr_annual_tax(base, tariff)
            after_tax = isr_annual_tax(after, tariff)
            return {"method": f"Article 152 annual tariff {year}", "taxable_income_before_mxn": _money(base),
                    "taxable_income_after_mxn": _money(after), "isr_before_mxn": _money(before_tax),
                    "isr_after_mxn": _money(after_tax), "isr_change_mxn": _money(after_tax - before_tax),
                    "_change": after_tax - before_tax}, []
        if rate_raw is None:
            return None, []
    if rate_raw is not None:
        rate = _decimal(rate_raw, "marginal_rate")
        if rate > 1:
            raise ValueError("marginal_rate must be a decimal between 0 and 1")
        change = direction * amount * rate
        return {"method": "caller-supplied marginal rate; bracket crossings are not modeled", "marginal_rate": _rate(rate),
                "isr_change_mxn": _money(change), "_change": change}, []
    return None, ["taxable_income_before_mxn or marginal_rate"]


def _public(estimate: dict[str, Any] | None) -> dict[str, Any] | None:
    return None if estimate is None else {k: v for k, v in estimate.items() if not k.startswith("_")}


# ---------------------------------------------------------------------------
# 1. Instrument classification and liquidity
# ---------------------------------------------------------------------------

_INSTRUMENTS: dict[str, dict[str, Any]] = {
    "cetes": {
        "name": "CETES (Certificados de la Tesoreria)", "issuer": "Gobierno Federal", "denomination": "MXN",
        "tax_regime": "interest", "tax_summary": "Discount is interest; real interest accumulates annually; provisional retention on capital at the LIF rate.",
        "citations": ["LISR Arts. 133-135"],
        "liquidity": {"class": "maturity", "notes": "Zero-coupon to maturity (28 days to 2 years); early sale depends on the platform or broker and is at market price."},
    },
    "bondes": {
        "name": "BONDES (floating-rate development bonds)", "issuer": "Gobierno Federal", "denomination": "MXN",
        "tax_regime": "interest", "tax_summary": "Coupons and sale gains are interest; real interest accumulates annually.",
        "citations": ["LISR Arts. 133-135"],
        "liquidity": {"class": "market_or_maturity", "notes": "Tradable in the secondary market through a broker; price risk before maturity."},
    },
    "udibonos": {
        "name": "UDIBONOS (UDI-denominated bonds)", "issuer": "Gobierno Federal", "denomination": "UDI",
        "tax_regime": "interest", "tax_summary": "Interest and the UDI adjustment to principal are both interest income; real interest then applies.",
        "citations": ["LISR Arts. 8 and 133 (interest definition; UDI principal adjustment treated as interest - confirm with the constancia)", "LISR Art. 143 fr. V (states the UDI rule expressly for Chapter IX)", "LISR Art. 134"],
        "liquidity": {"class": "market_or_maturity", "notes": "Tradable before maturity at market price; MXN value requires a dated UDI value from Banxico."},
    },
    "mbonos": {
        "name": "Bonos M (fixed-rate federal bonds)", "issuer": "Gobierno Federal", "denomination": "MXN",
        "tax_regime": "interest", "tax_summary": "Coupons and sale gains are interest; real interest accumulates annually.",
        "citations": ["LISR Arts. 133-135"],
        "liquidity": {"class": "market_or_maturity", "notes": "Tradable before maturity; duration and price risk."},
    },
    "bank_pagare": {
        "name": "Pagare con rendimiento liquidable al vencimiento", "issuer": "Banco", "denomination": "MXN",
        "tax_regime": "interest", "tax_summary": "Bank interest; retention on capital by the bank; constancia by February 15.",
        "citations": ["LISR Arts. 54-55, 133-135"],
        "liquidity": {"class": "maturity", "notes": "Generally not redeemable before the stated term; confirm the contract."},
    },
    "bank_deposit": {
        "name": "Cuenta de deposito / ahorro bancario", "issuer": "Banco", "denomination": "MXN",
        "tax_regime": "interest", "tax_summary": "Bank interest; retention on capital; some small-balance accounts may be exempt (confirm LISR Art. 54 and Art. 93).",
        "citations": ["LISR Arts. 54-55, 133-135"],
        "liquidity": {"class": "daily", "notes": "Available on demand subject to the account terms."},
    },
    "fibra": {
        "name": "FIBRA (CBFIs, real-estate trust certificates)", "issuer": "Fideicomiso (LISR Art. 187)", "denomination": "MXN",
        "tax_regime": "fibra", "tax_summary": "Distributed resultado fiscal: retention at the Art. 9 rate by the intermediary, accumulated as Art. 114 fr. II income and creditable; distributions above resultado fiscal are capital reimbursements that reduce cost; gains on sales through recognized markets are exempt for resident individuals.",
        "citations": ["LISR Art. 188 fr. V (distributions)", "LISR Art. 188 fr. IX (reimbursement of capital)", "LISR Art. 188 fr. X (exempt gains)"],
        "liquidity": {"class": "exchange_listed", "notes": "Listed on BMV/BIVA; T+2 settlement; liquidity varies by issuer."},
        "pfic_risk": "possible",
    },
    "bmv_equity": {
        "name": "Accion de emisora mexicana (BMV/BIVA)", "issuer": "Sociedad mexicana", "denomination": "MXN",
        "tax_regime": "article_129", "tax_summary": "Gains on qualifying exchange sales: 10% definitive (Art. 129). Dividends: accumulate with credit for corporate ISR plus additional 10% withholding on post-2013 CUFIN (Art. 140).",
        "citations": ["LISR Art. 129", "LISR Art. 140"],
        "liquidity": {"class": "exchange_listed", "notes": "T+2 settlement; liquidity varies by issuer."},
    },
    "bmv_etf": {
        "name": "TRAC / ETF listado en BMV o BIVA (fideicomiso mexicano)", "issuer": "Fideicomiso mexicano", "denomination": "MXN",
        "tax_regime": "article_129", "tax_summary": "Index-representative titles sold on the exchange: 10% definitive under Art. 129 fr. II; distributions follow their underlying character.",
        "citations": ["LISR Art. 129 fr. II"],
        "liquidity": {"class": "exchange_listed", "notes": "T+2 settlement."},
        "pfic_risk": "likely",
    },
    "sic_foreign_listing": {
        "name": "Valor extranjero listado en el SIC", "issuer": "Emisora extranjera", "denomination": "MXN quote of foreign security",
        "tax_regime": "article_129", "tax_summary": "Shares of foreign issuers listed on the Mexican exchange (SIC) are within Art. 129 fr. I: 10% definitive on gains computed in MXN by the intermediary. Dividends are foreign dividends (Art. 142 fr. V: accumulate plus additional 10%).",
        "citations": ["LISR Art. 129 fr. I", "LISR Art. 142 fr. V"],
        "liquidity": {"class": "exchange_listed", "notes": "Settles through the Mexican custody chain; SIC liquidity may be lower than the home market."},
        "us_estate_note": "US-domiciled issuers remain US-situs for US estate tax regardless of Mexican custody; see wealth.estate.",
    },
    "mutual_fund_debt": {
        "name": "Fondo de inversion en instrumentos de deuda", "issuer": "Fondo de inversion mexicano", "denomination": "MXN",
        "tax_regime": "interest_via_fund", "tax_summary": "The fund retains on capital and reports nominal/real interest to the investor; real interest accumulates annually.",
        "citations": ["LISR Arts. 87-89", "LISR Arts. 133-135"],
        "liquidity": {"class": "fund_redemption", "notes": "Redemption on the fund's liquidity schedule (daily to monthly); confirm the prospectus."},
        "pfic_risk": "likely",
    },
    "mutual_fund_equity": {
        "name": "Fondo de inversion de renta variable", "issuer": "Fondo de inversion mexicano", "denomination": "MXN",
        "tax_regime": "mixed_via_fund", "tax_summary": "Equity-gain portion follows Art. 129-type treatment reported by the fund; interest portion is real interest.",
        "citations": ["LISR Arts. 87-89", "LISR Art. 129"],
        "liquidity": {"class": "fund_redemption", "notes": "Redemption on the fund's liquidity schedule; confirm the prospectus."},
        "pfic_risk": "likely",
    },
    "afore_rcv": {
        "name": "AFORE subcuenta de retiro, cesantia en edad avanzada y vejez (RCV)", "issuer": "SAR / AFORE", "denomination": "MXN",
        "tax_regime": "retirement_sar", "tax_summary": "Mandatory contributions; withdrawals follow LISR Art. 93 exemptions and Art. 145 rules at retirement.",
        "citations": ["Ley del Seguro Social (1997) retirement provisions", "LISR Art. 93 fr. IV", "LISR Art. 145"],
        "liquidity": {"class": "locked_until_retirement", "notes": "Illiquid until the pension conditions of the Ley del Seguro Social are met; partial withdrawals only for unemployment or marriage under the conditions set by law and CONSAR. Confirm ages and weeks of contribution with the AFORE/CONSAR."},
    },
    "afore_vivienda": {
        "name": "Subcuenta de vivienda (INFONAVIT / FOVISSSTE)", "issuer": "INFONAVIT / FOVISSSTE", "denomination": "MXN",
        "tax_regime": "retirement_sar", "tax_summary": "Housing fund balance; used for housing credit or returned at retirement under the housing-fund law.",
        "citations": ["Ley del INFONAVIT", "Ley del ISSSTE"],
        "liquidity": {"class": "restricted_housing", "notes": "Not freely withdrawable: applied to a housing credit or released at pension under the applicable law."},
    },
    "afore_voluntary_short": {
        "name": "Aportaciones voluntarias de corto plazo (AFORE)", "issuer": "AFORE", "denomination": "MXN",
        "tax_regime": "retirement_voluntary", "tax_summary": "Deductible under Art. 151 fr. V only if the retirement permanence requirements are met; otherwise not deductible and gains are interest.",
        "citations": ["LISR Art. 151 fr. V", "Ley de los Sistemas de Ahorro para el Retiro"],
        "liquidity": {"class": "short_lockup", "notes": "Withdrawable after the AFORE's minimum holding period (set per AFORE/CONSAR; not assumed)."},
    },
    "afore_voluntary_long": {
        "name": "Aportaciones complementarias de retiro (AFORE, largo plazo)", "issuer": "AFORE", "denomination": "MXN",
        "tax_regime": "retirement_deductible", "tax_summary": "Deductible within the Art. 151 fr. V cap; early withdrawal is accumulable income (Chapter IX).",
        "citations": ["LISR Art. 151 fr. V"],
        "liquidity": {"class": "locked_until_retirement", "notes": "Available at retirement/pension or at age 65, or on invalidity/incapacity; early withdrawal becomes accumulable income."},
    },
    "ppr": {
        "name": "Plan Personal de Retiro (LISR Art. 151 fr. V)", "issuer": "Institucion autorizada", "denomination": "MXN",
        "tax_regime": "retirement_deductible", "tax_summary": "Deductible up to 10% of accumulable income capped at five annual UMAs, outside the global personal-deduction cap; withdrawal before age 65 (absent invalidity/incapacity) is accumulable income under Chapter IX with retention by the institution.",
        "citations": ["LISR Art. 151 fr. V", "LISR Art. 142 fr. XVIII (early withdrawal: deducted contributions and real interest, updated, are income)"],
        "liquidity": {"class": "locked_until_age_65", "notes": "Resources are for use at age 65 or invalidity/incapacity; earlier withdrawal is taxable and loses the deduction benefit.", "early_withdrawal_penalty": "Deducted contributions and returns become accumulable income; institutional retention applies (rate needs verification)."},
    },
    "art185_account": {
        "name": "Cuenta especial para el ahorro / seguro de pension / fondo identificable (LISR Art. 185)", "issuer": "Institucion autorizada", "denomination": "MXN",
        "tax_regime": "art185_deferral", "tax_summary": "Deposits up to $152,000 per year reduce the Art. 152 base; withdrawals and returns are accumulable when withdrawn, at a rate no higher than the deposit-year rate.",
        "citations": ["LISR Art. 185"],
        "liquidity": {"class": "deferral_lockup", "notes": "Fund shares under Art. 185 are custody-locked for five years (except death); account withdrawals become accumulable income.", "early_withdrawal_penalty": "Withdrawals are accumulable income in the year received."},
    },
    "foreign_brokerage": {
        "name": "Cuenta de corretaje en el extranjero", "issuer": "Intermediario extranjero", "denomination": "foreign currency",
        "tax_regime": "sic_listing_decides", "tax_summary": "Securities listed in the SIC keep the 10% definitive Art. 129 fr. I rate even when bought and sold through a foreign broker (SAT criterio normativo 37/ISR/N); the taxpayer computes the MXN gain (average cost, INPC update) and there is no withholding or constancia. Securities not listed in the SIC are Title IV Chapter IV (Arts. 119-124) income at progressive rates. Foreign dividends: Art. 142 fr. V plus Art. 5 credit.",
        "citations": ["LISR Art. 129 fr. I", "SAT criterio normativo 37/ISR/N (Anexo 7 RMF 2026)", "LISR Title IV Chapter IV (Arts. 119-124)", "LISR Art. 142 fr. V", "LISR Art. 5"],
        "liquidity": {"class": "exchange_listed", "notes": "Liquid through the foreign broker; repatriation and FX timing apply."},
    },
}

_ALIASES = {
    "cete": "cetes", "bonde": "bondes", "bondes_d": "bondes", "bondes_f": "bondes", "udibono": "udibonos",
    "mbono": "mbonos", "bonos_m": "mbonos", "m_bono": "mbonos", "pagare": "bank_pagare", "fibras": "fibra",
    "sic": "sic_foreign_listing", "trac": "bmv_etf", "etf_mx": "bmv_etf", "afore": "afore_rcv", "infonavit": "afore_vivienda",
    "fovissste": "afore_vivienda", "plan_personal_de_retiro": "ppr", "sociedad_de_inversion_deuda": "mutual_fund_debt",
    "sociedad_de_inversion_renta_variable": "mutual_fund_equity", "foreign_broker": "foreign_brokerage",
}


def instrument_types() -> dict[str, dict[str, Any]]:
    """Return the supported instrument classification table (read-only copy)."""
    return {key: dict(value) for key, value in _INSTRUMENTS.items()}


def _holding_value_mxn(holding: dict[str, Any], path: str, spec: dict[str, Any]) -> tuple[Decimal | None, list[str], dict[str, Any]]:
    detail: dict[str, Any] = {}
    if spec["denomination"] == "UDI" or "udis" in holding:
        if "udis" not in holding:
            return None, [f"{path}.udis"], detail
        udis = _decimal(holding["udis"], f"{path}.udis")
        missing = [f"{path}.{key}" for key in ("udi_value", "udi_date", "udi_source") if key not in holding]
        if missing:
            return None, missing, {"udis": format(udis, "f")}
        udi_value = _decimal(holding["udi_value"], f"{path}.udi_value")
        detail = {"udis": format(udis, "f"), "udi_value": format(udi_value, "f"), "udi_date": _date(holding["udi_date"], f"{path}.udi_date").isoformat(), "udi_source": _text(holding["udi_source"], f"{path}.udi_source")}
        return udis * udi_value, [], detail
    if "value_mxn" in holding:
        return _decimal(holding["value_mxn"], f"{path}.value_mxn"), [], detail
    if "value" in holding:
        currency = _text(holding.get("currency"), f"{path}.currency").upper()
        value = _decimal(holding["value"], f"{path}.value")
        if currency == "MXN":
            return value, [], detail
        missing = [f"{path}.{key}" for key in ("fx_to_mxn", "fx_source") if key not in holding]
        if missing:
            return None, missing, {"value": format(value, "f"), "currency": currency}
        fx = _decimal(holding["fx_to_mxn"], f"{path}.fx_to_mxn")
        detail = {"value": format(value, "f"), "currency": currency, "fx_to_mxn": format(fx, "f"), "fx_source": _text(holding["fx_source"], f"{path}.fx_source")}
        return value * fx, [], detail
    return None, [f"{path}.value_mxn"], detail


def classify_holdings(inputs: dict[str, Any]) -> dict[str, Any]:
    """Classify Mexican-resident holdings by tax regime and liquidity/lockup.

    ``holdings`` rows need ``id`` and ``type`` (see :func:`instrument_types`);
    valuation uses ``value_mxn``, ``value``+``currency`` (+``fx_to_mxn``/``fx_source``
    for non-MXN), or ``udis``+``udi_value``+``udi_date``+``udi_source`` for UDIBONOS.
    Alternatively a canonical ``household`` whose positions carry ``mx_instrument_type``.
    """
    holdings = inputs.get("holdings")
    assumptions: list[str] = []
    if holdings is None and isinstance(inputs.get("household"), dict):
        positions = inputs["household"].get("positions", [])
        if not isinstance(positions, list):
            raise ValueError("household.positions must be a list")
        holdings = [{"id": p.get("id"), "type": p.get("mx_instrument_type"), "value": p.get("value"), "currency": p.get("currency"),
                     **{k: p[k] for k in ("udis", "udi_value", "udi_date", "udi_source", "fx_to_mxn", "fx_source") if k in p}}
                    for p in positions if isinstance(p, dict)]
        assumptions.append("Mapped household positions using their mx_instrument_type field; positions without it are unclassified, not assumed.")
    if not isinstance(holdings, list) or not holdings:
        return _envelope("needs_input", {}, missing=["holdings (or household positions with mx_instrument_type)"],
                         sources=[_lisr("Titulo IV", "persona fisica investment income")])
    us_person = inputs.get("us_person")
    if us_person is not None and not isinstance(us_person, bool):
        raise ValueError("us_person must be a boolean")
    rows: list[dict[str, Any]] = []
    missing: list[str] = []
    warnings: list[str] = []
    totals: dict[str, Decimal] = {}
    seen: set[str] = set()
    for index, holding in enumerate(holdings):
        if not isinstance(holding, dict):
            raise ValueError(f"holdings[{index}] must be an object")
        path = f"holdings[{index}]"
        holding_id = _text(holding.get("id"), f"{path}.id")
        if holding_id in seen:
            raise ValueError(f"duplicate holding id: {holding_id}")
        seen.add(holding_id)
        raw_type = holding.get("type")
        if raw_type is None:
            missing.append(f"{path}.type")
            rows.append({"id": holding_id, "classification": None, "reason": "instrument type unknown; not guessed"})
            continue
        key = _text(raw_type, f"{path}.type").lower().replace("-", "_").replace(" ", "_")
        key = _ALIASES.get(key, key)
        if key not in _INSTRUMENTS:
            missing.append(f"{path}.type (supported: {', '.join(sorted(_INSTRUMENTS))})")
            rows.append({"id": holding_id, "classification": None, "reason": f"unsupported instrument type {raw_type!r}"})
            continue
        spec = _INSTRUMENTS[key]
        value, value_missing, detail = _holding_value_mxn(holding, path, spec)
        missing.extend(value_missing)
        row = {"id": holding_id, "type": key, **{k: spec[k] for k in ("name", "tax_regime", "tax_summary", "citations")},
               "liquidity": dict(spec["liquidity"]), "value_mxn": None if value is None else _money(value), "valuation": detail}
        if us_person is True and spec.get("pfic_risk"):
            row["pfic_warning"] = f"PFIC status {spec['pfic_risk']} for a US person (IRC 1291-1298; Form 8621). Confirm with a US tax adviser."
        if spec.get("us_estate_note"):
            row["us_estate_note"] = spec["us_estate_note"]
        if value is not None:
            liquidity = spec["liquidity"]["class"]
            totals[liquidity] = totals.get(liquidity, Decimal(0)) + value
        rows.append(row)
    if us_person is None and any(_INSTRUMENTS.get(r.get("type") or "", {}).get("pfic_risk") for r in rows):
        missing.append("us_person (needed to decide PFIC warnings)")
    if any(r.get("type") in {"afore_voluntary_short", "ppr", "art185_account"} for r in rows):
        warnings.append("Lockup periods and early-withdrawal retentions depend on the contract and current CONSAR/SAT rules; they are not assumed.")
    warnings.append("Totals cover valued holdings only; unvalued holdings are excluded, not zero.")
    status = "partial" if missing else "ready"
    locked = {"locked_until_retirement", "locked_until_age_65", "restricted_housing", "deferral_lockup"}
    result = {
        "holdings": rows,
        "value_by_liquidity_mxn": {k: _money(v) for k, v in sorted(totals.items())},
        "liquid_or_exchange_mxn": _money(sum((v for k, v in totals.items() if k not in locked), Decimal(0))),
        "locked_or_restricted_mxn": _money(sum((v for k, v in totals.items() if k in locked), Decimal(0))),
        "scope": "Classification describes the general regime; the intermediary's constancia controls the reported amounts.",
    }
    sources = [_lisr("articulos 129, 133-135, 142, 151, 185, 188", "instrument regimes"),
               {"title": "Banxico SIE, valor de la UDI", "url": BANXICO_UDI_URL}, {"title": "CONSAR", "url": CONSAR_URL}]
    return _envelope(status, result, missing=missing, warnings=warnings, sources=sources, assumptions=assumptions)


# ---------------------------------------------------------------------------
# 2. Real interest (LISR Arts. 133-136)
# ---------------------------------------------------------------------------

def real_interest(inputs: dict[str, Any]) -> dict[str, Any]:
    """Compute nominal vs real interest, retention credit and annual ISR effect.

    Each ``accounts`` row: ``id``, ``nominal_interest_mxn``, ``average_daily_balance_mxn``,
    ``days``, and either ``inflation_factor`` or ``inpc_first_month`` + ``inpc_last_month``
    (INPC of the first and most recent month of the investment period, LISR Art. 134);
    ``retention_withheld_mxn`` from the constancia; optional ``udi_adjustment_mxn``,
    ``constancia_real_interest_mxn`` for reconciliation.
    """
    tax_year = inputs.get("tax_year")
    accounts = inputs.get("accounts")
    missing = [key for key, value in (("tax_year", tax_year), ("accounts", accounts)) if value is None]
    sources = [_lisr("articulos 133-136", "real interest = nominal interest less inflation adjustment on average daily balance; loss rules; retention on capital"),
               _lisr("articulo 55 fraccion II", "constancia of nominal and real interest and retention by February 15"),
               {"title": "INEGI, Indice Nacional de Precios al Consumidor", "url": INEGI_INPC_URL}]
    if missing:
        return _envelope("needs_input", {}, missing=missing, sources=sources)
    year = _year(tax_year)
    if not isinstance(accounts, list) or not accounts:
        raise ValueError("accounts must be a nonempty list")
    params = _Params(inputs)
    rate = params.decimal("interest_annual_retention_rate", year)
    rows: list[dict[str, Any]] = []
    warnings: list[str] = []
    total_nominal = total_adjustment = total_real = total_loss = total_retention = Decimal(0)
    retention_known = True
    for index, account in enumerate(accounts):
        if not isinstance(account, dict):
            raise ValueError(f"accounts[{index}] must be an object")
        path = f"accounts[{index}]"
        account_id = _text(account.get("id"), f"{path}.id")
        needed = [f"{path}.{k}" for k in ("nominal_interest_mxn", "average_daily_balance_mxn", "days") if k not in account]
        if "inflation_factor" not in account and not ("inpc_first_month" in account and "inpc_last_month" in account):
            needed.append(f"{path}.inflation_factor or {path}.inpc_first_month+inpc_last_month")
        if needed:
            missing.extend(needed)
            continue
        nominal = _decimal(account["nominal_interest_mxn"], f"{path}.nominal_interest_mxn")
        udi_adjustment = _decimal(account.get("udi_adjustment_mxn", 0), f"{path}.udi_adjustment_mxn")
        balance = _decimal(account["average_daily_balance_mxn"], f"{path}.average_daily_balance_mxn")
        days = account["days"]
        if isinstance(days, bool) or not isinstance(days, int) or not 1 <= days <= 366:
            raise ValueError(f"{path}.days must be an integer between 1 and 366")
        if "inflation_factor" in account:
            factor = _decimal(account["inflation_factor"], f"{path}.inflation_factor", nonnegative=False)
            factor_basis = "caller-supplied inflation factor"
        else:
            first = _decimal(account["inpc_first_month"], f"{path}.inpc_first_month")
            last = _decimal(account["inpc_last_month"], f"{path}.inpc_last_month")
            if first == 0:
                raise ValueError(f"{path}.inpc_first_month must be greater than zero")
            factor = last / first - 1
            factor_basis = "INPC most recent month / INPC first month - 1 (LISR Art. 134)"
        income = nominal + udi_adjustment
        adjustment = balance * factor
        real = income - adjustment
        row: dict[str, Any] = {"id": account_id, "institution": account.get("institution"), "nominal_interest_mxn": _money(nominal),
                               "udi_adjustment_mxn": _money(udi_adjustment), "average_daily_balance_mxn": _money(balance), "days": days,
                               "inflation_factor": format(factor.quantize(Decimal("0.000001")), "f"), "inflation_factor_basis": factor_basis,
                               "inflation_adjustment_mxn": _money(adjustment),
                               "real_interest_mxn": _money(max(real, Decimal(0))), "real_interest_loss_mxn": _money(max(-real, Decimal(0)))}
        total_nominal += income
        total_adjustment += adjustment
        if real >= 0:
            total_real += real
        else:
            total_loss += -real
        if rate is not None:
            row["expected_retention_estimate_mxn"] = _money(balance * rate * days / 365)
        if "retention_withheld_mxn" in account:
            withheld = _decimal(account["retention_withheld_mxn"], f"{path}.retention_withheld_mxn")
            row["retention_withheld_mxn"] = _money(withheld)
            total_retention += withheld
            if rate is not None and abs(withheld - balance * rate * days / 365) > Decimal("1.00") + withheld * Decimal("0.05"):
                warnings.append(f"{account_id}: constancia retention differs from the rate x balance x days/365 estimate; the constancia amount is credited.")
        else:
            retention_known = False
            missing.append(f"{path}.retention_withheld_mxn (from the constancia de retenciones)")
        if "constancia_real_interest_mxn" in account:
            reported = _decimal(account["constancia_real_interest_mxn"], f"{path}.constancia_real_interest_mxn", nonnegative=False)
            difference = real - reported
            row["constancia_reconciliation"] = {"reported_real_interest_mxn": _money(reported), "difference_mxn": _money(difference),
                                                "matches": abs(difference) <= Decimal("1.00")}
            if abs(difference) > Decimal("1.00"):
                warnings.append(f"{account_id}: computed real interest differs from the constancia; the constancia is what the institution reported to SAT (Art. 55).")
        rows.append(row)
    missing.extend(params.missing)
    if not rows:
        return _envelope("needs_input", {"accounts": rows, **params.report()}, missing=missing, sources=sources + params.sources,
                         assumptions=params.assumptions)
    estimate, estimate_missing = _incremental_isr(inputs, params, year, total_real)
    missing.extend(m for m in estimate_missing if m not in missing)
    for item in params.missing:
        if item not in missing:
            missing.append(item)
    effect = None
    if estimate is not None:
        effect = {**_public(estimate)}
        if retention_known:
            effect["retention_credited_mxn"] = _money(total_retention)
            effect["net_isr_after_retention_mxn"] = _money(estimate["_change"] - total_retention)
            effect["note"] = "Negative net means retention exceeds the estimated ISR on real interest (a potential balance in favor), subject to the full annual return."
    definitive_limit = params.decimal("art135_definitive_option_limit_mxn", year)
    warnings.append("Art. 134: a real-interest loss may reduce other income of the year except Chapters I (salaries) and II (business); the unused part carries forward five years, updated for inflation. It is reported, not applied, in this estimate.")
    if total_loss > 0:
        warnings.append("One or more accounts show a real-interest loss; confirm the offset against eligible income with a contador.")
    result = {
        "tax_year": year, "currency": "MXN", "accounts": rows,
        "totals": {"nominal_interest_mxn": _money(total_nominal), "inflation_adjustment_mxn": _money(total_adjustment),
                   "real_interest_mxn": _money(total_real), "real_interest_loss_mxn": _money(total_loss),
                   "retention_credited_mxn": _money(total_retention) if retention_known else None},
        "retention_rate": None if rate is None else _rate(rate),
        "annual_isr_effect": effect,
        "definitive_retention_option": {"limit_mxn": _money(definitive_limit) if definitive_limit is not None else None,
            "within_limit": None if definitive_limit is None else total_nominal <= definitive_limit,
            "condition": "Available only if the person's only accumulable income is interest (Chapter VI) and it does not exceed the limit (Art. 135).",
            "note": "Art. 150 also allows salaried people with income up to $400,000 and real interest up to $100,000 subject to retention to skip the annual return."},
        **params.report(),
        "scope": "Per-account arithmetic from supplied balances and INPC; Art. 134 day-proration for partial months is only applied if reflected in the supplied factor.",
    }
    status = "needs_input" if params.needs_verification else ("partial" if missing else "ready")
    return _envelope(status, result, missing=missing, warnings=params.warnings + warnings, sources=sources + params.sources,
                     assumptions=params.assumptions + ["Real interest per account is floored at zero; losses are summed separately."])


# ---------------------------------------------------------------------------
# 3. Personal deductions and retirement contributions (Arts. 151, 185)
# ---------------------------------------------------------------------------

def personal_deductions(inputs: dict[str, Any]) -> dict[str, Any]:
    """Apply the Art. 151 global cap and PPR cap, and estimate the saving of an extra contribution.

    Inputs: ``tax_year``, ``total_income_mxn`` (including exempt income),
    ``accumulable_income_mxn``, ``deductions`` = {``general_mxn`` (items subject
    to the global cap), ``retirement_151v_mxn`` (existing fr. V contributions),
    ``art185_mxn`` (existing Art. 185 deposits), ``outside_global_cap_mxn``
    (optional: already-capped amounts the caller has confirmed are outside the cap)},
    ``proposed_ppr_contribution_mxn`` and/or ``proposed_art185_mxn``, and
    ``taxable_income_before_mxn`` (after existing deductions) or ``marginal_rate``.
    """
    sources = [_lisr("articulo 151", "personal deductions; fr. V retirement contributions capped at 10% of accumulable income and five annual UMAs; global cap lesser of five annual UMAs or 15% of total income, not applicable to fr. V"),
               _lisr("articulo 185", "special savings accounts deduction up to $152,000; accumulable on withdrawal"),
               {"title": "SAT, Deducciones personales", "url": SAT_DEDUCCIONES_URL}]
    required = ("tax_year", "total_income_mxn", "accumulable_income_mxn", "deductions")
    missing = [key for key in required if key not in inputs]
    if missing:
        return _envelope("needs_input", {}, missing=missing, sources=sources)
    year = _year(inputs["tax_year"])
    total_income = _decimal(inputs["total_income_mxn"], "total_income_mxn")
    accumulable = _decimal(inputs["accumulable_income_mxn"], "accumulable_income_mxn")
    if accumulable > total_income:
        raise ValueError("accumulable_income_mxn cannot exceed total_income_mxn")
    deductions = inputs["deductions"]
    if not isinstance(deductions, dict):
        raise ValueError("deductions must be an object")
    dmissing = [f"deductions.{k}" for k in ("general_mxn", "retirement_151v_mxn", "art185_mxn") if k not in deductions]
    if dmissing:
        return _envelope("needs_input", {}, missing=dmissing, sources=sources,
                         warnings=["Existing deductions are required; absence is not treated as zero because it changes the remaining room."])
    general = _decimal(deductions["general_mxn"], "deductions.general_mxn")
    existing_151v = _decimal(deductions["retirement_151v_mxn"], "deductions.retirement_151v_mxn")
    existing_185 = _decimal(deductions["art185_mxn"], "deductions.art185_mxn")
    outside = _decimal(deductions.get("outside_global_cap_mxn", 0), "deductions.outside_global_cap_mxn")
    params = _Params(inputs)
    uma_annual = params.decimal("uma_annual_mxn", year)
    limit_185 = params.decimal("art185_annual_limit_mxn", year)
    if uma_annual is None:
        return _envelope("needs_input", params.report(), missing=params.missing, sources=sources,
                         warnings=["The annual UMA for this year is not verified in the parameter table; supply it with its INEGI/DOF source."],
                         assumptions=params.assumptions)
    five_uma = uma_annual * 5
    global_cap = min(five_uma, total_income * Decimal("0.15"))
    general_allowed = min(general, global_cap)
    ppr_cap = min(accumulable * Decimal("0.10"), five_uma)
    allowed_151v = min(existing_151v, ppr_cap)
    ppr_room = max(ppr_cap - existing_151v, Decimal(0))
    room_185 = max(limit_185 - existing_185, Decimal(0)) if limit_185 is not None else None
    warnings: list[str] = [
        "The Art. 151 fr. V statutory text caps contributions at five general minimum wages elevated to the year; under the 2016 constitutional desindexation decree that reference is read as five annual UMAs. Confirm with SAT guidance.",
        "The annual UMA used is the value in force for the tax year (published in January, effective February 1); January uses the prior year's UMA. Confirm the value SAT applies in the annual return.",
    ]
    if general > global_cap:
        warnings.append("General deductions exceed the global cap; the excess is not deductible.")
    if existing_151v > ppr_cap:
        warnings.append("Existing Art. 151 fr. V contributions already exceed the cap; the excess is not deductible.")
    missing = []
    scenarios: dict[str, Any] = {}
    for key, room, label in (("proposed_ppr_contribution_mxn", ppr_room, "art151_v"), ("proposed_art185_mxn", room_185, "art185")):
        if key not in inputs:
            continue
        proposed = _decimal(inputs[key], key)
        if room is None:
            missing.append(f"room for {label}")
            continue
        deductible = min(proposed, room)
        estimate, estimate_missing = _incremental_isr(inputs, params, year, deductible, direction=-1)
        missing.extend(m for m in estimate_missing if m not in missing)
        scenarios[label] = {
            "proposed_mxn": _money(proposed), "deductible_mxn": _money(deductible), "non_deductible_excess_mxn": _money(proposed - deductible),
            "estimated_isr_saving_mxn": None if estimate is None else _money(-estimate["_change"]),
            "estimate": _public(estimate),
            "deadline": f"{year}-12-31" if label == "art151_v" else f"before filing the {year} annual return (April {year + 1})",
            "tradeoff": ("Deduction is permanent if held to age 65 or invalidity; earlier withdrawal is accumulable income with retention."
                         if label == "art151_v" else
                         "Deferral, not exemption: deposits and returns are accumulable when withdrawn, at a rate no higher than the deposit-year rate; Art. 185 fund shares are locked for five years."),
        }
    missing.extend(m for m in params.missing if m not in missing)
    if not scenarios:
        warnings.append("No proposed contribution was supplied; only caps and remaining room are reported.")
    result = {
        "tax_year": year, "currency": "MXN",
        "caps": {"five_annual_umas_mxn": _money(five_uma), "fifteen_percent_total_income_mxn": _money(total_income * Decimal("0.15")),
                 "global_cap_mxn": _money(global_cap), "art151_v_cap_mxn": _money(ppr_cap),
                 "art185_limit_mxn": None if limit_185 is None else _money(limit_185)},
        "allowed": {"general_mxn": _money(general_allowed), "art151_v_mxn": _money(allowed_151v), "outside_global_cap_mxn": _money(outside),
                    "art185_mxn": None if limit_185 is None else _money(min(existing_185, limit_185)),
                    "total_personal_deductions_mxn": _money(general_allowed + allowed_151v + outside)},
        "remaining_room": {"art151_v_mxn": _money(ppr_room), "art185_mxn": None if room_185 is None else _money(room_185)},
        "contribution_scenarios": scenarios,
        **params.report(),
        "scope": "Caps and marginal estimates only; donation (7%) and tuition-decree limits must be applied by the caller before passing outside_global_cap_mxn.",
    }
    status = "needs_input" if params.needs_verification else ("partial" if missing else "ready")
    return _envelope(status, result, missing=missing, warnings=params.warnings + warnings, sources=sources + params.sources,
                     assumptions=params.assumptions + ["Existing deductions are taken as supplied and assumed to meet CFDI and payment-method requirements."])


# ---------------------------------------------------------------------------
# 4. Foreign securities held at foreign brokers
# ---------------------------------------------------------------------------

def foreign_securities(inputs: dict[str, Any]) -> dict[str, Any]:
    """Scenario for foreign securities sold through a foreign broker: MXN gains incl. FX, dividends, credit.

    Whether the security is listed in the SIC decides the regime, not the broker or venue: SIC-listed
    securities keep the 10% definitive Art. 129 fr. I rate (SAT criterio normativo 37/ISR/N); others
    are Title IV Chapter IV income at progressive rates.

    ``sales`` rows: ``id``, ``currency``, ``proceeds``, ``fx_sale``, ``cost``, ``fx_acquisition``,
    ``acquired_on``, ``sold_on``, ``sic_listed`` (bool), optional ``security_type`` (``share`` |
    ``equity_etf`` | ``other_etf``) and ``cost_update_factor`` (INPC update, Art. 124).
    ``dividends`` rows: ``id``, ``currency``, ``gross``, ``withheld``, ``fx``, ``paid_on``,
    ``source_country``, optional ``w8ben_on_file`` (bool, for US-source dividends).
    """
    sources = [_lisr("articulo 129 fraccion I", "10% definitive on shares of foreign issuers listed on Mexican exchanges, including the SIC"),
               {"title": "SAT Anexo 7 RMF 2026, criterio normativo 37/ISR/N (SIC-listed shares sold through foreign intermediaries)",
                "url": "https://www.sat.gob.mx/minisitio/NormatividadRMFyRGCE/documentos2026/rmf/anexos/Anexo_7_RMF2026-09012026.pdf"},
               _lisr("Titulo IV Capitulo IV (articulos 119-128)", "gains on alienation of property, cost updating and annualization"),
               _lisr("articulo 142 fraccion V", "foreign dividends accumulate plus additional 10% definitive tax"),
               _lisr("articulo 5", "credit for foreign income tax on foreign-source income that is taxable in Mexico"),
               {"title": "US-Mexico Income Tax Convention and Protocols", "url": TREATY_URL}]
    tax_year = inputs.get("tax_year")
    sales = inputs.get("sales", [])
    dividends = inputs.get("dividends", [])
    if tax_year is None or (not sales and not dividends):
        return _envelope("needs_input", {}, missing=[m for m, bad in (("tax_year", tax_year is None), ("sales or dividends", not sales and not dividends)) if bad], sources=sources)
    year = _year(tax_year)
    if not isinstance(sales, list) or not isinstance(dividends, list):
        raise ValueError("sales and dividends must be lists")
    params = _Params(inputs)
    missing: list[str] = []
    warnings: list[str] = []
    assumptions: list[str] = ["MXN cost uses the exchange rate at acquisition and MXN proceeds use the rate at sale; the FX effect is part of the taxable gain in MXN."]
    sale_rows: list[dict[str, Any]] = []
    total_gain = Decimal(0)
    sic_gain = Decimal(0)
    for index, sale in enumerate(sales):
        if not isinstance(sale, dict):
            raise ValueError(f"sales[{index}] must be an object")
        path = f"sales[{index}]"
        needed = [f"{path}.{k}" for k in ("id", "currency", "proceeds", "fx_sale", "cost", "fx_acquisition", "acquired_on", "sold_on", "sic_listed") if k not in sale]
        if needed:
            missing.extend(needed)
            continue
        if sale.get("venue") in {"sic", "bmv", "biva"}:
            raise ValueError(f"{path} is a Mexican-exchange sale; use the Article 129 tax scenario instead")
        proceeds = _decimal(sale["proceeds"], f"{path}.proceeds")
        cost = _decimal(sale["cost"], f"{path}.cost")
        fx_sale = _decimal(sale["fx_sale"], f"{path}.fx_sale")
        fx_acq = _decimal(sale["fx_acquisition"], f"{path}.fx_acquisition")
        acquired = _date(sale["acquired_on"], f"{path}.acquired_on")
        sold = _date(sale["sold_on"], f"{path}.sold_on")
        if acquired > sold:
            raise ValueError(f"{path}.acquired_on cannot be after sold_on")
        if sold.year != year:
            raise ValueError(f"{path}.sold_on is outside tax_year")
        cost_mxn = cost * fx_acq
        update = _decimal(sale.get("cost_update_factor", 1), f"{path}.cost_update_factor")
        if update < 1:
            raise ValueError(f"{path}.cost_update_factor must be at least 1")
        updated_cost = cost_mxn * update
        proceeds_mxn = proceeds * fx_sale
        gain = proceeds_mxn - updated_cost
        local_gain_at_sale_fx = (proceeds - cost) * fx_sale
        fx_component = cost * (fx_sale - fx_acq)
        if not isinstance(sale["sic_listed"], bool):
            raise ValueError(f"{path}.sic_listed must be true or false")
        security_type = sale.get("security_type", "share")
        if security_type not in {"share", "equity_etf", "other_etf"}:
            raise ValueError(f"{path}.security_type must be share, equity_etf or other_etf")
        regime = "article_129" if sale["sic_listed"] else "progressive"
        if regime == "article_129":
            sic_gain += gain
            if security_type == "equity_etf":
                warnings.append(f"{_text(sale['id'], f'{path}.id')}: criterio 37/ISR/N names shares; applying it to a SIC-listed equity ETF through a foreign broker relies on Art. 129 fr. II and is contested.")
            elif security_type == "other_etf":
                warnings.append(f"{_text(sale['id'], f'{path}.id')}: a non-equity ETF (bonds, commodities) sold through a foreign broker may fall outside Art. 129 even if SIC-listed; the 10% here is doubtful.")
        else:
            total_gain += gain
        if "cost_update_factor" not in sale:
            warnings.append(f"{_text(sale['id'], f'{path}.id')}: cost was not updated for inflation (INPC); the gain is overstated if an update applies.")
        sale_rows.append({"id": _text(sale["id"], f"{path}.id"), "currency": _text(sale["currency"], f"{path}.currency").upper(),
                          "proceeds_mxn": _money(proceeds_mxn), "cost_mxn_at_acquisition_fx": _money(cost_mxn),
                          "cost_update_factor": format(update, "f"), "updated_cost_mxn": _money(updated_cost),
                          "gain_or_loss_mxn": _money(gain), "components": {"local_currency_gain_at_sale_fx_mxn": _money(local_gain_at_sale_fx),
                          "fx_effect_on_cost_mxn": _money(fx_component), "inflation_update_mxn": _money(cost_mxn - updated_cost)},
                          "years_held": str(round(Decimal((sold - acquired).days) / Decimal("365.25"), 2)),
                          "regime": regime})
    dividend_rows: list[dict[str, Any]] = []
    total_div = total_withheld = Decimal(0)
    treaty_rate = params.decimal("us_mx_treaty_portfolio_dividend_rate", year)
    additional_rate = params.decimal("foreign_dividend_additional_rate", year)
    for index, dividend in enumerate(dividends):
        if not isinstance(dividend, dict):
            raise ValueError(f"dividends[{index}] must be an object")
        path = f"dividends[{index}]"
        needed = [f"{path}.{k}" for k in ("id", "currency", "gross", "withheld", "fx", "paid_on", "source_country") if k not in dividend]
        if needed:
            missing.extend(needed)
            continue
        gross = _decimal(dividend["gross"], f"{path}.gross")
        withheld = _decimal(dividend["withheld"], f"{path}.withheld")
        if withheld > gross:
            raise ValueError(f"{path}.withheld cannot exceed gross")
        fx = _decimal(dividend["fx"], f"{path}.fx")
        country = _text(dividend["source_country"], f"{path}.source_country").upper()
        gross_mxn, withheld_mxn = gross * fx, withheld * fx
        total_div += gross_mxn
        total_withheld += withheld_mxn
        row = {"id": _text(dividend["id"], f"{path}.id"), "source_country": country, "gross_mxn": _money(gross_mxn), "withheld_mxn": _money(withheld_mxn),
               "withholding_rate": _rate((withheld / gross).quantize(Decimal("0.0001"))) if gross else "0",
               "additional_10pct_definitive_mxn": None if additional_rate is None else _money(gross_mxn * additional_rate),
               "additional_10pct_due": "by the 17th of the month after receipt (Art. 142 fr. V)"}
        if country in {"US", "USA"}:
            w8 = dividend.get("w8ben_on_file")
            if w8 is None:
                missing.append(f"{path}.w8ben_on_file")
            elif w8 is True and treaty_rate is not None and gross and withheld / gross > treaty_rate + Decimal("0.0001"):
                warnings.append(f"{row['id']}: US withholding exceeds the 10% treaty portfolio rate despite a W-8BEN; excess is generally a US refund claim, not a Mexican credit.")
            elif w8 is False:
                warnings.append(f"{row['id']}: without a W-8BEN claiming treaty benefits the US statutory 30% rate generally applies; only the treaty-rate portion is typically creditable.")
        dividend_rows.append(row)
    missing.extend(m for m in params.missing if m not in missing)
    taxable_addition = max(total_gain, Decimal(0)) + total_div
    estimate, estimate_missing = _incremental_isr(inputs, params, year, taxable_addition)
    missing.extend(m for m in estimate_missing if m not in missing)
    credit = None
    if estimate is not None and total_div > 0 and taxable_addition > 0:
        attributable = estimate["_change"] * total_div / taxable_addition
        credit_value = min(total_withheld, max(attributable, Decimal(0)))
        credit = {"foreign_tax_withheld_mxn": _money(total_withheld), "mexican_isr_attributable_to_dividends_mxn": _money(attributable),
                  "estimated_credit_mxn": _money(credit_value), "uncredited_foreign_tax_mxn": _money(total_withheld - credit_value),
                  "method": "Art. 5 credit limited to Mexican progressive ISR proportionally attributable to the dividends; not applied against the additional 10%"}
    warnings.extend([
        "Losses on foreign share sales are not netted beyond the year in this scenario; Chapter IV loss rules are narrower than Article 129 and are not modeled.",
        "Art. 120 annualization of gains (dividing by years held, up to 20) is not modeled; accumulating the whole gain in one year is a conservative upper estimate.",
        "Whether the foreign withholding reduces the base of the additional 10% on foreign dividends is not settled here; the gross amount is used.",
    ])
    if total_gain < 0:
        warnings.append("The net result on sales is a loss; it is not deducted from other income in this estimate.")
    if sale_rows and any(row["regime"] == "article_129" for row in sale_rows):
        warnings.append("SIC-listed sales through a foreign broker: no withholding or constancia is issued; the gain must be computed (average cost, INPC update) and declared in the annual return. Article 129 losses offset only Article 129 gains.")
    result = {
        "tax_year": year, "currency": "MXN", "sales": sale_rows, "dividends": dividend_rows,
        "totals": {"net_gain_or_loss_mxn": _money(total_gain),
                   "article_129_net_gain_or_loss_mxn": _money(sic_gain),
                   "article_129_tax_mxn": _money(max(sic_gain, Decimal(0)) * Decimal("0.10")), "dividends_gross_mxn": _money(total_div), "foreign_tax_withheld_mxn": _money(total_withheld),
                   "additional_10pct_dividend_tax_mxn": None if additional_rate is None else _money(total_div * additional_rate)},
        "progressive_isr_scenario": _public(estimate),
        "foreign_tax_credit": credit,
        "net_estimated_mexican_tax_mxn": None if estimate is None or additional_rate is None else _money(
            estimate["_change"] - (Decimal(credit["estimated_credit_mxn"]) if credit else Decimal(0)) + total_div * additional_rate
            + max(sic_gain, Decimal(0)) * Decimal("0.10")),
        "flags": [CONSULT_FLAG],
        "execution_ready": False,
        **params.report(),
        "scope": "Scenario with explicit assumptions for a Mexican resident; not a determination of the applicable chapter or credit.",
    }
    status = "needs_input" if params.needs_verification or (not sale_rows and not dividend_rows) else ("partial" if missing else "ready")
    return _envelope(status, result, missing=missing, warnings=params.warnings + warnings, sources=sources + params.sources,
                     assumptions=params.assumptions + assumptions)


# ---------------------------------------------------------------------------
# 6. Tax calendar
# ---------------------------------------------------------------------------

def tax_calendar(inputs: dict[str, Any]) -> dict[str, Any]:
    """Return dated Mexico (and optional US-person) deadlines for a tax year as monitor-ready data.

    Inputs: ``tax_year``; optional ``as_of`` (adds days_until), and booleans
    ``business_or_professional_income``, ``rental_income``, ``foreign_dividends``, ``us_person``.
    """
    if "tax_year" not in inputs:
        return _envelope("needs_input", {}, missing=["tax_year"], sources=[_lisr("articulo 150", "annual return in April")])
    year = _year(inputs["tax_year"])
    as_of = _date(inputs["as_of"], "as_of") if "as_of" in inputs else None
    flags = {}
    for key in ("business_or_professional_income", "rental_income", "foreign_dividends", "us_person"):
        value = inputs.get(key)
        if value is not None and not isinstance(value, bool):
            raise ValueError(f"{key} must be a boolean")
        flags[key] = value
    items: list[dict[str, Any]] = []

    def add(item_id: str, when: date, title: str, basis: str, applies: Any, status: str = "verified", note: str | None = None) -> None:
        item = {"id": item_id, "kind": "deadline", "jurisdiction": "US" if item_id.startswith("us_") else "MX", "date": when.isoformat(),
                "title": title, "basis": basis, "applies": applies, "verification": status}
        if note:
            item["note"] = note
        if as_of is not None:
            item["days_until"] = (when - as_of).days
        items.append(item)

    add("mx_ppr_151v_contributions", date(year, 12, 31), f"Last day for {year} Art. 151 fr. V (PPR / complementary retirement) contributions and for paying deductible personal expenses",
        "LISR Art. 151 (deductions of the year paid in the year)", True)
    add("mx_constancias_intereses", date(year + 1, 2, 15), f"Financial institutions deliver {year} interest constancias (nominal, real, loss, retention)",
        "LISR Art. 55 fr. II", True)
    add("mx_fibra_distribution_cutoff", date(year + 1, 3, 15), f"FIBRA trustees must distribute {year} resultado fiscal by this date or pay tax on the difference",
        "LISR Art. 188 fr. VIII", True, note="Informational for FIBRA holders.")
    add("mx_annual_return", date(year + 1, 4, 30), f"Annual return (declaracion anual) for {year}, personas fisicas",
        "LISR Art. 150 (month of April of the following year)", True,
        note="If the last day is not a business day, CFF Art. 12 moves it to the next business day; SAT may announce facilities.")
    add("mx_art185_deposits", date(year + 1, 4, 30), f"Art. 185 deposits made before filing the {year} return can be applied to {year}",
        "LISR Art. 185 (deposits before the return is filed)", True, note="The effective cut-off is the actual filing date, at the latest the April deadline.")
    monthly_basis = []
    if flags["business_or_professional_income"]:
        monthly_basis.append(("mx_provisional_business", "Monthly provisional ISR payment (business/professional income)", "LISR Art. 106"))
    if flags["rental_income"]:
        monthly_basis.append(("mx_provisional_rental", "Provisional ISR payment on rental income", "LISR Art. 116"))
    if flags["foreign_dividends"]:
        monthly_basis.append(("mx_foreign_dividend_10pct", "Additional 10% on foreign dividends received in the prior month", "LISR Art. 142 fr. V"))
    for item_id, title, basis in monthly_basis:
        for month in range(1, 13):
            due = date(year + (month == 12), 1 if month == 12 else month + 1, 17)
            add(f"{item_id}_{year}_{month:02d}", due, f"{title} for {year}-{month:02d}", basis + " (17th of the following month)",
                True if item_id != "mx_foreign_dividend_10pct" else "if foreign dividends were received that month",
                note="Rental taxpayers with low income may qualify for quarterly payments; confirm under Art. 116." if item_id == "mx_provisional_rental" else None)
    if flags["us_person"]:
        add("us_form_1040", date(year + 1, 4, 15), f"US federal return and payment for {year}", "IRC 6072(a); Treas. Reg. 1.6081-5 for taxpayers abroad", True, "statutory",
            "US persons abroad get an automatic filing extension to June 15, but interest runs on unpaid tax from April 15.")
        add("us_form_1040_abroad", date(year + 1, 6, 15), f"Automatic extended US filing date for US persons living abroad ({year})", "Treas. Reg. 1.6081-5(a)(5)", True, "statutory")
        add("us_fbar", date(year + 1, 4, 15), f"FBAR (FinCEN 114) for {year} foreign accounts over the threshold; automatic extension to October 15",
            "31 CFR 1010.306(c)", True, "statutory")
    unknown = [k for k, v in flags.items() if v is None]
    missing = [f"{k} (true/false) to include conditional deadlines" for k in unknown]
    items.sort(key=lambda item: (item["date"], item["id"]))
    result = {"tax_year": year, "items": items, "monitor_hint": "Each item is a dated deadline; a monitor can alert when days_until falls below a threshold."}
    return _envelope("partial" if missing else "ready", result, missing=missing,
                     warnings=["Dates are statutory defaults; SAT facilities, holidays and weekend rules can move them."],
                     sources=[_lisr("articulos 55, 106, 116, 142, 150, 151, 185, 188", "deadline provisions")])


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------

_TASKS = {
    "mx_holdings": classify_holdings,
    "mx_interest": real_interest,
    "mx_deductions": personal_deductions,
    "mx_foreign": foreign_securities,
    "mx_calendar": tax_calendar,
}


def run(task: str, inputs: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    """Service entry point for the Mexico-resident tasks listed in ``_TASKS``."""
    if not isinstance(task, str) or task not in _TASKS:
        raise ValueError(f"mexico.run supports tasks {sorted(_TASKS)}")
    if not isinstance(inputs, dict) or not isinstance(context, dict):
        raise ValueError("inputs and context must be objects")
    working = dict(inputs)
    assumptions: list[str] = []
    if task == "mx_holdings" and "holdings" not in working and "household" not in working and context.get("household") is not None:
        working["household"] = context.get("household")
        assumptions.append("Used the stored household for classification.")
    report = _TASKS[task](working)
    report["assumptions"] = assumptions + report["assumptions"]
    return report


__all__ = ["PARAMETERS", "classify_holdings", "foreign_securities", "instrument_types", "isr_annual_tax",
           "personal_deductions", "real_interest", "run", "tax_calendar"]
