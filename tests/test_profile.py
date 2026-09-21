import json
from datetime import date, datetime, timedelta, timezone

import pytest

from wealth.profile import (
    fact_action, form_facts, profile_view, time_weighted_return, xirr,
)
from wealth.service import WealthService

TODAY = datetime.now(timezone.utc).date()


def _src(kind="user", observed=None, ref="test"):
    return {"kind": kind, "ref": ref, "observed_on": (observed or TODAY).isoformat()}


def seed_mexico(db_path, client_id="mx"):
    """A fictional Mexico-resident household reporting in MXN with USD holdings."""
    service = WealthService(db_path)
    service.create(client_id, "Fictional MX client")
    iso = TODAY.isoformat()
    household = {
        "currency": "MXN", "as_of": iso, "complete": True,
        "people": [{"id": "ana", "name": "Ana"}, {"id": "luis", "name": "Luis"}],
        "accounts": [
            {"id": "gbm", "name": "Brokerage MX", "owner_id": "ana", "type": "brokerage", "currency": "MXN"},
            {"id": "ib", "name": "US brokerage", "owner_id": "luis", "type": "taxable", "currency": "USD"},
            {"id": "afore", "name": "Retirement (Afore)", "owner_id": "ana", "type": "retirement", "currency": "MXN"},
        ],
        "positions": [
            {"id": "p1", "account_id": "ib", "instrument_id": "VOO", "symbol": "VOO", "quantity": 20,
             "value": 10000, "currency": "USD", "asset_class": "fund"},
            {"id": "p2", "account_id": "ib", "instrument_id": "AAPL", "symbol": "AAPL", "quantity": 10,
             "value": 2000, "currency": "USD", "asset_class": "equity", "economic_currency": "USD"},
            {"id": "p3", "account_id": "gbm", "instrument_id": "CETES", "symbol": "CETES", "quantity": 1,
             "value": 150000, "currency": "MXN", "asset_class": "fixed income", "economic_currency": "MXN"},
            {"id": "p4", "account_id": "afore", "instrument_id": "AFORE", "symbol": "AFORE", "quantity": 1,
             "value": 300000, "currency": "MXN", "asset_class": "pension", "economic_currency": "MXN"},
        ],
        "lots": [], "income_exposures": [],
        "liabilities": [{"id": "car", "value": 80000, "currency": "MXN"}],
        "external_assets": [],
        "fx": [{"from": "USD", "to": "MXN", "rate": 18, "as_of": iso, "source": "fictional"}],
        "fund_holdings": [{"instrument_id": "VOO", "as_of": iso, "source": "fictional factsheet", "holdings": [
            {"instrument_id": "AAPL", "symbol": "AAPL", "weight": 0.07, "asset_class": "equity", "economic_currency": "USD"},
            {"instrument_id": "MSFT", "symbol": "MSFT", "weight": 0.06, "asset_class": "equity", "economic_currency": "USD"},
            {"instrument_id": "REST", "symbol": "Other S&P 500", "weight": 0.87, "asset_class": "equity", "economic_currency": "USD"},
        ]}],
    }
    history_start = TODAY - timedelta(days=400)
    valuations = [{"date": (history_start + timedelta(days=d)).isoformat(), "value": v}
                  for d, v in [(0, 500000), (100, 520000), (200, 560000), (300, 555000), (400, 640000)]]
    old = TODAY - timedelta(days=500)
    facts = [
        {"key": "client.profile", "value": {"reporting_currency": "MXN", "currencies": ["MXN", "USD"],
                                            "tax_residence": ["Mexico"], "dependents": 1,
                                            "monthly_income": {"amount": 85000, "currency": "MXN"}},
         "source": _src(), "confidence": "reported"},
        {"key": "household", "value": household, "source": _src("document", ref="statement.pdf"),
         "confidence": "reported"},
        {"key": "plan.resources", "value": {"currency": "MXN", "available_capital": 450000, "cash_available": 150000,
                                            "monthly_essentials": 40000, "reserve_months": 6,
                                            "reserve_outside_pool": 0, "debt_payments_from_pool": 0},
         "source": _src(), "confidence": "reported"},
        {"key": "goals", "value": [
            {"id": "school", "name": "School fees", "currency": "MXN", "due": (TODAY + timedelta(days=200)).isoformat(),
             "target_amount": 120000, "funded_outside_pool": 0, "protect_now": True},
            {"id": "house", "name": "Beach house", "timing": "someday"},
        ], "source": _src(), "confidence": "reported"},
        {"key": "preference.esg", "value": "Avoid tobacco", "source": _src(observed=old), "confidence": "reported",
         "expires_on": (old + timedelta(days=30)).isoformat()},
        {"key": "constraint.leverage", "value": "No borrowing to invest", "source": _src(), "confidence": "reported"},
        {"key": "performance.history", "value": {
            "currency": "MXN", "valuations": valuations,
            "cash_flows": [{"date": (history_start + timedelta(days=150)).isoformat(), "amount": 30000},
                           {"date": (history_start + timedelta(days=350)).isoformat(), "amount": 50000}],
            "benchmark": {"name": "IPC + S&P blend", "values": [
                {"date": (history_start + timedelta(days=d)).isoformat(), "value": v}
                for d, v in [(0, 100), (200, 104), (400, 109)]]}},
         "source": _src("tool", ref="wealth://import/test"), "confidence": "reported"},
    ]
    service.remember(client_id, facts, 0)
    snap = service.inspect(client_id)
    evidence = [f["id"] for f in snap["facts"] if f["key"] == "plan.resources"]
    service.propose(client_id, "Keep six months of spending in CETES", "Reserve matches essentials.",
                    snap["client"]["revision"], evidence)
    return service


