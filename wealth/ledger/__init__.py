"""Transaction ledger: the source of truth that holdings, lots, income,
spending, and performance are derived from.

* :mod:`.model` validates the one posting input shape (a *batch*).
* ``WealthStore.post_ledger`` persists batches idempotently and append-only.
* :mod:`.derive` and :mod:`.performance` are pure functions over the ledger
  document returned by ``WealthStore.ledger``.

``run(task, inputs, context)`` follows the shared module envelope so the
service can route ``ledger`` and ``performance`` tasks here.  The ledger
document comes from ``inputs.ledger`` or ``context["ledger"]``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping

from .derive import (FxTable, active_entries, envelope, holdings, investment_income, match_transfers,
                     price_table, realized_gains, reconcile, replay, to_household)
from .model import LedgerInputError, normalize_batch
from .performance import (exposure_groups, external_flows, performance, performance_by_account,
                          valuation_series, xirr)


def post(store: Any, client_id: str, batch: Mapping[str, Any]) -> dict[str, Any]:
    """Post a batch and reconcile the statement assertions it carried."""

    receipt = store.post_ledger(client_id, batch)
    ledger = store.ledger(client_id)
    asserted = set(receipt["assertions"]["added"]) | set(receipt["assertions"]["existing"])
    if asserted:
        subset = dict(ledger, assertions=[a for a in ledger["assertions"] if a["id"] in asserted])
        check = reconcile(subset)
        receipt["reconciliation"] = {"status": check["status"], "breaks": check["result"]["breaks"],
                                     "checks": len(check["result"]["checks"])}
    return receipt


def _ledger(inputs: Mapping[str, Any], context: Mapping[str, Any]) -> Mapping[str, Any] | None:
    ledger = inputs.get("ledger", context.get("ledger"))
    if ledger is not None and not isinstance(ledger, Mapping):
        raise ValueError("ledger must be an object")
    return ledger


def _prices(inputs: Mapping[str, Any], key: str = "prices"):
    series = inputs.get(key) or {}
    if not isinstance(series, Mapping):
        raise ValueError(f"{key} must map instrument_id to [{{date, price}}]")
    return price_table(series, int(inputs.get("max_price_age_days", 5)))


def _benchmark(inputs: Mapping[str, Any]):
    spec = inputs.get("benchmark")
    if spec is None:
        return None, None
    if not isinstance(spec, Mapping) or not isinstance(spec.get("series"), (list, Mapping)):
        raise ValueError("benchmark must be {name, series: [{date, value}]}")
    provider = price_table({"benchmark": spec["series"]}, int(inputs.get("max_price_age_days", 5)))
    return (lambda day: provider("benchmark", day)), spec.get("name")


def run(task: str, inputs: dict[str, Any], context: dict[str, Any] | None = None) -> dict[str, Any]:
    """``ledger`` (view=holdings|household|reconcile|realized|income|transfers|groups) or ``performance``."""

    context = context or {}
    if not isinstance(inputs, dict):
        raise ValueError("inputs must be an object")
    ledger = _ledger(inputs, context)
    if ledger is None:
        return envelope("needs_input", {}, missing=[{"key": "ledger", "reason": "missing",
                        "detail": "No ledger is stored for this client; post a statement or transactions first."}])
    today = datetime.now(timezone.utc).date().isoformat()
    as_of = inputs.get("as_of", today)
    fx_age = int(inputs.get("max_fx_age_days", 5))
    include_inferred = bool(inputs.get("include_inferred", False))
    if task == "performance":
        for key in ("start", "currency"):
            if key not in inputs:
                return envelope("needs_input", {}, missing=[{"key": key, "reason": "missing",
                                "detail": "performance needs start, end (default today) and currency."}])
        bench, name = _benchmark(inputs)
        kwargs = dict(method=inputs.get("method", "linked"), benchmark=bench, benchmark_name=name,
                      include_inferred=include_inferred, fx_max_age_days=fx_age)
        if inputs.get("by_account"):
            return performance_by_account(ledger, inputs["start"], inputs.get("end", as_of), inputs["currency"],
                                          _prices(inputs), **kwargs)
        return performance(ledger, inputs["start"], inputs.get("end", as_of), inputs["currency"], _prices(inputs),
                           account_ids=inputs.get("account_ids"), **kwargs)
    if task != "ledger":
        raise ValueError("task must be ledger or performance")
    view = inputs.get("view", "holdings")
    lot_method = inputs.get("lot_method", "fifo")
    if view == "holdings":
        return holdings(ledger, as_of, lot_method=lot_method, include_inferred=include_inferred, fx_max_age_days=fx_age)
    if view == "household":
        if "currency" not in inputs:
            return envelope("needs_input", {}, missing=[{"key": "currency", "reason": "missing",
                            "detail": "The household view needs a reporting currency."}])
        return to_household(ledger, as_of, inputs["currency"], _prices(inputs),
                            default_owner_id=inputs.get("default_owner_id"), complete=bool(inputs.get("complete", False)),
                            lot_method=lot_method, include_inferred=include_inferred, fx_max_age_days=fx_age)
    if view == "reconcile":
        return reconcile(ledger, as_of=inputs.get("as_of"), tolerance=str(inputs.get("tolerance", "0.01")),
                         include_inferred=include_inferred)
    if view == "realized":
        return realized_gains(ledger, year=inputs.get("year"), as_of=as_of, lot_method=lot_method,
                              include_inferred=include_inferred, fx_max_age_days=fx_age)
    if view == "income":
        return investment_income(ledger, year=inputs.get("year"), as_of=as_of, include_inferred=include_inferred,
                                 fx_max_age_days=fx_age)
    if view == "transfers":
        entries, notes = active_entries(ledger, include_inferred=include_inferred)
        matched = match_transfers(entries)
        return envelope("partial" if matched["unmatched"] else "ready",
                        {"pairs": matched["pairs"], "unmatched": matched["unmatched"]},
                        warnings=notes + matched["warnings"])
    if view == "groups":
        if "currency" not in inputs:
            return envelope("needs_input", {}, missing=[{"key": "currency", "reason": "missing",
                            "detail": "Grouping needs a reporting currency."}])
        return exposure_groups(ledger, as_of, inputs["currency"], _prices(inputs), by=inputs.get("by", "underlying"),
                               account_ids=inputs.get("account_ids"), include_inferred=include_inferred,
                               fx_max_age_days=fx_age)
    raise ValueError("view must be holdings, household, reconcile, realized, income, transfers, or groups")


__all__ = [
    "FxTable", "LedgerInputError", "active_entries", "exposure_groups", "external_flows", "holdings",
    "investment_income", "match_transfers", "normalize_batch", "performance", "performance_by_account", "post",
    "price_table", "realized_gains", "reconcile", "replay", "run", "to_household", "valuation_series", "xirr",
]
