"""Scoped tax review calculations for taxable securities.

This module estimates the incremental effect of a proposed scenario.  It does
not prepare a return, recommend a trade, or claim that an estimate is tax
savings.  Inputs are deliberately explicit: the canonical household supplies
accounts and lots, while the request supplies current prices, sales, tax facts,
and coverage assertions.

U.S. modes (``inputs.mode``):

* ``harvest``: every loss lot sold on ``sale_date`` (screen).
* ``rebalance``: the explicit ``sales`` list.
* ``lot_selection``: choose lots for a ``target`` sale amount under FIFO, LIFO,
  HIFO, specific identification and a tax-minimising search, and compare.
* ``harvest_report``: year-end tax-loss harvesting report.

Wash sales use share matching (IRS Publication 550; Treas. Reg. 1.1091-1):
only the loss on as many shares as were replaced is disallowed; it is added to
the replacement shares' basis with a tacked holding period, or permanently
disallowed when the replacement sits in an IRA/Roth (Rev. Rul. 2008-5).
Replacements include explicit ``purchases`` and household lots of
substantially identical instruments acquired within 30 days either side of the
sale.  A purchase is never its own replacement (Rev. Rul. 56-602).

Tax is computed with the dated federal bracket engine in
:mod:`wealth.us_tax_parameters` when ``us_return_facts`` is supplied; supplying
``rates`` overrides it with flat marginal rates.
"""

from __future__ import annotations

from dataclasses import dataclass, field as dataclass_field
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from itertools import combinations
import math
from typing import Any

from .household import account_tax_treatment, canonical_account_type, validate_household
from . import us_tax_parameters as us_params


IRS_PUB_550 = "https://www.irs.gov/publications/p550"
MEXICO_LISR = "https://www.diputados.gob.mx/LeyesBiblio/pdf/LISR.pdf"
MEXICO_LISR_HISTORY = "https://www.diputados.gob.mx/LeyesBiblio/ref/lisr.htm"
_PUB_550_REVISION = 2025

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
_US_RULES = [
    "more-than-one-year holding period",
    "short-term and long-term capital gain/loss netting and carryovers",
    "capital-loss deduction limit",
    "wash sales, share matching, basis adjustment and holding-period tacking",
]
_EXHAUSTIVE_LOT_LIMIT = 8
_LOT_METHODS = ("fifo", "lifo", "hifo", "specific_id", "tax_min")


def _us_source(tax_year: int | None) -> dict[str, Any]:
    version = f"Publication 550 ({_PUB_550_REVISION}), for {_PUB_550_REVISION} returns; checked 2026-09-21"
    if tax_year is not None and tax_year > _PUB_550_REVISION:
        version += f"; no {tax_year} revision was published when checked, so the {_PUB_550_REVISION} rules are cited for {tax_year} sales"
    return {"title": "IRS Publication 550, Investment Income and Expenses", "url": IRS_PUB_550, "version": version, "rules": list(_US_RULES)}


def _decimal(value: Any, path: str, *, nonnegative: bool = True) -> Decimal:
    if isinstance(value, bool) or value is None or value == "":
        raise ValueError(f"{path} must be a finite number")
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError(f"{path} must be a finite number")
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError(f"{path} must be a finite number") from None
    if not number.is_finite() or (nonnegative and number < 0):
        qualifier = "nonnegative finite" if nonnegative else "finite"
        raise ValueError(f"{path} must be a {qualifier} number")
    return number


def _date(value: Any, path: str) -> date:
    if not isinstance(value, str):
        raise ValueError(f"{path} must be an ISO date")
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        raise ValueError(f"{path} must be an ISO date") from None
    if parsed.isoformat() != value:
        raise ValueError(f"{path} must be an ISO date")
    return parsed


def _text(value: Any, path: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{path} must be a nonempty string")
    return value.strip()


def _money(value: Decimal) -> str:
    value = value.quantize(Decimal("0.01"))
    return format(value + 0, "f")


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
        "warnings": list(dict.fromkeys(warnings or [])),
        "sources": sources or [],
        "assumptions": list(dict.fromkeys(assumptions or [])),
    }


def _household(inputs: dict[str, Any], context: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str], list[str]]:
    if "household" in inputs:
        value = inputs["household"]
        assumption = ["Used the household supplied in this request instead of stored context."]
    else:
        value = context.get("household")
        assumption = []
    if value is None:
        return None, assumption, []
    checked = validate_household(value)
    return checked["household"], assumption, checked["warnings"]


def _index_household(household: dict[str, Any]) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    accounts = {item["id"]: item for item in household["accounts"]}
    lots = {item["id"]: item for item in household["lots"]}
    return accounts, lots


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


def _selected_lots(inputs: dict[str, Any], lots: dict[str, dict[str, Any]], key: str = "sales") -> dict[str, Decimal] | None:
    if key not in inputs:
        return None
    sales = inputs[key]
    if not isinstance(sales, list) or not sales:
        raise ValueError(f"{key} must be a nonempty list when supplied")
    selected: dict[str, Decimal] = {}
    for index, sale in enumerate(sales):
        if not isinstance(sale, dict):
            raise ValueError(f"{key}[{index}] must be an object")
        lot_id = _text(sale.get("lot_id"), f"{key}[{index}].lot_id")
        if lot_id not in lots:
            raise ValueError(f"{key}[{index}].lot_id does not identify a household lot")
        if lot_id in selected:
            raise ValueError(f"duplicate proposed sale for lot {lot_id}")
        selected[lot_id] = _decimal(sale.get("quantity"), f"{key}[{index}].quantity")
        if selected[lot_id] == 0:
            raise ValueError(f"{key}[{index}].quantity must be greater than zero")
    return selected


def _identity_groups(inputs: dict[str, Any]) -> dict[str, set[str]]:
    """Map each instrument to the instruments treated as substantially identical.

    Group ids are validated because callers cite them as evidence; membership
    alone drives the screen.
    """
    raw = inputs.get("substantially_identical_groups", [])
    if not isinstance(raw, list):
        raise ValueError("substantially_identical_groups must be a list")
    groups: dict[str, set[str]] = {}
    seen_ids: set[str] = set()
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ValueError(f"substantially_identical_groups[{index}] must be an object")
        group_id = _text(item.get("id"), f"substantially_identical_groups[{index}].id")
        if group_id in seen_ids:
            raise ValueError(f"duplicate substantially_identical_groups id {group_id}")
        seen_ids.add(group_id)
        instruments = item.get("instrument_ids")
        if not isinstance(instruments, list) or len(instruments) < 2:
            raise ValueError(f"substantially_identical_groups[{index}].instrument_ids must contain at least two ids")
        members = {_text(value, f"substantially_identical_groups[{index}].instrument_ids") for value in instruments}
        for member in members:
            groups.setdefault(member, set()).update(members)
    return groups


def _holding_character(acquired: date, sold: date) -> str:
    if acquired > sold:
        raise ValueError("lot acquired_on cannot be after sale_date")
    try:
        anniversary = acquired.replace(year=acquired.year + 1)
    except ValueError:  # February 29 in a non-leap anniversary year.
        anniversary = acquired.replace(year=acquired.year + 1, day=28)
    return "long_term" if sold > anniversary else "short_term"


def _long_term_from(acquired: date) -> date:
    try:
        anniversary = acquired.replace(year=acquired.year + 1)
    except ValueError:
        anniversary = acquired.replace(year=acquired.year + 1, day=28)
    return anniversary + timedelta(days=1)


