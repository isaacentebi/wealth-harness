"""Source-grounded public company and listed-fund research calculations.

The host owns prose synthesis and investment judgment.  This module only
normalizes supplied evidence (or an explicitly requested yfinance pull), links
it to remembered client context, and performs reproducible calculations.

Public tasks are ``research`` and ``value``.  See :data:`TASK_SCHEMAS` for the
small, JSON-compatible input contract accepted by :func:`run`.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
import math
from typing import Any
from urllib.parse import urlparse

from .household import (
    current_funds, fx_converter, lookthrough, normalize_symbol, ownership_shares, validate_household,
)


TASKS = ("research", "value")

TASK_SCHEMAS: dict[str, dict[str, Any]] = {
    "research": {
        "required": ["symbol"],
        "optional": {
            "as_of": "ISO date (defaults to current UTC date and is disclosed)",
            "entity_type": "company|fund (default company)",
            "instrument_id": "optional canonical instrument id used for household matching",
            "max_fund_age_days": "optional household look-through freshness limit, default 90",
            "max_fx_age_days": "optional household FX freshness limit for reporting-currency link totals, default 7",
            "live_fetch": "boolean, default false; opts in to Yahoo/yfinance",
            "sources": [{
                "id": "unique string", "title": "string", "url": "https URL",
                "as_of": "ISO date", "kind": "filing|issuer|market|fund|provider|other",
                "max_age_days": "optional nonnegative integer",
            }],
            "business_facts": [{"label": "string", "value": "JSON value", "source_ids": ["source id"]}],
            "fund_facts": [{"label": "string", "value": "JSON value", "source_ids": ["source id"]}],
            "statements": [{
                "period_end": "ISO date", "period_type": "FY|Q|TTM (required for growth, ROE/ROA and leverage)",
                "currency": "ISO-like 3-letter code",
                "source_ids": ["source id"], "metrics": "mapping of metric names to finite numbers",
            }],
            "fund_holdings": [{"symbol": "string", "name": "optional", "weight": "decimal fraction", "source_ids": ["source id"]}],
            "thesis_evidence": {
                "bullish": [{"claim": "host-supplied text", "source_ids": ["source id"]}],
                "bearish": [{"claim": "host-supplied text", "source_ids": ["source id"]}],
                "disconfirming": [{"claim": "host-supplied text", "source_ids": ["source id"]}],
            },
        },
    },
    "value": {
        "required": ["symbol", "sources", "scenarios"],
        "scenarios": {
            "dcf_fcff": ["name", "currency", "valuation_date", "forecast", "discount_rate", "terminal_growth", "net_debt", "shares_outstanding", "source_ids"],
            "multiples": ["name", "currency", "valuation_date", "basis", "metric_name", "metric_value", "multiple", "shares_outstanding", "source_ids"],
        },
        "optional": {
            "dcf_fcff": ["first_period_end + stub_fcf_basis (full_period|remaining_stub)", "mid_year_convention"],
            "enterprise bridge": ["minority_interest", "preferred_equity", "lease_liabilities or leases_included_in_net_debt=true", "non_operating_assets"],
            "dilution": ["options: [{count, strike}] (treasury stock method at the implied value)", "rsus"],
            "price": ["current_price with current_price_currency"],
        },
        "notes": "DCF forecast entries are {period, free_cash_flow}; rates are decimal annual effective rates; without first_period_end cash flows occur at whole-year period ends. Enterprise-value multiples also require net_debt. Non-positive multiple metrics are reported as not meaningful.",
    },
}

_SOURCE_DEFAULT_AGE = {
    "market": 3,
    "fund": 120,
    "provider": 120,
    "filing": 550,
    "issuer": 550,
    "other": 365,
}
_CURRENCY_LEN = 3
_HOUSEHOLD_MAX_AGE_DAYS = 31


def _today() -> date:
    return datetime.now(timezone.utc).date()


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a nonempty string")
    return value.strip()


def _iso_date(value: Any, field: str) -> date:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be an ISO date")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{field} must be an ISO date") from exc
    if parsed.isoformat() != value:
        raise ValueError(f"{field} must be an ISO date")
    return parsed


def _number(value: Any, field: str, *, nonnegative: bool = False, positive: bool = False) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise ValueError(f"{field} must be a finite number")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError(f"{field} must be a finite number") from exc
    if not result.is_finite():
        raise ValueError(f"{field} must be a finite number")
    if nonnegative and result < 0:
        raise ValueError(f"{field} must be nonnegative")
    if positive and result <= 0:
        raise ValueError(f"{field} must be positive")
    return result


def _out(value: Decimal) -> str:
    if value == 0:
        return "0"
    rendered = format(value.normalize(), "f")
    return rendered.rstrip("0").rstrip(".") if "." in rendered else rendered


def _currency(value: Any, field: str) -> str:
    result = _text(value, field)
    if len(result) != _CURRENCY_LEN or not result.isalpha() or result.upper() != result:
        raise ValueError(f"{field} must be three uppercase letters")
    return result


def _list(value: Any, field: str) -> list[Any]:
    if not isinstance(value, list):
        raise ValueError(f"{field} must be a list")
    return value


def _mapping(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{field} must be an object")
    return value


def _missing(key: str, reason: str, detail: str) -> dict[str, str]:
    return {"key": key, "reason": reason, "detail": detail}


def _base() -> dict[str, Any]:
    return {"status": "needs_input", "result": {}, "missing": [], "warnings": [], "sources": [], "assumptions": []}


def _source_ids(value: Any, field: str, known: set[str]) -> list[str]:
    values = _list(value, field)
    result: list[str] = []
    for index, item in enumerate(values):
        source_id = _text(item, f"{field}[{index}]")
        if source_id not in known:
            raise ValueError(f"{field}[{index}] refers to unknown source {source_id}")
        if source_id not in result:
            result.append(source_id)
    if not result:
        raise ValueError(f"{field} must contain at least one source id")
    return result


def _normalize_sources(raw: Any, as_of: date, packet: dict[str, Any]) -> set[str]:
    known: set[str] = set()
    for index, item in enumerate(_list(raw, "sources")):
        source = _mapping(item, f"sources[{index}]")
        source_id = _text(source.get("id"), f"sources[{index}].id")
        if source_id in known:
            raise ValueError(f"duplicate source id {source_id}")
        title = _text(source.get("title"), f"sources[{index}].title")
        url = _text(source.get("url"), f"sources[{index}].url")
        parsed_url = urlparse(url)
        if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
            raise ValueError(f"sources[{index}].url must be an http(s) URL")
        source_date = _iso_date(source.get("as_of"), f"sources[{index}].as_of")
        if source_date > as_of:
            raise ValueError(f"sources[{index}].as_of cannot be after as_of")
        kind = source.get("kind", "other")
        if kind not in _SOURCE_DEFAULT_AGE:
            raise ValueError(f"sources[{index}].kind is unsupported")
        max_age = source.get("max_age_days", _SOURCE_DEFAULT_AGE[kind])
        if not isinstance(max_age, int) or isinstance(max_age, bool) or max_age < 0:
            raise ValueError(f"sources[{index}].max_age_days must be a nonnegative integer")
        age = (as_of - source_date).days
        freshness = "stale" if age > max_age else "current"
        normalized = {
            "id": source_id,
            "title": title,
            "url": url,
            "as_of": source_date.isoformat(),
            "kind": kind,
            "provider": source.get("provider"),
            "freshness": freshness,
            "age_days": age,
        }
        packet["sources"].append(normalized)
        known.add(source_id)
        if freshness == "stale":
            packet["missing"].append(_missing(f"sources.{source_id}", "stale", f"{title} is {age} days old; maximum age is {max_age} days."))
    return known


def _normalize_facts(raw: Any, field: str, known: set[str], as_of: date) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for index, item in enumerate(_list(raw, field)):
        fact = _mapping(item, f"{field}[{index}]")
        normalized = {
            "label": _text(fact.get("label"), f"{field}[{index}].label"),
            "value": fact.get("value"),
            "source_ids": _source_ids(fact.get("source_ids"), f"{field}[{index}].source_ids", known),
        }
        if "as_of" in fact:
            fact_date = _iso_date(fact["as_of"], f"{field}[{index}].as_of")
            if fact_date > as_of:
                raise ValueError(f"{field}[{index}].as_of cannot be after packet as_of")
            normalized["as_of"] = fact_date.isoformat()
        result.append(normalized)
    return result


def _metric(metrics: dict[str, Decimal], *keys: str) -> Decimal | None:
    for key in keys:
        if key in metrics:
            return metrics[key]
    return None


def _ratio(numerator: Decimal | None, denominator: Decimal | None) -> str | None:
    if numerator is None or denominator in {None, Decimal(0)}:
        return None
    return _out(numerator / denominator)


_PERIOD_TYPES = ("FY", "TTM", "Q")
_PERIOD_PRIORITY = {"TTM": 0, "FY": 1, "Q": 2}
_ANNUALISATION = {"FY": (Decimal(1), "none (annual period)"), "TTM": (Decimal(1), "none (trailing twelve months)"), "Q": (Decimal(4), "quarterly flow x 4")}


def _statement_metrics(raw: Any, known: set[str], as_of: date) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]], list[str]]:
    """Normalise statements and calculate like-for-like metrics.

    ``period_type`` (FY, Q or TTM) states the length of the flow period.  Flow
    ratios against balances (ROE, ROA, net debt/EBITDA) are annualised from it
    and growth compares a period with the same type ending about one year
    earlier.  Without a period type those metrics are reported as gaps.
    """
    periods: list[dict[str, Any]] = []
    seen: set[tuple[date, str | None]] = set()
    for index, item in enumerate(_list(raw, "statements")):
        statement = _mapping(item, f"statements[{index}]")
        period_end = _iso_date(statement.get("period_end"), f"statements[{index}].period_end")
        if period_end > as_of:
            raise ValueError(f"statements[{index}].period_end cannot be after packet as_of")
        period_type = statement.get("period_type")
        if period_type is not None:
            period_type = _text(period_type, f"statements[{index}].period_type").upper()
            if period_type not in _PERIOD_TYPES:
                raise ValueError(f"statements[{index}].period_type must be FY, Q or TTM")
        key = (period_end, period_type)
        if key in seen:
            raise ValueError(f"duplicate statement period {period_end.isoformat()} {period_type or ''}".rstrip())
        seen.add(key)
        currency = _currency(statement.get("currency"), f"statements[{index}].currency")
        source_ids = _source_ids(statement.get("source_ids"), f"statements[{index}].source_ids", known)
        metric_input = _mapping(statement.get("metrics"), f"statements[{index}].metrics")
        metrics = {str(key): _number(value, f"statements[{index}].metrics.{key}") for key, value in metric_input.items()}
        periods.append({"period_end": period_end, "period_type": period_type, "currency": currency, "source_ids": source_ids, "metrics": metrics})
    periods.sort(key=lambda item: (-item["period_end"].toordinal(), _PERIOD_PRIORITY.get(item["period_type"], 9)))
    if len({item["currency"] for item in periods}) > 1:
        raise ValueError("statement periods require one currency; implicit FX is unsupported")

    calculated: dict[str, dict[str, Any]] = {}
    gaps: list[str] = []
    if not periods:
        return periods, calculated, gaps
    current = periods[0]
    latest = current["metrics"]
    period_type = current["period_type"]
    stamp = {"period_end": current["period_end"].isoformat(), "period_type": period_type, "source_ids": current["source_ids"]}
    revenue = _metric(latest, "revenue")
    gross_profit = _metric(latest, "gross_profit")
    operating_income = _metric(latest, "operating_income", "ebit")
    net_income = _metric(latest, "net_income")
    assets = _metric(latest, "total_assets")
    equity = _metric(latest, "shareholders_equity", "total_equity")
    cash = _metric(latest, "cash")
    debt = _metric(latest, "total_debt")
    ebitda = _metric(latest, "ebitda")
    current_assets = _metric(latest, "current_assets")
    current_liabilities = _metric(latest, "current_liabilities")
    cfo = _metric(latest, "operating_cash_flow")
    capex = _metric(latest, "capital_expenditure")
    for name, value in {
        "gross_margin": _ratio(gross_profit, revenue),
        "operating_margin": _ratio(operating_income, revenue),
        "net_margin": _ratio(net_income, revenue),
        "current_ratio": _ratio(current_assets, current_liabilities),
    }.items():
        if value is not None:
            calculated[name] = {"value": value, "unit": "ratio", **stamp}
    factor, annualisation = _ANNUALISATION.get(period_type, (None, None))
    for name, numerator, denominator in (("return_on_assets", net_income, assets), ("return_on_equity", net_income, equity)):
        if numerator is None or denominator in {None, Decimal(0)}:
            continue
        if factor is None:
            gaps.append(f"{name}: period_type is unknown, so the net income flow cannot be annualised.")
            continue
        calculated[name] = {"value": _out(numerator * factor / denominator), "unit": "annualised ratio", "annualisation": annualisation,
                            "denominator": "period-end balance", **stamp}
    if debt is not None and cash is not None:
        net_debt = debt - cash
        calculated["net_debt"] = {"value": _out(net_debt), "unit": current["currency"], **stamp}
        if ebitda not in {None, Decimal(0)}:
            if factor is None:
                gaps.append("net_debt_to_ebitda: period_type is unknown, so EBITDA cannot be annualised.")
            else:
                calculated["net_debt_to_ebitda"] = {"value": _out(net_debt / (ebitda * factor)), "unit": "multiple", "annualisation": annualisation, **stamp}
    if cfo is not None and capex is not None:
        calculated["free_cash_flow_proxy"] = {
            "value": _out(cfo - abs(capex)), "unit": current["currency"],
            "formula": "operating_cash_flow - abs(capital_expenditure)", **stamp,
        }
    if revenue is not None and len(periods) > 1:
        if period_type is None:
            gaps.append("revenue_growth: period_type is unknown, so no like-for-like comparison period can be chosen.")
        else:
            comparable = next((
                item for item in periods[1:]
                if item["period_type"] == period_type and 350 <= (current["period_end"] - item["period_end"]).days <= 380
                and _metric(item["metrics"], "revenue") not in {None, Decimal(0)}
            ), None)
            if comparable is None:
                gaps.append(f"revenue_growth: no {period_type} period ending about one year before {current['period_end'].isoformat()} was supplied.")
            else:
                calculated["revenue_growth"] = {
                    "value": _out(revenue / _metric(comparable["metrics"], "revenue") - 1), "unit": "ratio",
                    "comparison": f"year-over-year, like-for-like {period_type}",
                    **stamp,
                    "comparison_period_end": comparable["period_end"].isoformat(),
                    "source_ids": list(dict.fromkeys(current["source_ids"] + comparable["source_ids"])),
                }
    return periods, calculated, gaps


def _normalize_thesis(raw: Any, known: set[str]) -> dict[str, list[dict[str, Any]]]:
    thesis = _mapping(raw, "thesis_evidence")
    result: dict[str, list[dict[str, Any]]] = {}
    for side in ("bullish", "bearish", "disconfirming"):
        if side not in thesis:
            raise ValueError(f"thesis_evidence.{side} must be supplied; use an explicit empty list when none is known")
        entries: list[dict[str, Any]] = []
        for index, item in enumerate(_list(thesis[side], f"thesis_evidence.{side}")):
            evidence = _mapping(item, f"thesis_evidence.{side}[{index}]")
            entries.append({
                "claim": _text(evidence.get("claim"), f"thesis_evidence.{side}[{index}].claim"),
                "source_ids": _source_ids(evidence.get("source_ids"), f"thesis_evidence.{side}[{index}].source_ids", known),
            })
        result[side] = entries
    return result


def _household_links(
    symbol: str,
    instrument_id: str | None,
    context: dict[str, Any],
    max_fund_age_days: int,
    as_of: date,
    max_fx_age_days: int = 7,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Link direct and look-through household exposure to the researched asset.

    Uses the same look-through as :func:`wealth.household.lookthrough`:
    unreported fund weight, funds without current holdings and cycles are
    returned as explicit unknown exposure.  Freshness is judged at the research
    ``as_of`` and values are totalled in the household reporting currency.
    """
    household = context.get("household")
    if not isinstance(household, dict):
        return [], {
            "status": "unavailable", "lookthrough_complete": False,
            "warnings": ["No eligible household fact was available; household exposure is unknown."],
        }
    try:
        checked = validate_household(household)
    except ValueError as exc:
        return [], {
            "status": "unavailable", "lookthrough_complete": False,
            "warnings": [f"Household failed validation ({exc}); household exposure is unknown."],
        }
    data = checked["household"]
    warnings: list[str] = list(checked["warnings"])
    household_as_of = _iso_date(data["as_of"], "household.as_of")
    if household_as_of > as_of:
        return [], {
            "status": "unavailable", "lookthrough_complete": False,
            "warnings": [f"Household as_of {data['as_of']} is after the research as_of {as_of.isoformat()}; point-in-time exposure is unknown."],
        }
    household_age = (as_of - household_as_of).days
    if household_age > _HOUSEHOLD_MAX_AGE_DAYS:
        warnings.append(f"Household snapshot is stale ({household_age} days before {as_of.isoformat()}); exposure may have changed.")
    if not data["complete"]:
        warnings.append("Household is marked incomplete; absence of a matching exposure is not conclusive.")
    if {"positions", "fund_holdings"} & set(data["unknown_sections"]):
        warnings.append("Household positions or fund holdings are unknown; look-through exposure may be missing.")

    targets = {normalize_symbol(symbol)}
    if instrument_id is not None:
        targets |= {normalize_symbol(instrument_id), instrument_id.strip().upper()}
    targets.discard(None)

    def matches(identifier: str, metadata: dict[str, Any]) -> bool:
        return normalize_symbol(identifier) in targets or identifier.upper() in targets or normalize_symbol(metadata.get("symbol")) in targets

    fresh_funds, stale_funds = current_funds(data, as_of, max_fund_age_days, warnings)
    fx = fx_converter(data, as_of, max_fx_age_days, warnings)
    reporting = data["currency"]
    accounts = {item["id"]: item for item in data["accounts"]}
    links: list[dict[str, Any]] = []
    unknown: list[dict[str, Any]] = []
    totals = {"direct": Decimal(0), "fund_lookthrough": Decimal(0), "unknown_lookthrough": Decimal(0)}
    excluded_for_fx = 0
    for position in data["positions"]:
        account = accounts[position["account_id"]]
        position_value = _number(position["value"], f"household position {position['id']} value", nonnegative=True)
        reporting_value = fx.convert(position_value, position["currency"], reporting, f"position {position['id']} from link totals")
        if reporting_value is None:
            excluded_for_fx += 1
        shares, _ = ownership_shares(account)
        for leaf in lookthrough(position["instrument_id"], position, fresh_funds, opaque=stale_funds, stop=matches):
            weighted = position_value * leaf.weight
            weighted_reporting = reporting_value * leaf.weight if reporting_value is not None else None
            if leaf.kind == "match":
                direct = len(leaf.path) == 1
                if weighted_reporting is not None:
                    totals["direct" if direct else "fund_lookthrough"] += weighted_reporting
                links.append({key: value for key, value in {
                    "position_id": position["id"], "account_id": position["account_id"],
                    "owner_id": account["owner_id"],
                    "attribution": [{"person_id": person, "share": _out(share)} for person, share in shares],
                    "exposure_type": "direct" if direct else "fund_lookthrough",
                    "matched_on": "normalized symbol/instrument_id",
                    "path": list(leaf.path), "weight": _out(leaf.weight),
                    "weighted_value": _out(weighted), "currency": position["currency"],
                    "weighted_value_reporting": _out(weighted_reporting) if weighted_reporting is not None else None,
                    "quantity": position["quantity"] if direct else None,
                }.items() if value is not None})
            elif leaf.unknown:
                if weighted_reporting is not None:
                    totals["unknown_lookthrough"] += weighted_reporting
                if leaf.kind == "residual":
                    warnings.append(f"Fund {leaf.path[-1]} has {_out(leaf.weight)} residual unreported weight; exposure in that residual is unknown.")
                elif leaf.kind == "opaque_fund":
                    if leaf.instrument_id not in stale_funds:
                        warnings.append(f"No current fund holdings supplied for {leaf.instrument_id}; indirect exposure through it is unknown.")
                else:
                    warnings.append(f"Fund holdings cycle at {' -> '.join(leaf.path)}; affected exposure is unknown.")
                unknown.append({
                    "position_id": position["id"], "kind": leaf.kind, "path": list(leaf.path), "weight": _out(leaf.weight),
                    "value_reporting": _out(weighted_reporting) if weighted_reporting is not None else None,
                })
    deduped_warnings = list(dict.fromkeys(warnings))
    coverage_complete = data["complete"] and not deduped_warnings and not ({"positions", "fund_holdings"} & set(data["unknown_sections"]))
    return links, {
        "status": "complete" if coverage_complete else "partial",
        "lookthrough_complete": not unknown,
        "max_fund_age_days": max_fund_age_days,
        "evaluated_at": as_of.isoformat(),
        "reporting_currency": reporting,
        "totals_reporting": {
            "direct_value": _out(totals["direct"]),
            "fund_lookthrough_value": _out(totals["fund_lookthrough"]),
            "total_linked_value": _out(totals["direct"] + totals["fund_lookthrough"]),
            "unknown_lookthrough_value": _out(totals["unknown_lookthrough"]),
            "positions_excluded_for_fx": excluded_for_fx,
        },
        "unknown_exposure": unknown,
        "warnings": deduped_warnings,
    }


