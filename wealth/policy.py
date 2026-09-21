"""The Investment Policy Statement (IPS): a deterministic draft, its decision lifecycle and checks.

A private-bank IPS states objectives and horizons, the return each goal needs,
risk tolerance (ability versus willingness), liquidity needs, constraints, a
strategic allocation with ranges, a rebalancing rule and a review cadence.
Every recommendation is then checked against it.

* :func:`draft` derives an IPS from the canonical situation
  (:func:`wealth.situation.build`) and the person's ``preference.*`` and
  ``constraint.*`` facts.  Every number carries the rule that produced it
  (``rationale``); unknown inputs become ``missing`` entries, never guesses.
* :func:`propose` records a draft as a decision citing the evidence fact ids
  and holds the draft under the decision id.  When the person accepts that
  decision (the ordinary decision lifecycle), :func:`on_accepted` stores it as
  the fact ``policy.ips``.  A later accepted draft supersedes it; the store
  keeps the history and the new value names the decision it supersedes.
* :func:`check` tests a proposed trade or target allocation against an IPS
  and returns pass / warn / violation per rule with a plain explanation.

Every rule lives in :data:`RULES`; its text is repeated in the output next to
the number it produced.
"""
from __future__ import annotations

import math
from datetime import date, datetime, timezone
from typing import Any, Iterable, Mapping

from .situation.model import UNDERLYING, add_months, build as build_situation

# ------------------------------------------------------------------ rules

LEVELS = ("low", "medium", "high")
PROFILE_FOR_LEVEL = {"low": "conservative", "medium": "balanced", "high": "growth"}
PROFILES = ("conservative", "balanced", "growth")

RULES: dict[str, str] = {
    "required_return": (
        "Required return: the constant annual rate r at which the funded amount today plus the monthly "
        "contribution (paid at each month end, compounding monthly at (1+r)^(1/12)-1) reaches the target "
        "on the target date."),
    "funded": (
        "Funded amount: cash saved with purpose goal:<id>. When nothing is earmarked for the goal the funded "
        "amount is 0."),
    "return_requirement": (
        "Portfolio return requirement: the highest required return among goals 3 or more years away; goals "
        "due sooner are held outside risk assets and do not set it."),
    "horizon": (
        "Horizon: months to the furthest dated active goal (debt payoff goals excluded); without dated goals, "
        "the years until age 65 from birth_year. 10+ years = high, 5-10 = medium, under 5 = low."),
    "reserve_months": (
        "Reserve: months of essential spending held in reserve cash. 6+ = high, 3-6 = medium, under 3 = low."),
    "income_stability": (
        "Income stability: share of monthly income from salary or pension. 75%+ = high, 50-75% = medium, "
        "under 50% = low."),
    "debt_load": (
        "Debt load: monthly debt payments / monthly income. Up to 15% = high, 15-35% = medium, over 35% = "
        "low. Any debt at 20% a year or more = low (paying it is a better guaranteed return)."),
    "ability": (
        "Ability to take risk: the weakest of horizon, reserve, income stability and debt load. One low "
        "factor makes it low even when others are unknown."),
    "willingness": (
        "Willingness to take risk: from the answer to a 20% fall (sell = low, hold = medium, buy more = "
        "high), capped at medium with no investing experience."),
    "profile": (
        "Risk profile: the stricter of ability and willingness (low = conservative, medium = balanced, "
        "high = growth)."),
    "reserve_target": (
        "Reserve target: the saved reserve target; without one, 6 months of essential spending."),
    "buckets": (
        "Buckets by goal horizon: under 3 years = liquidity (no risk assets); 3-5 years = at most "
        "conservative; 5-10 years = at most balanced; 10+ years or an undated investing plan = the "
        "strategic profile."),
    "allocation": (
        "Strategic allocation: the model portfolio for the profile. Conservative 30% equity / 60% fixed "
        "income / 10% cash; balanced 60/35/5; growth 80/15/5."),
    "bands": (
        "Ranges and rebalancing: a sleeve of 20% or more may drift +/-5 percentage points; a smaller sleeve "
        "+/-25% of its target. Outside the range, rebalance to target, using new contributions first, then "
        "sales, preferring the least taxable lots."),
    "concentration": (
        "Concentration: no single security above 10% of the investment portfolio unless a "
        "constraint.concentration fact says otherwise. Diversified funds are limited by their sleeve, not "
        "this rule."),
    "leverage": (
        "Leverage: no borrowing, margin, short selling or leveraged products unless constraint.leverage is "
        "{allowed: true}."),
    "review": (
        "Review: once a year, and at once after a goal, income or residence change, a withdrawal need, or a "
        "sleeve outside its range."),
    "mx_vehicles": (
        "Mexico resident: fixed income and cash in Mexican government paper (CETES, UDIBONOS) in MXN; "
        "equity global, with US exposure through Irish UCITS (e.g. CSPX, listed in the SIC as CSPXN), which "
        "are not US-situs for US estate tax (US$60k exemption for non-residents, rates to 40%)."),
    "us_vehicles": (
        "US resident: fixed income in US Treasuries or aggregate bond funds, cash in T-bills or money market "
        "funds, equity in US-domiciled index funds. Non-US funds (e.g. Irish UCITS) are PFICs for a US "
        "person and are avoided."),
}

MODEL_PORTFOLIOS: dict[str, dict[str, float]] = {
    "conservative": {"equity": 0.30, "fixed_income": 0.60, "cash": 0.10},
    "balanced": {"equity": 0.60, "fixed_income": 0.35, "cash": 0.05},
    "growth": {"equity": 0.80, "fixed_income": 0.15, "cash": 0.05},
}
ABSOLUTE_BAND = 0.05
RELATIVE_BAND = 0.25
SMALL_SLEEVE = 0.20
DEFAULT_CONCENTRATION = 0.10
DEFAULT_RESERVE_MONTHS = 6
LIQUIDITY_MONTHS = 36
HIGH_INTEREST = 0.20
RETIREMENT_AGE = 65
REVIEW_CADENCES = ("annual", "semiannual", "quarterly")
_CADENCE_MONTHS = {"annual": 12, "semiannual": 6, "quarterly": 3}

# Sleeves per residence. ``asset`` is the model-portfolio class the sleeve implements.
SLEEVES: dict[str, dict[str, dict[str, Any]]] = {
    "MX": {
        "equity": {"id": "global_equity", "name": "Global equity",
                   "vehicles": ["CSPX (Irish UCITS, S&P 500, accumulating; SIC: CSPXN)",
                                "VWRA (Irish UCITS, global, accumulating)", "IWDA/SWDA (Irish UCITS, developed markets)"]},
        "fixed_income": {"id": "mx_fixed_income", "name": "Mexican government fixed income",
                         "vehicles": ["CETES (28-728 days)", "UDIBONOS (inflation-linked)"]},
        "cash": {"id": "cash", "name": "Cash (MXN)", "vehicles": ["CETES 28 days", "government money market fund"]},
    },
    "US": {
        "equity": {"id": "global_equity", "name": "Global equity",
                   "vehicles": ["VTI (US total market)", "VXUS (international)", "or VT (global)"]},
        "fixed_income": {"id": "us_fixed_income", "name": "US fixed income",
                         "vehicles": ["US Treasuries", "BND/AGG (US aggregate bonds)", "TIPS (inflation-linked)"]},
        "cash": {"id": "cash", "name": "Cash (USD)", "vehicles": ["T-bills", "money market fund"]},
    },
    "other": {
        "equity": {"id": "global_equity", "name": "Global equity", "vehicles": []},
        "fixed_income": {"id": "fixed_income", "name": "Local government fixed income", "vehicles": []},
        "cash": {"id": "cash", "name": "Cash", "vehicles": []},
    },
}

# Symbols whose issuer domicile and asset class are known, for checks without statement data.
US_DOMICILED = frozenset({"VOO", "IVV", "SPY", "SPLG", "VTI", "ITOT", "QQQ", "QQQM", "VT", "ACWI", "VXUS",
                          "BND", "AGG", "VEA", "VWO", "SCHD", "VYM", "VUG", "SCHB", "SCHX"})
