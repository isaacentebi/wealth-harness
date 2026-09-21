"""Deterministic context compilation for the wealth-management host.

The functions in this module select evidence and perform bounded arithmetic.
They do not research securities, make recommendations, or reconstruct history.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
import re
from typing import Any, Callable


_INTENTS = {"overview", "plan", "exposure", "income", "research", "tax"}
_CONTEXT_PREFIXES = ("client.", "preference.", "constraint.", "thesis.")
_CURRENCY = re.compile(r"[A-Z]{3}")


def _today_utc() -> date:
    return datetime.now(timezone.utc).date()


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


def _decimal(value: Any, field: str) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise ValueError(f"{field} must be a nonnegative finite number")
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError(f"{field} must be a nonnegative finite number") from exc
    if not number.is_finite() or number < 0:
        raise ValueError(f"{field} must be a nonnegative finite number")
    return number


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a nonempty string")
    return value


def _currency(value: Any, field: str) -> str:
    currency = _text(value, field)
    if _CURRENCY.fullmatch(currency) is None:
        raise ValueError(f"{field} must be three uppercase letters")
    return currency


def _out(number: Decimal) -> str:
    """Stable, JSON-safe decimal representation without scientific notation."""
    if number == 0:
        return "0"
    rendered = format(number.normalize(), "f")
    return rendered.rstrip("0").rstrip(".") if "." in rendered else rendered


def _money(number: Decimal, currency: str) -> dict[str, str]:
    return {"currency": currency, "amount": _out(number)}


def _missing(key: str, reason: str, detail: str) -> dict[str, str]:
    return {"key": key, "reason": reason, "detail": detail}


def _question(missing: list[dict[str, str]]) -> str | None:
    if not missing:
        return None
    key = missing[0]["key"]
    questions = {
        "plan.resources": "What capital, monthly essentials, reserve target, outside reserve, and pool-funded debt payments should this plan use?",
        "goals": "What goals should this pool fund, or should I record that there are none?",
        "portfolio.snapshot": "What holdings and values are in the portfolio scope you want measured?",
        "income.schedule": "What monthly cash is expected to be received, what outflows are committed, and what spending need should the calendar use?",
        "income.schedule.months": "Which months should the cash-flow calendar cover, and what cash is expected in each?",
        "portfolio.snapshot.total_value": "Does this portfolio scope have a positive measured value?",
        "tax.jurisdiction": "What tax jurisdiction applies?",
        "account.*": "Which accounts are relevant to the tax question?",
        "lot.*": "Do you have tax-lot cost basis and acquisition dates for the relevant holdings?",
    }
    return questions.get(key, f"Can you provide {key}?")


def _base(snapshot: Any, intent: str) -> dict[str, Any]:
    client = snapshot.get("client", {}) if isinstance(snapshot, dict) else {}
    return {
        "client_id": client.get("id") if isinstance(client, dict) else None,
        "client_revision": client.get("revision") if isinstance(client, dict) else None,
        "intent": intent,
        "status": "needs_input",
        "evidence_ids": [],
        "facts": [],
        "decisions": [],
        "missing": [],
        "warnings": [],
        "calculations": {},
        "next_question": None,
        "capability": {
            "arithmetic": "deterministic_decimal",
            "historical_reconstruction": False,
            "external_research": False,
            "execution": False,
        },
    }


def _fact_view(fact: dict[str, Any]) -> dict[str, Any]:
    return {
        key: fact.get(key)
        for key in ("id", "key", "value", "source", "confidence", "expires_on")
        if key in fact
    }


def _select(snapshot: Any, as_of: date, packet: dict[str, Any]) -> dict[str, dict[str, Any]]:
    if not isinstance(snapshot, dict):
        packet["missing"].append(_missing("snapshot", "invalid", "snapshot must be an object"))
        return {}
    raw_facts = snapshot.get("facts", [])
    if not isinstance(raw_facts, list):
        packet["missing"].append(_missing("facts", "invalid", "facts must be a list"))
        return {}

    selected: dict[str, dict[str, Any]] = {}
    for index, fact in enumerate(raw_facts):
        if not isinstance(fact, dict) or not isinstance(fact.get("key"), str):
            packet["warnings"].append(f"Excluded malformed fact at index {index}.")
            continue
        key = fact["key"]
        reason: str | None = None
        reason_code: str | None = None
        if fact.get("confidence") == "inferred":
            reason = "inferred evidence is ineligible for decision context and arithmetic"
            reason_code = "inferred"
        source = fact.get("source")
        observed_on = source.get("observed_on") if isinstance(source, dict) else None
        try:
            observed = _date(observed_on, f"{key}.source.observed_on")
        except ValueError:
            reason = "source observed_on is missing or invalid"
            reason_code = "invalid_date"
        else:
            if observed > as_of:
                reason = "source observation is after as_of"
                reason_code = "future_observation"
        expires_on = fact.get("expires_on")
        if expires_on is not None:
            try:
                expiry = _date(expires_on, f"{key}.expires_on")
            except ValueError:
                reason = "expiry is invalid"
                reason_code = "invalid_date"
            else:
                if expiry < as_of:
                    reason = "evidence expired before as_of"
                    reason_code = "stale"
        if reason:
            packet["warnings"].append(f"Excluded {key}: {reason}.")
            packet.setdefault("_excluded", {})[key] = {
                "reason": reason_code or "ineligible",
                "detail": reason,
            }
            continue
        # Snapshots normally contain one current row per key. Be defensive and
        # retain only the greatest fact revision if a caller supplies history.
        previous = selected.get(key)
        revision = fact.get("revision", -1)
        previous_revision = previous.get("revision", -1) if previous else -1
        if not isinstance(revision, int) or isinstance(revision, bool):
            revision = -1
        if not isinstance(previous_revision, int) or isinstance(previous_revision, bool):
            previous_revision = -1
        if previous is None or revision > previous_revision:
            selected[key] = fact
    return selected


def _add_fact(packet: dict[str, Any], fact: dict[str, Any] | None) -> None:
    if fact is None or fact.get("id") in packet["evidence_ids"]:
        return
    packet["facts"].append(_fact_view(fact))
    if fact.get("id") is not None:
        packet["evidence_ids"].append(fact["id"])


def _require_fresh(
    facts: dict[str, dict[str, Any]], key: str, packet: dict[str, Any]
) -> dict[str, Any] | None:
    fact = facts.get(key)
    if fact is None:
        excluded = packet.get("_excluded", {}).get(key)
        if excluded:
            packet["missing"].append(_missing(key, excluded["reason"], excluded["detail"] + "."))
        else:
            packet["missing"].append(_missing(key, "missing_fact", f"No eligible {key} fact is available."))
        return None
    _add_fact(packet, fact)
    if fact.get("value") is None:
        packet["missing"].append(_missing(key, "known_none", f"{key} is explicitly recorded as unknown."))
        return None
    if fact.get("expires_on") is None:
        packet["missing"].append(
            _missing(key, "freshness_unbounded", f"{key} needs an explicit expiry before it can drive arithmetic.")
        )
        return None
    return fact


def _context(facts: dict[str, dict[str, Any]], packet: dict[str, Any]) -> None:
    for key in sorted(facts):
        if key.startswith(_CONTEXT_PREFIXES):
            _add_fact(packet, facts[key])


def _decisions(snapshot: Any, packet: dict[str, Any]) -> None:
    if not isinstance(snapshot, dict):
        return
    decisions = snapshot.get("decisions", [])
    if not isinstance(decisions, list):
        packet["warnings"].append("Excluded malformed decisions collection.")
        return
    for index, decision in enumerate(decisions):
        if not isinstance(decision, dict):
            packet["warnings"].append(f"Excluded malformed decision at index {index}.")
            continue
        packet["decisions"].append({
            key: decision.get(key)
            for key in ("id", "title", "rationale", "revision", "evidence_ids", "status", "needs_review")
            if key in decision
        })


def _check_reporting_currency(
    facts: dict[str, dict[str, Any]], currency: str, calculation_field: str
) -> None:
    profile = facts.get("client.profile", {}).get("value")
    if not isinstance(profile, dict) or profile.get("reporting_currency") is None:
        return
    reporting = _currency(profile["reporting_currency"], "client.profile.reporting_currency")
    if currency != reporting:
        raise ValueError(
            f"{calculation_field} conflicts with client.profile.reporting_currency; implicit FX is unsupported"
        )


def _plan(facts: dict[str, dict[str, Any]], packet: dict[str, Any], as_of: date | None = None) -> None:
    resources_fact = _require_fresh(facts, "plan.resources", packet)
    goals_fact = _require_fresh(facts, "goals", packet)
    if resources_fact is None or goals_fact is None:
        return
    try:
        resources = resources_fact["value"]
        goals = goals_fact["value"]
        if not isinstance(resources, dict):
            raise ValueError("plan.resources must be an object")
        if not isinstance(goals, list):
            raise ValueError("goals must be a list")
        currency = _currency(resources.get("currency"), "plan.resources.currency")
        _check_reporting_currency(facts, currency, "plan.resources.currency")
        available = _decimal(resources.get("available_capital"), "plan.resources.available_capital")
        monthly = _decimal(resources.get("monthly_essentials"), "plan.resources.monthly_essentials")
        reserve_months = _decimal(resources.get("reserve_months"), "plan.resources.reserve_months")
        outside_reserve = _decimal(resources.get("reserve_outside_pool"), "plan.resources.reserve_outside_pool")
        debt = _decimal(resources.get("debt_payments_from_pool"), "plan.resources.debt_payments_from_pool")

        parsed_goals: list[dict[str, Any]] = []
        ids: set[str] = set()
        for index, goal in enumerate(goals):
            if not isinstance(goal, dict):
                raise ValueError(f"goals[{index}] must be an object")
            goal_id = _text(goal.get("id"), f"goals[{index}].id")
            if goal_id in ids:
                raise ValueError(f"duplicate goal id: {goal_id}")
            ids.add(goal_id)
            goal_currency = _currency(goal.get("currency"), f"goals[{index}].currency")
            if goal_currency != currency:
                raise ValueError(f"goals[{index}].currency conflicts with plan.resources.currency")
            due = _date(goal.get("due"), f"goals[{index}].due")
            protect = goal.get("protect_now")
            if not isinstance(protect, bool):
                raise ValueError(f"goals[{index}].protect_now must be a boolean")
            target = _decimal(goal.get("target_amount"), f"goals[{index}].target_amount")
            outside = _decimal(goal.get("funded_outside_pool"), f"goals[{index}].funded_outside_pool")
            if outside > target:
                raise ValueError(f"goals[{index}].funded_outside_pool cannot exceed target_amount")
            parsed_goals.append({
                "id": goal_id,
                "name": _text(goal.get("name"), f"goals[{index}].name"),
                "due": due,
                "protect_now": protect,
                "required": target - outside,
                "outside": outside,
                "outside_funding": goal.get("outside_funding"),
            })

        any_outside = outside_reserve > 0 or any(goal["outside"] > 0 for goal in parsed_goals)
        source_rows = resources.get("outside_sources")
        reserve_rows = resources.get("reserve_funding")
        if any_outside and not isinstance(source_rows, list):
            raise ValueError("plan.resources.outside_sources is required when outside funding is nonzero")
        if any_outside and not isinstance(reserve_rows, list):
            raise ValueError("plan.resources.reserve_funding is required when any outside funding is nonzero")
        if source_rows is None:
            source_rows = []
        if reserve_rows is None:
            reserve_rows = []
        if not isinstance(source_rows, list):
            raise ValueError("plan.resources.outside_sources must be a list")
        if not isinstance(reserve_rows, list):
            raise ValueError("plan.resources.reserve_funding must be a list")

        sources: dict[str, Decimal] = {}
        for index, source in enumerate(source_rows):
            if not isinstance(source, dict):
                raise ValueError(f"plan.resources.outside_sources[{index}] must be an object")
            source_id = _text(source.get("id"), f"plan.resources.outside_sources[{index}].id")
            if source_id in sources:
                raise ValueError(f"duplicate outside source id: {source_id}")
            source_currency = _currency(
                source.get("currency"), f"plan.resources.outside_sources[{index}].currency"
            )
            if source_currency != currency:
                raise ValueError(
                    f"plan.resources.outside_sources[{index}].currency conflicts with plan.resources.currency"
                )
            sources[source_id] = _decimal(
                source.get("balance"), f"plan.resources.outside_sources[{index}].balance"
            )

        usage = {source_id: Decimal(0) for source_id in sources}

        def outside_allocations(rows: Any, field: str, expected: Decimal) -> None:
            if any_outside and not isinstance(rows, list):
                raise ValueError(f"{field} is required when outside funding is nonzero")
            if rows is None:
                rows = []
            if not isinstance(rows, list):
                raise ValueError(f"{field} must be a list")
            allocated = Decimal(0)
            for allocation_index, allocation in enumerate(rows):
                if not isinstance(allocation, dict):
                    raise ValueError(f"{field}[{allocation_index}] must be an object")
                source_id = _text(
                    allocation.get("source_id"), f"{field}[{allocation_index}].source_id"
                )
                if source_id not in sources:
                    raise ValueError(f"{field}[{allocation_index}].source_id does not reference a declared outside source")
                amount = _decimal(allocation.get("amount"), f"{field}[{allocation_index}].amount")
                allocated += amount
                usage[source_id] += amount
            if allocated != expected:
                raise ValueError(f"{field} allocations must sum exactly to its outside funding amount")

        outside_allocations(reserve_rows, "plan.resources.reserve_funding", outside_reserve)
        for index, goal in enumerate(parsed_goals):
            outside_allocations(goal["outside_funding"], f"goals[{index}].outside_funding", goal["outside"])
        for source_id, amount in usage.items():
            if amount > sources[source_id]:
                raise ValueError(f"combined outside funding usage exceeds balance for source {source_id!r}")
    except ValueError as exc:
        packet["missing"].append(_missing("plan", "invalid", str(exc)))
        packet["warnings"].append("Plan calculations were withheld because plan inputs are invalid.")
        return

    if as_of is not None and any(goal["due"] < as_of for goal in parsed_goals):
        packet["warnings"].append(
            "One or more goals were already due before as_of; they remain visible and are listed by declared due date."
        )

    reserve_target = monthly * reserve_months
    reserve_from_pool = max(Decimal(0), reserve_target - outside_reserve)
    requirements: list[dict[str, Any]] = []
    for goal in sorted(parsed_goals, key=lambda item: (item["due"], item["id"])):
        requirements.append({
            "id": goal["id"],
            "name": goal["name"],
            "due": goal["due"].isoformat(),
            "protect_now": goal["protect_now"],
            "required_from_pool": _money(goal["required"], currency),
        })
    protected_goals = sum(
        (goal["required"] for goal in parsed_goals if goal["protect_now"]), Decimal(0)
    )
    uncommitted = available - reserve_from_pool - debt - protected_goals
    shortfall = max(Decimal(0), -uncommitted)
    packet["calculations"] = {
        "currency": currency,
        "available_capital": _money(available, currency),
        "reserve_target": _money(reserve_target, currency),
        "reserve_funded_outside_pool": _money(outside_reserve, currency),
        "reserve_required_from_pool": _money(reserve_from_pool, currency),
        "debt_payments_from_pool": _money(debt, currency),
        "goal_requirements": requirements,
        "protected_goals_from_pool": _money(protected_goals, currency),
        "uncommitted_capital": _money(uncommitted, currency),
        "funding_shortfall": _money(shortfall, currency),
    }


def _exposure(facts: dict[str, dict[str, Any]], packet: dict[str, Any]) -> None:
    fact = _require_fresh(facts, "portfolio.snapshot", packet)
    if fact is None:
        return
    try:
        value = fact["value"]
        if not isinstance(value, dict):
            raise ValueError("portfolio.snapshot must be an object")
        currency = _currency(value.get("currency"), "portfolio.snapshot.currency")
        _check_reporting_currency(facts, currency, "portfolio.snapshot.currency")
        scope = _text(value.get("scope"), "portfolio.snapshot.scope")
        complete = value.get("complete")
        if not isinstance(complete, bool):
            raise ValueError("portfolio.snapshot.complete must be a boolean")
        positions = value.get("positions")
        if not isinstance(positions, list):
            raise ValueError("portfolio.snapshot.positions must be a list")
        symbols: dict[str, Decimal] = {}
        accounts: dict[str, Decimal] = {}
        classes: dict[str, Decimal] = {}
        classified = Decimal(0)
        total = Decimal(0)
        for index, position in enumerate(positions):
            if not isinstance(position, dict):
                raise ValueError(f"positions[{index}] must be an object")
            account = _text(position.get("account_id"), f"positions[{index}].account_id")
            symbol = _text(position.get("symbol"), f"positions[{index}].symbol")
            amount = _decimal(position.get("value"), f"positions[{index}].value")
            total += amount
            symbols[symbol] = symbols.get(symbol, Decimal(0)) + amount
            accounts[account] = accounts.get(account, Decimal(0)) + amount
            asset_class = position.get("asset_class")
            if asset_class is not None:
                klass = _text(asset_class, f"positions[{index}].asset_class")
                classes[klass] = classes.get(klass, Decimal(0)) + amount
                classified += amount
    except ValueError as exc:
        packet["missing"].append(_missing("portfolio.snapshot", "invalid", str(exc)))
        packet["warnings"].append("Exposure calculations were withheld because portfolio inputs are invalid.")
        return
    if total == 0:
        packet["missing"].append(_missing("portfolio.snapshot.total_value", "invalid", "Portfolio value must be positive to compute weights."))
        return

    def allocations(values: dict[str, Decimal], label: str) -> list[dict[str, str]]:
        return [
            {label: key, "value": _out(amount), "weight": _out(amount / total)}
            for key, amount in sorted(values.items(), key=lambda item: (-item[1], item[0]))
        ]

    weights = [amount / total for amount in symbols.values()]
    hhi = sum((weight * weight for weight in weights), Decimal(0))
    packet["calculations"] = {
        "currency": currency,
        "scope": scope,
        "scope_complete": complete,
        "total_measured_value": _money(total, currency),
        "symbol_allocation": allocations(symbols, "symbol"),
        "account_allocation": allocations(accounts, "account_id"),
        "asset_class_allocation": allocations(classes, "asset_class"),
        "asset_class_coverage": {
            "classified_value": _money(classified, currency),
            "unclassified_value": _money(total - classified, currency),
            "classified_weight": _out(classified / total),
        },
        "concentration": {
            "largest_symbol_weight": _out(max(weights)),
            "herfindahl_index": _out(hhi),
            "effective_symbol_count": _out(Decimal(1) / hhi),
        },
        "limitations": [
            "Weights describe only the named scope.",
            "Values are grouped by supplied symbol labels; issuer identity, venue uniqueness and fund look-through are not verified.",
        ],
    }
    if not complete:
        packet["warnings"].append("Portfolio coverage is incomplete within the named scope.")
    if classified < total:
        packet["warnings"].append("Asset-class coverage is partial; unclassified value remains explicit.")


def _income(facts: dict[str, dict[str, Any]], packet: dict[str, Any]) -> None:
    fact = _require_fresh(facts, "income.schedule", packet)
    if fact is None:
        return
    try:
        value = fact["value"]
        if not isinstance(value, dict):
            raise ValueError("income.schedule must be an object")
        currency = _currency(value.get("currency"), "income.schedule.currency")
        _check_reporting_currency(facts, currency, "income.schedule.currency")
        monthly_need = _decimal(value.get("monthly_need"), "income.schedule.monthly_need")
        months = value.get("months")
        if not isinstance(months, list):
            raise ValueError("income.schedule.months must be a list")
        seen: set[str] = set()
        rows: list[tuple[str, Decimal, Decimal]] = []
        ordinals: list[int] = []
        for index, row in enumerate(months):
            if not isinstance(row, dict):
                raise ValueError(f"months[{index}] must be an object")
            month = row.get("month")
            if not isinstance(month, str) or len(month) != 7:
                raise ValueError(f"months[{index}].month must be YYYY-MM")
            try:
                parsed = date.fromisoformat(month + "-01")
            except ValueError as exc:
                raise ValueError(f"months[{index}].month must be YYYY-MM") from exc
            if parsed.strftime("%Y-%m") != month:
                raise ValueError(f"months[{index}].month must be YYYY-MM")
            if month in seen:
                raise ValueError(f"duplicate schedule month: {month}")
            seen.add(month)
            if "expected_income" in row:
                raise ValueError(
                    f"months[{index}].expected_income is unsupported; use expected_cash_received without inferred translation"
                )
            income = _decimal(
                row.get("expected_cash_received"), f"months[{index}].expected_cash_received"
            )
            outflow = _decimal(row.get("committed_outflow"), f"months[{index}].committed_outflow")
            rows.append((month, income, outflow))
            ordinals.append(parsed.year * 12 + parsed.month - 1)
    except ValueError as exc:
        packet["missing"].append(_missing("income.schedule", "invalid", str(exc)))
        packet["warnings"].append("Income calculations were withheld because schedule inputs are invalid.")
        return

    if not rows:
        packet["missing"].append(
            _missing(
                "income.schedule.months",
                "missing_input",
                "At least one scheduled month is required before cash-flow totals can be computed.",
            )
        )
        packet["warnings"].append("The income schedule explicitly contains no months.")
        return

    calendar_missing: list[str] = []
    if ordinals:
        present = set(ordinals)
        for ordinal in range(min(ordinals), max(ordinals) + 1):
            if ordinal not in present:
                year, month_index = divmod(ordinal, 12)
                calendar_missing.append(f"{year:04d}-{month_index + 1:02d}")
    results: list[dict[str, Any]] = []
    total_gap = Decimal(0)
    total_surplus = Decimal(0)
    for month, expected, committed in sorted(rows):
        required = monthly_need + committed
        gap = max(Decimal(0), required - expected)
        surplus = max(Decimal(0), expected - required)
        total_gap += gap
        total_surplus += surplus
        results.append({
            "month": month,
            "expected_cash_received": _money(expected, currency),
            "required_cash": _money(required, currency),
            "gap": _money(gap, currency),
            "surplus": _money(surplus, currency),
        })
    packet["calculations"] = {
        "currency": currency,
        "monthly_need": _money(monthly_need, currency),
        "months": results,
        "scheduled_months_total_gap": _money(total_gap, currency),
        "scheduled_months_total_surplus": _money(total_surplus, currency),
        "calendar_coverage": {
            "scheduled_month_count": len(results),
            "first_month": min((row[0] for row in rows), default=None),
            "last_month": max((row[0] for row in rows), default=None),
            "missing_months_between": calendar_missing,
        },
        "limitations": [
            "Expected cash received is anticipated cash net of withholding, not inferred dividends or guaranteed income; unpaid taxes belong in committed outflow."
        ],
    }
    if calendar_missing:
        packet["warnings"].append("The income schedule has missing calendar months within its stated range.")


def _research(facts: dict[str, dict[str, Any]], packet: dict[str, Any]) -> None:
    for key in sorted(facts):
        if key.startswith(("research.", "thesis.", "constraint.", "client.")) or key in {"goals", "portfolio.snapshot"}:
            _add_fact(packet, facts[key])
    packet["status"] = "research_required"
    packet["calculations"] = {
        "evidence_checklist": [
            "Define the claim and decision it would affect.",
            "Obtain current primary-source evidence outside this package.",
            "Record dates, scope, and material counter-evidence.",
            "Return evidence-backed facts through a new store revision.",
        ]
    }
    packet["capability"]["research"] = "host_required"


def _tax(facts: dict[str, dict[str, Any]], packet: dict[str, Any]) -> None:
    relevant = [key for key in sorted(facts) if key.startswith(("tax.", "account.", "lot.", "client."))]
    for key in relevant:
        _add_fact(packet, facts[key])
    if "tax.jurisdiction" not in facts or facts.get("tax.jurisdiction", {}).get("value") is None:
        reason = "known_none" if "tax.jurisdiction" in facts else "missing_fact"
        packet["missing"].append(_missing("tax.jurisdiction", reason, "Tax jurisdiction is required for any later tax workflow."))
    if not any(key.startswith("account.") for key in facts):
        packet["missing"].append(_missing("account.*", "missing_fact", "Relevant account type and tax treatment are required."))
    if not any(key.startswith("lot.") for key in facts):
        packet["missing"].append(_missing("lot.*", "missing_fact", "Relevant tax-lot basis and acquisition dates are required."))
    packet["status"] = "unsupported"
    packet["capability"]["tax_computation"] = "unsupported"
    packet["warnings"].append("This package does not compute taxes or tax savings.")


def _set_status(packet: dict[str, Any], intent: str) -> None:
    if intent in {"research", "tax"}:
        return
    if packet["missing"]:
        packet["status"] = "partial" if packet["calculations"] or packet["facts"] else "needs_input"
    elif intent == "exposure" and packet["calculations"] and (
        not packet["calculations"].get("scope_complete", True)
        or packet["calculations"].get("asset_class_coverage", {}).get("classified_weight") != "1"
    ):
        packet["status"] = "partial"
    elif intent == "income" and packet["calculations"].get("calendar_coverage", {}).get("missing_months_between"):
        packet["status"] = "partial"
    elif intent == "overview" and not packet["facts"]:
        packet["status"] = "needs_input"
    else:
        packet["status"] = "ready"


def prepare(snapshot: Any, intent: str, as_of: date | str | None = None) -> dict[str, Any]:
    """Compile eligible evidence and deterministic calculations for ``intent``.

    ``as_of`` controls freshness selection only. It does not reconstruct a past
    snapshot. Invalid financial inputs are reported and never partially used.
    """
    packet = _base(snapshot, intent)
    if intent not in _INTENTS:
        packet["status"] = "unsupported"
        packet["missing"].append(_missing("intent", "unsupported", f"Unsupported intent: {intent!r}."))
        packet["next_question"] = "Should this be an overview, plan, exposure, income, research, or tax request?"
        return packet
    try:
        effective_date = _today_utc() if as_of is None else (_date(as_of, "as_of") if isinstance(as_of, str) else as_of)
        if not isinstance(effective_date, date) or isinstance(effective_date, datetime):
            raise ValueError("as_of must be a date or ISO date string")
    except ValueError as exc:
        packet["missing"].append(_missing("as_of", "invalid", str(exc)))
        packet["next_question"] = "What valid ISO date should be used for freshness checks?"
        return packet

    facts = _select(snapshot, effective_date, packet)
    _context(facts, packet)
    _add_fact(packet, facts.get("goals"))
    _decisions(snapshot, packet)
    handlers: dict[str, Callable[[dict[str, dict[str, Any]], dict[str, Any]], None]] = {
        "exposure": _exposure,
        "income": _income,
        "research": _research,
        "tax": _tax,
    }
    if intent == "overview":
        for key in ("plan.resources", "goals", "portfolio.snapshot", "income.schedule"):
            _add_fact(packet, facts.get(key))
    else:
        if intent == "plan":
            _plan(facts, packet, effective_date)
        else:
            handlers[intent](facts, packet)
    _set_status(packet, intent)
    packet["next_question"] = _question(packet["missing"])
    packet.pop("_excluded", None)
    return packet