# ------------------------------------------------------------------ math

def test_twr_without_flows_is_simple_return():
    assert time_weighted_return([(date(2025, 1, 1), 100), (date(2026, 1, 1), 110)], []) == pytest.approx(0.10)


def test_twr_daily_valuations_ignore_flow_size():
    # Valuation on every flow date: +10% then +10% regardless of the 1,000 deposit.
    points = [(date(2025, 1, 1), 100), (date(2025, 6, 1), 1110), (date(2026, 1, 1), 1221)]
    flows = [(date(2025, 6, 1), 1000)]
    assert time_weighted_return(points, flows) == pytest.approx(0.21)


def test_twr_modified_dietz_weights_flow_by_time():
    start, end = date(2025, 1, 1), date(2025, 1, 31)
    flow_day = start + timedelta(days=15)
    r = time_weighted_return([(start, 1000), (end, 1600)], [(flow_day, 500)])
    assert r == pytest.approx(100 / (1000 + 500 * 15 / 30))


def test_twr_needs_two_points():
    assert time_weighted_return([(date(2025, 1, 1), 100)], []) is None


def test_xirr_one_year_ten_percent():
    assert xirr([(date(2025, 1, 1), -1000), (date(2026, 1, 1), 1100)]) == pytest.approx(0.10, abs=1e-6)


def test_xirr_root_zeroes_npv_with_intermediate_flows():
    flows = [(date(2024, 1, 1), -10000), (date(2024, 7, 1), -2000), (date(2025, 3, 1), 1500), (date(2026, 1, 1), 12500)]
    rate = xirr(flows)
    npv = sum(a / (1 + rate) ** ((d - flows[0][0]).days / 365) for d, a in flows)
    assert abs(npv) < 1e-4
    assert -0.99 < rate < 1


def test_xirr_losses_and_no_root():
    assert xirr([(date(2025, 1, 1), -1000), (date(2026, 1, 1), 500)]) == pytest.approx(-0.5, abs=1e-6)
    assert xirr([(date(2025, 1, 1), 1000), (date(2026, 1, 1), 500)]) is None


# ------------------------------------------------------------------ view