def _previous_case(symbol: str, context: dict[str, Any]) -> dict[str, Any] | None:
    candidate = context.get(f"research.{symbol}")
    if not isinstance(candidate, dict):
        return None
    if isinstance(candidate.get("result"), dict):
        return candidate["result"]
    return candidate


def _case_deltas(current: dict[str, dict[str, Any]], previous: dict[str, Any] | None) -> list[dict[str, str]]:
    if not previous:
        return []
    old_metrics = previous.get("financial_metrics")
    if not isinstance(old_metrics, dict):
        return []
    result: list[dict[str, str]] = []
    for name in sorted(set(current).intersection(old_metrics)):
        old = old_metrics[name]
        if not isinstance(old, dict) or "value" not in old:
            continue
        try:
            old_value = _number(old["value"], f"prior.{name}.value")
            new_value = _number(current[name]["value"], f"current.{name}.value")
        except ValueError:
            continue
        result.append({"metric": name, "prior": _out(old_value), "current": _out(new_value), "change": _out(new_value - old_value)})
    return result


def _fund_holdings(raw: Any, known: set[str]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    holdings: list[dict[str, Any]] = []
    for index, item in enumerate(_list(raw, "fund_holdings")):
        holding = _mapping(item, f"fund_holdings[{index}]")
        weight = _number(holding.get("weight"), f"fund_holdings[{index}].weight", nonnegative=True)
        if weight > 1:
            raise ValueError(f"fund_holdings[{index}].weight must be a decimal fraction no greater than 1")
        holdings.append({
            "symbol": _text(holding.get("symbol"), f"fund_holdings[{index}].symbol").upper(),
            "name": holding.get("name"), "weight": _out(weight),
            "source_ids": _source_ids(holding.get("source_ids"), f"fund_holdings[{index}].source_ids", known),
        })
    total = sum((_number(item["weight"], "holding.weight") for item in holdings), Decimal(0))
    if total > 1:
        raise ValueError("fund_holdings weights cannot sum to more than 1")
    top_ten = sum(sorted((_number(item["weight"], "holding.weight") for item in holdings), reverse=True)[:10], Decimal(0))
    metrics = {"reported_holdings_weight": _out(total), "top_10_concentration": _out(top_ten), "residual_unreported_weight": _out(Decimal(1) - total)}
    return holdings, metrics


def _safe_plain(value: Any) -> Any:
    """Convert common pandas/numpy values into finite JSON-friendly scalars."""
    if value is None:
        return None
    if hasattr(value, "item"):
        try:
            value = value.item()
        except (ValueError, TypeError):
            pass
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return None
    if isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(key): _safe_plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_safe_plain(item) for item in value]
    if hasattr(value, "to_dict"):
        try:
            return _safe_plain(value.to_dict())
        except (TypeError, ValueError):
            pass
    return str(value)


