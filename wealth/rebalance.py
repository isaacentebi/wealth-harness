"""Tax-aware rebalancing and asset-location guidance.

:func:`plan` turns a household (or a ledger's household view) and sleeve
targets into a concrete, deterministic trade list:

1. New cash and planned contributions are deployed first ("rebalance with
   flows"); sells happen only when a sleeve is still outside its range.
2. Sells are chosen by tax cost.  US: loss lots in taxable accounts (unless a
   recent or planned identical purchase makes it a wash sale), then
   tax-advantaged accounts, then long-term gain lots, then short-term gain
   lots; the tax engine (:mod:`wealth.tax`) re-checks wash sales on the final
   plan and prices it.  Mexico: the SIC/BMV listing decides the 10% Article 129
   regime wherever the security is held; gains on non-SIC securities at a
   foreign broker are progressive income and are not realised by default;
   commission plus IVA on GBM Trading MX is part of the cost ranking.
3. Whole shares where the account requires them (GBM Trading MX, SIC);
   fractional quantities where the account supports them.  Minimum trade size.
4. Reserve and goal-bucket money (``purpose: reserve | goal:<id>`` on an
   account or position, ``constraints.protected_*``) and bank cash are never
   touched.

Every run also returns a no-sell alternative (flows only) and its drift.
Missing prices, lots, FX, commissions or tax facts are reported as missing and
never replaced with zero.

:func:`location` compares where sleeves sit today with a suggested placement
by account type (US) or by broker/listing/domicile (Mexico), with the annual
tax-drag difference as a range under stated assumptions.  It never forces a
trade.  Nothing here places orders.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field, replace as dc_replace
from datetime import date, timedelta
from decimal import Decimal, ROUND_FLOOR
from typing import Any

from . import tax as tax_engine
from ._common import envelope
from .household import account_tax_treatment, canonical_account_type, fx_converter, validate_household
from .mexico import PARAMETERS as MX_PARAMETERS, isr_annual_tax

ZERO = Decimal(0)
ONE = Decimal(1)
_CENT = Decimal("0.01")
_FRACTION_STEP = Decimal("0.0001")
_MX_FACTS = "docs/mexico-investing-facts.md (verified 2026-09-21)"

# Broker platforms.  Commission values are the published GBM rates recorded in
# docs/mexico-investing-facts.md; other platforms have no default commission
# (unknown is not zero), so the caller supplies commission_rate per account.
PLATFORMS: dict[str, dict[str, Any]] = {
    "gbm_trading_mx": {"broker_kind": "mx_broker", "commission_rate": "0.0025", "vat_rate": "0.16", "fractional": False,
                       "venues": ("bmv", "biva", "sic"),
                       "note": "GBM Trading MX: whole shares only; 0.25% commission (<= MXN 1M invested over 3 months) plus 16% IVA."},
    "gbm_trading_usa": {"broker_kind": "foreign_broker", "commission_rate": "0.0025", "fractional": True, "venues": ("us",),
                        "note": "GBM Trading USA (DriveWealth): fractional from US$1, 0.25% per trade; IVA on it is not published and not added."},
    "mx_broker": {"broker_kind": "mx_broker", "fractional": False, "venues": ("bmv", "biva", "sic")},
    "ibkr": {"broker_kind": "foreign_broker", "venues": ("us", "other")},
    "foreign_broker": {"broker_kind": "foreign_broker", "venues": ("us", "other")},
    "us_broker": {"broker_kind": "us_broker", "venues": ("us", "other")},
}
_MX_LISTED_VENUES = frozenset({"bmv", "biva", "sic"})
_ADVANTAGED = frozenset({"tax_deferred", "tax_exempt"})

_SOURCES_US = [
    {"title": "IRS Publication 550 (wash sales, holding period)", "url": tax_engine.IRS_PUB_550},
    {"title": "Treas. Reg. 1.1091-1 and Rev. Rul. 2008-5 (IRA replacement purchases)", "url": tax_engine.IRS_PUB_550},
]
_SOURCES_US_LOCATION = [
    {"title": "IRS Publication 550 (investment income: interest, qualified dividends, capital gains)", "url": tax_engine.IRS_PUB_550},
    {"title": "IRS Publication 590-B (distributions from traditional and Roth IRAs)", "url": "https://www.irs.gov/publications/p590b"},
]
_SOURCES_MX = [
    {"title": "LISR articulo 129 fraccion I (10% on SIC/BMV-listed shares)", "url": tax_engine.MEXICO_LISR},
    {"title": "SAT Anexo 7 RMF 2026, criterio normativo 37/ISR/N",
     "url": "https://www.sat.gob.mx/minisitio/NormatividadRMFyRGCE/documentos2026/rmf/anexos/Anexo_7_RMF2026-09012026.pdf"},
    {"title": "LISR Titulo IV Capitulo IV (progressive gains on non-SIC foreign securities)", "url": tax_engine.MEXICO_LISR},
    {"title": "GBM fee and product pages, as recorded in " + _MX_FACTS, "url": "https://gbm.com/"},
]


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _dec(value: Any, path: str, *, nonnegative: bool = True) -> Decimal:
    return tax_engine._decimal(value, path, nonnegative=nonnegative)


def _opt_dec(value: Any, path: str) -> Decimal | None:
    return None if value is None else _dec(value, path)


def _share(value: Any, path: str) -> Decimal:
    """A share of the portfolio written as a fraction (0.05 = 5%); a percent such as 5 is rejected."""
    result = _dec(value, path)
    if result > ONE:
        raise ValueError(f"{path} must be a fraction between 0 and 1, not a percent (got {value}); "
                         f"use {format(result / 100, 'f')} for {value}% (usa {format(result / 100, 'f')} para {value}%)")
    return result


def _date(value: Any, path: str) -> date:
    return tax_engine._date(value, path)


def _money(value: Decimal | None) -> str | None:
    return None if value is None else format(value.quantize(_CENT) + 0, "f")


def _qty(value: Decimal) -> str:
    return tax_engine._quantity(value)


def _ratio(value: Decimal | None) -> str | None:
    return None if value is None else format(value.quantize(Decimal("0.0001")) + 0, "f")


def _obj(value: Any, path: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"{path} must be an object")
    return value


def _protected_purpose(value: Any) -> bool:
    return isinstance(value, str) and (value == "reserve" or value.startswith("goal:"))


def _kind(name: str, explicit: Any = None) -> str:
    """Location kind of a sleeve: bond, mx_fixed_income, reit, high_growth, intl_equity, broad_equity, cash, other."""
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip().lower()
    text = name.lower()
    if any(word in text for word in ("cetes", "udibono", "mbono", "bondes", "mx_fixed", "mx_bond", "fixed_income_mx", "renta_fija")):
        return "mx_fixed_income"
    if any(word in text for word in ("bond", "fixed", "treasur", "credit", "tips")):
        return "bond"
    if any(word in text for word in ("reit", "fibra", "real_estate")):
        return "reit"
    if "cash" in text:
        return "cash"
    if any(word in text for word in ("growth", "small", "emerging", "tech", "nasdaq")):
        return "high_growth"
    if any(word in text for word in ("intl", "international", "developed", "ex_us", "global")):
        return "intl_equity"
    if any(word in text for word in ("mx_equity", "naftrac", "ipc")):
        return "mx_equity"
    if any(word in text for word in ("equity", "stock", "index", "s&p", "sp500", "total_market", "us_")):
        return "broad_equity"
    return "other"


_EQUITY_KINDS = frozenset({"broad_equity", "high_growth", "intl_equity", "us_equity", "mx_equity", "equity"})
_ORDINARY_KINDS = frozenset({"bond", "reit", "cash"})


# ---------------------------------------------------------------------------
# Resolved inputs
# ---------------------------------------------------------------------------

@dataclass
class _Account:
    id: str
    type: str
    treatment: str
    currency: str
    platform: str | None
    broker_kind: str | None
    commission_rate: Decimal | None
    vat_rate: Decimal
    fractional: bool
    min_trade: Decimal
    venues: tuple[str, ...] | None
    protected: bool
    deployable_cash: bool

    @property
    def cost_rate(self) -> Decimal | None:
        return None if self.commission_rate is None else self.commission_rate * (ONE + self.vat_rate)


@dataclass
class _Instrument:
    id: str
    symbol: str
    currency: str | None
    price: Decimal | None
    price_as_of: str | None
    price_source: str | None
    asset_class: str | None
    sleeve: str | None
    underlying: str | None
    venue: str | None
    sic_listed: bool | None
    security_type: str
    us_situs: bool | None
    fractional: bool | None
    accounts: tuple[str, ...] | None


@dataclass
class _Holding:
    account: str
    instrument: str
    quantity: Decimal
    sleeve: str | None
    lots: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class _Sleeve:
    name: str
    weight: Decimal
    low: Decimal
    high: Decimal
    buy: list[str]
    kind: str


class _Context:
    """Everything the simulation needs, resolved and validated once."""

    def __init__(self, household: dict[str, Any], targets: dict[str, Any], jurisdiction_context: dict[str, Any],
                 cash_flows: list[Any] | None, tax_inputs: dict[str, Any] | None, constraints: dict[str, Any] | None,
                 missing: list[str], warnings: list[str], assumptions: list[str]):
        self.missing, self.warnings, self.assumptions = missing, warnings, assumptions
        jc = _obj(jurisdiction_context, "jurisdiction_context")
        self.constraints = _obj(constraints, "constraints")
        self.tax_inputs = _obj(tax_inputs, "tax_inputs")
        jurisdiction = str(jc.get("jurisdiction", "")).upper()
        if jurisdiction not in {"US", "MX"}:
            raise ValueError("jurisdiction_context.jurisdiction must be US or MX")
        self.jurisdiction = jurisdiction
        self.household = household
        self.as_of = _date(household["as_of"], "household.as_of")
        self.trade_date = _date(jc["trade_date"], "jurisdiction_context.trade_date") if jc.get("trade_date") else self.as_of
        if self.trade_date < self.as_of:
            raise ValueError("jurisdiction_context.trade_date cannot precede household.as_of")
        self.currency = str(jc.get("currency") or household["currency"])
        self.fx = fx_converter(household, self.trade_date, int(jc.get("max_fx_age_days", 7)), warnings)
        mode = self.constraints.get("mode", "full")
        if mode not in {"full", "flows_only"}:
            raise ValueError("constraints.mode must be full or flows_only")
        self.mode = mode
        self.min_trade = _dec(self.constraints.get("min_trade_amount", 0), "constraints.min_trade_amount")
        self.harvest_first = self.constraints.get("harvest_losses_first", True) is not False
        self.avoid_progressive = self.constraints.get("avoid_progressive_gains", True) is not False
        self.sell_to = self.constraints.get("sell_to", "target")
        if self.sell_to not in {"target", "band_edge"}:
            raise ValueError("constraints.sell_to must be target or band_edge")
        self.protected_accounts = set(self.constraints.get("protected_account_ids") or [])
        self.protected_positions = set(self.constraints.get("protected_position_ids") or [])
        self.no_sell = set(self.constraints.get("no_sell_instrument_ids") or [])
        self.deployable_bank = set(self.constraints.get("deployable_cash_account_ids") or [])
        self.accounts = self._accounts(jc)
        self.instruments = self._instruments(jc)
        self.sleeves, self.sleeve_by, self.sleeve_map = self._sleeves(_obj(targets, "targets"))
        self.cash_sleeve = next((s for s in self.sleeves if s.kind == "cash" and s.name == "cash"), None)
        self.protected: list[dict[str, Any]] = []
        self.outside: list[dict[str, Any]] = []
        self.holdings: dict[tuple[str, str], _Holding] = {}
        self.cash: dict[str, Decimal] = {}
        self._positions()
        self._flows(cash_flows)
        self.groups = tax_engine._identity_groups(self.tax_inputs)
        self.used_lots: list[tuple[str, Decimal]] = []
        self.recent_buys = self._recent_buys()

    # -- accounts and instruments -------------------------------------------
    def _accounts(self, jc: dict[str, Any]) -> dict[str, _Account]:
        overrides = _obj(jc.get("accounts"), "jurisdiction_context.accounts")
        result: dict[str, _Account] = {}
        for raw in self.household["accounts"]:
            extra = _obj(overrides.get(raw["id"]), f"jurisdiction_context.accounts.{raw['id']}")
            merged = {**raw, **extra}
            platform = merged.get("platform")
            if platform is not None and platform not in PLATFORMS:
                raise ValueError(f"accounts {raw['id']}: platform must be one of {', '.join(sorted(PLATFORMS))}")
            spec = PLATFORMS.get(platform, {})
            if spec.get("note"):
                self.assumptions.append(spec["note"] + f" Source: {_MX_FACTS}.")
            canonical = canonical_account_type(merged.get("type")) or str(merged.get("type"))
            commission = merged.get("commission_rate", spec.get("commission_rate"))
            venues = merged.get("venues", spec.get("venues"))
            result[raw["id"]] = _Account(
                id=raw["id"], type=canonical, treatment=account_tax_treatment(merged.get("type")),
                currency=raw["currency"], platform=platform,
                broker_kind=merged.get("broker_kind", spec.get("broker_kind")),
                commission_rate=_opt_dec(commission, f"accounts.{raw['id']}.commission_rate"),
                vat_rate=_dec(merged.get("vat_rate", spec.get("vat_rate", 0)), f"accounts.{raw['id']}.vat_rate"),
                fractional=bool(merged.get("fractional", spec.get("fractional", False))),
                min_trade=_dec(merged.get("min_trade_amount", 0), f"accounts.{raw['id']}.min_trade_amount"),
                venues=tuple(venues) if venues else None,
                protected=_protected_purpose(merged.get("purpose")) or raw["id"] in self.protected_accounts,
                deployable_cash=canonical != "bank" or raw["id"] in self.deployable_bank,
            )
        unknown = set(overrides) - set(result)
        if unknown:
            raise ValueError(f"jurisdiction_context.accounts names unknown accounts {sorted(unknown)}")
        return result

    def _instruments(self, jc: dict[str, Any]) -> dict[str, _Instrument]:
        overrides = _obj(jc.get("instruments"), "jurisdiction_context.instruments")
        seen: dict[str, dict[str, Any]] = {}
        for position in self.household["positions"]:
            seen.setdefault(position["instrument_id"], position)
        result: dict[str, _Instrument] = {}
        for instrument_id in sorted(set(seen) | set(overrides)):
            position = seen.get(instrument_id, {})
            extra = _obj(overrides.get(instrument_id), f"jurisdiction_context.instruments.{instrument_id}")
            price = price_as_of = source = None
            currency = extra.get("currency", position.get("currency"))
            if extra.get("price") is not None:
                price = _dec(extra["price"], f"jurisdiction_context.instruments.{instrument_id}.price")
                if price == 0:
                    raise ValueError(f"jurisdiction_context.instruments.{instrument_id}.price must be positive")
                price_as_of = extra.get("price_as_of", self.trade_date.isoformat())
                source = extra.get("price_source", "caller-supplied price")
                if currency is None:
                    raise ValueError(f"jurisdiction_context.instruments.{instrument_id}.currency is required with a price")
            elif position and Decimal(str(position["quantity"])) > 0 and position.get("asset_class") != "cash":
                price = Decimal(str(position["value"])) / Decimal(str(position["quantity"]))
                price_as_of = self.household["as_of"]
                source = "position value / quantity"
            if price_as_of is not None and (self.trade_date - _date(price_as_of, f"{instrument_id}.price_as_of")).days > int(jc.get("max_price_age_days", 5)):
                self.warnings.append(f"Price for {instrument_id} is dated {price_as_of}, more than {int(jc.get('max_price_age_days', 5))} days before the trade date; refresh it before acting.")
            venue = extra.get("venue", position.get("venue"))
            sic = extra.get("sic_listed")
            if sic is None and venue == "sic":
                sic = True
            if sic is not None and not isinstance(sic, bool):
                raise ValueError(f"instruments.{instrument_id}.sic_listed must be a boolean")
            domicile = extra.get("issuer_domicile", position.get("issuer_domicile"))
            situs = extra.get("us_situs")
            if situs is None and domicile is not None:
                situs = domicile == "US"
            security_type = extra.get("security_type", "share")
            if security_type not in {"share", "equity_etf", "other_etf"}:
                raise ValueError(f"instruments.{instrument_id}.security_type must be share, equity_etf or other_etf")
            accounts = extra.get("accounts")
            result[instrument_id] = _Instrument(
                id=instrument_id, symbol=extra.get("symbol", position.get("symbol", instrument_id)), currency=currency,
                price=price, price_as_of=price_as_of, price_source=source,
                asset_class=extra.get("asset_class", position.get("asset_class")), sleeve=extra.get("sleeve"),
                underlying=extra.get("underlying_symbol", position.get("underlying_symbol")), venue=venue,
                sic_listed=sic, security_type=security_type, us_situs=situs,
                fractional=extra.get("fractional"), accounts=tuple(accounts) if accounts else None,
            )
        return result

    def _sleeves(self, targets: dict[str, Any]) -> tuple[list[_Sleeve], str, dict[str, str]]:
        by = targets.get("by", "asset_class")
        if by not in {"asset_class", "underlying", "instrument"}:
            raise ValueError("targets.by must be asset_class, underlying or instrument")
        raw = targets.get("sleeves")
        if not isinstance(raw, list) or not raw:
            raise ValueError("targets.sleeves must be a nonempty list of {name, weight, min?, max?, buy?}")
        default_band = _share(targets.get("band", "0.05"), "targets.band")
        sleeves: list[_Sleeve] = []
        for index, item in enumerate(raw):
            path = f"targets.sleeves[{index}]"
            if not isinstance(item, dict):
                raise ValueError(f"{path} must be an object")
            name = tax_engine._text(item.get("name"), f"{path}.name")
            if any(s.name == name for s in sleeves):
                raise ValueError(f"duplicate sleeve {name}")
            weight = _share(item.get("weight"), f"{path}.weight")
            band = _share(item.get("band", default_band), f"{path}.band")
            low = _share(item["min"], f"{path}.min") if item.get("min") is not None else max(weight - band, ZERO)
            high = _share(item["max"], f"{path}.max") if item.get("max") is not None else min(weight + band, ONE)
            if not low <= weight <= high <= 1:
                raise ValueError(f"{path} needs min <= weight <= max <= 1")
            buy = item.get("buy") or []
            if not isinstance(buy, list):
                raise ValueError(f"{path}.buy must be a list of instrument ids")
            sleeves.append(_Sleeve(name, weight, low, high, [str(x) for x in buy], _kind(name, item.get("kind"))))
        total = sum((s.weight for s in sleeves), ZERO)
        if abs(total - ONE) > Decimal("0.0001"):
            raise ValueError(f"targets.sleeves weights must sum to 1 (got {total})")
        mapping = _obj(targets.get("map"), "targets.map")
        names = {s.name for s in sleeves}
        bad = {v for v in mapping.values() if v not in names}
        if bad:
            raise ValueError(f"targets.map points to unknown sleeves {sorted(bad)}")
        return sleeves, by, {str(k): str(v) for k, v in mapping.items()}

    # -- holdings, cash and flows ---------------------------------------------
    def sleeve_of(self, instrument_id: str, position: dict[str, Any] | None = None) -> str | None:
        names = {s.name for s in self.sleeves}
        if instrument_id in self.sleeve_map:
            return self.sleeve_map[instrument_id]
        meta = self.instruments.get(instrument_id)
        if meta and meta.sleeve in names:
            return meta.sleeve
        if self.sleeve_by == "instrument":
            key = instrument_id
        elif self.sleeve_by == "underlying":
            key = (position or {}).get("underlying_symbol") or (meta.underlying if meta else None) or (meta.symbol if meta else None)
        else:
            key = (position or {}).get("asset_class") or (meta.asset_class if meta else None)
        return key if key in names else None

    def to_rep(self, amount: Decimal, currency: str, label: str) -> Decimal | None:
        return self.fx.convert(amount, currency, self.currency, label)

    def rate(self, source: str, target: str) -> Decimal | None:
        return self.fx.rate(source, target)

    def _positions(self) -> None:
        lots_by: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for lot in self.household["lots"]:
            lots_by.setdefault((lot["account_id"], lot["instrument_id"]), []).append(lot)
        for position in sorted(self.household["positions"], key=lambda p: (p["account_id"], p["instrument_id"])):
            account = self.accounts[position["account_id"]]
            value = Decimal(str(position["value"]))
            rep = self.to_rep(value, position["currency"], f"position {position['id']}")
            label = {"position_id": position["id"], "account_id": account.id, "instrument_id": position["instrument_id"],
                     "value": _money(rep) if rep is not None else None, "currency": self.currency}
            if account.protected or position["id"] in self.protected_positions or _protected_purpose(position.get("purpose")):
                self.protected.append({**label, "reason": "reserve or goal money; never traded or deployed"})
                continue
            if rep is None:
                self.missing.append(f"fx.{position['currency']}/{self.currency} (position {position['id']})")
                self.outside.append({**label, "reason": "no usable FX rate"})
                continue
            is_cash = position.get("asset_class") == "cash" or str(position["instrument_id"]).startswith("cash:")
            if is_cash:
                if not account.deployable_cash:
                    self.outside.append({**label, "reason": "bank cash is not deployed unless listed in constraints.deployable_cash_account_ids"})
                    continue
                if account.treatment not in {"taxable", "tax_deferred", "tax_exempt"}:
                    self.outside.append({**label, "reason": f"account treatment {account.treatment} is not modelled"})
                    continue
                self.cash[account.id] = self.cash.get(account.id, ZERO) + rep
                continue
            sleeve = self.sleeve_of(position["instrument_id"], position)
            if sleeve is None:
                self.outside.append({**label, "reason": "not assigned to a target sleeve (targets.map or jurisdiction_context.instruments.<id>.sleeve)"})
                self.warnings.append(f"Position {position['id']} is not assigned to any sleeve; it is held outside the plan.")
                continue
            if account.treatment not in {"taxable", "tax_deferred", "tax_exempt"}:
                self.outside.append({**label, "reason": f"account treatment {account.treatment} is not modelled"})
                continue
            instrument = self.instruments[position["instrument_id"]]
            if instrument.price is None:
                self.missing.append(f"prices.{position['instrument_id']}")
                self.outside.append({**label, "reason": "no price"})
                continue
            if self.price_rep(position["instrument_id"]) is None:
                self.missing.append(f"fx.{instrument.currency}/{self.currency} (price of {position['instrument_id']})")
                self.outside.append({**label, "reason": f"no usable FX rate for its {instrument.currency} price"})
                continue
            self.holdings[(account.id, position["instrument_id"])] = _Holding(
                account.id, position["instrument_id"], Decimal(str(position["quantity"])), sleeve,
                sorted(lots_by.get((account.id, position["instrument_id"]), []), key=lambda l: (l["acquired_on"], l["id"])))

    def _flows(self, cash_flows: list[Any] | None) -> None:
        self.flows: list[dict[str, Any]] = []
        if cash_flows is None:
            return
        if not isinstance(cash_flows, list):
            raise ValueError("cash_flows must be a list")
        for index, flow in enumerate(cash_flows):
            path = f"cash_flows[{index}]"
            if not isinstance(flow, dict):
                raise ValueError(f"{path} must be an object")
            account_id = tax_engine._text(flow.get("account_id"), f"{path}.account_id")
            if account_id not in self.accounts:
                raise ValueError(f"{path}.account_id does not reference a household account")
            amount = _dec(flow.get("amount"), f"{path}.amount", nonnegative=False)
            if amount <= 0:
                raise ValueError(f"{path}.amount must be positive; withdrawals are not modelled here (use the plan task)")
            currency = flow.get("currency", self.accounts[account_id].currency)
            if flow.get("date") and _date(flow["date"], f"{path}.date") > self.trade_date:
                self.assumptions.append(f"{path} is planned for {flow['date']}; the plan treats it as available on the trade date.")
            account = self.accounts[account_id]
            if account.protected or _protected_purpose(flow.get("purpose")):
                self.warnings.append(f"{path} is reserve or goal money and was not deployed.")
                continue
            rep = self.to_rep(amount, currency, path)
            if rep is None:
                self.missing.append(f"fx.{currency}/{self.currency} ({path})")
                continue
            self.cash[account_id] = self.cash.get(account_id, ZERO) + rep
            self.flows.append({"id": flow.get("id", path), "account_id": account_id, "amount": _money(amount), "currency": currency})

    def _recent_buys(self) -> list[tuple[str, str, date, str]]:
        """(account, instrument, date, ref) purchases in the 30 days before the trade date."""
        low = self.trade_date - timedelta(days=30)
        result = []
        raw = self.tax_inputs.get("purchases") or []
        if not isinstance(raw, list):
            raise ValueError("tax_inputs.purchases must be a list")
        for index, item in enumerate(raw):
            if not isinstance(item, dict):
                raise ValueError(f"tax_inputs.purchases[{index}] must be an object")
            day = _date(item.get("trade_date"), f"tax_inputs.purchases[{index}].trade_date")
            if low <= day <= self.trade_date:
                result.append((str(item.get("account_id")), str(item.get("instrument_id")), day, f"purchase:{index}"))
        for lot in self.household["lots"]:
            day = _date(lot["acquired_on"], "lot.acquired_on")
            if low <= day <= self.trade_date:
                result.append((lot["account_id"], lot["instrument_id"], day, f"lot:{lot['id']}"))
        return result

    def identical(self, instrument: str) -> set[str]:
        return self.groups.get(instrument, set()) | {instrument}

    def price_rep(self, instrument_id: str) -> Decimal | None:
        meta = self.instruments.get(instrument_id)
        if meta is None or meta.price is None or meta.currency is None:
            return None
        rate = self.rate(meta.currency, self.currency)
        return None if rate is None else meta.price * rate

    def usd_price(self, instrument: _Instrument) -> Decimal | None:
        """Sale price per share in USD, the currency of a US gain (trade-date FX)."""
        if instrument.price is None or instrument.currency is None:
            return None
        rate = self.rate(instrument.currency, "USD")
        if rate is None:
            return None
        if instrument.currency != "USD":
            self._note_usd_conversion()
        return instrument.price * rate

    def usd_basis(self, lot: dict[str, Any]) -> Decimal | None:
        """Lot basis in USD: ``cost_basis_usd`` when supplied, else the basis at trade-date FX."""
        if lot.get("cost_basis_usd") is not None:
            return _dec(lot["cost_basis_usd"], f"lots[{lot['id']}].cost_basis_usd")
        rate = self.rate(str(lot["currency"]).upper(), "USD")
        if rate is None:
            return None
        if str(lot["currency"]).upper() != "USD":
            self._note_usd_conversion()
        return Decimal(str(lot["cost_basis"])) * rate

    def _note_usd_conversion(self) -> None:
        note = ("A US gain on a non-USD holding is computed in USD: proceeds at the trade-date FX rate and basis at "
                "lots[].cost_basis_usd when supplied, otherwise at the same trade-date rate. US tax uses the rate on "
                "the acquisition date for basis, so without cost_basis_usd the currency part of the gain is left out; "
                "treat the gain and tax as approximate.")
        if note not in self.assumptions:
            self.assumptions.append(note)

    def whole_shares(self, account: _Account, instrument: _Instrument) -> bool:
        if instrument.fractional is False:
            return True
        if instrument.venue in _MX_LISTED_VENUES:
            return True  # SIC and BMV trade whole shares
        return not account.fractional

    def can_hold(self, account: _Account, instrument: _Instrument) -> bool:
        if account.protected or account.treatment not in {"taxable", "tax_deferred", "tax_exempt"} or not account.deployable_cash:
            return False
        if instrument.accounts is not None:
            return account.id in instrument.accounts
        if account.venues is not None:
            return instrument.venue in account.venues if instrument.venue else self.jurisdiction == "US"
        if self.jurisdiction == "MX" and instrument.venue in _MX_LISTED_VENUES:
            return account.broker_kind in {None, "mx_broker"}
        return True


# ---------------------------------------------------------------------------
# Tax treatment of a sale
# ---------------------------------------------------------------------------

@dataclass
class _Unit:
    """One sellable slice: a lot (US taxable), a whole holding at average cost (MX), or an advantaged holding."""

    account: str
    instrument: str
    quantity: Decimal
    tier: int
    rank: Decimal
    gain_per_share: Decimal | None  # in the tax currency (USD for US lots, MXN for MX)
    tax_rate: Decimal | None  # marginal rate applied to the gain
    regime: str
    lot: dict[str, Any] | None = None
    character: str | None = None
    note: str = ""


def _us_rates(ctx: _Context) -> tuple[dict[str, Decimal] | None, Any]:
    """Per-trade marginal rates and the tax-engine model (or None)."""
    ti = ctx.tax_inputs
    model = None
    if ti.get("us_return_facts") is not None or ti.get("rates") is not None:
        model = tax_engine._tax_model(ti, ctx.trade_date)
    marginal = model.marginal_rates() if model is not None and model.ready else None
    if marginal is not None:
        return {key: Decimal(value) for key, value in marginal.items()}, model
    raw = ti.get("rates")
    if isinstance(raw, dict) and "ordinary" in raw and "long_term" in raw:
        extra = sum((_dec(raw.get(k, 0), f"tax_inputs.rates.{k}") for k in ("state", "niit")), ZERO)
        st = _dec(raw["ordinary"], "tax_inputs.rates.ordinary") + extra
        lt = _dec(raw["long_term"], "tax_inputs.rates.long_term") + extra
        return {"short_term_gain": st, "long_term_gain": lt, "short_term_loss": st, "long_term_loss": lt}, model
    return None, model


def _mx_progressive_rate(ctx: _Context) -> tuple[Decimal | None, Decimal | None]:
    """(marginal progressive rate, taxable income base) from tax_inputs."""
    ti = ctx.tax_inputs
    if ti.get("marginal_rate") is not None:
        return _dec(ti["marginal_rate"], "tax_inputs.marginal_rate"), None
    if ti.get("taxable_income_before_mxn") is not None:
        base = _dec(ti["taxable_income_before_mxn"], "tax_inputs.taxable_income_before_mxn")
        tariff = _mx_tariff(ctx.trade_date.year)
        if tariff is None:
            return None, base
        step = Decimal(1000)
        return (isr_annual_tax(base + step, tariff) - isr_annual_tax(base, tariff)) / step, base
    return None, None


def _mx_tariff(year: int) -> list[dict[str, str]] | None:
    entry = MX_PARAMETERS["isr_annual_tariff"].get(year)
    return entry["value"] if entry and entry.get("status") == "verified" else None


def _mx_regime(ctx: _Context, account: _Account, instrument: _Instrument) -> str:
    """article_129 | progressive | unknown for a sale by a Mexican resident."""
    if instrument.venue in _MX_LISTED_VENUES and account.broker_kind in {None, "mx_broker"}:
        return "article_129"
    if instrument.sic_listed is True:
        return "article_129"
    if instrument.sic_listed is False:
        return "progressive"
    return "unknown"


def _wash_blocker(ctx: _Context, instrument: str, planned_buys: list[tuple[str, str]], lot_id: str | None) -> str | None:
    identical = ctx.identical(instrument)
    for account, other, day, ref in ctx.recent_buys:
        if other in identical and ref != f"lot:{lot_id}":
            return f"{other} bought in {account} on {day.isoformat()} (within 30 days before the sale)"
    for account, other in planned_buys:
        if other in identical:
            return f"this plan buys {other} in {account}"
    return None


def _units(ctx: _Context, holding: _Holding, planned_buys: list[tuple[str, str]], rates: dict[str, Decimal] | None,
           mx_rate: Decimal | None, blocked: list[dict[str, Any]]) -> list[_Unit]:
    account = ctx.accounts[holding.account]
    instrument = ctx.instruments[holding.instrument]
    price = instrument.price
    cost_rate = account.cost_rate or ZERO
    if holding.quantity <= 0:
        return []
    if holding.instrument in ctx.no_sell:
        blocked.append({"account_id": account.id, "instrument_id": holding.instrument, "reason": "constraints.no_sell_instrument_ids"})
        return []
    if account.treatment in _ADVANTAGED:
        return [_Unit(account.id, holding.instrument, holding.quantity, 1, cost_rate, None, ZERO, account.treatment,
                      note=f"{account.type} account: no tax on the sale")]
    if ctx.jurisdiction == "US":
        if not holding.lots:
            ctx.missing.append(f"household.lots for {account.id}/{holding.instrument} (taxable sale needs basis and dates)")
            blocked.append({"account_id": account.id, "instrument_id": holding.instrument, "reason": "missing lots"})
            return []
        units = []
        price_usd = ctx.usd_price(instrument)
        for lot in holding.lots:
            quantity = Decimal(str(lot["quantity"]))
            if quantity <= 0:
                continue
            basis_usd = ctx.usd_basis(lot)
            if price_usd is None or basis_usd is None:
                pair = instrument.currency if price_usd is None else lot["currency"]
                ctx.missing.append(f"fx.{pair}/USD (US gain on lot {lot['id']} is computed in USD)")
                blocked.append({"account_id": account.id, "instrument_id": holding.instrument, "lot_id": lot["id"],
                                "reason": "gain in USD cannot be computed without FX"})
                continue
            per_share = price_usd - basis_usd / quantity
            start = _date(lot.get("holding_period_start") or lot["acquired_on"], "lot.acquired_on")
            character = tax_engine._holding_character(start, ctx.trade_date)
            if per_share < 0:
                blocker = _wash_blocker(ctx, holding.instrument, planned_buys, lot["id"])
                if blocker:
                    blocked.append({"account_id": account.id, "instrument_id": holding.instrument, "lot_id": lot["id"],
                                    "reason": f"wash sale: {blocker}; the loss would be disallowed"})
                    continue
                tier = 0 if ctx.harvest_first else 2
            else:
                tier = 3 if character == "long_term" else 4
            rate = None if rates is None else rates[f"{character}_{'loss' if per_share < 0 else 'gain'}"]
            rank = (per_share / price_usd) * (rate if rate is not None else ONE) + cost_rate
            units.append(_Unit(account.id, holding.instrument, quantity, tier, rank, per_share, rate, "us_capital_gain",
                               lot=lot, character=character))
        return units
    # Mexico: average cost in MXN for the whole holding.
    regime = _mx_regime(ctx, account, instrument)
    to_mxn = ctx.rate(instrument.currency, "MXN") if instrument.currency else None
    basis_mxn = ZERO
    for lot in holding.lots:
        if lot.get("cost_basis_mxn") is not None:
            basis_mxn += Decimal(str(lot["cost_basis_mxn"]))
        elif lot["currency"] == "MXN":
            basis_mxn += Decimal(str(lot["cost_basis"]))
        else:
            basis_mxn = None
            ctx.missing.append(f"lots[{lot['id']}].cost_basis_mxn (MXN cost at the acquisition exchange rate)")
            break
    if not holding.lots or basis_mxn is None or to_mxn is None:
        if not holding.lots:
            ctx.missing.append(f"household.lots for {account.id}/{holding.instrument} (MXN average cost)")
        if to_mxn is None:
            ctx.missing.append(f"fx.{instrument.currency}/MXN")
        blocked.append({"account_id": account.id, "instrument_id": holding.instrument, "reason": "gain in MXN cannot be computed"})
        return []
    quantity = sum((Decimal(str(l["quantity"])) for l in holding.lots), ZERO)
    per_share = price * to_mxn - basis_mxn / quantity
    if regime == "unknown":
        ctx.missing.append(f"jurisdiction_context.instruments.{holding.instrument}.sic_listed (decides 10% vs progressive)")
        if per_share > 0:
            blocked.append({"account_id": account.id, "instrument_id": holding.instrument,
                            "reason": "SIC listing unknown: the gain might be progressive income"})
            return []
    if regime == "progressive" and per_share > 0 and ctx.avoid_progressive:
        blocked.append({"account_id": account.id, "instrument_id": holding.instrument,
                        "reason": "non-SIC security at a foreign broker: the gain would be progressive income (LISR Title IV Ch. IV); "
                                  "not realised (constraints.avoid_progressive_gains=false to allow)"})
        return []
    if per_share < 0:
        tier, rate = (0 if ctx.harvest_first else 2), ZERO
    elif regime == "article_129":
        tier, rate = 3, Decimal(MX_PARAMETERS["art129_rate"]["any"]["value"])
    else:
        tier, rate = 4, mx_rate
    price_mxn = price * to_mxn
    rank = (per_share / price_mxn) * (rate if rate is not None else ONE) + cost_rate
    return [_Unit(account.id, holding.instrument, holding.quantity, tier, rank, per_share, rate, regime)]


# ---------------------------------------------------------------------------
# Simulation
# ---------------------------------------------------------------------------

class _Sim:
    def __init__(self, ctx: _Context):
        self.ctx = ctx
        self.holdings = deepcopy(ctx.holdings)
        self.qty = {key: h.quantity for key, h in self.holdings.items()}
        self.lot_left = {lot["id"]: Decimal(str(lot["quantity"])) for h in self.holdings.values() for lot in h.lots}
        self.used_lots: list[tuple[str, Decimal]] = []
        self.cash = dict(ctx.cash)
        self.trades: list[dict[str, Any]] = []
        self.planned_buys: list[tuple[str, str]] = []
        self.loss_sold: set[str] = set()
        self.blocked: list[dict[str, Any]] = []
        self.skipped: list[dict[str, Any]] = []
        self.cost_unknown: set[str] = set()

    # -- valuation -------------------------------------------------------------
    def sleeve_values(self) -> dict[str, Decimal]:
        values = {s.name: ZERO for s in self.ctx.sleeves}
        for key, holding in self.holdings.items():
            values[holding.sleeve] += self.qty[key] * self.ctx.price_rep(holding.instrument)
        if self.ctx.cash_sleeve is not None:
            values[self.ctx.cash_sleeve.name] += sum(self.cash.values(), ZERO)
        return values

    def total(self) -> Decimal:
        invested = sum((self.qty[k] * self.ctx.price_rep(h.instrument) for k, h in self.holdings.items()), ZERO)
        return invested + sum(self.cash.values(), ZERO)

    def allocation(self) -> dict[str, Any]:
        total = self.total()
        values = self.sleeve_values()
        rows = {}
        for s in self.ctx.sleeves:
            weight = values[s.name] / total if total else ZERO
            rows[s.name] = {"value": _money(values[s.name]), "weight": _ratio(weight), "target": _ratio(s.weight),
                            "min": _ratio(s.low), "max": _ratio(s.high), "drift": _ratio(weight - s.weight),
                            "in_range": s.low <= weight <= s.high}
        uninvested = sum(self.cash.values(), ZERO) if self.ctx.cash_sleeve is None else ZERO
        return {"total": _money(total), "sleeves": rows, "uninvested_cash": _money(uninvested),
                "uninvested_cash_weight": _ratio(uninvested / total if total else ZERO)}

    def drift(self) -> dict[str, str | None]:
        total = self.total()
        values = self.sleeve_values()
        diffs = [(values[s.name] / total - s.weight) if total else ZERO for s in self.ctx.sleeves]
        outside = [s.name for s, d in zip(self.ctx.sleeves, diffs)
                   if not s.low <= s.weight + d <= s.high]
        return {"max_abs": _ratio(max((abs(d) for d in diffs), default=ZERO)),
                "total_abs_half": _ratio(sum((abs(d) for d in diffs), ZERO) / 2), "sleeves_out_of_range": outside}

    def out_of_range(self) -> bool:
        total = self.total()
        values = self.sleeve_values()
        return any(not s.low <= (values[s.name] / total if total else ZERO) <= s.high for s in self.ctx.sleeves)

    # -- trades ------------------------------------------------------------------
    def _cost(self, account: _Account, amount: Decimal) -> Decimal | None:
        if account.cost_rate is None:
            self.cost_unknown.add(account.id)
            return None
        return amount * account.cost_rate

    def _round(self, raw: Decimal, whole: bool) -> Decimal:
        """Round a quantity down: buys never exceed the budget, and sells never exceed the excess (rounding a
        sale up to a whole share could realise tax and strand proceeds the account cannot reinvest)."""
        if whole:
            return raw.to_integral_value(rounding=ROUND_FLOOR)
        return raw.quantize(_FRACTION_STEP, rounding=ROUND_FLOOR)

    def _below_minimum(self, account: _Account, amount: Decimal) -> str | None:
        """Why a trade of ``amount`` (reporting currency) is below a minimum, or None.

        ``constraints.min_trade_amount`` is in the reporting currency; an account's
        ``min_trade_amount`` is in that account's currency and is compared there.
        """
        ctx = self.ctx
        if amount < ctx.min_trade:
            return (f"{_money(amount)} {ctx.currency} is below constraints.min_trade_amount "
                    f"{_money(ctx.min_trade)} {ctx.currency}")
        if account.min_trade > 0:
            local = ctx.fx.convert(amount, ctx.currency, account.currency, f"minimum trade check for {account.id}")
            if local is None:
                ctx.missing.append(f"fx.{ctx.currency}/{account.currency} (account {account.id} minimum trade)")
                return f"no FX rate to compare with the account minimum in {account.currency}"
            if local < account.min_trade:
                return (f"{_money(local)} {account.currency} is below the account minimum trade "
                        f"{_money(account.min_trade)} {account.currency}")
        return None

    def buy(self, deficits: dict[str, Decimal], phase: str) -> None:
        ctx = self.ctx
        spendable = None
        if ctx.cash_sleeve is not None:
            spendable = sum(self.cash.values(), ZERO) - ctx.cash_sleeve.weight * self.total()
        pairs = []
        start = dict(deficits)
        for sleeve in ctx.sleeves:
            if deficits.get(sleeve.name, ZERO) <= 0 or sleeve is ctx.cash_sleeve:
                continue
            if not sleeve.buy:
                ctx.warnings.append(f"Sleeve {sleeve.name} is underweight but lists no buy instrument (targets.sleeves[].buy).")
                continue
            for account in sorted(ctx.accounts.values(), key=lambda a: a.id):
                if self.cash.get(account.id, ZERO) <= 0:
                    continue
                options = []
                for position, instrument_id in enumerate(sleeve.buy):
                    instrument = ctx.instruments.get(instrument_id)
                    if instrument is None or instrument.price is None:
                        ctx.missing.append(f"jurisdiction_context.instruments.{instrument_id}.price (buy candidate for {sleeve.name})")
                        continue
                    if ctx.price_rep(instrument_id) is None:
                        ctx.missing.append(f"fx.{instrument.currency}/{ctx.currency} (buy candidate {instrument_id} for {sleeve.name})")
                        continue
                    if not ctx.can_hold(account, instrument):
                        continue
                    if any(instrument_id in ctx.identical(sold) for sold in self.loss_sold):
                        ctx.warnings.append(f"{instrument_id} was not bought: it is identical to a security this plan sells at a loss (wash sale).")
                        continue
                    options.append((self._buy_preference(sleeve, account, instrument, position), instrument))
                if options:
                    preference, instrument = min(options, key=lambda o: o[0])
                    pairs.append((preference, -start[sleeve.name], sleeve.name, account.id, instrument))
        for _, _, sleeve_name, account_id, instrument in sorted(pairs, key=lambda p: p[:4]):
            need = deficits[sleeve_name]
            cash = self.cash.get(account_id, ZERO)
            if need <= 0 or cash <= 0:
                continue
            account = ctx.accounts[account_id]
            price = ctx.price_rep(instrument.id)
            cost_rate = account.cost_rate or ZERO
            budget = min(need, cash / (ONE + cost_rate))
            if spendable is not None:
                budget = min(budget, spendable)
            quantity = self._round(budget / price, ctx.whole_shares(account, instrument))
            amount = quantity * price
            if quantity <= 0:
                self.skipped.append({"account_id": account_id, "instrument_id": instrument.id, "side": "buy",
                                     "reason": f"budget {_money(budget)} {ctx.currency} is below one share ({_money(price)})"})
                continue
            below = self._below_minimum(account, amount)
            if below:
                self.skipped.append({"account_id": account_id, "instrument_id": instrument.id, "side": "buy", "reason": below})
                continue
            cost = self._cost(account, amount)
            self.cash[account_id] = cash - amount - (cost or ZERO)
            if spendable is not None:
                spendable -= amount + (cost or ZERO)
            deficits[sleeve_name] = need - amount
            key = (account_id, instrument.id)
            if key in self.qty:
                self.qty[key] += quantity
            else:
                self.holdings[key] = _Holding(account_id, instrument.id, ZERO, sleeve_name, [])
                self.qty[key] = quantity
            self.planned_buys.append(key)
            self._record(account, instrument, "buy", quantity, amount, cost, None, sleeve_name,
                         self._buy_reason(phase, sleeve_name, account, instrument), [])

    def _buy_preference(self, sleeve: _Sleeve, account: _Account, instrument: _Instrument, position: int) -> tuple:
        if self.ctx.jurisdiction == "US":
            order = {"bond": ("tax_deferred", "tax_exempt", "taxable"), "reit": ("tax_deferred", "tax_exempt", "taxable"),
                     "cash": ("tax_deferred", "tax_exempt", "taxable"),
                     "high_growth": ("tax_exempt", "taxable", "tax_deferred")}.get(sleeve.kind, ("taxable", "tax_exempt", "tax_deferred"))
            return (order.index(account.treatment) if account.treatment in order else 9, position)
        regime = _mx_regime(self.ctx, account, instrument)
        return ({"article_129": 0, "unknown": 1, "progressive": 2}[regime],
                {False: 0, None: 1, True: 2}[instrument.us_situs], account.cost_rate if account.cost_rate is not None else ONE, position)

    def _buy_reason(self, phase: str, sleeve: str, account: _Account, instrument: _Instrument) -> str:
        source = "new cash and planned contributions" if phase == "flows" else "sale proceeds and remaining cash"
        text = f"{sleeve} is below target; deploys {source}."
        if self.ctx.jurisdiction == "US":
            text += f" Placed in {account.type} ({account.treatment}) by asset-location preference and where cash is."
        else:
            regime = _mx_regime(self.ctx, account, instrument)
            text += {"article_129": " SIC/BMV-listed: future gains 10% (Art. 129).",
                     "progressive": " Not SIC-listed: future gains are progressive income.",
                     "unknown": " SIC listing unknown."}[regime]
            if instrument.us_situs is False:
                text += " Not US-situs for US estate tax."
            elif instrument.us_situs:
                text += " US-situs for US estate tax."
            if account.broker_kind == "mx_broker":
                text += " Mexican broker issues the constancia."
        if self.ctx.whole_shares(account, instrument):
            text += " Whole shares."
        return text

    def sell(self, rates: dict[str, Decimal] | None, mx_rate: Decimal | None) -> None:
        ctx = self.ctx
        total = self.total()
        values = self.sleeve_values()
        for sleeve in sorted(ctx.sleeves, key=lambda s: s.name):
            goal = sleeve.weight if ctx.sell_to == "target" else sleeve.high
            excess = values[sleeve.name] - goal * total
            if excess <= 0 or sleeve is ctx.cash_sleeve:
                continue
            units: list[_Unit] = []
            for key, holding in sorted(self.holdings.items()):
                if holding.sleeve == sleeve.name and self.qty[key] > 0 and key not in self.planned_buys:
                    live = _Holding(holding.account, holding.instrument, self.qty[key], holding.sleeve,
                                    [self._live_lot(l) for l in holding.lots if self.lot_left[l["id"]] > 0])
                    units.extend(_units(ctx, live, self.planned_buys, rates, mx_rate, self.blocked))
            units.sort(key=lambda u: (u.tier, u.rank, u.account, (u.lot or {}).get("acquired_on", ""), (u.lot or {}).get("id", u.instrument)))
            grouped: dict[tuple[str, str], dict[str, Any]] = {}
            for unit in units:
                if excess <= 0:
                    break
                account = ctx.accounts[unit.account]
                instrument = ctx.instruments[unit.instrument]
                price = ctx.price_rep(unit.instrument)
                key = (unit.account, unit.instrument)
                available = min(unit.quantity, self.qty[key])
                quantity = min(self._round(excess / price, ctx.whole_shares(account, instrument)), available)
                if quantity <= 0:
                    continue
                amount = quantity * price
                below = None if key in grouped else self._below_minimum(account, amount)
                if below:
                    self.skipped.append({"account_id": unit.account, "instrument_id": unit.instrument, "side": "sell", "reason": below})
                    continue
                excess -= amount
                self.qty[key] -= quantity
                entry = grouped.setdefault(key, {"quantity": ZERO, "amount": ZERO, "units": [], "gain": ZERO, "tax": ZERO,
                                                 "tax_known": True, "regimes": set()})
                entry["quantity"] += quantity
                entry["amount"] += amount
                entry["regimes"].add(unit.regime)
                if unit.lot is not None:
                    self.lot_left[unit.lot["id"]] -= quantity
                elif ctx.jurisdiction == "MX" and account.treatment == "taxable":
                    for lot in self.holdings[key].lots:  # average cost relieves lots pro rata
                        self.lot_left[lot["id"]] -= self.lot_left[lot["id"]] * quantity / (self.qty[key] + quantity)
                gain = None if unit.gain_per_share is None else unit.gain_per_share * quantity
                if gain is not None:
                    entry["gain"] += gain
                    if gain < 0:
                        self.loss_sold.add(unit.instrument)
                    if unit.tax_rate is None:
                        entry["tax_known"] = False
                    else:
                        entry["tax"] += (max(gain, ZERO) if unit.regime == "article_129" else gain) * unit.tax_rate
                entry["units"].append((unit, quantity, gain))
            for key, entry in grouped.items():
                account = ctx.accounts[key[0]]
                instrument = ctx.instruments[key[1]]
                cost = self._cost(account, entry["amount"])
                self.cash[key[0]] = self.cash.get(key[0], ZERO) + entry["amount"] - (cost or ZERO)
                tax_ccy = "USD" if ctx.jurisdiction == "US" else "MXN"
                tax_value = entry["tax"] if entry["tax_known"] else None
                if account.treatment in _ADVANTAGED:
                    tax_value = ZERO
                tax_rep = None if tax_value is None else ctx.fx.convert(tax_value, tax_ccy, ctx.currency, "estimated tax")
                lots = [{"lot_id": u.lot["id"], "quantity": _qty(q), "acquired_on": u.lot["acquired_on"], "character": u.character,
                         "gain_or_loss": _money(g), "currency": tax_ccy} for u, q, g in entry["units"] if u.lot is not None]
                self.used_lots.extend((u.lot["id"], q) for u, q, _ in entry["units"] if u.lot is not None)
                self._record(account, instrument, "sell", entry["quantity"], entry["amount"], cost, tax_rep, sleeve.name,
                             self._sell_reason(sleeve, account, instrument, entry), lots,
                             gain=None if account.treatment in _ADVANTAGED else entry["gain"], tax_ccy=tax_ccy,
                             regime=sorted(entry["regimes"])[0])

    def _live_lot(self, lot: dict[str, Any]) -> dict[str, Any]:
        """The unsold remainder of a lot, with basis scaled to it."""
        left = self.lot_left[lot["id"]]
        factor = left / Decimal(str(lot["quantity"]))
        live = dict(lot, quantity=str(left), cost_basis=str(Decimal(str(lot["cost_basis"])) * factor))
        for key in ("cost_basis_mxn", "cost_basis_usd"):
            if lot.get(key) is not None:
                live[key] = str(Decimal(str(lot[key])) * factor)
        return live

    def _sell_reason(self, sleeve: _Sleeve, account: _Account, instrument: _Instrument, entry: dict[str, Any]) -> str:
        text = f"{sleeve.name} is above target after deploying cash and a sleeve is out of range. "
        regime = sorted(entry["regimes"])[0]
        if account.treatment in _ADVANTAGED:
            return text + f"Sold in {account.type} first: no tax on sales inside a {account.treatment.replace('_', '-')} account."
        if self.ctx.jurisdiction == "US":
            if entry["gain"] < 0:
                return text + "Taxable lots at a loss first (specific identification); no identical purchase within 30 days."
            return text + "Taxable lots chosen by tax cost: losses, then long-term, then short-term gains (specific identification)."
        text += {"article_129": "SIC/BMV-listed: gain taxed at 10% (Art. 129) wherever held",
                 "progressive": "Not SIC-listed at a foreign broker: gain is progressive income",
                 "unknown": "SIC listing unknown"}[regime]
        text += "; no constancia from a foreign broker, declare it in April." if account.broker_kind == "foreign_broker" else "."
        if account.cost_rate:
            text += f" Commission{' plus IVA' if account.vat_rate else ''} included in the cost ranking."
        return text

    def _record(self, account: _Account, instrument: _Instrument, side: str, quantity: Decimal, amount_rep: Decimal,
                cost_rep: Decimal | None, tax_rep: Decimal | None, sleeve: str, reason: str, lots: list[dict[str, Any]],
                *, gain: Decimal | None = None, tax_ccy: str | None = None, regime: str | None = None) -> None:
        local = quantity * instrument.price
        row = {
            "account_id": account.id, "instrument_id": instrument.id, "symbol": instrument.symbol, "side": side,
            "quantity": _qty(quantity), "price": _money(instrument.price), "price_currency": instrument.currency,
            "price_as_of": instrument.price_as_of, "price_source": instrument.price_source,
            "estimated_amount": {"currency": instrument.currency, "amount": _money(local)},
            "estimated_amount_reporting": {"currency": self.ctx.currency, "amount": _money(amount_rep)},
            "estimated_cost": None if cost_rep is None else {"currency": self.ctx.currency, "amount": _money(cost_rep)},
            "estimated_tax": None if side == "sell" and tax_rep is None else {"currency": self.ctx.currency, "amount": _money(tax_rep or ZERO)},
            "sleeve": sleeve, "whole_shares": self.ctx.whole_shares(account, instrument), "reason": reason,
        }
        if side == "sell":
            row["realized_gain_or_loss"] = None if gain is None else {"currency": tax_ccy, "amount": _money(gain)}
            row["tax_regime"] = regime
            if lots:
                row["lots"] = lots
        if cost_rep is None:
            row["cost_note"] = f"commission for account {account.id} unknown (jurisdiction_context.accounts.{account.id}.commission_rate)"
        self.trades.append(row)


def _run(ctx: _Context, allow_sells: bool, rates: dict[str, Decimal] | None, mx_rate: Decimal | None) -> _Sim:
    sim = _Sim(ctx)
    total = sim.total()
    values = sim.sleeve_values()
    sim.buy({s.name: s.weight * total - values[s.name] for s in ctx.sleeves}, "flows")
    if allow_sells and sim.out_of_range():
        sim.sell(rates, mx_rate)
        total = sim.total()
        values = sim.sleeve_values()
        sim.buy({s.name: s.weight * total - values[s.name] for s in ctx.sleeves}, "proceeds")
    sim.trades = _merge(sim.trades)
    return sim


def _merge(trades: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One row per account, instrument and side (flow buys and proceeds buys combine)."""
    merged: dict[tuple[str, str, str], dict[str, Any]] = {}
    for trade in trades:
        key = (trade["account_id"], trade["instrument_id"], trade["side"])
        if key not in merged:
            merged[key] = trade
            continue
        row = merged[key]
        row["quantity"] = _qty(Decimal(row["quantity"]) + Decimal(trade["quantity"]))
        for field_name in ("estimated_amount", "estimated_amount_reporting", "estimated_cost", "estimated_tax"):
            if row[field_name] is None or trade[field_name] is None:
                row[field_name] = None
            else:
                row[field_name] = {**row[field_name], "amount": _money(Decimal(row[field_name]["amount"]) + Decimal(trade[field_name]["amount"]))}
        if trade["reason"] != row["reason"]:
            row["reason"] = row["reason"] + " " + trade["reason"]
    return list(merged.values())


