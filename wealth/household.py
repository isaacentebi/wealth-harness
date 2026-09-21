"""Canonical household imports and deterministic exposure calculations.

The module deliberately has no database or provider integration.  It turns an
explicit source into the canonical household document, or calculates exposure
from a supplied/current household.  Amounts are emitted as decimal strings so
that the result remains JSON safe and reproducible.
"""

from __future__ import annotations

import csv
from copy import deepcopy
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
import io
import json
from pathlib import Path
import re
from typing import Any


_CURRENCY = re.compile(r"[A-Z]{3}")
_ENTITIES = (
    "people", "accounts", "positions", "lots", "liabilities",
    "external_assets", "income_exposures", "fx", "fund_holdings",
)
def _text(value: Any, field: str, *, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a nonempty string")
    return value.strip()


def _currency(value: Any, field: str) -> str:
    value = _text(value, field)
    if _CURRENCY.fullmatch(value) is None:
        raise ValueError(f"{field} must be three uppercase letters")
    return value


def _number(value: Any, field: str, *, nonnegative: bool = False) -> Decimal:
    if isinstance(value, bool) or value is None or value == "":
        raise ValueError(f"{field} must be a finite number")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be a finite number") from exc
    if not result.is_finite() or (nonnegative and result < 0):
        qualifier = "nonnegative " if nonnegative else ""
        raise ValueError(f"{field} must be a {qualifier}finite number")
    return result


def _date(value: Any, field: str) -> date:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be an ISO date")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{field} must be an ISO date") from exc
    if parsed.isoformat() != value:
        raise ValueError(f"{field} must be an ISO date")
    return parsed


def _out(value: Decimal) -> str:
    if value == 0:
        return "0"
    rendered = format(value.normalize(), "f")
    return rendered.rstrip("0").rstrip(".") if "." in rendered else rendered


def _unique(items: list[dict[str, Any]], entity: str) -> set[str]:
    seen: set[str] = set()
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            raise ValueError(f"{entity}[{index}] must be an object")
        identifier = _text(item.get("id"), f"{entity}[{index}].id")
        if identifier in seen:
            raise ValueError(f"duplicate {entity} id: {identifier}")
        seen.add(identifier)
    return seen