def test_empty_profile_is_honest(tmp_path):
    service = WealthService(tmp_path / "w.db")
    service.create("new", "New")
    view = profile_view(service, "new")
    json.dumps(view)
    assert view["overview"] == {"status": "empty"}
    assert view["performance"]["status"] == "insufficient"
    assert view["completeness"]["known"] == []
    assert {g["id"] for g in view["memory"]} == {"money_in", "money_out", "own", "owe", "goals", "invest", "about"}
    assert all(g["entries"] == [] and g["missing"] for g in view["memory"])
    assert len(view["completeness"]["missing"]) == 10
    assert view["upcoming"] == []


def test_mexico_household_overview(tmp_path):
    service = seed_mexico(tmp_path / "w.db")
    view = profile_view(service, "mx")
    json.dumps(view)
    ov = view["overview"]
    assert view["reporting_currency"] == "MXN"
    # 12,000 USD at 18 = 216,000 MXN + 450,000 MXN positions - 80,000 debt.
    assert ov["known_assets"] == pytest.approx(666000)
    assert ov["net_worth"] == pytest.approx(586000)
    assert ov["known_assets"] - ov["liquid"] == pytest.approx(300000)  # retirement account
    currencies = {row["name"]: row for row in ov["allocations"]["currency"]}
    assert currencies["USD"]["native"]["amount"] == 12000
    assert currencies["USD"]["value"] == pytest.approx(216000)
    owners = {row["name"] for row in ov["allocations"]["owner"]}
    assert "USD" in currencies and currencies["MXN"]["native"] is None
    assert owners == {"Ana", "Luis"}
    aapl = next(r for r in ov["lookthrough"]["rows"] if r["instrument"] == "AAPL")
    assert aapl["direct"] == pytest.approx(36000)
    assert aapl["via_funds"] == pytest.approx(180000 * 0.07)
    assert ov["lookthrough"]["through_funds"] == pytest.approx(180000)
    assert ov["top_positions"][0]["symbol"] == "AFORE"
    voo = next(p for p in ov["top_positions"] if p["symbol"] == "VOO")
    assert voo["native"] == {"amount": 10000, "currency": "USD"}


def test_performance_and_benchmark(tmp_path):
    service = seed_mexico(tmp_path / "w.db")
    perf = profile_view(service, "mx")["performance"]
    assert perf["status"] == "ready"
    assert perf["days"] == 400
    assert perf["twr"] is not None and perf["twr_annualized"] is not None
    assert perf["irr_annualized"] is not None
    assert perf["benchmark"]["return"] == pytest.approx(0.09)
    assert perf["series"][0]["index"] == 100
    assert perf["flow_count"] == 2 and perf["net_flows"] == 80000


def test_performance_requires_known_flows(tmp_path):
    service = WealthService(tmp_path / "w.db")
    service.create("c", "C")
    service.remember("c", [{"key": "performance.history", "value": {"valuations": [
        {"date": "2025-01-01", "value": 1}, {"date": "2025-06-01", "value": 2}]},
        "source": _src(), "confidence": "reported"}], 0)
    perf = profile_view(service, "c")["performance"]
    assert perf == {"status": "insufficient", "reason": "flows_unknown", "statements": 2}


def _goals(service, client_id):
    from wealth.profile import goals_view
    return goals_view(service.inspect(client_id), TODAY)


def test_goals_unknowns_are_not_zero(tmp_path):
    service = seed_mexico(tmp_path / "w.db")
    goals = {g["id"]: g for g in _goals(service, "mx")["items"]}
    assert goals["house"]["target"] is None
    assert goals["house"]["funded_ratio"] is None and goals["house"]["reserved"] is None
    assert goals["house"]["timing"] == "someday"
    # An incomplete goal withholds the plan, so pool funding stays unknown.
    assert goals["school"]["funded_ratio"] is None
    assert _goals(service, "mx")["plan_status"] == "incomplete"
    rows = next(g for g in profile_view(service, "mx")["memory"] if g["id"] == "goals")["entries"]
    assert {r["field"]: r["funded_ratio"] for r in rows if r["editor"] == "goal"} == {"school": None, "house": None}


