"""Opt-in, caller-driven monitoring. Returns events; never sends messages or trades."""
from __future__ import annotations

import hashlib
import json
import math
from datetime import date, datetime, timezone


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ValueError("monitor values must be finite numbers")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("monitor values must be finite numbers")
    return number


def evaluate(snapshot: dict, previous: dict, inputs: dict) -> dict:
    today = datetime.now(timezone.utc).date()
    records = {fact["key"]: fact for fact in snapshot["facts"]}
    eligible = {key: fact["value"] for key, fact in records.items()
                if fact["confidence"] != "inferred" and
                (not fact.get("expires_on") or fact["expires_on"] >= today.isoformat())}
    rules = inputs.get("rules", eligible.get("monitor.rules", []))
    if not isinstance(rules, list):
        raise ValueError("monitor rules must be a list")
    results, events, state, seen = [], [], {}, set()
    for rule in rules:
        if not isinstance(rule, dict) or not isinstance(rule.get("id"), str) or not rule["id"]:
            raise ValueError("each monitor rule needs an id")
        rid = rule["id"]
        if rid in seen:
            raise ValueError("monitor rule IDs must be unique")
        seen.add(rid)
        enabled = rule.get("enabled", True)
        if not isinstance(enabled, bool):
            raise ValueError("monitor enabled must be boolean")
        if not enabled:
            continue
        kind = rule.get("kind")
        status, detail, identity = "clear", {}, []
        if kind == "review":
            changed = [d["id"] for d in snapshot["decisions"] if d["needs_review"] and d["status"] != "dismissed"]
            status = "active" if changed else "clear"
            detail = {"decisions_requiring_review": changed}
            identity = sorted(changed)
        elif kind == "expiry":
            keys = rule.get("keys", list(records))
            if not isinstance(keys, list) or any(not isinstance(k, str) for k in keys):
                raise ValueError("expiry keys must be a list of fact keys")
            stale = [key for key in keys if key not in eligible]
            status = "active" if stale else "clear"
            detail = {"stale_or_missing": stale}
            identity = sorted(stale)
        elif kind == "drift":
            holding = eligible.get("portfolio.snapshot")
            target = rule.get("target")
            band = _number(rule.get("band_pp", 5))
            if band < 0 or not isinstance(target, dict) or not target:
                raise ValueError("drift needs nonnegative band_pp and target weights")
            target = {k: _number(v) for k, v in target.items()}
            if min(target.values()) < 0 or abs(sum(target.values()) - 1) > 1e-8:
                raise ValueError("target weights must be nonnegative and sum to 1")
            values, total = {}, 0
            household = eligible.get("household")
            if household:
                from .household import run
                dimension = rule.get("dimension", "instrument")
                report = run("exposure", {"household": household}, {})
                exposure = report["result"]
                coverage = exposure["coverage"]
                if dimension not in exposure["exposures"]:
                    raise ValueError("drift dimension must be a household exposure dimension")
                if (not coverage["household_complete"] or coverage["unknown_sections"]
                        or coverage["excluded_value_records"] or not coverage["lookthrough_complete"]):
                    status, detail = "unknown", {"missing": "complete household and look-through coverage", "coverage": coverage}
                else:
                    values = {r["name"]: _number(r["value"]) for r in exposure["exposures"][dimension]}
                    total = _number(exposure["known_assets"])
                    if values.get("unknown", 0):
                        status, detail = "unknown", {"missing": "complete classification for " + dimension}
            elif not holding or holding.get("complete") is not True:
                status, detail = "unknown", {"missing": "a complete fresh household or portfolio.snapshot"}
            else:
                for position in holding["positions"]:
                    if position.get("currency", holding["currency"]) != holding["currency"]:
                        raise ValueError("legacy drift snapshot cannot mix currencies; import a household with explicit FX")
                    amount = _number(position["value"])
                    if amount < 0:
                        raise ValueError("negative positions unsupported by drift monitor")
                    values[position["symbol"]] = values.get(position["symbol"], 0) + amount
                total = sum(values.values())
            if status != "unknown":
                if total <= 0:
                    status, detail = "unknown", {"missing": "positive portfolio value"}
                else:
                    drift = {k: 100 * (values.get(k, 0) / total - target.get(k, 0)) for k in set(values) | set(target)}
                    breaches = {k: round(v, 4) for k, v in drift.items() if abs(v) > band}
                    status = "active" if breaches else "clear"
                    detail = {"drift_pp": breaches, "band_pp": band}
                    identity = sorted((k, 1 if v > 0 else -1) for k, v in breaches.items())
        elif kind == "goal_due":
            days = rule.get("within_days", 90)
            if not isinstance(days, int) or isinstance(days, bool) or not 0 <= days <= 3650:
                raise ValueError("within_days must be 0–3650")
            goals = eligible.get("goals")
            if goals is None:
                status, detail = "unknown", {"missing": "fresh goals"}
            else:
                due = [g for g in goals if (date.fromisoformat(g["due"]) - today).days <= days]
                status = "active" if due else "clear"
                detail = {"goals": [{"id": g["id"], "name": g["name"], "due": g["due"]} for g in due]}
                identity = sorted((g["id"], g["due"]) for g in due)
        elif kind in {"threshold", "thesis"}:
            key = rule.get("fact_key")
            if not isinstance(key, str):
                raise ValueError("monitor rule needs fact_key")
            if key not in eligible:
                status, detail = "unknown", {"missing": key}
            else:
                value = eligible[key]
                path = rule.get("field", [])
                if not isinstance(path, list) or any(not isinstance(k, str) for k in path):
                    raise ValueError("field is a list of object keys")
                try:
                    for part in path:
                        value = value[part]
                except (KeyError, TypeError):
                    value = None
                if kind == "thesis":
                    if value is None:
                        status, detail = "unknown", {"missing": [key, *path]}
                    else:
                        digest = hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()
                        baseline = previous.get(rid, {}).get("value_hash")
                        status = "active" if baseline and baseline != digest else "clear"
                        detail = {"fact_key": key, "changed": status == "active"}
                        identity = [digest]
                elif value is None:
                    status, detail = "unknown", {"missing": [key, *path]}
                else:
                    left, right = _number(value), _number(rule.get("value"))
                    op = rule.get("op")
                    if op not in {"gt", "lt", "gte", "lte"}:
                        raise ValueError("threshold op must be gt,lt,gte,lte")
                    active = {"gt": left > right, "lt": left < right, "gte": left >= right, "lte": left <= right}[op]
                    status = "active" if active else "clear"
                    detail = {"fact_key": key, "observed": left, "threshold": right, "op": op}
        else:
            raise ValueError("monitor kind must be review, expiry, drift, goal_due, threshold, or thesis")
        fingerprint = hashlib.sha256(json.dumps([rule, status, identity], sort_keys=True).encode()).hexdigest()
        old = previous.get(rid)
        item = {"rule_id": rid, "kind": kind, "status": status, "detail": detail}
        results.append(item)
        if not old:
            if status in {"active", "unknown"}:
                events.append({**item, "event": "review_needed"})
        elif old.get("status") != status:
            events.append({**item, "event": "resolved" if status == "clear" else "review_needed"})
        elif status in {"active", "unknown"} and old.get("fingerprint") != fingerprint:
            events.append({**item, "event": "review_needed"})
        state[rid] = {"fingerprint": fingerprint, "status": status}
        if kind == "thesis":
            if status != "unknown":
                state[rid]["value_hash"] = digest
            elif old and old.get("value_hash"):
                state[rid]["value_hash"] = old["value_hash"]
    return {"status": "ready", "result": {"checks": results, "events": events,
            "checked_on": today.isoformat(), "configured_rules": len(rules),
            "delivery": "returned to caller only; no external messages or trades"},
            "missing": [], "warnings": [], "sources": [], "assumptions": [], "state": state}
