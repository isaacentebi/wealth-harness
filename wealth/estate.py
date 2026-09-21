"""US estate-tax exposure for Mexico residents, including US persons living in Mexico.

Non-resident aliens (not US citizens and not US-domiciled) are taxed only on
US-situs assets, with a unified credit of $13,000 (IRC 2102(b)(1)), which
shelters $60,000 of taxable estate.  There is no US-Mexico estate tax treaty,
so no treaty exemption or pro-rata credit is available.  US citizens (and
US-domiciled green-card holders) are taxed on the worldwide estate with the
basic exclusion amount for the year of death.

Outputs are exposure estimates with neutral structural alternatives; this
module never recommends products.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from .mexico import _decimal, _envelope, _money, _text, _year


IRS_NRA_ESTATE_URL = "https://www.irs.gov/individuals/international-taxpayers/some-nonresidents-with-us-assets-must-file-estate-tax-returns"
IRS_WHATS_NEW_URL = "https://www.irs.gov/businesses/small-businesses-self-employed/whats-new-estate-and-gift-tax"
IRS_RP_2025_32_URL = "https://www.irs.gov/pub/irs-drop/rp-25-32.pdf"
USC_2001_URL = "https://www.law.cornell.edu/uscode/text/26/2001"
USC_2102_URL = "https://www.law.cornell.edu/uscode/text/26/2102"
USC_2104_URL = "https://www.law.cornell.edu/uscode/text/26/2104"
USC_2105_URL = "https://www.law.cornell.edu/uscode/text/26/2105"
USC_PFIC_URL = "https://www.law.cornell.edu/uscode/text/26/1297"
LISR_URL = "https://www.diputados.gob.mx/LeyesBiblio/pdf/LISR.pdf"

CONSULT_FLAG = "consult_estate_attorney"

# IRC 2001(c) rate schedule: (over, base tax, marginal rate).
RATE_SCHEDULE: tuple[tuple[Decimal, Decimal, Decimal], ...] = tuple(
    (Decimal(over), Decimal(base), Decimal(rate)) for over, base, rate in (
        ("0", "0", "0.18"), ("10000", "1800", "0.20"), ("20000", "3800", "0.22"), ("40000", "8200", "0.24"),
        ("60000", "13000", "0.26"), ("80000", "18200", "0.28"), ("100000", "23800", "0.30"), ("150000", "38800", "0.32"),
        ("250000", "70800", "0.34"), ("500000", "155800", "0.37"), ("750000", "248300", "0.39"), ("1000000", "345800", "0.40"),
    )
)
NRA_UNIFIED_CREDIT = Decimal("13000")

PARAMETERS: dict[str, dict[Any, dict[str, Any]]] = {
    "basic_exclusion_amount_usd": {
        2026: {"value": "15000000", "status": "verified", "checked_on": "2026-09-21",
               "source": {"title": "IRS Rev. Proc. 2025-32 section 2.13; IRC 2010(c)(3) as amended by Pub. L. 119-21", "url": IRS_RP_2025_32_URL}},
        2025: {"value": "13990000", "status": "verified", "checked_on": "2026-09-21",
               "source": {"title": "IRS, What's new - Estate and gift tax (Rev. Proc. 2024-40)", "url": IRS_WHATS_NEW_URL}},
    },
    "nra_unified_credit_usd": {"any": {"value": "13000", "status": "statutory", "source": {"title": "26 U.S.C. 2102(b)(1)", "url": USC_2102_URL}}},
    "rate_schedule": {"any": {"value": "IRC 2001(c) 18%-40%", "status": "statutory", "source": {"title": "26 U.S.C. 2001(c)", "url": USC_2001_URL}}},
}

_SOURCES = [
    {"title": "26 U.S.C. 2001(c) rate schedule", "url": USC_2001_URL},
    {"title": "26 U.S.C. 2102(b) unified credit for nonresidents not citizens", "url": USC_2102_URL},
    {"title": "26 U.S.C. 2104 property within the United States", "url": USC_2104_URL},
    {"title": "26 U.S.C. 2105 property without the United States", "url": USC_2105_URL},
    {"title": "IRS, Some nonresidents with U.S. assets must file estate tax returns (Form 706-NA)", "url": IRS_NRA_ESTATE_URL},
]

# asset type -> (US situs for a non-resident alien?, basis)
_SITUS: dict[str, tuple[bool | None, str]] = {
    "us_stock": (True, "Stock of a US corporation is US-situs regardless of where held (IRC 2104(a))."),
    "us_domiciled_fund": (True, "Shares of a US-domiciled ETF or mutual fund are US-situs stock (IRC 2104(a))."),
    "us_money_market_fund": (True, "US-domiciled money-market fund shares are US-situs stock."),
    "us_real_estate": (True, "Real property located in the United States is US-situs."),
    "us_tangible_property": (True, "Tangible personal property located in the United States is US-situs."),
    "us_bank_deposit": (False, "US bank deposits not effectively connected with a US business are treated as non-US property (IRC 2105(b)(1))."),
    "us_debt_portfolio_interest": (False, "Debt whose interest would qualify as portfolio interest is non-US property (IRC 2105(b)(3))."),
    "us_debt_other": (True, "Debt of a US obligor not qualifying for the portfolio-interest exclusion is US-situs."),
    "non_us_domiciled_fund": (False, "Shares of a non-US fund (e.g., Irish UCITS) are stock of a foreign corporation: not US-situs."),
    "mexican_fund": (False, "Mexican-domiciled fund shares are not US-situs."),
    "mexican_stock": (False, "Stock of a Mexican corporation is not US-situs."),
    "foreign_stock": (False, "Stock of a non-US corporation is not US-situs."),
    "mexican_real_estate": (False, "Real property outside the United States is not US-situs."),
    "cash_outside_us": (False, "Deposits held outside the United States are not US-situs."),
    "us_brokerage_cash": (None, "Cash sweep at a US broker may be a bank deposit or a money-market fund; the situs depends on the sweep vehicle."),
}


def tentative_tax(taxable_amount: Decimal) -> Decimal:
    """IRC 2001(c) tentative tax on ``taxable_amount`` (USD)."""
    if taxable_amount <= 0:
        return Decimal(0)
    over, base, rate = RATE_SCHEDULE[0]
    for row in RATE_SCHEDULE:
        if taxable_amount > row[0]:
            over, base, rate = row
    return base + (taxable_amount - over) * rate


def _exclusion(year: int, inputs: dict[str, Any], assumptions: list[str], used: list[dict[str, Any]]) -> tuple[Decimal | None, list[str]]:
    override = inputs.get("parameters", {}).get("basic_exclusion_amount_usd") if isinstance(inputs.get("parameters"), dict) else None
    if override is not None:
        if not isinstance(override, dict):
            raise ValueError("parameters.basic_exclusion_amount_usd must be an object with value and source")
        value = _decimal(override.get("value"), "parameters.basic_exclusion_amount_usd.value")
        source = _text(override.get("source"), "parameters.basic_exclusion_amount_usd.source")
        assumptions.append(f"Used caller-supplied basic exclusion amount for {year} from: {source}.")
        used.append({"key": "basic_exclusion_amount_usd", "year": year, "value": format(value, "f"), "status": "caller_supplied", "source": source})
        return value, []
    entry = PARAMETERS["basic_exclusion_amount_usd"].get(year)
    if entry is None:
        return None, [f"parameters.basic_exclusion_amount_usd (year {year}; verify with the IRS Rev. Proc. for that year)"]
    used.append({"key": "basic_exclusion_amount_usd", "year": year, **entry})
    return Decimal(entry["value"]), []


def _alternatives(us_person: bool) -> list[dict[str, Any]]:
    alternatives = [
        {"structure": "Non-US-domiciled (e.g., Irish UCITS) fund holding US equities",
         "us_estate_situs": "not US-situs for a non-resident alien",
         "considerations": ["Fund-level US dividend withholding under the fund's own treaty position; costs and tracking vary.",
                            "Mexican tax: foreign fund not listed in the SIC falls outside Article 129; accumulating vs distributing share classes change timing - consult a contador.",
                            "For US persons: likely a PFIC (Form 8621) and no US estate benefit because a US person is taxed worldwide."]},
        {"structure": "Mexican-domiciled fund (fondo de inversion) or Mexican-listed index trust",
         "us_estate_situs": "not US-situs",
         "considerations": ["Mexican tax handled by the fund/intermediary (Arts. 87-89 or Art. 129).",
                            "For US persons: likely PFIC treatment."]},
        {"structure": "Keeping US-situs holdings with estate liquidity planning",
         "us_estate_situs": "US-situs",
         "considerations": ["Exposure remains; filing Form 706-NA may be needed above $60,000 of US-situs assets.",
                            "Life insurance or other liquidity arrangements are planning choices for an adviser."]},
    ]
    if us_person:
        alternatives = alternatives[:2]
    return alternatives


def us_estate_exposure(inputs: dict[str, Any]) -> dict[str, Any]:
    """Estimate US estate-tax exposure for a Mexico resident.

    Inputs: ``year`` (year of death scenario); ``decedent`` = {``us_citizen``: bool,
    ``green_card``: bool, ``us_domiciled``: bool (required unless us_citizen)};
    ``assets`` rows {``id``, ``type`` (see ``_SITUS``), ``value_usd``, optional
    ``custody`` (``sic``, ``mx_broker``, ``us_broker``, ``foreign_broker``)};
    optional ``estimated_deductions_usd`` (low end of the range) and, for US
    persons, ``adjusted_taxable_gifts_usd`` (required) and ``marital_deduction_usd``.
    """
    missing: list[str] = []
    if "year" not in inputs:
        missing.append("year")
    decedent = inputs.get("decedent")
    if not isinstance(decedent, dict):
        missing.append("decedent")
    assets = inputs.get("assets")
    if not isinstance(assets, list) or not assets:
        missing.append("assets")
    if missing:
        return _envelope("needs_input", {}, missing=missing, sources=_SOURCES)
    year = _year(inputs["year"], "year")
    citizen = decedent.get("us_citizen")
    green_card = decedent.get("green_card")
    domiciled = decedent.get("us_domiciled")
    for key, value in (("us_citizen", citizen), ("green_card", green_card), ("us_domiciled", domiciled)):
        if value is not None and not isinstance(value, bool):
            raise ValueError(f"decedent.{key} must be a boolean")
    if citizen is None:
        missing.append("decedent.us_citizen")
    if citizen is not True and domiciled is None:
        missing.append("decedent.us_domiciled (facts-and-circumstances domicile, not residence for income tax)")
    if missing:
        return _envelope("needs_input", {}, missing=missing, sources=_SOURCES,
                         warnings=["Estate-tax status depends on citizenship and domicile; neither is assumed."])
    worldwide = citizen is True or domiciled is True
    warnings: list[str] = ["There is no US-Mexico estate or gift tax treaty; no treaty exemption or credit applies.",
                           "Mexico does not levy an estate tax; inheritances are exempt from Mexican ISR (LISR Art. 93 fr. XXII), subject to disclosure rules."]
    assumptions: list[str] = []
    rows: list[dict[str, Any]] = []
    situs_total = worldwide_total = sic_situs = Decimal(0)
    seen: set[str] = set()
    for index, asset in enumerate(assets):
        if not isinstance(asset, dict):
            raise ValueError(f"assets[{index}] must be an object")
        path = f"assets[{index}]"
        asset_id = _text(asset.get("id"), f"{path}.id")
        if asset_id in seen:
            raise ValueError(f"duplicate asset id: {asset_id}")
        seen.add(asset_id)
        if "value_usd" not in asset:
            missing.append(f"{path}.value_usd")
            continue
        value = _decimal(asset["value_usd"], f"{path}.value_usd")
        asset_type = _text(asset.get("type"), f"{path}.type").lower() if asset.get("type") is not None else None
        if asset_type not in _SITUS:
            missing.append(f"{path}.type (supported: {', '.join(sorted(_SITUS))})")
            continue
        situs, basis = _SITUS[asset_type]
        custody = asset.get("custody")
        row = {"id": asset_id, "type": asset_type, "value_usd": _money(value), "us_situs": situs, "basis": basis}
        worldwide_total += value
        if situs is None and not worldwide:
            missing.append(f"{path}.type (resolve the sweep vehicle: us_bank_deposit or us_money_market_fund)")
            row["uncertain"] = True
        elif situs:
            situs_total += value
            if custody == "sic":
                sic_situs += value
                row["situs_note"] = ("Held through the SIC/Mexican custody chain. Situs follows the issuer's domicile, so it is treated as US-situs; "
                                     "how intermediated foreign custody is documented and enforced at death is uncertain and is not a basis to exclude it.")
        rows.append(row)
    used: list[dict[str, Any]] = []
    deductions = _decimal(inputs.get("estimated_deductions_usd", 0), "estimated_deductions_usd")
    result: dict[str, Any] = {"year": year, "currency": "USD", "assets": rows,
                              "flags": [CONSULT_FLAG], "execution_ready": False}
    if worldwide:
        status_label = "US citizen" if citizen else "US-domiciled non-citizen"
        exclusion, excl_missing = _exclusion(year, inputs, assumptions, used)
        missing.extend(excl_missing)
        if "adjusted_taxable_gifts_usd" not in inputs:
            missing.append("adjusted_taxable_gifts_usd (lifetime taxable gifts; unknown is not zero)")
        marital = _decimal(inputs.get("marital_deduction_usd", 0), "marital_deduction_usd")
        if marital > 0 and decedent.get("spouse_us_citizen") is not True:
            warnings.append("The unlimited marital deduction for a non-citizen spouse generally requires a QDOT (IRC 2056(d)); not assumed.")
            marital = Decimal(0)
        exposure = None
        if exclusion is not None and "adjusted_taxable_gifts_usd" in inputs:
            gifts = _decimal(inputs["adjusted_taxable_gifts_usd"], "adjusted_taxable_gifts_usd")
            credit = tentative_tax(exclusion)
            high = max(tentative_tax(worldwide_total - marital + gifts) - credit, Decimal(0))
            low = max(tentative_tax(max(worldwide_total - marital - deductions, Decimal(0)) + gifts) - credit, Decimal(0))
            exposure = {"gross_estate_usd": _money(worldwide_total), "basic_exclusion_usd": _money(exclusion),
                        "estimated_tax_range_usd": {"low": _money(low), "high": _money(high)},
                        "method": "tentative tax on taxable estate plus adjusted taxable gifts less applicable credit; state estate taxes and gift-tax paid adjustments excluded"}
        result.update({"tax_status": status_label, "basis": "worldwide estate", "exposure": exposure})
        if green_card and not citizen:
            warnings.append("Green-card holders living in Mexico may or may not be US-domiciled for estate tax; the caller's domicile determination drives this result.")
        warnings.append("US persons holding Mexican funds, Mexican index trusts or non-US ETFs likely hold PFICs (IRC 1291-1298, Form 8621); FIBRAs may also be PFICs.")
    else:
        taxable_high = situs_total
        taxable_low = max(situs_total - deductions, Decimal(0))
        high = max(tentative_tax(taxable_high) - NRA_UNIFIED_CREDIT, Decimal(0))
        low = max(tentative_tax(taxable_low) - NRA_UNIFIED_CREDIT, Decimal(0))
        result.update({
            "tax_status": "non-resident alien (not US citizen, not US-domiciled)", "basis": "US-situs assets only",
            "exposure": {"us_situs_total_usd": _money(situs_total), "of_which_held_via_sic_usd": _money(sic_situs),
                         "exemption_equivalent_usd": "60000.00", "unified_credit_usd": _money(NRA_UNIFIED_CREDIT),
                         "form_706na_likely_required": situs_total > Decimal("60000"),
                         "estimated_tax_range_usd": {"low": _money(low), "high": _money(high)},
                         "method": "IRC 2001(c) tentative tax less the $13,000 unified credit; low end subtracts caller-estimated deductions (IRC 2106 proportional deductions require worldwide-estate disclosure)"},
        })
        if green_card:
            warnings.append("A green-card holder treated as not US-domiciled: confirm domicile with counsel; expatriation (IRC 877A, 2801) rules may also apply.")
        if "estimated_deductions_usd" not in inputs:
            assumptions.append("No deductions were supplied; low and high ends are equal.")
    result["alternatives"] = _alternatives(worldwide)
    result["alternatives_note"] = "Neutral structural comparisons, not product recommendations; tax, cost and suitability require professional review."
    result["parameters_used"] = used
    sources = list(_SOURCES)
    if worldwide:
        sources.append({"title": "IRS Rev. Proc. 2025-32 (2026 inflation adjustments)", "url": IRS_RP_2025_32_URL})
        sources.append({"title": "26 U.S.C. 1297 PFIC definition", "url": USC_PFIC_URL})
    sources.append({"title": "Ley del Impuesto sobre la Renta, articulo 93 fraccion XXII (herencias)", "url": LISR_URL})
    status = "partial" if missing else "ready"
    if result.get("exposure") is None:
        status = "needs_input"
    return _envelope(status, result, missing=missing, warnings=warnings, sources=sources, assumptions=assumptions)


def run(task: str, inputs: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    """Service entry point for task ``estate``."""
    if task != "estate":
        raise ValueError("estate.run supports only task='estate'")
    if not isinstance(inputs, dict) or not isinstance(context, dict):
        raise ValueError("inputs and context must be objects")
    return us_estate_exposure(inputs)


__all__ = ["PARAMETERS", "RATE_SCHEDULE", "run", "tentative_tax", "us_estate_exposure"]