# --------------------------------------------------------------------------
# Lots, replacements and wash-sale share matching
# --------------------------------------------------------------------------

@dataclass
class _Lot:
    id: str
    account_id: str
    account_type: str
    tax_treatment: str
    instrument_id: str
    quantity: Decimal
    cost_basis: Decimal
    acquired: date
    holding_start: date
    currency: str

    @property
    def us_taxable(self) -> bool:
        return self.tax_treatment == "taxable" and self.currency == "USD"


@dataclass
class _Replacement:
    ref: str
    kind: str
    account_id: str
    account_type: str
    tax_treatment: str
    instrument_id: str
    acquired: date
    quantity: Decimal
    related_party: str
    lot_id: str | None = None


@dataclass
class _Book:
    """Everything needed to evaluate a sale plan on one sale date."""

    lots: dict[str, _Lot]
    prices: dict[str, dict[str, Any]]
    sale_date: date
    groups: dict[str, set[str]]
    purchases: list[_Replacement]
    linked_purchases: list[dict[str, Any]] = dataclass_field(default_factory=list)

    def identical(self, instrument: str) -> set[str]:
        return self.groups.get(instrument, set()) | {instrument}

    def price_for(self, lot: _Lot) -> dict[str, Any] | None:
        price = self.prices.get(lot.instrument_id)
        if price is None:
            return None
        if price["currency"] != lot.currency:
            raise ValueError(f"price currency for {lot.instrument_id} does not match lot {lot.id}; no FX is inferred")
        if price["as_of"] != self.sale_date:
            raise ValueError(f"price for {lot.instrument_id} must be a verified sale-date quote dated {self.sale_date.isoformat()}")
        return price


def _build_lots(household: dict[str, Any], accounts: dict[str, dict[str, Any]]) -> dict[str, _Lot]:
    result = {}
    for lot in household["lots"]:
        account = accounts[lot["account_id"]]
        acquired = _date(lot["acquired_on"], f"household.lots[{lot['id']}].acquired_on")
        holding_start = _date(lot["holding_period_start"], f"household.lots[{lot['id']}].holding_period_start") if lot.get("holding_period_start") else acquired
        if holding_start > acquired:
            raise ValueError(f"household.lots[{lot['id']}].holding_period_start cannot be after acquired_on")
        quantity = _decimal(lot["quantity"], f"household.lots[{lot['id']}].quantity")
        if quantity == 0:
            raise ValueError(f"household.lots[{lot['id']}].quantity must be greater than zero")
        result[lot["id"]] = _Lot(
            id=lot["id"], account_id=lot["account_id"],
            account_type=canonical_account_type(account["type"]) or str(account["type"]),
            tax_treatment=account_tax_treatment(account["type"]),
            instrument_id=lot["instrument_id"], quantity=quantity,
            cost_basis=_decimal(lot["cost_basis"], f"household.lots[{lot['id']}].cost_basis"),
            acquired=acquired, holding_start=holding_start, currency=str(lot["currency"]).upper(),
        )
    return result


def _wash_purchases(inputs: dict[str, Any], accounts: dict[str, dict[str, Any]], lots: dict[str, _Lot]) -> tuple[list[_Replacement], list[dict[str, Any]]]:
    raw = inputs.get("purchases", [])
    if not isinstance(raw, list):
        raise ValueError("purchases must be a list")
    purchases: list[_Replacement] = []
    linked: list[dict[str, Any]] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ValueError(f"purchases[{index}] must be an object")
        account_id = _text(item.get("account_id"), f"purchases[{index}].account_id")
        related_party = _text(item.get("related_party", "household"), f"purchases[{index}].related_party")
        if account_id not in accounts and related_party not in {"spouse", "controlled_corporation", "taxpayer"}:
            raise ValueError(f"purchases[{index}] outside the household requires an explicit related_party")
        quantity = _decimal(item.get("quantity"), f"purchases[{index}].quantity")
        if quantity == 0:
            raise ValueError(f"purchases[{index}].quantity must be greater than zero")
        instrument = _text(item.get("instrument_id"), f"purchases[{index}].instrument_id")
        trade_date = _date(item.get("trade_date"), f"purchases[{index}].trade_date")
        if item.get("lot_id") is not None:
            lot_id = _text(item["lot_id"], f"purchases[{index}].lot_id")
            if lot_id not in lots:
                raise ValueError(f"purchases[{index}].lot_id does not identify a household lot")
            lot = lots[lot_id]
            if (lot.account_id, lot.instrument_id, lot.acquired) != (account_id, instrument, trade_date):
                raise ValueError(f"purchases[{index}] does not match the account, instrument and acquisition date of lot {lot_id}")
            linked.append({"purchase_index": index, "lot_id": lot_id, "basis": "explicit lot_id"})
            continue
        match = next((
            lot for lot in lots.values()
            if (lot.account_id, lot.instrument_id, lot.acquired, lot.quantity) == (account_id, instrument, trade_date, quantity)
        ), None)
        if match is not None:
            linked.append({"purchase_index": index, "lot_id": match.id, "basis": "same account, instrument, date and quantity"})
            continue
        if account_id in accounts:
            account_type = canonical_account_type(accounts[account_id]["type"]) or str(accounts[account_id]["type"])
            treatment = account_tax_treatment(accounts[account_id]["type"])
        else:
            account_type, treatment = "external", _text(item.get("tax_treatment", "taxable"), f"purchases[{index}].tax_treatment")
        purchases.append(_Replacement(
            ref=f"purchase:{index}", kind="purchase", account_id=account_id, account_type=account_type,
            tax_treatment=treatment, instrument_id=instrument, acquired=trade_date, quantity=quantity,
            related_party=related_party,
        ))
    return purchases, linked


def _replacement_pool(book: _Book, plan: dict[str, Decimal], excluded_accounts: set[str]) -> list[_Replacement]:
    low, high = book.sale_date - timedelta(days=30), book.sale_date + timedelta(days=30)
    pool = [item for item in book.purchases if low <= item.acquired <= high]
    for lot in book.lots.values():
        if lot.account_id in excluded_accounts or not low <= lot.acquired <= high:
            continue
        available = lot.quantity - plan.get(lot.id, Decimal(0))
        if available > 0:
            pool.append(_Replacement(
                ref=f"lot:{lot.id}", kind="household_lot", account_id=lot.account_id, account_type=lot.account_type,
                tax_treatment=lot.tax_treatment, instrument_id=lot.instrument_id, acquired=lot.acquired,
                quantity=available, related_party="household", lot_id=lot.id,
            ))
    return sorted(pool, key=lambda item: (item.acquired, item.ref))


@dataclass
class _Outcome:
    rows: list[dict[str, Any]]
    short_term: Decimal
    long_term: Decimal
    disallowed: Decimal
    permanently_disallowed: Decimal
    adjustments: list[dict[str, Any]]
    missing_prices: list[str]