def _df_periods(frame: Any) -> list[Any]:
    columns = getattr(frame, "columns", [])
    try:
        return sorted(list(columns), reverse=True)
    except TypeError:
        return list(columns)


def _df_value(frame: Any, row_names: tuple[str, ...], period: Any) -> Any:
    index = getattr(frame, "index", [])
    for name in row_names:
        if name in index:
            try:
                return _safe_plain(frame.loc[name, period])
            except (KeyError, TypeError, ValueError):
                return None
    return None


def _fetch_yfinance(symbol: str, entity_type: str, retrieved_on: date) -> dict[str, Any]:
    """Fetch current Yahoo data, stamped with the date it was retrieved.

    Yahoo returns current data only, so the stamp is the retrieval date (and
    the quote's own market time for price), never a requested historical date.
    """
    try:
        import yfinance as yf  # type: ignore
    except ImportError as exc:
        raise RuntimeError("live_fetch requires the optional analytics dependency: install wealth-harness[analytics]") from exc

    ticker = yf.Ticker(symbol)
    info = dict(ticker.get_info() or {})
    source_id = "YF1"
    source = {
        "id": source_id,
        "title": f"Yahoo Finance aggregator data for {symbol}",
        "url": f"https://finance.yahoo.com/quote/{symbol}/",
        "as_of": retrieved_on.isoformat(), "kind": "market", "provider": "Yahoo via yfinance",
        "max_age_days": 3, "retrieved_on": retrieved_on.isoformat(),
    }
    market_time = info.get("regularMarketTime")
    price_date = retrieved_on
    if isinstance(market_time, (int, float)) and not isinstance(market_time, bool) and math.isfinite(market_time):
        price_date = min(retrieved_on, datetime.fromtimestamp(market_time, timezone.utc).date())
    business_facts = []
    for label, key in (("name", "longName"), ("quote_type", "quoteType"), ("sector", "sector"), ("industry", "industry"), ("website", "website"), ("market_cap", "marketCap"), ("enterprise_value", "enterpriseValue"), ("price", "currentPrice")):
        value = _safe_plain(info.get(key))
        if value is not None:
            fact_date = price_date if label == "price" else retrieved_on
            business_facts.append({"label": label, "value": value, "source_ids": [source_id], "as_of": fact_date.isoformat()})

    result: dict[str, Any] = {"sources": [source], "business_facts": business_facts}
    if entity_type == "fund":
        fund_facts: list[dict[str, Any]] = []
        holdings: list[dict[str, Any]] = []
        try:
            funds = ticker.get_funds_data()
            for label, attr in (("description", "description"), ("fund_overview", "fund_overview"), ("asset_classes", "asset_classes"), ("sector_weightings", "sector_weightings")):
                value = _safe_plain(getattr(funds, attr, None))
                if value is not None:
                    fund_facts.append({"label": label, "value": value, "source_ids": [source_id], "as_of": retrieved_on.isoformat()})
            operations = getattr(funds, "fund_operations", None)
            if operations is not None and hasattr(operations, "columns") and len(operations.columns):
                fund_column = symbol if symbol in operations.columns else operations.columns[0]
                for operation_name in operations.index:
                    value = _safe_plain(operations.loc[operation_name, fund_column])
                    if value is not None:
                        fund_facts.append({"label": str(operation_name), "value": value, "source_ids": [source_id], "as_of": retrieved_on.isoformat()})
            top = getattr(funds, "top_holdings", None)
            if top is not None and hasattr(top, "iterrows"):
                for holding_symbol, row in top.iterrows():
                    weight = _safe_plain(row.get("Holding Percent"))
                    if weight is not None:
                        holdings.append({"symbol": str(holding_symbol), "name": _safe_plain(row.get("Name")), "weight": weight, "source_ids": [source_id]})
        except Exception:  # Yahoo availability differs by security; packet records the gap.
            pass
        result.update({
            "fund_facts": fund_facts,
            "fund_holdings": holdings,
            "provider_gaps": [{
                "key": "fund_holdings.as_of", "reason": "unknown",
                "detail": "Yahoo/yfinance did not expose the holdings effective date; retrieval date is not treated as holdings date.",
            }] if holdings else [],
        })
        return result

    try:
        income = ticker.get_income_stmt(freq="yearly")
        balance = ticker.get_balance_sheet(freq="yearly")
        cashflow = ticker.get_cash_flow(freq="yearly")
    except Exception as exc:
        raise RuntimeError(f"Yahoo/yfinance financial statement fetch failed: {exc}") from exc
    periods = sorted(set(_df_periods(income) + _df_periods(balance) + _df_periods(cashflow)), reverse=True)[:4]
    currency = str(info.get("financialCurrency") or info.get("currency") or "").upper()
    statements: list[dict[str, Any]] = []
    mappings = {
        "revenue": (income, ("TotalRevenue",)), "gross_profit": (income, ("GrossProfit",)),
        "operating_income": (income, ("OperatingIncome", "EBIT")), "net_income": (income, ("NetIncome",)),
        "ebitda": (income, ("EBITDA", "NormalizedEBITDA")),
        "total_assets": (balance, ("TotalAssets",)), "total_liabilities": (balance, ("TotalLiabilitiesNetMinorityInterest",)),
        "shareholders_equity": (balance, ("StockholdersEquity",)),
        "cash": (balance, ("CashCashEquivalentsAndShortTermInvestments", "CashAndCashEquivalents")),
        "total_debt": (balance, ("TotalDebt",)), "current_assets": (balance, ("CurrentAssets",)),
        "current_liabilities": (balance, ("CurrentLiabilities",)),
        "operating_cash_flow": (cashflow, ("OperatingCashFlow", "TotalCashFromOperatingActivities")),
        "capital_expenditure": (cashflow, ("CapitalExpenditure", "CapitalExpenditures")),
    }
    for period in periods:
        period_date = getattr(period, "date", lambda: period)()
        if not isinstance(period_date, date) or len(currency) != 3:
            continue
        metrics = {name: value for name, (frame, rows) in mappings.items() if (value := _df_value(frame, rows, period)) is not None}
        if metrics:
            statements.append({"period_end": period_date.isoformat(), "period_type": "FY", "currency": currency, "source_ids": [source_id], "metrics": metrics})
    result["statements"] = statements
    return result


