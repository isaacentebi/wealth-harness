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


TASKS = ("research", "value")

TASK_SCHEMAS: dict[str, dict[str, Any]] = {
    "research": {
        "required": ["symbol"],
        "optional": {
            "as_of": "ISO date (defaults to current UTC date and is disclosed)",
            "entity_type": "company|fund (default company)",
            "instrument_id": "optional canonical instrument id used for household matching",
            "max_fund_age_days": "optional household look-through freshness limit, default 90",
            "live_fetch": "boolean, default false; opts in to Yahoo/yfinance",
            "sources": [{
                "id": "unique string", "title": "string", "url": "https URL",
                "as_of": "ISO date", "kind": "filing|issuer|market|fund|provider|other",
                "max_age_days": "optional nonnegative integer",
            }],
            "business_facts": [{"label": "string", "value": "JSON value", "source_ids": ["source id"]}],
            "fund_facts": [{"label": "string", "value": "JSON value", "source_ids": ["source id"]}],
            "statements": [{
                "period_end": "ISO date", "currency": "ISO-like 3-letter code",
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
        "notes": "DCF forecast entries are {period, free_cash_flow}; rates are decimal annual effective rates and cash flows occur at period end. Enterprise-value multiples also require net_debt.",
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


def _statement_metrics(raw: Any, known: set[str], as_of: date) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    periods: list[dict[str, Any]] = []
    seen: set[date] = set()
    for index, item in enumerate(_list(raw, "statements")):
        statement = _mapping(item, f"statements[{index}]")
        period_end = _iso_date(statement.get("period_end"), f"statements[{index}].period_end")
        if period_end > as_of:
            raise ValueError(f"statements[{index}].period_end cannot be after packet as_of")
        if period_end in seen:
            raise ValueError(f"duplicate statement period {period_end.isoformat()}")
        seen.add(period_end)
        currency = _currency(statement.get("currency"), f"statements[{index}].currency")
        source_ids = _source_ids(statement.get("source_ids"), f"statements[{index}].source_ids", known)
        metric_input = _mapping(statement.get("metrics"), f"statements[{index}].metrics")
        metrics = {str(key): _number(value, f"statements[{index}].metrics.{key}") for key, value in metric_input.items()}
        periods.append({"period_end": period_end, "currency": currency, "source_ids": source_ids, "metrics": metrics})
    periods.sort(key=lambda item: item["period_end"], reverse=True)
    if len({item["currency"] for item in periods}) > 1:
        raise ValueError("statement periods require one currency; implicit FX is unsupported")

    calculated: dict[str, dict[str, Any]] = {}
    if not periods:
        return periods, calculated
    latest = periods[0]["metrics"]
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
        "return_on_assets": _ratio(net_income, assets),
        "return_on_equity": _ratio(net_income, equity),
        "current_ratio": _ratio(current_assets, current_liabilities),
    }.items():
        if value is not None:
            calculated[name] = {"value": value, "unit": "ratio", "period_end": periods[0]["period_end"].isoformat(), "source_ids": periods[0]["source_ids"]}
    if debt is not None and cash is not None:
        net_debt = debt - cash
        calculated["net_debt"] = {"value": _out(net_debt), "unit": periods[0]["currency"], "period_end": periods[0]["period_end"].isoformat(), "source_ids": periods[0]["source_ids"]}
        leverage = _ratio(net_debt, ebitda)
        if leverage is not None:
            calculated["net_debt_to_ebitda"] = {"value": leverage, "unit": "multiple", "period_end": periods[0]["period_end"].isoformat(), "source_ids": periods[0]["source_ids"]}
    if cfo is not None and capex is not None:
        calculated["free_cash_flow_proxy"] = {
            "value": _out(cfo - abs(capex)), "unit": periods[0]["currency"],
            "period_end": periods[0]["period_end"].isoformat(),
            "formula": "operating_cash_flow - abs(capital_expenditure)",
            "source_ids": periods[0]["source_ids"],
        }
    if len(periods) > 1:
        prior_revenue = _metric(periods[1]["metrics"], "revenue")
        if revenue is not None and prior_revenue not in {None, Decimal(0)}:
            calculated["revenue_growth"] = {
                "value": _out(revenue / prior_revenue - 1), "unit": "ratio",
                "period_end": periods[0]["period_end"].isoformat(),
                "comparison_period_end": periods[1]["period_end"].isoformat(),
                "source_ids": list(dict.fromkeys(periods[0]["source_ids"] + periods[1]["source_ids"])),
            }
    return periods, calculated


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
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Link direct and look-through household exposure to the researched asset."""
    household = context.get("household")
    if not isinstance(household, dict):
        return [], {
            "status": "unavailable", "lookthrough_complete": False,
            "warnings": ["No eligible household fact was available; household exposure is unknown."],
        }
    try:
        household_as_of = _iso_date(household.get("as_of"), "household.as_of")
    except ValueError:
        return [], {
            "status": "partial", "lookthrough_complete": False,
            "warnings": ["Household as_of is missing or invalid; fund look-through freshness is unknown."],
        }

    links: list[dict[str, Any]] = []
    warnings: list[str] = []
    complete = bool(household.get("complete", False))
    unknown_sections = household.get("unknown_sections", [])
    if not isinstance(unknown_sections, list):
        unknown_sections = ["unknown_sections"]
    if not complete:
        warnings.append("Household is marked incomplete; absence of a matching exposure is not conclusive.")
    if {"positions", "fund_holdings"} & set(unknown_sections):
        warnings.append("Household positions or fund holdings are unknown; look-through exposure may be missing.")

    target_ids = {symbol.upper()}
    if instrument_id is not None:
        target_ids.add(instrument_id.upper())

    def matches(item: dict[str, Any]) -> bool:
        return any(
            isinstance(item.get(field), str) and item[field].upper() in target_ids
            for field in ("instrument_id", "symbol")
        )

    fresh_funds: dict[str, dict[str, Any]] = {}
    stale_funds: set[str] = set()
    raw_funds = household.get("fund_holdings", [])
    if not isinstance(raw_funds, list):
        raw_funds = []
        warnings.append("Household fund_holdings is malformed; look-through exposure is unknown.")
    for index, fund in enumerate(raw_funds):
        if not isinstance(fund, dict) or not isinstance(fund.get("instrument_id"), str):
            warnings.append(f"Ignored malformed household fund holdings at index {index}.")
            continue
        fund_id = fund["instrument_id"]
        try:
            fund_as_of = _iso_date(fund.get("as_of"), f"household.fund_holdings[{index}].as_of")
        except ValueError:
            warnings.append(f"Fund holdings for {fund_id} have no valid as_of; look-through excluded.")
            stale_funds.add(fund_id)
            continue
        age = (household_as_of - fund_as_of).days
        if age < 0:
            warnings.append(f"Fund holdings for {fund_id} are after household.as_of; look-through excluded.")
            stale_funds.add(fund_id)
        elif age > max_fund_age_days:
            warnings.append(f"Fund holdings for {fund_id} are stale ({fund_as_of.isoformat()}); look-through excluded.")
            stale_funds.add(fund_id)
        elif not isinstance(fund.get("holdings"), list):
            warnings.append(f"Fund holdings for {fund_id} are malformed; look-through excluded.")
        else:
            fresh_funds[fund_id] = fund

    def walk(
        current_id: str,
        metadata: dict[str, Any],
        weight: Decimal,
        path: tuple[str, ...],
    ) -> list[tuple[Decimal, tuple[str, ...]]]:
        current_path = path + (current_id,)
        if matches(metadata):
            return [(weight, current_path)]
        if current_id in path:
            warnings.append(f"Fund holdings cycle at {' -> '.join(current_path)}; affected exposure is unknown.")
            return []
        if current_id in stale_funds:
            return []
        fund = fresh_funds.get(current_id)
        if fund is None:
            if str(metadata.get("asset_class", "")).lower() in {"fund", "etf", "mutual fund"}:
                warnings.append(f"No current fund holdings supplied for {current_id}; indirect exposure through it is unknown.")
            return []
        found: list[tuple[Decimal, tuple[str, ...]]] = []
        total = Decimal(0)
        for child_index, child in enumerate(fund["holdings"]):
            if not isinstance(child, dict) or not isinstance(child.get("instrument_id"), str):
                warnings.append(f"Ignored malformed constituent {child_index} in fund {current_id}; affected weight is unknown.")
                continue
            try:
                child_weight = _number(child.get("weight"), f"household fund {current_id} holding weight", nonnegative=True)
            except ValueError:
                warnings.append(f"Ignored invalid constituent weight in fund {current_id}; affected weight is unknown.")
                continue
            total += child_weight
            found.extend(walk(child["instrument_id"], child, weight * child_weight, current_path))
        if total < 1:
            warnings.append(f"Fund {current_id} has {_out(Decimal(1) - total)} residual unreported weight; exposure in that residual is unknown.")
        if total > 1:
            warnings.append(f"Fund {current_id} constituent weights exceed 1; look-through results may be unreliable.")
        return found

    accounts = {item.get("id"): item for item in household.get("accounts", []) if isinstance(item, dict)}
    positions = household.get("positions", [])
    if not isinstance(positions, list):
        positions = []
        warnings.append("Household positions is malformed; household exposure is unknown.")
    for position in positions:
        if not isinstance(position, dict):
            continue
        account = accounts.get(position.get("account_id"), {})
        position_instrument = position.get("instrument_id")
        if not isinstance(position_instrument, str):
            warnings.append(f"Position {position.get('id', 'unknown')} has no instrument_id; look-through excluded.")
            continue
        try:
            position_value = _number(position.get("value"), f"household position {position.get('id')} value", nonnegative=True)
        except ValueError:
            warnings.append(f"Position {position.get('id', 'unknown')} has no valid value; weighted exposure is unknown.")
            continue
        if matches(position):
            found = [(Decimal(1), (position_instrument,))]
        elif position_instrument in stale_funds:
            found = []
        else:
            looks_like_fund = str(position.get("asset_class", "")).lower() in {"fund", "etf", "mutual fund"}
            if looks_like_fund and position_instrument not in fresh_funds:
                warnings.append(f"No current fund holdings supplied for {position_instrument}; indirect exposure through it is unknown.")
                found = []
            else:
                found = walk(position_instrument, position, Decimal(1), ())
        for weight, path in found:
            direct = weight == 1 and len(path) == 1
            links.append({key: value for key, value in {
                "position_id": position.get("id"), "account_id": position.get("account_id"),
                "owner_id": account.get("owner_id"),
                "exposure_type": "direct" if direct else "fund_lookthrough",
                "path": list(path), "weight": _out(weight),
                "weighted_value": _out(position_value * weight), "currency": position.get("currency"),
                "quantity": position.get("quantity") if direct else None,
            }.items() if value is not None})
    deduped_warnings = list(dict.fromkeys(warnings))
    coverage_complete = complete and not deduped_warnings and not ({"positions", "fund_holdings"} & set(unknown_sections))
    return links, {
        "status": "complete" if coverage_complete else "partial",
        "lookthrough_complete": coverage_complete,
        "max_fund_age_days": max_fund_age_days,
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


def _fetch_yfinance(symbol: str, entity_type: str, as_of: date) -> dict[str, Any]:
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
        "as_of": as_of.isoformat(), "kind": "market", "provider": "Yahoo via yfinance",
        "max_age_days": 3,
    }
    business_facts = []
    for label, key in (("name", "longName"), ("quote_type", "quoteType"), ("sector", "sector"), ("industry", "industry"), ("website", "website"), ("market_cap", "marketCap"), ("enterprise_value", "enterpriseValue"), ("price", "currentPrice")):
        value = _safe_plain(info.get(key))
        if value is not None:
            business_facts.append({"label": label, "value": value, "source_ids": [source_id], "as_of": as_of.isoformat()})

    result: dict[str, Any] = {"sources": [source], "business_facts": business_facts}
    if entity_type == "fund":
        fund_facts: list[dict[str, Any]] = []
        holdings: list[dict[str, Any]] = []
        try:
            funds = ticker.get_funds_data()
            for label, attr in (("description", "description"), ("fund_overview", "fund_overview"), ("asset_classes", "asset_classes"), ("sector_weightings", "sector_weightings")):
                value = _safe_plain(getattr(funds, attr, None))
                if value is not None:
                    fund_facts.append({"label": label, "value": value, "source_ids": [source_id], "as_of": as_of.isoformat()})
            operations = getattr(funds, "fund_operations", None)
            if operations is not None and hasattr(operations, "columns") and len(operations.columns):
                fund_column = symbol if symbol in operations.columns else operations.columns[0]
                for operation_name in operations.index:
                    value = _safe_plain(operations.loc[operation_name, fund_column])
                    if value is not None:
                        fund_facts.append({"label": str(operation_name), "value": value, "source_ids": [source_id], "as_of": as_of.isoformat()})
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
            statements.append({"period_end": period_date.isoformat(), "currency": currency, "source_ids": [source_id], "metrics": metrics})
    result["statements"] = statements
    return result


def _merge_live(inputs: dict[str, Any], packet: dict[str, Any], symbol: str, entity_type: str, as_of: date) -> dict[str, Any]:
    if not inputs.get("live_fetch", False):
        return dict(inputs)
    if inputs.get("live_fetch") is not True:
        raise ValueError("live_fetch must be a boolean")
    try:
        live = _fetch_yfinance(symbol, entity_type, as_of)
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
    household_links, household_coverage = _household_links(symbol, instrument_id, context, int(fund_age))
    merged = _merge_live(inputs, packet, symbol, entity_type, as_of)
    raw_sources = merged.get("sources")
    if raw_sources is None:
        packet["missing"].append(_missing("sources", "missing", "Provide dated source evidence or set live_fetch to true."))
        raw_sources = []
    known = _normalize_sources(raw_sources, as_of, packet)
    business = _normalize_facts(merged.get("business_facts", []), "business_facts", known, as_of)
    fund_facts = _normalize_facts(merged.get("fund_facts", []), "fund_facts", known, as_of)
    periods, metrics = _statement_metrics(merged.get("statements", []), known, as_of)

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
        "statement_periods": [{"period_end": item["period_end"].isoformat(), "currency": item["currency"], "source_ids": item["source_ids"], "metrics": {key: _out(value) for key, value in item["metrics"].items()}} for item in periods],
        "financial_metrics": metrics, "thesis_evidence": thesis,
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
    if "household" in context and household_coverage["status"] == "partial":
        packet["warnings"].extend(
            f"Household coverage: {warning}" for warning in household_coverage["warnings"]
        )
    packet["result"] = result
    if not packet["sources"] or not result.get("business_facts") and entity_type == "company" or not result.get("fund_facts") and entity_type == "fund":
        packet["status"] = "needs_input"
    elif packet["missing"] or ("household" in context and household_coverage["status"] == "partial"):
        packet["status"] = "partial"
    else:
        packet["status"] = "ready"
    return packet


def _dcf_scenario(raw: dict[str, Any], index: int, known: set[str]) -> dict[str, Any]:
    prefix = f"scenarios[{index}]"
    name = _text(raw.get("name"), f"{prefix}.name")
    currency = _currency(raw.get("currency"), f"{prefix}.currency")
    valuation_date = _iso_date(raw.get("valuation_date"), f"{prefix}.valuation_date")
    discount = _number(raw.get("discount_rate"), f"{prefix}.discount_rate")
    growth = _number(raw.get("terminal_growth"), f"{prefix}.terminal_growth")
    if discount <= growth:
        raise ValueError(f"{prefix}.discount_rate must exceed terminal_growth")
    if discount <= Decimal("-1") or growth <= Decimal("-1"):
        raise ValueError(f"{prefix} rates must exceed -1")
    net_debt = _number(raw.get("net_debt"), f"{prefix}.net_debt")
    shares = _number(raw.get("shares_outstanding"), f"{prefix}.shares_outstanding", positive=True)
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
    pv_forecast = sum((cash / ((Decimal(1) + discount) ** period) for period, cash in forecasts), Decimal(0))
    last_period, last_cash = forecasts[-1]
    terminal_value = last_cash * (Decimal(1) + growth) / (discount - growth)
    pv_terminal = terminal_value / ((Decimal(1) + discount) ** last_period)
    enterprise_value = pv_forecast + pv_terminal
    equity_value = enterprise_value - net_debt
    per_share = equity_value / shares
    result = {
        "name": name, "kind": "dcf_fcff", "currency": currency, "valuation_date": valuation_date.isoformat(),
        "basis": "enterprise_value", "rate_convention": "decimal annual effective; end-of-period FCFF",
        "discount_rate": _out(discount), "terminal_growth": _out(growth),
        "forecast_present_value": _out(pv_forecast), "terminal_value_at_horizon": _out(terminal_value),
        "terminal_value_present_value": _out(pv_terminal), "enterprise_value": _out(enterprise_value),
        "net_debt": _out(net_debt), "equity_value": _out(equity_value),
        "shares_outstanding": _out(shares), "implied_value_per_share": _out(per_share),
        "source_ids": _source_ids(raw.get("source_ids"), f"{prefix}.source_ids", known),
        "interpretation": "scenario, not prediction",
    }
    if "current_price" in raw:
        current_price = _number(raw["current_price"], f"{prefix}.current_price", positive=True)
        result.update({"current_price": _out(current_price), "upside_downside": _out(per_share / current_price - 1)})
    return result


def _multiple_scenario(raw: dict[str, Any], index: int, known: set[str]) -> dict[str, Any]:
    prefix = f"scenarios[{index}]"
    name = _text(raw.get("name"), f"{prefix}.name")
    currency = _currency(raw.get("currency"), f"{prefix}.currency")
    valuation_date = _iso_date(raw.get("valuation_date"), f"{prefix}.valuation_date")
    basis = raw.get("basis")
    if basis not in {"enterprise_value", "equity_value"}:
        raise ValueError(f"{prefix}.basis must be enterprise_value or equity_value")
    metric_name = _text(raw.get("metric_name"), f"{prefix}.metric_name")
    metric_value = _number(raw.get("metric_value"), f"{prefix}.metric_value")
    multiple = _number(raw.get("multiple"), f"{prefix}.multiple", nonnegative=True)
    shares = _number(raw.get("shares_outstanding"), f"{prefix}.shares_outstanding", positive=True)
    reference_value = metric_value * multiple
    if basis == "enterprise_value":
        net_debt = _number(raw.get("net_debt"), f"{prefix}.net_debt")
        enterprise_value = reference_value
        equity_value = enterprise_value - net_debt
    else:
        if "net_debt" in raw:
            raise ValueError(f"{prefix}.net_debt is not used with an equity_value multiple")
        net_debt = None
        enterprise_value = None
        equity_value = reference_value
    per_share = equity_value / shares
    result = {
        "name": name, "kind": "multiples", "currency": currency, "valuation_date": valuation_date.isoformat(),
        "basis": basis, "metric_name": metric_name, "metric_value": _out(metric_value), "multiple": _out(multiple),
        "equity_value": _out(equity_value), "shares_outstanding": _out(shares),
        "implied_value_per_share": _out(per_share),
        "source_ids": _source_ids(raw.get("source_ids"), f"{prefix}.source_ids", known),
        "interpretation": "scenario, not prediction",
    }
    if enterprise_value is not None:
        result.update({"enterprise_value": _out(enterprise_value), "net_debt": _out(net_debt)})
    if "current_price" in raw:
        current_price = _number(raw["current_price"], f"{prefix}.current_price", positive=True)
        result.update({"current_price": _out(current_price), "upside_downside": _out(per_share / current_price - 1)})
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
            calculated = _dcf_scenario(scenario, index, known)
        elif kind == "multiples":
            calculated = _multiple_scenario(scenario, index, known)
        else:
            raise ValueError(f"scenarios[{index}].kind must be dcf_fcff or multiples")
        if calculated["name"] in names:
            raise ValueError(f"duplicate scenario name {calculated['name']}")
        names.add(calculated["name"])
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