NON_US_FUNDS = {"CSPX": "IE", "CSPXN": "IE", "VUSA": "IE", "VUAA": "IE", "SXR8": "IE", "CNDX": "IE", "EQQQ": "IE",
                "VWRA": "IE", "VWRL": "IE", "IWDA": "IE", "SWDA": "IE", "NAFTRAC": "MX", "IVVPESO": "MX"}
FIXED_INCOME_SYMBOLS = frozenset({"BND", "AGG", "CETES", "UDIBONOS", "BONOS", "BONDDIA", "TLT", "IEF", "SHY", "TIP"})
UCITS_EQUIVALENT = {"S&P 500": "CSPX", "Nasdaq-100": "CNDX", "US total market": "CSPX", "Global equity": "VWRA",
                    "Developed markets": "IWDA"}
LEVERAGED_INSTRUMENTS = frozenset({"margin", "option", "short", "leveraged_etf", "future", "cfd"})
# Short government paper and money market funds: allowed for the reserve and the liquidity bucket.
CASH_LIKE_SYMBOLS = frozenset({"CETES", "BONDDIA", "SHV", "BIL", "SGOV", "TBILL", "T-BILL"})

POLICY_FACT_KEYS = ("client.profile", "income.", "spending.monthly", "cash.", "liability.", "investment.", "goals",
                    "reserve", "preference.", "constraint.", "policy.ips", "onboarding")
"""Facts the policy tasks read; the service warns when one of them is stale."""


# ------------------------------------------------------------------ small helpers


