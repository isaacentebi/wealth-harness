"""Dated U.S. federal individual income-tax parameters used by :mod:`wealth.tax`.

Every table carries its primary source and the date it was checked.  A tax
year that is absent here is unsupported: callers fail closed (or supply their
own marginal rates) rather than extrapolating.  Thresholds are taxable-income
amounts in USD; each ordinary bracket is ``(lower_bound, rate)``.

Figures were transcribed from the IRS PDFs and checked on 2026-09-21:

* 2025: Rev. Proc. 2024-40, section 2.01 (Tables 1-4) and 2.03.  The 2025
  tables were not changed by P.L. 119-21 (OBBBA), whose rate-table changes
  apply to taxable years beginning after 2025.
* 2026: Rev. Proc. 2025-32, section 4.01 (Tables 1-4) and 4.03, which reflects
  P.L. 119-21.

NIIT thresholds (IRC 1411(b)) and the capital-loss limit (IRC 1211(b)) are
statutory and not inflation indexed.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any


FILING_STATUSES = ("single", "married_filing_jointly", "married_filing_separately", "head_of_household", "qualifying_surviving_spouse")


def _brackets(*rows: tuple[str, str]) -> tuple[tuple[Decimal, Decimal], ...]:
    return tuple((Decimal(lower), Decimal(rate)) for lower, rate in rows)


_RATES = ("0.10", "0.12", "0.22", "0.24", "0.32", "0.35", "0.37")


def _table(*lowers: str) -> tuple[tuple[Decimal, Decimal], ...]:
    return _brackets(*zip(("0", *lowers), _RATES))


US_FEDERAL_PARAMETERS: dict[int, dict[str, Any]] = {
    2025: {
        "status": "verified",
        "source": {
            "title": "IRS Rev. Proc. 2024-40, sections 2.01 and 2.03 (2025 inflation adjustments)",
            "url": "https://www.irs.gov/pub/irs-drop/rp-24-40.pdf",
            "checked_on": "2026-09-21",
        },
        "ordinary_brackets": {
            "married_filing_jointly": _table("23850", "96950", "206700", "394600", "501050", "751600"),
            "head_of_household": _table("17000", "64850", "103350", "197300", "250500", "626350"),
            "single": _table("11925", "48475", "103350", "197300", "250525", "626350"),
            "married_filing_separately": _table("11925", "48475", "103350", "197300", "250525", "375800"),
        },
        # (maximum zero-rate amount, maximum 15-percent-rate amount), IRC 1(j)(5)(B)
        "capital_gain_thresholds": {
            "married_filing_jointly": (Decimal("96700"), Decimal("600050")),
            "married_filing_separately": (Decimal("48350"), Decimal("300000")),
            "head_of_household": (Decimal("64750"), Decimal("566700")),
            "single": (Decimal("48350"), Decimal("533400")),
        },
    },
    2026: {
        "status": "verified",
        "source": {
            "title": "IRS Rev. Proc. 2025-32, sections 4.01 and 4.03 (2026 inflation adjustments, post P.L. 119-21)",
            "url": "https://www.irs.gov/pub/irs-drop/rp-25-32.pdf",
            "checked_on": "2026-09-21",
        },
        "ordinary_brackets": {
            "married_filing_jointly": _table("24800", "100800", "211400", "403550", "512450", "768700"),
            "head_of_household": _table("17700", "67450", "105700", "201750", "256200", "640600"),
            "single": _table("12400", "50400", "105700", "201775", "256225", "640600"),
            "married_filing_separately": _table("12400", "50400", "105700", "201775", "256225", "384350"),
        },
        "capital_gain_thresholds": {
            "married_filing_jointly": (Decimal("98900"), Decimal("613700")),
            "married_filing_separately": (Decimal("49450"), Decimal("306850")),
            "head_of_household": (Decimal("66200"), Decimal("579600")),
            "single": (Decimal("49450"), Decimal("545500")),
        },
    },
}

NIIT = {
    "rate": Decimal("0.038"),
    "thresholds": {
        "married_filing_jointly": Decimal("250000"),
        "qualifying_surviving_spouse": Decimal("250000"),
        "married_filing_separately": Decimal("125000"),
        "single": Decimal("200000"),
        "head_of_household": Decimal("200000"),
    },
    "source": {"title": "IRC 1411(a)(1) and (b); Treas. Reg. 1.1411-4(d)(2) for the capital-loss offset", "url": "https://www.law.cornell.edu/uscode/text/26/1411", "indexed": False},
}

CAPITAL_LOSS_LIMIT = {
    "married_filing_separately": Decimal("1500"),
    "default": Decimal("3000"),
    "source": {"title": "IRC 1211(b) and 1212(b)", "url": "https://www.law.cornell.edu/uscode/text/26/1211"},
}


def _table_status(filing_status: str) -> str:
    return "married_filing_jointly" if filing_status == "qualifying_surviving_spouse" else filing_status


def parameters(tax_year: int) -> dict[str, Any] | None:
    """Return the verified parameter table for ``tax_year`` or ``None``."""
    table = US_FEDERAL_PARAMETERS.get(tax_year)
    return table if table is not None and table.get("status") == "verified" else None


def capital_loss_limit(filing_status: str) -> Decimal:
    return CAPITAL_LOSS_LIMIT["married_filing_separately"] if filing_status == "married_filing_separately" else CAPITAL_LOSS_LIMIT["default"]


def ordinary_tax(table: dict[str, Any], filing_status: str, taxable: Decimal) -> Decimal:
    brackets = table["ordinary_brackets"][_table_status(filing_status)]
    tax = Decimal(0)
    for index, (lower, rate) in enumerate(brackets):
        upper = brackets[index + 1][0] if index + 1 < len(brackets) else None
        if taxable <= lower:
            break
        tax += ((taxable if upper is None else min(taxable, upper)) - lower) * rate
    return tax


def federal_tax(
    table: dict[str, Any],
    filing_status: str,
    *,
    ordinary_income: Decimal,
    qualified_dividends: Decimal,
    net_short_term_gain: Decimal,
    net_capital_gain: Decimal,
    capital_loss_deduction: Decimal,
    magi_excluding_capital_gains: Decimal,
    nii_excluding_capital_gains: Decimal,
) -> dict[str, Decimal]:
    """Regular tax (Qualified Dividends and Capital Gain Tax Worksheet) plus NIIT.

    ``ordinary_income`` is taxable income before any capital gain, capital-loss
    deduction and qualified dividends.  ``net_short_term_gain`` is the part of
    the net gain taxed at ordinary rates; ``net_capital_gain`` the preferential
    part (IRC 1222(11)).  AMT, the 25%/28% gain classes, and credits are out
    of scope.
    """
    status = _table_status(filing_status)
    taxable_income = max(Decimal(0), ordinary_income + qualified_dividends + net_short_term_gain + net_capital_gain - capital_loss_deduction)
    preferential = min(qualified_dividends + net_capital_gain, taxable_income)
    ordinary_part = taxable_income - preferential
    zero_max, fifteen_max = table["capital_gain_thresholds"][status]

    def overlap(low: Decimal, high: Decimal | None) -> Decimal:
        top = taxable_income if high is None else min(taxable_income, high)
        return max(Decimal(0), top - max(ordinary_part, low))

    at_fifteen = overlap(zero_max, fifteen_max)
    at_twenty = overlap(fifteen_max, None)
    at_zero = preferential - at_fifteen - at_twenty
    regular_ordinary = ordinary_tax(table, filing_status, ordinary_part)
    preferential_tax = at_fifteen * Decimal("0.15") + at_twenty * Decimal("0.20")
    # The worksheet takes the smaller of this and regular tax on all income.
    regular = min(regular_ordinary + preferential_tax, ordinary_tax(table, filing_status, taxable_income))
    capital_items = net_short_term_gain + net_capital_gain - capital_loss_deduction
    magi = magi_excluding_capital_gains + capital_items
    nii = max(Decimal(0), nii_excluding_capital_gains + capital_items)
    threshold = NIIT["thresholds"][filing_status]
    niit = NIIT["rate"] * max(Decimal(0), min(nii, magi - threshold))
    return {
        "taxable_income": taxable_income,
        "ordinary_taxable_income": ordinary_part,
        "preferential_income": preferential,
        "preferential_at_0": at_zero,
        "preferential_at_15": at_fifteen,
        "preferential_at_20": at_twenty,
        "regular_tax": regular,
        "ordinary_tax": regular_ordinary,
        "preferential_tax": preferential_tax,
        "niit": niit,
        "magi": magi,
        "net_investment_income": nii,
        "total_tax": regular + niit,
    }
