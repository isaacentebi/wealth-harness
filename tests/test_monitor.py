from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from wealth.monitor import evaluate


def _fact(key, value, **extra):
    return {"key": key, "value": value, "confidence": "reported", "expires_on": None, **extra}


def _snapshot(*facts):
    return {"facts": list(facts), "decisions": []}


THESIS = {"rules": [{"id": "watch", "kind": "thesis", "fact_key": "thesis.acme"}], "timezone": "UTC"}


def _run(snapshot, state, inputs):
    report = evaluate(snapshot, state, inputs)
    return report, report.pop("state")


def test_thesis_change_stays_active_until_acknowledged():
    _, state = _run(_snapshot(_fact("thesis.acme", {"view": "durable"})), {}, THESIS)
    changed = _snapshot(_fact("thesis.acme", {"view": "impaired"}))

    first, state = _run(changed, state, THESIS)
    assert first["result"]["checks"][0]["status"] == "active"
    assert first["result"]["events"][0]["event"] == "review_needed"

    # Unreviewed: the baseline does not move, so the change neither repeats nor "resolves".
    repeat, state = _run(changed, state, THESIS)
    assert repeat["result"]["checks"][0]["status"] == "active"
    assert repeat["result"]["checks"][0]["detail"]["acknowledgement_required"] is True
    assert repeat["result"]["events"] == []

    acknowledged, state = _run(changed, state, {**THESIS, "acknowledge": ["watch"]})
    assert acknowledged["result"]["checks"][0]["status"] == "clear"
    assert acknowledged["result"]["events"][0]["event"] == "acknowledged"

    after, _ = _run(changed, state, THESIS)
    assert after["result"]["checks"][0]["status"] == "clear"
    assert after["result"]["events"] == []

    with pytest.raises(ValueError, match="acknowledge must be a list"):
        evaluate(changed, state, {**THESIS, "acknowledge": "watch"})


def test_goal_without_due_date_is_unknown_not_a_crash():
    today = datetime.now(ZoneInfo("UTC")).date()
    goals = [
        {"id": "home", "name": "Home", "due": (today + timedelta(days=10)).isoformat()},
        {"id": "someday", "name": "Someday"},
    ]
    rule = {"rules": [{"id": "due", "kind": "goal_due", "within_days": 30}], "timezone": "UTC"}
    report = evaluate(_snapshot(_fact("goals", goals)), {}, rule)
    check = report["result"]["checks"][0]
    assert check["status"] == "active"
    assert check["detail"]["goals_without_valid_due_date"] == [{"id": "someday", "name": "Someday"}]

    only_undated = evaluate(_snapshot(_fact("goals", [goals[1]])), {}, rule)
    assert only_undated["result"]["checks"][0]["status"] == "unknown"


def test_dates_use_client_timezone_then_local():
    rules = {"rules": [{"id": "limit", "kind": "threshold", "fact_key": "x", "op": "gt", "value": 1}]}
    snapshot = _snapshot(_fact("x", 0))
    for zone in ("Pacific/Kiritimati", "Pacific/Pago_Pago"):
        report = evaluate(snapshot, {}, {**rules, "timezone": zone})
        assert report["result"]["checked_on"] == datetime.now(ZoneInfo(zone)).date().isoformat()
        assert zone in report["result"]["date_basis"]

    profiled = _snapshot(_fact("x", 0), _fact("client.profile", {"timezone": "America/Mexico_City"}))
    report = evaluate(profiled, {}, rules)
    assert report["result"]["date_basis"] == "client.profile.timezone (America/Mexico_City)"

    local = evaluate(snapshot, {}, rules)
    assert local["result"]["checked_on"] == datetime.now().astimezone().date().isoformat()
    assert local["result"]["date_basis"].startswith("local timezone")

    with pytest.raises(ValueError, match="unknown timezone"):
        evaluate(snapshot, {}, {**rules, "timezone": "Mars/Olympus"})


def test_drift_is_unknown_for_a_stale_household():
    household = {
        "currency": "USD", "as_of": "2026-01-02", "complete": True, "people": [{"id": "p1"}],
        "accounts": [{"id": "a1", "owner_id": "p1", "type": "taxable", "currency": "USD"}],
        "positions": [{"id": "x", "account_id": "a1", "instrument_id": "X", "symbol": "X", "quantity": 1, "value": 100, "currency": "USD"}],
        "lots": [], "liabilities": [], "external_assets": [], "income_exposures": [], "fx": [], "fund_holdings": [],
    }
    rule = {"rules": [{"id": "drift", "kind": "drift", "target": {"X": 1}}], "timezone": "UTC"}
    report = evaluate(_snapshot(_fact("household", household)), {}, rule)
    check = report["result"]["checks"][0]
    assert check["status"] == "unknown"
    assert check["detail"]["coverage"]["household_stale"] is True
