"""Service entry: the manager_* tasks."""
from __future__ import annotations

from typing import Any, Mapping

from .._common import envelope
from .edgar import Edgar, Snapshot, TRACKING_CAVEATS
from .positions import find, holdings
from .analytics import backtest, compare, profile
from .sleeve import mirror
from .example import example_pages


# ------------------------------------------------------------------ service entry

TASKS = ("manager_search", "manager_holdings", "manager_mirror", "manager_profile", "manager_compare")
_KEYS = {
    "manager_search": {"name", "limit", "snapshot"},
    "manager_holdings": {"cik", "period", "include_options", "tickers", "snapshot", "as_of"},
    "manager_profile": {"cik", "quarters", "sectors", "tickers", "snapshot", "as_of"},
    "manager_compare": {"ciks", "quarters", "sectors", "tickers", "snapshot", "as_of"},
    "manager_mirror": {"holdings", "cik", "period", "sleeve_amount", "currency", "constraints", "ips", "residence",
                       "backtest", "tickers", "snapshot", "as_of"},
}


def client_for(snapshot: Any = None) -> Edgar:
    if snapshot is None:
        return Edgar()
    if snapshot == "example":
        return Edgar(transport=Snapshot(example_pages()))
    if isinstance(snapshot, Mapping):
        return Edgar(transport=Snapshot(snapshot))
    raise ValueError('snapshot must be "example" or recorded pages {url: body}')


def run(task: str, inputs: dict[str, Any], context: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Service entry for the manager_* tasks."""
    context = context if context is not None else {}
    if task not in TASKS:
        raise ValueError(f"managers tasks are {', '.join(TASKS)}")
    if not isinstance(inputs, dict):
        raise ValueError("inputs must be an object")
    unknown = sorted(set(inputs) - _KEYS[task])
    if unknown:
        raise ValueError(f"{task} inputs: unknown {unknown}; expected {sorted(_KEYS[task])}")
    client = client_for(inputs.get("snapshot"))
    tickers = inputs.get("tickers")
    if tickers is not None and not isinstance(tickers, Mapping):
        raise ValueError("tickers must map CUSIPs to tickers")
    if task == "manager_search":
        if "name" not in inputs:
            return envelope("needs_input", {}, missing=["name (a manager, firm or person)"])
        return find(inputs["name"], client=client, limit=inputs.get("limit", 8))
    if task == "manager_holdings":
        if "cik" not in inputs:
            return envelope("needs_input", {}, missing=["cik (find it with manager_search)"])
        return holdings(inputs["cik"], inputs.get("period"), client=client, tickers=tickers,
                        include_options=bool(inputs.get("include_options", False)), as_of=inputs.get("as_of"))
    if task == "manager_profile":
        if "cik" not in inputs:
            return envelope("needs_input", {}, missing=["cik (find it with manager_search)"])
        return profile(inputs["cik"], inputs.get("quarters", 8), client=client, tickers=tickers,
                       sectors=inputs.get("sectors", True) is not False, as_of=inputs.get("as_of"))
    if task == "manager_compare":
        if not isinstance(inputs.get("ciks"), list):
            return envelope("needs_input", {}, missing=["ciks: a list of 2-6 CIKs"])
        return compare(inputs["ciks"], inputs.get("quarters", 8), client=client, tickers=tickers,
                       sectors=inputs.get("sectors", True) is not False, as_of=inputs.get("as_of"))
    # manager_mirror
    need = [k for k in ("sleeve_amount", "currency") if k not in inputs]
    if "holdings" not in inputs and "cik" not in inputs:
        need.insert(0, "holdings (a manager_holdings result) or cik")
    if need:
        return envelope("needs_input", {"tracking_caveats": TRACKING_CAVEATS}, missing=need)
    book = inputs.get("holdings")
    if book is None:
        book = holdings(inputs["cik"], inputs.get("period"), client=client, tickers=tickers, as_of=inputs.get("as_of"))
        if book["status"] == "needs_input":
            book["result"]["tracking_caveats"] = TRACKING_CAVEATS
            return book
    ips = inputs.get("ips", context.get("policy.ips"))
    residence = inputs.get("residence")
    situation = {"residence": {"country": residence}} if isinstance(residence, str) else context.get("client.profile")
    report = mirror(book, inputs["sleeve_amount"], inputs["currency"], ips=ips, situation=situation,
                    constraints=inputs.get("constraints"))
    spec = inputs.get("backtest")
    if spec:
        if not isinstance(spec, Mapping):
            raise ValueError("backtest must be {quarters?, prices?|price_csv+price_source?|years?, benchmark?}")
        cik = (report["result"].get("manager") or {}).get("cik") or inputs.get("cik")
        if not cik:
            report["warnings"].append("The backtest needs the manager's CIK.")
        else:
            cap = report["result"].get("cap", {}).get("single_name")
            constraints = inputs.get("constraints") or {}
            price_inputs = {k: spec[k] for k in ("prices", "price_csv", "price_source", "years") if k in spec}
            replay = backtest(cik, quarters=int(spec.get("quarters", 8)), price_inputs=price_inputs,
                              rules={"top_n": constraints.get("top_n"), "min_weight": constraints.get("min_weight"),
                                     "cap": cap},
                              benchmark=spec.get("benchmark"), client=client, tickers=tickers)
            report["result"]["backtest"] = replay["result"] if replay["status"] != "needs_input" else None
            report["warnings"] += [f"backtest: {w}" for w in replay["warnings"]]
            if replay["status"] == "needs_input":
                report["missing"] += [f"backtest: {m}" for m in replay["missing"]]
    return report