def _realize(book: _Book, plan: dict[str, Decimal], excluded_accounts: set[str]) -> _Outcome:
    rows: list[dict[str, Any]] = []
    missing: list[str] = []
    for lot_id, quantity in plan.items():
        lot = book.lots[lot_id]
        if lot.tax_treatment != "taxable":
            raise ValueError(f"lot {lot_id} is not in a supported taxable account")
        if lot.currency != "USD":
            raise ValueError(f"U.S. scenario lot {lot_id} must have USD basis; no FX is inferred")
        if quantity > lot.quantity:
            raise ValueError(f"sale quantity exceeds lot {lot_id} quantity")
        price = book.price_for(lot)
        if price is None:
            missing.append(f"prices[{lot.instrument_id}]")
            continue
        basis = lot.cost_basis * quantity / lot.quantity
        proceeds = price["price"] * quantity
        rows.append({
            "lot": lot, "quantity": quantity, "basis": basis, "proceeds": proceeds,
            "gain": proceeds - basis, "character": _holding_character(lot.holding_start, book.sale_date), "price": price,
        })
    pool = _replacement_pool(book, plan, excluded_accounts)
    remaining = {item.ref: item.quantity for item in pool}
    adjustments: list[dict[str, Any]] = []
    for row in sorted((row for row in rows if row["gain"] < 0), key=lambda row: (row["lot"].holding_start, row["lot"].id)):
        lot = row["lot"]
        per_share_loss = -row["gain"] / row["quantity"]
        need = row["quantity"]
        matches = []
        for item in pool:
            if need == 0:
                break
            if item.lot_id == lot.id or item.instrument_id not in book.identical(lot.instrument_id) or remaining[item.ref] == 0:
                continue
            take = min(need, remaining[item.ref])
            remaining[item.ref] -= take
            need -= take
            disallowed = per_share_loss * take
            permanent = item.tax_treatment in {"tax_deferred", "tax_exempt"}
            match = {
                "replacement": item.ref, "kind": item.kind, "account_id": item.account_id, "account_type": item.account_type,
                "related_party": item.related_party, "instrument_id": item.instrument_id,
                "acquired_on": item.acquired.isoformat(), "shares": _quantity(take),
                "disallowed_loss": _money(disallowed), "permanent": permanent,
            }
            matches.append((match, disallowed, permanent))
            if not permanent:
                tacked = (book.sale_date - lot.holding_start).days
                adjustments.append({
                    "replacement": item.ref, "account_id": item.account_id, "instrument_id": item.instrument_id,
                    "shares": _quantity(take), "basis_increase": _money(disallowed),
                    "holding_period_tacked_days": tacked,
                    "adjusted_holding_period_start": (item.acquired - timedelta(days=tacked)).isoformat(),
                    "from_lot_id": lot.id,
                })
        row["wash_matches"] = matches
    short_term = long_term = disallowed_total = permanent_total = Decimal(0)
    output = []
    for row in rows:
        lot = row["lot"]
        matches = row.get("wash_matches", [])
        disallowed = sum((amount for _, amount, _ in matches), Decimal(0))
        permanent = sum((amount for _, amount, flag in matches if flag), Decimal(0))
        recognized = row["gain"] + disallowed
        if row["character"] == "short_term":
            short_term += recognized
        else:
            long_term += recognized
        disallowed_total += disallowed
        permanent_total += permanent
        matched_shares = sum((Decimal(match["shares"]) for match, _, _ in matches), Decimal(0))
        if not matches:
            treatment = "included"
        elif matched_shares >= row["quantity"]:
            treatment = "loss_disallowed_wash_sale"
        else:
            treatment = "loss_partially_disallowed_wash_sale"
        output.append({
            "lot_id": lot.id, "account_id": lot.account_id, "instrument_id": lot.instrument_id,
            "quantity": _quantity(row["quantity"]), "basis": _money(row["basis"]), "proceeds": _money(row["proceeds"]),
            "gain_or_loss": _money(row["gain"]), "recognized_gain_or_loss": _money(recognized),
            "character": row["character"], "acquired_on": lot.acquired.isoformat(),
            **({"holding_period_start": lot.holding_start.isoformat()} if lot.holding_start != lot.acquired else {}),
            "sale_date": book.sale_date.isoformat(), "price": _money(row["price"]["price"]),
            "price_as_of": row["price"]["as_of"].isoformat(), "price_source": row["price"]["source"],
            "wash_sale": {
                "matched_shares": _quantity(matched_shares),
                "disallowed_loss": _money(disallowed),
                "permanently_disallowed_loss": _money(permanent),
                "replacements": [match for match, _, _ in matches],
            },
            "scenario_treatment": treatment,
        })
    return _Outcome(output, short_term, long_term, disallowed_total, permanent_total, adjustments, sorted(set(missing)))


# --------------------------------------------------------------------------
# Netting and tax
# --------------------------------------------------------------------------

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


def _serialise_net(net: dict[str, Decimal]) -> dict[str, str]:
    return {key: _money(value) for key, value in net.items()}


_FACT_KEYS = (
    "short_term_gains", "short_term_losses", "long_term_gains", "long_term_losses",
    "short_term_loss_carryover", "long_term_loss_carryover", "ordinary_income_loss_deduction_available",
)
_RETURN_KEYS = ("ordinary_taxable_income", "qualified_dividends", "magi_excluding_capital_gains", "net_investment_income_excluding_capital_gains")


@dataclass
class _TaxModel:
    """Evaluates the tax effect of scenario ST/LT results, or explains what is missing."""

    method: str | None
    filing_status: str | None
    facts: dict[str, Decimal] | None
    return_facts: dict[str, Decimal] | None
    rates: dict[str, Decimal] | None
    table: dict[str, Any] | None
    tax_year: int
    missing: list[str]
    assumptions: list[str]
    warnings: list[str]

    @property
    def netting_ready(self) -> bool:
        return self.facts is not None

    @property
    def ready(self) -> bool:
        return self.facts is not None and self.method is not None

    def _before(self) -> tuple[Decimal, Decimal]:
        facts = self.facts
        return (
            facts["short_term_gains"] - facts["short_term_losses"] - facts["short_term_loss_carryover"],
            facts["long_term_gains"] - facts["long_term_losses"] - facts["long_term_loss_carryover"],
        )

    def net(self, st: Decimal, lt: Decimal) -> tuple[dict[str, Decimal], dict[str, Decimal]]:
        st_before, lt_before = self._before()
        limit = self.facts["ordinary_income_loss_deduction_available"]
        return _net_us(st_before, lt_before, limit), _net_us(st_before + st, lt_before + lt, limit)

    def _tax(self, net: dict[str, Decimal]) -> tuple[Decimal, dict[str, str]]:
        if self.method == "marginal_rates":
            rates = self.rates
            state_niit = rates["state"] + rates["niit"]
            total = (
                max(net["net_short_term"], Decimal(0)) * (rates["ordinary"] + state_niit)
                + max(net["net_long_term"], Decimal(0)) * (rates["long_term"] + state_niit)
                - net["ordinary_income_loss_deduction"] * (rates["ordinary"] + state_niit)
            )
            return total, {}
        result = us_params.federal_tax(
            self.table, self.filing_status,
            ordinary_income=self.return_facts["ordinary_taxable_income"],
            qualified_dividends=self.return_facts["qualified_dividends"],
            net_short_term_gain=max(net["net_short_term"], Decimal(0)),
            net_capital_gain=max(net["net_long_term"], Decimal(0)),
            capital_loss_deduction=net["ordinary_income_loss_deduction"],
            magi_excluding_capital_gains=self.return_facts["magi_excluding_capital_gains"],
            nii_excluding_capital_gains=self.return_facts["net_investment_income_excluding_capital_gains"],
        )
        return result["total_tax"], {key: _money(value) for key, value in result.items()}

    def incremental(self, st: Decimal, lt: Decimal) -> Decimal | None:
        if not self.ready:
            return None
        before, after = self.net(st, lt)
        return self._tax(after)[0] - self._tax(before)[0]

    def estimate(self, st: Decimal, lt: Decimal) -> dict[str, Any] | None:
        if not self.ready:
            return None
        before, after = self.net(st, lt)
        before_tax, before_parts = self._tax(before)
        after_tax, after_parts = self._tax(after)
        estimate = {
            "currency": "USD",
            "before_scenario": _money(before_tax),
            "after_scenario": _money(after_tax),
            "incremental_tax": _money(after_tax - before_tax),
        }
        if self.method == "marginal_rates":
            estimate.update(
                method="marginal-rate scenario estimate (caller override); negative incremental tax is an estimated reduction, not guaranteed savings",
                rates={key: format(value, "f") for key, value in self.rates.items()},
            )
        else:
            estimate.update(
                method="federal bracket engine: ordinary brackets, 0/15/20% preferential brackets stacked on ordinary taxable income, and NIIT; negative incremental tax is an estimated reduction, not guaranteed savings",
                tax_year=self.tax_year, filing_status=self.filing_status,
                parameters_source=self.table["source"], niit_source=us_params.NIIT["source"],
                components={"before_scenario": before_parts, "after_scenario": after_parts},
                marginal_rates_before_scenario=self.marginal_rates(),
            )
        return estimate

    def marginal_rates(self) -> dict[str, str] | None:
        if not self.ready:
            return None
        step = Decimal(1000)
        base = self.incremental(Decimal(0), Decimal(0))
        return {
            "short_term_gain": _quantity(((self.incremental(step, Decimal(0)) - base) / step).quantize(Decimal("0.0001"))),
            "long_term_gain": _quantity(((self.incremental(Decimal(0), step) - base) / step).quantize(Decimal("0.0001"))),
            "short_term_loss": _quantity(((base - self.incremental(-step, Decimal(0))) / step).quantize(Decimal("0.0001"))),
            "long_term_loss": _quantity(((base - self.incremental(Decimal(0), -step)) / step).quantize(Decimal("0.0001"))),
        }


