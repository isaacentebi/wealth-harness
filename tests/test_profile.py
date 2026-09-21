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
    assert {g["id"] for g in view["groups"]} == {"money_in", "money_out", "own", "owe", "goals", "invest", "about"}
    assert all(g["entries"] == [] and g["missing"] for g in view["groups"])
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
    rows = next(g for g in profile_view(service, "mx")["groups"] if g["id"] == "goals")["entries"]
    assert {r["field"]: r["funded_ratio"] for r in rows if r["editor"] == "goal"} == {"school": None, "house": None}


def test_goal_funded_ratio_from_complete_plan(tmp_path):
    from wealth.agent import seed_demo
    seed_demo(tmp_path / "d.db", "demo")
    goals = _goals(WealthService(tmp_path / "d.db"), "demo")
    assert goals["plan_status"] == "ready"
    assert goals["items"][0]["funded_ratio"] == 1.0
    assert goals["items"][0]["reserved_basis"] == "plan_reserved"


def _rows(view):
    return {g["id"]: g for g in view["groups"]}


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
    resources = {e["field"] for g in view["groups"] for e in g["entries"] if e["key"] == "plan.resources"}
    assert "reserve_outside_pool" not in resources and "currency" not in resources
    assert not any(e["key"] == "performance.history" for g in view["groups"] for e in g["entries"])
    assert groups["invest"]["missing"] == ["risk"]
    assert view["completeness"] == {"known": ["income", "spending", "savings", "investments", "debts",
                                              "dependents", "tax_residence", "currencies", "goals"],
                                    "missing": ["risk"]}
    # Coming up holds only what rows do not already show: no stale facts, no goal dates.
    types = [(u["type"], u.get("key") or u.get("label")) for u in view["upcoming"]]
    assert types[0] == ("decision", "Keep six months of spending in CETES")
    assert ("review", "household") in types
    assert not any(k == "preference.esg" or t == "goal" for t, k in types)
    assert "reporting_currency" not in {e["field"] for g in view["groups"] for e in g["entries"]}


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
    esg = next(e for g in profile_view(service, "mx")["groups"] for e in g["entries"]
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
    keys = [e["key"] for g in profile_view(service, "mx")["groups"] for e in g["entries"]]
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


# ------------------------------------------------------------------ memory as sentences

def _live(tmp_path, facts=None, statement=True):
    """The 2026-09-21 live session (legacy shapes) plus a confirmed GBM statement."""
    import os
    from tests.fixtures.ingest import statements
    from tests.test_situation import LIVE, _client
    from wealth.service import upload_dir
    service = _client(tmp_path, LIVE if facts is None else facts)
    if statement:
        os.environ.pop("WEALTH_UPLOAD_DIR", None)
        folder = upload_dir("ana", service.db_path)
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "gbm.pdf").write_bytes(statements.gbm_multicurrency())
        proposal = service.ingest("ana", "file", {"path": "gbm.pdf"})
        service.ingest("ana", "confirm", {"proposal_id": proposal["result"]["proposal_id"]})
    return service


def _spans(item):
    return [item["text"][a:b] for a, b in item["emphasis"]]


def test_memory_reads_as_grouped_sentences_with_quiet_origins(tmp_path, monkeypatch):
    monkeypatch.delenv("WEALTH_UPLOAD_DIR", raising=False)
    view = profile_view(_live(tmp_path), "ana", language="es")
    assert set(view["memory"]) == {"language", "es"} and view["memory"]["language"] == "es"
    memory = view["memory"]["es"]
    groups = {g["id"]: g for g in memory["groups"]}
    assert list(groups) == ["money_in", "money_out", "own", "owe", "goals", "invest", "about"]
    assert groups["money_in"]["summary"]["text"] == "Te sobran unos $40,000 al mes, sin contar el pago del coche."
    assert _spans(groups["money_in"]["summary"]) == ["$40,000"]
    assert groups["owe"]["summary"]["text"] == "Aún no sé cuánto pagas del coche al mes."
    assert groups["goals"]["summary"]["text"] == "Apartas $10,000 al mes para tus metas; te quedan $30,000 libres."
    own = [f["text"] for f in groups["own"]["facts"]]
    # One sentence per institution, whole pesos, no parenthetical dates, no difference sentence.
    assert own == ["Tienes $150,000 en efectivo en Nu.", "En GBM tienes $217,837 y $1,000 USD en tu cuenta de inversión."]
    gbm = groups["own"]["facts"][1]
    assert _spans(gbm) == ["$217,837", "$1,000 USD"]
    assert gbm["origin"] == {"kind": "statement", "institution": "GBM", "as_of": "2026-08-31"}
    assert gbm["edit"] is None and gbm["forget"] is None  # statement values change with a statement
    cash = groups["own"]["facts"][0]
    assert cash["origin"] == {"kind": "said"} and cash["edit"] == {
        "field": "cash:cash0", "kind": "amount", "amount": 150000, "currency": "MXN"}
    assert [f["text"] for f in groups["invest"]["facts"]] == [
        "Tu posición más grande es el Udibono 2035: 36% de lo que tienes invertido.",
        "Tienes el S&P 500 dos veces: a través de CSPX y IVV."]
    # The contradiction is one card, worded from the person's side, with the statement's figure ready.
    (card,) = memory["conflicts"]
    assert card["text"] == "Dijiste unos $200,000 en GBM; tu estado de cuenta dice $236,087."
    assert card["use_statement"] == {"field": "investments:investment0", "amount": 236087, "currency": "MXN", "wrap": None}
    assert memory["review"] == [] and "dependents" in memory["missing"]
    en = profile_view(_live(tmp_path / "en"), "ana")["memory"]
    assert set(en) == {"language", "en", "es"}
    assert en["en"]["conflicts"][0]["text"] == "You said about $200,000 at GBM; your statement says $236,087."


