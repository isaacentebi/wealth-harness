"""The debt engine: amortization, prepay vs invest, refinance offers and payoff strategies.

One task, ``debt``, four modes:

* ``amortize``: months, interest and the schedule (monthly rows, summarised yearly) for each debt:
  fixed-rate loans, credit cards (a stated payment or the minimum-payment rule), mortgages and, in
  Mexico, Infonavit/Fovissste credits denominated in VSM or UMA with their annual update.
* ``prepay_vs_invest``: an extra monthly amount or a lump sum against one debt or into investments,
  after tax, with the breakeven return, the net-worth difference at the horizon and a verdict.
* ``refinance``: a refinance, balance transfer or consolidation offer against the current path.
* ``strategies``: avalanche vs snowball vs hybrid for a monthly budget.

Everything is deterministic Decimal arithmetic.  Interest accrues monthly at annual_rate / 12 (the
tasa, not the CAT); Mexican consumer credit also carries IVA on interest.  A figure that is not
known is reported as unknown (``None`` and a ``missing`` entry), never as zero.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any, Callable, Mapping

from . import finmath
from .situation.model import D, _as_date, add_months, annuity_payment, num

MODES = ("amortize", "prepay_vs_invest", "refinance", "strategies")
MAX_MONTHS = 600
ZERO, ONE = Decimal(0), Decimal(1)
CENT = Decimal("0.005")
MX_IVA = Decimal("0.16")
INDEXED_KINDS = ("infonavit", "fovissste")
KINDS = ("card", "mortgage", "auto", "personal", "student", *INDEXED_KINDS, "other")
_KIND_ALIASES = {"credit_card": "card", "tarjeta": "card", "tdc": "card", "car": "auto", "vehicle": "auto",
                 "hipoteca": "mortgage", "home": "mortgage", "loan": "other"}
# Consumer credit in Mexico pays IVA on interest; home credit (and Infonavit/Fovissste) is exempt.
_IVA_KINDS = ("card", "auto", "personal", "other")
UNIT_DAYS = Decimal("30.4")               # INEGI: monthly UMA = daily UMA x 30.4
INDEXATION_ASSUMED = Decimal("0.04")      # annual VSM/UMA update when none is stated (an estimate)
LIBERATION_MONTHS = 360                   # VSM/UMA credits (post-1997 regime): cancelled after 30 years without omissions
US_MORTGAGE_DEBT_LIMIT = Decimal(750000)  # IRC 163(h)(3)(F): acquisition debt after 2017-12-15
MONTHLY_ROWS_DEFAULT = 12
HIGH_INTEREST_RATE = Decimal("0.20")      # as proactive: a debt this costly beats any safe return
STARTER_RESERVE_MONTHS = ONE              # a 20%+ debt waits only for one month of essentials
SERIES_POINTS = 120
MAX_DEBTS = 12

# Expected nominal returns before tax for a diversified equity index fund, by currency.  Planning
# assumptions for a range, not forecasts; a caller's expected_return replaces them.
EXPECTED_RETURNS = {
    "USD": {"conservative": Decimal("0.04"), "base": Decimal("0.065"),
            "source": "Wealth planning assumption for a diversified US equity index fund in USD (e.g. VTI): "
                      "4% conservative, 6.5% base, nominal, before tax and fees; not a forecast"},
    "MXN": {"conservative": Decimal("0.075"), "base": Decimal("0.10"),
            "source": "Wealth planning assumption for diversified equity held in MXN: 7.5% conservative, "
                      "10% base, nominal (includes Mexican inflation), before tax; not a forecast"},
}
US_LTCG_ASSUMED = Decimal("0.15")
US_STUDENT_INTEREST_CAP = Decimal(2500)   # IRC 221: student-loan interest deduction, per return per year
MX_INFLATION_ASSUMED = Decimal("0.04")
MARGINAL_BOUNDS = {"US": (Decimal("0.10"), Decimal("0.37")), "MX": (Decimal("0.0192"), Decimal("0.35"))}

SOURCES = {
    "liva": {"title": "Ley del Impuesto al Valor Agregado, articulos 1, 15 fraccion X y 18-A",
             "url": "https://www.diputados.gob.mx/LeyesBiblio/pdf/LIVA.pdf",
             "rules": ["IVA (16%) applies to interest on consumer and card credit; interest on credit for the "
                       "casa habitacion is exempt"]},
    "cat": {"title": "Banco de Mexico, Costo Anual Total (CAT)", "url": "https://www.banxico.org.mx/CAT/",
            "rules": ["CAT is an annual all-in cost for comparing credits: interest plus fees, without IVA; the "
                      "interest charged each month is the tasa (plus IVA)"]},
    "minimum": {"title": "Banco de Mexico, Circular 13/2011 (pago minimo de tarjetas de credito)",
                "url": "https://www.banxico.org.mx/marco-normativo/",
                "rules": ["minimum payment: the greater of 1.5% of the balance plus interest and IVA, or 1.25% "
                          "of the credit limit"]},
    "uma": {"title": "INEGI, Unidad de Medida y Actualizacion", "url": "https://www.inegi.org.mx/temas/uma/",
            "rules": ["the UMA is updated every year by the December INPC and takes effect on February 1; "
                      "monthly value = daily value x 30.4"]},
    "infonavit": {"title": "Ley del Infonavit, articulo 51", "url": "https://www.diputados.gob.mx/LeyesBiblio/",
                  "rules": ["credits in VSM/UMA are updated with the unit each year; for VSM/UMA credits under the "
                            "post-1997 regime, a balance left after 30 years of payments without omissions is "
                            "cancelled (Fovissste has an equivalent rule); fixed-peso credits amortize by contract"]},
    "irs936": {"title": "IRS Publication 936, Home Mortgage Interest Deduction; IRC 163(h)(3)",
               "url": "https://www.irs.gov/publications/p936",
               "rules": ["mortgage interest is deductible only for those who itemize; acquisition debt after "
                         "2017-12-15 counts up to $750,000"]},
    "irs970": {"title": "IRS Publication 970, Student Loan Interest Deduction; IRC 221",
               "url": "https://www.irs.gov/publications/p970",
               "rules": ["up to $2,500 a year of student-loan interest is deductible above the line; the deduction "
                         "phases out with modified AGI"]},
    "art151": {"title": "Ley del Impuesto sobre la Renta, articulo 151 fraccion IV",
               "url": "https://www.diputados.gob.mx/LeyesBiblio/pdf/LISR.pdf",
               "rules": ["real interest on a mortgage for the casa habitacion, from the financial system, credit up "
                         "to 750,000 UDIs, is a personal deduction within the global cap"]},
    "art129": {"title": "Ley del Impuesto sobre la Renta, articulo 129",
               "url": "https://www.diputados.gob.mx/LeyesBiblio/pdf/LISR.pdf",
               "rules": ["10% definitive tax on the gain (proceeds less the inflation-updated average cost) from "
                         "shares and ETFs listed on the BMV/BIVA or in the SIC, sold through a Mexican intermediary"]},
    "cetes_tax": {"title": "Ley del Impuesto sobre la Renta, articulos 133-135",
                  "url": "https://www.diputados.gob.mx/LeyesBiblio/pdf/LISR.pdf",
                  "rules": ["CETES interest is taxed on real interest (nominal less inflation) at the marginal rate"]},
    "tbill_tax": {"title": "IRS Publication 550; 31 U.S.C. 3124",
                  "url": "https://www.irs.gov/publications/p550",
                  "rules": ["Treasury bill interest is federally taxable and exempt from state income tax"]},
}


# --------------------------------------------------------------------------- inputs


def _ratio(value: Any, path: str, *, high: Decimal = Decimal(3)) -> Decimal | None:
    if value is None:
        return None
    number = D(value)
    if number is None or number < 0 or number > high:
        raise ValueError(f"{path} must be a decimal between 0 and {high} (0.45 for 45%)")
    return number


def _money_in(value: Any, path: str) -> Decimal | None:
    if value is None:
        return None
    number = D(value)
    if number is None or number < 0:
        raise ValueError(f"{path} must be a nonnegative amount")
    return number


def _kind(row: Mapping[str, Any]) -> str:
    raw = str(row.get("kind") or row.get("type") or "").strip().lower()
    kind = _KIND_ALIASES.get(raw, raw)
    name = f" {str(row.get('name') or '')} {str(row.get('lender') or '')} {row.get('id') or ''} ".lower()
    for indexed in INDEXED_KINDS:
        if indexed in name or indexed == kind:
            return indexed
    return kind if kind in KINDS else "other"


def _iva(row: Mapping[str, Any], kind: str, currency: str | None) -> tuple[Decimal, str | None]:
    """IVA charged on interest: stated, else 16% for Mexican consumer credit, else none."""
    stated = row.get("iva_on_interest")
    if stated is True:
        return MX_IVA, None
    if stated is False:
        return ZERO, None
    if stated is not None:
        return _ratio(stated, "iva_on_interest", high=ONE), None
    if currency == "MXN" and kind in _IVA_KINDS:
        return MX_IVA, (f"{row.get('id') or kind}: IVA of 16% is added to interest, as Mexican card and consumer "
                        "credit statements charge it (set iva_on_interest to 0 if your statement shows none).")
    return ZERO, None


def normalize(row: Mapping[str, Any], index: int, today: date) -> tuple[dict | None, list[dict], list[str]]:
    """A debt ready for the engine, what it is missing, and the assumptions made about it."""
    if not isinstance(row, Mapping):
        raise ValueError(f"liabilities[{index}] must be an object")
    ident = str(row.get("id") or f"debt{index}")
    kind = _kind(row)
    currency = row.get("currency")
    missing: list[dict] = []
    assumptions: list[str] = []
    denomination = str(row.get("denomination") or "").upper() or None
    if denomination not in (None, "VSM", "UMA", "MXN", "PESOS", "USD"):
        raise ValueError(f"{ident}.denomination must be VSM, UMA or MXN")
    indexed = denomination in ("VSM", "UMA")
    if indexed:
        currency = "MXN"
    balance = _money_in(row.get("balance", row.get("value")), f"{ident}.balance")
    rate = _ratio(row.get("annual_rate", row.get("interest_rate")), f"{ident}.annual_rate")
    cat = _ratio(row.get("cat"), f"{ident}.cat", high=Decimal(20))
    debt: dict[str, Any] = {"id": ident, "name": row.get("name") or ident, "kind": kind, "currency": currency,
                            "balance": balance, "rate": rate, "cat": cat, "rate_basis": "tasa" if rate is not None else None,
                            "denomination": "MXN" if denomination == "PESOS" else denomination, "indexed": indexed,
                            "estimate": False}
    if rate is None and cat is not None:
        # CAT is an effective annual cost with fees; its monthly equivalent stands in for the tasa (an estimate).
        rate = ((ONE + cat) ** (ONE / 12) - ONE) * 12
        debt.update(rate=rate, rate_basis="cat_estimate", estimate=True)
        assumptions.append(f"{ident}: only the CAT is known, so interest accrues at its monthly equivalent "
                           f"({num(rate / 12 * 100, 2)}% a month); the statement's tasa is exact.")
    if rate is None:
        missing.append({"key": f"liability.{ident}.annual_rate", "reason": "missing",
                        "detail": f"{ident} needs its annual interest rate (the tasa on the statement)"})
    iva, note = _iva(row, kind, currency)
    debt["iva"] = iva
    if note:
        assumptions.append(note)
    payment = D(row.get("monthly_payment", row.get("payment")))
    frequency = row.get("payment_frequency")
    if frequency is not None and frequency not in finmath.PER_MONTH:
        raise ValueError(f"{ident}.payment_frequency must be one of {', '.join(finmath.PER_MONTH)}")
    if payment is not None and frequency not in (None, "monthly"):
        payment = finmath.per_month(payment, frequency)
        assumptions.append(f"{ident}: a {frequency} payment is read as {num(payment)} a month "
                           f"({num(finmath.PER_MONTH[frequency] * 12, 0)} payments a year / 12).")
    if payment is not None and payment <= 0:
        raise ValueError(f"{ident}.monthly_payment must be positive")
    term = row.get("remaining_term_months")
    maturity = _as_date(row.get("maturity"))
    if term is None and maturity and maturity >= today:
        # A maturity later this month still leaves one payment: never zero months.
        term = max(finmath.months_between(today, maturity), 1)
    if term is not None and (not isinstance(term, int) or isinstance(term, bool) or term <= 0):
        raise ValueError(f"{ident}.remaining_term_months must be a positive whole number")
    rule = row.get("minimum_payment") if isinstance(row.get("minimum_payment"), Mapping) else None
    debt.update(payment=payment, term=term, rule=None, payment_basis="stated" if payment is not None else None)
    credit_limit = _money_in(row.get("credit_limit"), f"{ident}.credit_limit")
    if rule is not None:
        debt["rule"] = _rule(rule, ident, missing, credit_limit)
    if payment is None and debt["rule"] is None and term is not None and balance is not None and rate is not None:
        # The level payment that repays it in ``term`` months at the monthly cost, IVA included.
        debt["payment"], debt["payment_basis"] = payment_for_term(balance, rate, iva, term), "from remaining term"
    if payment is None and debt["rule"] is not None:
        debt["payment_basis"] = "minimum rule"
    soft: list[dict] = []  # gaps that leave a partial projection rather than none
    if indexed:
        _indexed(debt, row, today, missing, assumptions, soft)
    elif balance is None:
        missing.append({"key": f"liability.{ident}.balance", "reason": "missing", "detail": f"{ident} needs its balance"})
    if debt["payment"] is None and debt["rule"] is None and not indexed:
        missing.append({"key": f"liability.{ident}.monthly_payment", "reason": "missing",
                        "detail": f"{ident} needs its monthly payment, remaining_term_months, or (cards) the "
                                  "minimum_payment rule"})
    debt["original_principal"] = _money_in(row.get("original_principal"), f"{ident}.original_principal")
    debt["credit_limit"] = credit_limit
    ready = not missing
    debt["partial"] = bool(soft)
    return (debt if ready else None), missing + soft, assumptions


def payment_for_term(balance: Decimal, rate: Decimal, iva: Decimal, months: int) -> Decimal:
    """The level monthly payment that repays ``balance`` in ``months`` at tasa / 12 plus IVA on the interest."""
    return annuity_payment(balance, rate * (ONE + iva), months)


def _rule(raw: Mapping[str, Any], ident: str, missing: list[dict], credit_limit: Decimal | None = None) -> dict:
    percent = _ratio(raw.get("percent_of_balance"), f"{ident}.minimum_payment.percent_of_balance", high=ONE)
    if percent is None:
        raise ValueError(f"{ident}.minimum_payment needs percent_of_balance (0.015 for 1.5%)")
    floor = _money_in(raw.get("floor"), f"{ident}.minimum_payment.floor")
    limit_pct = _ratio(raw.get("percent_of_limit"), f"{ident}.minimum_payment.percent_of_limit", high=ONE)
    limit = _money_in(raw.get("credit_limit"), f"{ident}.minimum_payment.credit_limit")
    if limit is None:
        limit = credit_limit  # the card's own credit_limit, beside the rule
    if limit_pct is not None and limit is None:
        missing.append({"key": f"liability.{ident}.minimum_payment.credit_limit", "reason": "missing",
                        "detail": f"{ident}: percent_of_limit needs the credit limit"})
    by_limit = limit_pct * limit if limit_pct is not None and limit is not None else None
    lowest = max([v for v in (floor, by_limit) if v is not None], default=None)
    if lowest is None:
        missing.append({"key": f"liability.{ident}.minimum_payment.floor", "reason": "missing",
                        "detail": f"{ident}: the minimum-payment rule needs its floor (a fixed amount, or "
                                  "percent_of_limit with credit_limit); without one the balance never reaches zero"})
    return {"percent_of_balance": percent, "plus_interest": raw.get("plus_interest", True) is not False,
            "floor": lowest}


def _indexed(debt: dict, row: Mapping[str, Any], today: date, missing: list[dict], assumptions: list[str],
             soft: list[dict]) -> None:
    """Infonavit/Fovissste credit in VSM or UMA: units, the peso value of a unit and its annual update."""
    from . import mexico
    ident, denomination = debt["id"], debt["denomination"]
    value = _money_in(row.get("unit_value_mxn"), f"{ident}.unit_value_mxn")
    value_source = "stated" if value is not None else None
    if value is None and denomination == "UMA":
        entry = (mexico.PARAMETERS["uma_daily_mxn"].get(today.year) or {})
        daily = D(entry.get("value"))
        if daily is not None:
            value, value_source = daily * UNIT_DAYS, entry["source"]["title"] + " x 30.4"
    if value is None:
        missing.append({"key": f"liability.{ident}.unit_value_mxn", "reason": "missing",
                        "detail": f"{ident}: the peso value of one {denomination} (monthly) this year"})
    units = D(row.get("balance_units"))
    if units is None and debt["balance"] is not None and value:
        units = debt["balance"] / value
    if units is None:
        missing.append({"key": f"liability.{ident}.balance_units", "reason": "missing",
                        "detail": f"{ident}: the balance in {denomination} (or in pesos)"})
    growth = _ratio(row.get("unit_growth_annual"), f"{ident}.unit_growth_annual", high=ONE)
    if growth is None:
        growth = INDEXATION_ASSUMED
        assumptions.append(f"{ident}: the {denomination} is assumed to rise {num(growth * 100, 1)}% a year "
                           "(an estimate near expected inflation); the balance and payment in pesos rise with it.")
    payment_units = D(row.get("monthly_payment_units"))
    if payment_units is None and debt["payment"] is not None and value:
        payment_units = debt["payment"] / value
        assumptions.append(f"{ident}: the payment in pesos is read as a fixed number of {denomination} "
                           "(payroll deductions rise with salary each year).")
    if payment_units is None:
        missing.append({"key": f"liability.{ident}.monthly_payment", "reason": "missing",
                        "detail": f"{ident}: the monthly payment in pesos or in {denomination}"})
    update_month = row.get("update_month", 2 if denomination == "UMA" else 1)
    if not isinstance(update_month, int) or not 1 <= update_month <= 12:
        raise ValueError(f"{ident}.update_month must be 1-12")
    paid = row.get("months_paid")
    origination = _as_date(row.get("origination_date") or row.get("start_date"))
    if paid is None and origination is not None and origination <= today:
        paid = finmath.months_between(origination, today)
        assumptions.append(f"{ident}: {paid} months of payments are counted from the origination date "
                           f"{origination.isoformat()} (no omissions assumed).")
    if paid is not None and (not isinstance(paid, int) or isinstance(paid, bool) or paid < 0):
        raise ValueError(f"{ident}.months_paid must be a nonnegative whole number")
    if paid is None:
        # Unknown is not zero: without it the 30-year liberation date cannot be placed.
        soft.append({"key": f"liability.{ident}.months_paid", "reason": "missing",
                     "detail": f"{ident}: months of payments already made (or origination_date); without it the "
                               "projection runs to repayment and leaves out the 30-year liberation"})
    eligible = row.get("liberation_eligible")
    if eligible is not None and not isinstance(eligible, bool):
        raise ValueError(f"{ident}.liberation_eligible must be true or false")
    if eligible is None and paid is not None:
        assumptions.append(f"{ident}: the 30-year liberation (Ley del Infonavit art. 51 / Fovissste equivalent) is "
                           f"applied as for a {denomination} credit under the post-1997 regime, and only if no "
                           "payment was omitted; set liberation_eligible to false if your credit is excluded.")
    debt.update(unit_value=value, unit_value_source=value_source, units=units, growth=growth,
                payment_units=payment_units, update_month=update_month, months_paid=paid,
                liberation=eligible is not False and paid is not None, estimate=True,
                balance=units * value if units is not None and value is not None else debt["balance"],
                payment_basis="stated in pesos" if row.get("monthly_payment_units") is None else "stated in units")


def _monthly(debt: Mapping[str, Any]) -> Decimal:
    """What one month costs per unit of balance: tasa / 12, plus IVA on that interest."""
    return debt["rate"] / 12 * (ONE + debt["iva"])


def effective_annual(monthly: Decimal) -> Decimal:
    return (ONE + monthly) ** 12 - ONE


# --------------------------------------------------------------------------- one loan, month by month


def _schedule(balance: Decimal, rate_for: Callable[[int], Decimal], iva: Decimal,
              pay_for: Callable[[int, Decimal, Decimal], Decimal], today: date, *,
              extra: Decimal = ZERO, max_months: int = MAX_MONTHS) -> dict:
    """Monthly rows until the balance is repaid (or ``max_months``): payment, interest, IVA, principal, balance."""
    rows: list[dict] = []
    bal = balance
    totals = {"interest": ZERO, "iva": ZERO, "paid": ZERO}
    growing = False
    for month in range(1, max_months + 1):
        if bal <= CENT:
            break
        charge = bal * rate_for(month) / 12
        tax = charge * iva
        due = bal + charge + tax
        pay = min(pay_for(month, bal, charge + tax) + extra, due)
        if pay < charge + tax:
            growing = True
        new = due - pay
        totals["interest"] += charge
        totals["iva"] += tax
        totals["paid"] += pay
        rows.append({"month": month, "date": add_months(today, month).isoformat()[:7], "payment": pay,
                     "interest": charge, "iva": tax, "principal": pay - charge - tax, "balance": max(new, ZERO)})
        bal = new
    repaid = bal <= CENT
    out = {"status": "ready" if repaid else "never", "rows": rows, "months": len(rows) if repaid else None,
           "date": rows[-1]["date"] if repaid and rows else None, "interest": totals["interest"],
           "iva": totals["iva"], "paid": totals["paid"], "left": max(bal, ZERO), "negative_amortization": growing}
    if not repaid:
        out["detail"] = (f"not repaid within {max_months} months at this payment"
                         + ("; the payment does not cover the monthly interest" if growing else ""))
    return out


def _payer(debt: Mapping[str, Any], payment: Decimal | None = None) -> Callable[[int, Decimal, Decimal], Decimal]:
    fixed = payment if payment is not None else debt.get("payment")
    rule = debt.get("rule")
    if fixed is not None:
        return lambda _m, _b, _c: fixed
    if rule is None:
        raise ValueError(f"{debt['id']} has no payment")

    def minimum(_month: int, bal: Decimal, charge: Decimal) -> Decimal:
        amount = bal * rule["percent_of_balance"] + (charge if rule["plus_interest"] else ZERO)
        return max(amount, rule["floor"] or ZERO)
    return minimum


def _plan(debt: Mapping[str, Any], today: date, *, payment: Decimal | None = None, extra: Decimal = ZERO,
          rule: bool = False) -> dict:
    pay = _payer({**debt, "payment": None} if rule else debt, None if rule else payment)
    return _schedule(debt["balance"], lambda _m: debt["rate"], debt["iva"], pay, today, extra=extra)


def _indexed_plan(debt: Mapping[str, Any], today: date) -> dict:
    """An Infonavit/Fovissste credit in units: interest on units, a payment in units, the unit updated yearly."""
    units, value, rate = debt["units"], debt["unit_value"], debt["rate"]
    left_months = max(LIBERATION_MONTHS - debt["months_paid"], 0) if debt["liberation"] else MAX_MONTHS
    rows: list[dict] = []
    totals = {"interest": ZERO, "paid": ZERO, "indexation": ZERO}
    for month in range(1, min(left_months, MAX_MONTHS) + 1):
        if units <= Decimal("0.00005"):
            break
        day = add_months(today, month)
        if day.month == debt["update_month"]:
            raised = value * (ONE + debt["growth"])
            totals["indexation"] += units * (raised - value)
            value = raised
        charge = units * rate / 12
        pay = min(debt["payment_units"], units + charge)
        units = units + charge - pay
        totals["interest"] += charge * value
        totals["paid"] += pay * value
        rows.append({"month": month, "date": day.isoformat()[:7], "payment": pay * value, "interest": charge * value,
                     "iva": ZERO, "principal": pay * value - charge * value, "balance": max(units, ZERO) * value,
                     "balance_units": max(units, ZERO), "unit_value": value})
    repaid = units <= Decimal("0.00005")
    # A balance still owed after 30 years of regular payments is cancelled: the credit ends then.
    liberated = debt["liberation"] and not repaid and len(rows) == left_months
    ended = repaid or liberated
    return {"status": "ready" if ended else "never", "rows": rows, "months": len(rows) if ended else None,
            "date": rows[-1]["date"] if ended and rows else None, "interest": totals["interest"], "iva": ZERO,
            "paid": totals["paid"], "indexation": totals["indexation"], "left": ZERO if repaid else units * value,
            "forgiven_at_30_years": units * value if liberated else None,
            "negative_amortization": debt["payment_units"] <= debt["units"] * rate / 12,
            **({} if ended else {"detail": f"not repaid within {MAX_MONTHS} months"})}


def _yearly(rows: list[dict], start_balance: Decimal) -> list[dict]:
    years: list[dict] = []
    for row in rows:
        index = (row["month"] - 1) // 12
        if index == len(years):
            years.append({"year": index + 1, "from": row["date"], "to": row["date"], "start_balance": start_balance,
                          "paid": ZERO, "interest": ZERO, "iva": ZERO, "principal": ZERO, "end_balance": ZERO})
        year = years[index]
        year["to"] = row["date"]
        for field in ("interest", "iva", "principal"):
            year[field] += row[field]
        year["paid"] += row["payment"]
        year["end_balance"] = row["balance"]
        start_balance = row["balance"]
    return [{k: (num(v) if isinstance(v, Decimal) else v) for k, v in year.items()} for year in years]


def _row_out(row: Mapping[str, Any]) -> dict:
    return {k: (num(v, 4) if k in ("balance_units",) else num(v) if isinstance(v, Decimal) else v)
            for k, v in row.items()}


def _series(start: Decimal, rows: list[dict], today: date, limit: int = SERIES_POINTS) -> list[dict]:
    """Balance over time: the starting month, then month ends, sampled to at most ``limit`` points."""
    points = [{"x": today.isoformat()[:7], "y": num(start)}] + [{"x": r["date"], "y": num(r["balance"])} for r in rows]
    if len(points) > limit:
        step = (len(points) - 1) / (limit - 1)
        points = [points[round(i * step)] for i in range(limit)]
    return points


def _plan_out(plan: Mapping[str, Any], debt: Mapping[str, Any], monthly_rows: int | None) -> dict:
    rows = plan["rows"]
    shown = rows if monthly_rows is None else rows[:monthly_rows]
    out = {"status": plan["status"], "months": plan["months"], "payoff_date": plan["date"],
           "total_interest": num(plan["interest"]), "total_iva": num(plan["iva"]) if debt["iva"] else None,
           "interest_cost": num(plan["interest"] + plan["iva"]),
           "total_paid": num(plan["paid"]), "first_payment": num(rows[0]["payment"]) if rows else None,
           "negative_amortization": plan["negative_amortization"],
           "yearly": _yearly(rows, debt["balance"]), "monthly": [_row_out(r) for r in shown],
           "monthly_rows_omitted": len(rows) - len(shown)}
    if plan["status"] != "ready":
        out["detail"] = plan.get("detail")
        out["balance_left"] = num(plan["left"])
    if "indexation" in plan:
        out["indexation"] = num(plan["indexation"])
        out["forgiven_at_30_years"] = num(plan["forgiven_at_30_years"])
    return out


# --------------------------------------------------------------------------- amortize


def amortize(debts: list[dict], today: date, monthly_rows: int | None = MONTHLY_ROWS_DEFAULT) -> dict:
    out, warnings, sources = [], [], []
    for debt in debts:
        if debt["indexed"]:
            plan = _indexed_plan(debt, today)
        else:
            plan = _plan(debt, today)
        row: dict[str, Any] = {
            "id": debt["id"], "name": debt["name"], "kind": debt["kind"], "currency": debt["currency"],
            "balance": num(debt["balance"]), "annual_rate": num(debt["rate"], 6), "rate_basis": debt["rate_basis"],
            "iva_on_interest": num(debt["iva"], 4), "effective_annual_rate": num(effective_annual(_monthly(debt)), 6),
            "payment_basis": debt["payment_basis"], "monthly_payment": num(debt["payment"]),
            "estimate": debt["estimate"], **_plan_out(plan, debt, monthly_rows)}
        row["balance_series"] = _series(debt["balance"], plan["rows"], today)
        if debt["cat"] is not None:
            row["cat"] = num(debt["cat"], 6)
            row["cat_vs_tasa"] = _cat_note(debt)
            sources.append(SOURCES["cat"])
        if debt["iva"]:
            sources.append(SOURCES["liva"])
        if debt["rule"] is not None:
            sources.append(SOURCES["minimum"])
            row["minimum_rule"] = {"percent_of_balance": num(debt["rule"]["percent_of_balance"], 4),
                                   "plus_interest": debt["rule"]["plus_interest"], "floor": num(debt["rule"]["floor"])}
            if debt["payment"] is not None:  # both known: what paying only the minimum costs instead
                minimum = _plan(debt, today, rule=True)
                row["if_minimum_only"] = {
                    "status": minimum["status"], "months": minimum["months"], "payoff_date": minimum["date"],
                    "total_interest": num(minimum["interest"] + minimum["iva"]),
                    "extra_cost": num(minimum["interest"] + minimum["iva"] - plan["interest"] - plan["iva"])
                    if minimum["status"] == "ready" and plan["status"] == "ready" else None}
        if debt["indexed"]:
            sources += [SOURCES["uma"], SOURCES["infonavit"]]
            row.update(denomination=debt["denomination"], balance_units=num(debt["units"], 4),
                       unit_value_mxn=num(debt["unit_value"]), unit_value_source=debt["unit_value_source"],
                       unit_growth_annual=num(debt["growth"], 4), monthly_payment_units=num(debt["payment_units"], 4),
                       monthly_payment=num(debt["payment_units"] * debt["unit_value"]), months_paid=debt["months_paid"],
                       liberation_applied=debt["liberation"], projection="partial" if debt["partial"] else "full")
            if debt["months_paid"] is None:
                warnings.append(f"{debt['id']}: partial projection. Months already paid are unknown, so the 30-year "
                                "liberation is left out; give months_paid or origination_date.")
            warnings.append(f"{debt['id']}: an estimate. The {debt['denomination']} rises every year, so pesos paid "
                            "and the balance in pesos depend on future updates; your Infonavit/Fovissste statement "
                            "in pesos is exact.")
            if plan.get("forgiven_at_30_years"):
                warnings.append(f"{debt['id']}: after 30 years of regular payments about "
                                f"{num(plan['forgiven_at_30_years'])} MXN would be left and cancelled; missed "
                                "payments lengthen this.")
        if plan["status"] != "ready":
            warnings.append(f"{debt['id']}: {plan.get('detail') or 'not repaid within the horizon'}.")
        if debt["kind"] == "mortgage" and debt["currency"] == "USD":
            row["note"] = "The payment should be principal and interest only; escrow for taxes and insurance is not debt."
        out.append(row)
    currencies = {d["currency"] for d in debts}
    result: dict[str, Any] = {"mode": "amortize", "as_of": today.isoformat(), "debts": out}
    if len(currencies) == 1:
        result["currency"] = next(iter(currencies))
        ready = [r for r in out if r["status"] == "ready"]
        result["totals"] = {"balance": num(sum((D(r["balance"]) for r in out), ZERO)),
                            "interest": num(sum((D(r["total_interest"]) + (D(r["total_iva"]) or ZERO)
                                                 for r in ready), ZERO)) if len(ready) == len(out) else None,
                            "months_to_debt_free": max(r["months"] for r in ready) if len(ready) == len(out) else None}
    return {"result": result, "warnings": warnings, "sources": _unique(sources),
            "assumptions": ["Interest accrues monthly at annual_rate / 12 (the tasa); IVA, where it applies, is added "
                            "to each month's interest.",
                            "Payments are made on time every month; no new charges, fees or rate changes."]}


def _cat_note(debt: Mapping[str, Any]) -> str:
    eff = effective_annual(_monthly(debt))
    if debt["rate_basis"] == "cat_estimate":
        return (f"Only the CAT ({num(debt['cat'] * 100, 1)}%) is known. CAT is the all-in annual cost used to compare "
                "credits (interest plus fees, without IVA); interest here accrues at its monthly equivalent, an estimate.")
    return (f"CAT {num(debt['cat'] * 100, 1)}% vs tasa {num(debt['rate'] * 100, 1)}%: interest is charged on the tasa "
            f"({num(debt['rate'] / 12 * 100, 2)}% a month" + (f" plus {num(debt['iva'] * 100, 0)}% IVA" if debt["iva"] else "")
            + f", {num(eff * 100, 1)}% a year compounded). The CAT adds fees and leaves out IVA; use it to compare offers.")


# --------------------------------------------------------------------------- strategies


def _simulate(debts: list[dict], budget: Decimal, order: list[str], today: date) -> dict:
    """Minimums first, then the rest of the budget down ``order``; a freed minimum rolls to the next debt."""
    bal = {d["id"]: d["balance"] for d in debts}
    by_id = {d["id"]: d for d in debts}
    pay_fn = {d["id"]: _payer(d) for d in debts}
    paid_off: dict[str, int] = {}
    interest = ZERO
    series = [{"x": today.isoformat()[:7], "y": num(sum(bal.values(), ZERO))}]
    month = 0
    while any(b > CENT for b in bal.values()) and month < MAX_MONTHS:
        month += 1
        charges = {}
        for key, b in bal.items():
            if b > CENT:
                charge = b * by_id[key]["rate"] / 12 * (ONE + by_id[key]["iva"])
                charges[key] = charge
                interest += charge
                bal[key] = b + charge
        remaining = budget
        for key in bal:
            if bal[key] > CENT:
                pay = min(pay_fn[key](month, bal[key] - charges[key], charges[key]), bal[key], max(remaining, ZERO))
                bal[key] -= pay
                remaining -= pay
        for key in order:
            if remaining <= 0:
                break
            if bal[key] > CENT:
                pay = min(remaining, bal[key])
                bal[key] -= pay
                remaining -= pay
        for key, b in bal.items():
            if b <= CENT and key not in paid_off:
                bal[key] = ZERO
                paid_off[key] = month
        series.append({"x": add_months(today, month).isoformat()[:7], "y": num(sum(bal.values(), ZERO))})
    if any(b > CENT for b in bal.values()):
        return {"status": "never", "detail": f"not repaid within {MAX_MONTHS} months at this budget", "order": order}
    if len(series) > SERIES_POINTS:
        step = (len(series) - 1) / (SERIES_POINTS - 1)
        series = [series[round(i * step)] for i in range(SERIES_POINTS)]
    return {"status": "ready", "months": month, "date": add_months(today, month).isoformat()[:7],
            "interest": num(interest), "order": order,
            "payoff": [{"id": k, "months": paid_off[k], "date": add_months(today, paid_off[k]).isoformat()[:7]}
                       for k in sorted(paid_off, key=lambda k: (paid_off[k], k))],
            "balance_series": series}


def _first_minimum(debt: Mapping[str, Any]) -> Decimal:
    """This month's minimum: the stated payment (or the card's rule), never more than the balance plus interest."""
    charge = debt["balance"] * _monthly(debt)
    return min(_payer(debt)(1, debt["balance"], charge), debt["balance"] + charge)


def strategies(debts: list[dict], budget: Any, today: date, *, order: list[str] | None = None,
               quick_win_months: Any = 3) -> dict:
    budget = D(budget)
    if budget is None or budget <= 0:
        raise ValueError("monthly_amount must be a positive number: the whole monthly budget for these debts")
    currencies = {d["currency"] for d in debts if d["currency"]}
    if len(currencies) > 1:
        raise ValueError(f"debts are in several currencies {sorted(currencies)}; run one currency at a time")
    if any(d["indexed"] for d in debts):
        raise ValueError("strategies compares debts in money; run an Infonavit/Fovissste credit with mode amortize")
    currency = next(iter(currencies), None)
    minimums = sum((_first_minimum(d) for d in debts), ZERO)
    if budget < minimums:
        return {"status": "needs_input", "result": {"mode": "strategies", "minimum_payments": num(minimums),
                                                    "currency": currency},
                "missing": [{"key": "monthly_amount", "reason": "invalid",
                             "detail": f"{num(budget)} is below this month's minimum payments {num(minimums)}"}]}
    weeks = D(quick_win_months)
    if weeks is None or weeks <= 0:
        raise ValueError("quick_win_months must be positive")
    extra = budget - minimums
    ids = {d["id"] for d in debts}
    avalanche = [d["id"] for d in sorted(debts, key=lambda d: (-_monthly(d), d["balance"], d["id"]))]
    snowball = [d["id"] for d in sorted(debts, key=lambda d: (d["balance"], -_monthly(d), d["id"]))]
    quick = [d for d in debts if d["balance"] <= extra * weeks]
    hybrid = [d["id"] for d in sorted(quick, key=lambda d: (d["balance"], d["id"]))]
    hybrid += [i for i in avalanche if i not in hybrid]
    plans = {"avalanche": avalanche, "snowball": snowball, "hybrid": hybrid}
    if order is not None:
        if not isinstance(order, list) or set(order) != ids or len(order) != len(ids):
            raise ValueError(f"order must list each debt id exactly once: {sorted(ids)}")
        plans["custom"] = list(order)
    runs = {name: _simulate(debts, budget, ids_, today) for name, ids_ in plans.items()}
    minimum_runs = [{"id": d["id"], **_minimum_only(d, today)} for d in debts]
    base = runs["avalanche"]
    rows = []
    for name, run in runs.items():
        row = {"strategy": name, "status": run["status"], "months": run.get("months"), "payoff_date": run.get("date"),
               "interest": run.get("interest"), "order": run["order"], "payoff": run.get("payoff"),
               "extra_interest_vs_avalanche": num(D(run["interest"]) - D(base["interest"]))
               if run["status"] == base["status"] == "ready" else None,
               "months_vs_avalanche": run["months"] - base["months"] if run["status"] == base["status"] == "ready" else None}
        rows.append(row)
    ready = [r for r in rows if r["status"] == "ready"]
    best = min(ready, key=lambda r: (D(r["interest"]), r["months"], r["strategy"] != "avalanche"))["strategy"] if ready else None
    all_minimum = all(m["status"] == "ready" for m in minimum_runs)
    minimum_interest = sum((D(m["interest"]) for m in minimum_runs), ZERO) if all_minimum else None
    first_win = {name: (runs[name].get("payoff") or [{}])[0].get("months") for name in runs}
    result = {
        "mode": "strategies", "currency": currency, "as_of": today.isoformat(), "monthly_amount": num(budget),
        "minimum_payments": num(minimums), "extra_each_month": num(extra), "strategies": rows, "best": best,
        "interest_difference": {"snowball_minus_avalanche": rows[1]["extra_interest_vs_avalanche"],
                                "hybrid_minus_avalanche": rows[2]["extra_interest_vs_avalanche"]},
        "first_debt_cleared_months": first_win,
        "minimums_only": {"debts": minimum_runs, "interest": num(minimum_interest),
                          "interest_saved_by_budget": num(minimum_interest - D(base["interest"]))
                          if minimum_interest is not None and base["status"] == "ready" else None},
        "balance_series": {name: run.get("balance_series") or [] for name, run in runs.items()},
        "hybrid_rule": f"Debts the extra {num(extra)} a month clears within {num(weeks, 1)} months go first, "
                       "smallest first (quick wins); the rest by highest rate.",
    }
    return {"status": "ready", "result": result, "missing": [], "warnings": [],
            "assumptions": ["Every debt receives its minimum first (this month's minimum for a card on the minimum "
                            "rule); the rest of the budget goes to one debt at a time in the strategy's order, and a "
                            "freed minimum rolls into the next debt.",
                            "Rates are fixed; IVA on interest where it applies; no new borrowing or fees.",
                            "Avalanche (highest rate first) always pays the least interest; snowball (smallest balance "
                            "first) and hybrid buy earlier wins at the interest difference shown."]}


def _minimum_only(debt: Mapping[str, Any], today: date) -> dict:
    plan = _plan(debt, today)
    return {"status": plan["status"], "months": plan["months"], "date": plan["date"],
            "interest": num(plan["interest"] + plan["iva"])}


# --------------------------------------------------------------------------- prepay vs invest


def _monthly_from_annual(rate: Decimal) -> Decimal:
    return (ONE + rate) ** (ONE / 12) - ONE


def _paths(balance: Decimal, debt_monthly: Decimal, payment: Decimal, extra: Decimal, lump: Decimal, horizon: int,
           invest_monthly: Decimal, gains_tax: Decimal, basis_monthly: Decimal = ZERO) -> dict:
    """Net worth at the horizon when the extra goes to the debt vs into the investment.

    Both paths spend the same each month (payment + extra).  Once a debt is gone its whole payment is invested.
    ``basis_monthly`` updates the cost basis each month (Mexico taxes the gain over the inflation-updated cost).
    """
    out = {}
    for path in ("prepay", "invest"):
        bal = balance
        inv = basis = ZERO
        if path == "prepay":
            used = min(lump, bal)
            bal -= used
            inv = basis = lump - used
        else:
            inv = basis = lump
        payoff_month = None
        interest = ZERO
        for month in range(1, horizon + 1):
            inv *= ONE + invest_monthly
            basis *= ONE + basis_monthly
            budget = payment + extra
            if bal > CENT:
                charge = bal * debt_monthly
                interest += charge
                due = bal + charge
                pay = min(payment + (extra if path == "prepay" else ZERO), due)
                bal = due - pay
                budget -= pay
                if bal <= CENT:
                    bal, payoff_month = ZERO, month
            inv += budget
            basis += budget
        tax = max(inv - basis, ZERO) * gains_tax
        out[path] = {"net_worth": inv - tax - max(bal, ZERO), "investments": inv - tax, "debt_left": max(bal, ZERO),
                     "debt_paid_off_month": payoff_month if balance > 0 else 0, "interest": interest}
    return out


def _after_tax_lump(rate: Decimal, gains_tax: Decimal, years: Decimal, inflation: Decimal = ZERO) -> Decimal:
    """Annualised after-tax return of a lump held ``years`` and taxed at the end on the gain over its cost,
    the cost updated by ``inflation`` (Mexico: the real gain; zero leaves the nominal gain)."""
    if years <= 0:
        return rate - max(rate - inflation, ZERO) * gains_tax
    grown = (ONE + rate) ** years
    cost = (ONE + inflation) ** years
    return (grown - max(grown - cost, ZERO) * gains_tax) ** (ONE / years) - ONE


def prepay_vs_invest(debt: dict, inputs: Mapping[str, Any], today: date, *, jurisdiction: str | None,
                     reserve: Mapping[str, Any] | None, risk_free: Mapping[str, Any] | None) -> dict:
    from . import mexico
    if debt["indexed"]:
        raise ValueError("prepay_vs_invest needs a debt in money; for Infonavit/Fovissste in VSM/UMA use mode amortize")
    extra = _money_in(inputs.get("extra_monthly"), "extra_monthly") or ZERO
    lump = _money_in(inputs.get("lump_sum"), "lump_sum") or ZERO
    if extra <= 0 and lump <= 0:
        raise ValueError("prepay_vs_invest needs extra_monthly or lump_sum (a positive amount)")
    currency = debt["currency"]
    jurisdiction = (jurisdiction or ("MX" if currency == "MXN" else "US" if currency == "USD" else None))
    if jurisdiction not in ("MX", "US"):
        raise ValueError("jurisdiction must be MX or US (or give the debt a currency of MXN or USD)")
    missing: list[dict] = []
    warnings: list[str] = []
    assumptions: list[str] = []
    sources: list[dict] = []
    payment = debt["payment"]
    if payment is None:  # a card on the minimum rule: this month's minimum, held fixed
        payment = _first_minimum(debt)
        assumptions.append(f"The card's payment is held at this month's minimum ({num(payment)}).")
    baseline = _plan(debt, today, payment=payment)
    horizon = inputs.get("horizon_months")
    if horizon is None:
        if baseline["status"] != "ready":
            raise ValueError("the current payment never repays this debt; give horizon_months")
        horizon = min(baseline["months"], 360)
        assumptions.append(f"The horizon is the debt's payoff at the current payment: {horizon} months.")
    if not isinstance(horizon, int) or isinstance(horizon, bool) or not 1 <= horizon <= MAX_MONTHS:
        raise ValueError(f"horizon_months must be a whole number from 1 to {MAX_MONTHS}")
    years = Decimal(horizon) / 12

    # -- tax on the debt side: which cases does the unknown leave open?
    marginal = _ratio(inputs.get("marginal_rate"), "marginal_rate", high=ONE)
    marginals = [marginal] if marginal is not None else list(MARGINAL_BOUNDS[jurisdiction])
    if marginal is None:
        missing.append({"key": "marginal_rate", "reason": "missing",
                        "detail": "Your marginal income-tax rate; the range uses the lowest and highest brackets."})
    deduction_share: list[Decimal] = [ZERO]
    deduction_note = "No interest deduction applies to this debt."
    if debt["kind"] in ("mortgage", *INDEXED_KINDS) and jurisdiction == "US":
        sources.append(SOURCES["irs936"])
        principal = debt["original_principal"] or debt["balance"]
        share = min(ONE, US_MORTGAGE_DEBT_LIMIT / principal) if principal > 0 else ONE
        itemizes = inputs.get("itemizes")
        if itemizes is True:
            standard = _money_in(inputs.get("standard_deduction"), "standard_deduction")
            other = _money_in(inputs.get("other_itemized_deductions"), "other_itemized_deductions")
            yearly = debt["balance"] * debt["rate"] * share
            if standard is not None and other is not None and yearly > 0:
                # Only the itemized total above the standard deduction saves tax.
                above = max(other + yearly, standard) - max(other, standard)
                share = share * above / yearly
                deduction_note = (f"You itemize: about {num(share * 100, 0)}% of the mortgage interest is above the "
                                  "standard deduction and lowers tax at your marginal rate.")
            else:
                deduction_note = "You itemize, so mortgage interest is deducted at your marginal rate."
                assumptions.append("All mortgage interest is taken as deductible at your marginal rate; it only saves "
                                   "tax on the part of your itemized deductions above the standard deduction (give "
                                   "standard_deduction and other_itemized_deductions to apply that hurdle).")
            deduction_share = [share]
        elif itemizes is False:
            deduction_note = "You take the standard deduction, so mortgage interest saves no tax."
        else:
            deduction_share = [share, ZERO]
            deduction_note = "Whether you itemize is unknown: the range covers both."
            missing.append({"key": "itemizes", "reason": "missing",
                            "detail": "Do you itemize deductions? Mortgage interest only lowers tax if you do."})
        if share < 1:
            warnings.append("The mortgage is above $750,000 of acquisition debt; only that share of interest is deductible.")
    elif debt["kind"] == "student" and jurisdiction == "US":
        sources.append(SOURCES["irs970"])
        yearly = debt["balance"] * debt["rate"]
        cap = min(ONE, US_STUDENT_INTEREST_CAP / yearly) if yearly > 0 else ONE
        if inputs.get("student_loan_deduction") is False:
            deduction_note = "The student-loan interest deduction does not apply to you, so the interest saves no tax."
        else:
            deduction_share = [cap]
            deduction_note = (f"Student-loan interest is deductible above the line up to $2,500 a year (IRC 221), "
                              f"itemizing or not: about {num(cap * 100, 0)}% of this interest lowers tax.")
            assumptions.append("The student-loan interest deduction phases out with modified AGI; it is taken in full "
                               "here (set student_loan_deduction to false if your income is above the phase-out).")
    elif debt["kind"] in ("mortgage", *INDEXED_KINDS) and jurisdiction == "MX":
        sources.append(SOURCES["art151"])
        inflation = _ratio(inputs.get("inflation"), "inflation", high=ONE)
        if inflation is None:
            inflation = MX_INFLATION_ASSUMED
            assumptions.append(f"Mexican inflation is taken at {num(inflation * 100, 1)}% a year (real interest).")
        raw = dict(inputs.get("mx_mortgage") or {})
        yearly_interest = debt["balance"] * debt["rate"]
        raw.setdefault("nominal_interest_paid_mxn", str(yearly_interest))
        raw.setdefault("inflation_adjustment_mxn", str(debt["balance"] * inflation))
        detail, gaps, notes = mexico._mortgage_real_interest(raw)
        warnings += notes
        deductible = detail.get("_deductible") if detail else None
        within = raw.get("within_global_cap")
        share = deductible / yearly_interest if deductible is not None and yearly_interest > 0 else None
        if share is not None and within is True:
            deduction_share = [share]
            deduction_note = (f"Real interest (Art. 151 fr. IV) is deductible: about {num(share * 100, 0)}% of the "
                              "interest lowers tax at your marginal rate.")
        elif share is not None and within is False:
            deduction_note = "Your personal deductions already fill the Art. 151 global cap, so the interest saves no tax."
        else:
            deduction_share = [share if share is not None else (max(debt["rate"] - inflation, ZERO) / debt["rate"]
                                                                  if debt["rate"] > 0 else ZERO), ZERO]
            deduction_note = "Whether the real-interest deduction applies is not confirmed: the range covers both."
            missing += [{"key": g, "reason": "missing", "detail": "Art. 151 fr. IV mortgage deduction"} for g in gaps]
            if within is None:
                missing.append({"key": "mx_mortgage.within_global_cap", "reason": "missing",
                                "detail": "Does the deduction fit within the Art. 151 global cap (the lesser of five "
                                          "annual UMAs or 15% of income)?"})

    # -- the investing side
    expected = inputs.get("expected_return")
    table = EXPECTED_RETURNS.get(currency)
    if isinstance(expected, Mapping):
        conservative = _ratio(expected.get("conservative"), "expected_return.conservative", high=ONE)
        base = _ratio(expected.get("base"), "expected_return.base", high=ONE)
        if conservative is None or base is None:
            raise ValueError("expected_return needs conservative and base (decimals)")
        return_source = str(expected.get("source") or "stated in the request")
    elif table:
        conservative, base, return_source = table["conservative"], table["base"], table["source"]
        assumptions.append(return_source + ".")
    else:
        raise ValueError(f"expected_return {{conservative, base, source}} is needed for {currency}")
    if conservative > base:
        raise ValueError("expected_return.conservative must not exceed base")
    account = inputs.get("account", "taxable")
    if account not in ("taxable", "tax_free"):
        raise ValueError("account must be taxable or tax_free (a Roth IRA, or a PPR held to 65)")
    inflation_mx = _ratio(inputs.get("inflation"), "inflation", high=ONE) or MX_INFLATION_ASSUMED
    channel = inputs.get("investment_channel")
    if channel not in (None, "mx_intermediary", "foreign_broker"):
        raise ValueError("investment_channel must be mx_intermediary (a Mexican casa de bolsa) or foreign_broker")
    gains_inflation = ZERO      # annual inflation that updates the cost basis (Mexico taxes the real gain)
    gains_at_marginal = False   # gains taxed at each case's marginal rate instead of a flat rate
    if account == "tax_free":
        gains_tax = ZERO
    elif jurisdiction == "MX":
        gains_inflation = inflation_mx
        sic = inputs.get("sic_listed")
        art129 = D(mexico.PARAMETERS["art129_rate"]["any"]["value"])
        if channel == "foreign_broker" and sic is not True:
            gains_tax, gains_at_marginal = None, True
            assumptions.append("Held through a foreign broker and not listed in the SIC: the gain is taxed at your "
                               "marginal rate (LISR Title IV, Chapter IV) on the real gain, the cost updated by "
                               f"inflation ({num(inflation_mx * 100, 1)}% a year); give sic_listed true if it is in the SIC.")
        else:
            gains_tax = art129
            sources.append(SOURCES["art129"])
            if channel is None:
                assumptions.append("The investment is taken to be listed on the BMV/BIVA or in the SIC and held through "
                                   "a Mexican intermediary (casa de bolsa); through a foreign broker, a security not in "
                                   "the SIC is taxed at your marginal rate instead (give investment_channel).")
            assumptions.append("LISR Art. 129: a 10% definitive tax on the real gain (proceeds less the cost updated by "
                               f"the INPC, taken at {num(inflation_mx * 100, 1)}% a year) from shares and ETFs listed on "
                               "the BMV/BIVA or in the SIC and sold through a Mexican intermediary; SIC securities "
                               "through a foreign broker keep the 10% but you compute and pay it.")
    else:
        gains_tax = _ratio(inputs.get("capital_gains_rate"), "capital_gains_rate", high=ONE)
        if gains_tax is None:
            gains_tax = US_LTCG_ASSUMED
            assumptions.append("US long-term capital gains are taxed at 15% when sold at the horizon (the common "
                               "bracket; give capital_gains_rate for yours).")
    investment = str(inputs.get("investment") or ("an equity index fund"))
    rf_rate = D(risk_free.get("rate")) if risk_free else None
    if rf_rate is None:
        missing.append({"key": "risk_free", "reason": "missing",
                        "detail": ("the CETES rate" if jurisdiction == "MX" else "the T-bill rate")
                                  + " {rate, source}; save cash_reference_rate for the currency"})
    else:
        sources.append(SOURCES["cetes_tax" if jurisdiction == "MX" else "tbill_tax"])
    federal = _ratio(inputs.get("federal_marginal_rate"), "federal_marginal_rate", high=ONE)

    cases = []
    for m in marginals:
        for share in dict.fromkeys(deduction_share):
            monthly = debt["rate"] / 12 * (ONE + debt["iva"]) - debt["rate"] / 12 * m * share
            debt_eff = effective_annual(monthly)
            if rf_rate is None:
                rf_after = None
            elif jurisdiction == "MX":
                rf_after = rf_rate - m * max(rf_rate - inflation_mx, ZERO)
            else:
                rf_after = rf_rate * (ONE - (federal if federal is not None else m))
            gains = m if gains_at_marginal else gains_tax
            cons_after = _after_tax_lump(conservative, gains, years, gains_inflation)
            base_after = _after_tax_lump(base, gains, years, gains_inflation)
            verdict, confidence = _verdict(debt_eff, rf_after, cons_after, base_after)
            cases.append({"marginal_rate": num(m, 4), "deduction_share": num(share, 4), "_monthly": monthly,
                          "_gains_tax": gains, "gains_tax": gains,
                          "debt_after_tax_rate": debt_eff, "risk_free_after_tax": rf_after,
                          "conservative_after_tax": cons_after, "base_after_tax": base_after,
                          "verdict": verdict, "confidence": confidence})
    central = cases[0]
    scenarios = {}
    basis_monthly = _monthly_from_annual(gains_inflation)
    for name, pre, invest_monthly in (
            ("risk_free", rf_rate, _monthly_from_annual(central["risk_free_after_tax"]) if rf_rate is not None else None),
            ("conservative", conservative, _monthly_from_annual(conservative)),
            ("base", base, _monthly_from_annual(base))):
        if invest_monthly is None:
            scenarios[name] = {"annual_return": None, "net_worth_difference": None, "winner": None}
            continue
        safe = name == "risk_free"
        values = [_paths(debt["balance"], c["_monthly"], payment, extra, lump, horizon,
                         _monthly_from_annual(c["risk_free_after_tax"]) if safe else invest_monthly,
                         ZERO if safe else c["_gains_tax"], ZERO if safe else basis_monthly)
                  for c in cases]
        diffs = [v["prepay"]["net_worth"] - v["invest"]["net_worth"] for v in values]
        paths = values[0]
        scenarios[name] = {
            "annual_return": num(pre, 4), "after_tax_annual": num(central[{"risk_free": "risk_free_after_tax",
                                                                           "conservative": "conservative_after_tax",
                                                                           "base": "base_after_tax"}[name]], 4),
            "net_worth_if_prepay": num(paths["prepay"]["net_worth"]), "net_worth_if_invest": num(paths["invest"]["net_worth"]),
            "net_worth_difference": num(diffs[0]),
            "net_worth_difference_range": [num(min(diffs)), num(max(diffs))] if len(diffs) > 1 else None,
            "winner": "prepay" if diffs[0] > 0 else "invest" if diffs[0] < 0 else "tie",
            "debt_paid_off_month_if_prepay": paths["prepay"]["debt_paid_off_month"],
            "debt_paid_off_month_if_invest": paths["invest"]["debt_paid_off_month"],
            "interest_saved_by_prepaying": num(paths["invest"]["interest"] - paths["prepay"]["interest"])}
    breakeven = _breakeven(debt["balance"], central["_monthly"], payment, extra, lump, horizon, central["_gains_tax"],
                           basis_monthly)
    verdicts = {(c["verdict"], c["confidence"]) for c in cases}
    kinds = {v for v, _ in verdicts}
    if len(kinds) == 1:
        verdict = kinds.pop()
        order = ["low", "medium", "high"]
        confidence = min((c for _, c in verdicts), key=order.index)
        decided_by = []
    else:
        verdict, confidence = "depends", "low"
        decided_by = [m["key"] for m in missing if m["key"] in ("marginal_rate", "itemizes", "mx_mortgage.within_global_cap")
                      or m["key"].startswith("mortgage.")]
    reserve_status = _reserve_status(reserve)
    # A 20%+ consumer debt only waits for a starter reserve (one month of essentials); lower-rate debt
    # (a mortgage, a car) waits for the full target.
    high = debt["kind"] not in ("mortgage", *INDEXED_KINDS) and effective_annual(_monthly(debt)) >= HIGH_INTEREST_RATE
    parallel = False
    condition = None
    if reserve_status == "below_starter" or (reserve_status == "below_target" and not high):
        verdict, confidence = "build_reserve_first", "high"
        warnings.append(("Your emergency reserve is below one month of essentials: set that month aside first, then "
                         "this debt is your best investment." if high else
                         "Your emergency reserve is below its target: put the extra there first.")
                        + " Money prepaid into a debt cannot be taken back out in an emergency.")
    elif reserve_status == "below_target":  # high-rate debt with at least a starter reserve
        verdict, confidence, parallel = "prepay", "high", True
        warnings.append("You have at least a month of reserve: pay this debt first and keep building the reserve to "
                        "its target in parallel.")
    elif reserve_status == "unknown" and verdict in ("prepay", "close_call", "depends"):
        condition = ("Only once you have at least one month of essentials set aside (not known yet)." if high else
                     "Only once your emergency reserve is at its target (not known yet).")
        missing.append({"key": "reserve", "reason": "missing",
                        "detail": "Reserve months and target: prepaying only makes sense with the reserve "
                                  + ("at one month or more." if high else "full.")})
    prepay_allowed = verdict != "build_reserve_first"
    guaranteed = {
        "after_tax_rate": num(central["debt_after_tax_rate"], 4),
        "after_tax_rate_range": _range([c["debt_after_tax_rate"] for c in cases]),
        "certainty": "guaranteed",
        "explain": "Money prepaid earns exactly the debt's after-tax rate, "
                   f"{num(central['debt_after_tax_rate'] * 100, 2)}% a year, with no risk: it is interest you "
                   "stop paying. The money is locked in the debt until it is repaid.",
        "tax": deduction_note,
    }
    investing = {
        "investment": investment, "certainty": "uncertain",
        "expected_return": {"conservative": num(conservative, 4), "base": num(base, 4), "source": return_source},
        "after_tax": {"risk_free": num(central["risk_free_after_tax"], 4), "conservative": num(central["conservative_after_tax"], 4),
                      "base": num(central["base_after_tax"], 4)},
        "risk_free": ({"rate": num(rf_rate, 4), "name": risk_free.get("name"), "source": risk_free.get("source"),
                       "as_of": risk_free.get("as_of")} if rf_rate is not None else None),
        "gains_tax": num(central["_gains_tax"], 4), "account": account,
        "gains_tax_basis": ("none" if account == "tax_free" else
                            "real gain (cost updated by inflation)" if jurisdiction == "MX" else "nominal gain"),
        "gains_tax_rule": ("tax-free account" if account == "tax_free" else
                           "marginal rate (foreign broker, not in the SIC)" if gains_at_marginal else
                           "LISR Art. 129: 10% definitive (SIC-listed, through a foreign broker: you compute and pay it)"
                           if jurisdiction == "MX" and channel == "foreign_broker" else
                           "LISR Art. 129: 10% definitive (BMV/SIC via a Mexican intermediary)" if jurisdiction == "MX"
                           else "US long-term capital gains rate"),
        "after_tax_range": ({key: _range([c[field] for c in cases if c[field] is not None])
                             for key, field in (("risk_free", "risk_free_after_tax"),
                                                ("conservative", "conservative_after_tax"),
                                                ("base", "base_after_tax"))} if len(cases) > 1 else None),
        "marginal_rate_known": marginal is not None,
        "marginal_label": (None if marginal is not None else
                           {"en": f"lowest bracket shown; range by your marginal rate ({num(marginals[0] * 100, 2)}%-"
                                  f"{num(marginals[-1] * 100, 2)}%)",
                            "es": f"se muestra la tasa más baja; rango según tu tasa marginal ({num(marginals[0] * 100, 2)}%-"
                                  f"{num(marginals[-1] * 100, 2)}%)"}),
        "explain": ("Investing returns are expected, not promised: markets can return less than the conservative "
                    "case or lose money over years, and the gap between the cases is that risk."),
    }
    text = _verdict_text(verdict, guaranteed["after_tax_rate"], investing["after_tax"], breakeven, investment)
    if parallel:
        text = {"en": text["en"] + " Keep building your reserve to its target in parallel.",
                "es": text["es"] + " Sigue juntando tu reserva hasta la meta en paralelo."}
    elif verdict == "build_reserve_first" and high:
        text = {"en": "Set aside one month of essentials first; after that, this debt is your best investment.",
                "es": "Primero junta un mes de gastos esenciales; después de juntar un mes de reserva, esta deuda es tu "
                      "mejor inversión."}
    result = {
        "mode": "prepay_vs_invest", "as_of": today.isoformat(), "currency": currency, "jurisdiction": jurisdiction,
        "debt": {"id": debt["id"], "name": debt["name"], "kind": debt["kind"], "balance": num(debt["balance"]),
                 "annual_rate": num(debt["rate"], 6), "monthly_payment": num(payment),
                 "months_left_at_current_payment": baseline["months"]},
        "extra_monthly": num(extra), "lump_sum": num(lump), "horizon_months": horizon,
        "guaranteed": guaranteed, "investing": investing, "scenarios": scenarios,
        "breakeven": {"pre_tax_return": num(breakeven, 4) if breakeven is not None else None,
                      "after_tax_return": num(central["debt_after_tax_rate"], 4),
                      "explain": "Investing must earn more than this before tax (after tax: more than the debt's "
                                 "after-tax rate) for the extra to be worth more invested than prepaid."
                      if breakeven is not None else "No return in -90%..300% changes the answer at this horizon."},
        "verdict": verdict, "confidence": confidence, "verdict_text": text, "condition": condition,
        "decided_by": decided_by, "reserve": {"status": reserve_status, "in_parallel": parallel,
                                                   "rule": "starter (one month)" if high else "full target", **({k: reserve.get(k) for k in ("months", "target_months")}
                                                                           if reserve else {})},
        "prepay_recommended": verdict == "prepay" and prepay_allowed,
        "cases": [{k: (num(v, 4) if isinstance(v, Decimal) else v) for k, v in c.items() if not k.startswith("_")} for c in cases],
    }
    return {"result": result, "missing": missing, "warnings": warnings, "sources": _unique(sources),
            "assumptions": assumptions + [
                "Both paths spend the same each month (payment plus extra); once a debt is repaid its payment is invested.",
                "Investment gains are taxed when sold at the horizon; risk-free interest is taxed as it is earned.",
                "Rates are fixed; the investment return is constant (the range shows how the answer moves)."]}


def _verdict(debt: Decimal, rf: Decimal | None, cons: Decimal, base: Decimal) -> tuple[str, str]:
    if debt >= base:
        return "prepay", "high"
    if debt >= cons:
        return "close_call", "low"
    if rf is not None and debt < rf:
        return "invest", "high"
    return "invest", "medium"


def _verdict_text(verdict: str, debt_rate: Any, after: Mapping[str, Any], breakeven: Decimal | None,
                  investment: str) -> dict:
    pct = lambda v: "—" if v is None else f"{num(Decimal(str(v)) * 100, 2)}%"  # noqa: E731
    d, base, cons = pct(debt_rate), pct(after.get("base")), pct(after.get("conservative"))
    be = pct(breakeven)
    texts = {
        "prepay": (f"Pay the debt down: it returns a guaranteed {d} after tax, more than {investment} is expected to "
                   f"earn after tax even in the base case ({base}).",
                   f"Paga la deuda: te da un {d} garantizado después de impuestos, más de lo que se espera de "
                   f"{investment} después de impuestos incluso en el caso base ({base})."),
        "close_call": (f"A close call: the debt's guaranteed {d} beats the conservative case ({cons}) but not the base "
                       f"case ({base}); investing wins only if returns beat {be} before tax. Splitting the extra is reasonable.",
                       f"Es una decisión cerrada: el {d} garantizado de la deuda supera el caso conservador ({cons}) pero "
                       f"no el base ({base}); invertir gana sólo si rinde más de {be} antes de impuestos. Dividir el extra es razonable."),
        "invest": (f"Invest the extra: {investment} is expected to earn {cons}–{base} after tax, above the debt's "
                   f"guaranteed {d}; that edge is expected, not guaranteed.",
                   f"Invierte el extra: se espera que {investment} rinda {cons}–{base} después de impuestos, más que el "
                   f"{d} garantizado de la deuda; esa ventaja es esperada, no garantizada."),
        "depends": ("The answer depends on your taxes: see decided_by for what settles it.",
                    "La respuesta depende de tus impuestos: decided_by dice qué la define."),
        "build_reserve_first": ("Build your emergency reserve first; compare prepaying and investing once it is full.",
                                "Primero completa tu fondo de emergencia; después compara pagar la deuda contra invertir."),
    }
    en, es = texts[verdict]
    return {"en": en, "es": es}


def _breakeven(balance, debt_monthly, payment, extra, lump, horizon, gains_tax, basis_monthly=ZERO) -> Decimal | None:
    def gap(rate: Decimal) -> Decimal:
        paths = _paths(balance, debt_monthly, payment, extra, lump, horizon, _monthly_from_annual(rate), gains_tax,
                       basis_monthly)
        return paths["invest"]["net_worth"] - paths["prepay"]["net_worth"]
    low, high = Decimal("-0.9"), Decimal("3")
    g_low, g_high = gap(low), gap(high)
    if g_low == 0:
        return low
    if (g_low > 0) == (g_high > 0):
        return None
    for _ in range(40):
        mid = (low + high) / 2
        g_mid = gap(mid)
        if (g_mid > 0) == (g_high > 0):
            high, g_high = mid, g_mid
        else:
            low, g_low = mid, g_mid
        if high - low < Decimal("0.00001"):
            break
    return (low + high) / 2


def _reserve_status(reserve: Mapping[str, Any] | None) -> str:
    """below_starter (under one month of essentials), below_target, at_target, or unknown."""
    if not reserve:
        return "unknown"
    months, target = D(reserve.get("months")), D(reserve.get("target_months"))
    if months is not None and months < STARTER_RESERVE_MONTHS:
        return "below_starter"
    if months is None or target is None:
        return "unknown"
    return "below_target" if months < target else "at_target"


def _range(values: list[Decimal]) -> list | None:
    if len(values) < 2 or min(values) == max(values):
        return None
    return [num(min(values), 4), num(max(values), 4)]


# --------------------------------------------------------------------------- refinance, balance transfer, consolidation


def refinance(debts: list[dict], offer: Mapping[str, Any], today: date) -> dict:
    if not isinstance(offer, Mapping):
        raise ValueError("offer must be an object {kind, annual_rate, fee?, fee_percent?, term_months?, promo_rate?, promo_months?}")
    kind = offer.get("kind", "consolidation" if len(debts) > 1 else "refinance")
    if kind not in ("refinance", "balance_transfer", "consolidation"):
        raise ValueError("offer.kind must be refinance, balance_transfer or consolidation")
    if any(d["indexed"] for d in debts):
        raise ValueError("refinance compares debts in money; convert the Infonavit/Fovissste balance to pesos first")
    currencies = {d["currency"] for d in debts}
    if len(currencies) > 1:
        raise ValueError("an offer replaces debts in one currency")
    currency = next(iter(currencies))
    rate = _ratio(offer.get("annual_rate"), "offer.annual_rate")
    promo_rate = _ratio(offer.get("promo_rate"), "offer.promo_rate")
    assumed_rate = None
    if rate is None and promo_rate is not None and len({d["rate"] for d in debts}) == 1:
        # "0% for 12 months" with no rate after it: assume the rate they pay today (stated, never hidden).
        rate = assumed_rate = debts[0]["rate"]
    if rate is None:
        raise ValueError("offer.annual_rate is required (the rate after any promo; the tasa)")
    promo_months = offer.get("promo_months")
    if (promo_rate is None) != (promo_months is None):
        raise ValueError("offer.promo_rate and offer.promo_months go together")
    if promo_months is not None and (not isinstance(promo_months, int) or promo_months <= 0):
        raise ValueError("offer.promo_months must be a positive whole number")
    balance = sum((d["balance"] for d in debts), ZERO)
    fee = (_money_in(offer.get("fee"), "offer.fee") or ZERO) + balance * (_ratio(offer.get("fee_percent"), "offer.fee_percent",
                                                                                   high=ONE) or ZERO)
    financed = offer.get("fees_financed", kind != "refinance")
    iva, iva_note = _iva(offer, "card" if kind == "balance_transfer" else "personal" if kind == "consolidation"
                         else debts[0]["kind"], currency)
    assumptions = [iva_note] if iva_note else []
    if offer.get("fee") is None and offer.get("fee_percent") is None:
        assumptions.append("No fee was given, so the offer's fees are taken as 0 (sin comisión indicada; las "
                           "transferencias suelen cobrar 3–5%).")
    if assumed_rate is not None:
        assumptions.append(f"The offer gave no rate after the promotion: it is assumed to be your current rate "
                           f"({num(assumed_rate * 100, 2)}% a year); the offer's terms are exact.")
    current_payment = sum((d["payment"] if d["payment"] is not None else _first_minimum(d) for d in debts), ZERO)
    term = offer.get("term_months")
    if offer.get("monthly_payment") is not None:
        payment, basis = D(offer["monthly_payment"]), "offer's payment"
    elif term is not None:
        if not isinstance(term, int) or term <= 0:
            raise ValueError("offer.term_months must be a positive whole number")
        payment, basis = payment_for_term(balance + (fee if financed else ZERO), rate, iva, term), "offer's term"
    else:
        payment, basis = current_payment, "your current payment"
        assumptions.append(f"The new debt is paid at your current payment ({num(current_payment)} a month).")
    start = balance + (fee if financed else ZERO)
    current = _simulate(debts, current_payment, [d["id"] for d in sorted(debts, key=lambda d: -_monthly(d))], today)
    current_rows = _current_costs(debts, current_payment, today)
    rate_for = (lambda m: promo_rate if promo_months is not None and m <= promo_months else rate)
    new = _schedule(start, rate_for, iva, lambda _m, _b, _c: payment, today)
    offer_cost = new["interest"] + new["iva"] + fee
    current_cost = D(current.get("interest")) if current["status"] == "ready" else None
    saved = current_cost - offer_cost if current_cost is not None and new["status"] == "ready" else None
    # Breakeven: the first month the offer's cumulative cost (fees on day one, then interest) is below the current path's.
    cum_new, cum_old, breakeven = fee, ZERO, None
    for month in range(1, max(len(new["rows"]), len(current_rows)) + 1):
        if month <= len(new["rows"]):
            cum_new += new["rows"][month - 1]["interest"] + new["rows"][month - 1]["iva"]
        if month <= len(current_rows):
            cum_old += current_rows[month - 1]
        if cum_old >= cum_new:
            breakeven = month
            break
    risk: dict[str, Any] = {"promo_months": promo_months}
    warnings: list[str] = []
    if promo_months is not None:
        at_end = new["rows"][promo_months - 1]["balance"] if len(new["rows"]) >= promo_months else ZERO
        to_clear = annuity_payment(start, promo_rate * (ONE + iva), promo_months) if promo_rate is not None else None
        after = sum((r["interest"] + r["iva"] for r in new["rows"][promo_months:]), ZERO)
        risk.update(promo_ends_before_payoff=at_end > CENT, balance_at_promo_end=num(at_end),
                    payment_to_clear_within_promo=num(to_clear), interest_after_promo=num(after),
                    go_to_rate=num(rate, 4))
        if at_end > CENT:
            warnings.append(f"The promo ends in month {promo_months} with {num(at_end)} still owed, which then accrues "
                            f"at {num(rate * 100, 1)}%: {num(after)} of interest after the promo. Paying "
                            f"{num(to_clear)} a month clears it inside the promo.")
        if offer.get("deferred_interest") is True:
            retro = start * rate / 12 * promo_months * (ONE + iva)  # back interest on the promo balance
            risk["deferred_interest_if_not_cleared"] = num(retro) if at_end > CENT else 0
            if at_end > CENT:
                warnings.append(f"Deferred interest: if the balance is not cleared by the promo's end, about {num(retro)} "
                                "of back interest is charged from day one.")
                offer_cost += retro
                saved = current_cost - offer_cost if saved is not None else None
    if saved is None or saved <= 0:
        breakeven = None  # fees never paid back when the offer saves nothing overall
    # The same offer paid at today's payment: a lower rate without stretching the term.
    same = None
    if basis != "your current payment":
        same_plan = _schedule(start, rate_for, iva, lambda _m, _b, _c: current_payment, today)
        same_cost = same_plan["interest"] + same_plan["iva"] + fee
        if promo_months is not None and offer.get("deferred_interest") is True:
            same_end = same_plan["rows"][promo_months - 1]["balance"] if len(same_plan["rows"]) >= promo_months else ZERO
            if same_end > CENT:
                same_cost += start * rate / 12 * promo_months * (ONE + iva)
        same_saved = current_cost - same_cost if current_cost is not None and same_plan["status"] == "ready" else None
        same = {"status": same_plan["status"], "monthly_payment": num(current_payment), "months": same_plan["months"],
                "payoff_date": same_plan["date"], "interest": num(same_plan["interest"] + same_plan["iva"]),
                "total_cost": num(same_cost), "interest_saved": num(same_saved)}
    same_saved = D(same["interest_saved"]) if same else None
    longer_lower = (current["status"] == "ready" and new["status"] == "ready" and new["months"] > current["months"]
                    and payment < current_payment)
    if saved is not None and saved > 0:
        verdict = "take_offer"
    elif same_saved is not None and same_saved > 0:
        verdict = "take_offer_keep_payment"
    elif saved is None and same_saved is None:
        verdict = None
    else:
        verdict = "keep"
    verdict_text = _refinance_text(verdict, saved, same_saved, longer_lower, payment, current_payment)
    if current["status"] == "ready" and new["status"] == "ready" and new["months"] > current["months"]:
        warnings.append(f"The offer takes {new['months'] - current['months']} months longer to repay than now."
                        + (" A longer term lowers the payment but costs more in total." if longer_lower else ""))
    if rate > max(d["rate"] for d in debts) and promo_months is None:
        warnings.append("The offer's rate is higher than every debt it replaces.")
    result = {
        "mode": "refinance", "kind": kind, "as_of": today.isoformat(), "currency": currency,
        "replaces": [d["id"] for d in debts], "balance": num(balance), "fees": num(fee), "fees_financed": bool(financed),
        "current": {"status": current["status"], "monthly_payment": num(current_payment), "months": current.get("months"),
                    "payoff_date": current.get("date"), "interest": current.get("interest")},
        "offer": {"status": new["status"], "annual_rate": num(rate, 6), "promo_rate": num(promo_rate, 6),
                  "monthly_payment": num(payment), "payment_basis": basis, "starting_balance": num(start),
                  "months": new["months"], "payoff_date": new["date"], "interest": num(new["interest"] + new["iva"]),
                  "total_cost": num(offer_cost)},
        "same_payment": same,
        "interest_saved": num(saved), "breakeven_month": breakeven,
        "breakeven_date": add_months(today, breakeven).isoformat()[:7] if breakeven else None,
        "verdict": verdict, "verdict_text": verdict_text,
        "worth_it": verdict in ("take_offer", "take_offer_keep_payment") if verdict is not None else None, "risk": risk,
        "balance_series": {"current": current.get("balance_series") or [], "offer": _series(start, new["rows"], today)},
    }
    if breakeven is None and saved is not None and saved <= 0 and verdict == "keep":
        warnings.append("The offer never recovers its fees: it costs more than keeping the current debt.")
    return {"result": result, "warnings": warnings, "assumptions": assumptions + [
        "interest_saved = current interest - (offer interest + fees); both paths make their payments on time.",
        "same_payment keeps paying today's monthly amount on the offer's rate, to separate the lower rate from a "
        "longer term.",
        "The breakeven month is when the interest avoided has paid back the fees."],
        "sources": [SOURCES["liva"]] if iva else []}


def _refinance_text(verdict: str | None, saved: Decimal | None, same_saved: Decimal | None, longer_lower: bool,
                    payment: Decimal, current_payment: Decimal) -> dict | None:
    if verdict is None:
        return None
    longer = (" A longer term lowers the payment but costs more.", " Un plazo más largo baja el pago pero cuesta más.")
    if verdict == "take_offer":
        en = f"The offer saves {num(saved)} in interest and fees at its own payment ({num(payment)} a month)."
        es = f"La oferta ahorra {num(saved)} en intereses y comisiones con su propio pago ({num(payment)} al mes)."
        if same_saved is not None and same_saved > saved:
            en += f" Keeping your current {num(current_payment)} a month on it saves more: {num(same_saved)}."
            es += f" Si sigues pagando tus {num(current_payment)} al mes, ahorras más: {num(same_saved)}."
    elif verdict == "take_offer_keep_payment":
        cost_en = f"costs {num(-saved)} more" if saved is not None else "is never repaid"
        cost_es = f"cuesta {num(-saved)} de más" if saved is not None else "nunca se liquida"
        en = (f"Worth it only if you keep paying {num(current_payment)} a month: that saves {num(same_saved)}; at the "
              f"offer's own payment ({num(payment)}) it {cost_en}.")
        es = (f"Conviene sólo si sigues pagando {num(current_payment)} al mes: así ahorras {num(same_saved)}; con el pago "
              f"de la oferta ({num(payment)}) {cost_es}.")
    else:
        en = "Keep the current debt: the offer costs more in interest and fees, even at your current payment."
        es = "Conserva tu deuda actual: la oferta cuesta más en intereses y comisiones, aun con tu pago actual."
    if longer_lower:
        en, es = en + longer[0], es + longer[1]
    return {"en": en, "es": es}


def _current_costs(debts: list[dict], budget: Decimal, today: date) -> list[Decimal]:
    """The current path's interest month by month (same order and budget as ``_simulate``)."""
    bal = {d["id"]: d["balance"] for d in debts}
    by_id = {d["id"]: d for d in debts}
    pay_fn = {d["id"]: _payer(d) for d in debts}
    order = [d["id"] for d in sorted(debts, key=lambda d: -_monthly(d))]
    costs = []
    for month in range(1, MAX_MONTHS + 1):
        if all(b <= CENT for b in bal.values()):
            break
        cost, charges = ZERO, {}
        for key, b in bal.items():
            if b > CENT:
                charges[key] = b * _monthly(by_id[key])
                cost += charges[key]
                bal[key] = b + charges[key]
        remaining = budget
        for key in bal:
            if bal[key] > CENT:
                pay = min(pay_fn[key](month, bal[key] - charges[key], charges[key]), bal[key], max(remaining, ZERO))
                bal[key] -= pay
                remaining -= pay
        for key in order:
            if remaining <= 0:
                break
            pay = min(remaining, bal[key]) if bal[key] > CENT else ZERO
            bal[key] -= pay
            remaining -= pay
        costs.append(cost)
    return costs