def _num(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _round(value: float | None, places: int = 4) -> float | None:
    return None if value is None else round(value, places)


def _as_date(value: Any) -> date | None:
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None


def _level_min(levels: Iterable[str]) -> str:
    return min(levels, key=LEVELS.index)


def _miss(field: str, needed_for: str) -> dict:
    return {"field": field, "for": needed_for}


def band(target: float) -> tuple[float, float, str]:
    """Range for a sleeve target under the band rule: (min, max, which band)."""
    if target >= SMALL_SLEEVE:
        width, kind = ABSOLUTE_BAND, "absolute"
    else:
        width, kind = target * RELATIVE_BAND, "relative"
    return round(max(0.0, target - width), 4), round(min(1.0, target + width), 4), kind


# ------------------------------------------------------------------ preferences from facts


def preferences_from_snapshot(snapshot: Mapping[str, Any], today: date | str | None = None) -> dict[str, dict]:
    """Eligible ``preference.*`` and ``constraint.*`` facts as ``{key: {"value", "id"}}``.

    Inferred, past-review and forgotten facts are left out, as for every calculation.
    """
    day = (_as_date(today) or datetime.now(timezone.utc).date()).isoformat()
    out: dict[str, dict] = {}
    for fact in snapshot.get("facts") or []:
        key = fact.get("key") if isinstance(fact, dict) else None
        if not isinstance(key, str) or not key.startswith(("preference.", "constraint.")):
            continue
        if fact.get("status", "active") != "active" or fact.get("value") is None:
            continue
        if fact.get("confidence") == "inferred" or (fact.get("expires_on") and fact["expires_on"] < day):
            continue
        out[key] = {"value": fact["value"], "id": fact.get("id")}
    return out


def _pref(preferences: Mapping[str, Any] | None, key: str) -> tuple[Any, str | None]:
    item = (preferences or {}).get(key)
    if isinstance(item, dict) and "value" in item and set(item) <= {"value", "id"}:
        return item["value"], item.get("id")
    return item, None


# ------------------------------------------------------------------ required return


def future_value(funded: float, monthly: float, months: int, annual_rate: float) -> float:
    """Funded amount plus month-end contributions compounded monthly at the effective annual rate."""
    m = (1 + annual_rate) ** (1 / 12) - 1
    growth = (1 + m) ** months
    annuity = months if abs(m) < 1e-12 else (growth - 1) / m
    return funded * growth + monthly * annuity


def required_return(target: float, funded: float, monthly: float, months: int) -> dict:
    """The constant annual rate that reaches ``target`` in ``months`` (bisection, to 1e-9).

    A target already reached at a 0 % return is ``met`` with rate 0: no growth is
    needed, and a negative "required" return is never reported.
    """
    if months <= 0:
        reached = funded >= target
        return {"status": "reached" if reached else "due", "rate": None,
                "shortfall": None if reached else round(target - funded, 2)}
    if future_value(funded, monthly, months, 0.0) >= target:
        return {"status": "met", "rate": 0.0,
                "note": "reached without any return: the funded amount and contributions already cover the target"}
    if funded <= 0 and monthly <= 0:
        return {"status": "unfunded", "rate": None}
    low, high = 0.0, 1.0
    if future_value(funded, monthly, months, high) < target:
        # A debt that compounds can make the value fall as the rate rises; scan for any rate that works.
        grid = [i / 100 for i in range(1, 101)]
        above = next((r for r in grid if future_value(funded, monthly, months, r) >= target), None)
        if above is None:
            return {"status": "unrealistic", "rate": None,
                    "note": "needs more than 100% a year: raise the contribution, extend the date or lower the target"}
        high = above
    for _ in range(200):
        mid = (low + high) / 2
        if future_value(funded, monthly, months, mid) >= target:
            high = mid
        else:
            low = mid
        if high - low < 1e-9:
            break
    return {"status": "ready", "rate": round(high, 6) + 0.0}


def _funded(goal: Mapping[str, Any], sit: Mapping[str, Any]) -> tuple[float | None, list[str], str | None]:
    """Earmarked cash for a goal in the goal's currency; (amount, cash keys, missing reason)."""
    earmark = f"goal:{goal['id']}"
    total, keys = 0.0, []
    for row in sit.get("cash") or []:
        if row.get("purpose") != earmark or not row.get("counted", True):
            continue
        keys.append(row["key"])
        if row.get("currency") == goal.get("currency") and _num(row.get("amount")) is not None:
            total += _num(row["amount"])
        elif goal.get("currency") == sit.get("currency") and _num(row.get("value")) is not None:
            total += _num(row["value"])
        else:
            return None, keys, f"fx.{row.get('currency')}/{goal.get('currency')}"
    return total, keys, None


# ------------------------------------------------------------------ the draft


def _residence(sit: Mapping[str, Any]) -> tuple[str | None, str]:
    profile = sit.get("profile") or {}
    tax = profile.get("tax_residence") or []
    if tax:
        return tax[0], "stated tax residence"
    country = (profile.get("residence") or {}).get("country")
    return (country, "country of residence (tax residence assumed there)") if country else (None, "unknown")


def _objectives(sit: Mapping[str, Any], evidence: dict, missing: list) -> list[dict]:
    objectives = []
    for goal in sit.get("goals") or []:
        if goal.get("status") != "active" or not goal.get("eligible"):
            continue
        evidence["goals"] = True
        months = goal.get("months_left")
        target, monthly = _num(goal.get("target_amount")), _num(goal.get("monthly_contribution"))
        row = {"goal_id": goal["id"], "name": goal["name"], "action": goal.get("action"),
               "target_amount": target, "currency": goal.get("currency"), "target_date": goal.get("target_date"),
               "months_left": months, "monthly_contribution": monthly, "funded": None,
               "required_return": None, "kind": "target"}
        if goal.get("action") == "pay_off":
            row.update(kind="debt", rationale="A debt payoff goal is paid from the monthly surplus, not the portfolio.")
            objectives.append(row)
            continue
        if target is None:
            row["kind"] = "accumulation"
            row["rationale"] = ("An investing plan without a target amount has no required return; it follows "
                                "the strategic allocation." if months is None else
                                "No target amount, so no required return.")
            if months is None and monthly is None:
                missing.append(_miss(f"goals.{goal['id']}.target_amount", "objectives"))
            objectives.append(row)
            continue
        if months is None:
            missing.append(_miss(f"goals.{goal['id']}.target_date", "required return and bucket"))
            row["rationale"] = "No target date, so neither the required return nor the bucket can be set."
            objectives.append(row)
            continue
        funded, keys, fx_missing = _funded(goal, sit)
        for key in keys:
            evidence[key] = True
        if fx_missing:
            missing.append(_miss(fx_missing, f"funded amount of {goal['id']}"))
            objectives.append(row)
            continue
        row["funded"] = round(funded, 2)
        if monthly is None:
            missing.append(_miss(f"goals.{goal['id']}.monthly_contribution", "required return"))
            row["rationale"] = RULES["funded"] + " The monthly contribution is unknown, so the required return is not computed."
            objectives.append(row)
            continue
        solved = required_return(target, funded, monthly, months)
        at_zero = funded + monthly * max(months, 0)
        row["at_zero_return"] = round(at_zero, 2)
        if months < LIQUIDITY_MONTHS and at_zero < target:
            needed = (target - funded) / months if months > 0 else None
            row["shortfall_at_zero"] = round(target - at_zero, 2)
            row["monthly_needed_at_zero"] = round(needed, 2) if needed is not None else None
        row["required_return"] = solved["rate"]
        row["required_return_status"] = solved["status"]
        if solved.get("note"):
            row["note"] = solved["note"]
        row["rationale"] = (f"{RULES['required_return']} {RULES['funded']} Here: {funded:,.0f} funded + "
                            f"{monthly:,.0f}/month for {months} months to reach {target:,.0f} {goal.get('currency')}.")
        objectives.append(row)
    return objectives


def _factor(level: str | None, value: Any, rule: str, **extra: Any) -> dict:
    return {"level": level, "value": value, "rule": rule, **extra}


def _ability(sit: Mapping[str, Any], objectives: list[dict], today: date, evidence: dict, missing: list) -> dict:
    factors: dict[str, dict] = {}
    # horizon
    dated = [o["months_left"] for o in objectives if o.get("months_left") is not None and o["kind"] != "debt"]
    birth = (sit.get("profile") or {}).get("birth_year")
    if dated:
        months = max(dated)
        basis = "furthest dated goal"
    elif birth:
        months = max(0, (birth + RETIREMENT_AGE - today.year) * 12)
        basis = f"years to age {RETIREMENT_AGE}"
        evidence["client.profile"] = True
    else:
        months, basis = None, None
    if months is None:
        missing.append(_miss("goals (a dated goal) or client.profile.birth_year", "ability: horizon"))
        factors["horizon"] = _factor(None, None, RULES["horizon"])
    else:
        years = months / 12
        factors["horizon"] = _factor("high" if years >= 10 else "medium" if years >= 5 else "low",
                                     round(years, 1), RULES["horizon"], unit="years", basis=basis)
    # reserve months
    reserve = sit.get("reserve") or {}
    reserve_months = _num(reserve.get("months"))
    if reserve_months is None:
        missing.append(_miss("cash.<id> and spending.monthly", "ability: reserve months"))
        factors["reserve"] = _factor(None, None, RULES["reserve_months"])
    else:
        for key in ("spending.monthly",):
            evidence[key] = True
        for row in sit.get("cash") or []:
            if row["id"] in (reserve.get("sources") or []):
                evidence[row["key"]] = True
        factors["reserve"] = _factor("high" if reserve_months >= 6 else "medium" if reserve_months >= 3 else "low",
                                     reserve_months, RULES["reserve_months"], unit="months")
    # income stability
    items = (sit.get("income") or {}).get("items") or []
    monthly_total = sum(_num(i.get("monthly")) or 0 for i in items)
    if not items or monthly_total <= 0:
        missing.append(_miss("income.<id>", "ability: income stability"))
        factors["income_stability"] = _factor(None, None, RULES["income_stability"])
    elif any(i.get("kind") is None for i in items):
        unknown = [i["id"] for i in items if i.get("kind") is None]
        for item_id in unknown:
            missing.append(_miss(f"income.{item_id}.kind", "ability: income stability"))
        factors["income_stability"] = _factor(None, None, RULES["income_stability"])
    else:
        for i in items:
            evidence[i["key"]] = True
        stable = sum(_num(i.get("monthly")) or 0 for i in items if i.get("kind") in ("salary", "pension"))
        share = stable / monthly_total
        factors["income_stability"] = _factor("high" if share >= 0.75 else "medium" if share >= 0.5 else "low",
                                              round(share, 4), RULES["income_stability"], unit="share")
    # debt load
    liabilities = sit.get("liabilities") or []
    steps = (((sit.get("profile") or {}).get("onboarding") or {}).get("steps") or {})
    flow = sit.get("cash_flow") or {}
    income = _num(flow.get("income"))
    expensive = [r for r in liabilities if (_num(r.get("annual_rate")) or 0) >= HIGH_INTEREST and (_num(r.get("balance")) or 0) > 0]
    if expensive:
        for r in expensive:
            evidence[r["key"]] = True
        factors["debt_load"] = _factor("low", None, RULES["debt_load"],
                                       high_interest=[r["id"] for r in expensive])
    elif not liabilities and steps.get("debts") != "done":
        missing.append(_miss("liability.<id> (or say there are no debts)", "ability: debt load"))
        factors["debt_load"] = _factor(None, None, RULES["debt_load"])
    elif liabilities and (flow.get("debt_payments") is None or any(r.get("annual_rate") is None for r in liabilities)):
        for r in liabilities:
            if r.get("annual_rate") is None:
                missing.append(_miss(f"liability.{r['id']}.annual_rate", "ability: debt load"))
            if r.get("monthly_payment") is None:
                missing.append(_miss(f"liability.{r['id']}.payment", "ability: debt load"))
        factors["debt_load"] = _factor(None, None, RULES["debt_load"])
    elif not income:
        missing.append(_miss("income.<id>", "ability: debt load"))
        factors["debt_load"] = _factor(None, None, RULES["debt_load"])
    else:
        for r in liabilities:
            evidence[r["key"]] = True
        payments = _num(flow.get("debt_payments")) or 0.0
        in_spending = sum(_num(r.get("monthly_payment")) or 0 for r in liabilities if r.get("in_spending"))
        ratio = (payments + in_spending) / income
        factors["debt_load"] = _factor("high" if ratio <= 0.15 else "medium" if ratio <= 0.35 else "low",
                                       round(ratio, 4), RULES["debt_load"], unit="share of income")
    known = [f["level"] for f in factors.values() if f["level"]]
    if "low" in known:
        level = "low"
    elif len(known) == len(factors):
        level = _level_min(known)
    else:
        level = None
    return {"level": level, "factors": factors, "rationale": RULES["ability"]}


def _willingness(preferences: Mapping[str, Any] | None, evidence_ids: dict, missing: list) -> dict:
    value, fact_id = _pref(preferences, "preference.risk")
    risk = value if isinstance(value, dict) else {}
    reaction, experience = risk.get("drop_reaction"), risk.get("experience")
    base = {"sell": "low", "hold": "medium", "buy_more": "high"}.get(reaction)
    if base and fact_id:
        evidence_ids["preference.risk"] = fact_id
    if base is None:
        missing.append(_miss("preference.risk.drop_reaction", "willingness"))
        level = None
    elif base == "low":
        level = "low"
    elif experience is None:
        missing.append(_miss("preference.risk.experience", "willingness"))
        level = None
    else:
        level = "medium" if experience == "none" and base == "high" else base
    return {"level": level, "drop_reaction": reaction, "experience": experience, "rationale": RULES["willingness"]}


def _constraints(preferences: Mapping[str, Any] | None, country: str | None, us_person: bool | None,
                 evidence_ids: dict, warnings: list) -> dict:
    leverage = {"allowed": False, "basis": "default", "rationale": RULES["leverage"]}
    concentration = {"limit": DEFAULT_CONCENTRATION, "basis": "default", "rationale": RULES["concentration"]}
    exclusions: dict[str, Any] = {"tags": [], "symbols": [], "unparsed": [], "basis": None}
    other = []
    for key in sorted(k for k in (preferences or {}) if k.startswith("constraint.")):
        value, fact_id = _pref(preferences, key)
        name = key.split(".", 1)[1].lower()
        used = True
        if "leverage" in name or "margin" in name:
            allowed = isinstance(value, dict) and value.get("allowed") is True
            leverage.update(allowed=allowed, basis=key, stated=value)
        elif "concentration" in name:
            raw = value if not isinstance(value, dict) else next(
                (value.get(f) for f in ("max_single_holding", "limit", "max") if value.get(f) is not None), None)
            limit = _num(raw)
            if limit is not None and 1 < limit <= 100:
                limit = limit / 100
            if limit is None or not 0 < limit <= 1:
                warnings.append(f"{key} is not a readable limit (a share such as 0.05); the 10% default applies.")
                used = False
            else:
                concentration.update(limit=round(limit, 4), basis=key)
        elif "esg" in name or "exclu" in name or "ethic" in name:
            if isinstance(value, list):
                tags = value
            elif isinstance(value, dict):
                tags = value.get("exclude") or value.get("tags") or []
                exclusions["symbols"] += [str(s).upper() for s in value.get("symbols") or []]
            else:
                tags = []
                exclusions["unparsed"].append({"key": key, "text": value})
            exclusions["tags"] += [str(t).strip().lower() for t in tags if str(t).strip()]
            exclusions["basis"] = key
        else:
            other.append({"key": key, "value": value, "checked": False})
        if used and fact_id:
            evidence_ids[key] = fact_id
    if exclusions["unparsed"]:
        warnings.append("Exclusions written as free text are recorded but cannot be checked automatically; save "
                        "them as a list, e.g. constraint.esg {exclude: [tobacco, weapons]}.")
    exclusions["rationale"] = ("Holdings tagged with an excluded theme, or excluded symbols, are violations."
                               if exclusions["basis"] else "No exclusions stated.")
    tax: list[dict] = []
    estate = {"prefer": None, "rationale": None}
    if country == "MX":
        tax.append({"rule": "SIC listing decides the 10% rate", "text": "Gains on foreign shares listed in the SIC are "
                    "taxed at 10% definitive (LISR Art. 129), even through a foreign broker; unlisted foreign "
                    "securities are progressive income."})
        if us_person:
            estate = {"prefer": None, "rationale": "A US person is taxed on the worldwide estate; Irish UCITS are "
                      "PFICs for a US person, so no non-US fund preference applies."}
            tax.append({"rule": "PFIC", "text": RULES["us_vehicles"]})
        else:
            estate = {"prefer": "non_us_domiciled", "rationale": RULES["mx_vehicles"]}
    elif country == "US" or us_person:
        tax.append({"rule": "PFIC", "text": RULES["us_vehicles"]})
    return {"leverage": leverage, "concentration": concentration, "exclusions": exclusions,
            "estate_situs": estate, "tax": tax, "other": other}


def _cap(profile: str | None, cap: str) -> str | None:
    if profile is None:
        return None
    return PROFILES[min(PROFILES.index(profile), PROFILES.index(cap))]


def _bucket(months: int | None, profile: str | None) -> tuple[str | None, str | None]:
    if months is None:
        return None, None
    if months < LIQUIDITY_MONTHS:
        return "liquidity", None
    if months < 60:
        return "short", _cap(profile, "conservative")
    if months < 120:
        return "medium", _cap(profile, "balanced")
    return "long", profile


def _allocation(profile: str, country: str | None) -> dict:
    table = SLEEVES.get(country or "", SLEEVES["other"])
    sleeves = []
    for asset, target in MODEL_PORTFOLIOS[profile].items():
        low, high, kind = band(target)
        spec = table[asset]
        sleeves.append({"id": spec["id"], "name": spec["name"], "asset": asset, "target": target, "min": low,
                        "max": high, "band": kind, "vehicles": list(spec["vehicles"])})
    rule = RULES["allocation"] + " " + RULES["bands"]
    if country == "MX":
        rule += " " + RULES["mx_vehicles"]
    elif country == "US":
        rule += " " + RULES["us_vehicles"]
    return {"model": profile, "sleeves": sleeves, "rationale": rule}


OVERRIDES = ("profile", "concentration_limit", "reserve_months", "review_cadence")


def draft(situation: Mapping[str, Any], preferences: Mapping[str, Any] | None = None, *,
          overrides: Mapping[str, Any] | None = None) -> dict:
    """A deterministic IPS draft from the canonical situation and ``preference.*``/``constraint.*`` facts.

    ``preferences`` maps fact keys to values or to ``{"value", "id"}`` (see
    :func:`preferences_from_snapshot`).  ``overrides`` records the person's
    amendments: ``profile`` (only stricter than the derived one),
    ``concentration_limit``, ``reserve_months``, ``review_cadence``.

    Returns the module envelope: ``status`` (ready | partial | needs_input),
    ``result.ips``, ``missing``, ``warnings``, ``sources`` and ``assumptions``.
    """
    sit = situation
    overrides = dict(overrides or {})
    unknown = sorted(set(overrides) - set(OVERRIDES))
    if unknown:
        raise ValueError(f"policy overrides: unknown {unknown}; allowed {list(OVERRIDES)}")
    today = _as_date(sit.get("as_of")) or datetime.now(timezone.utc).date()
    missing: list[dict] = []
    warnings: list[str] = []
    used_keys: dict[str, bool] = {}
    pref_ids: dict[str, str] = {}
    currency = sit.get("currency")
    country, country_basis = _residence(sit)
    if country is None:
        missing.append(_miss("client.profile.residence", "vehicles, tax and estate constraints"))
    else:
        used_keys["client.profile"] = True
    us_person = (sit.get("profile") or {}).get("us_person")
    citizenship = (sit.get("profile") or {}).get("citizenship") or []
    if us_person is None and "US" in citizenship:
        us_person = True
    if country == "MX" and us_person is None:
        missing.append(_miss("client.profile.us_person", "estate situs preference (assumed not a US person)"))
    if currency is None:
        missing.append(_miss("client.profile.reporting_currency", "amounts"))

    objectives = _objectives(sit, used_keys, missing)
    ability = _ability(sit, objectives, today, used_keys, missing)
    willingness = _willingness(preferences, pref_ids, missing)
    levels = [lvl for lvl in (ability["level"], willingness["level"]) if lvl]
    if "low" in levels:
        level = "low"
    elif ability["level"] and willingness["level"]:
        level = _level_min(levels)
    else:
        level = None
    derived = PROFILE_FOR_LEVEL.get(level) if level else None
    profile = derived
    profile_basis = RULES["profile"]
    if overrides.get("profile") is not None:
        wanted = overrides["profile"]
        if wanted not in PROFILES:
            raise ValueError(f"profile must be one of {'|'.join(PROFILES)}")
        if derived and PROFILES.index(wanted) > PROFILES.index(derived):
            raise ValueError(f"profile {wanted!r} takes more risk than the derived {derived!r}; an amendment may "
                             "only be stricter. Update preference.risk or the picture instead.")
        profile, profile_basis = wanted, f"The person chose {wanted}, stricter than or equal to the derived profile."
    risk = {"ability": ability, "willingness": willingness, "level": level, "profile": profile,
            "derived_profile": derived, "rationale": profile_basis}

    # liquidity and buckets
    reserve = sit.get("reserve") or {}
    spending = sit.get("spending") or {}
    essential = _num(spending.get("essential_for_reserve"))
    reserve_months = _num(overrides.get("reserve_months"))
    if reserve_months is not None:
        target_amount = reserve_months * essential if essential else None
        reserve_basis = f"The person set the reserve at {reserve_months:g} months of essential spending."
    elif _num(reserve.get("target_amount")) is not None:
        target_amount, reserve_basis = _num(reserve["target_amount"]), RULES["reserve_target"]
        reserve_months = _num(reserve.get("target_months"))
        used_keys["reserve"] = True
    elif essential:
        reserve_months = DEFAULT_RESERVE_MONTHS
        target_amount, reserve_basis = DEFAULT_RESERVE_MONTHS * essential, RULES["reserve_target"]
    else:
        target_amount, reserve_basis = None, RULES["reserve_target"]
    if essential:
        used_keys["spending.monthly"] = True
    if target_amount is None:
        missing.append(_miss("spending.monthly", "reserve target"))
    near = []
    buckets: dict[str, dict] = {
        "liquidity": {"id": "liquidity", "horizon": "under 3 years", "max_profile": None, "max_equity": 0.0, "goals": []},
        "short": {"id": "short", "horizon": "3-5 years", "max_profile": _cap(profile, "conservative"), "goals": []},
        "medium": {"id": "medium", "horizon": "5-10 years", "max_profile": _cap(profile, "balanced"), "goals": []},
        "long": {"id": "long", "horizon": "10+ years", "max_profile": profile, "goals": []},
    }
    for bucket in buckets.values():
        if bucket["id"] != "liquidity":
            bucket["max_equity"] = MODEL_PORTFOLIOS[bucket["max_profile"]]["equity"] if bucket["max_profile"] else None
    for objective in objectives:
        if objective["kind"] == "debt":
            continue
        months = objective.get("months_left")
        if months is None and objective["kind"] == "accumulation":
            name = "long"
        else:
            name, _ = _bucket(months, profile)
        objective["bucket"] = name
        if name:
            buckets[name]["goals"].append(objective["goal_id"])
        if name == "liquidity":
            near.append({"goal_id": objective["goal_id"], "name": objective["name"],
                         "target_amount": objective["target_amount"], "currency": objective["currency"],
                         "funded": objective["funded"], "target_date": objective["target_date"]})
            if objective.get("shortfall_at_zero"):
                needed = objective.get("monthly_needed_at_zero")
                warnings.append(
                    f"{objective['name']} is due in {objective['months_left']} months and is held outside risk "
                    f"assets: at the current contribution it reaches {objective['at_zero_return']:,.0f} of "
                    f"{objective['target_amount']:,.0f} {objective['currency']}"
                    + (f"; it needs {needed:,.0f} a month, or a later date." if needed else "."))
    reserve_held = _num(reserve.get("amount"))
    liquidity = {
        "reserve": {"target_amount": _round(target_amount, 2), "target_months": reserve_months,
                    "held": reserve_held, "gap": _round(target_amount - reserve_held, 2)
                    if target_amount is not None and reserve_held is not None else None,
                    "currency": currency, "sources": list(reserve.get("sources") or []), "rationale": reserve_basis},
        "near_goals": near,
        "rationale": "The reserve and every goal due within 3 years are held outside risk assets (cash, "
                     "short government paper); they are never a source for investing.",
    }
    for bucket in buckets.values():
        bucket["rationale"] = RULES["buckets"]

    # return requirement
    candidates = [o for o in objectives if o.get("required_return") is not None and (o.get("months_left") or 0) >= LIQUIDITY_MONTHS]
    if candidates:
        top = max(candidates, key=lambda o: o["required_return"])
        return_requirement = {"value": top["required_return"], "goal_id": top["goal_id"],
                              "rationale": RULES["return_requirement"]}
    else:
        return_requirement = {"value": None, "goal_id": None, "rationale": RULES["return_requirement"]}

    constraints = _constraints(preferences, country, us_person, pref_ids, warnings)
    if overrides.get("concentration_limit") is not None:
        limit = _num(overrides["concentration_limit"])
        if limit is None or not 0 < limit <= 1:
            raise ValueError("concentration_limit must be a share between 0 and 1, such as 0.05")
        constraints["concentration"].update(limit=limit, basis="amendment")

    allocation = _allocation(profile, country) if profile else None
    if allocation is None:
        warnings.append("No risk profile yet, so no strategic allocation; the missing entries say what to ask.")
    cadence = overrides.get("review_cadence") or "annual"
    if cadence not in REVIEW_CADENCES:
        raise ValueError(f"review_cadence must be one of {'|'.join(REVIEW_CADENCES)}")
    rebalancing = {"absolute_band": ABSOLUTE_BAND, "relative_band": RELATIVE_BAND, "small_sleeve_below": SMALL_SLEEVE,
                   "method": "contributions first, then sales of the least taxable lots", "rationale": RULES["bands"]}
    review = {"cadence": cadence, "next_review": add_months(today, _CADENCE_MONTHS[cadence]).isoformat(),
              "triggers": ["goal added, changed or due", "income change of 20% or more", "change of residence",
                           "a withdrawal need", "a sleeve outside its range"],
              "rationale": RULES["review"] if cadence == "annual" else f"The person chose a {cadence} review."}

    fact_ids = sit.get("evidence") or {}
    evidence = {k: fact_ids[k] for k in sorted(used_keys) if fact_ids.get(k)}
    evidence.update(pref_ids)
    ips = {
        "version": 1, "as_of": today.isoformat(), "currency": currency,
        "residence": {"country": country, "basis": country_basis, "us_person": us_person},
        "objectives": objectives, "return_requirement": return_requirement, "risk": risk,
        "liquidity": liquidity, "buckets": list(buckets.values()), "allocation": allocation,
        "constraints": constraints, "rebalancing": rebalancing, "review": review,
        "missing": missing, "evidence": dict(sorted(evidence.items())),
    }
    status = "needs_input" if allocation is None else ("partial" if missing else "ready")
    return {"status": status, "result": {"ips": ips}, "missing": [m["field"] for m in missing],
            "warnings": warnings, "sources": [{"title": "Wealth IPS rules (wealth/policy.py RULES)"}],
            "assumptions": ["Model portfolios, bands and thresholds are the documented policy rules above, not "
                            "forecasts; the person may amend them (only towards less risk for the profile)."]}


def summary(ips: Mapping[str, Any] | None) -> dict | None:
    """A compact view of an IPS: profile, sleeves with ranges, reserve and review (for the profile page)."""
    if not isinstance(ips, Mapping):
        return None
    allocation = ips.get("allocation") or {}
    constraints = ips.get("constraints") or {}
    return {
        "version": ips.get("version"), "accepted_on": ips.get("accepted_on"), "decision_id": ips.get("decision_id"),
        "profile": (ips.get("risk") or {}).get("profile"), "currency": ips.get("currency"),
        "sleeves": [{k: s.get(k) for k in ("id", "name", "target", "min", "max")} for s in allocation.get("sleeves") or []],
        "reserve": {k: ((ips.get("liquidity") or {}).get("reserve") or {}).get(k) for k in ("target_amount", "target_months")},
        "concentration_limit": (constraints.get("concentration") or {}).get("limit"),
        "no_leverage": not (constraints.get("leverage") or {}).get("allowed", False),
        "next_review": (ips.get("review") or {}).get("next_review"),
    }


def describe(ips: Mapping[str, Any]) -> str:
    """One paragraph for the decision rationale."""
    risk = ips.get("risk") or {}
    sleeves = ", ".join(f"{s['name']} {_pct(s['target'])} ({_pct(s['min'])}-{_pct(s['max'])})"
                        for s in (ips.get("allocation") or {}).get("sleeves") or [])
    reserve = (ips.get("liquidity") or {}).get("reserve") or {}
    parts = [f"Profile {risk.get('profile')}: ability {(risk.get('ability') or {}).get('level') or 'unknown'}, "
             f"willingness {(risk.get('willingness') or {}).get('level') or 'unknown'}.",
             f"Allocation: {sleeves}."]
    if reserve.get("target_amount") is not None:
        parts.append(f"Reserve {reserve['target_amount']:,.0f} {ips.get('currency')} kept out of risk assets.")
    requirement = (ips.get("return_requirement") or {}).get("value")
    if requirement is not None:
        parts.append(f"Return requirement {requirement:.1%} a year.")
    parts.append(f"Single holdings at most {(ips.get('constraints') or {}).get('concentration', {}).get('limit', DEFAULT_CONCENTRATION):.0%}; "
                 f"rebalance outside the ranges; review {ips.get('review', {}).get('cadence', 'annual')}.")
    return " ".join(parts)


# ------------------------------------------------------------------ decision lifecycle

FACT_KEY = "policy.ips"


def current(snapshot: Mapping[str, Any], today: date | str | None = None) -> dict | None:
    """The accepted IPS, if an eligible ``policy.ips`` fact exists."""
    day = (_as_date(today) or datetime.now(timezone.utc).date()).isoformat()
    for fact in snapshot.get("facts") or []:
        if fact.get("key") == FACT_KEY and fact.get("value") is not None and fact.get("status", "active") == "active":
            if fact.get("confidence") == "inferred" or (fact.get("expires_on") and fact["expires_on"] < day):
                return None
            return {**fact["value"], "_fact_id": fact.get("id")}
    return None


def _held(decision: Mapping[str, Any]) -> dict | None:
    """The IPS a decision proposes (the ``alternatives`` entry of kind ``policy.ips``)."""
    for item in decision.get("alternatives") or []:
        if isinstance(item, dict) and item.get("kind") == FACT_KEY and isinstance(item.get("ips"), dict):
            return item["ips"]
    return None


def propose(store: Any, client_id: str, report: Mapping[str, Any], expected_revision: int) -> dict:
    """Record a drafted IPS as a decision citing its evidence.

    The decision record holds the exact IPS being proposed (``alternatives[0]``,
    kind ``policy.ips``) beside the model portfolios not chosen, so what the
    person accepts cannot differ from what was proposed.  Returns a compact view.
    """
    ips = (report.get("result") or {}).get("ips")
    if not isinstance(ips, dict) or not ips.get("allocation"):
        raise ValueError("an IPS without a strategic allocation cannot be proposed; ask for what is missing first")
    evidence_ids = [i for i in dict.fromkeys((ips.get("evidence") or {}).values()) if not str(i).startswith("request:")]
    if not evidence_ids:
        raise ValueError("an IPS proposal needs saved evidence (the person's goals, reserve, risk answers)")
    profile = ips["risk"]["profile"]
    alternatives: list[dict] = [{"kind": FACT_KEY, "ips": ips}]
    alternatives += [{"model": name, "allocation": MODEL_PORTFOLIOS[name],
                      "why_not": "takes more risk than the stricter of ability and willingness allows"
                      if PROFILES.index(name) > PROFILES.index(profile) else "takes less risk than needed"}
                     for name in PROFILES if name != profile]
    decision = store.save_decision(client_id, f"Investment policy: {profile} ({ips.get('currency')})", describe(ips),
                                   expected_revision, evidence_ids, alternatives)
    return {k: decision[k] for k in ("id", "title", "rationale", "status", "evidence_ids", "needs_review")} | {
        "alternatives": [a for a in decision["alternatives"] if a.get("kind") != FACT_KEY]}


def on_accepted(store: Any, client_id: str, decision: Mapping[str, Any]) -> dict | None:
    """Store an accepted IPS decision as ``policy.ips``; it supersedes any earlier accepted policy.

    The value records ``decision_id``, ``accepted_on``, ``version`` and the
    ``supersedes`` decision id; the store keeps every earlier version in the history.
    """
    ips = _held(decision)
    if ips is None or decision.get("status") != "accepted":
        return None
    snapshot = store.snapshot(client_id)
    prior = next((f for f in snapshot["facts"] if f["key"] == FACT_KEY and f.get("value") is not None), None)
    if prior and prior["value"].get("decision_id") == decision["id"]:
        return {"key": FACT_KEY, "id": prior["id"], "version": prior["value"].get("version"),
                "supersedes": prior["value"].get("supersedes")}
    today = datetime.now(timezone.utc).date().isoformat()
    value = {**ips, "decision_id": decision["id"], "accepted_on": today,
             "version": (prior["value"].get("version", 0) + 1) if prior else 1,
             "supersedes": prior["value"].get("decision_id") if prior else None}
    # The policy is due for review on its own cadence: past next_review it stops driving checks.
    review_on = add_months(_as_date(today), _CADENCE_MONTHS.get((ips.get("review") or {}).get("cadence"), 12))
    value["review"] = {**(ips.get("review") or {}), "next_review": review_on.isoformat()}
    receipt = store.remember(client_id, [{
        "key": FACT_KEY, "value": value, "confidence": "confirmed", "expires_on": review_on.isoformat(),
        "source": {"kind": "user", "ref": f"decision:{decision['id']}", "observed_on": today}}],
        snapshot["client"]["revision"] if prior else None)
    written = receipt["written"][0] if receipt.get("written") else {}
    return {"key": FACT_KEY, "id": written.get("id"), "version": value["version"], "supersedes": value["supersedes"]}


# ------------------------------------------------------------------ checks

STATUS_ORDER = ("pass", "warn", "violation")


def _rule(rule: str, status: str, explanation: str, **extra: Any) -> dict:
    return {"rule": rule, "status": status, "explanation": explanation, **extra}


def _symbol(value: Any) -> str | None:
    return str(value).strip().upper() if isinstance(value, str) and value.strip() else None


def _asset_of(item: Mapping[str, Any]) -> str | None:
    """equity | fixed_income | cash, from an explicit sleeve/asset class or a known symbol."""
    explicit = str(item.get("asset_class") or "").lower()
    if explicit in ("cash", "money_market"):
        return "cash"
    if explicit in ("bond", "fixed_income", "cetes", "udibonos", "bond_fund"):
        return "fixed_income"
    if explicit in ("stock", "equity", "equity_fund"):
        return "equity"
    symbol = _symbol(item.get("symbol")) or ""
    if symbol in FIXED_INCOME_SYMBOLS or symbol.startswith(("CETES", "UDI", "BONO")):
        return "fixed_income"
    if symbol.startswith("CASH"):
        return "cash"
    if explicit in ("fund", "etf") or symbol in UNDERLYING or symbol in NON_US_FUNDS or symbol in US_DOMICILED:
        return "equity" if symbol not in FIXED_INCOME_SYMBOLS else "fixed_income"
    return None


def _diversified(item: Mapping[str, Any]) -> bool | None:
    explicit = str(item.get("asset_class") or "").lower()
    if explicit in ("fund", "etf", "equity_fund", "bond_fund", "cash", "money_market", "cetes", "udibonos"):
        return True
    if explicit in ("stock", "bond"):
        return False
    symbol = _symbol(item.get("symbol")) or ""
    if symbol in UNDERLYING or symbol in NON_US_FUNDS or symbol in US_DOMICILED or symbol in FIXED_INCOME_SYMBOLS:
        return True
    return None


def _domicile(item: Mapping[str, Any]) -> str | None:
    if isinstance(item.get("domicile"), str):
        return item["domicile"].upper()
    symbol = _symbol(item.get("symbol")) or ""
    if symbol in US_DOMICILED:
        return "US"
    return NON_US_FUNDS.get(symbol)


def _sleeve_for(item: Mapping[str, Any], sleeves: list[dict]) -> str | None:
    if item.get("sleeve") in {s["id"] for s in sleeves}:
        return item["sleeve"]
    asset = _asset_of(item)
    return next((s["id"] for s in sleeves if s.get("asset") == asset), None) if asset else None


def check(ips: Mapping[str, Any], proposal: Mapping[str, Any], portfolio: Mapping[str, Any] | None = None,
          situation: Mapping[str, Any] | None = None) -> dict:
    """Check a proposed trade or target allocation against an IPS.

    ``proposal``: ``{kind: trade, action: buy|sell, symbol, amount, currency?, account?, asset_class?,
    sleeve?, domicile?, funding?: reserve|surplus|sale|cash:<id>|goal:<id>, instrument?, leverage?, tags?}``
    or ``{kind: allocation, target: {sleeve id: share}}``.
    ``portfolio`` (optional): ``{currency, positions: [{symbol, value, asset_class?, sleeve?, domicile?}]}``
    in one currency; without it band and concentration effects of a trade cannot be computed.
    Returns ``{verdict, rules: [{rule, status: pass|warn|violation, explanation}]}``.
    """
    if not isinstance(ips, Mapping) or not isinstance((ips.get("allocation") or {}).get("sleeves"), list):
        raise ValueError("ips must be an investment policy with allocation.sleeves")
    if not isinstance(proposal, Mapping):
        raise ValueError("proposal must be an object")
    kind = proposal.get("kind") or ("allocation" if "target" in proposal else "trade")
    sleeves = ips["allocation"]["sleeves"]
    constraints = ips.get("constraints") or {}
    rules: list[dict] = []
    if kind == "allocation":
        rules += _check_allocation(sleeves, proposal.get("target"))
    elif kind == "trade":
        rules += _check_trade(ips, sleeves, constraints, proposal, portfolio, situation)
    else:
        raise ValueError("proposal.kind must be trade or allocation")
    verdict = max((r["status"] for r in rules), key=STATUS_ORDER.index, default="pass")
    return {"verdict": verdict, "rules": rules,
            "policy": {"decision_id": ips.get("decision_id"), "version": ips.get("version"),
                       "profile": (ips.get("risk") or {}).get("profile")}}


def _pct(value: float) -> str:
    """0.05 -> '5%', 0.0375 -> '3.8%'."""
    text = f"{value * 100:.1f}".rstrip("0").rstrip(".")
    return text + "%"


def _outside(sleeve: Mapping[str, Any], weight: float) -> bool:
    return weight < sleeve["min"] - 1e-9 or weight > sleeve["max"] + 1e-9


def _band_rules(sleeves: list[dict], weights: Mapping[str, float], label: str,
                before: Mapping[str, float] | None = None, traded: str | None = None) -> list[dict]:
    """Sleeves against their ranges.  With ``before`` (a trade), a sleeve the trade pushes out of range,
    or the traded sleeve pushed further out, is a violation; a sleeve that was already outside is a warning."""
    out = []
    for sleeve in sleeves:
        weight = weights.get(sleeve["id"], 0.0)
        span = f"{_pct(sleeve['min'])}-{_pct(sleeve['max'])}"
        extra = {"sleeve": sleeve["id"], "weight": round(weight, 4)}
        if not _outside(sleeve, weight):
            if before is None or sleeve["id"] == traded:
                out.append(_rule("allocation_band", "pass", f"{sleeve['name']} {weight:.1%} is within {span}.", **extra))
            continue
        text = (f"{sleeve['name']} would be {weight:.1%} {label}, outside its {span} range "
                f"(target {_pct(sleeve['target'])}).")
        if before is None:
            out.append(_rule("allocation_band", "violation", text, **extra))
            continue
        prior = before.get(sleeve["id"], 0.0)
        further = abs(weight - sleeve["target"]) > abs(prior - sleeve["target"]) + 1e-9
        if not _outside(sleeve, prior) or (sleeve["id"] == traded and further):
            out.append(_rule("allocation_band", "violation", text, **extra))
        else:
            out.append(_rule("allocation_band", "warn",
                             f"{sleeve['name']} is already outside its {span} range ({prior:.1%} now, {weight:.1%} "
                             f"after); rebalance toward {_pct(sleeve['target'])}.", **extra))
    return out


def _check_allocation(sleeves: list[dict], target: Any) -> list[dict]:
    if not isinstance(target, Mapping) or not target:
        raise ValueError("an allocation proposal needs target {sleeve id: share}")
    weights: dict[str, float] = {}
    rules = []
    known = {s["id"] for s in sleeves}
    for name, share in target.items():
        value = _num(share)
        if value is None or value < 0:
            raise ValueError(f"target.{name} must be a share such as 0.6")
        if name not in known:
            rules.append(_rule("allocation_band", "violation",
                               f"{name} is not a sleeve of the policy ({', '.join(sorted(known))}).", sleeve=name))
        weights[name] = value
    total = sum(weights.values())
    if abs(total - 1) > 0.005:
        rules.append(_rule("allocation_total", "warn", f"The target adds up to {total:.1%}, not 100%."))
    return rules + _band_rules(sleeves, weights, "of the target")


def _check_trade(ips: Mapping[str, Any], sleeves: list[dict], constraints: Mapping[str, Any],
                 proposal: Mapping[str, Any], portfolio: Mapping[str, Any] | None,
                 situation: Mapping[str, Any] | None) -> list[dict]:
    action = proposal.get("action")
    if action not in ("buy", "sell"):
        raise ValueError("proposal.action must be buy or sell")
    amount = _num(proposal.get("amount"))
    if amount is None or amount <= 0:
        raise ValueError("proposal.amount must be a positive number")
    symbol = _symbol(proposal.get("symbol"))
    if not symbol:
        raise ValueError("proposal.symbol is required")
    signed = amount if action == "buy" else -amount
    rules: list[dict] = []
    sleeve = _sleeve_for(proposal, sleeves)

    # -- allocation bands and concentration need the current portfolio
    positions = (portfolio or {}).get("positions") if isinstance(portfolio, Mapping) else None
    currency_ok = not portfolio or not proposal.get("currency") or portfolio.get("currency") in (None, proposal.get("currency"))
    if isinstance(positions, list) and positions and currency_ok:
        values: dict[str, float] = {}
        by_symbol: dict[str, float] = {}
        unassigned = []
        for position in positions:
            value = _num(position.get("value"))
            if value is None:
                continue
            sid = _sleeve_for(position, sleeves)
            if sid is None:
                unassigned.append(_symbol(position.get("symbol")) or "?")
            else:
                values[sid] = values.get(sid, 0.0) + value
            sym = _symbol(position.get("symbol")) or "?"
            by_symbol[sym] = by_symbol.get(sym, 0.0) + value
        before_total = sum(by_symbol.values())
        before = {k: v / before_total for k, v in values.items()} if before_total > 0 else {}
        after_total = before_total + signed
        if sleeve:
            values[sleeve] = values.get(sleeve, 0.0) + signed
        by_symbol[symbol] = by_symbol.get(symbol, 0.0) + signed
        if by_symbol[symbol] < -1e-6:
            rules.append(_rule("position", "violation", f"Selling {amount:,.0f} of {symbol} is more than is held."))
        if after_total <= 0:
            rules.append(_rule("allocation_band", "warn", "The portfolio would be empty after this trade."))
        elif sleeve is None or unassigned:
            names = ", ".join(sorted(set(unassigned + ([symbol] if sleeve is None else []))))
            rules.append(_rule("allocation_band", "warn",
                               f"Not checked: which sleeve {names} belongs to is unknown; give asset_class or sleeve."))
        else:
            rules += _band_rules(sleeves, {k: v / after_total for k, v in values.items()}, "after the trade",
                                 before, sleeve)
        # concentration
        limit = _num((constraints.get("concentration") or {}).get("limit")) or DEFAULT_CONCENTRATION
        diversified = _diversified(proposal)
        weight = by_symbol[symbol] / after_total if after_total > 0 else 0.0
        if diversified:
            rules.append(_rule("concentration", "pass",
                               f"{symbol} is a diversified fund; the single-holding limit ({limit:.0%}) does not apply."))
        elif diversified is None and action == "buy":
            rules.append(_rule("concentration", "warn",
                               f"Not checked: whether {symbol} is a single security or a fund is unknown; at "
                               f"{weight:.1%} it would {'exceed' if weight > limit else 'be within'} the {limit:.0%} "
                               "single-holding limit."))
        elif weight > limit + 1e-9 and action == "buy":
            rules.append(_rule("concentration", "violation",
                               f"{symbol} would be {weight:.1%} of the portfolio, above the {limit:.0%} single-holding limit.",
                               weight=round(weight, 4)))
        elif weight > 0.8 * limit and action == "buy":
            rules.append(_rule("concentration", "warn",
                               f"{symbol} would be {weight:.1%}, close to the {limit:.0%} single-holding limit.",
                               weight=round(weight, 4)))
        else:
            rules.append(_rule("concentration", "pass", f"{symbol} would be {weight:.1%}, within the {limit:.0%} limit."))
    else:
        reason = "the portfolio is in another currency" if positions and not currency_ok else "no current portfolio was given"
        rules.append(_rule("allocation_band", "warn", f"Not checked: {reason}."))
        if _diversified(proposal) is not True and action == "buy":
            rules.append(_rule("concentration", "warn", f"Not checked: {reason}."))

    # -- liquidity: the reserve and near-dated goals are never a source
    funding = proposal.get("funding")
    liquidity = ips.get("liquidity") or {}
    reserve = liquidity.get("reserve") or {}
    goal_bucket = {o["goal_id"]: o.get("bucket") for o in ips.get("objectives") or [] if isinstance(o, dict)}
    risky = not (_asset_of(proposal) == "cash" or (_symbol(proposal.get("symbol")) or "") in CASH_LIKE_SYMBOLS)
    if action == "buy":
        if funding == "reserve" or (isinstance(funding, str) and funding.startswith("cash:")
                                    and funding[5:] in (reserve.get("sources") or [])):
            rules.append(_rule("liquidity_reserve", "violation",
                               "This would be paid from the emergency reserve, which the policy keeps untouched."))
        elif funding is None:
            gap = None
            if situation:
                sit_reserve = situation.get("reserve") or {}
                target = _num(reserve.get("target_amount"))
                held = _num(sit_reserve.get("amount"))
                gap = target - held if target is not None and held is not None else None
            if gap is not None and gap > 0:
                rules.append(_rule("liquidity_reserve", "warn",
                                   f"The reserve is {gap:,.0f} {ips.get('currency')} below its target; fill it before investing."))
            else:
                rules.append(_rule("liquidity_reserve", "pass" if gap is not None else "warn",
                                   "The reserve is at its target." if gap is not None else
                                   "Say where the money comes from; the reserve must stay untouched."))
        else:
            rules.append(_rule("liquidity_reserve", "pass", "The reserve is not used."))
        if isinstance(funding, str) and funding.startswith("goal:"):
            bucket = goal_bucket.get(funding[5:])
            if bucket == "liquidity" and risky:
                rules.append(_rule("goal_bucket", "violation",
                                   f"Money for {funding[5:]} is due within 3 years and must stay out of risk assets."))
            elif bucket in ("short", "medium") and _asset_of(proposal) == "equity":
                cap = next((b.get("max_equity") for b in ips.get("buckets") or [] if b.get("id") == bucket), None)
                rules.append(_rule("goal_bucket", "warn",
                                   f"Money for {funding[5:]} ({bucket} horizon) may hold at most "
                                   f"{cap:.0%} equity." if cap is not None else
                                   f"Money for {funding[5:]} has a {bucket} horizon; keep its equity share limited."))
            else:
                rules.append(_rule("goal_bucket", "pass", "The goal's bucket allows this."))
        elif funding != "reserve":
            rules.append(_rule("goal_bucket", "pass", "No goal money is used."))

    # -- constraints: leverage and exclusions
    leverage = constraints.get("leverage") or {}
    instrument = str(proposal.get("instrument") or "").lower()
    levered = proposal.get("leverage") is True or instrument in LEVERAGED_INSTRUMENTS
    if levered and not leverage.get("allowed"):
        rules.append(_rule("leverage", "violation",
                           "The policy rules out borrowing, margin, short selling and leveraged products."))
    else:
        rules.append(_rule("leverage", "pass", "No leverage."))
    exclusions = constraints.get("exclusions") or {}
    tags = {str(t).lower() for t in proposal.get("tags") or []}
    hit = sorted(tags & set(exclusions.get("tags") or []))
    if symbol in set(exclusions.get("symbols") or []) or hit:
        rules.append(_rule("exclusions", "violation",
                           f"{symbol} is excluded by the policy" + (f" ({', '.join(hit)})." if hit else ".")))
    elif action == "buy" and (exclusions.get("tags") or exclusions.get("unparsed")) and "tags" not in proposal:
        themes = ", ".join(exclusions.get("tags") or [str(u.get("text")) for u in exclusions.get("unparsed") or []])
        rules.append(_rule("exclusions", "warn", f"Not checked: confirm {symbol} holds nothing in the excluded "
                                                 f"themes ({themes})."))
    else:
        rules.append(_rule("exclusions", "pass", "No excluded theme."))

    # -- Mexico: estate situs preference
    estate = constraints.get("estate_situs") or {}
    if estate.get("prefer") == "non_us_domiciled" and action == "buy":
        domicile = _domicile(proposal)
        if domicile == "US":
            underlying = UNDERLYING.get(symbol)
            swap = UCITS_EQUIVALENT.get(underlying or "")
            rules.append(_rule("estate_situs", "warn",
                               f"{symbol} is US-domiciled, so it counts toward US estate tax for a non-resident "
                               f"(US$60k exemption, up to 40%)" + (f"; {swap} is an Irish UCITS alternative for "
                                                                   f"the same exposure." if swap else "."),
                               alternative=swap))
        elif domicile is None and _asset_of(proposal) != "fixed_income":
            rules.append(_rule("estate_situs", "warn",
                               f"Not checked: the domicile of {symbol} is unknown; US-domiciled securities are "
                               "US-situs for estate tax."))
        else:
            rules.append(_rule("estate_situs", "pass", f"{symbol} is not US-situs."))
    return rules


# ------------------------------------------------------------------ service tasks

TASKS = ("policy_draft", "policy_check")


def snapshot_from_facts(facts: Any, today: str) -> dict:
    """An inline snapshot for runs without a client: ``[{key, value}]`` treated as stated today."""
    if not isinstance(facts, list):
        raise ValueError("facts must be a list of {key, value}")
    rows = []
    for index, fact in enumerate(facts):
        if not isinstance(fact, dict) or not isinstance(fact.get("key"), str) or "value" not in fact:
            raise ValueError(f"facts[{index}] must be {{key, value}}")
        rows.append({"id": f"request:{fact['key']}", "key": fact["key"], "value": fact["value"],
                     "confidence": "reported", "status": "active",
                     "source": {"kind": "user", "ref": "current request", "observed_on": today}})
    return {"client": {"id": None, "revision": None}, "facts": rows, "decisions": []}


def run_task(task: str, inputs: Mapping[str, Any], snapshot: Mapping[str, Any], ledger: Any, today: str) -> dict:
    """Service entry for ``policy_draft`` and ``policy_check``; ``_evidence`` lists the fact ids read."""
    if task not in TASKS:
        raise ValueError(f"policy tasks are {', '.join(TASKS)}")
    inputs = dict(inputs)
    as_of = inputs.pop("as_of", None) or today
    if task == "policy_draft":
        inputs.pop("propose", None)  # handled by the service
    if task == "policy_draft":
        allowed = {"overrides", "facts"}
        unknown = sorted(set(inputs) - allowed)
        if unknown:
            raise ValueError(f"policy_draft inputs: unknown {unknown}; expected {{overrides?, propose?, facts?, as_of?}}")
        if "facts" in inputs:
            snapshot = snapshot_from_facts(inputs["facts"], as_of)
        sit = build_situation(snapshot, ledger, as_of)
        report = draft(sit, preferences_from_snapshot(snapshot, as_of), overrides=inputs.get("overrides"))
        report["_evidence"] = list((report["result"]["ips"].get("evidence") or {}).values())
        return report
    allowed = {"proposal", "ips", "portfolio"}
    unknown = sorted(set(inputs) - allowed)
    if unknown or "proposal" not in inputs:
        raise ValueError(f"policy_check inputs: {'unknown ' + str(unknown) if unknown else 'missing proposal'}; "
                         "expected {proposal, ips?, portfolio?, as_of?}")
    ips = inputs.get("ips")
    evidence = []
    if ips is None:
        ips = current(snapshot, as_of)
        if ips is None:
            lapsed = next((f for f in snapshot.get("facts") or [] if f.get("key") == FACT_KEY
                           and f.get("value") is not None and f.get("status", "active") == "active"), None)
            warning = (f"The accepted investment policy passed its review date ({lapsed.get('expires_on')}); review "
                       "it with the person: draft again (policy_draft, propose=true) and record their acceptance."
                       if lapsed else "No accepted investment policy yet; draft one with policy_draft (propose=true) "
                                      "and record the person's acceptance.")
            return {"status": "needs_input", "result": {}, "missing": ["policy.ips"], "warnings": [warning],
                    "sources": [], "assumptions": [], "_evidence": []}
        evidence.append(ips.pop("_fact_id"))
    sit = build_situation(snapshot, ledger, as_of) if snapshot.get("facts") else None
    result = check(ips, inputs["proposal"], inputs.get("portfolio"), sit)
    return {"status": "ready", "result": result, "missing": [], "warnings": [],
            "sources": [{"title": "Accepted investment policy" if evidence else "Supplied investment policy",
                         "decision_id": ips.get("decision_id")}],
            "assumptions": [], "_evidence": [e for e in evidence if e]}


__all__ = ["RULES", "MODEL_PORTFOLIOS", "TASKS", "POLICY_FACT_KEYS", "band", "check", "current", "describe", "draft",
           "future_value", "on_accepted", "preferences_from_snapshot", "propose", "required_return", "run_task",
           "summary"]