def _tax_model(inputs: dict[str, Any], sale_date: date) -> _TaxModel:
    missing: list[str] = []
    assumptions: list[str] = []
    warnings: list[str] = []
    tax_year = sale_date.year
    filing = None
    if "filing_status" in inputs:
        filing = _text(inputs["filing_status"], "filing_status").lower()
        if filing not in us_params.FILING_STATUSES:
            raise ValueError("filing_status is not a supported U.S. filing status")
    else:
        missing.append("filing_status")

    facts = None
    raw = inputs.get("us_tax_facts")
    if raw is None:
        missing.extend([*(f"us_tax_facts.{key}" for key in _FACT_KEYS), "us_tax_facts.complete=true", "us_tax_facts.as_of"])
    else:
        if not isinstance(raw, dict):
            raise ValueError("us_tax_facts must be an object")
        fact_missing = [f"us_tax_facts.{key}" for key in _FACT_KEYS if key not in raw]
        if raw.get("complete") is not True:
            fact_missing.append("us_tax_facts.complete=true")
        if "as_of" not in raw:
            fact_missing.append("us_tax_facts.as_of")
        elif _date(raw["as_of"], "us_tax_facts.as_of") != sale_date:
            fact_missing.append(f"us_tax_facts.as_of={sale_date.isoformat()}")
        missing.extend(fact_missing)
        if not fact_missing and filing is not None:
            facts = {key: _decimal(raw[key], f"us_tax_facts.{key}") for key in _FACT_KEYS}
            if facts["ordinary_income_loss_deduction_available"] > us_params.capital_loss_limit(filing):
                raise ValueError("us_tax_facts.ordinary_income_loss_deduction_available exceeds the statutory filing-status limit")
            assumptions.append("us_tax_facts describe the full tax year's other realized results; the scenario is added to them.")

    method = None
    rates = None
    return_facts = None
    table = None
    if inputs.get("rates") is not None:
        raw_rates = inputs["rates"]
        if not isinstance(raw_rates, dict):
            raise ValueError("rates must be an object")
        rate_missing = [f"rates.{key}" for key in ("ordinary", "long_term") if key not in raw_rates]
        missing.extend(rate_missing)
        if not rate_missing:
            rates = {key: _decimal(raw_rates[key], f"rates.{key}") for key in ("ordinary", "long_term")}
            for key in ("state", "niit"):
                rates[key] = _decimal(raw_rates[key], f"rates.{key}") if key in raw_rates else Decimal(0)
                if key in raw_rates:
                    assumptions.append(f"Applied the supplied marginal {key.upper()} rate additively to this scenario.")
            if any(value > 1 for value in rates.values()):
                raise ValueError("rates must be decimals between 0 and 1")
            method = "marginal_rates"
            warnings.append("Caller-supplied marginal rates override the bracket engine; brackets, NIIT thresholds and AMT are not modelled.")
    elif inputs.get("us_return_facts") is not None:
        raw_return = inputs["us_return_facts"]
        if not isinstance(raw_return, dict):
            raise ValueError("us_return_facts must be an object")
        return_missing = [f"us_return_facts.{key}" for key in _RETURN_KEYS if key not in raw_return]
        if raw_return.get("complete") is not True:
            return_missing.append("us_return_facts.complete=true")
        if raw_return.get("tax_year") != tax_year:
            return_missing.append(f"us_return_facts.tax_year={tax_year}")
        missing.extend(return_missing)
        table = us_params.parameters(tax_year)
        if table is None:
            missing.append(f"verified U.S. federal parameters for tax year {tax_year} (or rates as an explicit marginal override)")
            warnings.append(f"No verified federal bracket table exists for {tax_year}; the engine fails closed rather than extrapolating.")
        if not return_missing and table is not None:
            return_facts = {key: _decimal(raw_return[key], f"us_return_facts.{key}", nonnegative=key != "ordinary_taxable_income") for key in _RETURN_KEYS}
            method = "federal_brackets"
            assumptions.append("us_return_facts are full-year projections excluding capital gains, capital-loss deduction and (for ordinary_taxable_income) qualified dividends.")
    else:
        missing.append("us_return_facts (bracket engine) or rates (marginal override)")
    return _TaxModel(method, filing, facts, return_facts, rates, table, tax_year, missing, assumptions, warnings)


# --------------------------------------------------------------------------
# U.S. modes
# --------------------------------------------------------------------------

def _provisional_reasons(inputs: dict[str, Any], household: dict[str, Any], sale_date: date, as_of: date) -> list[str]:
    coverage = inputs.get("wash_sale_coverage", {})
    if not isinstance(coverage, dict):
        raise ValueError("wash_sale_coverage must be an object")
    future_end = sale_date + timedelta(days=30)
    reasons: list[str] = []
    if future_end > as_of:
        reasons.append(f"future replacement-purchase window remains open through {future_end.isoformat()}")
    for flag in ("accounts_complete", "identity_mapping_complete", "automatic_reinvestment_reviewed", "spouse_accounts_reviewed", "controlled_accounts_reviewed"):
        if coverage.get(flag) is not True:
            reasons.append(f"wash_sale_coverage.{flag} is not confirmed")
    if "purchases_from" not in coverage or _date(coverage["purchases_from"], "wash_sale_coverage.purchases_from") > sale_date - timedelta(days=30):
        reasons.append("purchase history does not cover the full 30-day pre-sale window")
    required_through = min(as_of, future_end)
    if "purchases_through" not in coverage or _date(coverage["purchases_through"], "wash_sale_coverage.purchases_through") < required_through:
        reasons.append("purchase history is not current through the required review date")
    if household.get("complete") is not True:
        reasons.append("household account coverage is not marked complete")
    return reasons