def _us_engine(ctx: _Context, sim: _Sim, model: Any) -> tuple[dict[str, Any] | None, Decimal | None]:
    """Re-check the planned taxable sales with the tax engine (wash sales, netting, brackets)."""
    lots_plan: dict[str, Decimal] = {}
    for lot_id, quantity in sim.used_lots:
        lots_plan[lot_id] = lots_plan.get(lot_id, ZERO) + quantity
    if not lots_plan:
        return None, ZERO
    accounts = {a["id"]: a for a in ctx.household["accounts"]}
    lots = tax_engine._build_lots(ctx.household, accounts)
    # The engine works in USD: non-USD taxable lots are restated in USD the same way the plan priced them
    # (see Context.usd_basis / usd_price), so they are netted with the rest instead of dropped.
    raw_lots = {lot["id"]: lot for lot in ctx.household["lots"]}
    for lot_id, lot in list(lots.items()):
        if lot.tax_treatment == "taxable" and lot.currency != "USD":
            basis = ctx.usd_basis(raw_lots[lot_id])
            if basis is not None:
                lots[lot_id] = dc_replace(lot, cost_basis=basis, currency="USD")
    lots_plan = {k: v for k, v in lots_plan.items() if lots[k].us_taxable}
    if not lots_plan:
        return None, ZERO
    prices = {}
    for lot_id in lots_plan:
        instrument = ctx.instruments[lots[lot_id].instrument_id]
        prices[instrument.id] = {"price": ctx.usd_price(instrument), "currency": "USD", "as_of": ctx.trade_date,
                                 "source": instrument.price_source or "plan price"}
    purchases, _ = tax_engine._wash_purchases({"purchases": ctx.tax_inputs.get("purchases") or []}, accounts, lots)
    for account_id, instrument_id in sim.planned_buys:
        account = ctx.accounts[account_id]
        purchases.append(tax_engine._Replacement(
            ref=f"planned:{account_id}:{instrument_id}", kind="planned_buy", account_id=account_id, account_type=account.type,
            tax_treatment=account.treatment, instrument_id=instrument_id, acquired=ctx.trade_date,
            quantity=Decimal("1e9"), related_party="household"))
    book = tax_engine._Book(lots, prices, ctx.trade_date, ctx.groups, purchases)
    outcome = tax_engine._realize(book, lots_plan, set())
    check = {"realized": {"short_term": _money(outcome.short_term), "long_term": _money(outcome.long_term)},
             "wash_sale_disallowed_loss": _money(outcome.disallowed),
             "permanently_disallowed_loss": _money(outcome.permanently_disallowed)}
    if outcome.disallowed:
        ctx.warnings.append("The tax engine found a wash sale in the final plan; review the disallowed loss before acting.")
    netting = _netting_model(ctx, model)
    total = None
    if netting is not None:
        estimate = netting.estimate(outcome.short_term, outcome.long_term)
        check["incremental_tax_estimate"] = estimate
        total = Decimal(estimate["incremental_tax"])
        _attribute_us_tax(ctx, sim, netting, book, lots_plan)
    return check, total


