"""Planning views over the canonical model: debt payoff, plan and calendar inputs."""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any, Mapping

from .model import D, _as_date, add_months, num, payoff

ORDERS = ("avalanche", "snowball", "custom")


def _debts(rows: list[Mapping[str, Any]]) -> tuple[list[dict], list[dict]]:
    ready, missing = [], []
    for index, row in enumerate(rows):
        balance, rate = D(row.get("balance")), D(row.get("annual_rate"))
        payment = D(row.get("monthly_payment", row.get("payment")))
        name = row.get("id") or f"debt{index}"
        gaps = [f for f, v in (("balance", balance), ("annual_rate", rate)) if v is None]
        if payment is None:
            gaps.append("monthly_payment (the minimum payment)")
        if gaps:
            missing.append({"key": f"liability.{name}", "reason": "missing", "detail": f"{name} needs {', '.join(gaps)}"})
            continue
        ready.append({"id": name, "name": row.get("name") or row.get("kind") or name, "balance": balance, "rate": rate,
                      "minimum": payment, "currency": row.get("currency")})
    return ready, missing


def _simulate(debts: list[dict], budget: Decimal, order: list[str], today: date, max_months: int = 600) -> dict:
    balances = {d["id"]: d["balance"] for d in debts}
    rates = {d["id"]: d["rate"] / 12 for d in debts}
    minimum = {d["id"]: d["minimum"] for d in debts}
    paid_off: dict[str, int] = {}
    interest = Decimal(0)
    month = 0
    while any(b > 0 for b in balances.values()) and month < max_months:
        month += 1
        for key in balances:
            if balances[key] > 0:
                charge = balances[key] * rates[key]
                interest += charge
                balances[key] += charge
        remaining = budget
        for key in balances:  # minimums first
            pay = min(minimum[key], balances[key]) if balances[key] > 0 else Decimal(0)
            balances[key] -= pay
            remaining -= pay
        for key in order:  # the extra goes down the chosen order
            if remaining <= 0:
                break
            if balances[key] > 0:
                pay = min(remaining, balances[key])
                balances[key] -= pay
                remaining -= pay
        for key, balance in balances.items():
            if balance <= Decimal("0.005") and key not in paid_off:
                balances[key] = Decimal(0)
                paid_off[key] = month
    if any(b > 0 for b in balances.values()):
        return {"status": "never", "detail": f"not repaid within {max_months} months at this budget"}
    return {"status": "ready", "months": month, "date": add_months(today, month).isoformat()[:7],
            "interest": num(interest), "order": order,
            "payoff": [{"id": k, "months": paid_off[k], "date": add_months(today, paid_off[k]).isoformat()[:7]}
                       for k in sorted(paid_off, key=lambda k: (paid_off[k], k))]}


def debt_payoff(liabilities: list[Mapping[str, Any]], monthly_amount: Any, currency: str | None = None,
                order: list[str] | None = None, as_of: str | None = None) -> dict:
    """Payoff dates for a monthly debt budget: avalanche (highest rate first) vs a chosen order.

    ``monthly_amount`` is the whole monthly budget for these debts, minimums included.
    Returns the standard envelope; ``interest_saved`` is chosen-order interest minus avalanche interest.
    """
    today = _as_date(as_of) or date.today()
    budget = D(monthly_amount)
    if budget is None or budget <= 0:
        raise ValueError("monthly_amount must be a positive number: the total monthly budget for these debts")
    ready, missing = _debts(list(liabilities))
    currencies = {d["currency"] for d in ready if d["currency"]}
    if len(currencies) > 1:
        raise ValueError(f"debts are in several currencies {sorted(currencies)}; run one currency at a time")
    currency = currency or next(iter(currencies), None)
    if not ready:
        return {"status": "needs_input", "result": {}, "missing": missing or [{"key": "liabilities", "reason": "missing",
                "detail": "No debts with balance, annual_rate and monthly_payment are known."}],
                "warnings": [], "sources": [], "assumptions": []}
    minimums = sum((d["minimum"] for d in ready), Decimal(0))
    if budget < minimums:
        return {"status": "needs_input", "result": {"minimum_payments": num(minimums), "currency": currency},
                "missing": [{"key": "monthly_amount", "reason": "invalid",
                             "detail": f"{num(budget)} is below the sum of minimum payments {num(minimums)}"}],
                "warnings": [], "sources": [], "assumptions": []}
    avalanche = [d["id"] for d in sorted(ready, key=lambda d: (-d["rate"], d["balance"], d["id"]))]
    known = {d["id"] for d in ready}
    if order is not None:
        if not isinstance(order, list) or set(order) != known or len(order) != len(known):
            raise ValueError(f"order must list each debt id exactly once: {sorted(known)}")
    chosen = order or [d["id"] for d in sorted(ready, key=lambda d: (d["balance"], d["id"]))]
    minimums_only = [{"id": d["id"], **payoff(d["balance"], d["rate"], d["minimum"], today)} for d in ready]
    best = _simulate(ready, budget, avalanche, today)
    other = _simulate(ready, budget, chosen, today)
    saved = None
    if best["status"] == "ready" and other["status"] == "ready":
        saved = num(D(other["interest"]) - D(best["interest"]))
    return {
        "status": "partial" if missing else "ready",
        "result": {"currency": currency, "monthly_amount": num(budget), "minimum_payments": num(minimums),
                   "avalanche": best, "chosen": {**other, "label": "custom" if order else "snowball"},
                   "interest_saved_by_avalanche": saved, "minimums_only": minimums_only},
        "missing": missing, "warnings": [], "sources": [],
        "assumptions": ["Rates are fixed and interest accrues monthly on the balance (annual_rate / 12).",
                        "Every debt receives its minimum first; the rest of the budget goes to one debt at a time "
                        "in the stated order, and a freed minimum rolls into the next debt.",
                        "No new borrowing or fees."],
    }