def _plan_summary(book: _Book, plan: dict[str, Decimal], outcome: _Outcome, model: _TaxModel) -> dict[str, Any]:
    incremental = model.incremental(outcome.short_term, outcome.long_term)
    carry = None
    if model.netting_ready:
        _, after = model.net(outcome.short_term, outcome.long_term)
        carry = {"short_term": _money(after["short_term_carryforward"]), "long_term": _money(after["long_term_carryforward"])}
    return {
        "lots": [
            {key: row[key] for key in ("lot_id", "quantity", "acquired_on", "basis", "proceeds", "gain_or_loss", "recognized_gain_or_loss", "character")}
            | {"wash_sale_disallowed_loss": row["wash_sale"]["disallowed_loss"]}
            for row in outcome.rows
        ],
        "proceeds": _money(sum((Decimal(row["proceeds"]) for row in outcome.rows), Decimal(0))),
        "realized": {"short_term": _money(outcome.short_term), "long_term": _money(outcome.long_term)},
        "wash_sale_disallowed_loss": _money(outcome.disallowed),
        "incremental_tax": _money(incremental) if incremental is not None else None,
        "carryforward_after": carry,
    }


def _fill(order: list[_Lot], quantity: Decimal) -> dict[str, Decimal]:
    plan: dict[str, Decimal] = {}
    need = quantity
    for lot in order:
        if need <= 0:
            break
        take = min(lot.quantity, need)
        plan[lot.id] = take
        need -= take
    return plan


def _lot_selection(inputs: dict[str, Any], book: _Book, model: _TaxModel, excluded: set[str]) -> tuple[dict[str, Any], list[str], list[str]]:
    target = inputs.get("target")
    if not isinstance(target, dict):
        raise ValueError("lot_selection requires target {instrument_id, proceeds|quantity, account_id?}")
    instrument = _text(target.get("instrument_id"), "target.instrument_id")
    account_filter = _text(target["account_id"], "target.account_id") if target.get("account_id") is not None else None
    lots = [
        lot for lot in book.lots.values()
        if lot.instrument_id == instrument and lot.us_taxable and (account_filter is None or lot.account_id == account_filter)
    ]
    if not lots:
        raise ValueError(f"no taxable USD lots of {instrument} are available for lot selection")
    price = book.price_for(lots[0])
    if price is None:
        return {"target": target}, [f"prices[{instrument}]"], []
    if ("proceeds" in target) == ("quantity" in target):
        raise ValueError("target must give exactly one of proceeds or quantity")
    if "proceeds" in target:
        quantity = _decimal(target["proceeds"], "target.proceeds") / price["price"]
    else:
        quantity = _decimal(target["quantity"], "target.quantity")
    available = sum((lot.quantity for lot in lots), Decimal(0))
    if quantity <= 0 or quantity > available:
        raise ValueError(f"target requires {_quantity(quantity)} shares; {_quantity(available)} are available")
    methods = inputs.get("methods", [m for m in _LOT_METHODS if m != "specific_id" or "specific_lots" in inputs])
    if not isinstance(methods, list) or not methods or any(m not in _LOT_METHODS for m in methods):
        raise ValueError(f"methods must be a nonempty list drawn from {', '.join(_LOT_METHODS)}")
    orders = {
        "fifo": sorted(lots, key=lambda lot: (lot.acquired, lot.id)),
        "lifo": sorted(lots, key=lambda lot: (-lot.acquired.toordinal(), lot.id)),
        "hifo": sorted(lots, key=lambda lot: (-(lot.cost_basis / lot.quantity), lot.acquired, lot.id)),
    }
    plans: dict[str, dict[str, Decimal]] = {name: _fill(order, quantity) for name, order in orders.items() if name in methods}
    missing: list[str] = []
    if "specific_id" in methods:
        specific = _selected_lots(inputs, {lot.id: lot for lot in lots}, "specific_lots")
        if specific is None:
            missing.append("specific_lots")
        else:
            if abs(sum(specific.values(), Decimal(0)) - quantity) > Decimal("0.000001"):
                raise ValueError(f"specific_lots must total the target quantity {_quantity(quantity)}")
            plans["specific_id"] = specific
    comparison: dict[str, Any] = {name: _plan_summary(book, plan, _realize(book, plan, excluded), model) for name, plan in plans.items()}
    search = None
    if "tax_min" in methods:
        if not model.ready:
            comparison["tax_min"] = None
            missing.append("tax facts required for the tax-minimising selector")
        else:
            best, search = _tax_min(book, lots, quantity, model, excluded, list(plans.values()))
            comparison["tax_min"] = _plan_summary(book, best, _realize(book, best, excluded), model)
    ranked = sorted(
        (name for name, summary in comparison.items() if summary and summary["incremental_tax"] is not None),
        key=lambda name: (Decimal(comparison[name]["incremental_tax"]), _LOT_METHODS.index(name)),
    )
    result = {
        "target": {"instrument_id": instrument, "account_id": account_filter, "quantity": _quantity(quantity),
                   "price": _money(price["price"]), "price_as_of": price["as_of"].isoformat()},
        "methods": comparison,
        "ranking_by_incremental_tax": ranked,
        "lowest_tax_method": ranked[0] if ranked else None,
        "tax_min_search": search,
        "note": "Specific identification requires adequate identification to the broker by settlement (Treas. Reg. 1.1012-1(c)); a comparison is not an instruction.",
    }
    return result, missing, []


def _tax_min(book: _Book, lots: list[_Lot], quantity: Decimal, model: _TaxModel, excluded: set[str], seeds: list[dict[str, Decimal]]) -> tuple[dict[str, Decimal], dict[str, Any]]:
    price = book.price_for(lots[0])["price"]

    def gain_per_share(lot: _Lot) -> Decimal:
        return price - lot.cost_basis / lot.quantity

    def character(lot: _Lot) -> str:
        return _holding_character(lot.holding_start, book.sale_date)

    candidates: list[dict[str, Decimal]] = list(seeds)
    rank = {"short_term": 0, "long_term": 1}
    orders = [
        sorted(lots, key=lambda lot: (gain_per_share(lot), lot.id)),
        sorted(lots, key=lambda lot: (gain_per_share(lot) >= 0, rank[character(lot)] if gain_per_share(lot) < 0 else 1 - rank[character(lot)], gain_per_share(lot), lot.id)),
        sorted(lots, key=lambda lot: (gain_per_share(lot) >= 0, 1 - rank[character(lot)] if gain_per_share(lot) < 0 else 1 - rank[character(lot)], gain_per_share(lot), lot.id)),
    ]
    candidates.extend(_fill(order, quantity) for order in orders)
    exhaustive = len(lots) <= _EXHAUSTIVE_LOT_LIMIT
    if exhaustive:
        for size in range(len(lots) + 1):
            for full in combinations(lots, size):
                taken = sum((lot.quantity for lot in full), Decimal(0))
                if taken > quantity:
                    continue
                base = {lot.id: lot.quantity for lot in full}
                if taken == quantity:
                    candidates.append(base)
                    continue
                for partial in lots:
                    if partial.id not in base and partial.quantity >= quantity - taken:
                        candidates.append({**base, partial.id: quantity - taken})
    best: tuple[Any, ...] | None = None
    seen: set[tuple[tuple[str, Decimal], ...]] = set()
    for index, plan in enumerate(candidates):
        key = tuple(sorted(plan.items()))
        if key in seen:
            continue
        seen.add(key)
        outcome = _realize(book, plan, excluded)
        tax = model.incremental(outcome.short_term, outcome.long_term)
        _, after = model.net(outcome.short_term, outcome.long_term)
        carry = after["short_term_carryforward"] + after["long_term_carryforward"]
        score = (tax, -carry, index)
        if best is None or score < best[0]:
            best = (score, plan)
    return best[1], {
        "candidates_evaluated": len(seen),
        "exhaustive_vertex_search": exhaustive,
        "objective": "minimise current-year incremental federal tax (wash sales applied); ties prefer larger remaining carryforward",
        "optimality": (
            "exact over all solutions with at most one partially sold lot; a bracket or loss-limit kink inside a lot can make a two-partial-lot split marginally better"
            if exhaustive else
            f"heuristic: more than {_EXHAUSTIVE_LOT_LIMIT} lots, so ranked orderings and the standard methods were evaluated"
        ),
    }