def _netting_model(ctx: _Context, model: Any) -> Any:
    """A tax model that nets the plan's ST/LT gains and losses (IRC 1222 netting, 1211(b) limit).

    With full-year facts the model is used as given.  With caller marginal rates but no
    us_tax_facts, the plan is netted on its own: no other realized results this year and the
    statutory capital-loss deduction limit (said so in the assumptions).
    """
    if model is None:
        return None
    if model.ready:
        return model
    if model.method != "marginal_rates":
        return None
    limit = tax_engine.us_params.capital_loss_limit(model.filing_status or "single")
    facts = {key: ZERO for key in tax_engine._FACT_KEYS}
    facts["ordinary_income_loss_deduction_available"] = limit
    ctx.assumptions.append(
        "Plan tax nets the plan's short- and long-term gains and losses against each other (US netting) at the supplied "
        f"marginal rates, assuming no other realized gains or losses this year and a {_money(limit)} USD capital-loss "
        "deduction limit; supply us_tax_facts for the full-year picture.")
    return dc_replace(model, facts=facts)


def _attribute_us_tax(ctx: _Context, sim: _Sim, netting: Any, book: Any, lots_plan: dict[str, Decimal]) -> None:
    """Per-trade tax = the marginal change in the netted plan tax as each sell is added in order.

    The per-trade figures therefore add up to the netted plan total.
    """
    cumulative: dict[str, Decimal] = {}
    previous = ZERO
    for trade in sim.trades:
        if trade["side"] != "sell" or trade.get("tax_regime") != "us_capital_gain":
            continue
        added = False
        for lot in trade.get("lots") or []:
            if lot["lot_id"] in lots_plan:
                cumulative[lot["lot_id"]] = cumulative.get(lot["lot_id"], ZERO) + Decimal(lot["quantity"])
                added = True
        if not added:
            continue
        outcome = tax_engine._realize(book, dict(cumulative), set())
        running = netting.incremental(outcome.short_term, outcome.long_term).quantize(_CENT)  # rows add up to the total
        marginal = running - previous
        previous = running
        converted = ctx.fx.convert(marginal, "USD", ctx.currency, "estimated tax")
        trade["estimated_tax"] = None if converted is None else {"currency": ctx.currency, "amount": _money(converted)}
        trade["tax_attribution"] = "marginal change in the netted plan tax when this sale is added"


