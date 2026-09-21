"""Scoped tax review calculations for taxable securities.

This module estimates the incremental effect of a proposed scenario.  It does
not prepare a return, recommend a trade, or claim that an estimate is tax
savings.  Inputs are deliberately explicit: the canonical household supplies
accounts and lots, while the request supplies current prices, sales, tax facts,
and coverage assertions.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
import math
from typing import Any


IRS_PUB_550 = "https://www.irs.gov/publications/p550"
MEXICO_LISR = "https://www.diputados.gob.mx/LeyesBiblio/pdf/LISR.pdf"
MEXICO_LISR_HISTORY = "https://www.diputados.gob.mx/LeyesBiblio/ref/lisr.htm"

_US_SOURCE = {
    "title": "IRS Publication 550 (2025), Investment Income and Expenses",
    "url": IRS_PUB_550,
    "version": "2025 publication; accessed 2026-09-20",
    "rules": [
        "more-than-one-year holding period",
        "short-term and long-term capital gain/loss netting and carryovers",
        "capital-loss deduction limit",
        "wash sales and substantially identical securities",
    ],
}
_MX_SOURCE = {
    "title": "Ley del Impuesto sobre la Renta, articulo 129",
    "url": MEXICO_LISR,
    "version": "texto vigente, ultima reforma DOF 01-04-2024; checked 2026-09-20",
    "rules": [
        "10% final tax for qualifying individual taxpayers and covered exchange transactions",
        "issuer-level gain/loss calculation using updated average acquisition cost",
        "losses offset only Article 129 gains in the current and following ten years",
    ],
}


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
        qualifier = "nonnegative finite" if nonnegative else "finite"
        raise ValueError(f"{path} must be a {qualifier} number")
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


def _money(value: Decimal) -> str:
    value = value.quantize(Decimal("0.01"))
    return format(value, "f")


def _quantity(value: Decimal) -> str:
    return format(value.normalize(), "f")


def _envelope(
    status: str,
    result: dict[str, Any],
    *,
    missing: list[str] | None = None,
    warnings: list[str] | None = None,
    sources: list[dict[str, Any]] | None = None,
    assumptions: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "status": status,
        "result": result,
        "missing": missing or [],
        "warnings": warnings or [],
        "sources": sources or [],
        "assumptions": assumptions or [],
    }


def _household(inputs: dict[str, Any], context: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
    if "household" in inputs:
        value = inputs["household"]
        assumption = ["Used the household supplied in this request instead of stored context."]
    else:
        value = context.get("household")
        assumption = []
    if value is None:
        return None, assumption
    if not isinstance(value, dict):
        raise ValueError("household must be an object")
    return value, assumption


def _index_household(household: dict[str, Any]) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    accounts: dict[str, dict[str, Any]] = {}
    positions: dict[str, dict[str, Any]] = {}
    lots: dict[str, dict[str, Any]] = {}
    for key, target in (("accounts", accounts), ("positions", positions), ("lots", lots)):
        values = household.get(key, [])
        if not isinstance(values, list):
            raise ValueError(f"household.{key} must be a list")
        for index, item in enumerate(values):
            if not isinstance(item, dict):
                raise ValueError(f"household.{key}[{index}] must be an object")
            item_id = _text(item.get("id"), f"household.{key}[{index}].id")
            if item_id in target:
                raise ValueError(f"duplicate household.{key} id: {item_id}")
            target[item_id] = item
    return accounts, positions, lots


def _prices(inputs: dict[str, Any]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    values = inputs.get("prices", [])
    if not isinstance(values, list):
        raise ValueError("prices must be a list")
    for index, item in enumerate(values):
        if not isinstance(item, dict):
            raise ValueError(f"prices[{index}] must be an object")
        instrument = _text(item.get("instrument_id"), f"prices[{index}].instrument_id")
        if instrument in result:
            raise ValueError(f"duplicate price for instrument_id {instrument}")
        result[instrument] = {
            "price": _decimal(item.get("price"), f"prices[{index}].price"),
            "currency": _text(item.get("currency"), f"prices[{index}].currency").upper(),
            "as_of": _date(item.get("as_of"), f"prices[{index}].as_of"),
            "source": _text(item.get("source"), f"prices[{index}].source"),
        }
    return result


def _selected_lots(inputs: dict[str, Any], lots: dict[str, dict[str, Any]]) -> dict[str, Decimal] | None:
    if "sales" not in inputs:
        return None
    sales = inputs["sales"]
    if not isinstance(sales, list) or not sales:
        raise ValueError("sales must be a nonempty list when supplied")
    selected: dict[str, Decimal] = {}
    for index, sale in enumerate(sales):
        if not isinstance(sale, dict):
            raise ValueError(f"sales[{index}] must be an object")
        lot_id = _text(sale.get("lot_id"), f"sales[{index}].lot_id")
        if lot_id not in lots:
            raise ValueError(f"sales[{index}].lot_id does not identify a household lot")
        if lot_id in selected:
            raise ValueError(f"duplicate proposed sale for lot {lot_id}")
        selected[lot_id] = _decimal(sale.get("quantity"), f"sales[{index}].quantity")
        if selected[lot_id] == 0:
            raise ValueError(f"sales[{index}].quantity must be greater than zero")
    return selected


def _identity_groups(inputs: dict[str, Any]) -> dict[str, set[str]]:
    raw = inputs.get("substantially_identical_groups", [])
    if not isinstance(raw, list):
        raise ValueError("substantially_identical_groups must be a list")
    groups: dict[str, set[str]] = {}
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ValueError(f"substantially_identical_groups[{index}] must be an object")
        group_id = _text(item.get("id"), f"substantially_identical_groups[{index}].id")
        instruments = item.get("instrument_ids")
        if not isinstance(instruments, list) or len(instruments) < 2:
            raise ValueError(f"substantially_identical_groups[{index}].instrument_ids must contain at least two ids")
        members = {_text(value, f"substantially_identical_groups[{index}].instrument_ids") for value in instruments}
        for member in members:
            groups.setdefault(member, set()).update(members)
            groups[member].discard(member)
        del group_id  # identifier is evidence metadata; membership drives the screen
    return groups


def _wash_purchases(inputs: dict[str, Any], accounts: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    raw = inputs.get("purchases", [])
    if not isinstance(raw, list):
        raise ValueError("purchases must be a list")
    purchases: list[dict[str, Any]] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ValueError(f"purchases[{index}] must be an object")
        account_id = _text(item.get("account_id"), f"purchases[{index}].account_id")
        related_party = item.get("related_party", "household")
        related_party = _text(related_party, f"purchases[{index}].related_party")
        if account_id not in accounts and related_party not in {"spouse", "controlled_corporation", "taxpayer"}:
            raise ValueError(f"purchases[{index}] outside the household requires an explicit related_party")
        quantity = _decimal(item.get("quantity"), f"purchases[{index}].quantity")
        if quantity == 0:
            raise ValueError(f"purchases[{index}].quantity must be greater than zero")
        account_type = _text(accounts[account_id].get("type"), f"household.accounts[{account_id}].type") if account_id in accounts else "external"
        purchases.append(
            {
                "account_id": account_id,
                "account_type": account_type,
                "instrument_id": _text(item.get("instrument_id"), f"purchases[{index}].instrument_id"),
                "trade_date": _date(item.get("trade_date"), f"purchases[{index}].trade_date"),
                "quantity": quantity,
                "related_party": related_party,
            }
        )
    return purchases


def _holding_character(acquired: date, sold: date) -> str:
    if acquired >= sold:
        raise ValueError("lot acquired_on must precede sale_date")
    try:
        anniversary = acquired.replace(year=acquired.year + 1)
    except ValueError:  # February 29 in a non-leap anniversary year.
        anniversary = acquired.replace(year=acquired.year + 1, day=28)
    return "long_term" if sold > anniversary else "short_term"


def _us_tax_facts(inputs: dict[str, Any], sale_date: date) -> tuple[dict[str, Decimal] | None, list[str]]:
    raw = inputs.get("us_tax_facts")
    numeric_keys = (
        "short_term_gains",
        "short_term_losses",
        "long_term_gains",
        "long_term_losses",
        "short_term_loss_carryover",
        "long_term_loss_carryover",
        "ordinary_income_loss_deduction_available",
    )
    if raw is None:
        return None, [
            *(f"us_tax_facts.{key}" for key in numeric_keys),
            "us_tax_facts.complete=true",
            "us_tax_facts.as_of",
            "filing_status",
        ]
    if not isinstance(raw, dict):
        raise ValueError("us_tax_facts must be an object")
    missing = [f"us_tax_facts.{key}" for key in numeric_keys if key not in raw]
    if raw.get("complete") is not True:
        missing.append("us_tax_facts.complete=true")
    if "as_of" not in raw:
        missing.append("us_tax_facts.as_of")
    elif _date(raw["as_of"], "us_tax_facts.as_of") != sale_date:
        missing.append(f"us_tax_facts.as_of={sale_date.isoformat()}")
    if "filing_status" not in inputs:
        missing.append("filing_status")
    if missing:
        return None, missing
    result = {key: _decimal(raw[key], f"us_tax_facts.{key}") for key in numeric_keys}
    filing = _text(inputs["filing_status"], "filing_status").lower()
    statutory_limit = Decimal("1500") if filing == "married_filing_separately" else Decimal("3000")
    if filing not in {"single", "married_filing_jointly", "married_filing_separately", "head_of_household", "qualifying_surviving_spouse"}:
        raise ValueError("filing_status is not a supported U.S. filing status")
    if result["ordinary_income_loss_deduction_available"] > statutory_limit:
        raise ValueError("us_tax_facts.ordinary_income_loss_deduction_available exceeds the statutory filing-status limit")
    return result, []


def _net_us(st: Decimal, lt: Decimal, deduction_limit: Decimal) -> dict[str, Decimal]:
    st_after, lt_after = st, lt
    if st_after < 0 < lt_after:
        combined = st_after + lt_after
        st_after, lt_after = (Decimal(0), combined) if combined >= 0 else (combined, Decimal(0))
    elif lt_after < 0 < st_after:
        combined = st_after + lt_after
        st_after, lt_after = (combined, Decimal(0)) if combined >= 0 else (Decimal(0), combined)
    total_loss = max(-(st_after + lt_after), Decimal(0))
    deduction = min(total_loss, deduction_limit)
    remaining = deduction
    st_loss = max(-st_after, Decimal(0))
    used_st = min(st_loss, remaining)
    remaining -= used_st
    used_lt = min(max(-lt_after, Decimal(0)), remaining)
    return {
        "net_short_term": st_after,
        "net_long_term": lt_after,
        "ordinary_income_loss_deduction": deduction,
        "short_term_carryforward": st_loss - used_st,
        "long_term_carryforward": max(-lt_after, Decimal(0)) - used_lt,
    }


def _us_rates(inputs: dict[str, Any]) -> tuple[dict[str, Decimal] | None, list[str], list[str]]:
    raw = inputs.get("rates")
    if raw is None:
        return None, ["rates.ordinary", "rates.long_term"], []
    if not isinstance(raw, dict):
        raise ValueError("rates must be an object")
    missing = [f"rates.{key}" for key in ("ordinary", "long_term") if key not in raw]
    if missing:
        return None, missing, []
    rates = {key: _decimal(raw[key], f"rates.{key}") for key in ("ordinary", "long_term")}
    assumptions = []
    for key in ("state", "niit"):
        if key in raw:
            rates[key] = _decimal(raw[key], f"rates.{key}")
            assumptions.append(f"Applied the supplied marginal {key.upper()} rate additively to this scenario.")
        else:
            rates[key] = Decimal(0)
    if any(value > 1 for value in rates.values()):
        raise ValueError("rates must be decimals between 0 and 1")
    return rates, [], assumptions


def _marginal_us_tax(net: dict[str, Decimal], rates: dict[str, Decimal]) -> Decimal:
    state_niit = rates["state"] + rates["niit"]
    return (
        max(net["net_short_term"], Decimal(0)) * (rates["ordinary"] + state_niit)
        + max(net["net_long_term"], Decimal(0)) * (rates["long_term"] + state_niit)
        - net["ordinary_income_loss_deduction"] * (rates["ordinary"] + state_niit)
    )


def _serialise_net(net: dict[str, Decimal]) -> dict[str, str]:
    return {key: _money(value) for key, value in net.items()}


def _run_us(inputs: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    household, assumptions = _household(inputs, context)
    if household is None:
        return _envelope("needs_input", {}, missing=["household"], sources=[_US_SOURCE])
    accounts, _positions, lots = _index_household(household)
    if not lots:
        return _envelope("needs_input", {}, missing=["household.lots"], sources=[_US_SOURCE], assumptions=assumptions)
    prices = _prices(inputs)
    sale_value = inputs.get("sale_date", household.get("as_of"))
    as_of_value = inputs.get("as_of", household.get("as_of"))
    date_missing = [key for key, value in (("sale_date", sale_value), ("as_of", as_of_value)) if value is None]
    if date_missing:
        return _envelope("needs_input", {}, missing=date_missing, sources=[_US_SOURCE], assumptions=assumptions)
    sale_date = _date(sale_value, "sale_date")
    as_of = _date(as_of_value, "as_of")
    if sale_date > as_of:
        raise ValueError("sale_date cannot be after as_of")
    mode = _text(inputs.get("mode", "harvest"), "mode").lower()
    if mode not in {"harvest", "rebalance"}:
        raise ValueError("mode must be harvest or rebalance")
    selected = _selected_lots(inputs, lots)
    if mode == "rebalance" and selected is None:
        return _envelope("needs_input", {}, missing=["sales"], sources=[_US_SOURCE], assumptions=assumptions)

    groups = _identity_groups(inputs)
    purchases = _wash_purchases(inputs, accounts)
    candidates: list[dict[str, Any]] = []
    scenario_st = Decimal(0)
    scenario_lt = Decimal(0)
    missing: list[str] = []
    warnings: list[str] = []
    for lot_id, lot in lots.items():
        if selected is not None and lot_id not in selected:
            continue
        account_id = _text(lot.get("account_id"), f"household.lots[{lot_id}].account_id")
        if account_id not in accounts:
            raise ValueError(f"lot {lot_id} references unknown account_id")
        account_type = _text(accounts[account_id].get("type"), f"household.accounts[{account_id}].type").lower()
        if account_type not in {"taxable", "taxable_brokerage", "brokerage"}:
            if selected is not None:
                raise ValueError(f"lot {lot_id} is not in a supported taxable account")
            continue
        instrument = _text(lot.get("instrument_id"), f"household.lots[{lot_id}].instrument_id")
        lot_currency = _text(lot.get("currency"), f"household.lots[{lot_id}].currency").upper()
        if lot_currency != "USD":
            if selected is not None:
                raise ValueError(f"U.S. scenario lot {lot_id} must have USD basis; no FX is inferred")
            continue
        lot_quantity = _decimal(lot.get("quantity"), f"household.lots[{lot_id}].quantity")
        if lot_quantity == 0:
            raise ValueError(f"household.lots[{lot_id}].quantity must be greater than zero")
        quantity = selected[lot_id] if selected is not None else lot_quantity
        if quantity > lot_quantity:
            raise ValueError(f"sale quantity exceeds lot {lot_id} quantity")
        basis = _decimal(lot.get("cost_basis"), f"household.lots[{lot_id}].cost_basis") * quantity / lot_quantity
        price = prices.get(instrument)
        if price is None:
            missing.append(f"prices[{instrument}]")
            continue
        if price["currency"] != lot_currency:
            raise ValueError(f"price currency for {instrument} does not match lot {lot_id}; no FX is inferred")
        if price["as_of"] != sale_date:
            raise ValueError(f"price for {instrument} must be a verified sale-date quote dated {sale_date.isoformat()}")
        proceeds = price["price"] * quantity
        gain_loss = proceeds - basis
        if selected is None and gain_loss >= 0:
            continue
        acquired = _date(lot.get("acquired_on"), f"household.lots[{lot_id}].acquired_on")
        character = _holding_character(acquired, sale_date)
        conflicts = []
        if gain_loss < 0:
            identical = groups.get(instrument, set()) | {instrument}
            for purchase in purchases:
                if purchase["instrument_id"] in identical and sale_date - timedelta(days=30) <= purchase["trade_date"] <= sale_date + timedelta(days=30):
                    conflicts.append(
                        {
                            "account_id": purchase["account_id"],
                            "account_type": purchase["account_type"],
                            "related_party": purchase["related_party"],
                            "instrument_id": purchase["instrument_id"],
                            "trade_date": purchase["trade_date"].isoformat(),
                            "quantity": _quantity(purchase["quantity"]),
                        }
                    )
        excluded = bool(conflicts)
        if not excluded:
            if character == "short_term":
                scenario_st += gain_loss
            else:
                scenario_lt += gain_loss
        candidates.append(
            {
                "lot_id": lot_id,
                "account_id": account_id,
                "instrument_id": instrument,
                "quantity": _quantity(quantity),
                "basis": _money(basis),
                "proceeds": _money(proceeds),
                "gain_or_loss": _money(gain_loss),
                "character": character,
                "acquired_on": acquired.isoformat(),
                "sale_date": sale_date.isoformat(),
                "price": _money(price["price"]),
                "price_as_of": price["as_of"].isoformat(),
                "price_source": price["source"],
                "wash_conflicts": conflicts,
                "scenario_treatment": "excluded_entire_lot_conservatively" if excluded else "included",
            }
        )

    if missing:
        return _envelope(
            "needs_input",
            {"jurisdiction": "US", "candidates_with_verified_prices": candidates, "execution_ready": False},
            missing=sorted(set(missing)),
            warnings=["No basis, price, or exchange rate was invented; the scenario is incomplete."],
            sources=[_US_SOURCE],
            assumptions=assumptions,
        )

    tax_facts, tax_fact_missing = _us_tax_facts(inputs, sale_date)
    missing.extend(tax_fact_missing)
    before = None
    after = None
    if tax_facts is not None:
        st_before = tax_facts["short_term_gains"] - tax_facts["short_term_losses"] - tax_facts["short_term_loss_carryover"]
        lt_before = tax_facts["long_term_gains"] - tax_facts["long_term_losses"] - tax_facts["long_term_loss_carryover"]
        deduction_available = tax_facts["ordinary_income_loss_deduction_available"]
        before = _net_us(st_before, lt_before, deduction_available)
        after = _net_us(st_before + scenario_st, lt_before + scenario_lt, deduction_available)
    rates, rate_missing, rate_assumptions = _us_rates(inputs)
    assumptions.extend(rate_assumptions)

    coverage = inputs.get("wash_sale_coverage", {})
    if not isinstance(coverage, dict):
        raise ValueError("wash_sale_coverage must be an object")
    future_end = sale_date + timedelta(days=30)
    provisional_reasons: list[str] = []
    if future_end > as_of:
        provisional_reasons.append(f"future replacement-purchase window remains open through {future_end.isoformat()}")
    required_flags = ("accounts_complete", "identity_mapping_complete", "automatic_reinvestment_reviewed", "spouse_accounts_reviewed", "controlled_accounts_reviewed")
    for flag in required_flags:
        if coverage.get(flag) is not True:
            provisional_reasons.append(f"wash_sale_coverage.{flag} is not confirmed")
    if "purchases_from" not in coverage or _date(coverage["purchases_from"], "wash_sale_coverage.purchases_from") > sale_date - timedelta(days=30):
        provisional_reasons.append("purchase history does not cover the full 30-day pre-sale window")
    required_through = min(as_of, future_end)
    if "purchases_through" not in coverage or _date(coverage["purchases_through"], "wash_sale_coverage.purchases_through") < required_through:
        provisional_reasons.append("purchase history is not current through the required review date")
    if household.get("complete") is not True:
        provisional_reasons.append("household account coverage is not marked complete")
    if any(candidate["wash_conflicts"] for candidate in candidates):
        warnings.append("A replacement conflict conservatively excludes the entire proposed lot even when fewer replacement shares were found; actual share matching can allow a narrower adjustment.")
    warnings.append("Capital-gain distributions and other distributions are not included as realized security-sale gains by this calculator.")

    estimate = None
    if rates is not None and before is not None and after is not None:
        before_tax = _marginal_us_tax(before, rates)
        after_tax = _marginal_us_tax(after, rates)
        estimate = {
            "currency": "USD",
            "before_scenario": _money(before_tax),
            "after_scenario": _money(after_tax),
            "incremental_tax": _money(after_tax - before_tax),
            "method": "marginal-rate scenario estimate; negative incremental tax is an estimated reduction, not guaranteed savings",
            "rates": {key: format(value, "f") for key, value in rates.items()},
        }
        warnings.append("The marginal-rate estimate does not model taxable-income limits, brackets, AMT, NIIT thresholds, or other return-level interactions.")
    result = {
        "jurisdiction": "US federal taxable securities",
        "mode": mode,
        "currency": "USD",
        "sale_date": sale_date.isoformat(),
        "review_status": "provisional" if provisional_reasons else "screened",
        "provisional_reasons": provisional_reasons,
        "execution_ready": False,
        "candidates": candidates,
        "included_scenario_gain_or_loss": {"short_term": _money(scenario_st), "long_term": _money(scenario_lt)},
        "netting": None if before is None or after is None else {"before_scenario": _serialise_net(before), "after_scenario": _serialise_net(after)},
        "incremental_tax_estimate": estimate,
        "scope": "Federal capital-gain scenario only; excludes return-level interactions and any unsupplied state or NIIT effect.",
    }
    missing.extend(rate_missing)
    status = "partial" if missing or provisional_reasons else "ready"
    return _envelope(status, result, missing=missing, warnings=warnings, sources=[_US_SOURCE], assumptions=assumptions)


def _run_mx(inputs: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    household, assumptions = _household(inputs, context)
    if household is None:
        return _envelope("needs_input", {}, missing=["household"], sources=[_MX_SOURCE])
    accounts, _positions, lots = _index_household(household)
    sales = inputs.get("article_129_sales")
    if not isinstance(sales, list) or not sales:
        return _envelope("needs_input", {}, missing=["article_129_sales"], sources=[_MX_SOURCE], assumptions=assumptions)
    global_eligibility = inputs.get("article_129_eligibility")
    if global_eligibility is not None and not isinstance(global_eligibility, dict):
        raise ValueError("article_129_eligibility must be an object")
    eligibility_flags = ("individual_taxpayer", "eligible_security", "eligible_venue", "eligible_acquisition", "no_exclusion_applies")
    eligibility_text = ("source", "security_scope", "venue", "acquisition_scope")
    eligibility_missing: list[str] = []
    for index, sale in enumerate(sales):
        if not isinstance(sale, dict):
            raise ValueError(f"article_129_sales[{index}] must be an object")
        row_eligibility = sale.get("article_129_eligibility")
        prefix = f"article_129_sales[{index}].article_129_eligibility"
        if row_eligibility is None and len(sales) == 1 and global_eligibility is not None:
            row_eligibility = global_eligibility
            prefix = "article_129_eligibility"
        if not isinstance(row_eligibility, dict):
            eligibility_missing.extend(f"{prefix}.{key}" for key in (*eligibility_flags, *eligibility_text))
            continue
        eligibility_missing.extend(f"{prefix}.{key}=true" for key in eligibility_flags if row_eligibility.get(key) is not True)
        for key in eligibility_text:
            value = row_eligibility.get(key)
            if not isinstance(value, str) or not value.strip():
                eligibility_missing.append(f"{prefix}.{key}")
        sale_lot_id = sale.get("lot_id")
        if isinstance(sale_lot_id, str) and sale_lot_id in lots:
            instrument_id = lots[sale_lot_id].get("instrument_id")
            if row_eligibility.get("security_scope") != instrument_id:
                eligibility_missing.append(f"{prefix}.security_scope={instrument_id}")
            if row_eligibility.get("acquisition_scope") != sale_lot_id:
                eligibility_missing.append(f"{prefix}.acquisition_scope={sale_lot_id}")
    if eligibility_missing:
        return _envelope(
            "needs_input", {"jurisdiction": "Mexico Article 129", "execution_ready": False}, missing=eligibility_missing,
            warnings=["Article 129 treatment is not assumed unless eligibility evidence identifies each proposed security, venue, and acquisition scope."],
            sources=[_MX_SOURCE], assumptions=assumptions,
        )
    rows = []
    scenario = Decimal(0)
    seen_lots: set[str] = set()
    for index, sale in enumerate(sales):
        lot_id = _text(sale.get("lot_id"), f"article_129_sales[{index}].lot_id")
        if lot_id not in lots:
            raise ValueError(f"article_129_sales[{index}].lot_id does not identify a household lot")
        if lot_id in seen_lots:
            raise ValueError(f"duplicate Article 129 sale row for lot {lot_id}")
        seen_lots.add(lot_id)
        lot = lots[lot_id]
        account_id = _text(lot.get("account_id"), f"household.lots[{lot_id}].account_id")
        if account_id not in accounts:
            raise ValueError(f"lot {lot_id} references unknown account_id")
        account_type = _text(accounts[account_id].get("type"), f"household.accounts[{account_id}].type").lower()
        if account_type not in {"taxable", "taxable_brokerage", "brokerage"}:
            raise ValueError(f"Article 129 lot {lot_id} is not in a supported taxable account")
        if _text(lot.get("currency"), f"household.lots[{lot_id}].currency").upper() != "MXN":
            raise ValueError(f"Article 129 lot {lot_id} must use MXN; no FX is inferred")
        quantity = _decimal(sale.get("quantity"), f"article_129_sales[{index}].quantity")
        lot_quantity = _decimal(lot.get("quantity"), f"household.lots[{lot_id}].quantity")
        _decimal(lot.get("cost_basis"), f"household.lots[{lot_id}].cost_basis")
        _date(lot.get("acquired_on"), f"household.lots[{lot_id}].acquired_on")
        if quantity == 0 or quantity > lot_quantity:
            raise ValueError(f"article_129_sales[{index}].quantity must be greater than zero and no more than the lot quantity")
        proceeds = _decimal(sale.get("proceeds_mxn"), f"article_129_sales[{index}].proceeds_mxn")
        adjusted_basis = _decimal(sale.get("article_129_adjusted_basis_mxn"), f"article_129_sales[{index}].article_129_adjusted_basis_mxn")
        basis_source = _text(sale.get("basis_source"), f"article_129_sales[{index}].basis_source")
        proceeds_source = _text(sale.get("proceeds_source"), f"article_129_sales[{index}].proceeds_source")
        gain_loss = proceeds - adjusted_basis
        scenario += gain_loss
        rows.append(
            {
                "lot_id": lot_id,
                "account_id": account_id,
                "instrument_id": _text(lot.get("instrument_id"), f"household.lots[{lot_id}].instrument_id"),
                "quantity": _quantity(quantity),
                "proceeds_mxn": _money(proceeds),
                "article_129_adjusted_basis_mxn": _money(adjusted_basis),
                "gain_or_loss_mxn": _money(gain_loss),
                "basis_source": basis_source,
                "proceeds_source": proceeds_source,
                "article_129_eligibility": sale.get("article_129_eligibility", global_eligibility),
            }
        )
    as_of_value = inputs.get("as_of", household.get("as_of"))
    if as_of_value is None:
        return _envelope("needs_input", {"sales": rows, "execution_ready": False}, missing=["as_of"], sources=[_MX_SOURCE], assumptions=assumptions)
    as_of = _date(as_of_value, "as_of")
    current_year = as_of.year
    mx_tax_missing = [
        key for key in ("article_129_realized_gain_or_loss_mxn", "article_129_loss_carryforwards") if key not in inputs
    ]
    if mx_tax_missing:
        return _envelope(
            "partial",
            {
                "jurisdiction": "Mexico Article 129",
                "currency": "MXN",
                "sales": rows,
                "scenario_gain_or_loss_mxn": _money(scenario),
                "incremental_tax_estimate": None,
                "execution_ready": False,
            },
            missing=mx_tax_missing,
            warnings=["Current-year Article 129 results and carryforward coverage must be explicit before estimating tax; absence is not treated as zero."],
            sources=[_MX_SOURCE],
            assumptions=assumptions,
        )
    realized = _decimal(inputs["article_129_realized_gain_or_loss_mxn"], "article_129_realized_gain_or_loss_mxn", nonnegative=False)
    carry_values = inputs["article_129_loss_carryforwards"]
    if not isinstance(carry_values, list):
        raise ValueError("article_129_loss_carryforwards must be a list")
    available_carry = Decimal(0)
    carries = []
    for index, item in enumerate(carry_values):
        if not isinstance(item, dict):
            raise ValueError(f"article_129_loss_carryforwards[{index}] must be an object")
        year = item.get("origin_year")
        if isinstance(year, bool) or not isinstance(year, int):
            raise ValueError(f"article_129_loss_carryforwards[{index}].origin_year must be an integer")
        amount = _decimal(item.get("available_updated_mxn"), f"article_129_loss_carryforwards[{index}].available_updated_mxn")
        updated = _text(item.get("updated_through"), f"article_129_loss_carryforwards[{index}].updated_through")
        eligible = 1 <= current_year - year <= 10
        if eligible:
            available_carry += amount
        carries.append({"origin_year": year, "available_updated_mxn": _money(amount), "updated_through": updated, "eligible_this_year": eligible})

    def mx_net(value: Decimal) -> tuple[Decimal, Decimal, Decimal]:
        gain = max(value, Decimal(0))
        carry_used = min(gain, available_carry)
        taxable = gain - carry_used
        generated_loss = max(-value, Decimal(0))
        return taxable, carry_used, generated_loss

    before_taxable, before_used, before_loss = mx_net(realized)
    after_taxable, after_used, after_loss = mx_net(realized + scenario)
    rate = Decimal("0.10")
    before_tax = before_taxable * rate
    after_tax = after_taxable * rate
    warnings = [
        "This scenario uses supplied Article 129 adjusted basis; it does not recreate statutory average-cost or inflation-index calculations from canonical lot basis.",
        "Distributions are outside this securities-sale calculation and are not treated as gains.",
        "Unused losses require return and intermediary records; failing to use an available loss can forfeit it to the extent it could have been used.",
    ]
    result = {
        "jurisdiction": "Mexico Article 129 eligible listed shares",
        "currency": "MXN",
        "tax_year": current_year,
        "execution_ready": False,
        "sales": rows,
        "scenario_gain_or_loss_mxn": _money(scenario),
        "loss_carryforwards": carries,
        "netting": {
            "before_scenario": {"article_129_gain_or_loss_mxn": _money(realized), "carry_used_mxn": _money(before_used), "taxable_gain_mxn": _money(before_taxable), "new_loss_mxn": _money(before_loss)},
            "after_scenario": {"article_129_gain_or_loss_mxn": _money(realized + scenario), "carry_used_mxn": _money(after_used), "taxable_gain_mxn": _money(after_taxable), "new_loss_mxn": _money(after_loss)},
        },
        "incremental_tax_estimate": {
            "currency": "MXN",
            "before_scenario": _money(before_tax),
            "after_scenario": _money(after_tax),
            "incremental_tax": _money(after_tax - before_tax),
            "rate": "0.10",
            "method": "Article 129 scoped scenario; negative incremental tax is an estimated reduction, not guaranteed savings",
        },
        "scope": "Article 129 only; no U.S. holding-period, wash-sale, or capital-loss rules are imported.",
    }
    return _envelope("ready", result, warnings=warnings, sources=[_MX_SOURCE, {"title": "LISR amendment history", "url": MEXICO_LISR_HISTORY, "version": "checked 2026-09-20"}], assumptions=assumptions)


def run(task: str, inputs: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    """Run a scoped tax scenario for ``US`` or ``MX_ARTICLE_129``."""
    if not isinstance(task, str) or task.strip().lower() != "tax":
        raise ValueError("tax.run supports only task='tax'")
    if not isinstance(inputs, dict) or not isinstance(context, dict):
        raise ValueError("inputs and context must be objects")
    jurisdiction = inputs.get("jurisdiction")
    if jurisdiction is None:
        return _envelope("needs_input", {}, missing=["jurisdiction"], sources=[_US_SOURCE, _MX_SOURCE])
    normalized = _text(jurisdiction, "jurisdiction").upper().replace("-", "_")
    if normalized in {"US", "USA", "US_FEDERAL"}:
        return _run_us(inputs, context)
    if normalized in {"MX", "MEXICO", "MX_ARTICLE_129", "MEXICO_ARTICLE_129"}:
        return _run_mx(inputs, context)
    raise ValueError("jurisdiction must be US or MX_ARTICLE_129")