def _last_trade_date(year: int) -> date:
    day = date(year, 12, 31)
    while day.weekday() >= 5:
        day -= timedelta(days=1)
    return day


def _harvest_tickets(rows: list[dict[str, Any]], cumulative: list[dict[str, Any]],
                     model: _TaxModel, sale_date: date) -> dict[str, Any]:
    """Draft ``order_ticket`` inputs for the ranked harvest: one ticket per account, one sell line per instrument.

    Each line carries the lot ids it relieves (specific identification), each lot's marginal current-year tax
    reduction in plan order, and the wash-sale ``repurchase_not_before`` date.  The draft is only a proposal: the
    model passes ``inputs`` to the ``order_ticket`` task and the person confirms on the card.  Lots whose loss a
    wash sale would disallow, or that add only to carryforwards, are left out and listed with the reason.
    """
    from .execution.tickets import order_symbol  # lazy: execution is optional for tax screens

    marginal = {step["through_lot_id"]: step["marginal_tax_reduction"] for step in cumulative}
    groups: dict[str, dict[str, list[dict[str, Any]]]] = {}
    exclusions: list[dict[str, Any]] = []
    for row in rows:
        lot = row["_lot"]
        symbol = order_symbol(lot.instrument_id)
        saving = marginal.get(lot.id)
        if row["_recognized"] >= 0:
            exclusions.append({"lot_id": lot.id, "reason": "wash_sale",
                               "detail": "Purchases of the same or substantially identical shares within 30 days "
                                         "would disallow the whole loss."})
        elif saving is not None and Decimal(saving) <= 0:
            exclusions.append({"lot_id": lot.id, "reason": "carryforward_only",
                               "detail": "Adds to loss carryforwards with no current-year federal tax reduction "
                                         "under the supplied return facts."})
        elif symbol is None:
            exclusions.append({"lot_id": lot.id, "reason": "not_tradable_here",
                               "detail": f"{lot.instrument_id} is not a US ticker the order card can trade."})
        else:
            groups.setdefault(lot.account_id, {}).setdefault(symbol, []).append({
                "lot_id": lot.id, "quantity": _quantity(lot.quantity), "estimated_tax_saving": saving,
                "character": row["character"], "repurchase_not_before": row["repurchase_not_before"],
                "account_id": lot.account_id})
    drafts = []
    for account_id in sorted(groups):
        orders = []
        for symbol, lots in sorted(groups[account_id].items()):
            quantity = sum((Decimal(lot["quantity"]) for lot in lots), Decimal(0))
            orders.append({"symbol": symbol, "side": "sell", "qty": _quantity(quantity), "type": "limit",
                           "account_id": account_id, "reason": "tax-loss harvest", "tags": ["tax_loss_harvest"],
                           "lots": lots})
        savings = [lot["estimated_tax_saving"] for order in orders for lot in order["lots"]]
        total = None if any(v is None for v in savings) else sum((Decimal(v) for v in savings), Decimal(0))
        repurchase = max(lot["repurchase_not_before"] for order in orders for lot in order["lots"])
        saving_text = (f"an estimated USD {_money(total)} lower federal tax for {sale_date.year} (an estimate from the "
                       "supplied return facts, not guaranteed)" if total is not None
                       else "a tax reduction that cannot be estimated without filing status and return facts")
        drafts.append({
            "account_id": account_id,
            "estimated_tax_saving": _money(total) if total is not None else None,
            "repurchase_not_before": repurchase,
            "inputs": {"source": "user_request", "orders": orders,
                       "rationale": (f"Tax-loss harvest: sell {sum(len(o['lots']) for o in orders)} loss lot(s) by "
                                     f"specific identification for {saving_text}. Do not buy the same or substantially "
                                     f"identical securities in any household account before {repurchase}.")},
        })
    return {
        "order_tickets": drafts,
        "order_ticket_exclusions": exclusions,
        "order_ticket_note": ("Proposals only: pass order_tickets[i].inputs to the order_ticket task; nothing is sent "
                              "until the person confirms the card. Each line lists the lot ids to relieve; ask the broker "
                              "for specific-lot relief (its default method, often FIFO, may pick other lots and change "
                              "the tax result). Savings are marginal in plan order and are null when the tax model "
                              "lacks inputs." + ("" if model.ready else " Filing status and return facts are missing, "
                                                 "so no saving is estimated.")),
    }