def _summary(ctx: _Context, sim: _Sim, before: dict[str, Any], engine_total: Decimal | None, engine_used: bool) -> dict[str, Any]:
    costs = [t["estimated_cost"] for t in sim.trades]
    taxes = [t["estimated_tax"] for t in sim.trades if t["side"] == "sell"]
    cost_total = None if any(c is None for c in costs) else sum((Decimal(c["amount"]) for c in costs), ZERO)
    if engine_used and engine_total is not None:
        other = sum((Decimal(t["estimated_tax"]["amount"]) for t in sim.trades
                     if t["side"] == "sell" and t.get("tax_regime") != "us_capital_gain" and t["estimated_tax"]), ZERO)
        converted = ctx.fx.convert(engine_total, "USD", ctx.currency, "engine tax")
        tax_total = None if converted is None else converted + other
    else:
        tax_total = None if any(t is None for t in taxes) else sum((Decimal(t["amount"]) for t in taxes), ZERO)
    turnover = sum((Decimal(t["estimated_amount_reporting"]["amount"]) for t in sim.trades), ZERO)
    return {
        "currency": ctx.currency, "before": before, "after": sim.allocation(),
        "total_estimated_tax": _money(tax_total), "total_estimated_cost": _money(cost_total),
        "turnover": _money(turnover), "residual_drift": sim.drift(),
        "cash_left_by_account": {k: _money(v) for k, v in sorted(sim.cash.items()) if v},
        "trade_count": len(sim.trades),
    }