def test_memory_edits_and_forgets_legacy_items_in_place(tmp_path, monkeypatch):
    monkeypatch.delenv("WEALTH_UPLOAD_DIR", raising=False)
    service = _live(tmp_path)
    memory = profile_view(service, "ana", language="en")["memory"]["en"]
    cash = next(f for g in memory["groups"] for f in g["facts"] if f["text"].startswith("You have $150,000"))
    snap = service.inspect("ana")
    facts = fact_action(snap, cash["key"], "edit", field=cash["edit"]["field"], value={"amount": 160000, "currency": "MXN"})
    service.remember("ana", facts, snap["client"]["revision"])
    assert service.situation("ana")["cash"][0]["amount"] == 160000
    # "Use the statement" rewrites only the stated GBM figure and retires the card.
    card = profile_view(service, "ana", language="en")["memory"]["en"]["conflicts"][0]
    use = card["use_statement"]
    snap = service.inspect("ana")
    service.remember("ana", fact_action(snap, card["key"], "edit", field=use["field"],
                                        value={"amount": use["amount"], "currency": use["currency"]}),
                     snap["client"]["revision"])
    resources = service.inspect("ana", key="plan.resources")["facts"][0]["value"]
    assert resources["investments"][0]["amount"] == 236087 and "approximate" not in resources["investments"][0]
    assert resources["cash"][0]["amount"] == 160000 and resources["debts"][0]["balance"] == 60000
    assert profile_view(service, "ana", language="en")["memory"]["en"]["conflicts"] == []
    # Forget removes exactly one item.
    debt = next(f for g in profile_view(service, "ana", language="en")["memory"]["en"]["groups"] for f in g["facts"]
                if g["id"] == "owe")
    snap = service.inspect("ana")
    service.remember("ana", fact_action(snap, debt["key"], "delete", field=debt["forget"]["field"]),
                     snap["client"]["revision"])
    after = service.situation("ana")
    assert after["liabilities"] == [] and after["cash"][0]["amount"] == 160000
    with pytest.raises(LookupError):
        fact_action(service.inspect("ana"), "plan.resources", "edit", field="debts:debt0", value={"amount": 1, "currency": "MXN"})


def test_keeping_what_was_said_retires_the_card(tmp_path, monkeypatch):
    monkeypatch.delenv("WEALTH_UPLOAD_DIR", raising=False)
    service = _live(tmp_path)
    card = profile_view(service, "ana", language="es")["memory"]["es"]["conflicts"][0]
    snap = service.inspect("ana")
    service.remember("ana", fact_action(snap, card["key"], "confirm", field=card["edit"]["field"]), snap["client"]["revision"])
    assert profile_view(service, "ana", language="es")["memory"]["es"]["conflicts"] == []


def test_goal_contribution_edits_keep_the_goal_shape(tmp_path):
    from tests.test_situation import LIVE
    service = _live(tmp_path, statement=False)
    goal = next(f for g in profile_view(service, "ana", language="es")["memory"]["es"]["groups"]
                for f in g["facts"] if g["id"] == "goals")
    assert goal["edit"]["wrap"] == "monthly_contribution" and goal["edit"]["amount"] == 10000
    snap = service.inspect("ana")
    service.remember("ana", fact_action(snap, "goals", "edit", field=goal["edit"]["field"],
                                        value={"monthly_contribution": 12000, "currency": "MXN"}),
                     snap["client"]["revision"])
    saved = service.inspect("ana", key="goals")["facts"][0]["value"][0]
    assert saved["amount"] == 12000 and saved["frequency"] == "monthly" and saved["id"] == LIVE["goals"][0]["id"]
    assert service.situation("ana")["goals"][0]["monthly_contribution"] == 12000