# -- fund holdings for automatic look-through (wealth.household exposure) ---------------------------

_LIVE_FUND_CACHE: dict[tuple[str, str], dict[str, Any] | None] = {}  # (symbol, retrieval date) -> fetched record
_ASSET_CLASS_KEYS = {"stockPosition": "equity", "bondPosition": "fixed_income", "cashPosition": "cash"}
_ASSET_CLASS_DOMINANT = Decimal("0.9")


def fund_asset_class(fund_facts: Any) -> str | None:
    """The fund's asset class from a dated fund fact: an explicit ``asset_class`` label, or an ``asset_classes``
    breakdown (Yahoo's ``stockPosition``/``bondPosition``/``cashPosition``) with one class at 90% or more."""
    for fact in fund_facts if isinstance(fund_facts, list) else []:
        if not isinstance(fact, dict):
            continue
        if fact.get("label") == "asset_class" and isinstance(fact.get("value"), str) and fact["value"].strip():
            return fact["value"].strip().lower()
        if fact.get("label") == "asset_classes" and isinstance(fact.get("value"), dict):
            for key, name in _ASSET_CLASS_KEYS.items():
                try:
                    share = Decimal(str(fact["value"].get(key)))
                except (InvalidOperation, TypeError, ValueError):
                    continue
                if share.is_finite() and share >= _ASSET_CLASS_DOMINANT:
                    return name
    return None