def _harvest_report(book: _Book, model: _TaxModel, excluded: set[str]) -> tuple[dict[str, Any], list[str]]:
    sale_date = book.sale_date
    loss_plans = []
    gain_watch = []
    missing: list[str] = []
    for lot in sorted(book.lots.values(), key=lambda lot: (lot.acquired, lot.id)):
        if not lot.us_taxable:
            continue
        price = book.price_for(lot)
        if price is None:
            missing.append(f"prices[{lot.instrument_id}]")
            continue
        gain = price["price"] * lot.quantity - lot.cost_basis
        character = _holding_character(lot.holding_start, sale_date)
        long_term_on = _long_term_from(lot.holding_start)
        if gain < 0:
            loss_plans.append(lot)
        elif gain > 0 and character == "short_term" and (long_term_on - sale_date).days <= 60:
            gain_watch.append({"lot_id": lot.id, "instrument_id": lot.instrument_id, "unrealized_gain": _money(gain),
                               "long_term_from": long_term_on.isoformat(),
                               "note": "Selling before this date realizes a short-term gain; waiting changes the character to long-term."})
    rows = []
    for lot in loss_plans:
        outcome = _realize(book, {lot.id: lot.quantity}, excluded)
        row = outcome.rows[0]
        incremental = model.incremental(outcome.short_term, outcome.long_term)
        recognized = Decimal(row["recognized_gain_or_loss"])
        benefit = -incremental if incremental is not None else None
        long_term_on = _long_term_from(lot.holding_start)
        rows.append({
            "lot_id": lot.id, "account_id": lot.account_id, "instrument_id": lot.instrument_id,
            "quantity": _quantity(lot.quantity), "unrealized_loss": row["gain_or_loss"],
            "recognized_loss_if_sold_alone": _money(recognized), "character": row["character"],
            "wash_sale_disallowed_if_sold_alone": row["wash_sale"]["disallowed_loss"],
            "wash_sale_replacements": row["wash_sale"]["replacements"],
            "standalone_tax_reduction": _money(benefit) if benefit is not None else None,
            "tax_reduction_per_dollar_of_loss": _quantity((benefit / -recognized).quantize(Decimal("0.0001"))) if benefit is not None and recognized < 0 else None,
            "turns_long_term_on": long_term_on.isoformat() if row["character"] == "short_term" else None,
            "repurchase_not_before": (sale_date + timedelta(days=31)).isoformat(),
            "_benefit": benefit if benefit is not None else Decimal(0), "_recognized": recognized, "_lot": lot,
        })
    rows.sort(key=lambda row: (-(row["_benefit"] / -row["_recognized"]) if row["_recognized"] < 0 else Decimal(0), row["character"] != "short_term", row["lot_id"]))
    cumulative = []
    plan: dict[str, Decimal] = {}
    previous = Decimal(0)
    exhausted_at = None
    for row in rows:
        plan[row["lot_id"]] = row["_lot"].quantity
        outcome = _realize(book, dict(plan), excluded)
        incremental = model.incremental(outcome.short_term, outcome.long_term)
        step = None
        if incremental is not None:
            step = previous - incremental
            previous = incremental
            if step <= 0 and exhausted_at is None:
                exhausted_at = row["lot_id"]
        carry = None
        if model.netting_ready:
            _, after = model.net(outcome.short_term, outcome.long_term)
            carry = {"short_term": _money(after["short_term_carryforward"]), "long_term": _money(after["long_term_carryforward"])}
        cumulative.append({
            "through_lot_id": row["lot_id"],
            "recognized": {"short_term": _money(outcome.short_term), "long_term": _money(outcome.long_term)},
            "wash_sale_disallowed": _money(outcome.disallowed),
            "cumulative_tax_reduction": _money(-incremental) if incremental is not None else None,
            "marginal_tax_reduction": _money(step) if step is not None else None,
            "carryforward_after": carry,
        })
    tickets = _harvest_tickets(rows, cumulative, model, sale_date)
    for row in rows:
        for key in ("_benefit", "_recognized", "_lot"):
            row.pop(key)
    deadline = _last_trade_date(sale_date.year)
    report = {
        "tax_year": sale_date.year,
        "planned_trade_date": sale_date.isoformat(),
        "last_trade_date_for_tax_year": deadline.isoformat(),
        "deadline_note": "Trade date governs for losses; exchange holidays are not modelled.",
        "candidates": rows,
        "cumulative_plan": cumulative,
        "current_year_benefit_exhausted_at_lot": exhausted_at,
        "marginal_rates_before_scenario": model.marginal_rates(),
        "short_term_gains_turning_long_term": gain_watch,
        "interpretation": "Losses beyond the point where marginal tax reduction reaches zero only add to carryforwards; ranking is by current-year federal tax reduction per dollar of recognized loss.",
        **tickets,
    }
    warnings = []
    if sale_date > deadline:
        warnings.append(f"Planned trade date {sale_date.isoformat()} is after the last trading day of {sale_date.year}.")
    return report, missing + warnings