def _unique(sources: list[dict]) -> list[dict]:
    seen, out = set(), []
    for source in sources:
        if source["title"] not in seen:
            seen.add(source["title"])
            out.append(source)
    return out


# --------------------------------------------------------------------------- for proactive


def payoff_in(row: Mapping[str, Any], today: date, months: int, *, currency: str | None = None) -> dict | None:
    """One debt row as the engine sees it: the payment that clears it in ``months`` (IVA included), both paths.

    Returns ``None`` when the row lacks a balance or rate.  ``current`` is ``None`` when the payment is unknown.
    """
    keys = ("id", "name", "kind", "type", "lender", "balance", "annual_rate", "iva_on_interest", "cat")
    src = {k: row[k] for k in keys if row.get(k) is not None}
    src["currency"] = row.get("currency") or currency
    src["remaining_term_months"] = months
    try:
        debt, _missing, _notes = normalize(src, 0, today)
    except ValueError:
        return None
    if debt is None or debt["indexed"] or not debt["balance"] or debt["balance"] <= 0:
        return None
    monthly = _monthly(debt)
    # Rounded up to the cent, so the payment shown really clears it within ``months``.
    needed = (debt["payment"] * 100).to_integral_value(rounding="ROUND_CEILING") / 100
    debt = {**debt, "payment": needed}
    fast = _plan(debt, today)
    payment = D(row.get("monthly_payment"))
    current = _plan(debt, today, payment=payment) if payment is not None and payment > 0 else None
    summary = lambda p: {"status": p["status"], "months": p["months"], "date": p["date"],  # noqa: E731
                         "interest": p["interest"] + p["iva"]}
    return {"monthly_factor": monthly, "effective_annual": effective_annual(monthly), "iva": debt["iva"],
            "payment_to_clear": debt["payment"], "fast": summary(fast),
            "current": summary(current) if current is not None else None}