def _holdings_record(symbol: str, holdings: Any, fund_facts: Any, *, as_of: str | None, source: str,
                     origin: str, note: str | None = None) -> dict[str, Any] | None:
    rows = []
    for item in holdings if isinstance(holdings, list) else []:
        if not isinstance(item, dict) or not isinstance(item.get("symbol"), str):
            continue
        try:
            weight = Decimal(str(item.get("weight")))
        except (InvalidOperation, TypeError, ValueError):
            continue
        if not weight.is_finite() or weight <= 0 or weight > 1:
            continue
        child = normalize_symbol(item["symbol"]) or item["symbol"].upper()
        row = {"instrument_id": child, "symbol": child, "weight": format(weight, "f")}
        if isinstance(item.get("name"), str):
            row["issuer"] = item["name"][:80]
        rows.append(row)
    if not rows or sum(Decimal(r["weight"]) for r in rows) > 1:
        return None
    return {"instrument_id": symbol, "as_of": as_of, "source": source, "origin": origin, "holdings": rows,
            "asset_class": fund_asset_class(fund_facts), "note": note}


def fund_holdings_for(symbol: str, context: dict[str, Any] | None, *, live: bool = False,
                      today: date | None = None) -> tuple[dict[str, Any] | None, str]:
    """Holdings of fund ``symbol`` for look-through when the household supplies none.

    Order: a saved ``research.<SYMBOL>`` fund packet (offline, the cache), then, only when ``live`` (market data
    online), one Yahoo/yfinance pull per symbol per day, kept in memory.  Returns ``(record, why)``: a record
    ``{instrument_id, as_of, source, origin, holdings, asset_class, note}`` or ``None`` with the reason.  Yahoo
    exposes only the top holdings and no holdings date, so a live record's ``as_of`` is ``None`` (retrieval date in
    ``note``) and the rest of the fund stays an explicit unknown residual.
    """
    key = normalize_symbol(symbol) or str(symbol).upper()
    saved = (context or {}).get(f"research.{key}")
    result = saved.get("result") if isinstance(saved, dict) else None
    if isinstance(result, dict) and result.get("entity_type") == "fund" and result.get("fund_holdings"):
        packet_sources = [s for s in (saved.get("sources") or []) if isinstance(s, dict)]
        used = {sid for h in result["fund_holdings"] if isinstance(h, dict) for sid in h.get("source_ids") or []}
        dated = sorted(s["as_of"] for s in packet_sources if s.get("id") in used and s.get("as_of"))
        titles = sorted({s.get("title") for s in packet_sources if s.get("id") in used and s.get("title")})
        record = _holdings_record(key, result["fund_holdings"], result.get("fund_facts"),
                                  as_of=dated[0] if dated else result.get("as_of"),
                                  source="; ".join(titles) or f"saved research.{key}", origin="saved_research",
                                  note=None if dated else "holdings date unknown; the research packet date is used")
        if record is not None:
            return record, "saved research"
    if not live:
        return None, "no saved fund research and market data is offline"
    day = (today or _today()).isoformat()
    if (key, day) not in _LIVE_FUND_CACHE:
        try:
            fetched = _fetch_yfinance(key, "fund", date.fromisoformat(day))
        except Exception:  # noqa: BLE001 - provider trouble means unknown, never a crash
            fetched = None
        _LIVE_FUND_CACHE[(key, day)] = None if fetched is None else _holdings_record(
            key, fetched.get("fund_holdings"), fetched.get("fund_facts"), as_of=None,
            source=f"Yahoo Finance via yfinance, top holdings retrieved {day}", origin="live_yahoo",
            note=f"retrieved {day}; Yahoo gives no holdings date and only the top holdings")
    record = _LIVE_FUND_CACHE[(key, day)]
    return (record, "live") if record is not None else (None, "the provider returned no holdings")


def _merge_live(inputs: dict[str, Any], packet: dict[str, Any], symbol: str, entity_type: str, as_of: date) -> dict[str, Any]:
    if not inputs.get("live_fetch", False):
        return dict(inputs)
    if inputs.get("live_fetch") is not True:
        raise ValueError("live_fetch must be a boolean")
    retrieved_on = _today()
    if as_of < retrieved_on:
        packet["missing"].append(_missing(
            "live_fetch", "not_point_in_time",
            f"Yahoo/yfinance returns current data retrieved on {retrieved_on.isoformat()}; it cannot evidence a packet dated {as_of.isoformat()}. Supply dated sources instead.",
        ))
        return dict(inputs)
    try:
        live = _fetch_yfinance(symbol, entity_type, retrieved_on)
    except RuntimeError as exc:
        packet["missing"].append(_missing("live_fetch", "provider_unavailable", str(exc)))
        return dict(inputs)
    merged = dict(inputs)
    for key in ("sources", "business_facts", "fund_facts", "statements", "fund_holdings"):
        merged[key] = list(inputs.get(key, [])) + list(live.get(key, []))
    packet["missing"].extend(live.get("provider_gaps", []))
    packet["warnings"].append("Live values come from Yahoo Finance via the unofficial yfinance adapter; they are aggregator data, not primary filings.")
    return merged