def test_goal_funded_ratio_from_complete_plan(tmp_path):
    from wealth.agent import seed_demo
    seed_demo(tmp_path / "d.db", "demo")
    goals = _goals(WealthService(tmp_path / "d.db"), "demo")
    assert goals["plan_status"] == "ready"
    assert goals["items"][0]["funded_ratio"] == 1.0
    assert goals["items"][0]["reserved_basis"] == "plan_reserved"


def _rows(view):
    return {g["id"]: g for g in view["memory"]}


def test_memory_is_grouped_trimmed_and_value_first(tmp_path):
    service = seed_mexico(tmp_path / "w.db")
    view = profile_view(service, "mx")
    groups = _rows(view)
    assert list(groups) == ["money_in", "money_out", "own", "owe", "goals", "invest", "about"]
    assert groups["money_in"]["headline"] == {"amount": 85000.0, "currency": "MXN"}
    assert groups["own"]["headline"] == {"amount": 666000.0, "currency": "MXN"}
    assert groups["goals"]["headline"] == {"count": 2}  # goals only, not their funding rows
    income = groups["money_in"]["entries"][0]
    # Only what the row renders: no review dates, confidence, ids or refs.
    assert set(income) <= {"id", "key", "field", "label", "value", "editor", "source", "observed_on",
                           "currency", "stale", "can_confirm", "unconfirmed", "funded_ratio"}
    assert income["value"] == {"amount": 85000, "currency": "MXN"} and income["source"] == "you_said"
    esg = next(e for e in groups["invest"]["entries"] if e["key"] == "preference.esg")
    assert esg["stale"] is True and esg["can_confirm"] is True
    assert groups["invest"]["entries"][0] is esg  # stale facts lead their group
    # What you own lists accounts only, and its total is the sum of those rows.
    own = groups["own"]["entries"]
    assert [e["label"] for e in own] == ["Retirement (Afore)", "US brokerage", "Brokerage MX"]
    assert all(e["key"] == "household" and e["derived"] and e["source"] == "document" for e in own)
    assert sum(e["value"]["amount"] for e in own) == groups["own"]["headline"]["amount"]
    # What you owe shows the debt behind its total.
    assert [(e["label"], e["value"]["amount"]) for e in groups["owe"]["entries"]] == [("Car", 80000.0)]
    assert groups["owe"]["headline"] == {"amount": 80000.0, "currency": "MXN"}
    # Planning resources sit with goals, after the goals, and never beside holdings.
    goal_rows = groups["goals"]["entries"]
    assert [e["editor"] for e in goal_rows[:2]] == ["goal", "goal"]
    assert {e["field"] for e in goal_rows[2:]} == {"available_capital", "cash_available", "reserve_months"}
    assert esg["label"] == "ESG"
    resources = {e["field"] for g in view["memory"] for e in g["entries"] if e["key"] == "plan.resources"}
    assert "reserve_outside_pool" not in resources and "currency" not in resources
    assert not any(e["key"] == "performance.history" for g in view["memory"] for e in g["entries"])
    assert groups["invest"]["missing"] == ["risk"]
    assert view["completeness"] == {"known": ["income", "spending", "savings", "investments", "debts",
                                              "dependents", "tax_residence", "currencies", "goals"],
                                    "missing": ["risk"]}
    # Coming up holds only what rows do not already show: no stale facts, no goal dates.
    types = [(u["type"], u.get("key") or u.get("label")) for u in view["upcoming"]]
    assert types[0] == ("decision", "Keep six months of spending in CETES")
    assert ("review", "household") in types
    assert not any(k == "preference.esg" or t == "goal" for t, k in types)
    assert "reporting_currency" not in {e["field"] for g in view["memory"] for e in g["entries"]}


def test_fact_detail_on_tap(tmp_path):
    from wealth.profile import fact_detail
    service = seed_mexico(tmp_path / "w.db")
    detail = fact_detail(service, "mx", "preference.esg")
    assert detail["stale"] is True and detail["confidence"] == "reported"
    assert detail["source"] == {"label": "you_said", "ref": "test", "observed_on": (TODAY - timedelta(days=500)).isoformat()}
    assert detail["review_on"] and detail["revisions"] == 1
    with pytest.raises(LookupError):
        fact_detail(service, "mx", "nope")