def _unwrap(holdings: Any, missing: list[str], warnings: list[str]) -> dict[str, Any] | None:
    if isinstance(holdings, dict) and "status" in holdings and isinstance(holdings.get("result"), dict):
        for item in holdings.get("missing") or []:
            key = item.get("key") if isinstance(item, dict) else str(item)
            # Only gaps that left a position or account out of the household matter to the plan;
            # lot-level gaps are re-checked per sale.
            if key.startswith(("prices.", "accounts[")):
                missing.append(key)
        warnings.extend(holdings.get("warnings") or [])
        holdings = holdings["result"].get("household")
    if isinstance(holdings, dict) and "household" in holdings and "accounts" not in holdings:
        holdings = holdings["household"]
    return holdings


def plan(holdings: Any, targets: dict[str, Any] | None, *, jurisdiction_context: dict[str, Any],
         cash_flows: list[dict[str, Any]] | None = None, tax_inputs: dict[str, Any] | None = None,
         constraints: dict[str, Any] | None = None) -> dict[str, Any]:
    """Trade list from the current allocation to ``targets``; see the module docstring.

    ``holdings``: a canonical household, ``{"household": ...}`` or a ``ledger`` household
    envelope.  ``targets``: ``{by: asset_class|underlying|instrument, band?, map?,
    sleeves: [{name, weight, min?, max?, band?, kind?, buy: [instrument_id]}]}``.
    ``jurisdiction_context``: ``{jurisdiction: US|MX, trade_date?, currency?,
    accounts: {id: {platform?, commission_rate?, vat_rate?, fractional?, min_trade_amount?, purpose?}},
    instruments: {id: {price, currency, sleeve?, venue?, sic_listed?, security_type?, issuer_domicile?}}}``.
    Weights, ``band``, ``min`` and ``max`` are shares of the portfolio in [0, 1] (0.05 = 5%).
    ``constraints.min_trade_amount`` is in the reporting currency; an account's
    ``min_trade_amount`` is in that account's currency.  A US lot in another currency may carry
    ``cost_basis_usd`` (basis at the acquisition-date rate); otherwise its gain is restated in USD
    at the trade-date FX rate (an approximation, stated in the assumptions).
    ``run`` defaults ``jurisdiction_context.jurisdiction`` from ``client.profile`` when it is omitted.
    """
    missing: list[str] = []
    warnings: list[str] = []
    assumptions: list[str] = []
    household = _unwrap(holdings, missing, warnings)
    if household is None or targets is None:
        return envelope("needs_input", {}, missing=[m for m, bad in (("household (or ledger with prices)", household is None),
                                                                        ("targets", targets is None)) if bad])
    checked = validate_household(household)
    warnings.extend(checked["warnings"])
    ctx = _Context(checked["household"], targets, jurisdiction_context, cash_flows, tax_inputs, constraints,
                   missing, warnings, assumptions)
    if not ctx.holdings and not ctx.cash:
        return envelope("needs_input", {"protected": ctx.protected, "outside_plan": ctx.outside},
                        missing=missing + ["holdings with prices inside the plan"], warnings=warnings, assumptions=assumptions)
    rates = model = mx_rate = None
    if ctx.jurisdiction == "US":
        rates, model = _us_rates(ctx)
        if model is not None:
            warnings.extend(model.warnings)
            assumptions.extend(model.assumptions)
    else:
        mx_rate, _ = _mx_progressive_rate(ctx)
    before = _Sim(ctx).allocation()
    full = _run(ctx, ctx.mode == "full", rates, mx_rate)
    no_sell = _run(ctx, False, rates, mx_rate)
    engine_check, engine_total = (None, None)
    if ctx.jurisdiction == "US":
        engine_check, engine_total = _us_engine(ctx, full, model)
        if any(t["side"] == "sell" and t["estimated_tax"] is None for t in full.trades):
            missing.append("tax_inputs.rates {ordinary, long_term} or filing_status + us_tax_facts + us_return_facts (tax per trade)")
            if model is not None:
                missing.extend(model.missing)
    elif any(t["side"] == "sell" and t["estimated_tax"] is None for t in full.trades):
        missing.append("tax_inputs.marginal_rate or taxable_income_before_mxn (progressive gains)")
    for sim in (full, no_sell):
        for account_id in sorted(sim.cost_unknown):
            item = f"jurisdiction_context.accounts.{account_id}.commission_rate"
            if item not in missing:
                missing.append(item)
    summary = _summary(ctx, full, before, engine_total, engine_check is not None and "incremental_tax_estimate" in (engine_check or {}))
    alt = _summary(ctx, no_sell, before, None, False)
    result = {
        "jurisdiction": ctx.jurisdiction, "trade_date": ctx.trade_date.isoformat(), "mode": ctx.mode,
        "trades": full.trades, "summary": summary,
        "alternatives": {"no_sell": {"description": "Deploy new cash and planned contributions only; nothing is sold.",
                                     "trades": no_sell.trades, "after": alt["after"], "residual_drift": alt["residual_drift"],
                                     "total_estimated_tax": "0.00", "total_estimated_cost": alt["total_estimated_cost"]}},
        "cash_flows_used": ctx.flows, "blocked": _dedupe(full.blocked), "skipped": full.skipped,
        "protected": ctx.protected, "outside_plan": ctx.outside,
        "tax_engine_check": engine_check, "execution_ready": False,
        "scope": "Deterministic plan from supplied prices and lots; not an order. Taxes are estimates, not filing positions.",
    }
    if ctx.jurisdiction == "US":
        sold_at_loss = sorted({t["instrument_id"] for t in full.trades if t["side"] == "sell"
                               and t.get("realized_gain_or_loss") and Decimal(t["realized_gain_or_loss"]["amount"]) < 0})
        if sold_at_loss:
            until = (ctx.trade_date + timedelta(days=31)).isoformat()
            warnings.append(f"Do not buy {', '.join(sold_at_loss)} or a substantially identical security in any household account "
                            f"(including IRAs) before {until}, or the harvested loss is disallowed.")
        assumptions.append("US sells: taxable loss lots first (when harvest_losses_first), then tax-advantaged accounts, then "
                           "long-term and short-term gain lots; lots by specific identification.")
        assumptions.append("A loss lot is not sold when an identical security was bought in the prior 30 days or is bought by this plan.")
    else:
        assumptions.append("Mexican gains use average MXN cost without the INPC update, so gains are overstated when an update applies.")
        assumptions.append("Art. 129 losses are shown at zero tax per trade; they can offset Art. 129 gains of the year and later "
                           "years, which this estimate does not net (conservative).")
        if any(ctx.instruments[t["instrument_id"]].security_type == "equity_etf" and ctx.accounts[t["account_id"]].broker_kind == "foreign_broker"
               and t.get("tax_regime") == "article_129" for t in full.trades if t["side"] == "sell"):
            warnings.append("A SIC-listed ETF sold through a foreign broker at 10% is contested (criterio 37/ISR/N names shares).")
    assumptions.append("Tax is not withheld from sale proceeds in the simulation; set the estimate aside.")
    assumptions.append("Sells happen only when a sleeve is outside its range after cash is deployed; then overweight sleeves go "
                       f"to their {'target' if ctx.sell_to == 'target' else 'band edge'}.")
    missing = list(dict.fromkeys(missing))
    status = "partial" if missing else "ready"
    sources = _SOURCES_US if ctx.jurisdiction == "US" else _SOURCES_MX
    return envelope(status, result, missing=missing, warnings=list(dict.fromkeys(warnings)), sources=sources,
                    assumptions=list(dict.fromkeys(assumptions)))