def _research(inputs: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    packet = _base()
    symbol = _text(inputs.get("symbol"), "symbol").upper()
    entity_type = inputs.get("entity_type", "company")
    if entity_type not in {"company", "fund"}:
        raise ValueError("entity_type must be company or fund")
    as_of = _iso_date(inputs["as_of"], "as_of") if "as_of" in inputs else _today()
    if as_of > _today():
        raise ValueError("as_of cannot be in the future")
    if "as_of" not in inputs:
        packet["assumptions"].append(f"as_of defaulted to current UTC date {as_of.isoformat()}")
    instrument_id = _text(inputs["instrument_id"], "instrument_id") if "instrument_id" in inputs else None
    fund_age = _number(inputs.get("max_fund_age_days", 90), "max_fund_age_days", nonnegative=True)
    if fund_age != fund_age.to_integral_value():
        raise ValueError("max_fund_age_days must be an integer")
    fx_age = _number(inputs.get("max_fx_age_days", 7), "max_fx_age_days", nonnegative=True)
    if fx_age != fx_age.to_integral_value():
        raise ValueError("max_fx_age_days must be an integer")
    household_links, household_coverage = _household_links(symbol, instrument_id, context, int(fund_age), as_of, int(fx_age))
    merged = _merge_live(inputs, packet, symbol, entity_type, as_of)
    raw_sources = merged.get("sources")
    if raw_sources is None:
        packet["missing"].append(_missing("sources", "missing", "Provide dated source evidence or set live_fetch to true."))
        raw_sources = []
    known = _normalize_sources(raw_sources, as_of, packet)
    business = _normalize_facts(merged.get("business_facts", []), "business_facts", known, as_of)
    fund_facts = _normalize_facts(merged.get("fund_facts", []), "fund_facts", known, as_of)
    periods, metrics, metric_gaps = _statement_metrics(merged.get("statements", []), known, as_of)
    packet["warnings"].extend(metric_gaps)

    previous = _previous_case(symbol, context)
    thesis_raw = merged.get("thesis_evidence")
    remembered_thesis = context.get(f"thesis.{symbol}")
    if remembered_thesis is None and isinstance(previous, dict):
        remembered_thesis = previous.get("thesis_evidence")
    if thesis_raw is None:
        thesis = {"bullish": [], "bearish": [], "disconfirming": []}
        packet["missing"].append(_missing("thesis_evidence", "missing", "Provide current, source-linked bullish, bearish, and disconfirming evidence; remembered thesis is returned separately and narrative is never generated here."))
    else:
        thesis = _normalize_thesis(thesis_raw, known)

    result: dict[str, Any] = {
        "symbol": symbol, "entity_type": entity_type, "as_of": as_of.isoformat(),
        "business_facts": business, "fund_facts": fund_facts,
        "statement_periods": [{"period_end": item["period_end"].isoformat(), "period_type": item["period_type"], "currency": item["currency"], "source_ids": item["source_ids"], "metrics": {key: _out(value) for key, value in item["metrics"].items()}} for item in periods],
        "financial_metrics": metrics, "financial_metric_gaps": metric_gaps, "thesis_evidence": thesis,
        "remembered_thesis": remembered_thesis,
        "household_links": household_links,
        "household_link_coverage": household_coverage,
    }
    if entity_type == "company":
        if not business:
            packet["missing"].append(_missing("business_facts", "missing", "At least one source-grounded company fact is required for a complete packet."))
        if not periods:
            packet["missing"].append(_missing("statements", "missing", "Supply statement periods or enable live_fetch to calculate company financial metrics."))
    else:
        holdings, fund_metrics = _fund_holdings(merged.get("fund_holdings", []), known)
        result.update({"fund_holdings": holdings, "fund_metrics": fund_metrics})
        if not fund_facts:
            packet["missing"].append(_missing("fund_facts", "missing", "At least one dated source-grounded fund fact is required for a complete packet."))
        if not holdings:
            packet["missing"].append(_missing("fund_holdings", "missing", "Dated fund holdings are required to calculate concentration and residual coverage."))
    result["prior_case"] = {"available": previous is not None, "deltas": _case_deltas(metrics, previous)}
    if previous is None:
        packet["warnings"].append("No prior research case was available; this packet establishes the comparison baseline.")
    result["source_coverage"] = {
        "source_count": len(packet["sources"]),
        "current_source_count": sum(source["freshness"] == "current" for source in packet["sources"]),
        "stale_source_count": sum(source["freshness"] == "stale" for source in packet["sources"]),
    }
    if "household" in context and household_coverage["status"] != "complete":
        packet["warnings"].extend(
            f"Household coverage: {warning}" for warning in household_coverage["warnings"]
        )
    packet["result"] = result
    if not packet["sources"] or not result.get("business_facts") and entity_type == "company" or not result.get("fund_facts") and entity_type == "fund":
        packet["status"] = "needs_input"
    elif packet["missing"] or ("household" in context and household_coverage["status"] != "complete"):
        packet["status"] = "partial"
    else:
        packet["status"] = "ready"
    return packet


_BRIDGE_DEDUCTIONS = ("minority_interest", "preferred_equity", "lease_liabilities")
_TERMINAL_SHARE_WARNING = Decimal("0.75")


def _equity_bridge(raw: dict[str, Any], prefix: str, enterprise_value: Decimal) -> tuple[Decimal, dict[str, str], list[str]]:
    """Enterprise value to equity value with every claim explicit."""
    net_debt = _number(raw.get("net_debt"), f"{prefix}.net_debt")
    leases_in_net_debt = raw.get("leases_included_in_net_debt")
    if leases_in_net_debt is not None and not isinstance(leases_in_net_debt, bool):
        raise ValueError(f"{prefix}.leases_included_in_net_debt must be a boolean")
    if leases_in_net_debt is True and "lease_liabilities" in raw:
        raise ValueError(f"{prefix}.lease_liabilities would double count leases already included in net_debt")
    bridge = {"enterprise_value": _out(enterprise_value), "less_net_debt": _out(net_debt)}
    not_supplied: list[str] = []
    equity = enterprise_value - net_debt
    for key in _BRIDGE_DEDUCTIONS:
        if key == "lease_liabilities" and leases_in_net_debt is True:
            bridge["lease_liabilities"] = "included in net_debt"
            continue
        if key in raw:
            amount = _number(raw[key], f"{prefix}.{key}", nonnegative=True)
            equity -= amount
            bridge[f"less_{key}"] = _out(amount)
        else:
            not_supplied.append(key)
    if "non_operating_assets" in raw:
        amount = _number(raw["non_operating_assets"], f"{prefix}.non_operating_assets", nonnegative=True)
        equity += amount
        bridge["plus_non_operating_assets"] = _out(amount)
    else:
        not_supplied.append("non_operating_assets")
    bridge["equity_value"] = _out(equity)
    return equity, bridge, not_supplied


def _per_share(raw: dict[str, Any], prefix: str, equity: Decimal) -> tuple[dict[str, str], list[str]]:
    """Per-share value on diluted shares (treasury stock method at the implied value)."""
    basic = _number(raw.get("shares_outstanding"), f"{prefix}.shares_outstanding", positive=True)
    options = []
    for index, item in enumerate(_list(raw.get("options", []), f"{prefix}.options")):
        option = _mapping(item, f"{prefix}.options[{index}]")
        options.append((
            _number(option.get("count"), f"{prefix}.options[{index}].count", nonnegative=True),
            _number(option.get("strike"), f"{prefix}.options[{index}].strike", nonnegative=True),
        ))
    rsus = _number(raw.get("rsus", 0), f"{prefix}.rsus", nonnegative=True)
    not_supplied = [] if ("options" in raw or "rsus" in raw) else ["dilutive_securities"]
    diluted = basic
    method = "basic shares; no dilutive securities supplied" if not_supplied else "treasury stock method at the implied value per share"
    if equity > 0 and (options or rsus):
        price = equity / (basic + rsus)
        for _ in range(200):
            diluted = basic + rsus + sum((count * (1 - strike / price) for count, strike in options if strike < price), Decimal(0))
            updated = equity / diluted
            converged = abs(updated - price) <= abs(price) * Decimal("1e-12")
            price = updated
            if converged:
                break
    elif equity <= 0 and (options or rsus):
        diluted = basic + rsus
        method = "non-positive equity: options treated as out of the money; RSUs added"
    return {
        "shares_outstanding": _out(basic), "diluted_shares": _out(diluted), "dilution_method": method,
        "implied_value_per_share": _out(equity / diluted),
    }, not_supplied


def _price_comparison(raw: dict[str, Any], prefix: str, currency: str, per_share: Decimal, warnings: list[str]) -> dict[str, str]:
    if "current_price" not in raw:
        return {}
    current_price = _number(raw["current_price"], f"{prefix}.current_price", positive=True)
    result = {"current_price": _out(current_price)}
    price_currency = raw.get("current_price_currency")
    if price_currency is None:
        warnings.append(f"{prefix}: current_price_currency is not stated; upside/downside is not calculated (a listing currency can differ from the reporting currency).")
        return result
    price_currency = _currency(price_currency, f"{prefix}.current_price_currency")
    result["current_price_currency"] = price_currency
    if price_currency != currency:
        warnings.append(f"{prefix}: current_price is in {price_currency} but the scenario is in {currency}; no implicit FX is applied, so upside/downside is not calculated.")
        return result
    result["upside_downside"] = _out(per_share / current_price - 1)
    return result


def _discount_schedule(raw: dict[str, Any], prefix: str, valuation_date: date, periods: int) -> tuple[list[Decimal], Decimal, Decimal, dict[str, Any]]:
    """Return per-period discount exponents (years), the terminal exponent, the
    period-1 cash-flow scale, and a description of the timing convention."""
    mid_year = raw.get("mid_year_convention", False)
    if not isinstance(mid_year, bool):
        raise ValueError(f"{prefix}.mid_year_convention must be a boolean")
    stub = Decimal(1)
    first_scale = Decimal(1)
    timing: dict[str, Any] = {"mid_year_convention": mid_year}
    if raw.get("first_period_end") is not None:
        first_end = _iso_date(raw["first_period_end"], f"{prefix}.first_period_end")
        days = (first_end - valuation_date).days
        if not 0 < days <= 366:
            raise ValueError(f"{prefix}.first_period_end must fall within one year after valuation_date")
        basis = raw.get("stub_fcf_basis")
        if basis not in {"full_period", "remaining_stub"}:
            raise ValueError(f"{prefix}.stub_fcf_basis must say whether period 1 free_cash_flow is full_period (prorated here) or remaining_stub")
        stub = Decimal(days) / Decimal(365)
        first_scale = stub if basis == "full_period" else Decimal(1)
        timing.update(first_period_end=first_end.isoformat(), stub_fraction=_out(stub.quantize(Decimal("0.000001"))), stub_fcf_basis=basis)
    exponents = []
    for period in range(1, periods + 1):
        end = stub + (period - 1)
        start = end - (stub if period == 1 else Decimal(1))
        exponents.append((start + end) / 2 if mid_year else end)
    horizon = stub + (periods - 1)
    terminal = horizon - Decimal("0.5") if mid_year else horizon
    timing["convention"] = (
        "mid-period discounting; Gordon terminal value discounted half a year before the horizon because it capitalises flows received through each year"
        if mid_year else "end-of-period discounting"
    )
    return exponents, terminal, first_scale, timing


def _dcf_scenario(raw: dict[str, Any], index: int, known: set[str], warnings: list[str]) -> dict[str, Any]:
    prefix = f"scenarios[{index}]"
    name = _text(raw.get("name"), f"{prefix}.name")
    currency = _currency(raw.get("currency"), f"{prefix}.currency")
    valuation_date = _iso_date(raw.get("valuation_date"), f"{prefix}.valuation_date")
    discount = _number(raw.get("discount_rate"), f"{prefix}.discount_rate")
    growth = _number(raw.get("terminal_growth"), f"{prefix}.terminal_growth")
    if discount <= Decimal("-1") or growth <= Decimal("-1"):
        raise ValueError(f"{prefix} rates must exceed -1")
    if growth >= discount:
        raise ValueError(f"{prefix}.discount_rate must exceed terminal_growth; a perpetuity growing at or above the discount rate has no finite value")
    forecast_raw = _list(raw.get("forecast"), f"{prefix}.forecast")
    if not forecast_raw:
        raise ValueError(f"{prefix}.forecast must contain at least one period")
    forecasts: list[tuple[int, Decimal]] = []
    for f_index, item in enumerate(forecast_raw):
        entry = _mapping(item, f"{prefix}.forecast[{f_index}]")
        period = entry.get("period")
        if not isinstance(period, int) or isinstance(period, bool) or period <= 0:
            raise ValueError(f"{prefix}.forecast[{f_index}].period must be a positive integer")
        forecasts.append((period, _number(entry.get("free_cash_flow"), f"{prefix}.forecast[{f_index}].free_cash_flow")))
    forecasts.sort()
    if [period for period, _ in forecasts] != list(range(1, len(forecasts) + 1)):
        raise ValueError(f"{prefix}.forecast periods must be consecutive from 1")
    exponents, terminal_exponent, first_scale, timing = _discount_schedule(raw, prefix, valuation_date, len(forecasts))
    one_plus = Decimal(1) + discount
    pv_forecast = Decimal(0)
    for (period, cash), exponent in zip(forecasts, exponents):
        pv_forecast += (cash * first_scale if period == 1 else cash) / (one_plus ** exponent)
    last_cash = forecasts[-1][1]
    terminal_value = last_cash * (Decimal(1) + growth) / (discount - growth)
    pv_terminal = terminal_value / (one_plus ** terminal_exponent)
    enterprise_value = pv_forecast + pv_terminal
    equity_value, bridge, not_supplied = _equity_bridge(raw, prefix, enterprise_value)
    shares, dilution_gaps = _per_share(raw, prefix, equity_value)
    not_supplied.extend(dilution_gaps)
    terminal_share = pv_terminal / enterprise_value if enterprise_value > 0 else None
    if terminal_share is None:
        warnings.append(f"{prefix} ({name}): enterprise value is not positive; terminal-value share is undefined.")
    elif terminal_share > _TERMINAL_SHARE_WARNING:
        warnings.append(
            f"{prefix} ({name}): terminal value is {_out((terminal_share * 100).quantize(Decimal('0.1')))}% of enterprise value; "
            "the result is dominated by the perpetuity assumptions."
        )
    if discount - growth < Decimal("0.01"):
        warnings.append(f"{prefix} ({name}): discount rate exceeds terminal growth by less than one percentage point; value is highly sensitive to both.")
    if not_supplied:
        warnings.append(f"{prefix} ({name}): bridge items not supplied and therefore excluded: {', '.join(not_supplied)}.")
    result = {
        "name": name, "kind": "dcf_fcff", "status": "calculated", "currency": currency, "valuation_date": valuation_date.isoformat(),
        "basis": "enterprise_value", "rate_convention": "decimal annual effective", "timing": timing,
        "discount_rate": _out(discount), "terminal_growth": _out(growth),
        "forecast_present_value": _out(pv_forecast), "terminal_value_at_horizon": _out(terminal_value),
        "terminal_value_present_value": _out(pv_terminal),
        "terminal_value_share_of_enterprise_value": _out(terminal_share) if terminal_share is not None else None,
        "enterprise_value": _out(enterprise_value),
        "net_debt": bridge["less_net_debt"], "equity_bridge": bridge, "bridge_items_not_supplied": not_supplied,
        "equity_value": _out(equity_value), **shares,
        "source_ids": _source_ids(raw.get("source_ids"), f"{prefix}.source_ids", known),
        "interpretation": "scenario, not prediction",
    }
    result.update(_price_comparison(raw, prefix, currency, equity_value / Decimal(shares["diluted_shares"]), warnings))
    return result


def _multiple_scenario(raw: dict[str, Any], index: int, known: set[str], warnings: list[str]) -> dict[str, Any]:
    prefix = f"scenarios[{index}]"
    name = _text(raw.get("name"), f"{prefix}.name")
    currency = _currency(raw.get("currency"), f"{prefix}.currency")
    valuation_date = _iso_date(raw.get("valuation_date"), f"{prefix}.valuation_date")
    basis = raw.get("basis")
    if basis not in {"enterprise_value", "equity_value"}:
        raise ValueError(f"{prefix}.basis must be enterprise_value or equity_value")
    metric_name = _text(raw.get("metric_name"), f"{prefix}.metric_name")
    metric_value = _number(raw.get("metric_value"), f"{prefix}.metric_value")
    multiple = _number(raw.get("multiple"), f"{prefix}.multiple", positive=True)
    source_ids = _source_ids(raw.get("source_ids"), f"{prefix}.source_ids", known)
    base = {
        "name": name, "kind": "multiples", "currency": currency, "valuation_date": valuation_date.isoformat(),
        "basis": basis, "metric_name": metric_name, "metric_value": _out(metric_value), "multiple": _out(multiple),
        "source_ids": source_ids, "interpretation": "scenario, not prediction",
    }
    if metric_value <= 0:
        return {
            **base, "status": "not_meaningful", "equity_value": None, "implied_value_per_share": None,
            "reason": f"A multiple of a non-positive {metric_name} has no valuation meaning; use a different metric or an explicit cash-flow scenario.",
        }
    reference_value = metric_value * multiple
    if basis == "enterprise_value":
        equity_value, bridge, not_supplied = _equity_bridge(raw, prefix, reference_value)
        extra = {"enterprise_value": _out(reference_value), "net_debt": bridge["less_net_debt"], "equity_bridge": bridge}
    else:
        stray = [key for key in ("net_debt", *_BRIDGE_DEDUCTIONS, "non_operating_assets") if key in raw]
        if stray:
            raise ValueError(f"{prefix}.{stray[0]} is not used with an equity_value multiple")
        equity_value, not_supplied, extra = reference_value, [], {}
    shares, dilution_gaps = _per_share(raw, prefix, equity_value)
    not_supplied.extend(dilution_gaps)
    if not_supplied:
        warnings.append(f"{prefix} ({name}): bridge items not supplied and therefore excluded: {', '.join(not_supplied)}.")
    result = {**base, "status": "calculated", **extra, "bridge_items_not_supplied": not_supplied, "equity_value": _out(equity_value), **shares}
    result.update(_price_comparison(raw, prefix, currency, equity_value / Decimal(shares["diluted_shares"]), warnings))
    return result


def _value(inputs: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    del context
    packet = _base()
    symbol = _text(inputs.get("symbol"), "symbol").upper()
    as_of = _iso_date(inputs["as_of"], "as_of") if "as_of" in inputs else _today()
    if as_of > _today():
        raise ValueError("as_of cannot be in the future")
    if "as_of" not in inputs:
        packet["assumptions"].append(f"as_of defaulted to current UTC date {as_of.isoformat()}")
    if "sources" not in inputs:
        packet["missing"].append(_missing("sources", "missing", "Valuation inputs require dated sources."))
        return packet
    known = _normalize_sources(inputs["sources"], as_of, packet)
    raw_scenarios = inputs.get("scenarios")
    if raw_scenarios is None:
        packet["missing"].append(_missing("scenarios", "missing", "Provide at least one DCF or multiples scenario."))
        return packet
    scenarios: list[dict[str, Any]] = []
    names: set[str] = set()
    for index, item in enumerate(_list(raw_scenarios, "scenarios")):
        scenario = _mapping(item, f"scenarios[{index}]")
        kind = scenario.get("kind")
        if kind == "dcf_fcff":
            calculated = _dcf_scenario(scenario, index, known, packet["warnings"])
        elif kind == "multiples":
            calculated = _multiple_scenario(scenario, index, known, packet["warnings"])
        else:
            raise ValueError(f"scenarios[{index}].kind must be dcf_fcff or multiples")
        if calculated["name"] in names:
            raise ValueError(f"duplicate scenario name {calculated['name']}")
        names.add(calculated["name"])
        if calculated["valuation_date"] > as_of.isoformat():
            packet["warnings"].append(f"scenarios[{index}].valuation_date is after the packet as_of.")
        if calculated["status"] == "not_meaningful":
            packet["missing"].append(_missing(f"scenarios.{calculated['name']}", "not_meaningful", calculated["reason"]))
        scenarios.append(calculated)
    if not scenarios:
        packet["missing"].append(_missing("scenarios", "empty", "Provide at least one valuation scenario."))
        return packet
    packet["result"] = {"symbol": symbol, "as_of": as_of.isoformat(), "scenarios": scenarios, "scenario_disclaimer": "These are deterministic assumption cases, not forecasts or recommendations."}
    packet["assumptions"].append("DCF uses FCFF and an enterprise-value bridge; multiples preserve the stated enterprise or equity basis.")
    packet["status"] = "partial" if packet["missing"] else "ready"
    return packet


def run(task: str, inputs: dict[str, Any], context: dict[str, Any] | None = None) -> dict[str, Any]:
    """Run a source-grounded research or valuation calculation.

    Live network access is opt-in.  ``run('research', {'symbol': 'MSFT'}, {})``
    returns a precise ``needs_input`` packet; add supplied evidence or set
    ``live_fetch`` to ``True`` to use the optional Yahoo/yfinance adapter.
    """
    if task not in TASKS:
        raise ValueError(f"unsupported research task {task!r}; expected one of {', '.join(TASKS)}")
    if not isinstance(inputs, dict):
        raise ValueError("inputs must be an object")
    if context is None:
        context = {}
    if not isinstance(context, dict):
        raise ValueError("context must be an object")
    if "live_fetch" in inputs and not isinstance(inputs["live_fetch"], bool):
        raise ValueError("live_fetch must be a boolean")
    return _research(inputs, context) if task == "research" else _value(inputs, context)