def validate_household(household: Any) -> dict[str, Any]:
    """Validate and copy a canonical household, returning data plus scope warnings.

    Unknown external values and liabilities are allowed because unknown is not
    zero.  They are reported by the exposure calculator as excluded coverage.
    """
    if not isinstance(household, dict):
        raise ValueError("household must be an object")
    data = deepcopy(household)
    _currency(data.get("currency"), "household.currency")
    household_as_of = _date(data.get("as_of"), "household.as_of")
    if household_as_of > datetime.now(timezone.utc).date():
        raise ValueError("household.as_of cannot be in the future")
    if not isinstance(data.get("complete"), bool):
        raise ValueError("household.complete must be a boolean")

    supplied_unknown = data.get("unknown_sections")
    if supplied_unknown is not None and not (
        isinstance(supplied_unknown, list)
        and all(isinstance(item, str) and item in _ENTITIES for item in supplied_unknown)
        and len(set(supplied_unknown)) == len(supplied_unknown)
    ):
        raise ValueError("household.unknown_sections must contain unique canonical section names")
    unknown_sections = set(supplied_unknown or [])
    infer_unknown = supplied_unknown is None
    warnings: list[str] = []
    for entity in _ENTITIES:
        if entity not in data:
            data[entity] = []
            if infer_unknown:
                unknown_sections.add(entity)
        elif not isinstance(data[entity], list):
            raise ValueError(f"household.{entity} must be a list")
    data["unknown_sections"] = sorted(unknown_sections)
    for entity in data["unknown_sections"]:
        warnings.append(f"{entity} is unknown, not confirmed empty.")
    if not data["complete"]:
        warnings.append("Household is marked incomplete; totals cover known records only.")

    people = _unique(data["people"], "people")
    for index, person in enumerate(data["people"]):
        if "name" in person:
            _text(person["name"], f"people[{index}].name")
        if "tax_residencies" in person and not (
            isinstance(person["tax_residencies"], list)
            and all(isinstance(x, str) and x.strip() for x in person["tax_residencies"])
        ):
            raise ValueError(f"people[{index}].tax_residencies must be a list of strings")

    account_ids = _unique(data["accounts"], "accounts")
    for index, account in enumerate(data["accounts"]):
        owner = _text(account.get("owner_id"), f"accounts[{index}].owner_id")
        if owner not in people:
            raise ValueError(f"accounts[{index}].owner_id does not reference a person")
        _text(account.get("type"), f"accounts[{index}].type")
        _currency(account.get("currency"), f"accounts[{index}].currency")
        for field in ("liquid", "restricted"):
            if field in account and not isinstance(account[field], bool):
                raise ValueError(f"accounts[{index}].{field} must be a boolean")
        if "restrictions" in account and not (
            isinstance(account["restrictions"], list)
            and all(isinstance(value, str) and value.strip() for value in account["restrictions"])
        ):
            raise ValueError(f"accounts[{index}].restrictions must be a list of strings")
        if "tax_unit" in account:
            _text(account["tax_unit"], f"accounts[{index}].tax_unit")

    _unique(data["positions"], "positions")
    position_keys: set[tuple[str, str]] = set()
    for index, position in enumerate(data["positions"]):
        account = _text(position.get("account_id"), f"positions[{index}].account_id")
        if account not in account_ids:
            raise ValueError(f"positions[{index}].account_id does not reference an account")
        instrument = _text(position.get("instrument_id"), f"positions[{index}].instrument_id")
        key = (account, instrument)
        if key in position_keys:
            raise ValueError(f"duplicate position for account/instrument: {account}/{instrument}")
        position_keys.add(key)
        _text(position.get("symbol"), f"positions[{index}].symbol")
        _number(position.get("quantity"), f"positions[{index}].quantity", nonnegative=True)
        _number(position.get("value"), f"positions[{index}].value", nonnegative=True)
        _currency(position.get("currency"), f"positions[{index}].currency")
        for field in ("liquid", "restricted"):
            if field in position and not isinstance(position[field], bool):
                raise ValueError(f"positions[{index}].{field} must be a boolean")
        if "restrictions" in position and not (
            isinstance(position["restrictions"], list)
            and all(isinstance(value, str) and value.strip() for value in position["restrictions"])
        ):
            raise ValueError(f"positions[{index}].restrictions must be a list of strings")
        for field in ("asset_class", "issuer", "sector", "country", "economic_currency"):
            if field in position:
                (_currency if field == "economic_currency" else _text)(position[field], f"positions[{index}].{field}")

    _unique(data["lots"], "lots")
    lot_quantities: dict[tuple[str, str], Decimal] = {}
    for index, lot in enumerate(data["lots"]):
        account = _text(lot.get("account_id"), f"lots[{index}].account_id")
        instrument = _text(lot.get("instrument_id"), f"lots[{index}].instrument_id")
        key = (account, instrument)
        if account not in account_ids:
            raise ValueError(f"lots[{index}].account_id does not reference an account")
        if key not in position_keys:
            raise ValueError(f"lots[{index}] has no matching account/instrument position")
        quantity = _number(lot.get("quantity"), f"lots[{index}].quantity", nonnegative=True)
        lot_quantities[key] = lot_quantities.get(key, Decimal(0)) + quantity
        acquired_on = _date(lot.get("acquired_on"), f"lots[{index}].acquired_on")
        if acquired_on > household_as_of:
            raise ValueError(f"lots[{index}].acquired_on cannot be after household.as_of")
        _number(lot.get("cost_basis"), f"lots[{index}].cost_basis", nonnegative=True)
        _currency(lot.get("currency"), f"lots[{index}].currency")
    position_quantity = {
        (p["account_id"], p["instrument_id"]): _number(p["quantity"], "position.quantity")
        for p in data["positions"]
    }
    for key, quantity in lot_quantities.items():
        if quantity != position_quantity[key]:
            raise ValueError(f"lot quantities do not reconcile to position {key[0]}/{key[1]}")

    for entity in ("liabilities", "external_assets", "income_exposures"):
        _unique(data[entity], entity)
    for index, item in enumerate(data["liabilities"]):
        if item.get("value") not in (None, ""):
            _number(item["value"], f"liabilities[{index}].value", nonnegative=True)
        else:
            warnings.append(f"Liability {item['id']} has unknown value and is excluded from known NAV.")
        _currency(item.get("currency"), f"liabilities[{index}].currency")
        if "monthly_payment" in item:
            _number(item["monthly_payment"], f"liabilities[{index}].monthly_payment", nonnegative=True)
    for index, item in enumerate(data["external_assets"]):
        _text(item.get("name"), f"external_assets[{index}].name")
        if item.get("value") not in (None, ""):
            _number(item["value"], f"external_assets[{index}].value", nonnegative=True)
        else:
            warnings.append(f"External asset {item['id']} has unknown value and is excluded from known totals.")
        _currency(item.get("currency"), f"external_assets[{index}].currency")
        if not isinstance(item.get("liquid"), bool):
            raise ValueError(f"external_assets[{index}].liquid must be a boolean")
        for field in ("asset_class", "sector", "country"):
            if field in item:
                _text(item[field], f"external_assets[{index}].{field}")
    for index, item in enumerate(data["income_exposures"]):
        _text(item.get("description"), f"income_exposures[{index}].description")
        _currency(item.get("currency"), f"income_exposures[{index}].currency")
        for field in ("sector", "country"):
            if field in item:
                _text(item[field], f"income_exposures[{index}].{field}")
        if "annual_amount" in item:
            _number(item["annual_amount"], f"income_exposures[{index}].annual_amount", nonnegative=True)

    fx_keys: set[tuple[str, str]] = set()
    for index, item in enumerate(data["fx"]):
        if not isinstance(item, dict):
            raise ValueError(f"fx[{index}] must be an object")
        source, target = _currency(item.get("from"), f"fx[{index}].from"), _currency(item.get("to"), f"fx[{index}].to")
        if source == target or (source, target) in fx_keys:
            raise ValueError(f"duplicate or identity FX pair: {source}/{target}")
        fx_keys.add((source, target))
        if _number(item.get("rate"), f"fx[{index}].rate") <= 0:
            raise ValueError(f"fx[{index}].rate must be positive")
        fx_as_of = _date(item.get("as_of"), f"fx[{index}].as_of")
        if fx_as_of > household_as_of:
            raise ValueError(f"fx[{index}].as_of cannot be after household.as_of")
        _text(item.get("source"), f"fx[{index}].source")

    fund_ids: set[str] = set()
    for index, fund in enumerate(data["fund_holdings"]):
        if not isinstance(fund, dict):
            raise ValueError(f"fund_holdings[{index}] must be an object")
        instrument = _text(fund.get("instrument_id"), f"fund_holdings[{index}].instrument_id")
        if instrument in fund_ids:
            raise ValueError(f"duplicate fund holdings instrument_id: {instrument}")
        fund_ids.add(instrument)
        fund_as_of = _date(fund.get("as_of"), f"fund_holdings[{index}].as_of")
        if fund_as_of > household_as_of:
            raise ValueError(f"fund_holdings[{index}].as_of cannot be after household.as_of")
        _text(fund.get("source"), f"fund_holdings[{index}].source")
        holdings = fund.get("holdings")
        if not isinstance(holdings, list):
            raise ValueError(f"fund_holdings[{index}].holdings must be a list")
        total = Decimal(0)
        children: set[str] = set()
        for child_index, holding in enumerate(holdings):
            if not isinstance(holding, dict):
                raise ValueError(f"fund_holdings[{index}].holdings[{child_index}] must be an object")
            child = _text(holding.get("instrument_id"), f"fund_holdings[{index}].holdings[{child_index}].instrument_id")
            if child in children:
                raise ValueError(f"duplicate holding {child} in fund {instrument}")
            children.add(child)
            weight = _number(holding.get("weight"), f"fund_holdings[{index}].holdings[{child_index}].weight", nonnegative=True)
            total += weight
            for field in ("symbol", "issuer", "sector", "country", "asset_class"):
                if field in holding:
                    _text(holding[field], f"fund holding {child}.{field}")
            if "economic_currency" in holding:
                _currency(holding["economic_currency"], f"fund holding {child}.economic_currency")
        if total > 1:
            raise ValueError(f"fund holdings weights exceed 1 for {instrument}")

    return {"household": data, "warnings": warnings}