# ------------------------------------------------------------------ writes

def _write(service, facts):
    rev = service.inspect("mx")["client"]["revision"]
    return service.remember("mx", facts, rev)


def test_confirm_edit_delete_round_trip(tmp_path):
    service = seed_mexico(tmp_path / "w.db")
    snap = service.inspect("mx")
    _write(service, fact_action(snap, "preference.esg", "confirm"))
    esg = next(e for g in profile_view(service, "mx")["memory"] for e in g["entries"]
               if e["key"] == "preference.esg")
    assert "stale" not in esg
    assert service.inspect("mx", key="preference.esg")["facts"][0]["confidence"] == "confirmed"

    _write(service, fact_action(service.inspect("mx"), "client.profile", "edit", field="monthly_income",
                                value={"amount": "90000", "currency": "mxn"}))
    profile = service.inspect("mx", key="client.profile")["facts"][0]["value"]
    assert profile["monthly_income"] == {"amount": 90000, "currency": "MXN"}
    assert profile["dependents"] == 1  # untouched fields survive the merge

    _write(service, fact_action(service.inspect("mx"), "client.profile", "delete", field="dependents"))
    assert "dependents" not in service.inspect("mx", key="client.profile")["facts"][0]["value"]

    _write(service, fact_action(service.inspect("mx"), "goals", "edit", field="house",
                                value={"target_amount": "2500000", "currency": "MXN"}))
    _write(service, fact_action(service.inspect("mx"), "goals", "delete", field="school"))
    goals = service.inspect("mx", key="goals")["facts"][0]["value"]
    assert [g["id"] for g in goals] == ["house"] and goals[0]["target_amount"] == 2500000

    _write(service, fact_action(service.inspect("mx"), "constraint.leverage", "delete"))
    keys = [e["key"] for g in profile_view(service, "mx")["memory"] for e in g["entries"]]
    assert "constraint.leverage" not in keys
    assert len(service.inspect("mx", detail="history", key="constraint.leverage")["history"]) == 2


def test_fact_action_rejects_unsupported(tmp_path):
    service = seed_mexico(tmp_path / "w.db")
    snap = service.inspect("mx")
    with pytest.raises(ValueError):
        fact_action(snap, "household", "confirm")
    with pytest.raises(ValueError):
        fact_action(snap, "household", "edit", value={})
    with pytest.raises(LookupError):
        fact_action(snap, "missing.key", "confirm")
    with pytest.raises(ValueError):
        fact_action(snap, "client.profile", "edit", field="monthly_income", value={"amount": -1, "currency": "MXN"})


def test_form_merges_and_skips_blanks(tmp_path):
    service = seed_mexico(tmp_path / "w.db")
    facts = form_facts(service.inspect("mx"), {
        "income": {"amount": "", "currency": "MXN"}, "spending": {"amount": 42000, "currency": "MXN"},
        "debts": {"amount": 0, "currency": "MXN"}, "risk": "Moderate", "currencies": "mxn, usd",
        "goals": {"name": "Beach house", "target_amount": "", "currency": "MXN"}})
    _write(service, facts)
    profile = service.inspect("mx", key="client.profile")["facts"][0]["value"]
    assert profile["monthly_income"] == {"amount": 85000, "currency": "MXN"}  # blank did not overwrite
    assert profile["monthly_spending"] == {"amount": 42000, "currency": "MXN"}
    assert profile["debts"] == {"amount": 0, "currency": "MXN"}  # an explicit zero is a stated fact
    assert profile["currencies"] == ["MXN", "USD"]
    goals = service.inspect("mx", key="goals")["facts"][0]["value"]
    assert [g["id"] for g in goals] == ["school", "house", "beach-house"]
    assert "target_amount" not in goals[-1]
    assert profile_view(service, "mx")["completeness"]["missing"] == []
    assert form_facts(service.inspect("mx"), {"income": None}) == []
    with pytest.raises(ValueError):
        form_facts(service.inspect("mx"), {"salary": 1})