def test_a_statement_replaces_the_estimate_without_a_card(tmp_path, monkeypatch):
    from tests.test_situation import CANONICAL
    monkeypatch.delenv("WEALTH_UPLOAD_DIR", raising=False)
    service = _live(tmp_path, CANONICAL)
    # The statement is the source of truth for the balance it covers: no card asks about the old estimate.
    assert service.contradictions("ana")["contradictions"] == []
    memory = profile_view(service, "ana", language="es")["memory"]["es"]
    assert not memory["conflicts"]
    spending = next(f for g in memory["groups"] for f in g["facts"] if g["id"] == "money_out")
    assert spending["edit"] == {"field": "total", "kind": "amount", "amount": 45000, "currency": "MXN"}
    snap = service.inspect("ana")
    patch = fact_action(snap, "spending.monthly", "edit", field="total", value={"amount": 47000, "currency": "MXN"})
    assert patch[0]["merge"] and patch[0]["value"] == {"total": 47000, "currency": "MXN", "approximate": False}


def test_stale_facts_become_at_most_three_check_ins_and_since_needs_a_real_start(tmp_path):
    service = WealthService(tmp_path / "w.db")
    service.create("s", "S")
    old = TODAY - timedelta(days=500)
    facts = [{"key": f"cash.bank{i}", "value": {"amount": 1000 * (i + 1), "currency": "MXN", "institution": f"Banco {i}"},
              "source": _src(observed=old), "confidence": "reported"} for i in range(4)]
    start = (TODAY - timedelta(days=200)).replace(day=1).isoformat()
    facts.append({"key": "income.salary", "value": {"amount": 85000, "currency": "MXN", "frequency": "monthly", "net": True},
                  "source": _src(), "confidence": "reported", "valid_from": start})
    service.remember("s", facts)
    memory = profile_view(service, "s", language="es")["memory"]["es"]
    assert len(memory["review"]) == 3 and all(i["confirm"] and i["stale"] for i in memory["review"])
    own = next(g for g in memory["groups"] if g["id"] == "own")["facts"]
    assert len(own) == 1 and own[0]["stale"]  # the fourth stays in its group, not duplicated
    salary = next(g for g in memory["groups"] if g["id"] == "money_in")["facts"][0]
    assert salary["since"] == start
    assert own[0]["since"] is None  # valid_from defaults to when it was said: no "since"
    # A guess is worded, marked as mine, and one tap confirms it.
    service.remember("s", [{"key": "liability.card", "value": {"kind": "card", "balance": 12000, "currency": "MXN"},
                            "source": _src("inference", ref="chat"), "confidence": "inferred"}],
                     service.inspect("s")["client"]["revision"])
    memory = profile_view(service, "s", language="es")["memory"]["es"]
    guess = next(g for g in memory["groups"] if g["id"] == "owe")["facts"][0]
    assert guess["text"] == "Te quedan $12,000 de la tarjeta de crédito."
    assert guess["origin"] == {"kind": "guess"} and guess["unconfirmed"] and guess["confirm"]


def test_empty_memory_has_no_groups(tmp_path):
    service = WealthService(tmp_path / "w.db")
    service.create("e", "E")
    memory = profile_view(service, "e")["memory"]
    assert memory["en"]["groups"] == [] and memory["es"]["conflicts"] == [] and memory["es"]["review"] == []


def test_government_paper_reads_as_people_say_it():
    from wealth.profile import _instrument_label
    assert _instrument_label("S UDIBONO 351122") == "Udibono 2035"
    assert _instrument_label("BI CETES 261015") == "Cetes 2026"
    assert _instrument_label("M BONO 291201") == "Bono 2029"
    assert _instrument_label("LD BONDESF 290104") == "Bondes F 2029"
    assert _instrument_label("CETES") == "Cetes"
    for ticker in ("VOO", "NAFTRAC", "S&P 500", "IVVPESO"):
        assert _instrument_label(ticker) == ticker


def test_a_pending_contradiction_is_worded_with_both_amounts(tmp_path):
    from wealth.profile import memory_view
    service = WealthService(tmp_path / "w.sqlite3")
    service.create("ana", "Ana")
    today = TODAY.isoformat()
    service.remember("ana", [{"key": "cash.nu", "value": {"amount": 150000, "currency": "MXN", "institution": "Nu"},
                              "source": {"kind": "user", "ref": "chat", "observed_on": today}}])
    receipt = service.remember("ana", [{"key": "cash.nu", "value": {"amount": 160000, "currency": "MXN", "institution": "Nu"},
                                        "source": {"kind": "web", "ref": "https://example.com/nu", "observed_on": today},
                                        "confidence": "reported"}])
    assert receipt["needs_user"]
    view = profile_view(service, "ana", language="en")
    card = next(iter(view["memory"]["en"]["conflicts"]))
    assert "$150,000" in card["text"] and "$160,000" in card["text"]
    assert [card["text"][a:b] for a, b in card["emphasis"]] == ["$150,000", "$160,000"]