def _decode_cell(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    stripped = value.strip()
    if stripped == "":
        return None
    if stripped[:1] in "[{" or stripped in ("true", "false", "null"):
        try:
            return json.loads(stripped)
        except json.JSONDecodeError:
            pass
    return value


def _mapped_record(row: dict[str, Any], mapping: dict[str, Any], defaults: dict[str, Any]) -> dict[str, Any]:
    result = deepcopy(defaults)
    for canonical, source_column in mapping.items():
        if not isinstance(source_column, str):
            raise ValueError(f"mapping for {canonical} must name a source column")
        if source_column in row and row[source_column] not in (None, ""):
            result[canonical] = _decode_cell(row[source_column])
    return result


def _tabular_household(inputs: dict[str, Any], tables: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    mapping = inputs.get("mapping")
    defaults = inputs.get("defaults", {})
    if not isinstance(mapping, dict) or not isinstance(defaults, dict):
        raise ValueError("tabular import requires object mapping and defaults")
    metadata = defaults.get("household", {})
    if not isinstance(metadata, dict):
        raise ValueError("defaults.household must be an object")
    household = deepcopy(metadata)
    for entity in _ENTITIES:
        household[entity] = []

    if "rows" in tables:
        type_column = mapping.get("record_type")
        if not isinstance(type_column, str):
            raise ValueError("CSV mapping.record_type must name the record type column")
        grouped = {entity: [] for entity in _ENTITIES}
        for row_number, row in enumerate(tables["rows"], 2):
            entity = row.get(type_column)
            if entity not in grouped:
                raise ValueError(f"CSV row {row_number} has unknown record type: {entity}")
            grouped[entity].append(row)
        tables = grouped

    if "unknown_sections" not in household:
        household["unknown_sections"] = sorted(entity for entity in _ENTITIES if entity not in tables or not tables[entity])

    for entity in _ENTITIES:
        entity_mapping = mapping.get(entity, {})
        entity_defaults = defaults.get(entity, {})
        if not isinstance(entity_mapping, dict) or not isinstance(entity_defaults, dict):
            raise ValueError(f"mapping/defaults for {entity} must be objects")
        household[entity] = [
            _mapped_record(row, entity_mapping, entity_defaults) for row in tables.get(entity, [])
        ]
    return household


def _import_household(inputs: dict[str, Any]) -> tuple[dict[str, Any], list[str], list[str]]:
    if "household" in inputs:
        checked = validate_household(inputs["household"])
        return checked["household"], checked["warnings"], [str(inputs.get("source", "request.household"))]
    format_name = inputs.get("format")
    if format_name not in ("json", "csv", "xlsx"):
        raise ValueError("format must be json, csv, or xlsx")
    path = inputs.get("path")
    data = inputs.get("data")
    if (path is None) == (data is None):
        raise ValueError("provide exactly one of path or data")
    default_source = str(path) if path is not None else f"request.{format_name}"
    source = _text(inputs.get("source", default_source), "source")
    if format_name == "json":
        if path is not None:
            parsed = json.loads(Path(path).read_text(encoding="utf-8"))
        elif isinstance(data, str):
            parsed = json.loads(data)
        else:
            parsed = data
        checked = validate_household(parsed)
        return checked["household"], checked["warnings"], [source]
    if format_name == "csv":
        if path is not None:
            with Path(path).open(encoding="utf-8-sig", newline="") as handle:
                rows = list(csv.DictReader(handle))
        elif isinstance(data, str):
            rows = list(csv.DictReader(io.StringIO(data)))
        else:
            raise ValueError("CSV data must be text")
        imported = _tabular_household(inputs, {"rows": rows})
    else:
        try:
            from openpyxl import load_workbook
        except ImportError as exc:  # pragma: no cover - depends on optional extra
            raise ValueError("XLSX import requires the analytics extra (openpyxl)") from exc
        if path is not None:
            workbook = load_workbook(path, read_only=True, data_only=True)
        elif isinstance(data, (bytes, bytearray)):
            workbook = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        else:
            raise ValueError("XLSX data must be bytes")
        sheet_names = inputs.get("sheets", {})
        if not isinstance(sheet_names, dict):
            raise ValueError("sheets must be an object mapping entity to sheet name")
        tables: dict[str, list[dict[str, Any]]] = {}
        for entity in _ENTITIES:
            sheet_name = sheet_names.get(entity, entity)
            if sheet_name not in workbook.sheetnames:
                continue
            values = workbook[sheet_name].iter_rows(values_only=True)
            headers = next(values, None)
            if headers is None:
                tables[entity] = []
                continue
            tables[entity] = [dict(zip(headers, row)) for row in values]
        imported = _tabular_household(inputs, tables)
    checked = validate_household(imported)
    return checked["household"], checked["warnings"], [source]


def _fx_converter(household: dict[str, Any], as_of: date, max_age: int, warnings: list[str]):
    rates: dict[tuple[str, str], Decimal] = {}
    stale: set[tuple[str, str]] = set()
    for item in household["fx"]:
        key = (item["from"], item["to"])
        if (as_of - _date(item["as_of"], "fx.as_of")).days > max_age:
            stale.add(key)
            warnings.append(f"Excluded stale FX {key[0]}/{key[1]} dated {item['as_of']}.")
            continue
        rate = _number(item["rate"], "fx.rate")
        rates[key] = rate
        rates[(key[1], key[0])] = Decimal(1) / rate

    def convert(value: Decimal, source: str, target: str, label: str) -> Decimal | None:
        if source == target:
            return value
        rate = rates.get((source, target))
        if rate is None:
            reason = "stale" if (source, target) in stale or (target, source) in stale else "missing"
            warnings.append(f"Excluded {label}: {reason} FX for {source}/{target}.")
            return None
        return value * rate

    return convert


def _lookthrough(
    instrument: str,
    metadata: dict[str, Any],
    funds: dict[str, dict[str, Any]],
    *,
    path: tuple[str, ...] = (),
) -> tuple[list[tuple[Decimal, str, dict[str, Any]]], bool]:
    """Return leaf weights and whether coverage was incomplete."""
    if instrument in path:
        return [(Decimal(1), f"unknown:cycle:{instrument}", {"asset_class": "unknown"})], True
    fund = funds.get(instrument)
    if fund is None:
        return [(Decimal(1), instrument, metadata)], False
    leaves: list[tuple[Decimal, str, dict[str, Any]]] = []
    total = Decimal(0)
    partial = False
    for holding in fund["holdings"]:
        weight = _number(holding["weight"], "holding.weight")
        total += weight
        children, child_partial = _lookthrough(
            holding["instrument_id"], holding, funds, path=path + (instrument,)
        )
        leaves.extend((weight * child_weight, child_id, child_meta) for child_weight, child_id, child_meta in children)
        partial = partial or child_partial
    if total < 1:
        leaves.append((Decimal(1) - total, f"unknown:residual:{instrument}", {"asset_class": "unknown"}))
        partial = True
    return leaves, partial


def _targets(inputs: dict[str, Any]) -> list[dict[str, Any]]:
    raw = inputs.get("targets", [])
    if isinstance(raw, dict):
        expanded = []
        for dimension, values in raw.items():
            if not isinstance(values, dict):
                raise ValueError(f"targets.{dimension} must be an object")
            expanded.extend({"dimension": dimension, "name": name, "target_weight": value} for name, value in values.items())
        raw = expanded
    if not isinstance(raw, list):
        raise ValueError("targets must be a list or dimension mapping")
    result = []
    for index, target in enumerate(raw):
        if not isinstance(target, dict):
            raise ValueError(f"targets[{index}] must be an object")
        dimension = _text(target.get("dimension"), f"targets[{index}].dimension")
        if dimension not in ("asset_class", "issuer", "sector", "country", "economic_currency", "account", "person"):
            raise ValueError(f"unsupported target dimension: {dimension}")
        name = _text(target.get("name"), f"targets[{index}].name")
        weight = _number(target.get("target_weight"), f"targets[{index}].target_weight", nonnegative=True)
        if weight > 1:
            raise ValueError(f"targets[{index}].target_weight cannot exceed 1")
        result.append({"dimension": dimension, "name": name, "target_weight": weight})
    return result


def _exposure(household: dict[str, Any], inputs: dict[str, Any], base_warnings: list[str]) -> dict[str, Any]:
    warnings = list(base_warnings)
    reporting = household["currency"]
    as_of = _date(household["as_of"], "household.as_of")
    max_fx_age = int(_number(inputs.get("max_fx_age_days", 7), "max_fx_age_days", nonnegative=True))
    max_fund_age = int(_number(inputs.get("max_fund_age_days", 90), "max_fund_age_days", nonnegative=True))
    convert = _fx_converter(household, as_of, max_fx_age, warnings)

    fresh_funds: dict[str, dict[str, Any]] = {}
    stale_funds: set[str] = set()
    for fund in household["fund_holdings"]:
        age = (as_of - _date(fund["as_of"], "fund_holdings.as_of")).days
        if age > max_fund_age:
            warnings.append(f"Fund holdings for {fund['instrument_id']} are stale ({fund['as_of']}); look-through excluded.")
            stale_funds.add(fund["instrument_id"])
        else:
            fresh_funds[fund["instrument_id"]] = fund

    accounts = {account["id"]: account for account in household["accounts"]}
    owners = {identifier: account["owner_id"] for identifier, account in accounts.items()}
    exposure: dict[str, dict[str, Decimal]] = {
        dimension: {} for dimension in ("asset_class", "issuer", "sector", "country", "economic_currency", "account", "person", "instrument")
    }
    total_positions = Decimal(0)
    liquid_positions = Decimal(0)
    nonliquid_position_records = 0
    liquidity_type_defaults_used: set[str] = set()
    leaves_by_position: dict[str, dict[str, Decimal]] = {}
    incomplete_lookthrough = False
    excluded_value_records = 0

    def add(dimension: str, name: Any, value: Decimal) -> None:
        label = str(name) if name not in (None, "") else "unknown"
        exposure[dimension][label] = exposure[dimension].get(label, Decimal(0)) + value

    def unrestricted(item: dict[str, Any]) -> bool:
        return not item.get("restricted", False) and not item.get("restrictions", [])

    liquid_account_types = {"taxable", "brokerage", "bank", "cash", "checking", "savings"}

    def position_is_liquid(position: dict[str, Any]) -> bool:
        account = accounts[position["account_id"]]
        if not unrestricted(position) or not unrestricted(account):
            return False
        if position.get("liquid") is False or account.get("liquid") is False:
            return False
        if position.get("liquid") is True or account.get("liquid") is True:
            return True
        account_type = str(account["type"]).strip().lower().replace("_", " ").replace("-", " ")
        if account_type in liquid_account_types:
            liquidity_type_defaults_used.add(account_type)
            return True
        return False

    for position in household["positions"]:
        value = convert(_number(position["value"], "position.value"), position["currency"], reporting, f"position {position['id']}")
        if value is None:
            excluded_value_records += 1
            continue
        total_positions += value
        if position_is_liquid(position):
            liquid_positions += value
        else:
            nonliquid_position_records += 1
        add("account", position["account_id"], value)
        add("person", owners[position["account_id"]], value)
        looks_like_fund = str(position.get("asset_class", "")).lower() in {"fund", "etf", "mutual fund"}
        if position["instrument_id"] in stale_funds or (looks_like_fund and position["instrument_id"] not in fresh_funds):
            incomplete_lookthrough = True
            if position["instrument_id"] not in stale_funds:
                warnings.append(f"No current fund holdings supplied for {position['instrument_id']}; kept as a direct fund exposure.")
        leaves, partial = _lookthrough(position["instrument_id"], position, fresh_funds)
        incomplete_lookthrough = incomplete_lookthrough or partial
        leaf_map: dict[str, Decimal] = {}
        for weight, instrument, metadata in leaves:
            leaf_value = value * weight
            leaf_map[instrument] = leaf_map.get(instrument, Decimal(0)) + weight
            add("instrument", instrument, leaf_value)
            for dimension in ("asset_class", "issuer", "sector", "country", "economic_currency"):
                add(dimension, metadata.get(dimension), leaf_value)
        leaves_by_position[position["id"]] = leaf_map

    external_total = Decimal(0)
    liquid_external = Decimal(0)
    for item in household["external_assets"]:
        if item.get("value") in (None, ""):
            excluded_value_records += 1
            continue
        value = convert(_number(item["value"], "external_asset.value"), item["currency"], reporting, f"external asset {item['id']}")
        if value is None:
            excluded_value_records += 1
            continue
        external_total += value
        if item["liquid"]:
            liquid_external += value
        add("account", "external", value)
        add("person", "unknown", value)
        add("instrument", f"external:{item['id']}", value)
        for dimension in ("asset_class", "issuer", "sector", "country", "economic_currency"):
            add(dimension, item.get(dimension), value)

    known_liabilities = Decimal(0)
    unknown_liabilities = 0
    for item in household["liabilities"]:
        if item.get("value") in (None, ""):
            unknown_liabilities += 1
            continue
        value = convert(_number(item["value"], "liability.value"), item["currency"], reporting, f"liability {item['id']}")
        if value is None:
            unknown_liabilities += 1
        else:
            known_liabilities += value

    known_assets = total_positions + external_total
    known_nav = known_assets - known_liabilities
    liquid_capital = liquid_positions + liquid_external
    denominator = known_assets
    dimensions = {}
    for dimension, values in exposure.items():
        dimensions[dimension] = [
            {
                "name": name,
                "value": _out(value),
                "weight_of_known_assets": _out(value / denominator) if denominator else None,
            }
            for name, value in sorted(values.items(), key=lambda item: (-item[1], item[0]))
        ]

    income_rows = []
    economic_links = []
    income_gap_count = 0
    for item in household["income_exposures"]:
        gaps = []
        annual_amount = None
        if item.get("annual_amount") in (None, ""):
            gaps.append("annual_amount is unknown")
        else:
            annual_amount = {"amount": _out(_number(item["annual_amount"], "income_exposure.annual_amount")), "currency": item["currency"]}
        classifications = {
            "sector": item.get("sector"),
            "country": item.get("country"),
            "economic_currency": item["currency"],
        }
        links = []
        for dimension, name in classifications.items():
            if name in (None, ""):
                gaps.append(f"{dimension} classification is unknown")
                continue
            known_value = exposure[dimension].get(str(name), Decimal(0))
            links.append({
                "dimension": dimension,
                "name": str(name),
                "known_asset_value": _out(known_value),
                "known_assets_weight": _out(known_value / denominator) if denominator else None,
                "weight_basis": "known_assets",
            })
        income_gap_count += len(gaps)
        income_rows.append({
            "id": item["id"], "description": item["description"],
            "annual_amount": annual_amount,
            "sector": item.get("sector"), "country": item.get("country"), "currency": item["currency"],
            "gaps": gaps,
        })
        economic_links.append({
            "income_exposure_id": item["id"], "links": links, "gaps": gaps,
            "interpretation": "Shared labels show measured household concentration; they do not establish correlation or causation.",
        })

    overlaps = []
    position_list = [p for p in household["positions"] if p["id"] in leaves_by_position]
    for left_index, left in enumerate(position_list):
        for right in position_list[left_index + 1:]:
            left_map, right_map = leaves_by_position[left["id"]], leaves_by_position[right["id"]]
            shared = sorted(key for key in set(left_map) & set(right_map) if not key.startswith("unknown:"))
            overlap = sum((min(left_map[key], right_map[key]) for key in shared), Decimal(0))
            if overlap:
                overlaps.append({
                    "left_position_id": left["id"], "right_position_id": right["id"],
                    "overlap_weight": _out(overlap), "shared_instruments": shared,
                })

    target_rows = []
    unknown_asset_sections = sorted(set(household["unknown_sections"]) & {"positions", "external_assets"})
    unbounded_missing_assets = not household["complete"] or bool(unknown_asset_sections) or excluded_value_records > 0
    classification_dimensions = {"asset_class", "issuer", "sector", "country", "economic_currency"}
    for target in _targets(inputs):
        actual_value = exposure[target["dimension"]].get(target["name"], Decimal(0))
        unknown_value = Decimal(0) if target["name"] == "unknown" else exposure[target["dimension"]].get("unknown", Decimal(0))
        measured = actual_value / denominator if denominator else None
        unknown_weight = unknown_value / denominator if denominator else None
        classification_uncertain = (
            incomplete_lookthrough
            and target["dimension"] in classification_dimensions
            and not bool(unknown_weight)
        )
        if unbounded_missing_assets:
            minimum, maximum = Decimal(0), Decimal(1)
            indeterminate = True
            uncertainty = ["unbounded_missing_assets"]
        elif classification_uncertain:
            minimum = measured
            maximum = Decimal(1) if measured is not None else None
            indeterminate = True
            uncertainty = ["incomplete_fund_lookthrough"]
        else:
            minimum = measured
            maximum = measured + unknown_weight if measured is not None and unknown_weight is not None else None
            indeterminate = bool(unknown_weight)
            uncertainty = ["unclassified_known_assets"] if indeterminate else []
        if unbounded_missing_assets:
            status = "indeterminate"
        elif measured is None:
            status = "unknown"
        elif indeterminate and minimum <= target["target_weight"] <= maximum:
            status = "indeterminate"
        elif minimum > target["target_weight"]:
            status = "overweight"
        elif maximum is not None and maximum < target["target_weight"]:
            status = "underweight"
        elif measured == target["target_weight"] and not indeterminate:
            status = "on_target"
        else:
            status = "indeterminate"
        target_rows.append({
            "dimension": target["dimension"], "name": target["name"],
            "actual_weight": _out(measured) if measured is not None and not indeterminate else None,
            "measured_weight": _out(measured) if measured is not None else None,
            "measured_known_assets_weight": _out(measured) if measured is not None else None,
            "measured_weight_basis": "known_assets",
            "unknown_weight": _out(unknown_weight) if unknown_weight is not None else None,
            "possible_weight": {
                "minimum": _out(minimum), "maximum": _out(maximum),
            } if minimum is not None and maximum is not None and indeterminate else None,
            "target_weight": _out(target["target_weight"]),
            "difference": _out(measured - target["target_weight"]) if measured is not None and not indeterminate else None,
            "difference_range": {
                "minimum": _out(minimum - target["target_weight"]),
                "maximum": _out(maximum - target["target_weight"]),
            } if minimum is not None and maximum is not None and indeterminate else None,
            "status": status, "uncertainty": uncertainty,
        })

    partial = bool(warnings or incomplete_lookthrough or excluded_value_records or unknown_liabilities or income_gap_count)
    return {
        "status": "partial" if partial else "ready",
        "result": {
            "as_of": household["as_of"], "currency": reporting,
            "known_assets": _out(known_assets), "known_liabilities": _out(known_liabilities),
            "known_nav": _out(known_nav), "liquid_capital": _out(liquid_capital),
            "coverage": {
                "household_complete": household["complete"],
                "excluded_value_records": excluded_value_records,
                "unknown_liabilities": unknown_liabilities,
                "unknown_sections": household["unknown_sections"],
                "nonliquid_position_records": nonliquid_position_records,
                "income_exposure_gaps": income_gap_count,
                "lookthrough_complete": not incomplete_lookthrough,
                "weight_denominator": "known_assets",
            },
            "exposures": dimensions, "income_exposures": income_rows,
            "economic_links": economic_links, "overlap": overlaps, "targets": target_rows,
        },
        "missing": [], "warnings": list(dict.fromkeys(warnings)),
        "sources": ["household", *sorted({fund["source"] for fund in fresh_funds.values()})],
        "assumptions": [
            f"FX older than {max_fx_age} days is excluded.",
            f"Fund holdings older than {max_fund_age} days are excluded.",
            "Positions without explicit liquidity are liquid only in taxable, brokerage, bank, cash, checking, or savings accounts; restricted positions and accounts are excluded from liquid capital.",
            "Income amounts are not capitalized into assets or NAV; economic links are shared labels and do not estimate correlation or causation.",
            *([f"Used account-type liquidity defaults for: {', '.join(sorted(liquidity_type_defaults_used))}."] if liquidity_type_defaults_used else []),
        ],
    }


def run(task: str, inputs: dict[str, Any], context: dict[str, Any] | None = None) -> dict[str, Any]:
    """Run ``import`` or ``exposure`` using the shared module envelope."""
    if not isinstance(inputs, dict) or not isinstance(context or {}, dict):
        raise ValueError("inputs and context must be objects")
    context = context or {}
    if task == "import":
        household, warnings, sources = _import_household(inputs)
        return {
            "status": "partial" if warnings else "ready",
            "result": {"household": household},
            "household": household,
            "missing": [], "warnings": warnings, "sources": sources,
            "assumptions": [],
        }
    if task != "exposure":
        raise ValueError("task must be import or exposure")
    raw = inputs.get("household", context.get("household"))
    if raw is None:
        return {
            "status": "needs_input", "result": {},
            "missing": [{"key": "household", "reason": "missing", "detail": "Supply inputs.household or current context.household."}],
            "warnings": [], "sources": [], "assumptions": [],
        }
    checked = validate_household(raw)
    result = _exposure(checked["household"], inputs, checked["warnings"])
    if "household" in inputs:
        result["assumptions"].append("Used inputs.household in preference to context.household.")
    return result