# --------------------------------------------------------------------------- the task


def run(inputs: Mapping[str, Any], rows: list[Mapping[str, Any]], today: date, *, jurisdiction: str | None = None,
        reserve: Mapping[str, Any] | None = None, risk_free: Mapping[str, Any] | None = None) -> dict:
    """The ``debt`` task over ``rows`` (inline or stored liabilities): the standard envelope."""
    mode = inputs.get("mode")
    if mode not in MODES:
        raise ValueError(f"debt needs mode: one of {', '.join(MODES)}")
    if len(rows) > MAX_DEBTS:
        raise ValueError(f"at most {MAX_DEBTS} debts per run")
    ready, missing, assumptions = [], [], []
    for index, row in enumerate(rows):
        debt, gaps, notes = normalize(row, index, today)
        missing += gaps
        assumptions += notes
        if debt is not None:
            ready.append(debt)
    empty = {"status": "needs_input", "result": {"mode": mode}, "warnings": [], "sources": [], "assumptions": assumptions}
    if not ready:
        return {**empty, "missing": missing or [{"key": "liabilities", "reason": "missing",
                                                 "detail": "No debt with balance, rate and payment is known."}]}
    if mode == "amortize":
        rows_wanted = inputs.get("monthly_rows", MONTHLY_ROWS_DEFAULT)
        if rows_wanted == "all":
            rows_wanted = None
        elif not isinstance(rows_wanted, int) or isinstance(rows_wanted, bool) or rows_wanted < 0:
            raise ValueError("monthly_rows must be a whole number or \"all\"")
        report = amortize(ready, today, rows_wanted)
    elif mode == "strategies":
        if "monthly_amount" not in inputs:
            raise ValueError("strategies needs monthly_amount: the whole monthly budget for these debts, minimums included")
        report = strategies(ready, inputs["monthly_amount"], today, order=inputs.get("order"),
                            quick_win_months=inputs.get("quick_win_months", 3))
        if report["status"] == "needs_input":
            return {**empty, "result": report["result"], "missing": missing + report["missing"]}
    elif mode == "refinance":
        report = refinance(ready, inputs.get("offer"), today)
    else:
        if len(ready) != 1:
            if missing:
                return {**empty, "missing": missing}
            raise ValueError("prepay_vs_invest compares one debt: pass debt (an id or an object)")
        report = prepay_vs_invest(ready[0], inputs, today, jurisdiction=jurisdiction, reserve=reserve,
                                  risk_free=risk_free)
    missing = missing + report.get("missing", [])
    status = "partial" if missing else "ready"
    return {"status": status, "result": report["result"], "missing": missing,
            "warnings": report.get("warnings", []), "sources": report.get("sources", []),
            "assumptions": assumptions + report.get("assumptions", [])}


__all__ = ["MODES", "run", "normalize", "amortize", "strategies", "prepay_vs_invest", "refinance", "effective_annual",
           "payment_for_term", "payoff_in"]