def _dedupe(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen, out = set(), []
    for row in rows:
        key = tuple(sorted(row.items()))
        if key not in seen:
            seen.add(key)
            out.append(row)
    return out


# ---------------------------------------------------------------------------
# Asset location
# ---------------------------------------------------------------------------

# Annual drag assumptions (fractions of value), stated in every result.
_US_YIELD = {"bond": ("0.035", "0.05"), "reit": ("0.03", "0.045"), "cash": ("0.03", "0.05"),
             "broad_equity": ("0.013", "0.02"), "intl_equity": ("0.02", "0.03"), "high_growth": ("0", "0.01"),
             "us_equity": ("0.013", "0.02"), "other": ("0.01", "0.03")}
_US_GROWTH = {"high_growth": 5, "intl_equity": 4, "us_equity": 3, "broad_equity": 3, "reit": 2, "other": 1, "bond": 0, "cash": 0}
_US_ORDINARY = ("0.22", "0.37")
_US_QUALIFIED = ("0.15", "0.238")


def _range(low: Decimal, high: Decimal, currency: str) -> dict[str, Any]:
    low, high = min(low, high), max(low, high)
    return {"currency": currency, "low": _money(low), "high": _money(high)}


def location(accounts: list[dict[str, Any]], sleeves: list[dict[str, Any]], jurisdiction: str, *,
             currency: str | None = None, assumptions: dict[str, Any] | None = None) -> dict[str, Any]:
    """Asset-location guidance: current placement vs suggested, with annual tax-drag difference ranges.

    ``accounts``: ``[{id, type, platform?, value?}]``; ``sleeves``: ``[{name, kind?, expected_return?,
    yield?, holdings: [{account_id, value, instrument_id?, sic_listed?, issuer_domicile?|us_situs?,
    distributing?}]}]``.  Never produces trades.
    """
    if not isinstance(accounts, list) or not accounts:
        return envelope("needs_input", {}, missing=["accounts"])
    if not isinstance(sleeves, list) or not sleeves:
        return envelope("needs_input", {}, missing=["sleeves"])
    jurisdiction = str(jurisdiction).upper()
    if jurisdiction not in {"US", "MX"}:
        raise ValueError("jurisdiction must be US or MX")
    currency = currency or ("USD" if jurisdiction == "US" else "MXN")
    overrides = _obj(assumptions, "assumptions")
    index: dict[str, dict[str, Any]] = {}
    for position, account in enumerate(accounts):
        if not isinstance(account, dict):
            raise ValueError(f"accounts[{position}] must be an object")
        account_id = tax_engine._text(account.get("id"), f"accounts[{position}].id")
        platform = account.get("platform")
        if platform is not None and platform not in PLATFORMS:
            raise ValueError(f"accounts[{position}].platform must be one of {', '.join(sorted(PLATFORMS))}")
        index[account_id] = {**account, "treatment": account_tax_treatment(account.get("type")),
                             "canonical": canonical_account_type(account.get("type")) or str(account.get("type")),
                             "broker_kind": account.get("broker_kind", PLATFORMS.get(platform, {}).get("broker_kind"))}
    rows = []
    for position, sleeve in enumerate(sleeves):
        if not isinstance(sleeve, dict):
            raise ValueError(f"sleeves[{position}] must be an object")
        name = tax_engine._text(sleeve.get("name"), f"sleeves[{position}].name")
        holdings = sleeve.get("holdings") or []
        if not isinstance(holdings, list):
            raise ValueError(f"sleeves[{position}].holdings must be a list")
        for h_index, holding in enumerate(holdings):
            if not isinstance(holding, dict) or holding.get("account_id") not in index:
                raise ValueError(f"sleeves[{position}].holdings[{h_index}].account_id must reference an account")
            holding["_value"] = _dec(holding.get("value"), f"sleeves[{position}].holdings[{h_index}].value")
        rows.append({"name": name, "kind": _kind(name, sleeve.get("kind")), "raw": sleeve, "holdings": holdings,
                     "value": sum((h["_value"] for h in holdings), ZERO)})
    if jurisdiction == "US":
        result, missing, warnings, stated = _location_us(index, rows, currency, overrides)
    else:
        result, missing, warnings, stated = _location_mx(index, rows, currency, overrides)
    result.update(jurisdiction=jurisdiction, currency=currency, execution_ready=False, forces_trades=False,
                  note="Guidance for where new money and future rebalancing trades go. Moving existing taxable holdings "
                       "can realise gains; price any move with the rebalance task first.")
    for holding in (h for row in rows for h in row["holdings"]):
        holding.pop("_value", None)
    return envelope("partial" if missing else "ready", result, missing=missing, warnings=warnings,
                    sources=_SOURCES_US_LOCATION if jurisdiction == "US" else _SOURCES_MX, assumptions=stated)


def _pair(overrides: dict[str, Any], key: str, default: tuple[str, str]) -> tuple[Decimal, Decimal]:
    value = overrides.get(key, default)
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise ValueError(f"assumptions.{key} must be [low, high]")
    return _dec(value[0], f"assumptions.{key}[0]"), _dec(value[1], f"assumptions.{key}[1]")


def _location_us(accounts: dict[str, dict[str, Any]], rows: list[dict[str, Any]], currency: str,
                 overrides: dict[str, Any]) -> tuple[dict[str, Any], list[str], list[str], list[str]]:
    missing: list[str] = []
    warnings: list[str] = []
    ordinary = _pair(overrides, "ordinary_rate", _US_ORDINARY)
    qualified = _pair(overrides, "qualified_rate", _US_QUALIFIED)
    buckets = ("taxable", "tax_deferred", "tax_exempt")
    capacity = {b: ZERO for b in buckets}
    held_in: dict[str, Decimal] = {}
    for row in rows:
        for h in row["holdings"]:
            held_in[h["account_id"]] = held_in.get(h["account_id"], ZERO) + h["_value"]
    for account_id, account in accounts.items():
        if account["treatment"] not in buckets:
            warnings.append(f"Account {account_id} ({account.get('type')}) is outside the taxable/tax-deferred/Roth buckets and is ignored.")
            continue
        value = account.get("value")
        capacity[account["treatment"]] += _dec(value, f"accounts.{account_id}.value") if value is not None else held_in.get(account_id, ZERO)

    def drag(row: dict[str, Any], end: int) -> Decimal:
        kind = row["kind"]
        if row["raw"].get("yield") is not None:
            yld = _dec(row["raw"]["yield"], f"sleeves.{row['name']}.yield")
        else:
            yld = Decimal(_US_YIELD.get(kind, _US_YIELD["other"])[end])
        rate = (ordinary if kind in _ORDINARY_KINDS else qualified)[end]
        return yld * rate

    current = {}
    for row in rows:
        placed = {b: ZERO for b in buckets}
        for h in row["holdings"]:
            treatment = accounts[h["account_id"]]["treatment"]
            if treatment in placed:
                placed[treatment] += h["_value"]
        current[row["name"]] = placed
    remaining = dict(capacity)
    suggested = {row["name"]: {b: ZERO for b in buckets} for row in rows}
    left = {row["name"]: row["value"] for row in rows}

    def fill(bucket: str, ordered: list[dict[str, Any]]) -> None:
        for row in ordered:
            take = min(left[row["name"]], remaining[bucket])
            if take > 0:
                suggested[row["name"]][bucket] += take
                left[row["name"]] -= take
                remaining[bucket] -= take

    by_drag = sorted(rows, key=lambda r: (-(drag(r, 0) + drag(r, 1)), r["name"]))
    fill("tax_deferred", [r for r in by_drag if r["kind"] in _ORDINARY_KINDS])

    def growth(row: dict[str, Any]) -> Decimal:
        if row["raw"].get("expected_return") is not None:
            return _dec(row["raw"]["expected_return"], f"sleeves.{row['name']}.expected_return", nonnegative=False)
        return Decimal(_US_GROWTH.get(row["kind"], 1)) / 100

    fill("tax_exempt", sorted(rows, key=lambda r: (-growth(r), r["name"])))
    fill("tax_deferred", by_drag)
    fill("taxable", sorted(rows, key=lambda r: (drag(r, 0) + drag(r, 1), r["name"])))
    if any(v > 0 for v in left.values()):
        warnings.append("Account capacity is smaller than the sleeves' total value; some value is not placed.")
    out_rows = []
    cur = [ZERO, ZERO]
    sug = [ZERO, ZERO]
    for row in rows:
        for end in (0, 1):
            cur[end] += current[row["name"]]["taxable"] * drag(row, end)
            sug[end] += suggested[row["name"]]["taxable"] * drag(row, end)
        preferred = {"bond": "tax_deferred", "reit": "tax_deferred", "cash": "tax_deferred",
                     "high_growth": "tax_exempt"}.get(row["kind"], "taxable")
        reason = {"tax_deferred": "Interest and REIT distributions are taxed as ordinary income each year; tax-deferred accounts shelter them.",
                  "tax_exempt": "Highest expected growth compounds tax-free in a Roth.",
                  "taxable": "Broad equity index funds are tax-efficient (qualified dividends, few distributions, step-up and loss harvesting)."}[preferred]
        out_rows.append({"sleeve": row["name"], "kind": row["kind"], "value": _money(row["value"]),
                         "current": {b: _money(v) for b, v in current[row["name"]].items()},
                         "suggested": {b: _money(v) for b, v in suggested[row["name"]].items()},
                         "preferred_location": preferred, "reason": reason,
                         "annual_taxable_drag_rate": {"low": _ratio(drag(row, 0)), "high": _ratio(drag(row, 1))}})
    result = {
        "capacity": {b: _money(v) for b, v in capacity.items()}, "sleeves": out_rows,
        "estimated_annual_tax_drag": {"current": _range(cur[0], cur[1], currency), "suggested": _range(sug[0], sug[1], currency),
                                      "difference": _range(cur[0] - sug[0], cur[1] - sug[1], currency)},
    }
    stated = [
        "Annual drag counts only yearly taxable distributions in taxable accounts; yields by kind (unless sleeves[].yield): "
        + ", ".join(f"{k} {lo}-{hi}" for k, (lo, hi) in _US_YIELD.items()) + "; "
        f"ordinary rate {ordinary[0]}-{ordinary[1]} for bonds, REITs and cash; qualified rate {qualified[0]}-{qualified[1]} (incl. NIIT at the high end) for equity.",
        "Tax-deferred and Roth accounts have no annual drag; tax-deferred withdrawals are later taxed as ordinary income and Roth withdrawals are tax-free (not in the annual figure).",
        "REIT section 199A deduction, foreign tax credits, state tax and capital-gain distributions are not modelled.",
        "Placement fills tax-deferred space with ordinary-income sleeves, Roth space with the highest expected growth, then the rest.",
    ]
    return result, missing, warnings, stated


def _location_mx(accounts: dict[str, dict[str, Any]], rows: list[dict[str, Any]], currency: str,
                 overrides: dict[str, Any]) -> tuple[dict[str, Any], list[str], list[str], list[str]]:
    missing: list[str] = []
    warnings: list[str] = []
    marginal = _pair(overrides, "marginal_rate", ("0.20", "0.35"))
    equity_yield = _pair(overrides, "equity_yield", ("0.012", "0.018"))
    price_return = _pair(overrides, "price_return", ("0.04", "0.07"))
    ucits_wht = _dec(overrides.get("ucits_us_dividend_withholding", "0.15"), "assumptions.ucits_us_dividend_withholding")
    art129 = Decimal(MX_PARAMETERS["art129_rate"]["any"]["value"])
    additional = Decimal(MX_PARAMETERS["foreign_dividend_additional_rate"]["any"]["value"])
    out_rows = []
    cur = [ZERO, ZERO]
    sug = [ZERO, ZERO]
    us_situs_value = ZERO
    retirement = False
    for row in rows:
        kind = row["kind"]
        placements = []
        for h in row["holdings"]:
            account = accounts[h["account_id"]]
            value = h["_value"]
            broker = account["broker_kind"]
            if account["canonical"] in {"mx_ppr", "mx_afore"}:
                retirement = True
            situs = h.get("us_situs")
            if situs is None and h.get("issuer_domicile") is not None:
                situs = h["issuer_domicile"] == "US"
            sic = h.get("sic_listed")
            distributing = h.get("distributing")
            label = h.get("instrument_id", h["account_id"])
            if account["treatment"] == "taxable" and broker is None and account["canonical"] != "bank":
                missing.append(f"accounts.{h['account_id']}.platform (Mexican vs foreign broker decides constancia and regime)")
            if situs is None and kind in _EQUITY_KINDS:
                missing.append(f"sleeves.{row['name']}.holdings[{label}].issuer_domicile (US-situs estate exposure)")
            if situs:
                us_situs_value += value
            if kind in {"mx_fixed_income", "cash"}:
                regime = "interest"
            else:
                regime = "article_129" if (sic or (broker == "mx_broker" and kind in _EQUITY_KINDS)) else (
                    "progressive" if sic is False else "unknown")
            if regime == "unknown" and kind in _EQUITY_KINDS and account["treatment"] == "taxable":
                missing.append(f"sleeves.{row['name']}.holdings[{label}].sic_listed")
            low = high = ZERO
            if kind in _EQUITY_KINDS and account["treatment"] == "taxable":
                if distributing is False:
                    low, high = equity_yield[0] * ucits_wht, equity_yield[1] * ucits_wht
                else:
                    if distributing is None:
                        warnings.append(f"{label}: distribution policy unknown; treated as distributing.")
                    low = equity_yield[0] * (marginal[0] + additional)
                    high = equity_yield[1] * (marginal[1] + additional)
                if regime == "progressive":
                    high += price_return[1] * (marginal[1] - art129)
            cur[0] += value * low
            cur[1] += value * high
            if kind in _EQUITY_KINDS and account["treatment"] == "taxable":
                s_low, s_high = equity_yield[0] * ucits_wht, equity_yield[1] * ucits_wht
                target = "mx_broker_sic"
                why = ("Hold through a Mexican broker in the SIC (10% Art. 129, constancia issued) using an Irish-domiciled accumulating UCITS "
                       "for the same exposure (e.g. CSPX listed as CSPXN): not US-situs and no dividend event until sale.")
            elif kind in {"mx_fixed_income", "cash"}:
                s_low, s_high = low, high
                target = "mx_local"
                why = "Keep Mexican fixed income (CETES, BONOS, UDIBONOS) local: real-interest regime with retention by the Mexican intermediary."
            elif kind == "bond":
                s_low, s_high = low, high
                target = "keep"
                why = ("Foreign bond ETFs: a SIC-listed bond ETF through a foreign broker at 10% is doubtful (contested); "
                       "no change suggested without a contador's view.")
            else:
                s_low, s_high = low, high
                target = "keep"
                why = "No location change suggested."
            sug[0] += value * s_low
            sug[1] += value * s_high
            placements.append({"account_id": h["account_id"], "instrument_id": h.get("instrument_id"), "value": _money(value),
                               "broker_kind": broker, "gain_regime": regime, "us_situs": situs, "distributing": distributing,
                               "annual_drag": _range(value * low, value * high, currency),
                               "suggested_location": target, "suggested_annual_drag": _range(value * s_low, value * s_high, currency),
                               "reason": why})
        out_rows.append({"sleeve": row["name"], "kind": kind, "value": _money(row["value"]), "placements": placements})
    pointers = [{"task": "mx_deductions",
                 "why": "Voluntary PPR (Art. 151 fr. V) or Art. 185 contributions may be deductible; the saving depends on the global cap.",
                 "inputs_needed": ["tax_year", "total_income_mxn", "accumulable_income_mxn", "deductions", "proposed_ppr_contribution_mxn",
                                   "taxable_income_before_mxn or marginal_rate"]}]
    if us_situs_value > 0:
        pointers.append({"task": "estate", "why": f"{_money(us_situs_value)} {currency} is in US-situs securities (US-domiciled, "
                                                  "including through the SIC); US estate tax applies above US$60,000 for non-residents.",
                         "inputs_needed": ["year", "decedent", "assets"]})
    if retirement:
        warnings.append("PPR/AFORE balances are tax-deferred; their internal allocation has no annual drag here.")
    result = {"sleeves": out_rows, "us_situs_value": _money(us_situs_value), "related_tasks": pointers,
              "estimated_annual_tax_drag": {"current": _range(cur[0], cur[1], currency), "suggested": _range(sug[0], sug[1], currency),
                                            "difference": _range(cur[0] - sug[0], cur[1] - sug[1], currency)}}
    stated = [
        f"Equity dividend yield {equity_yield[0]}-{equity_yield[1]}; distributing US-domiciled funds bear the progressive marginal rate "
        f"{marginal[0]}-{marginal[1]} (US 10% withholding credited) plus the additional 10% (Art. 142 fr. V).",
        f"Irish accumulating UCITS bear about {ucits_wht} fund-level US withholding on US dividends and no Mexican dividend event.",
        f"Non-SIC securities at a foreign broker: the high end adds the gain-rate gap ({marginal[1]} vs {art129}) on a {price_return[1]} "
        "annual price return as if realised yearly; the low end assumes no sale (the gap is paid only on sale).",
        "The SIC listing decides the 10% regime, not the broker (criterio 37/ISR/N); SIC-listed ETFs through a foreign broker are contested.",
        f"Commission of 0.25% plus IVA on GBM Trading MX and the SIC premium are one-off costs of moving, not annual drag ({_MX_FACTS}).",
    ]
    return result, list(dict.fromkeys(missing)), list(dict.fromkeys(warnings)), stated


# ---------------------------------------------------------------------------
# Service entry point
# ---------------------------------------------------------------------------

_PLAN_KEYS = {"household", "ledger", "prices", "currency", "as_of", "default_owner_id", "complete", "targets",
              "jurisdiction_context", "cash_flows", "tax_inputs", "constraints"}
_LOCATION_KEYS = {"accounts", "sleeves", "jurisdiction", "currency", "assumptions"}


def run(task: str, inputs: dict[str, Any], context: dict[str, Any] | None = None) -> dict[str, Any]:
    """Service entry point for tasks ``rebalance`` and ``asset_location``."""
    context = context or {}
    if not isinstance(inputs, dict):
        raise ValueError("inputs must be an object")
    if task == "asset_location":
        unknown = set(inputs) - _LOCATION_KEYS
        if unknown:
            raise ValueError(f"asset_location inputs: unknown {sorted(unknown)}; expected {sorted(_LOCATION_KEYS)}")
        if "jurisdiction" not in inputs:
            return envelope("needs_input", {}, missing=["jurisdiction (US or MX)"])
        return location(inputs.get("accounts"), inputs.get("sleeves"), inputs["jurisdiction"],
                        currency=inputs.get("currency"), assumptions=inputs.get("assumptions"))
    if task != "rebalance":
        raise ValueError("rebalance.run supports rebalance and asset_location")
    unknown = set(inputs) - _PLAN_KEYS
    if unknown:
        raise ValueError(f"rebalance inputs: unknown {sorted(unknown)}; expected {sorted(_PLAN_KEYS)}")
    jc = inputs.get("jurisdiction_context")
    if jc is not None and not isinstance(jc, dict):
        raise ValueError("jurisdiction_context must be an object")
    jc = dict(jc or {})
    defaulted: list[str] = []
    if not jc.get("jurisdiction"):
        jurisdiction, why = _profile_jurisdiction(context.get("client.profile"))
        if jurisdiction is None:
            return envelope("needs_input", {}, missing=["jurisdiction_context {jurisdiction: US|MX, ...}" + (f" ({why})" if why else "")])
        jc["jurisdiction"] = jurisdiction
        defaulted.append(f"Jurisdiction {jurisdiction} taken from the saved profile ({why}); pass "
                         "jurisdiction_context.jurisdiction to override.")
    inputs = {**inputs, "jurisdiction_context": jc}
    report = _run_plan(inputs, context)
    if defaulted:
        report.setdefault("assumptions", [])
        report["assumptions"] = defaulted + list(report["assumptions"])
    return report


def _profile_jurisdiction(profile: Any) -> tuple[str | None, str | None]:
    """US or MX from ``client.profile`` (stated tax residence first, then residence), with the reason.

    Returns ``(None, why)`` when the profile does not settle it: no residence, a residence outside
    US/MX, tax residence in both, or a US person resident in Mexico (both regimes apply).
    """
    if not isinstance(profile, dict):
        return None, None
    from .situation.schema import country_code

    tax_residence = profile.get("tax_residence")
    if isinstance(tax_residence, str):
        tax_residence = [tax_residence]
    stated = [c for c in (country_code(x) for x in tax_residence or []) if c]
    residence = profile.get("residence")
    country = country_code(residence.get("country") if isinstance(residence, dict) else residence)
    if stated:
        known = sorted(set(stated) & {"US", "MX"})
        if len(known) != 1:
            return None, f"client.profile.tax_residence {stated} does not name exactly one of US or MX"
        jurisdiction, why = known[0], f"client.profile.tax_residence {stated}"
    elif country in {"US", "MX"}:
        jurisdiction, why = country, f"client.profile.residence {country}"
    else:
        return None, (f"client.profile.residence {country} is not US or MX" if country else None)
    if jurisdiction == "MX" and profile.get("us_person") is True:
        return None, "a US person resident in Mexico is taxed by both; choose the regime for this plan"
    return jurisdiction, why


def _run_plan(inputs: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    holdings = inputs.get("household")
    if holdings is None:
        ledger = inputs.get("ledger", context.get("ledger"))
        if ledger is not None and inputs.get("prices") is not None:
            from .ledger import price_table, to_household
            as_of = inputs.get("as_of") or inputs["jurisdiction_context"].get("trade_date")
            currency = inputs.get("currency") or inputs["jurisdiction_context"].get("currency")
            if not as_of or not currency:
                return envelope("needs_input", {}, missing=[k for k, v in (("as_of", as_of), ("currency", currency)) if not v])
            holdings = to_household(ledger, as_of, currency, price_table(inputs["prices"]),
                                    default_owner_id=inputs.get("default_owner_id"), complete=bool(inputs.get("complete", False)))
        elif ledger is not None:
            return envelope("needs_input", {}, missing=["prices {instrument_id: [{date, price}]} to value the ledger holdings"])
        else:
            holdings = context.get("household")
    return plan(holdings, inputs.get("targets"), jurisdiction_context=inputs["jurisdiction_context"],
                cash_flows=inputs.get("cash_flows"), tax_inputs=inputs.get("tax_inputs"), constraints=inputs.get("constraints"))


__all__ = ["PLATFORMS", "location", "plan", "run"]