def _run_us(inputs: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    household, assumptions, household_warnings = _household(inputs, context)
    if household is None:
        return _envelope("needs_input", {}, missing=["household"], sources=[_us_source(None)])
    accounts, raw_lots = _index_household(household)
    if not raw_lots:
        return _envelope("needs_input", {}, missing=["household.lots"], sources=[_us_source(None)], assumptions=assumptions)
    sale_value = inputs.get("sale_date", household.get("as_of"))
    as_of_value = inputs.get("as_of", household.get("as_of"))
    date_missing = [key for key, value in (("sale_date", sale_value), ("as_of", as_of_value)) if value is None]
    if date_missing:
        return _envelope("needs_input", {}, missing=date_missing, sources=[_us_source(None)], assumptions=assumptions)
    sale_date = _date(sale_value, "sale_date")
    as_of = _date(as_of_value, "as_of")
    source = _us_source(sale_date.year)
    if sale_date > as_of:
        raise ValueError("sale_date cannot be after as_of")
    mode = _text(inputs.get("mode", "harvest"), "mode").lower()
    if mode not in {"harvest", "rebalance", "lot_selection", "harvest_report"}:
        raise ValueError("mode must be harvest, rebalance, lot_selection or harvest_report")
    selected = _selected_lots(inputs, raw_lots)
    if mode == "rebalance" and selected is None:
        return _envelope("needs_input", {}, missing=["sales"], sources=[source], assumptions=assumptions)
    excluded_raw = inputs.get("wash_sale_excluded_account_ids", [])
    if not isinstance(excluded_raw, list):
        raise ValueError("wash_sale_excluded_account_ids must be a list")
    excluded = {_text(item, "wash_sale_excluded_account_ids[]") for item in excluded_raw}
    if excluded - set(accounts):
        raise ValueError("wash_sale_excluded_account_ids must reference household accounts")

    lots = _build_lots(household, accounts)
    purchases, linked = _wash_purchases(inputs, accounts, lots)
    book = _Book(lots, _prices(inputs), sale_date, _identity_groups(inputs), purchases, linked)
    model = _tax_model(inputs, sale_date)
    warnings = list(household_warnings) + model.warnings
    assumptions.extend(model.assumptions)
    assumptions.append(
        "Wash-sale replacements are explicit purchases plus household lots of substantially identical instruments acquired within 30 days of the sale, "
        "matched share-for-share in acquisition order; every household account is treated as the taxpayer's or spouse's unless listed in wash_sale_excluded_account_ids."
    )
    provisional = _provisional_reasons(inputs, household, sale_date, as_of)
    warnings.append("Capital-gain distributions and other distributions are not included as realized security-sale gains by this calculator.")
    scope = "Federal capital-gain scenario only; excludes AMT, 25%/28% gain classes, credits, phase-outs and state tax unless a state marginal rate is supplied."

    if mode in {"lot_selection", "harvest_report"}:
        if mode == "lot_selection":
            payload, extra_missing, extra_warnings = _lot_selection(inputs, book, model, excluded)
        else:
            payload, extra = _harvest_report(book, model, excluded)
            extra_missing = [item for item in extra if item.startswith("prices[")]
            extra_warnings = [item for item in extra if not item.startswith("prices[")]
        warnings.extend(extra_warnings)
        missing = sorted(set(extra_missing)) + model.missing
        result = {
            "jurisdiction": "US federal taxable securities", "mode": mode, "currency": "USD",
            "sale_date": sale_date.isoformat(), "review_status": "provisional" if provisional else "screened",
            "provisional_reasons": provisional, "execution_ready": False,
            "linked_purchases": linked, **payload, "scope": scope,
        }
        status = "partial" if missing or provisional else "ready"
        return _envelope(status, result, missing=missing, warnings=warnings, sources=[source], assumptions=assumptions)

    if selected is None:
        plan = {}
        for lot in lots.values():
            if not lot.us_taxable:
                continue
            price = book.prices.get(lot.instrument_id)
            if price is None or book.price_for(lot)["price"] * lot.quantity < lot.cost_basis:
                plan[lot.id] = lot.quantity
    else:
        plan = selected
    outcome = _realize(book, plan, excluded)
    if outcome.missing_prices:
        return _envelope(
            "needs_input",
            {"jurisdiction": "US", "candidates_with_verified_prices": outcome.rows, "execution_ready": False},
            missing=outcome.missing_prices,
            warnings=["No basis, price, or exchange rate was invented; the scenario is incomplete."],
            sources=[source], assumptions=assumptions,
        )
    netting = None
    if model.netting_ready:
        before, after = model.net(outcome.short_term, outcome.long_term)
        netting = {"before_scenario": _serialise_net(before), "after_scenario": _serialise_net(after)}
    result = {
        "jurisdiction": "US federal taxable securities",
        "mode": mode,
        "currency": "USD",
        "sale_date": sale_date.isoformat(),
        "review_status": "provisional" if provisional else "screened",
        "provisional_reasons": provisional,
        "execution_ready": False,
        "candidates": outcome.rows,
        "included_scenario_gain_or_loss": {"short_term": _money(outcome.short_term), "long_term": _money(outcome.long_term)},
        "wash_sale": {
            "disallowed_loss": _money(outcome.disallowed),
            "permanently_disallowed_loss": _money(outcome.permanently_disallowed),
            "replacement_basis_adjustments": outcome.adjustments,
            "linked_purchases": linked,
        },
        "netting": netting,
        "incremental_tax_estimate": model.estimate(outcome.short_term, outcome.long_term),
        "scope": scope,
    }
    status = "partial" if model.missing or provisional else "ready"
    return _envelope(status, result, missing=model.missing, warnings=warnings, sources=[source], assumptions=assumptions)


# --------------------------------------------------------------------------
# Mexico Article 129
# --------------------------------------------------------------------------

def _run_mx(inputs: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    household, assumptions, household_warnings = _household(inputs, context)
    if household is None:
        return _envelope("needs_input", {}, missing=["household"], sources=[_MX_SOURCE])
    accounts, lots = _index_household(household)
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
    sale_dates: set[date] = set()
    date_missing: list[str] = []
    warnings = list(household_warnings)
    for index, sale in enumerate(sales):
        lot_id = _text(sale.get("lot_id"), f"article_129_sales[{index}].lot_id")
        if lot_id not in lots:
            raise ValueError(f"article_129_sales[{index}].lot_id does not identify a household lot")
        if lot_id in seen_lots:
            raise ValueError(f"duplicate Article 129 sale row for lot {lot_id}")
        seen_lots.add(lot_id)
        lot = lots[lot_id]
        account_id = lot["account_id"]
        if account_tax_treatment(accounts[account_id]["type"]) != "taxable":
            raise ValueError(f"Article 129 lot {lot_id} is not in a supported taxable account")
        if str(lot["currency"]).upper() != "MXN":
            raise ValueError(f"Article 129 lot {lot_id} must use MXN; no FX is inferred")
        sale_value = sale.get("sale_date", inputs.get("sale_date"))
        sale_date = None
        if sale_value is None:
            date_missing.append(f"article_129_sales[{index}].sale_date")
        else:
            sale_date = _date(sale_value, f"article_129_sales[{index}].sale_date")
            sale_dates.add(sale_date)
        quantity = _decimal(sale.get("quantity"), f"article_129_sales[{index}].quantity")
        lot_quantity = _decimal(lot["quantity"], f"household.lots[{lot_id}].quantity")
        acquired = _date(lot["acquired_on"], f"household.lots[{lot_id}].acquired_on")
        if sale_date is not None and acquired > sale_date:
            raise ValueError(f"article_129_sales[{index}].sale_date precedes lot {lot_id} acquisition")
        if quantity == 0 or quantity > lot_quantity:
            raise ValueError(f"article_129_sales[{index}].quantity must be greater than zero and no more than the lot quantity")
        nominal_basis = _decimal(lot["cost_basis"], f"household.lots[{lot_id}].cost_basis") * quantity / lot_quantity
        proceeds = _decimal(sale.get("proceeds_mxn"), f"article_129_sales[{index}].proceeds_mxn")
        adjusted_basis = _decimal(sale.get("article_129_adjusted_basis_mxn"), f"article_129_sales[{index}].article_129_adjusted_basis_mxn")
        basis_source = _text(sale.get("basis_source"), f"article_129_sales[{index}].basis_source")
        proceeds_source = _text(sale.get("proceeds_source"), f"article_129_sales[{index}].proceeds_source")
        if adjusted_basis < nominal_basis:
            warnings.append(
                f"Article 129 adjusted basis for lot {lot_id} ({_money(adjusted_basis)}) is below its nominal household basis ({_money(nominal_basis)}); "
                "confirm the broker's average-cost calculation, which covers all shares of the issuer."
            )
        gain_loss = proceeds - adjusted_basis
        scenario += gain_loss
        rows.append(
            {
                "lot_id": lot_id,
                "account_id": account_id,
                "instrument_id": lot["instrument_id"],
                "quantity": _quantity(quantity),
                "sale_date": sale_date.isoformat() if sale_date else None,
                "acquired_on": acquired.isoformat(),
                "nominal_household_basis_mxn": _money(nominal_basis),
                "proceeds_mxn": _money(proceeds),
                "article_129_adjusted_basis_mxn": _money(adjusted_basis),
                "gain_or_loss_mxn": _money(gain_loss),
                "basis_source": basis_source,
                "proceeds_source": proceeds_source,
                "article_129_eligibility": sale.get("article_129_eligibility", global_eligibility),
            }
        )
    if date_missing:
        return _envelope("needs_input", {"sales": rows, "execution_ready": False}, missing=date_missing,
                         warnings=["The Article 129 tax year is the year of each sale; it is not inferred from as_of."],
                         sources=[_MX_SOURCE], assumptions=assumptions)
    years = {value.year for value in sale_dates}
    if len(years) != 1:
        raise ValueError("Article 129 sales in one scenario must fall in a single tax year")
    current_year = years.pop()
    mx_tax_missing = [
        key for key in ("article_129_realized_gain_or_loss_mxn", "article_129_loss_carryforwards") if key not in inputs
    ]
    if mx_tax_missing:
        return _envelope(
            "partial",
            {
                "jurisdiction": "Mexico Article 129",
                "currency": "MXN",
                "tax_year": current_year,
                "sales": rows,
                "scenario_gain_or_loss_mxn": _money(scenario),
                "incremental_tax_estimate": None,
                "execution_ready": False,
            },
            missing=mx_tax_missing,
            warnings=warnings + ["Current-year Article 129 results and carryforward coverage must be explicit before estimating tax; absence is not treated as zero."],
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
    warnings.extend([
        "This scenario uses supplied Article 129 adjusted basis; it does not recreate statutory average-cost or inflation-index calculations from canonical lot basis.",
        "Distributions are outside this securities-sale calculation and are not treated as gains.",
        "Unused losses require return and intermediary records; failing to use an available loss can forfeit it to the extent it could have been used.",
    ])
    result = {
        "jurisdiction": "Mexico Article 129 eligible listed shares",
        "currency": "MXN",
        "tax_year": current_year,
        "tax_year_basis": "year of sale",
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
        return _envelope("needs_input", {}, missing=["jurisdiction"], sources=[_us_source(None), _MX_SOURCE])
    normalized = _text(jurisdiction, "jurisdiction").upper().replace("-", "_")
    if normalized in {"US", "USA", "US_FEDERAL"}:
        return _run_us(inputs, context)
    if normalized in {"MX", "MEXICO", "MX_ARTICLE_129", "MEXICO_ARTICLE_129"}:
        return _run_mx(inputs, context)
    raise ValueError("jurisdiction must be US or MX_ARTICLE_129")