def plan_inputs(sit: Mapping[str, Any]) -> tuple[dict, list[dict], list[str]]:
    """``plan.resources`` and ``goals`` for the plan task, derived from the canonical model.

    Returns (inputs, missing, assumptions).  The pool is liquid cash plus liquid
    investments in the reporting currency; debts are paid from income.
    """
    currency = sit.get("currency")
    missing, assumptions = [], []
    nw, spending, reserve = sit["net_worth"], sit["spending"], sit["reserve"]
    if not currency:
        missing.append({"key": "client.profile.reporting_currency", "reason": "missing", "detail": "No reporting currency is known."})
    cash = sum((D(r["value"]) for r in sit["cash"] if r["counted"] and r["value"] is not None and r["liquid"]), Decimal(0))
    liquid = D(nw.get("liquid"))
    if liquid is None:
        missing.append({"key": "cash", "reason": "missing", "detail": "No cash or investment balances are known."})
    essential = D(spending.get("essential_for_reserve"))
    if essential is None:
        missing.append({"key": "spending.monthly", "reason": "missing", "detail": "Monthly spending is unknown."})
    months = D(reserve.get("target_months"))
    if months is None:
        missing.append({"key": "reserve.target_months", "reason": "missing",
                        "detail": "How many months of spending should the emergency reserve hold?"})
    if nw.get("unconverted"):
        assumptions.append("Amounts without a stored FX rate are left out of the pool: "
                           + ", ".join(f"{u['amount']} {u['currency']}" for u in nw["unconverted"]))
    goals = []
    for goal in sit["goals"]:
        if goal["status"] != "active" or not goal["eligible"]:
            continue
        if goal["target_amount"] is None or goal["target_date"] is None:
            if goal["monthly_contribution"] is not None:
                assumptions.append(f"{goal['name']} is a monthly contribution, not a dated target; it is not reserved from capital.")
            else:
                missing.append({"key": f"goals.{goal['id']}", "reason": "missing",
                                "detail": f"{goal['name']} needs target_amount and target_date"})
            continue
        if goal["currency"] != currency:
            missing.append({"key": f"goals.{goal['id']}.currency", "reason": "invalid",
                            "detail": f"{goal['name']} is in {goal['currency']}; the plan runs in {currency}"})
            continue
        protect = goal["protect_now"]
        if protect is None:
            protect = False
            assumptions.append(f"{goal['name']}: protect_now not stated, so its target is not reserved now.")
        goals.append({"id": goal["id"], "name": goal["name"], "currency": currency, "due": goal["target_date"],
                      "target_amount": goal["target_amount"], "funded_outside_pool": 0, "protect_now": protect})
    if missing:
        return {}, missing, assumptions
    assumptions.append("Planning capital is liquid cash plus liquid investments; debt payments come from income, not the pool.")
    resources = {"currency": currency, "available_capital": num(liquid), "cash_available": num(min(cash, liquid)),
                 "monthly_essentials": num(essential), "reserve_months": num(months, 2), "reserve_outside_pool": 0,
                 "debt_payments_from_pool": 0}
    return {"plan.resources": resources, "goals": goals}, [], assumptions


def calendar_inputs(sit: Mapping[str, Any], months: int = 12) -> tuple[dict, list[dict], list[str]]:
    """A 12-month ``income.schedule`` from income items, spending and debt payments."""
    currency = sit.get("currency")
    income, spending, flow = sit["income"], sit["spending"], sit["cash_flow"]
    missing = []
    if income["monthly"] is None and not income["extras"]:
        missing.append({"key": "income", "reason": "missing", "detail": "No income is known."})
    if spending["monthly"] is None:
        missing.append({"key": "spending.monthly", "reason": "missing", "detail": "Monthly spending is unknown."})
    if flow["debt_payments_unknown"]:
        missing.append({"key": "liability", "reason": "missing",
                        "detail": "Payments are unknown for: " + ", ".join(flow["debt_payments_unknown"])})
    if missing:
        return {}, missing, []
    start = date.fromisoformat(sit["as_of"]).replace(day=1)
    base = D(income["monthly"]) or Decimal(0)
    rows = []
    for offset in range(1, months + 1):
        month = add_months(start, offset)
        received = base
        for extra in income["extras"]:
            if extra["frequency"] == "annual" and extra.get("month") == month.month and extra["currency"] == currency:
                received += D(extra["amount"])
        rows.append({"month": month.isoformat()[:7], "expected_cash_received": num(received),
                     "committed_outflow": num(D(flow["debt_payments_known"]) or Decimal(0))})
    assumptions = ["Monthly income repeats; annual items (aguinaldo, PTU) land in their stated month.",
                   "Committed outflow is known debt payments not already inside spending."]
    return {"income.schedule": {"currency": currency, "monthly_need": spending["monthly"], "months": rows}}, [], assumptions


__all__ = ["debt_payoff", "plan_inputs", "calendar_inputs", "ORDERS"]
